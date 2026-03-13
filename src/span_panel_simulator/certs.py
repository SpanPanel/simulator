"""TLS certificate generation for the simulator.

Generates a self-signed CA and a server certificate at startup, matching
the real SPAN panel's TLS provisioning flow.
"""

from __future__ import annotations

import datetime
import logging
from dataclasses import dataclass
from pathlib import Path

import ipaddress

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

_LOGGER = logging.getLogger(__name__)

_CA_VALIDITY_DAYS = 3650
_SERVER_VALIDITY_DAYS = 365
_KEY_SIZE = 2048


@dataclass(frozen=True, slots=True)
class CertificateBundle:
    """Paths to generated certificate files."""

    ca_cert_path: Path
    ca_key_path: Path
    server_cert_path: Path
    server_key_path: Path
    ca_cert_pem: bytes


def _cert_has_ip(cert_path: Path, address: str) -> bool:
    """Check whether an existing server certificate contains *address* in its SAN."""
    cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
    except x509.ExtensionNotFound:
        return False
    target = ipaddress.ip_address(address)
    return target in san.value.get_values_for_type(x509.IPAddress)


def _load_existing(
    output_dir: Path, advertise_address: str | None = None
) -> CertificateBundle | None:
    """Return a CertificateBundle if all expected files already exist.

    When *advertise_address* is provided, the existing server certificate
    is checked for a matching SAN entry.  If the IP is missing the cached
    certs are considered stale and ``None`` is returned so that fresh
    certificates are generated.
    """
    ca_cert_path = output_dir / "ca.crt"
    ca_key_path = output_dir / "ca.key"
    server_cert_path = output_dir / "server.crt"
    server_key_path = output_dir / "server.key"

    if not all(p.exists() for p in (ca_cert_path, ca_key_path, server_cert_path, server_key_path)):
        return None

    if advertise_address and not _cert_has_ip(server_cert_path, advertise_address):
        _LOGGER.info(
            "Existing certificate missing SAN for %s — regenerating",
            advertise_address,
        )
        return None

    _LOGGER.info("Reusing existing TLS certificates in %s", output_dir)
    return CertificateBundle(
        ca_cert_path=ca_cert_path,
        ca_key_path=ca_key_path,
        server_cert_path=server_cert_path,
        server_key_path=server_key_path,
        ca_cert_pem=ca_cert_path.read_bytes(),
    )


def generate_certificates(
    output_dir: Path,
    hostname: str = "span-simulator",
    advertise_address: str | None = None,
) -> CertificateBundle:
    """Generate a self-signed CA and server certificate.

    If all certificate files already exist in *output_dir*, they are loaded
    and returned without regeneration.  This avoids overwriting certs that
    Mosquitto is already using.

    Args:
        output_dir: Directory to write PEM files into.
        hostname: Server hostname for the certificate SAN.
        advertise_address: Optional IP address to include in the SAN so
            that TLS clients connecting by IP pass verification.

    Returns:
        CertificateBundle with file paths and raw CA PEM bytes.
    """
    existing = _load_existing(output_dir, advertise_address=advertise_address)
    if existing is not None:
        return existing

    output_dir.mkdir(parents=True, exist_ok=True)

    # --- CA key + certificate ---
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=_KEY_SIZE)

    ca_name = x509.Name([
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "SPAN Simulator"),
        x509.NameAttribute(NameOID.COMMON_NAME, "SPAN Simulator CA"),
    ])

    now = datetime.datetime.now(datetime.UTC)
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=_CA_VALIDITY_DAYS))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(ca_key, hashes.SHA256())
    )

    ca_cert_path = output_dir / "ca.crt"
    ca_key_path = output_dir / "ca.key"

    ca_cert_pem = ca_cert.public_bytes(serialization.Encoding.PEM)
    ca_cert_path.write_bytes(ca_cert_pem)
    ca_key_path.write_bytes(
        ca_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )

    # --- Server key + certificate ---
    server_key = rsa.generate_private_key(public_exponent=65537, key_size=_KEY_SIZE)

    server_name = x509.Name([
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "SPAN Simulator"),
        x509.NameAttribute(NameOID.COMMON_NAME, hostname),
    ])

    server_cert = (
        x509.CertificateBuilder()
        .subject_name(server_name)
        .issuer_name(ca_name)
        .public_key(server_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=_SERVER_VALIDITY_DAYS))
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName(hostname),
                    x509.DNSName("localhost"),
                    x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                ]
                + (
                    [x509.IPAddress(ipaddress.ip_address(advertise_address))]
                    if advertise_address
                    else []
                )
            ),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )

    server_cert_path = output_dir / "server.crt"
    server_key_path = output_dir / "server.key"

    server_cert_path.write_bytes(server_cert.public_bytes(serialization.Encoding.PEM))
    server_key_path.write_bytes(
        server_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )

    _LOGGER.info("Generated TLS certificates in %s", output_dir)

    return CertificateBundle(
        ca_cert_path=ca_cert_path,
        ca_key_path=ca_key_path,
        server_cert_path=server_cert_path,
        server_key_path=server_key_path,
        ca_cert_pem=ca_cert_pem,
    )
