"""Merging one ticket into another (Intake → Tickets → Merge into…).

The feature writes to somebody else's tracker and then DELETES a ticket, so the
things worth pinning here are not the happy path — they are the partial ones:

1. **The merged-in text.** It is assembled once, in markdown, for four providers
   whose description formats do not agree, and it is the only surviving copy of
   the deleted ticket's content. It must carry description, criteria, comments
   and attachment names, and it must not reach back and change how the SURVIVING
   ticket's own acceptance criteria are mined.
2. **The order of the four writes, and every way it can half-fail.** The order
   is chosen so a late failure is survivable (see the module docstring); a test
   that only covered "all four succeeded" would not notice the order being
   swapped into the arrangement that loses work.
3. **The provider writes themselves** — that each adapter hits the endpoint that
   exists, appends rather than replaces, and reports a refusal as a refusal.
4. **The route contract**, including the one that matters most: a tracker that
   refuses the delete gets a 200 saying so, not a 502 that leaves the user
   believing nothing happened.

No network: the adapters go through the same ``_FakeSession`` stand-in the other
provider tests use, and the orchestration tests use a recording fake provider.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

from backend.ticket_ingestion.config import TicketProviderConfig
from backend.ticket_ingestion.models import Attachment
from backend.ticket_ingestion.providers import get_provider
from backend.ticket_ingestion.providers.base import ProviderError, TicketProvider
from backend.ticket_ingestion.providers.github_issues import GithubIssuesProvider
from backend.ticket_ingestion.providers.jira import JiraProvider, text_to_adf
from backend.ticket_ingestion.providers.linear import LinearProvider
from backend.ticket_ingestion.providers.shortcut import ShortcutProvider
from backend.web import server
from backend.web.core import ticket_merge
from tests._factories import make_ticket
from tests.unit.test_ticket_providers import _FakeResp, _FakeSession, _patch_session

client = TestClient(server.app)

_WHEN = datetime(2026, 3, 4, 9, 30, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# 1. The merged-in section
# --------------------------------------------------------------------------- #
def _rich(**over):
    base = dict(
        id=41,
        slug="sc-41",
        name="Login spinner never stops",
        description="The spinner spins forever after a failed login.",
        acceptance_criteria=["Spinner stops", "An error is shown"],
        comments=["[2026-01-01 by alice] happens on staging too"],
        attachments=[Attachment(name="shot.png", url="https://files/shot.png")],
        app_url="https://app.shortcut.com/story/41",
    )
    base.update(over)
    return make_ticket(**base)


def test_section_carries_every_part_of_the_ticket():
    text = ticket_merge.merged_section(_rich(), "sc-38", when=_WHEN)
    assert "Merged from sc-41 — Login spinner never stops" in text
    assert "The spinner spins forever after a failed login." in text
    assert "- Spinner stops" in text
    assert "- An error is shown" in text
    assert "shot.png" in text and "https://files/shot.png" in text
    assert "happens on staging too" in text
    # Where it went, when, and that the original is gone — the merged block is
    # the only place that survives.
    assert "Folded into sc-38 on 2026-03-04 and deleted." in text
    assert "https://app.shortcut.com/story/41" in text


def test_section_does_not_hijack_the_survivors_acceptance_criteria():
    """The criteria heading is qualified for a load-bearing reason.

    ``parse_acceptance_criteria`` enters its criteria section on a line matching
    ``^#+ acceptance criteria$`` exactly, and once it has found one it STOPS
    falling back to mining the description's other bullets. An unqualified
    heading in the merged block would therefore silently change how the
    surviving ticket's own criteria are read the next time it is ingested —
    the merge rewriting the meaning of text it never touched.
    """
    from backend.ticket_ingestion.providers.base import parse_acceptance_criteria

    survivor = "- B's own first criterion\n- B's own second criterion\n"
    merged = survivor + ticket_merge.merged_section(_rich(), "sc-38", when=_WHEN)
    mined = parse_acceptance_criteria(merged)
    # B's own bullets still mine, which is what the unqualified heading would
    # have broken.
    assert "B's own first criterion" in mined
    assert "B's own second criterion" in mined
    # …and A's come along with them rather than being lost.
    assert "Spinner stops" in mined


def test_section_uses_only_the_four_shapes_jira_can_translate():
    """Everything emitted must survive ``text_to_adf``, or it renders as
    literal punctuation on the one provider whose description is not markdown.

    Two halves: no markdown the translator does not understand goes in, and
    what comes out really is the four structured node kinds rather than one
    undifferentiated paragraph per line.
    """
    text = ticket_merge.merged_section(_rich(), "sc-38", when=_WHEN)
    # Bold/italic/code marks have no ADF translation here, so the section must
    # not reach for them — the headings carry the emphasis instead.
    assert "**" not in text and "`" not in text
    kinds = {n["type"] for n in text_to_adf(text)}
    assert kinds == {"rule", "heading", "bulletList", "paragraph"}


def test_section_flattens_a_multiline_comment_onto_one_bullet():
    story = _rich(comments=["[x by y] first line\n\nsecond line"])
    text = ticket_merge.merged_section(story, "sc-38", when=_WHEN)
    assert "- [x by y] first line second line" in text


def test_section_omits_the_parts_a_bare_ticket_does_not_have():
    story = make_ticket(
        id=9, slug="sc-9", name="Bare", description="", acceptance_criteria=[]
    )
    text = ticket_merge.merged_section(story, "sc-1", when=_WHEN)
    assert "Acceptance criteria" not in text
    assert "Attachments" not in text
    assert "Comments" not in text


def test_audit_comment_names_both_sides_and_the_original_url():
    line = ticket_merge.audit_comment(_rich(), _rich(id=38, slug="sc-38"), when=_WHEN)
    assert "sc-41" in line and "sc-38" in line
    assert "https://app.shortcut.com/story/41" in line
    assert "2026-03-04 09:30 UTC" in line


# --------------------------------------------------------------------------- #
# 2. The orchestration, and every way it can half-fail
# --------------------------------------------------------------------------- #
class _RecordingProvider(TicketProvider):
    """A merge-capable adapter that records the calls and can be told to fail
    any one of them."""

    name = "rec"
    label = "Recorder"
    slug_prefix = "rec"
    can_merge = True

    def __init__(self, cfg=None, *, fail: dict | None = None, tickets=None) -> None:
        super().__init__(cfg or TicketProviderConfig(provider="rec", id="rec"))
        self.calls: list[str] = []
        self.fail = fail or {}
        self.tickets = tickets or {}
        self.appended = ""

    async def search_assigned(self, since):  # pragma: no cover - unused
        return []

    async def fetch(self, ticket_id):
        self.calls.append(f"fetch:{ticket_id}")
        if "fetch" in self.fail:
            raise ProviderError(self.fail["fetch"])
        return self.tickets[str(ticket_id)]

    async def append_description(self, ticket_id, addition):
        self.calls.append(f"append:{ticket_id}")
        if "append" in self.fail:
            raise ProviderError(self.fail["append"])
        self.appended = addition

    async def add_comment(self, ticket_id, body):
        self.calls.append(f"comment:{ticket_id}")
        if "comment" in self.fail:
            raise ProviderError(self.fail["comment"])

    async def carry_attachments(self, from_id, to_id):
        self.calls.append(f"carry:{from_id}->{to_id}")
        if "carry" in self.fail:
            raise ProviderError(self.fail["carry"])
        return ["shot.png"], []

    async def delete_ticket(self, ticket_id):
        self.calls.append(f"delete:{ticket_id}")
        if "delete" in self.fail:
            raise ProviderError(self.fail["delete"])


def _wire(monkeypatch, provider):
    """Point ticket_merge's source resolution at ``provider``."""
    cfg = TicketProviderConfig(provider="rec", id="rec", label="Recorder")
    monkeypatch.setattr(ticket_merge, "_resolve_source", lambda source: (cfg, provider))
    return provider


def _pair():
    return {
        "41": _rich(),
        "38": _rich(id=38, slug="sc-38", name="Spinner bug", app_url="u/38"),
    }


@pytest.mark.asyncio
async def test_merge_runs_the_four_writes_in_the_safe_order(monkeypatch):
    """Append first, delete LAST.

    The order is the whole design: a failure at the append leaves both tickets
    untouched, and a failure at the delete leaves a duplicated ticket rather
    than an erased one. Swapped, there is a partial outcome that loses work
    permanently — which no other assertion in this file would catch.
    """
    p = _wire(monkeypatch, _RecordingProvider(tickets=_pair()))
    r = await ticket_merge.merge_tickets("rec", "41", "38")
    writes = [c for c in p.calls if not c.startswith("fetch:")]
    assert writes == ["append:38", "carry:41->38", "comment:38", "delete:41"]
    assert r["deleted"] is True and r["delete_error"] == ""
    assert r["from"]["slug"] == "sc-41" and r["into"]["slug"] == "sc-38"
    assert r["attachments_moved"] == ["shot.png"]


@pytest.mark.asyncio
async def test_both_tickets_are_fetched_before_anything_is_written(monkeypatch):
    """A typo'd target must fail with both tickets intact, not after A's content
    has been posted somewhere it does not belong."""
    p = _wire(monkeypatch, _RecordingProvider(tickets={"41": _rich()}))
    with pytest.raises(KeyError):
        await ticket_merge.merge_tickets("rec", "41", "999")
    assert not [c for c in p.calls if not c.startswith("fetch:")]


@pytest.mark.asyncio
async def test_a_failed_append_aborts_and_touches_nothing_else(monkeypatch):
    p = _wire(
        monkeypatch,
        _RecordingProvider(tickets=_pair(), fail={"append": "field is read-only"}),
    )
    with pytest.raises(ProviderError):
        await ticket_merge.merge_tickets("rec", "41", "38")
    assert "delete:41" not in p.calls
    assert "carry:41->38" not in p.calls


@pytest.mark.asyncio
async def test_a_failed_delete_is_reported_not_raised(monkeypatch):
    """The single most important behaviour here. The content has already landed
    on the survivor, so "merged, but I could not delete it" is a true sentence
    the UI can act on — where raising would tell the user nothing happened."""
    p = _wire(
        monkeypatch,
        _RecordingProvider(tickets=_pair(), fail={"delete": "needs admin"}),
    )
    r = await ticket_merge.merge_tickets("rec", "41", "38")
    assert r["deleted"] is False
    assert "needs admin" in r["delete_error"]
    assert "append:38" in p.calls  # the merge itself still happened


@pytest.mark.asyncio
async def test_a_failed_comment_or_carry_does_not_stop_the_merge(monkeypatch):
    p = _wire(
        monkeypatch,
        _RecordingProvider(
            tickets=_pair(), fail={"comment": "no permission", "carry": "gone"}
        ),
    )
    r = await ticket_merge.merge_tickets("rec", "41", "38")
    assert r["deleted"] is True
    assert "no permission" in r["comment_error"]
    # Every file the source had is reported unmoved rather than silently lost.
    assert r["attachments_failed"] == ["shot.png"]
    assert "delete:41" in p.calls


@pytest.mark.asyncio
async def test_files_that_travel_as_links_are_reported_as_linked(monkeypatch):
    """A provider whose uploads outlive the ticket reports nothing moved. That
    is correct, not a failure — and the payload says which, so the UI can say
    "3 file links" instead of an ominous zero."""

    class _Linky(_RecordingProvider):
        async def carry_attachments(self, from_id, to_id):
            self.calls.append("carry")
            return [], []

    p = _wire(monkeypatch, _Linky(tickets=_pair()))
    r = await ticket_merge.merge_tickets("rec", "41", "38")
    assert r["attachments_moved"] == [] and r["attachments_failed"] == []
    assert r["attachments_linked"] == ["shot.png"]
    assert p.calls  # sanity: it really did go through the merge


@pytest.mark.asyncio
async def test_a_ticket_cannot_be_merged_into_itself(monkeypatch):
    _wire(monkeypatch, _RecordingProvider(tickets=_pair()))
    with pytest.raises(ValueError):
        await ticket_merge.merge_tickets("rec", "41", "41")


@pytest.mark.asyncio
async def test_a_read_only_provider_is_refused_before_any_fetch(monkeypatch):
    class _ReadOnly(_RecordingProvider):
        can_merge = False

    p = _wire(monkeypatch, _ReadOnly(tickets=_pair()))
    with pytest.raises(ValueError, match="read-only"):
        await ticket_merge.merge_tickets("rec", "41", "38")
    assert p.calls == []


# --------------------------------------------------------------------------- #
# 3. The provider writes
# --------------------------------------------------------------------------- #
def test_only_the_four_supported_providers_advertise_merging():
    """Asana is read-only on purpose — it is not in the first cut, and a
    ``can_merge`` that drifted True there would offer a button that deletes a
    task and loses its files."""
    ready = {
        name: get_provider(TicketProviderConfig(provider=name)).can_merge
        for name in ("shortcut", "jira", "linear", "github_issues", "asana")
    }
    assert ready == {
        "shortcut": True,
        "jira": True,
        "linear": True,
        "github_issues": True,
        "asana": False,
    }


@pytest.mark.asyncio
async def test_a_read_only_adapter_refuses_by_name():
    p = get_provider(TicketProviderConfig(provider="asana"))
    with pytest.raises(ProviderError, match="Asana"):
        await p.delete_ticket("1")
    with pytest.raises(ProviderError, match="cannot be merged"):
        await p.append_description("1", "x")


class _FakeBytesResp(_FakeResp):
    """``_FakeResp`` that can also be ``read()`` as bytes — the only write path
    that downloads a body rather than parsing JSON is Jira's attachment carry."""

    def __init__(self, *a, blob: bytes = b"\x89PNG", **kw):
        super().__init__(*a, **kw)
        self._blob = blob

    async def read(self):
        return self._blob


class _FakeSessionRW(_FakeSession):
    """``_FakeSession`` plus the verbs the write paths use."""

    def __init__(self, **kw):
        put = kw.pop("put_responses", None)
        patch_ = kw.pop("patch_responses", None)
        delete = kw.pop("delete_responses", None)
        super().__init__(**kw)
        self._put = list(put or [])
        self._patch = list(patch_ or [])
        self._delete = list(delete or [])
        self.put_calls: list = []
        self.patch_calls: list = []
        self.delete_calls: list = []

    def put(self, url, **kwargs):
        self.put_calls.append((url, kwargs))
        return self._put.pop(0)

    def patch(self, url, **kwargs):
        self.patch_calls.append((url, kwargs))
        return self._patch.pop(0)

    def delete(self, url, **kwargs):
        self.delete_calls.append((url, kwargs))
        return self._delete.pop(0)


@pytest.mark.asyncio
async def test_shortcut_append_keeps_what_was_already_there():
    p = ShortcutProvider(TicketProviderConfig(api_token="t"))
    s = _FakeSessionRW(
        get_responses=[_FakeResp(json_data={"id": 38, "description": "ORIGINAL"})],
        put_responses=[_FakeResp(json_data={})],
    )
    with _patch_session(s):
        await p.append_description("38", "\n\nADDED")
    assert s.put_calls[0][1]["json"]["description"] == "ORIGINAL\n\nADDED"


@pytest.mark.asyncio
async def test_shortcut_carries_files_by_reference_and_unions_them():
    """Shortcut files are workspace entities a story merely references, so the
    carry is one PUT and the bytes never move — and the target's own files must
    survive it."""
    p = ShortcutProvider(TicketProviderConfig(api_token="t"))
    s = _FakeSessionRW(
        get_responses=[
            _FakeResp(json_data={"id": 41, "files": [{"id": 7, "name": "shot.png"}]}),
            _FakeResp(json_data={"id": 38, "files": [{"id": 3, "name": "own.png"}]}),
        ],
        put_responses=[_FakeResp(json_data={})],
    )
    with _patch_session(s):
        moved, failed = await p.carry_attachments("41", "38")
    assert s.put_calls[0][1]["json"]["file_ids"] == [3, 7]
    assert moved == ["shot.png"] and failed == []


@pytest.mark.asyncio
async def test_shortcut_delete_hits_the_story_endpoint():
    p = ShortcutProvider(TicketProviderConfig(api_token="t"))
    s = _FakeSessionRW(delete_responses=[_FakeResp(status=204)])
    with _patch_session(s):
        await p.delete_ticket("41")
    assert s.delete_calls[0][0].endswith("/stories/41")


@pytest.mark.asyncio
async def test_shortcut_delete_refusal_becomes_a_provider_error():
    p = ShortcutProvider(TicketProviderConfig(api_token="t"))
    s = _FakeSessionRW(delete_responses=[_FakeResp(status=403, text_data="nope")])
    with _patch_session(s):
        with pytest.raises(ProviderError, match="403"):
            await p.delete_ticket("41")


@pytest.mark.asyncio
async def test_jira_append_preserves_the_existing_adf_tree():
    """Read-modify-write on the RAW ADF: writing back the flattened text would
    strip every table, panel and code block the issue already had."""
    p = JiraProvider(
        TicketProviderConfig(
            base_url="https://x.atlassian.net", email="e", api_token="t"
        )
    )
    existing = {
        "type": "doc",
        "version": 1,
        "content": [{"type": "codeBlock", "content": [{"type": "text", "text": "x"}]}],
    }
    s = _FakeSessionRW(
        get_responses=[_FakeResp(json_data={"fields": {"description": existing}})],
        put_responses=[_FakeResp(status=204)],
    )
    with _patch_session(s):
        await p.append_description(
            "PROJ-1", "\n\n---\n\n#### Merged from jira-PROJ-9\n"
        )
    doc = s.put_calls[0][1]["json"]["fields"]["description"]
    assert doc["content"][0] == existing["content"][0]  # untouched, and first
    assert {n["type"] for n in doc["content"][1:]} == {"rule", "heading"}


@pytest.mark.asyncio
async def test_jira_append_handles_an_issue_with_no_description_yet():
    p = JiraProvider(
        TicketProviderConfig(
            base_url="https://x.atlassian.net", email="e", api_token="t"
        )
    )
    s = _FakeSessionRW(
        get_responses=[_FakeResp(json_data={"fields": {"description": None}})],
        put_responses=[_FakeResp(status=204)],
    )
    with _patch_session(s):
        await p.append_description("PROJ-1", "hello")
    doc = s.put_calls[0][1]["json"]["fields"]["description"]
    assert doc["type"] == "doc" and doc["content"][0]["type"] == "paragraph"


@pytest.mark.asyncio
async def test_jira_carries_attachment_bytes_and_survives_one_bad_file():
    """Jira attachments die with their issue, so the bytes really have to move —
    and one unreadable file must not cost the others."""
    p = JiraProvider(
        TicketProviderConfig(
            base_url="https://x.atlassian.net", email="e", api_token="t"
        )
    )
    issue = {
        "key": "PROJ-9",
        "fields": {
            "summary": "S",
            "created": "2026-01-01T00:00:00.000+0000",
            "attachment": [
                {"filename": "ok.png", "content": "https://j/ok.png"},
                {"filename": "bad.png", "content": "https://j/bad.png"},
            ],
        },
    }
    s = _FakeSessionRW(
        get_responses=[
            _FakeResp(json_data=issue),  # fetch()
            _FakeBytesResp(status=200),  # download ok.png
            _FakeBytesResp(status=404),  # download bad.png
        ],
        post_responses=[_FakeResp(status=200, json_data=[{}])],  # upload ok.png
    )
    with _patch_session(s):
        moved, failed = await p.carry_attachments("PROJ-9", "PROJ-1")
    assert moved == ["ok.png"] and failed == ["bad.png"]
    assert s.post_calls[0][0].endswith("/rest/api/3/issue/PROJ-1/attachments")
    # Jira's XSRF check rejects a multipart POST without this header.
    assert s.post_calls[0][1]["headers"]["X-Atlassian-Token"] == "no-check"


@pytest.mark.asyncio
async def test_jira_delete_asks_for_subtasks_and_explains_a_403():
    p = JiraProvider(
        TicketProviderConfig(
            base_url="https://x.atlassian.net", email="e", api_token="t"
        )
    )
    s = _FakeSessionRW(delete_responses=[_FakeResp(status=204)])
    with _patch_session(s):
        await p.delete_ticket("PROJ-9")
    assert s.delete_calls[0][1]["params"] == {"deleteSubtasks": "true"}

    s2 = _FakeSessionRW(delete_responses=[_FakeResp(status=403, text_data="no")])
    with _patch_session(s2):
        with pytest.raises(ProviderError, match="Delete Issues permission"):
            await p.delete_ticket("PROJ-9")


@pytest.mark.asyncio
async def test_linear_resolves_the_identifier_to_a_uuid_before_mutating():
    """Every Linear mutation takes the UUID, while everything the UI hands
    around is the human identifier — one query bridges the two."""
    p = LinearProvider(TicketProviderConfig(api_token="lin_api_x"))
    s = _FakeSessionRW(
        post_responses=[
            _FakeResp(
                json_data={"data": {"issue": {"id": "uuid-1", "description": "OLD"}}}
            ),
            _FakeResp(json_data={"data": {"issueUpdate": {"success": True}}}),
        ]
    )
    with _patch_session(s):
        await p.append_description("ENG-5", "\n\nNEW")
    variables = s.post_calls[1][1]["json"]["variables"]
    assert variables["id"] == "uuid-1"
    assert variables["description"] == "OLD\n\nNEW"


@pytest.mark.asyncio
async def test_linear_delete_uses_issue_delete():
    p = LinearProvider(TicketProviderConfig(api_token="lin_api_x"))
    s = _FakeSessionRW(
        post_responses=[
            _FakeResp(
                json_data={"data": {"issue": {"id": "uuid-1", "description": ""}}}
            ),
            _FakeResp(json_data={"data": {"issueDelete": {"success": True}}}),
        ]
    )
    with _patch_session(s):
        await p.delete_ticket("ENG-5")
    assert "issueDelete" in s.post_calls[1][1]["json"]["query"]


@pytest.mark.asyncio
async def test_linear_reports_an_unsuccessful_mutation():
    p = LinearProvider(TicketProviderConfig(api_token="lin_api_x"))
    s = _FakeSessionRW(
        post_responses=[
            _FakeResp(json_data={"data": {"issue": {"id": "uuid-1"}}}),
            _FakeResp(json_data={"data": {"issueDelete": {"success": False}}}),
        ]
    )
    with _patch_session(s):
        with pytest.raises(ProviderError, match="refused to delete"):
            await p.delete_ticket("ENG-5")


@pytest.mark.asyncio
async def test_github_append_patches_the_body():
    p = GithubIssuesProvider(TicketProviderConfig(api_token="t", project="o/r"))
    s = _FakeSessionRW(
        get_responses=[_FakeResp(json_data={"number": 7, "body": "ORIGINAL"})],
        patch_responses=[_FakeResp(status=200, json_data={})],
    )
    with _patch_session(s):
        await p.append_description("7", "\n\nADDED")
    assert s.patch_calls[0][1]["json"]["body"] == "ORIGINAL\n\nADDED"


@pytest.mark.asyncio
async def test_github_keeps_the_base_class_link_carry():
    """GitHub has no attachment API: an image dropped into an issue is a
    markdown link to user-content that outlives the issue, so copying the body
    IS carrying the file."""
    p = GithubIssuesProvider(TicketProviderConfig(api_token="t", project="o/r"))
    assert await p.carry_attachments("7", "8") == ([], [])


@pytest.mark.asyncio
async def test_github_delete_goes_through_graphql_and_names_the_permission():
    p = GithubIssuesProvider(TicketProviderConfig(api_token="t", project="o/r"))
    s = _FakeSessionRW(
        get_responses=[_FakeResp(json_data={"number": 7, "node_id": "I_abc"})],
        post_responses=[
            _FakeResp(
                status=200,
                json_data={"errors": [{"message": "Resource not accessible"}]},
            )
        ],
    )
    with _patch_session(s):
        with pytest.raises(ProviderError, match="admin permission"):
            await p.delete_ticket("7")
    assert s.post_calls[0][0].endswith("/graphql")
    assert s.post_calls[0][1]["json"]["variables"] == {"id": "I_abc"}


# --------------------------------------------------------------------------- #
# 4. The route
# --------------------------------------------------------------------------- #
def _result(**over):
    base = {
        "source": "sc",
        "from": {"id": "41", "slug": "sc-41", "name": "A", "url": "u/41"},
        "into": {"id": "38", "slug": "sc-38", "name": "B", "url": "u/38"},
        "comments_copied": 1,
        "attachments_moved": [],
        "attachments_failed": [],
        "attachments_linked": [],
        "comment_error": "",
        "deleted": True,
        "delete_error": "",
    }
    base.update(over)
    return base


def test_route_requires_all_three_ids():
    for body in ({"source": "sc", "from": "41"}, {"from": "41", "into": "38"}, {}):
        assert client.post("/api/tickets/merge", json=body).status_code == 400


def test_route_returns_the_merge_record():
    async def _fake(source, from_id, into_id):
        assert (source, from_id, into_id) == ("sc", "41", "38")
        return _result()

    with patch.object(server._ticket_merge, "merge_tickets", _fake):
        r = client.post(
            "/api/tickets/merge", json={"source": "sc", "from": "41", "into": "38"}
        )
    assert r.status_code == 200
    assert r.json()["deleted"] is True


def test_route_reports_a_refused_delete_as_a_success_with_a_reason():
    """NOT a 502. The content is already on the survivor by the time the delete
    is attempted, so a 502 here would tell the user nothing happened when in
    fact almost everything did."""

    async def _fake(source, from_id, into_id):
        return _result(deleted=False, delete_error="needs admin on the repo")

    with patch.object(server._ticket_merge, "merge_tickets", _fake):
        r = client.post(
            "/api/tickets/merge", json={"source": "gh", "from": "7", "into": "8"}
        )
    assert r.status_code == 200
    body = r.json()
    assert body["deleted"] is False
    assert "needs admin" in body["delete_error"]


@pytest.mark.parametrize(
    "err, status",
    [
        (ValueError("a ticket cannot be merged into itself"), 400),
        (LookupError("No ticketing source 'nope' is configured"), 404),
        (RuntimeError("Shortcut API returned 401"), 502),
    ],
)
def test_route_maps_failures_to_the_right_status(err, status):
    async def _fake(source, from_id, into_id):
        raise err

    with patch.object(server._ticket_merge, "merge_tickets", _fake):
        r = client.post(
            "/api/tickets/merge", json={"source": "sc", "from": "41", "into": "38"}
        )
    assert r.status_code == status


def test_route_drops_the_panel_cache_so_the_deleted_row_stops_being_listed():
    async def _fake(source, from_id, into_id):
        return _result()

    server._ASSIGNED_TICKETS_CACHE["v"] = (1e18, {"tickets": [{"id": "41"}]})
    with patch.object(server._ticket_merge, "merge_tickets", _fake):
        client.post(
            "/api/tickets/merge", json={"source": "sc", "from": "41", "into": "38"}
        )
    assert "v" not in server._ASSIGNED_TICKETS_CACHE


def test_listing_stamps_merge_readiness_on_every_row():
    """The UI hides the control on a source whose adapter cannot write, and it
    reads the adapter's own answer rather than keeping a second list of
    "providers that support merging" to disagree with it."""
    import inspect

    from backend.web.core import ticket_start

    src = inspect.getsource(ticket_start.list_assigned_tickets)
    assert '"merge_ready": merge_ready' in src
    assert 'getattr(provider, "can_merge", False)' in src


# --------------------------------------------------------------------------- #
# 5. End to end through the real seams
# --------------------------------------------------------------------------- #
def test_route_through_the_real_orchestrator_and_a_real_adapter(monkeypatch):
    """One test that stubs nothing but the socket.

    Everything above this point exercises a layer with its neighbours faked —
    the route with ``merge_tickets`` patched, the orchestration with a recording
    provider, the adapters with a fake session. That leaves the seams between
    them untested, which is where a renamed field or a changed call signature
    actually lands. This one goes HTTP request → route → merge_tickets →
    ShortcutProvider → (fake) socket and back, so the four have to agree.
    """
    src = TicketProviderConfig(provider="shortcut", id="sc", api_token="t")
    monkeypatch.setattr(
        ticket_merge,
        "_load_config",
        lambda: type("C", (), {"ticketing_sources": [src]})(),
    )
    story = {
        "id": 41,
        "name": "Login spinner never stops",
        "description": "It spins forever.",
        "created_at": "2026-01-01T00:00:00Z",
        "comments": [{"text": "seen on staging", "author_id": "alice"}],
        "files": [{"id": 7, "name": "shot.png", "url": "https://files/shot.png"}],
        "app_url": "https://app.shortcut.com/story/41",
    }
    target = {
        "id": 38,
        "name": "Spinner bug",
        "description": "ORIGINAL",
        "created_at": "2026-01-01T00:00:00Z",
        "app_url": "https://app.shortcut.com/story/38",
    }
    s = _FakeSessionRW(
        get_responses=[
            _FakeResp(json_data=story),  # fetch(41)
            _FakeResp(json_data=target),  # fetch(38)
            _FakeResp(json_data=target),  # append: read 38
            _FakeResp(json_data=story),  # carry: read 41
            _FakeResp(json_data=target),  # carry: read 38
        ],
        put_responses=[_FakeResp(json_data={}), _FakeResp(json_data={})],
        post_responses=[_FakeResp(status=201, json_data={})],  # the audit comment
        delete_responses=[_FakeResp(status=204)],
    )
    with _patch_session(s):
        r = client.post(
            "/api/tickets/merge", json={"source": "sc", "from": "41", "into": "38"}
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["deleted"] is True and body["delete_error"] == ""
    assert body["from"]["slug"] == "sc-41" and body["into"]["slug"] == "sc-38"
    assert body["attachments_moved"] == ["shot.png"]
    assert body["comments_copied"] == 1

    # The survivor's description KEPT its own text and gained the merged block.
    written = s.put_calls[0][1]["json"]["description"]
    assert written.startswith("ORIGINAL")
    assert "Merged from sc-41 — Login spinner never stops" in written
    assert "It spins forever." in written
    assert "seen on staging" in written
    # …the file was re-pointed rather than re-uploaded…
    assert s.put_calls[1][1]["json"]["file_ids"] == [7]
    # …the audit comment landed on the SURVIVOR, naming the deleted one…
    assert s.post_calls[0][0].endswith("/stories/38/comments")
    assert "sc-41" in s.post_calls[0][1]["json"]["text"]
    # …and the duplicate, not the survivor, is what got deleted.
    assert s.delete_calls[0][0].endswith("/stories/41")
