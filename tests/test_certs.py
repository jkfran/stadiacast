"""stadiacast.certs: the lab certificate, the start-up check and the clock pin."""

from __future__ import annotations

import datetime
import os

import pytest
from cryptography import x509

from stadiacast.certs import GOOGLE_SANS, create_self_signed, ensure_certificate, resolve_clock_pin
from stadiacast.config import Config
from stadiacast.log import EventLog


def test_self_signed_pair_covers_the_google_names_and_keeps_the_key_private(tmp_path):
    cert_path, key_path = str(tmp_path / "sub" / "lab.crt"), str(tmp_path / "sub" / "lab.key")
    create_self_signed(cert_path, key_path, days=10)
    with open(cert_path, "rb") as fh:
        cert = x509.load_pem_x509_certificate(fh.read())
    sans = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName)
    assert sans == GOOGLE_SANS
    assert cert.issuer == cert.subject
    assert (cert.not_valid_after_utc - cert.not_valid_before_utc).days == 11
    assert oct(os.stat(key_path).st_mode & 0o777) == "0o600"


def test_ensure_certificate_creates_a_lab_pair_only_when_asked(tmp_path):
    log = EventLog(None, echo=False)
    cfg = Config(cert=str(tmp_path / "c.pem"), key=str(tmp_path / "k.pem"))
    with pytest.raises(FileNotFoundError, match="--self-signed"):
        ensure_certificate(cfg, log)
    cfg.self_signed = True
    ensure_certificate(cfg, log)
    assert os.path.exists(cfg.cert) and os.path.exists(cfg.key)
    assert log.counts == {"self_signed_certificate_created": 1}
    ensure_certificate(cfg, log)  # already there: nothing happens
    assert log.counts == {"self_signed_certificate_created": 1}


def test_clock_pin_dates_and_the_certificate_midpoint(tmp_path):
    cfg = Config(cert=str(tmp_path / "c.pem"), key=str(tmp_path / "k.pem"), self_signed=True)
    cfg.clock_pin = "2026-06-01T12:00:00Z"
    noon = datetime.datetime(2026, 6, 1, 12, tzinfo=datetime.UTC).timestamp()
    assert resolve_clock_pin(cfg) == noon
    cfg.clock_pin = "2026-06-01T12:00:00"  # naive = UTC
    assert resolve_clock_pin(cfg) == noon
    cfg.clock_pin = "2026-06-01T14:00:00+02:00"
    assert resolve_clock_pin(cfg) == noon
    cfg.clock_pin = "not a date"
    with pytest.raises(ValueError):
        resolve_clock_pin(cfg)
    create_self_signed(cfg.cert, cfg.key, days=20)
    cfg.clock_pin = "auto"
    with open(cfg.cert, "rb") as fh:
        cert = x509.load_pem_x509_certificate(fh.read())
    lo, hi = cert.not_valid_before_utc.timestamp(), cert.not_valid_after_utc.timestamp()
    assert abs(resolve_clock_pin(cfg) - (lo + hi) / 2) < 1
