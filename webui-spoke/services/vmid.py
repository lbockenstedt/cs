"""Pure VMID / template-spec parsing helpers, moved verbatim from server.py."""
from __future__ import annotations

import re
from typing import Any, Mapping

from fastapi import HTTPException


def _parse_protected_vmids(raw: str) -> list[int | tuple[int, int]]:
    """Parse a protected VMIDs string into a list of ints and (lo, hi) range tuples.

    Accepts comma-separated entries where each entry is either a single VMID
    (e.g. ``101``) or an inclusive range (e.g. ``100-90000``).
    """
    result: list[int | tuple[int, int]] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            # Could be a range like "100-90000"
            lo_s, _, hi_s = part.partition("-")
            try:
                lo, hi = int(lo_s.strip()), int(hi_s.strip())
                if lo <= hi:
                    result.append((lo, hi))
            except ValueError:
                pass
        else:
            try:
                result.append(int(part))
            except ValueError:
                pass
    return result


_TEMPLATE_VMID_RANGE_CAP = 1000


def _normalize_vmid_spec(raw: Any, *, field_name: str = "template VMID spec") -> str:
    parts: list[str] = []
    for part in str(raw or "").split(","):
        token = part.strip()
        if not token:
            continue
        m = re.fullmatch(r"(\d+)-(\d+)", token)
        if m:
            lo, hi = int(m.group(1)), int(m.group(2))
            if lo > hi:
                raise ValueError(f"{field_name}: range start must be <= end ({token})")
            if (hi - lo) > _TEMPLATE_VMID_RANGE_CAP:
                raise ValueError(f"{field_name}: range too large ({token}); max span is {_TEMPLATE_VMID_RANGE_CAP + 1} VMIDs")
            parts.append(f"{lo}-{hi}")
            continue
        if re.fullmatch(r"\d+", token):
            parts.append(str(int(token)))
            continue
        raise ValueError(f"{field_name}: invalid token '{token}'")
    return ", ".join(parts)


def _parse_vmid_spec(raw: Any, *, field_name: str = "template VMID spec") -> list[int]:
    normalized = _normalize_vmid_spec(raw, field_name=field_name)
    vmids: set[int] = set()
    for token in normalized.split(","):
        token = token.strip()
        if not token:
            continue
        m = re.fullmatch(r"(\d+)-(\d+)", token)
        if m:
            lo, hi = int(m.group(1)), int(m.group(2))
            vmids.update(range(lo, hi + 1))
        else:
            vmids.add(int(token))
    return sorted(vmids)


def _template_spec_key(slot: int) -> str:
    return f"vm_image_{slot}_template_spec"


def _template_id_key(slot: int) -> str:
    return f"vm_image_{slot}_template_id"


def _legacy_template_id(source: Mapping[str, Any], slot: int) -> str:
    if slot == 1:
        keys = ("vm_image_1_template_id", "usb_linux_template_id", "usb_template_id")
        default = "100"
    else:
        keys = ("vm_image_2_template_id", "usb_windows_template_id")
        default = "200"
    for key in keys:
        raw = str(source.get(key, "") or "").strip()
        if re.fullmatch(r"\d+", raw):
            return str(max(1, int(raw)))
    return default


def _resolved_template_spec(source: Mapping[str, Any], slot: int) -> str:
    spec_key = _template_spec_key(slot)
    if spec_key in source:
        raw = str(source.get(spec_key, "") or "").strip()
        if not raw:
            return ""
        try:
            return _normalize_vmid_spec(raw, field_name=spec_key)
        except ValueError:
            return _legacy_template_id(source, slot)
    return _legacy_template_id(source, slot)


def _primary_template_id(spec: str, fallback: str) -> str:
    vmids = _parse_vmid_spec(spec) if str(spec or "").strip() else []
    return str(vmids[0]) if vmids else fallback


def _validate_template_specs(spec1: str, spec2: str) -> None:
    overlap = sorted(set(_parse_vmid_spec(spec1, field_name="vm_image_1_template_spec")) & set(_parse_vmid_spec(spec2, field_name="vm_image_2_template_spec")))
    if overlap:
        preview = ", ".join(str(vmid) for vmid in overlap[:5])
        suffix = "…" if len(overlap) > 5 else ""
        raise HTTPException(status_code=422, detail=f"VM Image 1 and VM Image 2 template VMID specs overlap: {preview}{suffix}")
