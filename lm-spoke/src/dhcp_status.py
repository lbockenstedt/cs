"""DHCP-server status collector for the cs (Client-Simulation) spoke.

Reads the dnsmasq state that ``install_cs.sh`` (the port of
``installers/install-lxc.sh`` STEP 3) provisions on the spoke's second NIC and
returns a small status dict — installed? running? DHCP pool size, active
leases, and utilization %. The result rides the spoke's 10 s ``CS_TELEMETRY``
frame (see ``proxmox_deploy.relay_payload``) up to the hub, which caches it in
``simulations_cache`` for the Setup → Simulations "DHCP Server" card.

Defensive by design: this runs in the telemetry hot path, so ``collect_dhcp_status``
never raises — a parse/IO failure degrades to ``{installed, running, error}``
rather than killing the relay loop. It only reads world-readable files
(``/etc/dnsmasq.d/client-sim.conf``, ``/etc/network/interfaces.d/<iface>.conf``,
the dnsmasq lease file) and runs the non-privileged ``systemctl is-active
dnsmasq``, so it works under the ``svc_lm`` service user. Mirrors the
subprocess idiom in ``simulation_engine._find_iface`` and the status shape of
the sibling ``lm/dhcp`` Kea spoke's ``KeaManager.status()``.
"""

from __future__ import annotations

import ipaddress
import logging
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("CSDhcpStatus")

DNSMASQ_CONF = "/etc/dnsmasq.d/client-sim.conf"
DEFAULT_LEASE_FILE = "/var/lib/misc/dnsmasq.leases"
_MAX_LEASE_ROWS = 50


def collect_dhcp_status() -> Dict[str, Any]:
    """Return the dnsmasq DHCP-server status for the spoke.

    Never raises — wraps every probe so a broken parse can't destabilize the
    telemetry loop. Shape:
        {installed, running, iface, subnet, iface_address, pool_start,
         pool_end, pool_size, leases_used, leases_free, utilization_pct,
         lease_file, lease_time, leases[], ts}
    When dnsmasq is not installed → ``{"installed": False}`` (the WebUI shows
    "Not configured"). On an unexpected error → ``{"installed": True,
    "running": False, "error": ...}``.
    """
    try:
        return _collect()
    except Exception as exc:  # noqa: BLE001 — telemetry hot path; never propagate
        logger.warning("dhcp_status collection failed: %s", exc)
        return {"installed": True, "running": False, "error": str(exc) or repr(exc)}


def _collect() -> Dict[str, Any]:
    conf_path = Path(DNSMASQ_CONF)
    installed = shutil.which("dnsmasq") is not None or conf_path.exists()
    if not installed:
        return {"installed": False}

    iface, pool_start, pool_end, lease_time, lease_file = _parse_dnsmasq_conf(conf_path)
    lease_file = lease_file or DEFAULT_LEASE_FILE
    running = _is_running()

    now = time.time()
    leases = _read_leases(lease_file, now)
    leases_used = len(leases)
    pool_size = _pool_size(pool_start, pool_end)
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
        "leases_used":     leases_used,
        "leases_free":     leases_free,
        "utilization_pct": utilization_pct,
        "lease_file":      lease_file,
        "lease_time":      lease_time,
        "leases":          leases,
        "ts":              now,
    }


def _parse_dnsmasq_conf(conf_path: Path):
    """Return (iface, pool_start, pool_end, lease_time, lease_file) from the
    dnsmasq drop-in. Any field may be ``None`` if absent/unparseable."""
    iface = pool_start = pool_end = lease_time = lease_file = None
    if not conf_path.exists():
        return iface, pool_start, pool_end, lease_time, lease_file
    try:
        text = conf_path.read_text(errors="replace")
    except Exception:
        return iface, pool_start, pool_end, lease_time, lease_file
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("interface="):
            # Skips `except-interface=lo` (different prefix).
            iface = line.split("=", 1)[1].strip() or iface
        elif line.startswith("dhcp-range="):
            parts = line.split("=", 1)[1].split(",")
            if len(parts) >= 1:
                pool_start = parts[0].strip() or None
            if len(parts) >= 2:
                pool_end = parts[1].strip() or None
            if len(parts) >= 3:
                lease_time = parts[2].strip() or None
        elif line.startswith("dhcp-leasefile="):
            lease_file = line.split("=", 1)[1].strip() or None
    return iface, pool_start, pool_end, lease_time, lease_file


def _is_running() -> bool:
    """True iff dnsmasq is active under systemd (non-privileged query)."""
    if not shutil.which("systemctl"):
        return False
    try:
        r = subprocess.run(
            ["systemctl", "is-active", "dnsmasq"],
            capture_output=True, text=True, timeout=3,
        )
        return r.returncode == 0 and r.stdout.strip() == "active"
    except Exception:
        return False


def _read_leases(lease_file: str, now: float) -> List[Dict[str, Any]]:
    """Parse the dnsmasq lease file (``<expiry> <mac> <ip> <hostname> [<clientid>]``).
    Drops expired entries (expiry 0 = infinite/static lease → kept). Caps at
    ``_MAX_LEASE_ROWS`` for payload size; the count is still exact up to that cap."""
    out: List[Dict[str, Any]] = []
    try:
        text = Path(lease_file).read_text(errors="replace")
    except Exception:
        return out
    for raw in text.splitlines():
        parts = raw.split()
        if len(parts) < 4:
            continue
        try:
            expiry = int(parts[0])
        except ValueError:
            continue
        if expiry != 0 and expiry < now:
            continue  # expired
        out.append({
            "mac": parts[1], "ip": parts[2],
            "hostname": parts[3], "expiry": expiry,
        })
        if len(out) >= _MAX_LEASE_ROWS:
            break
    return out


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