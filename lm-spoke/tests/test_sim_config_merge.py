"""Hub-managed override merge — the hub pushes sim/user config via
``CS_CONFIG_UPDATE`` (``sim_conf_override`` / ``user_conf_override`` INI text),
which the spoke writes to ``configs/hub-sim-overrides.conf`` /
``configs/hub-user-overrides.conf``. These must merge on top of the base repo
files so the hub-managed edits actually take effect in the engine resolver,
the ``/api/config`` client route, and ``CS_GET_CONFIG`` readback.

Before this fix the override files were dead: only ``[simulation] sim_phy``
was ever read (command_queue.usb_config_payload); every other key — and all
of ``hub-user-overrides.conf`` — was silently dropped. Ports the legacy
webui-spoke ``_merge_ini_override`` (server.py:675).
"""

import configparser

import sim_config
from cs_spoke import CSSpoke


def _write(path, text):
    path.write_text(text, encoding="utf-8")


def _parser(text):
    p = sim_config._new_parser()
    p.read_string(text)
    return p


SIM_CONF = """[simulation]
kill_switch=off
sim_load=100
sim_phy=wireless

[server]
server_url=http://169.253.1.1:8080

[s0]
wsite=MIA
ssid=PSK
"""


USER_CONF = """[jsmith]
wsite=MIA
dns_fail=off
"""


def test_load_configs_merges_hub_sim_override_on_top(tmp_path):
    base = tmp_path / "configs"
    base.mkdir()
    _write(base / "simulation.conf", SIM_CONF)
    # Hub override changes a value AND adds a new section key.
    _write(base / "hub-sim-overrides.conf",
           "[simulation]\nkill_switch=on\nnew_key=hello\n[s2]\nwsite=DFW\n")
    sim_conf, _ = sim_config.load_configs(base)
    # Override wins over the base value.
    assert sim_conf.get("simulation", "kill_switch") == "on"
    # Untouched base keys survive.
    assert sim_conf.get("simulation", "sim_load") == "100"
    # New key added by the override.
    assert sim_conf.get("simulation", "new_key") == "hello"
    # New section added by the override.
    assert sim_conf.has_section("s2")
    assert sim_conf.get("s2", "wsite") == "DFW"
    # Base sections preserved.
    assert sim_conf.get("s0", "wsite") == "MIA"


def test_load_configs_merges_hub_user_override_on_top(tmp_path):
    base = tmp_path / "configs"
    base.mkdir()
    _write(base / "simulation.conf", SIM_CONF)
    _write(base / "user-overrides.conf", USER_CONF)
    # Hub user-override flips jsmith's dns_fail AND adds a brand-new user.
    _write(base / "hub-user-overrides.conf",
           "[jsmith]\ndns_fail=on\n[amoran]\nwsite=LAX\n")
    _, user_conf = sim_config.load_configs(base)
    assert user_conf.get("jsmith", "dns_fail") == "on"
    assert user_conf.has_section("amoran")
    assert user_conf.get("amoran", "wsite") == "LAX"


def test_missing_override_files_is_noop(tmp_path):
    base = tmp_path / "configs"
    base.mkdir()
    _write(base / "simulation.conf", SIM_CONF)
    _write(base / "user-overrides.conf", USER_CONF)
    # No hub-*-overrides.conf present → base config returned unchanged.
    sim_conf, user_conf = sim_config.load_configs(base)
    assert sim_conf.get("simulation", "kill_switch") == "off"
    assert user_conf.get("jsmith", "dns_fail") == "off"


def test_resolve_profile_reflects_hub_sim_override(tmp_path):
    """The engine resolver must pick up the merged override — the whole point
    of the fix. A hub-pushed kill_switch=on must reach resolve_profile output."""
    base = tmp_path / "configs"
    base.mkdir()
    _write(base / "simulation.conf", SIM_CONF)
    _write(base / "hub-sim-overrides.conf", "[simulation]\nkill_switch=on\n")
    sim_conf, user_conf = sim_config.load_configs(base)
    # bucket_for(hostname) deterministic; kill_switch is a [simulation] global
    # so it overlays regardless of bucket.
    prof = sim_config.resolve_profile("any-host", sim_conf, user_conf)
    assert prof["profile"]["kill_switch"] == "on"


def test_malformed_override_does_not_raise(tmp_path):
    base = tmp_path / "configs"
    base.mkdir()
    _write(base / "simulation.conf", SIM_CONF)
    _write(base / "hub-sim-overrides.conf", "this is not = valid [ini\n] garbage")
    # Must not raise; base config intact (merge skipped on parse failure).
    sim_conf, _ = sim_config.load_configs(base)
    assert sim_conf.get("simulation", "kill_switch") == "off"


def test_cs_get_config_returns_merged_config(monkeypatch):
    """CS_GET_CONFIG must return the MERGED config so the hub's Sim Config
    editor reads back effective state on Refresh, not the raw base file.
    Patch sim_config.load_configs to return a pre-merged parser pair and
    assert the handler serializes it (override key visible)."""
    sim_conf = _parser("[simulation]\nkill_switch=on\nsim_phy=ethernet\n")
    user_conf = _parser("[amoran]\nwsite=LAX\n")
    monkeypatch.setattr(sim_config, "load_configs", lambda _d: (sim_conf, user_conf))

    import asyncio
    # CSSpoke() construction creates a ClientRegistry (asyncio.Lock), which on
    # Python 3.9 needs a current event loop. Earlier tests (test_hub_config)
    # call asyncio.set_event_loop(None), poisoning the process so a fresh loop
    # must be set BEFORE constructing the spoke. Guard retrieval, create a loop,
    # construct + run on it, and leave a usable loop behind for later tests.
    try:
        prev_loop = asyncio.get_event_loop()
        if prev_loop.is_closed():
            prev_loop = None
    except RuntimeError:
        prev_loop = None
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        spoke = CSSpoke("test-cs", {})
        resp = loop.run_until_complete(spoke.handle_command("CS_GET_CONFIG", {}))
    finally:
        if prev_loop is not None:
            asyncio.set_event_loop(prev_loop)
        else:
            asyncio.set_event_loop(asyncio.new_event_loop())
        loop.close()
    assert resp["status"] == "SUCCESS"
    # Re-parse the returned text (configparser writes " = " around the value)
    # and confirm the merged override value is present.
    sim_back = sim_config._new_parser()
    sim_back.read_string(resp["simulation_conf"])
    assert sim_back.get("simulation", "kill_switch") == "on"
    user_back = sim_config._new_parser()
    user_back.read_string(resp["user_overrides"])
    assert user_back.get("amoran", "wsite") == "LAX"


def test_serialize_ini_round_trips(tmp_path):
    base = tmp_path / "configs"
    base.mkdir()
    _write(base / "simulation.conf", SIM_CONF)
    sim_conf, _ = sim_config.load_configs(base)
    text = sim_config.serialize_ini(sim_conf)
    # Round-trip: re-parsing the serialized text yields the same sections/keys.
    again = sim_config._new_parser()
    again.read_string(text)
    assert again.get("simulation", "kill_switch") == "off"
    assert again.get("s0", "ssid") == "PSK"

# ── SET_AGENT_CONFIG cache deep-merge (enabled/tenant save must not wipe usb_config) ──
from cs_spoke import _deep_merge_cfg  # noqa: E402


def test_deep_merge_preserves_usb_config_on_enabled_only_save():
    # Bridge cached the full config (with vidpids); UI then saves enabled/tenant only.
    cached = {"display_name": "cs", "client_simulation": {
        "enabled": True, "tenant_id": "default",
        "usb_config": {"vidpids": [{"vidpid": "2357:012e"}], "max_slots": 24}}}
    ui_save = {"client_simulation": {"enabled": True, "tenant_id": "default"}}
    merged = _deep_merge_cfg(cached, ui_save)
    cs = merged["client_simulation"]
    assert cs["usb_config"]["vidpids"] == [{"vidpid": "2357:012e"}]  # NOT wiped
    assert cs["usb_config"]["max_slots"] == 24
    assert cs["enabled"] is True and cs["tenant_id"] == "default"


def test_deep_merge_replaces_vidpids_list_whole_on_real_usb_push():
    cached = {"client_simulation": {"usb_config": {"vidpids": [{"vidpid": "2357:012e"}]}}}
    bridge = {"client_simulation": {"usb_config": {"vidpids": [{"vidpid": "1234:5678"}]}}}
    merged = _deep_merge_cfg(cached, bridge)
    assert merged["client_simulation"]["usb_config"]["vidpids"] == [{"vidpid": "1234:5678"}]


# ── load_configs mtime cache (Request-Timeout fix) ────────────────────────────
# load_configs runs on the cs spoke's shared event loop (engine every ~5 s,
# /api/config per client fetch). The 4-file read+parse is mtime-cached so the
# sync disk syscalls don't stall the loop. /api/config MUTATES the returned
# sim_conf (merges user_conf + render_ini_for_client bakes [sX] overrides), so
# the cache MUST hand out deep copies — a live-object cache would leak per-
# client / per-request mutations into the canonical pair. These lock that in.


def test_load_configs_cache_hit_returns_equivalent_merged_content(tmp_path):
    base = tmp_path / "configs"
    base.mkdir()
    _write(base / "simulation.conf", SIM_CONF)
    _write(base / "hub-sim-overrides.conf", "[simulation]\nkill_switch=on\n")
    a_sim, _ = sim_config.load_configs(base)
    b_sim, _ = sim_config.load_configs(base)  # cache hit (no file changed)
    assert b_sim.get("simulation", "kill_switch") == "on"   # override still merged
    assert b_sim.get("simulation", "sim_load") == "100"     # base key preserved
    # Different objects (deepcopy), same content.
    assert a_sim is not b_sim


def test_load_configs_cache_isolated_from_caller_mutation(tmp_path):
    """The critical safety property: a caller mutating its returned parser
    (exactly what /api/config does — add_section/set + render_ini_for_client)
    must NOT corrupt the cached canonical pair handed to the next caller."""
    base = tmp_path / "configs"
    base.mkdir()
    _write(base / "simulation.conf", SIM_CONF)
    first, _ = sim_config.load_configs(base)
    # /api/config-style mutation: add a section + bake a per-client override.
    if not first.has_section("s7"):
        first.add_section("s7")
    first.set("s7", "ssid", "LEAKED")
    first.set("simulation", "kill_switch", "on")  # base had "off"

    second, _ = sim_config.load_configs(base)  # cache hit — must be pristine
    assert not second.has_section("s7"), "caller mutation leaked into cache"
    assert second.get("simulation", "kill_switch") == "off", \
        "caller mutation leaked into cached canonical parser"
    assert second.get("s0", "wsite") == "MIA"


def test_load_configs_cache_invalidates_on_file_mtime_change(tmp_path):
    base = tmp_path / "configs"
    base.mkdir()
    _write(base / "simulation.conf", SIM_CONF)
    _, _ = sim_config.load_configs(base)
    # Rewrite the base file with a new value → mtime changes → cache misses.
    _write(base / "simulation.conf",
           SIM_CONF.replace("kill_switch=off", "kill_switch=on"))
    sim_conf, _ = sim_config.load_configs(base)
    assert sim_conf.get("simulation", "kill_switch") == "on"


def test_load_configs_cache_invalidates_on_override_change(tmp_path):
    base = tmp_path / "configs"
    base.mkdir()
    _write(base / "simulation.conf", SIM_CONF)
    _write(base / "hub-sim-overrides.conf", "[simulation]\nkill_switch=on\n")
    sim_conf, _ = sim_config.load_configs(base)
    assert sim_conf.get("simulation", "kill_switch") == "on"
    # Hub pushes a new override flipping it back off → mtime change → miss.
    _write(base / "hub-sim-overrides.conf", "[simulation]\nkill_switch=off\n")
    sim_conf, _ = sim_config.load_configs(base)
    assert sim_conf.get("simulation", "kill_switch") == "off"


def test_load_configs_cache_bounded_per_dir(tmp_path):
    """Only the latest mtime tuple per config_dir is retained — repeated writes
    must not grow the cache unbounded."""
    base = tmp_path / "configs"
    base.mkdir()
    _write(base / "simulation.conf", SIM_CONF)
    for i in range(5):
        _write(base / "simulation.conf", f"[simulation]\nsim_load={i}\n")
        sim_config.load_configs(base)
    same_dir_keys = [k for k in sim_config._LOAD_CACHE if k[0] == str(base)]
    assert len(same_dir_keys) == 1
