"""Tests for TLS certificate generation."""

from __future__ import annotations

from pathlib import Path

import pytest

from span_panel_simulator.certs import generate_certificates


class TestCertificateGeneration:
    """Verify certificate bundle creation."""

    def test_generates_all_files(self, tmp_path: Path) -> None:
        bundle = generate_certificates(tmp_path / "certs")

        assert bundle.ca_cert_path.exists()
        assert bundle.ca_key_path.exists()
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
