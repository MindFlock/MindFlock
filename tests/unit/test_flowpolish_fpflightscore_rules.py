"""Flow-polish round, worker fp-flights-core: a route does exactly what its
author wrote, and every waiting or failed stop says why and offers an exit
that works (``backend.web.core.flights`` + the add-on kinds' configs)."""

import pytest

from backend.web.core import flight_kinds as K
from backend.web.core import flights as F

NOW = 1_800_000_000.0

K.load()


@pytest.fixture(autouse=True)
def _store(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDFLOCK_FLIGHTS_DIR", str(tmp_path / "flights"))
    yield tmp_path / "flights"


def step(kind, sid=None, on_fail="halt", **config):
    out = {"kind": kind, "config": config, "on_fail": on_fail}
    if sid:
        out["id"] = sid
    return out


def sess(**kw):
    base = {"present": True, "status": "running", "activity": "idle"}
    base.update(kw)
    return base


def fly(*steps, session="s", **kw):
    plan = F.save_plan({"name": "P", "steps": list(steps)}, now=NOW)
    return F.take_off(plan, session=session, now=NOW, **kw)


def run_step(f, now=NOW, obs=None):
    a = F.next_action(f, obs or {"session": sess()}, now)
    assert a["action"] == "run", a
    return F.apply(f, a, now)


def _verify_facts(**kw):
    base = {
        "plan": "p",
        "branch": "b",
        "state": "generated",
        "steps": ["s1", "s2"],
        "settled": False,
        "open_human": [],
        "open_agent": [],
        "failed": [],
        "live": False,
        "live_branch": "main",
        "latest_run_at": 0.0,
        "progress": {},
        "checker": {},
        "run_budget_s": 1800.0,
    }
    base.update(kw)
    return base


def _vnext(f, facts, at):
    return F.next_action(f, {"session": sess(), "verify": facts}, at)


def _verify_started(**cfg):
    f = run_step(fly(step("verify", **cfg)))
    a = _vnext(f, _verify_facts(), NOW + 1)
    assert a["action"] == "run" and a["phase"] == "agent_run"
    return F.apply(f, dict(a, detail=dict(a["detail"], run_started=True)), NOW + 1)


# --------------------------------------------------------------------------- #
# 1. A blocked Verify park offers a Retry that works
# --------------------------------------------------------------------------- #
def test_retry_works_on_a_verify_parked_blocked():
    f = _verify_started(who="agent")
    out = _vnext(
        f,
        _verify_facts(
            latest_run_at=NOW + 2, outcome="blocked", outcome_note="no browser"
        ),
        NOW + 60,
    )
    assert out["action"] == "you" and out["detail"]["ask"] == "blocked"
    assert "Retry" in out["detail"]["items"][0]
    f = F.apply(f, out, NOW + 60)
    assert f["state"] == "waiting_you"
    F.op_retry(f, "s1", now=NOW + 70)
    assert f["state"] == "running"
    assert f["step_states"]["s1"]["state"] == "pending"


def test_the_never_written_needs_you_ask_is_gone():
    src = open(F.__file__, encoding="utf-8").read()
    assert '"needs_you", "memory"' not in src
    assert '("needs_you", "blocked"' not in src


# --------------------------------------------------------------------------- #
# 2. A parked stop keeps its reason
# --------------------------------------------------------------------------- #
def test_a_parked_stop_keeps_its_reason_and_the_bell_says_it():
    f = _verify_started(who="both")
    facts = _verify_facts(
        latest_run_at=NOW + 2,
        outcome="needs_you",
        confirm_items=["log in on a phone", "log out"],
    )
    out = _vnext(f, facts, NOW + 60)
    f = F.apply(f, out, NOW + 60)
    assert f["step_states"]["s1"]["note"] == "2 steps for you to check, then confirm"
    later = F.next_action(f, {"session": sess(), "verify": facts}, NOW + 65)
    assert later == {
        "action": "wait",
        "note": "2 steps for you to check, then confirm",
    }
    f = F.apply(f, later, NOW + 65)
    text, items = F.you_text(f)
    assert "2 steps for you to check" in text
    assert items == ["log in on a phone", "log out"]


# --------------------------------------------------------------------------- #
# 3. Options are never silently changed (strict); stored plans still load
# --------------------------------------------------------------------------- #
def test_unknown_option_is_refused_naming_the_stop_and_what_it_has():
    with pytest.raises(F.PlanError) as err:
        F.normalize_step({"kind": "checks", "config": {"timeout": 5}}, 2)
    assert err.value.message == (
        "unknown option 'timeout' on a CI checks stop — it has: timeout_minutes"
    )
    assert err.value.field == "steps[2].config.timeout"
    # Lenient (a stored plan): dropped as before.
    s = F.normalize_step({"kind": "checks", "config": {"timeout": 5}}, 0, strict=False)
    assert s["config"] == {"timeout_minutes": 60}


def test_legacy_spellings_still_read_strictly():
    assert (
        F.normalize_step({"kind": "wait", "config": {"minutes": 2}})["config"][
            "seconds"
        ]
        == 120
    )
    v = F.normalize_step({"kind": "verify", "config": {"wait_live": True}})
    assert v["config"]["where"] == "live"
    loop = F.normalize_step(
        {"kind": "loop", "config": {"every_minutes": 2, "times": 2}}, strict=True
    )
    assert loop["config"]["every_seconds"] == 120


@pytest.mark.parametrize(
    "kind, config, words",
    [
        (
            "split",
            {"prompts": "a", "count": 50},
            "count on a Split stop must be at most 20 (got 50)",
        ),
        ("checks", {"timeout_minutes": 0}, "must be at least 1 (got 0)"),
        ("checks", {"timeout_minutes": "soon"}, "must be a whole number (got 'soon')"),
        (
            "agent",
            {"prompt": "x", "fresh": "maybe"},
            "fresh on an Agent stop is true or false",
        ),
        ("ship", {"lane": "deploy"}, "a ship step goes to commit, push, pr or merge"),
        ("join", {"of": "some"}, "of is all or spawned"),
        ("wait", {"seconds": 10**9}, "seconds on a Wait stop is 0 to 1209600"),
    ],
)
def test_strict_refuses_what_it_used_to_change(kind, config, words):
    with pytest.raises(F.PlanError) as err:
        F.normalize_step({"kind": kind, "config": config})
    assert words in err.value.message
    # A stored plan keeps loading (coerced as it always was).
    assert F.normalize_step({"kind": kind, "config": config}, strict=False)


def test_true_false_words_and_numbers_still_read():
    s = F.normalize_step(
        {"kind": "agent", "config": {"prompt": "x", "fresh": "yes", "join": "off"}}
    )
    assert s["config"]["fresh"] is True and s["config"]["join"] is False
    s = F.normalize_step({"kind": "split", "config": {"prompts": "a", "count": "3"}})
    assert s["config"]["count"] == 3


def test_a_saved_route_refuses_a_bad_option():
    with pytest.raises(F.PlanError) as err:
        F.save_plan(
            {
                "name": "P",
                "steps": [step("agent", prompt="x"), step("checks", timeout=9)],
            },
            now=NOW,
        )
    assert err.value.field == "steps[1].config.timeout"


@pytest.mark.parametrize(
    "expect, status, ok",
    [
        ("2", 204, True),
        ("2xx", 200, True),
        ("2xx", 302, False),
        ("20x", 201, True),
        ("201", 201, True),
        ("201", 200, False),
        ("200-299", 250, True),
        ("200-299", 300, False),
    ],
)
def test_call_expect_spellings(expect, status, ok):
    from backend.web.core.flight_kinds import call as C

    cfg = F.normalize_step(
        {"kind": "call", "config": {"url": "https://x.dev", "expect": expect}}
    )["config"]
    assert cfg["expect"] == expect
    assert C.expect_matches(cfg["expect"], status) is ok


def test_call_expect_refuses_nonsense_strictly_and_reads_2_leniently():
    with pytest.raises(F.PlanError) as err:
        F.normalize_step(
            {"kind": "call", "config": {"url": "https://x.dev", "expect": "ok"}}
        )
    assert "2xx" in err.value.message and err.value.field.endswith(".expect")
    s = F.normalize_step(
        {"kind": "call", "config": {"url": "https://x.dev", "expect": "ok"}},
        strict=False,
    )
    assert s["config"]["expect"] == "2"


def test_budget_refuses_an_unreadable_limit_and_reads_dollars():
    with pytest.raises(F.PlanError) as err:
        F.normalize_step({"kind": "budget", "config": {"max_usd": "five"}})
    assert err.value.field.endswith(".max_usd")
    with pytest.raises(F.PlanError):
        F.normalize_step({"kind": "budget", "config": {"max_minutes": "an hour"}})
    s = F.normalize_step({"kind": "budget", "config": {"max_usd": "$2.50"}})
    assert s["config"]["max_usd"] == 2.5


def test_ask_someone_needs_who_or_a_webhook():
    with pytest.raises(F.PlanError) as err:
        F.normalize_step({"kind": "someone", "config": {"text": "look"}})
    assert err.value.field.endswith(".who")
    assert F.normalize_step(
        {"kind": "someone", "config": {"webhook": "https://hooks.example/x"}}
    )


def test_an_unreadable_stored_when_halts_instead_of_running():
    f = fly(step("notify", text="hi"), step("notify", text="again"))
    f["steps"][0]["when"] = "nonsense ==="
    a = F.next_action(f, {"session": sess(), "facts": {}}, NOW)
    assert a["action"] == "fail" and a["then"] == "halt"
    assert a["reason"] == "this stop's 'Only when' can't be read — edit or remove it"
    f = F.apply(f, a, NOW)
    assert f["state"] == "halted"
    # The fix: edit the condition away on the failed stop, then Retry.
    F.op_edit_step(f, "s1", {"when": ""}, now=NOW + 1)
    F.op_retry(f, "s1", now=NOW + 2)
    assert F.next_action(f, {"session": sess()}, NOW + 3)["action"] == "run"


# --------------------------------------------------------------------------- #
# 4. Edit the current stop where it matters
# --------------------------------------------------------------------------- #
def test_edit_the_current_stop_at_a_halt_point():
    f = fly(dict(step("command", command="make"), halt=True))
    a = F.next_action(f, {"session": sess()}, NOW)
    f = F.apply(f, a, NOW)
    assert f["state"] == "paused" and f["reason"] == F.HALT_POINT
    F.op_edit_step(f, "s1", {"config": {"command": "make test"}}, now=NOW + 1)
    assert f["steps"][0]["config"]["command"] == "make test"
    ss = f["step_states"]["s1"]
    assert ss["state"] == "pending" and ss["detail"] == {"halt_hit": True}
    F.op_resume(f, now=NOW + 2)
    assert F.next_action(f, {"session": sess()}, NOW + 3)["action"] == "run"


def test_edit_a_failed_stop_then_retry_runs_the_new_options():
    f = run_step(fly(step("command", command="false")))
    a = F.next_action(
        f, {"session": sess(), "command": {"exit": 1, "output": "boom"}}, NOW + 1
    )
    f = F.apply(f, a, NOW + 1)
    assert f["state"] == "halted"
    F.op_edit_step(
        f, "s1", {"config": {"command": "true"}, "title": "Run it"}, now=NOW + 2
    )
    assert f["step_states"]["s1"]["state"] == "pending"
    assert f["history"][-1]["event"] == "edited"
    assert "next Retry" in f["history"][-1]["note"]
    F.op_retry(f, "s1", now=NOW + 3)
    a = F.next_action(f, {"session": sess()}, NOW + 4)
    assert a["action"] == "run"


def test_edit_a_failure_handed_to_the_person_halts_it_for_retry():
    f = run_step(fly(step("command", on_fail="you", command="false")))
    a = F.next_action(
        f, {"session": sess(), "command": {"exit": 1, "output": "boom"}}, NOW + 1
    )
    f = F.apply(f, a, NOW + 1)
    assert f["state"] == "waiting_you"
    F.op_edit_step(f, "s1", {"config": {"command": "true"}}, now=NOW + 2)
    assert f["state"] == "halted" and "press Retry" in f["reason"]
    F.op_retry(f, "s1", now=NOW + 3)
    assert f["state"] == "running"


def test_a_running_stop_still_refuses_an_edit():
    f = run_step(fly(step("command", command="sleep 9")))
    with pytest.raises(F.FlightError) as err:
        F.op_edit_step(f, "s1", {"config": {"command": "true"}}, now=NOW + 1)
    assert err.value.error == "in_progress"


# --------------------------------------------------------------------------- #
# 5. Verify stop judging
# --------------------------------------------------------------------------- #
def test_verify_unreadable_check_parks_blocked_at_once():
    f = run_step(fly(step("verify")))
    out = F.next_action(
        f, {"session": sess(), "verify": {"error": "disk full"}}, NOW + 5
    )
    assert out["action"] == "you" and out["detail"]["ask"] == "blocked"
    assert out["note"] == (
        "the check couldn't be read: disk full — Retry to verify again, or Skip"
    )
    f = F.apply(f, out, NOW + 5)
    F.op_retry(f, "s1", now=NOW + 6)
    assert f["state"] == "running"


def test_verify_record_gone_fails_with_retry():
    f = run_step(fly(step("verify")))
    out = F.next_action(
        f, {"session": sess(), "verify": {"plan_gone": "tp_1"}}, NOW + 5
    )
    assert out["action"] == "fail"
    assert out["reason"] == "the check's record is gone — Retry starts a new one"


def test_verify_empty_obs_still_waits_for_the_check():
    f = run_step(fly(step("verify")))
    out = F.next_action(f, {"session": sess(), "verify": {}}, NOW + 5)
    assert out == {"action": "wait", "note": "getting the check ready"}


def test_a_released_blocked_run_parks_instead_of_never_started():
    f = _verify_started(who="agent")
    out = _vnext(
        f,
        _verify_facts(
            latest_run_at=NOW - 500,
            outcome="blocked",
            outcome_note="the agent's window closed before it finished",
        ),
        NOW + F.VERIFY_START_CHECK_S + 5,
    )
    assert out["action"] == "you" and out["detail"]["ask"] == "blocked"
    assert "window closed" in out["note"]


def test_failed_reason_carries_the_evidence():
    f = _verify_started(who="agent")
    out = _vnext(
        f,
        _verify_facts(
            latest_run_at=NOW + 2,
            outcome="failed",
            failed=["login works", "logout works"],
            failed_notes=["POST /login answered 500 " + "x" * 300, ""],
        ),
        NOW + 60,
    )
    assert out["action"] == "fail"
    r = out["reason"]
    assert r.startswith("Failed · 2: login works — saw: POST /login answered 500")
    assert "; logout works" in r and len(r) < 260
    # Dict items work too.
    assert F._verify_failed_reason([{"text": "a", "note": "b"}]) == (
        "Failed · 1: a — saw: b"
    )


def test_a_doubled_not_checked_is_said_once():
    f = _verify_started(who="agent")
    out = _vnext(
        f,
        _verify_facts(
            latest_run_at=NOW + 2,
            outcome="blocked",
            outcome_note="not checked: the agent stopped answering",
        ),
        NOW + 60,
    )
    assert out["note"] == "the check couldn't finish: the agent stopped answering"


def test_verify_who_labels_and_summary():
    spec = F.STEP_KINDS["verify"]
    who = next(f for f in spec["fields"] if f["name"] == "who")
    assert who["option_labels"]["you"] == "Agent, then my sign-off"
    assert who["option_labels"]["agent"].startswith("Agent only — it attempts")
    assert "behavior" not in spec["summary"]


# --------------------------------------------------------------------------- #
# 6. CI checks that can't be read
# --------------------------------------------------------------------------- #
def test_unreadable_checks_fail_after_five_minutes_saying_why():
    f = run_step(fly(step("checks", timeout_minutes=60)))
    obs = {
        "session": sess(),
        "checks": {"verdict": "unknown", "why": "no GitHub token"},
    }
    a = F.next_action(f, obs, NOW + 10)
    assert a["action"] == "wait" and "no GitHub token" in a["note"]
    assert a["detail"]["unknown_since"] == NOW + 10
    f = F.apply(f, a, NOW + 10)
    assert F.next_action(f, obs, NOW + 200)["action"] == "wait"
    out = F.next_action(f, obs, NOW + 10 + F.CHECKS_UNKNOWN_FAIL_S)
    assert out["action"] == "fail"
    assert out["reason"].startswith("can't read the CI checks: no GitHub token")


def test_checks_readable_again_resets_the_unknown_clock():
    f = run_step(fly(step("checks", timeout_minutes=60)))
    unknown = {"session": sess(), "checks": {"verdict": "unknown"}}
    f = F.apply(f, F.next_action(f, unknown, NOW + 10), NOW + 10)
    pending = {"session": sess(), "checks": {"verdict": "pending"}}
    f = F.apply(f, F.next_action(f, pending, NOW + 100), NOW + 100)
    assert "unknown_since" not in f["step_states"]["s1"]["detail"]
    a = F.next_action(f, unknown, NOW + 400)
    assert a["action"] == "wait"  # missing why tolerated, clock restarted
    assert "no answer" in a["note"]


def test_pending_checks_keep_the_timeout_message():
    f = run_step(fly(step("checks", timeout_minutes=5)))
    out = F.next_action(
        f, {"session": sess(), "checks": {"verdict": "pending"}}, NOW + 301
    )
    assert out["action"] == "fail"
    assert out["reason"].startswith("checks still not finished after 5 min")


# --------------------------------------------------------------------------- #
# 7. Ad-hoc ids follow the author
# --------------------------------------------------------------------------- #
def test_adhoc_loop_back_to_follows_its_target_and_id_map_fills():
    ids: dict = {}
    plan = F.adhoc_plan(
        [
            step("agent", sid="build", prompt="build it"),
            step("command", sid="test", command="make test"),
            step("loop", sid="again", back_to="build", times=2),
        ],
        id_map=ids,
    )
    assert [s["id"] for s in plan["steps"]] == ["s1", "s2", "s3"]
    assert plan["steps"][2]["config"]["back_to"] == "s1"
    assert ids == {"build": "s1", "test": "s2", "again": "s3"}


def test_adhoc_after_a_saved_route_never_aims_at_its_stops():
    base = F.save_plan(
        {"name": "B", "steps": [step("agent", prompt="x"), step("notify")]}, now=NOW
    )
    ids: dict = {}
    plan = F.adhoc_plan(
        [step("command", sid="s1", command="make"), step("loop", back_to="s1")],
        base=base,
        id_map=ids,
    )
    assert [s["id"] for s in plan["steps"]] == ["s1", "s2", "s3", "s4"]
    assert plan["steps"][3]["config"]["back_to"] == "s3"
    assert ids == {"s1": "s3"}


# --------------------------------------------------------------------------- #
# 8. Attaching sends the author's prompts
# --------------------------------------------------------------------------- #
def test_attach_sends_agent_stops_with_prompts():
    f = fly(
        step("agent", prompt="add tests"),
        step("agent", prompt="add docs"),
        step("ship", lane="commit"),
        attach=True,
    )
    assert f["cursor"] == 0
    assert F.next_action(f, {"session": sess()}, NOW)["action"] == "run"


def test_attach_skips_only_prompt_less_leading_agent_stops_one_line_each():
    f = fly(
        step("agent"),
        step("agent", prompt="  "),
        step("agent", prompt="add docs"),
        step("ship", lane="commit"),
        attach=True,
    )
    assert f["cursor"] == 2
    skipped = [h for h in f["history"] if h["event"] == "skipped"]
    assert [h["step"] for h in skipped] == ["s1", "s2"]
    assert all("no prompt to send" in h["note"] for h in skipped)


# --------------------------------------------------------------------------- #
# 9. Wording
# --------------------------------------------------------------------------- #
def test_one_duration_style():
    assert F._mins(23 * 3600 + 5 * 60 + 30) == "23 h 5 min"
    assert F._mins(12 * 60 + 59) == "12 min"
    assert F._mins(45) == "45 s"


def test_ship_busy_and_turned_off_say_what_to_do():
    assert F._BUSY_NOTE == (
        "this session's own Fast-track is already shipping it — stop that or wait"
    )
    f = run_step(fly(step("ship", lane="push")))
    out = F.next_action(f, {"session": sess(), "autopilot": None}, NOW + 5)
    assert out["action"] == "fail"
    assert "Goes as far as" not in out["reason"] and "Retry" in out["reason"]
