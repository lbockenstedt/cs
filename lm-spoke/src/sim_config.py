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
import copy
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


def merge_override(parser: configparser.ConfigParser, path: os.PathLike | str) -> None:
    """Merge a hub-managed override ``.conf`` on top of an already-parsed parser.

    Ports the legacy webui-spoke ``_merge_ini_override`` (server.py:675).
    Every key/section in the override file wins over the base file. Missing
    file → no-op. Parse failures log a warning and skip (never raise) so a
    malformed hub override can't take down the engine's per-iteration reload.

    The override files (``hub-sim-overrides.conf`` / ``hub-user-overrides.conf``)
    are written by the spoke's ``CS_CONFIG_UPDATE`` handler from the
    ``sim_conf_override`` / ``user_conf_override`` INI text the LM hub pushes.
    Without this merge they were dead files — only ``[simulation] sim_phy``
    was ever read (command_queue.usb_config_payload). Merging here makes the
    hub-managed override path effective in the engine resolver, the
    ``/api/config`` client route, and ``CS_GET_CONFIG`` readback.
    """
    p = Path(path)
    if not p.exists():
        return
    try:
        ov = _new_parser()
        ov.read_string(p.read_text(encoding="utf-8"))
        for section in ov.sections():
            if not parser.has_section(section):
                parser.add_section(section)
            for key, value in ov.items(section):
                parser.set(section, key, value)
    except Exception as exc:  # noqa: BLE001 — best-effort; never break reload
        logger.warning("Could not apply hub override %s: %s", p, exc)


def serialize_ini(parser: configparser.ConfigParser) -> str:
    """Render a parser back to INI text (round-trips ``load_ini`` output)."""
    buf = io.StringIO()
    parser.write(buf)
    return buf.getvalue()


def sections_dict(parser: configparser.ConfigParser) -> Dict[str, Dict[str, str]]:
    """``{section: {key: value}}`` view of a parser. Shared by every consumer
    that needs simulation.conf as nested JSON rather than a ConfigParser
    object — client_api.py's ``/api/config/parsed`` and local_ui_routes.py's
    ``/config/simulation-conf-parsed`` both call this instead of each
    re-implementing the same three-line conversion."""
    return {s: dict(parser.items(s)) for s in parser.sections()}


# mtime-keyed cache for load_configs. Keyed by (str(config_dir), mtime-tuple of
# the 4 input files). The cached pair holds the CANONICAL merged parsers; callers
# that mutate the returned object (/api/config merges user_conf into sim_conf
# then render_ini_for_client bakes per-client [sX] overrides) get a deepcopy so
# the cache can't be corrupted across requests. Bounded: only the latest mtime
# tuple per config_dir is retained.
_LOAD_CACHE: Dict[Tuple[str, Tuple[int, int, int, int]],
                  Tuple[configparser.ConfigParser, configparser.ConfigParser]] = {}


def _mtime_ns(path: Path) -> int:
    """Best-effort mtime in ns; 0 if the path is absent/unreadable."""
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return 0


def load_configs(config_dir: os.PathLike | str) -> Tuple[configparser.ConfigParser, configparser.ConfigParser]:
    """Load ``(simulation.conf, user-overrides.conf)`` from *config_dir*.

    ``user-overrides.conf`` is optional — an empty parser is returned if absent.

    Hub-managed overrides (``hub-sim-overrides.conf`` / ``hub-user-overrides.conf``,
    written by ``CS_CONFIG_UPDATE``) are merged on top so the hub-pushed config
    takes effect everywhere this loader is used: the engine's per-iteration
    ``resolve_profile``, the ``/api/config`` client route, and ``CS_GET_CONFIG``.
    Mirrors the legacy webui-spoke, which merged the override before serving.

    The 4-file read+parse is mtime-cached. ``load_configs`` runs on the cs spoke's
    SINGLE shared event loop (the engine calls it every iteration ~5 s, and
    ``/api/config`` calls it per client fetch); under disk contention the 6+
    synchronous ``stat``/``open``/``read``/``close`` syscalls per call stalled
    the loop long enough to trip the hub's "Request Timeout after 5.0 s". The
    cache collapses a repeated load to one ``stat`` per file (inode-cached) and a
    deepcopy (pure CPU, no I/O). Callers mutate the returned parsers, so hits
    return deep copies of the canonical merged pair; a miss returns the fresh
    objects and caches independent copies.
    """
    d = Path(config_dir)
    sim_path = d / "simulation.conf"
    user_path = d / "user-overrides.conf"
    hsim_path = d / "hub-sim-overrides.conf"
    huser_path = d / "hub-user-overrides.conf"
    key = (str(d), (_mtime_ns(sim_path), _mtime_ns(user_path),
                    _mtime_ns(hsim_path), _mtime_ns(huser_path)))
    cached = _LOAD_CACHE.get(key)
    if cached is not None:
        return copy.deepcopy(cached[0]), copy.deepcopy(cached[1])

    sim_conf = load_ini(sim_path)
    user_conf = load_ini(user_path) if user_path.exists() else _new_parser()
    merge_override(sim_conf, hsim_path)
    merge_override(user_conf, huser_path)
    # Cache independent copies so a caller mutating the returned objects can't
    # corrupt the canonical pair held here.
    _LOAD_CACHE[key] = (copy.deepcopy(sim_conf), copy.deepcopy(user_conf))
    # Bounded: drop stale keys for this dir (old mtime tuple after a write).
    for k in list(_LOAD_CACHE):
        if k[0] == key[0] and k != key:
            _LOAD_CACHE.pop(k, None)
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


def pure_bucket_profile(
    hostname: str,
    config_dir: os.PathLike | str,
) -> Dict[str, str]:
    """The ``simulation.conf`` bucket default for *hostname* WITHOUT the
    ``[username]`` overlay from ``user-overrides.conf``.

    Used by :meth:`ClientRegistry.set_overrides` to prune redundant per-client
    registry overrides: when an override's on/off value matches this pure
    bucket default, the override entry is DROPPED so the client falls back to
    the bucket (overrides stay a true diff over the bucket). The ``[username]``
    overlay is deliberately excluded — the WebUI mirrors each toggle into
    ``user-overrides.conf`` (best-effort sync for the Config/User-Overrides
    card), so including it would make the "bucket default" reflect the mirror's
    own prior writes and defeat the prune. Comparing against the pure
    ``simulation.conf`` bucket is the user's intent: "turn off → revert to the
    bucket default".
    """
    sim_conf, _ = load_configs(config_dir)
    return resolve_profile(hostname, sim_conf, _new_parser())["profile"]


def effective_client_fields(
    hostname: str,
    sim_conf: Optional[configparser.ConfigParser],
    user_conf: Optional[configparser.ConfigParser],
    reported_sim_id: str = "",
    reported_config: Optional[Dict[str, str]] = None,
) -> Tuple[str, Dict[str, str]]:
    """Server-resolved Sim-ID / Site / PHY for a client — the original hub's
    ``effective_config``.

    The bash client's status write omits ``wsite`` and ``sim_phy`` entirely and
    may report a stale ``simulation_id`` (old character-position hashing →
    letters like ``"sl"``). Resolve the authoritative bucket profile from the
    hostname so the Clients view always shows the correct ``s0``–``s9`` bucket +
    Site + PHY, regardless of what the (possibly un-updated) client reported.

    Returns ``(simulation_id, config)``. Falls back to the reported values on any
    resolve error or when ``sim_conf`` is None. Shared by both the hub-telemetry
    relay (``control_plane``) and the local dashboard (``local_ui_routes``) so
    the two Clients views never diverge."""
    sim_id = reported_sim_id or ""
    cfg = dict(reported_config or {})
    if sim_conf is None:
        return sim_id, cfg
    try:
        resolved = resolve_profile(hostname, sim_conf, user_conf)
        sim_id = resolved.get("simulation_id") or sim_id
        prof = resolved.get("profile") or {}
        if prof.get("wsite"):
            cfg["wsite"] = prof["wsite"]
        if prof.get("sim_phy"):
            cfg["sim_phy"] = prof["sim_phy"]
    except Exception:  # noqa: BLE001 — degrade to reported values
        pass
    return sim_id, cfg


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