"""The encrypted address record (backend/peer/fleet_record.py) —
docs/devices-anywhere.md §2: round trip, and every way a record must be
ignored (tampered, replayed, stale, another fleet/epoch/slot, bad address)."""

from __future__ import annotations

import base64

import pytest

from backend.peer import fleet_record as fr

KEY = "k" * 43
FID = "0123456789abcdef"
URL = "wss://quiet-words-here.trycloudflare.com/abcdefghijklmnopqrstuvwxyz"
FP = "ab" * 16
NOW = 1_800_000_000.0
MEMBERS = {"laptop", "rig"}


def _keys(key=KEY, fid=FID, epoch=3):
    return fr.derive(key, fid, epoch)


def _seal(keys=None, device="laptop", url=URL, ctr=5, ts=NOW):
    keys = keys or _keys()
    return fr.file_name(keys, device), fr.seal(
        keys, fr.Record(device=device, url=url, fp=FP, ctr=ctr, ts=ts)
    )


def _open(name, text, keys=None, **kw):
    kw.setdefault("members", MEMBERS)
    kw.setdefault("self_key", "rig")
    kw.setdefault("now", NOW)
    return fr.open_record(keys or _keys(), name, text, **kw)


def _reason(name, text, **kw):
    with pytest.raises(fr.RecordError) as e:
        _open(name, text, **kw)
    return e.value.reason


def test_round_trip():
    name, text = _seal()
    rec = _open(name, text)
    assert (rec.device, rec.url, rec.fp, rec.ctr, rec.ts) == ("laptop", URL, FP, 5, NOW)


def test_reveals_nothing_readable():
    name, text = _seal()
    for clear in ("laptop", "trycloudflare", "quiet-words", "abcdefghijklmnop", FID):
        assert clear not in name and clear not in text
    # The file name is not a plain hash of the device name either.
    assert fr.slot_of(name) and len(fr.slot_of(name)) == 32


def test_each_seal_is_fresh():
    a = _seal()[1]
    b = _seal()[1]
    assert a != b  # random nonce


@pytest.mark.parametrize("pos", [6, 20, -1])
def test_tampered_byte_rejected(pos):
    name, text = _seal()
    raw = bytearray(base64.urlsafe_b64decode(text[5:] + "=" * (-len(text[5:]) % 4)))
    raw[pos] ^= 0x01
    bad = fr.PREFIX + base64.urlsafe_b64encode(bytes(raw)).decode().rstrip("=")
    assert _reason(name, bad) in ("tag", "version")


def test_wrong_fleet_key_rejected():
    name, text = _seal(keys=_keys(key="x" * 43))
    assert _reason(name, text) == "tag"


def test_other_fleet_id_rejected():
    name, text = _seal(keys=_keys(fid="fedcba9876543210"))
    assert _reason(name, text) in ("tag", "format")


def test_old_epoch_rejected():
    old = _keys(epoch=2)
    name = fr.file_name(_keys(), "laptop")  # put it where the new one lives
    text = fr.seal(old, fr.Record("laptop", URL, FP, 5, NOW))
    assert _reason(name, text) == "tag"


def test_copied_into_another_devices_slot_rejected():
    _name, text = _seal(device="laptop")
    other = fr.file_name(_keys(), "rig")
    assert _reason(other, text) == "tag"  # the slot is in the AAD


def test_replay_of_older_counter_rejected():
    name, text = _seal(ctr=5)
    assert _reason(name, text, last_ctr=6) == "replay"
    assert _open(name, text, last_ctr=5).ctr == 5  # the same record again is fine


def test_stale_and_future_rejected():
    name, text = _seal(ts=NOW - fr.MAX_AGE - 1)
    assert _reason(name, text) == "stale"
    name, text = _seal(ts=NOW + fr.MAX_SKEW + 60)
    assert _reason(name, text) == "future"


def test_non_member_or_self_rejected():
    name, text = _seal(device="gone")
    assert _reason(name, text) == "member"
    name, text = _seal(device="rig")
    assert _reason(name, text) == "member"  # never our own


@pytest.mark.parametrize(
    "url",
    [
        "wss://evil.example.com/abcdefghijklmnopqrstuvwxyz",
        "wss://api.trycloudflare.com.evil.com/abc",
        "wss://a.trycloudflare.com:8443/abc",
        "wss://127.0.0.1:9999/abc",
        "1.2.3.4:443",
    ],
)
def test_bad_address_rejected(url):
    name, text = _seal(url=url)
    assert _reason(name, text) == "address"


def test_loopback_address_only_in_test_mode():
    name, text = _seal(url="wss://127.0.0.1:9999/abcdefghij")
    assert _reason(name, text) == "address"
    assert _open(name, text, loopback=True).url.startswith("wss://127.0.0.1")


def test_garbage_and_unknown_version():
    name = fr.file_name(_keys(), "laptop")
    assert _reason(name, "hello") == "format"
    assert _reason(name, fr.PREFIX + "!!!") == "format"
    blob = bytes([2]) + b"\0" * 40
    assert (
        _reason(name, fr.PREFIX + base64.urlsafe_b64encode(blob).decode()) == "version"
    )
    assert _reason("README.md", _seal()[1]) == "format"


def test_derive_refuses_empty():
    with pytest.raises(ValueError):
        fr.derive("", FID, 1)
    with pytest.raises(ValueError):
        fr.derive(KEY, "", 1)


def test_next_ctr_monotonic():
    assert fr.next_ctr(10, now=0.001) == 11
    assert fr.next_ctr(10, now=NOW) == int(NOW * 1000)
