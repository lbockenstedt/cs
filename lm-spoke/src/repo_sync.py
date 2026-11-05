"""Periodic pull of the tenant's GitHub config branch on the cs lm-spoke.

The CS module supports a per-tenant GitHub source-of-truth: a user creates
their own branch in the simulation config repo (e.g. ``LRB`` instead of
``main``), edits simulation code / config there, and the cs spoke mirrors that
branch so the on-disk config the running sim reads reflects the tenant's
branch. The **push** path lives in ``cs_spoke._push_files_to_github`` (a WebUI
edit writes the file locally, commits, and pushes to the branch). This module
is the missing **pull** side: a background loop that periodically fetches the
tenant's branch and hard-resets the working tree onto ``origin/<branch>`` so
edits made directly on GitHub (not via the WebUI) reach the spoke, and so a
fresh install switches off ``main`` onto the tenant's branch once the hub has
delivered the tenant's ``github_config``.

Operationally:
- The loop is serialized on ``spoke._git_lock`` (whose comment explicitly
  anticipates "a config push + a repo sync"), so a sync never races the push
  path's fetch+reset+commit+push. The push path re-applies its ``file_map``
  after its own ``checkout -B``, so a sync resetting the tree first is safe.
- It is a pure pull: no files written, no commit, no push. ``checkout -B
  <branch> origin/<branch>`` hard-resets to origin — correct in GitHub mode
  (origin is the source of truth) and identical to the reset the push path
  already does. Untracked files (``hub-*-overrides.conf``, ``hub-config-source``)
  are gitignored and survive the reset, so hub overrides are not lost.
- It only acts when ``source_of_truth == "github"`` AND a token + repo_url +
  branch are configured AND ``REPO_DIR/.git`` exists. In ``source=hub`` mode it
  is a no-op (the loader ignores the repo base there, so a pull could never
  revert a hub edit anyway). Before the hub delivers ``github_config``
  (e.g. right after install), ``_github_config`` is empty → skip → repo stays
  on ``main`` until creds are saved.
- ``sim_config.load_configs`` reads ``configs/*.conf`` from disk on demand, so
  a pulled-in ``simulation.conf`` / ``user-overrides.conf`` is picked up
  naturally — no explicit reload/teardown is needed (mirrors the push path's
  local-write semantics). A pull that advances SPOKE CODE (``lm-spoke/src/``),
  however, is NOT picked up — the running process has the old Python in memory
  — so ``_sync_once`` reloads the spoke itself (``_prepare_service_restart`` +
  ``os._exit(3)``) when ``lm-spoke/src/`` changed, closing the ≤300s stale
  window the code-drift watchdog would otherwise cover. Config-only advances
  (the common case, incl. the spoke's own pushed config commits) do not exit.
- Never raises: a bad fetch logs a WARNING and the loop continues.
"""
# Allow modern type-hint syntax without evaluating annotations at import time.
from __future__ import annotations

# asyncio runs the background sync loop as a task.
import asyncio
# Standard-library logging for sync progress and failures.
import logging
# Used for environment variables, file permissions and the hard process exit.
import os
# Generates a unique name for the temporary git credential helper script.
import uuid as _uuid
# Type hints only.
from typing import Optional

# Dedicated logger so repo-sync messages can be filtered on their own.
logger = logging.getLogger("CSRepoSync")

# How often to fetch + reset onto the tenant's branch. Default 60s mirrors
# SimQuotaEngine's RECONCILE_INTERVAL_S. Override with LM_CS_REPO_SYNC_INTERVAL.
REPO_SYNC_INTERVAL_S = float(os.getenv("LM_CS_REPO_SYNC_INTERVAL") or 60.0)


class RepoSync:
    """Periodic ``git fetch --prune origin`` + ``checkout -B <branch>
    origin/<branch>`` on the tenant's GitHub config branch. No-op unless the
    effective source of truth is ``github`` AND the in-memory ``github_config``
    has a token + repo_url + branch. Mirrors the ``SimQuotaEngine`` loop shape
    (``start``/``stop``/``trigger``/``_loop``/``_sync_once``)."""

    def __init__(self, spoke) -> None:
        """Remember the owning spoke; the loop is not started until start() is called."""
        # The owning spoke; provides git helpers, settings, the git lock and github_config.
        self.spoke = spoke
        # Handle of the background loop task, once started.
        self._task: Optional[asyncio.Task] = None

    def start(self) -> None:
        """Spawn the sync loop on the running event loop. Idempotent — a
        second call is a no-op while the prior task is alive."""
        # Already running: do nothing.
        if self._task is not None and not self._task.done():
            return
        # Launch the loop as a named background task.
        self._task = asyncio.create_task(self._loop(), name="cs-repo-sync")

    def stop(self) -> None:
        """Cancel the background sync loop if it is running."""
        # Only cancel when a loop is running, then forget the task.
        if self._task is not None:
            self._task.cancel()
            self._task = None

    def trigger(self) -> None:
        """Immediate best-effort sync (on a CS_CONFIG_UPDATE that delivered
        github_config / changed config_source), so the branch switches at
        save-time rather than waiting up to REPO_SYNC_INTERVAL_S."""
        # Run one sync right now in the background.
        try:
            asyncio.create_task(self._sync_once(), name="cs-repo-sync-now")
        # No event loop is running yet; the periodic loop will sync later instead.
        except RuntimeError:
            pass  # no running loop yet — the periodic loop will catch it

    async def _loop(self) -> None:
        """Run a sync pass every REPO_SYNC_INTERVAL_S seconds until cancelled."""
        # Repeat forever until the task is cancelled.
        while True:
            try:
                # Try one fetch + reset pass.
                await self._sync_once()
            # Cancellation means shutdown: let it propagate so the task ends.
            except asyncio.CancelledError:
                raise
            # Any other error is logged; the loop keeps running.
            except Exception as exc:  # noqa: BLE001 — a bad fetch must not kill the loop
                logger.warning("repo sync failed: %s", exc)
            # Wait before the next sync pass.
            await asyncio.sleep(REPO_SYNC_INTERVAL_S)

    async def _sync_once(self) -> None:
        """Fetch the tenant's branch and reset onto it when GitHub is the source of truth.

        Restarts the spoke process if the pull changed spoke source code.
        """
        # Shorthand for the owning spoke and its id (used in log messages).
        spoke = self.spoke
        sid = getattr(spoke, "spoke_id", "cs")
        # Read the tenant's GitHub settings delivered by the hub (empty dict if none yet).
        gc = getattr(spoke, "_github_config", None) or {}
        # Token, repository URL and branch; the branch defaults to main.
        token = str(gc.get("github_token") or "").strip()
        repo_url = str(gc.get("repo_url") or "").strip()
        branch = str(gc.get("repo_branch") or "").strip() or "main"
        # The git checkout is the parent of the configs directory.
        repo_dir = spoke.settings.config_dir.parent

        # Source of truth flag — same file sim_config.load_configs reads.
        # Read the source-of-truth flag file; "hub" means the hub manages config.
        try:
            _source = (spoke.settings.config_dir / "hub-config-source").read_text(
                encoding="utf-8").strip().lower()
        # A missing/unreadable flag file means the standalone default: GitHub is the source.
        except Exception:  # noqa: BLE001
            _source = "github"

        # STANDALONE-ONLY CONTRACT: repo_sync (spoke-side GitHub pull) is the
        # no-hub fallback ONLY. Whenever a hub manages this spoke it drives
        # config_source='hub' (the hub is the sole GitHub client — it pulls,
        # commits, and fans config down; see the hub's github_config_client), so
        # this returns here and the spoke never touches GitHub. Only a genuinely
        # standalone spoke (never overridden by a hub → flag stays 'github')
        # reaches the pull below. Do NOT re-enable spoke pulls under a hub.
        # Hub-managed spoke: never pull from GitHub here (see the comment above).
        if _source != "github":
            logger.debug("repo sync[%s]: skipped (source=%s → hub-managed, standalone-only path)",
                         sid, _source)
            return
        # No token means we cannot authenticate to GitHub yet.
        if not token:
            logger.debug("repo sync[%s]: skipped (no github_token in memory)", sid)
            return
        # No repository configured yet.
        if not repo_url:
            logger.debug("repo sync[%s]: skipped (no repo_url)", sid)
            return
        # The config directory is not a git checkout, so there is nothing to sync.
        if not (repo_dir / ".git").exists():
            logger.debug("repo sync[%s]: skipped (%s is not a git repo)", sid, repo_dir)
            return

        # Unique path for a throwaway credential helper inside the repo directory.
        askpass = repo_dir / f".git-askpass-{_uuid.uuid4().hex}.sh"
        # Everything below cleans up the helper in the finally block, even on failure.
        try:
            # Write a tiny shell script git calls for credentials: it answers "x-access-token" for
            # the username and the token (read from an env var, not stored in the file) for the
            # password.
            askpass.write_text(
                "#!/bin/sh\ncase \"$1\" in\n"
                "  *Username*) printf '%s\\n' 'x-access-token' ;;\n"
                "  *) printf '%s\\n' \"$GITHUB_TOKEN\" ;;\nesac\n",
                encoding="utf-8")
            # Only the owner may read or run the helper.
            os.chmod(askpass, 0o700)
            # Environment for git: use the helper, never prompt on a terminal, and pass the token.
            push_env = {"GIT_ASKPASS": str(askpass), "GIT_TERMINAL_PROMPT": "0",
                        "GITHUB_TOKEN": token}
            # Hold the shared git lock so this never races the config push path.
            async with spoke._git_lock:
                # Make sure origin points at the configured repository.
                await spoke._git("remote", "set-url", "origin", repo_url)
                # Remember the current commit so we can tell whether the pull changed anything.
                head_before = await spoke._git("rev-parse", "HEAD")
                # Download the latest branches from GitHub, pruning deleted ones.
                await spoke._git("fetch", "--prune", "origin", env=push_env)
                # Hard-reset the local branch onto the remote branch (origin is the source of
                # truth).
                await spoke._git("checkout", "-B", branch, f"origin/{branch}")
                # Commit we are on after the reset.
                new_head = await spoke._git("rev-parse", "HEAD")
                # Reload the spoke ONLY when the pull advanced SPOKE CODE
                # (lm-spoke/src/). Config-only advances are the common case —
                # a WebUI edit the spoke itself just pushed
                # (_push_files_to_github commits configs/*.conf), or an operator
                # edit made directly on GitHub — and must NOT restart:
                # sim_config.load_configs reads configs/*.conf from disk on
                # demand, so the new config is picked up on the next
                # /api/config read. Restarting on every config advance would
                # storm the spoke (every save → reset 60s later → exit). Only a
                # src/ change needs a process reload: the running process has
                # the old Python in memory. Mirrors
                # RoleConnection._update_sibling_repo's pull+exit(3), gated on
                # a code diff so config edits stay non-disruptive. The 300s
                # code-drift watchdog (BaseControlPlane) remains the backstop.
                # The pull moved HEAD: list files changed under the spoke's source directory.
                if new_head != head_before:
                    try:
                        changed = await spoke._git(
                            "diff", "--name-only", head_before, new_head,
                            "--", "lm-spoke/src/")
                    # If the diff fails, assume no code change so we do not restart needlessly.
                    except Exception:  # noqa: BLE001 — diff failure → don't restart
                        changed = ""
                # Nothing changed at all.
                else:
                    changed = ""
            # Record which commit we are now on.
            logger.info("repo sync[%s]: reset to origin/%s @ %s",
                        sid, branch, new_head[:12])
            # Spoke code changed: the running process must restart to load it.
            if changed.strip():
                logger.info("repo sync[%s]: code changed (lm-spoke/src/) "
                            "%s → %s; reloading spoke", sid,
                            head_before[:12], new_head[:12])
                # Prepare the service restart; if allowed, flush buffered logs and exit with code 3
                # (the service manager restarts the spoke).
                if spoke._prepare_service_restart(reason="repo-sync"):
                    await spoke._flush_log_relay_async()
                    os._exit(3)
        # Any failure in this pass is logged; the loop will retry next interval.
        except Exception as exc:  # noqa: BLE001 — never let a bad fetch kill the loop
            logger.warning("repo sync[%s]: FAILED — %s", sid, exc)
        # Always delete the temporary credential helper.
        finally:
            try:
                # Ignore errors if it was never created or is already gone.
                askpass.unlink()
            except Exception:  # noqa: BLE001
                pass