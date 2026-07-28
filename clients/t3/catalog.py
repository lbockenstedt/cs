#!/usr/bin/env python3
"""IoT device catalog loader / validator / emitter for the T3 + cs fleet.

``iot_catalog.json`` is the single source of truth for the per-device DHCP
fingerprint (option 60 vendor-class-id + option 55 parameter-request-list),
the vendor OUI MAC prefix, the simulated hostname, and the per-vendor traffic
endpoints. Two existing T3 files are DERIVED from it:

  * ``mac_config.json``        — the OUI pool consumed by ``gen_macs.sh``
    (``--emit-mac-config``).
  * the fingerprint+traffic    — the per-device block in ``wireless.sh``
    table (``--emit-fingerprints``).

It is also the device MENU for the sim-quota engine's new ``device`` quota kind
("N devices of profile X at site Y"). ``--sim-quota-catalog`` emits the list
the hub/UI renders as the Device dropdown, including each device's **surface**
— whether the engine realizes it on a T3 virtual WLAN interface
(``t3-vwlan``, wireless IoT) or on a cs VM client (``vm-client``, wired/edge).
The engine routes a device quota by that surface (see ``device_surface``).

Surface resolution (first wins):
  1. an explicit ``surface`` field on the catalog entry (operator override);
  2. ``DEVICE_SURFACE_OVERRIDE`` below (known exceptions to the category rule);
  3. ``CATEGORY_SURFACE[category]`` (default by device category);
  4. ``t3-vwlan`` (the wireless-IoT default — most catalog entries).

Run standalone:
  python3 catalog.py --validate
  python3 catalog.py --emit-mac-config > mac_config.json
  python3 catalog.py --emit-fingerprints
  python3 catalog.py --sim-quota-catalog
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

THIS_DIR = Path(__file__).resolve().parent
CATALOG_PATH = THIS_DIR / "iot_catalog.json"

SURFACE_T3 = "t3-vwlan"
SURFACE_VM = "vm-client"
SURFACES = (SURFACE_T3, SURFACE_VM)

# Default surface by device category. Wired/enterprise-edge categories default
# to a cs VM client; everything else (wireless IoT) defaults to a T3 vwlan.
CATEGORY_SURFACE: Dict[str, str] = {
    "printer": SURFACE_VM,
    "ip-phone": SURFACE_VM,
    "ip-camera": SURFACE_VM,
    "serial-server": SURFACE_VM,
    "network-infra": SURFACE_VM,
    "iot-gateway": SURFACE_VM,
    "mobile": SURFACE_VM,
}

# Known exceptions: devices whose category would default to vm-client but are
# realized on T3 vwlan (they appear in wireless.sh as wireless interfaces), or
# vice-versa. An explicit ``surface`` field on the entry always wins over this.
DEVICE_SURFACE_OVERRIDE: Dict[str, str] = {
    # in T3 wireless.sh as vwlan (wireless), though their category is wired-ish
    "hp-jetdirect": SURFACE_T3,
    "zebra-zt230": SURFACE_T3,
    "polycom-ip-phone": SURFACE_T3,
    "axis-p3265": SURFACE_T3,
    "moxa-device-server": SURFACE_T3,
    "cisco-ap": SURFACE_T3,
    # mobile devices are wireless clients
    "samsung-mobile-a": SURFACE_T3,
    "samsung-mobile-b": SURFACE_T3,
    "apple-mobile": SURFACE_T3,
    # wireless cameras / doorbells
    "wyze-cam": SURFACE_T3,
    # wired bridges/hubs whose category defaults to t3-vwlan but are ethernet
    "hue-bridge": SURFACE_VM,
}

_OUI_RE = re.compile(r"^[0-9a-f]{2}:[0-9a-f]{2}:[0-9a-f]{2}$")


def load_catalog(path: Path = CATALOG_PATH) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def devices(catalog: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [d for d in catalog.get("devices", []) if isinstance(d, dict)
            and d.get("id")]


def device_surface(dev: Dict[str, Any]) -> str:
    """Where the engine realizes this profile: a T3 vwlan interface or a cs
    VM client. Explicit entry ``surface`` > override map > category default >
    ``t3-vwlan``."""
    s = str(dev.get("surface") or "").strip().lower()
    if s in SURFACES:
        return s
    s = DEVICE_SURFACE_OVERRIDE.get(str(dev.get("id", "")).strip().lower())
    if s:
        return s
    return CATEGORY_SURFACE.get(str(dev.get("category", "")).strip().lower(), SURFACE_T3)


def validate(catalog: Dict[str, Any]) -> List[str]:
    """Return a list of validation errors (empty == valid)."""
    errs: List[str] = []
    seen_ids: set[str] = set()
    mac_total = 0
    for i, d in enumerate(catalog.get("devices", [])):
        if not isinstance(d, dict):
            errs.append(f"device #{i}: not an object")
            continue
        if d.get("_comment"):
            continue
        did = str(d.get("id") or "").strip()
        if not did:
            errs.append(f"device #{i}: missing 'id'")
            continue
        if did in seen_ids:
            errs.append(f"device {did!r}: duplicate id")
        seen_ids.add(did)
        for req in ("vendor", "category", "hostname"):
            if not str(d.get(req) or "").strip():
                errs.append(f"device {did!r}: missing '{req}'")
        oui = d.get("oui")
        if oui is not None and not _OUI_RE.match(str(oui).lower()):
            errs.append(f"device {did!r}: oui {oui!r} not aa:bb:cc")
        cnt = d.get("count", 1 if oui else 0)
        try:
            cnt_i = int(cnt)
            if cnt_i < 0:
                errs.append(f"device {did!r}: count {cnt_i} < 0")
            if oui and cnt_i > 0:
                mac_total += cnt_i
        except (TypeError, ValueError):
            errs.append(f"device {did!r}: count {cnt!r} not an int")
        dhcp = d.get("dhcp") or {}
        vci = str(dhcp.get("vendor_class_id") or "").strip()
        prl = dhcp.get("param_request_list")
        if vci and not isinstance(vci, str):
            errs.append(f"device {did!r}: vendor_class_id not a string")
        if prl is not None:
            if not isinstance(prl, list) or not all(isinstance(x, int) for x in prl):
                errs.append(f"device {did!r}: param_request_list not an int list")
        surf = str(d.get("surface") or "").strip().lower()
        if surf and surf not in SURFACES:
            errs.append(f"device {did!r}: surface {surf!r} not in {SURFACES}")
        tr = d.get("traffic") or {}
        for k in ("dns", "curl", "http", "wget"):
            v = tr.get(k)
            if v is not None and not isinstance(v, list):
                errs.append(f"device {did!r}: traffic.{k} not a list")
    if mac_total > 25:
        errs.append(f"MAC pool total count {mac_total} exceeds the gen_macs.sh "
                    f"limit of 25 — reduce counts or null out OUIs")
    return errs


def emit_mac_config(catalog: Dict[str, Any]) -> str:
    """The mac_config.json consumed by gen_macs.sh: [{vendor, oui, count}] for
    every device with a non-null OUI and count > 0, in catalog order. Errors
    out (exit 1) if the total count exceeds 25."""
    out: List[Dict[str, Any]] = []
    total = 0
    for d in devices(catalog):
        oui = d.get("oui")
        if not oui:
            continue
        cnt = int(d.get("count", 1) or 0)
        if cnt <= 0:
            continue
        total += cnt
        out.append({"vendor": d["vendor"], "oui": str(oui).lower(), "count": cnt})
    if total > 25:
        print(f"ERROR: emitted MAC pool total {total} > 25 (gen_macs.sh limit)",
              file=sys.stderr)
        sys.exit(1)
    return json.dumps(out, indent=2) + "\n"


def emit_fingerprints(catalog: Dict[str, Any]) -> str:
    """A TSV fingerprint table: id\\thostname\\tvendor_class_id (opt60)\\t
    param_request_list (opt55, comma-joined)\\tsurface. For wiring wireless.sh
    / a VM client's dhclient/dhcpcd invocation from the catalog."""
    lines = ["id\thostname\tvendor_class_id(opt60)\tparam_request_list(opt55)\tsurface"]
    for d in devices(catalog):
        dhcp = d.get("dhcp") or {}
        prl = dhcp.get("param_request_list") or []
        lines.append("\t".join([
            d["id"],
            d["hostname"],
            str(dhcp.get("vendor_class_id") or ""),
            ",".join(str(x) for x in prl),
            device_surface(d),
        ]))
    return "\n".join(lines) + "\n"


def parse_compose(spec: Any) -> Dict[str, int]:
    """Parse an engine-delivered device composition ``"id:count,id:count"`` (the
    sim-quota ``iot_devices`` override) into ``{device_id: count}``. Tolerant:
    skips malformed tokens, sums duplicates, ignores non-positive counts."""
    out: Dict[str, int] = {}
    for tok in re.split(r"[,\s]+", str(spec or "").strip()):
        if not tok or ":" not in tok:
            continue
        did, _, cnt = tok.partition(":")
        did = did.strip()
        try:
            n = int(cnt.strip())
        except (TypeError, ValueError):
            continue
        if did and n > 0:
            out[did] = out.get(did, 0) + n
    return out


def run_plan(catalog: Dict[str, Any], max_ifaces: int = 25,
             compose: Any = None) -> List[Dict[str, Any]]:
    """The per-virtual-interface run plan the linux client's ``iot`` mode drives.

    Default (``compose`` is None): expand each ``t3-vwlan`` device (surface ==
    wireless IoT) with a non-null OUI and ``count`` > 0 into that many interface
    slots, in catalog order — the whole fleet.

    Engine-driven (``compose`` = ``{device_id: count}`` or an ``"id:count,..."``
    string): build the fleet from EXACTLY that composition (each device repeated
    its count), so a device quota ("10 Teslas + 4 BarcoShare in MIA") stages just
    those. Unknown ids, non-``t3-vwlan`` surfaces, and OUI-less devices are
    skipped.

    Either way each slot carries what the bash engine needs — OUI (for the
    deterministic vendor MAC), hostname, and DHCP fingerprint (opt60 + opt55) —
    numbered sequentially from vwlan1 and capped at ``max_ifaces`` (the
    mac80211_hwsim / gen_macs 25-interface limit)."""
    def _slot(iface: int, d: Dict[str, Any]) -> Dict[str, Any]:
        dhcp = d.get("dhcp") or {}
        prl = dhcp.get("param_request_list") or []
        return {"iface": iface, "id": d["id"], "oui": str(d.get("oui")).lower(),
                "hostname": d["hostname"],
                "opt60": str(dhcp.get("vendor_class_id") or ""),
                "opt55": ",".join(str(x) for x in prl)}

    def _eligible(d: Dict[str, Any]) -> bool:
        return device_surface(d) == SURFACE_T3 and bool(d.get("oui"))

    # Build the ordered list of device instances to stage.
    instances: List[Dict[str, Any]] = []
    if compose is not None:
        comp = compose if isinstance(compose, dict) else parse_compose(compose)
        by_id = {str(d.get("id")): d for d in devices(catalog)}
        for did, cnt in comp.items():
            d = by_id.get(str(did))
            if d and _eligible(d):
                instances.extend([d] * max(0, int(cnt)))
    else:
        for d in devices(catalog):
            if _eligible(d):
                instances.extend([d] * max(0, int(d.get("count", 1) or 0)))

    plan: List[Dict[str, Any]] = []
    for iface, d in enumerate(instances, start=1):
        if iface > max_ifaces:
            break
        plan.append(_slot(iface, d))
    return plan


def emit_run_plan(catalog: Dict[str, Any], max_ifaces: int = 25,
                  compose: Any = None) -> str:
    """TSV run plan (one row per vwlan interface): iface, id, oui, hostname,
    opt60, opt55. Consumed by the linux client's iot_sim.sh — the single
    data-driven source for interface setup + the DHCP fingerprint. With
    ``compose`` set it emits the engine-delivered device composition instead of
    the whole catalog."""
    lines = ["iface\tid\toui\thostname\topt60\topt55"]
    for r in run_plan(catalog, max_ifaces, compose):
        lines.append("\t".join([
            str(r["iface"]), r["id"], r["oui"], r["hostname"], r["opt60"], r["opt55"],
        ]))
    return "\n".join(lines) + "\n"


def emit_traffic(catalog: Dict[str, Any], device_id: str) -> str:
    """The traffic targets for one device, one ``kind<TAB>target`` line per entry
    (kind ∈ dns/http/curl/wget), in catalog order. The iot engine calls this for
    the device it is driving this cycle and dispatches each line through the
    linux client's existing traffic helpers. Empty output for an unknown id or a
    device with no traffic."""
    did = str(device_id or "").strip().lower()
    lines: List[str] = []
    for d in devices(catalog):
        if str(d.get("id", "")).strip().lower() != did:
            continue
        tr = d.get("traffic") or {}
        for kind in ("dns", "http", "curl", "wget"):
            for target in (tr.get(kind) or []):
                if str(target).strip():
                    lines.append(f"{kind}\t{target}")
        break
    return "\n".join(lines) + ("\n" if lines else "")


def sim_quota_catalog(catalog: Dict[str, Any]) -> Dict[str, Any]:
    """The device menu the hub/UI renders for the ``device`` sim-quota kind.
    Each entry: id, vendor, model, category, surface, oui (or null), verified.
    The engine routes a device quota by ``surface``."""
    return {
        "devices": [
            {
                "id": d["id"],
                "vendor": d["vendor"],
                "model": d.get("model", ""),
                "category": d.get("category", ""),
                "surface": device_surface(d),
                "oui": d.get("oui"),
                "verified": bool(d.get("verified", False)),
            }
            for d in devices(catalog)
        ],
        "categories": dict(catalog.get("categories", {})),
        "surfaces": list(SURFACES),
    }


def _cmd_validate(args) -> int:
    cat = load_catalog()
    errs = validate(cat)
    if errs:
        for e in errs:
            print(f"ERROR: {e}", file=sys.stderr)
        return 1
    print(f"OK — {len(devices(cat))} devices, "
          f"{sum(1 for d in devices(cat) if d.get('oui'))} with OUI")
    return 0


def _cmd_emit_mac_config(args) -> int:
    sys.stdout.write(emit_mac_config(load_catalog()))
    return 0


def _cmd_emit_fingerprints(args) -> int:
    sys.stdout.write(emit_fingerprints(load_catalog()))
    return 0


def _cmd_sim_quota_catalog(args) -> int:
    print(json.dumps(sim_quota_catalog(load_catalog()), indent=2))
    return 0


def _cmd_emit_run_plan(args) -> int:
    sys.stdout.write(emit_run_plan(load_catalog(), int(getattr(args, "max", 25) or 25),
                                   compose=getattr(args, "compose", None) or None))
    return 0


def _cmd_emit_traffic(args) -> int:
    sys.stdout.write(emit_traffic(load_catalog(), args.emit_traffic))
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="IoT device catalog loader/emitter")
    grp = p.add_mutually_exclusive_group(required=True)
    grp.add_argument("--validate", action="store_true", help="validate the catalog")
    grp.add_argument("--emit-mac-config", action="store_true",
                     help="emit mac_config.json (the gen_macs.sh OUI pool)")
    grp.add_argument("--emit-fingerprints", action="store_true",
                     help="emit the opt60/opt55/hostname fingerprint table")
    grp.add_argument("--sim-quota-catalog", action="store_true",
                     help="emit the device menu for the sim-quota 'device' kind")
    grp.add_argument("--emit-run-plan", action="store_true",
                     help="emit the per-vwlan-interface run plan (TSV) for the iot mode")
    grp.add_argument("--emit-traffic", metavar="DEVICE_ID",
                     help="emit the kind<TAB>target traffic lines for one device id")
    p.add_argument("--max", type=int, default=25,
                   help="max vwlan interfaces for --emit-run-plan (default 25)")
    p.add_argument("--compose", default=None,
                   help="device composition 'id:count,id:count' for --emit-run-plan "
                        "(the engine-delivered iot_devices quota); default = whole catalog")
    args = p.parse_args(argv)
    if args.validate:
        return _cmd_validate(args)
    if args.emit_mac_config:
        return _cmd_emit_mac_config(args)
    if args.emit_fingerprints:
        return _cmd_emit_fingerprints(args)
    if args.sim_quota_catalog:
        return _cmd_sim_quota_catalog(args)
    if args.emit_run_plan:
        return _cmd_emit_run_plan(args)
    if args.emit_traffic:
        return _cmd_emit_traffic(args)
    return 2


if __name__ == "__main__":
    sys.exit(main())