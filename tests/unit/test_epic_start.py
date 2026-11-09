"""Epics (docs/epics.md): one lead session per epic, working only the
person's own open stories, in parallel where independent and stacked where
one blocks another."""

import asyncio
import types

import pytest

from backend.ticket_ingestion.providers import shortcut as SC
from backend.web.core import epic_start as E

ME = "me-1"


@pytest.fixture(autouse=True)
def _fresh_listing_cache():
    """The listing cache is module state: a failing test must not leak a
    cached listing into the next one."""
    E._LIST_CACHE.clear()
    yield
    E._LIST_CACHE.clear()


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
    out = asyncio.run(E.list_epics(force=True))
    (row,) = out["epics"]
    assert row["title"] == "sc-epic-500" and row["mine_open"] == 3
    assert (
        row["state"] == "On Deck" and row["eligible"] is True and row["auto"] is False
    )


def test_auto_start_only_on_auto_sources_and_once(wired, monkeypatch):
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
    assert asyncio.run(E.auto_start_once()) == []


# --------------------------------------------------------------------------- #
# Every epic I own is listed; ``epic_states`` only gates the AUTO start
# --------------------------------------------------------------------------- #
EPIC_WORKFLOW = [
    {"id": "10", "name": "To Do", "type": "unstarted"},
    {"id": "11", "name": "On Deck", "type": "unstarted"},
    {"id": "12", "name": "In Progress", "type": "started"},
    {"id": "13", "name": "Done", "type": "done"},
]


def _sc_provider(monkeypatch, epics, *, workflow_error=None, member_id=ME):
    p = SC.ShortcutProvider.__new__(SC.ShortcutProvider)
    p.cfg = types.SimpleNamespace(
        api_token="t", member_id=member_id, id="sc", provider="shortcut"
    )
    calls = []

    async def fake_get(path, params=None):
        calls.append(path)
        if path == "/epic-workflow":
            if workflow_error is not None:
                raise workflow_error
            return {"epic_states": [dict(s, id=int(s["id"])) for s in EPIC_WORKFLOW]}
        assert path == "/epics"
        return epics

    monkeypatch.setattr(p, "_get_json", fake_get)
    return p, calls


def _sc_epic(eid, state, *, owners=(ME,), archived=False, legacy=""):
    return {
        "id": eid,
        "name": "E%s" % eid,
        "app_url": "u/%s" % eid,
        "epic_state_id": state,
        "state": legacy,
        "owner_ids": list(owners),
        "archived": archived,
        "stats": {"num_stories_total": 2},
        "labels": [],
    }


def test_shortcut_list_epics_none_means_every_state_but_done(monkeypatch):
    p, _calls = _sc_provider(
        monkeypatch,
        [
            _sc_epic(1, 10),
            _sc_epic(2, 12),
            _sc_epic(3, 13),  # done-type: dropped
            _sc_epic(4, 11, archived=True),  # archived: dropped
            _sc_epic(5, 11, owners=("someone",)),  # not mine: dropped
            _sc_epic(6, 99, legacy="done"),  # unknown state, legacy "done"
            _sc_epic(7, 99, legacy="in progress"),
        ],
    )
    rows = asyncio.run(p.list_epics(None, mine=True))
    assert [r["id"] for r in rows] == ["1", "2", "7"]
    assert [r["state_type"] for r in rows] == ["unstarted", "started", "started"]
    # Without ``mine`` someone else's shows; done and archived still don't.
    assert [r["id"] for r in asyncio.run(p.list_epics(None, mine=False))] == [
        "1",
        "2",
        "5",
        "7",
    ]


def test_shortcut_list_epics_named_states_still_work(monkeypatch):
    p, _calls = _sc_provider(
        monkeypatch, [_sc_epic(1, 10), _sc_epic(2, 11), _sc_epic(3, 13)]
    )
    rows = asyncio.run(p.list_epics(["11", "13"]))
    assert [(r["id"], r["state_type"]) for r in rows] == [
        ("2", "unstarted"),
        ("3", "done"),  # a named done state is the caller's choice
    ]
    assert asyncio.run(p.list_epics([])) == []


class WorkflowProvider(FakeProvider):
    """Owns one epic per workflow state that ``list_epics(None)`` returns."""

    def __init__(self, epics, detail=DETAIL):
        super().__init__(detail)
        self.epics = epics
        self.asked = []

    async def list_epic_states(self):
        return [dict(s) for s in EPIC_WORKFLOW]

    async def list_epics(self, states, mine=True):
        self.asked.append(states)
        return [
            {
                "id": eid,
                "name": "E%s" % eid,
                "url": "u/%s" % eid,
                "state_id": sid,
                "state_type": "",
                "owner_ids": [ME],
                "stories_total": 3,
                "labels": [],
            }
            for eid, sid in self.epics
        ]


def _listing(monkeypatch, sources):
    monkeypatch.setattr(E, "_sources", lambda: sources)
    return asyncio.run(E.list_epics(force=True))


def test_every_owned_epic_is_listed_and_only_in_states_ones_auto_start(
    wired, monkeypatch
):
    prov = WorkflowProvider([("1", "10"), ("2", "11"), ("3", "12")])
    out = _listing(monkeypatch, [(_src(epic_states="11", epic_auto=True), prov)])
    assert prov.asked == [None]  # every not-done state, not just epic_states
    by = {r["id"]: r for r in out["epics"]}
    assert set(by) == {"1", "2", "3"}
    assert out["epics"][0]["id"] == "2"  # auto-start-state epics come first
    two, one, three = by["2"], by["1"], by["3"]
    assert two["in_states"] is True and two["eligible"] is True
    assert two["state"] == "On Deck" and two["state_type"] == "unstarted"
    assert two["state_rank"] == 1
    for row in (one, three):
        assert row["in_states"] is False and row["eligible"] is False
        assert row["reasons"] == ["not in an auto-start state"]
        assert row["startable"] is True and row["auto"] is True
    assert (three["state"], three["state_type"], three["state_rank"]) == (
        "In Progress",
        "started",
        2,
    )
    assert out["sources"] == [
        {
            "source": "sc",
            "source_label": "Allure",
            "epic_states": ["On Deck"],
            "auto": True,
        }
    ]


def test_story_reads_go_to_in_states_epics_first(wired, monkeypatch):
    epics = [(str(i), "10") for i in range(E.MAX_DETAIL)] + [("hot", "11")]
    prov = WorkflowProvider(epics)
    read = []

    async def detail(epic_id):
        read.append(epic_id)
        return DETAIL

    prov.epic_detail = detail
    out = _listing(monkeypatch, [(_src(epic_states="11"), prov)])
    assert read[0] == "hot" and len(read) == E.MAX_DETAIL
    by = {r["id"]: r for r in out["epics"]}
    assert by["hot"]["mine_open"] == 3
    assert by[str(E.MAX_DETAIL - 1)]["mine_open"] is None  # past the cap


def test_a_source_without_epic_states_lists_and_starts(monkeypatch, tmp_path):
    prov = WorkflowProvider([("500", "12")])
    src = _src(epic_states="", epic_auto=True)
    monkeypatch.setattr(E, "GetConfigDir", lambda: str(tmp_path))
    monkeypatch.setattr(E, "_has_session", lambda t: False)
    out = _listing(monkeypatch, [(src, prov)])
    (row,) = out["epics"]
    assert row["in_states"] is False and row["eligible"] is False
    assert row["startable"] is True
    assert out["sources"][0]["epic_states"] == []
    # The auto loop never starts it…
    launched = []

    async def fake_launch(source, epic_id, auto=False):
        launched.append(epic_id)
        return {"title": "sc-epic-%s" % epic_id}

    real_launch = E.launch
    monkeypatch.setattr(E, "launch", fake_launch)
    assert asyncio.run(E.auto_start_once()) == [] and launched == []
    # …but _source() resolves the source, so a person's Start reaches it
    # (it gets as far as the repo check — the fake source has none).
    monkeypatch.setattr(E, "launch", real_launch)
    assert E._source("sc") == (src, prov)
    with pytest.raises(E.EpicError) as err:
        asyncio.run(E.launch("sc", "500"))
    assert err.value.status == 400 and "no repository" in err.value.body["error"]


def test_a_source_with_no_epics_still_gets_a_heading(wired, monkeypatch):
    out = _listing(
        monkeypatch,
        [
            (_src(epic_states="11"), WorkflowProvider([("1", "11")])),
            (_src(id="sc2", label="", epic_states=""), WorkflowProvider([])),
        ],
    )
    assert [s["source"] for s in out["sources"]] == ["sc", "sc2"]
    assert out["sources"][1] == {
        "source": "sc2",
        "source_label": "Shortcut",
        "epic_states": [],
        "auto": False,
    }
    assert [r["source"] for r in out["epics"]] == ["sc"]


def test_auto_start_skips_out_of_state_epics(wired, monkeypatch):
    prov = WorkflowProvider([("1", "10"), ("2", "11")])
    monkeypatch.setattr(
        E, "_sources", lambda: [(_src(epic_states="11", epic_auto=True), prov)]
    )
    started = []

    async def fake_launch(source, epic_id, auto=False):
        started.append(epic_id)
        E._claim("sc-epic-%s" % epic_id, force=False)
        return {"title": "sc-epic-%s" % epic_id}

    monkeypatch.setattr(E, "launch", fake_launch)
    assert asyncio.run(E.auto_start_once()) == ["sc-epic-2"]
    assert started == ["2"]
    assert asyncio.run(E.auto_start_once()) == []  # claimed: not again
    assert not E._claim_path("sc-epic-1").exists()


# --------------------------------------------------------------------------- #
# ShortcutProvider.list_epics: the edges of "None = every state but done"
# --------------------------------------------------------------------------- #
def test_shortcut_list_epics_none_refuses_to_guess_without_the_workflow(
    monkeypatch,
):
    p, calls = _sc_provider(
        monkeypatch, [_sc_epic(1, 10)], workflow_error=RuntimeError("503")
    )
    with pytest.raises(RuntimeError):
        asyncio.run(p.list_epics(None))
    assert calls == ["/epic-workflow"]  # /epics never read


def test_shortcut_list_epics_named_states_survive_a_workflow_failure(monkeypatch):
    p, _calls = _sc_provider(
        monkeypatch,
        [_sc_epic(1, 11), _sc_epic(2, 11, legacy="in progress"), _sc_epic(3, 12)],
        workflow_error=RuntimeError("503"),
    )
    rows = asyncio.run(p.list_epics(["11"]))
    # Only state_type is lost (or comes from the legacy state when set).
    assert [(r["id"], r["state_type"]) for r in rows] == [
        ("1", ""),
        ("2", "started"),
    ]


def test_shortcut_list_epics_empty_names_read_nothing(monkeypatch):
    p, calls = _sc_provider(monkeypatch, [_sc_epic(1, 10)])
    assert asyncio.run(p.list_epics([])) == []
    assert asyncio.run(p.list_epics(["", "  "])) == []
    assert calls == []
    # None is NOT the same as an empty list: it means every not-done state.
    assert [r["id"] for r in asyncio.run(p.list_epics(None))] == ["1"]


def test_shortcut_list_epics_strips_named_state_ids(monkeypatch):
    p, _calls = _sc_provider(monkeypatch, [_sc_epic(1, 11), _sc_epic(2, 12)])
    rows = asyncio.run(p.list_epics([" 11 "]))
    assert [(r["id"], r["state_id"]) for r in rows] == [("1", "11")]


def test_shortcut_list_epics_unknown_state_falls_back_to_the_legacy_state(
    monkeypatch,
):
    p, _calls = _sc_provider(
        monkeypatch,
        [
            _sc_epic(1, 99, legacy="to do"),
            _sc_epic(2, 99, legacy="someday"),  # unrecognized: kept, type ""
            _sc_epic(3, 99),  # no legacy state: kept, type ""
        ],
    )
    rows = asyncio.run(p.list_epics(None))
    assert [(r["id"], r["state_type"]) for r in rows] == [
        ("1", "unstarted"),
        ("2", ""),
        ("3", ""),
    ]


def test_shortcut_list_epics_the_workflow_type_beats_the_legacy_state(monkeypatch):
    p, _calls = _sc_provider(
        monkeypatch,
        [
            _sc_epic(1, 13, legacy="in progress"),  # workflow says done
            _sc_epic(2, 12, legacy="done"),  # workflow says started
        ],
    )
    rows = asyncio.run(p.list_epics(None))
    assert [(r["id"], r["state_type"]) for r in rows] == [("2", "started")]


def test_shortcut_list_epics_int_state_ids_match_and_carry_a_type(monkeypatch):
    p, _calls = _sc_provider(
        monkeypatch, [_sc_epic(1, 10), _sc_epic(2, 11), _sc_epic(3, 12)]
    )
    rows = asyncio.run(p.list_epics(None))
    assert [(r["state_id"], r["state_type"]) for r in rows] == [
        ("10", "unstarted"),
        ("11", "unstarted"),
        ("12", "started"),
    ]
    assert [r["id"] for r in asyncio.run(p.list_epics([12]))] == ["3"]


def test_shortcut_list_epics_mine_without_a_member_id_is_empty(monkeypatch):
    p, _calls = _sc_provider(
        monkeypatch,
        [_sc_epic(1, 10), _sc_epic(2, 10, owners=("someone",))],
        member_id="",
    )
    assert asyncio.run(p.list_epics(None, mine=True)) == []
    assert [r["id"] for r in asyncio.run(p.list_epics(None, mine=False))] == [
        "1",
        "2",
    ]


# --------------------------------------------------------------------------- #
# The listing: errors, names, order, the story-read cap, the reasons
# --------------------------------------------------------------------------- #
def test_auto_states_parse_the_comma_list():
    assert E._auto_states(_src(epic_states="a, b,,c ")) == ["a", "b", "c"]
    assert E._auto_states(_src(epic_states="")) == []
    assert E._auto_states(_src(epic_states=None)) == []
    assert E._auto_states(types.SimpleNamespace()) == []


def test_a_source_whose_workflow_fails_is_an_error_and_still_a_heading(
    wired, monkeypatch
):
    broken = WorkflowProvider([("9", "10")])

    async def boom():
        raise RuntimeError("workflow down")

    broken.list_epic_states = boom
    out = _listing(
        monkeypatch,
        [
            (_src(id="bad", epic_states="11"), broken),
            (_src(epic_states="11"), WorkflowProvider([("1", "11")])),
        ],
    )
    assert out["errors"] == [{"source": "bad", "error": "workflow down"}]
    # The heading was added before the read: it stays, with raw state ids.
    assert [s["source"] for s in out["sources"]] == ["bad", "sc"]
    assert out["sources"][0]["epic_states"] == ["11"]
    assert [(r["source"], r["id"]) for r in out["epics"]] == [("sc", "1")]


def test_unknown_state_ids_show_raw_and_sort_last(wired, monkeypatch):
    prov = WorkflowProvider([("x", "77"), ("1", "12"), ("2", "10")])
    out = _listing(monkeypatch, [(_src(epic_states="11, 42"), prov)])
    assert out["sources"][0]["epic_states"] == ["On Deck", "42"]
    assert [r["id"] for r in out["epics"]] == ["2", "1", "x"]  # workflow order
    x = out["epics"][-1]
    assert x["state"] == "77" and x["state_rank"] == len(EPIC_WORKFLOW)
    assert x["state_type"] == ""


def test_an_in_state_epic_past_the_story_cap_is_still_eligible(wired, monkeypatch):
    epics = [(str(i), "11") for i in range(E.MAX_DETAIL + 1)]
    out = _listing(
        monkeypatch,
        [(_src(epic_states="11", epic_auto=True), WorkflowProvider(epics))],
    )
    rows = out["epics"]
    assert [r["mine_open"] for r in rows[: E.MAX_DETAIL]] == [3] * E.MAX_DETAIL
    last = rows[E.MAX_DETAIL]
    # Unread stories don't block: only a known ``mine_open == 0`` does.
    assert last["mine_open"] is None
    assert last["eligible"] is True and last["startable"] is True


def test_reasons_eligible_and_startable(wired, monkeypatch):
    empty = dict(DETAIL, stories=[_story("4", "x", mine=False)])
    prov = WorkflowProvider([("1", "10"), ("2", "11")], detail=empty)
    prov.labels = ()
    monkeypatch.setattr(E, "_has_session", lambda t: t == "sc-epic-1")
    out = _listing(
        monkeypatch,
        [(_src(epic_states="11", ingest_labels="mindflock, agent"), prov)],
    )
    by = {r["id"]: r for r in out["epics"]}
    # Everything wrong at once: every reason, in order; neither start works.
    assert by["1"]["reasons"] == [
        "not in an auto-start state",
        "a lead session is already working it",
        "no stories of yours left to do in it",
        "no auto-start label (mindflock, agent)",
    ]
    assert by["1"]["eligible"] is False and by["1"]["startable"] is False
    # In state, not taken, but nothing of mine left: no Start either.
    assert by["2"]["reasons"] == [
        "no stories of yours left to do in it",
        "no auto-start label (mindflock, agent)",
    ]
    assert by["2"]["startable"] is False


def test_startable_ignores_state_and_labels_but_not_a_session(wired, monkeypatch):
    prov = WorkflowProvider([("1", "10"), ("2", "10")])
    monkeypatch.setattr(E, "_has_session", lambda t: t == "sc-epic-2")
    out = _listing(
        monkeypatch, [(_src(epic_states="11", ingest_labels="mindflock"), prov)]
    )
    by = {r["id"]: r for r in out["epics"]}
    assert by["1"]["eligible"] is False and by["1"]["startable"] is True
    assert by["2"]["has_session"] is True and by["2"]["startable"] is False


def test_auto_start_only_launches_the_auto_sources_in_state_epics(wired, monkeypatch):
    a = WorkflowProvider([("1", "11"), ("2", "10")])
    b = WorkflowProvider([("3", "11")])
    monkeypatch.setattr(
        E,
        "_sources",
        lambda: [
            (_src(id="a", epic_states="11", epic_auto=True), a),
            (_src(id="b", epic_states="", epic_auto=False), b),
        ],
    )
    launched = []

    async def fake_launch(source, epic_id, auto=False):
        launched.append((source, epic_id, auto))
        return {"title": "sc-epic-%s" % epic_id}

    monkeypatch.setattr(E, "launch", fake_launch)
    assert asyncio.run(E.auto_start_once()) == ["sc-epic-1"]
    assert launched == [("a", "1", True)]
    # B can't auto-start anything, so the auto pass doesn't even read it.
    assert a.asked == [None] and b.asked == []


class _StopLoop(Exception):
    pass


def _one_loop_pass(monkeypatch, sources):
    calls = []

    async def fake_once():
        calls.append(1)
        return []

    async def stop(_s):
        raise _StopLoop

    monkeypatch.setattr(E, "_sources", lambda: sources)
    monkeypatch.setattr(E, "auto_start_once", fake_once)
    monkeypatch.setattr(E, "asyncio", types.SimpleNamespace(sleep=stop))
    with pytest.raises(_StopLoop):
        asyncio.run(E.run_loop())
    return calls


def test_the_loop_idles_while_no_source_auto_starts(monkeypatch):
    prov = WorkflowProvider([("1", "11")])
    # A source with no epic_states is listed now, but it doesn't wake the loop.
    sources = [(_src(epic_states="", epic_auto=False), prov)]
    assert _one_loop_pass(monkeypatch, sources) == []
    sources.append((_src(id="sc2", epic_auto=True), prov))
    assert _one_loop_pass(monkeypatch, sources) == [1]


def test_a_done_state_in_epic_states_no_longer_lists_or_starts(monkeypatch):
    """Behavior change: the listing always asks for every NOT-done state, so
    a done-type state named in ``epic_states`` matches nothing."""
    p, _calls = _sc_provider(monkeypatch, [_sc_epic(1, 13), _sc_epic(2, 11)])
    monkeypatch.setattr(p, "make_slug", lambda x: "sc-%s" % x)

    async def detail(epic_id):
        return DETAIL

    monkeypatch.setattr(p, "epic_detail", detail)
    monkeypatch.setattr(E, "_has_session", lambda t: False)
    src = _src(epic_states="13", epic_auto=True)
    out = _listing(monkeypatch, [(src, p)])
    assert out["sources"][0]["epic_states"] == ["Done"]
    assert [(r["id"], r["in_states"]) for r in out["epics"]] == [("2", False)]
    monkeypatch.setattr(E, "launch", pytest.fail)
    assert asyncio.run(E.auto_start_once()) == []


def test_the_cached_listing_keeps_its_shape(wired, monkeypatch):
    prov = WorkflowProvider([("1", "11")])
    monkeypatch.setattr(E, "_sources", lambda: [(_src(epic_states="11"), prov)])
    first = asyncio.run(E.list_epics(force=True))
    assert set(first) == {"epics", "sources", "errors"}
    prov.epics = []  # a fresh read would differ
    assert asyncio.run(E.list_epics()) is first
    assert prov.asked == [None]
    assert asyncio.run(E.list_epics(force=True))["epics"] == []


# --------------------------------------------------------------------------- #
# The HTTP routes
# --------------------------------------------------------------------------- #
@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from backend.web import server

    return TestClient(server.app)


def test_get_epics_returns_rows_sources_and_errors(client, wired, monkeypatch):
    monkeypatch.setattr(
        E,
        "_sources",
        lambda: [(_src(epic_states="11"), WorkflowProvider([("1", "11")]))],
    )
    r = client.get("/api/epics?refresh=1")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"epics", "sources", "errors"}
    (row,) = body["epics"]
    assert {"state_type", "state_rank", "in_states"} <= set(row)
    assert (row["state_type"], row["state_rank"], row["in_states"]) == (
        "unstarted",
        1,
        True,
    )
    assert body["sources"][0]["epic_states"] == ["On Deck"]


def test_get_epics_refresh_forces_a_fresh_read(client, monkeypatch):
    seen = []

    async def fake_list(force=False):
        seen.append(force)
        return {"epics": [], "sources": [], "errors": []}

    monkeypatch.setattr(E, "list_epics", fake_list)
    client.get("/api/epics")
    client.get("/api/epics?refresh=1")
    assert seen == [False, True]


def test_post_start_works_on_a_source_without_epic_states(
    client, monkeypatch, tmp_path
):
    from backend.web import server

    repo = types.SimpleNamespace(url="https://example.invalid/r.git")
    src = _src(epic_states="", repo_targets=lambda: [repo])
    monkeypatch.setattr(E, "_sources", lambda: [(src, WorkflowProvider([]))])
    monkeypatch.setattr(E, "GetConfigDir", lambda: str(tmp_path))
    monkeypatch.setattr(E, "_has_session", lambda t: False)
    pending = []
    monkeypatch.setattr(server, "_pending_add", lambda t, *a, **k: pending.append(t))
    monkeypatch.setattr(server, "_register_task", lambda coro: coro.close())
    r = client.post("/api/epics/start", json={"source": "sc", "id": "500"})
    assert r.status_code == 202, r.text
    assert r.json()["title"] == "sc-epic-500" and r.json()["stories"] == 3
    assert pending == ["sc-epic-500"]
    r = client.post("/api/epics/start", json={"source": "nope", "id": "500"})
    assert r.status_code == 404


def test_the_auto_pass_never_caches_its_partial_listing(wired, monkeypatch):
    monkeypatch.setattr(
        E,
        "_sources",
        lambda: [
            (_src(epic_states="11", epic_auto=True), WorkflowProvider([("1", "10")])),
            (_src(id="b", epic_states=""), WorkflowProvider([("2", "11")])),
        ],
    )
    part = asyncio.run(E.list_epics(force=True, auto_only=True))
    assert [s["source"] for s in part["sources"]] == ["sc"]
    assert "all" not in E._LIST_CACHE
    full = asyncio.run(E.list_epics())
    assert [s["source"] for s in full["sources"]] == ["sc", "b"]
