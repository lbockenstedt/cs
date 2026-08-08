"""Tests for ClientRegistry.apply_status's gateway-reachability stability
latch (gateway_state / gateway_state_since / gateway_confirmed_down).

sim_quota_engine reads gateway_confirmed_down to decide whether a client is
genuinely non-working (e.g. a detached T2 dongle) vs a normal per-loop-
iteration flicker. The latch must only flip after a full continuous
_GATEWAY_CONFIRM_S (60 min) streak in either direction — never on a single
reading — so these tests drive apply_status across simulated time via a
monkeypatched time.time() rather than sleeping for real.
"""
import asyncio
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
SRC = HERE.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import client_registry as client_registry_module  # noqa: E402
from client_registry import ClientRegistry, _GATEWAY_CONFIRM_S  # noqa: E402


def _run(coro):
    return _LOOP.run_until_complete(coro)


# A dedicated, stable loop for every _run() in this module. ClientRegistry
# creates an asyncio.Lock in __init__ that binds to whichever loop first runs
# it (eager binding on Python 3.9), so all _run() calls MUST share a loop —
# and it MUST NOT be the global get_event_loop() (sibling test files tear the
# global loop down to None in their fixtures, which would make
# asyncio.get_event_loop() raise mid-suite on Python 3.9). See
# test_sim_quota_engine.py for the same pattern.
_LOOP = asyncio.new_event_loop()
asyncio.set_event_loop(_LOOP)


@pytest.fixture(autouse=True)
def _bind_registry_loop():
    asyncio.set_event_loop(_LOOP)


@pytest.fixture
def _clock(monkeypatch):
    state = {"t": 1_000_000.0}

    def _time():
        return state["t"]

    monkeypatch.setattr(client_registry_module.time, "time", _time)
    return state


def test_single_bad_reading_does_not_confirm_down(tmp_path, _clock):
    reg = ClientRegistry(tmp_path)
    _run(reg.apply_status("c0", {"gateway_reachable": True}))
    _run(reg.apply_status("c0", {"gateway_reachable": False}))
    entry = reg.get("c0")
    assert entry.get("gateway_confirmed_down") is not True


def test_continuous_60min_down_confirms(tmp_path, _clock):
    reg = ClientRegistry(tmp_path)
    _run(reg.apply_status("c0", {"gateway_reachable": True}))
    _run(reg.apply_status("c0", {"gateway_reachable": False}))
    _clock["t"] += _GATEWAY_CONFIRM_S + 1
    _run(reg.apply_status("c0", {"gateway_reachable": False}))
    entry = reg.get("c0")
    assert entry.get("gateway_confirmed_down") is True


def test_flicker_within_window_resets_the_streak(tmp_path, _clock):
    reg = ClientRegistry(tmp_path)
    _run(reg.apply_status("c0", {"gateway_reachable": True}))
    _run(reg.apply_status("c0", {"gateway_reachable": False}))
    _clock["t"] += _GATEWAY_CONFIRM_S - 10   # almost confirmed...
    _run(reg.apply_status("c0", {"gateway_reachable": True}))   # ...but flickers back up
    _clock["t"] += 20
    _run(reg.apply_status("c0", {"gateway_reachable": False}))  # down again, streak restarts
    entry = reg.get("c0")
    assert entry.get("gateway_confirmed_down") is not True


def test_confirmed_down_clears_only_after_continuous_60min_up(tmp_path, _clock):
    reg = ClientRegistry(tmp_path)
    _run(reg.apply_status("c0", {"gateway_reachable": True}))
    _run(reg.apply_status("c0", {"gateway_reachable": False}))
    _clock["t"] += _GATEWAY_CONFIRM_S + 1
    _run(reg.apply_status("c0", {"gateway_reachable": False}))
    assert reg.get("c0").get("gateway_confirmed_down") is True

    # Comes back up, but not yet for a full 60 min — still latched down.
    _run(reg.apply_status("c0", {"gateway_reachable": True}))
    _clock["t"] += _GATEWAY_CONFIRM_S - 10
    _run(reg.apply_status("c0", {"gateway_reachable": True}))
    assert reg.get("c0").get("gateway_confirmed_down") is True

    # Now a full continuous 60 min of up — clears.
    _clock["t"] += 20
    _run(reg.apply_status("c0", {"gateway_reachable": True}))
    assert reg.get("c0").get("gateway_confirmed_down") is False


def test_missing_gateway_reachable_leaves_latch_untouched(tmp_path, _clock):
    reg = ClientRegistry(tmp_path)
    _run(reg.apply_status("c0", {"gateway_reachable": True}))
    _run(reg.apply_status("c0", {"gateway_reachable": False}))
    _clock["t"] += _GATEWAY_CONFIRM_S + 1
    _run(reg.apply_status("c0", {"gateway_reachable": False}))
    assert reg.get("c0").get("gateway_confirmed_down") is True
    # A beacon that omits gateway_reachable entirely (e.g. a partial payload)
    # must not reset or clear the latch.
    _run(reg.apply_status("c0", {"ip": "10.0.0.5"}))
    assert reg.get("c0").get("gateway_confirmed_down") is True
