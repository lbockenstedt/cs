"""Tests for the cs spoke GitHub config-branch pull loop (``repo_sync.py``).

The pull loop wraps ``CSSpoke``'s existing ``_git`` helper + ``_git_lock`` +
in-memory ``_github_config``, so the tests build a real ``CSSpoke`` (isolated
config dir in tmp), drop a fake ``.git`` marker so the repo-exists guard
passes, and monkeypatch ``spoke._git`` with a recorder. Style mirrors
``test_hub_config.py`` (explicit event loop + ``_run``).
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import List, Tuple

import pytest

from command_queue import CommandQueue, CSSettings
from client_registry import ClientRegistry
from cs_spoke import CSSpoke
from repo_sync import RepoSync, github_remote

CONFIGS = Path(__file__).resolve().parent.parent.parent / "configs"


def _make_spoke(data_dir: Path, config_dir: Path) -> "tuple[CSSpoke, asyncio.AbstractEventLoop]":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    s = CSSpoke("test-cs", {})
    s.settings = CSSettings(data_dir, config_dir)
    s.registry = ClientRegistry(data_dir)
    s.queue = CommandQueue(data_dir, s.settings)
    return s, loop


def _run(loop, coro):
    return loop.run_until_complete(coro)


@pytest.fixture
def spoke_loop(tmp_path):
    """A CSSpoke whose config_dir lives under tmp; repo_dir = tmp_path (parent).
    A ``.git`` marker is created so the repo-exists guard passes by default
    (individual tests may delete it). Yields ``(spoke, loop)``."""
    data = tmp_path / "data"
    configs = tmp_path / "configs"
    data.mkdir()
    configs.mkdir()
    (tmp_path / ".git").mkdir()  # repo_dir = tmp_path has a .git → guard passes
    s, loop = _make_spoke(data, configs)
    try:
        yield s, loop
    finally:
        s.repo_sync.stop()
        loop.close()
        asyncio.set_event_loop(None)


class _GitRecorder:
    """Fake ``spoke._git``: records (args, env) tuples, asserts each call runs
    while ``spoke._git_lock`` is held, and can be programmed to raise on a
    specific sub-command (arg[0])."""

    def __init__(self, lock: asyncio.Lock, *, raise_on: str = "",
                 outputs: "dict | None" = None) -> None:
        self.calls: List[Tuple[tuple, "dict | None"]] = []
        self._lock = lock
        self._raise_on = raise_on
        self._outputs = outputs or {}

    async def __call__(self, *args: str, timeout: float = 120.0,
                       env=None) -> str:
        assert self._lock.locked(), (
            f"_git({args}) called OUTSIDE _git_lock — sync must hold the lock")
        self.calls.append((args, env))
        if self._raise_on and args and args[0] == self._raise_on:
            raise RuntimeError(f"git {' '.join(args)} failed: simulated")
        return self._outputs.get(args[0] if args else "", "deadbeef")


def _git_args(recorder: _GitRecorder) -> List[tuple]:
    return [c[0] for c in recorder.calls]


# ── skip cases ──────────────────────────────────────────────────────────────

def test_skip_when_source_is_hub(spoke_loop):
    spoke, loop = spoke_loop
    (spoke.settings.config_dir / "hub-config-source").write_text("hub", "utf-8")
    spoke._github_config = {"github_token": "tok", "repo_url": "acme/sims",
                            "repo_branch": "main"}
    rec = _GitRecorder(spoke._git_lock)
    spoke._git = rec  # type: ignore[assignment]
    _run(loop, spoke.repo_sync._sync_once())
    assert rec.calls == []  # no git ops in hub mode


def test_skip_when_no_token(spoke_loop):
    spoke, loop = spoke_loop
    spoke._github_config = {"repo_url": "acme/sims", "repo_branch": "main"}  # no token
    rec = _GitRecorder(spoke._git_lock)
    spoke._git = rec  # type: ignore[assignment]
    _run(loop, spoke.repo_sync._sync_once())
    assert rec.calls == []


def test_skip_when_no_repo_url(spoke_loop):
    spoke, loop = spoke_loop
    spoke._github_config = {"github_token": "tok", "repo_branch": "main"}  # no url
    rec = _GitRecorder(spoke._git_lock)
    spoke._git = rec  # type: ignore[assignment]
    _run(loop, spoke.repo_sync._sync_once())
    assert rec.calls == []


def test_skip_when_not_a_git_repo(spoke_loop):
    spoke, loop = spoke_loop
    (spoke.settings.config_dir.parent / ".git").rmdir()  # remove the marker
    spoke._github_config = {"github_token": "tok", "repo_url": "acme/sims",
                            "repo_branch": "main"}
    rec = _GitRecorder(spoke._git_lock)
    spoke._git = rec  # type: ignore[assignment]
    _run(loop, spoke.repo_sync._sync_once())
    assert rec.calls == []


# ── happy path ──────────────────────────────────────────────────────────────

def test_happy_path_resets_to_origin_branch(spoke_loop):
    spoke, loop = spoke_loop
    spoke._github_config = {"github_token": "tok", "repo_url": "https://github.com/acme/sims",
                            "repo_branch": "LRB"}
    rec = _GitRecorder(spoke._git_lock, outputs={"rev-parse": "abc123def456"})
    spoke._git = rec  # type: ignore[assignment]
    askpass_dir = spoke.settings.config_dir.parent
    askpass_files_before = set(askpass_dir.glob(".git-askpass-*.sh"))
    assert askpass_files_before == set()  # nothing yet

    _run(loop, spoke.repo_sync._sync_once())

    args = _git_args(rec)
    assert args[0] == ("remote", "set-url", "origin", "https://github.com/acme/sims.git")
    # HEAD is captured BEFORE the fetch/checkout (head_before), so the sync can
    # tell afterwards whether the pull advanced lm-spoke/src/ and needs a
    # reload — see repo_sync._sync_once / commit 018b60e.
    assert args[1] == ("rev-parse", "HEAD")
    assert args[2] == ("fetch", "--prune", "origin")
    # fetch must carry the GITHUB_TOKEN env (askpass feed)
    assert rec.calls[2][1] is not None
    assert rec.calls[2][1].get("GITHUB_TOKEN") == "tok"
    assert args[3] == ("checkout", "-B", "LRB", "origin/LRB")
    assert args[4] == ("rev-parse", "HEAD")
    # askpass script was created and cleaned up (no leftover in repo_dir)
    assert set(askpass_dir.glob(".git-askpass-*.sh")) == set()


def test_branch_defaults_to_main(spoke_loop):
    spoke, loop = spoke_loop
    spoke._github_config = {"github_token": "tok", "repo_url": "acme/sims"}  # no branch
    rec = _GitRecorder(spoke._git_lock)
    spoke._git = rec  # type: ignore[assignment]
    _run(loop, spoke.repo_sync._sync_once())
    args = _git_args(rec)
    assert args[3] == ("checkout", "-B", "main", "origin/main")


# ── resilience ───────────────────────────────────────────────────────────────

def test_never_raises_on_git_failure(spoke_loop):
    """A bad fetch logs a warning and returns normally — the loop must survive."""
    spoke, loop = spoke_loop
    spoke._github_config = {"github_token": "tok", "repo_url": "acme/sims",
                            "repo_branch": "main"}
    rec = _GitRecorder(spoke._git_lock, raise_on="fetch")
    spoke._git = rec  # type: ignore[assignment]
    # No exception should escape _sync_once (it's caught inside).
    _run(loop, spoke.repo_sync._sync_once())
    # The fetch call was attempted (and failed), nothing after it. A
    # rev-parse HEAD (head_before) now runs between remote + fetch.
    attempted = [a[0][0] for a in rec.calls]
    assert attempted == ["remote", "rev-parse", "fetch"]
    # askpass still cleaned up despite the failure path
    assert set(spoke.settings.config_dir.parent.glob(".git-askpass-*.sh")) == set()


def test_trigger_schedules_an_immediate_sync(spoke_loop):
    """trigger() schedules a one-shot _sync_once task on the running loop."""
    spoke, loop = spoke_loop
    spoke._github_config = {"github_token": "tok", "repo_url": "acme/sims",
                            "repo_branch": "main"}
    rec = _GitRecorder(spoke._git_lock)
    spoke._git = rec  # type: ignore[assignment]
    # trigger() uses asyncio.create_task → needs a running loop; run inside the loop.
    async def _go():
        spoke.repo_sync.trigger()
        # let the scheduled task run to completion
        await asyncio.sleep(0)
        await asyncio.sleep(0)
    _run(loop, _go())
    assert len(rec.calls) > 0  # a sync actually fired
    spoke.repo_sync.stop()

# ── repo_url / branch validation ─────────────────────────────────────────────

@pytest.mark.parametrize("url", [
    "https://github.com/acme/sims", "https://github.com/acme/sims.git",
    "https://GitHub.com/acme/sims/", "git@github.com:acme/sims.git", "acme/sims",
])
def test_github_remote_accepts_github_forms(url):
    """The forms the hub accepts all become one canonical https origin."""
    assert github_remote({"repo_url": url, "repo_branch": "LRB/dev"}) == (
        "https://github.com/acme/sims.git", "LRB/dev")


@pytest.mark.parametrize("url,branch", [
    ("https://evil.example/acme/sims", "main"),       # token would go to another host
    ("http://github.com/acme/sims", "main"),           # cleartext
    ("https://x:tok@github.com/acme/sims", "main"),    # userinfo
    ("https://github.com.evil.example/acme/sims", "main"),
    ("https://github.com:8443/acme/sims", "main"),
    ("https://github.com/acme/sims/tree/main", "main"),
    ("ssh://git@github.com/acme/sims", "main"),
    ("file:///tmp/repo", "main"),
    ("/tmp/repo", "main"),
    ("--upload-pack=touch /tmp/x", "main"),
    ("https://[::1/acme/sims", "main"),                # malformed: no crash
    ("acme/sims", "--orphan"),
    ("acme/sims", "a..b"),
    ("acme/sims", "main.lock"),
    ("acme/sims", "foo.lock/bar"),                     # any component, not just the last
    ("acme/sims", "foo/bar.lock/baz"),
    ("acme/sims", "a//b"),
    ("acme/sims", "a/.b"),
    ("acme/sims", "a/"),
    ("acme/sims", "a."),
    ("acme/sims", "a b"),
    ("acme/sims", "HEAD@{1}"),
])
def test_github_remote_rejects_other_hosts_and_bad_branches(url, branch):
    assert github_remote({"repo_url": url, "repo_branch": branch}) is None


def test_sync_skips_non_github_repo_url(spoke_loop):
    """A repo_url on another host never reaches git, so the token is never
    offered to it and its branch is never checked out over the spoke code."""
    spoke, loop = spoke_loop
    spoke._github_config = {"github_token": "tok",
                            "repo_url": "https://evil.example/acme/sims",
                            "repo_branch": "main"}
    rec = _GitRecorder(spoke._git_lock)
    spoke._git = rec  # type: ignore[assignment]
    _run(loop, spoke.repo_sync._sync_once())
    assert rec.calls == []


def test_push_skips_non_github_repo_url(spoke_loop):
    """The WebUI push path applies the same check before touching git."""
    spoke, loop = spoke_loop
    spoke._github_config = {"github_token": "tok",
                            "repo_url": "https://evil.example/acme/sims",
                            "repo_branch": "main"}
    rec = _GitRecorder(spoke._git_lock)
    spoke._git = rec  # type: ignore[assignment]
    assert _run(loop, spoke._push_files_to_github({"configs/x.conf": "a"}, "m")) is False
    assert rec.calls == []


def test_push_uses_canonical_origin(spoke_loop):
    """A valid repo_url is pushed through the rebuilt https://github.com origin."""
    spoke, loop = spoke_loop
    spoke._github_config = {"github_token": "tok", "repo_url": "git@github.com:acme/sims.git",
                            "repo_branch": "LRB"}
    rec = _GitRecorder(spoke._git_lock, raise_on="fetch")
    spoke._git = rec  # type: ignore[assignment]
    _run(loop, spoke._push_files_to_github({"configs/x.conf": "a"}, "m"))
    assert ("remote", "set-url", "origin", "https://github.com/acme/sims.git") in _git_args(rec)


@pytest.mark.parametrize("url", [
    "https://x:ghp_SECRET123@github.com/acme/sims",
    "https://ghp_SECRET123@evil.example/acme/sims",
    "https://0123456789abcdefSECRET123/acme/sims",     # token pasted as the host
    "https://secret123abcdef.example/acme/sims",
    "git@ghp_SECRET123:acme/sims",
])
def test_rejected_repo_url_is_never_logged(spoke_loop, caplog, url):
    """A token inside a rejected repo_url never reaches a log line (rule 5.2):
    both the sync loop and the push path log only why it was rejected."""
    spoke, loop = spoke_loop
    spoke._github_config = {"github_token": "tok", "repo_url": url,
                            "repo_branch": "main"}
    spoke._git = _GitRecorder(spoke._git_lock)  # type: ignore[assignment]
    caplog.set_level(logging.DEBUG)
    _run(loop, spoke.repo_sync._sync_once())
    _run(loop, spoke._push_files_to_github({"configs/x.conf": "a"}, "m"))
    assert "is not a github.com owner/repo" in caplog.text
    assert "SECRET123" not in caplog.text.upper()


def test_rejected_branch_is_named_in_the_log(spoke_loop, caplog):
    """When only the branch is wrong, the log names the branch."""
    spoke, loop = spoke_loop
    spoke._github_config = {"github_token": "tok", "repo_url": "acme/sims",
                            "repo_branch": "a..b"}
    spoke._git = _GitRecorder(spoke._git_lock)  # type: ignore[assignment]
    caplog.set_level(logging.DEBUG)
    _run(loop, spoke.repo_sync._sync_once())
    assert "branch 'a..b' is not a plain branch name" in caplog.text


def test_rejection_warns_again_after_a_valid_config(spoke_loop, caplog):
    """A rejected value logs at WARNING once per run of rejections: after a
    valid config, the same bad value warns again instead of dropping to DEBUG."""
    spoke, loop = spoke_loop
    spoke._git = _GitRecorder(spoke._git_lock)  # type: ignore[assignment]
    bad = {"github_token": "tok", "repo_url": "acme/sims", "repo_branch": "a..b"}
    good = {"github_token": "tok", "repo_url": "acme/sims", "repo_branch": "main"}
    caplog.set_level(logging.DEBUG)
    for cfg in (bad, bad, good, bad):
        spoke._github_config = dict(cfg)
        _run(loop, spoke.repo_sync._sync_once())
    warnings = [r for r in caplog.records
                if r.levelno == logging.WARNING and "a..b" in r.getMessage()]
    assert len(warnings) == 2


@pytest.mark.parametrize("gap", [{"github_token": ""}, {"repo_url": ""}])
def test_rejection_warns_again_after_sync_was_not_configured(spoke_loop, caplog, gap):
    """A state where sync does not run (no token, no repo_url) also ends a run
    of rejections, so the same bad value warns again when it comes back."""
    spoke, loop = spoke_loop
    spoke._git = _GitRecorder(spoke._git_lock)  # type: ignore[assignment]
    bad = {"github_token": "tok", "repo_url": "acme/sims", "repo_branch": "a..b"}
    caplog.set_level(logging.DEBUG)
    for cfg in (bad, {**bad, **gap}, bad):
        spoke._github_config = dict(cfg)
        _run(loop, spoke.repo_sync._sync_once())
    warnings = [r for r in caplog.records
                if r.levelno == logging.WARNING and "a..b" in r.getMessage()]
    assert len(warnings) == 2
