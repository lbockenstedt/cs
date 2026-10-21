"""install_cs.sh hub-TLS resolution: --tls-verify must work without a CA file
(public-CA hub → system trust store), a re-run must keep the existing .env
choice, and a fresh install against a DNS-named hub verifies by default."""

import subprocess
from pathlib import Path

import pytest

INSTALLER = Path(__file__).resolve().parents[1] / "install_cs.sh"
HUB = "wss://lm-hub.example.com:443"


def _resolve(tmp_path, *args, env_lines=None):
    text = INSTALLER.read_text()
    prologue = text.split('[ "$(id -u)" -eq 0 ]', 1)[0]
    prologue = prologue.replace('LM_DIR="/opt/lm"', f'LM_DIR="{tmp_path}"', 1)
    if env_lines is not None:
        (tmp_path / "cs").mkdir(exist_ok=True)
        (tmp_path / "cs" / ".env").write_text("\n".join(env_lines) + "\n")
    script = tmp_path / "prologue.sh"
    script.write_text(prologue + '\necho "V=$HUB_TLS_VERIFY_ENV CA=$HUB_TLS_CA_ENV"\n')
    out = subprocess.run(["bash", str(script), *args], capture_output=True, text=True,
                         env={"PATH": "/usr/bin:/bin"})
    return out.returncode, out.stdout.strip().splitlines()[-1] if out.stdout.strip() else "", out


def test_tls_verify_without_ca_uses_system_store(tmp_path):
    rc, last, _ = _resolve(tmp_path, "--hub", HUB, "--tls-verify")
    assert rc == 0 and last == "V=1 CA="


def test_fresh_install_dns_hub_verifies_by_default(tmp_path):
    rc, last, _ = _resolve(tmp_path, "--hub", HUB)
    assert rc == 0 and last == "V=1 CA="


@pytest.mark.parametrize("hub", ["172.16.1.31", "wss://172.16.1.31:443", "auto"])
def test_fresh_install_ip_or_auto_hub_defaults_off(tmp_path, hub):
    rc, last, _ = _resolve(tmp_path, "--hub", hub)
    assert rc == 0 and last == "V=0 CA="


def test_rerun_keeps_existing_env_choice(tmp_path):
    ca = tmp_path / "ca.pem"; ca.write_text("x")
    rc, last, _ = _resolve(tmp_path, "--hub", HUB,
                           env_lines=["LM_HUB_TLS_VERIFY=1", f"LM_HUB_CA_CERT={ca}"])
    assert rc == 0 and last == f"V=1 CA={ca}"
    rc, last, _ = _resolve(tmp_path, "--hub", HUB, env_lines=["LM_HUB_TLS_VERIFY=0"])
    assert rc == 0 and last == "V=0 CA="


def test_flag_overrides_env_off(tmp_path):
    rc, last, _ = _resolve(tmp_path, "--hub", HUB, "--tls-verify",
                           env_lines=["LM_HUB_TLS_VERIFY=0"])
    assert rc == 0 and last == "V=1 CA="


def test_no_tls_verify_opt_out(tmp_path):
    rc, last, _ = _resolve(tmp_path, "--hub", HUB, "--no-tls-verify")
    assert rc == 0 and last == "V=0 CA="


def test_missing_ca_file_fails(tmp_path):
    rc, _, out = _resolve(tmp_path, "--hub", HUB, "--tls-verify",
                          "--tls-ca-cert", str(tmp_path / "nope.pem"))
    assert rc != 0 and "does not exist" in out.stdout
