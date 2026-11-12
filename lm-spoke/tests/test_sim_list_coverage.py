"""Every sim in the one sim list (lm core sims.json, imported as SIM_META) must
appear in the client scripts and the UI, which cannot import the list."""

import re
from pathlib import Path

import pytest

from sim_quota import SIM_META

ROOT = Path(__file__).resolve().parents[2]


def _text(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


def _js_list(text, name):
    """The quoted names in a sim-views.js `const NAME = [...]` or `new Set([...])`."""
    m = re.search(r"const %s = (?:new Set\()?\[([^\]]*)\]" % name, text)
    return set(re.findall(r"'([\w-]+)'", m.group(1)))


@pytest.mark.parametrize("sim", sorted(SIM_META))
def test_client_scripts_and_ui_cover_sim(sim):
    word = re.compile(r"\b%s\b" % sim)
    for rel in ("clients/linux/simulation.sh", "clients/windows/simulation.ps1",
                "clients/lib/common.sh", "clients/windows/common.ps1"):
        assert word.search(_text(rel)), "%s is missing from %s" % (sim, rel)
    # The dashboards' flag tables (what the operator sees on the client).
    flag_values = re.search(r"flag_values=\(([^)]*)\)", _text("clients/linux/dashboard.sh")).group(1)
    assert '"$%s"' % sim in flag_values, "%s is missing from dashboard.sh flag_values" % sim
    assert "$config.%s }" % sim in _text("clients/windows/dashboard.ps1"), \
        "%s is missing from the dashboard.ps1 flag table" % sim
    # The UI's per-client toggles and on/off editor keys.
    js = _text("lm-spoke/static/sim-views.js")
    assert sim in _js_list(js, "CS_CONTROL_FLAGS"), "%s is missing from CS_CONTROL_FLAGS" % sim
    assert sim in _js_list(js, "CS_ONOFF_KEYS"), "%s is missing from CS_ONOFF_KEYS" % sim
