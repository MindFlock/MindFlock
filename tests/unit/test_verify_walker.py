"""The WALKER half of Verify as a walkthrough.

A run has a ``walk`` mode: ``"agent"`` (the default, today's behaviour — only
actor=agent steps are the checker's) or ``"all"`` (the checker also attempts a
person's steps with its tools, and may settle one only with stated evidence).
These pin: ``agent_steps``' walk, ``start_run`` / ``_normalize_run`` keeping
``run.walk``, the evidence rule in ``finish_run`` / ``record_progress`` (stored
``by="agent", walked=True``), ``run_progress`` counting a walk-all run's
handovers, the run prompt's walkthrough shape (Do / Expect / Needs, the
walk-all rules, the ``prepared`` checkout line), the run route's ``walk`` body,
and a who:"agent" Verify stop starting its run with walk "all".

Isolation: the root conftest points ``$MINDFLOCK_TEST_PLANS_FILE`` and
``$MINDFLOCK_SETTINGS_FILE`` at tmp; the route tests pin the run route's probes
and capture session creation instead of performing it.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from backend.web.core import flight_driver as D
from backend.web.core import flights as F
from backend.web.core import test_plans as T

NOW = 1_800_000_000.0
EVIDENCE = "posted /sbx-ask via the Slack API; the bot replied 'ok' in 2 s"


def _plan(plan_id="sc-1", **over) -> dict:
    plan = {
        "id": plan_id,
        "title": plan_id,
        "repo_root": "/repo",
        "branch": "feature/%s" % plan_id,
        "sha": "a" * 40,
        "tip_sha": "b" * 40,
        "live_branch": "main",
        "state": "due",
        "live_at": 1.0,
        "generated_at": 100.0,
        "steps": [
            {"id": "s1", "text": "Call the API", "expect": "200", "actor": "agent"},
            {
                "id": "s2",
                "text": "Type /sbx-ask in the sandbox Slack channel",
                "expect": "the bot answers",
                "actor": "human",
            },
        ],
        "runs": [],
        "run_session": "",
    }
    plan.update(over)
    return plan


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDFLOCK_TEST_PLANS_FILE", str(tmp_path / "tp.json"))
    T.upsert(_plan())
    return "sc-1"


# --------------------------------------------------------------------------- #
# agent_steps: the human exclusion is the default; walk "all" opts in
# --------------------------------------------------------------------------- #
def test_agent_steps_default_excludes_human_and_walk_all_returns_every_step():
    plan = _plan()
    assert [s["id"] for s in T.agent_steps(plan)] == ["s1"]
    assert [s["id"] for s in T.agent_steps(plan, walk="agent")] == ["s1"]
    assert [s["id"] for s in T.agent_steps(plan, walk="all")] == ["s1", "s2"]
    assert [s["id"] for s in T.agent_steps(plan, ["s2"], walk="all")] == ["s2"]
    assert T.agent_steps(plan, ["s2"]) == []
    # Anything unrecognised is the default.
    assert [s["id"] for s in T.agent_steps(plan, walk="bogus")] == ["s1"]


# --------------------------------------------------------------------------- #
# The run record keeps its walk
# --------------------------------------------------------------------------- #
def test_start_run_stamps_walk_and_the_store_keeps_it(store):
    T.start_run(store, "verify-sc-1", walk="all")
    assert T.get(store)["runs"][-1]["walk"] == "all"
    T.cancel_run(store)
    T.start_run(store, "verify-sc-1")
    assert T.get(store)["runs"][-1]["walk"] == "agent"


def test_normalize_run_coerces_walk():
    assert T._normalize_run({}, [])["walk"] == "agent"
    assert T._normalize_run({"walk": "ALL"}, [])["walk"] == "all"
    assert T._normalize_run({"walk": "everything"}, [])["walk"] == "agent"


def test_walked_survives_normalisation_and_is_absent_otherwise():
    cell = T._normalize_result({"result": "pass", "note": "x", "walked": True})
    assert cell["walked"] is True and cell["by"] == "agent"
    assert "walked" not in T._normalize_result({"result": "pass"})


# --------------------------------------------------------------------------- #
# finish_run: the evidence rule
# --------------------------------------------------------------------------- #
def test_walk_all_accepts_an_evidenced_answer_to_a_person_step(store):
    T.start_run(store, "verify-sc-1", walk="all")
    p = T.finish_run(
        store,
        [
            {"id": "s1", "result": "pass", "note": "200 OK"},
            {"id": "s2", "result": "pass", "note": EVIDENCE},
        ],
    )
    cell = p["runs"][-1]["results"]["s2"]
    assert cell["result"] == "pass" and cell["by"] == "agent"
    assert cell["walked"] is True and cell["note"] == EVIDENCE
    assert "walked" not in p["runs"][-1]["results"]["s1"]
    assert p["runs"][-1]["verdict"] == "pass"
    assert T.is_settled(p) and T.open_human_steps(p) == []
    assert p["state"] == "done"


def test_walk_all_accepts_an_evidenced_fail(store):
    T.start_run(store, "verify-sc-1", walk="all")
    p = T.finish_run(store, [{"id": "s2", "result": "fail", "note": "no reply"}])
    cell = p["runs"][-1]["results"]["s2"]
    assert cell["result"] == "fail" and cell["walked"] is True


@pytest.mark.parametrize("note", ["", "   "])
def test_walk_all_refuses_a_claim_without_evidence(store, note):
    T.start_run(store, "verify-sc-1", walk="all")
    p = T.finish_run(store, [{"id": "s2", "result": "pass", "note": note}])
    cell = p["runs"][-1]["results"]["s2"]
    assert cell["result"] == "blocked" and "walked" not in cell
    assert "without saying what it observed" in cell["note"]
    assert [s["id"] for s in T.open_human_steps(p)] == ["s2"]


def test_walk_all_needs_you_parks_on_the_person(store):
    T.start_run(store, "verify-sc-1", walk="all")
    p = T.finish_run(
        store,
        [
            {"id": "s1", "result": "pass", "note": "ok"},
            {"id": "s2", "result": "blocked", "note": "needs you: sign in to Slack"},
        ],
    )
    cell = p["runs"][-1]["results"]["s2"]
    assert cell["result"] == "blocked" and "walked" not in cell
    assert not T.is_settled(p)
    prog = T.run_progress(p)
    assert prog["total"] == 2 and prog["answered"] == 2
    assert [n["step"] for n in prog["needs_you"]] == ["s2"]
    assert prog["needs_you"][0]["do"] == "sign in to Slack"


def test_a_pass_with_a_needs_you_note_is_not_evidence(store):
    T.start_run(store, "verify-sc-1", walk="all")
    p = T.finish_run(
        store, [{"id": "s2", "result": "pass", "note": "needs you: look at it"}]
    )
    assert p["runs"][-1]["results"]["s2"]["result"] == "blocked"


def test_walk_agent_keeps_todays_refusal_even_with_evidence(store):
    T.start_run(store, "verify-sc-1")
    p = T.finish_run(store, [{"id": "s2", "result": "pass", "note": EVIDENCE}])
    cell = p["runs"][-1]["results"]["s2"]
    assert cell["result"] == "blocked" and "walked" not in cell
    assert cell["note"] == EVIDENCE
    assert T.run_progress(p)["total"] == 1


def test_a_person_answer_still_wins_over_a_walked_one(store):
    T.start_run(store, "verify-sc-1", walk="all")
    T.record_result(store, "s2", "fail", "the bot was silent", by="human")
    p = T.finish_run(store, [{"id": "s2", "result": "pass", "note": EVIDENCE}])
    cell = p["runs"][-1]["results"]["s2"]
    assert cell["result"] == "fail" and cell["by"] == "human"


def test_a_walked_pass_on_the_wrong_tree_is_downgraded_and_unflagged(store):
    T.start_run(store, "verify-sc-1", walk="all")
    p = T.finish_run(
        store,
        [{"id": "s2", "result": "pass", "note": EVIDENCE}],
        tested_sha="c" * 40,
        expected_sha="b" * 40,
    )
    cell = p["runs"][-1]["results"]["s2"]
    assert cell["result"] == "blocked" and "walked" not in cell


# --------------------------------------------------------------------------- #
# record_progress: same rule mid-run
# --------------------------------------------------------------------------- #
def test_record_progress_applies_the_same_rule(store):
    T.start_run(store, "verify-sc-1", walk="all")
    p = T.record_progress(store, [{"id": "s2", "result": "pass", "note": EVIDENCE}])
    cell = p["runs"][-1]["results"]["s2"]
    assert cell["result"] == "pass" and cell["walked"] is True
    assert p["state"] == "running"
    stamp = p["runs"][-1]["progress_at"]
    again = T.record_progress(store, [{"id": "s2", "result": "pass", "note": EVIDENCE}])
    assert again["runs"][-1]["progress_at"] == stamp
    p = T.record_progress(store, [{"id": "s2", "result": "pass", "note": ""}])
    cell = p["runs"][-1]["results"]["s2"]
    assert cell["result"] == "blocked" and "walked" not in cell


def test_record_progress_walk_agent_refuses(store):
    T.start_run(store, "verify-sc-1")
    p = T.record_progress(store, [{"id": "s2", "result": "pass", "note": EVIDENCE}])
    assert p["runs"][-1]["results"]["s2"]["result"] == "blocked"


# --------------------------------------------------------------------------- #
# The run prompt
# --------------------------------------------------------------------------- #
def _walkthrough():
    return _plan(
        steps=[
            {"id": "s1", "text": "Post /sbx-ask", "expect": "", "actor": "agent"},
            {
                "id": "s2",
                "text": "Search Loki for the ask",
                "expect": "one log line with the ask id",
                "actor": "agent",
                "needs": ["s1"],
            },
            {"id": "s3", "text": "Look at the reply", "expect": "", "actor": "human"},
        ]
    )


def test_prompt_presents_steps_in_order_with_expect_and_needs():
    prompt = T.build_run_prompt(_walkthrough())
    s1, s2, s3 = (prompt.index(x) for x in ("s1 [YOURS]", "s2 [YOURS]", "s3 [human]"))
    assert s1 < s2 < s3
    assert "    Expect: one log line with the ask id" in prompt
    assert "    Needs: s1" in prompt
    assert "WALK THE STEPS IN ORDER" in prompt
    assert '"needs sN"' in prompt
    assert "never wait for someone else to have done it" in prompt
    # Default walk: the person's steps are not the checker's.
    assert "Steps marked [human] or [skip] are NOT yours" in prompt
    assert "[human, try it]" not in prompt


def test_walk_all_prompt_hands_over_person_steps_with_the_evidence_rule():
    prompt = T.build_run_prompt(_walkthrough(), walk="all")
    assert "s3 [human, try it] Look at the reply" in prompt
    assert "browser-automation" in prompt and "Slack" in prompt
    assert "ONLY with a note stating your evidence" in prompt
    assert '"needs you: "' in prompt
    assert "Steps marked [human] or [skip] are NOT yours" not in prompt


def test_prepared_is_off_by_default_and_changes_only_the_checkout_line():
    plan = _walkthrough()
    base = T.build_run_prompt(plan, live=False)
    assert base == T.build_run_prompt(plan, live=False, prepared=False)
    assert "git fetch --all && git checkout --detach %s" % ("b" * 40) in base
    prepared = T.build_run_prompt(plan, live=False, prepared=True)
    assert "you are already at %s in this directory" % ("b" * 40) in prepared
    assert "`git rev-parse HEAD`" in prepared
    assert "git checkout --detach" not in prepared
    assert 'mark every step "blocked"' in prepared
    # Everything after step 1 is the same text.
    tail = base[base.index("\n\n2. ") :]
    assert prepared.endswith(tail)


def test_prepared_on_live_names_the_ref_to_compare():
    prompt = T.build_run_prompt(_walkthrough(), live=True, prepared=True)
    assert "you are already at origin/main" in prompt
    assert "`git rev-parse origin/main`" in prompt


def test_prepared_on_an_environment_keeps_read_only_and_carries_on():
    prompt = T.build_run_prompt(
        _walkthrough(),
        where="sandbox",
        target="https://sb.example.com",
        checkout_branch="sandbox",
        prepared=True,
    )
    assert "you are already at origin/sandbox" in prompt
    assert "ONLY TO READ CODE" in prompt
    assert "carry on against the environment" in prompt


# --------------------------------------------------------------------------- #
# POST /api/test-plans/{id}/run {"walk": ...}
# --------------------------------------------------------------------------- #
@pytest.fixture
def client(monkeypatch, tmp_path):
    from backend.web import server

    monkeypatch.setenv("MINDFLOCK_TEST_PLANS_FILE", str(tmp_path / "tp.json"))
    monkeypatch.setattr(server, "git_available", lambda: True)
    monkeypatch.setattr(server, "_is_verify_repo_usable", lambda root: True)
    # The server prepares the checker's checkout before the agent starts
    # (real repos: tests/unit/test_verify_checkout.py); these repos are fake.
    monkeypatch.setattr(
        server,
        "_prepare_verify_checkout",
        lambda repo, title, ref: {
            "ok": True,
            "path": repo,
            "sha": ref,
            "reused": False,
            "warning": "",
        },
    )
    monkeypatch.setattr(server, "_kill_orphan_plan_tmux", lambda t: "")
    monkeypatch.setattr(server, "_free_stale_verify_worktree", lambda r, t: "")
    monkeypatch.setattr(server, "_clear_verify_results", lambda t: None)
    monkeypatch.setattr(server.ENGINE, "instances", {})
    made: list = []

    async def _create(payload, request=None):
        made.append(payload)
        return JSONResponse({"ok": True}, status_code=202)

    monkeypatch.setattr(server, "create_instance", _create)
    c = TestClient(server.app)
    c.made = made
    return c


def _human_only():
    return _plan(
        steps=[{"id": "s1", "text": "Look at it", "expect": "", "actor": "human"}]
    )


def test_route_rejects_an_unknown_walk(client):
    T.upsert(_plan())
    for bad in ("both", 3, ["all"]):
        r = client.post("/api/test-plans/sc-1/run", json={"walk": bad})
        assert r.status_code == 400, r.text
        assert "walk" in r.json()["error"]
    assert client.made == []


def test_route_default_walk_is_agent_and_still_refuses_human_only(client):
    T.upsert(_human_only())
    r = client.post("/api/test-plans/sc-1/run", json={})
    assert r.status_code == 409 and r.json()["reason"] == "human_only"
    T.upsert(_plan())
    r = client.post("/api/test-plans/sc-1/run", json={})
    assert r.status_code == 202, r.text
    assert T.get("sc-1")["runs"][-1]["walk"] == "agent"
    assert "[human, try it]" not in client.made[0]["prompt"]


def test_route_walk_all_runs_a_human_only_plan(client):
    T.upsert(_human_only())
    r = client.post("/api/test-plans/sc-1/run", json={"walk": "all"})
    assert r.status_code == 202, r.text
    assert T.get("sc-1")["runs"][-1]["walk"] == "all"
    assert "s1 [human, try it] Look at it" in client.made[0]["prompt"]


def test_route_walk_all_may_name_a_person_step(client):
    T.upsert(_plan())
    r = client.post("/api/test-plans/sc-1/run", json={"steps": ["s2"]})
    assert r.status_code == 400 and r.json()["human_steps"] == ["s2"]
    r = client.post("/api/test-plans/sc-1/run", json={"steps": ["s2"], "walk": "all"})
    assert r.status_code == 202, r.text
    prompt = client.made[0]["prompt"]
    assert "s2 [human, try it]" in prompt and "s1 [skip]" in prompt


# --------------------------------------------------------------------------- #
# The Verify stop: who "agent" walks everything
# --------------------------------------------------------------------------- #
def test_verify_walk_by_who():
    assert D._verify_walk({"who": "agent"}) == "all"
    assert D._verify_walk({"who": "both"}) == "agent"
    assert D._verify_walk({"who": "you"}) == "agent"
    assert D._verify_walk({}) == "agent"
    assert D._verify_walk(None) == "agent"


@pytest.fixture
def flights_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDFLOCK_FLIGHTS_DIR", str(tmp_path / "flights"))


def _flight(who, session="s"):
    plan = F.save_plan(
        {"name": "P", "steps": [{"kind": "verify", "config": {"who": who}}]},
        now=NOW,
    )
    f = F.take_off(plan, session=session, now=NOW)
    a = F.next_action(f, {"session": {"present": True, "status": "running"}}, NOW)
    assert a["action"] == "run", a
    return F.apply(f, a, NOW)


def test_agent_only_stop_sees_person_steps_as_the_agents(flights_dir, monkeypatch):
    from backend.web.core import test_plans as _tp

    plan = dict(_human_only(), state="generated")
    monkeypatch.setattr(_tp, "get", lambda pid: plan)
    agent = D._verify_obs(_flight("agent"), {"detail": {"plan": "sc-1"}}, {})
    assert agent["open_agent"] == ["Look at it"]
    both = D._verify_obs(_flight("both", "t"), {"detail": {"plan": "sc-1"}}, {})
    assert both["open_agent"] == []
    # Once walked with evidence, nothing is open and the stop can be done.
    plan["runs"] = [
        {
            "at": NOW,
            "walk": "all",
            "session": "",
            "results": {
                "s1": {
                    "result": "pass",
                    "note": EVIDENCE,
                    "by": "agent",
                    "walked": True,
                }
            },
        }
    ]
    agent = D._verify_obs(_flight("agent", "u"), {"detail": {"plan": "sc-1"}}, {})
    assert agent["open_agent"] == [] and agent["settled"] is True


def test_agent_only_stop_starts_its_run_with_walk_all(flights_dir, monkeypatch):
    ran = []

    async def fake_run(pid, req, payload):
        ran.append((pid, payload))
        return SimpleNamespace(status_code=202, body=b"{}")

    monkeypatch.setattr(D, "_server", lambda: SimpleNamespace(run_test_plan=fake_run))
    action = {"action": "run", "phase": "agent_run", "detail": {"plan": "sc-1"}}
    asyncio.run(D._perform(_flight("agent"), action, NOW))
    asyncio.run(D._perform(_flight("both", "t"), action, NOW))
    assert [p["walk"] for _, p in ran] == ["all", "agent"]
