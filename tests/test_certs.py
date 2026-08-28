"""Tests for TLS certificate provisioning.

The property under test throughout is that the certificate authority is fixed
and the server certificate is not. A consumer that pins the authority -- the
Home Assistant integration does -- treats a change as worth stopping for, so
every path that re-signs a leaf must leave the anchor alone, and the anchor
served must always be the packaged one.
"""

from __future__ import annotations

import datetime
import hashlib
import ipaddress
import socket
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import NameOID

from span_panel_simulator.certs import (
    CertificateBundle,
    StaticCAUnavailableError,
    generate_certificates,
)

# The published identity of the packaged authority, over the certificate's DER
# bytes -- the same value the integration pins, reports under `panel_ca` in
# diagnostics and shows in a certificate-authority-changed repair.
#
# Hardcoded here on purpose. This is the one form of drift no runtime check can
# catch: the simulator and panelbench each carry a copy of these bytes, and a
# well-meaning "refresh the certs" in either repository would fork the two
# fleets silently. Both repositories assert the same constant, so such a change
# fails a test instead of shipping.
STATIC_CA_FINGERPRINT = "3cf8c14a78900b8736870c95adcc931cdcb3a51bc3029c96efafd0a4cb790d97"


def _fingerprint(pem: bytes) -> str:
    cert = x509.load_pem_x509_certificate(pem)
    return hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()


def _foreign_ca() -> tuple[x509.Certificate, rsa.RSAPrivateKey]:
    """Mint an authority carrying the subject every generated CA used to have.

    Deliberately named identically to the authority older builds generated, so
    that a check comparing issuer names cannot tell it from the packaged one.
    """
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name(
        [
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "SPAN Simulator"),
            x509.NameAttribute(NameOID.COMMON_NAME, "SPAN Simulator CA"),
        ]
    )
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(key, hashes.SHA256())
    )
    return cert, key


def _leaf_signed_by(
    ca_cert: x509.Certificate,
    ca_key: rsa.RSAPrivateKey,
    *,
    names: list[x509.GeneralName] | None = None,
    not_after: datetime.datetime | None = None,
) -> tuple[bytes, bytes]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.datetime.now(datetime.UTC)
    # An already-expired leaf still needs a validity window that makes sense,
    # so back-date the start rather than letting it collide with the end.
    not_before = min(
        now - datetime.timedelta(days=1), (not_after or now) - datetime.timedelta(days=1)
    )
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "span-simulator")]))
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(not_after or (now + datetime.timedelta(days=365)))
        .add_extension(
            x509.SubjectAlternativeName(
                names
                if names is not None
                else [
                    x509.DNSName("span-simulator"),
                    x509.DNSName("localhost"),
                    x509.DNSName(socket.gethostname()),
                    x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                ]
            ),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    return (
        cert.public_bytes(serialization.Encoding.PEM),
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        ),
    )


def _leaf_of(bundle: CertificateBundle) -> x509.Certificate:
    return x509.load_pem_x509_certificate(bundle.server_cert_path.read_bytes())


class TestPackagedAuthority:
    def test_fingerprint_is_the_published_constant(self, tmp_path: Path) -> None:
        """Guards the one drift no runtime check can catch: the two repos forking."""
        bundle = generate_certificates(tmp_path / "certs")

        assert _fingerprint(bundle.ca_cert_pem) == STATIC_CA_FINGERPRINT

    def test_packaged_key_pairs_with_packaged_certificate(self, tmp_path: Path) -> None:
        """A mismatched pair would sign leaves the served authority rejects."""
        bundle = generate_certificates(tmp_path / "certs")
        leaf = _leaf_of(bundle)
        ca = x509.load_pem_x509_certificate(bundle.ca_cert_pem)

        # The leaf was signed with the packaged key; verifying it against the
        # packaged certificate's public key proves the two are a pair.
        ca.public_key().verify(  # type: ignore[union-attr]
            leaf.signature,
            leaf.tbs_certificate_bytes,
            padding.PKCS1v15(),
            leaf.signature_hash_algorithm,
        )

    def test_authority_does_not_expire_within_the_century(self, tmp_path: Path) -> None:
        bundle = generate_certificates(tmp_path / "certs")
        ca = x509.load_pem_x509_certificate(bundle.ca_cert_pem)

        assert ca.not_valid_after_utc.year == 2126

    def test_authority_private_key_is_never_written_to_disk(self, tmp_path: Path) -> None:
        """It is package data; writing it would publish it into a data volume."""
        certs = tmp_path / "certs"
        generate_certificates(certs)

        assert not (certs / "ca.key").exists()

    def test_missing_package_data_is_fatal_not_self_healing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Generating a replacement would mint an anchor no other install shares."""
        import span_panel_simulator.certs as certs_mod

        def _absent(_package: str) -> Path:
            return tmp_path / "does-not-exist"

        monkeypatch.setattr(certs_mod.resources, "files", _absent)

        with pytest.raises(StaticCAUnavailableError):
            generate_certificates(tmp_path / "certs")


class TestAuthorityIsInstalledUnconditionally:
    def test_a_generated_authority_left_behind_is_replaced(self, tmp_path: Path) -> None:
        """An add-on's data directory survives upgrades; a stale anchor must not."""
        certs = tmp_path / "certs"
        certs.mkdir()
        foreign_cert, _ = _foreign_ca()
        (certs / "ca.crt").write_bytes(foreign_cert.public_bytes(serialization.Encoding.PEM))

        bundle = generate_certificates(certs)

        assert _fingerprint((certs / "ca.crt").read_bytes()) == STATIC_CA_FINGERPRINT
        assert _fingerprint(bundle.ca_cert_pem) == STATIC_CA_FINGERPRINT

    def test_a_stale_authority_key_is_removed(self, tmp_path: Path) -> None:
        """Left by an older build, world-readable, and now signs nothing."""
        certs = tmp_path / "certs"
        certs.mkdir()
        (certs / "ca.key").write_bytes(b"-----BEGIN RSA PRIVATE KEY-----\nstale\n")

        generate_certificates(certs)

        assert not (certs / "ca.key").exists()

    def test_a_truncated_authority_file_is_overwritten(self, tmp_path: Path) -> None:
        certs = tmp_path / "certs"
        certs.mkdir()
        (certs / "ca.crt").write_bytes(b"-----BEGIN CERTIFICATE-----\ntrunc")

        generate_certificates(certs)

        assert _fingerprint((certs / "ca.crt").read_bytes()) == STATIC_CA_FINGERPRINT


class TestLeafIsResignedWithoutTouchingTheAuthority:
    """The original defect: a stale leaf rotated the anchor along with it."""

    def test_a_new_advertise_address_resigns_only_the_leaf(self, tmp_path: Path) -> None:
        certs = tmp_path / "certs"
        first = generate_certificates(certs, advertise_address="10.0.0.5")
        first_leaf = _leaf_of(first).serial_number

        second = generate_certificates(certs, advertise_address="10.0.0.9")

        assert _fingerprint(second.ca_cert_pem) == STATIC_CA_FINGERPRINT
        assert _fingerprint(first.ca_cert_pem) == _fingerprint(second.ca_cert_pem)
        assert _leaf_of(second).serial_number != first_leaf
        san = _leaf_of(second).extensions.get_extension_for_class(x509.SubjectAlternativeName)
        assert ipaddress.ip_address("10.0.0.9") in san.value.get_values_for_type(x509.IPAddress)

    def test_an_unchanged_address_reuses_the_leaf(self, tmp_path: Path) -> None:
        certs = tmp_path / "certs"
        first = generate_certificates(certs, advertise_address="10.0.0.5")
        first_leaf = _leaf_of(first).serial_number

        second = generate_certificates(certs, advertise_address="10.0.0.5")

        assert _leaf_of(second).serial_number == first_leaf

    def test_a_leaf_signed_by_a_superseded_authority_is_resigned(self, tmp_path: Path) -> None:
        """The upgrade path every existing install takes.

        The superseded authority carries the same subject as the packaged one,
        so a check comparing issuer names would keep this leaf and leave the
        emulator serving a chain its own published authority rejects -- which a
        pinned consumer cannot recover from.
        """
        certs = tmp_path / "certs"
        certs.mkdir()
        foreign_cert, foreign_key = _foreign_ca()
        leaf_pem, leaf_key_pem = _leaf_signed_by(foreign_cert, foreign_key)
        (certs / "server.crt").write_bytes(leaf_pem)
        (certs / "server.key").write_bytes(leaf_key_pem)

        bundle = generate_certificates(certs)

        ca = x509.load_pem_x509_certificate(bundle.ca_cert_pem)
        leaf = _leaf_of(bundle)
        assert leaf.public_bytes(serialization.Encoding.PEM) != leaf_pem
        # The served chain validates against the served authority.
        ca.public_key().verify(  # type: ignore[union-attr]
            leaf.signature,
            leaf.tbs_certificate_bytes,
            padding.PKCS1v15(),
            leaf.signature_hash_algorithm,
        )

    def test_an_expired_leaf_is_resigned(self, tmp_path: Path) -> None:
        certs = tmp_path / "certs"
        first = generate_certificates(certs)
        expired = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=1)
        ca = x509.load_pem_x509_certificate(first.ca_cert_pem)
        # Re-sign with the packaged key is not possible from here, so stand in
        # a foreign expired leaf: unfit for two reasons, re-signed for either.
        foreign_cert, foreign_key = _foreign_ca()
        leaf_pem, _ = _leaf_signed_by(foreign_cert, foreign_key, not_after=expired)
        (certs / "server.crt").write_bytes(leaf_pem)

        second = generate_certificates(certs)

        assert _leaf_of(second).not_valid_after_utc > datetime.datetime.now(datetime.UTC)
        assert _fingerprint(second.ca_cert_pem) == _fingerprint(first.ca_cert_pem)
        assert ca.subject == _leaf_of(second).issuer

    def test_an_unparseable_leaf_is_resigned_rather_than_raising(self, tmp_path: Path) -> None:
        """This runs under `set -euo pipefail`; a raise is a boot loop."""
        certs = tmp_path / "certs"
        certs.mkdir()
        (certs / "server.crt").write_bytes(b"-----BEGIN CERTIFICATE-----\nnot a cert\n")

        bundle = generate_certificates(certs)

        assert _leaf_of(bundle).subject is not None

    def test_a_key_that_does_not_match_the_leaf_forces_a_resign(self, tmp_path: Path) -> None:
        """A torn write can leave a certificate and a key from two generations.

        Both parse, so nothing here objects; the mismatch surfaces only when
        mosquitto refuses to start with a message about its keyfile, naming
        nothing that would lead anyone to the real problem.
        """
        certs = tmp_path / "certs"
        first = generate_certificates(certs)
        first_leaf = _leaf_of(first).serial_number
        stray = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        (certs / "server.key").write_bytes(
            stray.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption(),
            )
        )

        second = generate_certificates(certs)

        assert _leaf_of(second).serial_number != first_leaf
        # The pair that ends up on disk actually is a pair.
        key = serialization.load_pem_private_key(
            (certs / "server.key").read_bytes(), password=None
        )
        assert key.public_key().public_numbers() == _leaf_of(second).public_key().public_numbers()

    def test_a_missing_leaf_key_forces_a_resign(self, tmp_path: Path) -> None:
        """A certificate without its key cannot be served."""
        certs = tmp_path / "certs"
        first = generate_certificates(certs)
        first_leaf = _leaf_of(first).serial_number
        (certs / "server.key").unlink()

        second = generate_certificates(certs)

        assert (certs / "server.key").exists()
        assert _leaf_of(second).serial_number != first_leaf


class TestSanEdgeCases:
    def test_a_non_ip_advertise_address_does_not_crash(self, tmp_path: Path) -> None:
        """`ipaddress.ip_address` used to raise straight out of startup."""
        bundle = generate_certificates(tmp_path / "certs", advertise_address="not-an-ip")

        assert bundle.server_cert_path.exists()

    def test_a_non_ip_advertise_address_is_not_named(self, tmp_path: Path) -> None:
        bundle = generate_certificates(tmp_path / "certs", advertise_address="not-an-ip")
        san = _leaf_of(bundle).extensions.get_extension_for_class(x509.SubjectAlternativeName)

        assert "not-an-ip" not in san.value.get_values_for_type(x509.DNSName)

    def test_the_hostname_and_loopback_are_always_named(self, tmp_path: Path) -> None:
        bundle = generate_certificates(tmp_path / "certs")
        san = _leaf_of(bundle).extensions.get_extension_for_class(x509.SubjectAlternativeName)

        assert "localhost" in san.value.get_values_for_type(x509.DNSName)
        assert ipaddress.ip_address("127.0.0.1") in san.value.get_values_for_type(x509.IPAddress)


class TestBundleShape:
    def test_generates_the_files_it_reports(self, tmp_path: Path) -> None:
        bundle = generate_certificates(tmp_path / "certs")

        assert bundle.ca_cert_path.exists()
        assert bundle.server_cert_path.exists()
        assert bundle.server_key_path.exists()

    def test_ca_pem_is_valid(self, tmp_path: Path) -> None:
        bundle = generate_certificates(tmp_path / "certs")

        assert bundle.ca_cert_pem.startswith(b"-----BEGIN CERTIFICATE-----")
        assert bundle.ca_cert_pem.endswith(b"-----END CERTIFICATE-----\n")

    def test_creates_output_directory(self, tmp_path: Path) -> None:
        target = tmp_path / "nested" / "deep" / "certs"
        bundle = generate_certificates(target)

        assert target.exists()
        assert bundle.ca_cert_path.parent == target

    def test_no_temporary_files_are_left_behind(self, tmp_path: Path) -> None:
        certs = tmp_path / "certs"
        generate_certificates(certs)

        assert not [p for p in certs.iterdir() if p.name.startswith(".")]
