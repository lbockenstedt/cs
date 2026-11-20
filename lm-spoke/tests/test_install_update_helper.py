"""lm-component-update-restart (embedded in install_cs.sh) runs as root via
`sudo -n` from the non-root spoke user, with every argument chosen by that
user. It must run git and the recovery CLI as the caller, never as root, and
restart only an lm-* unit (only lm-cs for the non-root caller). These tests extract the helper and run it against
fake systemd-run / systemctl / runuser / git / python3 / getent on PATH."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

INSTALLER = Path(__file__).resolve().parents[1] / "install_cs.sh"

pytestmark = pytest.mark.skipif(not shutil.which("jq"), reason="helper needs jq")

# Each fake logs "<name> as=<user> <args>"; runuser sets the user it runs as.
_FAKES = {
    "systemd-run": 'echo "systemd-run $*" >> "$LOG"',
    "systemctl": (
        'echo "systemctl as=${FAKE_AS:-root} $*" >> "$LOG"\n'
        'case "$1" in\n'
        '  restart) echo active > "$STATE_FILE";;\n'
        '  show) case "$*" in *ActiveState*) cat "$STATE_FILE";; *) echo 0;; esac;;\n'
        'esac'),
    "runuser": (
        '[ "$1" = -u ] || exit 9\n'
        'u="$2"; shift 3\n'
        'FAKE_AS="$u" exec "$@"'),
    "git": 'echo "git as=${FAKE_AS:-root} $*" >> "$LOG"',
    "python3": 'echo "python3 as=${FAKE_AS:-root} $*" >> "$LOG"',
    "getent": 'case "$2" in svc_lm) echo "svc_lm:x:999:999::/home/svc_lm:/bin/bash";;'
              ' root) echo "root:x:0:0::/root:/bin/bash";; *) exit 2;; esac',
    "sleep": ":",
}


def _helper_text() -> str:
    """Return the helper script embedded in install_cs.sh's here-doc."""
    text = INSTALLER.read_text()
    start = text.index("cat > /usr/local/bin/lm-component-update-restart <<'HELPER'\n")
    body = text[text.index("\n", start) + 1:]
    return body[:body.index("\nHELPER\n")] + "\n"


def _run(tmp_path, args, env_extra):
    """Run the helper with fakes on PATH; return (rc, log lines)."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    for name, body in _FAKES.items():
        f = bindir / name
        f.write_text("#!/bin/bash\n" + body + "\n")
        f.chmod(0o755)
    helper = tmp_path / "helper.sh"
    helper.write_text(_helper_text())
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    (state / "pending_update.json").write_text(json.dumps(
        {"from_commit": "aaa", "to_commit": "bbb", "core_from_commit": "ccc",
         "core_to_commit": "ddd"}))
    log = tmp_path / "log"
    log.write_text("")
    state_file = tmp_path / "unit_state"
    state_file.write_text("failed\n")
    env = {"PATH": f"{bindir}:/usr/bin:/bin", "LOG": str(log),
           "STATE_FILE": str(state_file), **env_extra}
    argv = [a.replace("@STATE@", str(state)) for a in args]
    out = subprocess.run([shutil.which("bash"), str(helper), *argv],
                         capture_output=True, text=True, env=env, timeout=30)
    return out.returncode, log.read_text().splitlines()


_ROLLBACK_ARGS = ["--unit", "lm-cs", "--state-dir", "@STATE@", "--repo-root",
                  "/opt/lm/cs", "--core-repo-root", "/opt/lm", "--deadline", "0",
                  "--recovery-py", "/tmp/evil.py"]


def test_sudo_invocation_always_reexecs_with_caller(tmp_path):
    """A sudo caller cannot skip the re-exec by presetting the guard; the
    transient unit is told who the caller is."""
    rc, log = _run(tmp_path, _ROLLBACK_ARGS,
                   {"SUDO_USER": "svc_lm", "LM_COMP_UPDATE_GUARD": "1",
                    "LM_COMP_UPDATE_CALLER": "root"})
    assert rc == 0
    assert len(log) == 1 and log[0].startswith("systemd-run ")
    assert "--setenv=LM_COMP_UPDATE_CALLER=svc_lm" in log[0]


def test_rollback_runs_git_and_recovery_as_caller(tmp_path):
    """In the transient unit, git and the recovery CLI (even a caller-chosen
    --recovery-py) run as the service user; only systemctl runs as root."""
    rc, log = _run(tmp_path, _ROLLBACK_ARGS,
                   {"LM_COMP_UPDATE_GUARD": "1", "LM_COMP_UPDATE_CALLER": "svc_lm"})
    assert rc == 0, log
    git = [l for l in log if l.startswith("git ")]
    py = [l for l in log if l.startswith("python3 ")]
    assert any("reset --hard aaa" in l for l in git)
    assert any("/opt/lm reset --hard ccc" in l for l in git)
    assert py and all("/tmp/evil.py" in l for l in py)
    assert all(" as=svc_lm " in l for l in git + py), git + py
    assert any(l.startswith("systemctl as=root restart lm-cs") for l in log)


def test_root_caller_runs_directly(tmp_path):
    """The root agent (no sudo) keeps running everything as root."""
    rc, log = _run(tmp_path, _ROLLBACK_ARGS,
                   {"LM_COMP_UPDATE_GUARD": "1", "LM_COMP_UPDATE_CALLER": "root"})
    assert rc == 0
    assert all(" as=root " in l for l in log if l.startswith(("git ", "python3 ")))


@pytest.mark.parametrize("bad", [
    ["--unit", "sshd"],
    ["--unit", "lm-cs;reboot"],
    ["--unit", "lm-hub"],
    ["--unit", "lm-cs", "--deadline", "x"],
    ["--unit", "lm-cs", "--deadline", ""],
    ["--unit", "lm-cs", "--deadline", "3601"],
    ["--unit", "lm-cs", "--deadline", "99999999999999999999"],
])
def test_rejects_non_lm_unit_and_bad_deadline(tmp_path, bad):
    """Root restarts --unit, so only an lm-* unit and a 0-3600s deadline pass,
    and the service user may name only its own lm-cs unit."""
    rc, log = _run(tmp_path, ["--state-dir", "@STATE@", "--repo-root", "/x", *bad],
                   {"LM_COMP_UPDATE_GUARD": "1", "LM_COMP_UPDATE_CALLER": "svc_lm"})
    assert rc == 2 and log == []


def test_root_caller_may_restart_other_lm_unit(tmp_path):
    """The lm-cs pin applies to the sudo caller only; root keeps any lm-* unit."""
    args = ["lm-hub" if a == "lm-cs" else a for a in _ROLLBACK_ARGS]
    rc, log = _run(tmp_path, args,
                   {"LM_COMP_UPDATE_GUARD": "1", "LM_COMP_UPDATE_CALLER": "root"})
    assert rc == 0 and any(l.startswith("systemctl as=root restart lm-hub") for l in log)


def test_unknown_caller_refused(tmp_path):
    """A caller with no passwd entry stops before anything runs."""
    rc, log = _run(tmp_path, _ROLLBACK_ARGS,
                   {"LM_COMP_UPDATE_GUARD": "1", "LM_COMP_UPDATE_CALLER": "nobody9"})
    assert rc == 2 and log == []


def _migrate_fn() -> str:
    """Return the installer's migrate_recovery_owner function."""
    text = INSTALLER.read_text()
    start = text.index("migrate_recovery_owner() {")
    return text[start:text.index("\n}\n", start) + 3]


def test_installer_migrates_recovery_files_as_spoke_user(tmp_path):
    """The old helper ran update_recovery.py as root, leaving bad_commits.json
    root-owned in the spoke's state dir. The installer replaces such files
    with copies owned by the spoke user, and does it as that user (runuser),
    never with root's rights, so nothing the spoke user plants can redirect
    it. The fake runuser logs its user and runs as the test user; the spoke
    user is a name the test user is not, so every test file counts as foreign."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "runuser.log"
    (bindir / "runuser").write_text(
        '#!/bin/bash\n[ "$1" = -u ] || exit 9\necho "$2" >> "$LOG"\nshift 3\nexec "$@"\n')
    (bindir / "runuser").chmod(0o755)
    (bindir / "chown").write_text("#!/bin/sh\nexit 7\n")
    (bindir / "chown").chmod(0o755)
    base = tmp_path / "varliblm"
    spoke = base / "cs-1"
    (spoke / "snap").mkdir(parents=True)
    bad = spoke / "bad_commits.json"
    bad.write_text('{"commits": ["abc"]}')
    (spoke / "snap" / "deep").write_text("x")
    outside = tmp_path / "outside"
    outside.write_text("keep")
    (spoke / "sym").symlink_to(outside)
    (base / "top-file").write_text("t")
    (base / "dirlink").symlink_to(tmp_path / "elsewhere", target_is_directory=True)
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "elsewhere" / "victim").write_text("v")
    inodes = {p: p.stat().st_ino for p in
              (bad, spoke / "snap" / "deep", outside, base / "top-file",
               tmp_path / "elsewhere" / "victim")}

    subprocess.run(["bash", "-c", _migrate_fn() + f'migrate_recovery_owner "{base}" nobody\n'],
                   check=True, env={"PATH": f"{bindir}:/usr/bin:/bin", "LOG": str(log)})

    assert log.read_text().split() == ["nobody"]  # ran as the spoke user only
    assert bad.stat().st_ino != inodes[bad]  # replaced by a fresh copy
    assert bad.read_text() == '{"commits": ["abc"]}'
    # Not touched: nested files, symlink targets, top-level files, symlinked dirs.
    for p in (spoke / "snap" / "deep", outside, base / "top-file",
              tmp_path / "elsewhere" / "victim"):
        assert p.stat().st_ino == inodes[p], p
    assert (spoke / "sym").is_symlink()
    assert not list(spoke.glob("*.lm-own"))
