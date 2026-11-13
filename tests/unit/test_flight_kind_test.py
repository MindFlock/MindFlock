"""The ``test`` stop (``backend.web.core.flight_kinds.test``, docs/flights.md
"Round 15"): the four runners' parsers on real-looking output, auto-detecting
the command from a worktree's files, expect pass / fail, the one rerun of a
few failures, a restart mid-run, the receipt — against a tmp store and fake
sessions, never the real engine."""

import asyncio
import json
import time

import pytest

from backend.web.core import flight_driver as D
from backend.web.core import flights as F
from backend.web.core.flight_kinds import test as T

NOW = time.time()

PYTEST_FAIL = """\
============================= test session starts ==============================
platform linux -- Python 3.12.3, pytest-8.3.2, pluggy-1.5.0
collected 312 items

tests/unit/test_a.py ........F.....                                      [  4%]
tests/unit/test_b.py ..E...                                              [  6%]

=================================== FAILURES ===================================
_________________________________ test_adds __________________________________

    def test_adds():
>       assert 1 + 1 == 3
E       assert 2 == 3

tests/unit/test_a.py:9: AssertionError
=========================== short test summary info ============================
FAILED tests/unit/test_a.py::test_adds - assert 2 == 3
ERROR tests/unit/test_b.py::test_db - ConnectionRefusedError
======== 1 failed, 309 passed, 2 skipped, 1 error, 4 warnings in 12.34s ========
"""

PYTEST_PASS_Q = """\
........................................................................ [ 23%]
........................................................................ [100%]
312 passed in 3.21s
"""

VITEST_FAIL = """\

 RUN  v2.1.4 /repo/frontend

 ✓ src/__tests__/a.test.ts (3 tests) 5ms
 ❯ src/__tests__/b.test.ts (2 tests | 1 failed) 7ms
   × math > adds numbers 3ms
     → expected 2 to be 3

⎯⎯⎯⎯⎯⎯⎯ Failed Tests 1 ⎯⎯⎯⎯⎯⎯⎯

 FAIL  src/__tests__/b.test.ts > math > adds numbers
AssertionError: expected 2 to be 3

 Test Files  1 failed | 1 passed (2)
      Tests  1 failed | 4 passed (5)
   Start at  12:00:00
   Duration  300ms
"""

JEST_FAIL = """\
PASS src/a.test.js
FAIL src/b.test.js
  math
    ✓ subtracts (1 ms)
    ✕ adds numbers (3 ms)

  ● math › adds numbers

    expect(received).toBe(expected) // Object.is equality

  ● Console

    console.log
      hi

Test Suites: 1 failed, 1 passed, 2 total
Tests:       1 failed, 4 passed, 5 total
Snapshots:   0 total
Time:        0.8 s
"""

GO_FAIL_V = """\
=== RUN   TestAdd
--- FAIL: TestAdd (0.00s)
    math_test.go:10: expected 3, got 2
=== RUN   TestTable
=== RUN   TestTable/zero
    --- FAIL: TestTable/zero (0.00s)
--- FAIL: TestTable (0.00s)
=== RUN   TestSub
--- PASS: TestSub (0.00s)
FAIL
FAIL\texample.com/m/math\t0.002s
ok  \texample.com/m/util\t0.003s
?   \texample.com/m/cmd\t[no test files]
FAIL
"""

GO_PASS = """\
ok  \texample.com/m/math\t0.002s
ok  \texample.com/m/util\t(cached)
?   \texample.com/m/cmd\t[no test files]
"""

CARGO_FAIL = """\
running 3 tests
test tests::subtracts ... ok
test tests::adds ... FAILED
test tests::divides ... ok

failures:

---- tests::adds stdout ----
thread 'tests::adds' panicked at src/lib.rs:12:9:
assertion `left == right` failed

failures:
    tests::adds

test result: FAILED. 2 passed; 1 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.00s

   Doc-tests mylib

running 1 test
test src/lib.rs - add (line 3) ... ok

test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.20s
"""


# --------------------------------------------------------------------------- #
# Parsers
# --------------------------------------------------------------------------- #
def test_pytest_summary_counts_errors_as_failures_and_names_them():
    got = T.parse_pytest(PYTEST_FAIL)
    assert got == {
        "passed": 309,
        "failed": 2,
        "failures": [
            "tests/unit/test_a.py::test_adds",
            "tests/unit/test_b.py::test_db",
        ],
    }
    assert T.parse_pytest(PYTEST_PASS_Q) == {
        "passed": 312,
        "failed": 0,
        "failures": [],
    }
    assert T.parse_pytest("no tests ran in 0.01s\n")["passed"] == 0
    assert T.parse_pytest("copied 5 files in 2.3s\n") is None


def test_pytest_verbose_names_without_a_short_summary():
    out = "tests/x.py::test_a PASSED\ntests/x.py::test_b FAILED\n1 failed, 1 passed in 0.1s"
    assert T.parse_pytest(out)["failures"] == ["tests/x.py::test_b"]


def test_vitest_counts_and_names():
    got = T.parse_js(VITEST_FAIL)
    assert got == {
        "passed": 4,
        "failed": 1,
        "failures": ["src/__tests__/b.test.ts > math > adds numbers"],
    }


def test_jest_counts_and_bullet_names_skip_console():
    got = T.parse_js(JEST_FAIL)
    assert got == {"passed": 4, "failed": 1, "failures": ["math › adds numbers"]}


def test_jest_cross_lines_when_no_bullets():
    out = "  ✕ adds numbers (3 ms)\nTests:       1 failed, 2 passed, 3 total\n"
    assert T.parse_js(out)["failures"] == ["adds numbers"]


def test_go_verbose_counts_top_level_and_names_subtests():
    got = T.parse_go(GO_FAIL_V)
    assert got["passed"] == 1 and got["failed"] == 2
    assert got["failures"] == ["TestAdd", "TestTable/zero", "TestTable"]


def test_go_without_v_counts_packages():
    assert T.parse_go(GO_PASS) == {
        "passed": 2,
        "failed": 0,
        "failures": [],
        "unit": "packages",
    }
    built = "# example.com/m/math\nmath.go:3: undefined: x\nFAIL\texample.com/m/math [build failed]\n"
    got = T.parse_go(built)
    assert got["failed"] == 1 and got["failures"] == ["example.com/m/math"]


def test_cargo_sums_every_result_line():
    assert T.parse_cargo(CARGO_FAIL) == {
        "passed": 3,
        "failed": 1,
        "failures": ["tests::adds"],
    }


def test_parse_output_sums_runners_and_strips_colour():
    both = "\x1b[32m" + PYTEST_PASS_Q + "\x1b[0m" + VITEST_FAIL
    got = T.parse_output(both)
    assert got["runners"] == ["pytest", "js"]
    assert got["passed"] == 316 and got["failed"] == 1
    assert T.parse_output("make: nothing to be done\n") is None
    assert T.parse_output(GO_PASS)["unit"] == "packages"


def test_parse_output_caps_names():
    out = "".join("FAILED t.py::test_%d - boom\n" % i for i in range(30))
    out += "30 failed in 1.0s\n"
    got = T.parse_output(out)
    assert got["failed"] == 30 and len(got["failures"]) == T.MAX_FAILURES


# --------------------------------------------------------------------------- #
# Auto-detect
# --------------------------------------------------------------------------- #
def test_detect_pytest_from_pyproject_uv_and_venv(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\naddopts='-q'\n"
    )
    assert T.detect_command(str(tmp_path)) == "python3 -m pytest -q -rfE"
    (tmp_path / "uv.lock").write_text("")
    assert T.detect_command(str(tmp_path)).startswith("uv run pytest")


def test_detect_pytest_ini_setup_cfg_tox(tmp_path):
    for name, text in (
        ("pytest.ini", "[pytest]\n"),
        ("setup.cfg", "[metadata]\nname=x\n[tool:pytest]\ntestpaths=tests\n"),
        ("tox.ini", "[tox]\n[pytest]\n"),
    ):
        d = tmp_path / name
        d.mkdir()
        (d / name).write_text(text)
        assert "pytest" in T.detect_command(str(d)), name
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "setup.cfg").write_text("[metadata]\nname=x\n")
    (plain / "pyproject.toml").write_text("[project]\nname='x'\n")
    assert T.detect_command(str(plain)) == ""


def test_detect_npm_go_cargo(tmp_path):
    npm = tmp_path / "npm"
    npm.mkdir()
    (npm / "package.json").write_text(json.dumps({"scripts": {"test": "vitest run"}}))
    assert T.detect_command(str(npm)) == "npm test"
    (npm / "package.json").write_text(
        json.dumps({"scripts": {"test": 'echo "Error: no test specified" && exit 1'}})
    )
    assert T.detect_command(str(npm)) == ""
    (npm / "go.mod").write_text("module x\n")
    assert T.detect_command(str(npm)) == "go test ./..."
    rs = tmp_path / "rs"
    rs.mkdir()
    (rs / "Cargo.toml").write_text("[package]\n")
    assert T.detect_command(str(rs)) == "cargo test"
    assert T.detect_command(str(tmp_path / "missing")) == ""


# --------------------------------------------------------------------------- #
# The kind: config, judge, run
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _store(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDFLOCK_FLIGHTS_DIR", str(tmp_path / "flights"))
    monkeypatch.setenv("MINDFLOCK_AUTOPILOT_FILE", str(tmp_path / "autopilot.json"))
    monkeypatch.setattr(D, "_JOBS", {})
    monkeypatch.setattr(D, "wake", lambda: None)
    yield


def test_kind_is_registered_with_its_spec():
    row = next(k for k in F.kinds_dto()["kinds"] if k["kind"] == "test")
    assert row["family"] == "work" and row["risk"] == [] and row["actor"] == "robot"
    assert [f["name"] for f in row["fields"]] == [
        "command",
        "expect",
        "rerun_failures",
        "timeout_minutes",
    ]
    assert "test" in F.SESSION_KINDS


def test_config_defaults_and_strictness():
    step = F.normalize_step({"kind": "test"}, 0)
    assert step["config"] == {
        "command": "",
        "expect": "pass",
        "rerun_failures": True,
        "timeout_minutes": 30,
    }
    assert step["title"] == "Run the tests"
    fail = F.normalize_step({"kind": "test", "config": {"expect": "FAIL"}}, 0)
    assert fail["config"]["expect"] == "fail"
    assert fail["title"] == "Run the tests (expect a failure)"
    with pytest.raises(F.PlanError) as err:
        F.normalize_step({"kind": "test", "config": {"expect": "maybe"}}, 0)
    assert err.value.field == "steps[0].config.expect"
    lenient = F.normalize_step(
        {"kind": "test", "config": {"expect": "maybe"}}, 0, False
    )
    assert lenient["config"]["expect"] == "pass"


def _flight(session="s", **config):
    plan = F.adhoc_plan([{"kind": "test", "config": config}], name="t")
    f = F.take_off(plan, session=session, now=NOW)
    step = f["steps"][0]
    ss = f["step_states"][step["id"]]
    ss["state"] = "running"
    return f, step, ss


def _obs(job):
    return {"session": {"present": True}, "test": job}


def _done(output, code=0):
    return {"running": False, "exit": code, "output": output}


def test_pass_is_done_with_a_receipt():
    f, step, ss = _flight()
    out = F.next_action(f, _obs(_done(PYTEST_PASS_Q)), NOW)
    assert out["action"] == "done"
    assert out["detail"]["receipt"] == "312 passed"
    assert out["detail"]["passed"] == 312 and out["detail"]["runs"] == 1
    out = F.next_action(f, _obs(_done(GO_PASS)), NOW)
    assert out["detail"]["receipt"] == "2 packages ok"
    out = F.next_action(f, _obs(_done("all good\n")), NOW)
    assert out["detail"]["receipt"] == "passed" and out["detail"]["passed"] is None


def test_running_waits_and_interrupted_runs_again():
    f, step, ss = _flight(command="make test")
    out = F.next_action(f, _obs({"running": True, "exit": None, "output": ""}), NOW)
    assert out == {"action": "wait", "note": "running: make test"}
    out = F.next_action(f, _obs(None), NOW)
    assert out["action"] == "run"
    assert out["note"] == "running the tests again (interrupted)"


def test_a_few_failures_run_once_more_then_judge_the_second():
    f, step, ss = _flight()
    out = F.next_action(f, _obs(_done(PYTEST_FAIL, 1)), NOW)
    assert out["action"] == "run" and out["phase"] == "rerun"
    assert out["detail"]["runs"] == 2 and out["detail"]["first"]["failed"] == 2
    F.apply(f, out, NOW)
    assert ss["state"] == "running" and ss["detail"]["runs"] == 2
    # The second run passes: done, and the receipt says it took a rerun.
    out = F.next_action(f, _obs(_done(PYTEST_PASS_Q)), NOW)
    assert out["action"] == "done"
    assert out["detail"]["receipt"] == "312 passed after a rerun"
    # The second run fails again: no third run.
    out = F.next_action(f, _obs(_done(PYTEST_FAIL, 1)), NOW)
    assert out["action"] == "fail"
    assert out["reason"].startswith(
        "2 failed (again on the rerun): tests/unit/test_a.py::test_adds, "
        "tests/unit/test_b.py::test_db\n"
    )
    assert len(out["reason"]) <= T.TAIL


def test_many_failures_or_rerun_off_fail_at_once():
    f, step, ss = _flight()
    many = "".join("FAILED t.py::test_%d\n" % i for i in range(4)) + "4 failed in 1s\n"
    out = F.next_action(f, _obs(_done(many, 1)), NOW)
    assert out["action"] == "fail" and out["reason"].startswith(
        "4 failed: t.py::test_0"
    )
    assert out["detail"]["failures"][:2] == ["t.py::test_0", "t.py::test_1"]
    f, step, ss = _flight("s2", rerun_failures=False)
    out = F.next_action(f, _obs(_done(VITEST_FAIL, 1)), NOW)
    assert out["action"] == "fail"
    assert out["reason"].startswith("1 failed: src/__tests__/b.test.ts > math > adds")


def test_nonzero_exit_without_a_summary_fails_with_the_tail():
    f, step, ss = _flight()
    out = F.next_action(f, _obs(_done("x" * 5000 + "\nSyntaxError: bad", 2)), NOW)
    assert out["action"] == "fail"
    assert out["reason"].startswith("exited 2\n")
    assert out["reason"].endswith("SyntaxError: bad") and len(out["reason"]) <= 2000
    assert len(out["detail"]["output"]) == T.TAIL


def test_exit_zero_but_failures_listed_fails():
    f, step, ss = _flight(rerun_failures=False)
    out = F.next_action(f, _obs(_done(CARGO_FAIL, 0)), NOW)
    assert out["action"] == "fail" and "tests::adds" in out["reason"]


def test_expect_fail():
    f, step, ss = _flight(expect="fail")
    out = F.next_action(f, _obs(_done(JEST_FAIL, 1)), NOW)
    assert out["action"] == "done"
    assert out["detail"]["receipt"] == "failed as expected"
    out = F.next_action(f, _obs(_done("collection error\n", 2)), NOW)
    assert out["action"] == "done", "a non-zero exit is a failure too"
    out = F.next_action(f, _obs(_done(PYTEST_PASS_Q, 0)), NOW)
    assert out["action"] == "fail" and out["reason"].startswith("passed unexpectedly")
    out = F.next_action(f, _obs(_done("(timed out after 30 min)", -1)), NOW)
    assert out["action"] == "fail", "a timeout is not the failure asked for"


def test_expect_fail_is_not_met_by_a_runner_that_never_ran():
    """Exit 127 (command not found) / 126 (not executable) is the shell's,
    not a test's: a "write the failing test first" stop used to pass on a
    misspelled runner. It fails, saying the command could not run."""
    f, step, ss = _flight(expect="fail", command="pytestx -q")
    for code, said in ((127, "sh: 1: pytestx: not found"), (126, "Permission denied")):
        out = F.next_action(f, _obs(_done(said + "\n", code)), NOW)
        assert out["action"] == "fail", code
        assert out["reason"].startswith(
            "the test command could not run (exit %d)" % code
        )
        assert said in out["reason"]


def test_on_fail_agent_hands_over_the_names():
    plan = F.adhoc_plan(
        [{"kind": "test", "on_fail": "agent", "config": {"rerun_failures": False}}],
        name="t",
    )
    f = F.take_off(plan, session="s", now=NOW)
    ss = f["step_states"][f["steps"][0]["id"]]
    ss["state"] = "running"
    out = F.next_action(f, _obs(_done(PYTEST_FAIL, 1)), NOW)
    assert out["action"] == "fail" and out["then"] == "agent"
    note = D._fix_note(f["steps"][0], out)
    assert "tests/unit/test_a.py::test_adds" in note


def _wait_job(f, step):
    for _ in range(200):
        job = D.job_of(f, step)
        if job and not job["running"]:
            return job
        time.sleep(0.02)
    raise AssertionError("the job never ended")


def test_run_auto_detects_and_runs_in_the_worktree(tmp_path, monkeypatch):
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / "go.mod").write_text("module x\n")
    monkeypatch.setattr(D, "worktree_of", lambda flight: str(wt))
    seen = {}

    def fake_shell(flight, step, cmd, timeout_s, folder, env_extra=None, keep=2000):
        seen.update(cmd=cmd, folder=folder, env=env_extra, keep=keep, t=timeout_s)

    monkeypatch.setattr(D, "run_shell", fake_shell)
    f, step, ss = _flight()
    out = asyncio.run(D._perform(f, {"action": "run", "note": ""}, NOW))
    assert out["detail"] == {"command": "go test ./...", "runs": 1}
    assert seen["cmd"] == "go test ./..." and seen["folder"] == str(wt)
    assert seen["env"]["CI"] == "1" and seen["keep"] == T.KEEP
    assert seen["t"] == 30 * 60.0


def test_run_without_a_command_or_worktree_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(D, "worktree_of", lambda flight: str(tmp_path))
    f, step, ss = _flight()
    out = asyncio.run(D._perform(f, {"action": "run", "note": ""}, NOW))
    assert out["action"] == "fail" and out["reason"].startswith("no test command")
    monkeypatch.setattr(D, "worktree_of", lambda flight: "")
    out = asyncio.run(D._perform(f, {"action": "run", "note": ""}, NOW))
    assert out["action"] == "fail" and "no worktree" in out["reason"]


def test_a_real_run_is_observed_and_judged(tmp_path, monkeypatch):
    monkeypatch.setattr(D, "worktree_of", lambda flight: str(tmp_path))
    monkeypatch.setattr(D, "_session_obs", lambda flight, now, agent: {"present": True})
    f, step, ss = _flight(
        command="printf 'FAILED t.py::test_x\\n1 failed, 3 passed in 0.1s\\n'; exit 1"
    )
    ran = asyncio.run(D._perform(f, {"action": "run", "note": ""}, NOW))
    F.apply(f, ran, NOW)
    _wait_job(f, step)
    obs = asyncio.run(D.observe(f, NOW))
    out = F.next_action(f, obs, NOW)
    assert out["action"] == "run" and out["phase"] == "rerun"
    # The rerun goes through the kind's runner again and counts as run 2.
    ran = asyncio.run(D._perform(f, out, NOW))
    assert ran["detail"]["runs"] == 2 and ran["detail"]["first"]["failed"] == 1
    F.apply(f, ran, NOW)
    _wait_job(f, step)
    out = F.next_action(f, asyncio.run(D.observe(f, NOW)), NOW)
    assert out["action"] == "fail"
    assert out["reason"].startswith("1 failed (again on the rerun): t.py::test_x")
    assert out["detail"]["runs"] == 2
