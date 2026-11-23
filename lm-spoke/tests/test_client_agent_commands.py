"""Client agents must parse hub commands exactly and never run their text.

``"${1:-{}}"`` in bash is ``${1:-{}`` followed by a literal ``}``, so every
command a script passed on gained a stray brace and failed to parse: T3
answered every command with an empty "Unsupported action". T3 also built its
ack with an unquoted heredoc, so a server-supplied id or action became Python
source.
"""

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
T3_AGENT = REPO / "clients" / "t3" / "agent.sh"


def test_no_shell_default_appends_a_brace():
    """No shell script uses the ``:-{}}`` default that appends a ``}``."""
    bad = []
    for path in REPO.rglob("*.sh"):
        if any(part in {".git", "node_modules"} for part in path.parts):
            continue
        for n, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
            if re.search(r":-\{\}\}", line):
                bad.append(f"{path.relative_to(REPO)}:{n}")
    assert not bad, bad


def _t3_ack(raw: str) -> dict:
    """Run the T3 command worker on ``raw`` and return its JSON ack."""
    out = subprocess.run(
        ["bash", str(T3_AGENT), "--handle-command", raw],
        env={**os.environ, "CLIENT_SIM_SERVER_URL": "http://hub.invalid"},
        capture_output=True, text=True, timeout=30, check=True,
    )
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_t3_parses_a_command():
    """A well-formed reboot command is understood (no stray brace)."""
    ack = _t3_ack(json.dumps({"id": "cmd-1", "action": "reboot"}))
    assert ack == {"id": "cmd-1", "status": "completed",
                   "message": "Rebooting now", "reboot": "true"}


@pytest.mark.parametrize("field", ["id", "action"])
def test_t3_ack_never_runs_command_text(tmp_path, field):
    """A crafted id or action comes back as data and runs no code."""
    marker = tmp_path / "pwned"
    evil = f'x", "p": __import__("pathlib").Path({str(marker)!r}).touch(), "q": "'
    cmd = {"id": "cmd-2", "action": "nope", field: evil}
    ack = _t3_ack(json.dumps(cmd))
    assert not marker.exists()
    assert ack["status"] == "failed"
    if field == "id":
        assert ack["id"] == evil
    else:
        assert ack["message"] == f"Unsupported action: {evil}"


@pytest.mark.parametrize("raw", ["not json", "[1, 2]", ""])
def test_t3_malformed_command_gets_a_failed_ack(raw):
    """A malformed or non-object command still gets a valid failed ack."""
    ack = _t3_ack(raw)
    assert ack["status"] == "failed" and ack["id"] == ""


def test_t3_tab_in_id_cannot_shift_the_action():
    """A tab inside the id can't smuggle a value into the action field."""
    ack = _t3_ack(json.dumps({"id": "a\treboot", "action": "nope"}))
    assert ack["reboot"] == "false" and ack["status"] == "failed"
