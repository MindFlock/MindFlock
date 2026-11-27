"""One model spent, not the plan: step the session down a model
(:mod:`backend.web.core.model_downshift`, ``usage_limits.detect_model_limit``)."""

from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend import providers
from backend.providers import usage_limits
from backend.providers.claude import ClaudeProvider
from backend.session import Status
from backend.web.core import model_downshift as md
from backend.web.core import queue_driver

NOW = 1_795_600_000.0  # 2026-11-25

LADDER = ("fable", "opus", "sonnet", "haiku")

#: What Claude Code printed on the Mac when Fable ran out (2026-11-24).
FABLE_SCREEN = """\
❯ what is the idea behind the 3 minute length
  cap??
  ⎿  4 skills available
  ⎿  You've hit your individual spend limit ·
     run /usage-credits to ask your admin for a
     higher limit · your Fable limit resets
     Nov 30 at 11pm (America/New_York)

❯ continue
  ⎿  You've hit your individual spend limit ·
     run /usage-credits to ask your admin for a
     higher limit · your Fable limit resets
     Nov 30 at 11pm (America/New_York)
     /usage-credits to request more usage from
     your admin.

───────────────────────────────────────────────
❯
───────────────────────────────────────────────
  ⏵⏵ bypass permissions on
"""


# --------------------------------------------------------------------------- #
# detection
# --------------------------------------------------------------------------- #
def test_the_fable_notice_steps_to_opus_with_its_reset():
    got = usage_limits.detect_model_limit(FABLE_SCREEN, LADDER, now=NOW)
    assert got["model"] == "fable" and got["next"] == "opus"
    assert got["reset_at"] is not None and got["reset_at"] > NOW


@pytest.mark.parametrize(
    "text, model, nxt",
    [
        ("Opus limit reached ∙ resets 3am", "opus", "sonnet"),
        ("You've reached your Sonnet usage limit · resets in 2h", "sonnet", "haiku"),
        ("your Haiku limit resets in 3 hours", "haiku", None),
        ("your Opus 5.5 limit resets Dec 1 at 9am", "opus", "sonnet"),
    ],
)
def test_a_named_model_limit_names_the_next_one_down(text, model, nxt):
    got = usage_limits.detect_model_limit(text, LADDER, now=NOW)
    assert (got["model"], got["next"]) == (model, nxt)


@pytest.mark.parametrize(
    "text",
    [
        "Weekly limit reached ∙ resets Jul 24 at 10:59am (America/New_York)",
        "5-hour limit reached ∙ resets 3am",
        "You've hit your usage limit · resets 3pm",
        "your weekly limit resets Monday",
        "Claude usage limit reached",
        "the API rate limit reached its cap in the test",  # an agent's prose
        "",
    ],
)
def test_an_account_wide_limit_or_prose_is_not_a_model_limit(text):
    assert usage_limits.detect_model_limit(text, LADDER, now=NOW) is None


@pytest.mark.parametrize(
    "after",
    [
        "❯ /model opus\n  ⎿  Set model to Opus 5.5\n",
        "⏺ The 3-minute cap keeps each episode short.\n",
        "● Sure.\n",
    ],
)
def test_a_reply_or_a_switch_after_it_means_it_is_history(after):
    assert usage_limits.detect_model_limit(FABLE_SCREEN + after, LADDER) is None


def test_no_ladder_never_steps():
    assert usage_limits.detect_model_limit(FABLE_SCREEN, ()) is None


def test_claude_has_the_ladder_and_types_model_aliases():
    p = ClaudeProvider()
    assert tuple(p.model_ladder()) == LADDER
    assert p.model_switch_command("opus") == "/model opus"
    assert p.model_switch_command("gpt-5") is None
    assert p.model_limit(FABLE_SCREEN, NOW)["next"] == "opus"


def test_a_provider_without_a_ladder_never_reads_one():
    from backend.providers.base import BaseProvider

    class _P(BaseProvider):
        name = "x"

    assert _P().model_limit(FABLE_SCREEN) is None
    assert _P().model_switch_command("opus") is None


def test_a_toml_provider_opts_in_with_a_ladder_and_a_switch():
    from backend.providers.config import ProviderConfig
    from backend.providers.generic import GenericProvider

    cfg = ProviderConfig(
        name="toy",
        program_aliases=("toy",),
        usage_model_ladder=("big", "small"),
        usage_model_switch="/model {model}",
    )
    p = GenericProvider(cfg)
    assert p.model_switch_command("small") == "/model small"
    got = p.model_limit("you've hit your Big limit · resets 3pm")
    assert (got["model"], got["next"]) == ("big", "small")


# --------------------------------------------------------------------------- #
# the sweep
# --------------------------------------------------------------------------- #
class _Inst:
    def __init__(self, title="w", program="claude", status=Status.Ready):
        self.Title = title
        self.Program = program
        self.Status = status

    def Started(self):
        return True


class _Srv(SimpleNamespace):
    def __init__(self, *insts):
        super().__init__()
        self.ENGINE = SimpleNamespace(
            instances={i.Title: i for i in insts}, lock=threading.RLock()
        )
        self._HUMAN_INPUT_AT = {}
        self.sent = []

    def _live_session_name(self, name):
        return name

    def _send_to_agent(self, name, text, submit=True):
        self.sent.append((name, text))
        return True


def _row(title="w", **kw):
    r = {"title": title, "activity": "idle", "sleeping": None}
    r.update(kw)
    return r


@pytest.fixture(autouse=True)
def _quiet(monkeypatch, tmp_path):
    # Never the real ~/.claude/settings.json.
    default_file = str(tmp_path / "claude-settings.json")
    monkeypatch.setattr(
        ClaudeProvider, "model_default_file", lambda self, inst=None: default_file
    )
    md._RESTORE.clear()
    # Never the real ~/.mindflock/model-spent.json either.
    from backend.providers import model_spent

    monkeypatch.setattr(
        model_spent, "_path", lambda: str(tmp_path / "model-spent.json")
    )
    monkeypatch.setattr(md, "SWITCH_SETTLE_S", 0.0)
    monkeypatch.setattr(md, "enabled", lambda: True)
    monkeypatch.setattr(md, "_capture", lambda name: FABLE_SCREEN)
    monkeypatch.setattr(queue_driver, "_dialog_on_screen", lambda inst, name: False)
    from backend.web.core import sleep

    monkeypatch.setattr(sleep, "hold_lease", lambda name, ttl, now=None: True)
    md._SWITCHED.clear()
    yield
    md._SWITCHED.clear()
    md._RESTORE.clear()


def _tick(monkeypatch, srv, rows, emitted=None):
    from backend.web.core import events

    monkeypatch.setattr(events, "sessions_snapshot", lambda: rows)
    monkeypatch.setattr(
        events.BUS,
        "emit",
        lambda kind, **kw: (
            emitted.append((kind, kw)) if emitted is not None else None
        ),
    )
    return md.tick(srv, now=NOW)


def test_an_idle_session_out_of_fable_switches_to_opus_and_carries_on(monkeypatch):
    srv = _Srv(_Inst())
    emitted = []
    assert _tick(monkeypatch, srv, [_row()], emitted) == ["w"]
    assert srv.sent[0] == ("mindflock_w", "/model opus")
    name, text = srv.sent[1]
    assert "Fable" in text and "Opus" in text and "Carry on" in text
    ((kind, kw),) = emitted
    assert kind == md.EVENT and kw["session"] == "w"
    assert kw["data"]["from"] == "fable" and kw["data"]["to"] == "opus"
    assert "from Fable to Opus" in kw["data"]["detail"]


def test_it_switches_once_per_model_until_the_cooldown_passes(monkeypatch):
    srv = _Srv(_Inst())
    _tick(monkeypatch, srv, [_row()])
    assert len(srv.sent) == 2
    _tick(monkeypatch, srv, [_row()])  # the switch did not take: no loop
    assert len(srv.sent) == 2
    md._SWITCHED["w"] = ("fable", NOW - md.COOLDOWN_S - 1)
    _tick(monkeypatch, srv, [_row()])  # retried after the cooldown
    assert len(srv.sent) == 4


@pytest.mark.parametrize(
    "row",
    [
        _row(activity="working"),
        _row(activity="clarify"),
        _row(sleeping={"since": 1}),
        _row(device="mac"),
    ],
)
def test_a_session_not_idle_here_is_left_alone(monkeypatch, row):
    srv = _Srv(_Inst())
    assert _tick(monkeypatch, srv, [row]) == []
    assert srv.sent == []


def test_a_person_typing_or_a_dialog_holds_it(monkeypatch):
    srv = _Srv(_Inst())
    srv._HUMAN_INPUT_AT["w"] = NOW - 5
    assert _tick(monkeypatch, srv, [_row()]) == []
    srv._HUMAN_INPUT_AT["w"] = NOW - 120
    monkeypatch.setattr(queue_driver, "_dialog_on_screen", lambda inst, name: True)
    assert _tick(monkeypatch, srv, [_row()]) == []
    assert srv.sent == []


def test_other_clis_paused_sessions_and_the_setting_off_do_nothing(monkeypatch):
    srv = _Srv(_Inst("a", program="aider"), _Inst("p", status=Status.Paused))
    assert _tick(monkeypatch, srv, [_row("a"), _row("p")]) == []
    srv = _Srv(_Inst())
    monkeypatch.setattr(md, "enabled", lambda: False)
    assert _tick(monkeypatch, srv, [_row()]) == []
    assert srv.sent == []


def test_an_account_wide_limit_is_left_to_the_queue(monkeypatch):
    srv = _Srv(_Inst())
    monkeypatch.setattr(
        md, "_capture", lambda name: "Weekly limit reached ∙ resets Jul 24 at 10am"
    )
    assert _tick(monkeypatch, srv, [_row()]) == []


def test_the_bottom_of_the_ladder_stays_put(monkeypatch):
    srv = _Srv(_Inst())
    monkeypatch.setattr(md, "_capture", lambda name: "your Haiku limit resets 3pm\n❯ ")
    assert _tick(monkeypatch, srv, [_row()]) == []


def test_the_setting_reads_on_when_unset(monkeypatch):
    monkeypatch.undo()
    from backend.config import settings as _settings

    for stored, on in ((None, True), (True, True), (False, False)):
        monkeypatch.setattr(
            _settings,
            "load_settings",
            lambda v=stored: SimpleNamespace(
                general=SimpleNamespace(model_downshift=v)
            ),
        )
        assert md.enabled() is on


def test_the_setting_round_trips():
    from backend.config.settings import GeneralSettings

    g = GeneralSettings.from_dict({"model_downshift": False})
    assert g.model_downshift is False
    assert g.to_dict()["model_downshift"] is False
    assert "model_downshift" not in GeneralSettings.from_dict({}).to_dict()


def test_texts_say_when_the_model_is_back():
    assert md._when(None) == ""
    t = md.resume_text("fable", "opus", None)
    assert t.startswith("MindFlock note: Fable's limit is used up, so")
    assert md.notice_text("api", "fable", "opus", None) == (
        "Switched “api” from Fable to Opus: Fable is used up"
    )


def test_providers_resolve_claude_with_the_ladder():
    assert tuple(providers.resolve("claude").model_ladder()) == LADDER


# --------------------------------------------------------------------------- #
# the person's own default stays theirs
# --------------------------------------------------------------------------- #
def _write(path, obj):
    import json

    with open(path, "w") as f:
        json.dump(obj, f)


def _read(path):
    import json

    with open(path) as f:
        return json.load(f)


def test_the_switch_puts_the_persons_default_model_back(monkeypatch, tmp_path):
    """Claude Code's ``/model opus`` also saves opus as the default for every
    new session; the person's own default (fable) comes back, other keys
    untouched."""
    path = ClaudeProvider().model_default_file()
    _write(path, {"model": "fable", "theme": "dark"})
    srv = _Srv(_Inst())

    def _cli_saves(name, text, submit=True):
        srv.sent.append((name, text))
        if text.startswith("/model "):
            _write(path, {"model": text.split()[1], "theme": "dark"})
        return True

    srv._send_to_agent = _cli_saves
    assert _tick(monkeypatch, srv, [_row()]) == ["w"]
    assert _read(path) == {"model": "fable", "theme": "dark"}
    # The CLI writes it late: the next pass inside the window fixes it too.
    _write(path, {"model": "opus", "theme": "dark"})
    assert md.restore_defaults(NOW + 10) == [path]
    assert _read(path)["model"] == "fable"
    # Past the window the watch ends: a later change is the person's own.
    md.restore_defaults(NOW + md.RESTORE_WATCH_S + 1)
    _write(path, {"model": "sonnet", "theme": "dark"})
    assert md.restore_defaults(NOW + md.RESTORE_WATCH_S + 2) == []
    assert _read(path)["model"] == "sonnet"


def test_no_default_before_means_none_after(tmp_path):
    path = str(tmp_path / "s.json")
    _write(path, {"theme": "dark"})
    md._remember_default(path, NOW)
    _write(path, {"theme": "dark", "model": "opus"})
    md.restore_defaults(NOW)
    assert _read(path) == {"theme": "dark"}


def test_a_second_switch_keeps_the_first_snapshot(tmp_path):
    path = str(tmp_path / "s.json")
    _write(path, {"model": "fable"})
    md._remember_default(path, NOW)
    _write(path, {"model": "opus"})  # the first switch saved opus
    md._remember_default(path, NOW + 5)  # opus ran out too: a second switch
    _write(path, {"model": "sonnet"})
    md.restore_defaults(NOW + 6)
    assert _read(path) == {"model": "fable"}


def test_claudes_default_file_follows_its_config_dir(monkeypatch, tmp_path):
    monkeypatch.undo()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
    assert ClaudeProvider().model_default_file() == str(
        tmp_path / "cfg" / "settings.json"
    )


# --------------------------------------------------------------------------- #
# spent models: every launch starts below them until they reset
# --------------------------------------------------------------------------- #
from backend.providers import model_spent  # noqa: E402

#: Claude Code 2.1.332 on the laptop (2026-11-26): the reset has a comma, no "at".
FABLE_COMMA_NOTICE = (
    "  ⎿  You've hit your Fable limit · resets Nov 30, 1am (America/New_York)\n"
    "\n────────\n❯ \n────────\n"
)


def test_the_comma_form_of_the_reset_is_read():
    got = ClaudeProvider().model_limit(FABLE_COMMA_NOTICE, NOW)
    assert (got["model"], got["next"]) == ("fable", "opus")
    # 2026-11-30 01:00 America/New_York
    assert got["reset_at"] == 1_796_018_400.0


def _claude():
    return ClaudeProvider()


def test_a_spent_model_starts_launches_on_the_next_one_down(tmp_path):
    p, key = _claude(), _claude().model_default_file()
    assert model_spent.launch_args(p, None, (), NOW) == ()
    model_spent.record(key, "fable", NOW + 3600, NOW)
    # No default saved: Claude Code starts on its best, Fable.
    assert model_spent.launch_args(p, None, (), NOW) == ("--model", "opus")
    # A saved default of Fable (in any spelling) too.
    Path(key).write_text('{"model": "claude-fable-5-2"}')
    assert model_spent.launch_args(p, None, (), NOW) == ("--model", "opus")
    # Opus spent as well: Sonnet.
    model_spent.record(key, "opus", NOW + 3600, NOW)
    assert model_spent.launch_args(p, None, (), NOW) == ("--model", "sonnet")


def test_a_launch_that_names_its_model_or_defaults_below_is_left_alone(tmp_path):
    p, key = _claude(), _claude().model_default_file()
    model_spent.record(key, "fable", NOW + 3600, NOW)
    assert model_spent.launch_args(p, None, ("--model", "haiku"), NOW) == ()
    assert model_spent.launch_args(p, None, ("--model=fable",), NOW) == ()
    Path(key).write_text('{"model": "opus"}')
    assert model_spent.launch_args(p, None, (), NOW) == ()


def test_a_spent_model_is_back_after_its_reset(tmp_path):
    p, key = _claude(), _claude().model_default_file()
    model_spent.record(key, "fable", NOW + 3600, NOW)
    assert model_spent.launch_args(p, None, (), NOW + 3601) == ()
    # No readable reset: it holds for a while, not forever.
    model_spent.record(key, "fable", None, NOW)
    assert model_spent.launch_args(p, None, (), NOW + 3600) == ("--model", "opus")
    assert (
        model_spent.launch_args(p, None, (), NOW + model_spent.UNKNOWN_RESET_S + 1)
        == ()
    )


def test_each_account_has_its_own_allowance(tmp_path):
    p = _claude()
    model_spent.record(
        str(tmp_path / "other" / "settings.json"), "fable", NOW + 3600, NOW
    )
    assert model_spent.launch_args(p, None, (), NOW) == ()


def test_the_sweep_records_the_spent_model_for_every_launch(monkeypatch):
    srv = _Srv(_Inst())
    assert _tick(monkeypatch, srv, [_row()]) == ["w"]
    key = _claude().model_default_file()
    # FABLE_SCREEN: "resets Nov 30 at 11pm (America/New_York)"
    assert model_spent.spent(key, NOW) == {"fable": 1_796_097_600.0}
    assert model_spent.launch_args(_claude(), None, (), NOW) == ("--model", "opus")


def test_a_session_out_of_view_still_teaches_the_launches(monkeypatch):
    # Someone typing holds the switch, but the notice is still evidence.
    srv = _Srv(_Inst())
    srv._HUMAN_INPUT_AT["w"] = NOW
    assert _tick(monkeypatch, srv, [_row()]) == []
    assert "fable" in model_spent.spent(_claude().model_default_file(), NOW)


def test_every_launch_path_gets_the_step_down(monkeypatch):
    # profile_overlay is what the first start, a relaunch, a launcher rewrite
    # and the standalone command all ask for their flags.
    from backend.providers import launch_script

    model_spent.record(_claude().model_default_file(), "fable", None)
    assert launch_script.profile_overlay("claude")[1] == ("--model", "opus")
    _, cmd = launch_script.launch_command("claude")
    assert "--model opus" in cmd
    # Other CLIs have no ladder: untouched.
    assert "--model" not in " ".join(launch_script.profile_overlay("codex")[1])


def test_local_models_keep_their_own_model(monkeypatch):
    from backend.providers import launch_script

    model_spent.record(_claude().model_default_file(), "fable", None)
    monkeypatch.setattr(
        launch_script, "local_overlay", lambda program: ({"X": "1"}, ())
    )
    assert launch_script.profile_overlay("claude")[1] == ()


#: This very session, 2026-11-27: the agent's reply quoted the notice, then
#: its next command echoed it under a tool call. Neither is the CLI's notice,
#: and the sweep switched the session anyway.
QUOTED_SCREEN = """\
● Yes, switching to Opus when Fable runs out already works. It's been on by default since 0.14.32: a session that gets "You've hit your Fable limit" is
  switched to Opus and told to carry on with the request that bounced.

● Seeing how the false positive looked on screen
  ⎿  $ tmux capture-pane -p | grep -n "hit your Fable limit"

────────
❯ 
────────
"""


@pytest.mark.parametrize(
    "screen",
    [
        QUOTED_SCREEN,
        "❯ why did I hit your Fable limit so fast?\n\n────\n❯ \n",
        "● Done.\n  ⎿  tests: your Fable limit resets Nov 30 at 1am in the fixture\n❯ \n",
    ],
    ids=["reply-and-echo", "persons-prompt", "tool-output"],
)
def test_words_about_a_limit_are_not_a_limit(screen):
    assert ClaudeProvider().model_limit(screen, NOW) is None


def test_a_quoting_session_is_not_switched(monkeypatch):
    monkeypatch.setattr(md, "_capture", lambda name: QUOTED_SCREEN)
    srv = _Srv(_Inst())
    assert _tick(monkeypatch, srv, [_row()]) == []
    assert srv.sent == []
    assert model_spent.spent(_claude().model_default_file(), NOW) == {}
