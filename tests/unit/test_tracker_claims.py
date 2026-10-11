"""Claims a teammate can see (backend.ticket_ingestion.tracker_claims).

* the pure rule: assigned to someone else + the claim label, or a started
  state; any-assignee sources count the label only; an unknown "me" claims
  nothing;
* the mirror: assign (only an unassigned ticket, never on an any-assignee
  source) + label, recorded per session, label removed on release, never
  un-assigned;
* each provider's assign / label / unlabel calls, over a stubbed aiohttp;
* the Begin-work refusal payload (409 ``claimed_by`` + ``override``), the
  owner-only override and the pipeline's refusal.

No network: every HTTP call goes to :class:`_Session`.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from backend.ticket_ingestion import tracker_claims as tc
from backend.ticket_ingestion.config import TicketProviderConfig
from backend.ticket_ingestion.providers import get_provider
from tests._factories import make_ticket


@pytest.fixture(autouse=True)
def _fresh_caches():
    tc.clear_cache()
    yield
    tc.clear_cache()


def _src(**over):
    base = dict(provider="shortcut", api_token="t", member_id="me")
    base.update(over)
    return TicketProviderConfig(**base)


# --------------------------------------------------------------------------- #
# The rule
# --------------------------------------------------------------------------- #
def test_claim_label_defaults_and_is_configurable():
    assert tc.claim_label(_src()) == "mindflock:working"
    assert tc.claim_label(_src(claim_label="acme bot")) == "acme-bot:working"
    assert tc.claim_label(_src(claim_label="x:working")) == "x:working"


def test_someone_else_with_the_label_is_a_claim():
    story = make_ticket(
        owner_ids=["bob"], owner_names=["Bob Smith"], labels=["MindFlock:Working"]
    )
    claim = tc.claimed_by_other(story, _src(), me="me")
    assert claim["kind"] == "tracker" and claim["why"] == "label"
    assert claim["name"] == "Bob Smith"
    assert tc.describe(claim) == "Bob Smith is on this"


def test_someone_else_in_a_started_state_is_a_claim():
    story = make_ticket(owner_ids=["bob"], owner_names=["Bob"], state_id="500")
    claim = tc.claimed_by_other(story, _src(), me="me", started_ids={"500"})
    assert claim["why"] == "started"
    # …and not in an unstarted one.
    assert tc.claimed_by_other(story, _src(), me="me", started_ids={"9"}) is None


@pytest.mark.parametrize(
    "owners, me",
    [
        (["me"], "me"),  # mine
        (["me", "bob"], "me"),  # co-owned with me
        ([], "me"),  # nobody's
        (["bob"], ""),  # "me" unknown: can't tell, claims nothing
    ],
)
def test_no_claim_when_not_someone_elses(owners, me):
    story = make_ticket(owner_ids=owners, labels=["mindflock:working"])
    assert tc.claimed_by_other(story, _src(), me=me) is None


def test_any_assignee_source_counts_the_label_only():
    qa = _src(assignee_scope="anyone", workflow_state="500")
    story = make_ticket(owner_ids=["author"], owner_names=["Author"], state_id="500")
    assert tc.claimed_by_other(story, qa, me="me", started_ids={"500"}) is None
    story.labels = ["mindflock:working"]
    claim = tc.claimed_by_other(story, qa, me="me", started_ids={"500"})
    # The assignee wrote the code; whoever labelled it is unnamed.
    assert claim["name"] == "A teammate"


# --------------------------------------------------------------------------- #
# The check and the mirror against a fake provider
# --------------------------------------------------------------------------- #
class _FakeProvider:
    can_claim = True

    def __init__(self, me="me", states=()):
        self.me = me
        self.states = list(states)
        self.calls: list = []

    async def claim_identity(self):
        return self.me

    async def list_states(self):
        self.calls.append(("states",))
        return self.states

    async def assign_to(self, tid, member):
        self.calls.append(("assign", tid, member))

    async def add_label(self, tid, label):
        self.calls.append(("add", tid, label))

    async def remove_label(self, tid, label):
        self.calls.append(("remove", tid, label))


def test_tracker_holder_reads_the_started_states():
    prov = _FakeProvider(states=[{"id": "7", "type": "started"}, {"id": "8"}])
    story = make_ticket(owner_ids=["bob"], owner_names=["Bob"], state_id="7")
    claim = asyncio.run(tc.tracker_holder(story, src=_src(), provider=prov))
    assert claim["why"] == "started"


def test_tracker_holder_fails_open():
    class _Broken(_FakeProvider):
        async def claim_identity(self):
            raise RuntimeError("tracker down")

    story = make_ticket(owner_ids=["bob"], labels=["mindflock:working"])
    assert asyncio.run(tc.tracker_holder(story, src=_src(), provider=_Broken())) is None


@pytest.fixture
def fake_source(monkeypatch):
    prov = _FakeProvider()
    src = _src(id="sc")
    monkeypatch.setattr(tc, "_source_of", lambda story, config=None: src)
    monkeypatch.setattr(
        "backend.ticket_ingestion.providers.get_provider", lambda s: prov
    )
    monkeypatch.setattr(
        "backend.ticket_ingestion.config.config_for_launch",
        lambda fallback=None: SimpleNamespace(source_for=lambda key: src),
    )
    return prov


def test_mirror_assigns_an_unassigned_ticket_and_labels_it(fake_source):
    story = make_ticket(id=42, owner_ids=[], slug="sc-42")
    out = asyncio.run(tc.mirror_started(story))
    assert out == {"assigned": True, "labelled": True, "label": "mindflock:working"}
    assert fake_source.calls == [
        ("assign", "42", "me"),
        ("add", "42", "mindflock:working"),
    ]
    rec = tc.mirrored()["sc-42"]
    assert rec["source"] == "sc" and rec["id"] == "42"


def test_mirror_never_reassigns(fake_source):
    story = make_ticket(id=42, owner_ids=["bob"], slug="sc-42")
    asyncio.run(tc.mirror_started(story))
    assert [c[0] for c in fake_source.calls] == ["add"]


def test_our_own_label_is_not_someone_elses_claim(fake_source):
    story = make_ticket(id=42, owner_ids=[], slug="sc-42")
    asyncio.run(tc.mirror_started(story, title="sc-42"))
    seen = make_ticket(id=42, owner_ids=["bob"], labels=["mindflock:working"])
    seen.source_key = "sc"
    assert (
        asyncio.run(tc.tracker_holder(seen, src=_src(id="sc"), provider=fake_source))
        is None
    )


def test_release_removes_the_label_but_not_the_assignment(fake_source):
    story = make_ticket(id=42, owner_ids=[], slug="sc-42")
    asyncio.run(tc.mirror_started(story, title="sc-42"))
    fake_source.calls.clear()
    assert asyncio.run(tc.release("sc-42")) is True
    assert fake_source.calls == [("remove", "42", "mindflock:working")]
    assert "sc-42" not in tc.mirrored()
    # Nothing mirrored -> nothing to do (and no thread).
    assert asyncio.run(tc.release("sc-42")) is False
    assert tc.release_soon("never-mirrored") is False


def test_a_retry_session_keeps_the_label_on(fake_source):
    story = make_ticket(id=42, owner_ids=[], slug="sc-42")
    asyncio.run(tc.mirror_started(story, title="sc-42"))
    asyncio.run(tc.mirror_started(story, title="sc-42-2"))
    fake_source.calls.clear()
    assert asyncio.run(tc.release("sc-42")) is False
    assert fake_source.calls == []
    assert asyncio.run(tc.release("sc-42-2")) is True


# --------------------------------------------------------------------------- #
# Providers over a stubbed aiohttp
# --------------------------------------------------------------------------- #
class _Resp:
    def __init__(self, status=200, data=None):
        self.status = status
        self._data = data if data is not None else {}

    async def json(self):
        return self._data

    async def text(self):
        return json.dumps(self._data)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _Session:
    """Answers by (method, url substring); records every call."""

    def __init__(self, routes):
        self.routes = routes  # [(method, needle, _Resp | callable(json))]
        self.calls: list = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def _call(self, method, url, **kw):
        self.calls.append((method, url, kw.get("json")))
        for m, needle, resp in self.routes:
            if m == method and needle in url:
                return resp(kw.get("json")) if callable(resp) else resp
        return _Resp(404, {"error": "no route"})

    def get(self, url, **kw):
        return self._call("GET", url, **kw)

    def post(self, url, **kw):
        return self._call("POST", url, **kw)

    def put(self, url, **kw):
        return self._call("PUT", url, **kw)

    def delete(self, url, **kw):
        return self._call("DELETE", url, **kw)


def _run(provider, session, coro_fn):
    with patch("aiohttp.ClientSession", return_value=session):
        return asyncio.run(coro_fn(provider))


def test_shortcut_assign_keeps_owners_and_labels_replace_the_set():
    p = get_provider(_src())
    story = {"owner_ids": ["bob"], "labels": [{"name": "bug"}]}
    s = _Session(
        [("GET", "/stories/9", _Resp(200, story)), ("PUT", "/stories/9", _Resp(200))]
    )
    _run(p, s, lambda p: p.assign_to("9", "me"))
    assert s.calls[-1] == ("PUT", s.calls[-1][1], {"owner_ids": ["bob", "me"]})
    s.calls.clear()
    _run(p, s, lambda p: p.add_label("9", "mindflock:working"))
    assert s.calls[-1][2] == {
        "labels": [{"name": "bug"}, {"name": "mindflock:working"}]
    }
    # Already labelled / already gone: no write.
    s2 = _Session([("GET", "/stories/9", _Resp(200, {"labels": [{"name": "bug"}]}))])
    _run(p, s2, lambda p: p.remove_label("9", "mindflock:working"))
    assert [c[0] for c in s2.calls] == ["GET"]


def test_jira_assign_and_label_updates():
    p = get_provider(
        _src(provider="jira", base_url="https://x.atlassian.net", email="e@x")
    )
    s = _Session([("PUT", "/rest/api/3/issue/ENG-1", _Resp(204))])
    _run(p, s, lambda p: p.assign_to("ENG-1", "acc-1"))
    _run(p, s, lambda p: p.add_label("ENG-1", "mindflock:working"))
    _run(p, s, lambda p: p.remove_label("ENG-1", "mindflock:working"))
    assert s.calls[0][1].endswith("/rest/api/3/issue/ENG-1/assignee")
    assert s.calls[0][2] == {"accountId": "acc-1"}
    assert s.calls[1][2] == {"update": {"labels": [{"add": "mindflock:working"}]}}
    assert s.calls[2][2] == {"update": {"labels": [{"remove": "mindflock:working"}]}}


def test_linear_label_is_found_or_created_then_added_by_id():
    p = get_provider(_src(provider="linear"))

    def gql(body):
        q = body["query"]
        if "issueLabels" in q:
            return _Resp(200, {"data": {"issueLabels": {"nodes": []}}})
        if "issueLabelCreate" in q:
            return _Resp(
                200,
                {
                    "data": {
                        "issueLabelCreate": {
                            "success": True,
                            "issueLabel": {"id": "L1"},
                        }
                    }
                },
            )
        if "issueUpdate" in q:
            return _Resp(200, {"data": {"issueUpdate": {"success": True}}})
        return _Resp(
            200,
            {
                "data": {
                    "issue": {"id": "U1", "team": {"id": "T1"}, "labels": {"nodes": []}}
                }
            },
        )

    s = _Session([("POST", "linear.app", gql)])
    _run(p, s, lambda p: p.add_label("ENG-5", "mindflock:working"))
    update = s.calls[-1][2]
    assert "addedLabelIds" in update["query"]
    assert update["variables"] == {"id": "U1", "l": ["L1"]}
    create = next(c for c in s.calls if "issueLabelCreate" in c[2]["query"])
    assert create[2]["variables"] == {"name": "mindflock:working", "team": "T1"}


def test_linear_assign_sets_the_assignee():
    p = get_provider(_src(provider="linear"))
    s = _Session(
        [
            (
                "POST",
                "linear.app",
                _Resp(200, {"data": {"issueUpdate": {"success": True}}}),
            )
        ]
    )
    _run(p, s, lambda p: p.assign_to("ENG-5", "user-1"))
    assert s.calls[0][2]["variables"] == {"id": "ENG-5", "a": "user-1"}


def test_asana_tag_is_reused_or_created_and_removed():
    p = get_provider(_src(provider="asana", project="111"))
    s = _Session(
        [
            ("GET", "/tasks/5", _Resp(200, {"data": {"tags": []}})),
            (
                "GET",
                "/workspaces/111/tags",
                _Resp(200, {"data": [{"gid": "T9", "name": "MindFlock:Working"}]}),
            ),
            ("POST", "/tasks/5/addTag", _Resp(200, {"data": {}})),
            ("PUT", "/tasks/5", _Resp(200, {"data": {}})),
        ]
    )
    _run(p, s, lambda p: p.add_label("5", "mindflock:working"))
    assert s.calls[-1][2] == {"data": {"tag": "T9"}}
    _run(p, s, lambda p: p.assign_to("5", "u-1"))
    assert s.calls[-1][2] == {"data": {"assignee": "u-1"}}
    s2 = _Session(
        [
            (
                "GET",
                "/tasks/5",
                _Resp(
                    200,
                    {"data": {"tags": [{"gid": "T9", "name": "mindflock:working"}]}},
                ),
            ),
            ("POST", "/tasks/5/removeTag", _Resp(200, {"data": {}})),
        ]
    )
    _run(p, s2, lambda p: p.remove_label("5", "mindflock:working"))
    assert s2.calls[-1][1].endswith("/tasks/5/removeTag")


def test_github_assign_label_and_unlabel(monkeypatch):
    p = get_provider(_src(provider="github_issues", project="o/r", member_id="octo"))

    async def _headers():
        return {"Authorization": "Bearer x"}

    monkeypatch.setattr(p, "_headers", _headers)
    s = _Session(
        [
            ("POST", "/issues/3/assignees", _Resp(201)),
            ("POST", "/issues/3/labels", _Resp(200)),
            ("DELETE", "/issues/3/labels/", _Resp(404)),
        ]
    )
    _run(p, s, lambda p: p.assign_to("3", "octo"))
    _run(p, s, lambda p: p.add_label("3", "mindflock:working"))
    _run(p, s, lambda p: p.remove_label("3", "mindflock:working"))  # 404 = gone
    assert s.calls[0][2] == {"assignees": ["octo"]}
    assert s.calls[1][2] == {"labels": ["mindflock:working"]}
    assert s.calls[2][1].endswith("/labels/mindflock%3Aworking")
    assert asyncio.run(p.claim_identity()) == "octo"


def test_github_issue_carries_its_labels(monkeypatch):
    p = get_provider(_src(provider="github_issues", project="o/r"))
    issue = {"number": 3, "title": "t", "labels": [{"name": "mindflock:working"}]}
    ticket = asyncio.run(p._issue_to_ticket(None, {}, issue))
    assert ticket.labels == ["mindflock:working"]


# --------------------------------------------------------------------------- #
# Begin work: the refusal payload and the owner-only override
# --------------------------------------------------------------------------- #
from tests.unit.test_ticket_launch import launch_env  # noqa: E402,F401
from backend.web import server  # noqa: E402
from backend.web.core import ticket_start  # noqa: E402

_CLAIM = {
    "kind": "tracker",
    "name": "Bob",
    "label": "Bob",
    "why": "label",
    "source": "sc",
    "device": "",
    "since": 0.0,
    "person": "",
}


def test_begin_work_refuses_a_ticket_a_teammate_is_on(launch_env, monkeypatch):
    monkeypatch.setattr(tc, "tracker_holder", AsyncMock(return_value=dict(_CLAIM)))
    with pytest.raises(ticket_start.LaunchError) as err:
        asyncio.run(ticket_start.launch("sc", "23588"))
    assert err.value.status == 409
    body = err.value.body
    assert body["claimed_by"]["name"] == "Bob"
    assert body["override"] == "override_claim"
    assert "Bob is on this" in body["error"]
    assert not launch_env.tasks  # nothing launched
    # The owner's Override starts it anyway.
    out = asyncio.run(ticket_start.launch("sc", "23588", override_claim=True))
    assert out["started"] is True


def test_override_is_the_owners_only(launch_env, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setattr(tc, "tracker_holder", AsyncMock(return_value=dict(_CLAIM)))
    monkeypatch.setattr(server, "git_available", lambda: True)
    client = TestClient(server.app)
    r = client.post("/api/tickets/start", json={"source": "sc", "id": "23588"})
    assert r.status_code == 409 and r.json()["claimed_by"]["name"] == "Bob"
    r = client.post(
        "/api/tickets/start",
        json={"source": "sc", "id": "23588", "override_claim": True, "spawned": True},
    )
    assert r.status_code == 403
    r = client.post(
        "/api/tickets/start",
        json={"source": "sc", "id": "23588", "override_claim": True},
    )
    assert r.status_code == 202


def test_describe_words_a_tracker_claim():
    from backend.web.core import fleet_claims

    assert fleet_claims.describe(_CLAIM) == "Bob is on this"


def test_holder_for_falls_back_to_the_tracker(monkeypatch):
    from backend.web.core import fleet_claims

    monkeypatch.setattr(fleet_claims, "holder", AsyncMock(return_value=None))
    monkeypatch.setattr(tc, "tracker_holder", AsyncMock(return_value=dict(_CLAIM)))
    story = make_ticket()
    assert asyncio.run(fleet_claims.holder_for("sc-1", story))["kind"] == "tracker"
    assert asyncio.run(fleet_claims.holder_for("sc-1", story, tracker=False)) is None
    assert asyncio.run(fleet_claims.holder_for("sc-1")) is None


# --------------------------------------------------------------------------- #
# The pipeline
# --------------------------------------------------------------------------- #
def test_the_pipeline_skips_a_ticket_a_teammate_is_on(tmp_path, monkeypatch):
    from backend.ticket_ingestion import orchestrator as orch_mod
    from backend.ticket_ingestion.config import PipelineConfig
    from backend.ticket_ingestion.orchestrator import PipelineOrchestrator

    monkeypatch.setattr(orch_mod, "_STATE_DIR", tmp_path)
    cfg = PipelineConfig(
        ticketing=_src(member_id="member-123"),
        repo_url="git@github.com:org/repo.git",
        workspace_dir=tmp_path / "ws",
        min_description_length=5,
        log_file=tmp_path / "p.log",
        log_level="INFO",
    )
    orch = PipelineOrchestrator(cfg)
    orch._ingest_filter_miss_now = AsyncMock(return_value="")
    orch._tracker_claim_now = AsyncMock(return_value=dict(_CLAIM))
    orch._provisioner.provision = AsyncMock()
    story = make_ticket(id=77)
    asyncio.run(orch.process_story(story))
    orch._provisioner.provision.assert_not_called()
    from backend.ticket_ingestion.state import load_processed_story_ids

    # Nothing recorded: the ticket is the teammate's, not done here.
    assert story.slug not in load_processed_story_ids(tmp_path)


def test_the_intake_listing_says_who_is_on_it(monkeypatch, tmp_path):
    import types

    from backend.ticket_ingestion import backfill
    from backend.ticket_ingestion import providers as providers_mod

    monkeypatch.setattr(ticket_start, "_REPO_ROOT", tmp_path)
    src = _src(id="sc", repo_url="acme/app", member_id="member-123")
    monkeypatch.setattr(
        ticket_start,
        "_load_config",
        lambda: types.SimpleNamespace(ticketing_sources=[src], repo_url="acme/app"),
    )
    mine = make_ticket(id=1, state_id="2")
    labelled = make_ticket(
        id=2, owner_ids=["bob"], owner_names=["Bob"], labels=["mindflock:working"]
    )
    started = make_ticket(id=3, owner_ids=["ann"], owner_names=["Ann"], state_id="2")
    prov = types.SimpleNamespace(
        label="Shortcut",
        search_assigned_all=AsyncMock(return_value=[mine, labelled, started]),
        list_states=AsyncMock(
            return_value=[{"id": "2", "name": "In Progress", "type": "started"}]
        ),
        claim_identity=AsyncMock(return_value="member-123"),
    )
    monkeypatch.setattr(providers_mod, "get_provider", lambda s: prov)

    async def _no_branches(repo):
        return set()

    monkeypatch.setattr(backfill, "_get_existing_branches", _no_branches)
    out = asyncio.run(ticket_start.list_assigned_tickets())
    rows = {t["id"]: t for t in out["tickets"]}
    assert "claimed_by" not in rows["1"]
    assert rows["2"]["claimed_by"] == {"name": "Bob", "why": "label"}
    assert rows["2"]["reasons"][0] == "Bob is on this"
    assert rows["2"]["eligible"] is False
    assert rows["3"]["claimed_by"] == {"name": "Ann", "why": "started"}
