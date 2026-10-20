"""The linux client watchdog (sys_mon.sh --check-once, 1-min timer) must read
the same PID file agent.sh writes. A mismatch made every check report
pidfile_missing and restart a healthy agent, dropping its /ws/client socket
every minute."""

import re
from pathlib import Path

LINUX = Path(__file__).resolve().parents[2] / "clients" / "linux"


def _pid_file(script: str) -> str:
    text = (LINUX / script).read_text()
    m = re.search(r'^PID_FILE="(?:\$\{PID_FILE:-)?([^"}]+)\}?"', text, re.M)
    assert m, f"no PID_FILE in {script}"
    return m.group(1)


def test_watchdog_reads_agent_pid_file():
    assert _pid_file("sys_mon.sh") == _pid_file("agent.sh")
