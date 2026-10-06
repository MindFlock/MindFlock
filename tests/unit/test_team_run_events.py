"""Team-run notifications: the rules the one emitter feeds.

The emitter itself (once per (run, task, reason, incarnation), never for a
dialog, silent at boot, deferred through the boot quiet window, run.finished
once across restarts) is pinned in test_team_run_driver. These pin the rule
side: which run events notify by default, and that the sentence the emitter
writes is what a push says — so every channel (ntfy, desktop, the client's
notify.js) agrees, with nothing filtered per channel.
"""

from __future__ import annotations

from backend.web.addons import notify


def _rules():
    return {r["id"]: r for r in notify.NOTIFY_RULES}


def test_the_three_run_rules_and_their_defaults():
    rules = _rules()
    assert rules["run_needs_you"]["event"] == "run.needs_you"
    assert rules["run_needs_you"]["default_enabled"] is True
    assert rules["run_finished"]["event"] == "run.finished"
    assert rules["run_finished"]["default_enabled"] is True
    # Every member's PR is ambient: the Outbox and the bell already show it.
    assert rules["run_task_shipped"]["event"] == "run.task_shipped"
    assert rules["run_task_shipped"]["default_enabled"] is False


def test_the_push_says_the_emitters_sentence():
    env = {
        "event": "run.needs_you",
        "session": "jira-PAY-421",
        "new": "ship_halted",
        "data": {"detail": "Q4 payments: PAY-421 mypy failed twice"},
    }
    rule = _rules()["run_needs_you"]
    assert notify._matches(rule, env)
    assert notify._fill(rule["body"], env) == "Q4 payments: PAY-421 mypy failed twice"
    fin = {
        "event": "run.finished",
        "session": "",
        "data": {"detail": "Q4 payments finished — 5 shipped, 1 failed"},
    }
    rule = _rules()["run_finished"]
    assert notify._matches(rule, fin)
    assert (
        notify._fill(rule["body"], fin) == "Q4 payments finished — 5 shipped, 1 failed"
    )
    # A group-level push names no session, so its title must not template one.
    assert "{session}" not in rule["title"]


def test_run_changed_never_notifies():
    assert not any(r["event"] == "run.changed" for r in notify.NOTIFY_RULES)
