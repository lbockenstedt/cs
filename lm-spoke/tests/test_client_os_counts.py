"""Client OS breakdown for the dashboard + the Central/Mist API pull.

Aggregated on the SPOKE (not in the browser) so the WebUI tabs, the dashboard
and any API caller all read the same numbers.
"""
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from central_poller import client_os_counts  # noqa: E402
from mist_poller import client_os_counts as mist_client_os_counts  # noqa: E402

import pytest  # noqa: E402

IMPLS = pytest.mark.parametrize("counts", [client_os_counts, mist_client_os_counts])


@IMPLS
def test_counts_and_orders_biggest_first(counts):
    clients = ([{"os": "Linux"}] * 500 + [{"os": "Windows"}] * 40
               + [{"os": "Tesla"}] * 10)
    assert list(counts(clients).items()) == [("Linux", 500), ("Windows", 40), ("Tesla", 10)]


@IMPLS
def test_unreported_os_collapses_to_unknown(counts):
    # Central/Mist normalize a missing OS to the em dash; "12 —" reads as a
    # rendering bug in a count, so it becomes "Unknown".
    assert counts([{"os": "—"}, {"os": ""}, {}, {"os": None}]) == {"Unknown": 4}


@IMPLS
def test_equal_counts_are_alphabetical_so_render_is_deterministic(counts):
    assert list(counts([{"os": "Windows"}, {"os": "Linux"}]).keys()) == ["Linux", "Windows"]


@IMPLS
def test_empty_and_malformed_input(counts):
    assert counts([]) == {}
    assert counts(None) == {}
    assert counts([None, {"os": "Linux"}]) == {"Unknown": 1, "Linux": 1}


def test_mist_copy_matches_central():
    # The two are deliberate duplicates (Mist mirrors Central, never imports it
    # — test_mist_tracker enforces that). Keep them behaviourally identical.
    sample = [{"os": "Linux"}] * 3 + [{"os": "Tesla"}] + [{"os": "—"}] + [{}]
    assert client_os_counts(sample) == mist_client_os_counts(sample)
