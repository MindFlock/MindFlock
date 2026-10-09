"""One Tailscale truth (backend.tailscale_cli): where the CLI is, the shared
status snapshot, and the health model the doctor and Settings → Devices'
"Tailscale on this device" card render.

What is pinned:

* resolver precedence — ``$MINDFLOCK_TAILSCALE_BIN`` → PATH → the macOS app
  bundle → (WSL) Windows ``tailscale.exe``, which is reported but never used;
* the ``status --json`` snapshot is cached and shared: N callers inside the
  window cost one subprocess, and every call site reads through it;
* health parsing for signed-out, stopped, expired, near-expiry and tagged
  nodes, with the one fix each needs;
* the routes: health is read-only and blanks the login and sign-in URL for
  anyone but the person at this device; login is refused for them.
"""

from __future__ import annotations

import datetime as dt
import json
import shutil
import subprocess
import threading

import pytest
from fastapi.testclient import TestClient

from backend import osenv
from backend import tailscale_cli as ts
from backend.web import server

NOW = dt.datetime(2026, 10, 9, 12, 0, tzinfo=dt.timezone.utc)


def _exe(tmp_path, name):
    p = tmp_path / name
    p.write_text("#!/bin/sh\n")
    p.chmod(0o755)
    return str(p)


@pytest.fixture
def linux(monkeypatch):
    monkeypatch.setattr(osenv, "os_kind", lambda: "linux")
    monkeypatch.delenv(ts.BIN_ENV, raising=False)
    monkeypatch.delenv(ts.STATUS_FILE_ENV, raising=False)


def _status(**over):
    doc = {
        "Version": "1.102.2",
        "BackendState": "Running",
        "AuthURL": "",
        "MagicDNSSuffix": "tail0000.ts.net",
        "CurrentTailnet": {
            "Name": "me@example.com",
            "MagicDNSSuffix": "tail0000.ts.net",
            "MagicDNSEnabled": True,
        },
        "CertDomains": ["box.tail0000.ts.net"],
        "Health": [],
        "Self": {
            "ID": "n1",
            "HostName": "Box",
            "DNSName": "box.tail0000.ts.net.",
            "OS": "linux",
            "UserID": 1,
            "TailscaleIPs": ["100.64.0.10", "fd7a:115c:a1e0::a"],
            "KeyExpiry": "2027-04-04T18:05:17Z",
        },
        "User": {"1": {"LoginName": "me@example.com"}},
        "Peer": {},
    }
    self_over = over.pop("Self", None)
    doc.update(over)
    if self_over is not None:
        doc["Self"] = {**doc["Self"], **self_over}
    return doc


def _health(monkeypatch, doc, os_kind="linux"):
    monkeypatch.setattr(osenv, "os_kind", lambda: os_kind)
    monkeypatch.setattr(
        shutil, "which", lambda n: "/usr/bin/tailscale" if n == "tailscale" else None
    )
    monkeypatch.setattr(
        ts,
        "status_json",
        lambda fresh=False: json.loads(json.dumps(doc)) if doc is not None else None,
    )
    return ts.health(now=NOW)


def _ids(h):
    return [i["id"] for i in h["issues"]]


# --------------------------------------------------------------------------- #
# Resolver
# --------------------------------------------------------------------------- #
def test_env_override_beats_path(monkeypatch, tmp_path, linux):
    mine = _exe(tmp_path, "my-tailscale")
    monkeypatch.setenv(ts.BIN_ENV, mine)
    monkeypatch.setattr(shutil, "which", lambda n: "/usr/bin/tailscale")
    r = ts.resolve()
    assert (r.path, r.source, r.kind, r.usable) == (mine, "env", "native", True)


def test_a_missing_env_override_falls_through_to_path(monkeypatch, tmp_path, linux):
    monkeypatch.setenv(ts.BIN_ENV, str(tmp_path / "nope"))
    monkeypatch.setattr(
        shutil, "which", lambda n: "/usr/bin/tailscale" if n == "tailscale" else None
    )
    assert ts.resolve().path == "/usr/bin/tailscale"


def test_path_beats_the_app_bundle(monkeypatch, tmp_path, linux):
    monkeypatch.setattr(osenv, "os_kind", lambda: "macos")
    monkeypatch.setattr(ts, "APP_BUNDLE_CLI", _exe(tmp_path, "Tailscale"))
    monkeypatch.setattr(
        shutil,
        "which",
        lambda n: "/opt/homebrew/bin/tailscale" if n == "tailscale" else None,
    )
    assert ts.resolve().source == "path"


def test_macos_app_bundle_when_nothing_is_on_path(monkeypatch, tmp_path, linux):
    cli = _exe(tmp_path, "Tailscale")
    monkeypatch.setattr(osenv, "os_kind", lambda: "macos")
    monkeypatch.setattr(ts, "APP_BUNDLE_CLI", cli)
    monkeypatch.setattr(shutil, "which", lambda n: None)
    r = ts.resolve()
    assert (r.path, r.kind, r.usable) == (cli, "app-bundle", True)
    assert ts.tailscale_bin() == cli
    # The bundle binary only acts as a CLI with TAILSCALE_BE_CLI=1.
    assert ts.env()["TAILSCALE_BE_CLI"] == "1"
    assert ts.command(["tailscale", "status"]) == [cli, "status"]


def test_app_bundle_is_only_looked_for_on_macos(monkeypatch, tmp_path, linux):
    monkeypatch.setattr(ts, "APP_BUNDLE_CLI", _exe(tmp_path, "Tailscale"))
    monkeypatch.setattr(shutil, "which", lambda n: None)
    assert ts.resolve() is None


def test_wsl_windows_exe_is_reported_but_never_used(monkeypatch, tmp_path, linux):
    exe = tmp_path / "tailscale.exe"
    exe.write_text("")
    monkeypatch.setattr(osenv, "os_kind", lambda: "wsl")
    monkeypatch.setattr(ts, "WINDOWS_CANDIDATES", (str(exe),))
    monkeypatch.setattr(shutil, "which", lambda n: None)
    r = ts.resolve()
    assert (r.kind, r.usable) == ("wsl-windows-host", False)
    assert ts.tailscale_bin() is None and not ts.available()
    # Nothing shells out to the Windows node for this machine's identity.
    assert ts.status_json() is None
    assert ts.command(["tailscale", "status"]) == ["tailscale", "status"]


def test_wsl_windows_exe_on_path_is_found(monkeypatch, linux):
    monkeypatch.setattr(osenv, "os_kind", lambda: "wsl")
    monkeypatch.setattr(ts, "WINDOWS_CANDIDATES", ())
    monkeypatch.setattr(
        shutil,
        "which",
        lambda n: "/mnt/c/Tools/tailscale.exe" if n == "tailscale.exe" else None,
    )
    assert ts.windows_tailscale() == "/mnt/c/Tools/tailscale.exe"


def test_wsl_with_both_prefers_the_in_wsl_node(monkeypatch, tmp_path, linux):
    exe = tmp_path / "tailscale.exe"
    exe.write_text("")
    monkeypatch.setattr(ts, "WINDOWS_CANDIDATES", (str(exe),))
    h = _health(monkeypatch, _status(), os_kind="wsl")
    assert h["installed"] and h["path"] == "/usr/bin/tailscale"
    assert h["windows_host"] == str(exe)
    assert _ids(h) == ["wsl_two_nodes"]  # a note, not a problem


# --------------------------------------------------------------------------- #
# Shared cache
# --------------------------------------------------------------------------- #
def test_status_is_cached_and_shared_across_callers(monkeypatch, linux):
    from backend.web.core import mobile_access, remote, shared_link, tailnet_trust

    calls = []

    class CP:
        returncode = 0
        stdout = json.dumps(_status()).encode()

    def fake_run(args, **kw):
        calls.append(args)
        return CP()

    monkeypatch.setattr(
        shutil, "which", lambda n: "/usr/bin/tailscale" if n == "tailscale" else None
    )
    monkeypatch.setattr(subprocess, "run", fake_run)
    remote.tailscale_nodes()
    tailnet_trust.status()
    shared_link._tailscale_status()
    mobile_access._tailscale_info()
    ts.health()
    assert calls == [["/usr/bin/tailscale", "status", "--json"]]
    # Callers get a copy: mutating it doesn't poison the next reader.
    ts.status_json()["Self"]["HostName"] = "changed"
    assert ts.status_json()["Self"]["HostName"] == "Box"
    ts.status_json(fresh=True)
    assert len(calls) == 2


def test_cache_expires(monkeypatch, linux):
    n = {"calls": 0}

    class CP:
        returncode = 0
        stdout = b"{}"

    def fake_run(args, **kw):
        n["calls"] += 1
        return CP()

    clock = {"t": 1000.0}
    monkeypatch.setattr(ts.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(
        shutil, "which", lambda n: "/usr/bin/tailscale" if n == "tailscale" else None
    )
    monkeypatch.setattr(subprocess, "run", fake_run)
    ts.status_json()
    clock["t"] += ts.CACHE_TTL - 0.1
    ts.status_json()
    assert n["calls"] == 1
    clock["t"] += 0.2
    ts.status_json()
    assert n["calls"] == 2


def test_concurrent_callers_share_one_subprocess(monkeypatch, linux):
    gate = threading.Event()
    n = {"calls": 0}

    class CP:
        returncode = 0
        stdout = b'{"BackendState": "Running"}'

    def slow_run(args, **kw):
        n["calls"] += 1
        gate.wait(2)
        return CP()

    monkeypatch.setattr(
        shutil, "which", lambda n: "/usr/bin/tailscale" if n == "tailscale" else None
    )
    monkeypatch.setattr(subprocess, "run", slow_run)
    out = []
    threads = [
        threading.Thread(target=lambda: out.append(ts.status_json())) for _ in range(5)
    ]
    for t in threads:
        t.start()
    gate.set()
    for t in threads:
        t.join(3)
    assert n["calls"] == 1
    assert out == [{"BackendState": "Running"}] * 5


def test_status_file_hook_is_one_truth(monkeypatch, tmp_path, linux):
    path = tmp_path / "ts.json"
    path.write_text(json.dumps(_status()))
    monkeypatch.setenv(ts.STATUS_FILE_ENV, str(path))
    monkeypatch.setattr(shutil, "which", lambda n: None)
    assert ts.status_json()["Self"]["HostName"] == "Box"
    assert ts.tailnet_ips() == ["100.64.0.10", "fd7a:115c:a1e0::a"]
    assert ts.self_ipv4() == "100.64.0.10"


# --------------------------------------------------------------------------- #
# Health
# --------------------------------------------------------------------------- #
def test_healthy_running_node(monkeypatch):
    h = _health(monkeypatch, _status())
    assert h["installed"] and h["running"] and h["backend_state"] == "Running"
    assert h["tailnet"] == "me@example.com" and h["user"] == "me@example.com"
    assert h["device"]["dns"] == "box.tail0000.ts.net"
    assert h["device"]["ips"] == ["100.64.0.10", "fd7a:115c:a1e0::a"]
    assert h["magicdns"] is True and h["https"] is True
    assert h["key_expiry"]["at"] == "2027-04-04T18:05:17Z"
    assert h["key_expiry"]["warn"] is False
    assert h["issues"] == []


def test_needs_login(monkeypatch):
    h = _health(
        monkeypatch,
        _status(
            BackendState="NeedsLogin",
            AuthURL="https://login.tailscale.com/a/abc",
            CurrentTailnet=None,
            Self={"TailscaleIPs": [], "KeyExpiry": None},
        ),
    )
    assert not h["running"] and h["auth_url"] == "https://login.tailscale.com/a/abc"
    assert _ids(h) == ["sign_in"]
    assert h["issues"][0]["fix"] == "sudo tailscale up --operator=$USER"
    assert h["https"] is None  # unknown, not "off"


def test_needs_login_on_macos_says_tailscale_login(monkeypatch):
    h = _health(monkeypatch, _status(BackendState="NeedsLogin"), os_kind="macos")
    assert h["issues"][0]["fix"] == "tailscale login"


def test_stopped(monkeypatch):
    h = _health(monkeypatch, _status(BackendState="Stopped"))
    assert _ids(h) == ["stopped"] and h["issues"][0]["fix"] == "tailscale up"


def test_daemon_not_answering(monkeypatch):
    h = _health(monkeypatch, None)
    assert h["installed"] and _ids(h) == ["not_running"]


def test_expired_key_fails(monkeypatch):
    h = _health(
        monkeypatch,
        _status(Self={"KeyExpiry": "2026-10-01T00:00:00Z", "Expired": True}),
    )
    assert h["key_expiry"]["expired"] is True
    issue = h["issues"][0]
    assert issue["id"] == "key_expiry" and issue["level"] == "fail"
    assert "has expired" in issue["message"]
    assert "Disable key expiry" in issue["message"]


def test_expiry_within_30_days_warns(monkeypatch):
    h = _health(monkeypatch, _status(Self={"KeyExpiry": "2026-10-23T15:25:55Z"}))
    exp = h["key_expiry"]
    assert exp["days"] == 14 and exp["warn"] and not exp["expired"]
    assert h["issues"][0]["level"] == "warn"
    assert "expires in 14 days" in h["issues"][0]["message"]


def test_expiry_beyond_30_days_is_quiet(monkeypatch):
    h = _health(monkeypatch, _status(Self={"KeyExpiry": "2026-11-09T12:00:01Z"}))
    assert h["key_expiry"]["days"] == 31 and not h["key_expiry"]["warn"]
    assert "key_expiry" not in _ids(h)


def test_no_key_expiry_at_all(monkeypatch):
    h = _health(monkeypatch, _status(Self={"KeyExpiry": None}))
    assert h["key_expiry"] == {"at": "", "days": None, "expired": False, "warn": False}


def test_tagged_device_warns_that_console_tagging_keeps_expiry(monkeypatch):
    h = _health(
        monkeypatch,
        _status(
            Self={
                "Tags": ["tag:mindflock"],
                "UserID": 2,
                "KeyExpiry": "2026-10-23T15:25:55Z",
            }
        ),
    )
    assert h["tagged"] and h["tags"] == ["tag:mindflock"]
    assert h["user"] == ""  # a tagged node is nobody's login
    msg = h["issues"][0]["message"]
    assert "keeps its expiry" in msg and "Disable key expiry" in msg
    assert "--force-reauth" in msg  # offered only with its warning
    assert "SSH" in msg


def test_magicdns_off(monkeypatch):
    doc = _status()
    doc["CurrentTailnet"]["MagicDNSEnabled"] = False
    h = _health(monkeypatch, doc)
    assert h["magicdns"] is False and _ids(h) == ["magicdns_off"]


def test_https_off(monkeypatch):
    h = _health(monkeypatch, _status(CertDomains=None))
    assert h["https"] is False and _ids(h) == ["https_off"]


def test_issues_are_worst_first(monkeypatch):
    doc = _status(CertDomains=None, Self={"KeyExpiry": "2026-10-01T00:00:00Z"})
    h = _health(monkeypatch, doc)
    assert [i["level"] for i in h["issues"]] == ["fail", "info"]


def test_not_installed_linux(monkeypatch, linux):
    monkeypatch.setattr(shutil, "which", lambda n: None)
    h = ts.health()
    assert not h["installed"] and _ids(h) == ["missing"]
    assert h["issues"][0]["fix"] == ts.LINUX_INSTALL


def test_magicdns_off_drops_the_name_from_phone_urls(monkeypatch, linux):
    from backend.web.core import mobile_access

    doc = _status()
    doc["CurrentTailnet"]["MagicDNSEnabled"] = False
    monkeypatch.setattr(ts, "status_json", lambda fresh=False: doc)
    assert mobile_access._tailscale_info() == (None, "100.64.0.10")


# --------------------------------------------------------------------------- #
# Sign in
# --------------------------------------------------------------------------- #
class _FakeProc:
    def __init__(self, lines, rc=None):
        import io

        self.stdout = io.BytesIO(b"".join(line.encode() + b"\n" for line in lines))
        self._rc = rc
        self.returncode = rc

    def poll(self):
        return self._rc

    def wait(self, timeout=None):
        return self._rc


@pytest.fixture
def fresh_login(monkeypatch, linux):
    monkeypatch.setattr(
        ts, "_LOGIN", {"proc": None, "auth_url": "", "output": [], "started": 0.0}
    )
    monkeypatch.setattr(
        shutil, "which", lambda n: "/usr/bin/tailscale" if n == "tailscale" else None
    )


def test_login_returns_the_auth_url_without_waiting(monkeypatch, fresh_login):
    seen = {}

    def popen(args, **kw):
        seen["args"] = args
        return _FakeProc(
            [
                "",
                "To authenticate, visit:",
                "",
                "\thttps://login.tailscale.com/a/xyz",
                "",
            ]
        )

    monkeypatch.setattr(
        ts, "status_json", lambda fresh=False: {"BackendState": "NeedsLogin"}
    )
    monkeypatch.setattr(subprocess, "Popen", popen)
    out = ts.start_login(wait=2)
    assert out["ok"] and out["auth_url"] == "https://login.tailscale.com/a/xyz"
    assert seen["args"][:2] == ["/usr/bin/tailscale", "login"]


def test_login_brings_a_stopped_node_up(monkeypatch, fresh_login):
    seen = {}

    def popen(args, **kw):
        seen["args"] = args
        return _FakeProc([], rc=0)

    monkeypatch.setattr(
        ts, "status_json", lambda fresh=False: {"BackendState": "Stopped"}
    )
    monkeypatch.setattr(subprocess, "Popen", popen)
    assert ts.start_login(wait=1)["ok"]
    assert seen["args"] == ["/usr/bin/tailscale", "up"]


def test_login_without_operator_says_the_one_command(monkeypatch, fresh_login):
    monkeypatch.setattr(
        ts, "status_json", lambda fresh=False: {"BackendState": "NeedsLogin"}
    )
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda args, **kw: _FakeProc(
            ["Access denied: watch IPN bus access denied, must have operator"], rc=1
        ),
    )
    out = ts.start_login(wait=1)
    assert not out["ok"] and out["fix"] == "sudo tailscale up --operator=$USER"


def test_login_when_already_running_does_nothing(monkeypatch, fresh_login):
    monkeypatch.setattr(
        ts, "status_json", lambda fresh=False: {"BackendState": "Running"}
    )
    monkeypatch.setattr(
        subprocess, "Popen", lambda *a, **k: pytest.fail("must not run login")
    )
    assert ts.start_login(wait=0) == {
        "ok": True,
        "state": "Running",
        "auth_url": "",
        "error": "",
        "fix": "",
    }


def test_login_refuses_the_windows_node_from_wsl(monkeypatch, tmp_path, linux):
    exe = tmp_path / "tailscale.exe"
    exe.write_text("")
    monkeypatch.setattr(osenv, "os_kind", lambda: "wsl")
    monkeypatch.setattr(ts, "WINDOWS_CANDIDATES", (str(exe),))
    monkeypatch.setattr(shutil, "which", lambda n: None)
    monkeypatch.setattr(
        subprocess, "Popen", lambda *a, **k: pytest.fail("must not run tailscale.exe")
    )
    out = ts.start_login(wait=0)
    assert not out["ok"] and "inside WSL" in out["error"]
    assert "tailscale up --hostname=" in out["fix"]


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
_PENDING = {
    "installed": True,
    "backend_state": "NeedsLogin",
    "user": "me@example.com",
    "auth_url": "https://login.tailscale.com/a/xyz",
    "issues": [],
}


def test_health_route_for_this_machine_has_the_sign_in_url(monkeypatch):
    monkeypatch.setattr(ts, "health", lambda fresh=False: dict(_PENDING))
    c = TestClient(
        server.app, client=("127.0.0.1", 50000), headers={"host": "127.0.0.1"}
    )
    body = c.get("/api/tailscale/health").json()
    assert body["auth_url"] == "https://login.tailscale.com/a/xyz"
    assert body["user"] == "me@example.com"
    assert "auth_qr_svg" in body


def test_health_route_blanks_identity_and_auth_url_for_others(monkeypatch):
    monkeypatch.setattr(ts, "health", lambda fresh=False: dict(_PENDING))
    c = TestClient(server.app, client=("100.64.0.99", 50000))
    body = c.get("/api/tailscale/health").json()
    assert body["auth_url"] == "" and body["user"] == ""
    assert "auth_qr_svg" not in body
    assert body["backend_state"] == "NeedsLogin"


def test_login_route_is_refused_off_this_machine(monkeypatch):
    monkeypatch.setattr(
        ts, "start_login", lambda: pytest.fail("must not start a login")
    )
    c = TestClient(server.app, client=("100.64.0.99", 50000))
    assert c.post("/api/tailscale/login").status_code == 403
    relayed = TestClient(
        server.app, client=("127.0.0.1", 50000), headers={"host": "127.0.0.1"}
    )
    r = relayed.post("/api/tailscale/login", headers={"X-MindFlock-Remote": "laptop"})
    assert r.status_code == 403


def test_login_route_from_this_machine(monkeypatch):
    monkeypatch.setattr(
        ts,
        "start_login",
        lambda: {
            "ok": True,
            "state": "NeedsLogin",
            "auth_url": "https://login.tailscale.com/a/q",
            "error": "",
            "fix": "",
        },
    )
    c = TestClient(
        server.app, client=("127.0.0.1", 50000), headers={"host": "127.0.0.1"}
    )
    r = c.post("/api/tailscale/login")
    assert r.status_code == 200
    assert r.json()["auth_url"] == "https://login.tailscale.com/a/q"


def test_mobile_payload_leads_with_tailscale_on_the_phone(monkeypatch):
    from backend.web.core import mobile_access

    monkeypatch.setattr(mobile_access, "_tailscale_login", lambda: "me@example.com")
    here = (
        TestClient(
            server.app, client=("127.0.0.1", 50000), headers={"host": "127.0.0.1"}
        )
        .get("/api/mobile")
        .json()
    )
    assert here["phone_app"]["url"] == "https://tailscale.com/download"
    assert here["phone_app"]["login"] == "me@example.com"
    away = (
        TestClient(server.app, client=("100.64.0.99", 50000)).get("/api/mobile").json()
    )
    assert away["phone_app"]["login"] == ""  # who you are is for this device only
