#!/usr/bin/env python3
"""
cloud_nac_onboard.py — Headless Cloud NAC (Cloud Authentication & Policy)
onboarding for Linux sim clients via the browser Onboard flow.

WHY THIS EXISTS
---------------
Cloud NAC issues 802.1X EAP-TLS client certs ONLY through the interactive
Onboard app / browser flow (Aruba's own CA — no BYO CA, no SCEP/EST, no
headless issuance API). Sim clients are headless, so we drive the same
browser flow with Playwright (keystrokes + DOM scrape, NOT an API) to:
  1. Open the tenant Onboard `provisioning_url`.
  2. Complete the Entra ID SSO login (username + password, NO MFA — the
     account must be MFA-exempt / conditional-access-bypassed for automation).
  3. Capture the issued cert bundle (PKCS#12) from the download the Onboard
     page serves, then split it into the client cert / private key / CA cert
     files an nmcli EAP-TLS profile consumes.

This is FRAGILE RPA: any Entra or Onboard UI change (field ids, button text,
MFA prompt, redirect chain) can break it. Selectors are configurable via
flags / env so a tenant-specific tweak does not require a code edit.

REQUIREMENTS
------------
  pip install playwright
  python -m playwright install chromium
  openssl (for p12 → PEM split)

USAGE
----
  python3 cloud_nac_onboard.py \
      --provisioning-url "$CLOUD_NAC_PROVISIONING_URL" \
      --user kbell@example.com --password 'Secret123!' \
      --p12-password 'OnboardP12Pass!' \
      --out-dir /usr/local/scripts/cloud-nac \
      --device-name "sim-$(hostname)"

  # then in simulation.conf:
  #   dot1x_eap=tls
  #   dot1x_client_cert=/usr/local/scripts/cloud-nac/client.crt
  #   dot1x_private_key=/usr/local/scripts/cloud-nac/client.key
  #   dot1x_ca_cert=/usr/local/scripts/cloud-nac/ca.crt
  # and connect_1x_tls() in simulation.sh builds the EAP-TLS nmcli profile.

EXIT CODES
----------
  0  success — cert files written to --out-dir
  2  SSO login failed (bad creds / MFA prompt / selector mismatch)
  3  Onboard post-SSO flow did not yield a downloadable cert
  4  p12 capture/extract failed
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
except ImportError:  # pragma: no cover
    sys.stderr.write(
        "Playwright not installed. Run: pip install playwright && "
        "python -m playwright install chromium\n"
    )
    sys.exit(1)


# --- Entra ID SSO selectors (stable, Microsoft-hosted login UI) -------------
# These are the standard login.microsoftonline.com / login.live.com ids and
# have been stable for years; override via env only if a tenant uses a custom
# branded sign-in page that changed them.
ENTRA_USER_SEL = os.environ.get("ENTRA_USER_SEL", 'input[name="loginfmt"]')
ENTRA_USER_NEXT = os.environ.get("ENTRA_USER_NEXT", '#idSIButton9')
ENTRA_PASS_SEL = os.environ.get("ENTRA_PASS_SEL", 'input[name="passwd"]')
ENTRA_PASS_NEXT = os.environ.get("ENTRA_PASS_NEXT", '#idSIButton9')
# "Stay signed in?" prompt — click "Yes" (idSIButton9) or "No" (idBtn_Back).
ENTRA_STAY_YES = os.environ.get("ENTRA_STAY_YES", '#idSIButton9')

# --- Onboard post-SSO selectors (tenant-specific) ---------------------------
# After SSO the Onboard web page presents device onboarding. The cert is
# usually a download button/link. Cloud NAC tenants vary — point these at the
# real elements (env-overridable so no code edit is needed per tenant).
# If unset, the script falls back to capturing ANY download the page triggers
# (page.on("download")) after clicking a configurable "Download" button.
ONBOARD_DOWNLOAD_SEL = os.environ.get(
    "ONBOARD_DOWNLOAD_SEL", 'a:has-text("Download"), button:has-text("Download")'
)
# Some Onboard flows ask for a device name before issuing the cert.
ONBOARD_DEVICE_SEL = os.environ.get("ONBOARD_DEVICE_SEL", 'input[name="device_name"]')
ONBOARD_DEVICE_SUBMIT = os.environ.get(
    "ONBOARD_DEVICE_SUBMIT", 'button:has-text("Continue"), button:has-text("Submit")'
)


def log(msg: str) -> None:
    print(f"[cloud-nac-onboard] {msg}", flush=True)


def entra_login(page, user: str, password: str) -> None:
    """Drive the Entra ID SSO login. Raises on timeout / MFA prompt."""
    log("entering Entra SSO — filling username")
    page.wait_for_selector(ENTRA_USER_SEL, timeout=30_000)
    page.fill(ENTRA_USER_SEL, user)
    page.click(ENTRA_USER_NEXT)

    log("filling password")
    page.wait_for_selector(ENTRA_PASS_SEL, timeout=30_000)
    page.fill(ENTRA_PASS_SEL, password)
    page.click(ENTRA_PASS_NEXT)

    # The "Stay signed in?" dialog appears after pw acceptance. If an MFA
    # challenge appears instead, this wait will time out → exit code 2.
    log("handling post-password prompt (stay-signed-in / MFA watch)")
    try:
        page.wait_for_selector(ENTRA_STAY_YES, timeout=20_000)
        page.click(ENTRA_STAY_YES)
    except PWTimeout:
        # Could be the "No" path or an MFA interrupt. Detect MFA markers.
        body = (page.inner_text("body") or "").lower()
        if any(m in body for m in ("enter the code", "approve a sign-in",
                                   "authenticator", "verify it's you", "mfa")):
            log("MFA prompt detected — account is NOT MFA-exempt; aborting.")
            sys.exit(2)
        log("no stay-signed-in prompt; continuing (tenant may skip it).")


def onboard_download(page, download_dir: Path, device_name: str) -> Path:
    """Complete the Onboard flow and return the path of the downloaded cert
    bundle (expected PKCS#12). Falls back to any download event."""
    downloaded = {"path": None}

    def _on_download(download):
        # Save into our dir; Playwright saves to a tmp path by default.
        fname = download.suggested_filename or "onboard.p12"
        out = download_dir / fname
        download.save_as(str(out))
        downloaded["path"] = out
        log(f"download captured: {out}")

    page.on("download", _on_download)

    # Optional device-name step.
    try:
        page.wait_for_selector(ONBOARD_DEVICE_SEL, timeout=8_000)
        page.fill(ONBOARD_DEVICE_SEL, device_name)
        try:
            page.click(ONBOARD_DEVICE_SUBMIT)
        except PWTimeout:
            pass
    except PWTimeout:
        log("no device-name step; proceeding to download")

    log("clicking Onboard download")
    try:
        page.click(ONBOARD_DOWNLOAD_SEL, timeout=20_000)
    except PWTimeout:
        # Some flows auto-trigger the download on page load.
        log("no explicit download button — relying on auto-download event")

    # Wait for the download event to fire.
    deadline = time.time() + 40
    while downloaded["path"] is None and time.time() < deadline:
        page.wait_for_timeout(500)
    if downloaded["path"] is None:
        log("no download fired within 40s — Onboard flow did not serve a cert.")
        sys.exit(3)
    return downloaded["path"]


def p12_to_pem(p12_path: Path, p12_password: str, out_dir: Path) -> dict:
    """Split a PKCS#12 into client.crt / client.key / ca.crt via openssl.
    Returns the three paths. Exits 4 on failure."""
    import subprocess
    client_crt = out_dir / "client.crt"
    client_key = out_dir / "client.key"
    ca_crt = out_dir / "ca.crt"
    env = dict(os.environ)
    try:
        # client cert + chain
        subprocess.run(
            ["openssl", "pkcs12", "-in", str(p12_path), "-clcerts", "-nokeys",
             "-passin", f"pass:{p12_password}", "-out", str(client_crt)],
            check=True, env=env, capture_output=True)
        # private key (unencrypted — nmcli reads it; lock file perms below)
        subprocess.run(
            ["openssl", "pkcs12", "-in", str(p12_path), "-nocerts",
             "-nodes", "-passin", f"pass:{p12_password}", "-out", str(client_key)],
            check=True, env=env, capture_output=True)
        # CA chain
        subprocess.run(
            ["openssl", "pkcs12", "-in", str(p12_path), "-cacerts", "-nokeys",
             "-passin", f"pass:{p12_password}", "-out", str(ca_crt)],
            check=True, env=env, capture_output=True)
    except subprocess.CalledProcessError as e:
        log("openssl p12 split failed: " + (e.stderr.decode() if e.stderr else str(e)))
        sys.exit(4)
    os.chmod(client_key, 0o600)
    os.chmod(client_crt, 0o644)
    os.chmod(ca_crt, 0o644)
    return {"client_cert": client_crt, "private_key": client_key, "ca_cert": ca_crt}


def main() -> int:
    ap = argparse.ArgumentParser(description="Headless Cloud NAC Onboard (Entra, no MFA).")
    ap.add_argument("--provisioning-url", required=True,
                    help="Cloud NAC Onboard provisioning URL (tenant-specific).")
    ap.add_argument("--user", required=True, help="Entra UPN, e.g. kbell@example.com")
    ap.add_argument("--password", required=True, help="Entra password (no MFA).")
    ap.add_argument("--p12-password", required=True,
                    help="Password protecting the issued PKCS#12 bundle.")
    ap.add_argument("--out-dir", required=True, help="Where to write cert files.")
    ap.add_argument("--device-name", default="",
                    help="Optional device name Onboard may prompt for.")
    ap.add_argument("--headless", default="1",
                    help="1=run headless (default). 0=show browser (needs a display).")
    ap.add_argument("--keep-p12", action="store_true",
                    help="Keep the raw .p12 in --out-dir after splitting.")
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=(args.headless == "1"))
        ctx = browser.new_context(accept_downloads=True)
        page = ctx.new_page()

        log(f"opening provisioning URL: {args.provisioning_url}")
        page.goto(args.provisioning_url, wait_until="domcontentloaded")

        entra_login(page, args.user, args.password)
        p12 = onboard_download(page, out_dir, args.device_name)

        log(f"splitting PKCS#12: {p12}")
        certs = p12_to_pem(p12, args.p12_password, out_dir)
        if not args.keep_p12:
            try:
                p12.unlink()
            except OSError:
                pass

        browser.close()

    log("SUCCESS — EAP-TLS material ready:")
    log(f"  client_cert = {certs['client_cert']}")
    log(f"  private_key = {certs['private_key']}")
    log(f"  ca_cert     = {certs['ca_cert']}")
    log("Set in simulation.conf: dot1x_eap=tls + the three paths above.")
    return 0


if __name__ == "__main__":
    sys.exit(main())