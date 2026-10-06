"""This instance's peer identity: one Ed25519 key and a self-signed TLS cert.

The key (``identity/ed25519.key``, PKCS8 PEM, 0600) IS the instance's identity:
peers pin its raw 32-byte public key. The cert (``identity/cert.pem``, CN
``mindflock-peer``, 10 years) only exists so the TLS 1.3 listener has something
to present; it is regenerated whenever it is missing, unreadable, expired or not
over our key. Nobody validates the chain — peers compare the raw key instead.

The key is never silently replaced: a corrupt or foreign-owned key file raises
:class:`IdentityError` (replacing it would orphan every link without telling
the user). Delete the file by hand to start over.
"""

from __future__ import annotations

import datetime
import hashlib
import os
import stat

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.x509.oid import NameOID

from backend.peer import paths

__all__ = [
    "Identity",
    "IdentityError",
    "load_or_create",
    "fingerprint_of",
    "pub_from_cert_der",
    "KEY_NAME",
    "CERT_NAME",
]

KEY_NAME = "ed25519.key"
CERT_NAME = "cert.pem"
CERT_CN = "mindflock-peer"
CERT_VALID_DAYS = 3650
_MAX_FILE = 64 * 1024


class IdentityError(Exception):
    """The identity key exists but cannot be used safely."""


def fingerprint_of(pub: bytes) -> bytes:
    """``sha256(pub)[:16]`` — what a pairing code carries."""
    return hashlib.sha256(pub).digest()[:16]


def _raw_pub(key: Ed25519PublicKey) -> bytes:
    return key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def pub_from_cert_der(der: bytes | None) -> bytes | None:
    """The raw Ed25519 public key inside a DER cert, or None for anything
    else (unparseable, another key type). Never raises."""
    if not der:
        return None
    try:
        key = x509.load_der_x509_certificate(bytes(der)).public_key()
    except Exception:
        return None
    if not isinstance(key, Ed25519PublicKey):
        return None
    return _raw_pub(key)


class Identity:
    def __init__(self, sk: Ed25519PrivateKey, key_path: str, cert_path: str):
        self._sk = sk
        self.pub: bytes = _raw_pub(sk.public_key())
        self.key_path = key_path
        self.cert_path = cert_path

    def sign(self, data: bytes) -> bytes:
        return self._sk.sign(bytes(data))

    def fingerprint(self) -> bytes:
        return fingerprint_of(self.pub)

    @staticmethod
    def verify(pub, sig, data) -> bool:
        """True only for a valid Ed25519 signature. Never raises."""
        try:
            if not isinstance(pub, (bytes, bytearray)) or len(pub) != 32:
                return False
            if not isinstance(sig, (bytes, bytearray)) or len(sig) != 64:
                return False
            if not isinstance(data, (bytes, bytearray)):
                return False
            Ed25519PublicKey.from_public_bytes(bytes(pub)).verify(
                bytes(sig), bytes(data)
            )
            return True
        except Exception:
            return False

    def __repr__(self) -> str:
        return f"Identity(fingerprint={self.fingerprint().hex()})"


def _write_new(path: str, data: bytes, mode: int) -> str:
    """Write ``data`` to a fresh tmp file next to ``path`` (O_EXCL, ``mode``),
    fsync it and return the tmp path."""
    tmp = f"{path}.tmp-{os.getpid()}-{os.urandom(4).hex()}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        os.fchmod(fd, mode)
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view) :]
        os.fsync(fd)
    finally:
        os.close(fd)
    return tmp


def _read_private_file(path: str) -> bytes | None:
    """Read a 0600 file we own. None when it doesn't exist. Tightens loose
    modes; refuses symlinks, non-regular files and files owned by others."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError as e:
        raise IdentityError(f"cannot open identity key: {e.strerror}") from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise IdentityError("identity key is not a regular file")
        if st.st_uid != os.geteuid():
            raise IdentityError("identity key is owned by another user")
        if st.st_mode & 0o077:
            os.fchmod(fd, 0o600)
        if st.st_size > _MAX_FILE:
            raise IdentityError("identity key file is too large")
        return os.read(fd, _MAX_FILE)
    finally:
        os.close(fd)


def _load_key(key_path: str) -> Ed25519PrivateKey | None:
    data = _read_private_file(key_path)
    if data is None:
        return None
    try:
        sk = serialization.load_pem_private_key(data, password=None)
    except Exception:
        raise IdentityError(
            f"identity key {key_path} is unreadable; delete it to create a new "
            "identity (existing links will stop working)"
        ) from None
    if not isinstance(sk, Ed25519PrivateKey):
        raise IdentityError("identity key is not an Ed25519 key")
    return sk


def _create_key(key_path: str) -> Ed25519PrivateKey:
    """Generate a key and publish it without clobbering: if another process
    won the race, its key is the one we use."""
    sk = Ed25519PrivateKey.generate()
    pem = sk.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    tmp = _write_new(key_path, pem, 0o600)
    try:
        os.link(tmp, key_path)
    except FileExistsError:
        pass
    finally:
        os.unlink(tmp)
    loaded = _load_key(key_path)
    if loaded is None:
        raise IdentityError("identity key vanished while being created")
    return loaded


def _cert_ok(cert_path: str, pub: bytes) -> bool:
    try:
        with open(cert_path, "rb") as f:
            data = f.read(_MAX_FILE)
        cert = x509.load_pem_x509_certificate(data)
        key = cert.public_key()
        if not isinstance(key, Ed25519PublicKey) or _raw_pub(key) != pub:
            return False
        soon = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=1)
        return cert.not_valid_after_utc > soon
    except Exception:
        return False


def _make_cert(sk: Ed25519PrivateKey) -> bytes:
    now = datetime.datetime.now(datetime.timezone.utc)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, CERT_CN)])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(sk.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=CERT_VALID_DAYS))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(sk, None)
    )
    return cert.public_bytes(serialization.Encoding.PEM)


def load_or_create(directory: str | None = None) -> Identity:
    """Load this instance's identity, creating the key and cert on first use.
    ``directory`` defaults to :func:`paths.identity_dir`."""
    if directory is None:
        paths.ensure_dir(paths.peer_root())
        directory = paths.identity_dir()
    paths.ensure_dir(directory)
    key_path = os.path.join(directory, KEY_NAME)
    cert_path = os.path.join(directory, CERT_NAME)

    sk = _load_key(key_path) or _create_key(key_path)
    ident = Identity(sk, key_path, cert_path)
    if not _cert_ok(cert_path, ident.pub):
        tmp = _write_new(cert_path, _make_cert(sk), 0o644)
        os.replace(tmp, cert_path)
    return ident
