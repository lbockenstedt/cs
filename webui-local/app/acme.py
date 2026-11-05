"""ACME certificate management for Hub.

Handles certificate requests and renewals via the ACME protocol (Let's Encrypt,
ZeroSSL, or any RFC 8555-compliant CA). Configured entirely through the web UI —
no env vars required.

Config is stored in the hub's global settings JSON (DATA_DIR/acme.json).
Account key is stored at DATA_DIR/tls/acme_account.pem.
Issued cert/key replace DATA_DIR/tls/cert.pem and DATA_DIR/tls/key.pem.

Challenge type: DNS-01 only.
Supported providers: cloudflare, hurricane_electric, godaddy, digitalocean,
                     porkbun, gcloud, dnsimple, azure_dns, route53, namecheap.

Hurricane Electric DNS setup:
  1. In dns.he.net, create a TXT record for _acme-challenge.<domain>
  2. Enable DDNS on that record and note the generated DDNS key
  3. Enter the DDNS key as the credential — the hostname is derived automatically
"""
from __future__ import annotations

# Standard library imports.
import asyncio
import base64
import contextlib
import hashlib
import hmac
import json
import logging
import traceback
# defusedxml forbids DTD/entity resolution — the Namecheap API response is
# third-party XML fetched over HTTP; a malicious/MITM response with an external
# entity could otherwise read local files (XXE) or cause DoS.
import defusedxml.ElementTree as ET
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# Certificate and key handling.
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.serialization import load_pem_private_key
from cryptography.x509.oid import NameOID

# Hub settings and credential encryption.
from .config import get_settings
from .crypto import decrypt_dict, encrypt_dict

# Module logger, and ACME directory URLs for each supported CA.
logger = logging.getLogger(__name__)
_CA_DIRECTORY = {
    "letsencrypt": "https://acme-v02.api.letsencrypt.org/directory",
    "letsencrypt_staging": "https://acme-staging-v02.api.letsencrypt.org/directory",
    "zerossl": "https://acme.zerossl.com/v2/DV90",
}
# DNS records created for pending challenges, per provider, keyed by (domain, TXT value).
# Kept so each record can be deleted after validation.
_cloudflare_records: dict[tuple[str, str], tuple[str, str, dict[str, str]]] = {}
_he_records: dict[tuple[str, str], str] = {}  # (domain, txt_record) → acme-challenge hostname
_godaddy_records: dict[tuple[str, str], tuple[str, str, dict[str, str]]] = {}
_do_records: dict[tuple[str, str], tuple[str, int, dict[str, str]]] = {}
_porkbun_records: dict[tuple[str, str], tuple[str, str]] = {}
_gcloud_records: dict[tuple[str, str], tuple[str, str, str]] = {}
_dnsimple_records: dict[tuple[str, str], tuple[str, int]] = {}
_azure_records: dict[tuple[str, str], tuple[str, str, str, str]] = {}
_route53_records: dict[tuple[str, str], tuple[str, str]] = {}
_namecheap_records: dict[tuple[str, str], dict[str, Any]] = {}


def _split_apex(domain: str) -> tuple[str, str]:
    """Split into (subdomain_part, apex). e.g. 'cs-hub.example.com' → ('cs-hub', 'example.com')"""
    # Two labels or fewer: the whole name is the apex.
    parts = domain.split(".")
    if len(parts) <= 2:
        return "", domain
    return ".".join(parts[:-2]), ".".join(parts[-2:])


# Progress of the current or last run, shown in the UI.
_acme_status: dict[str, Any] = {
    "running": False,
    "status": "idle",
    "last_result": None,
    "last_error": None,
    "last_log": "",
    "last_log_at": "",
}


class AcmeLogCapture:
    """Collects timestamped log lines for an ACME run and mirrors them into the status."""
    def __init__(self) -> None:
        """Start with an empty log."""
        self.lines: list[str] = []

    def append(self, message: str) -> None:
        """Add a timestamped line and update the status's log."""
        line = f"[{_iso(_utcnow())}] {message}"
        self.lines.append(line)
        _set_acme_status(last_log=self.text(), last_log_at=_iso(_utcnow()))

    def append_exception(self, exc: Exception) -> None:
        """Log an error and, if one is active, its traceback."""
        # Error line first.
        self.append(f"ERROR: {exc}")
        # Then the traceback, unless there is none.
        details = traceback.format_exc().strip()
        if details and details != "NoneType: None":
            self.append("Traceback:")
            self.lines.extend(details.splitlines())
            _set_acme_status(last_log=self.text(), last_log_at=_iso(_utcnow()))

    def text(self) -> str:
        """Return the log as one string."""
        return "\n".join(self.lines).strip()


def _set_acme_status(**updates: Any) -> None:
    """Merge updates into the module's ACME status."""
    _acme_status.update(updates)


def _derive_status(current: dict[str, Any], cfg: AcmeConfig | None = None) -> str:
    """Work out a one-word status: running, success, failed, error or idle."""
    # Running takes priority.
    if current.get("running"):
        return "running"
    # Then the last result.
    result = current.get("last_result")
    if isinstance(result, dict):
        if result.get("success") is True:
            return "success"
        if result.get("success") is False:
            return "failed"
    # Then any recorded error.
    if current.get("last_error") or (cfg and cfg.last_error):
        return "error"
    return "idle"


def get_acme_status() -> dict[str, Any]:
    """Return the ACME status for the UI.

    Values not set in this process (e.g. after a restart) come from the saved config.
    """
    # Copy the in-memory status.
    cfg = load_acme_config()
    status = dict(_acme_status)
    # Fill gaps from the saved config.
    if not status.get("last_log"):
        status["last_log"] = cfg.last_log
    if not status.get("last_log_at"):
        status["last_log_at"] = cfg.last_log_at
    if not status.get("last_error"):
        status["last_error"] = cfg.last_error or None
    # Work out the overall status.
    status["status"] = _derive_status(status, cfg)
    return status


@dataclass
class AcmeConfig:
    """Saved ACME settings and the result of the last run."""
    enabled: bool = False
    domain: str = ""
    email: str = ""
    challenge: str = "dns-01"
    ca: str = "letsencrypt"
    dns_provider: str = ""
    dns_credentials: dict = field(default_factory=dict)
    last_renewed: str = ""
    last_error: str = ""
    cert_expiry: str = ""
    last_log: str = ""
    last_log_at: str = ""


def _utcnow() -> datetime:
    """Return the current time in UTC."""
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None) -> str:
    """Format a datetime as UTC ISO-8601 with a Z suffix; empty string for None."""
    if not dt:
        return ""
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _cfg_path(data_dir: Path | None = None) -> Path:
    """Return the path of the ACME config file."""
    base = data_dir or Path(get_settings().data_dir)
    return base / "acme.json"


def _cert_path(data_dir: Path | None = None) -> Path:
    """Return the path of the hub's TLS certificate."""
    base = data_dir or Path(get_settings().data_dir)
    return base / "tls" / "cert.pem"


def _challenge_store() -> dict[str, str]:
    """Return an empty challenge store (placeholder)."""
    return {}


def _serialize_config(cfg: AcmeConfig, *, encrypt_credentials: bool) -> dict[str, Any]:
    """Convert the config to a dict for saving or display.

    With encrypt_credentials, DNS credentials are encrypted (or "" if empty); otherwise they
    are left as a plain dict.
    """
    data = asdict(cfg)
    creds = dict(cfg.dns_credentials or {})
    data["dns_credentials"] = encrypt_dict(creds) if encrypt_credentials and creds else ("" if encrypt_credentials else creds)
    return data


def _load_acme_config(path: Path) -> AcmeConfig:
    """Read an ACME config file; returns defaults if it is missing or unreadable."""
    # No file means defaults.
    if not path.exists():
        return AcmeConfig()
    # Unreadable JSON means defaults.
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("Failed to read ACME config %s: %s", path, exc)
        return AcmeConfig()
    # Decrypt the stored DNS credentials; anything invalid becomes an empty dict.
    creds = raw.get("dns_credentials")
    if isinstance(creds, str) and creds:
        try:
            raw["dns_credentials"] = decrypt_dict(creds)
        except Exception as exc:
            logger.warning("Failed to decrypt ACME DNS credentials: %s", exc)
            raw["dns_credentials"] = {}
    elif not isinstance(creds, dict):
        raw["dns_credentials"] = {}
    # Build the config from known fields only.
    allowed = {field.name for field in AcmeConfig.__dataclass_fields__.values()}
    return AcmeConfig(**{key: raw.get(key) for key in allowed if key in raw})


def load_acme_config() -> AcmeConfig:
    """Load the hub's ACME config."""
    return _load_acme_config(_cfg_path())


def save_acme_config(cfg: AcmeConfig) -> None:
    """Save the ACME config with DNS credentials encrypted."""
    path = _cfg_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_serialize_config(cfg, encrypt_credentials=True), indent=2), encoding="utf-8")


def _get_cert_info(path: Path) -> dict:
    """Describe a certificate file: domain, issuer, expiry, days left, and source.

    Source is "none", "self-signed", "custom", or "acme" if it matches the last ACME domain.
    """
    # No file.
    if not path.exists():
        return {"source": "none"}
    # Load it and read the common name.
    cert = x509.load_pem_x509_certificate(path.read_bytes())
    try:
        domain = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value
    except IndexError:
        domain = ""
    # Issuer: organisation name, or else common name.
    issuer = ""
    for oid in (NameOID.ORGANIZATION_NAME, NameOID.COMMON_NAME):
        attrs = cert.issuer.get_attributes_for_oid(oid)
        if attrs:
            issuer = attrs[0].value
            break
    # Expiry and whole days left.
    expires = cert.not_valid_after.replace(tzinfo=timezone.utc)
    remaining = max(0, int((expires - _utcnow()).total_seconds() // 86400))
    # Work out where the certificate came from.
    is_self_signed = cert.issuer == cert.subject
    source = "self-signed" if is_self_signed else "custom"
    cfg = load_acme_config()
    if cfg.last_renewed and cfg.domain and cfg.domain == domain:
        source = "acme"
    # Return the details.
    return {
        "domain": domain,
        "issuer": issuer,
        "expires": _iso(expires),
        "days_remaining": remaining,
        "is_self_signed": is_self_signed,
        "source": source,
    }


def get_cert_info() -> dict:
    """Describe the hub's current TLS certificate."""
    return _get_cert_info(_cert_path())


def _ca_directory(ca: str) -> str:
    """Return the directory URL for a CA name; unknown names use Let's Encrypt."""
    return _CA_DIRECTORY.get(ca, _CA_DIRECTORY["letsencrypt"])


def _get_or_create_account_key(key_path: Path) -> josepy.JWKRSA:
    """Load the ACME account key, creating and saving a new RSA key if there is none."""
    from josepy import JWKRSA

    # Load the key if it exists.
    key_path.parent.mkdir(parents=True, exist_ok=True)
    if key_path.exists():
        private_key = load_pem_private_key(key_path.read_bytes(), password=None)
    # Otherwise create a 2048-bit RSA key and save it.
    else:
        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        key_path.write_bytes(
            private_key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.TraditionalOpenSSL,
                encryption_algorithm=serialization.NoEncryption(),
            )
        )
    # Wrap it for the ACME client.
    return JWKRSA(key=private_key)


async def _dns01_cloudflare(domain: str, txt_record: str, credentials: dict, log: AcmeLogCapture | None = None) -> None:
    """Create the DNS-01 TXT record in Cloudflare and wait for it to propagate.

    Uses an API token, or an email and global API key. The record is remembered for cleanup.
    """
    # Imported here so the module loads even where httpx is not needed.
    import httpx

    # Use the API token if given; otherwise the email and global key.
    token = (credentials or {}).get("cf_api_token", "").strip()
    email = (credentials or {}).get("cf_email", "").strip()
    api_key = (credentials or {}).get("cf_api_key", "").strip()
    if token:
        headers = {"Authorization": f"Bearer {token}"}
    elif email and api_key:
        headers = {"X-Auth-Email": email, "X-Auth-Key": api_key}
    else:
        raise ValueError("Cloudflare credentials must include cf_api_token or cf_email + cf_api_key")
    headers["Content-Type"] = "application/json"

    # Find the zone by trying each parent of the domain in turn.
    labels = domain.split(".")
    zone_id = ""
    zone_name = ""
    if log:
        log.append(f"Preparing Cloudflare DNS-01 challenge for {domain}")
    async with httpx.AsyncClient(timeout=20) as client:
        for index in range(1, len(labels)):
            candidate = ".".join(labels[index:])
            if log:
                log.append(f"Checking Cloudflare zone candidate {candidate}")
            response = await client.get("https://api.cloudflare.com/client/v4/zones", params={"name": candidate}, headers=headers)
            response.raise_for_status()
            payload = response.json()
            result = payload.get("result") or []
            if result:
                zone_id = result[0]["id"]
                zone_name = candidate
                break
        if not zone_id:
            raise ValueError(f"Could not determine Cloudflare zone for {domain}")
        # Create the TXT record.
        record_name = f"_acme-challenge.{domain}" if domain != zone_name else f"_acme-challenge.{zone_name}"
        if log:
            log.append(f"Creating TXT record {record_name} in Cloudflare zone {zone_name}")
        response = await client.post(
            f"https://api.cloudflare.com/client/v4/zones/{zone_id}/dns_records",
            headers=headers,
            json={"type": "TXT", "name": record_name, "content": txt_record, "ttl": 120},
        )
        # Remember the record id for cleanup.
        response.raise_for_status()
        result = response.json().get("result") or {}
        record_id = result.get("id")
        if not record_id:
            raise ValueError("Cloudflare did not return a DNS record id")
        _cloudflare_records[(domain, txt_record)] = (zone_id, record_id, headers)
    # Give DNS time to propagate.
    if log:
        log.append("Cloudflare TXT record created; waiting 10 seconds for propagation")
    await asyncio.sleep(10)


async def _cleanup_cloudflare_record(domain: str, txt_record: str) -> None:
    """Delete the Cloudflare TXT record created for this challenge, ignoring errors."""
    # Imported here so the module loads even where httpx is not needed.
    import httpx

    # Nothing to do if no record was created.
    record = _cloudflare_records.pop((domain, txt_record), None)
    if not record:
        return
    zone_id, record_id, headers = record
    # Delete it; errors are ignored.
    async with httpx.AsyncClient(timeout=20) as client:
        with contextlib.suppress(Exception):
            await client.delete(
                f"https://api.cloudflare.com/client/v4/zones/{zone_id}/dns_records/{record_id}",
                headers=headers,
            )


async def _dns01_hurricane_electric(domain: str, txt_record: str, credentials: dict, log: AcmeLogCapture | None = None) -> None:
    """Set _acme-challenge TXT record via Hurricane Electric DDNS API.

    Prerequisites (one-time setup in dns.he.net):
      1. Create a TXT record named _acme-challenge.<domain>
      2. Enable DDNS on that record — HE generates a DDNS key
      3. Supply that key as ``he_ddns_key`` in credentials
    """
    # Imported here so the module loads even where httpx is not needed.
    import httpx

    # The DDNS key is required.
    ddns_key = (credentials or {}).get("he_ddns_key", "").strip()
    if not ddns_key:
        raise ValueError("Hurricane Electric DNS requires he_ddns_key credential")

    # Set the TXT value through the DDNS update API.
    challenge_hostname = f"_acme-challenge.{domain}"
    if log:
        log.append(f"Updating Hurricane Electric TXT record {challenge_hostname}")
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(
            "https://dyn.dns.he.net/nic/update",
            data={"hostname": challenge_hostname, "password": ddns_key, "txt": txt_record},
        )
        # Bad status, bad auth, or anything other than good/nochg is a failure.
        body = response.text.strip()
        if response.status_code not in (200, 204) or body.startswith("badauth"):
            raise ValueError(f"HE DNS update failed ({response.status_code}): {body}")
        if not (body.startswith("good") or body.startswith("nochg")):
            raise ValueError(f"HE DNS unexpected response: {body}")

    # Remember the host for cleanup.
    _he_records[(domain, txt_record)] = challenge_hostname
    if log:
        log.append("Hurricane Electric TXT record updated; waiting 15 seconds for propagation")
    # Allow time for DNS propagation before CA checks
    await asyncio.sleep(15)


async def _cleanup_he_record(domain: str, txt_record: str, ddns_key: str = "") -> None:
    """Clear the _acme-challenge TXT record after validation."""
    # Imported here so the module loads even where httpx is not needed.
    import httpx

    # Nothing to do without a record or key.
    entry = _he_records.pop((domain, txt_record), None)
    if not entry or not ddns_key:
        return
    # Set the TXT value to empty; errors are ignored.
    async with httpx.AsyncClient(timeout=20) as client:
        with contextlib.suppress(Exception):
            await client.post(
                "https://dyn.dns.he.net/nic/update",
                data={"hostname": entry, "password": ddns_key, "txt": ""},
            )


def _relative_subdomain(domain: str, zone_name: str) -> str:
    """Return the part of the domain in front of the zone name ("" if they are the same)."""
    # Domain is the zone itself.
    if zone_name and domain == zone_name:
        return ""
    # Strip the zone suffix.
    suffix = f".{zone_name}"
    if zone_name and domain.endswith(suffix):
        return domain[: -len(suffix)]
    # Otherwise guess using the last two labels.
    subdomain, _ = _split_apex(domain)
    return subdomain


def _urlsafe_b64(data: bytes) -> str:
    """Base64url-encode without padding."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _xml_local_name(tag: str) -> str:
    """Strip any XML namespace from a tag name."""
    return tag.rsplit("}", 1)[-1]


def _build_route53_change_xml(action: str, domain: str, txt_record: str) -> str:
    """Build a Route 53 request body that changes the _acme-challenge TXT record."""
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<ChangeResourceRecordSetsRequest xmlns="https://route53.amazonaws.com/doc/2013-04-01/">
  <ChangeBatch>
    <Changes>
      <Change>
        <Action>{action}</Action>
        <ResourceRecordSet>
          <Name>_acme-challenge.{domain}.</Name>
          <Type>TXT</Type>
          <TTL>300</TTL>
          <ResourceRecords>
            <ResourceRecord>
              <Value>"{txt_record}"</Value>
            </ResourceRecord>
          </ResourceRecords>
        </ResourceRecordSet>
      </Change>
    </Changes>
  </ChangeBatch>
</ChangeResourceRecordSetsRequest>'''


def _route53_auth_headers(access_key: str, secret_key: str, zone_id: str, body: str) -> dict[str, str]:
    """Build AWS Signature Version 4 headers for a Route 53 change request."""
    # Timestamp and payload hash.
    amz_date = _utcnow().strftime("%Y%m%dT%H%M%SZ")
    date_stamp = amz_date[:8]
    payload_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
    # Canonical request.
    canonical_uri = f"/2013-04-01/hostedzone/{zone_id}/rrset/"
    canonical_headers = (
        "content-type:application/xml\n"
        "host:route53.amazonaws.com\n"
        f"x-amz-content-sha256:{payload_hash}\n"
        f"x-amz-date:{amz_date}\n"
    )
    signed_headers = "content-type;host;x-amz-content-sha256;x-amz-date"
    canonical_request = "\n".join(["POST", canonical_uri, "", canonical_headers, signed_headers, payload_hash])
    # String to sign.
    credential_scope = f"{date_stamp}/us-east-1/route53/aws4_request"
    string_to_sign = "\n".join(
        [
            "AWS4-HMAC-SHA256",
            amz_date,
            credential_scope,
            hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
        ]
    )

    def _sign(key: bytes, message: str) -> bytes:
        """HMAC-SHA256."""
        return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()

    # Derive the signing key and sign.
    signing_key = _sign(_sign(_sign(_sign((f"AWS4{secret_key}").encode("utf-8"), date_stamp), "us-east-1"), "route53"), "aws4_request")
    signature = hmac.new(signing_key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    # Headers for the request.
    return {
        "Content-Type": "application/xml",
        "Host": "route53.amazonaws.com",
        "X-Amz-Content-Sha256": payload_hash,
        "X-Amz-Date": amz_date,
        "Authorization": (
            "AWS4-HMAC-SHA256 "
            f"Credential={access_key}/{credential_scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        ),
    }


async def _get_gcloud_access_token(credentials: dict) -> tuple[str, str, str]:
    """Get a Google Cloud OAuth token from a service account.

    Returns (access token, project id, zone name).
    """
    # Imported here so the module loads even where httpx is not needed.
    import httpx

    # Service account JSON and zone name are required.
    service_account_json = (credentials or {}).get("gcloud_service_account_json", "").strip()
    zone_name = (credentials or {}).get("gcloud_zone_name", "").strip()
    if not service_account_json:
        raise ValueError("Google Cloud DNS requires gcloud_service_account_json credential")
    if not zone_name:
        raise ValueError("Google Cloud DNS requires gcloud_zone_name credential")
    # Parse the JSON; it must include email, private key and project id.
    try:
        service_account = json.loads(service_account_json)
    except json.JSONDecodeError as exc:
        raise ValueError("Google Cloud DNS service account JSON is invalid") from exc
    client_email = str(service_account.get("client_email") or "").strip()
    private_key = str(service_account.get("private_key") or "").strip()
    project_id = str(service_account.get("project_id") or "").strip()
    if not client_email or not private_key or not project_id:
        raise ValueError("Google Cloud DNS service account JSON must include client_email, private_key, and project_id")

    # Build a signed JWT for the token request.
    now = int(_utcnow().timestamp())
    header = {"alg": "RS256", "typ": "JWT"}
    payload = {
        "iss": client_email,
        "scope": "https://www.googleapis.com/auth/ndev.clouddns.readwrite",
        "aud": "https://oauth2.googleapis.com/token",
        "iat": now,
        "exp": now + 3600,
    }
    signing_input = ".".join(
        [
            _urlsafe_b64(json.dumps(header, separators=(",", ":")).encode("utf-8")),
            _urlsafe_b64(json.dumps(payload, separators=(",", ":")).encode("utf-8")),
        ]
    )
    private_key_obj = load_pem_private_key(private_key.encode("utf-8"), password=None)
    signature = private_key_obj.sign(signing_input.encode("utf-8"), padding.PKCS1v15(), hashes.SHA256())
    assertion = f"{signing_input}.{_urlsafe_b64(signature)}"

    # Exchange the JWT for an access token.
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": assertion,
            },
        )
        response.raise_for_status()
        access_token = (response.json() or {}).get("access_token", "")
        if not access_token:
            raise ValueError("Google Cloud OAuth token exchange did not return an access token")
    return access_token, project_id, zone_name


async def _route53_change_record(access_key: str, secret_key: str, zone_id: str, body: str) -> None:
    """Send a change request to Route 53; raises ValueError on failure."""
    # Imported here so the module loads even where httpx is not needed.
    import httpx

    # Signed request.
    headers = _route53_auth_headers(access_key, secret_key, zone_id, body)
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(
            f"https://route53.amazonaws.com/2013-04-01/hostedzone/{zone_id}/rrset/",
            content=body.encode("utf-8"),
            headers=headers,
        )
        if response.status_code not in (200, 201):
            raise ValueError(f"Route53 change failed ({response.status_code}): {response.text.strip()}")


async def _get_namecheap_client_ip(credentials: dict) -> str:
    """Return the hub's public IP for Namecheap's API, from credentials or ipify.org."""
    # Imported here so the module loads even where httpx is not needed.
    import httpx

    # Use the configured IP if there is one.
    client_ip = (credentials or {}).get("namecheap_client_ip", "").strip()
    if client_ip:
        return client_ip
    # Otherwise look it up.
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.get("https://api.ipify.org")
        response.raise_for_status()
        return response.text.strip()


def _namecheap_parse_response(xml_text: str) -> ET.Element:
    """Parse a Namecheap API response; raise ValueError if it reports errors or a non-OK status."""
    # Collect any error messages.
    root = ET.fromstring(xml_text)
    errors = [
        (elem.text or "").strip()
        for elem in root.iter()
        if _xml_local_name(elem.tag) == "Error" and (elem.text or "").strip()
    ]
    if errors:
        raise ValueError(f"Namecheap API error: {'; '.join(errors)}")
    # Status must be OK.
    status = root.attrib.get("Status", root.attrib.get("status", "OK"))
    if str(status).upper() != "OK":
        raise ValueError(f"Namecheap API request failed with status {status}")
    return root


def _namecheap_parse_hosts(xml_text: str) -> list[dict[str, str]]:
    """Return the host records from a Namecheap getHosts response."""
    # Check the response, then read each host element.
    root = _namecheap_parse_response(xml_text)
    hosts: list[dict[str, str]] = []
    for elem in root.iter():
        if _xml_local_name(elem.tag).lower() != "host":
            continue
        attrs = elem.attrib
        hosts.append(
            {
                "Name": attrs.get("Name") or attrs.get("name") or "@",
                "Type": attrs.get("Type") or attrs.get("type") or "A",
                "Address": attrs.get("Address") or attrs.get("address") or "",
                "TTL": attrs.get("TTL") or attrs.get("ttl") or "300",
                "MXPref": attrs.get("MXPref") or attrs.get("mxpref") or "10",
            }
        )
    return hosts


async def _namecheap_get_hosts(username: str, api_key: str, client_ip: str, sld: str, tld: str) -> list[dict[str, str]]:
    """Fetch all host records for a Namecheap domain."""
    # Imported here so the module loads even where httpx is not needed.
    import httpx

    # Request parameters.
    params = {
        "ApiUser": username,
        "ApiKey": api_key,
        "UserName": username,
        "Command": "namecheap.domains.dns.getHosts",
        "ClientIp": client_ip,
        "SLD": sld,
        "TLD": tld,
    }
    # Fetch and parse.
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.get("https://api.namecheap.com/xml.response", params=params)
        response.raise_for_status()
    return _namecheap_parse_hosts(response.text)


async def _namecheap_set_hosts(username: str, api_key: str, client_ip: str, sld: str, tld: str, hosts: list[dict[str, str]]) -> None:
    """Replace all host records for a Namecheap domain.

    Namecheap's setHosts replaces the whole set, so callers send every record.
    """
    # Imported here so the module loads even where httpx is not needed.
    import httpx

    # Request parameters.
    params: dict[str, str] = {
        "ApiUser": username,
        "ApiKey": api_key,
        "UserName": username,
        "Command": "namecheap.domains.dns.setHosts",
        "ClientIp": client_ip,
        "SLD": sld,
        "TLD": tld,
    }
    # Numbered parameters for each record; MX priority only when relevant.
    for index, host in enumerate(hosts, start=1):
        host_type = str(host.get("Type") or "A").upper()
        params[f"HostName{index}"] = str(host.get("Name") or "@")
        params[f"RecordType{index}"] = host_type
        params[f"Address{index}"] = str(host.get("Address") or "")
        params[f"TTL{index}"] = str(host.get("TTL") or "300")
        mx_pref = str(host.get("MXPref") or "10")
        if host_type == "MX" or host.get("MXPref") not in (None, ""):
            params[f"MXPref{index}"] = mx_pref
    # Send and check for errors.
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.get("https://api.namecheap.com/xml.response", params=params)
        response.raise_for_status()
    _namecheap_parse_response(response.text)


async def _dns01_godaddy(domain: str, txt_record: str, credentials: dict, log: AcmeLogCapture | None = None) -> None:
    """Create the DNS-01 TXT record in GoDaddy and wait for it to propagate."""
    # Imported here so the module loads even where httpx is not needed.
    import httpx

    # Key and secret are required.
    api_key = (credentials or {}).get("godaddy_api_key", "").strip()
    api_secret = (credentials or {}).get("godaddy_api_secret", "").strip()
    if not api_key or not api_secret:
        raise ValueError("GoDaddy requires godaddy_api_key and godaddy_api_secret credentials")
    # Record name relative to the apex domain.
    subdomain, apex = _split_apex(domain)
    record_name = f"_acme-challenge.{subdomain}" if subdomain else "_acme-challenge"
    headers = {"Authorization": f"sso-key {api_key}:{api_secret}", "Content-Type": "application/json"}
    if log:
        log.append(f"Creating GoDaddy TXT record {record_name} in {apex}")
    # Create (or replace) the TXT record.
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.put(
            f"https://api.godaddy.com/v1/domains/{apex}/records/TXT/{record_name}",
            headers=headers,
            json=[{"data": txt_record, "ttl": 600}],
        )
        if response.status_code not in (200, 204):
            raise ValueError(f"GoDaddy DNS update failed ({response.status_code}): {response.text.strip()}")
    # Remember it for cleanup.
    _godaddy_records[(domain, txt_record)] = (apex, record_name, headers)
    if log:
        log.append("GoDaddy TXT record created; waiting 15 seconds for propagation")
    # Give DNS time to propagate.
    await asyncio.sleep(15)


async def _cleanup_godaddy_record(domain: str, txt_record: str) -> None:
    """Delete the GoDaddy TXT record created for this challenge, ignoring errors."""
    # Imported here so the module loads even where httpx is not needed.
    import httpx

    # Take the saved record for this challenge; stop if there is none.
    record = _godaddy_records.pop((domain, txt_record), None)
    if not record:
        return
    apex, record_name, headers = record
    # Delete it; errors are ignored.
    async with httpx.AsyncClient(timeout=20) as client:
        with contextlib.suppress(Exception):
            await client.delete(
                f"https://api.godaddy.com/v1/domains/{apex}/records/TXT/{record_name}",
                headers=headers,
            )


async def _dns01_digitalocean(domain: str, txt_record: str, credentials: dict, log: AcmeLogCapture | None = None) -> None:
    """Create the DNS-01 TXT record in DigitalOcean and wait for it to propagate."""
    # Imported here so the module loads even where httpx is not needed.
    import httpx

    # An API token is required.
    token = (credentials or {}).get("do_token", "").strip()
    if not token:
        raise ValueError("DigitalOcean requires do_token credential")
    # Record name relative to the apex domain.
    subdomain, apex = _split_apex(domain)
    record_name = f"_acme-challenge.{subdomain}" if subdomain else "_acme-challenge"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    if log:
        log.append(f"Creating DigitalOcean TXT record {record_name} in {apex}")
    # Create the TXT record and get its id.
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(
            f"https://api.digitalocean.com/v2/domains/{apex}/records",
            headers=headers,
            json={"type": "TXT", "name": record_name, "data": txt_record, "ttl": 300},
        )
        response.raise_for_status()
        record_id = int(((response.json() or {}).get("domain_record") or {}).get("id") or 0)
        if not record_id:
            raise ValueError("DigitalOcean did not return a DNS record id")
    # Remember it for cleanup.
    _do_records[(domain, txt_record)] = (apex, record_id, headers)
    if log:
        log.append("DigitalOcean TXT record created; waiting 15 seconds for propagation")
    # Give DNS time to propagate.
    await asyncio.sleep(15)


async def _cleanup_do_record(domain: str, txt_record: str) -> None:
    """Delete the DigitalOcean TXT record created for this challenge, ignoring errors."""
    # Imported here so the module loads even where httpx is not needed.
    import httpx

    # Take the saved record for this challenge; stop if there is none.
    record = _do_records.pop((domain, txt_record), None)
    if not record:
        return
    apex, record_id, headers = record
    # Delete it; errors are ignored.
    async with httpx.AsyncClient(timeout=20) as client:
        with contextlib.suppress(Exception):
            await client.delete(
                f"https://api.digitalocean.com/v2/domains/{apex}/records/{record_id}",
                headers=headers,
            )


async def _dns01_porkbun(domain: str, txt_record: str, credentials: dict, log: AcmeLogCapture | None = None) -> None:
    """Create the DNS-01 TXT record in Porkbun and wait for it to propagate."""
    # Imported here so the module loads even where httpx is not needed.
    import httpx

    # API key and secret key are required.
    api_key = (credentials or {}).get("porkbun_api_key", "").strip()
    secret_key = (credentials or {}).get("porkbun_secret_key", "").strip()
    if not api_key or not secret_key:
        raise ValueError("Porkbun requires porkbun_api_key and porkbun_secret_key credentials")
    # Record name relative to the apex domain.
    subdomain, apex = _split_apex(domain)
    record_name = f"_acme-challenge.{subdomain}" if subdomain else "_acme-challenge"
    if log:
        log.append(f"Creating Porkbun TXT record {record_name} in {apex}")
    # Create the TXT record and get its id.
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(
            f"https://api.porkbun.com/api/json/v3/dns/create/{apex}",
            json={
                "apikey": api_key,
                "secretapikey": secret_key,
                "type": "TXT",
                "name": record_name,
                "content": txt_record,
                "ttl": "300",
            },
        )
        response.raise_for_status()
        payload = response.json() or {}
        if payload.get("status") != "SUCCESS":
            raise ValueError(f"Porkbun DNS update failed: {payload.get('message') or response.text.strip()}")
        record_id = str(payload.get("id") or "").strip()
        if not record_id:
            raise ValueError("Porkbun did not return a DNS record id")
    # Remember it for cleanup.
    _porkbun_records[(domain, txt_record)] = (apex, record_id)
    if log:
        log.append("Porkbun TXT record created; waiting 15 seconds for propagation")
    # Give DNS time to propagate.
    await asyncio.sleep(15)


async def _cleanup_porkbun_record(domain: str, txt_record: str, credentials: dict | None = None) -> None:
    """Delete the Porkbun TXT record created for this challenge, ignoring errors."""
    # Imported here so the module loads even where httpx is not needed.
    import httpx

    # Take the saved record for this challenge; stop if there is none.
    record = _porkbun_records.pop((domain, txt_record), None)
    # Porkbun needs the keys to delete too.
    api_key = (credentials or {}).get("porkbun_api_key", "").strip()
    secret_key = (credentials or {}).get("porkbun_secret_key", "").strip()
    if not record or not api_key or not secret_key:
        return
    apex, record_id = record
    # Delete it; errors are ignored.
    async with httpx.AsyncClient(timeout=20) as client:
        with contextlib.suppress(Exception):
            await client.post(
                f"https://api.porkbun.com/api/json/v3/dns/delete/{apex}/{record_id}",
                json={"apikey": api_key, "secretapikey": secret_key},
            )


async def _dns01_gcloud(domain: str, txt_record: str, credentials: dict, log: AcmeLogCapture | None = None) -> None:
    """Create the DNS-01 TXT record in Google Cloud DNS and wait for it to propagate."""
    # Imported here so the module loads even where httpx is not needed.
    import httpx

    # Get an access token and the zone.
    access_token, project_id, zone_name = await _get_gcloud_access_token(credentials)
    record_name = f"_acme-challenge.{domain}."
    if log:
        log.append(f"Creating Google Cloud DNS TXT record {record_name} in zone {zone_name}")
    # Add the TXT record with a change request.
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(
            f"https://dns.googleapis.com/dns/v1/projects/{project_id}/managedZones/{zone_name}/changes",
            headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
            json={
                "additions": [
                    {
                        "name": record_name,
                        "type": "TXT",
                        "ttl": 300,
                        "rrdatas": [f'"{txt_record}"'],
                    }
                ]
            },
        )
        response.raise_for_status()
    # Remember it for cleanup.
    _gcloud_records[(domain, txt_record)] = (project_id, zone_name, record_name)
    if log:
        log.append("Google Cloud DNS TXT record created; waiting 20 seconds for propagation")
    # Give DNS time to propagate.
    await asyncio.sleep(20)


async def _cleanup_gcloud_record(domain: str, txt_record: str, credentials: dict | None = None) -> None:
    """Delete the Google Cloud DNS TXT record created for this challenge, ignoring errors."""
    # Imported here so the module loads even where httpx is not needed.
    import httpx

    # Take the saved record for this challenge; stop if there is none.
    record = _gcloud_records.pop((domain, txt_record), None)
    if not record:
        return
    project_id, zone_name, record_name = record
    # Need a new access token; give up quietly if that fails.
    try:
        access_token, _, _ = await _get_gcloud_access_token(credentials or {})
    except Exception:
        return
    # Delete it with a change request; errors are ignored.
    async with httpx.AsyncClient(timeout=20) as client:
        with contextlib.suppress(Exception):
            await client.post(
                f"https://dns.googleapis.com/dns/v1/projects/{project_id}/managedZones/{zone_name}/changes",
                headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
                json={
                    "deletions": [
                        {
                            "name": record_name,
                            "type": "TXT",
                            "ttl": 300,
                            "rrdatas": [f'"{txt_record}"'],
                        }
                    ]
                },
            )


async def _dns01_dnsimple(domain: str, txt_record: str, credentials: dict, log: AcmeLogCapture | None = None) -> None:
    """Create the DNS-01 TXT record in DNSimple and wait for it to propagate."""
    # Imported here so the module loads even where httpx is not needed.
    import httpx

    # Token and account id are required.
    token = (credentials or {}).get("dnsimple_token", "").strip()
    account_id = str((credentials or {}).get("dnsimple_account_id", "")).strip()
    if not token or not account_id:
        raise ValueError("DNSimple requires dnsimple_token and dnsimple_account_id credentials")
    # Record name relative to the apex domain.
    subdomain, apex = _split_apex(domain)
    record_name = f"_acme-challenge.{subdomain}" if subdomain else "_acme-challenge"
    if log:
        log.append(f"Creating DNSimple TXT record {record_name} in {apex}")
    # Create the TXT record and get its id.
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(
            f"https://api.dnsimple.com/v2/{account_id}/zones/{apex}/records",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"name": record_name, "type": "TXT", "content": txt_record, "ttl": 120},
        )
        response.raise_for_status()
        record_id = int(((response.json() or {}).get("data") or {}).get("id") or 0)
        if not record_id:
            raise ValueError("DNSimple did not return a DNS record id")
    # Remember it for cleanup.
    _dnsimple_records[(domain, txt_record)] = (apex, record_id)
    if log:
        log.append("DNSimple TXT record created; waiting 15 seconds for propagation")
    # Give DNS time to propagate.
    await asyncio.sleep(15)


async def _cleanup_dnsimple_record(domain: str, txt_record: str, credentials: dict | None = None) -> None:
    """Delete the DNSimple TXT record created for this challenge, ignoring errors."""
    # Imported here so the module loads even where httpx is not needed.
    import httpx

    # Take the saved record for this challenge; stop if there is none.
    record = _dnsimple_records.pop((domain, txt_record), None)
    # DNSimple needs the token and account to delete too.
    token = (credentials or {}).get("dnsimple_token", "").strip()
    account_id = str((credentials or {}).get("dnsimple_account_id", "")).strip()
    if not record or not token or not account_id:
        return
    apex, record_id = record
    # Delete it; errors are ignored.
    async with httpx.AsyncClient(timeout=20) as client:
        with contextlib.suppress(Exception):
            await client.delete(
                f"https://api.dnsimple.com/v2/{account_id}/zones/{apex}/records/{record_id}",
                headers={"Authorization": f"Bearer {token}"},
            )


async def _dns01_azure(domain: str, txt_record: str, credentials: dict, log: AcmeLogCapture | None = None) -> None:
    """Create the DNS-01 TXT record in Azure DNS and wait for it to propagate.

    Uses a service principal (tenant, client id and secret) to get a token.
    """
    # Imported here so the module loads even where httpx is not needed.
    import httpx

    # All Azure details are required.
    tenant_id = (credentials or {}).get("azure_tenant_id", "").strip()
    client_id = (credentials or {}).get("azure_client_id", "").strip()
    client_secret = (credentials or {}).get("azure_client_secret", "").strip()
    subscription_id = (credentials or {}).get("azure_subscription_id", "").strip()
    resource_group = (credentials or {}).get("azure_resource_group", "").strip()
    zone_name = (credentials or {}).get("azure_zone_name", "").strip()
    if not all([tenant_id, client_id, client_secret, subscription_id, resource_group, zone_name]):
        raise ValueError("Azure DNS requires tenant, client, secret, subscription, resource group, and zone name credentials")

    # Record name relative to the zone, and the token URL.
    subdomain = _relative_subdomain(domain, zone_name)
    record_name = f"_acme-challenge.{subdomain}" if subdomain else "_acme-challenge"
    token_url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
    if log:
        log.append(f"Creating Azure DNS TXT record {record_name} in zone {zone_name}")
    # Get an access token for the Azure management API.
    async with httpx.AsyncClient(timeout=20) as client:
        token_response = await client.post(
            token_url,
            data={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
                "scope": "https://management.azure.com/.default",
            },
        )
        token_response.raise_for_status()
        access_token = (token_response.json() or {}).get("access_token", "")
        if not access_token:
            raise ValueError("Azure OAuth token exchange did not return an access token")
        # Create the TXT record set.
        response = await client.put(
            f"https://management.azure.com/subscriptions/{subscription_id}/resourceGroups/{resource_group}/providers/Microsoft.Network/dnsZones/{zone_name}/TXT/{record_name}?api-version=2018-05-01",
            headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
            json={"properties": {"TTL": 300, "TXTRecords": [{"value": [txt_record]}]}},
        )
        response.raise_for_status()
    # Remember it for cleanup.
    _azure_records[(domain, txt_record)] = (subscription_id, resource_group, zone_name, record_name)
    if log:
        log.append("Azure DNS TXT record created; waiting 20 seconds for propagation")
    # Give DNS time to propagate.
    await asyncio.sleep(20)


async def _cleanup_azure_record(domain: str, txt_record: str, credentials: dict | None = None) -> None:
    """Delete the Azure DNS TXT record created for this challenge, ignoring errors."""
    # Imported here so the module loads even where httpx is not needed.
    import httpx

    # Take the saved record for this challenge; stop if there is none.
    record = _azure_records.pop((domain, txt_record), None)
    # Azure needs the service principal to delete too.
    tenant_id = (credentials or {}).get("azure_tenant_id", "").strip()
    client_id = (credentials or {}).get("azure_client_id", "").strip()
    client_secret = (credentials or {}).get("azure_client_secret", "").strip()
    if not record or not tenant_id or not client_id or not client_secret:
        return
    subscription_id, resource_group, zone_name, record_name = record
    # Get a token and delete the record; errors are ignored.
    async with httpx.AsyncClient(timeout=20) as client:
        with contextlib.suppress(Exception):
            token_response = await client.post(
                f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token",
                data={
                    "grant_type": "client_credentials",
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "scope": "https://management.azure.com/.default",
                },
            )
            token_response.raise_for_status()
            access_token = (token_response.json() or {}).get("access_token", "")
            if not access_token:
                return
            await client.delete(
                f"https://management.azure.com/subscriptions/{subscription_id}/resourceGroups/{resource_group}/providers/Microsoft.Network/dnsZones/{zone_name}/TXT/{record_name}?api-version=2018-05-01",
                headers={"Authorization": f"Bearer {access_token}"},
            )


async def _dns01_route53(domain: str, txt_record: str, credentials: dict, log: AcmeLogCapture | None = None) -> None:
    """Create the DNS-01 TXT record in Route 53 and wait for it to propagate."""
    # Access key, secret key and hosted zone id are required.
    access_key = (credentials or {}).get("route53_access_key", "").strip()
    secret_key = (credentials or {}).get("route53_secret_key", "").strip()
    zone_id = (credentials or {}).get("route53_zone_id", "").strip()
    if not access_key or not secret_key or not zone_id:
        raise ValueError("Route53 requires route53_access_key, route53_secret_key, and route53_zone_id credentials")
    if log:
        log.append(f"Creating Route53 TXT record for _acme-challenge.{domain}.")
    # Upsert the TXT record and remember it for cleanup.
    await _route53_change_record(access_key, secret_key, zone_id, _build_route53_change_xml("UPSERT", domain, txt_record))
    _route53_records[(domain, txt_record)] = (zone_id, f"_acme-challenge.{domain}.")
    if log:
        log.append("Route53 TXT record created; waiting 20 seconds for propagation")
    await asyncio.sleep(20)


async def _cleanup_route53_record(domain: str, txt_record: str, credentials: dict | None = None) -> None:
    """Delete the Route 53 TXT record created for this challenge, ignoring errors."""
    # Take the saved record for this challenge; stop if there is none.
    record = _route53_records.pop((domain, txt_record), None)
    # Route 53 needs the keys to delete too; the zone comes from the record.
    access_key = (credentials or {}).get("route53_access_key", "").strip()
    secret_key = (credentials or {}).get("route53_secret_key", "").strip()
    zone_id = (record or ("", ""))[0] or (credentials or {}).get("route53_zone_id", "").strip()
    if not record or not access_key or not secret_key or not zone_id:
        return
    # Delete it; errors are ignored.
    with contextlib.suppress(Exception):
        await _route53_change_record(access_key, secret_key, zone_id, _build_route53_change_xml("DELETE", domain, txt_record))


async def _dns01_namecheap(domain: str, txt_record: str, credentials: dict, log: AcmeLogCapture | None = None) -> None:
    """Create the DNS-01 TXT record in Namecheap and wait for it to propagate.

    Namecheap can only replace all records at once, so the existing records are read first,
    the TXT record is added, and the originals are kept for cleanup.
    """
    # API key and username are required.
    api_key = (credentials or {}).get("namecheap_api_key", "").strip()
    username = (credentials or {}).get("namecheap_username", "").strip()
    if not api_key or not username:
        raise ValueError("Namecheap requires namecheap_api_key and namecheap_username credentials")
    # Split the apex into second-level and top-level parts.
    subdomain, apex = _split_apex(domain)
    sld, _, tld = apex.partition(".")
    if not sld or not tld:
        raise ValueError(f"Could not determine Namecheap SLD/TLD for {domain}")
    # Hub's public IP (Namecheap whitelists callers) and the existing records.
    client_ip = await _get_namecheap_client_ip(credentials)
    hosts = await _namecheap_get_hosts(username, api_key, client_ip, sld, tld)
    # Record name relative to the apex.
    record_name = f"_acme-challenge.{subdomain}" if subdomain else "_acme-challenge"
    if log:
        log.append(f"Creating Namecheap TXT record {record_name} for {apex}")
    # Write the existing records plus the TXT record.
    await _namecheap_set_hosts(
        username,
        api_key,
        client_ip,
        sld,
        tld,
        [*hosts, {"Name": record_name, "Type": "TXT", "Address": txt_record, "TTL": "300"}],
    )
    # Keep the original records for cleanup.
    _namecheap_records[(domain, txt_record)] = {"sld": sld, "tld": tld, "client_ip": client_ip, "hosts": hosts}
    if log:
        log.append("Namecheap TXT record created; waiting 20 seconds for propagation")
    # Give DNS time to propagate.
    await asyncio.sleep(20)


async def _cleanup_namecheap_record(domain: str, txt_record: str, credentials: dict | None = None) -> None:
    """Restore the Namecheap records saved before the challenge, ignoring errors."""
    # Take the saved record for this challenge; stop if there is none.
    record = _namecheap_records.pop((domain, txt_record), None)
    # Namecheap needs the key and username to restore too.
    api_key = (credentials or {}).get("namecheap_api_key", "").strip()
    username = (credentials or {}).get("namecheap_username", "").strip()
    if not record or not api_key or not username:
        return
    # Write back the original records; errors are ignored.
    with contextlib.suppress(Exception):
        await _namecheap_set_hosts(
            username,
            api_key,
            str(record.get("client_ip") or "").strip() or await _get_namecheap_client_ip(credentials or {}),
            str(record.get("sld") or "").strip(),
            str(record.get("tld") or "").strip(),
            list(record.get("hosts") or []),
        )


async def request_certificate(cfg: AcmeConfig, data_dir: Path) -> dict:
    """Get a certificate for cfg.domain using a DNS-01 challenge and install it.

    Writes cert.pem and key.pem in data_dir/tls, saves the result and log in the config,
    and keeps the in-memory status updated. Always returns a result dict with "success";
    errors are recorded rather than raised. DNS records are cleaned up afterwards.
    """
    # TLS folder and account key path.
    data_dir = Path(data_dir)
    tls_dir = data_dir / "tls"
    tls_dir.mkdir(parents=True, exist_ok=True)
    account_key_path = tls_dir / "acme_account.pem"
    # Cleanup state; server and token are only used by HTTP-01, which is no longer offered.
    server: asyncio.AbstractServer | None = None
    dns_cleanup: tuple[str, str, str] | None = None
    token: str | None = None
    # Log for this run.
    log = AcmeLogCapture()
    # New private key for the certificate.
    new_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    key_pem = new_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )

    # Mark the run as started.
    domain = cfg.domain.strip()
    _set_acme_status(running=True, status="running", last_result=None, last_error=None, last_log="", last_log_at="")
    log.append(
        f"Starting ACME certificate request for {domain or '(unset domain)'} "
        f"via {cfg.ca or 'letsencrypt'} using dns-01"
    )

    try:
        # Domain and DNS provider are required.
        if not domain:
            raise ValueError("Domain is required")
        if not cfg.dns_provider:
            raise ValueError("DNS provider is required for dns-01")

        # ACME library, imported only when needed.
        from acme import client, messages

        # Load or create the account key and connect to the CA.
        log.append("Loading ACME account key")
        account_key = _get_or_create_account_key(account_key_path)
        network = client.ClientNetwork(account_key, user_agent="webui-hub-acme")
        directory_url = _ca_directory(cfg.ca)
        log.append(f"Fetching ACME directory {directory_url}")
        directory = messages.Directory.from_json(network.get(directory_url).json())
        acme_client = client.ClientV2(directory, network)

        # Register the account (or reuse the existing one).
        contact = [f"mailto:{cfg.email.strip()}"] if cfg.email.strip() else ()
        log.append("Registering or reusing ACME account")
        await asyncio.to_thread(
            acme_client.new_account,
            messages.NewRegistration.from_data(email=contact, terms_of_service_agreed=True),
        )

        # Certificate request for the domain.
        log.append(f"Creating CSR for {domain}")
        csr = (
            x509.CertificateSigningRequestBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, domain)]))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(domain)]), critical=False)
            .sign(new_key, hashes.SHA256())
        )
        # Start an order.
        log.append("Creating ACME order")
        order = await asyncio.to_thread(acme_client.new_order, csr.public_bytes(serialization.Encoding.PEM))

        # Find the DNS-01 challenge and the domain it is for.
        challenge_body = None
        authz_domain = domain
        for authorization in getattr(order, "authorizations", []):
            identifier = getattr(getattr(authorization, "body", None), "identifier", None)
            if getattr(identifier, "value", ""):
                authz_domain = identifier.value
            for candidate in getattr(getattr(authorization, "body", None), "challenges", []):
                challenge_obj = getattr(candidate, "chall", candidate)
                if getattr(challenge_obj, "typ", "") == cfg.challenge:
                    challenge_body = candidate
                    break
            if challenge_body is not None:
                break
        if challenge_body is None:
            raise RuntimeError(f"No dns-01 challenge offered for {domain}")

        # The challenge response.
        challenge_obj = getattr(challenge_body, "chall", challenge_body)
        response = challenge_body.response(account_key) if hasattr(challenge_body, "response") else challenge_obj.response(account_key)

        # Create the TXT record with the chosen provider, noting it for cleanup.
        validation = challenge_body.validation(account_key) if hasattr(challenge_body, "validation") else challenge_obj.validation(account_key)
        if cfg.dns_provider == "cloudflare":
            await _dns01_cloudflare(authz_domain, validation, cfg.dns_credentials, log=log)
            dns_cleanup = (authz_domain, validation, "cloudflare")
        elif cfg.dns_provider == "hurricane_electric":
            await _dns01_hurricane_electric(authz_domain, validation, cfg.dns_credentials, log=log)
            dns_cleanup = (authz_domain, validation, "hurricane_electric")
        elif cfg.dns_provider == "godaddy":
            await _dns01_godaddy(authz_domain, validation, cfg.dns_credentials, log=log)
            dns_cleanup = (authz_domain, validation, "godaddy")
        elif cfg.dns_provider == "digitalocean":
            await _dns01_digitalocean(authz_domain, validation, cfg.dns_credentials, log=log)
            dns_cleanup = (authz_domain, validation, "digitalocean")
        elif cfg.dns_provider == "porkbun":
            await _dns01_porkbun(authz_domain, validation, cfg.dns_credentials, log=log)
            dns_cleanup = (authz_domain, validation, "porkbun")
        elif cfg.dns_provider == "gcloud":
            await _dns01_gcloud(authz_domain, validation, cfg.dns_credentials, log=log)
            dns_cleanup = (authz_domain, validation, "gcloud")
        elif cfg.dns_provider == "dnsimple":
            await _dns01_dnsimple(authz_domain, validation, cfg.dns_credentials, log=log)
            dns_cleanup = (authz_domain, validation, "dnsimple")
        elif cfg.dns_provider == "azure_dns":
            await _dns01_azure(authz_domain, validation, cfg.dns_credentials, log=log)
            dns_cleanup = (authz_domain, validation, "azure_dns")
        elif cfg.dns_provider == "route53":
            await _dns01_route53(authz_domain, validation, cfg.dns_credentials, log=log)
            dns_cleanup = (authz_domain, validation, "route53")
        elif cfg.dns_provider == "namecheap":
            await _dns01_namecheap(authz_domain, validation, cfg.dns_credentials, log=log)
            dns_cleanup = (authz_domain, validation, "namecheap")
        else:
            raise ValueError(f"Unsupported DNS provider: {cfg.dns_provider}")

        # Answer the challenge and wait up to 60 seconds for the certificate.
        log.append("Answering ACME challenge")
        await asyncio.to_thread(acme_client.answer_challenge, challenge_body, response)
        deadline = _utcnow() + timedelta(seconds=60)
        log.append("Waiting for ACME validation and certificate finalization")
        order = await asyncio.to_thread(acme_client.poll_and_finalize, order, deadline)
        fullchain_pem = getattr(order, "fullchain_pem", "")
        if not fullchain_pem:
            raise RuntimeError("ACME CA did not return a certificate chain")

        # Write the certificate chain and key.
        cert_path = tls_dir / "cert.pem"
        key_path = tls_dir / "key.pem"
        log.append(f"Writing certificate to {cert_path}")
        cert_path.write_text(fullchain_pem, encoding="utf-8")
        key_path.write_bytes(key_pem)

        # Record success in the config and status.
        cert_info = _get_cert_info(cert_path)
        cfg.last_renewed = _iso(_utcnow())
        cfg.cert_expiry = cert_info.get("expires", "")
        cfg.last_error = ""
        log.append(f"Certificate issued successfully; expires {cfg.cert_expiry or 'unknown'}")
        cfg.last_log = log.text()
        cfg.last_log_at = _iso(_utcnow())
        save_acme_config(cfg)
        result = {"success": True, "expires": cfg.cert_expiry, "domain": cert_info.get("domain") or domain}
        _set_acme_status(
            running=False,
            status="success",
            last_result=result,
            last_error=None,
            last_log=cfg.last_log,
            last_log_at=cfg.last_log_at,
        )
        return result
    # Failure: record the error in the config and status.
    except Exception as exc:
        log.append_exception(exc)
        cfg.last_error = str(exc)
        cfg.last_log = log.text()
        cfg.last_log_at = _iso(_utcnow())
        save_acme_config(cfg)
        logger.exception("ACME certificate request failed for %s", cfg.domain)
        result = {"success": False, "error": str(exc)}
        _set_acme_status(
            running=False,
            status="failed",
            last_result=result,
            last_error=str(exc),
            last_log=cfg.last_log,
            last_log_at=cfg.last_log_at,
        )
        return result
    # Always clean up.
    finally:
        # Stop the HTTP-01 server if one was started.
        if server is not None:
            log.append("Stopping HTTP-01 challenge server")
            server.close()
            with contextlib.suppress(Exception):
                await server.wait_closed()
        # Remove any HTTP-01 token.
        if token:
            _challenge_store().pop(token, None)
        # Remove the DNS record with the provider that created it.
        if dns_cleanup:
            domain_c, txt_c, provider_c = dns_cleanup
            if provider_c == "hurricane_electric":
                log.append(f"Cleaning up Hurricane Electric TXT record for {domain_c}")
                await _cleanup_he_record(domain_c, txt_c, (cfg.dns_credentials or {}).get("he_ddns_key", ""))
            elif provider_c == "cloudflare":
                log.append(f"Cleaning up Cloudflare TXT record for {domain_c}")
                await _cleanup_cloudflare_record(domain_c, txt_c)
            elif provider_c == "godaddy":
                log.append(f"Cleaning up GoDaddy TXT record for {domain_c}")
                await _cleanup_godaddy_record(domain_c, txt_c)
            elif provider_c == "digitalocean":
                log.append(f"Cleaning up DigitalOcean TXT record for {domain_c}")
                await _cleanup_do_record(domain_c, txt_c)
            elif provider_c == "porkbun":
                log.append(f"Cleaning up Porkbun TXT record for {domain_c}")
                await _cleanup_porkbun_record(domain_c, txt_c, cfg.dns_credentials)
            elif provider_c == "gcloud":
                log.append(f"Cleaning up Google Cloud DNS TXT record for {domain_c}")
                await _cleanup_gcloud_record(domain_c, txt_c, cfg.dns_credentials)
            elif provider_c == "dnsimple":
                log.append(f"Cleaning up DNSimple TXT record for {domain_c}")
                await _cleanup_dnsimple_record(domain_c, txt_c, cfg.dns_credentials)
            elif provider_c == "azure_dns":
                log.append(f"Cleaning up Azure DNS TXT record for {domain_c}")
                await _cleanup_azure_record(domain_c, txt_c, cfg.dns_credentials)
            elif provider_c == "route53":
                log.append(f"Cleaning up Route53 TXT record for {domain_c}")
                await _cleanup_route53_record(domain_c, txt_c, cfg.dns_credentials)
            elif provider_c == "namecheap":
                log.append(f"Cleaning up Namecheap TXT record for {domain_c}")
                await _cleanup_namecheap_record(domain_c, txt_c, cfg.dns_credentials)
        # Save the final log if it changed.
        if cfg.last_log != log.text():
            cfg.last_log = log.text()
            cfg.last_log_at = _iso(_utcnow())
            save_acme_config(cfg)
            _set_acme_status(last_log=cfg.last_log, last_log_at=cfg.last_log_at)


async def renew_if_needed(data_dir: Path) -> bool:
    """Request a certificate if ACME is on and there is none or it expires within 30 days.

    Returns True if a new certificate was issued.
    """
    # Load the config; nothing to do if ACME is off.
    data_dir = Path(data_dir)
    cfg = _load_acme_config(_cfg_path(data_dir))
    if not cfg.enabled:
        return False
    # No certificate yet: request one.
    cert_info = _get_cert_info(_cert_path(data_dir))
    if cert_info.get("source") == "none":
        result = await request_certificate(cfg, data_dir)
        return bool(result.get("success"))
    # Renew if it expires within 30 days.
    expires = cert_info.get("expires")
    if not expires:
        return False
    expiry_dt = datetime.fromisoformat(expires.replace("Z", "+00:00"))
    if expiry_dt - _utcnow() < timedelta(days=30):
        result = await request_certificate(cfg, data_dir)
        return bool(result.get("success"))
    return False
