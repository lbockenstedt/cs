"""TLS certificate generation for the hub server."""
# Allow ``tuple[Path, Path]`` style hints on older Python versions.
from __future__ import annotations

# datetime sets the certificate validity window; ipaddress builds the 127.0.0.1 SAN entry.
import datetime
import ipaddress
# logging reports cert generation; socket gives the machine hostname for the cert.
import logging
import socket
# Path is used for certificate/key file locations.
from pathlib import Path

# cryptography library pieces: X.509 cert builder, hashing, PEM serialisation and RSA keys.
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
# NameOID provides the standard field identifiers (CN, O) for the certificate subject.
from cryptography.x509.oid import NameOID

# Module logger for TLS messages.
logger = logging.getLogger(__name__)


def generate_self_signed(cert_path: Path, key_path: Path) -> None:
    """Generate a self-signed TLS certificate valid for 10 years."""
    # os is only needed here to read HUB_HOSTNAME.
    import os
    # Make sure the destination folders exist before writing files.
    cert_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.parent.mkdir(parents=True, exist_ok=True)

    # New 2048-bit RSA key; 65537 is the standard public exponent.
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    # Local hostname is always included so the cert matches the machine name.
    hostname = socket.gethostname()

    # HUB_HOSTNAME allows comma-separated FQDNs (e.g. ACI public DNS name)
    extra_hostnames = [h.strip() for h in os.environ.get("HUB_HOSTNAME", "").split(",") if h.strip()]
    # Use the first configured public name as the Common Name, else the local hostname.
    cn = extra_hostnames[0] if extra_hostnames else hostname

    # Subject identifies the server; it is also the issuer because the cert is self-signed.
    subject = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, cn),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "HPE Hub"),
        ]
    )

    # Names the cert is valid for: hostname, localhost and every HUB_HOSTNAME entry.
    dns_names = [x509.DNSName(hostname), x509.DNSName("localhost")]
    for h in extra_hostnames:
        dns_names.append(x509.DNSName(h))

    # Subject Alternative Name extension, which browsers check; also covers the loopback IP.
    san = x509.SubjectAlternativeName(
        dns_names + [x509.IPAddress(ipaddress.IPv4Address("127.0.0.1"))]
    )

    # Build and sign the cert: self-issued, random serial, valid from now for ~10 years (3650 days).
    now = datetime.datetime.utcnow()
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(san, critical=False)
        .sign(key, hashes.SHA256())
    )

    # Save the private key as unencrypted PEM so the web server can load it without a passphrase.
    key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    # Save the certificate in PEM format next to the key.
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    logger.info(f"Generated self-signed TLS cert: {cert_path}")


def ensure_tls(settings) -> tuple[Path, Path]:
    """Return (cert_path, key_path), generating self-signed cert if needed."""
    # Base folder for auto-generated certificates.
    data_dir = Path(settings.data_dir)

    # Explicitly configured cert and key paths win; use them as-is.
    if settings.tls_cert_path and settings.tls_key_path:
        return Path(settings.tls_cert_path), Path(settings.tls_key_path)

    # Default locations inside the data directory.
    cert_path = data_dir / "tls" / "cert.pem"
    key_path = data_dir / "tls" / "key.pem"

    # Generate a new pair only when either file is missing, so restarts keep the same cert.
    if not cert_path.exists() or not key_path.exists():
        generate_self_signed(cert_path, key_path)

    return cert_path, key_path
