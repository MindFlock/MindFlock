"""A ticket source is connected only after a live test, and the form cannot
produce a broken one.

Pins: the catalog's required/auto markers and per-provider validation
(``validate_source``), the sources PUT's 400 shape, ``last_test`` carry-over
and the test route's ``record``, the explicit-null secret clear, the
any-assignee rule, and the three-way connection state Connections derives.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from backend.config import settings as S
from backend.ticket_ingestion.providers import (
    PROVIDER_META,
    missing_auto_fields,
    provider_meta,
    test_sensitive_keys as _sensitive_keys,
    validate_source,
)
from backend.web.addons import connections

_MASK = "•••set"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDFLOCK_SETTINGS_FILE", str(tmp_path / "settings.json"))
    monkeypatch.setenv("MINDFLOCK_AUTH", "0")
    S.invalidate()
    from backend.web.server import app

    with TestClient(app) as c:
        yield c
    S.invalidate()


def _put(client, *sources):
    return client.put(
        "/api/settings/ticketing/sources", json={"sources": list(sources)}
    )


# --------------------------------------------------------------------------- #
# Catalog: required markers and per-provider validation
# --------------------------------------------------------------------------- #
def test_every_server_required_field_is_marked_in_the_catalog():
    by_id = {p["id"]: {f["key"]: f for f in p["fields"]} for p in PROVIDER_META}
    assert by_id["shortcut"]["api_token"]["required"] is True
    # Filled by Test, still required before the source counts as connected.
    assert by_id["shortcut"]["member_id"]["required"] is True
    assert by_id["shortcut"]["member_id"]["auto"] is True
    assert "filled by Test" in by_id["shortcut"]["member_id"]["placeholder"]
    for key in ("base_url", "email", "api_token"):
        assert by_id["jira"][key]["required"] is True
    assert by_id["linear"]["api_token"]["required"] is True
    assert by_id["asana"]["api_token"]["required"] is True
    assert by_id["asana"]["project"]["required"] is True
    assert by_id["asana"]["project"]["auto"] is True
    # GitHub Issues stays the zero-config on-ramp.
    assert not any(f.get("required") for f in by_id["github_issues"].values())


def test_jira_and_linear_name_the_field_their_code_reads():
    # jira.py files into cfg.project as a project KEY; linear.py reads
    # cfg.project as a team KEY (search scope + create). The catalog now has
    # the field under the name the code means, so the error text matches.
    jira = provider_meta("jira")["fields"]
    linear = provider_meta("linear")["fields"]
    assert next(f for f in jira if f["key"] == "project")["label"] == "Project key"
    assert next(f for f in linear if f["key"] == "project")["label"] == "Team key"
    for fields in (jira, linear):
        assert next(f for f in fields if f["key"] == "project")["validate"] == "key"
        assert not next(f for f in fields if f["key"] == "project")["required"]


def test_asana_gets_scope_sections_and_a_start_section():
    keys = [f["key"] for f in provider_meta("asana")["fields"]]
    assert "assignee_scope" in keys
    assert "workflow_state" in keys and "start_state" in keys
    state = next(
        f for f in provider_meta("asana")["fields"] if f["key"] == "workflow_state"
    )
    assert state["type"] == "state" and "section" in state["label"].lower()


@pytest.mark.parametrize(
    "values, expected",
    [
        ({"provider": "shortcut"}, {"api_token": "Required"}),
        ({"provider": "shortcut", "api_token": "t"}, {}),
        (
            {"provider": "jira", "api_token": "t"},
            {"base_url": "Required", "email": "Required"},
        ),
        (
            {
                "provider": "jira",
                "api_token": "t",
                "base_url": "acme.atlassian.net",
                "email": "a",
            },
            {
                "base_url": "Must be a full URL starting with https://",
                "email": "Must be an email address",
            },
        ),
        (
            {
                "provider": "jira",
                "api_token": "t",
                "base_url": "https://acme.atlassian.net",
                "email": "a@b",
                "project": "bad key",
            },
            {"project": "A key like ENG — letters and digits, no spaces"},
        ),
        ({"provider": "linear"}, {"api_token": "Required"}),
        ({"provider": "linear", "api_token": "t", "project": "ENG"}, {}),
        # Asana's workspace is Test's to fill: not a save-time error…
        ({"provider": "asana", "api_token": "t"}, {}),
        # …but a typed one is checked for shape.
        (
            {"provider": "asana", "api_token": "t", "project": "acme"},
            {"project": "A numeric gid, e.g. 1201234567890"},
        ),
        ({"provider": "github_issues"}, {}),
        # Not in the catalog: nothing to check against (a hand-edited file).
        ({"provider": "github"}, {}),
    ],
)
def test_validate_source_per_provider(values, expected):
    assert validate_source(values) == expected


def test_auto_fields_are_required_before_connected_not_before_save():
    sc = {"provider": "shortcut", "api_token": "t"}
    assert validate_source(sc) == {}
    assert validate_source(sc, for_save=False) == {
        "member_id": "Filled in by Test — run it"
    }
    assert missing_auto_fields(sc) == ["member_id"]
    assert missing_auto_fields({**sc, "member_id": "m"}) == []
    assert missing_auto_fields({"provider": "asana", "api_token": "t"}) == ["project"]


def test_anyone_needs_an_ingest_state_per_field():
    base = {"provider": "shortcut", "api_token": "t", "assignee_scope": "anyone"}
    err = validate_source(base)
    assert set(err) == {"assignee_scope"}
    assert "ingest states" in err["assignee_scope"] and "label" in err["assignee_scope"]
    assert validate_source({**base, "workflow_state": "500"}) == {}
    # Shortcut can bound Anyone's with a label instead.
    assert validate_source({**base, "ingest_labels": "mindflock"}) == {}
    # Jira cannot: labels are Shortcut-only, so the rule wants a status.
    jira = {
        "provider": "jira",
        "api_token": "t",
        "base_url": "https://a.atlassian.net",
        "email": "a@b",
        "assignee_scope": "anyone",
        "ingest_labels": "x",
    }
    assert "assignee_scope" in validate_source(jira)
    assert "label" not in validate_source(jira)["assignee_scope"]
    assert validate_source({**jira, "workflow_state": "3"}) == {}
    # GitHub Issues is bounded by its repo, not a state.
    assert (
        validate_source({"provider": "github_issues", "assignee_scope": "anyone"}) == {}
    )
    # Asana is bounded by its sections.
    asana = {"provider": "asana", "api_token": "t", "assignee_scope": "anyone"}
    assert "sections" in validate_source(asana)["assignee_scope"]
    assert validate_source({**asana, "workflow_state": "77"}) == {}


def test_test_sensitive_keys_are_the_provider_and_its_catalog_fields():
    keys = _sensitive_keys("shortcut")
    assert keys[0] == "provider"
    assert {"api_token", "member_id", "workflow_state", "assignee_scope"} <= set(keys)
    for k in ("label", "repo_url", "agent", "effort", "depth"):
        assert k not in keys


# --------------------------------------------------------------------------- #
# PUT: 400 shapes, nothing written
# --------------------------------------------------------------------------- #
def test_put_refuses_a_broken_source_with_per_field_errors(client):
    r = _put(
        client,
        {"id": "sc", "provider": "shortcut"},
        {
            "id": "jira",
            "provider": "jira",
            "api_token": "t",
            "base_url": "nope",
            "email": "x",
        },
    )
    assert r.status_code == 400
    body = r.json()
    assert body["error"]
    assert body["errors"] == {
        "sc": {"api_token": "Required"},
        "jira": {
            "base_url": "Must be a full URL starting with https://",
            "email": "Must be an email address",
        },
    }
    # Nothing was written.
    assert client.get("/api/settings/ticketing/sources").json()["sources"] == []


def test_put_refuses_anyone_without_a_state_on_the_scope_field(client):
    r = _put(
        client,
        {
            "id": "sc",
            "provider": "shortcut",
            "api_token": "t",
            "assignee_scope": "anyone",
        },
    )
    assert r.status_code == 400
    assert list(r.json()["errors"]["sc"]) == ["assignee_scope"]
    r = _put(
        client,
        {
            "id": "sc",
            "provider": "shortcut",
            "api_token": "t",
            "assignee_scope": "anyone",
            "workflow_state": "500",
        },
    )
    assert r.status_code == 200


def test_put_saves_a_shortcut_source_before_test_fills_the_member_id(client):
    # member_id is required but Test's to fill — the save must go through so
    # Test has a stored source to record on.
    r = _put(client, {"id": "sc", "provider": "shortcut", "api_token": "t"})
    assert r.status_code == 200
    assert r.json()["sources"][0]["api_token"] == _MASK


def test_put_grandfathers_an_unchanged_stored_source(client):
    # A source saved before a rule existed must not block edits to its
    # neighbours — only a NEW or CHANGED source is validated.
    S.set_ticketing_sources([{"id": "old", "provider": "jira", "api_token": "t"}])
    r = _put(
        client,
        {"id": "old", "provider": "jira", "api_token": _MASK},
        {"id": "sc", "provider": "shortcut", "api_token": "tok"},
    )
    assert r.status_code == 200
    assert {s["id"] for s in r.json()["sources"]} == {"old", "sc"}
    # Touch the old one and it is held to the rules.
    r = _put(
        client, {"id": "old", "provider": "jira", "api_token": _MASK, "label": "x"}
    )
    assert r.status_code == 400
    assert set(r.json()["errors"]["old"]) == {"base_url", "email"}


# --------------------------------------------------------------------------- #
# Secrets: absent / "" / mask keep; explicit null clears
# --------------------------------------------------------------------------- #
def test_null_clears_the_stored_secret_blank_and_mask_keep_it(client):
    assert (
        _put(
            client, {"id": "sc", "provider": "shortcut", "api_token": "s3cret"}
        ).status_code
        == 200
    )

    def stored():
        return next(
            s for s in S.load_settings().ticketing.sources if s.id == "sc"
        ).api_token

    _put(client, {"id": "sc", "provider": "shortcut", "api_token": "", "label": "a"})
    assert stored() == "s3cret"
    _put(client, {"id": "sc", "provider": "shortcut", "api_token": _MASK, "label": "b"})
    assert stored() == "s3cret"
    _put(client, {"id": "sc", "provider": "shortcut", "label": "c"})
    assert stored() == "s3cret"
    # The Clear control: an explicit null clears the token on EVERY provider,
    # a required one included — the source is kept and reads "no credentials
    # yet" (off) instead of a 400 that would silently keep the old token.
    r = _put(
        client, {"id": "sc", "provider": "shortcut", "api_token": None, "label": "c"}
    )
    assert r.status_code == 200
    assert stored() == ""
    # …and clears on one whose token is optional.
    _put(client, {"id": "gh", "provider": "github_issues", "api_token": "ghp_x"})
    _put(client, {"id": "gh", "provider": "github_issues", "api_token": None})
    gh = next(s for s in S.load_settings().ticketing.sources if s.id == "gh")
    assert gh.api_token == ""
    assert (
        client.get("/api/settings/ticketing/sources").json()["sources"][0]["api_token"]
        == ""
    )


# --------------------------------------------------------------------------- #
# last_test: recorded by the test route, carried over by the PUT
# --------------------------------------------------------------------------- #
class _FakeProvider:
    def __init__(self, cfg):
        self.cfg = cfg

    async def test_connection(self):
        if self.cfg.api_token == "bad":
            return None, "Shortcut rejected the token (HTTP 401)"
        return {"member_id": "m-9", "name": "ethan"}, ""

    async def list_states(self):
        return [{"id": "500", "name": "Ready", "type": "unstarted", "extra": "dropped"}]


def _fake_registry(monkeypatch):
    import backend.ticket_ingestion.providers as providers

    monkeypatch.setattr(providers, "get_provider", lambda cfg: _FakeProvider(cfg))


def test_test_route_with_record_stamps_last_test_and_fills_member_id(
    client, monkeypatch
):
    _fake_registry(monkeypatch)
    _put(client, {"id": "sc", "provider": "shortcut", "api_token": "tok"})
    r = client.post(
        "/api/settings/test/ticketing", json={"id": "sc", "record": True}
    ).json()
    assert r["ok"] is True
    assert r["member_id"] == "m-9"
    assert r["message"] == "Connected — ethan"
    assert r["states"] == [{"id": "500", "name": "Ready", "type": "unstarted"}]
    assert "'tok'" not in str(r)  # the token itself never comes back
    src = r["source"]
    assert src["api_token"] == _MASK
    assert src["member_id"] == "m-9"
    lt = src["last_test"]
    assert lt["ok"] is True and lt["message"] == "Connected — ethan"
    assert lt["member_id"] == "m-9" and lt["states"] == r["states"]
    assert datetime.fromisoformat(lt["at"]).tzinfo is not None
    # Persisted, and read back by GET.
    got = client.get("/api/settings/ticketing/sources").json()["sources"][0]
    assert got["last_test"]["ok"] is True and got["member_id"] == "m-9"


def test_test_route_records_a_failure_too(client, monkeypatch):
    _fake_registry(monkeypatch)
    _put(client, {"id": "sc", "provider": "shortcut", "api_token": "bad"})
    r = client.post(
        "/api/settings/test/ticketing", json={"id": "sc", "record": True}
    ).json()
    assert r["ok"] is False and "401" in r["error"]
    assert r["source"]["last_test"] == {
        "ok": False,
        "at": r["source"]["last_test"]["at"],
        "message": "Shortcut rejected the token (HTTP 401)",
    }


def test_test_route_without_record_writes_nothing(client, monkeypatch):
    _fake_registry(monkeypatch)
    _put(client, {"id": "sc", "provider": "shortcut", "api_token": "tok"})
    r = client.post("/api/settings/test/ticketing", json={"id": "sc"}).json()
    assert r["ok"] is True and "source" not in r
    assert (
        "last_test"
        not in client.get("/api/settings/ticketing/sources").json()["sources"][0]
    )


def test_record_with_an_overriding_body_answers_but_does_not_record(
    client, monkeypatch
):
    # The test runs on the BODY's configuration; stamping that verdict onto the
    # stored source would mark a never-tested stored config connected.
    _fake_registry(monkeypatch)
    _put(client, {"id": "sc", "provider": "shortcut", "api_token": "bad"})
    r = client.post(
        "/api/settings/test/ticketing",
        json={"id": "sc", "record": True, "api_token": "tok"},
    ).json()
    assert r["ok"] is True
    assert r["recorded"] is False and "source" not in r
    assert r["message"] == "Connected — ethan — save first to record it"
    got = client.get("/api/settings/ticketing/sources").json()["sources"][0]
    assert "last_test" not in got and "member_id" not in got
    # A non-secret tested field counts too.
    r = client.post(
        "/api/settings/test/ticketing",
        json={"id": "sc", "record": True, "workflow_state": "500"},
    ).json()
    assert r["recorded"] is False


def test_record_with_a_body_that_resolves_to_the_stored_source_records(
    client, monkeypatch
):
    # Mask / blank secret means the stored one; blank fields fall back to the
    # stored values; equal values are no override — the card's own payload.
    _fake_registry(monkeypatch)
    _put(
        client,
        {
            "id": "sc",
            "provider": "shortcut",
            "api_token": "tok",
            "workflow_state": "500",
        },
    )
    r = client.post(
        "/api/settings/test/ticketing",
        json={
            "id": "sc",
            "provider": "shortcut",
            "record": True,
            "api_token": _MASK,
            "member_id": "",
            "workflow_state": "500",
        },
    ).json()
    assert r["ok"] is True and "recorded" not in r
    assert r["source"]["last_test"]["ok"] is True
    assert r["source"]["member_id"] == "m-9"


def test_put_keeps_a_test_filled_field_the_body_sends_blank(client, monkeypatch):
    # A blur while the post-save Test is in flight sends the card's stale ""
    # member id: that must not wipe the recorded one or drop the verdict.
    _fake_registry(monkeypatch)
    _put(client, {"id": "sc", "provider": "shortcut", "api_token": "tok"})
    client.post("/api/settings/test/ticketing", json={"id": "sc", "record": True})
    r = _put(
        client,
        {
            "id": "sc",
            "provider": "shortcut",
            "api_token": _MASK,
            "member_id": "",
            "label": "core",
        },
    )
    assert r.status_code == 200
    src = r.json()["sources"][0]
    assert src["member_id"] == "m-9" and src["label"] == "core"
    assert src["last_test"]["ok"] is True
    # A typed (non-blank) value is still the user's to change.
    r = _put(
        client,
        {"id": "sc", "provider": "shortcut", "api_token": _MASK, "member_id": "m-1"},
    )
    src = r.json()["sources"][0]
    assert src["member_id"] == "m-1" and "last_test" not in src


def test_put_keeps_the_asana_workspace_the_body_sends_blank(client):
    S.set_ticketing_sources(
        [
            {
                "id": "as",
                "provider": "asana",
                "api_token": "tok",
                "project": "1201",
                "member_id": "77",
            }
        ]
    )
    r = _put(
        client,
        {
            "id": "as",
            "provider": "asana",
            "api_token": _MASK,
            "project": "",
            "member_id": "",
        },
    )
    src = r.json()["sources"][0]
    assert src["project"] == "1201" and src["member_id"] == "77"


def test_put_carries_last_test_over_only_while_tested_fields_are_unchanged(
    client, monkeypatch
):
    _fake_registry(monkeypatch)
    _put(client, {"id": "sc", "provider": "shortcut", "api_token": "tok"})
    client.post("/api/settings/test/ticketing", json={"id": "sc", "record": True})
    # A label / repo / agent edit keeps the verdict…
    r = _put(
        client,
        {
            "id": "sc",
            "provider": "shortcut",
            "api_token": _MASK,
            "member_id": "m-9",
            "label": "core",
            "repo_url": "git@x",
        },
    )
    assert r.json()["sources"][0]["last_test"]["ok"] is True
    # …and the body's own last_test is never believed.
    r = _put(
        client,
        {
            "id": "sc",
            "provider": "shortcut",
            "api_token": _MASK,
            "member_id": "m-9",
            "label": "core",
            "repo_url": "git@x",
            "last_test": {
                "ok": True,
                "at": "2099-01-01T00:00:00+00:00",
                "message": "forged",
            },
        },
    )
    assert r.json()["sources"][0]["last_test"]["message"] == "Connected — ethan"
    # A new token drops it: the stored configuration is untested again.
    r = _put(
        client,
        {
            "id": "sc",
            "provider": "shortcut",
            "api_token": "tok2",
            "member_id": "m-9",
            "label": "core",
            "repo_url": "git@x",
        },
    )
    assert "last_test" not in r.json()["sources"][0]


def test_settings_roundtrip_keeps_last_test():
    s = S.Settings.from_dict(
        {
            "ticketing": {
                "sources": [
                    {
                        "id": "sc",
                        "provider": "shortcut",
                        "api_token": "t",
                        "last_test": {
                            "ok": True,
                            "at": "2026-10-10T00:00:00+00:00",
                            "message": "ok",
                        },
                    },
                    {"id": "x", "provider": "jira", "last_test": "junk"},
                ]
            }
        }
    )
    a, b = s.ticketing.sources
    assert a.last_test["ok"] is True
    assert a.to_dict()["last_test"]["message"] == "ok"
    assert b.last_test == {} and "last_test" not in b.to_dict()


# --------------------------------------------------------------------------- #
# Connections: connected means tested
# --------------------------------------------------------------------------- #
def _src(**kw):
    base = dict(
        id="sc",
        provider="shortcut",
        api_token="tok",
        member_id="m",
        project="",
        label="",
    )
    base.update(kw)
    return S.TicketingSource.from_dict(base)


def _at(age_s: float) -> str:
    return datetime.fromtimestamp(time.time() - age_s, tz=timezone.utc).isoformat()


def test_connection_state_ok_old_failed_no_token():
    ok = {"ok": True, "at": _at(60), "message": "Connected — ethan"}
    assert connections.source_test_state(_src(last_test=ok)) == (
        "connected",
        "Connected — ethan",
    )
    old = {"ok": True, "at": _at(25 * 3600), "message": "Connected — ethan"}
    state, msg = connections.source_test_state(_src(last_test=old))
    assert state == "needs_attention" and "over a day" in msg
    bad = {
        "ok": False,
        "at": _at(60),
        "message": "Shortcut rejected the token (HTTP 401)",
    }
    assert connections.source_test_state(_src(last_test=bad)) == (
        "needs_attention",
        "Shortcut rejected the token (HTTP 401)",
    )
    assert connections.source_test_state(_src(api_token="")) == (
        "off",
        "no credentials yet",
    )
    state, msg = connections.source_test_state(_src())
    assert state == "needs_attention" and "run Test" in msg
    # A pass that still has Test's field to fill is not connected either.
    state, msg = connections.source_test_state(_src(member_id="", last_test=ok))
    assert state == "needs_attention" and "member_id" in msg
    # An unparseable timestamp is treated as too old, never as fresh.
    state, _ = connections.source_test_state(_src(last_test={**ok, "at": "yesterday"}))
    assert state == "needs_attention"


def test_connections_endpoint_exposes_the_three_states_and_the_message(
    client, monkeypatch
):
    _fake_registry(monkeypatch)
    _put(client, {"id": "sc", "provider": "shortcut", "api_token": "tok"})
    client.post("/api/settings/test/ticketing", json={"id": "sc", "record": True})
    row = next(
        c
        for c in client.get("/api/connections?refresh=1").json()["connections"]
        if c["id"] == "ticketing"
    )
    assert row["status"] == "connected" and row["connection"] == "connected"
    assert row["message"] == "Connected — ethan" and row["tested_at"]
    # Change the token: untested again → needs attention, and the legacy status follows.
    _put(
        client,
        {"id": "sc", "provider": "shortcut", "api_token": "tok2", "member_id": "m-9"},
    )
    row = next(
        c
        for c in client.get("/api/connections?refresh=1").json()["connections"]
        if c["id"] == "ticketing"
    )
    assert row["status"] == "needs_attention" and row["connection"] == "needs_attention"
    assert row["legacy_status"] == "attention"
    assert "run Test" in row["message"]
    assert "tok2" not in str(row)
