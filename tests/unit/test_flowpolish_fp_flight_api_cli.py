"""Flow polish (fp-flight-api): the CLI steers a route all the way — retry,
resume ("carry on"), pause and jump next to done / skip / cancel — and
`mindflock flights` says WHY a route needs you and what works from there."""

from __future__ import annotations

import pytest

from backend import cli
from tests.unit.test_cli_flights import FakeServer, _flight


@pytest.fixture()
def srv(monkeypatch):
    return FakeServer(monkeypatch)


def _halted():
    return _flight(
        state="halted",
        reason="CI checks failed",
        cursor=2,
        step_states={
            "s1": {"state": "done"},
            "s2": {"state": "done"},
            "s3": {"state": "failed", "note": "2 checks red: lint, unit"},
        },
    )


class TestSteer:
    def test_retry_posts_the_current_stop(self, srv, capsys):
        srv.gets["/api/flights?all=1"] = {"flights": [_halted()]}
        assert cli.main(["flight", "retry", "f_1a"]) == 0
        assert srv.sent("POST") == [("/api/flights/f_1a2b3c/steps/s3/retry", {})]
        assert "running again: Merge on f_1a2b3c" in capsys.readouterr().out

    @pytest.mark.parametrize(
        "verb, word", [("resume", "carrying on with"), ("pause", "paused")]
    )
    def test_resume_and_pause(self, srv, capsys, verb, word):
        assert cli.main(["flight", verb, "sc-42"]) == 0
        assert srv.sent("POST") == [("/api/flights/f_1a2b3c/%s" % verb, {})]
        assert capsys.readouterr().out.startswith("%s f_1a2b3c" % word)

    @pytest.mark.parametrize("stop", ["s1", "1", "agent works", "Agent"])
    def test_jump_by_id_number_or_title(self, srv, capsys, stop):
        assert cli.main(["flight", "jump", "f_1a2b3c", stop]) == 0
        assert srv.sent("POST") == [("/api/flights/f_1a2b3c/jump", {"step": "s1"})]
        assert "jumped f_1a2b3c to Agent works the ticket" in capsys.readouterr().out

    def test_jump_to_no_such_stop_lists_them(self, srv, capsys):
        assert cli.main(["flight", "jump", "f_1a2b3c", "deploy"]) == 1
        err = capsys.readouterr().err
        assert "no stop 'deploy'" in err and "s2 You: approve the merge" in err

    def test_help_lists_every_action(self, capsys):
        with pytest.raises(SystemExit):
            cli.main(["flight", "--help"])
        out = capsys.readouterr().out
        for verb in ("done", "skip", "retry", "resume", "pause", "jump", "cancel"):
            assert verb in out


class TestWhy:
    def test_a_stop_waiting_on_you_says_why_and_what_works(self, srv, capsys):
        f = _flight()
        f["step_states"]["s2"]["note"] = "Look over the PR, then press Done"
        srv.gets["/api/flights"] = {"flights": [f]}
        assert cli.main(["flights"]) == 0
        out = capsys.readouterr().out
        assert (
            "f_1a2b3c needs you at You: approve the merge: Look over the PR, then "
            "press Done" in out
        )
        assert (
            "do: mindflock flight done f_1a2b3c · mindflock flight skip f_1a2b3c · "
            "mindflock flight cancel f_1a2b3c" in out
        )

    def test_a_halted_route_offers_retry_and_carry_on(self, srv, capsys):
        srv.gets["/api/flights"] = {"flights": [_halted()]}
        assert cli.main(["flights"]) == 0
        out = capsys.readouterr().out
        assert "f_1a2b3c is halted at Merge: 2 checks red: lint, unit" in out
        assert "mindflock flight retry f_1a2b3c" in out
        assert "mindflock flight resume f_1a2b3c" in out

    def test_a_verify_waiting_for_a_confirm(self, srv, capsys):
        f = _flight(
            steps=[
                {"id": "s1", "kind": "agent", "title": "Agent"},
                {"id": "s2", "kind": "verify", "title": "Verify"},
            ],
            step_states={
                "s1": {"state": "done"},
                "s2": {
                    "state": "waiting_you",
                    "note": "Needs you: 3 steps verified, confirm them",
                    "detail": {"ask": "confirm", "plan": "tp1"},
                },
            },
        )
        srv.gets["/api/flights"] = {"flights": [f]}
        assert cli.main(["flights"]) == 0
        out = capsys.readouterr().out
        assert "needs you at Verify: Needs you: 3 steps verified, confirm them" in out
        assert "do: mindflock flight done f_1a2b3c (Confirm verified) · " in out

    def test_a_running_route_has_no_extra_lines(self, srv, capsys):
        srv.gets["/api/flights"] = {
            "flights": [
                _flight(
                    state="running",
                    step_states={
                        "s1": {"state": "done"},
                        "s2": {"state": "running"},
                        "s3": {"state": "pending"},
                    },
                )
            ]
        }
        assert cli.main(["flights"]) == 0
        assert "do:" not in capsys.readouterr().out
