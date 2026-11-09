"""Epics (docs/epics.md): one lead session per epic, working only the
person's own open stories, in parallel where independent and stacked where
one blocks another."""

import asyncio
import types

import pytest

from backend.ticket_ingestion.providers import shortcut as SC
from backend.web.core import epic_start as E

ME = "me-1"


def _story(
    sid,
    name,
    *,
    mine=True,
    state_type="unstarted",
    blocks=(),
    blocked_by=(),
    relates=(),
):
    return {
        "id": sid,
        "slug": "sc-%s" % sid,
        "name": name,
        "url": "https://app.shortcut.com/x/story/%s" % sid,
        "state_id": "1",
        "state": "Ready",
        "state_type": state_type,
        "owner_ids": [ME] if mine else ["someone"],
        "mine": mine,
        "blocks": list(blocks),
        "blocked_by": list(blocked_by),
        "relates": list(relates),
    }


DETAIL = {
    "id": "500",
    "name": "Google Maps poisoning",
    "url": "https://app.shortcut.com/x/epic/500",
    "description": "Detect poisoned listings.",
    "state_id": "on-deck",
    "stories": [
        _story("1", "Schema for listings", blocks=["2"]),
        _story("2", "Scanner uses the schema", blocked_by=["1"]),
        _story("3", "Alert copy", relates=["2"]),
        _story("4", "Someone else's", mine=False),
        _story("5", "Already shipped", state_type="done"),
    ],
}


class FakeProvider:
    label = "Shortcut"
    can_epics = True

    labels = ("MindFlock",)

    def __init__(self, detail=DETAIL):
        self.detail = detail
        self.moved = []

    def make_slug(self, x):
        return "sc-%s" % x

    async def list_epic_states(self):
        return [{"id": "on-deck", "name": "On Deck", "type": "unstarted"}]

    async def list_epics(self, states, mine=True):
        return [
            {
                "id": "500",
                "name": DETAIL["name"],
                "url": DETAIL["url"],
                "state_id": "on-deck",
                "owner_ids": [ME],
                "stories_total": 5,
                "labels": list(self.labels),
            }
        ]

    async def epic_detail(self, epic_id):
        return self.detail

    async def set_epic_state(self, epic_id, state_id):
        self.moved.append((epic_id, state_id))


def _src(**kw):
    base = dict(
        id="sc",
        provider="shortcut",
        label="Allure",
        epic_states="on-deck",
        epic_auto=False,
        epic_start_state="",
        agent="",
    )
    base.update(kw)
    return types.SimpleNamespace(**base)


# --------------------------------------------------------------------------- #
# Only the person's own, open stories
# --------------------------------------------------------------------------- #
def test_open_stories_are_mine_and_not_done():
    mine, others = E.open_stories(DETAIL)
    assert [s["id"] for s in mine] == ["1", "2", "3"]
    assert [s["id"] for s in others] == ["4", "5"]


def test_the_lead_prompt_orders_and_splits_the_work():
    mine, others = E.open_stories(DETAIL)
    p = E.build_lead_prompt(DETAIL, "sc", "staging", mine, others)
    assert "# Epic: Google Maps poisoning" in p
    # Its own stories, with the dependency the links give.
    assert "**sc-1** — Schema for listings" in p and "blocks sc-2" in p
    assert "**sc-2** — Scanner uses the schema" in p and "blocked by sc-1" in p
    # Someone else's are context only.
    assert "context only" in p and "Someone else's" in p
    # One worker per story, pushed not PR'd; PRs stacked on the predecessor.
    assert 'spawn_ticket_session(ticket="sc-<id>", source="sc", autopilot="push"' in p
    assert "git reset --hard origin/<predecessor's branch>" in p
    assert 'ship_session(title=<worker>, depth="pr"' in p and '"staging"' in p
    assert "Never merge a PR" in p and "never push to staging" in p
    assert "Detect poisoned listings." in p


# --------------------------------------------------------------------------- #
# The provider reads Shortcut's links into blocks / blocked_by / relates
# --------------------------------------------------------------------------- #
def test_shortcut_epic_detail_reads_story_links(monkeypatch):
    cfg = types.SimpleNamespace(
        api_token="t", member_id=ME, id="sc", provider="shortcut"
    )
    p = SC.ShortcutProvider.__new__(SC.ShortcutProvider)
    p.cfg = cfg

    async def fake_get(path, params=None):
        if path == "/epics/500":
            return {
                "id": 500,
                "name": "E",
                "app_url": "u",
                "epic_state_id": 7,
                "description": "d",
            }
        return [
            {
                "id": 1,
                "name": "A",
                "owner_ids": [ME],
                "workflow_state_id": 11,
                "story_links": [{"verb": "blocks", "subject_id": 1, "object_id": 2}],
            },
            {
                "id": 2,
                "name": "B",
                "owner_ids": ["x"],
                "workflow_state_id": 12,
                "story_links": [
                    {"verb": "blocks", "subject_id": 1, "object_id": 2},
                    {"verb": "relates to", "subject_id": 2, "object_id": 3},
                ],
            },
            {"id": 9, "name": "gone", "archived": True},
        ]

    async def fake_states():
        return [
            {"id": "11", "name": "Ready", "label": "Ready", "type": "unstarted"},
            {"id": "12", "name": "Done", "label": "Done", "type": "done"},
        ]

    monkeypatch.setattr(p, "_get_json", fake_get)
    monkeypatch.setattr(p, "list_states", fake_states)
    monkeypatch.setattr(p, "make_slug", lambda x: "sc-%s" % x)
    d = asyncio.run(p.epic_detail("500"))
    a, b = d["stories"]
    assert len(d["stories"]) == 2  # archived dropped
    assert a["mine"] and a["blocks"] == ["2"] and a["state_type"] == "unstarted"
    assert not b["mine"] and b["blocked_by"] == ["1"] and b["relates"] == ["3"]
    assert b["state_type"] == "done"


# --------------------------------------------------------------------------- #
# Starting: refusals and the one-lead-per-epic claim
# --------------------------------------------------------------------------- #
@pytest.fixture
def wired(monkeypatch, tmp_path):
    prov = FakeProvider()
    monkeypatch.setattr(E, "_sources", lambda: [(_src(), prov)])
    monkeypatch.setattr(E, "GetConfigDir", lambda: str(tmp_path))
    monkeypatch.setattr(E, "_has_session", lambda t: False)
    return prov


def test_an_unknown_source_or_bad_id_is_refused(wired):
    with pytest.raises(E.EpicError) as err:
        asyncio.run(E.launch("nope", "500"))
    assert err.value.status == 404
    with pytest.raises(E.EpicError) as err:
        asyncio.run(E.launch("sc", "abc"))
    assert err.value.status == 400


def test_an_epic_with_nothing_of_mine_left_is_refused(wired):
    wired.detail = dict(
        DETAIL,
        stories=[_story("4", "x", mine=False), _story("5", "y", state_type="done")],
    )
    with pytest.raises(E.EpicError) as err:
        asyncio.run(E.launch("sc", "500"))
    assert err.value.status == 409 and "no stories of yours" in err.value.body["error"]


def test_a_working_lead_is_refused(wired, monkeypatch):
    monkeypatch.setattr(E, "_has_session", lambda t: t == "sc-epic-500")
    with pytest.raises(E.EpicError) as err:
        asyncio.run(E.launch("sc", "500"))
    assert err.value.status == 409 and err.value.body["title"] == "sc-epic-500"


def test_the_claim_is_exclusive_but_a_person_can_take_over(wired):
    assert E._claim("sc-epic-500", force=False) is True
    assert E._claim("sc-epic-500", force=False) is False  # the other server
    assert E._claim("sc-epic-500", force=True) is True  # a person's Start


def test_titles_and_branches_never_collide_with_stories():
    p = FakeProvider()
    assert E.title_for(p, "500") == "sc-epic-500"
    assert (
        E.branch_for(p, "500", "Google Maps: poisoning!")
        == "feature/sc-epic-500/google-maps-poisoning"
    )


# --------------------------------------------------------------------------- #
# The listing
# --------------------------------------------------------------------------- #
def test_the_listing_counts_my_open_stories(wired):
    E._LIST_CACHE.clear()
    out = asyncio.run(E.list_epics(force=True))
    (row,) = out["epics"]
    assert row["title"] == "sc-epic-500" and row["mine_open"] == 3
    assert (
        row["state"] == "On Deck" and row["eligible"] is True and row["auto"] is False
    )


def test_auto_start_only_on_auto_sources_and_once(wired, monkeypatch):
    E._LIST_CACHE.clear()
    started = []

    async def fake_launch(source, epic_id, auto=False):
        started.append((source, epic_id, auto))
        E._claim("sc-epic-%s" % epic_id, force=False)
        return {"title": "sc-epic-%s" % epic_id}

    monkeypatch.setattr(E, "launch", fake_launch)
    assert asyncio.run(E.auto_start_once()) == []  # epic_auto off
    monkeypatch.setattr(E, "_sources", lambda: [(_src(epic_auto=True), wired)])
    assert asyncio.run(E.auto_start_once()) == ["sc-epic-500"]
    assert started == [("sc", "500", True)]
    # Claimed once: a finished lead is never restarted by the loop.
    assert asyncio.run(E.auto_start_once()) == []


# --------------------------------------------------------------------------- #
# The source's Auto-start labels gate epics like they gate stories
# --------------------------------------------------------------------------- #
def _row(monkeypatch, wired, ingest_labels):
    monkeypatch.setattr(
        E,
        "_sources",
        lambda: [(_src(epic_auto=True, ingest_labels=ingest_labels), wired)],
    )
    E._LIST_CACHE.clear()
    (row,) = asyncio.run(E.list_epics(force=True))["epics"]
    return row


def test_no_labels_set_means_no_label_gate(wired, monkeypatch):
    row = _row(monkeypatch, wired, "")
    assert row["eligible"] is True and row["startable"] is True


def test_a_matching_label_any_case_lets_it_auto_start(wired, monkeypatch):
    row = _row(monkeypatch, wired, "needs-agent, mindflock")
    assert row["labels"] == ["MindFlock"]
    assert row["eligible"] is True


def test_a_missing_label_stops_only_the_auto_start(wired, monkeypatch):
    wired.labels = ("backend",)
    row = _row(monkeypatch, wired, "mindflock")
    assert row["eligible"] is False
    assert row["reasons"] == ["no auto-start label (mindflock)"]
    assert row["startable"] is True  # a person's Start still works
    E._LIST_CACHE.clear()
    assert asyncio.run(E.auto_start_once()) == []
