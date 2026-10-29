"""TLS certificate handling: the served chain, a lab self-signed pair and the clock pin.

The controller validates the server chain against its built-in Google Trust Services roots and
checks neither the hostname nor the validity dates (confirmed on a real controller), so any
GTS-issued certificate for a domain you own works for every name it connects to, and never
needs renewing.  See ``docs/setup.md`` for how to obtain one.
"""

from __future__ import annotations

import datetime
import os

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from stadiacast import SERVER_NAME
from stadiacast.config import Config
from stadiacast.log import EventLog

#: Subject alternative names of the lab (self-signed) certificate.
GOOGLE_SANS = [
    "*.googleapis.com",
    "googleapis.com",
    "*.sandbox.googleapis.com",
    "*.google.com",
    "*.gstatic.com",
    "localhost",
]


def resolve_clock_pin(cfg: Config) -> float:
    """The Unix epoch to pin the controller's clock to.

    ``"auto"`` is the midpoint of the served certificate's validity window; anything else is an
    ISO 8601 date-time (naive values are UTC).  The controller sets its clock from our ``Date``
    header and ``GetCurrentTime`` replies and judges the certificate's expiry against it.
    """
    pin = cfg.clock_pin or ""
    if pin == "auto":
        with open(cfg.cert, "rb") as fh:
            cert = x509.load_pem_x509_certificate(fh.read())
        lo = cert.not_valid_before_utc.timestamp()
        hi = cert.not_valid_after_utc.timestamp()
        if not hi > lo:
            raise ValueError(f"certificate has no validity window: {cfg.cert}")
        return lo + (hi - lo) / 2
    dt = datetime.datetime.fromisoformat(pin)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.UTC)
    return dt.timestamp()


def ensure_certificate(cfg: Config, log: EventLog) -> None:
    """Make sure ``cfg.cert`` / ``cfg.key`` exist before the HTTPS listener starts.

    With ``cfg.self_signed`` a lab pair is created if the files are missing; otherwise a
    missing file raises :class:`FileNotFoundError`.
    """
    if os.path.exists(cfg.cert) and os.path.exists(cfg.key):
        return
    if not cfg.self_signed:
        raise FileNotFoundError(f"certificate/key not found: {cfg.cert} / {cfg.key} (use --self-signed for a lab pair)")
    create_self_signed(cfg.cert, cfg.key)
    log(
        "self_signed_certificate_created",
        cert=cfg.cert,
        key=cfg.key,
        note="the controller rejects this with TLS alert unknown_ca; use a GTS-issued certificate for the device",
    )


def create_self_signed(cert_path: str, key_path: str, days: int = 3650) -> None:
    """Write an ECDSA P-256 self-signed certificate for the Google names (tests and the fake controller)."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, "cloudcast-pa.googleapis.com"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, f"{SERVER_NAME} (self-signed)"),
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
        .not_valid_after(now + datetime.timedelta(days=days))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(n) for n in GOOGLE_SANS]), critical=False)
        .sign(key, hashes.SHA256())
    )
    os.makedirs(os.path.dirname(os.path.abspath(cert_path)) or ".", exist_ok=True)
    with open(cert_path, "wb") as fh:
        fh.write(cert.public_bytes(serialization.Encoding.PEM))
    with open(key_path, "wb") as fh:
        fh.write(
            key.private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
            )
        )
    os.chmod(key_path, 0o600)
