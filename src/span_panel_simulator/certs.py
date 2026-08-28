"""TLS certificate provisioning for the emulator.

The certificate authority is fixed and ships with the package; the server
certificate is minted per install. That split is the whole design, and it
follows from what the emulators are for.

The simulator emulates SPAN firmware before r202633 and panelbench emulates
r202633 and later, so stopping one and starting the other rehearses a firmware
upgrade on a single panel. A firmware upgrade does not rotate a panel's
certificate authority, and a consumer that pins that authority -- as the Home
Assistant integration does -- is right to stop and ask when one changes. While
each emulator minted its own CA the swap presented a new anchor, so the
rehearsal simulated a panel substitution: the one event it must not.

The leaf cannot be shared the same way, because it has to name the address and
hostname *this* install answers on. So each install mints its own leaf and its
own key against the packaged authority, and the packaged private key is the only
material the two repositories hold in common. See ``_ca/README.md`` for why that
key is public on purpose and what it does and does not cost.

Nothing on disk is ever read to decide trust. ``ca.crt`` is written from the
packaged bytes on every start, so a generated anchor left in a persistent
``/data/certs`` by an older build cannot outlive an upgrade, and the certificate
this module serves, the one mosquitto trusts and the one the leaf chains to are
the same certificate by construction rather than by hope.
"""

from __future__ import annotations

import datetime
import ipaddress
import logging
import os
import socket
from dataclasses import dataclass
from importlib import resources
from typing import TYPE_CHECKING

from cryptography import x509
from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

if TYPE_CHECKING:
    from pathlib import Path

from cryptography.x509.oid import NameOID

_LOGGER = logging.getLogger(__name__)

_SERVER_VALIDITY_DAYS = 365
_KEY_SIZE = 2048

# A leaf is re-signed once it is inside this margin of expiring, rather than
# after it already has. Re-signing is free -- the authority is constant and the
# emulator holds its key -- while an expired leaf is a silent, permanent
# outage: a pinned consumer sees a handshake failure against an unchanged
# anchor, reports it as retryable, and retries for as long as the process runs.
_LEAF_RENEWAL_MARGIN = datetime.timedelta(days=30)

_CA_PACKAGE_DIR = "_ca"


class StaticCAUnavailableError(RuntimeError):
    """The packaged certificate authority is missing or unreadable.

    Fatal on purpose, and the one place this module refuses to self-heal.
    Absent or corrupt package data means the build or the image is broken, and
    the self-healing response -- mint a fresh CA -- would hand this install a
    private anchor that no other install shares, which is precisely the
    divergence the packaged CA exists to prevent. Failing loudly loses a test
    panel until the image is fixed; recovering quietly loses the property the
    design is for, and does it invisibly.
    """


@dataclass(frozen=True, slots=True)
class CertificateBundle:
    """Paths to the certificate files this emulator serves.

    There is no ``ca_key_path``. The authority's key is package data and is
    never written into the certificate directory: nothing at runtime reads it
    from there -- mosquitto is given ``cafile`` and the server pair, and the
    bootstrap endpoint serves the certificate -- so writing it would publish a
    world-readable private key into a persistent volume for no purpose, and add
    a fourth file that could drift from the other three.
    """

    ca_cert_path: Path
    server_cert_path: Path
    server_key_path: Path
    ca_cert_pem: bytes


def _packaged_ca() -> tuple[bytes, x509.Certificate, rsa.RSAPrivateKey]:
    """Load the shipped authority, or refuse to start.

    Returns the certificate's PEM bytes verbatim alongside the parsed pair. The
    raw bytes are what gets written to disk and served, so that what a consumer
    fingerprints is the shipped file rather than a re-serialisation of it.
    """
    anchor = resources.files(__package__).joinpath(_CA_PACKAGE_DIR)
    try:
        ca_pem = anchor.joinpath("ca.crt").read_bytes()
        ca_key_pem = anchor.joinpath("ca.key").read_bytes()
    except (FileNotFoundError, OSError) as err:
        raise StaticCAUnavailableError(
            f"The packaged certificate authority is missing from {_CA_PACKAGE_DIR}/. "
            "This build is incomplete; it cannot be repaired by generating one, "
            "because a generated authority would not match any other install."
        ) from err

    try:
        ca_cert = x509.load_pem_x509_certificate(ca_pem)
        ca_key = serialization.load_pem_private_key(ca_key_pem, password=None)
    except (ValueError, TypeError) as err:
        raise StaticCAUnavailableError(
            f"The packaged certificate authority in {_CA_PACKAGE_DIR}/ cannot be read: {err}"
        ) from err

    if not isinstance(ca_key, rsa.RSAPrivateKey):
        raise StaticCAUnavailableError(
            f"The packaged authority key in {_CA_PACKAGE_DIR}/ is {type(ca_key).__name__}, "
            "not the RSA key this module signs with."
        )
    return ca_pem, ca_cert, ca_key


def _write_atomic(path: Path, data: bytes, *, mode: int = 0o644) -> None:
    """Write a file so that a reader never sees it half-written.

    Both mosquitto and the emulator read this directory, and an add-on killed
    mid-write would otherwise leave a truncated certificate that reads back as
    corrupt on the next start. Written to a temporary name in the same
    directory and moved into place, which is atomic on the same filesystem.
    """
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_bytes(data)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def _install_ca(output_dir: Path, ca_pem: bytes) -> Path:
    """Put the packaged authority on disk, unconditionally.

    Written on every start rather than when absent, because an add-on's data
    directory survives upgrades: an install that ran a build which generated
    its own CA still has that file, and any rule of the form "keep what is
    already there" would serve it forever. Comparing first keeps the common
    case a read.
    """
    ca_cert_path = output_dir / "ca.crt"
    try:
        existing: bytes | None = ca_cert_path.read_bytes()
    except (FileNotFoundError, OSError):
        existing = None

    if existing != ca_pem:
        if existing:
            _LOGGER.warning(
                "Replacing the certificate authority in %s with the packaged one. A consumer "
                "pinned to the previous authority will report that it changed, once.",
                ca_cert_path,
            )
        _write_atomic(ca_cert_path, ca_pem)

    # An older build wrote the authority's private key here. It is
    # world-readable, nothing reads it any more, and leaving it behind invites
    # somebody to sign with it.
    stale_key = output_dir / "ca.key"
    if stale_key.exists():
        stale_key.unlink(missing_ok=True)
        _LOGGER.info("Removed the authority private key left in %s by an earlier build", stale_key)

    return ca_cert_path


def _issued_by(leaf: x509.Certificate, ca_cert: x509.Certificate) -> bool:
    """Whether ``ca_cert`` actually signed ``leaf``, by signature.

    Cryptographic rather than a comparison of issuer names, and that is
    load-bearing. Every authority these emulators have ever generated carries
    the same subject, and neither certificate carries a Subject or Authority
    Key Identifier, so names cannot tell two authorities apart. A name-based
    check would pass for a leaf signed by a superseded CA -- exactly what sits
    in the certificate directory of every install upgrading to the packaged
    authority -- and the emulator would then serve a chain its own published
    authority rejects, which a pinned consumer cannot recover from.
    """
    public_key = ca_cert.public_key()
    if not isinstance(public_key, rsa.RSAPublicKey):
        return False
    try:
        # Read inside the guard: this raises `UnsupportedAlgorithm` for a
        # signature OID `cryptography` does not know, and returns None for the
        # Ed25519/Ed448 family. Neither is a leaf this authority signed, and
        # neither may escape into a startup that cannot survive an exception.
        algorithm = leaf.signature_hash_algorithm
        if algorithm is None:
            return False
        public_key.verify(
            leaf.signature,
            leaf.tbs_certificate_bytes,
            padding.PKCS1v15(),
            algorithm,
        )
    except (InvalidSignature, ValueError, TypeError, UnsupportedAlgorithm):
        return False
    return True


def _key_matches(cert: x509.Certificate, key_path: Path) -> bool:
    """Whether `key_path` holds the private key for `cert`.

    A certificate and a key from two different generations both parse, and the
    mismatch surfaces only when mosquitto refuses to start with a message about
    its keyfile rather than anything naming the real problem. Compared by public
    key, which is the only thing that actually has to agree.
    """
    try:
        key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
    except (FileNotFoundError, OSError, ValueError, TypeError, UnsupportedAlgorithm):
        return False
    # Compared as DER public bytes rather than by key-type-specific numbers, so
    # that a key of some other algorithm answers False like any other mismatch
    # instead of raising out of a predicate that promises never to.
    encoding = serialization.Encoding.DER
    fmt = serialization.PublicFormat.SubjectPublicKeyInfo
    try:
        return key.public_key().public_bytes(encoding, fmt) == cert.public_key().public_bytes(
            encoding, fmt
        )
    except (ValueError, UnsupportedAlgorithm):
        return False


def _leaf_is_fit(
    cert_path: Path,
    key_path: Path,
    ca_cert: x509.Certificate,
    address: str | None,
    dns_name: str | None,
) -> bool:
    """Whether the leaf on disk can still be served as it stands.

    One predicate rather than a chain of special cases, because every way a
    leaf can be unfit has the same remedy: sign a new one. It is unfit if it
    cannot be parsed, if this authority did not sign it, if its private key is
    missing or is not the one it was issued for, if it has expired or is about
    to, or if it does not name the address and hostname this install answers
    on.

    Every failure is answered False rather than raised. This runs at startup
    under ``set -euo pipefail``; an exception here is not a diagnostic, it is a
    container that will not boot.
    """
    try:
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    except (FileNotFoundError, OSError, ValueError):
        return False

    if not _issued_by(cert, ca_cert):
        _LOGGER.info("The server certificate in %s was signed by another authority", cert_path)
        return False

    if not _key_matches(cert, key_path):
        _LOGGER.info("The server certificate in %s has no matching private key", cert_path)
        return False

    if cert.not_valid_after_utc - _LEAF_RENEWAL_MARGIN <= datetime.datetime.now(datetime.UTC):
        _LOGGER.info("The server certificate in %s has expired or is about to", cert_path)
        return False

    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
    except x509.ExtensionNotFound:
        return False

    if address:
        try:
            target = ipaddress.ip_address(address)
        except ValueError:
            # Not an address at all. Nothing can be asserted about a SAN entry
            # for it, and inventing one would put a name in the certificate
            # that the emulator was never reached by.
            _LOGGER.warning("Ignoring advertise address %r: not an IP address", address)
        else:
            if target not in san.value.get_values_for_type(x509.IPAddress):
                return False

    return not (dns_name and dns_name not in san.value.get_values_for_type(x509.DNSName))


def _sign_server_cert(
    output_dir: Path,
    ca_cert: x509.Certificate,
    ca_key: rsa.RSAPrivateKey,
    hostname: str,
    advertise_address: str | None,
) -> tuple[Path, Path]:
    """Mint a server certificate and key against the packaged authority."""
    server_key = rsa.generate_private_key(public_exponent=65537, key_size=_KEY_SIZE)

    names: list[x509.GeneralName] = [
        x509.DNSName(hostname),
        x509.DNSName("localhost"),
        x509.DNSName(socket.gethostname()),
        x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
    ]
    if advertise_address:
        try:
            names.append(x509.IPAddress(ipaddress.ip_address(advertise_address)))
        except ValueError:
            _LOGGER.warning("Ignoring advertise address %r: not an IP address", advertise_address)

    now = datetime.datetime.now(datetime.UTC)
    server_cert = (
        x509.CertificateBuilder()
        .subject_name(
            x509.Name(
                [
                    x509.NameAttribute(NameOID.ORGANIZATION_NAME, "SPAN Simulator"),
                    x509.NameAttribute(NameOID.COMMON_NAME, hostname),
                ]
            )
        )
        .issuer_name(ca_cert.subject)
        .public_key(server_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=_SERVER_VALIDITY_DAYS))
        .add_extension(x509.SubjectAlternativeName(names), critical=False)
        .sign(ca_key, hashes.SHA256())
    )

    server_cert_path = output_dir / "server.crt"
    server_key_path = output_dir / "server.key"
    _write_atomic(server_cert_path, server_cert.public_bytes(serialization.Encoding.PEM))
    _write_atomic(
        server_key_path,
        server_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        ),
    )
    return server_cert_path, server_key_path


def generate_certificates(
    output_dir: Path,
    hostname: str = "span-simulator",
    advertise_address: str | None = None,
) -> CertificateBundle:
    """Install the packaged authority and ensure a usable server certificate.

    The authority is written every time; the server certificate is reused when
    it is still fit to serve and re-signed otherwise. A leaf that has gone
    stale -- because the advertised address changed, because it expired, or
    because it was signed by an authority an older build generated -- costs a
    new leaf and nothing else. It never costs a new authority, which is what
    a consumer pinned to this panel would have to be asked about.

    Args:
        output_dir: Directory to write the certificate files into.
        hostname: Server hostname for the certificate's SAN.
        advertise_address: Optional IP address to include in the SAN so that
            TLS clients connecting by IP pass verification.

    Returns:
        CertificateBundle with the file paths and the authority's PEM bytes.

    Raises:
        StaticCAUnavailableError: the packaged authority is missing or unreadable.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    ca_pem, ca_cert, ca_key = _packaged_ca()
    ca_cert_path = _install_ca(output_dir, ca_pem)

    server_cert_path = output_dir / "server.crt"
    server_key_path = output_dir / "server.key"

    if _leaf_is_fit(
        server_cert_path, server_key_path, ca_cert, advertise_address, socket.gethostname()
    ):
        _LOGGER.info("Reusing the server certificate in %s", output_dir)
    else:
        server_cert_path, server_key_path = _sign_server_cert(
            output_dir, ca_cert, ca_key, hostname, advertise_address
        )
        _LOGGER.info("Signed a server certificate in %s", output_dir)

    return CertificateBundle(
        ca_cert_path=ca_cert_path,
        server_cert_path=server_cert_path,
        server_key_path=server_key_path,
        ca_cert_pem=ca_pem,
    )
