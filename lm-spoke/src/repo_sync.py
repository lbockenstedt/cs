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
from __future__ import annotations

import asyncio
import logging
import os
import uuid as _uuid
from typing import Optional

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
        self.spoke = spoke
        self._task: Optional[asyncio.Task] = None

    def start(self) -> None:
        """Spawn the sync loop on the running event loop. Idempotent — a
        second call is a no-op while the prior task is alive."""
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._loop(), name="cs-repo-sync")

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None

    def trigger(self) -> None:
        """Immediate best-effort sync (on a CS_CONFIG_UPDATE that delivered
        github_config / changed config_source), so the branch switches at
        save-time rather than waiting up to REPO_SYNC_INTERVAL_S."""
        try:
            asyncio.create_task(self._sync_once(), name="cs-repo-sync-now")
        except RuntimeError:
            pass  # no running loop yet — the periodic loop will catch it

    async def _loop(self) -> None:
        while True:
            try:
                await self._sync_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — a bad fetch must not kill the loop
                logger.warning("repo sync failed: %s", exc)
            await asyncio.sleep(REPO_SYNC_INTERVAL_S)

    async def _sync_once(self) -> None:
        spoke = self.spoke
        sid = getattr(spoke, "spoke_id", "cs")
        gc = getattr(spoke, "_github_config", None) or {}
        token = str(gc.get("github_token") or "").strip()
        repo_url = str(gc.get("repo_url") or "").strip()
        branch = str(gc.get("repo_branch") or "").strip() or "main"
        repo_dir = spoke.settings.config_dir.parent

        # Source of truth flag — same file sim_config.load_configs reads.
        try:
            _source = (spoke.settings.config_dir / "hub-config-source").read_text(
                encoding="utf-8").strip().lower()
        except Exception:  # noqa: BLE001
            _source = "github"

        if _source != "github":
            logger.debug("repo sync[%s]: skipped (source=%s)", sid, _source)
            return
        if not token:
            logger.debug("repo sync[%s]: skipped (no github_token in memory)", sid)
            return
        if not repo_url:
            logger.debug("repo sync[%s]: skipped (no repo_url)", sid)
            return
        if not (repo_dir / ".git").exists():
            logger.debug("repo sync[%s]: skipped (%s is not a git repo)", sid, repo_dir)
            return

        askpass = repo_dir / f".git-askpass-{_uuid.uuid4().hex}.sh"
        try:
            askpass.write_text(
                "#!/bin/sh\ncase \"$1\" in\n"
                "  *Username*) printf '%s\\n' 'x-access-token' ;;\n"
                "  *) printf '%s\\n' \"$GITHUB_TOKEN\" ;;\nesac\n",
                encoding="utf-8")
            os.chmod(askpass, 0o700)
            push_env = {"GIT_ASKPASS": str(askpass), "GIT_TERMINAL_PROMPT": "0",
                        "GITHUB_TOKEN": token}
            async with spoke._git_lock:
                await spoke._git("remote", "set-url", "origin", repo_url)
                head_before = await spoke._git("rev-parse", "HEAD")
                await spoke._git("fetch", "--prune", "origin", env=push_env)
                await spoke._git("checkout", "-B", branch, f"origin/{branch}")
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
                if new_head != head_before:
                    try:
                        changed = await spoke._git(
                            "diff", "--name-only", head_before, new_head,
                            "--", "lm-spoke/src/")
                    except Exception:  # noqa: BLE001 — diff failure → don't restart
                        changed = ""
                else:
                    changed = ""
            logger.info("repo sync[%s]: reset to origin/%s @ %s",
                        sid, branch, new_head[:12])
            if changed.strip():
                logger.info("repo sync[%s]: code changed (lm-spoke/src/) "
                            "%s → %s; reloading spoke", sid,
                            head_before[:12], new_head[:12])
                if spoke._prepare_service_restart(reason="repo-sync"):
                    await spoke._flush_log_relay_async()
                    os._exit(3)
        except Exception as exc:  # noqa: BLE001 — never let a bad fetch kill the loop
            logger.warning("repo sync[%s]: FAILED — %s", sid, exc)
        finally:
            try:
                askpass.unlink()
            except Exception:  # noqa: BLE001
                pass