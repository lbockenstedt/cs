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
        else:
            # A refused apply (no polkit/root) restores at once and never browses.
            assert "static apply failed; restoring DHCP" in result.stdout
            assert "browser" not in operations
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


@pytest.mark.parametrize("phase", ["static", "dhcp", None, "corrupt"])
def test_linux_restore_only(tmp_path, phase):
    """--restore-only recovers a stale static phase and never starts one."""
    env = prepare_linux(tmp_path)
    if phase == "corrupt":
        (tmp_path / "rogue_dns.state").write_text("{corrupt")
        # A normal launch still refuses to touch the network on a corrupt state.
        assert run_linux(tmp_path, env).returncode == 1 and not calls(tmp_path)
    elif phase:
        seed_state(tmp_path, phase)
    before = calls(tmp_path)
    result = subprocess.run(["bash", str(tmp_path / "rogue_dns.sh"), "--restore-only"], env=env,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    operations = calls(tmp_path)[len(before):]
    assert "device modify" not in operations and "browser" not in operations
    if phase in ("static", "corrupt"):
        assert "connection up uuid test-uuid ifname wlan0" in operations
        assert read_state(tmp_path)["phase"] == "dhcp"
        assert isinstance(read_state(tmp_path)["start"], int)
    elif phase == "dhcp":
        assert not operations
        assert read_state(tmp_path)["start"] == 0
    else:
        assert not (tmp_path / "rogue_dns.state").exists()


def test_linux_sigterm_mid_static_restores(tmp_path):
    """The orchestrator's TERM ends a long static phase and its trap restores DHCP."""
    env = prepare_linux(tmp_path)
    conf = tmp_path / "simulation.conf"
    conf.write_text(conf.read_text().replace("rogue_dns_duration=1", "rogue_dns_duration=60"))
    seed_state(tmp_path)
    proc = subprocess.Popen(["bash", str(tmp_path / "rogue_dns.sh")], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 10
        while "browser" not in calls(tmp_path) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert read_state(tmp_path)["phase"] == "static"
        proc.terminate()
        assert proc.wait(timeout=5) == 0
    finally:
        proc.kill()
    assert read_state(tmp_path)["phase"] == "dhcp"
    assert "connection up uuid test-uuid ifname wlan0" in calls(tmp_path)


FAKE_LINUX_ROGUE = """#!/bin/bash
state=ROOT/rogue_dns.state
if [[ ${1:-} == --restore-only ]]; then
  echo restore-only >> ROOT/events
  printf '{"phase": "dhcp", "start": 1}' > "$state"
  exit 0
fi
if [[ ${MODE:-} == stuck ]]; then
  trap '' TERM
else
  trap 'echo cleanup >> ROOT/events; printf "{\\"phase\\": \\"dhcp\\", \\"start\\": 1}" > "$state"; exit 0' TERM
fi
firefox-esr --headless https://rogue.test/ &
echo started >> ROOT/events
while :; do sleep 0.1; done
"""


def linux_orchestrator(tmp_path, *names):
    """Extract real simulation.sh helpers into a script FILE: `bash -c` text
    containing rogue_dns.sh would itself match the helpers' pgrep."""
    source = (ROOT / "clients/linux/simulation.sh").read_text()
    body = f'LOG_FILE="{tmp_path}/sim.log"\nrogue_dns_stop_grace=2\n'
    for name in names:
        body += f"{name}() {{" + source.split(f"\n{name}() {{", 1)[1].split("\n}\n", 1)[0] + "\n}\n"
    return body.replace("/usr/local/scripts", str(tmp_path))


def start_fake_linux_rogue(tmp_path, env, start=True):
    rogue = tmp_path / "rogue_dns.sh"
    rogue.write_text(FAKE_LINUX_ROGUE.replace("ROOT", str(tmp_path)))
    if not start:
        return None
    proc = subprocess.Popen(["bash", str(rogue)], env=env, start_new_session=True)
    deadline = time.monotonic() + 5
    while "started" not in events(tmp_path) and time.monotonic() < deadline:
        time.sleep(0.05)
    return proc


def events(tmp_path):
    path = tmp_path / "events"
    return path.read_text() if path.exists() else ""


@pytest.mark.parametrize("scenario,flag,kill,running,mode,phase", [
    ("on_to_off_static", "off", "off", True, "", "static"),
    ("kill_switch_static", "on", "on", True, "", "static"),
    ("unresponsive", "off", "off", True, "stuck", "static"),
    ("stale_while_off", "off", "off", False, "", "static"),
    ("on_and_running", "on", "off", True, "", "static"),
    ("idle_dhcp", "off", "off", False, "", "dhcp"),
    ("corrupt_state", "off", "off", False, "", "corrupt"),
    ("pgrep_error", "off", "off", False, "", "static"),
])
def test_linux_orchestrator_cancels_and_recovers(tmp_path, scenario, flag, kill, running, mode, phase):
    """Off/kill switch stops a running instance; stale static state is restored."""
    env = prepare_linux(tmp_path, mode=mode)
    if phase == "corrupt":
        (tmp_path / "rogue_dns.state").write_text("{corrupt")
    else:
        seed_state(tmp_path, phase)
    proc = start_fake_linux_rogue(tmp_path, env, start=running)
    script = tmp_path / "orchestrator.sh"
    script.write_text(linux_orchestrator(tmp_path, "rogue_dns_running", "reconcile_rogue_dns")
                      + f"rogue_dns={flag}\nkill_switch={kill}\nreconcile_rogue_dns\n")
    run_env = env
    if scenario == "pgrep_error":
        # pgrep exits 2+ when the check itself fails: unknown, not "not running".
        broken = tmp_path / "broken"
        broken.mkdir()
        (broken / "pgrep").write_text("#!/bin/bash\nexit 2\n")
        (broken / "pgrep").chmod(0o755)
        run_env = dict(env, PATH=f"{broken}{os.pathsep}{env['PATH']}")
    try:
        subprocess.run(["bash", str(script)], env=run_env, check=True, timeout=15)
        alive = proc is not None and proc.poll() is None
        log = (tmp_path / "sim.log").read_text() if (tmp_path / "sim.log").exists() else ""
        if scenario == "pgrep_error":
            assert "process check failed (pgrep rc=2)" in log
            assert not events(tmp_path) and read_state(tmp_path)["phase"] == "static"
            return
        if scenario == "corrupt_state":
            assert "rogue_dns state unreadable" in log and events(tmp_path) == "restore-only\n"
            return
        if scenario == "on_and_running":
            assert alive and "cleanup" not in events(tmp_path) and "restore-only" not in events(tmp_path)
            assert read_state(tmp_path)["phase"] == "static"
            return
        assert not alive
        assert read_state(tmp_path)["phase"] == "dhcp"
        if scenario == "idle_dhcp":
            assert not events(tmp_path)
        elif scenario == "unresponsive":
            assert "force-killing" in log and "restore-only" in events(tmp_path)
        elif scenario == "stale_while_off":
            assert events(tmp_path) == "restore-only\n"
        else:
            # Graceful: the instance's own cleanup restored, no extra pass needed.
            assert "cleanup" in events(tmp_path) and "restore-only" not in events(tmp_path)
    finally:
        if proc and proc.poll() is None:
            os.killpg(proc.pid, 9)
        subprocess.run(["pkill", "-f", f"{tmp_path}/bin/firefox-esr"], check=False)


def test_linux_reconcile_runs_every_cycle_outside_flag_gate():
    """Recovery must not sit behind the rogue_dns flag or the kill-switch gate."""
    source = (ROOT / "clients/linux/simulation.sh").read_text()
    call = source.index('[[ "$gkill_switch" == "on" ]] && rogue_dns=off\nreconcile_rogue_dns\n')
    assert source.index("gkill_switch=\"off\"") < call < source.index('if [ "$kill_switch" != "on" ]')
    assert "\npkill -f 'firefox-esr.*--headless'" not in source
    assert "\nkill_headless_firefox\n" in source


def test_linux_dispatch_waits_on_failed_process_check():
    """A pgrep error must not launch a second rogue_dns.sh."""
    source = (ROOT / "clients/linux/simulation.sh").read_text()
    assert '[[ "$rogue_dns" == "on" ]] && { rogue_dns_running; (( $? == 1 )); }; then\n    run_simulation "rogue_dns.sh" 0' in source


@pytest.mark.parametrize("www,rogue,pgrep_rc,due", [
    ("on", "on", 0, False),   # static phase running: www_traffic=on must not browse
    ("on", "on", 2, False),   # failed check is unknown, not "not running"
    ("off", "on", 1, True),   # DHCP-phase fallback
    ("on", "off", 2, True),   # no rogue_dns: unchanged, no process check
    ("off", "off", 1, False),
])
def test_linux_www_browse_gated_on_rogue_dns_state(tmp_path, www, rogue, pgrep_rc, due):
    """Browsing while rogue_dns is on needs a definite not-running (rc 1)."""
    script = (linux_orchestrator(tmp_path, "rogue_dns_running", "www_browse_due")
              + f"pgrep() {{ return {pgrep_rc}; }}\nwww_traffic={www}\nrogue_dns={rogue}\n_www_iters=(7)\n"
              + "www_browse_due 3 && echo wrong-iteration\nwww_browse_due 7 && echo due || echo idle\n")
    out = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=True, timeout=5).stdout
    assert out.splitlines()[-1] == ("due" if due else "idle") and "wrong-iteration" not in out


def test_linux_www_iters_filled_while_static_phase_runs(tmp_path):
    """A static phase running at cycle start must not cancel the cycle's fallback browsing."""
    source = (ROOT / "clients/linux/simulation.sh").read_text()
    block = source.split(" declare -a _www_iters=()\n", 1)[1].split("\n fi\n", 1)[0] + "\n fi\n"
    script = ("pgrep() { return 0; }\nwww_traffic=off\nrogue_dns=on\ndeclare -a _www_iters=()\n"
              + block + 'echo "${#_www_iters[@]}"\n')
    out = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=True, timeout=5).stdout
    assert out.strip() == "5"


def test_linux_firefox_kill_spares_rogue_dns_browser(tmp_path):
    """The periodic browser kill leaves rogue_dns's static-phase browser alone."""
    env = prepare_linux(tmp_path)
    (tmp_path / "bin/firefox-esr").write_text("#!/bin/bash\nwhile :; do sleep 0.1; done\n")
    seed_state(tmp_path, "static")
    rogue = start_fake_linux_rogue(tmp_path, env)
    other = subprocess.Popen([str(tmp_path / "bin/firefox-esr"), "--headless", "https://www.test/"], env=env)
    script = tmp_path / "orchestrator.sh"
    script.write_text(linux_orchestrator(tmp_path, "kill_headless_firefox") + "kill_headless_firefox\n")
    try:
        time.sleep(0.3)
        subprocess.run(["bash", str(script)], env=env, check=True, timeout=10)
        assert other.wait(timeout=5) is not None
        survivors = subprocess.run(["pgrep", "-f", f"{tmp_path}/bin/firefox-esr --headless https://rogue.test/"],
                                   capture_output=True, text=True).stdout.split()
        assert survivors, "rogue_dns's own browser was killed"
    finally:
        os.killpg(rogue.pid, 9)
        other.kill()
        subprocess.run(["pkill", "-f", f"{tmp_path}/bin/firefox-esr"], check=False)


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
    assert '    if www_browse_due "$z"; then\n' in source
    # Every rogue_dns process check goes through the three-state helper.
    assert "! pgrep -f '[r]ogue_dns.sh'" not in source
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
    "dhcp_lease", "restore_fail", "dns_restore_fail", "override", "cancel",
    "corrupt", "corrupt_restore",
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
    if scenario == "cancel":
        conf = tmp_path / "simulation.conf"
        conf.write_text(conf.read_text().replace("rogue_dns_duration=1", "rogue_dns_duration=60"))
    stale = scenario in {"stale", "no_ip", "no_route", "neither", "dhcp_lease", "restore_fail", "dns_restore_fail"}
    if scenario != "first":
        seed_state(tmp_path, "static" if stale else "dhcp", int(time.time()) if scenario == "gate" else 0)
        state = read_state(tmp_path)
        state.update(adapter="test-guid", address="192.0.2.10", prefix=24)
        (tmp_path / "rogue_dns.state").write_text(json.dumps(state))
    if scenario.startswith("corrupt"):
        (tmp_path / "rogue_dns.state").write_text("{corrupt")
    env = dict(os.environ, TEST_ROOT=str(tmp_path), SCENARIO=scenario, COMPUTERNAME="tester-client")
    args = ["-RestoreOnly"] if scenario == "corrupt_restore" else []
    result = subprocess.run([powershell, "-NoProfile", "-NonInteractive", "-File", str(tmp_path / "rogue_dns.ps1"), *args],
                            env=env, capture_output=True, text=True, timeout=20)
    if scenario == "corrupt":
        # A normal launch still refuses to touch the network on a corrupt state.
        assert result.returncode == 1 and not calls(tmp_path)
        return
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
        if scenario == "corrupt_restore":
            # No captured values: every manual address/route on the Wi-Fi adapter goes.
            assert "remove ip" in operations and "remove route" in operations
            assert "dhcp Disabled" not in operations
        elif not stale:
            assert "dhcp Disabled" in operations
            assert "new ip" in operations
        if scenario == "override":
            assert "dns 192.0.2.99" in operations
    if scenario == "cancel":
        # The stop file ends a 60s static phase (well inside the 20s timeout).
        assert "stop requested; restoring DHCP" in result.stdout


WINDOWS_ORCHESTRATOR_HARNESS = r"""
$root = $env:TEST_ROOT
$procFile = Join-Path $root 'procs.json'
function Get-Procs { if (Test-Path $procFile) { @(Get-Content $procFile -Raw | ConvertFrom-Json) } else { @() } }
function Set-Procs($procs) { ConvertTo-Json -InputObject @($procs) -Depth 3 | Set-Content $procFile }
function Add-Event($text) { Add-Content -LiteralPath (Join-Path $root 'events') -Value $text }
function Get-CimInstance { [CmdletBinding()] param($ClassName)
    $script:cimCalls++
    # cim_fail_late: the first query works, every later one fails.
    if ($env:MODE -eq 'cim_fail' -or ($env:MODE -eq 'cim_fail_late' -and $script:cimCalls -gt 1)) {
        Write-Error 'RPC server unavailable'
    } else { Get-Procs } }
function Write-SimLog { param($Message) Add-Event "log $Message" }
function Wait-Process { [CmdletBinding()] param($Id, $Timeout) }
function Get-Process { [CmdletBinding()] param($Name)
    Get-Procs | Where-Object { $_.Name -eq $Name } | ForEach-Object { [pscustomobject]@{ Id = $_.ProcessId } } }
function Stop-Process { [CmdletBinding()] param([Parameter(ValueFromPipelineByPropertyName)][int]$Id, [switch]$Force)
    process { Add-Event "stop $Id"; Set-Procs @(Get-Procs | Where-Object { [int]$_.ProcessId -ne $Id }) } }
# A graceful rogue_dns.ps1 sees the stop file, restores DHCP in its finally, exits.
function Start-Sleep { [CmdletBinding()] param($Seconds, $Milliseconds)
    if ($env:MODE -ne 'stuck' -and (Test-Path (Join-Path $root 'rogue_dns.stop'))) {
        Set-Procs @(Get-Procs | Where-Object { $_.CommandLine -notlike '*rogue_dns.ps1*' })
        Set-Content -LiteralPath (Join-Path $root 'rogue_dns.state') -Value '{"phase": "dhcp", "start": 1}'
        Add-Event 'cleanup'
    } }
. (Join-Path $root 'ini-parser.ps1')
$killSwitchPath = Join-Path $root 'kill_switch.txt'
$rogueDnsScript = Join-Path $root 'rogue_dns.ps1'
$rogueDnsStatePath = Join-Path $root 'rogue_dns.state'
$rogueDnsStopPath = Join-Path $root 'rogue_dns.stop'
$rogueDnsStopGrace = 2
. (Join-Path $root 'functions.ps1')
$script:username = 'tester'
$script:simulation_id = 's0'
$script:random_pool = 'off'
$script:rogue_dns = $env:FLAG
$script:kill_switch = $env:KILL
$script:www_traffic = $env:WWW
$script:rogueDnsBrowseReady = $true
$result = & $env:ACTION
if ($null -ne $result) { Add-Event "result $result" }
Add-Event "flag $script:rogue_dns"
"""

FAKE_WINDOWS_ROGUE = """param([switch]$RestoreOnly)
Add-Content -LiteralPath (Join-Path $env:TEST_ROOT 'events') -Value "restore-only $RestoreOnly"
Set-Content -LiteralPath (Join-Path $env:TEST_ROOT 'rogue_dns.state') -Value '{"phase": "dhcp", "start": 1}'
"""


@pytest.mark.parametrize("scenario,action,flag,kill,running,mode,phase,conf,expected,absent", [
    ("on_to_off_static", "Sync-RogueDns", "off", "off", True, "", "static", "", ["cleanup"], ["restore-only", "stop 10"]),
    ("kill_switch_static", "Sync-RogueDns", "on", "on", True, "", "static", "", ["cleanup"], ["restore-only", "stop 10"]),
    ("unresponsive", "Sync-RogueDns", "off", "off", True, "stuck", "static", "", ["stop 10", "force-killing", "restore-only True"], ["cleanup"]),
    ("stale_while_off", "Sync-RogueDns", "off", "off", False, "", "static", "", ["restore-only True"], ["stop "]),
    ("on_and_running", "Sync-RogueDns", "on", "off", True, "", "static", "", [], ["cleanup", "restore-only", "stop "]),
    ("idle_dhcp", "Sync-RogueDns", "off", "off", False, "", "dhcp", "", [], ["restore-only", "stop "]),
    ("corrupt_state", "Sync-RogueDns", "off", "off", False, "", "corrupt", "", ["rogue_dns state unreadable", "restore-only True"], ["stop "]),
    ("cim_fail_stale", "Sync-RogueDns", "off", "off", False, "cim_fail", "static", "", ["process check failed"], ["restore-only"]),
    ("cim_fail_during_stop", "Sync-RogueDns", "off", "off", True, "cim_fail_late", "static", "",
     ["instance state unknown after stop request; keeping stop marker"], ["restore-only", "stop 10", "force-killing"]),
    # www_traffic=on is also the re-armed launch latch: it must not browse during the static phase.
    ("browse_static_www_on", "Test-WwwBrowseDue", "on", "off", True, "", "static", "", ["result False"], []),
    ("browse_unknown", "Test-WwwBrowseDue", "on", "off", False, "cim_fail", "dhcp", "", ["result False"], []),
    ("browse_dhcp_fallback", "Test-WwwBrowseDue", "on", "off", False, "", "dhcp", "", ["result True"], []),
    ("browse_no_rogue_dns", "Test-WwwBrowseDue", "off", "off", True, "cim_fail", "static", "", ["result True"], []),
    ("midcycle_off", "Update-RogueDnsGate", "on", "off", True, "", "static", "[s0]\nrogue_dns=off\n", ["cleanup", "flag off"], ["restore-only"]),
    ("midcycle_kill_file", "Update-RogueDnsGate", "on", "off", True, "", "static", "[s0]\nrogue_dns=on\n", ["cleanup", "flag off"], ["restore-only"]),
    ("midcycle_still_on", "Update-RogueDnsGate", "on", "off", True, "", "static", "[s0]\nrogue_dns=on\n", ["flag on"], ["cleanup"]),
    ("firefox", "Stop-SimFirefox", "on", "off", True, "", "static", "", ["stop 12"], ["stop 10", "stop 11"]),
])
def test_windows_orchestrator_cancels_and_recovers(tmp_path, scenario, action, flag, kill, running, mode, phase, conf, expected, absent):
    """Run the real simulation.ps1 helpers against a mocked process table."""
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if not powershell:
        pytest.skip("PowerShell is not installed; run this harness on Windows CI")
    source = (ROOT / "clients/windows/simulation.ps1").read_text()
    functions = ""
    for name in ("Get-ScriptRunState", "Test-ScriptRunning", "Sync-RogueDns", "Update-RogueDnsGate", "Stop-SimFirefox",
                 "Test-WwwBrowseDue"):
        functions += f"function {name} {{" + source.split(f"\nfunction {name} {{", 1)[1].split("\n}\n", 1)[0] + "\n}\n"
    (tmp_path / "functions.ps1").write_text(functions.replace("C:\\Scripts\\", str(tmp_path) + "/"))
    (tmp_path / "ini-parser.ps1").write_text((ROOT / "clients/windows/ini-parser.ps1").read_text())
    (tmp_path / "harness.ps1").write_text(WINDOWS_ORCHESTRATOR_HARNESS)
    (tmp_path / "rogue_dns.ps1").write_text(FAKE_WINDOWS_ROGUE)
    (tmp_path / "simulation.conf").write_text(conf)
    if scenario == "midcycle_kill_file":
        (tmp_path / "kill_switch.txt").write_text("on\n")
    (tmp_path / "rogue_dns.state").write_text("{corrupt" if phase == "corrupt" else json.dumps({"phase": phase, "start": 1}))
    procs = [dict(ProcessId=1, ParentProcessId=0, Name="powershell", CommandLine="powershell -File simulation.ps1")]
    if running:
        procs += [dict(ProcessId=10, ParentProcessId=1, Name="powershell", CommandLine="powershell -File C:/Scripts/rogue_dns.ps1"),
                  dict(ProcessId=11, ParentProcessId=10, Name="firefox", CommandLine="firefox --headless https://rogue.test/")]
    procs.append(dict(ProcessId=12, ParentProcessId=1, Name="firefox", CommandLine="firefox --headless https://www.test/"))
    (tmp_path / "procs.json").write_text(json.dumps(procs))
    env = dict(os.environ, TEST_ROOT=str(tmp_path), ACTION=action, FLAG=flag, KILL=kill, MODE=mode,
               WWW="on" if scenario.startswith("browse") else "off")
    result = subprocess.run([powershell, "-NoProfile", "-NonInteractive", "-File", str(tmp_path / "harness.ps1")],
                            env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0 and not result.stderr, result.stdout + result.stderr
    log = events(tmp_path)
    for text in expected:
        assert text in log, log
    for text in absent:
        assert text not in log, log
    assert (tmp_path / "rogue_dns.stop").exists() == (scenario == "cim_fail_during_stop")
    if action == "Sync-RogueDns" and scenario not in {"on_and_running", "cim_fail_stale", "cim_fail_during_stop"}:
        assert read_state(tmp_path)["phase"] == "dhcp"


def test_windows_orchestrator_wiring():
    """Recovery runs every cycle outside the flag gate; www_traffic re-arm is unchanged."""
    source = (ROOT / "clients/windows/simulation.ps1").read_text()
    loop = source.split("\nwhile ($true) {", 1)[1]
    assert loop.index("$script:kill_switch = $globalKill") < loop.index("\n    Sync-RogueDns\n") < loop.index("if ($script:kill_switch -eq 'off')")
    assert "-RestoreOnly" not in source.split("\nwhile ($true) {", 1)[0].split("function Sync-RogueDns", 1)[0]
    assert "if ($script:rogue_dns -eq 'on') { Update-RogueDnsGate }" in loop
    assert "wwwTrafficConfigured" not in source
    assert "Stop-SimFirefox\n                    $script:www_traffic = 'on'\n" in source
    assert "Stop-Process -Name firefox" not in source
    # Dispatch launches only on a definite 'stopped', never on a failed query.
    assert "if ($rogueDnsRun -eq 'stopped') { Run-Simulation -ScriptName 'rogue_dns.ps1' }" in loop
    assert "Test-ScriptRunning -ScriptName 'rogue_dns.ps1'" not in source
    assert "                if (Test-WwwBrowseDue) {\n" in loop
