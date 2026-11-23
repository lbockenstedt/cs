"""Client update scripts must not run or write what the spoke serves as names.

``check_api_up`` built ``bash -c "... /dev/tcp/$host/$port"`` from the served
``server_url``, so a crafted host ran as shell. The Linux and Windows updaters
used each name from the served script list or manifest as a local path, so a
name with ``/``, ``\\`` or ``..`` wrote or hashed outside the scripts folder.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
UPDATE_SH = (REPO / "clients" / "linux" / "update.sh").read_text()
UPDATE_PS1 = (REPO / "clients" / "windows" / "update.ps1").read_text()


def _fn(name: str) -> str:
    """Return the source of shell function ``name`` from update.sh."""
    m = re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", UPDATE_SH, re.M | re.S)
    assert m, name
    return m.group(0)


def _run(script: str, cwd: Path) -> subprocess.CompletedProcess:
    """Run ``script`` in bash with the update.sh log variables set."""
    prelude = 'debug=/dev/null; log=/dev/null\ntimeout() { shift; "$@"; }\n'
    return subprocess.run(["bash", "-c", prelude + script], cwd=cwd,
                          capture_output=True, text=True, timeout=30)


@pytest.mark.parametrize("url", [
    "http://x$(touch pwned)y:1/",
    "http://x`touch pwned`y:1/",
    "http://x;touch pwned;y:1/",
])
def test_check_api_up_never_runs_the_host(tmp_path, url):
    """A crafted server_url host is refused and runs nothing."""
    res = _run(_fn("check_api_up") + f"check_api_up '{url}'", tmp_path)
    assert res.returncode != 0
    assert not (tmp_path / "pwned").exists()


def test_check_api_up_still_probes_a_plain_host(tmp_path):
    """A normal host and port still reach the TCP probe."""
    res = _run(_fn("check_api_up") + "check_api_up 'http://127.0.0.1:1/'", tmp_path)
    assert "Checking TCP 127.0.0.1:1" in res.stdout


@pytest.mark.parametrize("name,ok", [
    ("update.sh", True), ("VERSION", True), ("iot_catalog.json", True),
    ("../x.sh", False), ("a/b.sh", False), (".bashrc", False), ("..", False),
    ("a b.sh", False), ("", False), ("x$(id).sh", False),
])
def test_safe_script_name(tmp_path, name, ok):
    """Only bare file names pass the allowlist."""
    res = _run(_fn("_safe_script_name") + f"_safe_script_name '{name}'", tmp_path)
    assert (res.returncode == 0) is ok


@pytest.mark.skipif(not shutil.which("sha256sum"), reason="needs sha256sum")
def test_manifest_ignores_unsafe_names(tmp_path):
    """A manifest entry naming a path outside the scripts folder is ignored,
    so it can't force a sync or probe another file."""
    manifest = '{"../../../etc/hostname":"00"}'
    script = (f"curl() {{ printf '%s' '{manifest}'; }}\n"
              + _fn("_safe_script_name") + _fn("_scripts_sync_decision")
              + "_scripts_sync_decision http://spoke")
    assert _run(script, tmp_path).stdout.strip() == "same"


def test_linux_download_loop_checks_each_name():
    """The script-list download loop checks a name before using it as a path."""
    loop = UPDATE_SH[UPDATE_SH.index("for fname in $(echo \"$script_list\""):]
    assert loop.index('_safe_script_name "$fname"') < loop.index('-o "$tmp_web/$fname"')


def test_windows_checks_each_name_before_join_path():
    """update.ps1 checks served names in the manifest and before download."""
    assert re.search(r"Test-SafeScriptName \$fname\)\) \{ continue \}\s*\n\s*\$want", UPDATE_PS1)
    loop = UPDATE_PS1[UPDATE_PS1.index("foreach ($filename in $fileList)"):]
    assert loop.index("Test-SafeScriptName") < loop.index("Join-Path $scriptRoot $filename")
    assert "-cmatch '^[A-Za-z0-9][A-Za-z0-9._-]*$'" in UPDATE_PS1
