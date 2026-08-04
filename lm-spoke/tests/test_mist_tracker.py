"""Mist-owned tracker helpers (mirror of central_poller's). Central and Mist
are separate products — these must NOT import from central_poller. Covers the
threshold resolver, worst-of selector, the client-count baseline tracker
(record/entry/maybe_snapshot + die-off), and the per-check health history."""
import sys
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
SRC = HERE.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import mist_tracker as mt  # noqa: E402


def test_mist_tracker_does_not_import_central_poller():
    """INVARIANT: Mist must not share code with Central. Neither module may
    have an actual import of central_poller (prose/comments naming it to
    explain the de-sharing are fine). Checked via AST, not substring."""
    import ast

    def _imports(mod_path, target):
        tree = ast.parse((SRC / mod_path).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import) and any(a.name == target for a in node.names):
                return True
            if isinstance(node, ast.ImportFrom) and node.module == target:
                return True
        return False

    assert not _imports("mist_tracker.py", "central_poller")
    assert not _imports("mist_poller.py", "central_poller")
    assert _imports("mist_poller.py", "mist_tracker")  # poller DOES import mist_tracker


def test_mist_cc_thresholds_defaults_and_clamp():
    t = mt._mist_cc_thresholds({})
    assert t == {"warn_pct": 20.0, "error_pct": 50.0,
                 "die_off_frac": 0.2, "min_peak": 5,
                 # Steady-state baselines default to die_off_pct so an
                 # already-tuned tenant keeps its number.
                 "daily_frac": 0.2, "weekly_frac": 0.2, "monthly_frac": 0.2}
    # error_pct coerced up to warn_pct so red never trips before amber.
    t2 = mt._mist_cc_thresholds({"cc_thresholds": {"warn_pct": 40, "error_pct": 10}})
    assert t2["warn_pct"] == 40.0
    assert t2["error_pct"] == 40.0
    # out-of-range clamped.
    t3 = mt._mist_cc_thresholds({"cc_thresholds": {"warn_pct": -5, "error_pct": 999}})
    assert t3["warn_pct"] == 0.0
    assert t3["error_pct"] == 100.0
    # die_off_pct 0 disables.
    t4 = mt._mist_cc_thresholds({"cc_thresholds": {"die_off_pct": 0}})
    assert t4["die_off_frac"] == 0.0


def test_mist_cc_worst_severity_order():
    assert mt._mist_cc_worst("ok", "warning", "error") == "error"
    assert mt._mist_cc_worst("ok", "no_data") == "ok"
    # a single no_data surfaces as no_data; only the NO-arg case falls back to ok.
    assert mt._mist_cc_worst("no_data") == "no_data"
    assert mt._mist_cc_worst() == "ok"  # all-empty -> ok fallback
    assert mt._mist_cc_worst("warning", "ok") == "warning"


def test_client_count_tracker_no_data_then_drop(tmp_path):
    cc = mt.MistClientCountTracker(
        str(tmp_path / "bl.json"), str(tmp_path / "7d.json"))
    # zero samples -> no_data with zeroed fields.
    e = cc.entry("_", "MIA", "MIA", mt._mist_cc_thresholds({}))
    assert e["status"] == "no_data" and e["current"] == 0
    # Below min samples -> still no_data.
    cc.record("_", "MIA", 100)
    e = cc.entry("_", "MIA", "MIA", mt._mist_cc_thresholds({}))
    assert e["status"] == "no_data"
    # Enough samples at a healthy count -> ok.
    for _ in range(mt._CC_MIN_SAMPLES):
        cc.record("_", "MIA", 100)
    e = cc.entry("_", "MIA", "MIA", mt._mist_cc_thresholds({}))
    assert e["status"] == "ok"
    # A >50% drop vs the hourly average -> error.
    for _ in range(mt._CC_MIN_SAMPLES):
        cc.record("_", "MIA", 10)
    e = cc.entry("_", "MIA", "MIA", mt._mist_cc_thresholds({}))
    assert e["status"] == "error"
    assert e["drop_pct"] > 50.0


def test_client_count_tracker_persists_baseline(tmp_path):
    bl = tmp_path / "bl.json"
    sd = tmp_path / "7d.json"
    cc = mt.MistClientCountTracker(str(bl), str(sd))
    for _ in range(mt._CC_MIN_SAMPLES):
        cc.record("_", "MIA", 42)
    # Force a snapshot by rewinding the snapshot gate.
    cc._last_snapshot = time.time() - mt._CC_SNAPSHOT_INTERVAL - 1
    cc.maybe_snapshot()
    assert bl.exists()
    import json
    saved = json.loads(bl.read_text())
    assert any("MIA" in k for k in saved)


def test_check_health_history_records_and_summarizes(tmp_path):
    h = mt.MistCheckHealthHistory(str(tmp_path / "hh.json"))
    h.record("_", "MIA", "ap_offline", "ok")
    h.record("_", "MIA", "ap_offline", "error")
    h.save()
    summary = h.summary("_")
    assert "MIA" in summary
    assert "ap_offline" in summary["MIA"]
    today_bucket = summary["MIA"]["ap_offline"][-1]
    assert today_bucket["o"] >= 1 and today_bucket["e"] >= 1


def test_check_health_history_excludes_other_scope(tmp_path):
    h = mt.MistCheckHealthHistory(str(tmp_path / "hh.json"))
    h.record("other", "MIA", "ap_offline", "ok")
    h.record("_", "MIA", "ap_offline", "error")
    summary = h.summary("_")
    # Only the "_" scope row surfaces; the "other" tenant's check is excluded.
    assert "ap_offline" in summary.get("MIA", {})