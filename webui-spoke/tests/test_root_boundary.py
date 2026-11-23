"""The legacy dashboard's service user must have no unattended way to make
root run code it controls.

Three paths did that: a sudo grant to run ``<repo cache>/install-lxc.sh`` (a
file in a directory the service user owns), the root watchdog re-running the
installer (which then ran git, pip and the watchdog copy from service-owned
directories), and a branch name with ``..`` that pointed the root bootstrap at
another repo's installer.
"""
import asyncio
import logging
import os
import subprocess
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_INSTALLER = _ROOT.parent / "installers" / "install-lxc.sh"
_WATCHDOG = _ROOT / "watchdog.sh"


# ── installer ────────────────────────────────────────────────────────────────

def _valid_branch(name: str) -> bool:
    """Run the installer's own valid_branch function on ``name``."""
    text = _INSTALLER.read_text()
    start = text.index("valid_branch() {")
    func = text[start:text.index("\n}\n", start) + 3]
    return subprocess.run(["bash", "-c", func + 'valid_branch "$1"', "_", name]).returncode == 0


@pytest.mark.parametrize("name", ["main", "dev", "qa", "feature/x-1.2", "copilot/fix_a"])
def test_installer_accepts_real_branch_names(name):
    assert _valid_branch(name)


@pytest.mark.parametrize("name", [
    "../../attacker/repo/main", "main/../../x", "/main", "-main", "a//b", "a/",
    "a b", "", "main\n", "$(id)",
])
def test_installer_rejects_branch_names_that_leave_the_repo(name):
    assert not _valid_branch(name)


def test_installer_checks_the_branch_it_reads_from_env():
    """The bootstrap reads REPO_BRANCH from the service-owned .env; it must
    validate that value before putting it in the URL it runs as root."""
    text = _INSTALLER.read_text()
    read = text.index('REPO_BRANCH="${_env_br}"')
    assert text.index("valid_branch", read) < text.index("_bs_url=", read)


def _bootstrap_branch(tmp_path, cli_branch: str, env_branch: str) -> subprocess.CompletedProcess:
    """Run the installer's .env branch block with a given --branch and .env value."""
    text = _INSTALLER.read_text()
    start = text.index("valid_branch() {")
    func = text[start:text.index("\n}\n", start) + 3]
    block_start = text.index('  if [[ -z "$CLI_BRANCH"')
    block = text[block_start:text.index("\n  fi\n", block_start) + 6]
    (tmp_path / ".env").write_text(f"REPO_BRANCH={env_branch}\n")
    script = func + 'CLI_BRANCH="$1"; REPO_BRANCH="${1:-main}"; INSTALL_DIR="$2"\n' + block + 'echo "$REPO_BRANCH"\n'
    return subprocess.run(["bash", "-c", script, "_", cli_branch, str(tmp_path)],
                          capture_output=True, text=True)


def test_explicit_branch_recovers_from_a_bad_env_branch(tmp_path):
    """The error tells the operator to pass --branch; that must then work."""
    res = _bootstrap_branch(tmp_path, "main", "../../evil")
    assert res.returncode == 0 and res.stdout.strip() == "main"


def test_bad_env_branch_is_refused_without_an_explicit_branch(tmp_path):
    res = _bootstrap_branch(tmp_path, "", "../../evil")
    assert res.returncode == 1 and "pass --branch" in res.stderr


def test_good_env_branch_is_inherited_without_an_explicit_branch(tmp_path):
    res = _bootstrap_branch(tmp_path, "", "dev")
    assert res.returncode == 0 and res.stdout.strip() == "dev"


def test_installer_grants_the_service_user_no_sudo():
    """No sudoers line is written anywhere in the installer."""
    code = [ln for ln in _INSTALLER.read_text().splitlines() if not ln.lstrip().startswith("#")]
    assert not any("NOPASSWD" in ln for ln in code)


def _cleanup_block() -> str:
    """The installer's early root-only block that removes the old root paths."""
    text = _INSTALLER.read_text()
    start = text.index('SUDOERS_FILE="/etc/sudoers.d/')
    return text[start:text.index("\nfi\n", start) + 4]


def test_old_root_paths_are_removed_before_anything_can_exit():
    """Bad arguments, a failed hub tenant lookup, a hostile .env or a failed
    bootstrap all make the installer exit; the old sudo grant and the
    installer-running watchdog must already be gone by then."""
    text = _INSTALLER.read_text()
    block = text.index(_cleanup_block())
    code = "\n".join(ln for ln in text[:block].splitlines() if not ln.lstrip().startswith("#"))
    assert "exit" not in code and "$1" not in code
    assert block < text.index("while [[ $# -gt 0 ]]")


def test_old_root_paths_are_removed_even_when_the_arguments_are_bad(tmp_path):
    """Run the real installer head as root would, with an unknown option."""
    text = _INSTALLER.read_text()
    sudoers, watchdog = tmp_path / "sudoers", tmp_path / "webui-watchdog.sh"
    sudoers.write_text("grant\n")
    watchdog.write_text("#!/usr/bin/env bash\nrerun_installer() { :; }\n")
    stubs = tmp_path / "bin"
    stubs.mkdir()
    (stubs / "systemctl").write_text("#!/bin/sh\nexit 0\n")
    (stubs / "systemctl").chmod(0o755)
    head = (text[:text.index("\n###############################################################################\n# Config")]
            .replace('"$EUID" -eq 0', "1 -eq 1").replace('"$EUID" -ne 0', "1 -ne 1")
            .replace("/etc/sudoers.d/client-sim-dashboard", str(sudoers))
            .replace("/usr/local/bin/webui-watchdog.sh", str(watchdog))
            .replace('export PATH="/usr/sbin:/sbin:/usr/bin:/bin:$PATH"', f'export PATH="{stubs}:$PATH"'))
    res = subprocess.run(["bash", "-c", head, "install-lxc.sh", "--no-such-option"],
                         capture_output=True, text=True)
    assert res.returncode == 1 and "Unknown option" in res.stderr
    assert not sudoers.exists() and not watchdog.exists()


def _run_cleanup(tmp_path, watchdog_text: str) -> tuple[Path, Path, str]:
    """Run the cleanup block against temp copies; return (sudoers, watchdog, systemctl calls)."""
    sudoers, watchdog, calls = tmp_path / "sudoers", tmp_path / "webui-watchdog.sh", tmp_path / "calls"
    sudoers.write_text("dashboard ALL=(root) NOPASSWD: /bin/bash /opt/client-sim-repo/install-lxc.sh *\n")
    watchdog.write_text(watchdog_text)
    stubs = tmp_path / "bin"
    stubs.mkdir()
    (stubs / "systemctl").write_text(f'#!/bin/sh\necho "systemctl $*" >> "{calls}"\n')
    (stubs / "systemctl").chmod(0o755)
    block = (_cleanup_block().replace('"$EUID" -eq 0', "1 -eq 1")
             .replace("/etc/sudoers.d/client-sim-dashboard", str(sudoers))
             .replace("/usr/local/bin/webui-watchdog.sh", str(watchdog)))
    subprocess.run(["bash", "-c", "set -euo pipefail\n" + block],
                   env={**os.environ, "PATH": f"{stubs}:{os.environ['PATH']}"}, check=True)
    return sudoers, watchdog, (calls.read_text() if calls.exists() else "")


def test_old_grant_and_installer_running_watchdog_are_removed(tmp_path):
    old = "#!/usr/bin/env bash\nrerun_installer() {\n  bash /opt/client-sim-repo/webui-spoke/install-lxc.sh\n}\n"
    sudoers, watchdog, calls = _run_cleanup(tmp_path, old)
    assert not sudoers.exists() and not watchdog.exists()
    assert "disable --now webui-watchdog.timer" in calls


def test_restart_only_watchdog_is_kept(tmp_path):
    """The shipped watchdog carries the marker, so a re-run leaves it running."""
    _, watchdog, calls = _run_cleanup(tmp_path, _WATCHDOG.read_text())
    assert watchdog.exists() and calls == ""


# ── watchdog ─────────────────────────────────────────────────────────────────

def _run_watchdog(tmp_path, *, healthy: bool, failures: int) -> tuple[str, str]:
    """Run watchdog.sh with stub systemctl/curl; return (stub call log, watchdog log)."""
    stubs = tmp_path / "bin"
    stubs.mkdir()
    calls = tmp_path / "calls"
    rc = 0 if healthy else 1
    for tool in ("systemctl", "curl"):
        (stubs / tool).write_text(
            f'#!/bin/sh\necho "{tool} $*" >> "{calls}"\n'
            f'case "$*" in *githubusercontent*) exit 0 ;; restart*) exit 0 ;; esac\nexit {rc}\n')
        (stubs / tool).chmod(0o755)
    env_file = tmp_path / ".env"
    # The service user controls .env: an old version and a hostile branch.
    env_file.write_text("PORT=8000\nREPO_BRANCH=../../attacker/repo/main\nINSTALLER_VERSION=0\n")
    state = tmp_path / "state"
    state.write_text(f"{failures}\n")
    log = tmp_path / "watchdog.log"
    env = {**os.environ, "PATH": f"{stubs}:{os.environ['PATH']}", "STATE_DIR": str(tmp_path),
           "STATE_FILE": str(state), "LOG_FILE": str(log), "ENV_FILE": str(env_file)}
    subprocess.run(["bash", str(_WATCHDOG)], env=env, check=False, timeout=30)
    return (calls.read_text() if calls.exists() else ""), log.read_text()


def test_watchdog_never_fetches_an_installer_when_healthy(tmp_path):
    """A version mismatch in .env used to make root download and run the installer."""
    calls, _ = _run_watchdog(tmp_path, healthy=True, failures=0)
    assert "githubusercontent" not in calls


def test_watchdog_restarts_instead_of_reinstalling_after_five_failures(tmp_path):
    calls, log = _run_watchdog(tmp_path, healthy=False, failures=4)
    assert "githubusercontent" not in calls
    assert "systemctl restart client-sim-dashboard" in calls
    assert "installer_rerun_needed" in log


# ── self-update in the service ───────────────────────────────────────────────

@pytest.fixture
def proxmox_agent(monkeypatch, tmp_path):
    """Import services.proxmox_agent against a stub ``server`` module (importing
    the real one starts the app and writes settings.json)."""
    real = {"os": os, "asyncio": asyncio, "logger": logging.getLogger("t"), "Any": object}
    srv = types.ModuleType("server")
    srv.__getattr__ = lambda n: real[n] if n in real else MagicMock(name=n)
    monkeypatch.setitem(sys.modules, "server", srv)
    monkeypatch.syspath_prepend(str(_ROOT))
    monkeypatch.delitem(sys.modules, "services.proxmox_agent", raising=False)
    from services import proxmox_agent as pa
    installer = tmp_path / "install-lxc.sh"
    installer.write_text("VERSION=9\n")
    monkeypatch.setattr(pa, "_INSTALLER_PATH", installer)
    monkeypatch.setattr(pa, "update_state", {"update_in_progress": False, "update_error": None})
    monkeypatch.setattr(pa, "_broadcast_update_state", AsyncMock())
    monkeypatch.setattr(pa, "state_lock", asyncio.Lock())
    monkeypatch.setattr(pa, "commands", [])
    yield pa
    sys.modules.pop("services.proxmox_agent", None)


def test_self_update_without_root_runs_nothing(proxmox_agent, monkeypatch):
    spawn = AsyncMock()
    monkeypatch.setattr(asyncio, "create_subprocess_shell", spawn)
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    asyncio.run(proxmox_agent._run_self_update())
    spawn.assert_not_called()
    assert "needs root" in proxmox_agent.update_state["update_error"]
    assert proxmox_agent.update_state["update_in_progress"] is False


def test_hub_self_update_ack_is_an_error_without_root(proxmox_agent, monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    assert proxmox_agent._self_update_ack_result()["error"] == "needs_root"
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    assert "error" not in proxmox_agent._self_update_ack_result()


def test_hub_self_update_ack_reports_each_case_that_runs_nothing(proxmox_agent, monkeypatch):
    """As root, "triggered" only when _run_self_update will start the installer."""
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    proxmox_agent.update_state["update_in_progress"] = True
    assert proxmox_agent._self_update_ack_result()["error"] == "in_progress"
    proxmox_agent.update_state["update_in_progress"] = False
    proxmox_agent._INSTALLER_PATH.unlink()
    assert proxmox_agent._self_update_ack_result()["error"] == "installer_missing"


@pytest.mark.parametrize("repo_version", ["2", None])
def test_ui_self_update_refuses_instead_of_reporting_started(proxmox_agent, monkeypatch, repo_version):
    """The Update button got "Update … started" (or "Already up to date" when
    the repo version can't be read) for an update that can never run."""
    from fastapi import HTTPException
    srv = sys.modules["server"]
    run, sync = AsyncMock(), AsyncMock()
    for name, value in {
        "HTTPException": HTTPException, "update_state": {"update_in_progress": False, "current_version": "1"},
        "_sync_repo_now": sync, "_get_repo_version": lambda: repo_version,
        "_broadcast_update_state": AsyncMock(), "_run_self_update": run,
        "_self_update_ack_result": proxmox_agent._self_update_ack_result,
    }.items():
        monkeypatch.setattr(srv, name, value, raising=False)
    # Drop the package too: it caches the submodule as an attribute, so a
    # fresh import is the only way to bind this case's stubs.
    monkeypatch.delitem(sys.modules, "routers.updates", raising=False)
    monkeypatch.delitem(sys.modules, "routers", raising=False)
    from routers import updates
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(updates.api_self_update())
    assert exc.value.status_code == 403 and "needs root" in exc.value.detail
    run.assert_not_called()
    sync.assert_not_called()


@pytest.mark.parametrize("result", [
    {"task_type": "self_update", "error": "needs_root", "message": "Self-update needs root"},
    {"error": "reseed_in_progress", "message": "Reseed in progress"},
])
def test_lm_relay_reports_a_refused_fleet_command_as_an_error(monkeypatch, result):
    """A refusal acked as {error, message} must not reach LM as SUCCESS."""
    srv = types.ModuleType("server")

    async def _batch(cmds, ack_fn):
        """Ack the one command with the refusal under test."""
        await ack_fn(cmds[0]["id"], "executed", result)

    srv._apply_relay_command_batch = _batch
    monkeypatch.setitem(sys.modules, "server", srv)
    import lm_relay
    bridge = object.__new__(lm_relay.CSBridge)
    reply = asyncio.run(bridge._handle_queue_command({"type": "self_update"}))
    assert reply["status"] == "ERROR"
    assert reply["message"] == result["message"] and reply["result"] == result
