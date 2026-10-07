import datetime
import hashlib
import os
import stat

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.x509.oid import NameOID

from backend.peer import identity


def _mode(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def _cert(path):
    with open(path, "rb") as f:
        return x509.load_pem_x509_certificate(f.read())


def test_creates_key_and_cert_with_tight_perms(tmp_path):
    d = tmp_path / "id"
    ident = identity.load_or_create(str(d))
    assert len(ident.pub) == 32
    assert _mode(d) == 0o700
    assert _mode(ident.key_path) == 0o600
    with open(ident.key_path, "rb") as f:
        pem = f.read()
    assert pem.startswith(b"-----BEGIN PRIVATE KEY-----")  # PKCS8
    cert = _cert(ident.cert_path)
    assert (
        cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value
        == "mindflock-peer"
    )
    assert (
        identity.pub_from_cert_der(cert.public_bytes(serialization.Encoding.DER))
        == ident.pub
    )
    days = (cert.not_valid_after_utc - cert.not_valid_before_utc).days
    assert days >= 3650


def test_default_location_under_peer_home(peer_home):
    ident = identity.load_or_create()
    assert ident.key_path == os.path.join(str(peer_home), "identity", identity.KEY_NAME)
    assert _mode(peer_home) == 0o700
    assert _mode(peer_home / "identity") == 0o700


def test_reload_keeps_identity(tmp_path):
    a = identity.load_or_create(str(tmp_path))
    b = identity.load_or_create(str(tmp_path))
    assert a.pub == b.pub
    assert a.fingerprint() == b.fingerprint() == hashlib.sha256(a.pub).digest()[:16]


def test_sign_verify():
    ident_a = identity.Identity(Ed25519PrivateKey.generate(), "k", "c")
    sig = ident_a.sign(b"data")
    assert identity.Identity.verify(ident_a.pub, sig, b"data")
    assert not identity.Identity.verify(ident_a.pub, sig, b"datA")
    other = identity.Identity(Ed25519PrivateKey.generate(), "k", "c")
    assert not identity.Identity.verify(other.pub, sig, b"data")
    bad = bytearray(sig)
    bad[0] ^= 1
    assert not identity.Identity.verify(ident_a.pub, bytes(bad), b"data")


@pytest.mark.parametrize(
    "pub,sig,data",
    [
        (None, b"x" * 64, b"d"),
        (b"x" * 31, b"x" * 64, b"d"),
        (b"x" * 32, b"x" * 63, b"d"),
        (b"x" * 32, None, b"d"),
        ("a" * 32, b"x" * 64, b"d"),
        (b"\x00" * 32, b"\x00" * 64, b"d"),
        (b"\xff" * 32, b"\xff" * 64, b"d"),
        (b"x" * 32, b"x" * 64, None),
        (b"x" * 32, b"x" * 64, "str"),
    ],
)
def test_verify_never_raises(pub, sig, data):
    assert identity.Identity.verify(pub, sig, data) is False


def test_repr_has_no_key_material(tmp_path):
    ident = identity.load_or_create(str(tmp_path))
    assert "PRIVATE" not in repr(ident)
    assert ident.fingerprint().hex() in repr(ident)


def test_loose_key_mode_is_tightened(tmp_path):
    ident = identity.load_or_create(str(tmp_path))
    os.chmod(ident.key_path, 0o644)
    identity.load_or_create(str(tmp_path))
    assert _mode(ident.key_path) == 0o600


def test_corrupt_key_raises_and_is_not_overwritten(tmp_path):
    ident = identity.load_or_create(str(tmp_path))
    with open(ident.key_path, "wb") as f:
        f.write(b"garbage")
    with pytest.raises(identity.IdentityError):
        identity.load_or_create(str(tmp_path))
    with open(ident.key_path, "rb") as f:
        assert f.read() == b"garbage"


def test_non_ed25519_key_refused(tmp_path):
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    path = tmp_path / identity.KEY_NAME
    path.write_bytes(pem)
    os.chmod(path, 0o600)
    with pytest.raises(identity.IdentityError):
        identity.load_or_create(str(tmp_path))


def test_symlinked_key_refused(tmp_path):
    real = identity.load_or_create(str(tmp_path / "real"))
    d = tmp_path / "other"
    d.mkdir()
    os.symlink(real.key_path, d / identity.KEY_NAME)
    with pytest.raises(identity.IdentityError):
        identity.load_or_create(str(d))


def test_unreadable_cert_is_regenerated(tmp_path):
    ident = identity.load_or_create(str(tmp_path))
    with open(ident.cert_path, "wb") as f:
        f.write(b"not a cert")
    again = identity.load_or_create(str(tmp_path))
    cert = _cert(again.cert_path)
    assert (
        identity.pub_from_cert_der(cert.public_bytes(serialization.Encoding.DER))
        == ident.pub
    )


def test_expired_cert_is_regenerated(tmp_path):
    ident = identity.load_or_create(str(tmp_path))
    with open(ident.key_path, "rb") as f:
        sk = serialization.load_pem_private_key(f.read(), None)
    past = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=30)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "mindflock-peer")])
    old = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(sk.public_key())
        .serial_number(1)
        .not_valid_before(past - datetime.timedelta(days=10))
        .not_valid_after(past)
        .sign(sk, None)
    )
    with open(ident.cert_path, "wb") as f:
        f.write(old.public_bytes(serialization.Encoding.PEM))
    identity.load_or_create(str(tmp_path))
    assert _cert(ident.cert_path).not_valid_after_utc > datetime.datetime.now(
        datetime.timezone.utc
    )


def test_cert_over_another_key_is_regenerated(tmp_path):
    ident = identity.load_or_create(str(tmp_path))
    stranger = identity.load_or_create(str(tmp_path / "s"))
    with open(stranger.cert_path, "rb") as f:
        foreign = f.read()
    with open(ident.cert_path, "wb") as f:
        f.write(foreign)
    identity.load_or_create(str(tmp_path))
    cert = _cert(ident.cert_path)
    assert (
        identity.pub_from_cert_der(cert.public_bytes(serialization.Encoding.DER))
        == ident.pub
    )


def test_pub_from_cert_der_rejects_garbage_and_other_key_types():
    assert identity.pub_from_cert_der(None) is None
    assert identity.pub_from_cert_der(b"") is None
    assert identity.pub_from_cert_der(b"\x30\x82garbage") is None
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "x")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    assert (
        identity.pub_from_cert_der(cert.public_bytes(serialization.Encoding.DER))
        is None
    )


def test_fingerprint_of():
    pub = bytes(range(32))
    assert identity.fingerprint_of(pub) == hashlib.sha256(pub).digest()[:16]
