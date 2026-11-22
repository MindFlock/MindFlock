"""Reach my devices from anywhere, between two real MindFlock servers.

Two scratch servers on this machine (own HOME, peer home, tmux server and
ports; both in ``local`` mode — gate off, bound to 127.0.0.1, NO tailnet:
an empty tailscale status file, failing ``tailscale``/``cloudflared`` stubs)
are already one group of devices (a shared ``fleet.json``). With the switch
on, over the LOOPBACK carrier (the relay ingress itself stands in for the
Cloudflare tunnel: ``MINDFLOCK_DEVICES_ANYWHERE_LOOPBACK=1``) and a FOLDER
address book (``MINDFLOCK_DEVICES_ANYWHERE_STORE``, standing in for the
private GitHub repo):

* each lists the other as reachable — through the tunnel;
* a session made on one shows up in the other's list, and one is created
  through ``/api/devices/<device>/fwd/api/instances``;
* settings sync carries a change across (both gates are off: what comes
  through the tunnel is still held to the key — tests/unit/
  test_devices_anywhere.py pins the refusal);
* turning the switch off deletes that device's record and the other loses
  it;
* and a NEW device joins with a long code (docs/devices-anywhere.md §7):
  two servers in no group at all, the inviter's switch off — "Add a device
  → It isn't on my network" turns it on and makes the code, the new one
  pastes it, and both end up one group reaching each other through the
  tunnel (a wrong code first fails and changes nothing).

Real GitHub and a real Cloudflare tunnel are NOT exercised here (see
docs/devices-anywhere.md for the manual check). Opt-in::

    MINDFLOCK_E2E_ANYWHERE=1 pytest tests/e2e/test_devices_anywhere_trial.py
"""

from __future__ import annotations

import json
import os
import secrets
import time
from pathlib import Path

import pytest

from tests.e2e._peer_pair import PeerServer, wait_until

pytestmark = pytest.mark.skipif(
    os.environ.get("MINDFLOCK_E2E_ANYWHERE") != "1",
    reason="the two-server devices-anywhere trial runs only with MINDFLOCK_E2E_ANYWHERE=1",
)

FLEET_ID = secrets.token_hex(8)
FLEET_KEY = secrets.token_urlsafe(32)


class Device(PeerServer):
    def __init__(self, scratch: Path, name: str, store: Path, members: dict) -> None:
        super().__init__(scratch, name)
        self.store = store
        self.members = members

    def _lay_out_scratch(self) -> None:
        super()._lay_out_scratch()
        if self.members is None:
            # A fresh install: no group, the switch off.
            self.settings_path.write_text(json.dumps({"general": {"onboarded": True}}))
        else:
            self.settings_path.write_text(
                json.dumps(
                    {
                        "general": {
                            "onboarded": True,
                            "remote_control": "on",
                            "devices_anywhere": True,
                        }
                    }
                )
            )
        # Already one group: the same id/key/epoch and both on the roster.
        if self.members is not None:
            self._write_fleet()
        self._write_tailnet()

    def _write_fleet(self) -> None:
        (self.settings_path.parent / "fleet.json").write_text(
            json.dumps(
                {
                    "id": FLEET_ID,
                    "key": FLEET_KEY,
                    "epoch": 1,
                    "members": self.members,
                    "removed": {},
                    "prev_keys": {},
                    "admits": {},
                }
            )
        )

    def _write_tailnet(self) -> None:
        # "Tailscale" knows only this node itself (its name), no peers.
        (self.scratch / "no-tailnet.json").write_text(
            json.dumps(
                {
                    "Self": {
                        "DNSName": "%s.example.ts.net." % self.name,
                        "HostName": self.name,
                        "TailscaleIPs": [],
                        "Online": True,
                        "OS": "linux",
                    },
                    "Peer": {},
                }
            )
        )

    def _build_env(self) -> None:
        super()._build_env()
        self.env.update(
            {
                "MINDFLOCK_DEVICES_ANYWHERE_LOOPBACK": "1",
                "MINDFLOCK_DEVICES_ANYWHERE_STORE": str(self.store),
            }
        )


def _device(rows, key):
    rows = rows.get("devices", rows) if isinstance(rows, dict) else rows
    for d in rows or []:
        if d.get("device") == key:
            return d
    return None


@pytest.fixture
def pair(tmp_path):
    now = time.time()
    members = {
        n: {"host": n, "added_at": now, "added_by": n, "dns": "%s.example.ts.net" % n}
        for n in ("anya", "bart")
    }
    store = tmp_path / "address-book"
    a = Device(tmp_path / "a", "anya", store, members)
    b = Device(tmp_path / "b", "bart", store, members)
    started = []
    try:
        for d in (a, b):
            d.start()
            started.append(d)
        yield a, b
    finally:
        for d in started:
            d.stop()


def test_two_devices_without_tailscale(pair):
    a, b = pair

    # Both publish (two opaque files, nothing readable).
    wait_until(lambda: len(list(a.store.glob("*.rec"))) == 2, 60, "both records")
    for f in a.store.glob("*.rec"):
        text = f.read_text()
        assert "anya" not in text and "bart" not in text and "127.0.0.1" not in text
    st = a.ok("GET", "/api/devices/anywhere")
    assert st["on"] and st["state"] == "on", st

    # Each lists the other as reachable — through the tunnel.
    for me, other in ((a, "bart"), (b, "anya")):
        wait_until(
            lambda: (_device(me.ok("GET", "/api/devices"), other) or {}).get(
                "reachable"
            ),
            200,
            "%s sees %s" % (me.name, other),
        )
        assert _device(me.ok("GET", "/api/devices"), other)["via"] == "tunnel"
        fleet = me.ok("GET", "/api/fleet")
        row = next(m for m in fleet["members"] if m["key"] == other)
        assert row["reachable"] and row["via"] == "tunnel", row

    # Sessions: one made on bart shows on anya's list; one made from anya
    # through device forwarding lands on bart.
    # (start_anyway: these are plain bash shells — the memory guard is about
    # agents and would otherwise refuse on a busy machine.)
    b.ok(
        "POST",
        "/api/instances",
        {
            "title": "made-on-bart",
            "program": "bash",
            "repo_path": str(b.repo),
            "start_anyway": True,
        },
    )
    wait_until(
        lambda: any(
            r.get("title") == "bart::made-on-bart"
            or (
                r.get("device") == "bart"
                and r.get("title", "").endswith("made-on-bart")
            )
            for r in _rows(a)
        ),
        60,
        "bart's session on anya's list",
    )
    a.ok(
        "POST",
        "/api/devices/bart/fwd/api/instances",
        {
            "title": "via-anya",
            "program": "bash",
            "repo_path": str(b.repo),
            "start_anyway": True,
        },
    )
    b.wait_row("via-anya", timeout=30)

    # Settings sync, across the tunnel.
    a.ok("POST", "/api/settings/sync", {"enabled": True})
    b.ok("POST", "/api/settings/sync", {"enabled": True, "from": "anya"})
    a.ok("POST", "/api/settings", {"ui": {"accent": "#123456"}})
    wait_until(
        lambda: (b.settings().get("ui") or {}).get("accent") == "#123456",
        90,
        "accent synced to bart",
    )

    # Off on bart: its record goes, and anya stops reaching it through the tunnel.
    st = b.ok("POST", "/api/devices/anywhere", {"on": False})
    assert st["state"] == "off"
    assert len(list(a.store.glob("*.rec"))) == 1
    wait_until(
        lambda: not (_device(a.ok("GET", "/api/devices"), "bart") or {}).get(
            "reachable"
        ),
        120,
        "anya loses bart",
    )


def _rows(srv):
    rows = srv.ok("GET", "/api/instances")
    return rows.get("instances", rows) if isinstance(rows, dict) else rows


@pytest.fixture
def strangers(tmp_path):
    """Two fresh installs in no group, the switch off on both."""
    store = tmp_path / "address-book"
    a = Device(tmp_path / "a", "anya", store, None)
    b = Device(tmp_path / "b", "bart", store, None)
    started = []
    try:
        for d in (a, b):
            d.start()
            started.append(d)
        yield a, b
    finally:
        for d in started:
            d.stop()


def _join_state(srv):
    return srv.ok("GET", "/api/fleet").get("join") or {}


def test_pair_a_new_device_without_tailscale(strangers):
    a, b = strangers
    assert not a.ok("GET", "/api/fleet")["in_fleet"]

    # The switch is off: the flow has to ask first.
    st, body = a.call("POST", "/api/devices/anywhere/invite", {})
    assert st == 409 and body.get("needs_switch"), (st, body)
    inv = a.ok("POST", "/api/devices/anywhere/invite", {"turn_on": True})["invite"]
    code = inv["code"]
    assert code.startswith("mfd1:") and inv["expires_at"] > time.time()
    assert a.ok("GET", "/api/devices/anywhere")["on"]
    assert a.ok("GET", "/api/fleet")["in_fleet"]  # a group of one to join
    assert a.ok("GET", "/api/devices/anywhere/invite")["invite"]["code"] == code

    # A wrong code (right address, wrong secret) fails and changes nothing.
    from backend.peer import fleet_pair

    c = fleet_pair.parse_code(code, loopback=True)
    wrong = fleet_pair.encode_code(
        c.url, c.fp, bytes.fromhex(c.invite_id), b"w" * 20, loopback=True
    )
    b.ok("POST", "/api/devices/anywhere/join", {"code": wrong})
    wait_until(lambda: _join_state(b).get("state") == "error", 60, "wrong code fails")
    assert "didn't work" in _join_state(b)["error"]
    assert not b.ok("GET", "/api/fleet")["in_fleet"]

    # The real one.
    b.ok("POST", "/api/devices/anywhere/join", {"code": code, "reachable": True})
    wait_until(
        lambda: _join_state(b).get("state") not in ("joining", "finishing"),
        180,
        "bart finishes joining",
    )
    j = _join_state(b)
    assert j["state"] == "joined", j
    assert j["device"] == "anya" and j["via"] == "anywhere"
    assert b.ok("GET", "/api/devices/anywhere")["on"]

    # One group, on both sides; the code is used up.
    for me, other in ((a, "bart"), (b, "anya")):
        keys = {m["key"] for m in me.ok("GET", "/api/fleet")["members"]}
        assert keys == {"anya", "bart"}, (me.name, keys)
    assert a.ok("GET", "/api/devices/anywhere/invite")["invite"] is None

    # bart reaches anya through the tunnel at once (the address in the
    # code); anya reaches bart once bart published its own record.
    wait_until(
        lambda: (_device(b.ok("GET", "/api/devices"), "anya") or {}).get("via")
        == "tunnel",
        120,
        "bart reaches anya",
    )
    wait_until(lambda: len(list(a.store.glob("*.rec"))) == 2, 120, "both records")
    wait_until(
        lambda: (_device(a.ok("GET", "/api/devices"), "bart") or {}).get("reachable"),
        300,
        "anya reaches bart",
    )
    assert _device(a.ok("GET", "/api/devices"), "bart")["via"] == "tunnel"
