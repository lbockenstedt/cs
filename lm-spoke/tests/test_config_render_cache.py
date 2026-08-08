"""Byte-equivalence tests for the cached ``/api/config`` render (client_api).

The route now serves from a two-layer cache (merged-base parser + per-client
rendered text). These tests pin the output to a REFERENCE implementation that
replicates the ORIGINAL uncached code path verbatim, over a controlled tmp
configs dir — for hub and standalone modes, with and without registry
overrides — and verify the cache actually invalidates when the config file or
the per-client overrides change.
"""

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import client_api
import sim_config
from client_api import build_client_api_app, _exclusive_sim_active
from client_registry import ClientRegistry
from command_queue import CommandQueue, CSSettings
from cs_spoke import CSSpoke

BASE_CONF = """[simulation]
kill_switch=off
rapid_update=on
sim_load=100
web_server={ws}
site_based_ssid=off

[server]
server_url=http://169.253.1.1:8080

[address]
ping_address=1.1.1.1

[s0]
dns_fail=off
ping_test=on

[s3]
dns_fail=on
wsite=LAB1
"""

USER_CONF = """[kbell]
wsite=MIA
ssid=corp
"""


# ── reference: the ORIGINAL (uncached) /api/config body ──────────────────────
def reference_render(spoke, configs_dir: Path, hostname: str) -> str:
    sim_conf, user_conf = sim_config.load_configs(configs_dir)
    for section in user_conf.sections():
        if not sim_conf.has_section(section):
            sim_conf.add_section(section)
        for k, v in user_conf.items(section):
            sim_conf.set(section, k, v)
    overrides = {}
    if hostname and spoke.registry is not None:
        entry = spoke.registry.get(hostname)
        if entry and isinstance(entry.get("overrides"), dict):
            overrides = {str(k): str(v) for k, v in entry["overrides"].items()}
    demo = getattr(spoke, "demo", None)
    if demo is not None and hostname:
        overrides.update(demo.effective_flags(hostname))
    _TENANT_POOL = "Tenant-Wide Pool"

    def _physical_site(host):
        try:
            deploy = getattr(spoke, "deploy", None)
            n2h = deploy.name_to_host() if deploy is not None else {}
            srv = n2h.get(str(host).strip().lower())
            if srv:
                s = str(spoke.local_store.get_pxmx_site_map().get(srv) or "").strip()
                return "" if s == _TENANT_POOL else s
        except Exception:
            pass
        return ""

    resolved_site = str(overrides.get("wsite") or "").strip()
    try:
        if hostname:
            phys = _physical_site(hostname)
            if phys:
                overrides.setdefault("wsite", phys)
                resolved_site = resolved_site or phys
    except Exception:
        pass
    if not resolved_site and hostname:
        _un = sim_config.username_for(hostname)
        if user_conf.has_section(_un):
            resolved_site = str(user_conf.get(_un, "wsite", fallback="") or "").strip()
    try:
        pool_map = spoke.local_store.get_random_pool()
        rand_sims = spoke.local_store.get_randomizable_sims()
        if not sim_conf.has_section("simulation"):
            sim_conf.add_section("simulation")
        pool_on = bool(pool_map.get(resolved_site, pool_map.get("*", False)))
        sim_conf.set("simulation", "random_pool", "on" if pool_on else "off")
        for _kk, _kv in (spoke.local_store.get_sim_knob_overrides() or {}).items():
            sim_conf.set("simulation", str(_kk), str(_kv))
        if rand_sims:
            sim_conf.set("simulation", "randomizable_sims",
                         " ".join(str(s) for s in rand_sims))
        base_pct = spoke.local_store.get_ambient_pct()
        control_on = spoke.local_store.get_ambient_control()
        sim_conf.set("simulation", "ambient_control",
                     "on" if control_on else "off")
        _uname = sim_config.username_for(hostname) if hostname else ""
        if _exclusive_sim_active(hostname, sim_conf, overrides, user_conf, _uname):
            sim_conf.set("simulation", "ambient_pct", "0")
        elif control_on:
            site_w = spoke.local_store.get_ambient_site_weights()
            sfactor = site_w.get(resolved_site) or 1
            eff_level = max(0, min(100, int(round(base_pct * sfactor))))
            sim_conf.set("simulation", "ambient_pct", str(eff_level))
            weights = spoke.local_store.get_ambient_weights()
            if weights:
                if not sim_conf.has_section("ambient_weights"):
                    sim_conf.add_section("ambient_weights")
                for _sim, _w in weights.items():
                    sim_conf.set("ambient_weights", str(_sim), str(_w))
        else:
            sim_conf.set("simulation", "ambient_pct", str(base_pct))
    except Exception:
        pass
    # T3 IoT-fleet detection list (client_api.py api_config): deliver the
    # hub-config t3_pci_vidpids into [simulation] so the linux client's
    # iot_sim.sh detect_t3_pci matches. local_store defaults this list to
    # ["168c:0034"], so it is present unless explicitly cleared.
    try:
        _hc = (spoke.local_store.get_hub_config() or {}).get("hub_config") or {}
        _t3 = _hc.get("t3_pci_vidpids") or []
        if isinstance(_t3, str):
            _t3 = [_t3]
        _t3_str = " ".join(str(x).strip() for x in _t3 if str(x).strip())
        if _t3_str:
            if not sim_conf.has_section("simulation"):
                sim_conf.add_section("simulation")
            sim_conf.set("simulation", "t3_pci_vidpids", _t3_str)
    except Exception:
        pass
    if hostname and overrides:
        uname = sim_config.username_for(hostname)
        human_keys = (set(user_conf.options(uname))
                      if user_conf.has_section(uname) else set())
        if not sim_conf.has_section(uname):
            sim_conf.add_section(uname)
        for k, v in overrides.items():
            if k in human_keys:
                continue
            sim_conf.set(uname, str(k), str(v))
    ws_on = str(sim_conf.get("simulation", "web_server", fallback="") or "").strip().lower() == "on"
    if ws_on:
        for _b in (f"s{i}" for i in range(10)):
            if sim_conf.has_section(_b):
                sim_conf.remove_section(_b)
        return sim_config.render_ini_for_client(sim_conf, hostname, None)
    return sim_config.render_ini_for_client(sim_conf, hostname, overrides or None)


# ── fixtures ─────────────────────────────────────────────────────────────────
@pytest.fixture
def configs(tmp_path, monkeypatch) -> Path:
    cfg = tmp_path / "configs"
    cfg.mkdir()
    (cfg / "simulation.conf").write_text(BASE_CONF.format(ws="off"), encoding="utf-8")
    (cfg / "user-overrides.conf").write_text(USER_CONF, encoding="utf-8")
    monkeypatch.setattr(client_api, "CONFIGS_DIR", cfg)
    return cfg


@pytest.fixture
def spoke(tmp_path) -> CSSpoke:
    # py3.9: asyncio.Lock() in ClientRegistry.__init__ needs a current event
    # loop; an earlier test in a full run may have cleared it.
    import asyncio
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
    s = CSSpoke("test-cs", {})
    data = tmp_path / "data"
    data.mkdir()
    s.settings = CSSettings(data, tmp_path / "configs")
    s.registry = ClientRegistry(data)
    s.queue = CommandQueue(data, s.settings)
    return s


@pytest.fixture
def client(spoke, configs) -> TestClient:
    return TestClient(build_client_api_app(spoke))


HOSTS = ["kbell-1", "jdoe-7", "orphan", ""]


def _assert_matches_reference(client, spoke, configs, hosts=HOSTS):
    for h in hosts:
        got = client.get("/api/config", params={"hostname": h}).text
        want = reference_render(spoke, configs, h)
        assert got == want, f"render mismatch for {h!r}"
        # Second fetch exercises the text-cache hit path — must be identical.
        again = client.get("/api/config", params={"hostname": h}).text
        assert again == want, f"cache-hit mismatch for {h!r}"


def test_render_matches_reference_standalone(client, spoke, configs):
    _assert_matches_reference(client, spoke, configs)


def test_render_matches_reference_hub_mode(client, spoke, configs):
    # web_server=on strips the s0-s9 buckets and skips the [sX] bake.
    (configs / "simulation.conf").write_text(BASE_CONF.format(ws="on"), encoding="utf-8")
    _assert_matches_reference(client, spoke, configs)


def test_render_with_registry_overrides(client, spoke, configs):
    # Engine/registry overrides land in [username]; the human user-overrides
    # key (wsite for kbell) must win over an engine wsite (Model A).
    client.post("/api/clients/kbell-1/control",
                json={"overrides": {"dns_fail": "on", "wsite": "DEN"}})
    client.post("/api/clients/jdoe-7/control",
                json={"overrides": {"iperf": "on"}})
    _assert_matches_reference(client, spoke, configs, hosts=["kbell-1", "jdoe-7"])


def test_ambient_suppressed_for_bucket_default_exclusive_sim(client, spoke, configs):
    """A client whose EXCLUSIVE sim (ssidpw_fail) is only its BUCKET DEFAULT —
    no registry override, no human pin — must still suppress ambient_pct.
    _exclusive_sim_active used to check only the engine override + a human
    pin, silently missing the bucket-default layer: that client's ambient_pct
    stayed enabled, letting the client-side ambient rotation stack a shareable
    sim (e.g. ping_test) on top of a bucket-default exclusive one."""
    h = "bucket-default-excl-test"                # bucket_for(h) == "s1" — new,
    bucket = sim_config.bucket_for(h)              # doesn't collide with s0/s3
    conf = configs / "simulation.conf"
    conf.write_text(conf.read_text(encoding="utf-8") + f"\n[{bucket}]\nssidpw_fail=on\n",
                    encoding="utf-8")
    spoke.local_store.set_ambient_pct(50)
    spoke.local_store.set_ambient_control(False)
    text = client.get("/api/config", params={"hostname": h}).text
    m = re.search(r"(?m)^ambient_pct\s*=\s*(\S+)", text)
    assert m and m.group(1) == "0", f"expected ambient_pct=0, got {m.group(1) if m else None!r}"


def test_cache_invalidation_on_conf_change(client, spoke, configs):
    h = "jdoe-7"
    first = client.get("/api/config", params={"hostname": h}).text
    conf = configs / "simulation.conf"
    conf.write_text(conf.read_text(encoding="utf-8") + "\n[s9]\niperf=on\n",
                    encoding="utf-8")
    updated = client.get("/api/config", params={"hostname": h}).text
    assert updated != first
    assert updated == reference_render(spoke, configs, h)


def test_cache_invalidation_on_override_change(client, spoke, configs):
    h = "kbell-1"
    first = client.get("/api/config", params={"hostname": h}).text
    client.post(f"/api/clients/{h}/control", json={"overrides": {"dns_fail": "on"}})
    updated = client.get("/api/config", params={"hostname": h}).text
    assert updated != first
    assert updated == reference_render(spoke, configs, h)
