"""Unit tests for the ticket-provider adapters and registry.

Focus on the pure transforms (native API JSON -> normalized Ticket), slug
generation, and the registry — no network. The Shortcut adapter's parity with
the historic behaviour is covered by test_backfill.py.
"""

import logging
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from backend.ticket_ingestion.config import TicketProviderConfig
from backend.ticket_ingestion.models import Ticket
from backend.ticket_ingestion.providers import (
    PROVIDER_META,
    PROVIDER_REGISTRY,
    ProviderError,
    get_provider,
)
from backend.ticket_ingestion.providers.asana import AsanaProvider
from backend.ticket_ingestion.providers.base import (
    extract_link_attachments,
    ingests_any_assignee,
    parse_acceptance_criteria,
    workflow_state_list,
)
from backend.ticket_ingestion.providers.github_issues import GithubIssuesProvider
from backend.ticket_ingestion.providers.jira import JiraProvider, flatten_adf
from backend.ticket_ingestion.providers.linear import LinearProvider
from backend.ticket_ingestion.providers.shortcut import (
    ShortcutProvider,
    story_from_api_response,
)


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #
def test_registry_covers_all_providers():
    assert set(PROVIDER_REGISTRY) == {
        "shortcut",
        "jira",
        "linear",
        "github_issues",
        "asana",
    }


def test_get_provider_returns_instance():
    p = get_provider(TicketProviderConfig(provider="jira"))
    assert isinstance(p, JiraProvider)


def test_get_provider_unknown_raises():
    with pytest.raises(ProviderError):
        get_provider(TicketProviderConfig(provider="bugzilla"))


def test_get_provider_defaults_to_shortcut():
    assert isinstance(get_provider(TicketProviderConfig(provider="")), ShortcutProvider)


def test_provider_meta_shape():
    ids = {m["id"] for m in PROVIDER_META}
    assert ids == set(PROVIDER_REGISTRY)
    for meta in PROVIDER_META:
        assert meta["label"] and meta["blurb"]
        for f in meta["fields"]:
            assert {"key", "label", "secret"} <= set(f)
            # every field key maps onto a TicketProviderConfig attribute
            assert hasattr(TicketProviderConfig(), f["key"])


# --------------------------------------------------------------------------- #
# Slug generation (branch/session handle, globally unique per provider)
# --------------------------------------------------------------------------- #
def test_slugs_are_provider_scoped():
    assert ShortcutProvider(TicketProviderConfig()).make_slug(123) == "sc-123"
    assert JiraProvider(TicketProviderConfig()).make_slug("PROJ-1") == "jira-PROJ-1"
    assert LinearProvider(TicketProviderConfig()).make_slug("ENG-5") == "lin-ENG-5"
    assert GithubIssuesProvider(TicketProviderConfig()).make_slug(42) == "gh-42"
    assert AsanaProvider(TicketProviderConfig()).make_slug("120345") == "asana-120345"


def test_slug_sanitizes_unsafe_chars():
    # A key with slashes/spaces collapses to dashes (branch-safe).
    assert JiraProvider(TicketProviderConfig()).make_slug("A B/C") == "jira-A-B-C"


# --------------------------------------------------------------------------- #
# Shortcut parity
# --------------------------------------------------------------------------- #
def test_shortcut_story_keeps_int_id_and_sc_slug():
    t = story_from_api_response(
        {
            "id": 777,
            "name": "X",
            "description": "- do it",
            "created_at": "2025-01-01T00:00:00Z",
        }
    )
    assert t.id == 777 and t.slug == "sc-777" and t.provider == "shortcut"
    assert t.acceptance_criteria == ["do it"]


# --------------------------------------------------------------------------- #
# Jira
# --------------------------------------------------------------------------- #
def test_flatten_adf_preserves_bullets_and_paragraphs():
    doc = {
        "type": "doc",
        "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": "Intro line"}]},
            {
                "type": "bulletList",
                "content": [
                    {
                        "type": "listItem",
                        "content": [
                            {
                                "type": "paragraph",
                                "content": [{"type": "text", "text": "one"}],
                            }
                        ],
                    },
                    {
                        "type": "listItem",
                        "content": [
                            {
                                "type": "paragraph",
                                "content": [{"type": "text", "text": "two"}],
                            }
                        ],
                    },
                ],
            },
        ],
    }
    text = flatten_adf(doc)
    assert "Intro line" in text
    assert "- one" in text and "- two" in text


def test_flatten_adf_emits_markdown_heading_markers():
    # ADF headings must come out as real markdown headings: parse_acceptance_criteria
    # only recognizes an AC section from a "^#+ acceptance criteria$" LINE, so a
    # heading flattened to bare text silently disables the AC branch.
    def _heading(level, text):
        node = {"type": "heading", "content": [{"type": "text", "text": text}]}
        if level is not None:
            node["attrs"] = {"level": level}
        return node

    assert flatten_adf(_heading(1, "Background")) == "# Background\n"
    assert (
        flatten_adf(_heading(3, "Acceptance Criteria")) == "### Acceptance Criteria\n"
    )
    # Missing / out-of-range / non-numeric level still yields markers (degrading
    # to a marker-less line would break the miner, not just the rendering).
    assert flatten_adf(_heading(None, "No attrs")) == "# No attrs\n"
    assert flatten_adf(_heading(99, "Clamped")) == "###### Clamped\n"
    assert flatten_adf(_heading("2", "Stringy")) == "## Stringy\n"
    # The miner's pattern is end-anchored, so no trailing whitespace may leak in.
    assert flatten_adf(_heading(2, "  Acceptance Criteria  ")) == (
        "## Acceptance Criteria\n"
    )
    assert parse_acceptance_criteria(
        flatten_adf(_heading(2, "Acceptance Criteria")) + "- a\n"
    ) == ["a"]


def test_jira_issue_to_ticket():
    cfg = TicketProviderConfig(
        provider="jira",
        base_url="https://acme.atlassian.net",
        email="me@acme.com",
        api_token="tok",
    )
    prov = JiraProvider(cfg)

    def _bullets(*texts):
        return {
            "type": "bulletList",
            "content": [
                {
                    "type": "listItem",
                    "content": [
                        {"type": "paragraph", "content": [{"type": "text", "text": t}]}
                    ],
                }
                for t in texts
            ],
        }

    issue = {
        "key": "PROJ-42",
        "fields": {
            "summary": "Fix the thing",
            # Two bullet lists under two headings: only the ones under
            # "Acceptance Criteria" are criteria. This is what distinguishes the
            # miner's AC-section branch from its "every top-level bullet in the
            # description" fallback — a single-bullet description cannot.
            "description": {
                "type": "doc",
                "content": [
                    {
                        "type": "heading",
                        "attrs": {"level": 2},
                        "content": [{"type": "text", "text": "Background"}],
                    },
                    _bullets("legacy importer is slow", "reported by two customers"),
                    {
                        "type": "heading",
                        "attrs": {"level": 2},
                        "content": [{"type": "text", "text": "Acceptance Criteria"}],
                    },
                    _bullets("works", "no regressions"),
                ],
            },
            "status": {
                "name": "In Progress",
                "statusCategory": {"key": "indeterminate"},
            },
            "assignee": {"accountId": "acc-1"},
            "created": "2025-02-03T10:00:00.000+0000",
            "comment": {
                "comments": [
                    {
                        "author": {"displayName": "Al"},
                        "created": "2025-02-04",
                        "body": {
                            "type": "doc",
                            "content": [
                                {
                                    "type": "paragraph",
                                    "content": [{"type": "text", "text": "a note"}],
                                }
                            ],
                        },
                    }
                ]
            },
            "attachment": [
                {
                    "filename": "log.txt",
                    "content": "https://acme.atlassian.net/rest/attachment/1",
                    "mimeType": "text/plain",
                }
            ],
        },
    }
    t = prov._issue_to_ticket(issue)
    assert t.id == "PROJ-42" and t.slug == "jira-PROJ-42"
    assert t.name == "Fix the thing"
    assert t.source_label == "Jira"
    # ONLY the Acceptance Criteria bullets — the Background bullets are context.
    assert t.acceptance_criteria == ["works", "no regressions"]
    assert "## Acceptance Criteria" in t.description  # markers survived the ADF
    assert t.state == "In Progress"  # bucket for the assigned-tickets panel
    assert t.owner_ids == ["acc-1"]
    assert t.app_url == "https://acme.atlassian.net/browse/PROJ-42"
    assert t.comments and "a note" in t.comments[0]
    assert t.attachments and t.attachments[0].auth_headers.get(
        "Authorization", ""
    ).startswith("Basic ")


# --------------------------------------------------------------------------- #
# Linear
# --------------------------------------------------------------------------- #
def test_linear_issue_to_ticket():
    prov = LinearProvider(TicketProviderConfig(provider="linear", api_token="k"))
    issue = {
        "identifier": "ENG-9",
        "title": "Ship it",
        "description": "## Acceptance Criteria\n- green build",
        "url": "https://linear.app/acme/issue/ENG-9",
        "createdAt": "2025-03-01T00:00:00.000Z",
        "assignee": {"id": "user-1", "name": "Bo"},
        "comments": {
            "nodes": [
                {"body": "lgtm", "user": {"name": "Bo"}, "createdAt": "2025-03-02"}
            ]
        },
        "attachments": {"nodes": [{"url": "https://x/y.png", "title": "shot"}]},
    }
    t = prov._issue_to_ticket(issue)
    assert t.id == "ENG-9" and t.slug == "lin-ENG-9" and t.provider == "linear"
    assert t.acceptance_criteria == ["green build"]
    assert t.owner_ids == ["user-1"]
    assert t.app_url.endswith("/ENG-9")
    assert t.comments and "lgtm" in t.comments[0]
    assert t.attachments[0].url == "https://x/y.png"


# --------------------------------------------------------------------------- #
# GitHub Issues
# --------------------------------------------------------------------------- #
def test_github_repo_parsing_and_validation():
    prov = GithubIssuesProvider(
        TicketProviderConfig(provider="github_issues", project="octo/repo")
    )
    assert prov._repo() == ("octo", "repo")
    bad = GithubIssuesProvider(
        TicketProviderConfig(provider="github_issues", project="nope")
    )
    with pytest.raises(ProviderError):
        bad._repo()


async def test_github_issue_to_ticket(monkeypatch):
    prov = GithubIssuesProvider(
        TicketProviderConfig(provider="github_issues", project="octo/repo")
    )
    issue = {
        "number": 15,
        "title": "Bug",
        "body": "## Acceptance Criteria\n- no crash",
        "html_url": "https://github.com/octo/repo/issues/15",
        "created_at": "2025-04-01T00:00:00Z",
        "assignees": [{"login": "dev"}],
        "comments": 0,
    }
    t = await prov._issue_to_ticket(session=None, headers={}, issue=issue)
    assert t.id == 15 and t.slug == "gh-15" and t.provider == "github_issues"
    assert t.acceptance_criteria == ["no crash"]
    assert t.owner_ids == ["dev"]
    assert t.source_label == "GitHub"


# --------------------------------------------------------------------------- #
# Asana
# --------------------------------------------------------------------------- #
async def test_asana_task_to_ticket(monkeypatch):
    prov = AsanaProvider(
        TicketProviderConfig(provider="asana", api_token="pat", project="ws1")
    )
    # Avoid network for comments/attachments sub-fetches.
    monkeypatch.setattr(prov, "_comments", lambda *a, **k: _acoro([]))
    monkeypatch.setattr(prov, "_attachments", lambda *a, **k: _acoro([]))
    task = {
        "gid": "1200999",
        "name": "Do work",
        "notes": "## Acceptance Criteria\n- done",
        "permalink_url": "https://app.asana.com/0/1/1200999",
        "assignee": {"gid": "u1"},
        "created_at": "2025-05-01T00:00:00.000Z",
    }
    t = await prov._task_to_ticket(session=None, task=task)
    assert t.id == "1200999" and t.slug == "asana-1200999" and t.provider == "asana"
    assert t.acceptance_criteria == ["done"]
    assert t.owner_ids == ["u1"]


async def _acoro(value):
    return value


# --------------------------------------------------------------------------- #
# Shared AC parser sanity across markdown providers
# --------------------------------------------------------------------------- #
def test_parse_acceptance_criteria_shared():
    assert parse_acceptance_criteria("## Acceptance Criteria\n- a\n- b") == ["a", "b"]


# --------------------------------------------------------------------------- #
# Fake aiohttp session/response (drives the providers' HTTP paths, no network)
# --------------------------------------------------------------------------- #
class _FakeResp:
    """Stand-in for an aiohttp response used as an async context manager."""

    def __init__(self, status=200, json_data=None, text_data="", headers=None):
        self.status = status
        self._json = json_data
        self._text = text_data
        self.headers = headers or {}

    async def json(self):
        return self._json

    async def text(self):
        return self._text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class _FakeSession:
    """Stand-in for aiohttp.ClientSession. Responses are consumed in order;
    calls are recorded (url, kwargs) for assertions."""

    def __init__(self, get_responses=None, post_responses=None):
        self._get = list(get_responses or [])
        self._post = list(post_responses or [])
        self.get_calls = []
        self.post_calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def get(self, url, **kwargs):
        self.get_calls.append((url, kwargs))
        return self._get.pop(0)

    def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs))
        return self._post.pop(0)


def _patch_session(session):
    """Patch aiohttp.ClientSession so ``async with aiohttp.ClientSession()``
    yields ``session``."""
    return patch("aiohttp.ClientSession", return_value=session)


_SINCE = datetime(2025, 1, 1, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# base.extract_link_attachments (shared link mining across all providers)
# --------------------------------------------------------------------------- #
def test_extract_link_attachments_file_extension():
    atts = extract_link_attachments(["see http://ex.com/path/pic.png please"])
    assert len(atts) == 1
    assert atts[0].url == "http://ex.com/path/pic.png"
    assert atts[0].name == "pic.png"  # last path segment
    assert atts[0].auth_headers == {}


def test_extract_link_attachments_hosted_gets_headers_nonfile_skipped():
    atts = extract_link_attachments(
        ["hosted http://host/secret and plain http://other.com/page"],
        is_hosted=lambda u: "host/secret" in u,
        hosted_headers=lambda u: {"Authorization": "tok"},
    )
    # The hosted URL is captured (even with no file extension) and carries the
    # auth headers; the non-file, non-hosted URL is skipped.
    assert [a.url for a in atts] == ["http://host/secret"]
    assert atts[0].auth_headers == {"Authorization": "tok"}


def test_extract_link_attachments_strips_trailing_punctuation():
    atts = extract_link_attachments(["end of sentence http://ex.com/a.png."])
    assert [a.url for a in atts] == ["http://ex.com/a.png"]  # trailing '.' stripped


def test_extract_link_attachments_dedups_via_shared_seen_urls():
    seen: set = set()
    first = extract_link_attachments(
        ["http://ex.com/a.png and again http://ex.com/a.png"], seen_urls=seen
    )
    assert len(first) == 1  # duplicate within the blob collapsed
    # The shared seen set suppresses the same URL in a later call too.
    assert extract_link_attachments(["http://ex.com/a.png"], seen_urls=seen) == []


# --------------------------------------------------------------------------- #
# workflow_state filter parsing (base.workflow_state_list + Shortcut ids)
# --------------------------------------------------------------------------- #
def test_workflow_state_list_splits_trims_and_drops_blanks():
    cfg = TicketProviderConfig(workflow_state="a, b ,,c")
    assert workflow_state_list(cfg) == ["a", "b", "c"]


def test_workflow_state_list_empty():
    assert workflow_state_list(TicketProviderConfig(workflow_state="")) == []


def test_shortcut_ingest_state_ids_numeric_csv():
    prov = ShortcutProvider(
        TicketProviderConfig(provider="shortcut", workflow_state="100, 200")
    )
    assert prov._ingest_state_ids() == [100, 200]


def test_shortcut_ingest_state_ids_skips_non_numeric(caplog):
    prov = ShortcutProvider(
        TicketProviderConfig(provider="shortcut", workflow_state="100, nope, 200")
    )
    with caplog.at_level(logging.WARNING):
        out = prov._ingest_state_ids()
    assert out == [100, 200]  # the non-numeric entry is dropped
    assert any("non-numeric" in r.getMessage() for r in caplog.records)


def test_shortcut_ingest_state_ids_legacy_fallback_only_when_empty():
    # workflow_state empty -> fall back to the legacy integer workflow_state_id.
    prov = ShortcutProvider(
        TicketProviderConfig(
            provider="shortcut", workflow_state="", workflow_state_id=42
        )
    )
    assert prov._ingest_state_ids() == [42]
    # workflow_state set -> the legacy id is NOT consulted.
    prov2 = ShortcutProvider(
        TicketProviderConfig(
            provider="shortcut", workflow_state="7", workflow_state_id=42
        )
    )
    assert prov2._ingest_state_ids() == [7]


# --------------------------------------------------------------------------- #
# Shortcut HTTP paths: _search_stories, search_assigned fan-out, _hydrate_story
# --------------------------------------------------------------------------- #
class TestShortcutSearchStories:
    def _prov(self, **cfg):
        base = dict(provider="shortcut", api_token="t", member_id="m")
        base.update(cfg)
        return ShortcutProvider(TicketProviderConfig(**base))

    async def test_returns_list_and_posts_to_search(self):
        prov = self._prov()
        session = _FakeSession(post_responses=[_FakeResp(200, json_data=[{"id": 1}])])
        with _patch_session(session):
            out = await prov._search_stories({"owner_id": "m"})
        assert out == [{"id": 1}]
        url, kwargs = session.post_calls[0]
        assert url.endswith("/stories/search")
        assert kwargs["json"] == {"owner_id": "m"}

    async def test_201_accepted(self):
        prov = self._prov()
        session = _FakeSession(post_responses=[_FakeResp(201, json_data=[])])
        with _patch_session(session):
            assert await prov._search_stories({}) == []

    async def test_non_list_body_returns_empty(self):
        prov = self._prov()
        session = _FakeSession(post_responses=[_FakeResp(200, json_data={"x": 1})])
        with _patch_session(session):
            assert await prov._search_stories({}) == []

    async def test_error_status_raises_client_error(self):
        import aiohttp

        prov = self._prov()
        session = _FakeSession(post_responses=[_FakeResp(500, text_data="boom")])
        with _patch_session(session):
            with pytest.raises(aiohttp.ClientError, match="500"):
                await prov._search_stories({})


class TestShortcutSearchAssigned:
    async def test_fans_out_per_state_and_dedups(self, monkeypatch):
        prov = ShortcutProvider(
            TicketProviderConfig(
                provider="shortcut",
                api_token="t",
                member_id="m",
                workflow_state="100,200",
            )
        )
        bodies: list[dict] = []

        async def fake_search(body):
            bodies.append(body)
            story1 = {"id": 1, "name": "a", "created_at": "2025-01-01T00:00:00Z"}
            if body["workflow_state_id"] == 100:
                return [story1]
            return [
                story1,
                {"id": 2, "name": "b", "created_at": "2025-01-01T00:00:00Z"},
            ]

        async def fake_hydrate(session, sid):
            return {
                "id": sid,
                "name": f"full-{sid}",
                "description": "d",
                "created_at": "2025-01-01T00:00:00Z",
            }

        monkeypatch.setattr(prov, "_search_stories", fake_search)
        monkeypatch.setattr(prov, "_hydrate_story", fake_hydrate)
        # search_assigned opens a real ClientSession for the hydration loop;
        # hand it a fake so nothing hits the network.
        with _patch_session(_FakeSession()):
            out = await prov.search_assigned(_SINCE)

        # One search per configured state.
        assert [b["workflow_state_id"] for b in bodies] == [100, 200]
        # Story 1 (present in both states) de-duped; both stories hydrated.
        assert sorted(t.id for t in out) == [1, 2]


# --------------------------------------------------------------------------- #
# Assignee scope: "anyone" takes tickets by state, whoever owns them (the QA
# queue). Every case here is really one question — can this search still be
# bounded once the assignee filter comes off?
# --------------------------------------------------------------------------- #
class TestIngestsAnyAssignee:
    def test_default_is_assigned_to_me(self):
        assert not ingests_any_assignee(
            TicketProviderConfig(provider="shortcut", workflow_state="100")
        )

    def test_anyone_with_a_state_filter(self):
        assert ingests_any_assignee(
            TicketProviderConfig(
                provider="shortcut", assignee_scope="anyone", workflow_state="100"
            )
        )

    def test_anyone_without_a_state_filter_stays_narrow(self):
        # The whole tracker is the alternative, so the scope quietly reverts.
        assert not ingests_any_assignee(
            TicketProviderConfig(
                provider="shortcut", assignee_scope="anyone", workflow_state=""
            )
        )

    def test_github_needs_no_state_filter(self):
        # No workflow-state model at all; the repo is the bound.
        assert ingests_any_assignee(
            TicketProviderConfig(provider="github_issues", assignee_scope="anyone")
        )

    def test_asana_never_offers_it(self):
        assert not ingests_any_assignee(
            TicketProviderConfig(
                provider="asana", assignee_scope="anyone", workflow_state="x"
            )
        )

    def test_unknown_value_reads_as_mine(self):
        assert not ingests_any_assignee(
            TicketProviderConfig(
                provider="shortcut", assignee_scope="everyone", workflow_state="100"
            )
        )

    def test_asana_has_no_scope_field_in_the_catalog(self):
        by_id = {p["id"]: p for p in PROVIDER_META}
        keys = {f["key"] for f in by_id["asana"]["fields"]}
        assert "assignee_scope" not in keys
        for pid in ("shortcut", "jira", "linear", "github_issues"):
            fields = {f["key"]: f for f in by_id[pid]["fields"]}
            assert fields["assignee_scope"]["type"] == "choice"
            assert [o["value"] for o in fields["assignee_scope"]["options"]] == [
                "",
                "anyone",
            ]


class TestAnyAssigneeQueries:
    async def test_shortcut_poll_drops_owner_id(self, monkeypatch):
        prov = ShortcutProvider(
            TicketProviderConfig(
                provider="shortcut",
                api_token="t",
                member_id="m",
                workflow_state="100",
                assignee_scope="anyone",
            )
        )
        bodies: list[dict] = []

        async def fake_search(body):
            bodies.append(body)
            return []

        monkeypatch.setattr(prov, "_search_stories", fake_search)
        with _patch_session(_FakeSession()):
            await prov.search_assigned(_SINCE)
        assert bodies == [
            {"updated_at_start": _SINCE.isoformat(), "workflow_state_id": 100}
        ]

    async def test_shortcut_panel_keeps_the_state_filter(self, monkeypatch):
        # The assigned-to-me listing drops it on purpose to show every bucket;
        # without an owner filter that would be the whole organization.
        prov = ShortcutProvider(
            TicketProviderConfig(
                provider="shortcut",
                api_token="t",
                member_id="m",
                workflow_state="100,200",
                assignee_scope="anyone",
            )
        )
        bodies: list[dict] = []

        async def fake_search(body):
            bodies.append(body)
            return []

        monkeypatch.setattr(prov, "_search_stories", fake_search)
        monkeypatch.setattr(prov, "list_states", lambda: _acoro([]))
        monkeypatch.setattr(prov, "_member_names", lambda: _acoro({}))
        await prov.search_assigned_all()
        assert bodies == [{"workflow_state_id": 100}, {"workflow_state_id": 200}]

    async def test_shortcut_panel_names_the_owners(self, monkeypatch):
        prov = ShortcutProvider(
            TicketProviderConfig(
                provider="shortcut", api_token="t", member_id="m", workflow_state="100"
            )
        )
        story = {
            "id": 9,
            "name": "n",
            "created_at": "2025-01-01T00:00:00Z",
            "owner_ids": ["u-1"],
        }
        monkeypatch.setattr(prov, "_search_stories", lambda body: _acoro([story]))
        monkeypatch.setattr(prov, "list_states", lambda: _acoro([]))
        monkeypatch.setattr(prov, "_member_names", lambda: _acoro({"u-1": "Mauricio"}))
        out = await prov.search_assigned_all()
        assert out[0].owner_names == ["Mauricio"]

    async def test_shortcut_member_names_degrade_to_empty(self):
        prov = ShortcutProvider(
            TicketProviderConfig(provider="shortcut", api_token="t")
        )
        session = _FakeSession(get_responses=[_FakeResp(403, text_data="nope")])
        with _patch_session(session):
            assert await prov._member_names() == {}

    async def test_jira_jql_drops_the_assignee_clause(self, monkeypatch):
        prov = JiraProvider(
            TicketProviderConfig(
                provider="jira",
                base_url="https://x.atlassian.net",
                workflow_state="10001",
                assignee_scope="anyone",
            )
        )
        seen: list[str] = []
        monkeypatch.setattr(prov, "_search", lambda jql: seen.append(jql) or _acoro([]))
        await prov.search_assigned(_SINCE)
        await prov.search_assigned_all()
        assert "assignee" not in seen[0] and "status IN (10001)" in seen[0]
        # The panel query is bounded by status alone, with no dangling AND.
        assert seen[1] == "status IN (10001) ORDER BY updated DESC"

    async def test_jira_keeps_the_assignee_clause_by_default(self, monkeypatch):
        prov = JiraProvider(
            TicketProviderConfig(
                provider="jira", base_url="https://x.atlassian.net", workflow_state="1"
            )
        )
        seen: list[str] = []
        monkeypatch.setattr(prov, "_search", lambda jql: seen.append(jql) or _acoro([]))
        await prov.search_assigned(_SINCE)
        assert seen[0].startswith("assignee = currentUser() AND updated >=")

    async def test_linear_searches_from_the_issues_root(self, monkeypatch):
        prov = LinearProvider(
            TicketProviderConfig(
                provider="linear",
                api_token="k",
                project="QA",
                workflow_state="s-1",
                assignee_scope="anyone",
            )
        )
        seen: list[tuple[str, dict]] = []

        def fake_gql(query, variables):
            seen.append((query, variables))
            return _acoro({"issues": {"nodes": []}})

        monkeypatch.setattr(prov, "_gql", fake_gql)
        await prov.search_assigned(_SINCE)
        query, variables = seen[0]
        # viewer.assignedIssues IS the assignee filter, so scope changes the root.
        assert "viewer" not in query and " issues(" in query
        assert "team: { key: { eq: $team } }" in query
        assert variables["team"] == "QA" and variables["stateIds"] == ["s-1"]

    async def test_linear_panel_keeps_the_state_filter(self, monkeypatch):
        prov = LinearProvider(
            TicketProviderConfig(
                provider="linear",
                api_token="k",
                workflow_state="s-1",
                assignee_scope="anyone",
            )
        )
        seen: list[tuple[str, dict]] = []

        def fake_gql(query, variables):
            seen.append((query, variables))
            return _acoro({"issues": {"nodes": []}})

        monkeypatch.setattr(prov, "_gql", fake_gql)
        await prov.search_assigned_all()
        assert seen[0][1]["stateIds"] == ["s-1"]

    async def test_github_omits_the_assignee_param(self):
        prov = GithubIssuesProvider(
            TicketProviderConfig(
                provider="github_issues",
                api_token="t",
                project="octo/repo",
                assignee_scope="anyone",
            )
        )
        session = _FakeSession(get_responses=[_FakeResp(200, json_data=[])])
        with _patch_session(session):
            await prov.search_assigned(_SINCE)
        _url, kwargs = session.get_calls[0]
        assert "assignee" not in kwargs["params"]

    async def test_github_refuses_to_guess_when_scoped_to_me(self, monkeypatch):
        # Historically this fell through to assignee="*" — an any-assignee search
        # wearing an assigned-to-me label.
        prov = GithubIssuesProvider(
            TicketProviderConfig(
                provider="github_issues", api_token="t", project="octo/repo"
            )
        )
        monkeypatch.setattr(prov, "_login", lambda *a, **k: _acoro(""))
        with _patch_session(_FakeSession()):
            with pytest.raises(ProviderError, match="assigned to"):
                await prov.search_assigned(_SINCE)


class TestShortcutHydrateStory:
    async def test_retries_on_429_then_succeeds(self, monkeypatch):
        prov = ShortcutProvider(
            TicketProviderConfig(provider="shortcut", api_token="t")
        )
        session = _FakeSession(
            get_responses=[
                _FakeResp(429, headers={"Retry-After": "0"}),
                _FakeResp(200, json_data={"id": 5, "description": "x"}),
            ]
        )
        sleeps: list[float] = []

        async def fake_sleep(delay):
            sleeps.append(delay)

        monkeypatch.setattr(
            "backend.ticket_ingestion.providers.shortcut.asyncio.sleep", fake_sleep
        )
        out = await prov._hydrate_story(session, 5)
        assert out == {"id": 5, "description": "x"}
        assert sleeps == [0.0]  # Retry-After honored

    async def test_gives_up_after_5xx_returns_none(self, monkeypatch):
        prov = ShortcutProvider(
            TicketProviderConfig(provider="shortcut", api_token="t")
        )
        session = _FakeSession(get_responses=[_FakeResp(500) for _ in range(3)])

        async def fake_sleep(delay):
            return None

        monkeypatch.setattr(
            "backend.ticket_ingestion.providers.shortcut.asyncio.sleep", fake_sleep
        )
        # Exhausts _HYDRATE_ATTEMPTS -> slim fallback (None) rather than raising.
        assert await prov._hydrate_story(session, 9) is None


# --------------------------------------------------------------------------- #
# Jira HTTP path: JQL state clause construction + 200/201 acceptance
# --------------------------------------------------------------------------- #
class TestJiraSearchAssigned:
    def _prov(self, **cfg):
        base = dict(
            provider="jira",
            base_url="https://acme.atlassian.net",
            email="e@x.com",
            api_token="tok",
        )
        base.update(cfg)
        return JiraProvider(TicketProviderConfig(**base))

    async def _jql_for(self, prov):
        session = _FakeSession(
            post_responses=[_FakeResp(200, json_data={"issues": []})]
        )
        with _patch_session(session):
            await prov.search_assigned(_SINCE)
        return session.post_calls[0][1]["json"]["jql"]

    async def test_numeric_state_unquoted_in_clause(self):
        jql = await self._jql_for(self._prov(workflow_state="10001"))
        assert "status IN (10001)" in jql

    async def test_named_states_quoted_in_clause(self):
        jql = await self._jql_for(self._prov(workflow_state="In Progress, Done"))
        assert 'status IN ("In Progress", "Done")' in jql

    async def test_no_state_clause_when_unset(self):
        jql = await self._jql_for(self._prov(workflow_state=""))
        assert "status IN" not in jql

    async def test_201_accepted_and_issues_parsed(self):
        prov = self._prov()
        issue = {
            "key": "P-1",
            "fields": {"summary": "s", "created": "2025-01-01T00:00:00.000+0000"},
        }
        session = _FakeSession(
            post_responses=[_FakeResp(201, json_data={"issues": [issue]})]
        )
        with _patch_session(session):
            out = await prov.search_assigned(_SINCE)
        assert [t.id for t in out] == ["P-1"]

    async def test_error_status_raises_client_error(self):
        import aiohttp

        prov = self._prov()
        session = _FakeSession(post_responses=[_FakeResp(500, text_data="err")])
        with _patch_session(session):
            with pytest.raises(aiohttp.ClientError, match="500"):
                await prov.search_assigned(_SINCE)

    async def test_status_field_requested_for_the_bucket(self):
        prov = self._prov()
        session = _FakeSession(
            post_responses=[_FakeResp(200, json_data={"issues": []})]
        )
        with _patch_session(session):
            await prov.search_assigned(_SINCE)
        assert "status" in session.post_calls[0][1]["json"]["fields"]

    async def test_search_assigned_all_drops_state_and_age_filters(self):
        # The panel exists to surface the issue you are about to move INTO the
        # ingest state, so its query must carry NEITHER the status filter nor
        # the "updated >=" cutoff — even though the source configures one.
        prov = self._prov(workflow_state="10001")
        session = _FakeSession(
            post_responses=[_FakeResp(200, json_data={"issues": []})]
        )
        with _patch_session(session):
            await prov.search_assigned_all()
        body = session.post_calls[0][1]["json"]
        assert body["jql"] == "assignee = currentUser() ORDER BY updated DESC"
        assert "status IN" not in body["jql"] and "updated >=" not in body["jql"]
        assert "status" in body["fields"]

    async def test_search_assigned_all_populates_state(self):
        prov = self._prov()
        issue = {
            "key": "P-1",
            "fields": {
                "summary": "s",
                "created": "2025-01-01T00:00:00.000+0000",
                "status": {"name": "Ready for Dev"},
            },
        }
        session = _FakeSession(
            post_responses=[_FakeResp(200, json_data={"issues": [issue]})]
        )
        with _patch_session(session):
            out = await prov.search_assigned_all()
        assert [t.state for t in out] == ["Ready for Dev"]

    async def test_missing_status_leaves_state_empty(self):
        # No status in the payload -> "" (the panel's "No state" bucket), never None.
        t = self._prov()._issue_to_ticket({"key": "P-1", "fields": {"summary": "s"}})
        assert t.state == ""


# --------------------------------------------------------------------------- #
# Linear HTTP path: with_state query gating + GraphQL errors -> ProviderError
# --------------------------------------------------------------------------- #
class TestLinearSearchAssigned:
    async def test_state_filter_gates_query_and_variables(self, monkeypatch):
        prov = LinearProvider(
            TicketProviderConfig(
                provider="linear", api_token="k", workflow_state="s1,s2"
            )
        )
        captured: dict = {}

        async def fake_gql(query, variables):
            captured["query"] = query
            captured["variables"] = variables
            return {"viewer": {"assignedIssues": {"nodes": []}}}

        monkeypatch.setattr(prov, "_gql", fake_gql)
        await prov.search_assigned(_SINCE)
        assert "state: { id: { in: $stateIds } }" in captured["query"]
        assert captured["variables"]["stateIds"] == ["s1", "s2"]

    async def test_no_state_omits_filter(self, monkeypatch):
        prov = LinearProvider(TicketProviderConfig(provider="linear", api_token="k"))
        captured: dict = {}

        async def fake_gql(query, variables):
            captured["query"] = query
            captured["variables"] = variables
            return {"viewer": {"assignedIssues": {"nodes": []}}}

        monkeypatch.setattr(prov, "_gql", fake_gql)
        await prov.search_assigned(_SINCE)
        assert "stateIds" not in captured["query"]
        assert "stateIds" not in captured["variables"]

    async def test_search_assigned_all_drops_state_filter_and_age_cutoff(
        self, monkeypatch
    ):
        # The panel exists to surface the issue you are about to move INTO the
        # ingest state, so its query must carry no state filter at all — even
        # though this source configures one — and reach back to the epoch.
        prov = LinearProvider(
            TicketProviderConfig(
                provider="linear", api_token="k", workflow_state="s1,s2"
            )
        )
        captured: dict = {}

        async def fake_gql(query, variables):
            captured["query"] = query
            captured["variables"] = variables
            return {"viewer": {"assignedIssues": {"nodes": []}}}

        monkeypatch.setattr(prov, "_gql", fake_gql)
        await prov.search_assigned_all()
        assert "state:" not in captured["query"]
        assert "stateIds" not in captured["query"]
        assert "stateIds" not in captured["variables"]
        assert captured["variables"]["since"] == "1970-01-01T00:00:00+00:00"

    async def test_search_assigned_all_populates_state(self, monkeypatch):
        prov = LinearProvider(TicketProviderConfig(provider="linear", api_token="k"))

        async def fake_gql(query, variables):
            assert "state {" in query  # the state has to be asked for
            return {
                "viewer": {
                    "assignedIssues": {
                        "nodes": [
                            {
                                "identifier": "ENG-1",
                                "title": "T",
                                "state": {
                                    "id": "s1",
                                    "name": "In Progress",
                                    "team": {"key": "ENG"},
                                },
                            }
                        ]
                    }
                }
            }

        monkeypatch.setattr(prov, "_gql", fake_gql)
        out = await prov.search_assigned_all()
        # Team-prefixed, i.e. spelled exactly as list_states() spells it.
        assert [t.state for t in out] == ["ENG · In Progress"]

    async def test_gql_graphql_errors_raise_provider_error(self):
        prov = LinearProvider(TicketProviderConfig(provider="linear", api_token="k"))
        session = _FakeSession(
            post_responses=[_FakeResp(200, json_data={"errors": [{"message": "bad"}]})]
        )
        with _patch_session(session):
            with pytest.raises(ProviderError, match="GraphQL error"):
                await prov._gql("query {}", {})

    async def test_gql_401_raises_provider_error(self):
        prov = LinearProvider(TicketProviderConfig(provider="linear", api_token="k"))
        session = _FakeSession(post_responses=[_FakeResp(401)])
        with _patch_session(session):
            with pytest.raises(ProviderError, match="401"):
                await prov._gql("query {}", {})


# --------------------------------------------------------------------------- #
# GitHub Issues HTTP path: pull_request filtering in search_assigned
# --------------------------------------------------------------------------- #
class TestGithubSearchAssigned:
    def _prov(self, monkeypatch):
        prov = GithubIssuesProvider(
            TicketProviderConfig(
                provider="github_issues", project="octo/repo", member_id="me"
            )
        )

        async def fake_token():
            return "tok"

        monkeypatch.setattr(prov, "_resolve_token", fake_token)
        return prov

    async def test_filters_out_pull_requests(self, monkeypatch):
        prov = self._prov(monkeypatch)
        issues = [
            {
                "number": 1,
                "title": "real issue",
                "body": "b",
                "html_url": "u",
                "created_at": "2025-01-01T00:00:00Z",
                "assignees": [{"login": "me"}],
                "comments": 0,
            },
            {
                "number": 2,
                "title": "a PR",
                "body": "b",
                "html_url": "u",
                "created_at": "2025-01-01T00:00:00Z",
                "assignees": [],
                "comments": 0,
                "pull_request": {"url": "x"},  # the issues endpoint also lists PRs
            },
        ]
        session = _FakeSession(get_responses=[_FakeResp(200, json_data=issues)])
        with _patch_session(session):
            out = await prov.search_assigned(_SINCE)
        assert [t.id for t in out] == [1]  # the PR (#2) is filtered out
        # login came from member_id -> only ONE GET (the issues list).
        assert len(session.get_calls) == 1
        assert session.get_calls[0][1]["params"]["assignee"] == "me"

    async def test_error_status_raises_client_error(self, monkeypatch):
        import aiohttp

        prov = self._prov(monkeypatch)
        session = _FakeSession(get_responses=[_FakeResp(500, text_data="err")])
        with _patch_session(session):
            with pytest.raises(aiohttp.ClientError, match="500"):
                await prov.search_assigned(_SINCE)


# --------------------------------------------------------------------------- #
# Asana HTTP path: completed-task skipping + auth/scope errors
# --------------------------------------------------------------------------- #
class TestAsanaSearchAssigned:
    async def test_skips_completed_tasks(self, monkeypatch):
        prov = AsanaProvider(
            TicketProviderConfig(
                provider="asana", api_token="pat", project="ws1", member_id="me"
            )
        )
        monkeypatch.setattr(prov, "_comments", lambda *a, **k: _acoro([]))
        monkeypatch.setattr(prov, "_attachments", lambda *a, **k: _acoro([]))
        tasks = [
            {
                "gid": "1",
                "name": "open",
                "notes": "n",
                "created_at": "2025-01-01T00:00:00Z",
                "completed": False,
            },
            {
                "gid": "2",
                "name": "done",
                "notes": "n",
                "created_at": "2025-01-01T00:00:00Z",
                "completed": True,
            },
        ]
        session = _FakeSession(
            get_responses=[_FakeResp(200, json_data={"data": tasks})]
        )
        with _patch_session(session):
            out = await prov.search_assigned(_SINCE)
        assert [t.id for t in out] == ["1"]  # completed task #2 skipped

    async def test_requires_project(self):
        prov = AsanaProvider(TicketProviderConfig(provider="asana", api_token="pat"))
        with pytest.raises(ProviderError):
            await prov.search_assigned(_SINCE)

    async def test_auth_error_raises_provider_error(self):
        prov = AsanaProvider(
            TicketProviderConfig(provider="asana", api_token="pat", project="ws1")
        )
        session = _FakeSession(get_responses=[_FakeResp(401, text_data="no")])
        with _patch_session(session):
            with pytest.raises(ProviderError, match="401"):
                await prov.search_assigned(_SINCE)


class _RaisingResp:
    """An aiohttp response whose context entry raises (network failure)."""

    def __init__(self, exc):
        self._exc = exc

    async def __aenter__(self):
        raise self._exc

    async def __aexit__(self, *args):
        return False


# --------------------------------------------------------------------------- #
# base.parse_iso8601 (shared timestamp parser)
# --------------------------------------------------------------------------- #
class TestParseIso8601:
    def test_empty_returns_epoch(self):
        from backend.ticket_ingestion.providers.base import parse_iso8601

        assert parse_iso8601(None) == datetime(1970, 1, 1, tzinfo=timezone.utc)
        assert parse_iso8601("") == datetime(1970, 1, 1, tzinfo=timezone.utc)

    def test_invalid_returns_epoch(self):
        from backend.ticket_ingestion.providers.base import parse_iso8601

        assert parse_iso8601("not-a-date") == datetime(1970, 1, 1, tzinfo=timezone.utc)

    def test_z_suffix_and_naive_get_utc(self):
        from backend.ticket_ingestion.providers.base import parse_iso8601

        assert parse_iso8601("2025-01-15T10:00:00Z") == datetime(
            2025, 1, 15, 10, 0, tzinfo=timezone.utc
        )
        assert parse_iso8601("2025-01-15T10:00:00").tzinfo == timezone.utc


# --------------------------------------------------------------------------- #
# base default methods via a minimal concrete provider
# --------------------------------------------------------------------------- #
class _StubProvider(
    __import__(
        "backend.ticket_ingestion.providers.base", fromlist=["TicketProvider"]
    ).TicketProvider
):
    name = "stub"
    label = "Stub"
    slug_prefix = "stub"

    def __init__(self, cfg, tickets=None, error=None):
        super().__init__(cfg)
        self._tickets = tickets or []
        self._error = error
        self.since_seen = None

    async def search_assigned(self, since):
        self.since_seen = since
        if self._error:
            raise self._error
        return list(self._tickets)

    async def fetch(self, ticket_id):  # pragma: no cover - not exercised here
        raise NotImplementedError


class TestBaseDefaults:
    def _ticket(self):
        return Ticket(
            id=1,
            name="n",
            description="d",
            acceptance_criteria=[],
            owner_ids=[],
            app_url="",
            created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
        )

    async def test_search_assigned_all_uses_epoch(self):
        prov = _StubProvider(TicketProviderConfig(), tickets=[self._ticket()])
        out = await prov.search_assigned_all()
        assert [t.id for t in out] == [1]
        assert prov.since_seen == datetime(1970, 1, 1, tzinfo=timezone.utc)

    async def test_test_connection_success(self):
        prov = _StubProvider(TicketProviderConfig())
        identity, err = await prov.test_connection()
        assert identity == {} and err == ""

    async def test_test_connection_provider_error(self):
        prov = _StubProvider(TicketProviderConfig(), error=ProviderError("bad creds"))
        identity, err = await prov.test_connection()
        assert identity is None and err == "bad creds"

    async def test_test_connection_generic_exception(self):
        prov = _StubProvider(TicketProviderConfig(), error=ValueError("boom"))
        identity, err = await prov.test_connection()
        assert identity is None and err == "ValueError: boom"

    async def test_list_states_default_empty(self):
        prov = _StubProvider(TicketProviderConfig())
        assert await prov.list_states() == []

    def test_label_override_applied(self):
        prov = _StubProvider(TicketProviderConfig(label="Custom Label"))
        assert prov.label == "Custom Label"


# --------------------------------------------------------------------------- #
# GitHub Issues: auth CLI fallback, _login, comment fetch, fetch, test_connection
# --------------------------------------------------------------------------- #
class TestGithubGhAuthToken:
    async def test_returns_stripped_token(self):
        from backend.ticket_ingestion.providers import github_issues as ghm
        from unittest.mock import AsyncMock as _AM

        proc = MagicMock()
        proc.returncode = 0
        proc.communicate = _AM(return_value=(b"  ghp_x\n", b""))
        with patch(
            "asyncio.create_subprocess_exec", new_callable=_AM, return_value=proc
        ):
            assert await ghm._gh_auth_token() == "ghp_x"

    async def test_nonzero_exit_returns_none(self):
        from backend.ticket_ingestion.providers import github_issues as ghm
        from unittest.mock import AsyncMock as _AM

        proc = MagicMock()
        proc.returncode = 1
        proc.communicate = _AM(return_value=(b"", b"nope"))
        with patch(
            "asyncio.create_subprocess_exec", new_callable=_AM, return_value=proc
        ):
            assert await ghm._gh_auth_token() is None

    async def test_missing_gh_returns_none(self):
        from backend.ticket_ingestion.providers import github_issues as ghm
        from unittest.mock import AsyncMock as _AM

        with patch(
            "asyncio.create_subprocess_exec",
            new_callable=_AM,
            side_effect=FileNotFoundError,
        ):
            assert await ghm._gh_auth_token() is None


class TestGithubResolveToken:
    async def test_no_token_raises(self, monkeypatch):
        prov = GithubIssuesProvider(
            TicketProviderConfig(provider="github_issues", project="o/r")
        )

        async def none_secret(**kwargs):
            return None

        monkeypatch.setattr(
            "backend.ticket_ingestion.providers.github_issues.resolve_secret",
            none_secret,
        )
        with pytest.raises(ProviderError, match="No GitHub token"):
            await prov._resolve_token()

    async def test_token_cached(self, monkeypatch):
        prov = GithubIssuesProvider(
            TicketProviderConfig(provider="github_issues", project="o/r")
        )
        calls = {"n": 0}

        async def one_secret(**kwargs):
            calls["n"] += 1
            return "cached-tok"

        monkeypatch.setattr(
            "backend.ticket_ingestion.providers.github_issues.resolve_secret",
            one_secret,
        )
        assert await prov._resolve_token() == "cached-tok"
        assert await prov._resolve_token() == "cached-tok"
        assert calls["n"] == 1  # second call served from cache


class TestGithubLogin:
    async def test_member_id_short_circuits(self):
        prov = GithubIssuesProvider(
            TicketProviderConfig(
                provider="github_issues", project="o/r", member_id="me"
            )
        )
        # No session get should happen when member_id is configured.
        session = _FakeSession()
        assert await prov._login(session, {}) == "me"
        assert session.get_calls == []

    async def test_fetches_user_when_no_member_id(self):
        prov = GithubIssuesProvider(
            TicketProviderConfig(provider="github_issues", project="o/r")
        )
        session = _FakeSession(
            get_responses=[_FakeResp(200, json_data={"login": "octocat"})]
        )
        assert await prov._login(session, {}) == "octocat"

    async def test_user_non_200_raises(self):
        prov = GithubIssuesProvider(
            TicketProviderConfig(provider="github_issues", project="o/r")
        )
        session = _FakeSession(get_responses=[_FakeResp(403, text_data="forbidden")])
        with pytest.raises(ProviderError, match="403"):
            await prov._login(session, {})


class TestGithubFetchComments:
    def _prov(self):
        return GithubIssuesProvider(
            TicketProviderConfig(provider="github_issues", project="o/r")
        )

    async def test_empty_url_returns_empty(self):
        assert await self._prov()._fetch_comments(_FakeSession(), {}, "") == []

    async def test_renders_and_skips_empty(self):
        raw = [
            {"body": "hi", "user": {"login": "al"}, "created_at": "2025-01-02"},
            {"body": "  "},  # blank -> skipped
        ]
        session = _FakeSession(get_responses=[_FakeResp(200, json_data=raw)])
        out = await self._prov()._fetch_comments(session, {}, "http://c")
        assert out == ["[2025-01-02 by al] hi"]

    async def test_non_200_returns_empty(self):
        session = _FakeSession(get_responses=[_FakeResp(500, text_data="err")])
        assert await self._prov()._fetch_comments(session, {}, "http://c") == []

    async def test_client_error_returns_empty(self):
        import aiohttp

        session = _FakeSession(
            get_responses=[_RaisingResp(aiohttp.ClientError("reset"))]
        )
        assert await self._prov()._fetch_comments(session, {}, "http://c") == []


class TestGithubFetchAndTestConnection:
    def _prov(self, monkeypatch, **cfg):
        base = dict(provider="github_issues", project="octo/repo")
        base.update(cfg)
        prov = GithubIssuesProvider(TicketProviderConfig(**base))

        async def fake_token():
            return "tok"

        monkeypatch.setattr(prov, "_resolve_token", fake_token)
        return prov

    async def test_fetch_returns_ticket(self, monkeypatch):
        prov = self._prov(monkeypatch)
        issue = {
            "number": 15,
            "title": "Bug",
            "body": "b",
            "html_url": "u",
            "created_at": "2025-01-01T00:00:00Z",
            "assignees": [],
            "comments": 0,
        }
        session = _FakeSession(get_responses=[_FakeResp(200, json_data=issue)])
        with _patch_session(session):
            t = await prov.fetch("15")
        assert t.id == 15 and t.slug == "gh-15"

    async def test_fetch_non_200_raises(self, monkeypatch):
        prov = self._prov(monkeypatch)
        session = _FakeSession(get_responses=[_FakeResp(404, text_data="nope")])
        with _patch_session(session):
            with pytest.raises(ProviderError, match="404"):
                await prov.fetch("99")

    async def test_test_connection_success(self, monkeypatch):
        prov = self._prov(monkeypatch)
        session = _FakeSession(
            get_responses=[_FakeResp(200, json_data={"login": "octo", "name": "O"})]
        )
        with _patch_session(session):
            identity, err = await prov.test_connection()
        # ``project`` rides back so the UI can show (and store) whichever repo the
        # zero-config chain resolved to, instead of an empty auto-filled field.
        assert err == "" and identity == {
            "member_id": "octo",
            "name": "O",
            "project": "octo/repo",
        }

    async def test_test_connection_token_rejected(self, monkeypatch):
        prov = self._prov(monkeypatch)
        session = _FakeSession(get_responses=[_FakeResp(401)])
        with _patch_session(session):
            identity, err = await prov.test_connection()
        assert identity is None and "rejected" in err

    async def test_test_connection_other_status(self, monkeypatch):
        prov = self._prov(monkeypatch)
        session = _FakeSession(get_responses=[_FakeResp(500)])
        with _patch_session(session):
            identity, err = await prov.test_connection()
        assert identity is None and "500" in err

    async def test_test_connection_bad_scope_reports_provider_error(self, monkeypatch):
        # project not owner/repo -> _repo() raises ProviderError before any GET.
        prov = self._prov(monkeypatch, project="nope")
        identity, err = await prov.test_connection()
        assert identity is None and "owner/repo" in err

    async def test_test_connection_network_error(self, monkeypatch):
        import aiohttp

        prov = self._prov(monkeypatch)
        with patch("aiohttp.ClientSession", side_effect=aiohttp.ClientError("down")):
            identity, err = await prov.test_connection()
        assert identity is None and "network error" in err


# --------------------------------------------------------------------------- #
# Asana: _get error, _comments, _attachments, fetch, test_connection
# --------------------------------------------------------------------------- #
class TestAsanaHelpers:
    def _prov(self):
        return AsanaProvider(
            TicketProviderConfig(provider="asana", api_token="pat", project="ws1")
        )

    async def test_get_non_auth_error_raises_client_error(self):
        import aiohttp

        session = _FakeSession(get_responses=[_FakeResp(500, text_data="boom")])
        with pytest.raises(aiohttp.ClientError, match="500"):
            await self._prov()._get(session, "/tasks")

    async def test_comments_renders_only_comment_stories(self):
        stories = [
            {
                "type": "comment",
                "text": "hello",
                "created_at": "2025-01-01",
                "created_by": {"name": "Al"},
            },
            {"type": "system", "text": "changed status"},  # non-comment -> skipped
            {"type": "comment", "text": "  "},  # blank -> skipped
            {"type": "comment", "text": "no author"},  # -> unknown
        ]
        session = _FakeSession(
            get_responses=[_FakeResp(200, json_data={"data": stories})]
        )
        out = await self._prov()._comments(session, "gid1")
        assert out == ["[2025-01-01 by Al] hello", "[ by unknown] no author"]

    async def test_comments_swallows_errors(self):
        session = _FakeSession(get_responses=[_FakeResp(401, text_data="no")])
        assert await self._prov()._comments(session, "gid1") == []

    async def test_attachments_prefers_download_url_and_skips_urlless(self):
        atts = [
            {"name": "a.png", "download_url": "http://d/a.png"},
            {"name": "b.png", "view_url": "http://v/b.png"},
            {"name": "c"},  # no url -> skipped
        ]
        session = _FakeSession(get_responses=[_FakeResp(200, json_data={"data": atts})])
        out = await self._prov()._attachments(session, "gid1")
        assert [(a.name, a.url) for a in out] == [
            ("a.png", "http://d/a.png"),
            ("b.png", "http://v/b.png"),
        ]

    async def test_attachments_swallows_errors(self):
        session = _FakeSession(get_responses=[_FakeResp(403)])
        assert await self._prov()._attachments(session, "gid1") == []

    async def test_fetch_returns_ticket(self, monkeypatch):
        prov = self._prov()
        monkeypatch.setattr(prov, "_comments", lambda *a, **k: _acoro([]))
        monkeypatch.setattr(prov, "_attachments", lambda *a, **k: _acoro([]))
        task = {
            "gid": "1",
            "name": "T",
            "notes": "n",
            "created_at": "2025-01-01T00:00:00Z",
        }
        session = _FakeSession(get_responses=[_FakeResp(200, json_data={"data": task})])
        with _patch_session(session):
            t = await prov.fetch("1")
        assert t.id == "1" and t.slug == "asana-1"

    async def test_fetch_not_found_raises(self):
        session = _FakeSession(get_responses=[_FakeResp(200, json_data={"data": None})])
        with _patch_session(session):
            with pytest.raises(ProviderError, match="not found"):
                await self._prov().fetch("404")

    async def test_test_connection_success(self):
        session = _FakeSession(
            get_responses=[
                _FakeResp(200, json_data={"data": {"gid": "u1", "name": "Me"}})
            ]
        )
        with _patch_session(session):
            identity, err = await self._prov().test_connection()
        assert err == "" and identity == {"member_id": "u1", "name": "Me"}

    async def test_test_connection_network_error(self):
        import aiohttp

        with patch("aiohttp.ClientSession", side_effect=aiohttp.ClientError("x")):
            identity, err = await self._prov().test_connection()
        assert identity is None and "network error" in err


# --------------------------------------------------------------------------- #
# Jira: flatten_adf edges, empty comment/attachment skip, fetch, test, states
# --------------------------------------------------------------------------- #
class TestJiraFlattenAdfEdges:
    def test_none_and_non_dict(self):
        assert flatten_adf(None) == ""
        assert flatten_adf(42) == ""  # non str/list/dict

    def test_hardbreak_and_mention(self):
        node = {
            "type": "paragraph",
            "content": [
                {"type": "text", "text": "hi"},
                {"type": "hardBreak"},
                {"type": "mention", "attrs": {"text": "bob"}},
            ],
        }
        assert flatten_adf(node) == "hi\n@bob\n"

    def test_codeblock_and_ordered_list(self):
        node = {
            "type": "orderedList",
            "content": [
                {
                    "type": "listItem",
                    "content": [
                        {
                            "type": "codeBlock",
                            "content": [{"type": "text", "text": "x=1"}],
                        }
                    ],
                }
            ],
        }
        # listItem -> "- " + inner.strip() + "\n"; codeBlock adds a trailing \n.
        assert flatten_adf(node) == "- x=1\n"


class TestJiraIssueToTicketSkips:
    def _prov(self):
        return JiraProvider(
            TicketProviderConfig(
                provider="jira",
                base_url="https://acme.atlassian.net",
                email="e@x.com",
                api_token="tok",
            )
        )

    def test_blank_comment_and_urlless_attachment_dropped(self):
        issue = {
            "key": "P-1",
            "fields": {
                "summary": "s",
                "created": "2025-01-01T00:00:00.000+0000",
                "comment": {
                    "comments": [
                        {"author": {"displayName": "A"}, "created": "x", "body": None},
                        {
                            "author": {"displayName": "B"},
                            "created": "y",
                            "body": {
                                "type": "doc",
                                "content": [
                                    {
                                        "type": "paragraph",
                                        "content": [{"type": "text", "text": "real"}],
                                    }
                                ],
                            },
                        },
                    ]
                },
                "attachment": [
                    {"filename": "f.txt"},  # no content url -> skipped
                    {"filename": "g.txt", "content": "http://a/g.txt"},
                ],
            },
        }
        t = self._prov()._issue_to_ticket(issue)
        assert len(t.comments) == 1 and "real" in t.comments[0]
        assert [a.url for a in t.attachments] == ["http://a/g.txt"]

    def test_no_key_uses_base_url_as_browse(self):
        t = self._prov()._issue_to_ticket({"fields": {"summary": "s"}})
        assert t.app_url == "https://acme.atlassian.net"


class TestJiraFetchTestStates:
    def _prov(self, base_url="https://acme.atlassian.net"):
        return JiraProvider(
            TicketProviderConfig(
                provider="jira", base_url=base_url, email="e@x.com", api_token="tok"
            )
        )

    async def test_fetch_returns_ticket(self):
        issue = {"key": "P-1", "fields": {"summary": "s", "created": "2025-01-01"}}
        session = _FakeSession(get_responses=[_FakeResp(200, json_data=issue)])
        with _patch_session(session):
            t = await self._prov().fetch("P-1")
        assert t.id == "P-1"

    async def test_fetch_non_200_raises(self):
        session = _FakeSession(get_responses=[_FakeResp(404, text_data="nope")])
        with _patch_session(session):
            with pytest.raises(ProviderError, match="404"):
                await self._prov().fetch("P-9")

    async def test_test_connection_no_base_url(self):
        identity, err = await self._prov(base_url="").test_connection()
        assert identity is None and "no Jira site URL" in err

    async def test_test_connection_rejected(self):
        session = _FakeSession(get_responses=[_FakeResp(401)])
        with _patch_session(session):
            identity, err = await self._prov().test_connection()
        assert identity is None and "rejected" in err

    async def test_test_connection_other_status(self):
        session = _FakeSession(get_responses=[_FakeResp(500)])
        with _patch_session(session):
            identity, err = await self._prov().test_connection()
        assert identity is None and "500" in err

    async def test_test_connection_success(self):
        session = _FakeSession(
            get_responses=[
                _FakeResp(200, json_data={"accountId": "acc-1", "displayName": "Me"})
            ]
        )
        with _patch_session(session):
            identity, err = await self._prov().test_connection()
        assert err == "" and identity == {"member_id": "acc-1", "name": "Me"}

    async def test_test_connection_network_error(self):
        import aiohttp

        with patch("aiohttp.ClientSession", side_effect=aiohttp.ClientError("x")):
            identity, err = await self._prov().test_connection()
        assert identity is None and "network error" in err

    async def test_list_states_no_base_url_raises(self):
        with pytest.raises(ProviderError, match="no Jira site URL"):
            await self._prov(base_url="").list_states()

    async def test_list_states_non_200_raises(self):
        session = _FakeSession(get_responses=[_FakeResp(500, text_data="err")])
        with _patch_session(session):
            with pytest.raises(ProviderError, match="500"):
                await self._prov().list_states()

    async def test_list_states_dedups_and_names(self):
        statuses = [
            {"id": "1", "name": "To Do"},
            {"id": "1", "name": "dup"},  # duplicate id -> dropped
            {"id": "2"},  # no name -> falls back to id
            {"name": "no id"},  # no id -> dropped
        ]
        session = _FakeSession(get_responses=[_FakeResp(200, json_data=statuses)])
        with _patch_session(session):
            out = await self._prov().list_states()
        # No statusCategory -> type "" (bucket stays unparked).
        assert out == [
            {"id": "1", "name": "To Do", "type": ""},
            {"id": "2", "name": "2", "type": ""},
        ]

    async def test_list_states_maps_status_category_to_shared_type(self):
        # The assigned-tickets panel parks done-type buckets behind the Add menu
        # by reading type == "done", using the Shortcut adapter's vocabulary.
        statuses = [
            {"id": "1", "name": "To Do", "statusCategory": {"key": "new"}},
            {
                "id": "2",
                "name": "In Progress",
                "statusCategory": {"key": "indeterminate"},
            },
            {"id": "3", "name": "Done", "statusCategory": {"key": "done"}},
            {"id": "4", "name": "Odd", "statusCategory": {"key": "future-key"}},
        ]
        session = _FakeSession(get_responses=[_FakeResp(200, json_data=statuses)])
        with _patch_session(session):
            out = await self._prov().list_states()
        assert [(s["name"], s["type"]) for s in out] == [
            ("To Do", "unstarted"),
            ("In Progress", "started"),
            ("Done", "done"),
            ("Odd", ""),  # unknown category -> no type, never a made-up one
        ]

    async def test_list_states_names_match_ticket_state(self):
        # The panel matches Ticket.state against these names, so the two
        # spellings must agree exactly.
        prov = self._prov()
        status = {"id": "3", "name": "Ready for Dev", "statusCategory": {"key": "new"}}
        session = _FakeSession(get_responses=[_FakeResp(200, json_data=[status])])
        with _patch_session(session):
            states = await prov.list_states()
        ticket = prov._issue_to_ticket({"key": "P-1", "fields": {"status": status}})
        assert ticket.state == states[0]["name"]


# --------------------------------------------------------------------------- #
# Linear: fetch, test_connection, list_states, issue transform skips
# --------------------------------------------------------------------------- #
class TestLinearFetchTestStates:
    def _prov(self):
        return LinearProvider(TicketProviderConfig(provider="linear", api_token="k"))

    async def test_fetch_returns_ticket(self, monkeypatch):
        prov = self._prov()

        async def fake_gql(query, variables):
            return {"issue": {"identifier": "ENG-1", "title": "T"}}

        monkeypatch.setattr(prov, "_gql", fake_gql)
        t = await prov.fetch("ENG-1")
        assert t.id == "ENG-1" and t.slug == "lin-ENG-1"

    async def test_fetch_not_found_raises(self, monkeypatch):
        prov = self._prov()

        async def fake_gql(query, variables):
            return {"issue": None}

        monkeypatch.setattr(prov, "_gql", fake_gql)
        with pytest.raises(ProviderError, match="not found"):
            await prov.fetch("ENG-404")

    async def test_test_connection_success(self, monkeypatch):
        prov = self._prov()

        async def fake_gql(query, variables):
            return {"viewer": {"id": "u1", "name": "Me"}}

        monkeypatch.setattr(prov, "_gql", fake_gql)
        identity, err = await prov.test_connection()
        assert err == "" and identity == {"member_id": "u1", "name": "Me"}

    async def test_test_connection_provider_error(self, monkeypatch):
        prov = self._prov()

        async def fake_gql(query, variables):
            raise ProviderError("bad key")

        monkeypatch.setattr(prov, "_gql", fake_gql)
        identity, err = await prov.test_connection()
        assert identity is None and err == "bad key"

    async def test_test_connection_network_error(self, monkeypatch):
        import aiohttp

        prov = self._prov()

        async def fake_gql(query, variables):
            raise aiohttp.ClientError("down")

        monkeypatch.setattr(prov, "_gql", fake_gql)
        identity, err = await prov.test_connection()
        assert identity is None and "network error" in err

    async def test_list_states_team_prefix(self, monkeypatch):
        prov = self._prov()

        async def fake_gql(query, variables):
            return {
                "workflowStates": {
                    "nodes": [
                        {"id": "s1", "name": "Todo", "team": {"key": "ENG"}},
                        {"id": "s2", "name": "Done"},  # no team -> bare name
                        {"name": "no id"},  # dropped
                    ]
                }
            }

        monkeypatch.setattr(prov, "_gql", fake_gql)
        out = await prov.list_states()
        assert out == [
            {"id": "s1", "name": "ENG · Todo", "type": ""},
            {"id": "s2", "name": "Done", "type": ""},
        ]

    async def test_list_states_maps_linear_type_to_shared_vocabulary(self, monkeypatch):
        # Linear's own vocabulary is triage/backlog/unstarted/started/completed/
        # canceled; the panel's done-parking reads type == "done", so the
        # terminal states have to be translated, not passed through.
        prov = self._prov()
        nodes = [
            {"id": "a", "name": "Triage", "type": "triage"},
            {"id": "b", "name": "Backlog", "type": "backlog"},
            {"id": "c", "name": "Todo", "type": "unstarted"},
            {"id": "d", "name": "In Progress", "type": "started"},
            {"id": "e", "name": "Done", "type": "completed"},
            {"id": "f", "name": "Canceled", "type": "canceled"},
            {"id": "g", "name": "Future", "type": "something-new"},
        ]

        async def fake_gql(query, variables):
            assert "type" in query  # the type has to be asked for
            return {"workflowStates": {"nodes": nodes}}

        monkeypatch.setattr(prov, "_gql", fake_gql)
        out = await prov.list_states()
        assert [(s["name"], s["type"]) for s in out] == [
            ("Triage", "unstarted"),
            ("Backlog", "unstarted"),
            ("Todo", "unstarted"),
            ("In Progress", "started"),
            ("Done", "done"),
            ("Canceled", "done"),  # Shortcut's "Won't do" equivalent
            ("Future", ""),  # unknown type -> no type, never a made-up one
        ]

    async def test_list_states_names_match_ticket_state(self, monkeypatch):
        # The panel matches Ticket.state against these names, so the two
        # spellings (team prefix included) must agree exactly.
        prov = self._prov()
        state = {
            "id": "s1",
            "name": "Todo",
            "type": "unstarted",
            "team": {"key": "ENG"},
        }

        async def fake_gql(query, variables):
            return {"workflowStates": {"nodes": [state]}}

        monkeypatch.setattr(prov, "_gql", fake_gql)
        states = await prov.list_states()
        ticket = prov._issue_to_ticket({"identifier": "ENG-1", "state": state})
        assert ticket.state == states[0]["name"] == "ENG · Todo"

    def test_missing_state_leaves_state_empty(self):
        # No state in the payload -> "" (the panel's "No state" bucket).
        assert self._prov()._issue_to_ticket({"identifier": "ENG-1"}).state == ""

    def test_issue_transform_skips_blank_comment_and_urlless_attachment(self):
        prov = self._prov()
        issue = {
            "identifier": "ENG-2",
            "title": "T",
            "description": "d",
            "comments": {
                "nodes": [
                    {"body": "  ", "user": {"name": "x"}},  # blank -> skipped
                    {"body": "real", "createdAt": "2025-01-01"},  # no user -> unknown
                ]
            },
            "attachments": {
                "nodes": [
                    {"title": "no url"},  # no url -> skipped
                    {"url": "http://a", "title": "shot"},
                ]
            },
        }
        t = prov._issue_to_ticket(issue)
        assert t.comments == ["[2025-01-01 by unknown] real"]
        assert [a.url for a in t.attachments] == ["http://a"]
