"""Tests for the config-value knob-floor learner (sim_quota.knob_step).

The learner ratchets a sim's [simulation] intensity knobs (SIM_KNOBS) DOWN one
at a time, coordinate-descent, to the floor that still fires the alert. The step
function is pure, so these drive it with a synthetic firing oracle and assert the
learned floors — no live hub/Central needed. Mirrors the hub controller's use in
core/src/simulations/routes.py (_run_knob_learner).
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from sim_quota import KNOB_SETTLE_S, knob_step, knobs_for_sim  # noqa: E402

RATE = "dns_fail_rate"
DUR = "dns_fail_duration"


def _simulate(oracle, rounds=300, start_now=1000.0):
    """Cold-start then advance one settle window per round, feeding knob_step the
    firing signal computed from the currently-delivered knob values."""
    knobs = knobs_for_sim("dns_fail")
    st = knob_step({}, knobs, True, start_now)  # cold start seeds at `start`
    now = start_now
    for _ in range(rounds):
        now += KNOB_SETTLE_S + 1
        v = st.get("values") or {}
        firing = oracle(v.get(RATE), v.get(DUR))
        st = knob_step(st, knobs, firing, now)
    return st


def test_cold_start_seeds_high():
    knobs = knobs_for_sim("dns_fail")
    st = knob_step({}, knobs, True, 1000.0)
    assert st["values"] == {RATE: 3000, DUR: 600}  # begin at the known-firing high end
    assert st["floors"] == {RATE: None, DUR: None}
    assert st["active"] == 0
    assert st["mode"] == "learning"


def test_holds_before_settle_window():
    knobs = knobs_for_sim("dns_fail")
    st = knob_step({}, knobs, True, 1000.0)
    before = dict(st)
    # Only 10s later — far short of the 30-min settle — nothing should move.
    after = knob_step(st, knobs, True, 1010.0)
    assert after["values"] == before["values"]
    assert after["active"] == before["active"]


def test_firing_none_holds():
    knobs = knobs_for_sim("dns_fail")
    st = knob_step({}, knobs, True, 1000.0)
    after = knob_step(st, knobs, None, 1000.0 + KNOB_SETTLE_S + 5)
    assert after["values"] == st["values"]  # unknown signal → never move blind


def test_finds_each_knobs_floor():
    # Alert fires iff rate >= 800/min AND duration >= 300s. Coordinate descent
    # should discover exactly those floors (one knob minimised, then the next).
    st = _simulate(lambda r, d: (r or 0) >= 800 and (d or 0) >= 300)
    assert st["floors"][RATE] == 800
    assert st["floors"][DUR] == 300
    assert st["mode"] == "stable"


def test_floor_is_min_when_alert_always_fires():
    # If even the lowest values fire, each knob floors at its configured min.
    st = _simulate(lambda r, d: True)
    assert st["floors"][RATE] == 200   # client-clamp floor
    assert st["floors"][DUR] == 120


def test_recovers_when_max_barely_fires():
    # Alert only fires at the very top — the learner must NOT drop below and get
    # stuck; it steps back up and floors near the max.
    st = _simulate(lambda r, d: (r or 0) >= 3000 and (d or 0) >= 600)
    assert st["floors"][RATE] == 3000
    assert st["floors"][DUR] == 600


def test_knobs_for_sim_unknown_is_empty():
    assert knobs_for_sim("ping_test") == []
    assert knob_step({}, [], True, 1000.0) == {}  # no knobs → no-op
