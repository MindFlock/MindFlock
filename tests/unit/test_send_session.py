"""Send a session to another of your devices.

* ``POST /api/instances/{title}/send-to-device`` (core.send_session) against
  real throwaway repos: a bare ``origin``, this device's worktree on a
  feature branch, and a clone standing in for the OTHER device's checkout.
  The other device itself is a stub of ``remote.get_json`` / ``post_json``
  that answers ``GET /api/repos/find`` with the real
  :func:`repo_picker.find_repos_by_url` over that clone, and ``POST
  /api/instances`` by running the real ``shared_branch.prepare_continue`` on
  it — so "the device can start ON the pushed branch" is proven by git, with
  no second server and no network.
* ``GET /api/repos/find`` on this server.
* The MCP tools ``send_session`` / ``list_devices`` (thin calls + the scope
  rule) against the in-memory API.

Every test swaps ``ENGINE.instances`` for a private dict and never launches
anything.
"""

from __future__ import annotations

import builtins
import os
import subprocess

import pytest
from fastapi.testclient import TestClient

from backend import client as api_client
from backend.mcp.protocol import ToolError
from backend.session.storage import Status
from backend.web import server
from backend.web.core import remote
from backend.web.core import repo_picker
from backend.web.core import send_session
from backend.web.core import shared_branch
from tests.unit._mcp_fakes import FakeCtx, make_box, row

client = TestClient(server.app)

TITLE = "tripwire"
BRANCH = "me/tripwire"


def _git(cwd, *args):
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _commit(repo, name, text, msg):
    (repo / name).write_text(text)
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", msg)


class _Inst:
    def __init__(self, wt, task="", program="claude"):
        self.Title = TITLE
        self.Program = program
        self.Branch = BRANCH
        self.BaseBranch = "main"
        self.InPlace = False
        self.Path = wt
        self.Status = Status.Running
        self.Task = task
        self.PeerShare = ""
        self._wt = wt

    def Started(self):  # noqa: N802
        return True

    def GetWorktreePath(self):  # noqa: N802
        return self._wt


@pytest.fixture
def world(tmp_path, monkeypatch):
    """origin (bare) ← here (the session's worktree, on BRANCH with two
    commits past main) and there (the other device's checkout: main only)."""
    monkeypatch.delenv("CS_WEB_MODE", raising=False)
    monkeypatch.setenv("MINDFLOCK_AUTH", "0")
    for k, v in (
        ("GIT_AUTHOR_NAME", "Ada"),
        ("GIT_AUTHOR_EMAIL", "ada@example.invalid"),
        ("GIT_COMMITTER_NAME", "Ada"),
        ("GIT_COMMITTER_EMAIL", "ada@example.invalid"),
    ):
        monkeypatch.setenv(k, v)
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    here = tmp_path / "here"
    here.mkdir()
    _git(here, "init", "-q", "-b", "main")
    _commit(here, "README.md", "hi\n", "init")
    _git(here, "remote", "add", "origin", str(origin))
    _git(here, "push", "-q", "-u", "origin", "main")
    _git(here, "checkout", "-q", "-b", BRANCH)
    _commit(here, "tripwire.py", "print('armed')\n", "Add the tripwire daemon")
    _commit(here, "tripwire.service", "[Unit]\n", "Add a systemd unit")
    there = tmp_path / "there"
    _git(tmp_path, "clone", "-q", str(origin), str(there))

    instances: dict = {}
    monkeypatch.setattr(server.ENGINE, "instances", instances)
    monkeypatch.setattr(server.ENGINE, "save", lambda **kw: None)
    task = (
        "Build tripwire, a tiny file-watch daemon."
        + '\n\n---\nYou are MindFlock worker session "tripwire", spawned by x.'
    )
    inst = _Inst(str(here), task=task)
    instances[TITLE] = inst
    monkeypatch.setattr(
        server,
        "_agent_output",
        lambda i, t, view: (
            "last_reply",
            "Done: tripwire runs under systemd.",
            False,
            None,
            200,
        ),
    )
    monkeypatch.setattr(server, "_red_zone_breaches_for", lambda i, wt, target: [])

    # The other device: linked, answering, remote control on.
    monkeypatch.setattr(
        remote,
        "_SELF",
        {"key": "linux-box", "host": "linux-box", "os": "linux", "dns": "", "ip": ""},
    )
    monkeypatch.setattr(remote, "_DEVICES", {})
    dev = remote._device_state("mac")
    dev.update(
        host="Macs-Mac-mini",
        os="macOS",
        base_url="http://mac.invalid:8765",
        reachable=True,
        remote_control=True,
        auth=True,
    )
    monkeypatch.setattr(remote, "token_for", lambda key: "tok" if key == "mac" else "")
    monkeypatch.setattr(remote, "_fleet_key_for", lambda key: "")

    fake = _FakeDevice(there)
    monkeypatch.setattr(remote, "get_json", fake.get_json)
    monkeypatch.setattr(remote, "post_json", fake.post_json)

    async def _no_refresh(key):
        fake.refreshed.append(key)

    monkeypatch.setattr(remote, "refresh_instances", _no_refresh)
    return {"here": here, "there": there, "origin": origin, "inst": inst, "dev": fake}


class _FakeDevice:
    """The other device's MindFlock, answering only what a send asks."""

    def __init__(self, there):
        self.there = there
        self.calls: list = []
        self.created: list = []
        self.refreshed: list = []
        self.offline = False
        self.find_status = 200
        self.taken: set = set()

    async def get_json(self, dev, path, timeout=3.0, **kw):
        self.calls.append(("GET", path))
        if self.offline:
            return 0, None
        if path.startswith("/api/repos/find?"):
            if self.find_status != 200:
                return self.find_status, None
            from urllib.parse import parse_qs, urlsplit

            url = parse_qs(urlsplit(path).query)["url"][0]
            return 200, repo_picker.find_repos_by_url(
                url, recent_paths=[str(self.there)]
            )
        if path == "/api/repos/suggest":
            return 200, {
                "suggestions": [
                    {"path": "/elsewhere/other", "name": "other", "is_git": True},
                    {"path": str(self.there), "name": "origin", "is_git": True},
                ]
            }
        return 404, None

    async def post_json(self, dev, path, body, timeout=10.0, **kw):
        self.calls.append(("POST", path))
        if self.offline:
            return 0, None
        assert path == "/api/instances"
        if body["title"] in self.taken:
            return 409, {"error": "instance %s already exists" % body["title"]}
        status, remote_name, err = shared_branch.prepare_continue(
            body["repo_path"], body["base_ref"], body["continue_branch"]
        )
        if status:
            return status, {"error": err}
        self.created.append(body)
        return 202, {"title": body["title"], "status": "loading"}


def _send(device="mac", note="", **extra):
    return client.post(
        "/api/instances/%s/send-to-device" % TITLE,
        json=dict({"device": device, "note": note}, **extra),
    )


def _on_origin(world, branch=BRANCH):
    out = subprocess.run(
        ["git", "-C", str(world["origin"]), "rev-parse", "--verify", "--quiet", branch],
        capture_output=True,
        text=True,
    )
    return out.stdout.strip()


# --------------------------------------------------------------------------- #
# the route
# --------------------------------------------------------------------------- #
class TestSendRoute:
    def test_happy_path_pushes_starts_on_the_branch_and_briefs(self, world):
        r = _send(note="install this here as a launchd service")
        assert r.status_code == 200, r.text
        out = r.json()
        head = _git(world["here"], "rev-parse", "HEAD")
        assert out["title"] == "mac::" + TITLE
        assert out["device"] == "mac" and out["remote_title"] == TITLE
        assert out["branch"] == BRANCH and out["pushed"] is True
        assert out["commits"] == 2
        assert out["repo_path"] == os.path.realpath(world["there"])
        assert out["caveats"] == []
        # The branch is on origin at HEAD, and the other device's checkout
        # was made ready ON it (prepare_continue ran there for real).
        assert _on_origin(world) == head
        assert _git(world["there"], "rev-parse", BRANCH) == head
        # Exactly these keys went to the device: git coordinates + a brief.
        (body,) = world["dev"].created
        assert set(body) == {
            "title",
            "repo_path",
            "base_ref",
            "continue_branch",
            "prompt",
            "spawned",
            "program",
        }
        assert body["base_ref"] == "origin/" + BRANCH
        assert body["continue_branch"] == BRANCH
        assert body["program"] == "claude" and body["spawned"] is True
        assert world["dev"].refreshed == ["mac"]

    def test_brief_contents(self, world):
        assert _send(note="install this here as a launchd service").status_code == 200
        brief = world["dev"].created[0]["prompt"]
        assert brief.startswith(
            '[Handoff from MindFlock: session "tripwire" on device "linux-box"'
        )
        assert "not typed by your user" in brief
        assert "Note from the sender:\ninstall this here as a launchd service" in brief
        assert "Build tripwire, a tiny file-watch daemon." in brief
        assert "You are MindFlock worker session" not in brief  # footer dropped
        assert "2 commits on the branch since it left main" in brief
        assert "Add the tripwire daemon" in brief and "Add a systemd unit" in brief
        assert "tripwire.service" in brief  # the diffstat
        assert "Done: tripwire runs under systemd." in brief
        assert "no chat history, no logins" in brief
        assert len(brief) <= send_session.BRIEF_MAX

    def test_never_reads_or_sends_credentials_or_transcripts(
        self, world, tmp_path, monkeypatch
    ):
        home = tmp_path / "home"
        for rel in (
            ".claude/.credentials.json",
            ".claude/projects/x/abc.jsonl",
            ".codex/auth.json",
        ):
            p = home / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("SECRET-TOKEN")
        monkeypatch.setenv("HOME", str(home))
        opened = []
        real_open = builtins.open

        def spy(file, *a, **k):
            opened.append(str(file))
            return real_open(file, *a, **k)

        monkeypatch.setattr(builtins, "open", spy)
        r = _send()
        monkeypatch.setattr(builtins, "open", real_open)
        assert r.status_code == 200, r.text
        assert not [p for p in opened if p.startswith(str(home))]
        sent = repr(world["dev"].created)
        assert "SECRET-TOKEN" not in sent
        assert ".credentials" not in sent and ".jsonl" not in sent
        assert str(home) not in sent

    def test_already_pushed_is_not_pushed_again(self, world):
        _git(world["here"], "push", "-q", "-u", "origin", BRANCH)
        out = _send().json()
        assert out["pushed"] is False and out["already_pushed"] is True

    def test_uncommitted_work_is_refused_and_nothing_moves(self, world):
        (world["here"] / "tripwire.py").write_text("print('changed')\n")
        (world["here"] / "notes.txt").write_text("new\n")
        r = _send()
        assert r.status_code == 409
        body = r.json()
        assert body["code"] == "uncommitted"
        assert "tripwire.py" in body["error"] and "notes.txt" in body["error"]
        assert "Commit them first" in body["error"]
        assert "nothing was pushed" in body["error"]
        assert _on_origin(world) == ""
        assert world["dev"].calls == [] and world["dev"].created == []

    def test_device_asleep_is_refused_before_any_push(self, world):
        remote._DEVICES["mac"]["reachable"] = False
        r = _send()
        assert r.status_code == 409
        assert r.json()["code"] == "device_offline"
        assert "wake it" in r.json()["error"]
        assert _on_origin(world) == "" and world["dev"].calls == []

    def test_device_not_answering_the_lookup(self, world):
        world["dev"].offline = True
        r = _send()
        assert r.status_code == 409 and r.json()["code"] == "device_offline"
        assert _on_origin(world) == ""

    def test_remote_control_off(self, world):
        remote._DEVICES["mac"]["remote_control"] = False
        r = _send()
        assert r.status_code == 409 and r.json()["code"] == "remote_control_off"

    def test_a_device_that_is_not_yours_is_refused(self, world, monkeypatch):
        # Gate off, answering, remote control on: drivable — but not linked.
        monkeypatch.setattr(remote, "token_for", lambda key: "")
        remote._DEVICES["mac"]["auth"] = False
        r = _send()
        assert r.status_code == 409 and r.json()["code"] == "not_linked"
        assert "Settings → Your devices" in r.json()["error"]
        assert _on_origin(world) == "" and world["dev"].calls == []

    def test_unknown_device_names_the_known_ones(self, world):
        r = _send(device="laptop")
        assert r.status_code == 404
        assert r.json()["code"] == "no_device" and "mac" in r.json()["error"]

    def test_device_found_by_host_name(self, world):
        assert _send(device="macs-mac-mini").status_code == 200

    def test_repo_missing_on_the_device(self, world):
        # via=git: no bundle fallback (that path: tests/unit/test_send_files.py).
        world["dev"].there = world["here"].parent / "nowhere"
        r = _send(via="git")
        assert r.status_code == 409
        body = r.json()
        assert body["code"] == "repo_missing"
        assert "git clone %s" % world["origin"] in body["error"]
        assert body["url"] == str(world["origin"])
        assert _on_origin(world) == "" and world["dev"].created == []

    def test_an_older_device_is_matched_by_folder_name(self, world):
        world["dev"].find_status = 404
        r = _send()
        assert r.status_code == 200, r.text
        assert r.json()["repo_path"] == str(world["there"])
        assert "matched by folder name" in r.json()["caveats"][0]

    def test_a_title_taken_there_steps_to_dash_two(self, world):
        world["dev"].taken = {TITLE}
        out = _send().json()
        assert out["title"] == "mac::%s-2" % TITLE
        assert any("already had a session" in c for c in out["caveats"])

    def test_no_remote(self, world):
        _git(world["here"], "remote", "remove", "origin")
        r = _send(via="git")
        assert r.status_code == 400 and r.json()["code"] == "no_remote"
        assert "git remote add origin" in r.json()["error"]

    def test_a_branch_that_moved_on_is_refused_never_forced(self, world, tmp_path):
        _git(world["here"], "push", "-q", "-u", "origin", BRANCH)
        other = tmp_path / "other"
        _git(tmp_path, "clone", "-q", "-b", BRANCH, str(world["origin"]), str(other))
        _commit(other, "x.txt", "x\n", "someone else")
        _git(other, "push", "-q")
        theirs = _git(other, "rev-parse", "HEAD")
        r = _send()
        assert r.status_code == 409 and r.json()["code"] == "diverged"
        assert "git pull --rebase origin %s" % BRANCH in r.json()["error"]
        assert _on_origin(world) == theirs  # untouched
        assert world["dev"].created == []

    def test_red_zone_breach_is_refused(self, world, monkeypatch):
        monkeypatch.setattr(
            server,
            "_red_zone_breaches_for",
            lambda i, wt, target: [{"path": "secrets/x", "pattern": "secrets/**"}],
        )
        r = _send()
        assert r.status_code == 409 and r.json()["code"] == "red_zone"
        assert _on_origin(world) == ""

    def test_unknown_session_is_404(self, world):
        r = client.post("/api/instances/nope/send-to-device", json={"device": "mac"})
        assert r.status_code == 404

    def test_a_relayed_request_is_refused(self, world):
        r = client.post(
            "/api/instances/%s/send-to-device" % TITLE,
            json={"device": "mac"},
            headers={remote.REMOTE_HEADER: "elsewhere"},
        )
        assert r.status_code in (400, 403)
        assert world["dev"].calls == []

    def test_device_must_be_a_string(self, world):
        r = client.post("/api/instances/%s/send-to-device" % TITLE, json={"device": 3})
        assert r.status_code == 400


class TestRepoFind:
    def test_matches_across_transports_and_names_the_remote(self, tmp_path):
        repo = tmp_path / "app"
        repo.mkdir()
        _git(repo, "init", "-q")
        _git(repo, "remote", "add", "upstream", "git@github.com:Acme/app.git")
        found = repo_picker.find_repos_by_url(
            "https://github.com/Acme/app", recent_paths=[str(repo)]
        )
        assert found["matches"] == [
            {"path": os.path.realpath(repo), "name": "app", "remote": "upstream"}
        ]
        other = repo_picker.find_repos_by_url(
            "https://github.com/Acme/else", recent_paths=[str(repo)]
        )
        assert other["matches"] == []

    def test_route(self, tmp_path, monkeypatch):
        repo = tmp_path / "app"
        repo.mkdir()
        _git(repo, "init", "-q")
        _git(repo, "remote", "add", "origin", "https://github.com/Acme/app.git")
        monkeypatch.setattr(server, "_recent_repo_paths", lambda: [str(repo)])
        monkeypatch.setattr(server, "_repo_url_workdirs", lambda url: [])
        r = client.get("/api/repos/find", params={"url": "git@github.com:Acme/app.git"})
        assert r.status_code == 200
        assert r.json()["matches"][0]["path"] == os.path.realpath(repo)
        assert client.get("/api/repos/find").status_code == 400


# --------------------------------------------------------------------------- #
# the MCP tools
# --------------------------------------------------------------------------- #
def _tree():
    return [
        row("root"),
        row("orch", parent="root"),
        row("w1", parent="orch", spawned=True),
        row("other"),
        row("mac::far"),
    ]


def _send_route(api):
    seen = []

    def route(title, payload, q):
        seen.append((title, payload))
        return {
            "ok": True,
            "title": "mac::" + title,
            "device": "mac",
            "branch": "b",
            "pushed": True,
            "already_pushed": False,
            "commits": 2,
            "repo_path": "/Users/x/app",
            "queued": False,
            "caveats": [],
        }

    api.routes[("POST", "send-to-device")] = route
    return seen


class TestMcpSendSession:
    def test_sends_itself_by_default(self):
        box, api, clock = make_box(_tree(), me="orch")
        seen = _send_route(api)
        out = box.send_session({"device": "mac", "note": "launchd"}, FakeCtx(clock))
        assert seen == [("orch", {"device": "mac", "note": "launchd"})]
        assert (
            out["title"] == "mac::orch" and out["ok"] is True and out["sent"] == "orch"
        )

    def test_a_descendant_may_be_sent(self):
        box, api, clock = make_box(_tree(), me="orch")
        seen = _send_route(api)
        box.send_session({"device": "mac", "title": "w1"}, FakeCtx(clock))
        assert seen[0][0] == "w1"

    def test_someone_elses_session_is_refused(self):
        box, api, clock = make_box(_tree(), me="orch")
        seen = _send_route(api)
        with pytest.raises(ToolError, match="you or one of your descendants"):
            box.send_session({"device": "mac", "title": "other"}, FakeCtx(clock))
        with pytest.raises(ToolError):
            box.send_session({"device": "mac", "title": "root"}, FakeCtx(clock))
        assert seen == []

    def test_a_remote_row_is_refused(self):
        box, api, clock = make_box(_tree(), me="orch")
        with pytest.raises(ToolError, match="send it from there"):
            box.send_session({"device": "x", "title": "mac::far"}, FakeCtx(clock))

    def test_readonly_scope_is_refused(self):
        box, api, clock = make_box(_tree(), me="orch", scope="readonly")
        with pytest.raises(ToolError, match="readonly"):
            box.send_session({"device": "mac"}, FakeCtx(clock))

    def test_a_user_lane_below_push_holds(self):
        rows = _tree()
        rows[1]["lane"] = {"target": "commit", "by": "user"}
        box, api, clock = make_box(rows, me="orch")
        with pytest.raises(ToolError, match="stop at commit"):
            box.send_session({"device": "mac"}, FakeCtx(clock))

    def test_the_servers_sentence_comes_through(self):
        box, api, clock = make_box(_tree(), me="orch")

        def refuse(title, payload, q):
            raise api_client.ApiError(
                409,
                "orch has uncommitted changes that would not travel: a.py",
                {"code": "uncommitted"},
            )

        api.routes[("POST", "send-to-device")] = refuse
        with pytest.raises(ToolError, match="uncommitted changes"):
            box.send_session({"device": "mac"}, FakeCtx(clock))

    def test_an_old_server_says_update(self):
        box, api, clock = make_box(_tree(), me="orch")
        api.route_missing.add("send-to-device")
        with pytest.raises(ToolError, match="update it"):
            box.send_session({"device": "mac"}, FakeCtx(clock))

    def test_list_devices(self):
        box, api, clock = make_box(_tree(), me="orch")
        api.top_routes[("GET", "/api/devices")] = {
            "self": {"device": "linux-box"},
            "devices": [
                {
                    "device": "mac",
                    "os": "macOS",
                    "reachable": True,
                    "connected": True,
                    "member": True,
                    "same_fleet": True,
                    "sessions": 3,
                },
                {
                    "device": "stranger",
                    "reachable": True,
                    "connected": True,
                    "member": False,
                    "same_fleet": False,
                    "has_token": False,
                },
                {
                    "device": "laptop",
                    "reachable": False,
                    "connected": False,
                    "member": True,
                    "same_fleet": True,
                    "error": "timeout",
                },
            ],
        }
        out = box.list_devices({}, FakeCtx(clock))
        assert out["this_device"] == "linux-box"
        by = {d["device"]: d for d in out["devices"]}
        assert by["mac"]["can_send"] is True and by["mac"]["sessions"] == 3
        assert by["stranger"]["can_send"] is False and by["stranger"]["linked"] is False
        assert by["laptop"]["online"] is False and by["laptop"]["error"] == "timeout"


def test_brief_is_capped_and_keeps_the_reply_tail():
    facts = {"branch": "b", "remote": "origin"}
    brief = send_session.build_brief(
        title="t",
        from_device="d",
        facts=facts,
        work={"commits": [], "stat": []},
        task="x" * 5000,
        last_reply="start " + "y" * 5000 + " THE END",
        note="n" * 5000,
    )
    assert len(brief) <= send_session.BRIEF_MAX
    assert "THE END" in brief
    assert "no commits on the branch yet" in brief
