"""The Windows wifi profile builders XML-escape SSIDs and credentials.

A config value with & < > ' or " used to be pasted raw into the WLAN profile
XML: netsh rejected it, or a crafted value could add its own XML elements.
"""
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

WINDOWS = Path(__file__).resolve().parents[2] / "clients/windows"
HOSTILE = "a&b<c>d'e\"f</name><x>"
BUILDERS = {
    "network_common.ps1": ["ConvertTo-XmlText"],
    "connect_psk.ps1": ["New-WpaPskProfileXml"],
    "connect_open.ps1": ["New-OpenProfileXml"],
    "connect_1x.ps1": ["New-Peap1xProfileXml", "New-Peap1xUserDataXml"],
}


def _function(source: str, name: str) -> str:
    """Return one top-level PowerShell function (from its line to the closing ``}``)."""
    match = re.search(rf"^function {re.escape(name)} \{{\n.*?^\}}\n", source, re.S | re.M)
    assert match, name
    return match.group(0)


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    """Run each builder with hostile values and return the parsed XML roots."""
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if not powershell:
        pytest.skip("PowerShell is not installed; run this on Windows or Linux CI")
    funcs = "".join(_function((WINDOWS / f).read_text(), n) for f, names in BUILDERS.items() for n in names)
    calls = {
        "psk": "New-WpaPskProfileXml -Ssid $h -Password $h",
        "open": "New-OpenProfileXml -Ssid $h",
        "peap": "New-Peap1xProfileXml -Ssid $h -Identity $h",
        "user": "New-Peap1xUserDataXml -Username $h -Password $h",
    }
    script = funcs + "$h = '" + HOSTILE.replace("'", "''") + "'\n" + "".join(
        f"({call}) + \"`n=====`n\"\n" for call in calls.values())
    path = tmp_path_factory.mktemp("wifi") / "build.ps1"
    path.write_text(script)
    result = subprocess.run([powershell, "-NoProfile", "-NonInteractive", "-File", str(path)],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    parts = [p.strip() for p in result.stdout.split("=====") if p.strip()]
    return {key: ET.fromstring(part.encode()) for key, part in zip(calls, parts)}


@pytest.mark.parametrize("kind,count", [("psk", 3), ("open", 2), ("peap", 2), ("user", 3)])
def test_hostile_values_round_trip_as_text(built, kind, count):
    """Every SSID/secret element parses back to the exact value, and nothing
    the value tried to inject became an element."""
    root = built[kind]
    texts = [el.text for el in root.iter()]
    assert texts.count(HOSTILE) == count
    assert not any(el.tag.endswith("}x") or el.tag == "x" for el in root.iter())


def test_add_profile_writes_utf8():
    """ASCII turned a non-ASCII SSID or passphrase into '?'."""
    body = _function((WINDOWS / "network_common.ps1").read_text(), "Add-WifiProfileXml")
    assert "-Encoding UTF8" in body and "-Encoding ASCII" not in body
