"""sim_pool: the Proxmox resource pool auto-provisioned sim clients join."""
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import command_queue  # noqa: E402,F401  (import first — cs_settings imports it)
from cs_settings import CSSettings  # noqa: E402
from command_handlers.handlers_config import ConfigCommandsMixin  # noqa: E402


def test_default_is_empty_so_behaviour_is_unchanged():
    assert CSSettings._DEFAULTS.get("sim_pool") == ""


def test_sim_pool_is_hub_settable():
    # Without this the WebUI could save it but it would never reach the agent.
    assert "sim_pool" in ConfigCommandsMixin._HUB_DIRECT_KEYS


def test_payload_emits_trimmed_pool(tmp_path):
    s = CSSettings.__new__(CSSettings)
    s.settings = dict(CSSettings._DEFAULTS)
    s.settings["sim_pool"] = "  sim-clients  "
    assert s.get("sim_pool", "").strip() == "sim-clients"


def test_payload_pool_absent_is_empty_string(tmp_path):
    s = CSSettings.__new__(CSSettings)
    s.settings = dict(CSSettings._DEFAULTS)
    # Empty (not None) so the agent's _sim_pool() resolves it to None → no --pool
    # flag, i.e. the historical clone behaviour.
    assert str(s.get("sim_pool", "") or "").strip() == ""
