"""DHCP-server status collector for the cs (Client-Simulation) spoke.

Reads the cs-OWNED Kea DHCP4 instance (``kea-dhcp4-sim``) that ``install_cs.sh``
provisions on the spoke's second NIC — a SEPARATE Kea instance from the
``lm/dhcp`` module's, so both coexist on an all-in-one box — and returns a small
status dict: installed? running? DHCP pool size, active leases, and
utilization %. The result rides the spoke's 10 s ``CS_TELEMETRY`` frame (see
``proxmox_deploy.relay_payload``) up to the hub, which caches it in
``simulations_cache`` for the Setup → Simulations "DHCP Server" card.

Defensive by design: this runs in the telemetry hot path, so ``collect_dhcp_status``
never raises — a parse/IO failure degrades to ``{installed, running, error}``
rather than killing the relay loop. It only reads world-readable files
(``/etc/kea/kea-dhcp4-sim.conf``, ``/etc/network/interfaces.d/<iface>.conf``,
the Kea memfile lease CSV) and runs the non-privileged ``systemctl is-active
kea-dhcp4-sim``, so it works under the ``svc_lm`` service user. It deliberately
does NOT call the ctrl-agent (:8102) HTTP RPC in this hot path — the memfile CSV
read is far cheaper. Mirrors the subprocess idiom in
``simulation_engine._find_iface`` and the status shape of the sibling
``lm/dhcp`` Kea spoke's ``KeaManager.status()``.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import shutil
import subprocess
import time
from itertools import islice
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from dhcp_health import live_leases

logger = logging.getLogger("CSDhcpStatus")

# cs-owned Kea instance (distinct from the lm/dhcp module's kea-dhcp4-server).
KEA_DHCP4_CONF = "/etc/kea/kea-dhcp4-sim.conf"
KEA_SERVICE = "kea-dhcp4-sim"
DEFAULT_LEASE_FILE = "/var/lib/kea/kea-leases4-sim.csv"
_MAX_LEASE_ROWS = 50

# Dedupe the hot-path failure log: this runs every 10s on the CS_TELEMETRY frame,
# so a persistent condition (e.g. /etc/kea not readable by svc_lm — a permission
# drift the installer is supposed to prevent) must NOT re-log at WARNING every
# tick. Log WARNING once when the error first appears (and again when it changes
# or clears), DEBUG while it's steady. Mirrors the level discipline the polled
# CS_* handlers already follow (cs_spoke.handle_command).
_last_collect_error: Optional[str] = None


def collect_dhcp_status() -> Dict[str, Any]:
    """Return the cs-owned Kea (kea-dhcp4-sim) DHCP-server status for the spoke.

    Never raises — wraps every probe so a broken parse can't destabilize the
    telemetry loop. Shape:
        {installed, running, iface, subnet, iface_address, pool_start,
         pool_end, pool_size, leases_used, leases_readable, leases_free,
         utilization_pct, lease_file, lease_time, leases[], ts}
    ``leases_used``/``leases_free``/``utilization_pct`` are ``None`` (and
    ``leases_readable`` False) when the lease file exists but can't be read.
    When Kea is not installed → ``{"installed": False}`` (the WebUI shows
    "Not configured"). On an unexpected error → ``{"installed": True,
    "running": False, "error": ...}``.
    """
    global _last_collect_error
    try:
        result = _collect()
    except Exception as exc:  # noqa: BLE001 — telemetry hot path; never propagate
        msg = str(exc) or repr(exc)
        # Throttle: WARNING only when the error is new/changed (so a broken box is
        # loud once), DEBUG while it persists (so the 10s loop doesn't flood the
        # log — the original code logged WARNING every tick).
        if msg != _last_collect_error:
            logger.warning("dhcp_status collection failed: %s", exc)
            _last_collect_error = msg
        else:
            logger.debug("dhcp_status collection still failing: %s", exc)
        return {"installed": True, "running": False, "error": msg}
    # Recovered — announce the clear so the log shows the transition.
    if _last_collect_error is not None:
        logger.info("dhcp_status collection recovered")
        _last_collect_error = None
    return result


def _collect() -> Dict[str, Any]:
    conf_path = Path(KEA_DHCP4_CONF)
    # "installed" must mean the SIM instance exists (its conf) — not merely that
    # the shared kea-dhcp4 binary is present (the lm/dhcp module installs that too).
    # Keying on the binary made a box that never provisioned the sim Kea show a
    # false "Not running" instead of the intended "Not configured".
    installed = conf_path.exists()
    if not installed:
        return {"installed": False}

    iface, pool_start, pool_end, lease_time, lease_file = _parse_kea_conf(conf_path)
    lease_file = lease_file or DEFAULT_LEASE_FILE
    running = _is_running()

    now = time.time()
    leases_used, leases = _read_leases(lease_file, now)
    pool_size = _pool_size(pool_start, pool_end)
    if leases_used is None:
        # Lease file unreadable (it is 0640 _kea:_kea): say "unknown", not
        # "0 used / 100% free" — the same fix the health panel got in #55.
        leases_free = utilization_pct = None
    else:
        leases_free = max(pool_size - leases_used, 0) if pool_size else 0
        utilization_pct = round(leases_used / pool_size * 100, 1) if pool_size else 0.0

    subnet, iface_address = _iface_subnet(iface)

    return {
        "installed":       True,
        "running":         running,
        "iface":           iface,
        "subnet":          subnet,
        "iface_address":   iface_address,
        "pool_start":      pool_start,
        "pool_end":        pool_end,
        "pool_size":       pool_size,
        "leases_used":     leases_used,      # None = lease file unreadable
        "leases_readable": leases_used is not None,
        "leases_free":     leases_free,
        "utilization_pct": utilization_pct,
        "lease_file":      lease_file,
        "lease_time":      lease_time,
        "leases":          leases,
        "ts":              now,
    }


def _parse_kea_conf(conf_path: Path):
    """Return (iface, pool_start, pool_end, lease_time, lease_file) from the
    cs kea-dhcp4-sim JSON config. Reads the single sim ``subnet4`` pool, the
    bound interface, ``valid-lifetime`` (seconds), and the memfile lease path.
    Any field may be ``None`` if absent/unparseable."""
    iface = pool_start = pool_end = lease_time = lease_file = None
    if not conf_path.exists():
        return iface, pool_start, pool_end, lease_time, lease_file
    try:
        dhcp4 = json.loads(conf_path.read_text(errors="replace")).get("Dhcp4", {})
    except Exception:
        return iface, pool_start, pool_end, lease_time, lease_file

    ifaces = (dhcp4.get("interfaces-config", {}) or {}).get("interfaces", []) or []
    if ifaces:
        # Kea allows "eth1" or "eth1/169.253.1.1"; keep just the interface name.
        iface = str(ifaces[0]).split("/", 1)[0].strip() or None

    vl = dhcp4.get("valid-lifetime")
    if isinstance(vl, (int, float)):
        lease_time = int(vl)

    lease_file = ((dhcp4.get("lease-database", {}) or {}).get("name")) or None

    subnets = dhcp4.get("subnet4", []) or []
    if subnets:
        pools = (subnets[0] or {}).get("pools", []) or []
        if pools:
            # Kea pool form: "169.253.1.11 - 169.253.1.254" (or a "start-end"
            # without spaces). Split on the dash and strip.
            spec = (pools[0] or {}).get("pool", "")
            if "-" in spec:
                lo, _, hi = spec.partition("-")
                pool_start = lo.strip() or None
                pool_end = hi.strip() or None
    return iface, pool_start, pool_end, lease_time, lease_file


# Process-probe fallback pattern: matches the SIM kea-dhcp4 instance by its
# conf path — only the -sim instance uses kea-dhcp4-sim.conf, so the lm/dhcp
# module's kea-dhcp4 (a different conf) is never counted. [.] is a literal dot
# in ERE, so this is precise without needing the re module.
_KEA_SIM_PROC_RE = "kea-dhcp4-sim[.]conf"


def _kea_sim_running_pgrep() -> bool:
    """Fallback liveness probe for hosts where systemctl can't reach systemd
    (e.g. a container without systemd as PID 1). Matches the SIM instance by
    its conf path. Only called when systemctl is absent or reports 'not booted
    with systemd' — NEVER to override a real inactive/failed, which would mask
    a genuine crash-loop (e.g. the Kea >= 2.6.3 /run/kea permission failure)."""
    if not shutil.which("pgrep"):
        return False
    try:
        r = subprocess.run(
            ["pgrep", "-f", _KEA_SIM_PROC_RE],
            capture_output=True, text=True, timeout=3,
        )
        return r.returncode == 0 and bool(r.stdout.strip())
    except Exception:
        return False


def _is_running() -> bool:
    """True iff the cs kea-dhcp4-sim unit is active.

    Primary signal is ``systemctl is-active`` (authoritative when systemd is
    PID 1). We only fall back to the process probe when systemctl is absent or
    can't reach systemd (container without init) — NEVER to override a real
    inactive/failed/activating, which would mask a genuine crash-loop such as
    the Kea >= 2.6.3 control-socket permission failure (/run/kea must be 0750)."""
    if shutil.which("systemctl"):
        try:
            r = subprocess.run(
                ["systemctl", "is-active", KEA_SERVICE],
                capture_output=True, text=True, timeout=3,
            )
            out = r.stdout.strip()
            # systemctl reached systemd and reported a unit state — trust it.
            # 'activating' (auto-restart), 'failed', 'inactive' are NOT running.
            if out in ("active", "inactive", "failed", "activating",
                       "deactivating", "reloading"):
                return out == "active"
            # "System has not been booted with systemd as init system" or
            # "Unit ... could not be found" → fall through to the process probe.
        except Exception:
            pass
    return _kea_sim_running_pgrep()


def _read_leases(lease_file: str, now: float) -> Tuple[Optional[int], List[Dict[str, Any]]]:
    """Live leases in the Kea memfile lease CSV (``kea-leases4-sim.csv``).

    "Live" is ``dhcp_health.live_leases`` — the same rule the health panel
    uses, so the card and the panel can't disagree. Returns
    ``(count, rows)``:

    * ``count`` is exact (NOT capped). ``0`` when the file doesn't exist yet
      (Kea hasn't handed out a lease), ``None`` when it exists but can't be
      read (it is 0640 _kea:_kea and the spoke user often can't read it).
    * ``rows`` is ``{ip, mac, hostname, expiry}`` per live lease (the shape
      the old dnsmasq reader produced), capped at ``_MAX_LEASE_ROWS`` to keep
      the telemetry payload small."""
    try:
        with open(lease_file, errors="replace") as fh:
            live, _addresses, _rows = live_leases(fh, int(now))
    except FileNotFoundError:
        return 0, []
    except Exception:  # noqa: BLE001
        return None, []
    rows = [{"mac": cols[1].strip(),
             "ip": addr,
             "hostname": cols[8].strip() if len(cols) > 8 else "",
             "expiry": int(float(cols[4]))}
            for addr, cols in islice(live.items(), _MAX_LEASE_ROWS)]
    return len(live), rows


def _pool_size(pool_start: Optional[str], pool_end: Optional[str]) -> int:
    """Count of IPv4 addresses from pool_start..pool_end inclusive (0 if unset/invalid)."""
    try:
        s = ipaddress.IPv4Address(pool_start)
        e = ipaddress.IPv4Address(pool_end)
    except Exception:
        return 0
    if int(e) < int(s):
        return 0
    return int(e) - int(s) + 1


def _iface_subnet(iface: Optional[str]):
    """Read /etc/network/interfaces.d/<iface>.conf for the spoke's address +
    netmask → (subnet_cidr, address). Best-effort: (None, None) if absent."""
    if not iface:
        return None, None
    path = Path(f"/etc/network/interfaces.d/{iface}.conf")
    if not path.exists():
        return None, None
    addr = netmask = None
    try:
        for raw in path.read_text(errors="replace").splitlines():
            line = raw.strip()
            if line.startswith("address "):
                addr = line.split()[1]
            elif line.startswith("netmask "):
                netmask = line.split()[1]
    except Exception:
        return None, None
    if not addr:
        return None, None
    try:
        if netmask:
            net = ipaddress.IPv4Network(f"{addr}/{netmask}", strict=False)
        else:
            net = ipaddress.IPv4Network(f"{addr}/24", strict=False)
        return str(net), addr
    except Exception:
        return None, addr