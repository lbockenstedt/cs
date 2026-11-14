"""Isolated Rogue DNS state-machine tests: no real adapters or browsers touched."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time

import pytest

ROOT = Path(__file__).resolve().parents[2]


def prepare_linux(tmp_path, *, server="192.0.2.53", mode="", override=""):
    """Use the real parser/override helpers and fake only host commands."""
    for name in ("rogue_dns.sh", "ini-parser.sh", "common.sh"):
        source = (ROOT / "clients/linux" / name).read_text()
        (tmp_path / name).write_text(source.replace("/usr/local/scripts", str(tmp_path)))
    (tmp_path / "simulation.conf").write_text(
        f"[address]\nrogue_dns_server={server}\n[simulation]\n"
        "rogue_dns_dhcp_interval=1800\nrogue_dns_duration=1\n"
        f"rogue_dns_browse_interval=1\n[tester]\n{override}\n"
    )
    (tmp_path / "websites.txt").write_text("https://example.test/\n")
    (tmp_path / "VERSION").write_text("test\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    nmcli = bin_dir / "nmcli"
    nmcli.write_text("#!/usr/bin/env python3\n" + '''import json, os, pathlib, sys
args = sys.argv[1:]
root = pathlib.Path(os.environ['TEST_ROOT'])
mode = os.environ.get('MODE', '')
with (root / 'calls').open('a') as f:
    f.write(' '.join(args) + '\\n')
if args[:2] == ['device', 'modify']:
    s = json.loads((root / 'rogue_dns.state').read_text())
    assert s['phase'] == 'static' and s['address'] == '192.0.2.10/24'
    sys.exit(1 if mode == 'apply_fail' else 0)
if args[:2] == ['connection', 'up']:
    sys.exit(1 if mode == 'restore_fail' else 0)
if args[:2] == ['device', 'reapply']:
    sys.exit(0)
if args[0] == '-t':
    print('wlan0:wifi:connected')
else:
    values = {'GENERAL.CON-UUID': 'test-uuid', 'IP4.ADDRESS': '192.0.2.10/24',
              'IP4.GATEWAY': '192.0.2.1', 'IP4.DNS': '192.0.2.1',
              'ipv4.method': 'auto', 'DHCP4.OPTION': 'lease = yes'}
    print('' if mode == 'no_lease' and args[1] == 'IP4.ADDRESS' else values[args[1]])
''')
    nmcli.chmod(0o755)
    for name, body in {
        "firefox-esr": 'echo "browser $*" >> "$TEST_ROOT/calls"\n',
        "cpulimit": 'shift 4\nexec "$@"\n',
        # timeout -k 1 <delay> <cmd>: not on every host (macOS), so mock it too.
        "timeout": 'shift 3\nexec "$@"\n',
    }.items():
        executable = bin_dir / name
        executable.write_text("#!/bin/bash\n" + body)
        executable.chmod(0o755)
    env = dict(os.environ, PATH=str(bin_dir) + os.pathsep + os.environ["PATH"],
               TEST_ROOT=str(tmp_path), HOSTNAME="tester-client", MODE=mode)
    return env


def seed_state(tmp_path, phase="dhcp", start=0):
    """Write the persisted input shared by both platform harnesses."""
    state = dict(phase=phase, start=start, adapter="wlan0", address="192.0.2.10/24",
                 gateway="192.0.2.1", dns="192.0.2.1", connection="test-uuid")
    (tmp_path / "rogue_dns.state").write_text(json.dumps(state))


def run_linux(tmp_path, env):
    """Run a redirected script with a hard test timeout."""
    return subprocess.run(["bash", str(tmp_path / "rogue_dns.sh")], env=env,
                          capture_output=True, text=True, timeout=10)


def read_state(tmp_path):
    """Read the atomic state result."""
    return json.loads((tmp_path / "rogue_dns.state").read_text(encoding="utf-8-sig"))


def calls(tmp_path):
    """Read mocked network/browser invocations, if any."""
    path = tmp_path / "calls"
    return path.read_text() if path.exists() else ""


def test_linux_first_run_and_interval_gate(tmp_path):
    """First launch starts DHCP; subsequent early launches never touch NM."""
    env = prepare_linux(tmp_path)
    assert run_linux(tmp_path, env).returncode == 0
    first = read_state(tmp_path)
    assert first["phase"] == "dhcp" and first["start"] > 0
    assert run_linux(tmp_path, env).returncode == 0
    assert read_state(tmp_path) == first
    assert not calls(tmp_path)


@pytest.mark.parametrize("server", ["", "invalid", "0.1.2.3", "127.0.0.1", "224.0.0.1", "240.0.0.1", "255.255.255.255"])
def test_linux_invalid_dns(tmp_path, server):
    """Reject unusable resolvers before any network operation."""
    env = prepare_linux(tmp_path, server=server)
    seed_state(tmp_path)
    assert run_linux(tmp_path, env).returncode == 0
    assert not calls(tmp_path)
    assert read_state(tmp_path)["start"] == 0


@pytest.mark.parametrize("mode,expected,phase", [
    ("no_lease", 0, "dhcp"), ("", 0, "dhcp"),
    ("apply_fail", 1, "dhcp"), ("restore_fail", 1, "static"),
])
def test_linux_apply_and_restore(tmp_path, mode, expected, phase):
    """Persist before apply; reset the epoch only after successful restore."""
    env = prepare_linux(tmp_path, mode=mode)
    seed_state(tmp_path)
    result = run_linux(tmp_path, env)
    assert result.returncode == expected, result.stderr
    assert read_state(tmp_path)["phase"] == phase
    operations = calls(tmp_path)
    if mode == "no_lease":
        assert "device modify" not in operations
        assert read_state(tmp_path)["start"] == 0
    else:
        assert "ipv4.dns 192.0.2.53" in operations
        assert "connection up uuid test-uuid ifname wlan0" in operations
        if mode != "apply_fail":
            assert "browser --headless https://example.test/" in operations
        if phase == "dhcp":
            assert read_state(tmp_path)["start"] > 0


@pytest.mark.parametrize("mode", ["", "restore_fail"])
def test_linux_stale_recovery(tmp_path, mode):
    """Stale static state recovers even with blank DNS and restarts DHCP."""
    env = prepare_linux(tmp_path, server="", mode=mode)
    seed_state(tmp_path, "static")
    result = run_linux(tmp_path, env)
    assert result.returncode == (1 if mode else 0)
    assert read_state(tmp_path)["phase"] == ("static" if mode else "dhcp")
    assert "device modify" not in calls(tmp_path)
    assert "connection up" in calls(tmp_path)


def test_linux_per_client_override(tmp_path):
    """The real singular apply_override helper overrides the blank global DNS."""
    env = prepare_linux(tmp_path, server="", override="rogue_dns_server=192.0.2.99")
    seed_state(tmp_path)
    assert run_linux(tmp_path, env).returncode == 0
    assert "ipv4.dns 192.0.2.99" in calls(tmp_path)


def test_linux_dispatch_is_guarded_async_and_uncapped(tmp_path):
    """Execute the actual helper against a long sim; the second arg is a pause."""
    source = (ROOT / "clients/linux/simulation.sh").read_text()
    helper = source.split("run_simulation() {", 1)[1].split("\n}", 1)[0]
    assert 'nohup bash "/usr/local/scripts/$script"' in helper
    assert "timeout" not in helper
    assert "if pgrep -f" in helper
    assert "[[ \"$rogue_dns\" == \"on\" ]] && ! pgrep -f '[r]ogue_dns.sh'" in source
    assert 'run_simulation "rogue_dns.sh" 0' in source
    # A delayed child creates its marker independently of the launcher's return.
    (tmp_path / "rogue_dns.sh").write_text(f'sleep 1\ntouch "{tmp_path}/finished"\n')
    script = "run_simulation() {" + helper + "\n}\n"
    script = script.replace("/usr/local/scripts", str(tmp_path))
    script += '_rsleep() { :; }\nrun_simulation rogue_dns.sh 0\nrun_simulation rogue_dns.sh 0\n'
    subprocess.run(["bash", "-c", script], check=True, timeout=5)
    # No assertion based on wall time: async ownership and lack of a cap above
    # are deterministic; wait generously for cleanup of the test child.
    deadline = time.monotonic() + 5
    while not (tmp_path / "finished").exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert (tmp_path / "finished").exists()


def test_helpers_and_copy_contract():
    """All reviewed helper names exist and canonical/deploy libraries match."""
    lib = (ROOT / "clients/lib/common.sh").read_bytes()
    assert lib == (ROOT / "clients/linux/common.sh").read_bytes()
    assert b"apply_override()" in lib
    ps = (ROOT / "clients/windows/common.ps1").read_text()
    for name in ("Get-SimOverrides", "Write-SimLog", "Write-SimVersionBanner", "Get-WlanAdapter"):
        assert f"function {name} {{" in ps
    shell_keys = re.search(r"CS_OVERRIDE_KEYS=\((.*?)\)", lib.decode(), re.S)[1].replace("\\", "").split()
    ps_keys = re.search(r"\$script:CS_OVERRIDE_KEYS = @\((.*?)\)", ps, re.S)[1]
    assert shell_keys == re.findall(r"'([^']+)'", ps_keys)
    assert (ROOT / "clients/linux/rogue_dns.sh").read_text().count("version=0.01") == 1


@pytest.mark.parametrize("scenario", [
    "first", "gate", "invalid", "reserved", "broadcast", "no_lease", "success",
    "apply_fail", "disabled_no_ip", "stale", "no_ip", "no_route", "neither",
    "dhcp_lease", "restore_fail", "dns_restore_fail", "override",
])
def test_windows_mocked_state_machine(tmp_path, scenario):
    """Run the PS twin with mocked NetTCPIP commands, including partial restores."""
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if not powershell:
        pytest.skip("PowerShell is not installed; run this harness on Windows CI")
    for name in ("rogue_dns.ps1", "ini-parser.ps1", "common.ps1"):
        source = (ROOT / "clients/windows" / name).read_text()
        source = source.replace("C:\\Scripts\\", str(tmp_path) + "/").replace("C:\\Scripts", str(tmp_path))
        if name == "rogue_dns.ps1":
            source = source.replace("Write-SimVersionBanner 'rogue_dns.ps1' $version",
                                    f". '{tmp_path.as_posix()}/mocks.ps1'")
        (tmp_path / name).write_text(source)
    shutil.copyfile(Path(__file__).with_name("rogue_dns_mocks.ps1"), tmp_path / "mocks.ps1")
    server = {"invalid": "", "override": "", "reserved": "240.0.0.1", "broadcast": "255.255.255.255"}.get(scenario, "192.0.2.53")
    (tmp_path / "simulation.conf").write_text(
        f"[address]\nrogue_dns_server={server}\n[simulation]\nrogue_dns_dhcp_interval=1800\n"
        "rogue_dns_duration=1\nrogue_dns_browse_interval=99999999\n"
        + ("[tester]\nrogue_dns_server=192.0.2.99\n" if scenario == "override" else "")
    )
    stale = scenario in {"stale", "no_ip", "no_route", "neither", "dhcp_lease", "restore_fail", "dns_restore_fail"}
    if scenario != "first":
        seed_state(tmp_path, "static" if stale else "dhcp", int(time.time()) if scenario == "gate" else 0)
        state = read_state(tmp_path)
        state.update(adapter="test-guid", address="192.0.2.10", prefix=24)
        (tmp_path / "rogue_dns.state").write_text(json.dumps(state))
    env = dict(os.environ, TEST_ROOT=str(tmp_path), SCENARIO=scenario, COMPUTERNAME="tester-client")
    result = subprocess.run([powershell, "-NoProfile", "-NonInteractive", "-File", str(tmp_path / "rogue_dns.ps1")],
                            env=env, capture_output=True, text=True, timeout=20)
    failure = scenario in {"apply_fail", "restore_fail", "dns_restore_fail"}
    assert result.returncode == (1 if failure else 0), result.stdout + result.stderr
    state = read_state(tmp_path)
    assert state["phase"] == ("static" if scenario in {"restore_fail", "dns_restore_fail"} else "dhcp")
    operations = calls(tmp_path)
    if scenario in {"first", "gate", "invalid", "reserved", "broadcast", "no_lease"}:
        assert not operations
    else:
        if scenario != "restore_fail":
            assert "dhcp Enabled" in operations
        if scenario not in {"restore_fail", "dns_restore_fail"}:
            assert "dns reset" in operations
            assert state["start"] > 0
        if scenario in {"neither", "dhcp_lease"}:
            assert "remove ip" not in operations
        if not stale:
            assert "dhcp Disabled" in operations
            assert "new ip" in operations
        if scenario == "override":
            assert "dns 192.0.2.99" in operations
