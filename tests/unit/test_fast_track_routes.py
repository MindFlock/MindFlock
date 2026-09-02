"""End-to-end coverage of the fast-track routes against a real git worktree.

Proves the route actually arms/disarms and that the run shows up on the session
DTO — not merely that the paths are registered.
"""

import asyncio
import json
import subprocess

import pytest

from backend.web import server
from backend.web.core import autopilot as ap


def _git(wt, *args):
    subprocess.run(
        ["git", "-C", str(wt), *args],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


@pytest.fixture
def wt(tmp_path):
    """A real repo with one commit, so stage probes have something to read."""
    d = tmp_path / "repo"
    d.mkdir()
    _git(d, "init", "-q")
    _git(d, "config", "user.email", "t@example.com")
    _git(d, "config", "user.name", "T")
    (d / "a.txt").write_text("hello\n")
    _git(d, "add", "-A")
    _git(d, "commit", "-q", "-m", "init")
    return d


@pytest.fixture
def inst(wt, monkeypatch):
    """A started in-memory session whose workspace is ``wt``, in the engine."""
    from backend.session.instance import FromInstanceData
    from backend.session.storage import GitWorktreeData, InstanceData, Status
    from datetime import datetime, timezone

    t = datetime.now(timezone.utc)
    data = InstanceData(
        title="ft-session",
        path=str(wt),
        branch="b",
        status=Status.Running,
        created_at=t,
        updated_at=t,
        program="bash",
        worktree=GitWorktreeData(
            repo_path=str(wt),
            worktree_path=str(wt),
            session_name="ft-session",
            branch_name="b",
        ),
    )
    i = FromInstanceData(data, attach=False)
    monkeypatch.setitem(server.ENGINE.instances, "ft-session", i)
    return i


@pytest.fixture(autouse=True)
def _isolated_store(monkeypatch, tmp_path):
    monkeypatch.setenv("MINDFLOCK_AUTOPILOT_FILE", str(tmp_path / "ap.json"))
    yield


def _post(title, payload=None):
    return asyncio.run(server.instance_fast_track(title, payload))


def _body(resp):
    return json.loads(resp.body)


def test_arming_records_the_target(inst):
    resp = _post("ft-session", {"depth": "push", "message": "do the thing"})
    assert resp.status_code == 200
    assert _body(resp)["autopilot"]["depth"] == "push"
    rec = ap.get("ft-session")
    assert rec["depth"] == "push"
    assert rec["state"] == "running"
    assert rec["message"] == "do the thing"
    assert rec["source"] == "session"


def test_depth_defaults_to_the_configured_rung(inst):
    resp = _post("ft-session", {"message": "m"})
    assert resp.status_code == 200
    # Nothing configured in the isolated settings file -> the built-in default.
    assert _body(resp)["autopilot"]["depth"] == "pr"


def test_an_unknown_depth_is_refused(inst):
    resp = _post("ft-session", {"depth": "teleport", "message": "m"})
    assert resp.status_code == 400
    assert "unknown depth" in _body(resp)["error"]
    assert ap.get("ft-session") is None, "a refused request must arm nothing"


def test_the_agent_rung_is_intake_only(inst):
    """Arming "agent" on a session that already exists would mean "do nothing"."""
    resp = _post("ft-session", {"depth": "agent", "message": "m"})
    assert resp.status_code == 400
    assert "intake" in _body(resp)["error"]


def test_a_dirty_tree_gets_a_generated_message_rather_than_a_refusal(inst, wt):
    """THE ⏩ REGRESSION. The button presses with no message, and
    .mindflock_commit_msg only exists once something has committed THROUGH
    MindFlock — so the single most common press ("I have work, carry it to a PR")
    was rejected outright with a red toast. A generated subject beats a chain that
    refuses to start."""
    (wt / "b.txt").write_text("uncommitted\n")
    resp = _post("ft-session", {"depth": "pr"})
    assert resp.status_code == 200
    msg = ap.get("ft-session")["message"]
    assert msg, "a message must have been generated"
    assert "ft-session" in msg


def test_a_generated_message_is_marked_as_a_placeholder(inst, wt):
    """ "Work on ft-session" describes the SESSION, not the change. The flag is
    what licenses the commit step to replace it with a model-written message once
    the diff is final — and arming stays cheap, so no model is asked here."""
    (wt / "b.txt").write_text("uncommitted\n")
    _post("ft-session", {"depth": "pr"})
    assert ap.get("ft-session")["message_auto"] is True


def test_a_typed_message_is_never_a_placeholder(inst, wt):
    """What the user typed in the commit dialog outranks anything a model would
    write later, so it must not be flagged as replaceable."""
    (wt / "b.txt").write_text("uncommitted\n")
    _post("ft-session", {"depth": "pr", "message": "Fix the login redirect"})
    rec = ap.get("ft-session")
    assert rec["message"] == "Fix the login redirect"
    assert rec["message_auto"] is False


def test_rearming_an_intake_run_keeps_its_placeholder_flag(inst, wt):
    """⏩ on a session intake already armed adopts the ticket's name — and must
    carry its placeholder flag across, or re-arming would freeze the request's
    title into the commit that the generator was meant to replace."""
    (wt / "b.txt").write_text("uncommitted\n")
    ap.arm(
        "ft-session",
        "pr",
        source="tix",
        item="sc-1421",
        message="Add customer phone numbers to intake",
        message_auto=True,
    )
    _post("ft-session", {"depth": "pr"})
    rec = ap.get("ft-session")
    assert rec["message"] == "Add customer phone numbers to intake"
    assert rec["message_auto"] is True


def test_a_recovered_message_is_never_a_placeholder(inst, wt):
    (wt / "b.txt").write_text("uncommitted\n")
    (wt / server._COMMIT_MSG_FILE).write_text("recovered subject\n")
    (wt / server._COMMIT_STATUS_FILE).write_text("1\n")
    _post("ft-session", {"depth": "pr"})
    assert ap.get("ft-session")["message_auto"] is False


def test_a_clean_tree_needs_no_message(inst):
    """Nothing to commit means nothing to name — the chain may still push/PR."""
    resp = _post("ft-session", {"depth": "push"})
    assert resp.status_code == 200


def test_a_message_from_a_BLOCKED_commit_is_reused(inst, wt):
    """Reuse is for retrying a commit the hooks blocked, so it requires a pending
    FAILURE — not merely a message file lying around."""
    (wt / "b.txt").write_text("uncommitted\n")
    (wt / server._COMMIT_MSG_FILE).write_text("recovered subject\n")
    (wt / server._COMMIT_STATUS_FILE).write_text("1\n")
    resp = _post("ft-session", {"depth": "pr"})
    assert resp.status_code == 200
    assert ap.get("ft-session")["message"] == "recovered subject"


def test_a_message_left_by_UNRELATED_work_is_not_adopted(inst, wt):
    """The hazard this rule exists for: a message file survived earlier work and
    the next thing armed silently inherited its subject. Caught in the wild about
    to record one feature's files under a message describing a DB migration."""
    (wt / "b.txt").write_text("uncommitted\n")
    (wt / server._COMMIT_MSG_FILE).write_text("Refactor database schema\n")
    (wt / server._COMMIT_STATUS_FILE).write_text("0\n")  # that commit SUCCEEDED
    resp = _post("ft-session", {"depth": "pr"})
    assert resp.status_code == 200
    assert "Refactor database schema" not in ap.get("ft-session")["message"]


def test_arming_captures_the_branch_for_drift_detection(inst):
    _post("ft-session", {"depth": "push", "message": "m"})
    # The push/PR/merge routes resolve the LIVE branch, so a mid-run switch would
    # retarget the chain; the armed branch is what makes that detectable.
    assert ap.get("ft-session")["branch"] in ("b", "master", "main")


class TestTheCommitStepsMessage:
    """What the autopilot actually commits UNDER — the fast-track half of ✨.

    The commit step is where the diff is final, so it is the only honest place to
    ask a model for a subject; these prove which messages it may replace and that
    a model that can't answer never stops the commit.
    """

    @pytest.fixture
    def committed(self, monkeypatch):
        """Capture the message ``_autopilot_commit`` commits under."""
        seen: list[str] = []

        def fake_commit(title, wt, msg, skip=""):
            seen.append(msg)
            return None

        monkeypatch.setattr(server, "_commit_into_shell", fake_commit)
        monkeypatch.setattr(server, "_forget_probes", lambda *a, **k: None)
        monkeypatch.setattr(server._live_stage, "watch", lambda *a, **k: None)
        return seen

    def _run(self, wt, rec_over, fields=None):
        rec = ap._blank()
        rec.update({"depth": "pr", "state": "running"})
        rec.update(rec_over)
        out = fields if fields is not None else {}
        assert server._autopilot_commit("ft-session", str(wt), rec, {}, out) is True
        return out

    def test_a_placeholder_is_replaced_by_the_written_message(
        self, inst, wt, committed, monkeypatch
    ):
        monkeypatch.setattr(
            server._commit_message, "suggest_or_none", lambda *a, **k: "Add a b.txt"
        )
        fields = self._run(wt, {"message": "Work on ft-session", "message_auto": True})
        assert committed == ["Add a b.txt"]
        # Written back, so a pre-commit retry commits the same sentence instead of
        # paying for a second turn.
        assert fields["message"] == "Add a b.txt"
        assert fields["message_auto"] is False

    def test_a_human_message_is_left_alone(self, inst, wt, committed, monkeypatch):
        def never(*a, **k):
            raise AssertionError("must not ask a model about a typed message")

        monkeypatch.setattr(server._commit_message, "suggest_or_none", never)
        self._run(wt, {"message": "Fix the login redirect"})
        assert committed == ["Fix the login redirect"]

    def test_an_intake_items_own_name_is_replaced_by_the_written_message(
        self, inst, wt, committed, monkeypatch
    ):
        """An ingested ticket's title is the work as REQUESTED, not as MADE — and
        it is the same sentence for every commit in the run. So an intake message
        is a placeholder like the button's: the commit gets the ✨ generator's
        sentence, written from the final diff."""
        monkeypatch.setattr(
            server._commit_message, "suggest_or_none", lambda *a, **k: "Add a b.txt"
        )
        self._run(
            wt,
            {
                "message": "Add customer phone numbers to intake",
                "message_auto": True,
                "source": "tix",
                "item": "sc-1421",
            },
        )
        assert committed == ["Add a b.txt"]

    def test_an_intake_items_name_is_the_fallback_when_no_model_answers(
        self, inst, wt, committed, monkeypatch
    ):
        """…and it is a much better fallback than "Work on sc-1421", so it stays
        on the record for exactly that."""
        monkeypatch.setattr(
            server._commit_message, "suggest_or_none", lambda *a, **k: None
        )
        self._run(
            wt,
            {
                "message": "Add customer phone numbers to intake",
                "message_auto": True,
                "source": "tix",
                "item": "sc-1421",
            },
        )
        assert committed == ["Add customer phone numbers to intake"]

    def test_the_intake_items_name_reaches_the_generator_as_context(
        self, inst, wt, committed, monkeypatch
    ):
        """The ticket title says what the work was ASKED to be — real context for
        a model reading the diff. The ⏩ button's own placeholder is not, so it is
        never passed."""
        hints: list[str] = []

        def spy(*a, **k):
            hints.append(k.get("hint") or "")
            return "Add a b.txt"

        monkeypatch.setattr(server._commit_message, "suggest_or_none", spy)
        self._run(
            wt,
            {
                "message": "Add customer phone numbers to intake",
                "message_auto": True,
                "source": "tix",
                "item": "sc-1421",
            },
        )
        assert hints == ["sc-1421 — Add customer phone numbers to intake"]
        hints.clear()
        self._run(wt, {"message": "Work on ft-session", "message_auto": True})
        assert hints == [""]

    def test_the_placeholder_still_commits_when_no_model_answers(
        self, inst, wt, committed, monkeypatch
    ):
        """THE FALLBACK. aider installed, claude logged out, a timeout — the work
        gets committed under a duller subject, never left uncommitted."""
        monkeypatch.setattr(
            server._commit_message, "suggest_or_none", lambda *a, **k: None
        )
        self._run(wt, {"message": "Work on ft-session", "message_auto": True})
        assert committed == ["Work on ft-session"]

    def test_a_run_armed_with_no_message_gets_a_written_one(
        self, inst, wt, committed, monkeypatch
    ):
        """Arming on a CLEAN tree records no message (nothing to describe yet);
        by commit time there is a diff, and now a real subject."""
        monkeypatch.setattr(
            server._commit_message, "suggest_or_none", lambda *a, **k: "Written subject"
        )
        self._run(wt, {"message": ""})
        assert committed == ["Written subject"]

    def test_no_message_and_no_model_falls_back_to_the_default_subject(
        self, inst, wt, committed, monkeypatch
    ):
        monkeypatch.setattr(
            server._commit_message, "suggest_or_none", lambda *a, **k: None
        )
        self._run(wt, {"message": ""})
        assert committed and "ft-session" in committed[0]


def test_unknown_session_is_404():
    resp = _post("nope", {"depth": "pr"})
    assert resp.status_code == 404


def test_cancel_disarms(inst):
    _post("ft-session", {"depth": "pr", "message": "m"})
    resp = asyncio.run(server.instance_fast_track_cancel("ft-session"))
    assert resp.status_code == 200
    assert _body(resp)["stopped"] is True
    assert ap.get("ft-session") is None


def test_cancel_is_idempotent(inst):
    resp = asyncio.run(server.instance_fast_track_cancel("ft-session"))
    assert resp.status_code == 200
    assert _body(resp)["stopped"] is False


def test_the_run_shows_up_on_the_session_dto(inst):
    _post("ft-session", {"depth": "pr", "message": "m"})
    dto = server._autopilot_dto("ft-session")
    assert dto["depth"] == "pr" and dto["state"] == "running"
    # And it is present on BOTH snapshot paths, so a cheap row never omits it.
    assert server._autopilot_dto("no-such-session") is None


def test_retryable_hooks_come_from_settings_not_the_client(inst, monkeypatch):
    """The skip list ends up inside a shell command, so it must never be
    client-supplied."""
    monkeypatch.setattr(server, "_precommit_retry_hooks", lambda: ["gitnexus-index"])
    _post(
        "ft-session",
        {"depth": "pr", "message": "m", "retryable": ["run-tests", "anything"]},
    )
    assert ap.get("ft-session")["retryable"] == ["gitnexus-index"]


def _fake_settings(monkeypatch, *, hooks="", depth=""):
    """Stand in for the real settings store, via the module the server reads."""

    class _Repo:
        precommit_retry_hooks = hooks
        fasttrack_depth = depth

    class _S:
        repository = _Repo()

    # The helpers import the settings module locally on every call (the house
    # idiom, so a settings change needs no restart), so patch the real module.
    from backend.config import settings as settings_mod

    monkeypatch.setattr(settings_mod, "load_settings", lambda: _S())


def test_settings_hook_list_is_charset_filtered(monkeypatch):
    _fake_settings(monkeypatch, hooks="gitnexus-index, bad;name, run-tests, ok_hook.1")
    got = server._precommit_retry_hooks()
    assert "gitnexus-index" in got and "ok_hook.1" in got
    assert "bad;name" not in got, "a shell metacharacter must be dropped"
    assert "run-tests" not in got, "a test hook is never skippable"


def test_the_configured_hook_list_actually_reaches_the_run(inst, monkeypatch):
    """Regression: the helpers originally called a bare ``load_settings()`` that
    does not exist on this module, and their own try/except swallowed the
    NameError — so the setting silently never applied. Assert the real read."""
    _fake_settings(monkeypatch, hooks="gitnexus-index")
    _post("ft-session", {"depth": "pr", "message": "m"})
    assert ap.get("ft-session")["retryable"] == ["gitnexus-index"]


def test_the_configured_depth_actually_applies(inst, monkeypatch):
    _fake_settings(monkeypatch, depth="push")
    assert server._fasttrack_depth() == "push"
    resp = _post("ft-session", {"message": "m"})
    assert _body(resp)["autopilot"]["depth"] == "push"


def test_a_junk_configured_depth_falls_back(monkeypatch):
    _fake_settings(monkeypatch, depth="teleport")
    assert server._fasttrack_depth() == "pr"


def test_source_defaults_cannot_choose_merge():
    """A per-source default applies to every future item with nobody watching."""
    assert server._cap_source_depth("merge") == "pr"
    assert server._cap_source_depth("push") == "push"
    assert server._cap_source_depth("") == ""


def test_per_item_depth_override_accepts_merge():
    """The person picking it is looking at the one thing it will merge."""
    assert server._start_depth_override({"depth": "merge"}) == "merge"
    assert server._start_depth_override({}) == ""
    with pytest.raises(ValueError):
        server._start_depth_override({"depth": "teleport"})


def test_a_clean_tree_arm_still_gets_a_message_at_commit_time(inst, wt):
    """Arming on a CLEAN tree is the natural way to use this — arm the session, let
    the agent work — and the route records no message then, because there is
    nothing to describe yet. The run used to halt with "no commit message to reuse"
    at the very moment it was finally ready to commit."""
    resp = _post("ft-session", {"depth": "pr"})
    assert resp.status_code == 200
    assert ap.get("ft-session")["message"] == "", "nothing to describe yet"

    # The agent has since written something; the commit step must generate a
    # subject rather than refuse.
    (wt / "b.txt").write_text("the agent's work\n")
    sent = {}
    import backend.web.server as srv

    orig = srv._commit_into_shell

    def _capture(t, w, m, skip=""):
        sent["msg"] = m
        return None  # None == success; anything else is an error string

    srv._commit_into_shell = _capture
    try:
        fields: dict = {}
        ok = srv._autopilot_commit(
            "ft-session", str(wt), ap.get("ft-session"), {}, fields
        )
    finally:
        srv._commit_into_shell = orig
    assert ok is True, "must not halt for want of a message"
    assert sent["msg"], "a subject must have been generated"


def test_an_intake_name_survives_a_re_arm(inst, wt):
    """All three intake paths record the ticket / PR / issue NAME. Re-arming with
    the button used to overwrite it with a generated "Work on <slug>", throwing
    away the one genuinely descriptive subject available."""
    ap.arm("ft-session", "pr", source="tix", message="Add phone numbers to intake")
    (wt / "b.txt").write_text("uncommitted\n")
    resp = _post("ft-session", {"depth": "pr"})
    assert resp.status_code == 200
    assert ap.get("ft-session")["message"] == "Add phone numbers to intake"
