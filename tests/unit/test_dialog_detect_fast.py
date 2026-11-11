"""A dialog reaches the published row well before the 4s instances tick.

``core.state_events.DialogWatch`` polls only sessions whose published row is
``working`` (or ``clarify``, for the dialog leaving): a hook marker that turns
``clarify`` (Claude/Codex ``PermissionRequest``) makes the session urgent, and
every working session with a dialog parser has its visible screen captured
every ~0.3s — all of a poll's captures in ONE tmux call, parsed only when the
text changed. server.py's ``_dialog_watch_loop`` hands a sighting to
``_republish_dialog``, which patches ``activity``/``asking`` into the row and
emits through ``_emit_state_changes`` (settle + boot quiet + the tick race
guard, as the reply watch does). A second capture of the same dialog
``confirm_s`` later is the confirmed (``screen-stable``) reading that lets the
``activity_changed`` event skip the 3s settle.
"""

from __future__ import annotations

import asyncio
import inspect
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import hypothesis as hyp
import pytest
from hypothesis import strategies as st

from backend import session as session_mod
from backend.config import settings as S
from backend.session import tmux
from backend.web import server
from backend.web.core import agent_state, asking, spoken, state_events
from backend.web.core import events as events_mod
from backend.web.core import proc as proc_mod
from backend.web.core.events import EventBus
from backend.web.core.state_events import DialogRow, DialogWatch

DATA = Path(__file__).parent / "data" / "dialogs"
CODEX_EXEC = (DATA / "codex_exec.screen.txt").read_text()
CODEX_WORKING = (DATA / "codex_working.screen.txt").read_text()


def _row(title="t", activity="working", program="codex", parses=True):
    return DialogRow(title, "mf_" + title, activity, program, parses)


class _Pane:
    """Fake tmux: ``screens[name]`` is what a capture returns."""

    def __init__(self, **screens):
        self.screens = dict(screens)
        self.calls = []

    def __call__(self, names):
        self.calls.append(list(names))
        return {n: self.screens.get(n) for n in names}


def _fp_counting(counter):
    def fp(program, screen):
        counter.append(screen)
        return agent_state.dialog_fingerprint(program, screen)

    return fp


def _watch(pane, fps=None, **kw):
    return DialogWatch(pane, _fp_counting(fps if fps is not None else []), **kw)


# ---- DialogWatch: what gets captured ---------------------------------------- #
def test_idle_offline_and_unknown_rows_are_never_captured():
    pane = _Pane(mf_a=CODEX_EXEC, mf_b=CODEX_EXEC, mf_c=CODEX_EXEC)
    w = _watch(pane)
    rows = [
        _row("a", "idle"),
        _row("b", "offline"),
        _row("c", "limit"),
    ]
    for i in range(10):
        assert w.poll(rows, now=i * 0.5) == []
    assert pane.calls == []
    assert w.captures == 0


def test_a_poll_captures_all_due_sessions_in_one_call():
    pane = _Pane(mf_a=CODEX_WORKING, mf_b=CODEX_WORKING, mf_c=CODEX_WORKING)
    w = _watch(pane)
    rows = [_row("a"), _row("b"), _row("c"), _row("d", "idle")]
    w.poll(rows, now=1.0)
    assert pane.calls == [["mf_a", "mf_b", "mf_c"]]


def test_an_unchanged_screen_is_not_parsed_again():
    pane = _Pane(mf_t=CODEX_WORKING)
    fps = []
    w = _watch(pane, fps)
    for i in range(8):
        w.poll([_row()], now=1.0 + i * 0.31)
    assert w.captures == 8
    assert len(fps) == 1  # parsed once; seven identical frames skipped
    pane.screens["mf_t"] = CODEX_WORKING + "more output\n"
    w.poll([_row()], now=4.0)
    assert len(fps) == 2


def test_a_session_without_a_dialog_parser_is_not_screen_polled():
    pane = _Pane(mf_t=CODEX_EXEC)
    w = _watch(pane)
    for i in range(5):
        assert w.poll([_row(program="mystery", parses=False)], now=i * 0.4) == []
    assert pane.calls == []


def test_captures_are_capped_per_poll_and_rotate_through_everyone():
    names = ["s%02d" % i for i in range(20)]
    pane = _Pane(**{"mf_" + n: CODEX_WORKING for n in names})
    w = _watch(pane, max_captures=8, screen_s=0.3)
    rows = [_row(n) for n in names]
    t = 1.0
    seen = set()
    for _ in range(3):
        w.poll(rows, now=t)
        t += 0.1
    assert all(len(c) <= 8 for c in pane.calls)
    for c in pane.calls:
        seen.update(c)
    assert len(seen) == 20  # nobody starved behind the cap


def test_load_stretches_the_screen_interval():
    pane = _Pane(mf_t=CODEX_WORKING)
    load = {"v": 0.2}
    w = _watch(pane, load=lambda: load["v"], screen_s=0.3)
    for i in range(30):  # 3s at a 0.1s loop
        w.poll([_row()], now=i * 0.1)
    calm = w.captures
    assert 9 <= calm <= 11  # ~every 0.3s
    load["v"] = 5.0  # far past load_hard
    w2 = _watch(_Pane(mf_t=CODEX_WORKING), load=lambda: load["v"], screen_s=0.3)
    for i in range(30):
        w2.poll([_row()], now=i * 0.1)
    assert w2.captures <= 2  # 0.3s × 6 = 1.8s between captures
    assert w2.backoff() == w2.max_backoff
    load["v"] = 1.375  # half way between soft (0.75) and hard (2.0)
    assert w2.backoff() == pytest.approx(1 + 0.5 * (w2.max_backoff - 1))


def test_a_raising_capture_or_parser_never_breaks_the_poll():
    def boom(names):
        raise RuntimeError("tmux exploded")

    w = DialogWatch(boom, agent_state.dialog_fingerprint)
    assert w.poll([_row()], now=1.0) == []

    def bad_fp(program, screen):
        raise RuntimeError("parser exploded")

    w2 = DialogWatch(_Pane(mf_t=CODEX_EXEC), bad_fp)
    assert w2.poll([_row()], now=1.0) == []


# ---- DialogWatch: what gets republished ------------------------------------- #
def test_a_dialog_on_a_working_row_is_handed_over_then_confirmed_once():
    pane = _Pane(mf_t=CODEX_WORKING)
    w = _watch(pane, screen_s=0.3, urgent_s=0.1, confirm_s=0.15)
    rows = [_row()]
    assert w.poll(rows, now=1.0) == []
    pane.screens["mf_t"] = CODEX_EXEC  # the approval screen is drawn
    got = w.poll(rows, now=1.3)
    assert got == [("t", CODEX_EXEC, False)]  # first sighting: the row
    # The republish made the row clarify; the watch re-captures fast.
    rows = [_row(activity="clarify")]
    assert w.poll(rows, now=1.35) == []  # not due yet (urgent_s)
    got = w.poll(rows, now=1.4)
    assert got == []  # 0.1s < confirm_s since first seen
    got = w.poll(rows, now=1.5)
    assert got == [("t", CODEX_EXEC, True)]  # held still: confirmed
    for i in range(1, 20):
        assert w.poll(rows, now=1.5 + i * 0.1) == []  # once each


def test_a_dialog_already_up_on_first_sight_is_handed_over():
    w = _watch(_Pane(mf_t=CODEX_EXEC))
    assert w.poll([_row()], now=1.0) == [("t", CODEX_EXEC, False)]


def test_a_dialog_leaving_a_clarify_row_is_handed_over_once():
    pane = _Pane(mf_t=CODEX_EXEC)
    w = _watch(pane, clarify_s=0.5)
    rows = [_row(activity="clarify")]
    w.poll(rows, now=1.0)
    w.poll(rows, now=1.2)  # confirm
    pane.screens["mf_t"] = CODEX_WORKING  # answered: Codex runs the command
    got = []
    for i in range(10):
        got += w.poll(rows, now=1.3 + i * 0.2)
    assert got == [("t", CODEX_WORKING, False)]


def test_a_clarify_row_without_a_parsed_dialog_is_left_to_the_tick():
    """A clarify the parser never saw (a hook-only prompt) is not ours to
    clear: no hand-over however the screen moves."""
    pane = _Pane(mf_t=CODEX_WORKING)
    w = _watch(pane, clarify_s=0.2)
    rows = [_row(activity="clarify")]
    got = []
    for i in range(10):
        pane.screens["mf_t"] = CODEX_WORKING + "line %d\n" % i
        got += w.poll(rows, now=1.0 + i * 0.25)
    assert got == []


def test_the_same_dialog_raised_again_is_handed_over_again():
    pane = _Pane(mf_t=CODEX_EXEC)
    w = _watch(pane)
    assert w.poll([_row()], now=1.0) == [("t", CODEX_EXEC, False)]
    pane.screens["mf_t"] = CODEX_WORKING
    w.poll([_row(activity="clarify")], now=2.0)
    pane.screens["mf_t"] = CODEX_EXEC
    assert w.poll([_row()], now=3.0) == [("t", CODEX_EXEC, False)]


def test_a_session_that_goes_idle_is_forgotten():
    w = _watch(_Pane(mf_t=CODEX_EXEC))
    w.poll([_row()], now=1.0)
    assert "t" in w._state
    w.poll([_row(activity="idle")], now=2.0)
    assert w._state == {}


# ---- DialogWatch: the hook marker ------------------------------------------- #
class _Marker:
    def __init__(self):
        self.key = {"mf_t": (1, 10, 7)}
        self.state = {"mf_t": "working"}
        self.reads = 0

    def stat(self, name):
        return self.key.get(name)

    def read(self, name):
        self.reads += 1
        return self.state.get(name)

    def write(self, name, state):
        k = self.key.get(name) or (0, 0, 7)
        self.key[name] = (k[0] + 1, k[1], k[2])
        self.state[name] = state


def test_a_hook_clarify_makes_the_session_urgent_until_its_dialog_is_drawn():
    pane = _Pane(mf_t=CODEX_WORKING)
    m = _Marker()
    w = _watch(pane, marker_key=m.stat, marker_state=m.read, screen_s=0.3, urgent_s=0.1)
    w.poll([_row()], now=1.0)  # baseline
    assert m.reads == 0  # a marker already there is old news
    m.write("mf_t", "clarify")  # PermissionRequest: the hook beat the redraw
    assert w.poll([_row()], now=1.1) == []  # captured at once: not drawn yet
    assert len(pane.calls) == 2
    pane.screens["mf_t"] = CODEX_EXEC
    assert w.poll([_row()], now=1.21) == [("t", CODEX_EXEC, False)]
    assert len(pane.calls) == 3  # 0.1s, not the 0.3s screen cadence


def test_a_hook_clarify_with_no_parsable_dialog_hands_over_without_one():
    pane = _Pane(mf_t=CODEX_WORKING)
    m = _Marker()
    w = _watch(pane, marker_key=m.stat, marker_state=m.read, urgent_window_s=0.5)
    w.poll([_row()], now=1.0)
    m.write("mf_t", "clarify")
    got = []
    t = 1.0
    while t < 2.0:
        t = round(t + 0.1, 2)
        got += [(x[0], x[2], t) for x in w.poll([_row()], now=t)]
    # Once, when the urgent window ran out: the full probe reads the marker.
    assert [(g[0], g[1]) for g in got] == [("t", False)]
    assert 1.5 <= got[0][2] <= 1.7


def test_a_hook_clarify_on_a_cli_without_a_parser_hands_over_at_once():
    pane = _Pane()
    m = _Marker()
    w = _watch(pane, marker_key=m.stat, marker_state=m.read)
    rows = [_row(program="aiderish", parses=False)]
    w.poll(rows, now=1.0)
    m.write("mf_t", "clarify")
    assert w.poll(rows, now=1.1) == [("t", None, False)]
    assert pane.calls == []  # nothing to read the screen with


def test_marker_moves_to_working_do_not_trigger_anything():
    pane = _Pane(mf_t=CODEX_WORKING)
    m = _Marker()
    w = _watch(pane, marker_key=m.stat, marker_state=m.read, screen_s=10.0)
    w.poll([_row()], now=1.0)
    for i in range(5):
        m.write("mf_t", "working")  # every tool call rewrites the marker
        assert w.poll([_row()], now=1.1 + i * 0.1) == []
    assert len(pane.calls) == 1  # no extra captures


# ---- the fingerprint ---------------------------------------------------------- #
def test_fingerprint_matches_the_activity_probes_dialog_test():
    assert agent_state.dialog_fingerprint("codex", CODEX_EXEC)
    assert agent_state.dialog_fingerprint("codex", CODEX_WORKING) is None
    assert agent_state.dialog_fingerprint("codex", "") is None
    # The same dialog with its cursor moved by hand is the same dialog.
    moved = CODEX_EXEC.replace("› 1. Yes", "  1. Yes").replace(
        "  2. Yes, and", "› 2. Yes, and"
    )
    if agent_state.dialog_fingerprint("codex", moved):
        assert agent_state.dialog_fingerprint(
            "codex", moved
        ) == agent_state.dialog_fingerprint("codex", CODEX_EXEC)


def test_fingerprint_is_none_on_a_usage_limit_screen(monkeypatch):
    monkeypatch.setattr(agent_state, "is_limit_screen", lambda text: True)
    assert agent_state.dialog_fingerprint("codex", CODEX_EXEC) is None


# ---- capture_visible_many ------------------------------------------------------ #
def _fake_tmux(monkeypatch, panes, fail_on=None, rc=None):
    """``run_capped`` standing in for a tmux chain over ``panes``."""
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        out = []
        code = 0
        i = 1
        while i < len(argv):
            if argv[i] == "display-message":
                out.append(argv[i + 2] + "\n")
                i += 3
            elif argv[i] == "capture-pane":
                name = argv[i + 3].removeprefix("=").removesuffix(":")
                if name == fail_on or name not in panes:
                    code = 1
                    break
                out.append(panes[name])
                i += 4
            else:
                i += 1  # ";"
        if rc is not None:
            code = rc
        return subprocess.CompletedProcess(argv, code, "".join(out).encode(), b"")

    monkeypatch.setattr(proc_mod, "run_capped", run)
    return calls


def test_capture_many_splits_one_chain_per_session(monkeypatch):
    panes = {"a": "hello a\n\n\n", "b": "x\x1e\x0cy\u2028z\n# no header\n", "c": "\n"}
    calls = _fake_tmux(monkeypatch, panes)
    got = agent_state.capture_visible_many(["a", "b", "c", "a"])
    assert got == panes
    assert len(calls) == 1  # one tmux client for all three


def test_capture_many_continues_past_a_session_that_ended(monkeypatch):
    panes = {"a": "A\n", "c": "C\n", "d": "D\n"}
    calls = _fake_tmux(monkeypatch, panes)
    got = agent_state.capture_visible_many(["a", "gone", "c", "d"])
    assert got == {"a": "A\n", "gone": None, "c": "C\n", "d": "D\n"}
    assert len(calls) == 2  # the rest went round in a fresh chain


def test_capture_many_gives_up_on_a_hung_tmux(monkeypatch):
    _fake_tmux(monkeypatch, {"a": "A\n"}, rc=124)
    assert agent_state.capture_visible_many(["a", "b"]) == {"a": None, "b": None}


def test_capture_many_of_nothing_runs_nothing(monkeypatch):
    calls = _fake_tmux(monkeypatch, {})
    assert agent_state.capture_visible_many([]) == {}
    assert calls == []


@pytest.fixture()
def scratch_tmux(monkeypatch):
    """A private tmux server (short socket dir; ``$TMUX`` unset so nothing
    can reach the developer's real server). Sessions are killed one by one —
    never kill-server."""
    if not shutil.which("tmux"):
        pytest.skip("no tmux")
    d = tempfile.mkdtemp(prefix="mfdw", dir="/tmp")
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.setenv("TMUX_TMPDIR", d)
    made = []

    def new(name, text):
        env = dict(os.environ)
        subprocess.run(
            [
                "tmux",
                "new-session",
                "-d",
                "-s",
                name,
                "-x",
                "100",
                "-y",
                "20",
                "cat %s; sleep 60" % text,
            ],
            check=True,
            env=env,
        )
        made.append(name)
        sock = subprocess.run(
            ["tmux", "display-message", "-p", "-t", name, "#{socket_path}"],
            capture_output=True,
            text=True,
            env=env,
        ).stdout.strip()
        # realpath both: macOS reports /tmp/… as /private/tmp/….
        assert os.path.realpath(sock).startswith(os.path.realpath(d)), (
            "scratch tmux escaped to %r" % sock
        )

    yield new
    for name in made:
        subprocess.run(["tmux", "kill-session", "-t", name], capture_output=True)
    shutil.rmtree(d, ignore_errors=True)


def test_capture_many_equals_one_capture_per_pane_on_real_tmux(scratch_tmux, tmp_path):
    f = tmp_path / "codex.txt"
    f.write_text(CODEX_EXEC)
    g = tmp_path / "work.txt"
    g.write_text(CODEX_WORKING)
    scratch_tmux("mfdw_a", f)
    scratch_tmux("mfdw_b", g)
    time.sleep(0.4)
    got = agent_state.capture_visible_many(["mfdw_a", "mfdw_gone", "mfdw_b"])
    for name in ("mfdw_a", "mfdw_b"):
        assert got[name] == agent_state._capture_visible(None, name)
    assert got["mfdw_gone"] is None
    assert agent_state.dialog_fingerprint("codex", got["mfdw_a"])
    assert agent_state.dialog_fingerprint("codex", got["mfdw_b"]) is None


# ---- the server: a fake Codex pane → row clarify + asking ---------------------- #
class _Inst:
    Program = "codex"

    def __init__(self, title):
        self.Title = title
        self.Status = type("St", (), {"name": "Running"})()

    def Started(self):  # noqa: N802
        return True

    def GetWorktreePath(self):  # noqa: N802
        return "/tmp/wt"


def _forget(title="t"):
    for d in (asking._STATE, asking._INPUT_AT, asking._DIALOG_MEMO):
        d.pop(title, None)
    agent_state._ACTIVITY_CACHE.pop(title, None)
    agent_state._LAST_SCREEN.pop(title, None)


@pytest.fixture()
def srv(monkeypatch, tmp_path, isolate_settings_store):
    """ENGINE = one Codex session "t", published ``working``; tmux faked at
    the seams ``_agent_activity`` reads (has-session, pane meta) and the
    batch capture the watch takes."""
    S.update_settings(notifications={"prose_questions": True})
    _forget()
    b = EventBus()
    monkeypatch.setattr(events_mod, "BUS", b)
    server._EVENT_SNAPSHOT.clear()
    state_events._SPOKEN_ANNOUNCED.clear()
    spoken._REPLY_CACHE.clear()
    monkeypatch.setenv("MINDFLOCK_ACTIVITY_MARKER_DIR", str(tmp_path / "markers"))
    monkeypatch.setenv("MINDFLOCK_VOICE_FLAG_DIR", str(tmp_path / "voice"))
    monkeypatch.setattr(server, "_REPLY_WATCH_LOOP_ENABLED", False)
    monkeypatch.setattr(server, "_DIALOG_WATCH_LOOP_ENABLED", False)
    monkeypatch.setattr(server, "_REPLY_REPUBLISHED_AT", {})
    monkeypatch.setattr(server, "_in_boot_quiet", lambda: False)
    monkeypatch.setattr(server._prompt_queue, "peek_next", lambda title: None)
    inst = _Inst("t")
    monkeypatch.setattr(server.ENGINE, "instances", {"t": inst})
    monkeypatch.setattr(server, "_session_stage_cached", lambda i: {"stage": "agent"})
    name = tmux.to_mindflock_tmux_name("t")
    pane = {"screen": CODEX_WORKING, "captures": 0}

    def run_capped(argv, **kw):
        if argv[:2] == ["tmux", "has-session"]:
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        if "display-message" in argv:  # the watch's batch chain
            out = []
            for i, a in enumerate(argv):
                if a == "display-message":
                    out.append(argv[i + 2] + "\n")
                elif a == "capture-pane":
                    pane["captures"] += 1
                    out.append(pane["screen"])
            return subprocess.CompletedProcess(argv, 0, "".join(out).encode(), b"")
        if "capture-pane" in argv:
            pane["captures"] += 1
            return subprocess.CompletedProcess(argv, 0, pane["screen"].encode(), b"")
        raise AssertionError("unexpected subprocess %r" % (argv,))

    monkeypatch.setattr(proc_mod, "run_capped", run_capped)
    monkeypatch.setattr(
        server, "_pane_meta", lambda n: ("codex", time.time() - 3600, 4242, "100x30")
    )
    monkeypatch.setattr(server, "_agent_exited", lambda n, c: False)
    monkeypatch.setattr(agent_state, "_maybe_record_thread", lambda *a: None)

    def no_report(self, n):
        raise AssertionError("a parsed dialog must not wait on the CLI's report")

    events_mod.set_sessions_snapshot(
        [{"title": "t", "activity": "working", "stage": "agent", "asking": None}]
    )
    server._emit_state_changes("t", "running", "working", "agent")
    server._probe_seed("activity", inst, "working")
    yield {"bus": b, "inst": inst, "pane": pane, "name": name, "no_report": no_report}
    with server._PROBE_CACHE_LOCK:
        for p in ("activity", "asking"):
            server._PROBE_CACHE.pop((p, "t"), None)
    events_mod.set_sessions_snapshot([])
    server._EVENT_SNAPSHOT.clear()
    state_events._SPOKEN_ANNOUNCED.clear()
    _forget()


def _published(title="t"):
    return [r for r in events_mod.sessions_snapshot() if r["title"] == title][0]


def _clarify_events(got):
    return [
        e
        for e in got
        if e["event"] == "session.activity_changed" and e.get("new") == "clarify"
    ]


def test_republish_dialog_puts_clarify_and_asking_on_the_row(srv, monkeypatch):
    from backend import providers

    monkeypatch.setattr(
        type(providers.resolve("codex")), "activity_state", srv["no_report"]
    )
    got = []
    srv["bus"].subscribe(got.append)
    srv["pane"]["screen"] = CODEX_EXEC
    row = server._republish_dialog("t", CODEX_EXEC)
    assert row["activity"] == "clarify"
    assert row["asking"]["kind"] == "dialog"
    assert row["asking"]["parsed"] is True
    assert row["asking"]["dialog_id"]
    assert "run the following command" in (row["asking"]["text"] or "")
    assert _published()["activity"] == "clarify"
    assert _published()["asking"]["dialog_id"] == row["asking"]["dialog_id"]
    # One frame is a frame: the event waits for the settle…
    assert _clarify_events(got) == []
    # …or the watch's confirmation, whichever comes first.
    row = server._republish_dialog("t", CODEX_EXEC, stable=True)
    assert row["activity"] == "clarify"
    assert len(_clarify_events(got)) == 1
    assert agent_state.reading_is_authoritative("t", "clarify")


def test_republish_dialog_inside_boot_quiet_patches_the_row_silently(srv, monkeypatch):
    monkeypatch.setattr(server, "_in_boot_quiet", lambda: True)
    got = []
    srv["bus"].subscribe(got.append)
    srv["pane"]["screen"] = CODEX_EXEC
    server._republish_dialog("t", CODEX_EXEC, stable=True)
    assert _published()["activity"] == "clarify"
    assert [e for e in got if e["event"].startswith("session.")] == []


def test_republish_dialog_patches_only_activity_fields(srv):
    events_mod.set_sessions_snapshot(
        [
            {
                "title": "t",
                "activity": "working",
                "stage": "committed",
                "branch": "feature/x",
                "last_say": "Earlier words.",
                "background": [{"id": "j"}],
                "asking": None,
            }
        ]
    )
    srv["pane"]["screen"] = CODEX_EXEC
    server._republish_dialog("t", CODEX_EXEC)
    row = _published()
    assert row["activity"] == "clarify"
    assert row["stage"] == "committed" and row["branch"] == "feature/x"
    assert row["last_say"] == "Earlier words."  # reply fields untouched
    assert row["background"] == [{"id": "j"}]


def test_republish_dialog_without_a_screen_probes_in_full(srv, monkeypatch):
    seen = []
    monkeypatch.setattr(
        server, "_agent_activity", lambda i, t, **kw: seen.append(kw) or "clarify"
    )
    server._republish_dialog("t", None)
    assert seen == [{}]  # the plain probe: it reads the hook marker itself


def test_a_tick_that_read_working_before_the_dialog_neither_flaps_nor_regresses(
    srv, monkeypatch
):
    for n in ("_check_session_budget", "_prune_session_state"):
        monkeypatch.setattr(server, n, lambda *a, **k: None)
    monkeypatch.setattr(server, "_orphan_dangling_parents", lambda: None)
    monkeypatch.setattr(server._stage_reset, "prune", lambda titles: None)
    monkeypatch.setattr(server._revive, "observe", lambda *a, **k: None)
    monkeypatch.setattr(server._revive, "auto_tick", lambda s: None)
    got = []
    srv["bus"].subscribe(got.append)

    def build():
        stale = dict(_published(), activity="working", check=None)
        srv["pane"]["screen"] = CODEX_EXEC
        server._republish_dialog("t", CODEX_EXEC, stable=True)  # mid-build
        return [stale]

    monkeypatch.setattr(server, "_build_instances_snapshot", build)
    server._instances_tick()
    acts = [
        (e.get("old"), e.get("new"))
        for e in got
        if e["event"] == "session.activity_changed"
    ]
    assert acts == [("working", "clarify")]  # no clarify→working flick
    assert _published()["activity"] == "clarify"
    assert _published()["asking"]["dialog_id"]


def test_the_events_tick_skips_a_reading_the_dialog_overtook(srv, monkeypatch):
    got = []
    srv["bus"].subscribe(got.append)

    def stale_read(inst, title):
        srv["pane"]["screen"] = CODEX_EXEC
        server._republish_dialog("t", CODEX_EXEC, stable=True)
        return "working"

    monkeypatch.setattr(server, "_agent_activity_cached", stale_read)
    server._tick_state_changes()
    acts = [
        (e.get("old"), e.get("new"))
        for e in got
        if e["event"] == "session.activity_changed"
    ]
    assert acts == [("working", "clarify")]


def test_agent_activity_reads_a_supplied_screen_before_the_clis_report(
    srv, monkeypatch
):
    from backend import providers

    monkeypatch.setattr(
        type(providers.resolve("codex")), "activity_state", srv["no_report"]
    )
    inst = srv["inst"]
    assert agent_state._agent_activity(inst, "t", screen=CODEX_EXEC) == "clarify"
    assert not agent_state.reading_is_authoritative("t", "clarify")
    assert srv["pane"]["captures"] == 0  # the supplied screen, not a new one
    assert (
        agent_state._agent_activity(inst, "t", screen=CODEX_EXEC, screen_stable=True)
        == "clarify"
    )
    assert agent_state.reading_is_authoritative("t", "clarify")
    # The idle consumers never see the new source.
    assert not agent_state.reading_is_authoritative("t", "idle")


# ---- the loop, end to end: fake pane → row within the budget ------------------- #
def test_loop_publishes_a_codex_approval_within_the_budget(srv, monkeypatch):
    monkeypatch.setattr(server, "_DIALOG_WATCH_LOOP_ENABLED", True)
    monkeypatch.setattr(server, "_dialog_watch_load", lambda: 0.0)
    got = []
    srv["bus"].subscribe(lambda e: got.append((time.monotonic(), e)))

    async def run():
        task = asyncio.create_task(server._dialog_watch_loop())
        await asyncio.sleep(0.5)  # a few quiet polls of the working pane
        assert _published()["activity"] == "working"
        drawn = time.monotonic()
        srv["pane"]["screen"] = CODEX_EXEC  # Codex draws its approval box
        row_at = None
        end = drawn + 3.0
        while time.monotonic() < end:
            r = _published()
            if row_at is None and r["activity"] == "clarify":
                row_at = time.monotonic()
                assert r["asking"]["dialog_id"]
            if row_at and _clarify_events([e for _, e in got]):
                break
            await asyncio.sleep(0.01)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return drawn, row_at

    drawn, row_at = asyncio.run(run())
    assert row_at is not None, "the row never turned clarify"
    ev = [t for t, e in got if e.get("new") == "clarify"]
    assert ev, "no clarify event"
    print(
        "dialog drawn -> row ms",
        round((row_at - drawn) * 1000),
        "-> event ms",
        round((ev[0] - drawn) * 1000),
    )
    # ≤ 0.3s + a capture by design (0.3s screen cadence, 0.1s loop); the
    # event ~0.2s later (confirm). Generous bounds: full-suite load stalls
    # the event loop (cf. the reply-watch smoke test).
    assert row_at - drawn < 1.5
    assert ev[0] - drawn < 2.0


def test_loop_never_captures_an_idle_session(srv, monkeypatch):
    monkeypatch.setattr(server, "_DIALOG_WATCH_LOOP_ENABLED", True)
    monkeypatch.setattr(server, "_DIALOG_WATCH_INTERVAL", 0.02)
    events_mod.set_sessions_snapshot(
        [{"title": "t", "activity": "idle", "stage": "agent", "asking": None}]
    )
    srv["pane"]["screen"] = CODEX_EXEC

    async def run():
        task = asyncio.create_task(server._dialog_watch_loop())
        await asyncio.sleep(0.4)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(run())
    assert srv["pane"]["captures"] == 0
    assert _published()["activity"] == "idle"


def test_dialog_watch_rows_skip_idle_remote_and_gone_sessions(srv, monkeypatch):
    monkeypatch.setattr(
        server.ENGINE,
        "instances",
        {"t": srv["inst"], "i": _Inst("i"), "c": _Inst("c")},
    )
    events_mod.set_sessions_snapshot(
        [
            {"title": "t", "activity": "working"},
            {"title": "i", "activity": "idle"},
            {"title": "c", "activity": "clarify"},
            {"title": "dev::r", "activity": "working"},
            {"title": "ghost", "activity": "working"},
        ]
    )
    rows = server._dialog_watch_rows()
    assert [(r.title, r.activity, r.parses) for r in rows] == [
        ("t", "working", True),
        ("c", "clarify", True),
    ]
    assert rows[0].name == tmux.to_mindflock_tmux_name("t")


def test_dialog_watch_load_reads_critical_memory_as_maximal(monkeypatch):
    from backend import memory_pressure

    monkeypatch.setattr(memory_pressure, "level", lambda: "critical")
    assert server._dialog_watch_load() >= 1e6
    monkeypatch.setattr(memory_pressure, "level", lambda: "ok")
    assert 0.0 <= server._dialog_watch_load() < 1e6


def test_dialog_watch_loop_is_off_under_pytest_by_default(monkeypatch):
    monkeypatch.setattr(server, "_DIALOG_WATCH_LOOP_ENABLED", None)
    assert server._dialog_watch_loop_enabled() is False

    def _touched():
        raise AssertionError("the real snapshot was read")

    monkeypatch.setattr(server, "_dialog_watch_rows", _touched)
    assert asyncio.run(asyncio.wait_for(server._dialog_watch_loop(), 1.0)) is None
    monkeypatch.setattr(server._restart, "_under_pytest", lambda: False)
    assert server._dialog_watch_loop_enabled() is True  # on in a real server


def test_dialog_watch_loop_is_registered_by_the_lifespan():
    assert "_register_task(_dialog_watch_loop())" in inspect.getsource(server.lifespan)


def test_paused_session_is_offline_even_with_a_screen(srv):
    srv["inst"].Status = session_mod.Paused
    assert agent_state._agent_activity(srv["inst"], "t", screen=CODEX_EXEC) == "offline"


# ---- DialogWatch: timing, cadence and bookkeeping ------------------------------ #
CLAUDE_BASH = (DATA / "claude_bash.screen.txt").read_text()
CLAUDE_SUBAGENT = (DATA / "claude2_bash_subagent.screen.txt").read_text()
CLAUDE_WORKING = (
    "● Reading the search limiter.\n\n✻ Thinking… (3s · esc to interrupt)\n"
)


def test_confirmation_needs_confirm_s_of_stillness_then_fires_once():
    pane = _Pane(mf_t=CODEX_EXEC)
    w = _watch(pane, urgent_s=0.01, confirm_s=0.15)
    assert w.poll([_row()], now=0.0) == [("t", CODEX_EXEC, False)]
    assert w.poll([_row(activity="clarify")], now=0.1) == []  # 0.1 < confirm_s
    assert w.poll([_row(activity="clarify")], now=0.15) == [("t", CODEX_EXEC, True)]
    for i in range(1, 10):
        assert w.poll([_row(activity="clarify")], now=0.15 + i * 0.05) == []


def test_an_unconfirmed_dialog_is_recaptured_at_the_urgent_pace():
    w = _watch(
        _Pane(mf_t=CODEX_EXEC),
        screen_s=0.3,
        clarify_s=1.0,
        urgent_s=0.1,
        confirm_s=0.15,
    )
    w.poll([_row()], now=1.0)  # sighting: unconfirmed
    assert w._state["t"].due == pytest.approx(1.1)
    w.poll([_row(activity="clarify")], now=1.2)  # confirmed
    assert w._state["t"].due == pytest.approx(2.2)  # back to clarify_s
    w2 = _watch(_Pane(mf_t=CODEX_WORKING), screen_s=0.3, urgent_s=0.1)
    w2.poll([_row()], now=1.0)
    assert w2._state["t"].due == pytest.approx(1.3)  # no dialog: screen_s


def test_a_clarify_marker_already_there_on_first_sight_is_old_news():
    pane = _Pane(mf_t=CODEX_WORKING)
    m = _Marker()
    m.state["mf_t"] = "clarify"
    w = _watch(pane, marker_key=m.stat, marker_state=m.read, screen_s=10.0)
    w.poll([_row()], now=1.0)
    for i in range(1, 15):
        assert w.poll([_row()], now=1.0 + i * 0.1) == []
    assert m.reads == 0
    assert len(pane.calls) == 1  # never urgent: no extra captures


def test_a_marker_appearing_with_clarify_makes_the_session_urgent():
    pane = _Pane(mf_t=CODEX_WORKING)
    m = _Marker()
    del m.key["mf_t"]  # no marker yet: the key reads None
    w = _watch(pane, marker_key=m.stat, marker_state=m.read, screen_s=10.0)
    w.poll([_row()], now=1.0)
    m.write("mf_t", "clarify")
    w.poll([_row()], now=1.1)
    assert m.reads == 1
    assert len(pane.calls) == 2  # captured at once, not 10s later
    assert w._state["t"].urgent_until == pytest.approx(1.1 + w.urgent_window_s)


def test_an_urgent_window_running_out_on_a_clarify_row_hands_nothing_over():
    pane = _Pane(mf_t=CODEX_WORKING)
    m = _Marker()
    w = _watch(pane, marker_key=m.stat, marker_state=m.read, urgent_window_s=0.3)
    w.poll([_row()], now=1.0)
    m.write("mf_t", "clarify")
    w.poll([_row()], now=1.1)  # urgent from here
    got = []
    for i in range(1, 10):  # the tick published clarify meanwhile
        got += w.poll([_row(activity="clarify")], now=1.1 + i * 0.1)
    assert got == []
    assert w._state["t"].urgent_until == 0.0  # the window did close


def test_backoff_edges_and_a_raising_load():
    w = _watch(_Pane(), load=lambda: 0.75, load_soft=0.75, load_hard=2.0)
    assert w.backoff() == 1.0
    w = _watch(_Pane(), load=lambda: 2.0, load_soft=0.75, load_hard=2.0)
    assert w.backoff() == w.max_backoff
    w = _watch(_Pane(), load=lambda: 0.0)
    assert w.backoff() == 1.0

    def boom():
        raise OSError("no loadavg")

    assert _watch(_Pane(), load=boom).backoff() == 1.0
    assert _watch(_Pane()).backoff() == 1.0  # no load source at all


def test_a_zero_capture_cap_still_captures_one_session_a_poll():
    pane = _Pane(mf_a=CODEX_WORKING, mf_b=CODEX_WORKING)
    w = _watch(pane, max_captures=0)
    w.poll([_row("a"), _row("b")], now=1.0)
    assert pane.calls == [["mf_a"]]


def test_a_clarify_row_whose_dialog_changes_is_confirmed_again_not_left():
    other = CODEX_EXEC.replace("uv add redis", "uv add httpx")
    assert agent_state.dialog_fingerprint("codex", other) != (
        agent_state.dialog_fingerprint("codex", CODEX_EXEC)
    )
    pane = _Pane(mf_t=CODEX_EXEC)
    w = _watch(pane, urgent_s=0.05, confirm_s=0.15)
    rows = [_row(activity="clarify")]
    w.poll(rows, now=1.0)
    assert w.poll(rows, now=1.2) == [("t", CODEX_EXEC, True)]
    pane.screens["mf_t"] = other  # answered; the next command's approval
    got = []
    t = 2.5
    for _ in range(10):
        got += w.poll(rows, now=t)
        t += 0.05
    assert got == [("t", other, True)]  # no "left" (screen None-dialog) hand-over


def test_state_follows_the_title_through_a_rename_of_name_or_program():
    pane = _Pane(mf_t=CODEX_WORKING, mf_t2=CODEX_EXEC)
    w = _watch(pane)
    w.poll([_row()], now=1.0)
    row = DialogRow("t", "mf_t2", "working", "codex", True)
    assert w.poll([row], now=2.0) == [("t", CODEX_EXEC, False)]
    assert pane.calls[-1] == ["mf_t2"]
    # Same text under a new program: the hash is unchanged, so it is not
    # parsed again — the dialog already seen goes on to its confirmation.
    row = DialogRow("t", "mf_t2", "clarify", "claude", True)
    assert w.poll([row], now=3.0) == [("t", CODEX_EXEC, True)]
    assert list(w._state) == ["t"]


def test_counters_count_calls_captures_and_parses():
    pane = _Pane(mf_a=CODEX_WORKING, mf_b=CODEX_WORKING)
    fps = []
    w = _watch(pane, fps, screen_s=0.3)
    rows = [_row("a"), _row("b"), _row("c", program="x", parses=False)]
    w.poll(rows, now=1.0)
    assert (w.capture_calls, w.captures, w.parses) == (1, 2, 2)
    w.poll(rows, now=1.1)  # nobody due
    assert (w.capture_calls, w.captures, w.parses) == (1, 2, 2)
    w.poll(rows, now=1.3)  # both due, unchanged text: no parse
    assert (w.capture_calls, w.captures, w.parses) == (2, 4, 2)
    pane.screens["mf_a"] = CODEX_WORKING + "x\n"
    w.poll(rows, now=1.6)
    assert (w.capture_calls, w.captures, w.parses) == (3, 6, 3)
    assert len(fps) == w.parses


def test_twenty_static_working_sessions_stay_within_the_cpu_budget():
    names = ["s%02d" % i for i in range(20)]
    pane = _Pane(**{"mf_" + n: CODEX_WORKING for n in names})
    w = _watch(pane, load=lambda: 0.0)
    rows = [_row(n) for n in names]
    for i in range(100):  # 10 simulated seconds at the loop's 0.1s
        w.poll(rows, now=1.0 + i * 0.1)
    assert w.capture_calls <= 10 * 10  # ≤ one tmux exec per loop pass
    assert w.parses == 20  # each static screen parsed once, ever
    assert all(len(c) <= w.max_captures for c in pane.calls)
    # Every session is seen at least every ~screen_s × (20 / 8 per call).
    assert w.captures >= 20 * 10


# ---- DialogWatch on Claude Code -------------------------------------------------- #
def test_claude_permission_screen_is_handed_over_on_the_screen_path():
    pane = _Pane(mf_t=CLAUDE_WORKING)
    w = _watch(pane, screen_s=0.3)
    rows = [_row(program="claude")]
    assert w.poll(rows, now=1.0) == []
    pane.screens["mf_t"] = CLAUDE_BASH
    got = []
    for i in range(1, 4):
        got += w.poll(rows, now=1.0 + i * 0.1)
    assert got == [("t", CLAUDE_BASH, False)]  # within one screen_s


def test_claude_permission_hook_beats_the_screen_cadence():
    pane = _Pane(mf_t=CLAUDE_WORKING)
    m = _Marker()
    w = _watch(pane, marker_key=m.stat, marker_state=m.read, screen_s=10.0)
    rows = [_row(program="claude")]
    w.poll(rows, now=1.0)
    m.write("mf_t", "clarify")  # PermissionRequest
    pane.screens["mf_t"] = CLAUDE_BASH
    assert w.poll(rows, now=1.1) == [("t", CLAUDE_BASH, False)]


def test_a_background_subagents_dialog_is_seen_while_the_marker_says_working():
    """Claude runs sub-agents in-process: their tool events rewrite the
    marker to working (the main turn's Stop to idle) under a sub-agent's
    permission prompt. The screen path does not need the marker."""
    pane = _Pane(mf_t=CLAUDE_WORKING)
    m = _Marker()
    w = _watch(pane, marker_key=m.stat, marker_state=m.read, screen_s=0.3)
    rows = [_row(program="claude")]
    w.poll(rows, now=1.0)
    pane.screens["mf_t"] = CLAUDE_SUBAGENT
    got = []
    for i in range(1, 6):
        m.write("mf_t", "working" if i % 2 else "idle")
        got += w.poll(rows, now=1.0 + i * 0.1)
    assert got[0] == ("t", CLAUDE_SUBAGENT, False)
    assert m.reads  # the marker moved, and never said clarify


# ---- the fingerprint, harder ------------------------------------------------------ #
def test_fingerprint_ignores_the_cursor_but_not_the_command_or_labels():
    fp = agent_state.dialog_fingerprint("codex", CODEX_EXEC)
    moved = CODEX_EXEC.replace("› 1. Yes", "  1. Yes").replace(
        "  2. Yes, and", "› 2. Yes, and"
    )
    assert moved != CODEX_EXEC
    assert agent_state.dialog_fingerprint("codex", moved) == fp
    cmd = CODEX_EXEC.replace("uv add redis", "uv add httpx")
    assert agent_state.dialog_fingerprint("codex", cmd) not in (None, fp)
    label = CODEX_EXEC.replace("No, and tell Codex", "Nope, and tell Codex")
    assert agent_state.dialog_fingerprint("codex", label) not in (None, fp)
    claude = agent_state.dialog_fingerprint("claude", CLAUDE_BASH)
    assert claude and claude != fp


def test_fingerprint_is_none_when_resolve_or_the_parser_fails(monkeypatch):
    from backend import providers

    prov = type(providers.resolve("codex"))

    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(prov, "parse_dialog", boom)
    assert agent_state.dialog_fingerprint("codex", CODEX_EXEC) is None
    monkeypatch.setattr(prov, "parse_dialog", lambda self, t: {"options": []})
    assert agent_state.dialog_fingerprint("codex", CODEX_EXEC) is None
    monkeypatch.setattr(prov, "parse_dialog", lambda self, t: "not a dict")
    assert agent_state.dialog_fingerprint("codex", CODEX_EXEC) is None
    monkeypatch.setattr(agent_state.providers, "resolve", boom)
    assert agent_state.dialog_fingerprint("codex", CODEX_EXEC) is None


# ---- capture_visible_many, harder --------------------------------------------------- #
def _captured(argv):
    # The session each capture names (its target is the exact form, "=name:").
    return [
        argv[i + 3].removeprefix("=").removesuffix(":")
        for i, a in enumerate(argv)
        if a == "capture-pane"
    ]


def test_capture_many_keeps_every_non_newline_line_break(monkeypatch):
    odd = "a\rb\x0bc\x0cd\x1ce\x1df\x1eg\x85h i j\nlast"
    panes = {"a": odd + "\n", "b": "tail without newline"}
    _fake_tmux(monkeypatch, panes)
    assert agent_state.capture_visible_many(["a", "b"]) == panes


def test_capture_many_dedupes_and_drops_empty_names(monkeypatch):
    calls = _fake_tmux(monkeypatch, {"a": "A\n", "b": "B\n"})
    got = agent_state.capture_visible_many(["a", "", "a", None, "b", "b"])
    assert got == {"a": "A\n", "b": "B\n"}
    assert [_captured(c) for c in calls] == [["a", "b"]]


def test_capture_many_when_the_first_session_ended(monkeypatch):
    calls = _fake_tmux(monkeypatch, {"b": "B\n", "c": "C\n"})
    got = agent_state.capture_visible_many(["gone", "b", "c"])
    assert got == {"gone": None, "b": "B\n", "c": "C\n"}
    assert [_captured(c) for c in calls] == [["gone", "b", "c"], ["b", "c"]]


def test_capture_many_when_tmux_cannot_run_tries_once(monkeypatch):
    calls = []

    def boom(argv, **kw):
        calls.append(argv)
        raise FileNotFoundError("tmux")

    monkeypatch.setattr(proc_mod, "run_capped", boom)
    assert agent_state.capture_visible_many(["a", "b"]) == {"a": None, "b": None}
    assert len(calls) == 1


def test_capture_many_a_pane_printing_the_header_literal_stays_intact(monkeypatch):
    """The header carries a per-batch nonce: a pane that prints the fixed
    prefix (an agent quoting this file) cannot cut itself short."""
    panes = {
        "a": "before\n" + agent_state._BATCH_HEADER + "1\nafter\n",
        "b": "B\n",
    }
    _fake_tmux(monkeypatch, panes)
    assert agent_state.capture_visible_many(["a", "b"]) == panes


# ---- _agent_activity with a supplied screen ------------------------------------------ #
def _report(monkeypatch, value):
    """The CLI's own report (``provider.activity_state``) reads ``value``."""
    from backend import providers

    monkeypatch.setattr(
        type(providers.resolve("codex")), "activity_state", lambda self, n: value
    )


def test_a_supplied_screen_is_ignored_for_a_cli_without_a_parser(srv, monkeypatch):
    _report(monkeypatch, "working")
    monkeypatch.setattr(agent_state, "_reads_dialogs", lambda p: False)
    got = agent_state._agent_activity(srv["inst"], "t", screen=CODEX_EXEC)
    assert got == "working"  # no parser: a dialog on the screen is no evidence
    assert agent_state.last_screen("t", 60) != CODEX_EXEC
    assert srv["pane"]["captures"] == 0


def test_a_supplied_usage_limit_screen_is_left_to_the_limit_layers(srv, monkeypatch):
    _report(monkeypatch, "clarify")
    monkeypatch.setattr(agent_state, "is_limit_screen", lambda text: True)
    got = agent_state._agent_activity(
        srv["inst"], "t", screen=CODEX_EXEC, screen_stable=True
    )
    assert got == "limit"  # its menu parses, but limit outranks clarify
    assert not agent_state.reading_is_authoritative("t", "clarify")
    assert srv["pane"]["captures"] == 0  # the limit check read the same screen


def test_a_supplied_screen_without_a_dialog_is_noted_and_not_recaptured(
    srv, monkeypatch
):
    _report(monkeypatch, "working")
    got = agent_state._agent_activity(srv["inst"], "t", screen=CODEX_WORKING)
    assert got == "working"
    assert agent_state.last_screen("t", 60) == CODEX_WORKING
    assert srv["pane"]["captures"] == 0


def test_only_a_stable_screen_reading_is_authoritative(srv):
    inst = srv["inst"]
    agent_state._agent_activity(inst, "t", screen=CODEX_EXEC, screen_stable=True)
    assert agent_state.reading_is_authoritative("t", "clarify")
    agent_state._agent_activity(inst, "t", screen=CODEX_EXEC, screen_stable=False)
    assert not agent_state.reading_is_authoritative("t", "clarify")
    assert "screen-stable" in agent_state._AUTHORITATIVE_SOURCES
    assert "screen" not in agent_state._AUTHORITATIVE_SOURCES


# ---- _republish_dialog, harder ---------------------------------------------------------- #
def test_a_dialog_leaving_republishes_the_full_reading_and_clears_asking(
    srv, monkeypatch
):
    srv["pane"]["screen"] = CODEX_EXEC
    server._republish_dialog("t", CODEX_EXEC, stable=True)
    assert _published()["activity"] == "clarify"
    _report(monkeypatch, "working")
    srv["pane"]["screen"] = CODEX_WORKING  # answered: Codex runs the command
    row = server._republish_dialog("t", CODEX_WORKING)
    assert row["activity"] == "working"
    assert _published()["activity"] == "working"
    assert not (row["asking"] or {}).get("dialog_id")


def test_republish_dialog_drops_exactly_the_dialog_probe_memos(srv):
    inst = srv["inst"]
    for p in ("activity", "asking", "last_turn", "last_say"):
        server._probe_seed(p, inst, "STALE")
    with server._PROBE_CACHE_LOCK:
        kept = {p: server._PROBE_CACHE[(p, "t")] for p in ("last_turn", "last_say")}
    srv["pane"]["screen"] = CODEX_EXEC
    server._republish_dialog("t", CODEX_EXEC)
    with server._PROBE_CACHE_LOCK:
        for p in server._DIALOG_PROBES:
            hit = server._PROBE_CACHE.get((p, "t"))
            assert hit is None or hit[2] != "STALE", p
        for p, entry in kept.items():  # the reply's memos: the very same entry
            assert server._PROBE_CACHE.get((p, "t")) is entry, p
            server._PROBE_CACHE.pop((p, "t"), None)
    assert set(server._DIALOG_PROBES) == {"activity", "asking"}
    assert "t" in server._REPLY_REPUBLISHED_AT  # the tick race guard is armed


def test_republish_dialog_for_an_unpublished_row_republishes_in_full(srv, monkeypatch):
    events_mod.set_sessions_snapshot([])
    seen = []
    monkeypatch.setattr(
        server, "_republish_session", lambda t: seen.append(t) or {"title": t}
    )
    assert server._republish_dialog("t", CODEX_EXEC) == {"title": "t"}
    assert seen == ["t"]


def test_republish_dialog_for_a_gone_session_is_none(srv):
    assert server._republish_dialog("nope", CODEX_EXEC, stable=True) is None


def test_a_sighting_then_its_confirmation_push_needs_you_once(srv, monkeypatch):
    from backend.web.addons import notify as notify_addon
    from backend.web.core import ntfy

    S.update_settings(notifications={"ntfy_enabled": True, "ntfy_topic": "t1"})
    pushes = []
    monkeypatch.setattr(ntfy, "publish_soon", lambda cfg, **kw: pushes.append(kw))
    monkeypatch.setattr(notify_addon, "_DEDUPE_SECONDS", 0.0)  # only the dialog id
    fake = proc_mod.run_capped

    def run_capped(argv, **kw):  # the push's own /dialog read asks for text
        cp = fake(argv, **kw)
        if kw.get("text") and isinstance(cp.stdout, bytes):
            cp = subprocess.CompletedProcess(
                argv, cp.returncode, cp.stdout.decode(), ""
            )
        return cp

    monkeypatch.setattr(proc_mod, "run_capped", run_capped)
    srv["pane"]["screen"] = CODEX_EXEC
    assert notify_addon._read_dialog("t")["id"]  # the push reads THIS dialog
    addon = notify_addon.NotifyAddon()
    got = []
    srv["bus"].subscribe(got.append)
    srv["bus"].subscribe(addon._on_event)
    server._republish_dialog("t", CODEX_EXEC)  # sighting
    server._republish_dialog("t", CODEX_EXEC, stable=True)  # confirmation
    server._republish_dialog("t", CODEX_EXEC, stable=True)  # a stray repeat
    (ev,) = _clarify_events(got)
    assert len(pushes) == 1
    assert "redis" in pushes[0]["message"]  # the dialog's own words
    addon._on_event(ev)  # the same dialog announced again: still one push
    assert len(pushes) == 1


# ---- the loop's helpers ------------------------------------------------------------------ #
def test_dialog_marker_key_tracks_the_marker_file(monkeypatch, tmp_path):
    from backend.providers import activity_markers as am

    monkeypatch.setenv("MINDFLOCK_ACTIVITY_MARKER_DIR", str(tmp_path))
    assert server._dialog_marker_key("mf_t") is None
    path = am.marker_path("mf_t")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"state": "working"}')
    first = server._dialog_marker_key("mf_t")
    assert isinstance(first, tuple) and len(first) == 3
    path.write_text('{"state": "clarify", "x": 1}')
    assert server._dialog_marker_key("mf_t") != first


def test_dialog_watch_load_is_load_per_cpu_and_zero_without_one(monkeypatch):
    from backend import memory_pressure

    monkeypatch.setattr(memory_pressure, "level", lambda: "ok")
    monkeypatch.setattr(os, "getloadavg", lambda: (3.0, 2.0, 1.0))
    monkeypatch.setattr(os, "cpu_count", lambda: 4)
    assert server._dialog_watch_load() == pytest.approx(0.75)
    monkeypatch.setattr(memory_pressure, "level", lambda: "critical")
    assert server._dialog_watch_load() == 1e9

    def no_loadavg():
        raise OSError("unsupported")

    monkeypatch.setattr(memory_pressure, "level", lambda: "ok")
    monkeypatch.setattr(os, "getloadavg", no_loadavg)
    assert server._dialog_watch_load() == 0.0


def test_dialog_watch_loop_survives_raising_rows_and_republish(monkeypatch):
    monkeypatch.setattr(server, "_DIALOG_WATCH_LOOP_ENABLED", True)
    monkeypatch.setattr(server, "_DIALOG_WATCH_INTERVAL", 0.01)
    calls = {"rows": 0, "republish": 0}
    polls = []

    def rows():
        calls["rows"] += 1
        if calls["rows"] == 1:
            raise RuntimeError("snapshot exploded")
        if calls["rows"] == 2:
            return [_row()]
        return []

    class Watch:
        def poll(self, rows):
            polls.append(list(rows))
            return [("t", None, False)] if rows else []

    def republish(title, screen, stable):
        calls["republish"] += 1
        raise RuntimeError("republish exploded")

    monkeypatch.setattr(server, "_dialog_watch_rows", rows)
    monkeypatch.setattr(server, "_new_dialog_watch", Watch)
    monkeypatch.setattr(server, "_republish_dialog", republish)

    async def run():
        task = asyncio.create_task(server._dialog_watch_loop())
        await asyncio.sleep(0.3)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(run())
    assert calls["rows"] >= 4  # kept going past both explosions
    assert calls["republish"] == 1
    assert [[r.title for r in p] for p in polls if p] == [["t"]]
    assert [] in polls  # no rows: the watch is told to forget everyone


def test_dialog_watch_rows_skip_rows_whose_lookups_raise(srv, monkeypatch):
    from backend import providers

    insts = {n: _Inst(n) for n in ("t", "boom", "badname", "plain")}
    insts["boom"].Program = "boomcli"
    insts["plain"].Program = "mystery"  # the fallback provider: no parser
    monkeypatch.setattr(server.ENGINE, "instances", insts)
    events_mod.set_sessions_snapshot(
        [{"title": n, "activity": "working"} for n in insts]
    )
    real_resolve, real_name = providers.resolve, tmux.to_mindflock_tmux_name

    def resolve(program):
        if program == "boomcli":
            raise RuntimeError("bad provider")
        return real_resolve(program)

    def to_name(title):
        if title == "badname":
            raise ValueError("bad title")
        return real_name(title)

    monkeypatch.setattr(providers, "resolve", resolve)
    monkeypatch.setattr(tmux, "to_mindflock_tmux_name", to_name)
    rows = server._dialog_watch_rows()
    assert [(r.title, r.parses) for r in rows] == [("t", True), ("plain", False)]


# ---- screen-stable is a clarify-only authority ----------------------------------------- #
def test_a_screen_stable_clarify_lends_no_authority_to_an_idle(srv):
    """The queue drain's fast tier and the turn-end gate both ask
    ``reading_is_authoritative(title, "idle")``: a confirmed dialog reading
    must leave them on their slow, guarded paths."""
    from backend.web.core import queue_driver

    agent_state._agent_activity(srv["inst"], "t", screen=CODEX_EXEC, screen_stable=True)
    assert agent_state._ACTIVITY_CACHE["t"]["reading"] == ("clarify", "screen-stable")
    assert not agent_state.reading_is_authoritative("t", "idle")
    now = 1000.0
    early = {"idle_since": now - (queue_driver._QUEUE_IDLE_SETTLE_MARKER + 1)}
    assert queue_driver._idle_settled(early, "t", now) is False  # 12s tier, not 4s
    late = {"idle_since": now - (queue_driver._QUEUE_IDLE_SETTLE + 1)}
    assert queue_driver._idle_settled(late, "t", now) is True


@pytest.mark.parametrize("report", ["working", "idle"])
def test_screen_stable_on_a_screen_without_a_dialog_never_records_screen_stable(
    srv, monkeypatch, report
):
    _report(monkeypatch, report)
    got = agent_state._agent_activity(
        srv["inst"], "t", screen=CODEX_WORKING, screen_stable=True
    )
    assert got != "clarify"
    reading = agent_state._ACTIVITY_CACHE["t"]["reading"]
    assert reading[0] == got
    assert reading[1] not in ("screen", "screen-stable")
    assert not agent_state.reading_is_authoritative("t", "clarify")
    assert srv["pane"]["captures"] == 0  # the supplied screen served the pane layer


# ---- capture_visible_many, edges ------------------------------------------------------ #
def test_capture_many_when_every_session_ended_ends_in_one_pass_each(monkeypatch):
    calls = _fake_tmux(monkeypatch, {})
    got = agent_state.capture_visible_many(["a", "b", "c"])
    assert got == {"a": None, "b": None, "c": None}
    # Each failed chain drops its first session: the loop always shrinks.
    assert [_captured(c) for c in calls] == [["a", "b", "c"], ["b", "c"], ["c"]]


def test_capture_many_never_puts_a_name_through_a_tmux_format(monkeypatch):
    odd = ["has #{session_name} hash", "semi;colon", "two  spaces", "#S"]
    panes = {n: "screen of %s\n" % n for n in odd}
    calls = _fake_tmux(monkeypatch, panes)
    assert agent_state.capture_visible_many(odd) == panes
    (argv,) = calls
    headers = [argv[i + 2] for i, a in enumerate(argv) if a == "display-message"]
    assert len(headers) == len(odd)
    for idx, h in enumerate(headers):
        assert h.startswith(agent_state._BATCH_HEADER)
        assert h.rsplit(" ", 1)[1] == str(idx)  # the index, never the name
        assert "#" not in h and ";" not in h
    # Each name is one whole argv element after -t, as given.
    assert _captured(argv) == odd
    assert all(argv[argv.index("=" + n + ":") - 1] == "-t" for n in odd)


# ---- the fingerprint ignores everything but the dialog ---------------------------------- #
def test_fingerprint_ignores_the_screen_around_the_dialog_but_not_its_question():
    fp = agent_state.dialog_fingerprint("codex", CODEX_EXEC)
    assert fp
    spinner = "◦ Working (3s • esc to interrupt)\n" + CODEX_EXEC
    preamble = CODEX_EXEC.replace(
        "I need the redis client before wiring", "Let me add the redis client before"
    )
    assert agent_state.dialog_fingerprint("codex", spinner) == fp
    assert agent_state.dialog_fingerprint("codex", preamble) == fp
    asked = CODEX_EXEC.replace(
        "Would you like to run the following command?",
        "Would you like to run this command?",
    )
    assert agent_state.dialog_fingerprint("codex", asked) not in (None, fp)


# ---- the announce path: one clarify event per dialog ------------------------------------ #
def test_a_parked_sighting_settled_by_the_tick_is_not_announced_again_on_confirm(
    srv, monkeypatch
):
    monkeypatch.setattr(server, "_ACTIVITY_SETTLE_SECONDS", 0.0)
    got = []
    srv["bus"].subscribe(got.append)
    srv["pane"]["screen"] = CODEX_EXEC
    server._republish_dialog("t", CODEX_EXEC)  # a lone frame: parked
    assert _published()["activity"] == "clarify"
    assert _clarify_events(got) == []
    server._emit_state_changes("t", "running", "clarify", "agent")  # the tick
    assert len(_clarify_events(got)) == 1
    server._republish_dialog("t", CODEX_EXEC, stable=True)  # the confirmation
    assert len(_clarify_events(got)) == 1


# ---- DialogWatch: urgency never silences the screen ------------------------------------- #
def test_a_dialog_drawn_after_the_urgent_window_ran_out_is_still_handed_over():
    pane = _Pane(mf_t=CODEX_WORKING)
    m = _Marker()
    w = _watch(
        pane,
        marker_key=m.stat,
        marker_state=m.read,
        urgent_window_s=0.3,
        screen_s=0.3,
        confirm_s=0.15,
    )
    w.poll([_row()], now=1.0)
    m.write("mf_t", "clarify")
    got = []
    t = 1.0
    for _ in range(10):  # the hook said clarify; no dialog ever drew
        t = round(t + 0.1, 2)
        got += w.poll([_row()], now=t)
    assert [(g[0], g[2]) for g in got] == [("t", False)]  # the window's hand-over
    assert w._state["t"].urgent_until == 0.0
    pane.screens["mf_t"] = CODEX_EXEC  # drawn late
    late = []
    for _ in range(5):
        t = round(t + 0.1, 2)
        late += w.poll([_row()], now=t)
    assert late[0] == ("t", CODEX_EXEC, False)  # within one screen_s
    assert ("t", CODEX_EXEC, True) in late


def test_urgent_rows_jump_the_capture_cap():
    """The cap serves hook-urgent rows first (they wait on a redraw now),
    then the longest-waiting: overdue cadence rows never hold them back."""
    names = ["s%d" % i for i in range(6)]
    pane = _Pane(**{"mf_" + n: CODEX_WORKING for n in names})
    m = _Marker()
    m.key, m.state = {}, {}
    w = _watch(
        pane, marker_key=m.stat, marker_state=m.read, max_captures=2, screen_s=0.3
    )
    rows = [_row(n) for n in names]
    for t in (1.0, 1.1, 1.2):
        w.poll(rows, now=t)
    m.write("mf_s4", "clarify")
    m.write("mf_s5", "clarify")
    before = len(pane.calls)
    w.poll(rows, now=2.0)  # everyone overdue
    assert sorted(pane.calls[before]) == ["mf_s4", "mf_s5"]  # the first poll
    assert all(len(c) <= 2 for c in pane.calls)


# ---- the loop: several hand-overs in one poll ------------------------------------------- #
def test_dialog_watch_loop_republishes_each_hand_over_once_in_order(monkeypatch):
    monkeypatch.setattr(server, "_DIALOG_WATCH_LOOP_ENABLED", True)
    monkeypatch.setattr(server, "_DIALOG_WATCH_INTERVAL", 0.01)
    handed = [("a", CODEX_EXEC, False), ("b", None, False), ("c", CODEX_EXEC, True)]
    polls = {"n": 0}

    class Watch:
        def poll(self, rows):
            polls["n"] += 1
            return list(handed) if polls["n"] == 1 else []

    calls = []
    monkeypatch.setattr(server, "_dialog_watch_rows", lambda: [_row("a")])
    monkeypatch.setattr(server, "_new_dialog_watch", Watch)
    monkeypatch.setattr(
        server, "_republish_dialog", lambda *a: calls.append(a) or {"title": a[0]}
    )

    async def run():
        task = asyncio.create_task(server._dialog_watch_loop())
        await asyncio.sleep(0.2)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(run())
    assert polls["n"] >= 3
    assert calls == handed


# ---- _dialog_watch_rows: event-loop cost ------------------------------------------------ #
def test_dialog_watch_rows_resolves_a_provider_only_for_watched_rows(srv, monkeypatch):
    """It runs ON the event loop every 0.1s: idle rows cost a dict lookup,
    nothing more."""
    from backend import providers

    insts = {"s%02d" % i: _Inst("s%02d" % i) for i in range(50)}
    monkeypatch.setattr(server.ENGINE, "instances", insts)
    watched = {"s03", "s17", "s42"}
    events_mod.set_sessions_snapshot(
        [
            {
                "title": n,
                "activity": (
                    "working" if n in watched else ("clarify" if n == "s44" else "idle")
                ),
            }
            for n in insts
        ]
    )
    resolved = []
    real = providers.resolve
    monkeypatch.setattr(providers, "resolve", lambda p: resolved.append(p) or real(p))
    t0 = time.perf_counter()
    for _ in range(10):
        rows = server._dialog_watch_rows()
    spent = (time.perf_counter() - t0) / 10
    assert sorted(r.title for r in rows) == sorted(watched | {"s44"})
    assert len(resolved) == 10 * 4  # one per watched row per call
    assert spent < 0.05, spent  # generous: ms in practice


# ---- the loop's marker helpers follow the marker dir ------------------------------------ #
def test_dialog_marker_state_reads_the_marker_dir_override(monkeypatch, tmp_path):
    from backend.providers import activity_markers as am

    here, there = tmp_path / "here", tmp_path / "there"
    monkeypatch.setenv("MINDFLOCK_ACTIVITY_MARKER_DIR", str(here))
    path = am.marker_path("mf_t")
    assert path.parent == here
    path.parent.mkdir(parents=True)
    path.write_text('{"state": "clarify", "ts": %f}' % time.time())
    assert server._dialog_marker_state("mf_t") == "clarify"
    assert server._dialog_marker_key("mf_t") is not None
    monkeypatch.setenv("MINDFLOCK_ACTIVITY_MARKER_DIR", str(there))
    assert server._dialog_marker_state("mf_t") is None  # not misread elsewhere
    assert server._dialog_marker_key("mf_t") is None


# ---- the loop, end to end: the dialog leaving --------------------------------------------- #
def test_loop_republishes_working_and_clears_asking_when_the_dialog_is_answered(
    srv, monkeypatch
):
    monkeypatch.setattr(server, "_DIALOG_WATCH_LOOP_ENABLED", True)
    monkeypatch.setattr(server, "_dialog_watch_load", lambda: 0.0)
    got = []
    srv["bus"].subscribe(got.append)

    async def wait_for(cond, limit=5.0):
        end = time.monotonic() + limit
        while time.monotonic() < end:
            if cond():
                return True
            await asyncio.sleep(0.01)
        return False

    async def run():
        task = asyncio.create_task(server._dialog_watch_loop())
        try:
            await asyncio.sleep(0.2)
            srv["pane"]["screen"] = CODEX_EXEC
            shown = await wait_for(
                lambda: _published()["activity"] == "clarify" and _clarify_events(got)
            )
            asking_then = dict(_published()["asking"] or {})
            _report(monkeypatch, "working")
            srv["pane"]["screen"] = CODEX_WORKING  # answered
            left = await wait_for(lambda: _published()["activity"] == "working")
            return shown, asking_then, left
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    shown, asking_then, left = asyncio.run(run())
    assert shown, "the dialog never reached the row and the bus"
    assert asking_then.get("dialog_id") and asking_then.get("parsed") is True
    assert left, "the answered dialog never left the row"
    assert not (_published()["asking"] or {}).get("dialog_id")
    acts = [
        (e.get("old"), e.get("new"))
        for e in got
        if e["event"] == "session.activity_changed"
    ]
    assert acts == [("working", "clarify"), ("clarify", "working")]


# ---- property: the hand-over discipline under any sequence ----------------------------- #
_P_SCREENS = {"plain": "• Working\n", "A": "dialog A\n", "B": "dialog B\n"}
_P_FP = {"• Working\n": None, "dialog A\n": "fpA", "dialog B\n": "fpB"}


@hyp.settings(max_examples=300, deadline=None)
@hyp.given(
    st.lists(
        st.tuples(
            st.sampled_from(["working", "working", "clarify", "idle"]),
            st.sampled_from(sorted(_P_SCREENS)),
            st.sampled_from([0.05, 0.1, 0.1, 0.2, 0.35, 1.2]),
        ),
        min_size=1,
        max_size=60,
    )
)
def test_property_each_dialog_is_handed_over_once_unconfirmed_and_once_confirmed(seq):
    """For any sequence of published activity, screen and poll spacing: a run
    of one dialog across consecutive captures yields ≤ 1 confirmed hand-over
    and ≤ 1 unconfirmed one — more only to a row that reads ``working``
    again, ≥ ``clarify_s`` after the last (a tick that flipped it back); a
    confirmed one always carries a dialog; a
    dialog-less one (the dialog leaving) only follows, on a clarify row, a
    capture that showed a dialog since the last leaving (or forgetting)."""
    pane = {"screen": _P_SCREENS["plain"]}
    captured = []

    def capture(names):
        captured.append(pane["screen"])
        return {n: pane["screen"] for n in names}

    w = DialogWatch(capture, lambda program, text: _P_FP[text])
    run_id, run_fp = 0, object()
    counts = {}
    last_unconfirmed = {}
    dialog_since_leave = False
    now = 1.0
    for activity, screen, dt in seq:
        now += dt
        pane["screen"] = _P_SCREENS[screen]
        if activity == "idle":  # the row leaves the watch: state forgotten
            run_id, run_fp = run_id + 1, object()
            dialog_since_leave = False
        before = len(captured)
        out = w.poll([DialogRow("t", "mf_t", activity, "codex", True)], now=now)
        if len(captured) > before:
            fp = _P_FP[captured[-1]]
            if fp != run_fp:
                run_id, run_fp = run_id + 1, fp
            dialog_since_leave = dialog_since_leave or fp is not None
        for title, handed, stable in out:
            assert title == "t" and handed == captured[-1]  # this poll's capture
            fp = _P_FP[handed]
            if fp is None:
                assert not stable and activity == "clarify" and dialog_since_leave
                dialog_since_leave = False
                continue
            key = (run_id, stable)
            counts[key] = counts.get(key, 0) + 1
            if not stable and counts[key] > 1:
                assert activity == "working", key
                assert now - last_unconfirmed[run_id] >= w.clarify_s, key
            else:
                assert counts[key] == 1, key
            if not stable:
                last_unconfirmed[run_id] = now


def test_dialog_watch_rows_skip_a_paused_session(srv):
    srv["inst"].Status = session_mod.Paused
    events_mod.set_sessions_snapshot([{"title": "t", "activity": "working"}])
    assert server._dialog_watch_rows() == []  # its row lags the pause a tick


def test_a_row_flipped_back_to_working_under_its_dialog_is_handed_it_again():
    pane = _Pane(mf_t=CODEX_EXEC)
    w = _watch(pane, clarify_s=1.0, screen_s=0.3)
    assert w.poll([_row()], now=1.0) == [("t", CODEX_EXEC, False)]
    w.poll([_row(activity="clarify")], now=1.2)  # confirmed
    # A tick that disagreed rewrote the row to working; the dialog is up.
    got = []
    t = 1.3
    while t < 3.65:
        got += [(x, round(t, 2)) for x in w.poll([_row()], now=t)]
        t += 0.1
    assert [g[0] for g in got] == [("t", CODEX_EXEC, False)] * 2
    assert got[0][1] >= 2.0 and got[1][1] - got[0][1] >= 1.0  # ≤ 1 per clarify_s
