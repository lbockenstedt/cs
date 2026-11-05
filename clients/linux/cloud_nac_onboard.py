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
# Postpone evaluation of type hints so annotations like "dict" never cost anything at runtime.
from __future__ import annotations

# argparse parses the command-line flags described in the module docstring.
import argparse
# os reads env-var selector overrides, copies the environment and sets file permissions.
import os
# sys writes errors to stderr and exits with the documented exit codes.
import sys
# time drives the deadline loop that waits for the cert download.
import time
# Path builds the output/download file paths.
from pathlib import Path

# Playwright is an optional dependency; import it up front so we can fail with a helpful message.
try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
# Playwright is missing: tell the operator how to install it and stop immediately.
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
    """Print a progress message with a fixed prefix, flushed so it shows up live in logs."""
    # Prefix every line so this script's output is easy to find in the combined sim client log.
    print(f"[cloud-nac-onboard] {msg}", flush=True)


def entra_login(page, user: str, password: str) -> None:
    """Drive the Entra ID SSO login. Raises on timeout / MFA prompt."""
    # Step 1: wait for the username box, type the UPN and press Next.
    log("entering Entra SSO — filling username")
    page.wait_for_selector(ENTRA_USER_SEL, timeout=30_000)
    page.fill(ENTRA_USER_SEL, user)
    page.click(ENTRA_USER_NEXT)

    # Step 2: wait for the password box, type the password and press Sign in.
    log("filling password")
    page.wait_for_selector(ENTRA_PASS_SEL, timeout=30_000)
    page.fill(ENTRA_PASS_SEL, password)
    page.click(ENTRA_PASS_NEXT)

    # The "Stay signed in?" dialog appears after pw acceptance. If an MFA
    # challenge appears instead, this wait will time out → exit code 2.
    log("handling post-password prompt (stay-signed-in / MFA watch)")
    # Try to click "Yes" on the stay-signed-in dialog if it shows up within 20 seconds.
    try:
        page.wait_for_selector(ENTRA_STAY_YES, timeout=20_000)
        page.click(ENTRA_STAY_YES)
    except PWTimeout:
        # The dialog never appeared: either the tenant skips it or an MFA challenge replaced it.
        # Could be the "No" path or an MFA interrupt. Detect MFA markers.
        body = (page.inner_text("body") or "").lower()
        # Look for well-known MFA wording; MFA cannot be automated so abort with exit code 2.
        if any(m in body for m in ("enter the code", "approve a sign-in",
                                   "authenticator", "verify it's you", "mfa")):
            log("MFA prompt detected — account is NOT MFA-exempt; aborting.")
            sys.exit(2)
        # No MFA wording found, so assume the tenant simply skips the prompt and keep going.
        log("no stay-signed-in prompt; continuing (tenant may skip it).")


def onboard_download(page, download_dir: Path, device_name: str) -> Path:
    """Complete the Onboard flow and return the path of the downloaded cert
    bundle (expected PKCS#12). Falls back to any download event."""
    # Mutable holder so the nested download callback can hand the saved file path back to us.
    downloaded = {"path": None}

    def _on_download(download):
        """Save a download from the Onboard page into our directory and remember its path."""
        # Save into our dir; Playwright saves to a tmp path by default.
        fname = download.suggested_filename or "onboard.p12"
        out = download_dir / fname
        download.save_as(str(out))
        downloaded["path"] = out
        log(f"download captured: {out}")

    # Register the callback so any download the page triggers is captured automatically.
    page.on("download", _on_download)

    # Optional device-name step.
    # Some tenants ask for a device name first; fill it in if the field shows up within 8 seconds.
    try:
        page.wait_for_selector(ONBOARD_DEVICE_SEL, timeout=8_000)
        page.fill(ONBOARD_DEVICE_SEL, device_name)
        # Submit the device name; a missing submit button is tolerated because some forms
        # auto-advance.
        try:
            page.click(ONBOARD_DEVICE_SUBMIT)
        except PWTimeout:
            pass
    except PWTimeout:
        # No device-name field appeared, which is normal for many tenants.
        log("no device-name step; proceeding to download")

    # Click the download button to make Onboard issue and serve the cert bundle.
    log("clicking Onboard download")
    try:
        page.click(ONBOARD_DOWNLOAD_SEL, timeout=20_000)
    except PWTimeout:
        # No button found in time.
        # Some flows auto-trigger the download on page load.
        log("no explicit download button — relying on auto-download event")

    # Wait for the download event to fire.
    # Poll every 0.5 s for up to 40 s until the download callback has stored a path.
    deadline = time.time() + 40
    while downloaded["path"] is None and time.time() < deadline:
        page.wait_for_timeout(500)
    # Nothing was downloaded, so the Onboard flow failed to issue a cert: exit code 3.
    if downloaded["path"] is None:
        log("no download fired within 40s — Onboard flow did not serve a cert.")
        sys.exit(3)
    return downloaded["path"]


def p12_to_pem(p12_path: Path, p12_password: str, out_dir: Path) -> dict:
    """Split a PKCS#12 into client.crt / client.key / ca.crt via openssl.
    Returns the three paths. Exits 4 on failure."""
    # subprocess runs the openssl CLI; imported here because only this function needs it.
    import subprocess
    # Fixed output file names that simulation.conf points its dot1x_* settings at.
    client_crt = out_dir / "client.crt"
    client_key = out_dir / "client.key"
    ca_crt = out_dir / "ca.crt"
    # Pass a copy of the current environment through to openssl.
    env = dict(os.environ)
    # Run the three openssl extractions; any non-zero exit is treated as a failed split.
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
        # openssl failed (wrong p12 password, corrupt file, ...): log its stderr and exit code 4.
        log("openssl p12 split failed: " + (e.stderr.decode() if e.stderr else str(e)))
        sys.exit(4)
    # Private key readable only by its owner; certs are public so world-readable is fine.
    os.chmod(client_key, 0o600)
    os.chmod(client_crt, 0o644)
    os.chmod(ca_crt, 0o644)
    # Hand the three paths back to the caller for logging.
    return {"client_cert": client_crt, "private_key": client_key, "ca_cert": ca_crt}


def main() -> int:
    """Parse flags, run the browser onboarding flow and write the EAP-TLS cert files.

    Returns: 0 on success (failures exit directly with codes 2-4).
    """
    # Define the command-line interface; all flags are documented in the module docstring.
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

    # Make sure the output directory exists before the download is saved into it.
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Start Playwright, which manages the Chromium process and closes it on exit from the block.
    with sync_playwright() as pw:
        # Launch Chromium (headless unless --headless 0) with downloads enabled in a fresh context.
        browser = pw.chromium.launch(headless=(args.headless == "1"))
        ctx = browser.new_context(accept_downloads=True)
        page = ctx.new_page()

        # Open the tenant Onboard URL, which redirects to the Entra sign-in page.
        log(f"opening provisioning URL: {args.provisioning_url}")
        page.goto(args.provisioning_url, wait_until="domcontentloaded")

        # Log in through Entra SSO, then complete Onboard and grab the issued PKCS#12 file.
        entra_login(page, args.user, args.password)
        p12 = onboard_download(page, out_dir, args.device_name)

        # Convert the PKCS#12 bundle into the separate PEM files nmcli needs.
        log(f"splitting PKCS#12: {p12}")
        certs = p12_to_pem(p12, args.p12_password, out_dir)
        # Delete the raw .p12 unless asked to keep it, since it is no longer needed.
        if not args.keep_p12:
            try:
                p12.unlink()
            except OSError:
                # Failing to delete the leftover file is harmless, so ignore it.
                pass

        # Done with the browser; close it explicitly.
        browser.close()

    # Print where each file landed so the operator can copy the paths into simulation.conf.
    log("SUCCESS — EAP-TLS material ready:")
    log(f"  client_cert = {certs['client_cert']}")
    log(f"  private_key = {certs['private_key']}")
    log(f"  ca_cert     = {certs['ca_cert']}")
    log("Set in simulation.conf: dot1x_eap=tls + the three paths above.")
    return 0


# Run main() only when executed as a script and use its return value as the exit code.
if __name__ == "__main__":
    sys.exit(main())