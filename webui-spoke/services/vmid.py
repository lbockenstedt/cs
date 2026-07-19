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
    """Normalize a template VMID spec to a canonical ``", "``-joined string.

    Each comma-separated token is either a range (``100-200``), a bare VMID
    (``100``), or — for the clone-source field which accepts EITHER a vmid OR a
    template NAME — a bare non-numeric token (``debian-12-template``). A NAME
    token is accepted as-is (VM names are case-sensitive, so NOT lowercased);
    the pxmx agent's ``_resolve_template_vmid`` resolves it to a vmid via
    ``qm list``. Range/digit validation is unchanged; only the previous
    ``raise ValueError`` for a non-numeric token is lifted (names are valid)."""
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
        # Non-numeric token — a template NAME; accept verbatim (agent resolves).
        parts.append(token)
    return ", ".join(parts)


def _parse_vmid_spec(raw: Any, *, field_name: str = "template VMID spec") -> list[int]:
    """Int VMIDs in a spec (ranges expanded). NAME tokens are SKIPPED — a name
    isn't an int vmid; it flows through ``_primary_template_id``'s name fallback
    so the agent resolves it. Used by ``_primary_template_id`` (pick first vmid)
    and ``_validate_template_specs`` (overlap check — names can't overlap ints)."""
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
        elif re.fullmatch(r"\d+", token):
            vmids.add(int(token))
        # else: a NAME token — skip (not an int vmid).
    return sorted(vmids)


def _template_spec_key(slot: int) -> str:
    return f"vm_image_{slot}_template_spec"


def _template_id_key(slot: int) -> str:
    return f"vm_image_{slot}_template_id"


def _legacy_template_id(source: Mapping[str, Any], slot: int) -> str:
    """The simple ``vm_image_{slot}_template_id`` value (or its legacy aliases),
    as a string. Accepts EITHER a vmid (numeric → clamped >= 1) OR a template
    NAME (non-numeric → returned verbatim so the pxmx agent resolves it via
    ``qm list``). Falls back to the slot default only when no key is set."""
    if slot == 1:
        keys = ("vm_image_1_template_id", "usb_linux_template_id", "usb_template_id")
        default = "100"
    else:
        keys = ("vm_image_2_template_id", "usb_windows_template_id")
        default = "200"
    for key in keys:
        raw = str(source.get(key, "") or "").strip()
        if not raw:
            continue
        if re.fullmatch(r"\d+", raw):
            return str(max(1, int(raw)))
        # Non-numeric → a template NAME; pass through unchanged.
        return raw
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
    """Pick the single clone-source id to emit to the agent.

    A spec may carry int vmids (comma/range) OR a template NAME token. Returns
    the first int vmid if any; else the first NAME token (the agent resolves it
    via ``qm list``); else ``fallback`` (the legacy id, which itself may be a
    name). Always non-empty when ``fallback`` is the slot default."""
    spec_s = str(spec or "").strip()
    if spec_s:
        vmids = _parse_vmid_spec(spec_s)
        if vmids:
            return str(vmids[0])
        # No int vmids — look for a NAME token.
        for token in spec_s.split(","):
            token = token.strip()
            if token and not re.fullmatch(r"\d+-\d+|\d+", token):
                return token
    return fallback


def _validate_template_specs(spec1: str, spec2: str) -> None:
    overlap = sorted(set(_parse_vmid_spec(spec1, field_name="vm_image_1_template_spec")) & set(_parse_vmid_spec(spec2, field_name="vm_image_2_template_spec")))
    if overlap:
        preview = ", ".join(str(vmid) for vmid in overlap[:5])
        suffix = "…" if len(overlap) > 5 else ""
        raise HTTPException(status_code=422, detail=f"VM Image 1 and VM Image 2 template VMID specs overlap: {preview}{suffix}")
