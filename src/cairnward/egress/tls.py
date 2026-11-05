"""Cairnward's local CA for TLS inspection in cairnward-egress (FR-NET-2, ADR-0018).

Off by default, per environment (`[tls_inspection] enabled = true`). When on, egress ends the
tool's TLS connection with a certificate this CA signs for the host it asked for, opens its own
verified TLS connection to the real host, and passes HTTP/1.1 requests between them, checking
each one against the environment's method and path rules.

- The CA is made once per installation: an EC P-256 key, valid for two years, kept in
  `<data_dir>/egress-ca/` with owner-only permissions. Sandboxed agents can't read the data
  folder. `cairnward egress-ca rotate` replaces it.
- Host certificates are made on demand, valid for seven days, and only kept in memory (their
  key and certificate pass through a private temporary file, because Python's ssl module only
  loads them from files).
- A tool trusts the CA through the bundle `trust_bundle` writes: the usual public roots plus
  this CA, named by SSL_CERT_FILE and the like (`trust_env`).
"""

from __future__ import annotations

import contextlib
import datetime as dt
import ipaddress
import os
import shutil
import ssl
import tempfile
import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

CA_DIR = "egress-ca"
CA_CERT = "ca-cert.pem"
CA_KEY = "ca-key.pem"
CA_DAYS = 730
LEAF_DAYS = 7
MAX_CACHED = 256


def ca_dir(data_dir: Path) -> Path:
    return data_dir / CA_DIR


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _new_ca() -> tuple[bytes, bytes]:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name(
        [
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Cairnward"),
            x509.NameAttribute(NameOID.COMMON_NAME, "Cairnward local inspection CA"),
        ]
    )
    now = _now()
    ski = x509.SubjectKeyIdentifier.from_public_key(key.public_key())
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(hours=1))
        .not_valid_after(now + dt.timedelta(days=CA_DAYS))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(ski, critical=False)
        .sign(key, hashes.SHA256())
    )
    key_pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    return cert.public_bytes(serialization.Encoding.PEM), key_pem


def ensure_ca(data_dir: Path) -> Path:
    """The CA folder, made on first use. Two processes starting at once agree on one CA: the
    files are written to a private folder that is then renamed into place, and the loser of
    that race uses the winner's."""
    folder = ca_dir(data_dir)
    if (folder / CA_CERT).is_file() and (folder / CA_KEY).is_file():
        return folder
    data_dir.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=".egress-ca-", dir=data_dir))
    try:
        cert, key = _new_ca()
        fd = os.open(tmp / CA_KEY, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(key)
        (tmp / CA_CERT).write_bytes(cert)
        try:
            tmp.rename(folder)
        except OSError:  # someone else made it first (or an empty folder is in the way)
            if not (folder / CA_CERT).is_file():
                raise
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return folder


def rotate_ca(data_dir: Path) -> Path:
    """Replace the CA. Tools started before this keep the old bundle and stop trusting egress
    until their next run; a running egress keeps the old CA until it restarts."""
    folder = ca_dir(data_dir)
    if folder.exists():
        old = folder.with_name(f".{CA_DIR}-old-{os.getpid()}")
        folder.rename(old)
        shutil.rmtree(old, ignore_errors=True)
    return ensure_ca(data_dir)


def ca_cert_pem(data_dir: Path) -> bytes:
    return (ensure_ca(data_dir) / CA_CERT).read_bytes()


def ca_fingerprint(pem: bytes) -> str:
    cert = x509.load_pem_x509_certificate(pem)
    return cert.fingerprint(hashes.SHA256()).hex(":")


def public_roots() -> bytes:
    """The roots tools trust normally: certifi's (what httpx uses), else the system file."""
    try:
        import certifi

        return Path(certifi.where()).read_bytes()
    except (ImportError, OSError):
        paths = ssl.get_default_verify_paths()
        for p in (paths.cafile, paths.openssl_cafile):
            if p and Path(p).is_file():
                return Path(p).read_bytes()
    return b""


def trust_bundle(data_dir: Path, dest: Path) -> Path:
    """Public roots plus Cairnward's CA, in one PEM file, for a run's tools."""
    dest.write_bytes(public_roots().rstrip(b"\n") + b"\n" + ca_cert_pem(data_dir))
    return dest


def trust_env(bundle: Path, ca_only: Path) -> dict[str, str]:
    """Variables that make common tools trust `bundle`. Node only adds to its own roots, so it
    gets the CA alone."""
    b = str(bundle)
    return {
        "SSL_CERT_FILE": b,  # OpenSSL, Python's ssl, uv, Go (on Linux), Rust's native roots
        "REQUESTS_CA_BUNDLE": b,
        "CURL_CA_BUNDLE": b,
        "GIT_SSL_CAINFO": b,
        "PIP_CERT": b,
        "DENO_CERT": b,
        "AWS_CA_BUNDLE": b,
        "NODE_EXTRA_CA_CERTS": str(ca_only),
    }


@dataclass
class _Ca:
    cert: x509.Certificate
    key: ec.EllipticCurvePrivateKey


class Issuer:
    """Makes server-side SSL contexts for inspected hosts, signed by the local CA."""

    def __init__(self, data_dir: Path):
        folder = ensure_ca(data_dir)
        key = serialization.load_pem_private_key((folder / CA_KEY).read_bytes(), password=None)
        if not isinstance(key, ec.EllipticCurvePrivateKey):
            raise ValueError(f"{folder / CA_KEY}: not an EC key")
        self._ca = _Ca(x509.load_pem_x509_certificate((folder / CA_CERT).read_bytes()), key)
        self._leaf_key = ec.generate_private_key(ec.SECP256R1())
        self._cache: OrderedDict[str, tuple[ssl.SSLContext, dt.datetime]] = OrderedDict()
        self._lock = threading.Lock()

    @property
    def ca_pem(self) -> bytes:
        return self._ca.cert.public_bytes(serialization.Encoding.PEM)

    def _leaf(self, host: str) -> tuple[bytes, dt.datetime]:
        try:
            san: x509.GeneralName = x509.IPAddress(ipaddress.ip_address(host))
        except ValueError:
            san = x509.DNSName(host)
        now = _now()
        expires = now + dt.timedelta(days=LEAF_DAYS)
        ca = self._ca
        cert = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host[:64])]))
            .issuer_name(ca.cert.subject)
            .public_key(self._leaf_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(hours=1))
            .not_valid_after(min(expires, ca.cert.not_valid_after_utc))
            .add_extension(x509.SubjectAlternativeName([san]), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(ca.key.public_key()),
                critical=False,
            )
            .sign(ca.key, hashes.SHA256())
        )
        return cert.public_bytes(serialization.Encoding.PEM), expires

    def context(self, host: str) -> ssl.SSLContext:
        """A server context presenting a certificate for `host` (cached, LRU)."""
        host = host.strip("[]").lower()
        with self._lock:
            hit = self._cache.get(host)
            if hit and hit[1] - _now() > dt.timedelta(hours=1):
                self._cache.move_to_end(host)
                return hit[0]
        cert, expires = self._leaf(host)
        key = self._leaf_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.set_alpn_protocols(["http/1.1"])  # one request at a time; no HTTP/2 framing
        fd, name = tempfile.mkstemp(prefix="cw-leaf-", suffix=".pem")
        try:
            with os.fdopen(fd, "wb") as f:  # mkstemp makes it owner-only
                f.write(cert + self.ca_pem + key)
            ctx.load_cert_chain(name)
        finally:
            with contextlib.suppress(OSError):
                Path(name).unlink()
        with self._lock:
            self._cache[host] = (ctx, expires)
            self._cache.move_to_end(host)
            while len(self._cache) > MAX_CACHED:
                self._cache.popitem(last=False)
        return ctx


def upstream_context() -> ssl.SSLContext:
    """How egress checks the real site: public roots, hostname checked, TLS 1.2 or later."""
    ctx = ssl.create_default_context()
    with contextlib.suppress(ImportError, OSError, ssl.SSLError):
        import certifi

        ctx.load_verify_locations(certifi.where())
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.set_alpn_protocols(["http/1.1"])
    return ctx
