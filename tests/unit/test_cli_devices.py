"""`mindflock devices …` — the CLI side of "Your devices" (spec §7).

The CLI is a thin client over its OWN server's ``/api/fleet`` routes; these
tests fake that server at the ``backend.client`` seam (probe/get/post/delete)
the way tests/unit/test_cli.py does, so they pin what is sent and what the
person reads — not the fleet logic itself (tests/unit/test_fleet.py).
"""

from __future__ import annotations

import builtins
import json
import time

import pytest

from backend import cli, client

BASE = "http://127.0.0.1:8765"


def _status(**over) -> dict:
    """A GET /api/fleet payload: in a fleet of three, one asker, one invite,
    three other computers on the tailnet."""
    st = {
        "in_fleet": True,
        "id": "0123456789abcdef",
        "epoch": 1,
        "self": {"key": "laptop", "host": "Laptop"},
        "members": [
            {
                "key": "laptop",
                "host": "Laptop",
                "added_at": 1.0,
                "self": True,
                "reachable": True,
                "version": "0.7.3",
                "same_fleet": True,
                "error": "",
            },
            {
                "key": "mac-mini",
                "host": "Mac-Mini",
                "added_at": 2.0,
                "self": False,
                "reachable": True,
                "version": "0.7.4",
                "same_fleet": True,
                "error": "",
            },
            {
                "key": "ml-rig",
                "host": "ml-rig",
                "added_at": 3.0,
                "self": False,
                "reachable": False,
                "version": "",
                "same_fleet": False,
                "error": "",
            },
        ],
        "invites": [
            {
                "code": "ABCD-EFGH",
                "expires_at": time.time() + 590,
                "command": "mindflock devices join laptop ABCD-EFGH",
            }
        ],
        "requests": [
            {
                "id": "a1b2c3d4e5f60718",  # pragma: allowlist secret
                "device": "desktop",
                "host": "Desktop",
                "code": "123 456",
                "created_at": 10.0,
                "expires_at": time.time() + 500,
            }
        ],
        "join": {"state": "idle"},
        "stale_key": False,
        "gate_warning": False,
        "candidates": [
            {
                "device": "old-box",
                "host": "old-box",
                "version": "0.5.0",
                "fleet_proto": 0,
                "reachable": True,
                "member": False,
                "in_fleet": False,
                "same_fleet": False,
                "has_token": False,
            },
            {
                "device": "work-pc",
                "host": "Work-PC",
                "version": "0.7.4",
                "fleet_proto": 1,
                "reachable": True,
                "member": False,
                "in_fleet": False,
                "same_fleet": False,
                "has_token": True,
            },
            {
                "device": "spare",
                "host": "spare",
                "version": "0.7.4",
                "fleet_proto": 1,
                "reachable": True,
                "member": False,
                "in_fleet": True,
                "same_fleet": False,
                "has_token": False,
            },
            {
                "device": "gone",
                "host": "gone",
                "version": "",
                "fleet_proto": 1,
                "reachable": False,
                "member": False,
                "in_fleet": False,
                "same_fleet": False,
                "has_token": False,
            },
        ],
    }
    st.update(over)
    return st


class FakeServer:
    """Canned /api/fleet answers with call recording.

    ``gets`` maps a path to one answer, or to a list consumed one per call
    (the last one sticks); an Exception instance in it is raised instead.
    ``posts`` / ``deletes`` map a path to its answer the same way."""

    def __init__(self, monkeypatch, status=None):
        self.status = status if status is not None else _status()
        self.gets: dict = {}
        self.posts: dict = {}
        self.deletes: dict = {}
        self.calls: list = []
        self.probed: list = []
        self.sleeps: list = []
        monkeypatch.setattr(client, "probe", self._probe)
        monkeypatch.setattr(client, "get", self._get)
        monkeypatch.setattr(client, "post", self._post)
        monkeypatch.setattr(client, "delete", self._delete)
        monkeypatch.setattr(cli.time, "sleep", self.sleeps.append)
        monkeypatch.delenv("MINDFLOCK_HOST", raising=False)
        monkeypatch.delenv("MINDFLOCK_PORT", raising=False)

    def _probe(self, base, timeout=1.0):
        self.probed.append(base)
        return {"default_program": "claude", "repo_root": "/x"}

    @staticmethod
    def _answer(table: dict, path: str, default):
        ans = table.get(path, default)
        if isinstance(ans, list):
            ans = ans.pop(0) if len(ans) > 1 else ans[0]
        if isinstance(ans, BaseException):
            raise ans
        return ans

    def _get(self, base, path, timeout=None):
        self.calls.append(("GET", path, None))
        if path == "/api/fleet":
            return self._answer(self.gets, path, self.status)
        return self._answer(self.gets, path, None)

    def _post(self, base, path, payload=None, timeout=None):
        self.calls.append(("POST", path, payload))
        return self._answer(self.posts, path, {"ok": True})

    def _delete(self, base, path, timeout=None):
        self.calls.append(("DELETE", path, None))
        return self._answer(self.deletes, path, {"state": "idle"})

    def sent(self, method: str) -> list:
        return [(p, b) for m, p, b in self.calls if m == method]


@pytest.fixture()
def srv(monkeypatch):
    return FakeServer(monkeypatch)


def _inputs(monkeypatch, *answers):
    """Feed ``input()``; an Exception class/instance in ``answers`` is raised."""
    seen = []
    it = iter(answers)

    def _input(prompt=""):
        seen.append(prompt)
        ans = next(it)
        if isinstance(ans, BaseException) or (
            isinstance(ans, type) and issubclass(ans, BaseException)
        ):
            raise ans
        return ans

    monkeypatch.setattr(builtins, "input", _input)
    return seen


# --------------------------------------------------------------------------- #
# Wiring
# --------------------------------------------------------------------------- #
class TestWiring:
    def test_registered_as_a_server_command(self):
        assert cli._SESSION_COMMANDS["devices"] is cli._cmd_devices

    def test_top_level_help_lists_devices(self, capsys):
        with pytest.raises(SystemExit):
            cli.main(["--help"])
        assert "devices" in capsys.readouterr().out

    def test_no_server_is_one_line_and_exit_one(self, monkeypatch, capsys):
        monkeypatch.delenv("MINDFLOCK_HOST", raising=False)
        monkeypatch.delenv("MINDFLOCK_PORT", raising=False)
        monkeypatch.setattr(client, "probe", lambda base, timeout=1.0: None)
        assert cli.main(["devices"]) == 1
        assert "mindflock serve" in capsys.readouterr().err

    @pytest.mark.parametrize(
        "argv",
        [
            ["devices", "--port", "9123", "list"],
            ["devices", "list", "--port", "9123"],
        ],
    )
    def test_port_flag_works_on_either_level(self, srv, argv):
        assert cli.main(argv) == 0
        assert srv.probed == ["http://127.0.0.1:9123"]

    def test_api_error_is_one_line(self, srv, capsys):
        srv.posts["/api/fleet/invite"] = client.ApiError(
            403, "open this on the device itself"
        )
        assert cli.main(["devices", "add"]) == 1
        assert capsys.readouterr().err.strip() == (
            "error: open this on the device itself"
        )


# --------------------------------------------------------------------------- #
# list
# --------------------------------------------------------------------------- #
class TestList:
    def test_bare_devices_is_list(self, srv, capsys):
        assert cli.main(["devices"]) == 0
        assert srv.sent("GET") == [("/api/fleet", None)]
        assert "Your devices (3):" in capsys.readouterr().out

    def test_members_marked(self, srv, capsys):
        cli.main(["devices", "list"])
        out = capsys.readouterr().out
        lines = out.splitlines()
        me = next(l for l in lines if "Laptop" in l and "this device" in l)
        assert me.strip().startswith("✓")
        mini = next(l for l in lines if "Mac-Mini" in l)
        assert mini.strip().startswith("✓") and "reachable" in mini
        assert "v0.7.4" in mini
        rig = next(l for l in lines if l.strip().startswith("-"))
        assert "ml-rig" in rig and "offline" in rig

    def test_member_in_another_group_is_flagged(self, monkeypatch, capsys):
        st = _status()
        st["members"][2].update(reachable=True, same_fleet=False, error="401")
        FakeServer(monkeypatch, st)
        cli.main(["devices"])
        rig = next(l for l in capsys.readouterr().out.splitlines() if "ml-rig" in l)
        assert rig.strip().startswith("!")
        assert "not in this group" in rig and "— 401" in rig

    def test_pending_request_shows_code_and_approve_command(self, srv, capsys):
        cli.main(["devices"])
        out = capsys.readouterr().out
        assert "Desktop · code 123 456" in out
        assert "mindflock devices approve desktop" in out

    def test_invite_shows_command_and_expiry(self, srv, capsys):
        cli.main(["devices"])
        line = next(l for l in capsys.readouterr().out.splitlines() if "ABCD-EFGH" in l)
        assert "expires in 9m" in line
        assert "mindflock devices join laptop ABCD-EFGH" in line

    def test_candidates_get_the_right_hint(self, srv, capsys):
        cli.main(["devices"])
        out = capsys.readouterr().out
        assert "update MindFlock on old-box first" in out
        # Already holds its token → one-step add; otherwise ask to join.
        assert "mindflock devices add work-pc" in out
        assert "mindflock devices join spare" in out
        # Unreachable ones aren't offered at all.
        assert "gone" not in out

    def test_outgoing_join_in_progress_shown(self, monkeypatch, capsys):
        FakeServer(
            monkeypatch,
            _status(
                join={
                    "state": "waiting",
                    "device": "mac-mini",
                    "host": "Mac-Mini",
                    "code": "654 321",
                }
            ),
        )
        cli.main(["devices"])
        assert "Waiting for approval on Mac-Mini — code 654 321" in (
            capsys.readouterr().out
        )

    def test_stale_key_and_gate_warning(self, monkeypatch, capsys):
        FakeServer(monkeypatch, _status(stale_key=True, gate_warning=True))
        cli.main(["devices"])
        out = capsys.readouterr().out
        assert "key is out of date" in out
        assert "access-token gate is off" in out

    def test_not_in_a_fleet_alone_on_the_tailnet(self, monkeypatch, capsys):
        FakeServer(
            monkeypatch,
            _status(
                in_fleet=False,
                id="",
                members=[],
                invites=[],
                requests=[],
                candidates=[],
            ),
        )
        assert cli.main(["devices"]) == 0
        out = capsys.readouterr().out
        assert "isn't joined with your other devices" in out
        assert "mindflock devices add" in out
        assert "Your devices (" not in out

    def test_json_dumps_the_payload(self, srv, capsys):
        assert cli.main(["devices", "list", "--json"]) == 0
        assert json.loads(capsys.readouterr().out) == json.loads(json.dumps(srv.status))


# --------------------------------------------------------------------------- #
# add
# --------------------------------------------------------------------------- #
class TestAdd:
    def test_invite_prints_code_on_stdout_and_command_on_stderr(self, srv, capsys):
        srv.posts["/api/fleet/invite"] = {
            "code": "WXYZ-2345",
            "expires_at": time.time() + 600,
            "device": "laptop",
            "command": "mindflock devices join laptop WXYZ-2345",
        }
        assert cli.main(["devices", "add"]) == 0
        assert srv.sent("POST") == [("/api/fleet/invite", None)]
        out, err = capsys.readouterr()
        assert out == "WXYZ-2345\n"
        assert (
            "On the other computer run:  mindflock devices join laptop WXYZ-2345" in err
        )
        assert "choose laptop" in err

    def test_invite_without_command_field_builds_it(self, srv, capsys):
        srv.posts["/api/fleet/invite"] = {"code": "WXYZ-2345", "device": "laptop"}
        cli.main(["devices", "add"])
        assert "mindflock devices join laptop WXYZ-2345" in capsys.readouterr().err

    def test_add_paired_resolves_host_to_device_key(self, srv, capsys):
        assert cli.main(["devices", "add", "work-PC", "-y"]) == 0
        assert srv.sent("POST") == [("/api/fleet/add-paired", {"device": "work-pc"})]
        assert "added Work-PC to your devices" in capsys.readouterr().out

    def test_add_paired_unknown_name_passed_through(self, srv):
        # The server owns the "no such device" answer.
        cli.main(["devices", "add", "nas", "--yes"])
        assert srv.sent("POST") == [("/api/fleet/add-paired", {"device": "nas"})]

    def test_add_paired_refusal_exits_one(self, srv, capsys):
        srv.posts["/api/fleet/add-paired"] = client.ApiError(
            400, "no access token for work-pc"
        )
        assert cli.main(["devices", "add", "work-pc", "-y"]) == 1
        assert "no access token for work-pc" in capsys.readouterr().err

    def test_add_paired_asks_and_says_the_settings_go_the_other_way(
        self, srv, monkeypatch
    ):
        # add-paired makes Work-PC adopt THIS computer's group and start its
        # settings from here: the reverse of `devices join`.
        seen = _inputs(monkeypatch, "y")
        assert cli.main(["devices", "add", "work-pc"]) == 0
        assert (
            "Work-PC takes this computer's shared settings where this one has "
            "them; its own stay where this one has none." in seen[0]
        )
        assert "This computer takes" not in seen[0]
        assert [p for p, _ in srv.sent("POST")] == ["/api/fleet/add-paired"]

    @pytest.mark.parametrize("answer", ["n", "", EOFError])
    def test_add_paired_declined_sends_nothing(self, srv, monkeypatch, capsys, answer):
        _inputs(monkeypatch, answer)
        assert cli.main(["devices", "add", "work-pc"]) == 1
        assert srv.sent("POST") == []
        assert "not added" in capsys.readouterr().out

    def test_add_paired_refuses_a_device_that_is_already_a_member(
        self, monkeypatch, capsys
    ):
        # A member whose hello lags still comes back as a candidate; adding
        # it again would reset its settings to this computer's.
        st = _status()
        st["candidates"].append(
            {
                "device": "mac-mini",
                "host": "Mac-Mini",
                "version": "0.7.4",
                "fleet_proto": 1,
                "reachable": True,
                "member": True,
                "in_fleet": False,
                "same_fleet": False,
                "has_token": True,
            }
        )
        srv = FakeServer(monkeypatch, st)
        assert cli.main(["devices", "add", "mac-mini", "-y"]) == 1
        assert srv.sent("POST") == []
        assert "Mac-Mini is already one of your devices" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# join
# --------------------------------------------------------------------------- #
class TestJoinWithCode:
    def test_posts_device_and_code(self, srv, capsys):
        srv.posts["/api/fleet/join"] = {
            "state": "joined",
            "device": "laptop",
            "host": "Laptop",
            "error": "",
        }
        assert cli.main(["devices", "join", "-y", "laptop", "ABCD-EFGH"]) == 0
        assert srv.sent("POST") == [
            ("/api/fleet/join", {"device": "laptop", "code": "ABCD-EFGH"})
        ]
        assert "with Laptop" in capsys.readouterr().out

    def test_code_typed_with_a_space_arrives_whole(self, srv):
        srv.posts["/api/fleet/join"] = {"state": "joined"}
        cli.main(["devices", "join", "-y", "laptop", "abcd", "efgh"])
        assert srv.sent("POST")[0][1] == {"device": "laptop", "code": "abcd efgh"}

    def test_host_name_resolved_to_candidate_key(self, srv):
        srv.posts["/api/fleet/join"] = {"state": "joined"}
        cli.main(["devices", "join", "-y", "work-pc", "ABCD-EFGH"])
        cli.main(["devices", "join", "-y", "WORK-PC", "ABCD-EFGH"])
        assert [b["device"] for _, b in srv.sent("POST")] == ["work-pc", "work-pc"]

    def test_ambiguous_host_refused(self, monkeypatch, capsys):
        st = _status()
        st["candidates"].append(dict(st["candidates"][1], device="work-pc-2"))
        srv = FakeServer(monkeypatch, st)
        assert cli.main(["devices", "join", "-y", "Work-PC", "ABCD-EFGH"]) == 1
        assert srv.sent("POST") == []
        assert "names 2 devices" in capsys.readouterr().err

    def test_error_state_exits_one_with_message(self, srv, capsys):
        srv.posts["/api/fleet/join"] = {
            "state": "error",
            "error": "update MindFlock on old-box first",
        }
        assert cli.main(["devices", "join", "-y", "old-box", "ABCD-EFGH"]) == 1
        assert "update MindFlock on old-box first" in capsys.readouterr().err

    def test_wrong_code_403_exits_one(self, srv, capsys):
        srv.posts["/api/fleet/join"] = client.ApiError(
            403, "that code is wrong or expired"
        )
        assert cli.main(["devices", "join", "-y", "laptop", "ZZZZ-ZZZZ"]) == 1
        assert "that code is wrong or expired" in capsys.readouterr().err

    def test_joined_but_sync_failed_still_succeeds(self, srv, capsys):
        srv.posts["/api/fleet/join"] = {
            "state": "joined",
            "host": "Laptop",
            "error": "settings sync: Laptop didn't answer",
        }
        assert cli.main(["devices", "join", "-y", "laptop", "ABCD-EFGH"]) == 0
        out, err = capsys.readouterr()
        assert "joined" in out
        assert "didn't answer" in err


class TestJoinByRequest:
    def _ask(self, srv, *polls):
        srv.posts["/api/fleet/request"] = {
            "state": "waiting",
            "device": "mac-mini",
            "host": "Mac-Mini",
            "code": "123 456",
        }
        srv.gets["/api/fleet/request"] = list(polls)

    def test_waits_until_approved(self, srv, capsys):
        self._ask(
            srv,
            {"state": "waiting", "host": "Mac-Mini"},
            {"state": "joining", "host": "Mac-Mini"},
            {"state": "joined", "host": "Mac-Mini"},
        )
        assert cli.main(["devices", "join", "-y", "Work-PC"]) == 0
        assert srv.sent("POST") == [("/api/fleet/request", {"device": "work-pc"})]
        polls = [p for p, _ in srv.sent("GET") if p == "/api/fleet/request"]
        assert len(polls) == 3
        assert srv.sleeps == [cli._JOIN_POLL_S] * 3
        assert srv.sent("DELETE") == []
        out = capsys.readouterr().out
        assert "Approve on Mac-Mini; check it shows code 123 456" in out
        assert "joined" in out

    @pytest.mark.parametrize(
        "state, text",
        [
            ("denied", "said no"),
            ("expired", "expired"),
            ("idle", "cancelled"),
            ("error", "boom"),
        ],
    )
    def test_terminal_states_exit_one(self, srv, capsys, state, text):
        self._ask(srv, {"state": "waiting"}, {"state": state, "error": "boom"})
        assert cli.main(["devices", "join", "-y", "mac-mini"]) == 1
        assert text in capsys.readouterr().err
        assert srv.sent("DELETE") == []

    def test_immediate_error_skips_the_wait(self, srv, capsys):
        srv.posts["/api/fleet/request"] = {
            "state": "error",
            "error": "Mac-Mini isn't reachable",
        }
        assert cli.main(["devices", "join", "-y", "mac-mini"]) == 1
        assert srv.sleeps == []
        assert "isn't reachable" in capsys.readouterr().err

    def test_server_blip_during_poll_tolerated(self, srv):
        self._ask(
            srv,
            client.ConnectionDropped("dropped"),
            {"state": "joined"},
        )
        assert cli.main(["devices", "join", "-y", "mac-mini"]) == 0

    def test_auth_refusal_during_poll_is_not_swallowed(self, srv, capsys):
        self._ask(srv, client.AuthRejected(401, "unauthorized"))
        assert cli.main(["devices", "join", "-y", "mac-mini"]) == 1
        assert "unauthorized" in capsys.readouterr().err

    def test_ctrl_c_withdraws_the_request(self, srv, monkeypatch, capsys):
        self._ask(srv, {"state": "waiting"})

        def _sleep(s):
            raise KeyboardInterrupt

        monkeypatch.setattr(cli.time, "sleep", _sleep)
        assert cli.main(["devices", "join", "-y", "mac-mini"]) == 130
        assert srv.sent("DELETE") == [("/api/fleet/request", None)]
        assert "cancelled" in capsys.readouterr().err

    def test_gives_up_after_the_deadline(self, srv, monkeypatch, capsys):
        self._ask(srv, {"state": "waiting"})
        clock = iter([0.0, 1.0, cli._JOIN_WAIT_S + 1])
        monkeypatch.setattr(cli.time, "monotonic", lambda: next(clock))
        assert cli.main(["devices", "join", "-y", "mac-mini"]) == 1
        assert srv.sent("DELETE") == [("/api/fleet/request", None)]
        assert "gave up waiting for Mac-Mini" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# approve / deny
# --------------------------------------------------------------------------- #
APPROVE = "/api/fleet/requests/a1b2c3d4e5f60718/approve"
DENY = "/api/fleet/requests/a1b2c3d4e5f60718/deny"


class TestAnswerRequests:
    @pytest.mark.parametrize(
        "needle",
        ["desktop", "DESKTOP", "a1b2c3d4e5f60718", "a1b2"],  # pragma: allowlist secret
    )
    def test_approve_resolves_name_host_id_or_prefix(self, srv, capsys, needle):
        assert cli.main(["devices", "approve", needle, "--yes"]) == 0
        assert srv.sent("POST") == [(APPROVE, None)]
        assert "approved Desktop" in capsys.readouterr().out

    def test_approve_shows_the_code_and_asks(self, srv, monkeypatch):
        seen = _inputs(monkeypatch, "y")
        assert cli.main(["devices", "approve", "desktop"]) == 0
        assert "code 123 456" in seen[0] and "Desktop" in seen[0]
        assert srv.sent("POST") == [(APPROVE, None)]

    @pytest.mark.parametrize("answer", ["n", "", EOFError])
    def test_approve_declined_sends_nothing(self, srv, monkeypatch, capsys, answer):
        _inputs(monkeypatch, answer)
        assert cli.main(["devices", "approve", "desktop"]) == 1
        assert srv.sent("POST") == []
        assert "mindflock devices deny desktop" in capsys.readouterr().out

    def test_deny_needs_no_confirmation(self, srv, monkeypatch, capsys):
        _inputs(monkeypatch)  # any input() call would StopIteration
        assert cli.main(["devices", "deny", "Desktop"]) == 0
        assert srv.sent("POST") == [(DENY, None)]
        assert "denied Desktop" in capsys.readouterr().out

    def test_no_pending_requests(self, monkeypatch, capsys):
        srv = FakeServer(monkeypatch, _status(requests=[]))
        assert cli.main(["devices", "deny", "desktop"]) == 1
        assert srv.sent("POST") == []
        assert "no device is asking to join" in capsys.readouterr().err

    def test_unknown_needle_lists_who_is_waiting(self, srv, capsys):
        assert cli.main(["devices", "approve", "nas", "--yes"]) == 1
        assert srv.sent("POST") == []
        err = capsys.readouterr().err
        assert "'nas'" in err and "desktop" in err


# --------------------------------------------------------------------------- #
# remove / leave
# --------------------------------------------------------------------------- #
class TestRemoveLeave:
    def test_remove_by_host_reports_rekey(self, srv, capsys):
        srv.posts["/api/fleet/members/mac-mini/remove"] = {
            "rekeyed": ["ml-rig"],
            "missed": [],
        }
        assert cli.main(["devices", "remove", "Mac-Mini", "--yes"]) == 0
        assert srv.sent("POST") == [
            ("/api/fleet/members/mac-mini/remove", {"rotate_tokens": True})
        ]
        out, err = capsys.readouterr()
        assert "removed Mac-Mini" in out
        assert "new key sent to: ml-rig" in out
        assert (
            "If Mac-Mini was lost or stolen, also remove it from your tailnet in "
            "the Tailscale admin console — that cuts it off everywhere at once, "
            "even from devices that are offline now." in out
        )
        assert err == ""

    def test_remove_names_the_members_it_missed(self, srv, capsys):
        srv.posts["/api/fleet/members/mac-mini/remove"] = {
            "rekeyed": [],
            "missed": ["ml-rig"],
        }
        assert cli.main(["devices", "remove", "mac-mini", "-y"]) == 0
        out, err = capsys.readouterr()
        # Healed on its next contact (the others keep the old key a while) —
        # not "it must join again".
        assert "ml-rig was offline — it gets the new key when it's back" in out
        assert "join again" not in out + err

    def test_remove_confirm_carries_the_tailnet_advice(self, srv, monkeypatch):
        seen = _inputs(monkeypatch, "n")
        assert cli.main(["devices", "remove", "mac-mini"]) == 1
        assert "also remove it from your tailnet" in seen[0]

    def test_remove_this_computer_is_a_leave_with_leave_wording(
        self, srv, monkeypatch, capsys
    ):
        # The server answers a self-removal by leaving: no re-key, no token
        # rotation — so the CLI must not promise either.
        seen = _inputs(monkeypatch, "y")
        srv.posts["/api/fleet/members/laptop/remove"] = {"left": True}
        assert cli.main(["devices", "remove", "Laptop"]) == 0
        assert seen[0].startswith("Take this computer out of your devices?")
        assert "new shared key" not in seen[0]
        assert "doesn't change your devices' shared key" in seen[0]
        assert srv.sent("POST") == [("/api/fleet/members/laptop/remove", None)]
        out = capsys.readouterr().out
        assert "left your devices" in out
        assert "removed" not in out

    def test_remove_answer_left_true_reports_a_leave(self, monkeypatch, capsys):
        # The status listed this computer without `self` (an older server):
        # the server's {"left": true} still decides what is printed.
        st = _status()
        st["members"][0] = {**st["members"][0], "self": False}
        srv = FakeServer(monkeypatch, st)
        srv.posts["/api/fleet/members/laptop/remove"] = {"left": True}
        assert cli.main(["devices", "remove", "laptop", "-y"]) == 0
        out = capsys.readouterr().out
        assert "left your devices" in out and "removed laptop" not in out

    def test_remove_asks_first(self, srv, monkeypatch, capsys):
        seen = _inputs(monkeypatch, "n")
        assert cli.main(["devices", "remove", "mac-mini"]) == 1
        assert "Mac-Mini" in seen[0]
        assert srv.sent("POST") == []

    def test_remove_unknown_device(self, srv, capsys):
        assert cli.main(["devices", "remove", "nas", "--yes"]) == 1
        assert srv.sent("POST") == []
        err = capsys.readouterr().err
        assert "isn't one of your devices" in err and "mac-mini" in err

    def test_leave(self, srv, capsys):
        assert cli.main(["devices", "leave", "--yes"]) == 0
        assert srv.sent("POST") == [("/api/fleet/leave", None)]
        assert "settings sync is off" in capsys.readouterr().out

    def test_leave_declined(self, srv, monkeypatch):
        _inputs(monkeypatch, KeyboardInterrupt)
        assert cli.main(["devices", "leave"]) == 1
        assert srv.sent("POST") == []


# --------------------------------------------------------------------------- #
# Review fixes: join confirmation, cancel, sync_error, token rotation,
# who runs PR review
# --------------------------------------------------------------------------- #
SETTINGS_NOTE = (
    "takes Work-PC's shared settings where Work-PC has them; your own stay where "
    "it has none"
)


class TestJoinConfirms:
    """Joining makes the other device lead the first sync — say so and ask
    (it was the one step that changes this computer's settings with no
    question)."""

    def test_code_join_asks_and_names_the_settings_rule(self, srv, monkeypatch):
        seen = _inputs(monkeypatch, "y")
        srv.posts["/api/fleet/join"] = {"state": "joined", "host": "Work-PC"}
        assert cli.main(["devices", "join", "work-pc", "ABCD-EFGH"]) == 0
        assert SETTINGS_NOTE in seen[0]
        assert [p for p, _ in srv.sent("POST")] == ["/api/fleet/join"]

    @pytest.mark.parametrize("answer", ["n", "", EOFError, KeyboardInterrupt])
    def test_declined_code_join_sends_nothing(self, srv, monkeypatch, capsys, answer):
        _inputs(monkeypatch, answer)
        assert cli.main(["devices", "join", "laptop", "ABCD-EFGH"]) == 1
        assert srv.sent("POST") == []
        assert "not joined" in capsys.readouterr().out

    def test_declined_request_join_sends_nothing(self, srv, monkeypatch):
        seen = _inputs(monkeypatch, "n")
        assert cli.main(["devices", "join", "Work-PC"]) == 1
        assert "Work-PC's shared settings" in seen[0]
        assert srv.sent("POST") == []

    def test_yes_skips_the_question(self, srv, monkeypatch):
        _inputs(monkeypatch)  # any input() call would StopIteration
        srv.posts["/api/fleet/join"] = {"state": "joined"}
        assert cli.main(["devices", "join", "--yes", "laptop", "ABCD-EFGH"]) == 0


def _waiting(device="mac-mini", host="Mac-Mini", state="waiting"):
    return _status(
        join={"state": state, "device": device, "host": host, "code": "654 321"}
    )


class TestCancel:
    def test_cancel_withdraws_a_waiting_request(self, monkeypatch, capsys):
        srv = FakeServer(monkeypatch, _waiting())
        assert cli.main(["devices", "cancel"]) == 0
        assert srv.sent("DELETE") == [("/api/fleet/request", None)]
        assert "stopped asking" in capsys.readouterr().out

    def test_cancel_with_nothing_pending_sends_nothing(self, srv, capsys):
        assert cli.main(["devices", "cancel"]) == 0
        assert srv.sent("DELETE") == []
        assert "not asking to join" in capsys.readouterr().out

    def test_cancel_refused_while_joining(self, monkeypatch, capsys):
        srv = FakeServer(monkeypatch, _waiting(state="joining"))
        srv.deletes["/api/fleet/request"] = {
            "state": "joining",
            "host": "Mac-Mini",
        }
        assert cli.main(["devices", "cancel"]) == 1
        assert "too late to cancel — Mac-Mini already approved" in (
            capsys.readouterr().err
        )

    def test_list_names_the_cancel_command(self, monkeypatch, capsys):
        FakeServer(monkeypatch, _waiting())
        cli.main(["devices"])
        line = next(
            l for l in capsys.readouterr().out.splitlines() if "Waiting for" in l
        )
        assert "mindflock devices cancel" in line

    def test_join_while_asking_another_device_points_at_cancel(
        self, monkeypatch, capsys
    ):
        srv = FakeServer(monkeypatch, _waiting())
        assert cli.main(["devices", "join", "-y", "work-pc"]) == 1
        assert srv.sent("POST") == []
        assert "mindflock devices cancel" in capsys.readouterr().err

    def test_join_same_device_again_resumes_the_wait(self, monkeypatch, capsys):
        # The terminal that asked was closed: re-running the command picks the
        # request back up instead of "already joining — cancel that first".
        srv = FakeServer(monkeypatch, _waiting())
        srv.gets["/api/fleet/request"] = [{"state": "joined", "host": "Mac-Mini"}]
        _inputs(monkeypatch)  # no question: the request is already out
        assert cli.main(["devices", "join", "Mac-Mini"]) == 0
        assert srv.sent("POST") == []
        out = capsys.readouterr().out
        assert "Still waiting for Mac-Mini" in out and "654 321" in out


class TestCtrlCWithdraws:
    def _ask(self, srv):
        srv.posts["/api/fleet/request"] = {
            "state": "waiting",
            "device": "mac-mini",
            "host": "Mac-Mini",
            "code": "123 456",
        }
        srv.gets["/api/fleet/request"] = [{"state": "waiting"}]

    def test_ctrl_c_after_approval_says_the_join_goes_on(
        self, srv, monkeypatch, capsys
    ):
        self._ask(srv)
        srv.deletes["/api/fleet/request"] = {"state": "joining"}

        def _sleep(s):
            raise KeyboardInterrupt

        monkeypatch.setattr(cli.time, "sleep", _sleep)
        assert cli.main(["devices", "join", "-y", "mac-mini"]) == 130
        err = capsys.readouterr().err
        assert "already approved" in err and "cancelled" not in err

    def test_ctrl_c_says_the_other_device_stops_seeing_it(
        self, srv, monkeypatch, capsys
    ):
        self._ask(srv)

        def _sleep(s):
            raise KeyboardInterrupt

        monkeypatch.setattr(cli.time, "sleep", _sleep)
        assert cli.main(["devices", "join", "-y", "mac-mini"]) == 130
        assert srv.sent("DELETE") == [("/api/fleet/request", None)]
        assert "stopped asking Mac-Mini" in capsys.readouterr().err


class TestSyncErrorShown:
    def test_approve_prints_sync_error(self, srv, capsys):
        srv.posts[APPROVE] = {"ok": True, "sync_error": "settings sync: boom"}
        assert cli.main(["devices", "approve", "desktop", "--yes"]) == 0
        out, err = capsys.readouterr()
        assert "approved Desktop" in out
        assert "! settings sync: settings sync: boom" in err

    def test_approve_without_sync_error_is_quiet(self, srv, capsys):
        srv.posts[APPROVE] = {"ok": True, "sync_error": ""}
        cli.main(["devices", "approve", "desktop", "--yes"])
        assert capsys.readouterr().err == ""

    def test_add_paired_prints_sync_error(self, srv, capsys):
        srv.posts["/api/fleet/add-paired"] = {
            "ok": True,
            "device": "work-pc",
            "sync_error": "Work-PC didn't answer",
        }
        assert cli.main(["devices", "add", "work-pc", "-y"]) == 0
        assert "! settings sync: Work-PC didn't answer" in capsys.readouterr().err

    def test_join_prints_sync_error_field(self, srv, capsys):
        srv.posts["/api/fleet/join"] = {
            "state": "joined",
            "host": "Laptop",
            "sync_error": "Laptop didn't answer",
        }
        assert cli.main(["devices", "join", "-y", "laptop", "ABCD-EFGH"]) == 0
        assert "! settings sync: Laptop didn't answer" in capsys.readouterr().err


class TestRemoveTokens:
    PATH = "/api/fleet/members/mac-mini/remove"

    def test_keep_tokens_sends_false(self, srv):
        assert cli.main(["devices", "remove", "mac-mini", "-y", "--keep-tokens"]) == 0
        assert srv.sent("POST") == [(self.PATH, {"rotate_tokens": False})]

    def test_reports_rotated_and_failed(self, srv, capsys):
        srv.posts[self.PATH] = {
            "rekeyed": ["ml-rig"],
            "missed": [],
            "rotated": ["laptop", "ml-rig"],
            "rotate_failed": ["nas"],
        }
        assert cli.main(["devices", "remove", "mac-mini", "-y"]) == 0
        out, err = capsys.readouterr()
        assert "new access token on: laptop, ml-rig" in out
        assert "couldn't replace the access token on nas" in err

    def test_confirm_says_what_removal_does(self, srv, monkeypatch):
        seen = _inputs(monkeypatch, "n", "n")
        cli.main(["devices", "remove", "mac-mini"])
        cli.main(["devices", "remove", "mac-mini", "--keep-tokens"])
        assert "access token is replaced" in seen[0]
        assert "KEEP working" in seen[1]


class TestAutomationShown:
    def _st(self, *flags):
        st = _status()
        for m, on in zip(st["members"], flags):
            m["automation"] = on
        return st

    def test_member_running_pr_review_is_marked(self, monkeypatch, capsys):
        FakeServer(monkeypatch, self._st(False, True, False))
        cli.main(["devices"])
        out = capsys.readouterr().out
        mini = next(l for l in out.splitlines() if "Mac-Mini" in l)
        assert "runs PR review & issues" in mini
        assert sum("runs PR review & issues" in l for l in out.splitlines()) == 1
        assert "! " not in out.split("Your devices")[1].split("Code ")[0]

    def test_none_running_warns(self, monkeypatch, capsys):
        FakeServer(monkeypatch, self._st(False, False, False))
        cli.main(["devices"])
        out = capsys.readouterr().out
        assert "none of your devices runs PR review" in out
        # There's no "on" switch any more: the runner is moved with Run here.
        assert "choose Run here (Settings → Devices)" in out
        assert "turn it on" not in out

    def test_several_running_warns(self, monkeypatch, capsys):
        FakeServer(monkeypatch, self._st(True, True, False))
        cli.main(["devices"])
        out = capsys.readouterr().out
        assert "Laptop, Mac-Mini all run PR review" in out
        # ...and no "off" switch: point at Run here on the one to keep.
        assert "choose Run here (Settings → Devices)" in out
        assert "turn it off" not in out

    def test_old_server_without_the_field_says_nothing(self, srv, capsys):
        cli.main(["devices"])
        assert "PR review" not in capsys.readouterr().out

    def test_a_member_too_old_to_say_is_left_out(self, monkeypatch, capsys):
        # null = that device's MindFlock doesn't report it: neither "none of
        # your devices runs it" nor counted as running it.
        FakeServer(monkeypatch, self._st(None, None, False))
        cli.main(["devices"])
        assert "PR review" not in capsys.readouterr().out
        FakeServer(monkeypatch, self._st(None, True, False))
        cli.main(["devices"])
        out = capsys.readouterr().out
        assert "none of your devices" not in out and "all run" not in out
