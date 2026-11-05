"""ACME certificate management for the standalone spoke server.

Challenge types:
  http-01  — Spoke temporarily serves challenge tokens at /.well-known/acme-challenge/
              via an in-process asyncio HTTP server on port 80.
  dns-01   — Spoke calls DNS provider API to create TXT record.
              Supported: cloudflare (full), hurricane_electric (full),
              azure_dns (stub), route53 (stub).

Hurricane Electric DNS setup:
  1. In dns.he.net, create a TXT record named _acme-challenge.<domain>
  2. Enable DDNS on that record — HE generates a DDNS key
  3. Enter that key as the credential — the hostname is derived automatically
"""
# Postpone annotation evaluation so hints like josepy.JWKRSA need no runtime import.
from __future__ import annotations

# asyncio runs the temporary HTTP challenge server and pushes blocking ACME calls to threads.
import asyncio
# contextlib.suppress ignores errors during best-effort cleanup.
import contextlib
# importlib loads the third-party "acme" package under a private alias (see _load_external_acme).
import importlib
import importlib.util
# json reads and writes the acme.json settings file.
import json
# logging records ACME failures and warnings.
import logging
# sys gives access to sys.path and already-loaded modules.
import sys
# dataclass helpers define and serialise the AcmeConfig settings object.
from dataclasses import asdict, dataclass, field
# datetime helpers compute certificate expiry and timestamps in UTC.
from datetime import datetime, timedelta, timezone
# Path builds file paths relative to this module.
from pathlib import Path
# Any is used in type hints for loosely-typed JSON dicts.
from typing import Any

# cryptography builds keys and CSRs and reads certificate fields.
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import load_pem_private_key
from cryptography.x509.oid import NameOID

# One named logger for all ACME messages.
logger = logging.getLogger("Acme")
# Settings, the served TLS cert and the ACME account key all live beside this module.
BASE_DIR = Path(__file__).resolve().parent
_CFG_PATH = BASE_DIR / "acme.json"
_CERT_PATH = BASE_DIR / "tls" / "cert.pem"
_ACCOUNT_KEY_PATH = BASE_DIR / "tls" / "acme_account.pem"
# Marks a stored credential value as Fernet-encrypted.
_ENC_PREFIX = "enc:"
# ACME directory URLs of the supported certificate authorities.
_CA_DIRECTORY = {
    "letsencrypt": "https://acme-v02.api.letsencrypt.org/directory",
    "letsencrypt_staging": "https://acme-staging-v02.api.letsencrypt.org/directory",
    "zerossl": "https://acme.zerossl.com/v2/DV90",
}
# Remembers Cloudflare TXT records we created so they can be deleted after validation:
# (domain, txt) -> (zone id, record id, auth headers).
_cloudflare_records: dict[tuple[str, str], tuple[str, str, dict[str, str]]] = {}
_he_records: dict[tuple[str, str], str] = {}  # (domain, txt_record) → acme-challenge hostname
# True once the external python-acme package has been imported under its alias.
_ext_acme_loaded = False


@dataclass
class AcmeConfig:
    """Persisted ACME settings plus status fields shown in the UI."""
    # Whether automatic issuance/renewal is turned on.
    enabled: bool = False
    # Domain name to put on the certificate and the contact e-mail for the CA account.
    domain: str = ""
    email: str = ""
    # Which ACME challenge to use: "http-01" or "dns-01".
    challenge: str = "http-01"
    # Key into _CA_DIRECTORY choosing the certificate authority.
    ca: str = "letsencrypt"
    # dns-01 only: DNS provider name and its API credentials (stored encrypted on disk).
    dns_provider: str = ""
    dns_credentials: dict = field(default_factory=dict)
    # Status fields written after each attempt: last success, last error and cert expiry.
    last_renewed: str = ""
    last_error: str = ""
    cert_expiry: str = ""


def _utcnow() -> datetime:
    """Return the current time as a timezone-aware UTC datetime."""
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None) -> str:
    """Format a datetime as an ISO-8601 UTC string ending in Z, or "" when missing."""
    # No datetime given: return an empty string for the UI.
    if not dt:
        return ""
    # Normalise to UTC and use the short "Z" suffix instead of "+00:00".
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _fernet():
    """Return a Fernet cipher using the spoke's secret key, or None when encryption is unavailable.
    """
    # The cryptography Fernet module may be missing; without it secrets are stored as-is.
    try:
        from cryptography.fernet import Fernet
    except Exception:
        return None
    # The secret key file is shared with the rest of the spoke; no key means no encryption.
    key_file = BASE_DIR / ".secret_key"
    if not key_file.exists():
        return None
    # A corrupt key file disables encryption instead of crashing.
    try:
        return Fernet(key_file.read_bytes().strip())
    except Exception:
        return None


def _encrypt_secret(value: str) -> str:
    """Encrypt a credential value and tag it with the enc: prefix, if encryption is available."""
    # Leave empty values and keys-not-available cases unchanged.
    fernet = _fernet()
    if not fernet or not value:
        return value
    return _ENC_PREFIX + fernet.encrypt(value.encode()).decode()


def _decrypt_secret(value: str) -> str:
    """Decrypt an enc:-prefixed value; plain values pass through and bad ciphertext becomes ""."""
    # Values without the prefix were never encrypted.
    if not value or not value.startswith(_ENC_PREFIX):
        return value
    # Encrypted value but no key available: return it unchanged.
    fernet = _fernet()
    if not fernet:
        return value
    # Decrypt; failure (e.g. the key changed) returns "" so a bad secret is never used.
    try:
        return fernet.decrypt(value[len(_ENC_PREFIX):].encode()).decode()
    except Exception:
        logger.warning("Failed to decrypt ACME secret field")
        return ""


def _encrypt_dict(data: dict[str, Any]) -> dict[str, Any]:
    """Encrypt every value in a credentials dict, turning None/empty values into ""."""
    return {key: _encrypt_secret(str(value)) if value not in (None, "") else "" for key, value in data.items()}


def _decrypt_dict(data: dict[str, Any]) -> dict[str, Any]:
    """Decrypt every value in a credentials dict, turning None/empty values into ""."""
    return {key: _decrypt_secret(str(value)) if value not in (None, "") else "" for key, value in data.items()}


def _challenge_store() -> dict[str, str]:
    """Return the server module's shared token -> key-authorization dict used for http-01."""
    # Look the dict up on the already-imported server module to avoid a circular import.
    server = sys.modules.get("server")
    store = getattr(server, "_acme_challenges", None)
    # Fall back to a throwaway dict when server is not loaded (e.g. in tests).
    return store if isinstance(store, dict) else {}


def _load_external_acme() -> None:
    """Import the third-party python-acme package as _ext_acme.

    This file is itself named acme.py, so a plain "import acme" would import this module. We
    search sys.path for the real package and load it under a different name.
    """
    # Declared global because we flip the module-level "already loaded" flag.
    global _ext_acme_loaded
    # Only load once per process.
    if _ext_acme_loaded:
        return
    # Look in every import directory for a package named acme that is not this file.
    for entry in list(sys.path):
        candidate = Path(entry) / "acme" / "__init__.py"
        if candidate.exists() and candidate.resolve() != Path(__file__).resolve():
            # Build an import spec for the package so its submodules resolve inside it.
            spec = importlib.util.spec_from_file_location(
                "_ext_acme",
                candidate,
                submodule_search_locations=[str(candidate.parent)],
            )
            # Register and execute the module under the alias, then remember it is loaded.
            if spec and spec.loader:
                module = importlib.util.module_from_spec(spec)
                sys.modules["_ext_acme"] = module
                spec.loader.exec_module(module)
                _ext_acme_loaded = True
                return
    # No suitable package was found anywhere on sys.path.
    raise ImportError("The python-acme package is not installed")


def _serialize_config(cfg: AcmeConfig, *, encrypt_credentials: bool) -> dict[str, Any]:
    """Convert an AcmeConfig to a plain dict, optionally encrypting the DNS credentials."""
    # Turn the dataclass into a dict, then replace credentials with an encrypted or plain copy.
    data = asdict(cfg)
    data["dns_credentials"] = _encrypt_dict(cfg.dns_credentials or {}) if encrypt_credentials else dict(cfg.dns_credentials or {})
    return data


def load_acme_config() -> AcmeConfig:
    """Load the ACME settings from acme.json, returning defaults if missing or unreadable."""
    # Nothing saved yet: use defaults.
    if not _CFG_PATH.exists():
        return AcmeConfig()
    # A corrupt or unreadable file is logged and treated as "no settings".
    try:
        raw = json.loads(_CFG_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("Failed to read ACME config %s: %s", _CFG_PATH, exc)
        return AcmeConfig()
    # Decrypt stored credentials; anything that is not a dict is discarded.
    creds = raw.get("dns_credentials") or {}
    raw["dns_credentials"] = _decrypt_dict(creds) if isinstance(creds, dict) else {}
    # Only pass known fields to the dataclass so stale keys in the file cannot break loading.
    allowed = {field.name for field in AcmeConfig.__dataclass_fields__.values()}
    return AcmeConfig(**{key: raw.get(key) for key in allowed if key in raw})


def save_acme_config(cfg: AcmeConfig) -> None:
    """Write the ACME settings to acme.json with DNS credentials encrypted."""
    # Ensure the folder exists, then write pretty-printed JSON.
    _CFG_PATH.parent.mkdir(parents=True, exist_ok=True)
    _CFG_PATH.write_text(json.dumps(_serialize_config(cfg, encrypt_credentials=True), indent=2), encoding="utf-8")


def get_cert_info() -> dict:
    """Describe the cert the spoke currently serves: domain, issuer, expiry and source."""
    # No cert file at all.
    if not _CERT_PATH.exists():
        return {"source": "none"}
    # Parse the PEM certificate.
    cert = x509.load_pem_x509_certificate(_CERT_PATH.read_bytes())
    # Read the common name; certificates without one report an empty domain.
    try:
        domain = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value
    except IndexError:
        domain = ""
    # Prefer the issuer's organisation name, falling back to its common name.
    issuer = ""
    for oid in (NameOID.ORGANIZATION_NAME, NameOID.COMMON_NAME):
        attrs = cert.issuer.get_attributes_for_oid(oid)
        if attrs:
            issuer = attrs[0].value
            break
    # Expiry as an aware UTC datetime and whole days left (never negative).
    expires = cert.not_valid_after.replace(tzinfo=timezone.utc)
    days_remaining = max(0, int((expires - _utcnow()).total_seconds() // 86400))
    # Issuer equal to subject means self-signed; otherwise it was supplied externally.
    is_self_signed = cert.issuer == cert.subject
    source = "self-signed" if is_self_signed else "custom"
    # If ACME last issued a cert for this same domain, report the source as acme.
    cfg = load_acme_config()
    if cfg.last_renewed and cfg.domain and cfg.domain == domain:
        source = "acme"
    # Summary for the settings page.
    return {
        "domain": domain,
        "issuer": issuer,
        "expires": _iso(expires),
        "days_remaining": days_remaining,
        "is_self_signed": is_self_signed,
        "source": source,
    }


def _ca_directory(ca: str) -> str:
    """Return the ACME directory URL for a CA name, defaulting to Let's Encrypt."""
    return _CA_DIRECTORY.get(ca, _CA_DIRECTORY["letsencrypt"])


def _get_or_create_account_key(key_path: Path) -> josepy.JWKRSA:
    """Load the ACME account key from disk, creating and saving a new RSA key the first time."""
    # josepy wraps the key in the JWK format the ACME client needs.
    from josepy import JWKRSA

    # Make sure the tls folder exists.
    key_path.parent.mkdir(parents=True, exist_ok=True)
    # Reuse the existing account key so the CA account stays the same.
    if key_path.exists():
        private_key = load_pem_private_key(key_path.read_bytes(), password=None)
    else:
        # First run: generate a 2048-bit RSA key and save it as unencrypted PEM.
        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        key_path.write_bytes(
            private_key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.TraditionalOpenSSL,
                encryption_algorithm=serialization.NoEncryption(),
            )
        )
    # Wrap the key for the ACME client.
    return JWKRSA(key=private_key)


async def _serve_http01_challenge(token: str, key_authorization: str, port: int = 80) -> asyncio.AbstractServer:
    """Start a tiny HTTP server that answers the http-01 challenge for one token.

    Returns: the running asyncio server; the caller must close it.
    """
    # Also publish the token in the shared store so the main web server can answer too.
    store = _challenge_store()
    store[token] = key_authorization
    # The exact request line the CA will send for this token.
    expected = f"GET /.well-known/acme-challenge/{token} HTTP/1.1".encode()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Answer one HTTP connection: the key authorization for the token, 404 for anything else.
        """
        # Default to 404 with an empty body.
        body = b""
        status = b"404 Not Found"
        # Read the request headers (5 s limit) and accept HTTP/1.1 or HTTP/1.0 for our token.
        try:
            request = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
            if request.startswith(expected) or request.startswith(expected.replace(b"HTTP/1.1", b"HTTP/1.0")):
                body = key_authorization.encode()
                status = b"200 OK"
        except Exception:
            # Malformed or slow requests just get the 404.
            pass
        finally:
            # Always send a response and close the connection, ignoring errors from a vanished
            # client.
            # Minimal HTTP response headers; the two empty entries produce the blank line before the
            # body.
            headers = [
                b"HTTP/1.1 " + status,
                b"Content-Type: text/plain; charset=utf-8",
                f"Content-Length: {len(body)}".encode(),
                b"Connection: close",
                b"",
                b"",
            ]
            writer.write(b"\r\n".join(headers) + body)
            with contextlib.suppress(Exception):
                await writer.drain()
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    # Listen on all interfaces so the CA can reach the challenge from the internet.
    return await asyncio.start_server(handle, host="0.0.0.0", port=port)


async def _dns01_cloudflare(domain: str, txt_record: str, credentials: dict) -> None:
    """Create the _acme-challenge TXT record through the Cloudflare API."""
    # httpx is only needed for dns-01, so import it here.
    import httpx

    # Read the supported credential styles: an API token, or an e-mail plus global API key.
    token = (credentials or {}).get("cf_api_token", "").strip()
    email = (credentials or {}).get("cf_email", "").strip()
    api_key = (credentials or {}).get("cf_api_key", "").strip()
    # Choose the auth headers that match the supplied credentials.
    if token:
        headers = {"Authorization": f"Bearer {token}"}
    elif email and api_key:
        headers = {"X-Auth-Email": email, "X-Auth-Key": api_key}
    else:
        raise ValueError("Cloudflare credentials must include cf_api_token or cf_email + cf_api_key")
    # Every Cloudflare API request sends JSON.
    headers["Content-Type"] = "application/json"

    # Prepare to discover which Cloudflare zone owns this domain.
    labels = domain.split(".")
    zone_id = ""
    zone_name = ""
    # One HTTP client for all Cloudflare calls.
    async with httpx.AsyncClient(timeout=20) as client:
        # Try parent domains from longest to shortest until Cloudflare knows the zone.
        for index in range(1, len(labels)):
            candidate = ".".join(labels[index:])
            response = await client.get("https://api.cloudflare.com/client/v4/zones", params={"name": candidate}, headers=headers)
            response.raise_for_status()
            payload = response.json()
            result = payload.get("result") or []
            if result:
                zone_id = result[0]["id"]
                zone_name = candidate
                break
        # No zone matched: we cannot create the record.
        if not zone_id:
            raise ValueError(f"Could not determine Cloudflare zone for {domain}")
        # TXT record name for the challenge.
        record_name = f"_acme-challenge.{domain}" if domain != zone_name else f"_acme-challenge.{zone_name}"
        # Create the TXT record with a short TTL.
        response = await client.post(
            f"https://api.cloudflare.com/client/v4/zones/{zone_id}/dns_records",
            headers=headers,
            json={"type": "TXT", "name": record_name, "content": txt_record, "ttl": 120},
        )
        # Fail if Cloudflare rejected it, otherwise remember the record id for cleanup.
        response.raise_for_status()
        result = response.json().get("result") or {}
        record_id = result.get("id")
        if not record_id:
            raise ValueError("Cloudflare did not return a DNS record id")
        _cloudflare_records[(domain, txt_record)] = (zone_id, record_id, headers)
    # Give DNS a few seconds to propagate before the CA checks the record.
    await asyncio.sleep(10)


async def _cleanup_cloudflare_record(domain: str, txt_record: str) -> None:
    """Delete a TXT record previously created by _dns01_cloudflare (best effort)."""
    import httpx

    # Look up and forget the record; nothing to do if we never created it.
    record = _cloudflare_records.pop((domain, txt_record), None)
    if not record:
        return
    zone_id, record_id, headers = record
    # Delete it; errors are ignored because a leftover TXT record is harmless.
    async with httpx.AsyncClient(timeout=20) as client:
        with contextlib.suppress(Exception):
            await client.delete(
                f"https://api.cloudflare.com/client/v4/zones/{zone_id}/dns_records/{record_id}",
                headers=headers,
            )


async def _dns01_hurricane_electric(domain: str, txt_record: str, credentials: dict) -> None:
    """Set _acme-challenge TXT record via Hurricane Electric DDNS API.

    Prerequisites (one-time setup in dns.he.net):
      1. Create a TXT record named _acme-challenge.<domain>
      2. Enable DDNS on that record — HE generates a DDNS key
      3. Supply that key as ``he_ddns_key`` in credentials
    """
    import httpx

    # The DDNS key is the only credential HE needs.
    ddns_key = (credentials or {}).get("he_ddns_key", "").strip()
    if not ddns_key:
        raise ValueError("Hurricane Electric DNS requires he_ddns_key credential")

    # HE updates the record by its full hostname.
    challenge_hostname = f"_acme-challenge.{domain}"
    # Send the DDNS update with the new TXT value.
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(
            "https://dyn.dns.he.net/nic/update",
            data={"hostname": challenge_hostname, "password": ddns_key, "txt": txt_record},
        )
        # Treat HTTP errors or a "badauth" reply as failures.
        body = response.text.strip()
        if response.status_code not in (200, 204) or body.startswith("badauth"):
            raise ValueError(f"HE DNS update failed ({response.status_code}): {body}")
        # HE answers "good" when it changed the value and "nochg" when it already matched.
        if not (body.startswith("good") or body.startswith("nochg")):
            raise ValueError(f"HE DNS unexpected response: {body}")

    # Remember the hostname so the record can be cleared afterwards.
    _he_records[(domain, txt_record)] = challenge_hostname
    # Allow time for DNS propagation before CA checks
    await asyncio.sleep(15)


async def _cleanup_he_record(domain: str, txt_record: str, ddns_key: str = "") -> None:
    """Clear the _acme-challenge TXT record after validation."""
    import httpx

    # Forget the record; without an entry or a key there is nothing we can clear.
    entry = _he_records.pop((domain, txt_record), None)
    if not entry or not ddns_key:
        return
    # Blank the TXT value; errors are ignored because this is cleanup only.
    async with httpx.AsyncClient(timeout=20) as client:
        with contextlib.suppress(Exception):
            await client.post(
                "https://dyn.dns.he.net/nic/update",
                data={"hostname": entry, "password": ddns_key, "txt": ""},
            )


async def _dns01_azure(domain: str, txt_record: str, credentials: dict) -> None:
    """Placeholder for Azure DNS; not implemented yet, so it always raises."""
    logger.warning("Azure DNS provider not yet implemented")
    raise NotImplementedError("Azure DNS provider not yet implemented")


async def _dns01_route53(domain: str, txt_record: str, credentials: dict) -> None:
    """Placeholder for AWS Route53; not implemented yet, so it always raises."""
    logger.warning("Route53 provider not yet implemented")
    raise NotImplementedError("Route53 provider not yet implemented")


async def request_certificate(cfg: AcmeConfig, data_dir: Path) -> dict:
    """Run a full ACME issuance for cfg.domain and install the cert and key.

    Returns: {"success": True, "expires", "domain"} or {"success": False, "error"}. The outcome
    is also saved into acme.json.
    """
    # Make sure the tls folder exists inside the data dir.
    data_dir = Path(data_dir)
    tls_dir = data_dir / "tls"
    tls_dir.mkdir(parents=True, exist_ok=True)
    # Track resources that the finally block must clean up.
    server: asyncio.AbstractServer | None = None
    dns_cleanup: tuple[str, str, str] | None = None
    token: str | None = None
    # Fresh 2048-bit private key for the new certificate, serialised as unencrypted PEM.
    new_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    key_pem = new_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )

    # Any failure below is recorded in cfg.last_error and returned instead of raised.
    try:
        # Validate the settings before contacting the CA.
        if not cfg.domain.strip():
            raise ValueError("Domain is required")
        if cfg.challenge not in {"http-01", "dns-01"}:
            raise ValueError("Challenge must be http-01 or dns-01")
        if cfg.challenge == "dns-01" and not cfg.dns_provider:
            raise ValueError("DNS provider is required for dns-01")

        # Load the third-party ACME client modules.
        _load_external_acme()
        client = importlib.import_module("_ext_acme.client")
        messages = importlib.import_module("_ext_acme.messages")

        # Connect to the chosen CA with our account key.
        account_key = _get_or_create_account_key(_ACCOUNT_KEY_PATH)
        network = client.ClientNetwork(account_key, user_agent="webui-spoke-acme")
        directory = messages.Directory.from_json(network.get(_ca_directory(cfg.ca)).json())
        acme_client = client.ClientV2(directory, network)

        # Register (or re-use) the ACME account; blocking calls run in a worker thread.
        contact = [f"mailto:{cfg.email.strip()}"] if cfg.email.strip() else ()
        await asyncio.to_thread(
            acme_client.new_account,
            messages.NewRegistration.from_data(email=contact, terms_of_service_agreed=True),
        )

        # Build a CSR for the domain, signed with the new certificate key.
        csr = (
            x509.CertificateSigningRequestBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cfg.domain.strip())]))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(cfg.domain.strip())]), critical=False)
            .sign(new_key, hashes.SHA256())
        )
        # Ask the CA for a new order covering the CSR.
        order = await asyncio.to_thread(acme_client.new_order, csr.public_bytes(serialization.Encoding.PEM))

        # Find the challenge of the configured type among the order's authorizations.
        challenge_body = None
        authz_domain = cfg.domain.strip()
        for authorization in getattr(order, "authorizations", []):
            # Remember the domain name the authorization is for.
            identifier = getattr(getattr(authorization, "body", None), "identifier", None)
            if getattr(identifier, "value", ""):
                authz_domain = identifier.value
            # Look through the offered challenges for one matching our type.
            for candidate in getattr(getattr(authorization, "body", None), "challenges", []):
                challenge_obj = getattr(candidate, "chall", candidate)
                if getattr(challenge_obj, "typ", "") == cfg.challenge:
                    challenge_body = candidate
                    break
            if challenge_body is not None:
                break
        # The CA did not offer the configured challenge type.
        if challenge_body is None:
            raise RuntimeError(f"No {cfg.challenge} challenge offered for {cfg.domain}")

        # Compute the response object we will send back to the CA.
        challenge_obj = getattr(challenge_body, "chall", challenge_body)
        response = challenge_body.response(account_key) if hasattr(challenge_body, "response") else challenge_obj.response(account_key)

        # http-01: serve the key authorization on port 80 for the CA to fetch.
        if cfg.challenge == "http-01":
            token = challenge_obj.token.decode() if isinstance(challenge_obj.token, bytes) else str(challenge_obj.token)
            validation = challenge_body.validation(account_key) if hasattr(challenge_body, "validation") else challenge_obj.validation(account_key)
            server = await _serve_http01_challenge(token, validation, port=80)
        else:
            # dns-01: publish the validation value as a TXT record with the configured provider.
            validation = challenge_body.validation(account_key) if hasattr(challenge_body, "validation") else challenge_obj.validation(account_key)
            # Remember which providers created records so the finally block can remove them.
            if cfg.dns_provider == "cloudflare":
                await _dns01_cloudflare(authz_domain, validation, cfg.dns_credentials)
                dns_cleanup = (authz_domain, validation, "cloudflare")
            elif cfg.dns_provider == "hurricane_electric":
                await _dns01_hurricane_electric(authz_domain, validation, cfg.dns_credentials)
                dns_cleanup = (authz_domain, validation, "hurricane_electric")
            elif cfg.dns_provider == "azure_dns":
                await _dns01_azure(authz_domain, validation, cfg.dns_credentials)
            elif cfg.dns_provider == "route53":
                await _dns01_route53(authz_domain, validation, cfg.dns_credentials)
            else:
                # Unknown provider name.
                raise ValueError(f"Unsupported DNS provider: {cfg.dns_provider}")

        # Tell the CA we are ready, then wait up to 60 s for validation and the signed cert.
        await asyncio.to_thread(acme_client.answer_challenge, challenge_body, response)
        deadline = _utcnow() + timedelta(seconds=60)
        order = await asyncio.to_thread(acme_client.poll_and_finalize, order, deadline)
        # The full chain PEM is the certificate plus intermediates.
        fullchain_pem = getattr(order, "fullchain_pem", "")
        if not fullchain_pem:
            raise RuntimeError("ACME CA did not return a certificate chain")

        # Install the new cert and key where the spoke's TLS setup reads them.
        _CERT_PATH.write_text(fullchain_pem, encoding="utf-8")
        (_CERT_PATH.parent / "key.pem").write_bytes(key_pem)
        # Record the success and new expiry date in the settings file.
        cert_info = get_cert_info()
        cfg.last_renewed = _iso(_utcnow())
        cfg.cert_expiry = cert_info.get("expires", "")
        cfg.last_error = ""
        save_acme_config(cfg)
        return {"success": True, "expires": cfg.cert_expiry, "domain": cert_info.get("domain") or cfg.domain}
    except Exception as exc:
        # Any failure: save the error for the UI, log the traceback, and report failure.
        cfg.last_error = str(exc)
        save_acme_config(cfg)
        logger.exception("ACME certificate request failed for %s", cfg.domain)
        return {"success": False, "error": str(exc)}
    finally:
        # Always stop the challenge server, forget the token and remove DNS records.
        if server is not None:
            server.close()
            with contextlib.suppress(Exception):
                await server.wait_closed()
        # Remove the http-01 token from the shared store.
        if token:
            _challenge_store().pop(token, None)
        # Remove the TXT record from whichever DNS provider created it.
        if dns_cleanup:
            domain_c, txt_c, provider_c = dns_cleanup
            if provider_c == "hurricane_electric":
                await _cleanup_he_record(domain_c, txt_c, (cfg.dns_credentials or {}).get("he_ddns_key", ""))
            else:
                await _cleanup_cloudflare_record(domain_c, txt_c)


async def renew_if_needed(data_dir: Path) -> bool:
    """Request a certificate if ACME is enabled and the cert is missing or near expiry.

    Returns: True if a new certificate was installed.
    """
    # Do nothing while ACME is disabled.
    cfg = load_acme_config()
    if not cfg.enabled:
        return False
    # No certificate at all: request one now.
    cert_info = get_cert_info()
    if cert_info.get("source") == "none":
        result = await request_certificate(cfg, data_dir)
        return bool(result.get("success"))
    # Without an expiry date we cannot decide, so do nothing.
    expires = cert_info.get("expires")
    if not expires:
        return False
    # Renew when fewer than 30 days remain.
    expiry_dt = datetime.fromisoformat(expires.replace("Z", "+00:00"))
    if expiry_dt - _utcnow() < timedelta(days=30):
        result = await request_certificate(cfg, data_dir)
        return bool(result.get("success"))
    return False
