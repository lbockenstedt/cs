"""Resolving a client's reported OS across Central/Mist field spellings.

Regression: the first cut tried only osType/os_type, so every client rendered
"Unknown" in the WebUI while the Central UI plainly showed OS/Model — and
nothing in the log said which key it should have read.
"""
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from aruba import _client_os  # noqa: E402
from mist import _client_os as mist_client_os  # noqa: E402

DASH = "—"
IMPLS = pytest.mark.parametrize("resolve", [_client_os, mist_client_os])


@IMPLS
@pytest.mark.parametrize("key", [
    "osType", "os_type", "os", "operatingSystem", "osName",
    "clientOs", "deviceType", "device_type", "model",
])
def test_each_known_spelling_resolves(resolve, key):
    assert resolve({key: "Windows"}) == "Windows"


@IMPLS
def test_preferred_key_wins_over_last_resort_model(resolve):
    assert resolve({"osType": "Linux", "model": "ThinkPad"}) == "Linux"


@IMPLS
def test_placeholder_values_do_not_outrank_a_real_later_field(resolve):
    # A controller writing "unknown" into os_type must not mask a usable model —
    # this is what made the UI show "Unknown" instead of falling through.
    assert resolve({"os_type": "unknown", "model": "Tesla Model 3"}) == "Tesla Model 3"
    assert resolve({"osType": "", "deviceType": "Windows"}) == "Windows"
    assert resolve({"os": "N/A", "device_type": "Linux"}) == "Linux"


@IMPLS
def test_nested_object_values(resolve):
    assert resolve({"osType": {"name": "Android"}}) == "Android"


@IMPLS
def test_nothing_reported_is_the_em_dash(resolve):
    assert resolve({}) == DASH
    assert resolve({"osType": "unknown"}) == DASH
    assert resolve(None) == DASH
    assert resolve({"hostname": "x"}) == DASH


def test_mist_copy_matches_aruba():
    # Deliberate duplicates — Mist mirrors Central but never imports it.
    for sample in ({"osType": "Linux"}, {"model": "Pixel"}, {"os_type": "unknown"}, {}):
        assert _client_os(sample) == mist_client_os(sample)
