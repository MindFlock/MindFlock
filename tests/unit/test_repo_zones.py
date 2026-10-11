"""Shared zones: the committed ``.mindflock/zones.toml`` (backend.config.repo_zones).

* the file: parse, a bad entry skipped, a bad file ignored with one logged
  line, append/remove that keep every other byte, dedupe and the red/green
  conflict;
* precedence against the local store (an identical local zone stands in for
  a shared one unless it is waived here; red beats green);
* the guard and the push gate reading both, the file protected from agents
  and never a green breach;
* the Add-to-repo routes (owner only) and the human-session commit warning.
"""

from __future__ import annotations

import json
import logging
import os

import pytest
from fastapi.testclient import TestClient

from backend.config import red_zones
from backend.config import repo_zones as rpz
from backend.web import server

# The throwaway-repo session fixtures of the Code Map route tests (the autouse
# `_hermetic` one replaces ENGINE.instances with fakes).
from tests.unit.test_code_map_routes import (  # noqa: F401
    _FakeInst,
    _git,
    _hermetic,
    _rid,
    _write,
    repo,
    sess,
)

client = TestClient(server.app)

GOOD = """# team rules
[[zone]]
kind = "red"
pattern = "config/"
note = "prod settings"
author = "Ethan"
added = 2026-10-10

# keep the bundle
[[zone]]
kind = "green"
pattern = "/src/api"
name = "API"
"""


def _put(root, text):
    path = rpz.file_path(str(root))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


# --------------------------------------------------------------------------- #
# The file
# --------------------------------------------------------------------------- #
def test_parse_reads_every_field():
    zones, problems = rpz.parse(GOOD)
    assert problems == []
    red, green = zones
    assert red == {
        "id": rpz.zone_id("red", "config/"),
        "kind": "red",
        "pattern": "config/",
        "name": "",
        "note": "prod settings",
        "author": "Ethan",
        "added": "2026-10-10",
    }
    assert green["kind"] == "green" and green["pattern"] == "/src/api"
    assert green["name"] == "API"


def test_a_bad_entry_is_skipped_and_named():
    zones, problems = rpz.parse(
        '[[zone]]\npattern = "../up"\n[[zone]]\nkind = "blue"\npattern = "a"\n'
        '[[zone]]\npattern = "ok.txt"\n'
    )
    assert [z["pattern"] for z in zones] == ["ok.txt"]
    assert len(problems) == 2


@pytest.mark.parametrize("text", ["[[zone]\n", "zone = 3\n"])
def test_a_bad_file_raises_from_parse(text):
    with pytest.raises(ValueError):
        rpz.parse(text)


def test_a_bad_file_is_ignored_with_one_logged_line(tmp_path, caplog):
    _put(tmp_path, "[[zone]\nnot toml")
    with caplog.at_level(logging.WARNING, logger="backend.config.repo_zones"):
        assert rpz.load_doc(str(tmp_path))[0] == []
        assert rpz.load_doc(str(tmp_path))[1]  # the error is reported
        assert rpz.load(str(tmp_path)) == []
    assert len([r for r in caplog.records if "Ignoring" in r.message]) == 1
    _put(tmp_path, GOOD)  # fixed: read again (mtime/size moved)
    assert len(rpz.load(str(tmp_path))) == 2


def test_no_file_is_no_zones(tmp_path):
    assert rpz.load_doc(str(tmp_path)) == ([], "")


def test_add_creates_then_appends_keeping_every_byte(tmp_path):
    wt = str(tmp_path)
    zone, created = rpz.add(
        wt, "config.toml", note="n", author="Ethan", added="2026-10-10"
    )
    assert created and zone["pattern"] == "config.toml"
    text = open(rpz.file_path(wt), encoding="utf-8").read()
    assert text.startswith("# Zones MindFlock enforces")
    assert '[[zone]]\nkind = "red"\npattern = "config.toml"\nnote = "n"\n' in text
    assert "added = 2026-10-10\n" in text  # a bare TOML date
    _put(tmp_path, text + "# my comment\n")
    before = open(rpz.file_path(wt), encoding="utf-8").read()
    rpz.add(wt, "/secrets", kind="red", author='Q "uote"')
    after = open(rpz.file_path(wt), encoding="utf-8").read()
    assert after.startswith(before)  # nothing above the new block moved
    assert [z["author"] for z in rpz.load(wt)] == ["Ethan", 'Q "uote"']
    # Same kind + pattern: nothing written.
    again, created = rpz.add(wt, "config.toml")
    assert not created and again["id"] == zone["id"]
    assert open(rpz.file_path(wt), encoding="utf-8").read() == after


def test_add_refuses_the_other_kind_and_an_unusable_file(tmp_path):
    wt = str(tmp_path)
    rpz.add(wt, "src/")
    with pytest.raises(red_zones.ZoneConflict):
        rpz.add(wt, "/src", kind="green")
    with pytest.raises(ValueError):
        rpz.add(wt, "../x")
    _put(tmp_path, "[[zone]\nbroken")
    with pytest.raises(ValueError):
        rpz.add(wt, "a.txt")
    assert open(rpz.file_path(wt)).read() == "[[zone]\nbroken"  # never overwritten


def test_remove_takes_out_one_block_only(tmp_path):
    wt = str(tmp_path)
    _put(tmp_path, GOOD)
    gone = rpz.remove(wt, rpz.zone_id("red", "config/"))
    assert gone["pattern"] == "config/"
    text = open(rpz.file_path(wt), encoding="utf-8").read()
    assert text.startswith("# team rules\n")
    assert "# keep the bundle" in text and 'pattern = "/src/api"' in text
    assert "config/" not in text
    assert rpz.remove(wt, "sz_nope") is None


# --------------------------------------------------------------------------- #
# Precedence against the local store
# --------------------------------------------------------------------------- #
def test_effective_zones_add_the_shared_ones(tmp_path):
    wt = str(tmp_path)
    _put(tmp_path, GOOD)
    eff = red_zones.effective_zones(wt, "rid")
    shared = [z for z in eff if z["scope"] == "shared"]
    assert {z["pattern"] for z in shared} == {"config/", "/src/api"}
    red = next(z for z in shared if z["kind"] == "red")
    assert red["author"] == "Ethan" and red["locked"] and red["re"]
    assert red["source"] == ".mindflock/zones.toml"


def test_an_identical_local_zone_stands_in_unless_waived(tmp_path):
    wt = str(tmp_path)
    _put(tmp_path, GOOD)
    local = red_zones.add_zone("repo", "rid", "config/")
    eff = red_zones.effective_zones(wt, "rid")
    reds = [z for z in eff if z["kind"] == "red"]
    assert len(reds) == 1 and reds[0]["scope"] == "repo"
    assert reds[0]["shared"] is True and reds[0]["shared_author"] == "Ethan"
    # "Allow here" is a local choice: the committed rule still applies.
    red_zones.set_waiver(wt, local["id"], True)
    doc = red_zones.zones_doc(wt, "rid", ci=False, with_tests=False)
    assert [z["scope"] for z in doc["red"]] == ["shared"]
    assert red_zones.classify("config/a.toml", None, doc) == "blocked"


def test_red_beats_green_across_the_two_sources(tmp_path):
    wt = str(tmp_path)
    _put(tmp_path, '[[zone]]\npattern = "src/api/keys.py"\n')
    red_zones.add_zone("worktree", wt, "src/api", kind="green")
    doc = red_zones.zones_doc(wt, "rid", ci=False, with_tests=False)
    assert red_zones.classify("src/api/keys.py", None, doc) == "blocked"
    assert red_zones.classify("src/api/ok.py", None, doc) == "ok"


def test_the_file_itself_is_never_a_green_breach(tmp_path):
    wt = str(tmp_path)
    red_zones.add_zone("worktree", wt, "src/", kind="green")
    doc = red_zones.zones_doc(wt, "rid", ci=False, with_tests=False)
    assert red_zones.classify(".mindflock/zones.toml", None, doc) == "companion"
    assert red_zones.breach_verdicts(doc, [".mindflock/zones.toml"]) == {}


# --------------------------------------------------------------------------- #
# The guard and the push gate
# --------------------------------------------------------------------------- #
def test_the_guard_carries_shared_zones_and_protects_the_file(sess, repo):
    _put(repo, '[[zone]]\npattern = "config/"\nauthor = "Ethan"\n')
    root = os.path.realpath(str(repo))
    assert red_zones.sync_guard(root, _rid(repo), lroot=str(repo)) == "written"
    guard = json.load(open(red_zones.guard_path(root)))
    assert [r["pattern"] for r in guard["rules"]] == ["config/"]
    assert guard["rules"][0]["scope"] == "shared"
    assert os.path.join(root, ".mindflock", "zones.toml") in guard["protect"]
    assert "config/settings.toml" in guard["files"]


def test_the_push_gate_reads_the_shared_zones(sess, repo):
    _put(repo, '[[zone]]\npattern = "config/"\n')
    _write(repo, "config/settings.toml", "a = 2\n")
    _git(repo, "commit", "-qam", "touch config")
    hits = server._red_zone_breaches_for(sess, str(repo))
    assert [h["path"] for h in hits] == ["config/settings.toml"]


# --------------------------------------------------------------------------- #
# Routes: Add to repo (owner only)
# --------------------------------------------------------------------------- #
def test_add_to_repo_writes_the_worktree_file_with_the_author(sess, repo):
    from backend.web.core import person

    person.update(name="Ethan")
    r = client.post(
        "/api/instances/rzt/shared-zones",
        json={"pattern": "config/", "note": "prod", "kind": "red"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["created"] is True and body["shared_exists"] is True
    assert body["shared_path"] == ".mindflock/zones.toml"
    (z,) = body["shared"]
    assert (z["pattern"], z["author"], z["added"]) == ("config/", "Ethan", rpz.today())
    assert os.path.isfile(repo / ".mindflock" / "zones.toml")
    # Nothing committed: the file rides the branch with the next commit.
    assert "zones.toml" in _git(repo, "status", "--porcelain", "-uall")
    got = client.get("/api/instances/rzt/shared-zones").json()
    assert [s["id"] for s in got["shared"]] == [z["id"]]
    assert server._shared_zones_body(str(repo))["shared"][0]["id"] == z["id"]

    conflict = client.post(
        "/api/instances/rzt/shared-zones", json={"pattern": "/config", "kind": "green"}
    )
    assert conflict.status_code == 409
    bad = client.post("/api/instances/rzt/shared-zones", json={"pattern": "../x"})
    assert bad.status_code == 400

    rm = client.post("/api/instances/rzt/shared-zones/remove", json={"id": z["id"]})
    assert rm.status_code == 200 and rm.json()["shared"] == []
    again = client.post("/api/instances/rzt/shared-zones/remove", json={"id": z["id"]})
    assert again.status_code == 404


def test_add_to_repo_is_the_owners(sess, repo, monkeypatch):
    from backend.web.core import tailnet_trust

    async def _no(scope):
        return False

    monkeypatch.setattr(tailnet_trust, "request_trusted", _no)
    monkeypatch.setenv("CS_WEB_MODE", "tailscale")
    monkeypatch.setenv("MINDFLOCK_AUTH", "0")
    anon = TestClient(server.app, client=("100.64.0.5", 41000))
    r = anon.post("/api/instances/rzt/shared-zones", json={"pattern": "config/"})
    assert r.status_code == 403
    r = anon.post("/api/instances/rzt/shared-zones/remove", json={"id": "sz_x"})
    assert r.status_code == 403
    assert not os.path.exists(repo / ".mindflock" / "zones.toml")


# --------------------------------------------------------------------------- #
# A person's own window: a warning at commit, never a block
# --------------------------------------------------------------------------- #
@pytest.fixture
def commits(monkeypatch):
    done: list = []

    def _commit(title, wt, msg, skip=""):
        done.append((title, msg))
        return None

    monkeypatch.setattr(server, "_commit_into_shell", _commit)
    monkeypatch.setattr(server, "git_available", lambda: True)
    monkeypatch.setattr(server._live_stage, "watch", lambda *a, **k: None)
    return done


def test_a_human_commit_into_a_red_zone_is_warned_then_allowed(sess, repo, commits):
    _put(repo, '[[zone]]\npattern = "config/"\n')
    _write(repo, "config/settings.toml", "a = 3\n")
    _write(repo, "config/new.toml", "b = 1\n")  # untracked counts too
    r = client.post("/api/instances/rzt/commit", json={"message": "m"})
    assert r.status_code == 409
    body = r.json()
    assert body["zone_warning"] is True
    assert {h["path"] for h in body["red_zone_breaches"]} == {
        "config/settings.toml",
        "config/new.toml",
    }
    assert body["red_zone_breaches"][0]["pattern"] == "config/"
    assert commits == []
    r = client.post(
        "/api/instances/rzt/commit", json={"message": "m", "proceed_anyway": True}
    )
    assert r.status_code == 200 and commits == [("rzt", "m")]


def test_an_agents_commit_is_not_warned(sess, repo, commits):
    _put(repo, '[[zone]]\npattern = "config/"\n')
    _write(repo, "config/settings.toml", "a = 3\n")
    sess.Spawned = True  # an agent created it: the guard already stops edits
    r = client.post("/api/instances/rzt/commit", json={"message": "m"})
    assert r.status_code == 200 and commits == [("rzt", "m")]


def test_a_clean_human_commit_is_not_warned(sess, repo, commits):
    _put(repo, '[[zone]]\npattern = "config/"\n')
    _write(repo, "lib.py", "X = 2\n")
    r = client.post("/api/instances/rzt/commit", json={"message": "m"})
    assert r.status_code == 200
