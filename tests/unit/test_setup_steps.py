"""The first-run checklist (backend.web.core.setup_steps): its three groups
(get started / connect your work / optional, later), the settings shape and
its migrations, the hooks that mark steps (doctor / create / GitHub credential
/ ticket source / phone), ``GET /api/setup`` and the skip route, and the
wizard printing the same list."""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from backend import doctor
from backend.config import settings as S
from backend.web import server
from backend.web.core import setup_steps

STEP_IDS = ("tools", "agent_login", "repo", "github", "tracker", "phone")


def _check(cid, status, **kw):
    return doctor.Check(cid, kw.pop("label", cid), status, **kw)


# --------------------------------------------------------------------------- #
# settings shape + migration
# --------------------------------------------------------------------------- #
class TestSettings:
    def test_definition_three_required_two_to_connect_one_for_later(self):
        assert setup_steps.STEP_IDS == STEP_IDS
        assert setup_steps.REQUIRED == ("tools", "agent_login", "repo")
        assert setup_steps.OPTIONAL == ("github", "tracker", "phone")
        # The phone is never part of getting started: folded, uncounted.
        assert setup_steps.LATER == ("phone",)
        assert setup_steps.COUNTED == (
            "tools",
            "agent_login",
            "repo",
            "github",
            "tracker",
        )
        groups = [s["group"] for s in setup_steps.STEPS]
        assert groups == ["start", "start", "start", "connect", "connect", "later"]
        # Required is exactly the "start" group, so the UI's grouping and the
        # summary's required_done can never disagree.
        assert all((s["group"] == "start") == s["required"] for s in setup_steps.STEPS)

    def test_a_store_from_before_the_github_step_is_not_reasked(self):
        # "Connect a tracker and GitHub" was one step: settling it settled GitHub.
        for tracker in (
            {"done": True, "at": "2026-01-01T00:00:00+00:00"},
            {"skipped": True},
        ):
            g = S.GeneralSettings.from_dict({"setup": {"tracker": tracker}})
            assert g.setup["github"]["skipped"] is True, tracker
        # ...but a fresh store, or one that already has the step, is left alone.
        assert setup_steps.normalize({})["github"]["skipped"] is False
        assert (
            setup_steps.normalize(
                {"tracker": {"skipped": True}, "github": {"done": False, "at": None}}
            )["github"]["skipped"]
            is False
        )

    def test_untouched_store_serializes_unchanged(self):
        g = S.GeneralSettings.from_dict({})
        assert g.onboarded is False
        assert g.to_dict() == {}
        assert all(not v["done"] for v in g.setup.values())

    def test_old_onboarded_flag_migrates_to_repo_done(self):
        g = S.GeneralSettings.from_dict({"onboarded": True})
        assert g.setup["repo"]["done"] is True
        assert g.setup["repo"]["at"]
        assert g.setup["tools"]["done"] is False
        d = g.to_dict()
        # Written back as the checklist — and the derived flag stays, for
        # settings sync and every existing reader.
        assert d["setup"]["repo"]["done"] is True
        assert d["onboarded"] is True

    def test_onboarded_is_derived_from_repo_done(self):
        g = S.GeneralSettings.from_dict(
            {"setup": {"repo": {"done": True, "at": "2026-01-01T00:00:00+00:00"}}}
        )
        assert g.onboarded is True
        g2 = S.GeneralSettings.from_dict(
            {"setup": {"tools": {"done": True, "at": "2026-01-01T00:00:00+00:00"}}}
        )
        assert g2.onboarded is False
        assert "onboarded" not in g2.to_dict()

    def test_old_writers_of_onboarded_still_work(self):
        # Tests and older code call update_settings(general={"onboarded": True}).
        S.update_settings(general={"onboarded": True})
        assert S.load_settings().general.onboarded is True
        assert S.load_settings().general.setup["repo"]["done"] is True

    def test_skipped_is_kept_for_optional_steps_only(self):
        g = S.GeneralSettings.from_dict(
            {"setup": {"tracker": {"skipped": True}, "tools": {"skipped": True}}}
        )
        assert g.setup["tracker"]["skipped"] is True
        assert "skipped" not in g.setup["tools"]
        d = g.to_dict()["setup"]
        assert d["tracker"] == {"done": False, "at": None, "skipped": True}
        assert d["tools"] == {"done": False, "at": None}
        # Every step is written once anything is.
        assert set(d) == set(STEP_IDS)

    def test_garbage_reads_as_nothing_done(self):
        assert setup_steps.normalize("nope") == setup_steps.empty_state()
        assert (
            setup_steps.normalize({"repo": "yes", "x": {}}) == setup_steps.empty_state()
        )

    def test_general_setup_is_a_local_field(self):
        from backend.web.core import settings_sync

        assert "setup" in settings_sync.LOCAL["general"]
        assert "setup" not in settings_sync.SYNCED.get("general", ())


# --------------------------------------------------------------------------- #
# marking
# --------------------------------------------------------------------------- #
class TestMarking:
    def test_mark_is_forward_only_and_idempotent(self):
        assert setup_steps.mark("tools") is True
        at = setup_steps.state()["tools"]["at"]
        assert setup_steps.mark("tools") is False
        assert setup_steps.state()["tools"]["at"] == at
        assert setup_steps.mark("tools", done=False) is False
        assert setup_steps.state()["tools"]["done"] is True
        assert setup_steps.mark("nonsense") is False

    def test_a_session_create_only_starts_the_first_agent_step(self, monkeypatch):
        # "Start your first agent" is not done because a session row exists:
        # a session parked at a sign-in prompt has started nothing.
        monkeypatch.setattr(setup_steps, "_worked_noted", False)
        assert setup_steps.mark("repo") is True
        st = setup_steps.state()["repo"]
        assert st["done"] is False and st["started"]
        row = next(r for r in setup_steps.checklist() if r["id"] == "repo")
        assert row["status"] == "todo" and row["started"] is True
        assert setup_steps.mark("repo") is False  # idempotent
        assert S.load_settings().general.onboarded is False
        # The agent working on something is what finishes it.
        assert setup_steps.note_agent_worked() is True
        st = setup_steps.state()["repo"]
        assert st["done"] is True and st["at"]
        row = next(r for r in setup_steps.checklist() if r["id"] == "repo")
        assert row["status"] == "done" and row["started"] is False
        assert S.load_settings().general.onboarded is True
        # Free after the first time: a process flag, no settings read.
        assert setup_steps.note_agent_worked() is False

    def test_started_survives_the_round_trip_and_old_stores(self):
        full = setup_steps.empty_state()
        assert setup_steps.compact(full) == {}
        full["repo"]["started"] = "2026-10-14T00:00:00+00:00"
        out = setup_steps.compact(full)
        assert out["repo"] == {
            "done": False,
            "at": None,
            "started": "2026-10-14T00:00:00+00:00",
        }
        assert (
            setup_steps.normalize(out)["repo"]["started"] == "2026-10-14T00:00:00+00:00"
        )
        # A store from before `started` existed: done implies started.
        old = setup_steps.normalize(
            {"repo": {"done": True, "at": "2026-01-01T00:00:00+00:00"}}
        )
        assert old["repo"]["started"] == "2026-01-01T00:00:00+00:00"

    def test_the_activity_classifier_ticks_it_on_corroborated_work(self, monkeypatch):
        from backend.web.core import agent_state

        monkeypatch.setattr(setup_steps, "_worked_noted", False)
        setup_steps.mark("repo")
        rec: dict = {}
        # A reading that does not arm (a CPU blip) is not work.
        agent_state._verdict(rec, "working", 100.0, arms=False, source="pane")
        assert setup_steps.state()["repo"]["done"] is False
        agent_state._verdict(rec, "idle", 101.0, source="marker")
        assert setup_steps.state()["repo"]["done"] is False
        agent_state._verdict(rec, "working", 102.0, arms=True, source="marker")
        assert setup_steps.state()["repo"]["done"] is True

    def test_skip_is_for_optional_steps_and_done_wins(self):
        assert setup_steps.skip("tools") is False
        assert setup_steps.skip("phone") is True
        assert setup_steps.checklist()[5]["status"] == "skipped"
        setup_steps.mark("phone")
        row = setup_steps.checklist()[5]
        assert row["status"] == "done" and row["skipped"] is False
        assert setup_steps.unskip("tracker") is False
        setup_steps.skip("tracker")
        assert setup_steps.unskip("tracker") is True
        assert setup_steps.checklist()[4]["status"] == "todo"

    def test_summary_counts_skips_as_done_and_gates_the_card(self, monkeypatch):
        s = setup_steps.summary()
        assert s == {
            "done": 0,
            "total": 5,
            "required_done": False,
            "complete": False,
            "next": "tools",
        }
        for sid in ("tools", "agent_login"):
            setup_steps.mark(sid)
        monkeypatch.setattr(setup_steps, "_worked_noted", False)
        setup_steps.note_agent_worked()
        s = setup_steps.summary()
        assert s["required_done"] is True and s["complete"] is False
        assert s["done"] == 3 and s["next"] == "github"
        setup_steps.skip("github")
        setup_steps.skip("tracker")
        s = setup_steps.summary()
        # The phone (Optional, later) is neither counted nor waited on.
        assert s["complete"] is True and s["done"] == 5 and s["total"] == 5
        assert s["next"] == ""
        assert setup_steps.state()["phone"]["done"] is False

    def test_a_broken_optional_extra_does_not_hold_up_the_tools_step(self):
        prog = setup_steps.doctor_progress(
            [_check("git", "ok"), _check("tmux", "ok"), _check("tailscale", "fail")]
        )
        assert prog["tools"] is True
        prog = setup_steps.doctor_progress(
            [_check("git", "ok"), _check("tmux", "fail")]
        )
        assert prog["tools"] is False

    def test_extra_checks_match_the_frontend(self):
        import pathlib
        import re

        ts = (
            pathlib.Path(__file__).resolve().parents[2]
            / "frontend/src/components/setup/setupModel.ts"
        ).read_text(encoding="utf-8")
        block = ts[ts.index("export const EXTRA_CHECKS") :]
        block = block[: block.index("]);")]
        assert set(re.findall(r'"([a-z-]+)"', block)) == set(setup_steps.EXTRA_CHECKS)

    def test_a_github_credential_marks_github(self, monkeypatch):
        from backend.web.core import github_auth

        monkeypatch.setattr(github_auth, "resolve_token", lambda *a, **k: ("none", ""))
        assert setup_steps.github_connected([]) is False
        assert setup_steps.github_connected([_check("gh", "warn")]) is False
        # gh signed in (the doctor's row) — no subprocess of our own.
        assert setup_steps.github_connected([_check("gh", "ok")]) is True
        monkeypatch.setattr(
            github_auth, "resolve_token", lambda *a, **k: ("settings", "tok")
        )
        assert setup_steps.github_connected([]) is True
        setup_steps.status(None)
        assert setup_steps.state()["github"]["done"] is True

    def test_doctor_run_marks_tools_and_agent_login(self):
        checks = [
            _check("git", "ok"),
            _check("tmux", "ok"),
            _check("agent-cli", "ok", provider="claude"),
            _check("agent-auth", "ok", provider="claude"),
            _check("gh", "info"),
        ]
        setup_steps.note_doctor(checks)
        st = setup_steps.state()
        assert st["tools"]["done"] and st["agent_login"]["done"]

    def test_a_failing_required_check_marks_nothing(self):
        checks = [
            _check("tmux", "fail"),
            _check("agent-cli", "fail", provider="claude"),
            _check("agent-auth", "warn", provider="claude", cmd="claude"),
        ]
        setup_steps.note_doctor(checks)
        st = setup_steps.state()
        assert not st["tools"]["done"] and not st["agent_login"]["done"]

    def test_logged_out_agent_leaves_agent_login_todo(self):
        checks = [
            _check("tmux", "ok"),
            _check("agent-cli", "ok", provider="claude"),
            _check("agent-auth", "warn", provider="claude", cmd="claude"),
        ]
        setup_steps.note_doctor(checks)
        st = setup_steps.state()
        assert st["tools"]["done"] and not st["agent_login"]["done"]

    def test_no_checks_marks_nothing(self):
        setup_steps.note_doctor([])
        assert setup_steps.summary()["done"] == 0

    def test_run_checks_is_the_hook(self, monkeypatch):
        monkeypatch.setattr(
            doctor,
            "_ALL_CHECKS",
            [
                lambda: _check("tmux", "ok"),
                lambda: _check("agent-cli", "ok", provider="claude"),
                lambda: _check("agent-auth", "ok", provider="claude"),
            ],
        )
        doctor.run_checks()
        assert setup_steps.state()["tools"]["done"] is True
        assert setup_steps.state()["agent_login"]["done"] is True

    def test_doctor_strings_say_agent_cli(self):
        import inspect

        src = inspect.getsource(doctor)
        assert "Settings → Coding CLI" not in src
        assert "Settings → Agent CLI" in src

    def test_first_session_create_starts_repo(self):
        server._mark_onboarded()
        assert setup_steps.state()["repo"]["started"]
        assert setup_steps.state()["repo"]["done"] is False

    def test_report_text_is_kept_for_the_api(self):
        setup_steps.set_first_run_report("First run — hello")
        assert setup_steps.first_run_report() == "First run — hello"
        setup_steps.set_first_run_report("")

    def test_format_lines_name_every_step_with_its_state(self):
        setup_steps.mark("tools")
        setup_steps.skip("phone")
        lines = setup_steps.format_lines()
        assert len(lines) == 6
        assert lines[0].startswith("  ✓ 1. Install the tools") and lines[0].endswith(
            "done"
        )
        assert lines[1].startswith("  • 2. Sign in to your agent")
        assert lines[3].startswith("  • 4. Connect GitHub") and lines[3].endswith(
            "optional"
        )
        assert lines[5].startswith("  – 6. Use it from your phone")
        assert lines[5].endswith("skipped")
        setup_steps.unskip("phone")
        assert setup_steps.format_lines()[5].endswith("optional, later")


# --------------------------------------------------------------------------- #
# the routes
# --------------------------------------------------------------------------- #
_OK_CHECKS = [
    _check("git", "ok"),
    _check("tmux", "ok"),
    _check("agent-cli", "ok", provider="claude"),
    _check("agent-auth", "ok", provider="claude"),
    _check("tailscale", "warn", detail="not found", fix="curl … | sh"),
]


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(doctor, "run_checks", lambda: list(_OK_CHECKS))
    # The doctor addon caches its payload across tests; a direct probe keeps
    # the route's doctor summary tied to the checks above.
    monkeypatch.setattr(server, "ADDONS", [])
    monkeypatch.setattr(setup_steps, "provider_logins", lambda: [])
    # A GH_TOKEN in the shell running the suite must not tick "Connect GitHub".
    monkeypatch.setattr(setup_steps, "github_connected", lambda checks=(): False)
    setup_steps.set_first_run_report("")
    return TestClient(server.app)


class TestRoutes:
    def test_get_setup_shape(self, client):
        setup_steps.set_first_run_report(
            "First run — here is where this machine stands"
        )
        r = client.get("/api/setup")
        assert r.status_code == 200
        d = r.json()
        assert [s["id"] for s in d["steps"]] == list(STEP_IDS)
        # ① names the default agent (UX r5: "Install Claude Code"); the CLI's
        # TITLES keep the generic name.
        assert [s["title"] for s in d["steps"]] == [
            "Install Claude Code",
            "Sign in to your agent",
            "Start your first agent",
            "Connect GitHub",
            "Connect your tracker",
            "Use it from your phone",
        ]
        assert [s["required"] for s in d["steps"]] == [
            True,
            True,
            True,
            False,
            False,
            False,
        ]
        assert [s["group"] for s in d["steps"]] == [
            "start",
            "start",
            "start",
            "connect",
            "connect",
            "later",
        ]
        for s in d["steps"]:
            # `repo` also says whether a session exists that hasn't started yet.
            extra = {"started"} if s["id"] == "repo" else set()
            assert (
                set(s)
                == {
                    "id",
                    "title",
                    "required",
                    "group",
                    "done",
                    "at",
                    "skipped",
                    "status",
                }
                | extra
            )
        assert set(d["summary"]) == {
            "done",
            "total",
            "required_done",
            "complete",
            "next",
        }
        # The doctor run that served this answer marked the first two steps.
        assert d["steps"][0]["done"] and d["steps"][1]["done"]
        assert d["summary"]["done"] == 2 and d["summary"]["next"] == "repo"
        assert d["doctor"]["ok"] is True and len(d["doctor"]["checks"]) == 5
        assert d["tailscale"] == {
            "status": "warn",
            "detail": "not found",
            "fix": "curl … | sh",
            "docs": "",
        }
        assert d["logins"] == [] and d["default_provider"]
        assert d["report"].startswith("First run")

    def test_skip_route_marks_optional_steps_only(self, client):
        r = client.post("/api/setup/phone/skip")
        assert r.status_code == 200
        d = r.json()
        assert d["steps"][5]["skipped"] is True and d["steps"][5]["status"] == "skipped"
        # The phone is "Optional, later": skipping it moves no count.
        assert d["summary"]["done"] == 0
        r = client.post("/api/setup/github/skip")
        assert r.status_code == 200 and r.json()["summary"]["done"] == 1
        assert client.post("/api/setup/tools/skip").status_code == 400
        assert client.post("/api/setup/bogus/skip").status_code == 400
        assert S.load_settings().general.setup["phone"]["skipped"] is True
        r = client.post("/api/setup/phone/unskip")
        assert r.status_code == 200 and r.json()["steps"][5]["status"] == "todo"

    def test_skip_is_owner_only(self, client, monkeypatch):
        from backend.web.core import auth as web_auth

        async def _no(scope):
            return False

        monkeypatch.setattr(web_auth, "may_configure", _no)
        assert client.post("/api/setup/phone/skip").status_code == 403
        assert client.post("/api/setup/phone/unskip").status_code == 403

    def test_saving_the_first_ticket_source_marks_tracker(self, client):
        r = client.put(
            "/api/settings/ticketing/sources",
            json={"sources": [{"id": "sc", "provider": "shortcut", "api_token": "t"}]},
        )
        assert r.status_code == 200, r.text
        assert setup_steps.state()["tracker"]["done"] is True

    def test_saving_no_sources_marks_nothing(self, client):
        r = client.put("/api/settings/ticketing/sources", json={"sources": []})
        assert r.status_code == 200
        assert setup_steps.state()["tracker"]["done"] is False

    def test_the_owners_qr_fetch_marks_phone(self, client, monkeypatch):
        from backend.web.core import auth as web_auth

        # The test client is not loopback; the owner's own browser is.
        monkeypatch.setattr(web_auth, "may_see_own_token", lambda scope: True)
        monkeypatch.setattr(
            server,
            "_mobile_info",
            lambda include_tokens: {
                "urls": [],
                "qr_svg": "<svg/>" if include_tokens else None,
            },
        )
        # Settings and the restart poll read this route too: only the
        # checklist's own "Show QR" (?setup=1) means the person meant it.
        assert client.get("/api/mobile").status_code == 200
        assert setup_steps.state()["phone"]["done"] is False
        assert client.get("/api/mobile?setup=1").status_code == 200
        assert setup_steps.state()["phone"]["done"] is True

    def test_a_qr_without_tokens_marks_nothing(self, client, monkeypatch):
        from backend.web.core import auth as web_auth

        monkeypatch.setattr(web_auth, "may_see_own_token", lambda scope: False)
        monkeypatch.setattr(
            server,
            "_mobile_info",
            lambda include_tokens: {"urls": [], "qr_svg": "<svg/>"},
        )
        client.get("/api/mobile")
        assert setup_steps.state()["phone"]["done"] is False

    def test_a_phone_loading_m_marks_phone(self, client):
        # The test client is this machine ("testclient"); a phone is not.
        client.get("/m")
        assert setup_steps.state()["phone"]["done"] is False
        c2 = TestClient(server.app, client=("100.64.0.9", 51000))
        c2.get("/m")
        assert setup_steps.state()["phone"]["done"] is True

    def test_config_onboarded_follows_the_repo_step(self, client, monkeypatch):
        monkeypatch.setattr(setup_steps, "_worked_noted", False)
        assert client.get("/api/config").json()["onboarded"] is False
        setup_steps.note_agent_worked()
        assert client.get("/api/config").json()["onboarded"] is True


# --------------------------------------------------------------------------- #
# UX round 5: what step ① shows
# --------------------------------------------------------------------------- #
class TestRequiredChecks:
    def test_every_doctor_row_says_whether_it_blocks_getting_started(self):
        rows = setup_steps.tag_required(
            [
                {"id": "git", "status": "ok"},
                {"id": "agent-cli", "status": "fail"},
                {"id": "tailscale", "status": "warn"},
                {"id": "tailnet-mtu", "status": "warn"},
                {"id": "gh", "status": "info"},
                # A newer doctor that sets its own wins.
                {"id": "uv", "status": "ok", "required": False},
            ]
        )
        assert [r["required"] for r in rows] == [True, True, False, False, False, False]

    def test_required_is_exactly_what_tools_waits_for(self):
        for cid in setup_steps.EXTRA_CHECKS:
            assert setup_steps.is_required_check(cid) is False
        assert setup_steps.is_required_check("tmux") is True

    def test_the_doctor_route_and_the_setup_payload_carry_it(self, client, monkeypatch):
        monkeypatch.setattr(
            doctor,
            "run_checks",
            lambda: [_check("git", "ok"), _check("tailscale", "warn")],
        )
        body = client.get("/api/doctor?refresh=1").json()
        assert {c["id"]: c["required"] for c in body["checks"]} == {
            "git": True,
            "tailscale": False,
        }
        st = setup_steps.status(body)
        assert {c["id"]: c["required"] for c in st["doctor"]["checks"]} == {
            "git": True,
            "tailscale": False,
        }


class TestToolsTitle:
    def test_names_the_default_agent(self):
        checks = [{"id": "agent-cli", "status": "fail", "provider": "claude"}]
        assert setup_steps.tools_title(checks, "claude") == "Install Claude Code"
        assert setup_steps.tools_title([], "codex") == "Install Codex"

    def test_generic_only_when_the_agent_is_here_and_something_else_is_not(self):
        checks = [
            {"id": "agent-cli", "status": "ok"},
            {"id": "tmux", "status": "fail"},
        ]
        assert setup_steps.tools_title(checks, "claude") == "Install the tools"
        # An optional extra failing never makes the title generic.
        checks = [
            {"id": "agent-cli", "status": "ok"},
            {"id": "tailscale", "status": "fail"},
        ]
        assert setup_steps.tools_title(checks, "claude") == "Install Claude Code"

    def test_the_payload_uses_it_and_the_cli_list_keeps_its_own(self):
        st = setup_steps.status(
            {"checks": [{"id": "agent-cli", "status": "fail", "provider": "claude"}]}
        )
        tools = next(r for r in st["steps"] if r["id"] == "tools")
        assert tools["title"].startswith("Install ")
        assert setup_steps.TITLES["tools"] == "Install the tools"
