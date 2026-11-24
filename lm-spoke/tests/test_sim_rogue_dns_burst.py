"""Rogue DNS Burst: script behaviour on a fake host, plus wiring/parity checks."""
import json
import os
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[2]
CLIENTS = ROOT / "clients"
STATE_NAME = "rogue_dns_burst.state"

NMCLI = '''#!/usr/bin/env python3
import os, sys
args = sys.argv[1:]
root = os.environ['TEST_ROOT']
with open(root + '/calls', 'a') as f:
    f.write(' '.join(args) + '\\n')
if args[:2] in (['device', 'modify'], ['connection', 'up'], ['device', 'reapply']):
    sys.exit(0)
if args[0] == '-t':
    print('wlan0:wifi:connected')
else:
    values = {'GENERAL.CON-UUID': 'test-uuid', 'IP4.ADDRESS': '192.0.2.10/24',
              'IP4.GATEWAY': '192.0.2.1', 'IP4.DNS': '192.0.2.1',
              'ipv4.method': 'auto', 'DHCP4.OPTION': 'lease = yes'}
    print(values[args[1]])
'''


def prepare(tmp_path, *, with_dig=True, domains="a.test b.test"):
    """Redirect the real script/parser/common into tmp and fake host commands."""
    for name in ("rogue_dns_burst.sh", "ini-parser.sh", "common.sh"):
        text = (CLIENTS / "linux" / name).read_text()
        (tmp_path / name).write_text(text.replace("/usr/local/scripts", str(tmp_path)))
    (tmp_path / "simulation.conf").write_text(
        "[address]\nrogue_dns_burst_server=192.0.2.53\n"
        f"rogue_dns_burst_domains={domains}\n[simulation]\n"
        "rogue_dns_burst_dhcp_interval=1800\nrogue_dns_burst_duration=2\n"
        "rogue_dns_burst_browse_interval=1\nrogue_dns_burst_rate=3\n"
        "rogue_dns_burst_max_pending=5\n[tester]\n"
    )
    (tmp_path / "VERSION").write_text("test\n")
    (tmp_path / STATE_NAME).write_text(json.dumps(dict(
        phase="dhcp", start=0, adapter="wlan0", address="192.0.2.10/24",
        gateway="192.0.2.1", dns="192.0.2.1", connection="test-uuid")))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "nmcli").write_text(NMCLI)
    tools = {"timeout": 'shift 3\nexec "$@"\n'}
    if with_dig:
        tools["dig"] = 'echo "dig $*" >> "$TEST_ROOT/dig_calls"\n'
    for name, body in tools.items():
        (bin_dir / name).write_text("#!/bin/bash\n" + body)
    for path in bin_dir.iterdir():
        path.chmod(0o755)
    return dict(os.environ, PATH=str(bin_dir) + os.pathsep + os.environ["PATH"],
                TEST_ROOT=str(tmp_path), HOSTNAME="tester-client")


def run(tmp_path, env):
    """Run the redirected script with a hard timeout."""
    return subprocess.run(["bash", str(tmp_path / "rogue_dns_burst.sh")], env=env,
                          capture_output=True, text=True, timeout=30)


def test_burst_queries_rogue_server_round_robin_then_restores_dhcp(tmp_path):
    """The static window sends dig queries at the server; DHCP is restored after."""
    env = prepare(tmp_path)
    result = run(tmp_path, env)
    assert result.returncode == 0, result.stderr
    digs = (tmp_path / "dig_calls").read_text().splitlines()
    assert digs and all("@192.0.2.53" in d and "+time=2" in d and "+tries=1" in d for d in digs)
    assert any(" a.test " in d for d in digs) and any(" b.test " in d for d in digs)
    assert json.loads((tmp_path / STATE_NAME).read_text())["phase"] == "dhcp"
    calls = (tmp_path / "calls").read_text().splitlines()
    assert calls.index(next(c for c in calls if c.startswith("device modify"))) < \
        calls.index(next(c for c in calls if c.startswith("connection up")))


def test_burst_skipped_without_dig(tmp_path):
    """No dig: log it, still run the browse window and restore DHCP."""
    env = prepare(tmp_path, with_dig=False)
    # The host may ship dig: expose only the tools the script needs, not dig.
    import shutil
    for tool in ("bash", "python3", "date", "awk", "sed", "grep", "sleep", "paste", "head",
                 "tee", "cat", "mv", "rm", "kill", "dirname", "basename", "tr", "id",
                 "whoami", "hostname", "uname", "mkdir", "chmod", "ping", "ip", "pgrep", "env"):
        found = shutil.which(tool)
        if found and not (tmp_path / "bin" / tool).exists():
            (tmp_path / "bin" / tool).symlink_to(found)
    env["PATH"] = str(tmp_path / "bin")
    result = run(tmp_path, env)
    assert result.returncode == 0, result.stderr
    assert "dig not installed" in result.stdout
    assert json.loads((tmp_path / STATE_NAME).read_text())["phase"] == "dhcp"


def test_script_has_no_self_kill_and_shared_throttle():
    """Uses the shared ceiling + breaker, never pkill, never rogue_dns's state."""
    for name in ("linux/rogue_dns_burst.sh", "windows/rogue_dns_burst.ps1"):
        text = (CLIENTS / name).read_text()
        assert "pkill" not in text and "rogue_dns.state" not in text
    sh = (CLIENTS / "linux/rogue_dns_burst.sh").read_text()
    for helper in ("dns_ceiling_rate", "dns_gw_confirmed_down", "dns_ceiling_penalize", "dns_gw_stable"):
        assert helper in sh
    ps = (CLIENTS / "windows/rogue_dns_burst.ps1").read_text()
    for helper in ("Get-DnsCeilingRate", "Test-DnsGatewayConfirmedDown", "Invoke-DnsCeilingPenalize", "Test-DnsGatewayStable"):
        assert helper in ps
    assert ps.index("Stop-Job") < ps.index("Restore-RogueDnsBurstDhcp $state", ps.index("Stop-Job"))


def test_override_keys_and_buckets():
    """Flag and knobs are overridable on both platforms; every bucket has the flag."""
    keys = ["rogue_dns_burst", "rogue_dns_burst_server", "rogue_dns_burst_dhcp_interval",
            "rogue_dns_burst_duration", "rogue_dns_burst_browse_interval",
            "rogue_dns_burst_rate", "rogue_dns_burst_max_pending", "rogue_dns_burst_domains"]
    lib = (CLIENTS / "lib/common.sh").read_text()
    ps = (CLIENTS / "windows/common.ps1").read_text()
    for key in keys:
        assert re.search(rf"\b{key}\b", lib) and f"'{key}'" in ps
    conf = (ROOT / "configs/simulation.conf").read_text()
    assert len(re.findall(r"^rogue_dns_burst=off$", conf, re.M)) == 10
    assert "rogue_dns_burst_server=\n" in conf


def test_orchestrators_and_dashboards_dispatch_it():
    """Both orchestrators dispatch the new script behind its flag; dashboards list it."""
    assert 'run_simulation "rogue_dns_burst.sh"' in (CLIENTS / "linux/simulation.sh").read_text()
    assert "rogue_dns_burst.ps1" in (CLIENTS / "windows/simulation.ps1").read_text()
    assert "rogue_dns_burst" in (CLIENTS / "linux/dashboard.sh").read_text()
    assert "rogue_dns_burst" in (CLIENTS / "windows/dashboard.ps1").read_text()


def test_rogue_dns_burst_is_not_in_default_quarantine_exclusions():
    """Clients keep an IP, so a client with none is a bad dongle: not excluded."""
    from sim_quota_engine import QT_EXCLUDE_NONISOLATING_SIMS, _default_qt_exclude_sims
    assert "rogue_dns_burst" in QT_EXCLUDE_NONISOLATING_SIMS
    assert "rogue_dns_burst" not in _default_qt_exclude_sims()
