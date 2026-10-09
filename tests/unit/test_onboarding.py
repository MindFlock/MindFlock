"""The first-run plan (backend.onboarding), the onboarding addon's routes,
Connect GitHub's device flow (backend.web.core.github_auth), the new-computer
bootstrap line, per-member readiness and uninstall leaving your devices."""

from __future__ import annotations

import asyncio
import os
import subprocess

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import __version__, git_auth_hints, onboarding, uninstall
from backend.web.addons.onboarding import OnboardingAddon
from backend.web.core import auth as web_auth
from backend.web.core import bootstrap, github_auth, readiness


def _ids(plan):
    return [(s["id"], s["status"]) for s in plan["steps"]]


def _check(cid, status, **kw):
    return {"id": cid, "label": kw.pop("label", cid), "status": status, **kw}


_TS_OFF = {
    "installed": False,
    "backend_state": "",
    "issues": [
        {
            "id": "missing",
            "level": "info",
            "message": "Tailscale isn't installed.",
            "fix": "brew install --cask tailscale-app",
        }
    ],
}
_TS_ON = {
    "installed": True,
    "backend_state": "Running",
    "tailnet": "me.ts.net",
    "issues": [],
}


# --------------------------------------------------------------------------- #
# plan ordering snapshots
# --------------------------------------------------------------------------- #
def test_fresh_mac():
    plan = onboarding.build_plan(
        {
            "os": "macos",
            "checks": [
                _check("git", "ok"),
                _check("tmux", "fail", label="tmux"),
                _check(
                    "agent-cli", "fail", label="agent CLI (claude)", provider="claude"
                ),
                _check("agent-auth", "warn", provider="claude", cmd="claude"),
            ],
            "devices": {"choice": "", "in_fleet": False, "members": 0},
            "tailscale": _TS_OFF,
            "github": {"connected": False, "identity": {"name": "", "email": ""}},
            "repo": {"path": "", "onboarded": False},
        }
    )
    assert _ids(plan) == [
        ("deps", "todo"),
        ("devices", "todo"),
        ("agent", "todo"),
        ("tailscale", "skip"),
        ("github", "todo"),
        ("repo", "todo"),
    ]
    steps = {s["id"]: s for s in plan["steps"]}
    assert steps["deps"]["reason"] == "missing: tmux, agent CLI (claude)"
    assert steps["devices"]["ask"] is True
    assert steps["agent"]["reason"] == "install claude first (Dependencies)"
    assert plan["next"] == "deps" and not plan["done"]


def test_wsl_without_tmux_first_computer():
    plan = onboarding.build_plan(
        {
            "os": "wsl",
            "checks": [
                _check("git", "ok"),
                _check("tmux", "fail"),
                _check("agent-cli", "ok", provider="claude"),
                _check("agent-auth", "ok", provider="claude"),
            ],
            "devices": {"choice": "first", "in_fleet": False, "members": 0},
            "tailscale": _TS_OFF,
            "github": {
                "connected": True,
                "login": "octo",
                "identity": {"name": "Octo", "email": "o@x"},
            },
            "repo": {"path": "/home/me/repo"},
        }
    )
    assert _ids(plan) == [
        ("deps", "todo"),
        ("devices", "ok"),
        ("agent", "ok"),
        ("tailscale", "skip"),
        ("github", "ok"),
        ("repo", "ok"),
    ]
    assert plan["next"] == "deps"
    assert plan["steps"][4]["reason"] == "connected as @octo"


def test_joining_a_second_computer():
    """Joining brings the default agent and the GitHub token: both wait for
    it, and Tailscale is part of the plan — and blocks the join."""
    plan = onboarding.build_plan(
        {
            "os": "linux",
            "checks": [
                _check("tmux", "ok"),
                _check("agent-cli", "ok", provider="claude"),
                _check("agent-auth", "warn", provider="claude", cmd="claude"),
            ],
            "devices": {"choice": "join", "in_fleet": False, "members": 0},
            "tailscale": {
                "installed": True,
                "backend_state": "NeedsLogin",
                "issues": [
                    {
                        "id": "sign_in",
                        "level": "warn",
                        "message": "Tailscale isn't signed in on this device.",
                    }
                ],
            },
            "github": {"connected": False},
            "repo": {},
        }
    )
    assert _ids(plan) == [
        ("deps", "ok"),
        ("devices", "todo"),
        ("agent", "skip"),
        ("tailscale", "todo"),
        ("github", "skip"),
        ("repo", "todo"),
    ]
    steps = {s["id"]: s for s in plan["steps"]}
    assert "sign-ins stay on each computer" in steps["agent"]["reason"]
    assert "after Tailscale" in steps["devices"]["reason"]
    assert plan["next"] == "tailscale"  # what blocks the join


def test_joined_second_computer_signs_its_agent_in_here():
    plan = onboarding.build_plan(
        {
            "checks": [
                _check("agent-auth", "warn", provider="codex", cmd="codex login")
            ],
            "devices": {"choice": "join", "in_fleet": True, "members": 2},
            "tailscale": _TS_ON,
            "github": {
                "connected": True,
                "login": "",
                "identity": {"name": "a", "email": "b@c"},
            },
            "repo": {"onboarded": True},
        }
    )
    steps = {s["id"]: s for s in plan["steps"]}
    assert steps["devices"]["status"] == "ok"
    assert steps["agent"]["status"] == "todo" and steps["agent"]["sign_in"] is True
    assert steps["tailscale"]["status"] == "ok"
    assert plan["next"] == "agent"


def test_order_is_fixed():
    plan = onboarding.build_plan({})
    assert [s["id"] for s in plan["steps"]] == list(onboarding.ORDER)


def test_choice_is_validated_and_local(monkeypatch):
    from backend.config import settings as store
    from backend.web.core import settings_sync

    assert onboarding.set_choice("join") == "join"
    store.invalidate()
    assert store.load_settings().general.setup_devices == "join"
    with pytest.raises(ValueError):
        onboarding.set_choice("maybe")
    assert "setup_devices" in settings_sync.LOCAL["general"]
    assert "oauth_client_id" in settings_sync.SYNCED["github"]


# --------------------------------------------------------------------------- #
# routes: privileged only
# --------------------------------------------------------------------------- #
@pytest.fixture
def client(monkeypatch):
    state = {"privileged": False}

    async def privileged(scope):
        return state["privileged"]

    monkeypatch.setattr(web_auth, "privileged", privileged)
    app = FastAPI()
    app.include_router(OnboardingAddon().router)
    c = TestClient(app)
    c.state = state  # type: ignore[attr-defined]
    return c


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("post", "/api/onboarding/choice", {"choice": "first"}),
        ("post", "/api/github/device/start", None),
        ("post", "/api/github/device/poll", None),
        ("delete", "/api/github/device", None),
        ("post", "/api/github/token", {"token": "ghp_x"}),
        ("post", "/api/github/import-gh", None),
        ("post", "/api/github/gh-login-close", None),
        ("post", "/api/github/identity", {"name": "a", "email": "a@b"}),
        ("post", "/api/github/git-credential", None),
        ("get", "/api/github/push-check", None),
        ("post", "/api/fleet/bootstrap", None),
        ("get", "/api/fleet/readiness", None),
    ],
)
def test_mutating_routes_refuse_a_non_privileged_caller(client, method, path, body):
    kw = {"json": body} if body is not None else {}
    r = getattr(client, method)(path, **kw)
    assert r.status_code == 403, (path, r.text)


def test_readiness_self_wants_the_fleet_key(client, monkeypatch):
    from backend.web.core import fleet

    monkeypatch.setattr(fleet, "key_valid", lambda k: k == "the-key")
    monkeypatch.setattr(readiness, "self_summary", lambda **kw: {"ready": True})
    assert client.get("/api/fleet/readiness/self").status_code == 401
    r = client.get(
        "/api/fleet/readiness/self", headers={"Authorization": "Bearer the-key"}
    )
    assert r.status_code == 200 and r.json() == {"ready": True}


def test_gh_login_terminal_refuses_a_non_privileged_caller(client):
    with client.websocket_connect("/api/github/gh-login-terminal") as ws:
        msg = ws.receive_json()
    assert msg["type"] == "error" and "only allowed" in msg["message"]


def test_choice_route_stores_the_answer(client, monkeypatch):
    client.state["privileged"] = True
    monkeypatch.setattr(
        onboarding, "collect", lambda **kw: {"devices": {"choice": "first"}}
    )
    r = client.post("/api/onboarding/choice", json={"choice": "first"})
    assert r.status_code == 200 and r.json()["choice"] == "first"
    assert (
        client.post("/api/onboarding/choice", json={"choice": "x"}).status_code == 400
    )


# --------------------------------------------------------------------------- #
# the device flow
# --------------------------------------------------------------------------- #
class FakeGitHub:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, method, url, *, data=None, headers=None, timeout=10.0):
        self.calls.append((method, url, dict(data or {})))
        if url == github_auth.API_URL + "/user":
            return 200, {"X-OAuth-Scopes": "repo, read:org"}, {"login": "octo", "id": 7}
        return self.replies.pop(0)


@pytest.fixture
def flow(monkeypatch):
    monkeypatch.setenv(github_auth.CLIENT_ID_ENV, "Iv1.client")
    github_auth.cancel_device_flow()
    stored = []
    monkeypatch.setattr(github_auth, "store_token", stored.append)
    yield stored
    github_auth.cancel_device_flow()


def _start(monkeypatch, *polls):
    fake = FakeGitHub(
        [
            (
                200,
                {},
                {
                    "device_code": "dev-123",
                    "user_code": "ABCD-1234",
                    "verification_uri": "https://github.com/login/device",
                    "expires_in": 900,
                    "interval": 5,
                },
            ),
            *polls,
        ]
    )
    monkeypatch.setattr(github_auth, "_http", fake)
    out = github_auth.start_device_flow()
    assert out["ok"] and out["user_code"] == "ABCD-1234"
    assert "device_code" not in out  # never handed to the client
    return fake


def test_device_flow_waits_then_stores_the_token(monkeypatch, flow):
    fake = _start(
        monkeypatch,
        (200, {}, {"error": "authorization_pending"}),
        (200, {}, {"access_token": "gho_tok", "token_type": "bearer"}),
    )
    t0 = github_auth._FLOW["next_poll"]
    # Too early: no request at all.
    assert github_auth.poll_device_flow(now=t0 - 1)["state"] == "pending"
    assert len(fake.calls) == 1
    assert github_auth.poll_device_flow(now=t0)["state"] == "pending"
    assert github_auth.poll_device_flow(now=t0 + 2)["state"] == "pending"  # interval
    done = github_auth.poll_device_flow(now=t0 + 5)
    assert done["state"] == "done" and done["login"] == "octo"
    assert flow == ["gho_tok"]
    grant = fake.calls[2][2]
    assert grant["device_code"] == "dev-123"
    assert grant["grant_type"].endswith("device_code")


def test_device_flow_slow_down_backs_off(monkeypatch, flow):
    _start(monkeypatch, (200, {}, {"error": "slow_down", "interval": 10}))
    t0 = github_auth._FLOW["next_poll"]
    assert github_auth.poll_device_flow(now=t0)["interval"] == 10
    assert github_auth._FLOW["next_poll"] == t0 + 10


@pytest.mark.parametrize(
    "reply,state",
    [
        ({"error": "expired_token"}, "expired"),
        ({"error": "access_denied"}, "denied"),
        ({"error": "unsupported_grant_type", "error_description": "nope"}, "error"),
    ],
)
def test_device_flow_ends(monkeypatch, flow, reply, state):
    _start(monkeypatch, (200, {}, reply))
    out = github_auth.poll_device_flow(now=github_auth._FLOW["next_poll"])
    assert out["state"] == state and out["error"]
    assert flow == []
    # Ended: no further requests.
    assert github_auth.poll_device_flow(now=10**12)["state"] == state


def test_device_flow_expires_without_asking(monkeypatch, flow):
    fake = _start(monkeypatch)
    out = github_auth.poll_device_flow(now=github_auth._FLOW["expires_at"] + 1)
    assert out["state"] == "expired" and len(fake.calls) == 1


def test_no_client_id_no_device_flow(monkeypatch):
    monkeypatch.delenv(github_auth.CLIENT_ID_ENV, raising=False)
    out = github_auth.start_device_flow()
    assert not out["ok"] and "OAuth App" in out["error"]


def test_paste_token_refuses_what_github_rejects(monkeypatch):
    stored = []
    monkeypatch.setattr(github_auth, "store_token", stored.append)
    monkeypatch.setattr(
        github_auth, "_http", lambda *a, **k: (401, {}, {"message": "Bad"})
    )
    out = github_auth.paste_token("ghp_bad")
    assert not out["ok"] and stored == []
    monkeypatch.setattr(
        github_auth, "_http", lambda *a, **k: (200, {}, {"login": "octo", "id": 1})
    )
    github_auth._USERS.clear()
    assert github_auth.paste_token("ghp_good")["login"] == "octo"
    assert stored == ["ghp_good"]


def test_store_token_lands_in_github_token():
    from backend.config import settings as store

    github_auth.store_token("ghp_saved")
    store.invalidate()
    assert store.load_settings().github.token == "ghp_saved"


def test_credential_helper_answers_github_only(monkeypatch):
    monkeypatch.setattr(github_auth, "resolve_token", lambda: ("settings", "tok"))
    assert github_auth.credential_answer("protocol=https\nhost=github.com\n") == (
        "username=x-access-token\npassword=tok\n"
    )
    assert github_auth.credential_answer("protocol=https\nhost=gitlab.com\n") == ""
    assert github_auth.credential_answer("protocol=http\nhost=github.com\n") == ""


def test_identity_is_written_to_global_git_config_only_on_request(
    tmp_path, monkeypatch
):
    cfg = tmp_path / "gitconfig"
    cfg.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(cfg))
    assert github_auth.git_identity() == {"name": "", "email": ""}
    assert not github_auth.set_git_identity("A", "not-an-email")["ok"]
    out = github_auth.set_git_identity("Octo Cat", "7+octo@users.noreply.github.com")
    assert out["ok"] and out["name"] == "Octo Cat"
    assert "7+octo@users.noreply.github.com" in cfg.read_text()
    assert github_auth.suggested_identity({"login": "octo", "id": 7}) == {
        "name": "octo",
        "email": "7+octo@users.noreply.github.com",
    }


def test_push_check_names_a_missing_https_credential(tmp_path):
    repo = tmp_path / "r"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "remote",
            "add",
            "origin",
            str(tmp_path / "nowhere.git"),
        ],
        check=True,
    )
    out = github_auth.push_check(str(repo))
    assert out["ok"] is False and out["remote"]
    assert github_auth.push_check(str(tmp_path / "missing"))["ok"] is None


# --------------------------------------------------------------------------- #
# push failures
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "text,rid",
    [
        (
            "fatal: could not read Username for 'https://github.com': terminal prompts disabled",
            "https_auth",
        ),
        (
            "git@github.com: Permission denied (publickey).\nfatal: Could not read from remote",
            "ssh_auth",
        ),
        ("*** Please tell me who you are.", "identity"),
        (
            "remote: Permission to a/b.git denied to x.\nfatal: unable to access",
            "forbidden",
        ),
    ],
)
def test_classify_push_failures(text, rid):
    assert git_auth_hints.classify(text)["id"] == rid


def test_a_rejected_push_is_not_an_auth_problem():
    assert (
        git_auth_hints.classify("! [rejected] main -> main (non-fast-forward)") is None
    )


def test_push_watcher_reads_the_log_and_announces_once(tmp_path, monkeypatch):
    from backend.web.core import events, live_stage

    log = tmp_path / "mindflock_push.log"
    log.write_text(
        "fatal: could not read Username for 'https://github.com': terminal prompts disabled\n"
    )
    seen = []
    monkeypatch.setattr(events.BUS, "emit", lambda name, **kw: seen.append((name, kw)))
    rec = {"wt": str(tmp_path), "push_log": str(log), "failed_emitted": False}
    hint = live_stage._push_failure(rec)
    assert hint["id"] == "https_auth" and "could not read Username" in hint["detail"]
    live_stage._announce_push_failed("s1", rec, hint)
    live_stage._announce_push_failed("s1", rec, hint)
    assert [n for n, _ in seen] == ["session.push_failed"]
    assert seen[0][1]["data"]["reason"] == "https_auth"


def test_push_command_turns_prompts_off(monkeypatch, tmp_path):
    from backend.web.core import live_stage

    monkeypatch.setattr(live_stage, "push_log_path", lambda wt: str(tmp_path / "p.log"))
    cmd = live_stage.push_command("/wt")
    assert cmd.startswith("GIT_TERMINAL_PROMPT=0 git push --no-verify -u origin HEAD")
    assert "| tee " in cmd
    # The MCP ship tool still finds the push in the shell tail.
    from backend.mcp import ship

    assert ship.PUSH_COMMAND in cmd


# --------------------------------------------------------------------------- #
# a new computer in one line
# --------------------------------------------------------------------------- #
def test_bootstrap_line_pins_this_version():
    inv = {"device": "laptop", "code": "ABCD-EFGH", "expires_at": 1.0}
    b = bootstrap.lines(inv, version="0.7.4")
    assert b["ref"] == "v0.7.4" and b["pinned"]
    assert (
        "MINDFLOCK_INSTALL_REF=v0.7.4 sh -s -- --join 'laptop ABCD-EFGH'" in b["line"]
    )
    assert "/v0.7.4/install.sh" in b["line"]
    assert b["desktop"]["paste"] == "laptop ABCD-EFGH"
    assert b["desktop"]["download"].endswith("/releases/tag/v0.7.4")
    # The live default: THIS build's version.
    assert bootstrap.lines(inv)["ref"] == bootstrap.install_ref(__version__)
    assert bootstrap.install_ref("0+unknown") == "main"


def test_bootstrap_route_makes_an_invite(client, monkeypatch):
    from backend.web.core import fleet

    client.state["privileged"] = True
    monkeypatch.setattr(
        fleet,
        "create_invite",
        lambda: {"device": "rig", "code": "WXYZ-2345", "expires_at": 9.0},
    )
    monkeypatch.setattr(fleet, "invites", lambda: [])
    r = client.post("/api/fleet/bootstrap", json={})
    assert r.status_code == 200
    assert r.json()["line"].endswith("--join 'rig WXYZ-2345'")
    assert r.json()["ref"] == bootstrap.install_ref(__version__)


def test_install_sh_takes_join_and_stays_posix():
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    script = os.path.join(root, "install.sh")
    for sh in ("sh", "dash"):
        if subprocess.run(["which", sh], capture_output=True).returncode == 0:
            assert subprocess.run([sh, "-n", script]).returncode == 0
    text = open(script, encoding="utf-8").read()
    assert '"$MF" devices bootstrap --join "$JOIN"' in text
    # Re-running the installer restarts a running server and stops there —
    # except with --join, which still has to join afterwards.
    restart = text[text.index("# --- 5. a server already running here") :]
    restart = restart[: restart.index("# --- 6. join")]
    assert 'if [ -z "$JOIN" ]; then' in restart and "exit 0" in restart
    assert "UV_INSTALLER_SHA256=" in text  # the pinned bits stay


# --------------------------------------------------------------------------- #
# readiness
# --------------------------------------------------------------------------- #
def test_readiness_summary():
    r = readiness._summary_from(
        [
            _check("tmux", "fail", label="tmux"),
            _check("agent-auth", "warn", provider="claude"),
        ],
        deferred=["codex"],
        push={"ok": False, "fix": "Connect GitHub"},
        ts={
            "backend_state": "Running",
            "key_expiry": {"days": 3, "expired": False, "warn": True},
        },
        version="0.7.4",
    )
    assert not r["ready"] and r["missing"] == ["tmux"]
    assert r["agent"] == {"provider": "claude", "signed_in": False}
    assert r["tailscale"]["key_expiry_days"] == 3
    assert len(r["fixes"]) == 5
    ok = readiness._summary_from(
        [_check("agent-auth", "ok")], deferred=[], push=None, ts={}, version="1"
    )
    assert ok["ready"] and ok["fixes"] == []


# --------------------------------------------------------------------------- #
# uninstall leaves your devices
# --------------------------------------------------------------------------- #
@pytest.fixture
def in_a_fleet(monkeypatch):
    from backend.web.core import fleet

    monkeypatch.setattr(fleet, "in_fleet", lambda: True)
    monkeypatch.setattr(fleet, "_self_key", lambda: "laptop")
    monkeypatch.setattr(
        fleet,
        "live_members",
        lambda: {
            "laptop": {"host": "Laptop"},
            "rig": {"host": "Rig"},
            "mini": {"host": "Mini"},
        },
    )


def test_uninstall_leaves_first(in_a_fleet, monkeypatch):
    calls = []

    async def leave():
        calls.append("leave")
        return ["rig", "mini"]

    monkeypatch.setattr(uninstall, "_leave_async", leave)
    out = uninstall.leave_devices()
    assert calls == ["leave"] and out["state"] == "left" and out["missed"] == []


def test_uninstall_reports_who_missed_it(in_a_fleet, monkeypatch):
    async def leave():
        return ["rig"]

    monkeypatch.setattr(uninstall, "_leave_async", leave)
    out = uninstall.leave_devices()
    assert out["state"] == "partial" and out["missed"] == ["Mini"]


def test_uninstall_leave_failure_still_forgets_locally(in_a_fleet, monkeypatch):
    from backend.web.core import fleet, settings_sync

    async def leave():
        raise OSError("tailscale down")

    forgot = []
    monkeypatch.setattr(uninstall, "_leave_async", leave)
    monkeypatch.setattr(fleet, "leave", lambda: forgot.append("fleet"))
    monkeypatch.setattr(settings_sync, "disable", lambda: forgot.append("sync"))
    out = uninstall.leave_devices()
    assert out["state"] == "failed" and "tailscale down" in out["error"]
    assert forgot == ["fleet", "sync"]
    assert out["missed"] == ["Mini", "Rig"]


def test_uninstall_dry_run_only_says_so(in_a_fleet, monkeypatch):
    async def leave():  # pragma: no cover — must not run
        raise AssertionError

    monkeypatch.setattr(uninstall, "_leave_async", leave)
    assert uninstall.leave_devices(dry_run=True)["state"] == "would"


def test_cli_uninstall_purge_warns_when_leaving_fails(
    in_a_fleet, monkeypatch, capsys, tmp_path
):
    from backend import cli

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(uninstall, "server_is_running", lambda *a: False)
    monkeypatch.setattr(
        uninstall,
        "leave_devices",
        lambda dry_run=False: {
            "state": "failed",
            "others": ["Rig"],
            "missed": ["Rig"],
            "self": "laptop",
            "error": "offline",
        },
    )
    rc = cli.main(["uninstall", "--purge", "--yes"])
    err = capsys.readouterr().err
    assert "warning: couldn't tell Rig" in err
    assert "mindflock devices remove laptop" in err
    assert rc in (0, 1)


def test_not_in_a_fleet_nothing_to_leave(monkeypatch):
    from backend.web.core import fleet

    monkeypatch.setattr(fleet, "in_fleet", lambda: False)
    assert uninstall.leave_devices()["state"] == "none"


def test_leave_async_shuts_the_session(monkeypatch):
    from backend.web.core import fleet, remote

    seen = []

    async def discover():
        seen.append("discover")

    async def leave_fleet():
        seen.append("leave")
        return {"ok": True}

    async def shutdown():
        seen.append("shutdown")

    monkeypatch.setattr(remote, "discover_now", discover)
    monkeypatch.setattr(fleet, "leave_fleet", leave_fleet)
    monkeypatch.setattr(remote, "shutdown", shutdown)
    monkeypatch.setattr(fleet, "_visible_members", lambda: [{"key": "rig"}])
    assert asyncio.run(uninstall._leave_async()) == ["rig"]
    assert seen == ["discover", "leave", "shutdown"]


# --------------------------------------------------------------------------- #
# the CLI halves of the bootstrap
# --------------------------------------------------------------------------- #
def test_cli_bootstrap_prints_the_line(monkeypatch, capsys):
    from backend import cli, client

    monkeypatch.setattr(client, "discover", lambda h, p: "http://x")
    monkeypatch.setattr(
        client,
        "post",
        lambda base, path, body=None, timeout=0: bootstrap.lines(
            {"device": "laptop", "code": "ABCD-EFGH", "expires_at": 0}, version="0.7.4"
        ),
    )
    assert cli.main(["devices", "bootstrap"]) == 0
    out = capsys.readouterr()
    assert out.out.strip().endswith("--join 'laptop ABCD-EFGH'")
    assert "pinned to MindFlock v0.7.4" in out.err and "laptop ABCD-EFGH" in out.err


def test_cli_bootstrap_join_falls_back_to_asking(monkeypatch, capsys):
    from backend import cli, client

    monkeypatch.setattr(client, "discover", lambda h, p: "http://x")
    calls = []

    def get(base, path, timeout=0):
        calls.append(("GET", path))
        if path.startswith("/api/tailscale/health"):
            return {"installed": True, "backend_state": "Running"}
        return {}

    def post(base, path, body=None, timeout=0):
        calls.append(("POST", path, body))
        if path == "/api/fleet/join":
            raise client.ApiError(400, "that code is wrong or expired")
        if path == "/api/fleet/request":
            return {"state": "joined", "host": "Laptop"}
        return {}

    monkeypatch.setattr(client, "get", get)
    monkeypatch.setattr(client, "post", post)
    assert cli.main(["devices", "bootstrap", "--join", "laptop ABCD-EFGH"]) == 0
    posts = [c[1] for c in calls if c[0] == "POST"]
    assert posts == ["/api/devices/refresh", "/api/fleet/join", "/api/fleet/request"]
    join = next(c for c in calls if c[1] == "/api/fleet/join")
    assert join[2] == {"device": "laptop", "code": "ABCD-EFGH"}
    assert "expired while this computer was being set up" in capsys.readouterr().out


def test_cli_bootstrap_join_stops_without_tailscale(monkeypatch, capsys):
    from backend import cli, client

    monkeypatch.setattr(client, "discover", lambda h, p: "http://x")
    monkeypatch.setattr(
        client,
        "get",
        lambda base, path, timeout=0: {
            "installed": False,
            "issues": [{"message": "Tailscale isn't installed.", "fix": "curl … | sh"}],
        },
    )
    monkeypatch.setattr(client, "post", lambda *a, **k: pytest.fail("must not join"))
    assert cli.main(["devices", "bootstrap", "--join", "laptop ABCD-EFGH"]) == 1
    assert "run the same line again" in capsys.readouterr().out


def test_bootstrap_join_makes_the_new_computer_reachable_first(monkeypatch, capsys):
    """A fresh install listens on 127.0.0.1: before joining it saves what Make
    reachable saves (tailnet bind AND the gate, together)."""
    from backend import cli, client

    monkeypatch.setattr(client, "discover", lambda h, p: "http://x")
    order = []

    def get(base, path, timeout=0):
        if path.startswith("/api/tailscale/health"):
            return {"installed": True, "backend_state": "Running"}
        if path == "/api/fleet":
            return {"self_reachable": False}
        return {}

    def post(base, path, body=None, timeout=0):
        order.append((path, body))
        if path == "/api/fleet/join":
            return {"state": "joined", "host": "Laptop"}
        return {}

    monkeypatch.setattr(client, "get", get)
    monkeypatch.setattr(client, "post", post)
    assert cli.main(["devices", "bootstrap", "--join", "laptop ABCD-EFGH"]) == 0
    assert order[0] == (
        "/api/settings",
        {"general": {"serve_mode": "tailscale", "auth_mode": "on"}},
    )
    assert [p for p, _ in order].index("/api/settings") < [p for p, _ in order].index(
        "/api/fleet/join"
    )
    assert "mindflock token" in capsys.readouterr().out


def test_the_desktop_paste_goes_through_the_paste_a_code_router():
    from backend.web.core import fleet

    paste = bootstrap.lines({"device": "laptop", "code": "ABCD-EFGH"}, version="0.7.4")[
        "desktop"
    ]["paste"]
    assert fleet.classify_code(paste) == {
        "kind": "device",
        "code": "ABCD-EFGH",
        "device": "laptop",
    }


def test_setup_choice_saves_from_this_machine_on_the_real_app(tmp_path, monkeypatch):
    """Setup writes general.setup_devices through its own privileged route
    (not POST /api/settings, whose allow-list refuses remote callers): from
    loopback it saves; an anonymous tailnet caller of an exposed gate-off
    server is refused."""
    from backend.config import settings as S
    from backend.web.core import tailnet_trust
    from backend.web.server import app

    async def _no(scope):
        return False

    monkeypatch.setattr(tailnet_trust, "request_trusted", _no)
    monkeypatch.setattr(onboarding, "collect", lambda **kw: {})
    with TestClient(app):
        monkeypatch.setenv("CS_WEB_MODE", "tailscale")
        monkeypatch.setenv("MINDFLOCK_AUTH", "0")
        remote = TestClient(app, client=("100.64.0.5", 41000))
        r = remote.post("/api/onboarding/choice", json={"choice": "join"})
        assert r.status_code == 403
        local = TestClient(app, client=("127.0.0.1", 41000))
        r = local.post("/api/onboarding/choice", json={"choice": "join"})
        assert r.status_code == 200, r.text
    S.invalidate()
    assert S.load_settings().general.setup_devices == "join"
