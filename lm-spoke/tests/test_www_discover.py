"""WWW Discover contracts and isolated browser-launch tests (no network)."""
import configparser
import os
from pathlib import Path
import re
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]
URLS = [
    "https://worldaz.tr.teams.microsoft.com",
    "https://zoom.us",
    "https://www.okta.com",
    "https://www.google.com",
    "https://www.linkedin.com",
]


def test_defaults_and_list_copies():
    """The new list is dedicated, identical on both platforms, and opt-in."""
    expected = ("\n".join(URLS) + "\n").encode()
    for directory in ("configs", "clients/linux", "clients/windows"):
        assert (ROOT / directory / "www_discover_websites.txt").read_bytes() == expected
    config = configparser.ConfigParser()
    config.read(ROOT / "configs/simulation.conf")
    for bucket in range(10):
        assert config[f"s{bucket}"]["www_discover"] == "off"


def test_shared_override_parity():
    """Canonical and deploy libraries, including Windows keys, stay aligned."""
    canonical = (ROOT / "clients/lib/common.sh").read_bytes()
    assert canonical == (ROOT / "clients/linux/common.sh").read_bytes()
    shell_keys = re.search(r"CS_OVERRIDE_KEYS=\((.*?)\)", canonical.decode(), re.S)[1]
    shell_keys = shell_keys.replace("\\", "").split()
    ps = (ROOT / "clients/windows/common.ps1").read_text()
    ps_keys = re.search(r"\$script:CS_OVERRIDE_KEYS = @\((.*?)\)", ps, re.S)[1]
    assert shell_keys == re.findall(r"'([^']+)'", ps_keys)
    assert "www_discover" in shell_keys


def prepare_launch(tmp_path, contents, *, firefox=True, cpulimit=False):
    """Redirect deployment paths and stub only external browser/process tools."""
    source = (ROOT / "clients/linux/www_discover.sh").read_text()
    script = tmp_path / "www_discover.sh"
    script.write_text(source.replace("/usr/local/scripts", str(tmp_path)))
    if contents is not None:
        (tmp_path / "www_discover_websites.txt").write_text(contents)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("sed", "shuf", "tee"):
        if not shutil.which(name):
            pytest.skip(f"{name} is not installed (macOS has no shuf)")
        (bin_dir / name).symlink_to(shutil.which(name))
    stubs = {
        "timeout": 'printf "timeout %s\\n" "$*" >> "$CALLS"\nshift 3\nexec "$@"\n',
    }
    if firefox:
        stubs["firefox-esr"] = 'printf "firefox %s\\n" "$*" >> "$CALLS"\n'
    if cpulimit:
        stubs["cpulimit"] = 'printf "cpulimit %s\\n" "$*" >> "$CALLS"\nshift 4\nexec "$@"\n'
    for name, body in stubs.items():
        executable = bin_dir / name
        executable.write_text("#!/bin/bash\n" + body)
        executable.chmod(0o755)
    env = dict(os.environ, PATH=str(bin_dir), CALLS=str(tmp_path / "calls"))
    return script, env


@pytest.mark.parametrize("cpulimit", [False, True])
def test_one_uniform_pick_and_bounded_launch(tmp_path, cpulimit):
    """Both launch paths run one listed URL and retain the timeout boundary."""
    script, env = prepare_launch(tmp_path, "\n".join(URLS) + "\n\n", cpulimit=cpulimit)
    subprocess.run(["/bin/bash", str(script)], env=env, check=True, timeout=5)
    calls = (tmp_path / "calls").read_text().splitlines()
    browser = [line for line in calls if line.startswith("firefox ")]
    assert len(browser) == 1
    assert browser[0].removeprefix("firefox --headless ") in URLS
    assert calls[0].startswith("timeout -k 5 60 ")
    if cpulimit:
        assert calls[1].startswith("cpulimit -f -l 10 -- firefox-esr --headless ")
    else:
        assert len(calls) == 2
    assert "shuf -n 1" in script.read_text()


@pytest.mark.parametrize("contents,firefox,message", [
    (None, True, "Unable to read"),
    ("\n \n\t\n", True, "No websites listed"),
    (URLS[0] + "\n", False, "Firefox was not found"),
])
def test_graceful_skip(tmp_path, contents, firefox, message):
    """Missing prerequisites never launch a process and return success."""
    script, env = prepare_launch(tmp_path, contents, firefox=firefox)
    subprocess.run(["/bin/bash", str(script)], env=env, check=True, timeout=5)
    assert not (tmp_path / "calls").exists()
    assert message in (tmp_path / "sim.log").read_text()
    assert message in (tmp_path / "debug-www-discover.log").read_text()


def test_orchestrator_and_ui_wiring():
    """Flag reads, reporting, overrides, dispatch and editor lists all include it."""
    shell = (ROOT / "clients/linux/simulation.sh").read_text()
    assert 'run_simulation "www_discover.sh" 30' in shell
    assert 'www_discover=$(get_value $simulation_id' in shell
    assert '"www_discover":"%s"' in shell
    assert '"$(json_escape "${www_discover:-off}")"' in shell
    ps = (ROOT / "clients/windows/simulation.ps1").read_text()
    assert "$script:www_discover = get_value $script:simulation_id 'www_discover'" in ps
    assert "www_discover = [string]$script:www_discover" in ps
    assert "Test-ScriptRunning -ScriptName 'www_discover.ps1'" in ps
    assert "Run-Simulation -ScriptName 'www_discover.ps1'" in ps
    ui = (ROOT / "lm-spoke/static/sim-views.js").read_text()
    for name in ("CS_CONTROL_FLAGS", "CS_ONOFF_KEYS", "CS_SIM_BUCKET_FIELDS"):
        array = re.search(rf"const {name} = .*?\[(.*?)\]", ui, re.S)[1]
        if name == "CS_SIM_BUCKET_FIELDS":
            array = ui.split("const CS_SIM_BUCKET_FIELDS = [", 1)[1].split("\n];", 1)[0]
        assert "'www_discover'" in array
    for platform, extension in (("linux", "sh"), ("windows", "ps1")):
        dashboard = (ROOT / f"clients/{platform}/dashboard.{extension}").read_text()
        assert "www_discover" in dashboard
        assert "WWW Discover" in dashboard


def test_windows_launch_ownership():
    """PowerShell keeps the launched handle and never kills Firefox by name."""
    source = (ROOT / "clients/windows/www_discover.ps1").read_text()
    assert "Get-Random -InputObject $wwwfile" in source
    assert "-WindowStyle Hidden -PassThru" in source
    assert "$firefox.WaitForExit(60000)" in source
    assert "$firefox | Stop-Process -Force" in source
    assert "Stop-Process -Name" not in source
    assert "finally {" in source
