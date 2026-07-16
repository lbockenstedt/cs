"""Regression test for the shared monitored-check count evaluator.

Pins the type-silo bug fix that had to be applied in four separate deployments:
an alert-typed sim-quota MUST fire on a condition Central classifies as an
INSIGHT (e.g. "DNS Server Failed to Respond"), matched case-insensitively.
This test is duplicated per tree; keep it in sync with check_eval.py.
"""
import hashlib
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from check_eval import count_for_check, normalize_counts  # noqa: E402

# ── Drift guard ───────────────────────────────────────────────────────────────
# check_eval.py is vendored byte-identical to four trees (no shared package). This
# pins them in sync: bump EXPECTED_SHA when you legitimately change check_eval.py
# (the failure message spells out the re-sync). The lm repo's test_check_eval.py
# pins the SAME constant, so its copy can't drift either.
EXPECTED_SHA = "1bbe785c49d4406dbc9f4453d9c3f663c9ff4b5f75af22c67e31a239db0f5ade"
_REPO = Path(__file__).resolve().parents[2]  # cs repo root
_CS_COPIES = [
    _REPO / "lm-spoke" / "src" / "check_eval.py",
    _REPO / "webui-local" / "app" / "check_eval.py",
    _REPO / "webui-spoke" / "check_eval.py",
]


def test_cs_check_eval_copies_in_sync():
    bad = {
        str(p.relative_to(_REPO)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in _CS_COPIES
        if hashlib.sha256(p.read_bytes()).hexdigest() != EXPECTED_SHA
    }
    assert not bad, (
        "check_eval.py copies drifted. Re-sync from the canonical, then bump "
        "EXPECTED_SHA in BOTH repos' test_check_eval.py:\n"
        "  cp cs/lm-spoke/src/check_eval.py cs/webui-local/app/check_eval.py\n"
        "  cp cs/lm-spoke/src/check_eval.py cs/webui-spoke/check_eval.py\n"
        "  cp cs/lm-spoke/src/check_eval.py lm/core/src/simulations/check_eval.py\n"
        "  shasum -a 256 cs/lm-spoke/src/check_eval.py\n"
        f"Offenders (path: sha): {bad}"
    )


def test_normalize_folds_case_whitespace_and_sums():
    assert normalize_counts({" DNS Fail ": 2, "dns fail": 3}) == {"dns fail": 5}
    assert normalize_counts(None) == {}
    assert normalize_counts({"x": None}) == {"x": 0}


def test_alert_typed_check_fires_on_insight_bucket():
    # The bug: "DNS Server Failed to Respond" comes back as an INSIGHT while the
    # quota is typed "alert" — must still fire via the cross-bucket fall-back.
    check = {"id": "DNS Server Failed to Respond", "type": "alert"}
    insight_ci = normalize_counts({"dns server failed to respond": 4})
    assert count_for_check(check, normalize_counts({}), insight_ci) == 4


def test_insight_typed_check_fires_on_alert_bucket():
    check = {"id": "WPA Passphrase is Incorrect", "type": "insight"}
    alert_ci = normalize_counts({"wpa passphrase is incorrect": 6})
    assert count_for_check(check, alert_ci, normalize_counts({})) == 6


def test_case_insensitive_typed_bucket():
    check = {"id": "Maximum Associations", "type": "alert"}
    assert count_for_check(check, normalize_counts({"maximum associations": 7}), {}) == 7


def test_typed_bucket_wins_over_other():
    check = {"id": "DHCP Discover Timeout", "type": "alert"}
    alert_ci = normalize_counts({"dhcp discover timeout": 5})
    insight_ci = normalize_counts({"dhcp discover timeout": 9})
    assert count_for_check(check, alert_ci, insight_ci) == 5


def test_missing_type_defaults_to_alert():
    check = {"id": "Maximum Associations"}  # no type key
    assert count_for_check(check, normalize_counts({"maximum associations": 2}), {}) == 2


def test_absent_condition_and_blank_id_are_zero():
    assert count_for_check({"id": "Nope", "type": "alert"}, normalize_counts({"x": 1}), {}) == 0
    assert count_for_check({"id": "", "type": "alert"}, {"x": 1}, {}) == 0
