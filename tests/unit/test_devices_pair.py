"""Add a NEW device without Tailscale (docs/devices-anywhere.md §7): the
``mfd1:`` code, the pairing exchange on the device tunnel over the LOOPBACK
carrier (never Cloudflare), and the two halves around it
(:mod:`backend.web.core.devices_pair`) with the fleet in a scratch home.
Real GitHub, a real tunnel and real devices are never used; the two-server
run is ``tests/e2e/test_devices_anywhere_trial.py`` (opt-in)."""

from __future__ import annotations

import asyncio
import base64
import gc
import socket

import pytest

from backend.peer import fleet_pair as fp
from backend.peer import fleet_tunnel as ft
from backend.peer import identity, wire
from backend.peer.relay import RelayIngress
from backend.peer.transport import server_ssl_context
from backend.web.core import devices_anywhere as da
from backend.web.core import devices_pair as dp

pytestmark = pytest.mark.filterwarnings(
    "ignore::pytest.PytestUnraisableExceptionWarning"
)

FLEET_ID = "0123456789abcdef"
FLEET_KEY = "k" * 43


@pytest.fixture(autouse=True)
def _finalize_earlier_loops():
    """The same guard as the other tunnel test modules
    (test_devices_anywhere.py): relay/tunnel coroutines left pending when an
    earlier test's loop closed are otherwise finalized by the cycle collector
    at a random allocation DURING a later test, running their cleanup inside
    that test's loop — which can cut a live stream of the later test. So:
    collect first, then keep the collector out of this test. Test-only: a
    server runs one loop for its whole life."""
    gc.collect()
    gc.disable()
    try:
        yield
    finally:
        gc.enable()


def _bundle(device="mini", host="Mini"):
    return {
        "id": FLEET_ID,
        "key": FLEET_KEY,
        "epoch": 1,
        "members": {
            "laptop": {
                "host": "Laptop",
                "added_at": 1.0,
                "added_by": "laptop",
                "dns": "",
            },
            device: {"host": host, "added_at": 2.0, "added_by": "laptop", "dns": ""},
        },
        "removed": {},
    }


# --------------------------------------------------------------------------- #
# The code
# --------------------------------------------------------------------------- #
#: A fixed path token (parametrize ids must be the same in every xdist worker).
TOKEN = base64.b32encode(bytes(range(16, 32))).decode().rstrip("=").lower()
FP = bytes(range(16))
IID = bytes(range(8))
SECRET = bytes(range(20))


def test_code_round_trip_quick_tunnel():
    url = "wss://some-four-words.trycloudflare.com/%s" % TOKEN
    code = fp.encode_code(url, FP, IID, SECRET)
    assert code.startswith("mfd1:") and code == code.lower()
    got = fp.parse_code("here it is: %s thanks" % code.upper())
    assert got.url == url and got.fp == FP and got.secret == SECRET
    assert got.invite_id == IID.hex()
    # Never echoed by repr.
    assert (
        SECRET.hex() not in repr(got)
        and "secret" not in repr(got).split("invite_id")[1]
    )


def test_loopback_codes_only_parse_in_the_test_carrier():
    url = "wss://127.0.0.1:4567/%s" % TOKEN
    with pytest.raises(ValueError):
        fp.encode_code(url, FP, IID, SECRET)
    code = fp.encode_code(url, FP, IID, SECRET, loopback=True)
    assert fp.parse_code(code, loopback=True).url == url
    with pytest.raises(ValueError):
        fp.parse_code(code)


@pytest.mark.parametrize(
    "url",
    [
        "wss://evil.example.com/%s" % TOKEN,
        "wss://a.trycloudflare.com:8443/%s" % TOKEN,
        "wss://a.trycloudflare.com/short",
        "1.2.3.4:443",
    ],
)
def test_only_quick_tunnel_addresses_make_codes(url):
    with pytest.raises(ValueError):
        fp.encode_code(url, FP, IID, SECRET)


def test_a_damaged_code_is_refused_plainly():
    code = fp.encode_code("wss://a-b.trycloudflare.com/%s" % TOKEN, FP, IID, SECRET)
    body, check = code.rsplit("-", 1)
    flipped = body[:-3] + ("a" if body[-3] != "a" else "b") + body[-2:]
    for bad in (flipped + "-" + check, body + "-aaaa", "mfd1:", "nothing here"):
        with pytest.raises(ValueError) as e:
            fp.parse_code(bad)
        assert SECRET.hex() not in str(e.value)


# --------------------------------------------------------------------------- #
# The book
# --------------------------------------------------------------------------- #
def _t(x=b"t"):
    return x


def _proof(secret, t=b"t"):
    import hashlib
    import hmac

    return hmac.new(secret, t, hashlib.sha256).hexdigest()


def _secret_of(book, iid):
    return book._entries[iid].secret


def test_book_single_use_and_kill_after_five_wrong():
    book = fp.PairBook()
    a = book.create("wss://a-b.trycloudflare.com/%s" % TOKEN, FP)
    s = _secret_of(book, a["invite_id"])
    assert book.check(a["invite_id"], _proof(s), b"t") == s
    assert book.check(a["invite_id"], _proof(s), b"t") is None  # used
    b = book.create("wss://a-b.trycloudflare.com/%s" % TOKEN, FP)
    s = _secret_of(book, b["invite_id"])
    for _ in range(5):
        assert book.check(b["invite_id"], "0" * 64, b"t") is None
    assert book.check(b["invite_id"], _proof(s), b"t") is None  # killed
    assert book.stats["killed"] == 1


def test_book_expiry_rate_limit_and_one_live_code():
    now = {"t": 100.0}
    book = fp.PairBook(clock=lambda: now["t"], rate=3)
    a = book.create("wss://a-b.trycloudflare.com/%s" % TOKEN, FP)
    b = book.create("wss://a-b.trycloudflare.com/%s" % TOKEN, FP)
    assert [i["invite_id"] for i in book.active()] == [b["invite_id"]]  # a cancelled
    assert a["invite_id"] != b["invite_id"]
    s = _secret_of(book, b["invite_id"])
    now["t"] += fp.TTL_S + 1
    assert book.check(b["invite_id"], _proof(s), b"t") is None  # expired
    c = book.create("wss://a-b.trycloudflare.com/%s" % TOKEN, FP)
    s = _secret_of(book, c["invite_id"])
    assert book.check("ffffffffffffffff", "0" * 64, b"t") is None  # its own budget
    for _ in range(3):
        assert book.check(c["invite_id"], "0" * 64, b"t") is None
    # The 4th attempt naming the code in the window is refused even with the
    # right proof.
    assert book.check(c["invite_id"], _proof(s), b"t") is None
    assert book.stats["limited"] == 1
    now["t"] += fp.RATE_WINDOW_S + 1
    assert book.check(c["invite_id"], _proof(s), b"t") == s


# --------------------------------------------------------------------------- #
# The exchange, through a real TunnelServer over the loopback carrier
# --------------------------------------------------------------------------- #
class Origin:
    """The inviter's web server: the pair branch must never reach it."""

    def __init__(self):
        self.hits = 0

    async def _on(self, reader, writer):
        self.hits += 1
        writer.close()

    async def start(self) -> int:
        self.server = await asyncio.start_server(self._on, "127.0.0.1", 0)
        return self.server.sockets[0].getsockname()[1]


class Inviter:
    def __init__(self, tmp_path, name="laptop-id", book=None):
        self.ident = identity.load_or_create(str(tmp_path / name))
        self.book = book or fp.PairBook()
        self.committed = []
        self.outcomes = []
        self.bundles = 0
        self.commit_fails = False
        self.taken = set()
        #: The roster the members' handshake checks, and the keys it uses.
        self.members = {"laptop"}
        self.keys = None
        #: What serve_pair told: pending begun, unfinished joins, reuses.
        self.begun = []
        self.unfinished = []
        self.reused = []
        #: Drop the stream right after the commit (a lost ``added``).
        self.drop_added = False

    def make_bundle(self, device, host, dns):
        if device in self.taken:
            raise fp.PairRefused("taken")
        self.bundles += 1
        return _bundle(device, host)

    async def commit(self, device, host, dns, runs):
        if self.commit_fails:
            raise RuntimeError("disk full")
        self.committed.append((device, host, dns, runs))
        self.members.add(device)
        if self.drop_added:
            self._writer.transport.abort()

    async def pair(self, reader, writer, server_pub, sn, msg):
        self._writer = writer
        self.outcomes.append(
            await fp.serve_pair(
                reader,
                writer,
                server_pub=server_pub,
                sn=sn,
                msg=msg,
                book=self.book,
                self_key=lambda: "laptop",
                make_bundle=self.make_bundle,
                commit=self.commit,
                begin=lambda *a: self.begun.append(a[:4]),
                unfinished=lambda *a: self.unfinished.append(a),
                reused=lambda *a: self.reused.append(a),
                addr=lambda: self.url,
                confirm_timeout=5.0,
            )
        )

    async def start(self, with_pair=True):
        self.origin = Origin()
        oport = await self.origin.start()
        self.server = ft.TunnelServer(
            server_ssl_context(self.ident),
            self.ident.pub,
            keys=lambda: self.keys,
            self_key=lambda: "laptop",
            members=lambda: set(self.members),
            origin_port=lambda: oport,
            pair=self.pair if with_pair else None,
        )
        self.token = da._new_token()
        self.ingress = RelayIngress(self.server, "/" + self.token)
        port = await self.ingress.start()
        self.port = port
        self.url = "wss://127.0.0.1:%d/%s" % (port, self.token)
        return self

    def pair_url(self, ttl=fp.TTL_S) -> str:
        """A fresh pairing path (what devices_anywhere.open_pair_path does)."""
        import time

        token = da._new_token()

        async def on_pair(r, w, source=""):
            await self.server(r, w, source, pair_only=True)

        self.ingress.set_side_path("/" + token, on_pair, until=time.monotonic() + ttl)
        return "wss://127.0.0.1:%d/%s" % (self.port, token)

    def code(self) -> str:
        return self.book.create(
            self.pair_url(), self.ident.fingerprint(), loopback=True, tunnel=self.url
        )["code"]

    async def stop(self):
        await self.ingress.stop()
        self.origin.server.close()


@pytest.fixture
async def inviter(tmp_path):
    inv = await Inviter(tmp_path).start()
    yield inv
    await inv.stop()


async def _join(code_text, device="mini", adopt=None, **kw):
    got = []

    def default_adopt(inviter, bundle):
        got.append((inviter, bundle))

    code = fp.parse_code(code_text, loopback=True)
    res = await fp.pair(
        code,
        device=device,
        host=kw.pop("host", "Mini"),
        dns="",
        runs=False,
        adopt=adopt or default_adopt,
        edge_ssl=None,
        **kw,
    )
    return res, got


async def test_happy_path(inviter):
    (who, bundle, addr), got = await _join(inviter.code())
    assert who == "laptop" and addr == inviter.url  # the members' address
    assert bundle["key"] == FLEET_KEY and "mini" in bundle["members"]
    assert got == [("laptop", bundle)]
    assert inviter.committed == [("mini", "Mini", "", False)]
    assert inviter.outcomes == ["added"]
    assert inviter.server.stats["pair"] == 1
    assert inviter.origin.hits == 0  # nothing reached the web server
    assert inviter.book.active() == []  # single use
    assert inviter.begun == [("mini", "Mini", "", False)]  # pending before the bundle
    assert inviter.unfinished == []


async def test_used_code_fails_and_learns_nothing(inviter):
    code = inviter.code()
    await _join(code)
    with pytest.raises(fp.PairError) as e:
        await _join(code, device="rig")
    # It proved the code, so it is told it was used (M1) — and gets nothing.
    assert e.value.reason == "used"
    assert inviter.bundles == 1 and len(inviter.committed) == 1
    assert inviter.reused == [("rig", "Mini")]


def _with_secret(code_text: str, secret: bytes) -> str:
    c = fp.parse_code(code_text, loopback=True)
    return fp.encode_code(
        c.url, c.fp, bytes.fromhex(c.invite_id), secret, loopback=True
    )


async def test_wrong_secret_is_denied_and_counts(inviter):
    code = inviter.code()
    with pytest.raises(fp.PairError) as e:
        await _join(_with_secret(code, b"x" * 20))
    assert e.value.reason == "denied"
    assert inviter.bundles == 0 and inviter.committed == []
    assert inviter.book.stats["wrong"] == 1
    (who, _b, _a), _g = await _join(code)  # the real code still works
    assert who == "laptop"


async def test_five_wrong_proofs_kill_the_code(inviter):
    code = inviter.code()
    for _ in range(5):
        with pytest.raises(fp.PairError):
            await _join(_with_secret(code, b"y" * 20))
    with pytest.raises(fp.PairError) as e:
        await _join(code)
    assert e.value.reason == "denied"
    assert inviter.book.stats["killed"] == 1 and inviter.committed == []


async def test_expired_code_is_denied(tmp_path):
    now = {"t": 0.0}
    inv = await Inviter(tmp_path, book=fp.PairBook(clock=lambda: now["t"])).start()
    try:
        code = inv.code()
        now["t"] += fp.TTL_S + 1
        with pytest.raises(fp.PairError) as e:
            await _join(code)
        assert e.value.reason == "denied" and inv.bundles == 0
    finally:
        await inv.stop()


async def test_a_different_key_fails_at_the_pin_before_anything_is_sent(
    tmp_path, inviter
):
    """A man in the middle answering at the code's address with its own key
    (it can't present the inviter's): the joiner stops at the pin."""
    mitm = await Inviter(tmp_path, name="mitm-id").start()
    try:
        real = fp.parse_code(inviter.code(), loopback=True)
        # The code names the inviter's fingerprint, but the address leads to
        # the MITM (as if it held the hostname).
        forged = fp.encode_code(
            mitm.pair_url(),
            real.fp,
            bytes.fromhex(real.invite_id),
            real.secret,
            loopback=True,
        )
        with pytest.raises(fp.PairError) as e:
            await _join(forged)
        assert e.value.reason == "pin"
        # Nothing reached it: no proof was ever checked there.
        assert mitm.book.stats == {
            "ok": 0,
            "wrong": 0,
            "unknown": 0,
            "limited": 0,
            "killed": 0,
            "used": 0,
        }
        assert mitm.outcomes == []
        assert inviter.book.active()  # the real code is untouched
    finally:
        await mitm.stop()


async def test_refused_bundle_leaves_no_ghost_member(inviter):
    def refuse(_inviter, _bundle):
        raise fp.PairRefused(
            "this device is already one of 3 devices — leave that group first"
        )

    with pytest.raises(fp.PairError) as e:
        await _join(inviter.code(), adopt=refuse)
    assert e.value.reason == "refused" and "leave that group" in e.value.text
    await asyncio.sleep(0.05)
    assert inviter.outcomes == ["aborted"] and inviter.committed == []
    # The bundle went out: the inviter is told (it raises the alarm).
    assert inviter.unfinished == [("mini", "Mini", "aborted")]


async def test_no_added_means_unconfirmed(inviter):
    inviter.commit_fails = True
    adopted = []
    with pytest.raises(fp.PairError) as e:
        await _join(inviter.code(), adopt=lambda i, b: adopted.append(i))
    assert e.value.reason == "unconfirmed"
    assert adopted == ["laptop"]  # the caller undoes this (devices_pair)
    assert inviter.outcomes == ["error"]
    assert inviter.unfinished == [("mini", "Mini", "error")]


async def test_same_name_is_said_after_a_valid_proof_only(inviter):
    with pytest.raises(fp.PairError) as e:
        await _join(inviter.code(), device="laptop")
    assert e.value.reason == "same_name"
    inviter.taken.add("rig")
    with pytest.raises(fp.PairError) as e:
        await _join(inviter.code(), device="rig")
    assert e.value.reason == "same_name"
    # A wrong code naming a taken name learns only "denied".
    with pytest.raises(fp.PairError) as e:
        await _join(_with_secret(inviter.code(), b"z" * 20), device="rig")
    assert e.value.reason == "denied"
    assert inviter.committed == []


async def test_a_server_without_a_pair_handler_denies(tmp_path):
    inv = await Inviter(tmp_path).start(with_pair=False)
    try:
        with pytest.raises(fp.PairError) as e:
            await _join(inv.code())
        assert e.value.reason == "denied"
        assert inv.origin.hits == 0
    finally:
        await inv.stop()


# --------------------------------------------------------------------------- #
# serve_pair against a hand-driven client: the confirm is checked
# --------------------------------------------------------------------------- #
async def _pipe():
    a, b = socket.socketpair()
    ra, wa = await asyncio.open_connection(sock=a)
    rb, wb = await asyncio.open_connection(sock=b)
    return (ra, wa), (rb, wb)


async def _drive(book, iid, secret, then):
    """Run serve_pair on one end; play the client on the other with a valid
    pair frame, then ``then(reader, writer, t, ok)``."""
    (sr, sw), (cr, cw) = await _pipe()
    pub, sn, cn = b"P" * 32, b"s" * 32, b"c" * 32
    t = fp.transcript(pub, sn, cn, iid, "mini", "Mini", "", False)
    msg = {
        "t": "pair",
        "v": 1,
        "nonce": fp._b64(cn),
        "invite": iid,
        "device": "mini",
        "host": "Mini",
        "dns": "",
        "runs": False,
        "proof": fp._mac(secret, fp.CLIENT_LABEL, t),
    }
    committed = []
    told = []

    async def commit(*a):
        committed.append(a)

    server = asyncio.create_task(
        fp.serve_pair(
            sr,
            sw,
            server_pub=pub,
            sn=sn,
            msg=msg,
            book=book,
            self_key=lambda: "laptop",
            make_bundle=lambda d, h, n: _bundle(d, h),
            commit=commit,
            begin=lambda *a: told.append(("begin",) + a[:4]),
            unfinished=lambda *a: told.append(("unfinished",) + a),
            addr=lambda: MEMBERS_URL,
            confirm_timeout=2.0,
        )
    )
    ok = await wire.read_frame(cr, fp.BUNDLE_MAX_FRAME)
    assert ok["t"] == "ok" and ok["addr"] == MEMBERS_URL
    told.append(("ok-read",))
    tt = t + fp._field(b"laptop") + fp._field(MEMBERS_URL.encode())
    await then(cr, cw, tt, ok)
    outcome = await asyncio.wait_for(server, 5)
    cw.close()
    sw.close()
    _drive.told = told
    return outcome, committed


MEMBERS_URL = "wss://a-b.trycloudflare.com/%s" % TOKEN


@pytest.mark.parametrize("how", ["wrong_key", "no_confirm", "garbage"])
async def test_no_ghost_member_without_a_valid_confirm(how):
    book = fp.PairBook()
    inv = book.create("wss://a-b.trycloudflare.com/%s" % TOKEN, FP)
    iid = inv["invite_id"]
    secret = book._entries[iid].secret

    async def then(cr, cw, tt, ok):
        if how == "wrong_key":
            other = dict(ok["bundle"], key="z" * 43)
            proof = fp._mac(fp.confirm_key(other), fp.CONFIRM_LABEL, tt)
            cw.write(wire.encode({"t": "confirm", "proof": proof}))
        elif how == "garbage":
            cw.write(wire.encode({"t": "confirm", "proof": "0" * 64, "x": 1}))
        else:
            cw.close()  # vanished after the bundle
            return
        await cw.drain()

    outcome, committed = await _drive(book, iid, secret, then)
    assert outcome == "unconfirmed" and committed == []
    # Pending was recorded BEFORE the bundle was read, and the unfinished
    # join reported after (M1).
    assert [x[0] for x in _drive.told] == ["begin", "ok-read", "unfinished"]
    assert _drive.told[-1] == ("unfinished", "mini", "Mini", "unconfirmed")


async def test_valid_confirm_commits():
    book = fp.PairBook()
    inv = book.create("wss://a-b.trycloudflare.com/%s" % TOKEN, FP)
    iid = inv["invite_id"]
    secret = book._entries[iid].secret

    async def then(cr, cw, tt, ok):
        assert ok["proof"] == fp._mac(secret, fp.SERVER_LABEL, tt)
        cw.write(
            wire.encode(
                {
                    "t": "confirm",
                    "proof": fp._mac(
                        fp.confirm_key(ok["bundle"]), fp.CONFIRM_LABEL, tt
                    ),
                }
            )
        )
        await cw.drain()
        assert await wire.read_frame(cr) == {"t": "added"}

    outcome, committed = await _drive(book, iid, secret, then)
    assert outcome == "added" and committed == [("mini", "Mini", "", False)]
    assert [x[0] for x in _drive.told] == ["begin", "ok-read"]  # no alarm


# --------------------------------------------------------------------------- #
# devices_pair: the two halves around the exchange (fleet in a scratch home)
# --------------------------------------------------------------------------- #
@pytest.fixture
def home(tmp_path, monkeypatch):
    from backend.config import settings as store
    from backend.web.core import fleet, remote, settings_sync

    monkeypatch.setenv("MINDFLOCK_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("MINDFLOCK_PEER_HOME", str(tmp_path / "peer"))
    monkeypatch.setenv(da.LOOPBACK_ENV, "1")
    monkeypatch.delenv("CS_WEB_MODE", raising=False)
    monkeypatch.setenv("MINDFLOCK_AUTH", "0")
    store.invalidate()
    da._reset_for_tests()
    dp._reset_for_tests()
    monkeypatch.setattr(fleet, "_JOIN", fleet._idle_join())
    monkeypatch.setattr(fleet, "_JOIN_TASK", [None])
    monkeypatch.setattr(fleet, "_ADMITTED", {})
    monkeypatch.setattr(fleet, "_CACHE", {"sig": None, "doc": None})
    monkeypatch.setattr(fleet, "ADMIT_PROBE_DELAYS", ())
    monkeypatch.setattr(remote, "_DEVICES", {})
    monkeypatch.setattr(
        remote,
        "self_identity",
        lambda: {"key": "mini", "host": "Mini", "dns": "", "ip": "", "os": ""},
    )
    calls = []

    async def fake_enable(start_from="", *, seed=False):
        calls.append(("enable", start_from, seed))
        return {"enabled": True}

    monkeypatch.setattr(settings_sync, "enable", fake_enable)
    monkeypatch.setattr(settings_sync, "enabled", lambda: False)
    monkeypatch.setattr(settings_sync, "local_change", lambda *a, **k: [])
    yield fleet, calls
    store.invalidate()
    da._reset_for_tests()
    dp._reset_for_tests()


CODE_URL = "wss://127.0.0.1:4567/%s" % TOKEN


def _fake_pair(monkeypatch, *, fail_after_adopt=None):
    async def fake(code, *, device, host, dns, runs, adopt, edge_ssl, **kw):
        b = _bundle(device, host)
        await fp._maybe_await(adopt("laptop", b))
        if fail_after_adopt:
            raise fail_after_adopt
        return "laptop", b, CODE_URL

    monkeypatch.setattr(fp, "pair", fake)


async def _wait_join(fleet, states=("joining", "finishing")):
    for _ in range(200):
        if fleet.join_status()["state"] not in states:
            return fleet.join_status()
        await asyncio.sleep(0.02)
    raise AssertionError(fleet.join_status())


def _code():
    return fp.encode_code(CODE_URL, FP, IID, SECRET, loopback=True)


async def test_joiner_undoes_the_adopt_when_the_inviter_never_confirms(
    home, monkeypatch
):
    fleet, _calls = home
    assert not fleet.in_fleet()
    _fake_pair(monkeypatch, fail_after_adopt=fp.PairError("unconfirmed"))
    await dp.join(_code(), reachable=True, confirm=True)
    st = await _wait_join(fleet)
    assert st["state"] == "error" and "didn't confirm" in st["error"]
    assert not fleet.in_fleet()  # the key it took is gone again


async def test_checkbox_off_joins_but_leaves_the_switch_off(home, monkeypatch):
    fleet, calls = home
    _fake_pair(monkeypatch)
    turned = []
    monkeypatch.setattr(da, "save_switch", lambda on: turned.append(on))
    st = await dp.join(_code(), reachable=False, confirm=True)
    assert st["state"] == "joining" and st["via"] == "anywhere"
    st = await _wait_join(fleet)
    assert st["state"] == "joined", st
    assert st["note"] == dp.NOTE_SWITCH_OFF
    assert turned == [] and not da.enabled()
    assert fleet.in_fleet() and fleet.fleet_key() == FLEET_KEY
    assert "laptop" in fleet.live_members()
    assert calls == []  # no settings sync without a way to reach the inviter


async def test_joiner_without_github_is_told_what_to_do(home, monkeypatch):
    fleet, calls = home
    _fake_pair(monkeypatch)
    turned = []
    monkeypatch.setattr(da, "save_switch", lambda on: turned.append(on))

    async def nothing(*a, **k):
        return da.status()

    monkeypatch.setattr(da, "apply", nothing)
    monkeypatch.setattr(da, "publish_soon", nothing)

    async def no_github():
        return False

    monkeypatch.setattr(da, "github_ready", no_github)
    dev = {"key": "laptop", "host": "Laptop", "reachable": True, "dns": ""}

    async def reach(_who):
        return dev

    monkeypatch.setattr(dp, "_reach", reach)

    async def refresh(_k):
        return dev

    async def announce(*a, **k):
        return None

    monkeypatch.setattr(fleet, "_refresh", refresh)
    monkeypatch.setattr(fleet, "_announce", announce)
    await dp.join(_code(), reachable=True, confirm=True)
    st = await _wait_join(fleet)
    assert st["state"] == "joined", st
    assert turned == [True]
    assert st["note"] == dp.NOTE_NO_GITHUB.format(host="Laptop")
    assert ("enable", "laptop", False) in calls  # settings sync from the inviter
    # The inviter's address came from the code (it can't read GitHub yet).
    assert da._S.addrs["laptop"].url == CODE_URL
    assert da._S.addrs["laptop"].fp == FP.hex()


async def test_unreached_inviter_says_so(home, monkeypatch):
    fleet, _calls = home
    _fake_pair(monkeypatch)
    monkeypatch.setattr(da, "save_switch", lambda on: None)

    async def nothing(*a, **k):
        return None

    monkeypatch.setattr(da, "apply", nothing)
    monkeypatch.setattr(dp, "_reach", nothing)
    await dp.join(_code(), reachable=True, confirm=True)
    st = await _wait_join(fleet)
    assert st["state"] == "joined" and "didn't answer" in st["note"]


@pytest.mark.parametrize(
    "err,want",
    [
        (fp.PairError("pin"), "isn't the one that made the code"),
        (fp.PairError("denied"), "didn't work"),
        (fp.PairError("same_name"), "already has this computer's name"),
        (OSError("no route"), "Couldn't reach the other computer"),
    ],
)
async def test_join_errors_are_plain(home, monkeypatch, err, want):
    fleet, _calls = home

    async def fake(*a, **k):
        raise err

    monkeypatch.setattr(fp, "pair", fake)
    await dp.join(_code(), reachable=True, confirm=True)
    st = await _wait_join(fleet)
    assert st["state"] == "error" and want in st["error"]
    assert not fleet.in_fleet()


async def test_a_bad_code_is_refused_before_anything(home):
    with pytest.raises(ValueError):
        await dp.join("mfd1:abc-defg", reachable=True, confirm=True)


async def test_inviter_needs_the_switch_then_makes_a_code(home, monkeypatch):
    fleet, _calls = home
    with pytest.raises(dp.NeedsSwitch):
        await dp.create_invite()
    turned = []

    def save(on):
        turned.append(on)
        monkeypatch.setattr(da, "enabled", lambda: on)

    monkeypatch.setattr(da, "save_switch", save)

    async def up(timeout=45.0):
        da._S.url, da._S.tunnel_state = CODE_URL, "up"
        return True

    monkeypatch.setattr(da, "ensure_up", up)
    monkeypatch.setattr(da, "_FP", FP.hex())
    pair_url = "wss://127.0.0.1:4567/%s" % da._new_token()
    monkeypatch.setattr(da, "open_pair_path", lambda ttl: pair_url)
    inv = await dp.create_invite(turn_on=True)
    assert turned == [True] and fleet.in_fleet()  # a group of one to invite into
    c = fp.parse_code(inv["code"], loopback=True)
    # The code names the pairing path, never the members' one.
    assert c.url == pair_url and c.url != CODE_URL and c.fp == FP
    assert inv["safety"] == fp.safety_number(FP, c.invite_id)
    assert dp.invite_status()["invite"]["code"] == inv["code"]
    # The tunnel restarts: the code names a dead address — it goes.
    dp.tunnel_changed("wss://127.0.0.1:1/%s" % da._new_token())
    st = dp.invite_status()
    assert st["invite"] is None and "restarted" in st["gone"]


async def test_inviter_puts_the_joiner_on_its_roster_only_at_commit(home, monkeypatch):
    fleet, calls = home
    monkeypatch.setattr(
        __import__("backend.web.core.remote", fromlist=["x"]),
        "self_identity",
        lambda: {"key": "laptop", "host": "Laptop", "dns": "", "ip": "", "os": ""},
    )
    fleet.create()
    b = dp._make_bundle("mini", "Mini", "")
    assert "mini" in b["members"] and "mini" not in fleet.live_members()
    with pytest.raises(fp.PairRefused):
        dp._make_bundle("laptop", "x", "")  # a live member's name
    await dp._commit("mini", "Mini", "", False)
    assert "mini" in fleet.live_members()
    rows = fleet.admitted()
    assert rows and rows[0]["via"] == "anywhere" and rows[0]["host"] == "Mini"
    assert ("enable", "", True) in calls  # sync seeded here (after_admit)


async def test_a_seeded_address_survives_a_read_without_its_record(
    home, monkeypatch, tmp_path
):
    from backend.peer import fleet_record as fr
    from backend.web.core import github_records as gr

    keys = fr.derive(FLEET_KEY, FLEET_ID, 1)
    monkeypatch.setattr(da, "_keys", lambda: keys)
    monkeypatch.setattr(da, "_self_key", lambda: "mini")
    monkeypatch.setattr(da, "_members", lambda: {"mini", "laptop"})
    da._set_store_for_tests(gr.DirStore(str(tmp_path / "store")))
    da.seed("laptop", CODE_URL, FP.hex())
    await da.read_records()
    assert da._S.addrs["laptop"].url == CODE_URL
    # Once laptop's own record is there, it wins and the seed is gone.
    url2 = "wss://127.0.0.1:4568/%s" % da._new_token()
    text = fr.seal(
        keys,
        fr.Record(
            device="laptop", url=url2, fp="ab" * 16, ctr=5, ts=__import__("time").time()
        ),
    )
    await da._store().write(fr.file_name(keys, "laptop"), text)
    await da.read_records()
    assert da._S.addrs["laptop"].url == url2 and "laptop" not in da._S.seeded
    await da._shutdown()


@pytest.mark.parametrize("how", ["relayed", "tunneled"])
@pytest.mark.parametrize(
    "method,path",
    [
        ("post", "/api/devices/anywhere/invite"),
        ("get", "/api/devices/anywhere/invite"),
        ("post", "/api/devices/anywhere/join"),
    ],
)
def test_pairing_routes_refuse_other_devices(how, method, path, monkeypatch):
    from fastapi.testclient import TestClient

    from backend.web import server
    from backend.web.core import auth

    headers = {}
    if how == "relayed":
        headers["X-MindFlock-Remote"] = "rig"
    else:
        monkeypatch.setattr(auth, "tunneled", lambda scope: True)
    called = []
    monkeypatch.setattr(dp, "create_invite", lambda **k: called.append(k))
    monkeypatch.setattr(dp, "join", lambda *a, **k: called.append(a))
    client = TestClient(server.app)
    if method == "post":
        r = client.post(path, json={"code": _code(), "turn_on": True}, headers=headers)
    else:
        r = client.get(path, headers=headers)
    assert r.status_code in (401, 403), r.text
    assert called == []


def test_cli_join_routes_a_long_code_and_waits(monkeypatch, capsys):
    import argparse

    from backend import cli

    posted = []
    states = iter(
        [
            {"state": "finishing", "host": "Anya"},
            {"state": "joined", "host": "Anya", "note": "Connect GitHub."},
        ]
    )
    monkeypatch.setattr(cli.client, "discover", lambda h, p: "http://127.0.0.1:1")
    monkeypatch.setattr(
        cli.client,
        "post",
        lambda base, path, body, **k: posted.append((path, body))
        or {"state": "joining"},
    )
    monkeypatch.setattr(cli, "_fleet_status", lambda base: {"join": next(states)})
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    code = _code()
    args = argparse.Namespace(
        host=None, port=None, text=["paste:", code], yes=False, no_anywhere=True
    )
    assert cli._cmd_join(args) == 0
    assert posted == [
        ("/api/devices/anywhere/join", {"code": code, "reachable": False})
    ]
    out = capsys.readouterr().out
    assert "joined Anya's devices" in out and "Connect GitHub." in out
