"""Tests for the cs spoke GitHub config-branch pull loop (``repo_sync.py``).

The pull loop wraps ``CSSpoke``'s existing ``_git`` helper + ``_git_lock`` +
in-memory ``_github_config``, so the tests build a real ``CSSpoke`` (isolated
config dir in tmp), drop a fake ``.git`` marker so the repo-exists guard
passes, and monkeypatch ``spoke._git`` with a recorder. Style mirrors
``test_hub_config.py`` (explicit event loop + ``_run``).
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import List, Tuple

import pytest

from command_queue import CommandQueue, CSSettings
from client_registry import ClientRegistry
from cs_spoke import CSSpoke
from repo_sync import RepoSync

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
    spoke._github_config = {"github_token": "tok", "repo_url": "u",
                            "repo_branch": "main"}
    rec = _GitRecorder(spoke._git_lock)
    spoke._git = rec  # type: ignore[assignment]
    _run(loop, spoke.repo_sync._sync_once())
    assert rec.calls == []  # no git ops in hub mode


def test_skip_when_no_token(spoke_loop):
    spoke, loop = spoke_loop
    spoke._github_config = {"repo_url": "u", "repo_branch": "main"}  # no token
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
    spoke._github_config = {"github_token": "tok", "repo_url": "u",
                            "repo_branch": "main"}
    rec = _GitRecorder(spoke._git_lock)
    spoke._git = rec  # type: ignore[assignment]
    _run(loop, spoke.repo_sync._sync_once())
    assert rec.calls == []


# ── happy path ──────────────────────────────────────────────────────────────

def test_happy_path_resets_to_origin_branch(spoke_loop):
    spoke, loop = spoke_loop
    spoke._github_config = {"github_token": "tok", "repo_url": "https://gh/r",
                            "repo_branch": "LRB"}
    rec = _GitRecorder(spoke._git_lock, outputs={"rev-parse": "abc123def456"})
    spoke._git = rec  # type: ignore[assignment]
    askpass_dir = spoke.settings.config_dir.parent
    askpass_files_before = set(askpass_dir.glob(".git-askpass-*.sh"))
    assert askpass_files_before == set()  # nothing yet

    _run(loop, spoke.repo_sync._sync_once())

    args = _git_args(rec)
    assert args[0] == ("remote", "set-url", "origin", "https://gh/r")
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
    spoke._github_config = {"github_token": "tok", "repo_url": "u"}  # no branch
    rec = _GitRecorder(spoke._git_lock)
    spoke._git = rec  # type: ignore[assignment]
    _run(loop, spoke.repo_sync._sync_once())
    args = _git_args(rec)
    assert args[3] == ("checkout", "-B", "main", "origin/main")


# ── resilience ───────────────────────────────────────────────────────────────

def test_never_raises_on_git_failure(spoke_loop):
    """A bad fetch logs a warning and returns normally — the loop must survive."""
    spoke, loop = spoke_loop
    spoke._github_config = {"github_token": "tok", "repo_url": "u",
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
    spoke._github_config = {"github_token": "tok", "repo_url": "u",
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