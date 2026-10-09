"""End-to-end: three real servers become one person's devices, a fourth stays out.

Each server is a real ``uvicorn backend.web.server:app`` subprocess with its own
$HOME and settings file, bound to its own loopback address (127.0.0.2/3/4/5) on
ONE shared port — so the discovery code's "probe the peer's IP on my port" finds
them exactly the way it finds tailnet peers. ``$MINDFLOCK_TAILSCALE_STATUS_FILE``
feeds each server a fake ``tailscale status --json`` naming itself and the
others, and a stub ``tailscale`` on PATH makes every other tailscale call fail
harmlessly (nothing here may touch the machine's real tailnet).

alpha  gate OFF  (the owner's laptop: the longest-used machine, its settings lead)
beta   gate ON   joins alpha with a CODE made on alpha
gamma  gate ON   asks beta to join; beta APPROVES
rogue  gate OFF  never joins — it must never be synced from

Linux only (macOS has no 127.0.0.x aliases by default). Skip with
MINDFLOCK_SKIP_E2E=1.
"""

from __future__ import annotations

import json
import os
import socket
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Optional

import pytest

httpx = pytest.importorskip("httpx")

pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or os.environ.get("MINDFLOCK_SKIP_E2E") == "1",
    reason="fleet e2e needs Linux loopback aliases (MINDFLOCK_SKIP_E2E=1 skips)",
)

DOMAIN = "fleet-test.ts.net"
NODES = {
    "alpha": {"ip": "127.0.0.2", "token": ""},
    "beta": {"ip": "127.0.0.3", "token": "beta-token-0123456789abcdef"},
    "gamma": {"ip": "127.0.0.4", "token": "gamma-token-0123456789abcdef"},
    "rogue": {"ip": "127.0.0.5", "token": ""},
}


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _free_port_on_all() -> int:
    """A port free on every node address (they all bind the same one)."""
    for _ in range(50):
        s = socket.socket()
        s.bind(("127.0.0.2", 0))
        port = s.getsockname()[1]
        s.close()
        ok = True
        for spec in NODES.values():
            t = socket.socket()
            try:
                t.bind((spec["ip"], port))
            except OSError:
                ok = False
            finally:
                t.close()
        if ok and port != 8765:
            return port
    raise RuntimeError("no common free port")


def _node(name: str) -> dict:
    return {
        "DNSName": "%s.%s." % (name, DOMAIN),
        "HostName": name,
        "TailscaleIPs": [NODES[name]["ip"]],
        "OS": "linux",
        "Online": True,
    }


def wait_for(fn: Callable[[], object], timeout: float = 20.0, every: float = 0.5):
    """Poll ``fn`` until it returns something truthy; fail with its last value."""
    deadline = time.time() + timeout
    last: object = None
    while time.time() < deadline:
        try:
            last = fn()
        except Exception as err:  # noqa: BLE001 — keep polling through a restart
            last = err
        if last and not isinstance(last, Exception):
            return last
        time.sleep(every)
    raise AssertionError("timed out; last value: %r" % (last,))


class Server:
    def __init__(self, name: str, port: int, root: Path, stub_bin: Path) -> None:
        self.name = name
        self.ip = NODES[name]["ip"]
        self.token = NODES[name]["token"]
        self.port = port
        self.dir = root / name
        self.home = self.dir / "home"
        self.home.mkdir(parents=True)
        self.settings_file = self.dir / "settings.json"
        self.settings_file.write_text(
            json.dumps(
                {
                    "general": {
                        "onboarded": True,
                        "ingestion_autostart": False,
                        "remote_control": "off",
                    }
                }
            )
        )
        status = {
            "Self": _node(name),
            "Peer": {n: _node(n) for n in NODES if n != name},
        }
        self.status_file = self.dir / "tailscale-status.json"
        self.status_file.write_text(json.dumps(status))
        env = {
            k: v
            for k, v in os.environ.items()
            if k not in ("TMUX", "TMUX_PANE", "MINDFLOCK_AUTH_TOKEN")
        }
        env.update(
            {
                "HOME": str(self.home),
                "PATH": "%s:%s" % (stub_bin, os.environ.get("PATH", "")),
                "CS_WEB_MODE": "tailscale",
                "CS_CURSOR_AUTOADOPT": "0",
                "MINDFLOCK_AUTH": "1" if self.token else "0",
                "MINDFLOCK_SETTINGS_FILE": str(self.settings_file),
                "MINDFLOCK_PROMPT_QUEUE_FILE": str(self.dir / "queues.json"),
                "MINDFLOCK_HOOKS_DIR": str(self.dir / "hooks"),
                "MINDFLOCK_TEMPLATES_FILE": str(self.dir / "templates.json"),
                "MINDFLOCK_TAILSCALE_STATUS_FILE": str(self.status_file),
                "TMUX_TMPDIR": "/tmp/mf-e2e-%s-%d" % (name, port),
                "PORT": str(port),
                "UVICORN_PORT": str(port),
            }
        )
        if self.token:
            env["MINDFLOCK_AUTH_TOKEN"] = self.token
        os.makedirs(env["TMUX_TMPDIR"], exist_ok=True)
        self.log = open(self.dir / "server.log", "wb")
        self.proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "backend.web.server:app",
                "--host",
                self.ip,
                "--port",
                str(port),
                "--log-level",
                "warning",
            ],
            cwd=str(_repo_root()),
            env=env,
            stdout=self.log,
            stderr=subprocess.STDOUT,
        )
        self.http = httpx.Client(
            base_url="http://%s:%d" % (self.ip, port),
            headers={"Authorization": "Bearer " + self.token} if self.token else {},
            timeout=30.0,
        )

    # -- http ----------------------------------------------------------------
    def get(self, path: str, **kw):
        r = self.http.get(path, **kw)
        assert r.status_code == 200, "%s GET %s -> %s %s" % (
            self.name,
            path,
            r.status_code,
            r.text[:300],
        )
        return r.json()

    def post(self, path: str, body: Optional[dict] = None, ok: bool = True, **kw):
        r = self.http.post(path, json=body or {}, **kw)
        if ok:
            assert r.status_code == 200, "%s POST %s -> %s %s" % (
                self.name,
                path,
                r.status_code,
                r.text[:300],
            )
            return r.json()
        return r

    def put(self, path: str, body: dict):
        r = self.http.put(path, json=body)
        assert r.status_code == 200, "%s PUT %s -> %s %s" % (
            self.name,
            path,
            r.status_code,
            r.text[:300],
        )
        return r.json()

    # -- views ---------------------------------------------------------------
    def settings(self) -> dict:
        body = self.get("/api/settings")
        return body.get("settings", body)

    def sources(self) -> dict:
        return {
            s["id"]: s for s in self.get("/api/settings/ticketing/sources")["sources"]
        }

    def fleet(self) -> dict:
        return self.get("/api/fleet")

    def fleet_file(self) -> dict:
        return json.loads((self.dir / "fleet.json").read_text())

    def refresh(self) -> None:
        self.post("/api/devices/refresh")

    def sync_now(self) -> None:
        self.post("/api/settings/sync/now")

    def stop(self) -> None:
        self.http.close()
        self.proc.terminate()
        try:
            self.proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.log.close()


@pytest.fixture(scope="module")
def fleet(tmp_path_factory):
    for spec in NODES.values():  # the loopback aliases must be bindable
        s = socket.socket()
        try:
            s.bind((spec["ip"], 0))
        except OSError:
            pytest.skip("cannot bind %s" % spec["ip"])
        finally:
            s.close()
    root = tmp_path_factory.mktemp("fleet")
    stub_bin = root / "bin"
    stub_bin.mkdir()
    stub = stub_bin / "tailscale"
    stub.write_text("#!/bin/sh\necho 'tailscale disabled in tests' >&2\nexit 1\n")
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    port = _free_port_on_all()
    servers = {name: Server(name, port, root, stub_bin) for name in NODES}
    try:
        for srv in servers.values():
            wait_for(lambda s=srv: s.get("/api/remote/hello"), timeout=60)
        for srv in servers.values():
            srv.refresh()
        yield servers
    finally:
        for srv in servers.values():
            srv.stop()
        for srv in servers.values():
            if os.environ.get("MINDFLOCK_E2E_LOGS") == "1":
                print("==== %s ====" % srv.name)
                print((srv.dir / "server.log").read_text(errors="replace")[-6000:])


SOURCE_1 = {
    "id": "sc1",
    "provider": "shortcut",
    "api_token": "sc-secret-token",
    "project": "Platform",
    "workflow_state": "Ready",
    "label": "first",
}
SOURCE_2 = {
    "id": "lin2",
    "provider": "linear",
    "api_token": "lin-secret-token",
    "project": "Mobile",
    "workflow_state": "Todo",
    "label": "second",
}


def _seed_alpha(alpha: Server) -> None:
    alpha.post(
        "/api/settings",
        {
            "github": {
                "enabled": False,
                "issues_enabled": False,
                "repos": ["acme/app", "acme/api"],
            },
            "general": {"session_budget_usd": 7.5},
        },
    )
    alpha.put("/api/settings/ticketing/sources", {"sources": [SOURCE_1]})
    alpha.post(
        "/api/prefs",
        {
            "prompt_presets": [
                {"name": "Ship it", "prompt": "Run the tests, then ship."}
            ]
        },
    )


def test_fleet_end_to_end(fleet):
    alpha, beta, gamma, rogue = (fleet[n] for n in ("alpha", "beta", "gamma", "rogue"))

    # --- every node sees the others; nobody is in a fleet yet ----------------
    for srv in (alpha, beta, gamma):
        hello = srv.get("/api/remote/hello")
        assert hello["fleet"] == "" and hello["fleet_proto"] >= 1
    wait_for(
        lambda: {c["device"] for c in beta.fleet()["candidates"]} >= {"alpha", "gamma"}
    )

    _seed_alpha(alpha)

    # --- the rogue (not a member) can't read alpha's settings export ----------
    r = httpx.get(
        "http://%s:%d/api/settings/sync/export" % (alpha.ip, alpha.port),
        headers={"X-MindFlock-Remote": "rogue"},
    )
    assert r.status_code in (401, 403), r.text

    # --- privileged routes refuse a forwarded or relayed caller ---------------
    r = alpha.post(
        "/api/fleet/invite", ok=False, headers={"X-Forwarded-For": "100.64.0.9"}
    )
    assert r.status_code == 403, r.text
    r = alpha.post(
        "/api/fleet/invite", ok=False, headers={"X-MindFlock-Remote": "rogue"}
    )
    assert r.status_code == 403, r.text

    # --- 1. beta joins alpha with a code made on alpha ------------------------
    invite = alpha.post("/api/fleet/invite")
    assert invite["device"] == "alpha" and len(invite["code"].replace("-", "")) == 8
    assert "mindflock devices join alpha" in invite["command"]
    joined = beta.post("/api/fleet/join", {"device": "alpha", "code": invite["code"]})
    assert joined, joined
    wait_for(lambda: beta.fleet()["in_fleet"])
    assert alpha.fleet()["in_fleet"]
    assert alpha.fleet_file()["id"] == beta.fleet_file()["id"]
    # alpha lists beta once beta itself confirms (its announce) — redeeming
    # the code alone adds nobody.
    wait_for(lambda: {m["key"] for m in alpha.fleet()["members"]} == {"alpha", "beta"})
    # The code is single use.
    r = gamma.post(
        "/api/fleet/join", {"device": "alpha", "code": invite["code"]}, ok=False
    )
    assert r.status_code != 200

    # beta now carries alpha's settings — secrets included (fleet key = full trust).
    wait_for(lambda: beta.settings()["github"].get("repos") == ["acme/app", "acme/api"])
    assert beta.sources()["sc1"]["project"] == "Platform"
    sync_b = json.loads((beta.dir / "settings.json").read_text())
    assert sync_b["ticketing"]["sources"][0]["api_token"] == "sc-secret-token"
    assert beta.get("/api/prefs")["prompt_presets"][0]["name"] == "Ship it"
    assert beta.get("/api/settings/sync")["enabled"] is True

    # --- 2. gamma asks beta to join; beta approves ----------------------------
    req = gamma.post("/api/fleet/request", {"device": "beta"})
    code = req["code"]
    pending = wait_for(lambda: beta.fleet()["requests"])
    assert pending[0]["device"] == "gamma" and pending[0]["code"] == code
    beta.post("/api/fleet/requests/%s/approve" % pending[0]["id"])
    wait_for(lambda: gamma.get("/api/fleet/request")["state"] == "joined", timeout=30)
    wait_for(
        lambda: gamma.settings()["github"].get("repos") == ["acme/app", "acme/api"]
    )
    # Roster gossip: alpha learns about gamma without being involved.
    wait_for(
        lambda: {m["key"] for m in alpha.fleet()["members"]}
        == {"alpha", "beta", "gamma"},
        timeout=45,
    )

    # The fleet key is a credential on every member (gate-on gamma included).
    fkey = beta.fleet_file()["key"]
    r = httpx.get(
        "http://%s:%d/api/instances" % (gamma.ip, gamma.port),
        headers={"Authorization": "Bearer " + fkey},
    )
    assert r.status_code == 200, r.text
    r = httpx.get(
        "http://%s:%d/api/instances" % (gamma.ip, gamma.port),
        headers={"Authorization": "Bearer not-the-key-0000000000"},
    )
    assert r.status_code == 401

    # --- 3. an edit on gamma reaches alpha and beta ----------------------------
    gamma.post("/api/settings", {"general": {"session_budget_usd": 12.0}})

    def everyone_has_budget():
        for srv in (alpha, beta):
            srv.sync_now()
        return all(
            srv.settings()["general"].get("session_budget_usd") == 12.0
            for srv in (alpha, beta)
        )

    wait_for(everyone_has_budget, timeout=30, every=1.0)

    # --- 4. concurrent edits to DIFFERENT ticket sources both survive ---------
    alpha.put("/api/settings/ticketing/sources", {"sources": [SOURCE_1, SOURCE_2]})
    beta.put(
        "/api/settings/ticketing/sources",
        {"sources": [dict(SOURCE_1, label="first-edited", api_token="")]},
    )

    def sources_converged():
        for srv in (alpha, beta, gamma):
            srv.sync_now()
        views = [srv.sources() for srv in (alpha, beta, gamma)]
        return all(
            set(v) == {"sc1", "lin2"} and v["sc1"]["label"] == "first-edited"
            for v in views
        )

    wait_for(sources_converged, timeout=40, every=1.0)

    # --- 5. a delete propagates and doesn't come back --------------------------
    gamma.put(
        "/api/settings/ticketing/sources",
        {"sources": [dict(SOURCE_1, label="first-edited", api_token="")]},
    )

    def deleted_everywhere():
        for srv in (alpha, beta, gamma):
            srv.sync_now()
        return all(set(srv.sources()) == {"sc1"} for srv in (alpha, beta, gamma))

    wait_for(deleted_everywhere, timeout=40, every=1.0)

    # --- 6. the rogue's edits never land ---------------------------------------
    rogue.post("/api/settings", {"github": {"repos": ["evil/repo"]}})
    for srv in (alpha, beta, gamma):
        srv.refresh()
        srv.sync_now()
    for srv in (alpha, beta, gamma):
        assert srv.settings()["github"].get("repos") == ["acme/app", "acme/api"]

    # --- 7. guessing invite codes: one caller is locked out, many burn them -----
    invite2 = alpha.post("/api/fleet/invite")
    pub = "http://%s:%d/api/fleet/redeem" % (alpha.ip, alpha.port)

    def guess(code: str, source: Optional[str] = None, **kw) -> httpx.Response:
        transport = httpx.HTTPTransport(local_address=source) if source else None
        with httpx.Client(transport=transport, timeout=30.0) as c:
            return c.post(
                pub, json={"code": code, "device": "rogue", "host": "rogue"}, **kw
            )

    # The public join routes answer only the tailnet (or this machine itself,
    # unproxied): a caller behind a proxy hop is refused before any guess.
    r = guess("ZZZZ-ZZZZ", headers={"X-Forwarded-For": "203.0.113.9"})
    assert r.status_code == 403, r.text
    # One caller only ever locks ITSELF out — even the real code is refused
    # to it now — but its guesses never burn the invite for anyone else. (Its
    # own address: the servers' outbound calls all come from 127.0.0.1.)
    for i in range(5):
        r = guess("ZZZZ-ZZZ%d" % i, "127.0.0.11")
        assert r.status_code == 403, r.text
    r = guess(invite2["code"], "127.0.0.11")
    assert r.status_code == 429, "a locked-out caller must wait: " + r.text
    assert len(alpha.fleet()["invites"]) == 1, "one caller burned the invite"
    # Misses from several addresses (four, five each) cancel every invite.
    for source in ("127.0.0.6", "127.0.0.7", "127.0.0.8", "127.0.0.9"):
        for i in range(5):
            r = guess("YYYY-YYY%d" % i, source)
            assert r.status_code in (403, 429), r.text
    assert alpha.fleet()["invites"] == []
    r = guess(invite2["code"], "127.0.0.10")
    assert r.status_code == 403, "a burned invite must not work: " + r.text
    assert "rogue" not in {m["key"] for m in alpha.fleet()["members"]}

    # --- 8. removing gamma rekeys the others; gamma's old key stops working ----
    old_key = alpha.fleet_file()["key"]
    removed = alpha.post("/api/fleet/members/gamma/remove")
    assert "beta" in removed.get("rekeyed", []), removed
    # Access tokens are replaced too (the default): alpha's own (from its
    # settings) is; beta's is pinned by MINDFLOCK_AUTH_TOKEN, so that one is
    # reported as not replaced rather than silently skipped.
    assert "alpha" in removed["rotated"], removed
    assert "beta" in removed["rotate_failed"], removed
    assert "gamma" not in removed["rotated"] + removed["rotate_failed"], removed
    new_key = alpha.fleet_file()["key"]
    assert new_key != old_key
    wait_for(lambda: beta.fleet_file()["key"] == new_key)
    r = httpx.get(
        "http://%s:%d/api/instances" % (beta.ip, beta.port),
        headers={"Authorization": "Bearer " + old_key},
    )
    assert r.status_code == 401
    assert {m["key"] for m in beta.fleet()["members"]} == {"alpha", "beta"}
    # gamma (online, unaware) finds out within one gossip round.
    wait_for(
        lambda: gamma.fleet()["stale_key"] or not gamma.fleet()["in_fleet"],
        timeout=75,
        every=2.0,
    )

    # --- 9. beta leaves; alpha is alone ------------------------------------------
    beta.post("/api/fleet/leave")
    assert beta.fleet()["in_fleet"] is False
    wait_for(lambda: {m["key"] for m in alpha.fleet()["members"]} == {"alpha"})
