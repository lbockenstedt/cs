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
does NOT call the ctrl-agent (:8002) HTTP RPC in this hot path — the memfile CSV
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
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("CSDhcpStatus")

# cs-owned Kea instance (distinct from the lm/dhcp module's kea-dhcp4-server).
KEA_DHCP4_CONF = "/etc/kea/kea-dhcp4-sim.conf"
KEA_SERVICE = "kea-dhcp4-sim"
DEFAULT_LEASE_FILE = "/var/lib/kea/kea-leases4-sim.csv"
_MAX_LEASE_ROWS = 50


def collect_dhcp_status() -> Dict[str, Any]:
    """Return the cs-owned Kea (kea-dhcp4-sim) DHCP-server status for the spoke.

    Never raises — wraps every probe so a broken parse can't destabilize the
    telemetry loop. Shape:
        {installed, running, iface, subnet, iface_address, pool_start,
         pool_end, pool_size, leases_used, leases_free, utilization_pct,
         lease_file, lease_time, leases[], ts}
    When Kea is not installed → ``{"installed": False}`` (the WebUI shows
    "Not configured"). On an unexpected error → ``{"installed": True,
    "running": False, "error": ...}``.
    """
    try:
        return _collect()
    except Exception as exc:  # noqa: BLE001 — telemetry hot path; never propagate
        logger.warning("dhcp_status collection failed: %s", exc)
        return {"installed": True, "running": False, "error": str(exc) or repr(exc)}


def _collect() -> Dict[str, Any]:
    conf_path = Path(KEA_DHCP4_CONF)
    installed = shutil.which("kea-dhcp4") is not None or conf_path.exists()
    if not installed:
        return {"installed": False}

    iface, pool_start, pool_end, lease_time, lease_file = _parse_kea_conf(conf_path)
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


def _is_running() -> bool:
    """True iff the cs kea-dhcp4-sim unit is active under systemd (non-privileged)."""
    if not shutil.which("systemctl"):
        return False
    try:
        r = subprocess.run(
            ["systemctl", "is-active", KEA_SERVICE],
            capture_output=True, text=True, timeout=3,
        )
        return r.returncode == 0 and r.stdout.strip() == "active"
    except Exception:
        return False


def _read_leases(lease_file: str, now: float) -> List[Dict[str, Any]]:
    """Parse the Kea memfile lease CSV (``kea-leases4-sim.csv``).

    Kea leases4 columns:
        address,hwaddr,client_id,valid_lifetime,expire,subnet_id,fqdn_fwd,
        fqdn_rev,hostname,state,user_context,pool_id
    Kea APPENDS a new row on every lease change, so the LAST row for an address
    is authoritative (last-wins). Drops rows whose ``expire`` unix timestamp is
    in the past and rows with ``state != 0`` (0 = default/active; 1 = declined,
    2 = expired-reclaimed). Maps to the same ``{ip, mac, hostname, expiry}``
    shape the dnsmasq reader produced. Caps at ``_MAX_LEASE_ROWS`` for payload
    size; the count is still exact up to that cap."""
    out: List[Dict[str, Any]] = []
    try:
        text = Path(lease_file).read_text(errors="replace")
    except Exception:
        return out

    # Collapse to last-wins per address (Kea appends updates to the same file).
    latest: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    for line in text.splitlines():
        row = line.split(",")
        if len(row) < 10:
            continue
        ip = row[0].strip()
        if not ip or ip.lower() == "address":  # header / blank line
            continue
        try:
            expire = int(row[4])
            state = int(row[9])
        except ValueError:
            continue
        if ip not in latest:
            order.append(ip)
        latest[ip] = {
            "mac":      row[1].strip(),
            "ip":       ip,
            "hostname": row[8].strip(),
            "expiry":   expire,
            "_state":   state,
        }

    for ip in order:
        rec = latest[ip]
        if rec.pop("_state", 0) != 0:
            continue  # declined / reclaimed — not an active lease
        if rec["expiry"] < now:
            continue  # expired
        out.append(rec)
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