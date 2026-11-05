"""Kea sim-DHCP health report for CS_GET_DHCP_HEALTH.

Answers "why is the sim network not handing out IPs?" without ssh. Built from a
real incident: the config was valid and the NIC was correct, but AppArmor denied
Kea its runtime files, so it crash-looped with no lease file. Diagnosing that
took five round trips because each signal lived somewhere different (systemctl /
journal / dmesg / the conf / ip addr). ``collect()`` gathers them all and lets
the UI render the verdict.

Rule learned the hard way, three times in this one panel: never present absent
or stale data as "fine" or as a live fault. When something cannot be read, say so
in ``notes`` (unreadable lease file -> ``leases=None``, unreadable journal or
dmesg -> a note), label log tail-context as such, and age dmesg denials.
"""

# Allow modern type-hint syntax without evaluating annotations at import time.
from __future__ import annotations

# Parses Kea's JSON configuration file.
import json
# File existence/stat checks for the config and lease files.
import os
# Regular expressions for stripping config comments and parsing dmesg timestamps.
import re
# Runs systemctl, ip, journalctl, dmesg and kea-dhcp4 to gather signals.
import subprocess
# Current time, used to decide which leases are still live.
import time
# Type hints only.
from typing import Any, Dict, List, Optional, Tuple

# Path of the Kea DHCPv4 config used for the simulation network.
KEA_CONF = "/etc/kea/kea-dhcp4-sim.conf"
# systemd units that make up the sim DHCP service (server and control agent).
KEA_UNITS = ("kea-dhcp4-sim", "kea-ctrl-agent-sim")
# A denial within this many seconds (15 min) counts as "still happening".
RECENT_DENIAL_S = 900  # a denial within 15 min is "still happening"


def _run(argv, timeout=8) -> Tuple[int, str, str]:
    """Run ``argv`` and return (exit code, stdout, stderr); never raises."""
    # Run the command, capturing text output, and stop it after ``timeout`` seconds.
    try:
        # Return the exit code plus trimmed stdout and stderr.
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip()
    # Missing binary, timeout, etc.: report exit code -1 with the error text.
    except Exception as exc:  # noqa: BLE001
        return -1, "", str(exc)


def unit_states() -> Dict[str, Dict[str, str]]:
    """Return the systemd state properties of each Kea unit."""
    # Collect one properties dict per Kea unit.
    units = {}
    # Query each Kea unit.
    for unit in KEA_UNITS:
        # Ask systemd for the load/active/sub state, last result, restart count and enablement.
        _, so, _ = _run(["systemctl", "show", unit, "--no-pager",
                         "-p", "LoadState,ActiveState,SubState,Result,"
                               "NRestarts,ExecMainStatus,UnitFileState"])
        # Turn the "Key=Value" lines into a dict.
        units[unit] = dict(line.split("=", 1) for line in so.splitlines() if "=" in line)
    # Return unit name -> properties.
    return units


def read_kea_conf(conf: str, notes: List[str]) -> Dict[str, Any]:
    """The interfaces, subnet, pool and lease path Kea is actually configured with."""
    # Defaults returned when the config is missing or unreadable.
    info = {"interfaces_configured": [], "subnet": "", "pool": "", "lease_file": ""}
    # No config file: nothing to report.
    if not os.path.isfile(conf):
        return info
    # Read the raw config text.
    try:
        with open(conf) as fh:
            raw = fh.read()
        # Remove "//" comment lines (Kea allows them, JSON does not) and parse the JSON.
        cfg = json.loads(re.sub(r"^\s*//.*$", "", raw, flags=re.M))
        # Everything of interest sits under the "Dhcp4" section.
        d4 = (cfg or {}).get("Dhcp4", {}) or {}
        # Interfaces Kea is told to listen on, and the lease file path.
        info["interfaces_configured"] = (d4.get("interfaces-config") or {}).get("interfaces") or []
        info["lease_file"] = (d4.get("lease-database") or {}).get("name") or ""
        # Take the subnet and the first address pool from the first subnet, if any.
        subs = d4.get("subnet4") or []
        if subs:
            info["subnet"] = subs[0].get("subnet", "")
            pools = subs[0].get("pools") or []
            info["pool"] = (pools[0] or {}).get("pool", "") if pools else ""
    # A broken config is recorded in the notes rather than raised.
    except Exception as exc:  # noqa: BLE001
        notes.append(f"could not parse {conf}: {exc}")
    # Return what was found.
    return info


def _ipv4_addresses() -> Dict[str, List[str]]:
    """Return each network interface with its IPv4 addresses (from ``ip addr``)."""
    # List every interface's IPv4 addresses, one per line.
    _, so, _ = _run(["ip", "-o", "-4", "addr", "show"])
    # Build interface -> list of addresses.
    present: Dict[str, List[str]] = {}
    # Each line looks like "<index>: <iface> inet <addr/prefix> ...".
    for line in so.splitlines():
        parts = line.split()
        # Skip short lines; otherwise column 2 is the interface and column 4 the address.
        if len(parts) >= 4:
            present.setdefault(parts[1], []).append(parts[3])
    # Return the map.
    return present


def _nic_order() -> List[str]:
    """Non-loopback NICs in enumeration order (the installer's historical rule:
    first is the uplink, second is the sim network)."""
    # List all network links, one per line.
    _, so, _ = _run(["ip", "-o", "link", "show"])
    # Interface names in the order the system reports them.
    order = []
    # Each line looks like "<index>: <name>[@parent]: ...".
    for line in so.splitlines():
        try:
            # Extract the name and drop any "@parent" suffix.
            name = line.split(":")[1].strip().split("@")[0]
        # Skip lines that do not have that shape.
        except Exception:  # noqa: BLE001
            continue
        # Keep each non-loopback name once.
        if name and name != "lo" and name not in order:
            order.append(name)
    # Return the ordered list.
    return order


def sim_gateway(subnet: str) -> str:
    """The .1 of the sim subnet, e.g. 169.253.1.0/24 -> 169.253.1.1."""
    # An empty or non-IPv4 value has no gateway.
    if not subnet or "." not in subnet:
        return ""
    # Drop the prefix length, replace the last octet with 1.
    return subnet.split("/")[0].rsplit(".", 1)[0] + ".1"


def interface_report(want_ifaces: List[str], subnet: str) -> Dict[str, Any]:
    """Does the configured interface exist, and which NIC is really the sim network?

    A renamed NIC is the quiet killer: the conf still names ens19, the box now
    calls it ens20, Kea binds nothing and serves nobody with no error beyond
    "listening on interface ens19". Surface both the mismatch AND the likely
    replacement so the UI can name the fix. Deliberately NOT auto-rebound here:
    picking the wrong NIC would put a DHCP server on the production LAN.
    """
    # Interface -> IPv4 addresses currently present on this host.
    present = _ipv4_addresses()
    # The address the sim interface should carry.
    gw = sim_gateway(subnet)
    # Interfaces that already hold the sim gateway address.
    holder = [i for i, addrs in present.items()
              if gw and any(a.startswith(gw + "/") for a in addrs)]
    # Non-loopback interfaces in enumeration order.
    order = _nic_order()
    # Best signal for the sim NIC is the one with NO IPv4 address: the uplink
    # always has one, and the sim NIC only gets 169.253.1.1 from us. That is
    # more reliable than "second by index", which depends on enumeration order
    # surviving a rebuild. Preference: the NIC holding the sim gateway
    # (definitive) -> the address-less NIC -> index 1 (the historical rule).
    # Interfaces with no IPv4 address at all.
    bare = [n for n in order if not present.get(n)]
    # Pick the best guess for the real sim NIC, using the preference order above.
    if holder:
        suggested = holder[0]
    elif bare:
        suggested = bare[0]
    else:
        suggested = order[1] if len(order) > 1 else ""
    # Report what exists, whether every configured interface is present, what is missing, and the
    # suggested replacement NIC.
    return {
        "interfaces_present": present,
        "interface_ok": bool(want_ifaces) and all(i in present for i in want_ifaces),
        "interface_missing": [i for i in want_ifaces if i not in present],
        "sim_gateway": gw,
        "interface_holding_gateway": holder,
        "nic_order": order,
        "interfaces_without_ip": bare,
        "interface_suggested": suggested,
    }


def live_leases(lines, now: int) -> Tuple[Dict[str, List[str]], int, int]:
    """The ONE rule for "which leases are live" in a Kea memfile.

    Used by the health panel (``count_leases``) and the hub's DHCP Server
    card (``dhcp_status._read_leases``). They used to count with different
    rules and could show different numbers for the same pool.

    The memfile is APPEND-ONLY: every renew/release writes a NEW row for the
    same address, and the file only shrinks when kea-lfc compacts it. Counting
    lines reported 688 "leases" in a 244-address pool in the field. So the
    last row for an address wins (it is the address's current state), and
    that row is live only when ALL of:

    * ``valid_lifetime > 0`` — 0 is a RELEASE (Kea writes state 0 for it);
    * ``state == 0`` — 1 is declined, 2 is expired-reclaimed;
    * ``expire > now`` — a lapsed lease still sitting in the file.

    Columns: address,hwaddr,client_id,valid_lifetime,expire,subnet_id,
    fqdn_fwd,fqdn_rev,hostname,state,... (a missing state column counts as 0).

    Returns ``({address: columns}, distinct addresses, data rows)``. The dict
    keeps the order each address first appeared in the file.
    """
    # address -> (last row seen, is it live); and a count of data rows.
    latest: Dict[str, Tuple[List[str], bool]] = {}
    rows = 0
    # Walk every line of the lease file.
    for line in lines:
        # Split the CSV columns and read the address.
        cols = line.rstrip("\n").split(",")
        addr = cols[0].strip()
        # Skip blank lines and the header row.
        if not line.strip() or addr.lower() == "address":
            continue  # blank line / header
        # Count this as a data row.
        rows += 1
        # Skip rows too short to hold lifetime/expiry or with no address.
        if len(cols) < 5 or not addr:
            continue
        # Parse lifetime, expiry and state (state defaults to 0 when the column is missing).
        try:
            lifetime = int(float(cols[3] or 0))
            expire = int(float(cols[4] or 0))
            state = int(float(cols[9] or 0)) if len(cols) > 9 else 0
        # Skip rows with non-numeric values.
        except (TypeError, ValueError):
            continue
        # Later rows overwrite earlier ones, so the last state for each address wins.
        latest[addr] = (cols, lifetime > 0 and state == 0 and expire > now)
    # Keep only addresses whose latest row is live.
    live = {addr: cols for addr, (cols, is_live) in latest.items() if is_live}
    # Return live leases, distinct address count and data-row count.
    return live, len(latest), rows


def count_leases(lines, now: int) -> Tuple[int, int, int]:
    """Return (live leases, distinct addresses, data rows); see ``live_leases``."""
    # Apply the shared live-lease rule and return only the counts.
    live, addresses, rows = live_leases(lines, now)
    return len(live), addresses, rows


def lease_db_info(lease_path: str, notes: List[str]) -> Dict[str, Any]:
    """Lease DB stats: the thing that proves Kea is actually serving."""
    # Defaults: no file yet, nothing counted, readable until proven otherwise.
    info: Dict[str, Any] = {"exists": False, "leases": 0, "size": 0, "mtime": 0,
                            "readable": True}
    # Kea config named no lease file: nothing to inspect.
    if not lease_path:
        return info
    try:
        # Record the file's size and modification time.
        st = os.stat(lease_path)
        info.update({"exists": True, "size": st.st_size, "mtime": int(st.st_mtime)})
        # Count the leases in the file using the current time.
        with open(lease_path) as fh:
            leases, addresses, rows = count_leases(fh, int(time.time()))
        # Store the counts.
        info.update({"leases": leases, "addresses": addresses, "rows": rows})
        # A healthy file has rows ~= addresses; one kea-lfc has stopped
        # compacting grows without bound, so make that visible.
        # Many more rows than addresses means the file is not being compacted.
        if rows > max(50, addresses * 3):
            notes.append(f"lease file has {rows} rows for {addresses} address(es) — "
                         f"kea-lfc (Lease File Cleanup) is not compacting it")
    # No lease file yet: leave the "does not exist" defaults.
    except FileNotFoundError:
        pass
    # Some other error (usually permissions).
    except Exception as exc:  # noqa: BLE001
        # The file is 0640 _kea:_kea, so the spoke user often cannot read it.
        # Say UNKNOWN: 0 would read as "running but nobody has leased yet",
        # a much weaker claim than "I cannot tell".
        # Mark the count as unknown rather than zero, and explain why.
        info["readable"] = False
        info["leases"] = None
        notes.append(f"lease count unavailable: {exc}")
    # Return the stats.
    return info


def config_test(conf: str) -> Dict[str, Any]:
    """`kea-dhcp4 -t` catches a bad conf independent of the unit state."""
    # Validate the config with Kea's own test mode.
    rc, so, se = _run(["kea-dhcp4", "-t", conf], timeout=15)
    # Pass if it exits 0; keep the last 1500 characters of output as detail.
    return {"ok": rc == 0, "detail": (se or so)[-1500:]}


def recent_log(notes: List[str]) -> Dict[str, Any]:
    """Kea's own last error lines, or tail-context labelled as such.

    Without the label the UI showed DHCP4_LEASE_ALLOC / DHCPACK success lines
    under "Last errors", which read as a fault on a working spoke.
    """
    # Read the last 60 journal lines for the Kea DHCP unit, message text only.
    rc, so, _ = _run(["journalctl", "-u", "kea-dhcp4-sim", "--no-pager", "-n", "60", "-o", "cat"])
    # The journal could not be read: explain how to fix it.
    if rc != 0 or not so:
        notes.append("journal not readable by the spoke user — add it to the "
                     "systemd-journal group to surface Kea's own errors here")
        return {"last_errors": []}
    # Keep only lines that look like real errors.
    fatal = [l for l in so.splitlines() if "ERROR" in l or "Fatal" in l or "DENIED" in l]
    # Show the last 6 errors, or the last 3 lines as context, and say which it is.
    return {"last_errors": fatal[-6:] or so.splitlines()[-3:],
            "last_errors_are_fatal": bool(fatal)}


def _uptime_s() -> float:
    """Return seconds since boot, or 0.0 if unknown."""
    # /proc/uptime starts with the seconds since boot.
    try:
        with open("/proc/uptime") as fh:
            return float(fh.read().split()[0])
    # Not Linux or unreadable: report 0 (ages then cannot be computed).
    except Exception:  # noqa: BLE001
        return 0.0


def denial_age_s(line: str, uptime_s: float) -> Optional[int]:
    """Age of a dmesg line from its leading "[   1105.38]" (seconds since boot)."""
    # Extract the whole-seconds-since-boot timestamp at the start of the line.
    m = re.match(r"^\[\s*(\d+)\.\d+\]", line)
    # No timestamp or unknown uptime: the age cannot be computed.
    if not m or not uptime_s:
        return None
    # Age = uptime now minus the event time (never negative).
    return max(0, int(uptime_s - int(m.group(1))))


def apparmor_report(notes: List[str]) -> Dict[str, Any]:
    """AppArmor denials naming a kea profile, each with its age.

    dmesg is a RING BUFFER: a denial from hours ago stays in it and read as if
    it were happening now, which made a recovered spoke look broken. Report the
    age and whether any denial is recent; the UI greys out old ones.
    """
    # Read the kernel ring buffer.
    rc, so, _ = _run(["dmesg"], timeout=10)
    # dmesg is often restricted for non-root users: explain and suggest a manual command.
    if rc != 0 or not so:
        notes.append("dmesg not readable (kernel.dmesg_restrict) — AppArmor "
                     "denials cannot be shown; run: dmesg | grep -i 'apparmor.*kea'")
        return {"apparmor_denials": []}
    # Keep only AppArmor denial lines that mention Kea.
    denials = [l for l in so.splitlines()
               if "apparmor" in l.lower() and "kea" in l.lower() and "DENIED" in l]
    # Attach an age to each of the last 8 denials.
    uptime = _uptime_s()
    rows = [{"line": l, "age_s": denial_age_s(l, uptime)} for l in denials[-8:]]
    # Return the plain lines (for older UIs), the rows with ages, and whether any is recent.
    return {
        "apparmor_denials": [r["line"] for r in rows],  # older UI reads this
        "apparmor_denial_rows": rows,
        "apparmor_recent": any(r["age_s"] is not None and r["age_s"] <= RECENT_DENIAL_S
                               for r in rows),
    }


def collect(conf: str = KEA_CONF) -> Dict[str, Any]:
    """Gather every sim-DHCP health signal into one report for CS_GET_DHCP_HEALTH."""
    # Problems found while collecting are appended here and returned to the UI.
    notes: List[str] = []
    # Start the report with the unit states and whether the config file exists.
    out: Dict[str, Any] = {"status": "SUCCESS", "units": unit_states(), "notes": notes,
                           "conf_path": conf, "conf_exists": os.path.isfile(conf)}
    # Add the configured interfaces, subnet, pool and lease file.
    kea = read_kea_conf(conf, notes)
    out.update(kea)
    # Add the interface checks for the configured interfaces and subnet.
    out.update(interface_report(kea["interfaces_configured"], kea["subnet"]))
    # Add lease-file stats and the config validation result.
    out["lease_db"] = lease_db_info(kea["lease_file"], notes)
    out["config_test"] = config_test(conf)
    # Add recent Kea log lines and AppArmor denials.
    out.update(recent_log(notes))
    out.update(apparmor_report(notes))
    # Return the complete health report.
    return out
