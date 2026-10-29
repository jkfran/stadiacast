"""DTLS 1.2 over memory BIOs (pyOpenSSL) with the ``use_srtp`` extension and the SRTP key exporter.

The endpoint feeds datagrams in and sends the flights that come out; the session reports the
peer certificate fingerprint, the negotiated SRTP profile and the exported keys
(``EXTRACTOR-dtls_srtp``).  Ephemeral WebRTC-style certificates come from :class:`DtlsCertificate`.
"""

from __future__ import annotations

import datetime
import struct
from dataclasses import dataclass

from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from OpenSSL import SSL, crypto

from stadiacast import SERVER_NAME
from stadiacast.rtc.srtp import SRTP_PROFILES

# The controller only offers SRTP_AES128_CM_SHA1_80 (string at 0x60379076); the
# others are here so the fake controller / other peers can be exercised too.
DEFAULT_SRTP_PROFILES = "SRTP_AES128_CM_SHA1_80:SRTP_AEAD_AES_128_GCM:SRTP_AEAD_AES_256_GCM:SRTP_AES128_CM_SHA1_32"


class DtlsCertificate:
    """Ephemeral self-signed ECDSA P-256 certificate, WebRTC style."""

    def __init__(self, common_name: str = SERVER_NAME) -> None:
        self.key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
        now = datetime.datetime.now(datetime.UTC)
        self.cert = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(self.key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=30))
            .sign(self.key, hashes.SHA256())
        )
        self.pkey = crypto.PKey.from_cryptography_key(self.key)
        self.x509 = crypto.X509.from_cryptography(self.cert)

    @property
    def fingerprint(self) -> str:
        return "sha-256 " + self.cert.fingerprint(hashes.SHA256()).hex(":").upper()


def normalize_fingerprint(fp: str | None) -> str | None:
    if not fp:
        return None
    fp = fp.strip()
    if " " in fp:
        fp = fp.split(" ", 1)[1]
    return fp.replace(":", "").upper()


@dataclass
class SrtpKeys:
    profile: str
    client_key: bytes
    client_salt: bytes
    server_key: bytes
    server_salt: bytes


def split_dtls_records(buf: bytes, mtu: int) -> list[bytes]:
    """Group whole DTLS records (13-byte header carries the length) into datagrams <= mtu."""
    out: list[bytes] = []
    cur = bytearray()
    off = 0
    while off + 13 <= len(buf):
        ln = struct.unpack_from("!H", buf, off + 11)[0]
        rec = buf[off : off + 13 + ln]
        if len(rec) < 13 + ln:
            break
        if cur and len(cur) + len(rec) > mtu:
            out.append(bytes(cur))
            cur = bytearray()
        cur += rec
        off += 13 + ln
    if off < len(buf):
        cur += buf[off:]
    if cur:
        out.append(bytes(cur))
    return out


class DtlsSession:
    def __init__(
        self, cert: DtlsCertificate, role: str, srtp_profiles: str = DEFAULT_SRTP_PROFILES, mtu: int = 1200
    ) -> None:
        if role not in ("client", "server"):
            raise ValueError(role)
        ctx = SSL.Context(SSL.DTLS_METHOD)
        ctx.use_privatekey(cert.key)  # cryptography objects (pyOpenSSL >= 23.2)
        ctx.use_certificate(cert.cert)
        ctx.set_verify(SSL.VERIFY_PEER | SSL.VERIFY_FAIL_IF_NO_PEER_CERT, lambda *_: True)  # fingerprint checked later
        ctx.set_tlsext_use_srtp(srtp_profiles.encode())
        ctx.set_options(SSL.OP_NO_QUERY_MTU)
        self.conn = SSL.Connection(ctx, None)
        try:  # pyOpenSSL has no set_mtu wrapper; the cffi symbol is there.
            from OpenSSL._util import lib as _lib

            _lib.SSL_set_mtu(self.conn._ssl, mtu)
        except Exception:
            pass
        if role == "client":
            self.conn.set_connect_state()
        else:
            self.conn.set_accept_state()
        self.role = role
        self.mtu = mtu
        self.handshake_complete = False
        self.closed = False
        self.peer_fingerprint: str | None = None
        self.profile: str | None = None
        self.cipher: str | None = None

    def _pump_handshake(self) -> None:
        if self.handshake_complete:
            return
        try:
            self.conn.do_handshake()
        except SSL.WantReadError:
            return
        self.handshake_complete = True
        cert = self.conn.get_peer_certificate(as_cryptography=True)  # pyOpenSSL >= 23.2
        self.peer_fingerprint = ("sha-256 " + cert.fingerprint(hashes.SHA256()).hex(":").upper()) if cert else None
        prof = self.conn.get_selected_srtp_profile()
        self.profile = prof.decode() if prof else None
        self.cipher = self.conn.get_cipher_name()

    def _drain(self) -> list[bytes]:
        buf = bytearray()
        while True:
            try:
                chunk = self.conn.bio_read(65536)
            except SSL.WantReadError:
                break
            except SSL.Error:
                break
            if not chunk:
                break
            buf += chunk
        return split_dtls_records(bytes(buf), self.mtu)

    def start(self) -> list[bytes]:
        """Client: produce the ClientHello flight."""
        self._pump_handshake()
        return self._drain()

    def feed(self, datagram: bytes) -> tuple[list[bytes], list[bytes]]:
        """Returns (datagrams_to_send, application_data_chunks)."""
        self.conn.bio_write(datagram)
        app: list[bytes] = []
        if not self.handshake_complete:
            self._pump_handshake()
        if self.handshake_complete:
            while True:
                try:
                    chunk = self.conn.recv(65536)
                except SSL.WantReadError:
                    break
                except SSL.ZeroReturnError:
                    self.closed = True
                    break
                except SSL.SysCallError:
                    break
                if not chunk:
                    break
                app.append(chunk)
        return self._drain(), app

    def send_app(self, data: bytes) -> list[bytes]:
        self.conn.send(data)
        return self._drain()

    def timeout(self) -> float | None:
        try:
            return self.conn.DTLSv1_get_timeout()
        except Exception:
            return None

    def handle_timeout(self) -> list[bytes]:
        try:
            self.conn.DTLSv1_handle_timeout()
        except SSL.WantReadError:
            pass
        except SSL.Error:
            pass
        return self._drain()

    def export_srtp_keys(self) -> SrtpKeys:
        if not self.profile:
            raise RuntimeError("no SRTP profile negotiated")
        _, klen, slen, _, _ = SRTP_PROFILES[self.profile]
        mat = self.conn.export_keying_material(b"EXTRACTOR-dtls_srtp", 2 * (klen + slen), None)
        return SrtpKeys(
            self.profile, mat[:klen], mat[2 * klen : 2 * klen + slen], mat[klen : 2 * klen], mat[2 * klen + slen :]
        )
