"""A Verify stop's where / ready / who / notes / post-to-PR (docs/flights.md
"Verify stops"): the normaliser (legacy ``wait_live`` included), the ready
gate in :func:`flights._judge_verify`, the bodies the driver hands the
checklist plumbing, and the PR comment when the stop finishes. The plumbing
(``test_plans.environment`` / ``env_reached_at`` / ``results_markdown``) is
monkeypatched — never the real repo, server or ``~/.mindflock``."""

import asyncio
from types import SimpleNamespace

import pytest

from backend.web.core import flight_cookbook as C
from backend.web.core import flight_driver as D
from backend.web.core import flights as F
from backend.web.core import github_pr as _gh
from backend.web.core import test_plans as _tp

NOW = 1_800_000_000.0


@pytest.fixture(autouse=True)
def _store(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDFLOCK_FLIGHTS_DIR", str(tmp_path / "flights"))
    monkeypatch.setenv("MINDFLOCK_AUTOPILOT_FILE", str(tmp_path / "autopilot.json"))
    monkeypatch.setattr(D, "_JOBS", {})
    monkeypatch.setattr(D, "_REVIEWS", {})
    monkeypatch.setattr(D, "_CHECKS", {})
    yield


def vcfg(**config):
    return F.normalize_step({"kind": "verify", "config": config})["config"]


def fly(*steps, now=NOW, session="s"):
    plan = F.save_plan({"name": "P", "steps": list(steps)}, now=now)
    return F.take_off(plan, session=session, now=now)


def vstep(**config):
    return {"kind": "verify", "config": config}


def sess(**kw):
    base = {"present": True, "status": "running", "activity": "idle"}
    base.update(kw)
    return base


def started(f, now=NOW):
    a = F.next_action(f, {"session": sess()}, now)
    assert a["action"] == "run", a
    return F.apply(f, a, now)


def facts(**kw):
    base = {
        "plan": "tp1",
        "branch": "feat",
        "state": "generated",
        "steps": ["a", "b"],
        "settled": False,
        "open_human": ["you look"],
        "open_agent": ["curl it"],
        "failed": [],
        "live": False,
        "live_branch": "main",
        "env": None,
        "env_reached_at": 0.0,
    }
    base.update(kw)
    return base


#: A self-verify that settled after the stop began (``latest_run_at`` far
#: ahead of the driver's real clock): passed, or failed on one step.
DONE = facts(open_agent=[], settled=True, outcome="passed", latest_run_at=NOW * 2)
FAILED = facts(
    failed=["login works"], failed_at=NOW * 2, outcome="failed", latest_run_at=NOW * 2
)

SANDBOX = {
    "name": "sandbox",
    "reach": "https://sandbox.example",
    "branch": "sandbox",
    "deploy_minutes": 15,
    "implicit": False,
}


# --------------------------------------------------------------------------- #
# Normaliser
# --------------------------------------------------------------------------- #
def test_defaults():
    assert vcfg() == {
        "where": "branch",
        "ready": "now",
        "ready_minutes": 0,
        "who": "both",
        "notes": "",
        "post_to_pr": False,
    }


def test_legacy_wait_live_maps_to_where_and_ready():
    on = vcfg(wait_live=True, who="agent")
    assert (on["where"], on["ready"], on["who"]) == ("live", "on_branch", "agent")
    off = vcfg(wait_live=False)
    assert (off["where"], off["ready"]) == ("branch", "now")
    assert "wait_live" not in on and "wait_live" not in off


def test_branch_with_on_branch_reads_as_now_and_where_is_lowercased():
    assert vcfg(where="branch", ready="on_branch")["ready"] == "now"
    c = vcfg(where="SandBox", ready="after_prev", ready_minutes="20", notes=" x ")
    assert (c["where"], c["ready"], c["ready_minutes"], c["notes"]) == (
        "sandbox",
        "after_prev",
        20,
        "x",
    )
    assert vcfg(post_to_pr="true")["post_to_pr"] is True


@pytest.mark.parametrize(
    "config,field",
    [
        ({"where": "no spaces"}, "steps[0].config.where"),
        ({"where": "x" * 41}, "steps[0].config.where"),
        ({"ready": "later"}, "steps[0].config.ready"),
        ({"ready_minutes": 5000}, "steps[0].config.ready_minutes"),
        ({"ready_minutes": "soon"}, "steps[0].config.ready_minutes"),
        ({"who": "robot"}, "steps[0].config.who"),
    ],
)
def test_bad_values_are_refused_in_strict_mode(config, field):
    with pytest.raises(F.PlanError) as err:
        F.normalize_plan({"name": "x", "steps": [vstep(**config)]})
    assert err.value.field == field


def test_bad_values_fall_back_when_lenient():
    c = F.normalize_step(
        vstep(where="no spaces", ready="later", ready_minutes=5000), strict=False
    )["config"]
    assert (c["where"], c["ready"], c["ready_minutes"]) == ("branch", "now", 1440)


def test_kind_fields_in_contract_order():
    fields = F.STEP_KINDS["verify"]["fields"]
    assert [f["name"] for f in fields] == [
        "where",
        "ready",
        "ready_minutes",
        "who",
        "notes",
        "post_to_pr",
    ]
    by = {f["name"]: f for f in fields}
    assert by["where"]["type"] == "text" and by["where"]["default"] == "branch"
    assert by["ready"]["options"] == ["now", "on_branch", "after_prev"]
    assert by["who"]["option_labels"]["both"] == "Agent, then me"
    assert by["post_to_pr"]["default"] is False


def test_builtins_carry_the_new_config():
    t = F.get_plan("builtin:ticket")
    v = [s for s in t["steps"] if s["kind"] == "verify"][0]["config"]
    assert (v["where"], v["ready"], v["who"]) == ("branch", "now", "agent")
    b = F.get_plan("builtin:bugreport")
    v = [s for s in b["steps"] if s["kind"] == "verify"][0]["config"]
    assert (v["where"], v["ready"], v["who"]) == ("branch", "now", "agent")
    v = F.get_plan("builtin:verify")["steps"][0]["config"]
    assert (v["where"], v["ready"], v["who"]) == ("live", "on_branch", "both")


def test_sentences():
    assert (
        C.verify_sentence(
            {
                "where": "sandbox",
                "who": "both",
                "ready": "after_prev",
                "ready_minutes": 15,
            }
        )
        == "Verify on sandbox · agent, then you · 15 min after the stop before"
    )
    assert (
        C.verify_sentence(vcfg(who="agent"))
        == "Verify on this branch · agent only · right away"
    )
    assert (
        C.verify_sentence(vcfg(where="live", ready="on_branch"), env_branch="release")
        == "Verify on live · agent, then you · once on release"
    )
    assert C.step_phrase(vstep(**vcfg(where="live", ready="on_branch"))).startswith(
        "Verify on live · agent, then you · once on the live branch"
    )


# --------------------------------------------------------------------------- #
# The ready gate
# --------------------------------------------------------------------------- #
def judge(f, v, now):
    return F.next_action(f, {"session": sess(), "verify": v}, now)


def test_now_has_no_gate():
    f = started(fly(vstep(where="sandbox", ready="now")))
    a = judge(f, facts(env=SANDBOX), NOW)
    assert a["action"] == "run" and a["phase"] == "agent_run"


def test_setting_up_is_not_gated():
    f = started(fly(vstep(where="sandbox", ready="on_branch")))
    assert judge(f, {"env": SANDBOX}, NOW)["note"] == "getting the check ready"


def test_on_branch_waits_for_the_branch_then_the_deploy_minutes():
    f = started(fly(vstep(where="sandbox", ready="on_branch")))
    a = judge(f, facts(env=SANDBOX, env_reached_at=0.0), NOW)
    assert (
        a["action"] == "wait" and a["note"] == "waiting for the work to reach sandbox"
    )
    reached = NOW + 100
    a = judge(f, facts(env=SANDBOX, env_reached_at=reached), reached + 60)
    assert a["action"] == "wait" and a["note"] == "14 min for sandbox to be ready"
    a = judge(f, facts(env=SANDBOX, env_reached_at=reached), reached + 15 * 60)
    assert a["action"] == "run" and a["phase"] == "agent_run"


def test_on_branch_ready_minutes_beat_the_deploy_minutes():
    f = started(fly(vstep(where="sandbox", ready="on_branch", ready_minutes=2)))
    reached = NOW + 10
    assert (
        judge(f, facts(env=SANDBOX, env_reached_at=reached), reached + 60)["action"]
        == "wait"
    )
    assert (
        judge(f, facts(env=SANDBOX, env_reached_at=reached), reached + 120)["action"]
        == "run"
    )


def test_on_branch_live_without_env_facts_uses_the_live_mark():
    f = started(fly(vstep(wait_live=True, who="agent")))
    a = judge(f, facts(open_agent=[]), NOW)
    assert a["action"] == "wait" and "reach main" in a["note"]
    a = judge(f, facts(open_agent=[], live=True), NOW)
    assert a["action"] == "run" and a["phase"] == "agent_run"
    ok = facts(live=True, outcome="passed", latest_run_at=NOW + 5)
    assert judge(f, ok, NOW + 10)["action"] == "done"


def test_after_prev_waits_minutes_after_the_stop_started():
    f = started(fly(vstep(where="sandbox", ready="after_prev")), now=NOW)
    a = judge(f, facts(env=SANDBOX), NOW + 60)
    assert a["action"] == "wait" and a["note"] == "14 min for sandbox to be ready"
    assert judge(f, facts(env=SANDBOX), NOW + 15 * 60)["action"] == "run"
    g = started(fly(vstep(ready="after_prev", ready_minutes=5), session="t"))
    assert judge(g, facts(), NOW + 60)["note"] == "4 min for this branch to be ready"
    assert judge(g, facts(), NOW + 300)["action"] == "run"


def test_a_person_is_not_asked_before_the_gate():
    f = started(fly(vstep(where="sandbox", ready="on_branch", who="you")))
    need = dict(outcome="needs_you", latest_run_at=NOW + 5)
    a = judge(f, facts(env=SANDBOX, **need), NOW)
    assert a["action"] == "wait"
    a = judge(f, facts(env=SANDBOX, env_reached_at=NOW, **need), NOW + 15 * 60)
    assert a["action"] == "you" and a["detail"]["ask"] == "confirm"


def test_unknown_environment_fails_the_stop():
    f = started(fly(vstep(where="nowhere")))
    a = judge(f, {"env_missing": "nowhere"}, NOW)
    assert a["action"] == "fail"
    assert a["reason"] == "no environment named nowhere in this repo"


# --------------------------------------------------------------------------- #
# The driver
# --------------------------------------------------------------------------- #
class FakeInst:
    def __init__(self, path="/repo"):
        self.Path = path
        self.Branch = "feat"
        self.Status = SimpleNamespace(name="Running")

    def GetWorktreePath(self):
        return self.Path

    def GetGitWorktree(self):
        return SimpleNamespace(GetRepoPath=lambda: self.Path)


@pytest.fixture
def srv(monkeypatch):
    ns = SimpleNamespace(
        _SERVER_BOOT_ID="boot-1",
        ENGINE=SimpleNamespace(instances={"s": FakeInst()}),
        _pending_rows=lambda: [],
        git_available=lambda: True,
        _peer_guard=SimpleNamespace(refusal=lambda inst, what: None),
        _lane_refusal=lambda title, lane: None,
        _publish_autopilot=lambda title: None,
        asked=[],
        ran=[],
    )

    def ensure(title):
        ns.asked.append(title)
        return "tp1", ""

    ns._ensure_verify_plan = ensure

    async def fake_run(pid, req, payload):
        ns.ran.append((pid, payload))
        return SimpleNamespace(status_code=202, body=b"{}")

    ns.run_test_plan = fake_run
    monkeypatch.setattr(D, "_server", lambda: ns)
    monkeypatch.setattr(
        D,
        "_session_obs",
        lambda flight, now, agent: {
            "present": True,
            "pending": False,
            "status": "running",
            "sleeping": False,
            "activity": "idle",
            "branch": "feat",
            "folder": "/repo",
            "pr_url": "",
        },
    )
    monkeypatch.setattr(D, "_repo_root", lambda title: "/repo")
    return ns


@pytest.fixture
def envs(monkeypatch):
    known = {"sandbox": SANDBOX, "live": dict(SANDBOX, name="live", branch="main")}
    calls = []
    monkeypatch.setattr(
        _tp, "environment", lambda root, name: known.get(name), raising=False
    )

    def reached(pid, env):
        calls.append((pid, env["name"]))
        return 0.0

    monkeypatch.setattr(_tp, "env_reached_at", reached, raising=False)
    return calls


def run(coro):
    return asyncio.run(coro)


def test_driver_passes_where_and_notes_to_the_checklist_and_the_run(
    srv, envs, monkeypatch
):
    facts_now = {}
    monkeypatch.setattr(D, "_verify_obs", lambda f, ss, sess: dict(facts_now))
    f = fly(vstep(where="sandbox", notes="log in as qa"))
    run(D.step_flight(f["id"]))
    assert srv.asked == ["s"]  # the plan, made empty: nothing is generated
    facts_now.update(facts(env=SANDBOX))
    run(D.step_flight(f["id"]))
    assert srv.ran == [
        ("tp1", {"environment": "sandbox", "notes": "log in as qa", "walk": "agent"})
    ]


def test_unknown_environment_fails_at_the_start(srv, envs):
    f = fly(vstep(where="nowhere"))
    run(D.step_flight(f["id"]))
    got = F.get(f["id"])
    assert got["step_states"]["s1"]["state"] == "failed"
    assert (
        "no environment named nowhere in this repo" in got["step_states"]["s1"]["note"]
    )
    assert srv.asked == []


def test_verify_obs_reads_the_env_and_its_reach(srv, envs, monkeypatch):
    plan = {"branch": "feat", "state": "generated", "steps": [], "runs": []}
    monkeypatch.setattr(_tp, "get", lambda pid: plan)
    monkeypatch.setattr(_tp, "is_settled", lambda p: False)
    monkeypatch.setattr(_tp, "open_human_steps", lambda p: [])
    monkeypatch.setattr(_tp, "failed_steps", lambda p: [])
    f = started(fly(vstep(where="sandbox", ready="on_branch")))
    obs = D._verify_obs(f, {"detail": {"plan": "tp1"}}, {})
    assert obs["env"] == SANDBOX and obs["env_reached_at"] == 0.0
    assert envs == [("tp1", "sandbox")]
    g = started(fly(vstep(where="sandbox", ready="now"), session="t"))
    obs = D._verify_obs(g, {"detail": {"plan": "tp1"}}, {})
    assert obs["env"] == SANDBOX and envs == [("tp1", "sandbox")]
    h = started(fly(vstep(), session="u"))
    assert D._verify_obs(h, {"detail": {"plan": "tp1"}}, {})["env"] is None
    k = started(fly(vstep(where="nowhere"), session="v"))
    assert D._verify_obs(k, {"detail": {"plan": "tp1"}}, {}) == {
        "env_missing": "nowhere"
    }


@pytest.fixture
def pr(monkeypatch):
    """A PR to comment on, a token, the REST seam recorded."""
    posted = []
    state = {"pr": SimpleNamespace(number=7), "status": 201, "raise": None}

    async def pr_of(sess, flight):
        return (state["pr"], "o/r") if state["pr"] is not None else (None, "")

    async def token():
        return "tok"

    async def request(method, path, token=None, body=None, **kw):
        if state["raise"]:
            raise state["raise"]
        posted.append((method, path, body))
        return state["status"], {"message": "nope"}

    monkeypatch.setattr(D, "_pr_of", pr_of)
    monkeypatch.setattr(_gh, "api_token", token)
    monkeypatch.setattr(_gh, "_request", request)
    monkeypatch.setattr(_tp, "get", lambda pid: {"id": pid})
    monkeypatch.setattr(
        _tp, "results_markdown", lambda plan: "## Results\n- ok", raising=False
    )
    state["posted"] = posted
    return state


def _settle(f, monkeypatch, v):
    monkeypatch.setattr(D, "_verify_obs", lambda fl, ss, sess: dict(v))
    run(D.step_flight(f["id"]))
    return F.get(f["id"])


def test_post_to_pr_posts_once_when_the_stop_is_done(srv, envs, pr, monkeypatch):
    f = fly(vstep(post_to_pr=True, who="agent"), {"kind": "notify", "config": {}})
    run(D.step_flight(f["id"]))
    got = _settle(f, monkeypatch, DONE)
    assert got["step_states"]["s1"]["state"] == "done"
    assert pr["posted"] == [
        ("POST", "/repos/o/r/issues/7/comments", {"body": "## Results\n- ok"})
    ]
    assert got["step_states"]["s1"]["detail"]["pr_comment"] == "posted"
    assert any(
        h["event"] == "noted" and "posted the results" in h["note"]
        for h in got["history"]
    )
    run(D.step_flight(f["id"]))
    assert len(pr["posted"]) == 1


def test_post_to_pr_posts_when_the_stop_fails(srv, envs, pr, monkeypatch):
    f = fly(vstep(post_to_pr=True))
    run(D.step_flight(f["id"]))
    got = _settle(f, monkeypatch, FAILED)
    assert got["step_states"]["s1"]["state"] == "failed"
    assert len(pr["posted"]) == 1


def test_no_pr_is_skipped_with_a_note(srv, envs, pr, monkeypatch):
    pr["pr"] = None
    f = fly(vstep(post_to_pr=True, who="agent"))
    run(D.step_flight(f["id"]))
    got = _settle(f, monkeypatch, DONE)
    assert got["state"] == "done" and pr["posted"] == []
    assert got["step_states"]["s1"]["detail"]["pr_comment"].startswith("skipped")
    assert any(h["event"] == "noted" and "no PR" in h["note"] for h in got["history"])


def test_a_posting_error_does_not_fail_the_stop(srv, envs, pr, monkeypatch):
    pr["raise"] = RuntimeError("boom")
    f = fly(vstep(post_to_pr=True, who="agent"))
    run(D.step_flight(f["id"]))
    got = _settle(f, monkeypatch, DONE)
    assert got["state"] == "done" and got["step_states"]["s1"]["state"] == "done"
    assert got["step_states"]["s1"]["detail"]["pr_comment"] == "failed: boom"


def test_without_post_to_pr_nothing_is_posted(srv, envs, pr, monkeypatch):
    f = fly(vstep(who="agent"))
    run(D.step_flight(f["id"]))
    got = _settle(f, monkeypatch, DONE)
    assert got["state"] == "done" and pr["posted"] == []
    assert "pr_comment" not in got["step_states"]["s1"]["detail"]


# --------------------------------------------------------------------------- #
# The words (flight_cookbook)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "config,env_branch,want",
    [
        (None, "", "Verify on this branch · agent, then you · right away"),
        ("junk", "", "Verify on this branch · agent, then you · right away"),
        ({"who": "robot"}, "", "Verify on this branch · agent, then you · right away"),
        ({"who": "you"}, "", "Verify on this branch · only you · right away"),
        (
            {"where": "sandbox", "ready": "now", "who": "agent"},
            "",
            "Verify on sandbox · agent only · right away",
        ),
        (
            {"where": "sandbox", "ready": "on_branch"},
            "",
            "Verify on sandbox · agent, then you · once on the sandbox branch",
        ),
        (
            {"where": "live", "ready": "on_branch", "ready_minutes": 15},
            "release",
            "Verify on live · agent, then you · once on release + 15 min",
        ),
        # on_branch on this branch is not a wait: the code is already there.
        (
            {"where": "branch", "ready": "on_branch"},
            "release",
            "Verify on this branch · agent, then you · right away",
        ),
        (
            {"where": "sandbox", "ready": "after_prev", "ready_minutes": 5},
            "",
            "Verify on sandbox · agent, then you · 5 min after the stop before",
        ),
        # Pinned as is: 0 minutes after the stop before on this branch.
        (
            {"where": "branch", "ready": "after_prev", "ready_minutes": 0},
            "",
            (
                "Verify on this branch · agent, then you · after the stop before, "
                "once this branch has deployed"
            ),
        ),
        # Minutes are clamped to 0..1440.
        (
            {"where": "sandbox", "ready": "after_prev", "ready_minutes": 9999},
            "",
            "Verify on sandbox · agent, then you · 1440 min after the stop before",
        ),
    ],
)
def test_verify_sentence_matrix(config, env_branch, want):
    assert C.verify_sentence(config, env_branch=env_branch) == want


def test_verify_sentence_reads_every_where_ready_who():
    for where in ("branch", "live", "sandbox"):
        for ready in F.VERIFY_READY:
            for who, words in C.VERIFY_WHO_WORDS.items():
                s = C.verify_sentence(vcfg(where=where, ready=ready, who=who))
                place = "this branch" if where == "branch" else where
                assert s.startswith("Verify on %s · %s · " % (place, words)), s


def test_step_phrase_says_post_to_pr_and_reads_legacy_configs():
    assert C.step_phrase(vstep(**vcfg(post_to_pr=True, who="agent"))) == (
        "Verify on this branch · agent only · right away (posts the results to the PR)"
    )
    assert "posts the results" not in C.step_phrase(vstep(**vcfg()))
    # An unnormalised legacy step (an old snapshot) still reads right.
    assert C.step_phrase(vstep(wait_live=True)).startswith(
        "Verify on live · agent, then you · once on the live branch"
    )
    assert C.step_phrase(vstep(wait_live=False, who="you")).startswith(
        "Verify on this branch · only you · right away"
    )


def test_flights_reexports_verify_sentence():
    assert F.verify_sentence is C.verify_sentence


def test_cookbook_verify_entry_names_the_new_fields():
    text = " ".join(str(v) for v in C.STOPS["verify"].values())
    assert "wait_live" not in text
    for word in (
        "where=branch",
        "environments",
        "ready=now|on_branch|after_prev",
        "ready_minutes",
        "who=both|agent|you",
        "notes",
        "post_to_pr=true",
    ):
        assert word in text, word


# --------------------------------------------------------------------------- #
# The normaliser: boundaries and the legacy matrix
# --------------------------------------------------------------------------- #
def strict(**config):
    return F.normalize_plan({"name": "x", "steps": [vstep(**config)]})["steps"][0][
        "config"
    ]


def lenient(**config):
    return F.normalize_step(vstep(**config), strict=False)["config"]


@pytest.mark.parametrize(
    "raw,want", [("15", 15), (15.7, 15), (None, 0), ("", 0), (0, 0), (1440, 1440)]
)
def test_ready_minutes_accepted(raw, want):
    assert strict(ready_minutes=raw)["ready_minutes"] == want
    assert lenient(ready_minutes=raw)["ready_minutes"] == want


@pytest.mark.parametrize("raw,clamped", [(-1, 0), (1441, 1440), ("abc", 0)])
def test_ready_minutes_refused_strict_clamped_lenient(raw, clamped):
    with pytest.raises(F.PlanError) as err:
        strict(ready_minutes=raw)
    assert err.value.field == "steps[0].config.ready_minutes"
    assert lenient(ready_minutes=raw)["ready_minutes"] == clamped


@pytest.mark.parametrize(
    "config,where,ready",
    [
        ({"wait_live": True, "where": "sandbox"}, "sandbox", "on_branch"),
        ({"wait_live": True, "where": "branch"}, "branch", "now"),
        ({"wait_live": True, "ready": "after_prev"}, "live", "after_prev"),
        ({"wait_live": True, "where": "sandbox", "ready": "now"}, "sandbox", "now"),
        ({"wait_live": "true"}, "live", "on_branch"),
        ({"wait_live": False, "where": "sandbox"}, "sandbox", "now"),
    ],
)
def test_legacy_wait_live_matrix(config, where, ready):
    for c in (strict(**config), lenient(**config)):
        assert (c["where"], c["ready"]) == (where, ready)
        assert "wait_live" not in c
        assert set(c) == {
            "where",
            "ready",
            "ready_minutes",
            "who",
            "notes",
            "post_to_pr",
        }


def test_where_is_lowercased_and_validated():
    assert strict(where="Sandbox")["where"] == "sandbox"
    assert strict(where="qa_env-2")["where"] == "qa_env-2"
    assert strict(where="x" * 40)["where"] == "x" * 40
    for bad in ("sand box", "a/b", "x" * 41, "über"):
        with pytest.raises(F.PlanError) as err:
            strict(where=bad)
        assert err.value.field == "steps[0].config.where"
        assert lenient(where=bad)["where"] == "branch"


def test_notes_are_trimmed_and_capped():
    assert strict(notes="  log in as qa  ")["notes"] == "log in as qa"
    assert len(strict(notes="n" * 3000)["notes"]) == 2000


def test_a_stored_legacy_flight_reads_in_the_new_shape():
    """A flight saved before where/ready (``{who, wait_live}`` on disk) is
    normalised on load, and the judge reads it without a KeyError."""
    import json

    f = fly(vstep(who="agent"), session="old")
    path = F.flights_path()
    with open(path) as fh:
        data = json.load(fh)
    data["flights"][f["id"]]["steps"][0]["config"] = {"who": "agent", "wait_live": True}
    with open(path, "w") as fh:
        json.dump(data, fh)
    got = F.get(f["id"])
    assert got["steps"][0]["config"] == {
        "where": "live",
        "ready": "on_branch",
        "ready_minutes": 0,
        "who": "agent",
        "notes": "",
        "post_to_pr": False,
    }
    listed = next(x for x in F.list_flights() if x["id"] == f["id"])
    assert listed["steps"][0]["config"] == got["steps"][0]["config"]
    got = started(got)
    a = judge(got, facts(open_agent=[]), NOW)
    assert a["action"] == "wait" and "reach main" in a["note"]


# --------------------------------------------------------------------------- #
# The ready gate's edges
# --------------------------------------------------------------------------- #
def gate(cfg, v, now, since=NOW):
    return F._verify_ready_gate(vcfg(**cfg), {"since": since}, v, now)


def test_gate_on_branch_with_no_minutes_is_ready_once_reached():
    env = dict(SANDBOX, deploy_minutes=0)
    v = {"env": env, "env_reached_at": NOW}
    assert gate({"where": "sandbox", "ready": "on_branch"}, v, NOW) == ""


def test_gate_on_branch_boundary_and_rounding():
    v = {"env": SANDBOX, "env_reached_at": NOW}
    cfg = {"where": "sandbox", "ready": "on_branch"}
    assert gate(cfg, v, NOW + 15 * 60) == ""
    assert gate(cfg, v, NOW + 15 * 60 - 1) == "1 min for sandbox to be ready"
    assert gate(cfg, v, NOW + 1) == "15 min for sandbox to be ready"
    assert gate(cfg, v, NOW + 61) == "14 min for sandbox to be ready"


def test_gate_on_branch_waits_on_the_env_branch_name():
    v = {"env": dict(SANDBOX, branch="release"), "env_reached_at": 0}
    assert (
        gate({"where": "sandbox", "ready": "on_branch"}, v, NOW)
        == "waiting for the work to reach release"
    )
    v = {"env": dict(SANDBOX, branch=""), "env_reached_at": 0}
    assert (
        gate({"where": "sandbox", "ready": "on_branch"}, v, NOW)
        == "waiting for the work to reach the sandbox branch"
    )


def test_gate_on_branch_without_env_facts_uses_the_live_mark():
    cfg = {"where": "live", "ready": "on_branch"}
    assert gate(cfg, {}, NOW) == "waiting for the work to reach the live branch"
    assert gate(cfg, {"live_branch": "main"}, NOW) == (
        "waiting for the work to reach main"
    )
    assert gate(cfg, {"live": True}, NOW) == ""


def test_gate_deploy_minutes_out_of_range_are_clamped():
    cfg = {"where": "sandbox", "ready": "on_branch"}
    huge = {"env": dict(SANDBOX, deploy_minutes=99999), "env_reached_at": NOW}
    assert gate(cfg, huge, NOW + 60) == "1439 min for sandbox to be ready"
    assert gate(cfg, huge, NOW + 1440 * 60) == ""
    neg = {"env": dict(SANDBOX, deploy_minutes=-5), "env_reached_at": NOW}
    assert gate(cfg, neg, NOW) == ""
    junk = {"env": dict(SANDBOX, deploy_minutes="soon"), "env_reached_at": NOW}
    assert gate(cfg, junk, NOW) == ""


def test_gate_after_prev_without_a_start_is_ready():
    cfg = vcfg(where="sandbox", ready="after_prev", ready_minutes=10)
    assert F._verify_ready_gate(cfg, {"since": 0}, {}, NOW) == ""
    assert F._verify_ready_gate(cfg, {}, {}, NOW) == ""
    assert F._verify_ready_gate(cfg, {"since": NOW}, {}, NOW + 10 * 60) == ""
    assert (
        F._verify_ready_gate(cfg, {"since": NOW}, {}, NOW + 1)
        == "10 min for sandbox to be ready"
    )


def test_gate_after_prev_with_no_minutes_anywhere_is_ready():
    cfg = vcfg(where="branch", ready="after_prev")
    assert F._verify_ready_gate(cfg, {"since": NOW}, {}, NOW) == ""


def test_env_missing_beats_the_write_deadline_and_the_failures():
    f = started(fly(vstep(where="nowhere")))
    late = NOW + F.VERIFY_WRITE_DEADLINE_S + 60
    a = judge(f, {"env_missing": "nowhere"}, late)
    assert a["action"] == "fail"
    assert a["reason"] == "no environment named nowhere in this repo"
    a = judge(
        f,
        facts(env_missing="nowhere", failed=["login works"], failed_at=NOW * 2),
        NOW,
    )
    assert a["reason"] == "no environment named nowhere in this repo"


# --------------------------------------------------------------------------- #
# History and the kinds payload
# --------------------------------------------------------------------------- #
def test_noted_is_a_history_event_that_survives_a_reload():
    assert "noted" in F.HISTORY_EVENTS
    f = fly(vstep())
    F.mutate(f["id"], lambda x: F._hist(x, NOW, "s1", "noted", "posted the results"))
    got = F.get(f["id"])
    assert any(
        h["event"] == "noted" and h["note"] == "posted the results"
        for h in got["history"]
    )


def test_kinds_dto_passes_option_labels_through_for_verify_only():
    kinds = {k["kind"]: k for k in F.kinds_dto()["kinds"]}
    by = {fld["name"]: fld for fld in kinds["verify"]["fields"]}
    spec = {fld["name"]: fld for fld in F.STEP_KINDS["verify"]["fields"]}
    for name in ("ready", "who"):
        assert by[name]["option_labels"] == spec[name]["option_labels"]
        assert set(by[name]["option_labels"]) == set(by[name]["options"])
    assert by["ready"]["option_labels"] == {
        "now": "Right away",
        "on_branch": "When it reaches the environment's branch",
        "after_prev": "Minutes after the stop before",
    }
    assert by["who"]["option_labels"] == {
        "both": "Agent, then me",
        "agent": "Agent only",
        "you": "Only me",
    }
    with_labels = [
        (k["kind"], fld["name"])
        for k in F.kinds_dto()["kinds"]
        for fld in k["fields"]
        if "option_labels" in fld
    ]
    assert sorted(with_labels) == [("verify", "ready"), ("verify", "who")]
    # A copy: editing the payload never edits the registry.
    by["who"]["option_labels"]["both"] = "x"
    assert spec["who"]["option_labels"]["both"] == "Agent, then me"


# --------------------------------------------------------------------------- #
# The driver's environment lookup
# --------------------------------------------------------------------------- #
def test_verify_env_resolves_names(monkeypatch):
    roots = []

    def environment(root, name):
        roots.append((root, name))
        return {"sandbox": SANDBOX}.get(name)

    monkeypatch.setattr(_tp, "environment", environment, raising=False)
    monkeypatch.setattr(D, "_repo_root", lambda title: "/from-session")
    flight = {"id": "f1", "session": "s", "repo_root": ""}
    assert D._verify_env(flight, "branch") == (None, False)
    assert D._verify_env(flight, "") == (None, False)
    assert roots == []
    env, missing = D._verify_env(flight, "sandbox")
    assert env == SANDBOX and env is not SANDBOX and missing is False
    # The implicit live is never "missing"; any other unknown name is.
    assert D._verify_env(flight, "live") == (None, False)
    assert D._verify_env(flight, "nowhere") == (None, True)
    assert {r for r, _ in roots} == {"/from-session"}
    roots.clear()
    D._verify_env(dict(flight, repo_root="/stored"), "sandbox")
    assert roots == [("/stored", "sandbox")]


def test_verify_env_unreadable_environments_are_unknown_not_missing(monkeypatch):
    logged = []

    def boom(root, name):
        raise OSError("bad toml")

    monkeypatch.setattr(_tp, "environment", boom, raising=False)
    monkeypatch.setattr(D, "_repo_root", lambda title: "/repo")
    monkeypatch.setattr(D, "_warn", lambda fmt, *a: logged.append((fmt, a)))
    assert D._verify_env({"id": "f1", "session": "s"}, "sandbox") == (None, False)
    assert len(logged) == 1
    fmt, args = logged[0]
    assert "environments unavailable" in fmt and "bad toml" in str(args[-1])


def test_verify_obs_reach_errors_read_as_not_reached(srv, monkeypatch):
    plan = {"branch": "feat", "state": "generated", "steps": [], "runs": []}
    monkeypatch.setattr(
        _tp,
        "environment",
        lambda root, name: {"sandbox": SANDBOX}.get(name),
        raising=False,
    )

    def reached(pid, env):
        raise RuntimeError("git down")

    monkeypatch.setattr(_tp, "env_reached_at", reached, raising=False)
    monkeypatch.setattr(_tp, "get", lambda pid: plan)
    monkeypatch.setattr(_tp, "is_settled", lambda p: False)
    monkeypatch.setattr(_tp, "open_human_steps", lambda p: [])
    monkeypatch.setattr(_tp, "failed_steps", lambda p: [])
    f = started(fly(vstep(where="sandbox", ready="on_branch")))
    obs = D._verify_obs(f, {"detail": {"plan": "tp1"}}, {})
    assert obs["env"] == SANDBOX and obs["env_reached_at"] == 0.0


def test_verify_obs_after_prev_never_asks_for_the_reach(srv, envs, monkeypatch):
    plan = {"branch": "feat", "state": "generated", "steps": [], "runs": []}
    monkeypatch.setattr(_tp, "get", lambda pid: plan)
    monkeypatch.setattr(_tp, "is_settled", lambda p: False)
    monkeypatch.setattr(_tp, "open_human_steps", lambda p: [])
    monkeypatch.setattr(_tp, "failed_steps", lambda p: [])
    f = started(fly(vstep(where="sandbox", ready="after_prev")))
    obs = D._verify_obs(f, {"detail": {"plan": "tp1"}}, {})
    assert obs["env"] == SANDBOX and obs["env_reached_at"] == 0.0
    assert envs == []


def test_verify_obs_env_missing_before_any_plan(srv, envs):
    f = started(fly(vstep(where="nowhere")))
    assert D._verify_obs(f, {"detail": {}}, {}) == {"env_missing": "nowhere"}


def test_agent_run_passes_where_and_notes_and_legacy_defaults_to_branch(srv):
    f = started(fly(vstep(where="sandbox", notes="as qa")))
    action = {"action": "run", "phase": "agent_run", "detail": {"plan": "tp1"}}
    out = run(D._perform(f, action, NOW))
    assert out["detail"]["run_started"] is True
    assert srv.ran == [
        ("tp1", {"environment": "sandbox", "notes": "as qa", "walk": "agent"})
    ]
    # An unnormalised snapshot still carrying {who, wait_live}.
    g = started(fly(vstep(), session="t"))
    g["steps"][0]["config"] = {"who": "agent", "wait_live": True}
    run(D._perform(g, action, NOW))
    # who "agent" ("Agent only") walks every step.
    assert srv.ran[-1] == (
        "tp1",
        {"environment": "branch", "notes": "", "walk": "all"},
    )


# --------------------------------------------------------------------------- #
# Posting the results to the PR
# --------------------------------------------------------------------------- #
def _post(fid, obs=None):
    flight = F.get(fid)
    step = flight["steps"][0]
    return run(
        D._post_verify_results(
            fid, step, obs or {"session": sess(), "verify": {"plan": "tp1"}}, NOW
        )
    )


def test_no_token_is_skipped(srv, envs, pr, monkeypatch):
    async def no_token():
        return ""

    monkeypatch.setattr(_gh, "api_token", no_token)
    f = fly(vstep(post_to_pr=True, who="agent"))
    got = _post(f["id"])
    assert got["step_states"]["s1"]["detail"]["pr_comment"] == (
        "skipped: no GitHub token"
    )
    assert pr["posted"] == []
    assert any(
        h["event"] == "noted"
        and h["note"] == "results not posted to the PR (no GitHub token)"
        for h in got["history"]
    )


def test_a_refused_post_records_githubs_words_and_is_not_repeated(
    srv, envs, pr, monkeypatch
):
    pr["status"] = 422
    f = fly(vstep(post_to_pr=True, who="agent"))
    got = _post(f["id"])
    assert got["step_states"]["s1"]["detail"]["pr_comment"] == "failed: nope"
    assert len(pr["posted"]) == 1
    # Same attempt: the record stands, nothing is posted again.
    pr["status"] = 201
    assert _post(f["id"]) is None
    assert len(pr["posted"]) == 1


def test_no_checklist_is_skipped(srv, envs, pr, monkeypatch):
    monkeypatch.setattr(_tp, "get", lambda pid: None)
    f = fly(vstep(post_to_pr=True))
    got = _post(f["id"])
    assert got["step_states"]["s1"]["detail"]["pr_comment"] == (
        "skipped: no checklist to post"
    )
    got = _post(fly(vstep(post_to_pr=True), session="t")["id"], obs={"verify": {}})
    assert got["step_states"]["s1"]["detail"]["pr_comment"] == (
        "skipped: no checklist to post"
    )
    assert pr["posted"] == []


def test_a_retried_stop_posts_again(srv, envs, pr, monkeypatch):
    """Retry blanks the stop's state, so a new attempt is a new posting."""
    f = fly(vstep(post_to_pr=True))
    run(D.step_flight(f["id"]))
    got = _settle(f, monkeypatch, FAILED)
    assert got["state"] == "halted" and len(pr["posted"]) == 1
    F.mutate(f["id"], lambda x: F.op_retry(x, "s1"))
    assert "pr_comment" not in F.get(f["id"])["step_states"]["s1"]["detail"]
    run(D.step_flight(f["id"]))
    got = _settle(f, monkeypatch, FAILED)
    assert got["step_states"]["s1"]["state"] == "failed"
    assert len(pr["posted"]) == 2


def test_a_flight_gone_mid_post_returns_none(srv, envs, pr, monkeypatch):
    f = fly(vstep(post_to_pr=True))

    def gone(fid, fn):
        raise F.FlightError("gone", 404, "no such flight")

    monkeypatch.setattr(F, "mutate", gone)
    assert _post(f["id"]) is None
    assert len(pr["posted"]) == 1


def test_a_refused_record_keeps_the_pass_result(srv, envs, pr, monkeypatch):
    """The post's own record raising FlightError: the stop still finishes
    (``_step_member`` keeps its own ``after``)."""
    real = F.mutate

    def mutate(fid, fn):
        if fn.__qualname__.startswith("_post_verify_results"):
            raise F.FlightError("gone", 404, "no such flight")
        return real(fid, fn)

    monkeypatch.setattr(F, "mutate", mutate)
    f = fly(vstep(post_to_pr=True, who="agent"))
    run(D.step_flight(f["id"]))
    got = _settle(f, monkeypatch, DONE)
    assert got["state"] == "done" and len(pr["posted"]) == 1
    assert "pr_comment" not in got["step_states"]["s1"]["detail"]
