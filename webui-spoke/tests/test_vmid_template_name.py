"""Template clone-source field accepts EITHER a vmid (numeric) OR a template
NAME (text). The legacy webui-spoke's spec helpers must pass a name through
(mirroring the pxmx agent's ``_resolve_template_vmid`` name path) while
preserving the comma/range spec feature.

Pure unit tests on ``services/vmid.py`` — no FastAPI app, no subprocess.
"""
import os
import sys
from pathlib import Path

import pytest

# Make ``services`` importable when run from the webui-spoke root.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from fastapi import HTTPException  # noqa: E402

from services import vmid  # noqa: E402


# ── _normalize_vmid_spec: names accepted, specs unchanged ────────────────────

def test_normalize_accepts_bare_name():
    assert vmid._normalize_vmid_spec("debian-12-template") == "debian-12-template"
    # case preserved (VM names are case-sensitive)
    assert vmid._normalize_vmid_spec("Win11-Gold") == "Win11-Gold"


def test_normalize_preserves_digit_and_range():
    assert vmid._normalize_vmid_spec("100") == "100"
    assert vmid._normalize_vmid_spec("100-200") == "100-200"
    assert vmid._normalize_vmid_spec("100, 200-201, 300") == "100, 200-201, 300"


def test_normalize_range_validation_unchanged():
    with pytest.raises(ValueError):
        vmid._normalize_vmid_spec("200-100")
    with pytest.raises(ValueError):
        vmid._normalize_vmid_spec("100-5000")  # exceeds the 1000-span cap


# ── _parse_vmid_spec: names skipped, ints preserved ──────────────────────────

def test_parse_skips_name_tokens():
    assert vmid._parse_vmid_spec("debian-12") == []
    assert vmid._parse_vmid_spec("100, debian-12, 200-201") == [100, 200, 201]


# ── _primary_template_id: name fallback when no int vmids ───────────────────

def test_primary_returns_first_vmid_when_present():
    assert vmid._primary_template_id("100, 200", "100") == "100"
    assert vmid._primary_template_id("200-201", "100") == "200"


def test_primary_returns_name_when_no_int_vmids():
    assert vmid._primary_template_id("debian-12", "100") == "debian-12"


def test_primary_fallback_when_empty():
    assert vmid._primary_template_id("", "100") == "100"
    assert vmid._primary_template_id("", "win-gold") == "win-gold"


# ── _legacy_template_id: name passthrough ────────────────────────────────────

def test_legacy_returns_name_passthrough():
    assert vmid._legacy_template_id({"vm_image_1_template_id": "win-gold"}, 1) == "win-gold"
    assert vmid._legacy_template_id({"vm_image_2_template_id": "deb-12"}, 2) == "deb-12"


def test_legacy_clamps_numeric_and_defaults():
    assert vmid._legacy_template_id({"vm_image_1_template_id": "100"}, 1) == "100"
    assert vmid._legacy_template_id({}, 1) == "100"
    assert vmid._legacy_template_id({}, 2) == "200"


# ── _validate_template_specs: names don't false-overlap ──────────────────────

def test_validate_overlap_numeric_still_422():
    with pytest.raises(HTTPException) as exc:
        vmid._validate_template_specs("100-200", "200-300")
    assert exc.value.status_code == 422


def test_validate_name_vs_numeric_no_overlap():
    # A name can't overlap an int vmid — must NOT raise.
    vmid._validate_template_specs("debian-12", "100")
    vmid._validate_template_specs("debian-12", "win11-gold")