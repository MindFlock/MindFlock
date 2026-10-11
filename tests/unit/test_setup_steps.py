"""The five-step first-run checklist (backend.web.core.setup_steps): the
settings shape and its migration from ``general.onboarded``, the hooks that
mark steps (doctor / create / ticket source / phone), ``GET /api/setup`` and
the skip route, and the wizard printing the same list."""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from backend import doctor
from backend.config import settings as S
from backend.web import server
from backend.web.core import setup_steps

STEP_IDS = ("tools", "agent_login", "repo", "tracker", "phone")


def _check(cid, status, **kw):
    return doctor.Check(cid, kw.pop("label", cid), status, **kw)


# --------------------------------------------------------------------------- #
# settings shape + migration
# --------------------------------------------------------------------------- #
class TestSettings:
    def test_definition_is_five_steps_three_required(self):
        assert setup_steps.STEP_IDS == STEP_IDS
        assert setup_steps.REQUIRED == ("tools", "agent_login", "repo")
        assert setup_steps.OPTIONAL == ("tracker", "phone")

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
        assert setup_steps.mark("repo") is True
        at = setup_steps.state()["repo"]["at"]
        assert setup_steps.mark("repo") is False
        assert setup_steps.state()["repo"]["at"] == at
        assert setup_steps.mark("repo", done=False) is False
        assert setup_steps.state()["repo"]["done"] is True
        assert setup_steps.mark("nonsense") is False

    def test_skip_is_for_optional_steps_and_done_wins(self):
        assert setup_steps.skip("tools") is False
        assert setup_steps.skip("phone") is True
        assert setup_steps.checklist()[4]["status"] == "skipped"
        setup_steps.mark("phone")
        row = setup_steps.checklist()[4]
        assert row["status"] == "done" and row["skipped"] is False
        assert setup_steps.unskip("tracker") is False
        setup_steps.skip("tracker")
        assert setup_steps.unskip("tracker") is True
        assert setup_steps.checklist()[3]["status"] == "todo"

    def test_summary_counts_skips_as_done_and_gates_the_card(self):
        s = setup_steps.summary()
        assert s == {
            "done": 0,
            "total": 5,
            "required_done": False,
            "complete": False,
            "next": "tools",
        }
        for sid in ("tools", "agent_login", "repo"):
            setup_steps.mark(sid)
        s = setup_steps.summary()
        assert s["required_done"] is True and s["complete"] is False
        assert s["done"] == 3 and s["next"] == "tracker"
        setup_steps.skip("tracker")
        setup_steps.skip("phone")
        s = setup_steps.summary()
        assert s["complete"] is True and s["done"] == 5 and s["next"] == ""

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

    def test_first_session_create_marks_repo(self):
        server._mark_onboarded()
        assert setup_steps.state()["repo"]["done"] is True
        assert S.load_settings().general.onboarded is True

    def test_report_text_is_kept_for_the_api(self):
        setup_steps.set_first_run_report("First run — hello")
        assert setup_steps.first_run_report() == "First run — hello"
        setup_steps.set_first_run_report("")

    def test_format_lines_name_every_step_with_its_state(self):
        setup_steps.mark("tools")
        setup_steps.skip("phone")
        lines = setup_steps.format_lines()
        assert len(lines) == 5
        assert lines[0].startswith("  ✓ 1. Engine and tools") and lines[0].endswith(
            "done"
        )
        assert lines[1].startswith("  • 2. Sign in to your agent")
        assert lines[3].endswith("optional")
        assert lines[4].startswith("  – 5. Your phone") and lines[4].endswith("skipped")


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
        assert [s["title"] for s in d["steps"]] == [
            "Engine and tools",
            "Sign in to your agent",
            "Pick a repo",
            "Connect a tracker and GitHub",
            "Your phone",
        ]
        assert [s["required"] for s in d["steps"]] == [True, True, True, False, False]
        for s in d["steps"]:
            assert set(s) == {
                "id",
                "title",
                "required",
                "done",
                "at",
                "skipped",
                "status",
            }
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
        assert d["steps"][4]["skipped"] is True and d["steps"][4]["status"] == "skipped"
        assert d["summary"]["done"] == 1
        assert client.post("/api/setup/tools/skip").status_code == 400
        assert client.post("/api/setup/bogus/skip").status_code == 400
        assert S.load_settings().general.setup["phone"]["skipped"] is True
        r = client.post("/api/setup/phone/unskip")
        assert r.status_code == 200 and r.json()["steps"][4]["status"] == "todo"

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

    def test_config_onboarded_follows_the_repo_step(self, client):
        assert client.get("/api/config").json()["onboarded"] is False
        setup_steps.mark("repo")
        assert client.get("/api/config").json()["onboarded"] is True
