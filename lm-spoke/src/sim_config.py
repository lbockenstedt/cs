"""Simulation configuration model — the single resolver authority.

The cs spoke resolves a client's effective simulation profile the same way
regardless of who asks: the local ``SimulationEngine``, the ``/api/config``
mgmt route, and the ``CS_GET_SIMULATION_STATE`` LM-spoke command all call
:func:`resolve_profile`. One resolver, three callers.

Resolution order (last wins), mirroring ``configs/README.md``::

    [simulation] globals      (simulation.conf)
          ↓
    [address] / [server]      (simulation.conf — network targets + server URL)
          ↓
    [sX] bucket profile       (s0–s9, chosen by ``zlib.crc32(hostname) % 10``)
          ↓
    [username] override       (user-overrides.conf — may also pin ``simulation_id``)

The shipped ``clients/linux/simulation.sh`` only reads ``simulation.conf`` (it cannot
load a second INI without resetting section state), so the server bakes
per-username overrides into the file it serves via ``/api/config``. This Python
resolver loads both files directly — the canonical behaviour the webui-local hub used.
"""

from __future__ import annotations

import configparser
import io
import logging
import os
import re
import zlib
from pathlib import Path
from typing import Dict, Optional, Tuple

logger = logging.getLogger("CSConfig")

# Keys that may appear in [simulation]/[sX]/[username]. Used only to validate
# ``simulation_id`` pin values and to keep the override surface explicit.
SIM_FLAG_KEYS = (
    "kill_switch", "rapid_update", "sim_load", "github_repo", "repo_location",
    "site_based_ssid", "iperf_bw", "ssidpw_fail", "auth_fail", "allow_offline",
    "web_server", "server_url",
    "wsite", "ssid", "ssidpw", "dhcp_fail", "dns_fail", "assoc_fail",
    "port_flap", "ping_test", "download", "iperf", "www_traffic", "sim_phy",
    "l1", "smb_address", "ping_address", "iperf_server",
    "dns_latency_1", "dns_latency_2", "dns_latency_3",
    "dns_bad_ip_1", "dns_bad_ip_2", "dns_bad_ip_3",
    "dns_bad_record_1", "dns_bad_record_2", "dns_bad_record_3",
    "central_check",
)

_SLOT_RE = re.compile(r"^s[0-9]$")


def _new_parser() -> configparser.ConfigParser:
    """A case-preserving ConfigParser (keys stay as-written)."""
    p = configparser.ConfigParser()
    p.optionxform = str  # preserve key case (sections are already case-sensitive)
    return p


def load_ini(path: os.PathLike | str) -> configparser.ConfigParser:
    """Parse an INI file, preserving key case. Missing file → empty parser."""
    p = _new_parser()
    p.read(str(path), encoding="utf-8")
    return p


def load_configs(config_dir: os.PathLike | str) -> Tuple[configparser.ConfigParser, configparser.ConfigParser]:
    """Load ``(simulation.conf, user-overrides.conf)`` from *config_dir*.

    ``user-overrides.conf`` is optional — an empty parser is returned if absent.
    """
    d = Path(config_dir)
    sim_conf = load_ini(d / "simulation.conf")
    user_conf_path = d / "user-overrides.conf"
    user_conf = load_ini(user_conf_path) if user_conf_path.exists() else _new_parser()
    return sim_conf, user_conf


def bucket_for(hostname: str) -> str:
    """Deterministic bucket ``s0``–``s9`` via ``zlib.crc32(hostname) % 10``."""
    return f"s{zlib.crc32(hostname.encode()) % 10}"


def username_for(hostname: str) -> str:
    """``hostname`` with the first ``-`` segment stripped (``jsmith-1`` → ``jsmith``).

    Falls back to the whole hostname when there is no ``-``.
    """
    return hostname.split("-", 1)[0] if "-" in hostname else hostname


def _section_keys(parser: configparser.ConfigParser, section: str) -> Dict[str, str]:
    if not parser.has_section(section):
        return {}
    return dict(parser.items(section))


def resolve_profile(
    hostname: str,
    sim_conf: configparser.ConfigParser,
    user_conf: Optional[configparser.ConfigParser] = None,
) -> Dict[str, object]:
    """Resolve the effective profile for *hostname*.

    Returns ``{"hostname", "username", "simulation_id", "profile": {...}}``.
    """
    user_conf = user_conf if user_conf is not None else _new_parser()
    username = username_for(hostname)
    simulation_id = bucket_for(hostname)

    # [username] may pin a specific bucket, overriding the hash.
    if user_conf.has_section(username):
        pinned = user_conf.get(username, "simulation_id", fallback="").strip()
        if _SLOT_RE.match(pinned):
            simulation_id = pinned

    # Overlay in precedence order: lowest → highest. Last write wins.
    profile: Dict[str, str] = {}
    profile.update(_section_keys(sim_conf, "simulation"))
    profile.update(_section_keys(sim_conf, "address"))
    profile.update(_section_keys(sim_conf, "server"))
    profile.update(_section_keys(sim_conf, simulation_id))  # bucket
    profile.update(_section_keys(user_conf, username))       # per-user override

    return {
        "hostname": hostname,
        "username": username,
        "simulation_id": simulation_id,
        "profile": profile,
    }


def flag_on(profile: Dict[str, str], key: str) -> bool:
    """Truthy helper: a flag is "on" iff its value lowercased == ``"on"``."""
    return str(profile.get(key, "")).strip().lower() == "on"


def render_ini_for_client(
    sim_conf: configparser.ConfigParser,
    hostname: str,
    overrides: Optional[Dict[str, str]] = None,
) -> str:
    """Render ``simulation.conf`` text for a client, applying in-spoke overrides.

    The client re-derives its own bucket from ``crc32(hostname) % 10``, so the
    overrides are baked into that ``[sX]`` section as ``key=value`` replacements
    (matching the ``CLIENT_API.md`` contract). Values not present in the section
    are added; existing keys are overwritten.
    """
    sim_id = bucket_for(hostname)
    if overrides:
        if not sim_conf.has_section(sim_id):
            sim_conf.add_section(sim_id)
        for k, v in overrides.items():
            sim_conf.set(sim_id, k, str(v))
    buf = io.StringIO()
    sim_conf.write(buf)
    return buf.getvalue()


def validate_ini_text(text: str) -> configparser.ConfigParser:
    """Parse *text* as INI, raising ``ValueError`` on a malformed file.

    Used by ``CS_UPDATE_CONFIG`` / ``PUT /api/config/...`` so a bad edit is
    rejected before it overwrites the canon.
    """
    p = _new_parser()
    try:
        p.read_string(text)
    except configparser.Error as exc:
        raise ValueError(f"Invalid INI: {exc}") from exc
    return p