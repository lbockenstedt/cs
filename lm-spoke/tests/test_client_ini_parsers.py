"""The client ini parsers read simulation.conf the way the spoke writes it.

The spoke renders simulation.conf with Python's default configparser
(sim_config.py), which writes values unquoted and reads ``;`` and ``#`` inside a
value as part of it. So a passphrase such as ``ab #12;x`` or a base64 value with
``=`` must reach the client whole: configparser's own read-back is the oracle.
"""
import configparser
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CLIENTS = ROOT / "clients"
BASH_COPIES = ["lib", "linux", "t3"]
# Credentials as an operator might type them; every one broke an older parser.
SECRETS = {
    "pw_hash": "ab#12;x",
    "pw_lead": "#ab",
    "pw_b64": "YWI=c==",
    "pw_space_hash": "ab #12",
    "pw_space_semi": "pass ;word",
    "pw_tab": "v1\t# x",
}


def _write_like_the_spoke(path: Path) -> dict[str, str]:
    """Write SECRETS with configparser, as the spoke does; return its read-back."""
    writer = configparser.ConfigParser()
    writer.optionxform = str
    writer["kbell"] = SECRETS
    with path.open("w") as fh:
        writer.write(fh)
    reader = configparser.ConfigParser()
    reader.optionxform = str
    reader.read(path)
    return dict(reader["kbell"])


@pytest.mark.parametrize("copy", BASH_COPIES)
def test_bash_parser_regression_suite(copy):
    """Run the shell regression suite on each deployed copy."""
    parser = CLIENTS / copy / "ini-parser.sh"
    result = subprocess.run(["bash", str(CLIENTS / "tests/ini_parser_test.sh"), str(parser)],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr


def test_bash_parser_copies_are_identical():
    """lib is canonical; linux and t3 are cp copies of it."""
    canonical = (CLIENTS / "lib/ini-parser.sh").read_bytes()
    for copy in BASH_COPIES[1:]:
        assert (CLIENTS / copy / "ini-parser.sh").read_bytes() == canonical, copy


@pytest.mark.parametrize("copy", BASH_COPIES)
def test_bash_parser_reads_what_configparser_wrote(tmp_path, copy):
    """Each bash copy returns exactly the value configparser reads back."""
    conf = tmp_path / "simulation.conf"
    want = _write_like_the_spoke(conf)
    assert want == SECRETS
    keys = " ".join(SECRETS)
    script = (f'source "{CLIENTS / copy / "ini-parser.sh"}"\nprocess_ini_file "{conf}"\n'
              f'for k in {keys}; do printf "%s\\0" "$(get_value kbell "$k")"; done\n')
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert dict(zip(SECRETS, result.stdout.split("\0"))) == want


def test_windows_parser_reads_what_configparser_wrote(tmp_path):
    """The PowerShell twin returns exactly the value configparser reads back."""
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if not powershell:
        pytest.skip("PowerShell is not installed; run this on Windows or Linux CI")
    conf = tmp_path / "simulation.conf"
    want = _write_like_the_spoke(conf)
    script = tmp_path / "run.ps1"
    script.write_text(
        f". '{(CLIENTS / 'windows/ini-parser.ps1').as_posix()}'\n"
        f"$ini = Parse-IniFile '{conf.as_posix()}'\n"
        "$ini['kbell'] | ConvertTo-Json -Compress\n"
    )
    result = subprocess.run([powershell, "-NoProfile", "-NonInteractive", "-File", str(script)],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout) == want
