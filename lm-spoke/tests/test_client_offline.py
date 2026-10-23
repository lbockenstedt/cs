"""Lifecycle safety tests; fake adapters only, never change host networking."""
import configparser
import os
from pathlib import Path
import re
import signal
import subprocess
import time

import pytest

ROOT = Path(__file__).resolve().parents[2]
LINUX = ROOT / "clients/linux"


def test_copy_and_override_parity():
    canonical = (ROOT / "clients/lib/common.sh").read_bytes()
    assert canonical == (LINUX / "common.sh").read_bytes()
    shell_keys = re.search(r"CS_OVERRIDE_KEYS=\((.*?)\)", canonical.decode(), re.S)[1]
    shell_keys = shell_keys.replace("\\", "").split()
    ps = (ROOT / "clients/windows/common.ps1").read_text()
    ps_keys = re.search(r"\$script:CS_OVERRIDE_KEYS = @\((.*?)\)", ps, re.S)[1]
    assert shell_keys == re.findall(r"'([^']+)'", ps_keys)
    assert "client_offline" in shell_keys


def test_config_defaults():
    config = configparser.ConfigParser()
    config.read(ROOT / "configs/simulation.conf")
    assert config["simulation"]["allow_offline"] == "no"
    assert config["simulation"]["client_offline_min_minutes"] == "60"
    assert config["simulation"]["client_offline_max_minutes"] == "240"
    for bucket in range(10):
        assert config[f"s{bucket}"]["client_offline"] == "off"


def make_script(tmp_path):
    # Replace only deployment plumbing. Keep the real selection, duration,
    # management guard, link state, traps and wait code under test.
    source = (LINUX / "client_offline.sh").read_text()
    source = re.sub(r"^source .*\n", "", source, flags=re.M)
    source = source.replace("LOG_FILE='/usr/local/scripts/sim.log'", 'LOG_FILE="$TEST_LOG"')
    mocks = r'''
process_ini_file() { :; }
sim_version_banner() { :; }
get_value() {
  case "$2" in
    client_offline_min_minutes) printf '%s' "$TEST_MIN" ;;
    client_offline_max_minutes) printf '%s' "$TEST_MAX" ;;
  esac
}
detect_wlan_adapter() { wladapter="${TEST_WIFI-wlan0}"; }
detect_eth_adapter() { eadapter="${TEST_ETH-eth0}"; }
ip() {
  if [[ "$1" == -4 ]]; then
    [[ "${TEST_ADDR_ERROR:-0}" == 0 ]] || return 1
    if [[ "$TEST_MGMT" == "$5" ]]; then echo '    inet 169.253.1.10/24'; fi
    return 0
  fi
  if [[ "$*" == *'link set'* ]]; then
    echo "$*" >> "$TEST_ACTIONS"
    [[ "${TEST_DOWN_FAIL:-0}" == 0 || "$*" != *' down' ]]
    return
  fi
  echo '2: adapter: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500'
}
sudo() { "$@"; }
shuf() {
  echo "$2" > "$TEST_RANGE"
  echo "${2%-*}"
}
sleep() {
  echo "$1" >> "$TEST_SLEEPS"
  if [[ "${TEST_REAL_SLEEP:-0}" == 1 ]]; then command sleep 30; fi
}
'''
    script = tmp_path / "client_offline.sh"
    script.write_text(mocks + source)
    return script


def env_for(tmp_path, **updates):
    env = dict(os.environ, TEST_MIN="60", TEST_MAX="240", TEST_MGMT="",
               TEST_LOG=str(tmp_path / "sim.log"), TEST_RANGE=str(tmp_path / "range"),
               TEST_ACTIONS=str(tmp_path / "actions"), TEST_SLEEPS=str(tmp_path / "sleeps"))
    env.update(updates)
    return env


@pytest.mark.parametrize("phy,adapter", [("wireless", "wlan0"), ("ethernet", "eth0")])
def test_only_selected_adapter_is_restored(tmp_path, phy, adapter):
    script = make_script(tmp_path)
    # Management lives on the OTHER adapter; it must not block or touch it.
    env = env_for(tmp_path, TEST_MGMT="eth0" if phy == "wireless" else "wlan0")
    subprocess.run(["bash", str(script), phy], env=env, check=True, timeout=5)
    assert (tmp_path / "actions").read_text().splitlines() == [
        f"link set dev {adapter} down", f"link set dev {adapter} up"]
    assert (tmp_path / "sleeps").read_text().strip() == "3600"
    assert (tmp_path / "range").read_text().strip() == "3600-14400"


@pytest.mark.parametrize("phy,updates", [
    ("wireless", {"TEST_MGMT": "wlan0"}),
    ("ethernet", {"TEST_MGMT": "eth0"}),
    ("wireless", {"TEST_WIFI": ""}),
    ("ethernet", {"TEST_ETH": ""}),
    ("ethernet", {"TEST_ETH": "eth0\neth1"}),
    ("wireless", {"TEST_ADDR_ERROR": "1"}),
    ("unknown", {}),
])
def test_unsafe_adapter_skips_without_sleep(tmp_path, phy, updates):
    subprocess.run(["bash", str(make_script(tmp_path)), phy],
                   env=env_for(tmp_path, **updates), check=True, timeout=5)
    assert not (tmp_path / "actions").exists()
    assert not (tmp_path / "sleeps").exists()
    assert "skipping window" in (tmp_path / "sim.log").read_text()


@pytest.mark.parametrize("minimum,maximum,expected", [
    ("", "bad", "3600-14400"), ("0", "0", "3600-14400"),
    ("-1", "1.5", "3600-14400"), ("120", "60", "3600-7200"),
    ("1", "1", "60-60"), ("08", "09", "480-540"),
    ("9999999999999999999999", "240", "3600-14400"),
])
def test_duration_validation(tmp_path, minimum, maximum, expected):
    subprocess.run(["bash", str(make_script(tmp_path))],
                   env=env_for(tmp_path, TEST_MIN=minimum, TEST_MAX=maximum),
                   check=True, timeout=5)
    assert (tmp_path / "range").read_text().strip() == expected


def test_failed_disable_does_not_sleep_or_restore(tmp_path):
    subprocess.run(["bash", str(make_script(tmp_path))],
                   env=env_for(tmp_path, TEST_DOWN_FAIL="1"), check=True, timeout=5)
    assert (tmp_path / "actions").read_text().splitlines() == ["link set dev wlan0 down"]
    assert not (tmp_path / "sleeps").exists()


def test_usr1_interrupts_child_and_restores_before_return(tmp_path):
    child = make_script(tmp_path)
    orchestrator = (LINUX / "simulation.sh").read_text()
    trap_line = next(line for line in orchestrator.splitlines() if line.startswith("trap '") and line.endswith(" USR1"))
    dispatch = orchestrator.split('# Foreground lifecycle window:', 1)[1]
    dispatch = '# Foreground lifecycle window:' + dispatch.split('#Looping Script', 1)[0]
    dispatch = dispatch.replace('/usr/local/scripts/client_offline.sh', str(child))
    harness = tmp_path / "orchestrator.sh"
    harness.write_text('_sim_reload=0; _offline_pid=""; _sleep_pid=""\n'
                       'client_offline=on; kill_switch=off; sim_phy=wireless\n'
                       'pgrep() { return 1; }\n' + trap_line + '\n' + dispatch)
    proc = subprocess.Popen(["bash", str(harness)], env=env_for(tmp_path, TEST_REAL_SLEEP="1"),
                            start_new_session=True)
    try:
        deadline = time.monotonic() + 5
        while not (tmp_path / "sleeps").exists():
            assert proc.poll() is None
            assert time.monotonic() < deadline
            time.sleep(0.01)
        proc.send_signal(signal.SIGUSR1)
        assert proc.wait(timeout=5) == 0
        assert (tmp_path / "actions").read_text().splitlines() == [
            "link set dev wlan0 down", "link set dev wlan0 up"]
    finally:
        # The test's mock shell sleep may leave its own command child; reap it.
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait()
