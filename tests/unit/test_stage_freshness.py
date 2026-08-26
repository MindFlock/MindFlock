"""Stage freshness: the probe seed, publish-through, the SKIP= prefix, and the
pre-commit hook-ID parse.

The behaviour under test is the fix for "it takes a few seconds after a commit
finishes before Push appears": the publishing tick must never SERVE a memo entry
another ticker filled, and a single-session recompute must be able to reach the
list GET /api/instances serves.
"""

from backend.web import server
from backend.web.core import events as events_mod
from backend.web.core.agent_state import _parse_failed_hook_id


class _Inst:
    """Minimal weakref-able instance stand-in."""

    def __init__(self, title="s1"):
        self.Title = title


# --- _probe_seed / _session_stage_fresh -------------------------------------
def test_probe_seed_publishes_into_the_memo():
    inst = _Inst("seed1")
    server._forget_probes("seed1")
    server._probe_seed("stage", inst, {"stage": "committed"})
    # A subsequent cached read must serve the donated value without recomputing.
    calls = []

    def _compute():
        calls.append(1)
        return {"stage": "recomputed"}

    got = server._probe_cached("stage", inst, _compute)
    assert got == {"stage": "committed"}
    assert calls == [], "seeded value must satisfy the cached read"
    server._forget_probes("seed1")


def test_probe_seed_returns_the_value_and_skips_unweakrefable_stand_ins():
    class NoRef:
        __slots__ = ()  # not weakref-able
        Title = "noref"

    # Must not raise, and must still hand the value back.
    assert server._probe_seed("stage", NoRef(), {"stage": "x"}) == {"stage": "x"}


def test_session_stage_fresh_computes_then_seeds(monkeypatch):
    inst = _Inst("fresh1")
    server._forget_probes("fresh1")
    monkeypatch.setattr(server, "_session_stage", lambda i: {"stage": "pushed"})
    assert server._session_stage_fresh(inst) == {"stage": "pushed"}
    # Now a cached reader sees it without a recompute — even if _session_stage
    # would answer differently.
    monkeypatch.setattr(server, "_session_stage", lambda i: {"stage": "DIFFERENT"})
    assert server._session_stage_cached(inst) == {"stage": "pushed"}
    server._forget_probes("fresh1")


def test_snapshot_publisher_uses_the_fresh_variant():
    """Guards the actual fix: if the publisher went back to the memo, a stage
    flip would again be delayed by up to a whole extra publish period."""
    import inspect

    src = inspect.getsource(server._session_snapshot)
    assert "_session_stage_fresh(i)" in src
    assert "_session_stage_cached(i)" not in src


def test_state_ticker_still_rides_the_memo():
    """Only the publisher computes; every other reader keeps the cost collapse."""
    import inspect

    src = inspect.getsource(server._tick_state_changes)
    assert "_session_stage_cached" in src


# --- patch_session_snapshot -------------------------------------------------
def test_patch_session_snapshot_replaces_one_row():
    events_mod.set_sessions_snapshot(
        [{"title": "a", "stage": "agent"}, {"title": "b", "stage": "agent"}]
    )
    events_mod.patch_session_snapshot("b", {"title": "b", "stage": "pushed"})
    rows = {r["title"]: r["stage"] for r in events_mod.sessions_snapshot()}
    assert rows == {"a": "agent", "b": "pushed"}


def test_patch_session_snapshot_appends_an_unknown_title():
    events_mod.set_sessions_snapshot([{"title": "a", "stage": "agent"}])
    events_mod.patch_session_snapshot("new", {"title": "new", "stage": "committed"})
    assert len(events_mod.sessions_snapshot()) == 2


def test_patch_session_snapshot_ignores_a_blank_title():
    events_mod.set_sessions_snapshot([{"title": "a"}])
    events_mod.patch_session_snapshot("", {"title": "", "stage": "x"})
    assert len(events_mod.sessions_snapshot()) == 1


def test_patch_session_snapshot_stores_a_copy():
    events_mod.set_sessions_snapshot([{"title": "a", "stage": "agent"}])
    row = {"title": "a", "stage": "pushed"}
    events_mod.patch_session_snapshot("a", row)
    row["stage"] = "mutated after the call"
    assert events_mod.sessions_snapshot()[0]["stage"] == "pushed"


def test_republish_publishes_before_it_emits():
    """Ordering is load-bearing: the instances tick emits and only THEN
    publishes, so a client reacting to stage_changed by re-reading races the
    publish. _republish_session must not repeat that mistake."""
    import inspect

    src = inspect.getsource(server._republish_session)
    assert src.index("patch_session_snapshot") < src.index("_emit_state_changes")


def test_republish_never_forgets_probes():
    """_forget_probes would pop _ACTIVITY_CACHE — the only source of
    activity_since, which feeds attention ordering and the wedged-session
    watchdog — for no benefit, since the stage is already computed fresh."""
    import inspect

    # Check the BODY, not the docstring — which names _forget_probes on purpose,
    # to record why it must not be re-added.
    src = inspect.getsource(server._republish_session)
    body = src.split('"""')[2] if src.count('"""') >= 2 else src
    assert "_forget_probes" not in body


def test_republish_returns_none_for_a_missing_session():
    assert server._republish_session("definitely-not-a-session") is None


# --- The SKIP= prefix on the commit one-liner -------------------------------
def test_commit_command_is_byte_identical_without_a_skip():
    cmd = server._commit_shell_command()
    assert "SKIP=" not in cmd
    # The literal substrings other tests pin must survive.
    assert "git commit -F .mindflock_commit_msg" in cmd
    assert "git diff --quiet && break" in cmd
    assert 'echo $rc > .mindflock_commit_status; rm -f "$L"' in cmd


def test_a_landed_commit_clears_its_message_file():
    """A leftover message must only survive a FAILURE. It used to survive success
    and unrelated work, so the next commit arriving without a message silently
    adopted a stale subject — a run was caught about to record one feature's files
    under a message describing a database migration."""
    cmd = server._commit_shell_command()
    assert "[ $rc -eq 0 ] && rm -f .mindflock_commit_msg" in cmd
    # …and only on success: the retry path still needs the message.
    assert cmd.index("echo $rc") < cmd.index("rm -f .mindflock_commit_msg")


def test_a_stale_message_is_not_adopted_after_a_success(tmp_path):
    """`_pending_commit_message` mirrors GET /commit-message: offer the saved
    message only while a failure is pending."""
    (tmp_path / server._COMMIT_MSG_FILE).write_text("subject from other work\n")
    # Last attempt SUCCEEDED -> nothing pending, so nothing to adopt.
    (tmp_path / server._COMMIT_STATUS_FILE).write_text("0\n")
    assert server._pending_commit_message(str(tmp_path)) == ""
    # Last attempt FAILED -> the retry legitimately reuses it.
    (tmp_path / server._COMMIT_STATUS_FILE).write_text("1\n")
    assert server._pending_commit_message(str(tmp_path)) == "subject from other work"


def test_no_recorded_attempt_means_no_message_to_adopt(tmp_path):
    (tmp_path / server._COMMIT_MSG_FILE).write_text("orphan\n")
    assert server._pending_commit_message(str(tmp_path)) == ""


def test_commit_command_prefixes_skip_as_a_one_shot_env():
    cmd = server._commit_shell_command("gitnexus-index")
    assert "SKIP=gitnexus-index git commit -F .mindflock_commit_msg" in cmd
    # No tee/log/sed: the tty must stay intact so hook colour, GPG and
    # credential prompts behave exactly as they do without a skip.
    for banned in ("tee", "mindflock-rc", "sed "):
        assert banned not in cmd, banned


def test_commit_command_quotes_a_hostile_skip_value():
    """Defence in depth. The value is charset-filtered in
    ``_precommit_retry_hooks`` and never comes from a client, but it does end up
    inside a shell string, so the renderer quotes it too."""
    cmd = server._commit_shell_command("a; rm -rf /")
    # The separator must be inside quotes, never a live command boundary.
    assert "SKIP=a; rm -rf / git commit" not in cmd
    assert "SKIP='a; rm -rf /' git commit" in cmd


def test_commit_command_leaves_a_plain_id_list_unquoted():
    cmd = server._commit_shell_command("gitnexus-index,documentarian")
    assert "SKIP=gitnexus-index,documentarian git commit" in cmd


# --- The hook-ID parse ------------------------------------------------------
def test_hook_id_is_read_from_the_hook_id_line():
    pane = "Black format" + "." * 30 + "Failed\n- hook id: black\n- files were modified"
    assert _parse_failed_hook_id(pane) == "black"


def test_hook_id_anchors_at_the_last_failure():
    """A hook id printed by an earlier, verbose, PASSING hook must never be
    mistaken for the failing one."""
    pane = (
        "Secret scan" + "." * 30 + "Passed\n"
        "- hook id: detect-secrets\n"
        "GitNexus index" + "." * 20 + "Failed\n"
        "- hook id: gitnexus-index\n"
        "error: index is corrupt\n"
    )
    assert _parse_failed_hook_id(pane) == "gitnexus-index"


def test_hook_id_is_none_without_a_failure_line():
    assert _parse_failed_hook_id("- hook id: black\nall good") is None
    assert _parse_failed_hook_id("") is None
    assert _parse_failed_hook_id("nothing useful here") is None


def test_hook_id_is_none_for_raw_git_hooks():
    """Raw (non-pre-commit) hooks print no id at all — the retry policy must get
    None and halt rather than guess."""
    pane = "some-hook" + "." * 30 + "Failed\nboom\n"
    assert _parse_failed_hook_id(pane) is None


def test_display_name_and_hook_id_are_different_questions():
    """The motivating fact: pre-commit's `name:` is free text that does not
    slugify back to the id, so only the id line can key a retry decision."""
    from backend.web.core.agent_state import _parse_failed_step

    pane = "Black format" + "." * 30 + "Failed\n- hook id: black\n"
    assert _parse_failed_step(pane) == "Black format"
    assert _parse_failed_hook_id(pane) == "black"


# --- Wiring -----------------------------------------------------------------
def test_failed_hook_rides_the_snapshot_probe_keys():
    assert "failed_hook" in server._SNAPSHOT_PROBE_KEYS


def test_pending_rows_carry_the_same_keys_as_live_rows():
    """The invariant this test is named for, enforced against the real dicts.

    Grepping for one literal let four keys (the auth-profile block) go missing
    while the test stayed green — a pending row that is missing keys a live row
    has makes the SPA read `undefined` off half a provisioning row.
    """
    import inspect
    import re

    from backend.web.core import pending
    from backend.web.core import snapshot

    def _keys(fn):
        src = inspect.getsource(fn)
        # The literal keys of the single dict each function returns.
        return set(re.findall(r'^\s{8,}"([a-z_]+)":', src, re.M))

    live = _keys(snapshot._instance_json)
    pend = _keys(pending.rows)
    assert live, "no keys parsed from the live row builder"
    missing = live - pend
    assert not missing, "pending rows are missing live-row keys: %s" % sorted(missing)


def test_stage_route_is_registered():
    paths = [getattr(r, "path", "") for r in server.app.routes]
    assert "/api/instances/{title}/stage" in paths


def test_live_stage_watch_is_wired_into_commit_and_push():
    import inspect

    assert '_live_stage.watch(title, wt, "commit")' in inspect.getsource(
        server.instance_commit
    )
    assert '_live_stage.watch(title, wt, "push")' in inspect.getsource(
        server.instance_push_branch
    )


def test_live_stage_watch_is_a_noop_without_a_title_or_worktree():
    from backend.web.core import live_stage

    live_stage.watch("", "/tmp", "commit")
    live_stage.watch("t", "", "commit")
    assert live_stage.active_titles() == []


def test_fast_track_rejects_an_unknown_depth():
    """The route validates the rung before arming anything."""
    import inspect

    src = inspect.getsource(server.instance_fast_track)
    assert "unknown depth" in src
    assert "_autopilot.DEPTHS" in src
