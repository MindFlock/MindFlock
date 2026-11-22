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
  it.

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
