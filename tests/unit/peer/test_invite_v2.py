"""``mfp2:`` relay codes, peer addresses, and the link store's relay
addresses. Old ``mfp1:`` codes keep parsing; everything malformed is a
``ValueError`` that never echoes the code."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from backend.peer import invite, store
from backend.peer.addr import PeerAddr, check_path, parse_addr
from backend.peer.invite import encode_code, parse_code

IID, SECRET, FP = b"\x01" * 8, b"\x02" * 20, b"\x03" * 16
PATH = "/" + "a" * 26


def _b32(data: bytes) -> str:
    return base64.b32encode(data).decode().rstrip("=").lower()


def _code(prefix: str, payload: bytes) -> str:
    cs = _b32(hashlib.sha256(payload).digest()[:3])[:4]
    return f"{prefix}{_b32(payload)}-{cs}"


def _v2(
    carrier=2, host=b"x.trycloudflare.com", port=443, path=PATH.encode(), tail=None
):
    tail = tail if tail is not None else IID + SECRET + FP
    return (
        bytes([carrier, len(host)])
        + host
        + port.to_bytes(2, "big")
        + bytes([len(path)])
        + path
        + tail
    )


def test_relay_code_roundtrip():
    code = encode_code("x.trycloudflare.com", 443, IID, SECRET, FP, relay_path=PATH)
    assert code.startswith("mfp2:")
    info = parse_code(code)
    assert (info.carrier, info.host, info.port, info.path) == (
        "wss",
        "x.trycloudflare.com",
        443,
        PATH,
    )
    assert (info.invite_id, info.secret, info.server_fp) == (IID.hex(), SECRET, FP)
    assert str(info.addr) == "wss://x.trycloudflare.com:443" + PATH
    assert parse_code("  " + code.upper() + "\n") == info


def test_direct_codes_are_still_v1():
    code = encode_code("100.64.0.1", 8799, IID, SECRET, FP)
    assert code.startswith("mfp1:")
    info = parse_code(code)
    assert (info.carrier, info.path, str(info.addr)) == ("tcp", "", "100.64.0.1:8799")


def test_v2_tcp_carrier_parses():
    info = parse_code(
        _code("mfp2:", _v2(carrier=1, host=b"10.0.0.1", port=8799, path=b""))
    )
    assert (info.carrier, str(info.addr)) == ("tcp", "10.0.0.1:8799")


@pytest.mark.parametrize(
    "payload",
    [
        _v2(carrier=0),
        _v2(carrier=3),
        _v2(carrier=1),  # tcp with a path
        _v2(carrier=2, path=b""),  # relay without a path
        _v2(path=b"/a/../b"),
        _v2(path=b"/a/./b"),
        _v2(path=b"/a%2fb"),
        _v2(path=b"/a?x=1"),
        _v2(path=b"/a#f"),
        _v2(path=b"a/b"),
        _v2(path=b"/a//b"),
        _v2(path=b"/a/"),
        _v2(path=b"/\xff"),
        _v2(path=b"/" + b"a" * 250),
        _v2(host=b""),
        _v2(host=b"bad host"),
        _v2(host=b"0.0.0.0"),
        _v2(host=b"-x.example"),
        _v2(port=0),
        _v2(tail=b"\x00" * 10),  # truncated
        _v2() + b"\x00",  # trailing junk
        b"",
        b"\x02",
        b"\x02\x05ab",
        _v2()[:-1],
    ],
)
def test_malformed_v2_is_rejected(payload):
    code = _code("mfp2:", payload)
    with pytest.raises(ValueError) as exc:
        parse_code(code)
    assert code.split(":")[1][:20] not in str(exc.value)


def test_prefixes_do_not_cross():
    # A v2 payload behind the v1 prefix (and the reverse) is malformed.
    v1 = encode_code("10.0.0.1", 8799, IID, SECRET, FP)
    with pytest.raises(ValueError):
        parse_code("mfp2:" + v1[5:])
    v2 = encode_code("x.example", 443, IID, SECRET, FP, relay_path=PATH)
    with pytest.raises(ValueError):
        parse_code("mfp1:" + v2[5:])
    with pytest.raises(ValueError):
        parse_code("mfp3:" + v2[5:])


def test_checksum_and_typos():
    code = encode_code("x.example", 443, IID, SECRET, FP, relay_path=PATH)
    body, cs = code.rsplit("-", 1)
    flipped = body[:-1] + ("a" if body[-1] != "a" else "b")
    with pytest.raises(ValueError):
        parse_code(flipped + "-" + cs)


def test_encode_refuses_bad_relay_paths():
    for bad in ("", "a", "/", "/a/../b", "/a?b", "/" + "a" * 300, "/a b"):
        with pytest.raises(ValueError):
            encode_code("x.example", 443, IID, SECRET, FP, relay_path=bad)


def test_invite_book_mints_relay_codes_and_checks_them():
    book = invite.InviteBook(FP)
    inv = book.create("x.trycloudflare.com", 443, relay_path=PATH)
    info = parse_code(inv.code)
    assert info.carrier == "wss" and info.server_fp == FP
    tr = b"transcript"
    proof = hmac.new(info.secret, tr, hashlib.sha256).digest()
    assert book.check(info.invite_id, proof, tr)
    assert not book.check(info.invite_id, proof, tr)  # single use


_label = st.from_regex(r"[a-z0-9](?:[a-z0-9-]{0,20}[a-z0-9])?", fullmatch=True)
_hosts = st.lists(_label, min_size=1, max_size=4).map(".".join)
_segments = st.from_regex(
    r"[A-Za-z0-9_~-][A-Za-z0-9._~-]{0,20}", fullmatch=True
).filter(lambda s: s not in (".", ".."))
_paths = st.lists(_segments, min_size=1, max_size=5).map(lambda s: "/" + "/".join(s))


@settings(max_examples=300)
@given(
    host=_hosts,
    port=st.integers(1, 65535),
    path=_paths,
    iid=st.binary(min_size=8, max_size=8),
    secret=st.binary(min_size=20, max_size=20),
    fp=st.binary(min_size=16, max_size=16),
)
def test_v2_roundtrip_property(host, port, path, iid, secret, fp):
    code = encode_code(host, port, iid, secret, fp, relay_path=path)
    info = parse_code(code)
    assert (info.host, info.port, info.path, info.carrier) == (host, port, path, "wss")
    assert (info.invite_id, info.secret, info.server_fp) == (iid.hex(), secret, fp)
    assert parse_addr(str(info.addr)) == info.addr


@settings(max_examples=1000)
@given(payload=st.binary(max_size=400))
def test_v2_fuzz_parse_is_strict(payload):
    """Any payload with a VALID checksum either parses into well-formed
    fields or raises ValueError — never anything else."""
    try:
        info = parse_code(_code("mfp2:", payload))
    except ValueError:
        return
    assert info.carrier in ("tcp", "wss")
    addr = info.addr  # re-validates host/port/path
    assert addr.port == info.port
    if info.carrier == "wss":
        check_path(info.path)
    assert len(info.secret) == 20 and len(info.server_fp) == 16
    assert len(info.invite_id) == 16


@settings(max_examples=500)
@given(text=st.text(max_size=300))
def test_parse_code_fuzz_text(text):
    try:
        parse_code(text)
    except ValueError as e:
        if len(text) > 12:
            assert text[5:] not in str(e) or not text[5:]


# -- addresses -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,want",
    [
        ("10.0.0.1:8799", PeerAddr("tcp", "10.0.0.1", 8799)),
        ("[fd7a::1]:8799", PeerAddr("tcp", "fd7a::1", 8799)),
        ("host.example:1", PeerAddr("tcp", "host.example", 1)),
        (
            "wss://x.trycloudflare.com/abc",
            PeerAddr("wss", "x.trycloudflare.com", 443, "/abc"),
        ),
        ("wss://x.example:8443/p/q", PeerAddr("wss", "x.example", 8443, "/p/q")),
        ("wss://[fd7a::1]:443/p", PeerAddr("wss", "fd7a::1", 443, "/p")),
    ],
)
def test_parse_addr(text, want):
    assert parse_addr(text) == want


@pytest.mark.parametrize(
    "text",
    [
        "",
        "host",
        "host:0",
        "host:65536",
        "0.0.0.0:80",
        "ws://x.example/p",
        "https://x.example/p",
        "wss://x.example",
        "wss://x.example/",
        "wss://x.example/a/../b",
        "wss://x.example/a?b",
        "wss://user@x.example/a",
        "wss://x.example:0/a",
        "wss://[nothex]:443/a",
        "wss://x.example/a b",
        "wss://x.example/a\n",
        "x" * 700,
        None,
        123,
    ],
)
def test_parse_addr_rejects(text):
    with pytest.raises(ValueError):
        parse_addr(text)


def test_public_form_hides_the_token():
    a = parse_addr("wss://x.trycloudflare.com/" + "t" * 26)
    assert "t" * 26 not in a.public() and a.public().startswith("wss://x.")


# -- the store ------------------------------------------------------------------------


def _link(addr):
    return store.Link(
        link_id="ab" * 16,
        peer_name="alice",
        peer_pub="cd" * 32,
        role="dialer",
        peer_addr=addr,
    )


def test_store_accepts_canonical_relay_addresses(tmp_path):
    st_ = store.LinkStore(str(tmp_path / "links.json"))
    addr = "wss://x.trycloudflare.com:443" + PATH
    st_.add(_link(addr))
    assert st_.get("ab" * 16).peer_addr == addr
    new = "wss://y.trycloudflare.com:443" + PATH
    assert st_.update("ab" * 16, peer_addr=new).peer_addr == new


@pytest.mark.parametrize(
    "addr",
    [
        "wss://x.trycloudflare.com" + PATH,  # not canonical (no explicit port)
        "wss://x.example:443/a/../b",
        "wss://x.example:443/",
        "ws://x.example:443/a",
        "wss://x.example:443/a?b",
    ],
)
def test_store_rejects_bad_relay_addresses(tmp_path, addr):
    with pytest.raises(ValueError):
        _link(addr).validate()


def test_a_tampered_links_file_with_a_bad_relay_address_loads_empty(tmp_path):
    path = tmp_path / "links.json"
    st_ = store.LinkStore(str(path))
    st_.add(_link("wss://x.example:443/abc"))
    path.write_text(path.read_text().replace("/abc", "/a/../etc"))
    os.chmod(path, 0o600)
    assert st_.list() == []
