"""Send files (and a session's work as a git bundle) to another of your devices.

* ``POST /api/instances/{title}/send-files`` (core.send_files) and the
  bundle path of ``send-to-device`` (core.send_session + core.session_bundle)
  against real throwaway repos.
* The OTHER device is this same in-process app: ``remote.post_json`` /
  ``get_json`` / ``put_bytes`` are routed to a TestClient with the remote
  marker and a bearer, so the far side runs the REAL ``/api/transfers``
  routes, auth middleware gate and :mod:`core.inbox` — only the device's
  session create and message routes are recorded instead of run (no session
  ever launches). The inbox lives under a scratch ``MINDFLOCK_HOME``.
* Hostile input aimed straight at ``/api/transfers`` (a malicious sender).
* The MCP tool ``send_files`` (thin call + scope rule).
"""

from __future__ import annotations

import builtins
import hashlib
import json
import os
import stat
import subprocess
import time
import urllib.parse

import pytest
from fastapi.testclient import TestClient

from backend.config import settings as settings_mod
from backend.mcp.protocol import ToolError
from backend.session.storage import Status
from backend.web import server
from backend.web.core import auth as auth_mod
from backend.web.core import inbox
from backend.web.core import remote
from backend.web.core import repo_picker
from backend.web.core import send_files
from backend.web.core import session_bundle
from backend.web.core import shared_branch
from tests.unit._mcp_fakes import FakeCtx, make_box, row

client = TestClient(server.app)

TITLE = "tripwire"
BRANCH = "me/tripwire"
TOKEN = "tok-for-mlrig"


def _git(cwd, *args, env=None):
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    ).stdout.strip()


def _commit(repo, name, text, msg, mode=None):
    p = repo / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    if mode is not None:
        os.chmod(p, mode)
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", msg)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class _Inst:
    def __init__(self, wt, title=TITLE, program="claude"):
        self.Title = title
        self.Program = program
        self.Branch = BRANCH
        self.BaseBranch = "main"
        self.InPlace = False
        self.Path = wt
        self.Status = Status.Running
        self.Task = "Build tripwire, a tiny file-watch daemon."
        self.PeerShare = ""
        self._wt = wt

    def Started(self):  # noqa: N802
        return True

    def GetWorktreePath(self):  # noqa: N802
        return self._wt


class _Device:
    """MLRIG2023's MindFlock: the real transfer routes of this app, reached
    the way the device link reaches them; creates and messages recorded."""

    def __init__(self):
        self.created: list = []
        self.messages: list = []
        self.calls: list = []
        self.repo_there = None  # a checkout of the repo there, or None
        self.offline = False
        self.drop_next_put = False
        self.token = TOKEN

    def _headers(self):
        return {
            remote.REMOTE_HEADER: "linux-box",
            "Authorization": "Bearer " + self.token,
        }

    async def get_json(self, dev, path, timeout=3.0, **kw):
        self.calls.append(("GET", path))
        if self.offline:
            return 0, None
        if path.startswith("/api/repos/find?"):
            url = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)["url"][0]
            recent = [str(self.repo_there)] if self.repo_there else []
            return 200, repo_picker.find_repos_by_url(url, recent_paths=recent)
        r = client.get(path, headers=self._headers())
        return r.status_code, (r.json() if r.status_code == 200 else None)

    async def post_json(self, dev, path, body, timeout=10.0, **kw):
        self.calls.append(("POST", path))
        if self.offline:
            return 0, None
        if path == "/api/instances":
            if body.get("continue_branch"):
                st, _rn, err = shared_branch.prepare_continue(
                    body["repo_path"], body["base_ref"], body["continue_branch"]
                )
                if st:
                    return st, {"error": err}
            self.created.append(body)
            return 202, {"title": body["title"], "status": "loading"}
        if path.startswith("/api/instances/") and path.endswith("/messages"):
            self.messages.append((urllib.parse.unquote(path.split("/")[3]), body))
            return 201, {"delivery": "pending"}
        r = client.post(path, json=body, headers=self._headers())
        try:
            data = r.json()
        except ValueError:
            data = None
        return r.status_code, data

    async def put_bytes(self, dev, path, data, timeout=60.0):
        self.calls.append(("PUT", path))
        if self.offline:
            return 0, None
        r = client.put(path, content=data, headers=self._headers())
        if self.drop_next_put:
            # The chunk arrived, the answer was lost: the sender must resume.
            self.drop_next_put = False
            return 0, None
        return r.status_code, r.json()


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.delenv("CS_WEB_MODE", raising=False)
    monkeypatch.setenv("MINDFLOCK_AUTH", "0")
    home = tmp_path / "mfhome"
    home.mkdir()
    monkeypatch.setenv("MINDFLOCK_HOME", str(home))
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
    _commit(here, "README.md", "the big repo\n", "init")
    _commit(here, "lib/shared.py", "X = 1\n", "shared lib")
    _git(here, "remote", "add", "origin", str(origin))
    _git(here, "push", "-q", "-u", "origin", "main")
    _git(here, "checkout", "-q", "-b", BRANCH)
    _commit(
        here, "tripwire/daemon.py", "print('armed')\n", "Add the tripwire daemon", 0o755
    )
    _commit(here, "tripwire/tripwire.service", "[Unit]\n", "Add a systemd unit")

    instances: dict = {}
    monkeypatch.setattr(server.ENGINE, "instances", instances)
    monkeypatch.setattr(server.ENGINE, "save", lambda **kw: None)
    inst = _Inst(str(here))
    instances[TITLE] = inst
    # A session on "the other device" (the same app stands in for it).
    instances["listener"] = _Inst(str(tmp_path / "listener-wt"), title="listener")
    monkeypatch.setattr(
        server,
        "_agent_output",
        lambda i, t, view: ("last_reply", "Done: tripwire is armed.", False, None, 200),
    )
    monkeypatch.setattr(server, "_red_zone_breaches_for", lambda i, wt, target: [])

    monkeypatch.setattr(
        remote,
        "_SELF",
        {"key": "linux-box", "host": "linux-box", "os": "linux", "dns": "", "ip": ""},
    )
    monkeypatch.setattr(remote, "_DEVICES", {})
    dev = remote._device_state("mlrig2023")
    dev.update(
        host="MLRIG2023",
        os="windows",
        base_url="http://mlrig2023.invalid:8765",
        reachable=True,
        remote_control=True,
        auth=True,
    )
    monkeypatch.setattr(
        remote, "token_for", lambda key: TOKEN if key == "mlrig2023" else ""
    )
    monkeypatch.setattr(remote, "_fleet_key_for", lambda key: "")
    # The device's side: remote control on, and it knows this token.
    state = {"rc": True}
    monkeypatch.setattr(remote, "remote_control_enabled", lambda: state["rc"])
    monkeypatch.setattr(auth_mod, "token_valid", lambda t: t == TOKEN)

    fake = _Device()
    monkeypatch.setattr(remote, "get_json", fake.get_json)
    monkeypatch.setattr(remote, "post_json", fake.post_json)
    monkeypatch.setattr(remote, "put_bytes", fake.put_bytes)

    async def _no_refresh(key):
        return None

    monkeypatch.setattr(remote, "refresh_instances", _no_refresh)
    return {
        "here": here,
        "origin": origin,
        "home": home,
        "inbox": home / "inbox",
        "inst": inst,
        "dev": fake,
        "state": state,
        "tmp": tmp_path,
    }


def _set(**transfers):
    s = settings_mod.load_settings()
    for k, v in transfers.items():
        setattr(s.transfers, k, v)
    settings_mod.save_settings(s)


def _send(paths, device="MLRIG2023", **kw):
    body = {"device": device, "paths": paths}
    body.update(kw)
    return client.post("/api/instances/%s/send-files" % TITLE, json=body)


def _transfers(world):
    base = world["inbox"]
    out = []
    if not base.is_dir():
        return out
    for dev in base.iterdir():
        if dev.name.startswith(".") or not dev.is_dir():
            continue
        out += [p for p in dev.iterdir() if p.is_dir()]
    return out


def _partials(world):
    p = world["inbox"] / ".partial"
    return list(p.iterdir()) if p.is_dir() else []


# --------------------------------------------------------------------------- #
# files: happy paths
# --------------------------------------------------------------------------- #
class TestSendFiles:
    def test_a_file_lands_in_a_new_inbox_folder_with_its_checksum(self, world):
        r = _send(["tripwire/tripwire.service"])
        assert r.status_code == 200, r.text
        out = r.json()
        dest = world["inbox"] / "linux-box" / out["transfer_id"]
        assert out["path"] == str(dest)
        assert (dest / "tripwire" / "tripwire.service").read_text() == "[Unit]\n"
        want = _sha(b"[Unit]\n")
        assert out["files"] == [
            {"path": "tripwire/tripwire.service", "size": 7, "sha256": want}
        ]
        assert out["total_sha256"] == send_files.total_sha256(out["files"])
        rec = json.loads((dest.parent / (out["transfer_id"] + ".json")).read_text())
        assert rec["files"][0]["sha256"] == want and rec["from_session"] == TITLE
        assert out["uncommitted"] == [] and out["device"] == "mlrig2023"
        assert _partials(world) == []
        # Nothing was started or told: no note, no to_session.
        assert world["dev"].created == [] and world["dev"].messages == []

    def test_a_folder_recurses_and_leaves_out_what_must_not_travel(self, world):
        here = world["here"]
        t = here / "tripwire"
        (t / ".env").write_text("TOKEN=s3cret\n")
        (t / "deploy.pem").write_text("-----BEGIN-----\n")
        (t / ".env.example").write_text("TOKEN=\n")
        (t / "node_modules" / "x").mkdir(parents=True)
        (t / "node_modules" / "x" / "i.js").write_text("1")
        (here / ".gitignore").write_text("node_modules/\n")
        (t / ".claude").mkdir()
        (t / ".claude" / "settings.local.json").write_text("{}")
        (t / ".aider.chat.history.md").write_text("transcript")
        os.symlink("/etc/passwd", t / "passwd-link")
        os.mkfifo(t / "a-fifo")
        (t / "notes.md").write_text("draft\n")  # uncommitted
        r = _send(["tripwire"])
        assert r.status_code == 200, r.text
        out = r.json()
        sent = sorted(f["path"] for f in out["files"])
        assert sent == [
            "tripwire/.env.example",
            "tripwire/daemon.py",
            "tripwire/notes.md",
            "tripwire/tripwire.service",
        ]
        why = {s["path"]: s["why"] for s in out["skipped"]}
        assert why["tripwire/.env"] == "looks like a secret"
        assert why["tripwire/deploy.pem"] == "looks like a secret"
        assert why["tripwire/node_modules/"] == "ignored by git"
        assert why["tripwire/.claude"].startswith("never sent")
        assert why["tripwire/.aider.chat.history.md"].startswith("never sent")
        assert why["tripwire/passwd-link"] == "symlink"
        assert why["tripwire/a-fifo"] == "not a regular file"
        assert out["uncommitted"] == ["tripwire/.env.example", "tripwire/notes.md"]
        dest = world["inbox"] / "linux-box" / out["transfer_id"]
        landed = sorted(
            os.path.relpath(os.path.join(d, f), dest)
            for d, _, fs in os.walk(dest)
            for f in fs
        )
        assert landed == sent

    def test_executable_and_setuid_bits_are_stripped_and_recorded(self, world):
        t = world["here"] / "tripwire"
        (t / "run.sh").write_text("#!/bin/sh\necho hi\n")
        os.chmod(t / "run.sh", 0o4755)
        r = _send(["tripwire"])
        assert r.status_code == 200, r.text
        out = r.json()
        assert out["exec_stripped"] == ["tripwire/daemon.py", "tripwire/run.sh"]
        dest = world["inbox"] / "linux-box" / out["transfer_id"]
        for rel in ("tripwire/daemon.py", "tripwire/run.sh"):
            mode = stat.S_IMODE(os.lstat(dest / rel).st_mode)
            assert mode == 0o644, oct(mode)
        assert stat.S_IMODE(os.lstat(dest / "tripwire").st_mode) == 0o755

    def test_every_send_is_a_new_folder_nothing_is_overwritten(self, world):
        a = _send(["README.md"]).json()
        (world["here"] / "README.md").write_text("changed\n")
        b = _send(["README.md"]).json()
        assert a["transfer_id"] != b["transfer_id"]
        assert (
            world["inbox"] / "linux-box" / a["transfer_id"] / "README.md"
        ).read_text() == "the big repo\n"
        assert (
            world["inbox"] / "linux-box" / b["transfer_id"] / "README.md"
        ).read_text() == "changed\n"
        assert b["uncommitted"] == ["README.md"]

    def test_an_id_that_already_exists_is_refused_not_merged(self, world, monkeypatch):
        monkeypatch.setattr(inbox, "_new_id", lambda: "20261121-000000-deadbeef")
        assert _send(["README.md"]).status_code == 200
        r = _send(["lib/shared.py"])
        assert r.status_code == 409 and r.json()["code"] == "exists"
        dest = world["inbox"] / "linux-box" / "20261121-000000-deadbeef"
        assert sorted(os.listdir(dest)) == ["README.md"]

    def test_to_session_is_told_what_arrived_and_where(self, world):
        r = _send(
            ["tripwire/daemon.py"], to_session="listener", note="run it under systemd"
        )
        assert r.status_code == 200, r.text
        out = r.json()
        assert out["messaged"] == "mlrig2023::listener"
        ((to, body),) = world["dev"].messages
        assert to == "listener"
        assert out["path"] in body["text"]
        assert 'session "tripwire" on device "linux-box"' in body["text"]
        assert (
            "not your user" in body["text"]
            and "Nothing in it has been run" in body["text"]
        )
        assert "they were set on: tripwire/daemon.py" in body["text"]
        assert "run it under systemd" in body["text"]
        assert world["dev"].created == []

    def test_an_unknown_to_session_is_refused_before_any_byte(self, world):
        r = _send(["README.md"], to_session="listenr")
        assert r.status_code == 404
        body = r.json()
        assert body["code"] == "no_session" and "did you mean listener" in body["error"]
        assert _transfers(world) == [] and _partials(world) == []
        assert not any(c[0] == "PUT" for c in world["dev"].calls)

    def test_a_note_without_a_session_starts_one_in_that_folder(self, world):
        r = _send(["tripwire"], note="install it as a service and start it")
        assert r.status_code == 200, r.text
        out = r.json()
        (body,) = world["dev"].created
        assert body["repo_path"] == out["path"] and body["in_place"] is True
        assert body["spawned"] is True and body["program"] == "claude"
        assert "install it as a service and start it" in body["prompt"]
        assert "This session runs in that folder." in body["prompt"]
        assert out["title"] == "mlrig2023::" + TITLE

    def test_a_dropped_answer_resumes_from_what_arrived(self, world, monkeypatch):
        monkeypatch.setattr(send_files, "CHUNK", 5)
        world["dev"].drop_next_put = True
        r = _send(["tripwire/tripwire.service"])
        assert r.status_code == 200, r.text
        dest = world["inbox"] / "linux-box" / r.json()["transfer_id"]
        assert (dest / "tripwire" / "tripwire.service").read_text() == "[Unit]\n"
        puts = [c for c in world["dev"].calls if c[0] == "PUT"]
        assert (
            len(puts) == 2
            and ("GET", "/api/transfers/" + r.json()["transfer_id"])
            in world["dev"].calls
        )

    def test_device_name_is_case_insensitive_with_did_you_mean(self, world):
        assert _send(["README.md"], device="mlrig2023").status_code == 200
        r = _send(["README.md"], device="MLRIG2O23")
        assert r.status_code == 404
        assert r.json()["code"] == "no_device"
        assert r.json()["did_you_mean"] == "mlrig2023"
        assert "did you mean mlrig2023?" in r.json()["error"]


# --------------------------------------------------------------------------- #
# files: refusals on the sending side
# --------------------------------------------------------------------------- #
class TestSenderRefusals:
    @pytest.mark.parametrize(
        "path", ["../outside.txt", "/etc/hostname", "tripwire/../../x"]
    )
    def test_traversal_is_refused(self, world, path):
        (world["tmp"] / "outside.txt").write_text("no")
        r = _send([path])
        assert r.status_code in (403, 404), r.text
        if r.status_code == 403:
            assert r.json()["code"] == "outside_worktree"
        assert _transfers(world) == []

    def test_a_symlink_out_of_the_worktree_is_refused(self, world):
        (world["tmp"] / "outside.txt").write_text("no")
        os.symlink(world["tmp"] / "outside.txt", world["here"] / "link.txt")
        r = _send(["link.txt"])
        assert r.status_code == 403 and r.json()["code"] == "outside_worktree"
        assert _transfers(world) == []

    def test_a_named_secret_is_refused_by_default(self, world):
        (world["here"] / ".env").write_text("TOKEN=s3cret\n")
        r = _send([".env"])
        assert r.status_code == 403 and r.json()["code"] == "secret"
        assert "include_secrets" in r.json()["error"]

    def test_include_secrets_needs_the_owners_setting(self, world):
        (world["here"] / ".env").write_text("TOKEN=s3cret\n")
        r = _send([".env"], include_secrets=True)
        assert r.status_code == 403 and r.json()["code"] == "secrets_not_allowed"
        _set(allow_secrets=True)
        r = _send([".env"], include_secrets=True)
        assert r.status_code == 200, r.text
        assert r.json()["files"][0]["path"] == ".env"

    def test_secrets_baseline_files_count_as_secrets(self, world):
        (world["here"] / "lib" / "conf.py").write_text("KEY='abc'\n")
        (world["here"] / ".secrets.baseline").write_text(
            json.dumps({"results": {"lib/conf.py": [{"type": "Secret Keyword"}]}})
        )
        r = _send(["lib/conf.py"])
        assert r.status_code == 403 and r.json()["code"] == "secret"
        out = _send(["lib"]).json()
        assert {"path": "lib/conf.py", "why": "looks like a secret"} in out["skipped"]

    @pytest.mark.parametrize(
        "rel", [".git/config", ".claude/settings.json", ".codex/auth.json", ".git"]
    )
    def test_never_sent_even_with_secrets_allowed(self, world, rel):
        p = world["here"] / rel
        if not p.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("x")
        _set(allow_secrets=True)
        r = _send([rel], include_secrets=True)
        assert r.status_code == 403 and r.json()["code"] == "never_sent", r.text

    def test_size_and_count_limits(self, world):
        _set(max_mb=1)
        (world["here"] / "big.bin").write_bytes(b"x" * (1024 * 1024 + 1))
        r = _send(["big.bin"])
        assert r.status_code == 413 and r.json()["code"] == "too_large"
        assert "transfers.max_mb" in r.json()["error"]
        _set(max_mb=None, max_files=2)
        r = _send(["tripwire", "README.md"])
        assert r.status_code == 413 and r.json()["code"] == "too_many_files"
        assert _transfers(world) == []

    def test_red_zone_and_fence_are_honoured(self, world, monkeypatch):
        monkeypatch.setattr(
            server,
            "zone_hits_for_paths",
            lambda wt, rels: [
                {"path": r, "pattern": "tripwire/**", "kind": "red"}
                for r in rels
                if r.startswith("tripwire/")
            ],
        )
        r = _send(["tripwire"])
        assert r.status_code == 409 and r.json()["code"] == "red_zone"
        assert "red zone" in r.json()["error"]
        assert _send(["README.md"]).status_code == 200

    def test_remote_control_off_there_is_refused(self, world):
        dev = remote._DEVICES["mlrig2023"]
        dev["remote_control"] = False
        r = _send(["README.md"])
        assert r.status_code == 409 and r.json()["code"] == "remote_control_off"
        # Its hello says on, but it was switched off since: the device's own
        # gate refuses, and nothing lands.
        dev["remote_control"] = True
        world["state"]["rc"] = False
        r = _send(["README.md"])
        assert r.status_code == 409 and r.json()["code"] == "remote_control_off"
        assert _transfers(world) == [] and _partials(world) == []

    def test_device_asleep(self, world):
        world["dev"].offline = True
        r = _send(["README.md"])
        assert r.status_code == 409 and r.json()["code"] == "device_offline"

    def test_a_checksum_mismatch_drops_the_transfer(self, world, monkeypatch):
        real = send_files._read_chunk

        def flip(path, off, n):
            data = bytearray(real(path, off, n))
            data[0] ^= 1
            return bytes(data)

        monkeypatch.setattr(send_files, "_read_chunk", flip)
        r = _send(["README.md"])
        assert r.status_code == 422 and r.json()["code"] == "checksum_mismatch"
        assert _transfers(world) == [] and _partials(world) == []

    def test_relayed_and_guest_requests_are_refused(self, world):
        r = client.post(
            "/api/instances/%s/send-files" % TITLE,
            json={"device": "mlrig2023", "paths": ["README.md"]},
            headers={remote.REMOTE_HEADER: "elsewhere"},
        )
        assert r.status_code in (400, 403)
        assert _transfers(world) == []

    def test_bad_request_shapes(self, world):
        for body in (
            {"device": "mlrig2023", "paths": "README.md"},
            {"device": "mlrig2023", "paths": [1]},
            {"device": "mlrig2023", "paths": ["README.md"], "include_secrets": 1},
        ):
            r = client.post("/api/instances/%s/send-files" % TITLE, json=body)
            assert r.status_code == 400 and r.json()["code"] == "bad_request"

    def test_no_credential_or_transcript_is_opened(self, world, tmp_path, monkeypatch):
        home = tmp_path / "userhome"
        for rel in (
            ".claude/.credentials.json",
            ".claude/projects/x/a.jsonl",
            ".codex/auth.json",
        ):
            p = home / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("SECRET-TOKEN")
        monkeypatch.setenv("HOME", str(home))
        wt_claude = world["here"] / ".claude"
        wt_claude.mkdir()
        (wt_claude / "projects.jsonl").write_text("TRANSCRIPT")
        (world["here"] / ".codex").mkdir()
        (world["here"] / ".codex" / "auth.json").write_text("LOGIN")
        opened: list = []
        real_open, real_os_open = builtins.open, os.open

        def spy(file, *a, **k):
            opened.append(str(file))
            return real_open(file, *a, **k)

        def os_spy(path, *a, **k):
            opened.append(str(path))
            return real_os_open(path, *a, **k)

        monkeypatch.setattr(builtins, "open", spy)
        monkeypatch.setattr(os, "open", os_spy)
        r = _send(["."])
        monkeypatch.setattr(builtins, "open", real_open)
        monkeypatch.setattr(os, "open", real_os_open)
        assert r.status_code == 200, r.text
        assert not any(".claude" in p or ".codex" in p for p in opened), [
            p for p in opened if ".claude" in p or ".codex" in p
        ]
        sent = {f["path"] for f in r.json()["files"]}
        assert not any(p.startswith((".claude", ".codex", ".git/")) for p in sent)
        dest = r.json()["path"]
        for d, _dirs, fs in os.walk(dest):
            for f in fs:
                assert b"SECRET-TOKEN" not in open(os.path.join(d, f), "rb").read()
                assert b"TRANSCRIPT" not in open(os.path.join(d, f), "rb").read()


# --------------------------------------------------------------------------- #
# the receiving side, attacked directly
# --------------------------------------------------------------------------- #
def _hdr(token=TOKEN, marker=True):
    h = {"Authorization": "Bearer " + token}
    if marker:
        h[remote.REMOTE_HEADER] = "evil-box"
    return h


def _manifest(files, kind="files", **extra):
    wire = [
        {"path": p, "size": len(d), "sha256": _sha(d), "exec": False} for p, d in files
    ]
    m = {
        "v": 1,
        "kind": kind,
        "files": wire,
        "total_sha256": send_files.total_sha256(wire),
    }
    m.update(extra)
    return m


def _put_all(tid, files):
    for i, (_p, d) in enumerate(files):
        if d:
            r = client.put(
                "/api/transfers/%s/files/%d?offset=0" % (tid, i),
                content=d,
                headers=_hdr(),
            )
            assert r.status_code == 200, r.text


class TestReceiver:
    @pytest.mark.parametrize(
        "path",
        [
            "/etc/cron.d/x",
            "../escape",
            "a/../../escape",
            "./a",
            "a//b",
            "a\\..\\b",
            "C:/Windows/x",
            ".git/hooks/pre-commit",
            "sub/.GIT/config",
            "a\x00b",
            "",
        ],
    )
    def test_a_malicious_path_is_refused(self, world, path):
        r = client.post(
            "/api/transfers", json=_manifest([(path, b"x")]), headers=_hdr()
        )
        assert r.status_code == 400 and r.json()["code"] == "bad_manifest", r.text
        assert _partials(world) == [] and _transfers(world) == []

    def test_other_manifest_tricks(self, world):
        dup = _manifest([("a.txt", b"1"), ("A.TXT", b"2")])
        both = _manifest([("a", b"1"), ("a/b", b"2")])
        badsum = _manifest([("a.txt", b"1")])
        badsum["total_sha256"] = "0" * 64
        neg = _manifest([("a.txt", b"1")])
        neg["files"][0]["size"] = -1
        for m in (dup, both, badsum, neg, {"v": 2}, {"v": 1, "kind": "tar"}):
            r = client.post("/api/transfers", json=m, headers=_hdr())
            assert r.status_code == 400, (m, r.text)
        assert _partials(world) == []

    def test_only_a_linked_device_may_send(self, world):
        m = _manifest([("a.txt", b"1")])
        r = client.post("/api/transfers", json=m, headers=_hdr(marker=False))
        assert r.status_code == 403 and r.json()["code"] == "not_a_device"
        r = client.post("/api/transfers", json=m, headers=_hdr(token="wrong"))
        assert r.status_code == 401 and r.json()["code"] == "not_linked"
        world["state"]["rc"] = False
        r = client.post("/api/transfers", json=m, headers=_hdr())
        assert r.status_code == 403
        assert _partials(world) == []

    def test_receiver_limits_and_quota(self, world):
        _set(max_files=1)
        r = client.post(
            "/api/transfers", json=_manifest([("a", b"1"), ("b", b"2")]), headers=_hdr()
        )
        assert r.status_code == 413 and r.json()["code"] == "too_many_files"
        _set(max_files=None, inbox_mb=1)
        r = client.post(
            "/api/transfers",
            json=_manifest([("big", b"x" * (1024 * 1024 + 1))]),
            headers=_hdr(),
        )
        assert r.status_code == 507 and r.json()["code"] == "inbox_full"

    def test_chunk_tricks(self, world):
        files = [("a.txt", b"hello")]
        tid = client.post(
            "/api/transfers", json=_manifest(files), headers=_hdr()
        ).json()["id"]
        r = client.put(
            "/api/transfers/%s/files/0?offset=3" % tid, content=b"lo", headers=_hdr()
        )
        assert r.status_code == 409 and r.json()["code"] == "offset_mismatch"
        assert r.json()["received"] == 0
        r = client.put(
            "/api/transfers/%s/files/0?offset=0" % tid,
            content=b"hello!!",
            headers=_hdr(),
        )
        assert r.status_code == 400 and r.json()["code"] == "too_long"
        r = client.put(
            "/api/transfers/%s/files/7?offset=0" % tid, content=b"x", headers=_hdr()
        )
        assert r.status_code == 404
        r = client.post("/api/transfers/%s/finish" % tid, headers=_hdr())
        assert r.status_code == 409 and r.json()["code"] == "incomplete"
        assert _transfers(world) == []
        for bad in ("..", "x", "20261121-000000-zzzzzzzz"):
            r = client.get("/api/transfers/" + bad, headers=_hdr())
            assert r.status_code == 404, bad

    def test_a_planted_symlink_in_staging_is_never_followed(self, world, tmp_path):
        files = [("a.txt", b"hello")]
        tid = client.post(
            "/api/transfers", json=_manifest(files), headers=_hdr()
        ).json()["id"]
        target = tmp_path / "victim.txt"
        target.write_text("keep")
        os.symlink(target, world["inbox"] / ".partial" / tid / "data" / "a.txt")
        r = client.put(
            "/api/transfers/%s/files/0?offset=0" % tid, content=b"hello", headers=_hdr()
        )
        assert r.status_code == 409
        assert target.read_text() == "keep"

    @pytest.mark.parametrize("plant", ["symlink", "hardlink"])
    def test_a_planted_link_at_finish_drops_the_transfer(self, world, tmp_path, plant):
        files = [("a.txt", b"hello")]
        tid = client.post(
            "/api/transfers", json=_manifest(files), headers=_hdr()
        ).json()["id"]
        _put_all(tid, files)
        data = world["inbox"] / ".partial" / tid / "data"
        victim = tmp_path / "victim.txt"
        victim.write_text("keep")
        os.chmod(victim, 0o600)
        if plant == "symlink":
            os.symlink(victim, data / "extra")
        else:
            os.link(data / "a.txt", tmp_path / "second-name")
        r = client.post("/api/transfers/%s/finish" % tid, headers=_hdr())
        assert r.status_code == 409 and r.json()["code"] == "bad_state", r.text
        assert stat.S_IMODE(os.lstat(victim).st_mode) == 0o600
        assert _transfers(world) == [] and _partials(world) == []

    def test_cancel_removes_the_partial(self, world):
        tid = client.post(
            "/api/transfers", json=_manifest([("a.txt", b"1")]), headers=_hdr()
        ).json()["id"]
        assert client.post("/api/transfers/%s/cancel" % tid, headers=_hdr()).json()[
            "ok"
        ]
        assert _partials(world) == []

    def test_old_transfers_expire_unless_a_session_works_there(self, world):
        a = _send(["README.md"]).json()
        b = _send(["lib"]).json()
        old = time.time() - 30 * 86400
        for t in (a, b):
            os.utime(t["path"], (old, old))
        world["inst"]._wt = os.path.join(b["path"], "lib")  # a session lives in b
        gone = inbox.prune(14, server._inbox_in_use)
        assert gone == [a["path"]]
        assert not os.path.exists(a["path"]) and not os.path.exists(a["path"] + ".json")
        assert os.path.isdir(b["path"])


# --------------------------------------------------------------------------- #
# send_session: the bundle when the device has no checkout
# --------------------------------------------------------------------------- #
def _send_session(**body):
    payload = {"device": "MLRIG2023"}
    payload.update(body)
    return client.post("/api/instances/%s/send-to-device" % TITLE, json=payload)


def _on_origin(world, branch=BRANCH):
    return subprocess.run(
        ["git", "-C", str(world["origin"]), "rev-parse", "--verify", "--quiet", branch],
        capture_output=True,
        text=True,
    ).stdout.strip()


class TestBundle:
    def test_no_checkout_there_sends_a_bundle_and_starts_in_it(self, world):
        _commit(world["here"], ".env", "TOKEN=s3cret\n", "oops, a secret")
        r = _send_session(note="install it as a service and start it")
        assert r.status_code == 200, r.text
        out = r.json()
        assert out["via"] == "bundle" and out["pushed"] is False
        assert "has no checkout of" in out["how"] and "git bundle" in out["how"]
        assert _on_origin(world) == ""  # nothing was pushed
        repo = out["repo_path"]
        assert repo.startswith(str(world["inbox"] / "linux-box"))
        (body,) = world["dev"].created
        assert body["repo_path"] == repo and body["in_place"] is True
        assert "continue_branch" not in body and "base_ref" not in body
        assert "no checkout of that repository" in body["prompt"]
        assert "install it as a service and start it" in body["prompt"]
        assert ".env" in body["prompt"]  # named as left out
        # A real standalone repository on the branch, with the session's commits.
        assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD") == BRANCH
        log = _git(repo, "log", "--format=%s").splitlines()
        # The commit that only added .env carries nothing once it is left out.
        assert log[:2] == ["Add a systemd unit", "Add the tripwire daemon"]
        assert log[-1].startswith("MindFlock snapshot")
        assert len(log) == 3
        assert _git(repo, "log", "-1", "--format=%an", "HEAD") == "Ada"
        assert (
            open(os.path.join(repo, "tripwire", "daemon.py")).read()
            == "print('armed')\n"
        )
        assert open(os.path.join(repo, "lib", "shared.py")).read() == "X = 1\n"
        assert not os.path.exists(os.path.join(repo, ".env"))
        assert ".env" in out["bundle"]["left_out"]
        assert _git(repo, "remote") == ""
        mode = stat.S_IMODE(
            os.lstat(os.path.join(repo, "tripwire", "daemon.py")).st_mode
        )
        assert mode == 0o644
        assert _git(repo, "status", "--porcelain") == ""  # fileMode off: clean
        assert not os.path.isdir(os.path.join(repo, ".git", "hooks"))  # no templates
        assert not [
            p for p in os.listdir(os.path.dirname(repo)) if p.endswith(".bundle")
        ]

    def test_paths_send_only_that_folder_as_the_repo_root(self, world):
        r = _send_session(paths=["tripwire"])
        assert r.status_code == 200, r.text
        out = r.json()
        repo = out["repo_path"]
        assert os.path.basename(repo) == "tripwire"
        assert sorted(f for f in os.listdir(repo) if f != ".git") == [
            "daemon.py",
            "tripwire.service",
        ]
        assert "only tripwire of the repository" in world["dev"].created[0]["prompt"]
        assert out["bundle"]["scope"] == "tripwire"

    def test_a_checkout_there_still_pushes(self, world, tmp_path):
        there = tmp_path / "there"
        _git(tmp_path, "clone", "-q", str(world["origin"]), str(there))
        world["dev"].repo_there = there
        r = _send_session()
        assert r.status_code == 200, r.text
        out = r.json()
        assert out["via"] == "git" and out["pushed"] is True
        assert out["repo_path"] == os.path.realpath(there)
        assert _on_origin(world) == _git(world["here"], "rev-parse", "HEAD")
        assert _transfers(world) == []

    def test_via_git_refuses_without_a_checkout(self, world):
        r = _send_session(via="git")
        assert r.status_code == 409 and r.json()["code"] == "repo_missing"
        assert _transfers(world) == []

    def test_no_remote_goes_as_a_bundle(self, world):
        _git(world["here"], "remote", "remove", "origin")
        r = _send_session()
        assert r.status_code == 200, r.text
        assert r.json()["via"] == "bundle" and "no origin remote" in r.json()["how"]
        r = _send_session(via="git")
        assert r.status_code == 400 and r.json()["code"] == "no_remote"

    def test_too_large_suggests_the_folder(self, world):
        _set(max_mb=1)
        import random

        rnd = random.Random(1)
        big = bytes(rnd.getrandbits(8) for _ in range(1024 * 1024 + 4096))
        (world["here"] / "blob.bin").write_bytes(big)
        _git(world["here"], "add", "blob.bin")
        _git(world["here"], "commit", "-q", "-m", "a blob")
        r = _send_session()
        assert r.status_code == 413 and r.json()["code"] == "too_large"
        assert "paths=" in r.json()["error"]
        assert "tripwire" in r.json()["suggest_paths"]

    def test_uncommitted_work_is_still_refused(self, world):
        (world["here"] / "tripwire" / "daemon.py").write_text("changed\n")
        r = _send_session()
        assert r.status_code == 409 and r.json()["code"] == "uncommitted"
        assert _transfers(world) == []


def _bundle_manifest(path, branch=BRANCH, name="work"):
    data = open(path, "rb").read()
    return _manifest(
        [(name + ".bundle", data)],
        kind="bundle",
        bundle={"branch": branch, "name": name},
    ), [(name + ".bundle", data)]


class TestHostileBundles:
    def _repo(self, tmp_path):
        r = tmp_path / "evil"
        r.mkdir()
        _git(r, "init", "-q", "-b", "main")
        _commit(r, "a.txt", "1\n", "one")
        _git(r, "checkout", "-q", "-b", BRANCH)
        _commit(r, "b.txt", "2\n", "two")
        return r

    def _deliver(self, world, bundle, **kw):
        m, files = _bundle_manifest(bundle, **kw)
        r = client.post("/api/transfers", json=m, headers=_hdr())
        assert r.status_code == 201, r.text
        tid = r.json()["id"]
        _put_all(tid, files)
        return client.post("/api/transfers/%s/finish" % tid, headers=_hdr())

    def test_a_thin_bundle_with_prerequisites_is_refused(self, world, tmp_path):
        r = self._repo(tmp_path)
        b = tmp_path / "thin.bundle"
        _git(r, "bundle", "create", "-q", str(b), "main.." + BRANCH)
        resp = self._deliver(world, b)
        assert resp.status_code == 422 and resp.json()["code"] == "bad_bundle"
        assert _transfers(world) == [] and _partials(world) == []

    def test_two_heads_or_the_wrong_head_is_refused(self, world, tmp_path):
        r = self._repo(tmp_path)
        b = tmp_path / "two.bundle"
        _git(r, "bundle", "create", "-q", str(b), "main", BRANCH)
        assert self._deliver(world, b).json()["code"] == "bad_bundle"
        c = tmp_path / "other.bundle"
        _git(r, "bundle", "create", "-q", str(c), "main")
        assert self._deliver(world, c).json()["code"] == "bad_bundle"

    def test_garbage_is_refused(self, world, tmp_path):
        b = tmp_path / "junk.bundle"
        b.write_bytes(b"# v2 git bundle\nnot really\n")
        resp = self._deliver(world, b)
        assert resp.status_code == 422 and resp.json()["code"] == "bad_bundle"

    def test_symlinks_in_the_tree_land_as_plain_files_and_no_hook_runs(
        self, world, tmp_path
    ):
        r = self._repo(tmp_path)
        os.symlink("/etc/passwd", r / "pw")
        hooks = r / ".githooks"
        hooks.mkdir()
        canary = tmp_path / "canary"
        (hooks / "post-checkout").write_text("#!/bin/sh\ntouch %s\n" % canary)
        os.chmod(hooks / "post-checkout", 0o755)
        _git(r, "add", "pw", ".githooks")
        _git(r, "commit", "-q", "-m", "link + hook")
        b = tmp_path / "ok.bundle"
        _git(r, "bundle", "create", "-q", str(b), BRANCH)
        resp = self._deliver(world, b)
        assert resp.status_code == 200, resp.text
        repo = resp.json()["path"]
        assert os.path.isfile(os.path.join(repo, "pw"))
        assert not os.path.islink(os.path.join(repo, "pw"))
        assert open(os.path.join(repo, "pw")).read() == "/etc/passwd"
        assert not canary.exists()
        assert resp.json()["exec_stripped"] == [".githooks/post-checkout"]

    def test_bundle_export_is_standalone_and_verifies_in_an_empty_repo(
        self, world, tmp_path
    ):
        out = tmp_path / "out"
        out.mkdir()
        fork = _git(world["here"], "merge-base", "main", "HEAD")
        exp = session_bundle.export(str(world["here"]), BRANCH, fork, str(out))
        empty = tmp_path / "empty"
        _git(tmp_path, "init", "-q", str(empty))
        _git(empty, "bundle", "verify", "-q", exp["path"])
        assert exp["commits"] == 2 and exp["snapshot"] is True
        # The session's repository gained no objects or refs from the export.
        assert (
            _git(world["here"], "for-each-ref", "--format=%(refname)").count(
                "refs/heads"
            )
            == 2
        )


# --------------------------------------------------------------------------- #
# the MCP tool
# --------------------------------------------------------------------------- #
def _tree():
    return [
        row("root"),
        row("orch", parent="root"),
        row("w1", parent="orch", spawned=True),
        row("other"),
        row("mac::far"),
    ]


def _files_route(api):
    seen = []

    def route(title, payload, q):
        seen.append((title, payload))
        return {
            "ok": True,
            "device": "mlrig2023",
            "path": "/home/x/.mindflock/inbox/linux-box/20261121-000000-abcdef01",
            "files": [{"path": "a.txt", "size": 1, "sha256": "0" * 64}],
            "total_sha256": "1" * 64,
            "skipped": [],
            "uncommitted": [],
            "caveats": [],
        }

    api.routes[("POST", "send-files")] = route
    return seen


class TestMcpSendFiles:
    def test_sends_from_itself_by_default(self):
        box, api, clock = make_box(_tree(), me="orch")
        seen = _files_route(api)
        out = box.send_files(
            {"device": "MLRIG2023", "paths": ["tripwire"], "note": "spin it up"},
            FakeCtx(clock),
        )
        assert seen == [
            (
                "orch",
                {
                    "device": "MLRIG2023",
                    "paths": ["tripwire"],
                    "note": "spin it up",
                    "to_session": "",
                    "include_secrets": False,
                },
            )
        ]
        assert out["ok"] is True and out["path"].endswith("abcdef01")

    def test_scope_rule_follows_send_session(self):
        box, api, clock = make_box(_tree(), me="orch")
        seen = _files_route(api)
        box.send_files({"device": "m", "paths": ["a"], "title": "w1"}, FakeCtx(clock))
        assert seen[0][0] == "w1"
        with pytest.raises(ToolError, match="you or one of your descendants"):
            box.send_files(
                {"device": "m", "paths": ["a"], "title": "other"}, FakeCtx(clock)
            )
        with pytest.raises(ToolError, match="send its files from there"):
            box.send_files(
                {"device": "m", "paths": ["a"], "title": "mac::far"}, FakeCtx(clock)
            )
        box2, _api, clock2 = make_box(_tree(), me="orch", scope="readonly")
        with pytest.raises(ToolError, match="readonly"):
            box2.send_files({"device": "m", "paths": ["a"]}, FakeCtx(clock2))

    def test_an_old_server_says_update(self):
        box, api, clock = make_box(_tree(), me="orch")
        api.route_missing.add("send-files")
        with pytest.raises(ToolError, match="update it"):
            box.send_files({"device": "m", "paths": ["a"]}, FakeCtx(clock))

    def test_send_session_passes_via_and_paths(self):
        box, api, clock = make_box(_tree(), me="orch")
        seen = []

        def route(title, payload, q):
            seen.append(payload)
            return {"ok": True, "via": "bundle", "how": "…", "title": "m::orch"}

        api.routes[("POST", "send-to-device")] = route
        out = box.send_session(
            {"device": "m", "via": "bundle", "paths": ["tripwire"]}, FakeCtx(clock)
        )
        assert seen == [
            {"device": "m", "note": "", "via": "bundle", "paths": ["tripwire"]}
        ]
        assert out["via"] == "bundle" and out["how"] == "…"


def test_classify():
    c = send_files.classify
    assert c(".env") == "secret" and c("a/.env.production") == "secret"
    assert c(".env.example") is None and c("id_ed25519.pub") is None
    assert c("certs/server.key") == "secret" and c("x/credentials.json") == "secret"
    assert c(".git/config") == "never" and c("a/.claude/x.jsonl") == "never"
    assert c(".aider.chat.history.md") == "never" and c(".ssh/id_rsa") == "never"
    assert c("src/main.py") is None and c("docs/keys.md") is None
