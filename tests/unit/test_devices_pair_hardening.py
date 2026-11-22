"""Pairing a new device through the tunnel: the independent security review's
findings (M1, M2, L1-L7) and its undetermined items, one test (or more) each.
Loopback carrier, folder-free scratch home — never a real tunnel, device or
GitHub (as ``test_devices_pair.py``, whose harness this reuses)."""

from __future__ import annotations

import asyncio
import io
import logging
import time

import pytest

from backend.peer import fleet_pair as fp
from backend.peer import fleet_record as fr
from backend.peer import fleet_tunnel as ft
from backend.peer import wire
from backend.web.core import devices_anywhere as da
from backend.web.core import devices_pair as dp
from tests.unit.test_devices_pair import (  # noqa: F401 — fixtures
    FLEET_ID,
    FLEET_KEY,
    FP,
    IID,
    TOKEN,
    Inviter,
    _bundle,
    _code,
    _fake_pair,
    _finalize_earlier_loops,
    _join,
    _pipe,
    _wait_join,
    home,
    inviter,
)

pytestmark = pytest.mark.filterwarnings(
    "ignore::pytest.PytestUnraisableExceptionWarning"
)


def _as(monkeypatch, key, host=None):
    from backend.web.core import remote

    monkeypatch.setattr(
        remote,
        "self_identity",
        lambda: {
            "key": key,
            "host": host or key.title(),
            "dns": "",
            "ip": "",
            "os": "",
        },
    )


def _events(monkeypatch, fleet):
    got = []
    monkeypatch.setattr(fleet, "_emit", lambda ev, **d: got.append((ev, d)))
    return got


def _keys():
    return fr.derive(FLEET_KEY, FLEET_ID, 1)


# --------------------------------------------------------------------------- #
# M1 — no group key leaves without a trace
# --------------------------------------------------------------------------- #
async def test_m1_pending_is_recorded_before_the_bundle_and_is_no_member(
    home, monkeypatch
):
    fleet, _calls = home
    _as(monkeypatch, "laptop")
    fleet.create()
    b = dp._make_bundle("mini", "Mini", "")
    dp._begin("mini", "Mini", "", False, b)
    st = dp.pairing_status()
    assert [p["device"] for p in st["pending"]] == ["mini"]
    # Pending is not membership: on no roster, so the members' handshake
    # (which checks the roster) can't let it in under that name.
    assert "mini" not in fleet.live_members() and "mini" not in da._members()
    assert "mini" not in fleet.roster()["members"]
    # Durable: on disk, beside fleet.json, and on Your devices.
    assert '"mini"' in dp._store_path().read_text()
    assert fleet.status(True)["pairing"]["pending"][0]["device"] == "mini"


async def test_m1_an_unfinished_join_raises_a_durable_alarm(home, monkeypatch):
    fleet, _calls = home
    _as(monkeypatch, "laptop")
    fleet.create()
    ev = _events(monkeypatch, fleet)
    dp._begin("mini", "Mini", "", False, dp._make_bundle("mini", "Mini", ""))
    dp._unfinished("mini", "Mini", "aborted")
    st = dp.pairing_status()
    assert st["pending"] == []
    (alert,) = st["alerts"]
    assert alert["outcome"] == "aborted" and alert["host"] == "Mini"
    assert "change your devices' key" in alert["text"]
    assert [e for e, _ in ev] == ["device.pair_unfinished"]
    assert ev[0][1]["detail"] == alert["text"]
    # It survives this process: a fresh read of the store has it.
    fleet._CACHE.update(sig=None, doc=None)
    assert dp.pairing_status()["alerts"][0]["id"] == alert["id"]
    assert fleet.status(True)["pairing"]["alerts"][0]["id"] == alert["id"]


async def test_m1_a_pairing_cut_off_by_a_restart_becomes_an_alarm(home, monkeypatch):
    fleet, _calls = home
    _as(monkeypatch, "laptop")
    fleet.create()
    ev = _events(monkeypatch, fleet)
    dp._begin("mini", "Mini", "", False, dp._make_bundle("mini", "Mini", ""))
    dp._sweep_stale(now=time.time() + dp.STALE_PENDING_S + 1)
    st = dp.pairing_status()
    assert st["pending"] == [] and st["alerts"][0]["outcome"] == "interrupted"
    assert ev and ev[0][0] == "device.pair_unfinished"


async def test_m1_change_key_rotates_for_every_member_and_clears(home, monkeypatch):
    fleet, _calls = home
    from backend.web.core import auth

    _as(monkeypatch, "laptop")
    fleet.create()
    dp._unfinished("mini", "Mini", "unconfirmed")
    rotated = []

    async def rotate_key(exclude=()):
        rotated.append("key")
        return {
            "rekeyed": ["rig"],
            "missed": [],
            "rotated": ["rig"],
            "rotate_failed": [],
        }

    monkeypatch.setattr(fleet, "rotate_key", rotate_key)
    monkeypatch.setattr(auth, "rotate_token", lambda: rotated.append("token") or "t")
    out = await dp.change_key()
    assert rotated == ["key", "token"]
    assert out["rekeyed"] == ["rig"] and "laptop" in out["rotated"]
    assert dp.pairing_status()["alerts"] == []


async def test_m1_dismiss_drops_one_alarm(home, monkeypatch):
    fleet, _calls = home
    _as(monkeypatch, "laptop")
    fleet.create()
    dp._unfinished("a", "A", "aborted")
    dp._unfinished("b", "B", "aborted")
    first = dp.pairing_status()["alerts"][0]["id"]
    assert dp.dismiss_alerts(first) == 1
    assert [a["device"] for a in dp.pairing_status()["alerts"]] == ["b"]


async def test_m1_the_real_joiner_hears_already_used(inviter):
    """Someone read the bundle with a stolen code and stopped; the person's
    own computer then pastes the same code."""
    code = inviter.code()

    def take_and_stop(_inviter, _bundle):
        raise fp.PairRefused("nope")  # read the key, then abort

    with pytest.raises(fp.PairError):
        await _join(code, device="thief", adopt=take_and_stop)
    await asyncio.sleep(0.05)
    assert inviter.unfinished == [("thief", "Mini", "aborted")]
    with pytest.raises(fp.PairError) as e:
        await _join(code, device="mini")
    assert e.value.reason == "used"
    assert "already used" in dp._error_text(e.value)
    assert inviter.reused == [("mini", "Mini")]


async def test_m1_a_bundle_holder_cannot_pass_the_members_handshake_as_itself(
    inviter,
):
    inviter.keys = _keys()

    def take_and_stop(_inviter, _bundle):
        raise fp.PairRefused("nope")

    with pytest.raises(fp.PairError):
        await _join(inviter.code(), device="thief", adopt=take_and_stop)
    with pytest.raises(ft.TunnelAuthError) as e:
        await ft.dial(
            inviter.url,
            inviter.ident.fingerprint().hex(),
            _keys(),
            "thief",
            "laptop",
            edge_ssl=None,
        )
    assert e.value.reason == "denied"


# --------------------------------------------------------------------------- #
# M2 — a hostile code: an explicit confirm, a safety number, nothing unticked
# --------------------------------------------------------------------------- #
async def test_m2_join_needs_an_explicit_confirm_and_sends_nothing_first(
    home, monkeypatch
):
    fleet, _calls = home
    called = []

    async def never(*a, **k):
        called.append(a)

    monkeypatch.setattr(fp, "pair", never)
    with pytest.raises(dp.NeedsConfirm) as e:
        await dp.join(_code(), reachable=True)
    info = e.value.info
    assert info["text"] == dp.CONFIRM_TEXT
    assert "run agents on this computer" in info["text"]
    assert info["safety"] == fp.safety_number(FP, IID.hex())
    assert info["safety"] in info["safety_text"]
    assert info["other_group"] is None
    assert called == [] and fleet.join_status()["state"] == "idle"


def test_m2_the_safety_number_is_the_same_on_both_screens_and_per_code():
    book = fp.PairBook()
    url = "wss://a-b.trycloudflare.com/%s" % TOKEN
    a = book.create(url, FP)
    b = book.create(url, FP)
    assert dp.preview(a["code"])["safety"] == a["safety"]
    assert a["safety"] != b["safety"]  # a new code, a new number
    assert len(a["safety"].replace(" ", "")) == 9


async def test_m2_another_group_must_be_confirmed_by_name(home, monkeypatch):
    fleet, _calls = home
    fleet.create()
    fleet.add_member("rig", "Rig", by="mini")
    with pytest.raises(dp.NeedsConfirm) as e:
        await dp.join(_code(), reachable=True, confirm=True)
    other = e.value.info["other_group"]
    assert other == {"count": 2, "hosts": ["Rig"]}
    assert "Rig" in e.value.info["leave_text"]
    # Confirmed by name: it leaves that group, then joins.
    left = []

    async def leave_fleet():
        left.append(True)
        fleet.leave()
        return {"ok": True}

    monkeypatch.setattr(fleet, "leave_fleet", leave_fleet)
    monkeypatch.setattr(da, "save_switch", lambda on: None)
    _fake_pair(monkeypatch)
    await dp.join(_code(), reachable=False, confirm=True, leave_group=True)
    st = await _wait_join(fleet)
    assert st["state"] == "joined", st
    assert left == [True] and fleet.fleet_id() == FLEET_ID


async def test_m2_an_unticked_box_turns_nothing_on(home, monkeypatch):
    fleet, _calls = home
    turned = []
    monkeypatch.setattr(fleet, "_enable_remote_control", lambda: turned.append("rc"))
    monkeypatch.setattr(da, "save_switch", lambda on: turned.append(("switch", on)))
    _fake_pair(monkeypatch)
    await dp.join(_code(), reachable=False, confirm=True)
    st = await _wait_join(fleet)
    assert st["state"] == "joined" and st["note"] == dp.NOTE_SWITCH_OFF
    assert turned == []  # neither remote control nor the switch


def test_m2_the_join_route_answers_409_until_confirmed(home, monkeypatch):
    from fastapi.testclient import TestClient

    from backend.web import server
    from backend.web.core import auth

    async def yes(scope):
        return True

    monkeypatch.setattr(auth, "may_configure", yes)
    monkeypatch.setattr(auth, "privileged", yes)
    client = TestClient(server.app)
    r = client.post("/api/devices/anywhere/join", json={"code": _code()})
    assert r.status_code == 409, r.text
    body = r.json()
    assert body["needs_confirm"] and body["safety"] == fp.safety_number(FP, IID.hex())


def test_m2_the_cli_asks_first_with_the_number(monkeypatch, capsys):
    import argparse

    from backend import cli

    posted = []
    info = {
        "error": dp.CONFIRM_TEXT,
        "needs_confirm": True,
        "safety": "123 456 789",
        "text": dp.CONFIRM_TEXT,
        "safety_text": dp.SAFETY_TEXT.format(safety="123 456 789"),
        "other_group": None,
        "leave_text": "",
    }

    def post(base, path, body, **k):
        posted.append(dict(body))
        if not body.get("confirm"):
            raise cli.client.ApiError(409, info["error"], info)
        return {"state": "joined", "host": "Anya"}

    monkeypatch.setattr(cli.client, "discover", lambda h, p: "http://127.0.0.1:1")
    monkeypatch.setattr(cli.client, "post", post)
    args = argparse.Namespace(
        host=None, port=None, text=[_code()], yes=False, no_anywhere=False
    )
    monkeypatch.setattr("builtins.input", lambda q: "n")
    assert cli._cmd_join(args) == 1
    assert len(posted) == 1
    err = capsys.readouterr().err
    assert "run agents on this computer" in err and "123 456 789" in err
    posted.clear()
    monkeypatch.setattr("builtins.input", lambda q: "y")
    assert cli._cmd_join(args) == 0
    assert posted[-1]["confirm"] is True and posted[-1]["leave_group"] is False


# --------------------------------------------------------------------------- #
# L1 — the name is checked again at commit, under the lock
# --------------------------------------------------------------------------- #
async def test_l1_commit_never_overwrites_a_member_added_meanwhile(home, monkeypatch):
    fleet, _calls = home
    _as(monkeypatch, "laptop")
    fleet.create()
    b = dp._make_bundle("mini", "Mini", "")
    dp._begin("mini", "Mini", "", False, b)
    fleet.add_member("mini", "Other Mini", by="laptop")  # joined another way
    with pytest.raises(fleet.NameTaken):
        await dp._commit("mini", "Mini", "", False)
    assert fleet.live_members()["mini"]["host"] == "Other Mini"


# --------------------------------------------------------------------------- #
# L2 — the tombstone bypass is recorded only when the join is confirmed
# --------------------------------------------------------------------------- #
async def test_l2_no_admit_for_a_removed_device_before_the_confirm(home, monkeypatch):
    fleet, _calls = home
    _as(monkeypatch, "laptop")
    fleet.create()
    fleet.add_member("rig", "Rig", by="laptop")
    fleet.remove_member("rig")
    b = dp._make_bundle("rig", "Rig", "")
    dp._begin("rig", "Rig", "", False, b)
    assert "rig" not in fleet.state()["admits"]  # bundle out, nothing recorded
    await dp._commit("rig", "Rig", "", False)
    assert "rig" in fleet.state()["admits"] and "rig" in fleet.live_members()


# --------------------------------------------------------------------------- #
# L3 — names: no bidi / zero-width / junk DNS, capped
# --------------------------------------------------------------------------- #
def test_l3_host_and_dns_are_cleaned():
    from backend.web.core import fleet

    host = fp.clean_host("Ev‮il​ Mac " + "x" * 200)
    assert "‮" not in host and "​" not in host and " " not in host
    assert len(host) <= fp.MAX_HOST and host.startswith("Evil Mac")
    assert fleet.clean_dns("mini.tail-1.ts.net", "mini") == "mini.tail-1.ts.net"
    for bad in (
        "mini.ex ample.net",
        "mini.exаmple.net",  # a Cyrillic a
        "mini.-x.net",
        "mini." + "a" * 64 + ".net",
        "mini.x‮.net",
    ):
        assert fleet.clean_dns(bad, "mini") == "", bad
    assert not fp.dns_ok("mini..net") and fp.dns_ok("") and fp.dns_ok("mini.ts.net")
    # A gossiped roster is cleaned on read too.
    rows = fleet._clean_members(
        {"rig": {"host": "R‮ig", "added_at": 1.0, "dns": "rig.а.net"}}
    )
    assert rows["rig"]["host"] == "Rig" and rows["rig"]["dns"] == ""


async def _raw_pair(host, dns):
    """serve_pair against a hand-written pair frame (any host/dns bytes)."""
    book = fp.PairBook()
    inv = book.create("wss://a-b.trycloudflare.com/%s" % TOKEN, FP)
    iid = inv["invite_id"]
    secret = book._entries[iid].secret
    (sr, sw), (cr, cw) = await _pipe()
    pub, sn, cn = b"P" * 32, b"s" * 32, b"c" * 32
    t = fp.transcript(pub, sn, cn, iid, "mini", host, dns, False)
    msg = {
        "t": "pair",
        "v": 1,
        "nonce": fp._b64(cn),
        "invite": iid,
        "device": "mini",
        "host": host,
        "dns": dns,
        "runs": False,
        "proof": fp._mac(secret, fp.CLIENT_LABEL, t),
    }
    made = []

    def make_bundle(d, h, n):
        made.append(h)
        return _bundle(d, h)

    async def commit(*a):
        pass

    task = asyncio.create_task(
        fp.serve_pair(
            sr,
            sw,
            server_pub=pub,
            sn=sn,
            msg=msg,
            book=book,
            self_key=lambda: "laptop",
            make_bundle=make_bundle,
            commit=commit,
            confirm_timeout=0.2,
        )
    )
    first = await wire.read_frame(cr, fp.BUNDLE_MAX_FRAME)
    cw.close()
    outcome = await asyncio.wait_for(task, 5)
    sw.close()
    return first, outcome, made


async def test_l3_the_inviter_stores_a_cleaned_host_and_refuses_junk_dns():
    first, _outcome, made = await _raw_pair("Ev‮il​", "")
    assert first["t"] == "ok" and made == ["Evil"]
    first, outcome, made = await _raw_pair("Mini", "mini.а.net")
    assert first == {"t": "denied"} and outcome == "denied" and made == []


# --------------------------------------------------------------------------- #
# L4 — old codes' paths die; junk can't starve a real pairing or the log
# --------------------------------------------------------------------------- #
async def test_l4_the_members_path_never_takes_a_pair_frame(inviter):
    # A code that (as before) named the members' path: refused there,
    # without the book even being asked.
    real = fp.parse_code(inviter.code(), loopback=True)
    on_members = fp.encode_code(
        inviter.url, real.fp, bytes.fromhex(real.invite_id), real.secret, loopback=True
    )
    with pytest.raises(fp.PairError) as e:
        await _join(on_members)
    assert e.value.reason == "denied"
    assert inviter.outcomes == [] and inviter.book.stats["ok"] == 0
    assert inviter.book.stats["unknown"] == inviter.book.stats["wrong"] == 0


async def test_l4_a_new_code_retires_the_old_codes_path(inviter):
    old = inviter.code()
    new = inviter.code()
    with pytest.raises((OSError, ConnectionError)) as e:
        await _join(old)
    assert not isinstance(e.value, fp.PairError)  # a 404 at the ingress
    assert inviter.server.stats["pair"] == 0
    (who, _b, _a), _g = await _join(new)
    assert who == "laptop"


async def test_l4_an_expired_path_is_gone(inviter):
    token_url = inviter.pair_url(ttl=0.05)
    code = inviter.book.create(
        token_url, inviter.ident.fingerprint(), loopback=True, tunnel=inviter.url
    )["code"]
    await asyncio.sleep(0.1)
    with pytest.raises((OSError, ConnectionError)) as e:
        await _join(code)
    assert not isinstance(e.value, fp.PairError)


def test_l4_unknown_ids_have_their_own_budget():
    book = fp.PairBook(rate=3, unknown_rate=3)
    inv = book.create("wss://a-b.trycloudflare.com/%s" % TOKEN, FP)
    secret = book._entries[inv["invite_id"]].secret
    for i in range(20):
        assert book.check("%016x" % i, "0" * 64, b"t") is None
    assert book.stats["limited"] > 0
    import hashlib
    import hmac

    good = hmac.new(secret, b"t", hashlib.sha256).hexdigest()
    assert book.check(inv["invite_id"], good, b"t") == secret


def test_l4_denied_is_logged_at_most_once_a_minute(caplog):
    dp._reset_for_tests()
    with caplog.at_level(logging.INFO, logger=dp.log.name):
        for _ in range(50):
            dp._log_outcome("denied")
        dp._log_outcome("added")
    lines = [r.getMessage() for r in caplog.records]
    assert sum("denied" in m for m in lines) == 1
    assert any(m.endswith("added") for m in lines)


# --------------------------------------------------------------------------- #
# L5 — a lost "added": both sides end up agreeing
# --------------------------------------------------------------------------- #
async def _members_check(inviter):
    async def check(who, bundle, addr):
        keys = fr.derive(bundle["key"], bundle["id"], int(bundle["epoch"]))
        try:
            _r, w = await ft.dial(
                addr,
                inviter.ident.fingerprint().hex(),
                keys,
                "mini",
                who,
                edge_ssl=None,
            )
        except ft.TunnelAuthError as err:
            return False if err.reason == "denied" else None
        w.transport.abort()
        return True

    return check


async def test_l5_lost_added_after_the_commit_keeps_the_join(inviter):
    inviter.keys = _keys()
    inviter.drop_added = True
    (who, bundle, addr), got = await _join(
        inviter.code(), check_joined=await _members_check(inviter)
    )
    assert who == "laptop" and got and inviter.committed
    assert inviter.outcomes == ["added"] and inviter.unfinished == []


async def test_l5_no_commit_means_the_joiner_drops_it_too(inviter):
    inviter.keys = _keys()
    inviter.commit_fails = True
    with pytest.raises(fp.PairError) as e:
        await _join(inviter.code(), check_joined=await _members_check(inviter))
    assert e.value.reason == "unconfirmed"  # the caller restores
    assert inviter.unfinished == [("mini", "Mini", "error")]  # and the alarm


async def test_l5_check_joined_reads_the_members_handshake(home, monkeypatch):
    code = fp.parse_code(_code(), loopback=True)
    b = _bundle()
    answers = iter(
        [
            ft.TunnelAuthError("denied"),
            ft.TunnelAuthError("denied"),
            ft.TunnelAuthError("denied"),
        ]
    )
    monkeypatch.setattr(dp, "CHECK_STEP", 0.0)

    async def dial(*a, **k):
        raise next(answers)

    monkeypatch.setattr(ft, "dial", dial)
    assert await dp._check_joined(code, "laptop", b, CODE_URL) is False

    class W:
        class transport:
            @staticmethod
            def abort():
                pass

    async def ok(*a, **k):
        return None, W()

    monkeypatch.setattr(ft, "dial", ok)
    assert await dp._check_joined(code, "laptop", b, CODE_URL) is True

    async def gone(*a, **k):
        raise OSError("no route")

    monkeypatch.setattr(ft, "dial", gone)
    assert await dp._check_joined(code, "laptop", b, CODE_URL) is None


CODE_URL = "wss://127.0.0.1:4567/%s" % TOKEN


# --------------------------------------------------------------------------- #
# L6 — the code need not be an argument
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("how", ["stdin", "tty"])
def test_l6_cli_reads_the_code_from_stdin_or_a_hidden_prompt(monkeypatch, how):
    import argparse
    import getpass
    import sys

    from backend import cli

    code = _code()
    posted = []
    monkeypatch.setattr(cli.client, "discover", lambda h, p: "http://127.0.0.1:1")
    monkeypatch.setattr(
        cli.client,
        "post",
        lambda base, path, body, **k: posted.append(body) or {"state": "joined"},
    )
    if how == "stdin":
        monkeypatch.setattr(sys, "stdin", io.StringIO(code + "\n"))
    else:

        class Tty(io.StringIO):
            def isatty(self):
                return True

        monkeypatch.setattr(sys, "stdin", Tty(""))
        monkeypatch.setattr(getpass, "getpass", lambda prompt="": code)
    args = argparse.Namespace(host=None, port=None, text=[], yes=True, no_anywhere=True)
    assert cli._cmd_join(args) == 0
    assert posted and posted[0]["code"] == code


# --------------------------------------------------------------------------- #
# L7 — who can read a live code, said truthfully
# --------------------------------------------------------------------------- #
def test_l7_the_docs_do_not_claim_own_browser_only():
    from pathlib import Path

    from backend.web.routes import devices_anywhere as routes

    doc = (
        Path(__file__).resolve().parents[2] / "docs" / "devices-anywhere.md"
    ).read_text()
    assert "shown only to this device's own browser" not in doc
    assert "access token" in doc and "trusted Tailscale" in doc
    assert "owner's own browser only" not in (routes.__doc__ or "")


# --------------------------------------------------------------------------- #
# The reviewer's undetermined items
# --------------------------------------------------------------------------- #
async def test_the_ingress_checks_the_path_before_the_tunnel_server(inviter):
    real = fp.parse_code(inviter.code(), loopback=True)
    wrong = "wss://127.0.0.1:%d/%s" % (inviter.port, da._new_token())
    code = fp.encode_code(
        wrong, real.fp, bytes.fromhex(real.invite_id), real.secret, loopback=True
    )
    with pytest.raises((OSError, ConnectionError)):
        await _join(code)
    assert inviter.ingress.stats["rejected"] >= 1
    assert inviter.server.stats["pair"] == 0 and inviter.server.stats["denied"] == 0


async def test_a_raising_self_key_answers_and_never_leaks(inviter):
    def boom():
        raise RuntimeError("identity unreadable")

    book = inviter.book
    code = inviter.code()

    async def pair(reader, writer, server_pub, sn, msg):
        inviter.outcomes.append(
            await fp.serve_pair(
                reader,
                writer,
                server_pub=server_pub,
                sn=sn,
                msg=msg,
                book=book,
                self_key=boom,
                make_bundle=inviter.make_bundle,
                commit=inviter.commit,
            )
        )

    inviter.server._pair = pair
    with pytest.raises(fp.PairError) as e:
        await _join(code)
    assert e.value.reason == "denied" and inviter.outcomes == ["error"]
    assert inviter.bundles == 0 and inviter.server._unauth == 0


async def test_a_pair_handler_bug_still_closes_the_stream(inviter):
    async def broken(*a, **k):
        raise KeyError("bug")

    inviter.server._pair = broken
    with pytest.raises(fp.PairError) as e:
        await asyncio.wait_for(_join(inviter.code()), 10)
    assert e.value.reason == "denied"  # EOF at once, not a hang
    assert inviter.server._unauth == 0
