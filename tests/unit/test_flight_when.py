"""``when``: a condition on every stop (``backend.web.core.flight_when`` +
the core's normalise / plan / apply seams, the driver's facts and the
validation route — docs/flights.md "Round 15")."""

import asyncio
import subprocess
from types import SimpleNamespace

import pytest

from backend.web.core import flight_driver as D
from backend.web.core import flight_when as W
from backend.web.core import flights as F

NOW = 1_800_000_000.0


@pytest.fixture(autouse=True)
def _store(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDFLOCK_FLIGHTS_DIR", str(tmp_path / "flights"))
    monkeypatch.setenv("MINDFLOCK_AUTOPILOT_FILE", str(tmp_path / "autopilot.json"))
    monkeypatch.setattr(D, "_WHEN_DIFF", {})
    monkeypatch.setattr(D, "_WHEN_LABELS", {})
    yield


def step(kind, sid=None, when=None, **config):
    out = {"kind": kind, "config": config}
    if sid:
        out["id"] = sid
    if when is not None:
        out["when"] = when
    return out


def fly(*steps, session="s"):
    plan = F.save_plan({"name": "P", "steps": list(steps)}, now=NOW)
    return F.take_off(plan, session=session, now=NOW)


def sess(**kw):
    base = {"present": True, "status": "running", "activity": "idle"}
    base.update(kw)
    return base


# --------------------------------------------------------------------------- #
# Grammar
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "text, want",
    [
        ("always", "always"),
        ("NEVER", "never"),
        ("Weekday", "weekday"),
        ("weekend", "weekend"),
        ("diff touches frontend/**", "diff touches frontend/**"),
        (
            "diff touches frontend/**,*.sql , migrations/",
            "diff touches frontend/**, *.sql, migrations/",
        ),
        ("DIFF LINES > 200", "diff lines > 200"),
        ("files < 5", "files < 5"),
        ("hour between 9 and 17", "hour between 9 and 17"),
        ("hour between 22 and 6", "hour between 22 and 6"),
        ("branch matches release/*", "branch matches release/*"),
        ("label urgent", "label urgent"),
        ("label good first issue", "label good first issue"),
        ("judge is yes", "judge is yes"),
        ("judge s3 is NO", "judge s3 is no"),
        ("weekday and label urgent or never", "weekday and label urgent or never"),
        ("weekday and (label urgent or never)", "weekday and (label urgent or never)"),
        ("not weekday", "not weekday"),
        ("not (weekday or weekend)", "not (weekday or weekend)"),
        ("not(weekday)", "not weekday"),
        ("hour between 9 and 17 and weekday", "hour between 9 and 17 and weekday"),
        ("((always))", "always"),
    ],
)
def test_parse_and_canonical(text, want):
    cond = W.parse(text)
    assert W.canonical(cond) == want
    assert W.canonical(W.parse(want)) == want  # canonical is a fixed point


def test_and_binds_tighter_than_or():
    cond = W.parse("always or never and weekday")
    assert cond == ("or", ("always",), ("and", ("never",), ("weekday",)))
    assert W.parse("not always and never") == ("and", ("not", ("always",)), ("never",))


@pytest.mark.parametrize(
    "text, word",
    [
        ("", "empty"),
        ("   ", "empty"),
        ("foo", "'foo' is not a condition"),
        ("diff", "touches"),
        ("diff lines 3", "> or <"),
        ("diff touches", "at least one path"),
        ("files > x", "whole number"),
        ("hour between 9", "expected 'and'"),
        ("hour between 25 and 3", "0 to 24"),
        ("branch matches", "name or glob"),
        ("label", "missing"),
        ("judge", "judge is <value>"),
        ("judge s3", "expected 'is'"),
        ("always and", "ends too early"),
        ("(always", "expected ')'"),
        ("always)", "unexpected ')'"),
        ("and always", "cannot start with 'and'"),
        ("x" * 301, "at most 300"),
    ],
)
def test_errors_name_the_problem_and_carry_a_hint(text, word):
    with pytest.raises(W.WhenError) as err:
        W.parse(text)
    assert word in err.value.message
    assert "diff touches" in err.value.hint


@pytest.mark.parametrize(
    "text, groups",
    [
        ("always", set()),
        ("diff touches a", {"diff"}),
        ("files > 1 or diff lines < 3", {"diff"}),
        ("weekday", {"clock"}),
        ("hour between 1 and 2", {"clock"}),
        ("branch matches x", {"branch"}),
        ("label x", {"labels"}),
        ("judge is yes", {"judge"}),
        ("not (weekday and label x) or judge s1 is no", {"clock", "labels", "judge"}),
    ],
)
def test_needs(text, groups):
    assert W.needs(W.parse(text)) == groups


# --------------------------------------------------------------------------- #
# Evaluation
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "path, pattern, want",
    [
        ("frontend/src/App.tsx", "frontend/**", True),
        ("frontend/App.tsx", "frontend/**", True),
        ("frontend/App.tsx", "frontend/", True),
        ("frontend/App.tsx", "frontend", True),
        ("frontendx/App.tsx", "frontend", False),
        ("db/migrations/001.sql", "migrations/", True),
        ("db/migrations/001.sql", "*.sql", True),
        ("db/x.py", "*.sql", False),
        ("a/b/c.py", "a/*.py", False),
        ("a/c.py", "a/*.py", True),
        ("a/b/c.py", "a/**/c.py", True),
        ("a/c.py", "a/**/c.py", True),
        ("App.tsx", "App.tsx", True),
        ("x/App.tsx", "App.tsx", True),
        ("./x/App.tsx", "App.tsx", True),
        ("docs/flights.md", "docs/*.md", True),
        ("", "a", False),
    ],
)
def test_glob_matching(path, pattern, want):
    assert W.matches(path, pattern) is want


FACTS = {
    "files": ["frontend/App.tsx", "backend/x.py", "db/migrations/001.sql"],
    "added": 120,
    "removed": 90,
    "branch": "release/1.2",
    "labels": ["Urgent", "docs"],
    "hour": 23,
    "weekday": 5,
    "judges": {"s3": "yes"},
    "judge_last": "yes",
}


@pytest.mark.parametrize(
    "text, ok, why",
    [
        ("always", True, "always"),
        ("never", False, "never"),
        ("diff touches frontend/**", True, "diff touches frontend/App.tsx"),
        ("diff touches docs/**", False, "no changed file matches docs/**"),
        ("diff lines > 200", True, "210 lines changed"),
        ("diff lines < 200", False, "210 lines changed"),
        ("files > 2", True, "3 files changed"),
        ("files < 3", False, "3 files changed"),
        ("weekend", True, "it is Saturday"),
        ("weekday", False, "it is Saturday"),
        ("hour between 22 and 6", True, "it is 23:00"),
        ("hour between 9 and 17", False, "it is 23:00"),
        ("branch matches release/*", True, "branch is release/1.2"),
        ("branch matches feat/*", False, "branch is release/1.2"),
        ("label urgent", True, "label urgent"),
        ("label x", False, "labels: Urgent, docs"),
        ("judge is yes", True, "the judge answered yes"),
        ("judge s3 is no", False, "judge s3 answered yes"),
        ("judge s9 is yes", False, "judge s9 has not answered"),
        ("not weekday", True, "it is Saturday"),
        ("weekend and label urgent", True, "it is Saturday; label urgent"),
        ("weekday and label urgent", False, "it is Saturday"),
        ("weekday or label urgent", True, "label urgent"),
        ("weekday or label x", False, "it is Saturday; labels: Urgent, docs"),
    ],
)
def test_evaluate(text, ok, why):
    assert W.evaluate(W.parse(text), FACTS) == (ok, why)


def test_missing_facts_read_as_empty():
    assert W.evaluate(W.parse("diff touches x"), {}) == (
        False,
        "the diff touches no file",
    )
    assert W.evaluate(W.parse("files < 1"), None) == (True, "0 files changed")
    assert W.evaluate(W.parse("label x"), {}) == (False, "no labels")
    assert W.evaluate(W.parse("branch matches x"), {}) == (False, "no branch")
    assert W.evaluate(W.parse("judge is yes"), {})[0] is False


# --------------------------------------------------------------------------- #
# The normaliser
# --------------------------------------------------------------------------- #
def test_normalise_keeps_the_canonical_when():
    s = F.normalize_step({"kind": "agent", "when": "  DIFF touches frontend/** "}, 2)
    assert s["when"] == "diff touches frontend/**"
    assert "when" not in F.normalize_step({"kind": "agent", "when": ""}, 0)
    assert "when" not in F.normalize_step({"kind": "agent"}, 0)


def test_strict_bad_when_names_the_field():
    with pytest.raises(F.PlanError) as err:
        F.normalize_step({"kind": "agent", "when": "diff lines 3"}, 2)
    assert err.value.field == "steps[2].when"
    assert "> or <" in err.value.message
    with pytest.raises(F.PlanError) as err:
        F.normalize_step({"kind": "agent", "when": "always or " * 40}, 0)
    assert err.value.field == "steps[0].when" and "300" in err.value.message


def test_lenient_drops_a_bad_when():
    s = F.normalize_step({"kind": "agent", "when": "nonsense"}, 0, strict=False)
    assert s is not None and "when" not in s


def test_saved_plans_keep_when():
    plan = F.save_plan(
        {
            "name": "P",
            "steps": [step("agent", when="weekday"), step("wait", minutes=1)],
        },
        now=NOW,
    )
    assert plan["steps"][0]["when"] == "weekday"
    assert "when" not in plan["steps"][1]


# --------------------------------------------------------------------------- #
# The planner: skip_when
# --------------------------------------------------------------------------- #
def test_false_when_skips_the_step_true_runs_it():
    f = fly(step("wait", when="weekday", minutes=1), step("agent", prompt="x"))
    a = F.next_action(f, {"session": sess(), "facts": {"weekday": 6}}, NOW)
    assert a == {"action": "skip_when", "note": "skipped: weekday — it is Sunday"}
    f = F.apply(f, a, NOW)
    ss = f["step_states"]["s1"]
    assert ss["state"] == "skipped" and ss["note"] == "skipped: weekday — it is Sunday"
    assert f["history"][-1]["event"] == "skipped"
    assert f["history"][-1]["note"] == "skipped: weekday — it is Sunday"
    assert f["cursor"] == 1 and f["state"] == "running"
    # The next step is unaffected and the flight carries on.
    a = F.next_action(f, {"session": sess()}, NOW)
    assert a["action"] == "run"

    f = fly(step("wait", when="weekday", minutes=1), session="s2")
    a = F.next_action(f, {"session": sess(), "facts": {"weekday": 2}}, NOW)
    assert a["action"] == "run"


def test_without_facts_a_when_waits_visibly():
    f = fly(step("wait", when="label urgent", minutes=1))
    a = F.next_action(f, {"session": sess()}, NOW)
    assert a == {"action": "wait", "note": "checking label urgent"}


def test_skipping_the_last_step_lands_the_flight():
    f = fly(step("agent", prompt="x"), step("notify", when="never", text="hi"))
    f["step_states"]["s1"]["state"] = "done"
    f["cursor"] = 1
    a = F.next_action(f, {"session": sess(), "facts": {}}, NOW)
    assert a["action"] == "skip_when" and a["note"] == "skipped: never — never"
    f = F.apply(f, a, NOW)
    assert f["state"] == "done"


def test_a_halt_point_comes_before_the_when():
    f = fly(step("wait", when="never", minutes=1))
    f["steps"][0]["halt"] = True
    a = F.next_action(f, {"session": sess(), "facts": {}}, NOW)
    assert a["action"] == "hold"


def test_a_step_without_when_ignores_facts():
    f = fly(step("wait", minutes=1))
    a = F.next_action(f, {"session": sess(), "facts": {"weekday": 6}}, NOW)
    assert a["action"] == "run"


def test_fill_prompt_preview():
    f = fly(step("agent", prompt="see {preview} and {branch}"))
    assert F.fill_prompt("see {preview}|", f) == "see |"
    f["steps"].append(
        {"id": "s9", "kind": "preview", "title": "P", "config": {}, "on_fail": "halt"}
    )
    f["step_states"]["s9"] = {"state": "done", "detail": {"url": "https://x.test"}}
    assert F.preview_url(f) == "https://x.test"
    assert F.fill_prompt("see {preview}", f) == "see https://x.test"
    f["step_states"]["s9"]["state"] = "failed"
    assert F.fill_prompt("see {preview}", f) == "see "


# --------------------------------------------------------------------------- #
# The driver: facts
# --------------------------------------------------------------------------- #
def _git(repo, *args):
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": str(repo),
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
        },
    )


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    (r / "a.py").write_text("one\ntwo\n")
    (r / "frontend").mkdir()
    (r / "frontend" / "App.tsx").write_text("x\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "base")
    _git(r, "checkout", "-q", "-b", "feat")
    (r / "a.py").write_text("one\ntwo\nthree\nfour\n")
    _git(r, "commit", "-q", "-am", "work")
    return r


@pytest.fixture
def srv(monkeypatch, repo):
    inst = SimpleNamespace(
        Path=str(repo),
        Branch="feat",
        BaseBranch="main",
        GetWorktreePath=lambda: str(repo),
        GetGitWorktree=lambda: SimpleNamespace(GetRepoPath=lambda: str(repo)),
    )
    ns = SimpleNamespace(
        ENGINE=SimpleNamespace(instances={"s": inst}), _pending_rows=lambda: []
    )
    monkeypatch.setattr(D, "_server", lambda: ns)
    return ns


def _facts(f, s):
    return asyncio.run(D.when_facts(f, f["steps"][0], s, NOW))


def test_diff_facts_against_the_base_with_the_working_tree(srv, repo):
    f = fly(step("wait", when="diff touches a.py and diff lines > 1", minutes=1))
    s = sess(folder=str(repo), branch="feat")
    facts = _facts(f, s)
    assert facts == {"files": ["a.py"], "added": 2, "removed": 0}
    # Uncommitted edits and untracked files count too (a judge before the
    # first commit still sees the agent's work).
    (repo / "frontend" / "App.tsx").write_text("x\ny\n")
    (repo / "new.sql").write_text("select 1;\nselect 2;\n")
    facts = _facts(f, s)
    assert sorted(facts["files"]) == ["a.py", "frontend/App.tsx", "new.sql"]
    assert facts["added"] == 2 + 1 + 2 and facts["removed"] == 0
    ok, why = W.evaluate(W.parse("diff touches frontend/**"), facts)
    assert ok and why == "diff touches frontend/App.tsx"


def test_diff_facts_are_cached_per_head_and_tree(srv, repo, monkeypatch):
    f = fly(step("wait", when="files > 0", minutes=1))
    s = sess(folder=str(repo), branch="feat")
    _facts(f, s)
    assert len(D._WHEN_DIFF) == 1
    calls = []
    real = D._git_out

    def spy(folder, *args, **kw):
        calls.append(args[0])
        return real(folder, *args, **kw)

    monkeypatch.setattr(D, "_git_out", spy)
    _facts(f, s)
    assert "diff" not in calls and "merge-base" not in calls  # a warm pass
    (repo / "b.txt").write_text("hi\n")
    got = _facts(f, s)
    assert "b.txt" in got["files"] and len(D._WHEN_DIFF) == 2


def test_diff_base_prefers_the_row_then_origin_head_then_main(srv, repo):
    f = fly(step("wait", when="files > 0", minutes=1))
    assert D.diff_base(f, str(repo)) == "main"
    srv.ENGINE.instances["s"].BaseBranch = ""
    assert D.diff_base(f, str(repo)) == "main"  # no origin/HEAD: the main probe
    srv.ENGINE.instances["s"].BaseBranch = "nope"
    assert D.diff_base(f, str(repo)) == "main"
    assert D.diff_base(f, "") == ""
    assert D.when_diff(f, "/nonexistent")["files"] == []


def test_only_the_needed_groups_are_gathered(srv, repo, monkeypatch):
    monkeypatch.setattr(D, "when_diff", lambda *a: pytest.fail("diff gathered"))
    f = fly(step("wait", when="weekday and branch matches feat*", minutes=1))
    facts = _facts(f, sess(folder=str(repo), branch="feat"))
    assert set(facts) == {"hour", "weekday", "branch"}
    assert (
        facts["branch"] == "feat"
        and 0 <= facts["hour"] < 24
        and 0 <= facts["weekday"] < 7
    )


def test_judge_facts_come_from_done_judge_stops(srv):
    f = fly(
        step("judge", sid="j1", question="q1"),
        step("judge", sid="j2", question="q2"),
        step("agent", sid="a1", when="judge is yes", prompt="x"),
        step("judge", sid="j3", question="q3"),
    )
    f["step_states"]["j1"].update(state="done", detail={"value": "no"})
    f["step_states"]["j2"].update(state="done", detail={"value": "yes"})
    f["step_states"]["j3"].update(state="done", detail={"value": "maybe"})
    judges, last = D.when_judges(f, f["steps"][2])
    assert judges == {"j1": "no", "j2": "yes", "j3": "maybe"} and last == "yes"
    f["cursor"] = 2
    facts = asyncio.run(D.when_facts(f, f["steps"][2], sess(), NOW))
    assert facts == {"judges": judges, "judge_last": "yes"}
    assert F.next_action(f, {"session": sess(), "facts": facts}, NOW)["action"] == "run"
    f["step_states"]["j2"]["detail"]["value"] = "no"
    facts = asyncio.run(D.when_facts(f, f["steps"][2], sess(), NOW))
    a = F.next_action(f, {"session": sess(), "facts": facts}, NOW)
    assert (
        a["action"] == "skip_when"
        and a["note"] == "skipped: judge is yes — the judge answered no"
    )


def test_labels_come_from_github_and_are_cached(srv, repo, monkeypatch):
    from backend.web.core import github_pr as _gh

    asked = []

    async def req(method, path, *, token, params=None, body=None):
        asked.append(path)
        return 200, [{"name": "Urgent"}, {"name": "docs"}, "junk"]

    async def tok():
        return "t"

    monkeypatch.setattr(_gh, "_request", req)
    monkeypatch.setattr(_gh, "api_token", tok)
    monkeypatch.setattr(
        _gh, "repo_ref", lambda wt: SimpleNamespace(slug="o/r", owner="o")
    )
    f = fly(step("wait", when="label urgent", minutes=1))
    f["subject"] = {"kind": "pr", "id": "12", "title": "", "url": "", "source": ""}
    s = sess(folder=str(repo), branch="feat")
    assert _facts(f, s) == {"labels": ["Urgent", "docs"]}
    assert asked == ["/repos/o/r/issues/12/labels"]
    assert _facts(f, s) == {"labels": ["Urgent", "docs"]} and len(asked) == 1


def test_labels_without_a_pr_are_empty(srv, repo, monkeypatch):
    async def none(sess_, flight):
        return None, ""

    monkeypatch.setattr(D, "_pr_of", none)
    f = fly(step("wait", when="label urgent", minutes=1))
    assert _facts(f, sess(folder=str(repo), branch="feat")) == {"labels": []}


def test_observe_gathers_facts_only_for_a_pending_step_with_a_when(
    srv, repo, monkeypatch
):
    monkeypatch.setattr(
        D, "_session_obs", lambda f, now, agent: sess(folder=str(repo), branch="feat")
    )
    f = fly(step("wait", when="files > 0", minutes=1), step("wait", minutes=1))
    obs = asyncio.run(D.observe(f, NOW))
    assert obs["facts"]["files"] == ["a.py"]
    assert F.next_action(f, obs, NOW)["action"] == "run"
    f["step_states"]["s1"]["state"] = "done"
    f["cursor"] = 1
    assert "facts" not in asyncio.run(D.observe(f, NOW))


def test_a_pass_skips_then_runs_the_next_step(srv, repo, monkeypatch):
    """The driver's chain carries on after a skip_when within one pass."""
    monkeypatch.setattr(
        D, "_session_obs", lambda f, now, agent: sess(folder=str(repo), branch="feat")
    )
    monkeypatch.setattr(D, "_announce", lambda a, b: None)
    srv._SERVER_BOOT_ID = "boot"
    f = fly(step("wait", when="never", minutes=1), step("wait", minutes=1))
    asyncio.run(D.step_flight(f["id"]))
    got = F.get(f["id"])
    assert got["step_states"]["s1"]["state"] == "skipped"
    assert got["step_states"]["s1"]["note"] == "skipped: never — never"
    assert got["step_states"]["s2"]["state"] == "running"


# --------------------------------------------------------------------------- #
# The route
# --------------------------------------------------------------------------- #
@pytest.fixture
def client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from backend.web import server

    monkeypatch.setenv("MINDFLOCK_SETTINGS_FILE", str(tmp_path / "settings.json"))
    monkeypatch.setenv("MINDFLOCK_AUTH", "0")
    monkeypatch.delenv("CS_WEB_MODE", raising=False)
    return TestClient(server.app)


def test_when_route_validates_for_the_editor(client):
    r = client.get(
        "/api/flights/when", params={"expr": "Diff Touches frontend/** AND weekday"}
    )
    assert r.status_code == 200
    assert r.json() == {
        "ok": True,
        "canonical": "diff touches frontend/** and weekday",
        "error": "",
        "field": "",
    }
    r = client.get("/api/flights/when", params={"expr": "diff lines 3"})
    body = r.json()
    assert r.status_code == 200 and body["ok"] is False and body["field"] == "when"
    assert "> or <" in body["error"] and "diff touches" in body["hint"]
    assert client.get("/api/flights/when").json()["ok"] is True
    # Never read as a flight id.
    assert "/api/flights/when" in client.app.openapi()["paths"]
