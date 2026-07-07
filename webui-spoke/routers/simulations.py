"""Simulations API routes (moved verbatim from server.py; logic imported from server)."""
from __future__ import annotations

from fastapi import APIRouter
from server import (
    Any,
    REPO_DIR,
    _fetch_central_client_names,
    _merge_ini_override,
    _sim_conf_cache,
    _sim_clients_cache,
    asyncio,
    clients,
    compute_online,
    configparser,
    copy,
    datetime,
    logger,
    re,
    settings,
    state,
    state_lock,
    timezone,
    zlib,
)

router = APIRouter()




@router.get("/api/simulations")
async def api_simulations() -> dict[str, Any]:
    """Return simulation groups with client membership and Central PASS/FAIL status.

    Reads configs/simulation.conf for bucket profiles and proxmox/client-setup.conf
    for VMID→username mappings. Matches configured clients against live heartbeats
    and looks up Central alert status per simulation wsite + central_check.
    """
    sim_conf_path = REPO_DIR / "configs" / "simulation.conf"
    client_conf_path = REPO_DIR / "proxmox" / "client-setup.conf"

    sim_mtime = sim_conf_path.stat().st_mtime if sim_conf_path.exists() else -1.0
    client_mtime = client_conf_path.stat().st_mtime if client_conf_path.exists() else -1.0

    if (sim_mtime == _sim_conf_cache["sim_mtime"] and
            client_mtime == _sim_conf_cache["client_mtime"]):
        simulations: dict[str, dict[str, Any]] = copy.deepcopy(_sim_conf_cache["simulations"])
    else:
        simulations = {}

        # ── Parse simulation.conf ─────────────────────────────────────
        if sim_conf_path.exists():
            try:
                parser = configparser.ConfigParser()
                parser.read_string(sim_conf_path.read_text(encoding="utf-8"))
                # Apply hub-managed override on top (hub-connected mode only)
                _merge_ini_override(parser, REPO_DIR / "configs" / "hub-sim-overrides.conf")

                # Per-bucket test keys (read from [sN] sections)
                _BUCKET_TEST_KEYS = [
                    "dns_fail", "assoc_fail", "dhcp_fail", "port_flap",
                    "iperf", "www_traffic", "download", "ping_test",
                ]
                # Global test keys (read from [simulation] section, applied to all buckets)
                # WHY: ssidpw_fail and auth_fail are global settings in simulation.conf
                # but simulation.sh/dashboard.sh include them in active_simulations POSTs.
                _GLOBAL_TEST_KEYS = ["ssidpw_fail", "auth_fail"]
                global_tests = {
                    k: parser.get("simulation", k, fallback="off").strip().lower() == "on"
                    for k in _GLOBAL_TEST_KEYS
                }

                sim_section_re = re.compile(r"^s\d$")
                for section in parser.sections():
                    if not sim_section_re.match(section):
                        continue
                    bucket_tests = {
                        k: parser.get(section, k, fallback="off").strip().lower() == "on"
                        for k in _BUCKET_TEST_KEYS
                    }
                    simulations[section] = {
                        "id": section,
                        "wsite": parser.get(section, "wsite", fallback=""),
                        "central_check": parser.get(section, "central_check", fallback="").strip(),
                        "tests": {**bucket_tests, **global_tests},
                        "configured_clients": [],
                        "active_client_count": 0,
                        "central_pass_fail": None,
                    }
            except Exception as exc:
                logger.warning("api_simulations: could not parse simulation.conf: %s", exc)

        # ── Parse client-setup.conf — build VMID→hostname mapping ────
        if client_conf_path.exists():
            try:
                client_parser = configparser.ConfigParser()
                client_parser.read_string(client_conf_path.read_text(encoding="utf-8"))

                vmid_section_re = re.compile(r"^c(\d+)$")
                for section in client_parser.sections():
                    m = vmid_section_re.match(section)
                    if not m:
                        continue
                    vmid_str = m.group(1)
                    vmid = int(vmid_str)
                    vm_name = client_parser.get(section, "vm_name", fallback="").strip()
                    if not vm_name:
                        continue

                    # Hash the vm_name to assign bucket — matches zlib.crc32 used by clients.
                    sim_id = f"s{zlib.crc32(vm_name.encode()) % 10}"

                    if sim_id in simulations:
                        simulations[sim_id]["configured_clients"].append({
                            "hostname": vm_name,
                            "vmid": vmid,
                            "username": vm_name,
                            "reporting": False,
                            "online": False,
                            "last_seen": None,
                        })
            except Exception as exc:
                logger.warning("api_simulations: could not parse client-setup.conf: %s", exc)

        _sim_conf_cache.update({
            "sim_mtime": sim_mtime,
            "client_mtime": client_mtime,
            "simulations": copy.deepcopy(simulations),
        })

    # ── Match active clients + compute Central PASS/FAIL ─────────
    async with state_lock:
        active_snap = {h: dict(c) for h, c in clients.items()}

    for sim in simulations.values():
        active_count = 0

        # Primary: count any live client whose simulation_id matches this bucket
        for h, c in active_snap.items():
            if c.get("simulation_id", "") == sim["id"]:
                online = compute_online(c.get("last_seen", datetime.min.replace(tzinfo=timezone.utc)))
                if online:
                    active_count += 1

        # Secondary: update configured_clients reporting flags (for detail panel)
        for client_info in sim["configured_clients"]:
            h = client_info["hostname"]
            if h in active_snap:
                c = active_snap[h]
                online = compute_online(c.get("last_seen", datetime.min.replace(tzinfo=timezone.utc)))
                last_seen_dt = c.get("last_seen")
                client_info["reporting"] = True
                client_info["online"] = online
                client_info["last_seen"] = last_seen_dt.isoformat() if last_seen_dt else None

        sim["active_client_count"] = active_count
        sim["central_client_count"] = state.central_wireless_clients.get(sim["wsite"], None)

        # Central PASS/FAIL — look up wsite + central_check in polled status
        wsite = sim["wsite"]
        check_id = sim["central_check"]
        if wsite and check_id:
            site_checks = state.central_status.get(wsite, {})
            if check_id in site_checks:
                info = site_checks[check_id]
                sim["central_pass_fail"] = {
                    "firing": info["status"] == "OK",
                    "count": info["count"],
                    "check_name": info["check_name"],
                    "ts": info["ts"],
                }
            else:
                sim["central_pass_fail"] = {"firing": False, "count": 0, "check_name": check_id, "ts": None}

    return {
        "simulations": list(simulations.values()),
    }




@router.get("/api/simulations/{sim_id}/clients")
async def api_sim_clients(sim_id: str) -> dict[str, Any]:
    """Return per-client status for one simulation bucket.

    Each client entry includes:
      - api_online / api_last_seen — from live heartbeats
      - central_connected — matched by hostname from Central wireless client list
    """
    import configparser as _cp

    sim_conf_path = REPO_DIR / "configs" / "simulation.conf"
    client_conf_path = REPO_DIR / "proxmox" / "client-setup.conf"

    sim_mtime = sim_conf_path.stat().st_mtime if sim_conf_path.exists() else -1.0
    client_mtime = client_conf_path.stat().st_mtime if client_conf_path.exists() else -1.0

    # The wsite/central_site + configured-client dict are derived purely from
    # the two conf files + the static site_mappings setting — re-parsing them
    # on every call was wasted work. Memoize keyed on (sim_id, mtimes); the
    # live heartbeat overlay + Central fetch below stay uncached. A deepcopy of
    # `configured` is returned so the per-call overlay mutations don't leak
    # into the cached entry.
    cached = _sim_clients_cache.get(sim_id)
    if (cached and cached[0] == sim_mtime and cached[1] == client_mtime):
        wsite = cached[2]["wsite"]
        central_site = cached[2]["central_site"]
        configured: dict[str, dict[str, Any]] = copy.deepcopy(cached[2]["configured"])
    else:
        # --- Load simulation profile ---
        wsite = ""
        central_site = ""
        if sim_conf_path.exists():
            try:
                p = _cp.ConfigParser()
                p.read_string(sim_conf_path.read_text(encoding="utf-8"))
                _merge_ini_override(p, REPO_DIR / "configs" / "hub-sim-overrides.conf")
                if p.has_section(sim_id):
                    wsite = p.get(sim_id, "wsite", fallback="")
            except Exception:
                pass

        central_site = settings.get("site_mappings", {}).get(wsite, "")

        # --- Build configured client list from client-setup.conf ---
        configured = {}  # hostname → info
        if client_conf_path.exists():
            try:
                cp = _cp.ConfigParser()
                cp.read_string(client_conf_path.read_text(encoding="utf-8"))
                vmid_re = re.compile(r"^c(\d+)$")
                for section in cp.sections():
                    m = vmid_re.match(section)
                    if not m:
                        continue
                    vmid_str = m.group(1)
                    vm_name = cp.get(section, "vm_name", fallback="").strip()
                    if not vm_name:
                        continue
                    if f"s{zlib.crc32(vm_name.encode()) % 10}" != sim_id:
                        continue
                    configured[vm_name] = {
                        "hostname": vm_name,
                        "vmid": int(vmid_str),
                        "api_online": False,
                        "api_last_seen": None,
                        "central_connected": None,
                        "source": "configured",
                    }
            except Exception:
                pass
        _sim_clients_cache[sim_id] = (sim_mtime, client_mtime, {
            "wsite": wsite, "central_site": central_site, "configured": configured,
        })

    # --- Overlay live heartbeat data ---
    async with state_lock:
        active_snap = {h: dict(c) for h, c in clients.items()}

    for h, c in active_snap.items():
        if c.get("simulation_id", "") != sim_id:
            continue
        online = compute_online(c.get("last_seen", datetime.min.replace(tzinfo=timezone.utc)))
        last_seen_dt = c.get("last_seen")
        active_sims = list(c.get("active_simulations", []))
        if h in configured:
            configured[h]["api_online"] = online
            configured[h]["api_last_seen"] = last_seen_dt.isoformat() if last_seen_dt else None
            configured[h]["active_simulations"] = active_sims
        else:
            configured[h] = {
                "hostname": h,
                "vmid": None,
                "api_online": online,
                "api_last_seen": last_seen_dt.isoformat() if last_seen_dt else None,
                "active_simulations": active_sims,
                "central_connected": None,
                "source": "heartbeat",
            }

    # --- Match against Central client list ---
    central_names: list[str] = []
    if central_site:
        try:
            central_names = await asyncio.wait_for(
                _fetch_central_client_names(wsite, central_site), timeout=15
            )
        except Exception:
            pass

    central_set = {n.lower() for n in central_names}
    for info in configured.values():
        if central_set:
            info["central_connected"] = info["hostname"].lower() in central_set
        # else leave None (not configured / fetch failed)

    return {
        "sim_id": sim_id,
        "wsite": wsite,
        "central_site": central_site,
        "central_total": state.central_wireless_clients.get(wsite, None),
        "clients": sorted(configured.values(), key=lambda x: x["hostname"]),
    }
