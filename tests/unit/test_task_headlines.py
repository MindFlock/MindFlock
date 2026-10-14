"""core.task_headlines + the ``task_headline`` field on every instance row.

The pure reading is pinned in test_task_headline.py; this file pins what the
server does with it: the last MEANINGFUL prompt wins and is remembered per
session and conversation, the create task is the fallback, the field rides
the full snapshot, the cheap snapshot and the probe carry-over, pending rows
carry the key, a removed session is forgotten, and rows from a device that
predates the field get one computed here.
"""

import pytest

from backend import session
from backend.web import server
from backend.web.core import pending
from backend.web.core import remote
from backend.web.core import task_headlines as th


@pytest.fixture(autouse=True)
def _clean_memo(monkeypatch):
    monkeypatch.setattr(th, "_MEMO", {})


# --- core.task_headlines.for_session ------------------------------------------
def _text(*args):
    return th.for_session(*args).text


def test_a_meaningful_prompt_is_the_headline_and_is_remembered():
    prompt = "please add a csv export to the reports page"
    got = th.for_session("s", prompt, None, "x", "c1")
    assert got == th.TaskHeadline("Add CSV export to reports page", prompt)
    assert th._MEMO["s"] == ("c1", "Add CSV export to reports page", prompt)


def test_continue_keeps_the_last_real_task_and_its_prompt():
    prompt = "rename the sync flag to mirror"
    th.for_session("s", prompt, None, None, "c1")
    for weak in (
        "continue",
        "yes",
        "commit and push",
        "progress?",
        "[MindFlock note n1] committed",
    ):
        got = th.for_session("s", weak, None, None, "c1")
        # The tooltip source is the prompt behind the headline, never the
        # "continue" that came after it.
        assert got == th.TaskHeadline("Rename sync flag to mirror", prompt), weak


def test_a_new_conversation_does_not_inherit_the_old_headline():
    th.for_session("s", "rename the sync flag", None, None, "c1")
    # /clear (or a fresh launch) → new conversation id: the create task, not
    # the dead conversation's headline.
    got = th.for_session("s", "continue", None, "fix the login redirect loop", "c2")
    assert got == th.TaskHeadline(
        "Fix login redirect loop", "fix the login redirect loop"
    )


def test_the_create_task_is_the_fallback_before_any_prompt():
    assert (
        _text("s", None, None, "fix the login redirect loop")
        == "Fix login redirect loop"
    )
    got = th.for_session("s", "", "", "\n\n  add a README section\nwith details")
    assert got.text == "Add README section"
    assert got.prompt == "add a README section\nwith details"


def test_a_weak_prompt_is_shown_when_there_is_nothing_better():
    assert th.for_session("s", "continue", None, None) == th.TaskHeadline(
        "Continue", "continue"
    )
    assert _text("s", "commit and push", None, "") == "Commit and push"


def test_nothing_at_all_is_none():
    assert th.for_session("s", None, None, None) == th.TaskHeadline(None, None)
    assert th.for_session("s", "hello", None, None) == th.TaskHeadline(None, None)


def test_the_snippet_is_used_when_the_full_body_is_missing():
    assert _text("s", None, "why is the worker leaking memory", None) == (
        "Investigate worker leaking memory"
    )


def test_drop_and_prune_forget_sessions():
    th.for_session("a", "add retries to the sender", None, None)
    th.for_session("b", "add retries to the poller", None, None)
    th.for_session("rig::c", "add retries to the cron", None, None)
    th.drop("a")
    assert "a" not in th._MEMO
    th.prune(["x"])
    # A local title that left the engine goes; a remote row is not the
    # engine's to judge.
    assert "b" not in th._MEMO and "rig::c" in th._MEMO


def test_the_memo_is_bounded(monkeypatch):
    monkeypatch.setattr(th, "_MEMO_MAX", 3)
    for n in range(5):
        th.for_session("s%d" % n, "add feature number %d" % n, None, None)
    assert len(th._MEMO) == 3 and "s0" not in th._MEMO and "s4" in th._MEMO


# --- the server row -------------------------------------------------------------
def _inst(title, prompt="fix the login redirect loop"):
    # Never registered in ENGINE: these tests must not touch the live state.
    return session.NewInstance(
        session.InstanceOptions(
            title=title, path=".", program="claude", in_place=True, prompt=prompt
        )
    )


@pytest.fixture()
def probes(monkeypatch):
    """The transcript/git probes behind a row, replaced by settable values."""
    said = {"prompt": None, "conversation": "c1"}
    monkeypatch.setattr(
        server,
        "_session_last_prompt",
        lambda i: (said["prompt"] or "").split("\n")[0] or None,
    )
    monkeypatch.setattr(server, "_session_last_prompt_full", lambda i: said["prompt"])
    monkeypatch.setattr(server, "_session_conversation", lambda i: said["conversation"])
    monkeypatch.setattr(server, "_session_stage", lambda i: {"stage": "agent"})
    monkeypatch.setattr(server, "_session_tokens", lambda i: {})
    return said


def test_the_full_row_carries_the_headline(probes):
    inst = _inst("th-row-1")
    try:
        probes["prompt"] = "can you add a csv export to the reports page\nthanks"
        row = server._session_snapshot(inst, {})
        assert row["task_headline"] == "Add CSV export to reports page"
        assert row["last_prompt_full"].startswith("can you add a csv export")
        # The newest prompt is a bare "continue": the row keeps the task.
        server._forget_probes("th-row-1")
        probes["prompt"] = "continue"
        row = server._session_snapshot(inst, {})
        assert row["last_prompt"] == "continue"
        assert row["task_headline"] == "Add CSV export to reports page"
        # The row's tooltip source stays the prompt behind the headline.
        assert row["task_headline_prompt"].startswith("can you add a csv export")
    finally:
        server._forget_probes("th-row-1")


def test_the_full_row_falls_back_to_the_create_task(probes):
    inst = _inst("th-row-2", prompt="make the importer faster")
    try:
        row = server._session_snapshot(inst, {})
        assert row["last_prompt"] is None
        assert row["task_headline"] == "Speed up importer"
        assert row["task_headline_prompt"] == "make the importer faster"
    finally:
        server._forget_probes("th-row-2")


def test_the_cheap_row_has_the_field_and_carries_the_published_one():
    inst = _inst("th-row-3", prompt="fix the login redirect loop")
    cold = server._session_snapshot_cheap(inst, {}, None)
    assert cold["task_headline"] == "Fix login redirect loop"
    assert cold["task_headline_prompt"] == "fix the login redirect loop"
    prev = {
        "task_headline": "Add CSV export",
        "task_headline_prompt": "add a csv export",
    }
    warm = server._session_snapshot_cheap(inst, {}, prev)
    assert warm["task_headline"] == "Add CSV export"
    assert warm["task_headline_prompt"] == "add a csv export"
    assert "task_headline" in server._SNAPSHOT_PROBE_KEYS
    assert "task_headline_prompt" in server._SNAPSHOT_PROBE_KEYS


def test_the_headline_rides_the_probe_memo(probes):
    inst = _inst("th-row-4")
    calls = []
    real = th.for_session

    def counting(*a, **k):
        calls.append(1)
        return real(*a, **k)

    try:
        server._forget_probes("th-row-4")
        import backend.web.core.task_headlines as mod

        mod_for = mod.for_session
        mod.for_session = counting
        try:
            server._session_task_headline_cached(inst, "add a flag", None)
            server._session_task_headline_cached(inst, "add a flag", None)
        finally:
            mod.for_session = mod_for
        assert len(calls) == 1, "a second poll inside the TTL must not recompute"
    finally:
        server._forget_probes("th-row-4")


def test_pending_rows_carry_the_key():
    import inspect

    src = inspect.getsource(pending.rows)
    assert '"task_headline": None' in src
    assert '"task_headline_prompt": None' in src


def test_forgetting_a_session_drops_its_headline():
    th.for_session("th-gone", "add retries to the sender", None, None)
    server._forget_session_state("th-gone")
    assert "th-gone" not in th._MEMO


# --- remote rows ----------------------------------------------------------------
def _device(monkeypatch, rows):
    dev = {
        "key": "rig",
        "host": "rig",
        "instances": rows,
        "instances_ok": True,
    }
    monkeypatch.setattr(remote, "_DEVICES", {"rig": dev})
    monkeypatch.setattr(remote, "_connected", lambda d: True)


def test_an_old_devices_rows_get_a_headline_here(monkeypatch):
    _device(
        monkeypatch,
        [
            {
                "title": "w1",
                "last_prompt": "why is the cache cold after deploys",
                "task": None,
            }
        ],
    )
    (row,) = remote.merged_instances()
    assert row["title"] == "rig::w1"
    assert row["task_headline"] == "Investigate cache cold"
    assert row["task_headline_prompt"] == "why is the cache cold after deploys"


def test_a_new_devices_headline_is_passed_through(monkeypatch):
    _device(
        monkeypatch,
        [{"title": "w1", "last_prompt": "continue", "task_headline": "Theirs"}],
    )
    (row,) = remote.merged_instances()
    assert row["task_headline"] == "Theirs"
