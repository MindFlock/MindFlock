"""``GET /api/instances/{title}/thread`` and the row's ``last_report``.

A family's thread is assembled from the engine registry (lineage), the tick
snapshot rows and the mailbox — and reading it must NEVER consume mail: a
person looking at the Thread tab can't be allowed to mark read (and so cancel
the typing of) the report an orchestrator is long-polling for. The mailbox
here is the real one (``conftest`` points it at a temp file).
"""

from __future__ import annotations

import datetime as _dt
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend.session.storage import Status
from backend.web import server
from backend.web.core import mailbox, snapshot, thread
from backend.web.server import app

client = TestClient(app)

T0 = 1_760_000_000.0


class _Worktree:
    def __init__(self, sha):
        self.sha = sha

    def GetBaseCommitSHA(self):  # noqa: N802
        return self.sha


class _Inst:
    def __init__(self, title, parent="", created=T0, base_sha="", prompt=""):
        self.Title = title
        self.Parent = parent
        self.Program = "claude"
        self.Branch = "mindflock/" + title
        self.Status = Status.Running
        self.Prompt = prompt
        self.Provisioned = False
        self.CreatedAt = (
            _dt.datetime.fromtimestamp(created, _dt.timezone.utc)
            if created is not None
            else None
        )
        self._base = base_sha

    def GetGitWorktree(self):  # noqa: N802
        if not self._base:
            raise RuntimeError("no worktree")
        return _Worktree(self._base)

    def GetWorktreePath(self):  # noqa: N802
        return ""


@pytest.fixture
def fam(monkeypatch):
    """api (root) with workers api-billing / api-search, plus an unrelated
    session; tick snapshot rows for everyone but api-search."""
    instances: dict = {}
    monkeypatch.setattr(server.ENGINE, "instances", instances)
    monkeypatch.setattr(thread, "_SEEDS", {})
    rows: list = []
    monkeypatch.setattr(
        server._events, "sessions_snapshot", lambda: [dict(r) for r in rows]
    )
    probes: list = []

    def cheap_row(inst, cheap=False):
        probes.append((inst.Title, cheap))
        return {
            "title": inst.Title,
            "status": "running",
            "branch": inst.Branch,
            "diff_stat": None,
        }

    monkeypatch.setattr(server, "_instance_json", cheap_row)
    monkeypatch.setattr(server, "_agent_activity_cached", lambda i, t: "working")
    monkeypatch.setattr(server._agent_state, "state_since", lambda t: 123.0)

    def add(title, **kw):
        instances[title] = _Inst(title, **kw)
        return instances[title]

    add("api", created=T0, base_sha="aaa111")
    add(
        "api-billing",
        parent="api",
        created=T0 + 10,
        base_sha="bbb222",
        prompt="Add per-user rate limiting to services/billing/",
    )
    add("api-search", parent="api", created=T0 + 20, base_sha="bbb222")
    add("other", created=T0)
    for t, act in (("api", "idle"), ("api-billing", "clarify"), ("other", "idle")):
        rows.append(
            {
                "title": t,
                "status": "running",
                "activity": act,
                "activity_since": 99.0,
                "branch": "br-" + t,
                "diff_stat": {"files": 1, "additions": 2, "deletions": 3},
            }
        )
    return SimpleNamespace(instances=instances, rows=rows, add=add, probes=probes)


def _post(to, text, sender="", kind="message", ts=None, status=None):
    data = {"status": status} if status else None
    return mailbox.post(
        to, text, sender=sender, kind=kind, data=data, delivery="inbox", now=ts
    )


def _thread(title="api", **params):
    r = client.get("/api/instances/%s/thread" % title, params=params)
    assert r.status_code == 200, r.text
    return r.json()


# --------------------------------------------------------------------------- #
# last_report                                                                  #
# --------------------------------------------------------------------------- #
def test_last_report_is_the_newest_result_to_the_current_parent(fam):
    assert thread.last_report(fam.instances, "api-billing") is None
    _post(
        "api",
        "first try\n\nDetails:\nlong",
        sender="api-billing",
        kind="result",
        ts=T0 + 100,
        status="blocked",
    )
    _post("api", "a question", sender="api-billing", ts=T0 + 150)
    m = _post(
        "api",
        "billing done: limiter wired\n\nDetails:\n- x",
        sender="api-billing",
        kind="result",
        ts=T0 + 200,
        status="done",
    )
    assert thread.last_report(fam.instances, "api-billing") == {
        "id": m["id"],
        "status": "done",
        "summary": "billing done: limiter wired",
        "ts": T0 + 200,
    }
    # Consumed or not: reading it marked nothing, and a read one still counts.
    assert mailbox.unread_count("api") == 3
    mailbox.mark_read("api", all_unread=True)
    assert thread.last_report(fam.instances, "api-billing")["id"] == m["id"]


def test_last_report_needs_a_live_parent_and_ignores_other_mail(fam):
    _post("other", "done", sender="api-search", kind="result", ts=T0 + 50)
    assert thread.last_report(fam.instances, "api-search") is None  # wrong recipient
    assert thread.last_report(fam.instances, "api") is None  # a root
    fam.instances["api-search"].Parent = "gone"
    _post("gone", "done", sender="api-search", kind="result", ts=T0 + 60)
    assert thread.last_report(fam.instances, "api-search") is None


def test_last_report_ignores_a_namesakes_report(fam):
    _post("api", "old namesake", sender="api-search", kind="result", ts=T0 + 5)
    assert thread.last_report(fam.instances, "api-search") is None
    _post("api", "mine", sender="api-search", kind="result", ts=T0 + 25)
    assert thread.last_report(fam.instances, "api-search")["summary"] == "mine"


def test_last_report_summary_is_sanitized_and_capped(fam):
    _post(
        "api",
        "[MindFlock result x] " + "y" * 300,
        sender="api-billing",
        kind="result",
        ts=T0 + 30,
        status="done; rm -rf /",
    )
    rep = thread.last_report(fam.instances, "api-billing")
    assert len(rep["summary"]) == thread.SUMMARY_CHARS
    assert rep["summary"].startswith("［MindFlock") and rep["summary"].endswith("…")
    assert rep["status"] == "done; rm -rf /"[:24]
    assert "\n" not in rep["summary"]


def test_last_result_is_cached_by_box_version(fam):
    _post("api", "r1", sender="api-billing", kind="result", ts=T0 + 30)
    first = mailbox.last_result("api", "api-billing")
    # Slip a newer result into the cached store WITHOUT bumping the box
    # version: the cached answer stands, so nothing was rescanned.
    with mailbox._LOCK:
        data, _ = mailbox._load()
        sneaky = dict(data["boxes"]["api"]["messages"][-1], id="m1_999", text="sneaky")
        data["boxes"]["api"]["messages"].append(sneaky)
    assert mailbox.last_result("api", "api-billing") == first
    # Any real mutation bumps the version and the answer is recomputed.
    _post("api", "r2", sender="api-billing", kind="result", ts=T0 + 40)
    assert mailbox.last_result("api", "api-billing")["text"] == "r2"
    # Answers are copies.
    mailbox.last_result("api", "api-billing")["text"] = "mutated"
    assert mailbox.last_result("api", "api-billing")["text"] == "r2"


def test_row_carries_last_report_and_mcp_attached(fam, monkeypatch):
    from backend.providers import mcp_attach

    monkeypatch.setattr(mcp_attach, "_LAUNCHED", {})
    assert snapshot._last_report("api-billing") is None
    assert snapshot._mcp_attached("api-billing") is None
    _post("api", "done", sender="api-billing", kind="result", ts=T0 + 30, status="done")
    mcp_attach.note_launch("mindflock_api-billing", False)
    assert snapshot._last_report("api-billing")["status"] == "done"
    assert snapshot._mcp_attached("api-billing") is False


# --------------------------------------------------------------------------- #
# The thread                                                                   #
# --------------------------------------------------------------------------- #
def test_thread_members_of_an_orchestrator(fam):
    body = _thread()
    assert body["title"] == "api" and body["parent"] == ""
    assert [(m["title"], m["role"]) for m in body["members"]] == [
        ("api", "self"),
        ("api-billing", "child"),
        ("api-search", "child"),
    ]
    billing = body["members"][1]
    assert billing == {
        "title": "api-billing",
        "role": "child",
        "status": "running",
        "activity": "clarify",
        "activity_since": 99.0,
        "branch": "br-api-billing",
        "diff_stat": {"files": 1, "additions": 2, "deletions": 3},
        "created_at": T0 + 10,
        "last_report": None,
        "base_sha": "bbb222",
    }
    # No tick row for api-search: a cheap row plus the memoized activity.
    search = body["members"][2]
    assert search["activity"] == "working" and search["activity_since"] == 123.0
    assert ("api-search", True) in fam.probes


def test_thread_of_a_worker_has_its_parent_but_not_its_siblings(fam):
    body = _thread("api-billing")
    assert body["parent"] == "api"
    assert [(m["title"], m["role"]) for m in body["members"]] == [
        ("api-billing", "self"),
        ("api", "parent"),
    ]
    (spawn,) = [i for i in body["items"] if i["type"] == "spawn"]
    assert spawn["from"] == "api" and spawn["to"] == "api-billing"


def test_thread_items_spawns_and_family_mail_in_order(fam):
    m1 = _post("api-billing", "use common/ratelimit", sender="api", ts=T0 + 30)
    m2 = _post("api", "which window?", sender="api-billing", ts=T0 + 40)
    _post("api", "unrelated", sender="other", ts=T0 + 45)
    _post("api", "from the CLI", sender="", ts=T0 + 46)
    _post("other", "sibling chatter", sender="api-search", ts=T0 + 47)
    m3 = _post(
        "api",
        "done\n\nDetails:\nall green",
        sender="api-search",
        kind="result",
        ts=T0 + 50,
        status="done",
    )
    items = _thread()["items"]
    assert [(i["type"], i["from"], i["to"]) for i in items] == [
        ("spawn", "api", "api-billing"),
        ("spawn", "api", "api-search"),
        ("message", "api", "api-billing"),
        ("message", "api-billing", "api"),
        ("result", "api-search", "api"),
    ]
    spawn_b = items[0]
    assert spawn_b == {
        "type": "spawn",
        "id": "spawn:api-billing:%d" % int((T0 + 10) * 1000),
        "ts": T0 + 10,
        "from": "api",
        "to": "api-billing",
        "text": "Add per-user rate limiting to services/billing/",
        "status": None,
        "state": None,
        "base_sha": "bbb222",
    }
    assert items[1]["text"] == ""  # no seed prompt known
    assert [i["id"] for i in items[2:]] == [m1["id"], m2["id"], m3["id"]]
    assert items[4]["status"] == "done" and items[4]["state"] == "held"
    assert items[4]["text"] == "done\n\nDetails:\nall green"
    assert items[2]["status"] is None and items[2]["base_sha"] is None


def test_thread_never_consumes_mail(fam):
    """Pinned: a pending push stays pending (the lane still types it), held
    stays unread, and no box version moves."""
    pend = mailbox.post(
        "api",
        "report",
        sender="api-billing",
        kind="result",
        delivery="auto",
        now=T0 + 30,
    )
    held = _post("api-billing", "question", sender="api", ts=T0 + 31)
    versions = (mailbox.version("api"), mailbox.version("api-billing"))
    for _ in range(2):
        _thread()
        _thread("api-billing")
        thread.last_report(fam.instances, "api-billing")
    assert mailbox.get("api", pend["id"])["state"] == "pending"
    assert mailbox.get("api-billing", held["id"])["state"] == "held"
    assert (mailbox.version("api"), mailbox.version("api-billing")) == versions
    assert mailbox.unread_count("api") == 1 and mailbox.unread_count("api-billing") == 1


def test_thread_seed_from_the_create_memo(fam):
    inst = fam.instances["api-search"]
    thread.note_seed("api-search", T0 + 20, "Search piece: " + "z" * 400)
    spawn = [i for i in _thread()["items"] if i["to"] == "api-search"][0]
    assert spawn["text"].startswith("Search piece: ")
    assert len(spawn["text"]) == thread.SEED_CHARS
    # A namesake created later does not inherit the memo.
    inst.CreatedAt = _dt.datetime.fromtimestamp(T0 + 999, _dt.timezone.utc)
    spawn = [i for i in _thread()["items"] if i["to"] == "api-search"][0]
    assert spawn["text"] == ""


def test_thread_seed_from_a_provisioned_prompt_file(fam, tmp_path):
    from backend.session import provisioned

    (tmp_path / provisioned.PROMPT_BASENAME).write_text("Ticket: fix upload\n")
    inst = fam.add("api-upload", parent="api", created=T0 + 30)
    inst.Provisioned = True
    inst.GetWorktreePath = lambda: str(tmp_path)
    assert thread.seed_text(inst) == "Ticket: fix upload"


def test_thread_pagination(fam):
    ids = [
        _post("api", "m%d" % n, sender="api-billing", ts=T0 + 100 + n)["id"]
        for n in range(5)
    ]
    page = _thread(limit=3)
    assert [i["id"] for i in page["items"]] == ids[2:]
    assert page["more"] is True
    older = _thread(limit=3, before=ids[2])
    assert [i["type"] for i in older["items"]] == ["spawn", "message", "message"]
    assert older["more"] is True
    oldest = _thread(limit=3, before=older["items"][0]["id"])
    assert [i["type"] for i in oldest["items"]] == ["spawn"]
    assert oldest["more"] is False
    assert len(_thread(limit=999)["items"]) == 7  # clamped at MAX_LIMIT, all fit


@pytest.mark.parametrize(
    "params,msg",
    [
        ({"limit": "x"}, "limit must be an integer"),
        ({"limit": "0"}, "limit must be positive"),
        ({"before": "nope"}, "unknown item: nope"),
    ],
)
def test_thread_400s(fam, params, msg):
    r = client.get("/api/instances/api/thread", params=params)
    assert r.status_code == 400 and r.json()["error"] == msg


def test_thread_404_for_an_unknown_session(fam):
    assert client.get("/api/instances/ghost/thread").status_code == 404


def test_thread_of_a_loner_is_just_itself(fam):
    body = _thread("other")
    assert [m["role"] for m in body["members"]] == ["self"]
    assert body["items"] == [] and body["more"] is False


def test_thread_member_without_a_worktree_has_no_base_sha(fam):
    fam.add("api-new", parent="api", created=T0 + 40)
    spawn = [i for i in _thread()["items"] if i["to"] == "api-new"][0]
    assert spawn["base_sha"] is None


def test_between_is_read_only_and_family_scoped(fam):
    a = _post("api", "x", sender="api-billing", ts=T0 + 1)
    _post("api", "y", sender="other", ts=T0 + 2)
    b = _post("api-billing", "z", sender="api", ts=T0 + 3)
    got = mailbox.between(["api", "api-billing", ""])
    assert [m["id"] for m in got] == [a["id"], b["id"]]
    got[0]["text"] = "mutated"
    assert mailbox.get("api", a["id"])["text"] == "x"


# --------------------------------------------------------------------------- #
# Review 2026-10-05                                                            #
# --------------------------------------------------------------------------- #
def test_thread_leaves_out_a_namesakes_mail(fam):
    """split → wrap up → delete api-search → split again, same name: the old
    run's report is still in the parent's box, and must not show as the new
    worker's result (its row's last_report says it hasn't reported)."""
    _post("api", "old run: search done", sender="api-search", kind="result", ts=T0 + 5)
    _post("api-search", "old run: go", sender="api", ts=T0 + 4)
    _post("api", "new run: search done", sender="api-search", kind="result", ts=T0 + 30)
    texts = [i["text"] for i in _thread()["items"] if i["type"] != "spawn"]
    assert texts == ["new run: search done"]
    search = next(m for m in _thread()["members"] if m["title"] == "api-search")
    assert search["last_report"]["summary"] == "new run: search done"
    # The sender's side counts too: a parent re-created under its old name
    # doesn't inherit what the workers told its predecessor.
    fam.instances["api"].CreatedAt = _dt.datetime.fromtimestamp(
        T0 + 40, _dt.timezone.utc
    )
    assert [i for i in _thread()["items"] if i["type"] != "spawn"] == []


def test_paging_back_survives_an_evicted_anchor(fam):
    """ "Load older" sends the oldest item it has; an inbox over its caps may
    have dropped it meanwhile — the time its id carries stands in."""
    ids = [
        _post("api", "m%d" % n, sender="api-billing", ts=T0 + 100 + n)["id"]
        for n in range(5)
    ]
    gone = "m%d_999999" % int((T0 + 102.5) * 1000)  # between m2 and m3
    older = _thread(limit=10, before=gone)
    msgs = [i["id"] for i in older["items"] if i["type"] == "message"]
    assert msgs == ids[:3]
    spawn_gone = "spawn:api-search:%d" % int((T0 + 15) * 1000)
    older = _thread(limit=10, before=spawn_gone)
    assert [i["to"] for i in older["items"]] == ["api-billing"]


def test_last_result_cache_survives_a_store_reset(fam):
    """The box version comes from a file-wide counter that restarts when the
    store is deleted (or set aside as corrupt): a cached answer must not be
    served for the new store at the same version."""
    _post("api", "r1", sender="api-billing", kind="result", ts=T0 + 30)
    assert mailbox.last_result("api", "api-billing")["text"] == "r1"
    import os

    os.remove(mailbox.mailbox_path())
    _post("api", "r2", sender="api-billing", kind="result", ts=T0 + 31)
    assert mailbox.version("api") == 1  # the same version as before the reset
    assert mailbox.last_result("api", "api-billing")["text"] == "r2"


# --------------------------------------------------------------------------- #
# Finished children: a closed / deleted worker stays on its parent's Thread    #
# --------------------------------------------------------------------------- #
def _finish(fam, title, *, parent="api", created, **row):
    """Give ``title`` the snapshot row a tick would have, then remove it the
    way every removal route does (pop, then the flock-wide teardown)."""
    fam.rows[:] = [r for r in fam.rows if r["title"] != title]
    fam.rows.append(
        {
            "title": title,
            "parent": parent,
            "created_at": created,
            "branch": "br-" + title,
            "stage": row.pop("stage", "pushed"),
            "pr_url": row.pop("pr_url", ""),
            "diff_stat": {"files": 2, "additions": 30, "deletions": 4},
            "last_report": row.pop("last_report", None),
        }
    )
    for r in fam.rows:
        if r["title"] == parent:
            r["created_at"] = T0
    fam.instances.pop(title)
    server._on_session_removed(title)


def test_a_deleted_worker_stays_on_its_orchestrators_thread(fam):
    _post("api-billing", "use common/ratelimit", sender="api", ts=T0 + 30)
    rep = _post(
        "api",
        "billing done",
        sender="api-billing",
        kind="result",
        ts=T0 + 50,
        status="done",
    )
    thread.note_seed("api-billing", T0 + 10, "Add per-user rate limiting")
    _finish(fam, "api-billing", created=T0 + 10, pr_url="https://x/pr/7")

    body = _thread()
    assert [m["title"] for m in body["members"]] == ["api", "api-search"]
    (done,) = body["finished"]
    assert done["title"] == "api-billing" and done["how"] == "deleted"
    assert done["branch"] == "br-api-billing" and done["stage"] == "pushed"
    assert done["pr_url"] == "https://x/pr/7"
    assert done["diff_stat"] == {"files": 2, "additions": 30, "deletions": 4}
    # Its report is read back from the parent's box (the worker's own inbox
    # went with it, so the instruction TO it is not in the log any more).
    assert done["last_report"]["status"] == "done"
    assert done["last_report"]["id"] == rep["id"]
    kinds = [(i["type"], i["from"], i["to"]) for i in body["items"]]
    assert ("spawn", "api", "api-billing") in kinds
    assert ("result", "api-billing", "api") in kinds
    spawn = next(i for i in body["items"] if i["to"] == "api-billing")
    assert spawn["text"] == "Add per-user rate limiting"


def test_a_closed_worker_says_it_can_be_reopened(fam, monkeypatch):
    now = _dt.datetime.now().astimezone().isoformat()
    monkeypatch.setattr(
        server,
        "_load_recently_closed",
        lambda: [{"title": "api-search", "closed_at": now}],
    )
    _finish(fam, "api-search", created=T0 + 20)
    (done,) = _thread()["finished"]
    assert done["title"] == "api-search" and done["how"] == "closed"


def test_a_removed_parent_takes_its_finished_list_with_it(fam):
    _finish(fam, "api-billing", created=T0 + 10)
    assert [f["title"] for f in _thread()["finished"]] == ["api-billing"]
    # The orchestrator goes, and a new session takes its title.
    fam.instances.pop("api")
    server._on_session_removed("api")
    fam.add("api", created=T0 + 500)
    assert _thread()["finished"] == []


def test_a_reused_parent_title_does_not_inherit_finished_children(fam):
    _finish(fam, "api-billing", created=T0 + 10)
    # Replace the parent WITHOUT the removal hook (e.g. a crash + recreate):
    # the stored parent creation time no longer matches.
    fam.add("api", created=T0 + 900)
    assert _thread()["finished"] == []


def test_a_root_session_is_nobodys_finished_child(fam):
    fam.instances.pop("other")
    server._on_session_removed("other")
    assert _thread()["finished"] == []
    assert _thread("api-billing")["finished"] == []
