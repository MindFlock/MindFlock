"""`mindflock flights` / `mindflock flight …` — the CLI side of Flights
(docs/flights.md "The CLI").

A thin client over a running server's ``/api/flights`` routes; these tests fake
that server at the ``backend.client`` seam (probe/get/post) the way
tests/unit/test_cli_devices.py does, so they pin what is sent and what the
person reads — not the flight logic (tests/unit/test_flights*.py).
"""

from __future__ import annotations

import json

import pytest

from backend import cli, client


def _flight(**over) -> dict:
    f = {
        "id": "f_1a2b3c",
        "plan_id": "builtin:ticket",
        "plan_name": "Ticket to merge",
        "subject": {"kind": "ticket", "source": "sc", "id": "42", "title": "Fix it"},
        "session": "sc-42",
        "state": "waiting_you",
        "cursor": 1,
        "reason": "",
        "steps": [
            {"id": "s1", "kind": "agent", "title": "Agent works the ticket"},
            {"id": "s2", "kind": "you", "title": "You: approve the merge"},
            {"id": "s3", "kind": "ship", "title": "Merge"},
        ],
        "step_states": {
            "s1": {"state": "done"},
            "s2": {"state": "waiting_you"},
            "s3": {"state": "pending"},
        },
    }
    f.update(over)
    return f


_PLANS = {
    "plans": [
        {
            "id": "builtin:ticket",
            "name": "Ticket to merge",
            "builtin": True,
            "trigger": {"kind": "ticket"},
            "session": {"repo_path": ""},
            "steps": [{}] * 10,
        },
        {
            "id": "p_7f3a2c",
            "name": "Nightly tidy",
            "builtin": False,
            "trigger": {"kind": "schedule", "schedule": {"at": "21:00"}},
            "session": {"repo_path": "/repo"},
            "steps": [{}] * 2,
            "runs": {"schedule_label": "Every day at 21:00", "next_at": 1793286000.0},
        },
        {
            "id": "p_7f9999",
            "name": "Nightly deploy",
            "builtin": False,
            "trigger": {"kind": "manual"},
            "session": {"repo_path": ""},
            "steps": [{}],
        },
    ]
}


class FakeServer:
    """Canned /api/flights answers with call recording (an Exception in a
    table is raised instead of answered)."""

    def __init__(self, monkeypatch):
        self.gets: dict = {
            "/api/flights": {"flights": [_flight()]},
            "/api/flights?all=1": {"flights": [_flight()]},
            "/api/flights/plans": _PLANS,
            "/api/instances": [{"title": "sc-42"}, {"title": "other"}],
        }
        self.posts: dict = {}
        self.calls: list = []
        self.probed: list = []
        monkeypatch.setattr(client, "probe", self._probe)
        monkeypatch.setattr(client, "get", self._get)
        monkeypatch.setattr(client, "post", self._post)
        monkeypatch.delenv("MINDFLOCK_HOST", raising=False)
        monkeypatch.delenv("MINDFLOCK_PORT", raising=False)

    def _probe(self, base, timeout=1.0):
        self.probed.append(base)
        return {"default_program": "claude", "repo_root": "/x"}

    @staticmethod
    def _answer(table: dict, path: str, default):
        ans = table.get(path, default)
        if isinstance(ans, BaseException):
            raise ans
        return ans

    def _get(self, base, path, timeout=None):
        self.calls.append(("GET", path, None))
        return self._answer(self.gets, path, None)

    def _post(self, base, path, payload=None, timeout=None):
        self.calls.append(("POST", path, payload))
        return self._answer(
            self.posts, path, {"flight": _flight(cursor=2, state="running")}
        )

    def sent(self, method: str) -> list:
        return [(p, b) for m, p, b in self.calls if m == method]


@pytest.fixture()
def srv(monkeypatch):
    return FakeServer(monkeypatch)


# --------------------------------------------------------------------------- #
# Wiring
# --------------------------------------------------------------------------- #
class TestWiring:
    def test_registered_as_server_commands(self):
        assert cli._SESSION_COMMANDS["flights"] is cli._cmd_flights
        assert cli._SESSION_COMMANDS["flight"] is cli._cmd_flight

    def test_top_level_help_lists_them_under_sessions(self, capsys):
        with pytest.raises(SystemExit):
            cli.main(["--help"])
        out = capsys.readouterr().out
        sessions = out.split("Sessions:")[1].split("This computer:")[0]
        assert "  flights " in sessions and "  flight " in sessions

    def test_no_server_is_one_line_and_exit_one(self, monkeypatch, capsys):
        monkeypatch.delenv("MINDFLOCK_HOST", raising=False)
        monkeypatch.delenv("MINDFLOCK_PORT", raising=False)
        monkeypatch.setattr(client, "probe", lambda base, timeout=1.0: None)
        assert cli.main(["flights"]) == 1
        assert "mindflock serve" in capsys.readouterr().err

    @pytest.mark.parametrize(
        "argv",
        [
            ["flight", "--port", "9123", "plans"],
            ["flight", "plans", "--port", "9123"],
        ],
    )
    def test_port_flag_works_on_either_level(self, srv, argv):
        assert cli.main(argv) == 0
        assert srv.probed == ["http://127.0.0.1:9123"]


# --------------------------------------------------------------------------- #
# flights (list)
# --------------------------------------------------------------------------- #
class TestList:
    def test_table(self, srv, capsys):
        assert cli.main(["flights"]) == 0
        assert srv.sent("GET") == [("/api/flights", None)]
        out = capsys.readouterr().out.splitlines()
        assert out[0].split() == ["ID", "FLYWAY", "SUBJECT", "SESSION", "STATE", "STOP"]
        row = out[1]
        for cell in (
            "f_1a2b3c",
            "Ticket to merge",
            "ticket 42 Fix it",
            "sc-42",
            "waiting_you",
            "2/3 You: approve the merge (perched on you)",
        ):
            assert cell in row

    def test_all_and_a_halted_reason(self, srv, capsys):
        srv.gets["/api/flights?all=1"] = {
            "flights": [
                _flight(
                    state="halted",
                    reason="checks failed",
                    subject={"kind": "pr", "id": "7", "title": ""},
                )
            ]
        }
        assert cli.main(["flights", "--all"]) == 0
        assert srv.sent("GET") == [("/api/flights?all=1", None)]
        out = capsys.readouterr().out
        assert "halted: checks failed" in out and "pr #7" in out

    def test_none(self, srv, capsys):
        srv.gets["/api/flights"] = {"flights": []}
        assert cli.main(["flights"]) == 0
        assert "mindflock flight start" in capsys.readouterr().out

    def test_json(self, srv, capsys):
        assert cli.main(["flights", "--json"]) == 0
        assert json.loads(capsys.readouterr().out)["flights"][0]["id"] == "f_1a2b3c"


# --------------------------------------------------------------------------- #
# flight plans / start
# --------------------------------------------------------------------------- #
class TestPlans:
    def test_bare_flight_is_plans(self, srv, capsys):
        assert cli.main(["flight"]) == 0
        out = capsys.readouterr().out
        assert "Ticket to merge *" in out and "built-in" in out
        assert "Every day at 21:00 — next" in out

    def test_plans_json(self, srv, capsys):
        assert cli.main(["flight", "plans", "--json"]) == 0
        assert len(json.loads(capsys.readouterr().out)["plans"]) == 3

    def test_flyways_is_the_same_list(self, srv, capsys):
        assert cli.main(["flight", "flyways"]) == 0
        out = capsys.readouterr().out
        assert out.splitlines()[0].split() == [
            "ID",
            "NAME",
            "TRIGGER",
            "STOPS",
            "SCHEDULE",
        ]
        assert "Ticket to merge *" in out and "Flyways tab" in out


class TestStart:
    def test_by_name_any_case_with_the_plans_own_repo(self, srv, capsys):
        assert cli.main(["flight", "start", "nightly TIDY"]) == 0
        assert srv.sent("POST") == [("/api/flights/start", {"plan_id": "p_7f3a2c"})]
        assert "took off on Ticket to merge" in capsys.readouterr().out

    def test_repo_defaults_to_here_when_the_plan_has_none(
        self, srv, monkeypatch, tmp_path
    ):
        monkeypatch.chdir(tmp_path)
        assert cli.main(["flight", "start", "builtin:ticket"]) == 0
        ((_, body),) = srv.sent("POST")
        assert body == {"plan_id": "builtin:ticket", "repo_path": str(tmp_path)}

    def test_repo_and_title(self, srv, tmp_path):
        assert cli.main(["flight", "start", "p_7f3a2c", "--repo", str(tmp_path)]) == 0
        assert cli.main(["flight", "start", "p_7f3a2c", "--title", "sc-"]) == 0
        bodies = [b for _, b in srv.sent("POST")]
        assert bodies == [
            {"plan_id": "p_7f3a2c", "repo_path": str(tmp_path)},
            {"plan_id": "p_7f3a2c", "title": "sc-42"},
        ]

    def test_an_ambiguous_or_unknown_plan(self, srv, capsys):
        assert cli.main(["flight", "start", "nightly"]) == 1
        err = capsys.readouterr().err
        assert "matches 2 flyways" in err and "p_7f3a2c" in err and "p_7f9999" in err
        assert cli.main(["flight", "start", "nope"]) == 1
        assert "mindflock flight flyways" in capsys.readouterr().err
        assert srv.sent("POST") == []

    def test_a_refusal_reads_as_its_detail(self, srv, capsys):
        srv.posts["/api/flights/start"] = client.ApiError(
            409,
            "in_flight",
            {"error": "in_flight", "detail": "sc-42 is already on a flight"},
        )
        assert cli.main(["flight", "start", "p_7f3a2c", "--title", "sc-42"]) == 1
        assert capsys.readouterr().err.strip() == "error: sc-42 is already on a flight"


# --------------------------------------------------------------------------- #
# flight done / skip / cancel
# --------------------------------------------------------------------------- #
class TestStepOps:
    def test_done_posts_the_current_step(self, srv, capsys):
        assert cli.main(["flight", "done", "f_1a", "--note", "looks good"]) == 0
        assert srv.sent("POST") == [
            ("/api/flights/f_1a2b3c/steps/s2/done", {"note": "looks good"})
        ]
        assert "done: You: approve the merge on f_1a2b3c — now at Merge" in (
            capsys.readouterr().out
        )

    def test_skip_by_session_title(self, srv):
        assert cli.main(["flight", "skip", "sc-42"]) == 0
        assert srv.sent("POST") == [("/api/flights/f_1a2b3c/steps/s2/skip", {})]

    def test_cancel(self, srv, capsys):
        assert cli.main(["flight", "cancel", "f_1a2b3c"]) == 0
        assert srv.sent("POST") == [("/api/flights/f_1a2b3c/cancel", {})]
        assert "sc-42 stays open" in capsys.readouterr().out

    def test_unknown_flight(self, srv, capsys):
        assert cli.main(["flight", "done", "f_zz"]) == 1
        assert "mindflock flights" in capsys.readouterr().err

    def test_a_landed_flight_has_no_stop(self, srv, capsys):
        srv.gets["/api/flights?all=1"] = {"flights": [_flight(state="done", cursor=3)]}
        assert cli.main(["flight", "done", "f_1a2b3c"]) == 1
        assert "no stop left" in capsys.readouterr().err
