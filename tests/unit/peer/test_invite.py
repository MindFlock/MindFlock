import base64
import hashlib
import hmac
import re

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from backend.peer import invite
from tests.unit.peer.conftest import FakeClock

FP = bytes(range(16))
IID = bytes.fromhex("0102030405060708")
SECRET = bytes(range(100, 120))


def _code(host="example.com", port=8799, iid=IID, secret=SECRET, fp=FP):
    return invite.encode_code(host, port, iid, secret, fp)


def _b32(data: bytes) -> str:
    return base64.b32encode(data).decode().rstrip("=").lower()


def _forge(payload: bytes) -> str:
    """A code with a VALID checksum over an arbitrary payload."""
    cs = _b32(hashlib.sha256(payload).digest()[:3])[:4]
    return f"mfp1:{_b32(payload)}-{cs}"


def _payload(host=b"example.com", port=8799, tail=IID + SECRET + FP):
    return bytes([len(host)]) + host + port.to_bytes(2, "big") + tail


# -- code format --------------------------------------------------------------


def test_roundtrip():
    code = _code()
    assert re.match(r"^mfp1:[a-z2-7]+-[a-z2-7]{4}$", code)
    info = invite.parse_code(code)
    assert (info.host, info.port, info.invite_id, info.secret, info.server_fp) == (
        "example.com",
        8799,
        IID.hex(),
        SECRET,
        FP,
    )


def test_layout_matches_spec():
    code = _code(host="10.0.0.5", port=443)
    body, cs = code[len("mfp1:") :].rsplit("-", 1)
    payload = base64.b32decode(body.upper() + "=" * (-len(body) % 8))
    assert (
        payload
        == bytes([8]) + b"10.0.0.5" + (443).to_bytes(2, "big") + IID + SECRET + FP
    )
    assert cs == _b32(hashlib.sha256(payload).digest()[:3])[:4]


@pytest.mark.parametrize(
    "host", ["127.0.0.1", "::1", "fe80::1", "a.b-c.example", "localhost"]
)
def test_hosts_roundtrip(host):
    assert invite.parse_code(_code(host=host)).host == host


def test_whitespace_and_case_tolerated():
    code = _code()
    assert invite.parse_code(f"  {code.upper()}\n").secret == SECRET


def test_typo_caught_by_checksum():
    code = _code()
    i = len("mfp1:") + 10
    typo = code[:i] + ("a" if code[i] != "a" else "b") + code[i + 1 :]
    with pytest.raises(ValueError, match="checksum"):
        invite.parse_code(typo)


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "mfp1:",
        "mfp2:abcd-abcd",
        "mfp1:abcdefgh",  # no checksum
        "mfp1:-abcd",
        "mfp1:abc!defg-abcd",
        "mfp1:abcdefgh-abc",
        "mfp1:abcdefgh-abcde",
        "mfp1:a-aaaa",  # impossible base32 length
        "mfp1:" + "a" * 2000 + "-aaaa",
    ],
)
def test_malformed_rejected(bad):
    with pytest.raises(ValueError):
        invite.parse_code(bad)


def test_non_string_rejected():
    with pytest.raises(ValueError):
        invite.parse_code(None)
    with pytest.raises(ValueError):
        invite.parse_code(b"mfp1:aaaa-aaaa")


@pytest.mark.parametrize(
    "payload",
    [
        _payload() + b"\x00",  # trailing byte
        _payload()[:-1],  # short
        bytes([0]) + (8799).to_bytes(2, "big") + IID + SECRET + FP,  # empty host
        _payload(port=0),
        _payload(host=b"0.0.0.0"),
        _payload(host=b"::"),
        _payload(host=b"224.0.0.1"),
        _payload(host=b"bad host"),
        _payload(host=b"evil\x00.com"),
        _payload(host=b"\xff\xfe"),
        _payload(host=b"-lead.example"),
    ],
)
def test_forged_payloads_with_valid_checksum_rejected(payload):
    with pytest.raises(ValueError):
        invite.parse_code(_forge(payload))


def test_errors_never_echo_the_secret():
    code = _code()
    for bad in (
        code[:-1] + ("a" if code[-1] != "a" else "b"),
        code + "x",
        code.replace("mfp1", "mfp9"),
    ):
        with pytest.raises(ValueError) as e:
            invite.parse_code(bad)
        msg = str(e.value)
        assert (
            code[5:] not in msg
            and SECRET.hex() not in msg
            and _b32(SECRET)[:8] not in msg
        )


def test_codeinfo_repr_hides_secret():
    info = invite.parse_code(_code())
    assert SECRET.hex() not in repr(info) and repr(SECRET) not in repr(info)


def test_encode_rejects_bad_fields():
    with pytest.raises(ValueError):
        _code(host="0.0.0.0")
    with pytest.raises(ValueError):
        _code(port=0)
    with pytest.raises(ValueError):
        _code(port=True)
    with pytest.raises(ValueError):
        _code(secret=b"short")


@settings(max_examples=300, deadline=None)
@given(st.text(max_size=600))
def test_parse_fuzz_text_only_valueerror(s):
    try:
        invite.parse_code(s)
    except ValueError:
        pass


@settings(max_examples=300, deadline=None)
@given(st.binary(min_size=0, max_size=400))
def test_parse_fuzz_forged_payloads(payload):
    try:
        info = invite.parse_code(_forge(payload))
    except ValueError:
        return
    # Anything accepted is exactly re-encodable.
    assert invite.encode_code(
        info.host, info.port, bytes.fromhex(info.invite_id), info.secret, info.server_fp
    ) == _forge(payload)


# -- InviteBook ---------------------------------------------------------------


def _book(**kw):
    clock = FakeClock()
    return invite.InviteBook(FP, clock=clock, **kw), clock


def _proof(code: str, transcript: bytes) -> bytes:
    return hmac.new(invite.parse_code(code).secret, transcript, hashlib.sha256).digest()


def test_check_success_is_single_use():
    book, _ = _book()
    inv = book.create("127.0.0.1", 9000)
    tr = b"transcript"
    assert book.check(inv.invite_id, _proof(inv.code, tr), tr)
    assert not book.check(inv.invite_id, _proof(inv.code, tr), tr)
    assert book.active() == []


def test_proof_is_bound_to_transcript():
    book, _ = _book()
    inv = book.create("127.0.0.1", 9000)
    assert not book.check(inv.invite_id, _proof(inv.code, b"a"), b"b")
    assert book.check(inv.invite_id, _proof(inv.code, b"b"), b"b")


def test_code_embeds_fingerprint_and_address():
    book, _ = _book()
    info = invite.parse_code(book.create("192.168.1.2", 8799).code)
    assert (info.host, info.port, info.server_fp) == ("192.168.1.2", 8799, FP)


def test_five_failures_destroy():
    book, _ = _book()
    inv = book.create("127.0.0.1", 9000)
    for _ in range(5):
        assert not book.check(inv.invite_id, b"\x00" * 32, b"t")
    assert book.active() == []
    assert not book.check(inv.invite_id, _proof(inv.code, b"t"), b"t")


def test_four_failures_then_success():
    book, _ = _book()
    inv = book.create("127.0.0.1", 9000)
    for _ in range(4):
        assert not book.check(inv.invite_id, b"\x00" * 32, b"t")
    assert book.check(inv.invite_id, _proof(inv.code, b"t"), b"t")


def test_expiry_uses_injected_monotonic_clock():
    book, clock = _book()
    inv = book.create("127.0.0.1", 9000, ttl_s=60)
    clock.advance(59.9)
    assert [a["invite_id"] for a in book.active()] == [inv.invite_id]
    clock.advance(0.2)
    assert book.active() == []
    assert not book.check(inv.invite_id, _proof(inv.code, b"t"), b"t")


def test_unknown_and_garbage_ids():
    book, _ = _book()
    inv = book.create("127.0.0.1", 9000)
    p = _proof(inv.code, b"t")
    assert not book.check("0" * 16, p, b"t")
    assert not book.check(None, p, b"t")
    assert not book.check(inv.invite_id.upper(), p, b"t")
    assert not book.check(inv.invite_id, None, b"t")
    assert not book.check(inv.invite_id, p.hex(), b"t")
    assert book.check(inv.invite_id, p, b"t")


def test_revoke():
    book, _ = _book()
    inv = book.create("127.0.0.1", 9000)
    assert book.revoke(inv.invite_id)
    assert not book.revoke(inv.invite_id)
    assert not book.check(inv.invite_id, _proof(inv.code, b"t"), b"t")


def test_active_and_reprs_never_show_secret():
    book, _ = _book()
    inv = book.create("127.0.0.1", 9000)
    secret = invite.parse_code(inv.code).secret
    listed = book.active()
    assert set(listed[0]) == {"invite_id", "expires_at", "expires_in"}
    for text in (repr(listed), repr(inv), repr(book.__dict__)):
        assert inv.code not in text and secret.hex() not in text


def test_global_rate_limit():
    book, clock = _book(pair_rate=3, pair_window_s=60)
    inv = book.create("127.0.0.1", 9000)
    other = book.create("127.0.0.1", 9000)
    for _ in range(3):
        book.check("f" * 16, b"x" * 32, b"t")
    # Even a correct proof is refused while limited, and doesn't count as a failure.
    assert not book.check(inv.invite_id, _proof(inv.code, b"t"), b"t")
    assert not book.check(other.invite_id, _proof(other.code, b"t"), b"t")
    clock.advance(61)
    assert book.check(inv.invite_id, _proof(inv.code, b"t"), b"t")


def test_rate_limited_attempts_do_not_burn_invites():
    book, clock = _book(pair_rate=1)
    inv = book.create("127.0.0.1", 9000)
    book.check("f" * 16, b"x" * 32, b"t")
    for _ in range(10):
        book.check(inv.invite_id, b"x" * 32, b"t")  # all refused by the limiter
    clock.advance(61)
    assert book.check(inv.invite_id, _proof(inv.code, b"t"), b"t")


@pytest.mark.parametrize("ttl", [0, -1, 601, True, "60", float("nan")])
def test_ttl_bounds(ttl):
    book, _ = _book()
    with pytest.raises(ValueError):
        book.create("127.0.0.1", 9000, ttl_s=ttl)


def test_max_active():
    book, _ = _book(max_active=2)
    book.create("127.0.0.1", 9000)
    book.create("127.0.0.1", 9000)
    with pytest.raises(ValueError):
        book.create("127.0.0.1", 9000)


def test_create_rejects_unreachable_host():
    book, _ = _book()
    with pytest.raises(ValueError):
        book.create("0.0.0.0", 9000)


def test_secrets_are_unique():
    book, _ = _book(max_active=32)
    codes = {
        invite.parse_code(book.create("127.0.0.1", 9000).code).secret for _ in range(20)
    }
    assert len(codes) == 20
