"""Client scripts that run as root must not act on values another user or the
served config controls.

- startup.sh spliced ``syslog_server`` (served simulation.conf) into
  ``sudo sed``; a ``|`` ended the s command and GNU sed's ``e`` ran a shell.
- agent.sh (root, systemd) sent USR1 to whatever PID the desktop user wrote
  into simulation.pid; USR1 kills a process that doesn't trap it.
- T3 update_script.sh downloaded as root to fixed /tmp names, ran
  ``find /tmp -size 0 | xargs rm`` (whitespace-split paths, so a crafted name
  deleted any file), and left the root-run scripts mode 0777.
"""

import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
STARTUP = (REPO / "clients" / "linux" / "startup.sh").read_text()
AGENT = (REPO / "clients" / "linux" / "agent.sh").read_text()
T3_UPDATE = (REPO / "clients" / "t3" / "update_script.sh").read_text()
T3_WIRELESS = (REPO / "clients" / "t3" / "wireless.sh").read_text()


def _fn(src: str, name: str) -> str:
    """Return the source of shell function ``name`` from ``src``."""
    m = re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", src, re.M | re.S)
    assert m, name
    return m.group(0)


def _bash(script: str, cwd: Path) -> subprocess.CompletedProcess:
    """Run ``script`` in bash with ``sudo`` stubbed to run its arguments."""
    return subprocess.run(["bash", "-c", 'sudo() { "$@"; }\n' + script], cwd=cwd,
                          capture_output=True, text=True, timeout=30)


def _gnu_sed() -> bool:
    """True when ``sed`` is GNU sed (the clients' sed; it has the e command)."""
    return subprocess.run(["sed", "--version"], capture_output=True).returncode == 0


@pytest.mark.parametrize("value,ok", [
    ("169.253.1.5", True), ("syslog.lab.local", True), ("10.0.0.1:514", True),
    ("@10.0.0.1:514", True), ("[fd00::1]:514", True),
    ("x|;e touch PWN;#", False), ("a;b", False), ("a b", False), ("", False),
    ("-x", False), ("a/b", False), ("10.0.0.1:123456", False),
])
def test_syslog_target_allowlist(tmp_path, value, ok):
    """Only a plain host/IP with an optional :port and leading @ passes."""
    res = _bash(_fn(STARTUP, "_syslog_target_ok") + f"_syslog_target_ok '{value}'", tmp_path)
    assert (res.returncode == 0) is ok


@pytest.mark.skipif(not _gnu_sed(), reason="needs GNU sed, as on the clients")
@pytest.mark.parametrize("server,applied", [
    ("x|;e touch PWN;#", False), ("10.9.8.7", True), ("[fd00::1]:514", True),
])
def test_syslog_block_runs_no_shell(tmp_path, server, applied):
    """The real syslog block never runs a crafted syslog_server as shell, and
    still writes a plain one (with its comment line) into rsyslog.conf."""
    conf = tmp_path / "rsyslog.conf"
    conf.write_text('module(load="imuxsock")\n*.*@1.1.1.1\n')
    block = STARTUP[STARTUP.index("#Configuring Syslog Server"):STARTUP.index("#Scheduling Reboot")]
    block = block.replace("/etc/rsyslog.conf", str(conf)).replace("/usr/local/scripts/sim.log", "/dev/null")
    _bash(f"syslog=on\nsyslog_server='{server}'\n" + block, tmp_path)
    assert not (tmp_path / "PWN").exists()
    text = conf.read_text()
    assert (f"#Syslog Server\n*.*@{server}\n" in text) is applied


def _sim_pid(tmp_path: Path, pid: str, cmdline: bytes,
             exe: str = "/usr/bin/bash") -> subprocess.CompletedProcess:
    """Run agent.sh's sim_loop_pid against a fake pid file and /proc entry.

    ``pid`` may be ``$$`` (the test shell, which is alive); ``exe`` is what
    the fake /proc/<pid>/exe link points at."""
    proc = tmp_path / "proc"
    src = (_fn(AGENT, "pid_is_active") + _fn(AGENT, "sim_loop_pid")).replace(
        "/usr/local/scripts/simulation.pid", str(tmp_path / "simulation.pid")
    ).replace('"/proc/$pid/', f'"{proc}/$pid/')
    (tmp_path / "cmdline").write_bytes(cmdline)
    script = (f'echo "{pid}" > {tmp_path}/simulation.pid\n'
              f'mkdir -p "{proc}/$$" && cp {tmp_path}/cmdline "{proc}/$$/cmdline"\n'
              f'ln -s "{exe}" "{proc}/$$/exe"\n'
              + src + "sim_loop_pid")
    return _bash(script, tmp_path)


@pytest.mark.parametrize("cmdline,exe", [
    (b"/bin/bash\0/usr/local/scripts/startup.sh\0", "/usr/bin/bash"),
    (b"bash\0/usr/local/scripts/startup.sh\0", "/bin/bash"),
    (b"bash\0/usr/local/scripts/simulation.sh\0", "/usr/bin/bash (deleted)"),
])
def test_sim_loop_pid_accepts_the_sim_loop(tmp_path, cmdline, exe):
    """The startup.sh loop (desktop #! launch or sudo bash), and the loop
    after its re-exec, are signalled, also after a bash upgrade."""
    res = _sim_pid(tmp_path, "$$", cmdline, exe)
    assert res.returncode == 0 and res.stdout.strip().isdigit()


@pytest.mark.parametrize("pid,cmdline,exe", [
    ("$$", b"/usr/sbin/sshd\0-D\0", "/usr/sbin/sshd"),
    ("$$", b"bash\0/usr/local/scripts/startup.sh.evil\0", "/usr/bin/bash"),
    ("$$", b"bash\0-c\0/usr/local/scripts/startup.sh ; reboot\0", "/usr/bin/bash"),
    # Decoys carrying the path as some other argument.
    ("$$", b"sleep\0999\0/usr/local/scripts/startup.sh\0", "/usr/bin/sleep"),
    ("$$", b"bash\0/tmp/x.sh\0/usr/local/scripts/startup.sh\0", "/usr/bin/bash"),
    ("$$", b"bash\0/usr/local/scripts/startup.sh\0extra\0", "/usr/bin/bash"),
    # A process that rewrote its argv to look like the loop, or a copied bash.
    ("$$", b"bash\0/usr/local/scripts/startup.sh\0", "/usr/bin/perl"),
    ("$$", b"bash\0/usr/local/scripts/startup.sh\0", "/home/simuser/bash"),
    ("$$", b"/home/simuser/bash\0/usr/local/scripts/startup.sh\0", "/usr/bin/bash"),
    ("1 2", b"bash\0/usr/local/scripts/startup.sh\0", "/usr/bin/bash"),
    ("", b"bash\0/usr/local/scripts/startup.sh\0", "/usr/bin/bash"),
])
def test_sim_loop_pid_refuses_other_processes(tmp_path, pid, cmdline, exe):
    """A PID file naming any other process (or garbage) yields no PID."""
    res = _sim_pid(tmp_path, pid, cmdline, exe)
    assert res.returncode != 0 and res.stdout == ""


def test_agent_signals_only_a_verified_sim_pid():
    """Every USR1 and the reboot guard go through sim_loop_pid."""
    body = AGENT[AGENT.index("run_command() {"):]
    assert "simulation.pid" not in body
    assert body.count("kill -USR1") == 2
    assert body.count("sim_pid=$(sim_loop_pid)") == 2
    assert "if ! sim_loop_pid >/dev/null; then" in body


def test_t3_update_installs_only_its_own_downloads(tmp_path):
    """T3's updater installs non-empty downloads from a private temp folder at
    mode 0755, keeps a script whose download was empty or failed, ignores a
    planted /tmp file, deletes nothing else in /tmp and leaves no temp folder."""
    dest = tmp_path / "usr_scripts"
    dest.mkdir()
    for f in ("wireless.sh", "update_script.sh", "agent.sh"):
        (dest / f).write_text("old\n")
    planted = tmp_path / "tmp"
    planted.mkdir()
    (planted / "agent.sh").write_text("planted\n")
    (planted / "x ").mkdir()
    (planted / "x " / "victim").write_text("")
    tmpdir = tmp_path / "mktemp"
    tmpdir.mkdir()
    block = T3_UPDATE[T3_UPDATE.index('echo "Updating Simulation Script"'):
                      T3_UPDATE.index("sudo chmod -x /etc/udev")]
    block = block.replace("/usr/scripts/", f"{dest}/").replace("/tmp/", f"{planted}/")
    stub = ('wget() { case "$1" in *agent.sh) : > "$3";; *update_script.sh) echo half > "$3"; return 4;;'
            ' *) echo new > "$3";; esac; }\n'
            'chmod() { command chmod "$@"; }\ntee() { cat >/dev/null; }\n')
    res = _bash(f"cd {tmp_path}\nexport TMPDIR={tmpdir}\n" + stub + block, tmp_path)
    assert res.returncode == 0, res.stderr
    assert (dest / "wireless.sh").read_text() == "new\n"
    assert (dest / "update_script.sh").read_text() == "old\n"
    assert (dest / "agent.sh").read_text() == "old\n"
    assert (planted / "x " / "victim").exists()
    assert (planted / "agent.sh").read_text() == "planted\n"
    assert list(tmpdir.iterdir()) == []
    for f in ("wireless.sh", "update_script.sh", "agent.sh"):
        assert (dest / f).stat().st_mode & 0o777 == 0o755


def test_t3_scripts_are_never_world_writable():
    """Neither T3 script makes a root-run script world-writable, and neither
    deletes /tmp files through a whitespace-splitting xargs."""
    for src in (T3_UPDATE, T3_WIRELESS):
        assert "chmod 777" not in src
        assert "xargs" not in src
