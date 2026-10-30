"""The ``test`` stop (docs/flights.md "Round 15", task ``test``): run the
project's tests in the session's worktree and judge them by what the runner
printed, not just its exit code.

The command is the step's own, or (empty) the one the worktree's files point
at: pytest, ``npm test``, ``go test ./...``, ``cargo test``. The job is the
``command`` kind's (:func:`flight_driver.run_shell` / :func:`job_of`), with a
long output tail kept so the runner's summary survives. The parsers are pure
functions over that output (pytest, vitest / jest, go test, cargo test); the
counts of every runner that recognises the output are summed, so a
``make test`` running two of them reads as one result.

``expect: pass`` (the default) is done on exit 0 with no failures; 1..3
failures are run once more first (``rerun_failures``), and the second run is
the verdict. ``expect: fail`` ("write the failing test first") is done when
something failed and fails the stop when everything passed.
"""

from __future__ import annotations

import asyncio
import configparser
import json
import os
import re
from typing import List, Optional

from backend.web.core import flight_kinds as _kinds
from backend.web.core import flights as _flights

#: How much of the run's output the job keeps (the summary lines are at the
#: end, the failing names a little before them).
KEEP = 20000
#: What the step's detail keeps of it, and the longest fail reason.
TAIL = 2000
MAX_FAILURES = 20
#: A run with this many failures or fewer is run once more (flakes).
RERUN_AT_MOST = 3
EXPECT = ("pass", "fail")

_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07")


def _lines(output: str) -> List[str]:
    return _ANSI.sub("", output or "").replace("\r\n", "\n").split("\n")


def _uniq(names: List[str]) -> List[str]:
    out: List[str] = []
    for name in names:
        name = name.strip()
        if name and name not in out:
            out.append(name)
    return out


# --------------------------------------------------------------------------- #
# Parsers: output -> {passed, failed, failures, unit} or None (not this runner)
# --------------------------------------------------------------------------- #
_PYTEST_WORD = (
    r"(?:passed|failed|errors?|skipped|xfailed|xpassed|deselected|warnings?|rerun)"
)
_PYTEST_SUMMARY = re.compile(
    r"^=*\s*((?:\d+ %s(?:, )?)+|no tests ran)\s+in\s+[\d.]+s\b.*$" % _PYTEST_WORD
)
_PYTEST_COUNT = re.compile(r"(\d+) (passed|failed|errors?)\b")
_PYTEST_NAME = re.compile(r"^(?:FAILED|ERROR) (\S+)")
_PYTEST_VERBOSE = re.compile(r"^(\S+::\S+) (?:FAILED|ERROR)\b")


def parse_pytest(output: str) -> Optional[dict]:
    """pytest's last summary line ("=== 2 failed, 310 passed in 12.3s ===";
    errors count as failures) and the short summary's FAILED / ERROR names."""
    lines = _lines(output)
    summary = None
    for line in reversed(lines):
        m = _PYTEST_SUMMARY.match(line.strip())
        if m:
            summary = m.group(1)
            break
    if summary is None:
        return None
    passed = failed = 0
    for n, word in _PYTEST_COUNT.findall(summary):
        if word == "passed":
            passed += int(n)
        else:
            failed += int(n)
    names = [m.group(1) for m in map(_PYTEST_NAME.match, lines) if m]
    if not names:
        names = [m.group(1) for m in map(_PYTEST_VERBOSE.match, lines) if m]
    return {"passed": passed, "failed": failed, "failures": _uniq(names)}


_JS_TESTS = re.compile(r"^\s*Tests:?\s+(.*\d.*)$")
_JS_COUNT = re.compile(r"(\d+) (passed|failed)\b")
_VITEST_FAIL = re.compile(r"^\s*FAIL\s+(\S.* > .+?)\s*$")
_JEST_BULLET = re.compile(r"^\s*●\s+(.+ › .+?)\s*$")
_JS_CROSS = re.compile(r"^\s*[×✕✗]\s+(.+?)(?:\s+\(?\d+\s?ms\)?)?\s*$")
_JS_FAIL_FILE = re.compile(r"^\s*FAIL\s+(\S+)")


def parse_js(output: str) -> Optional[dict]:
    """vitest ("Tests  1 failed | 4 passed (5)") and jest ("Tests:  1 failed,
    4 passed, 5 total"); names from vitest's "FAIL  file > suite > test",
    jest's "● suite › test", else the ×/✕ lines, else the FAIL files."""
    lines = _lines(output)
    counts = None
    for line in reversed(lines):
        m = _JS_TESTS.match(line)
        if m:
            counts = m.group(1)
            break
    if counts is None:
        return None
    passed = failed = 0
    for n, word in _JS_COUNT.findall(counts):
        if word == "passed":
            passed += int(n)
        else:
            failed += int(n)
    names: List[str] = []
    for pattern in (_VITEST_FAIL, _JEST_BULLET, _JS_CROSS, _JS_FAIL_FILE):
        names = [m.group(1) for m in map(pattern.match, lines) if m]
        if names:
            break
    return {"passed": passed, "failed": failed, "failures": _uniq(names)}


_GO_RESULT = re.compile(r"^\s*--- (PASS|FAIL): (\S+)")
_GO_PACKAGE = re.compile(r"^(ok|FAIL)[ ]*\t(\S+)")


def parse_go(output: str) -> Optional[dict]:
    """go test: ``--- FAIL: TestName`` (top-level ones are counted, subtests
    named), ``--- PASS:`` under -v; without -v the ``ok`` / ``FAIL`` package
    lines are the counts (``unit: packages``)."""
    lines = _lines(output)
    tests = [
        (m.group(1), m.group(2), not line[:1].isspace())
        for line in lines
        for m in [_GO_RESULT.match(line)]
        if m
    ]
    packages = [(m.group(1), m.group(2)) for m in map(_GO_PACKAGE.match, lines) if m]
    if not tests and not packages:
        return None
    names = [name for state, name, _ in tests if state == "FAIL"]
    failed = sum(1 for state, _, top in tests if state == "FAIL" and top)
    passed = sum(1 for state, _, top in tests if state == "PASS" and top)
    if passed or failed:
        return {"passed": passed, "failed": failed, "failures": _uniq(names)}
    bad = [name for state, name in packages if state == "FAIL"]
    return {
        "passed": sum(1 for state, _ in packages if state == "ok"),
        "failed": len(bad),
        "failures": _uniq(names or bad),
        "unit": "packages",
    }


_CARGO_RESULT = re.compile(r"^test result: \w+\. (\d+) passed; (\d+) failed\b")
_CARGO_NAME = re.compile(r"^test (\S+) \.\.\. FAILED\b")


def parse_cargo(output: str) -> Optional[dict]:
    """cargo test: every "test result: ok. N passed; M failed; …" line summed
    (unit tests, integration tests, doc-tests), names from "test x ... FAILED"."""
    lines = _lines(output)
    results = [m for m in map(_CARGO_RESULT.match, lines) if m]
    if not results:
        return None
    return {
        "passed": sum(int(m.group(1)) for m in results),
        "failed": sum(int(m.group(2)) for m in results),
        "failures": _uniq([m.group(1) for m in map(_CARGO_NAME.match, lines) if m]),
    }


PARSERS = (
    ("pytest", parse_pytest),
    ("js", parse_js),
    ("go", parse_go),
    ("cargo", parse_cargo),
)


def parse_output(output: str) -> Optional[dict]:
    """The counts of every runner that recognises ``output``, summed:
    ``{passed, failed, failures (≤ 20), runners, unit}``; None when no runner
    printed a summary (the exit code alone then decides)."""
    out = None
    for runner, parse in PARSERS:
        got = parse(output)
        if got is None:
            continue
        if out is None:
            out = {"passed": 0, "failed": 0, "failures": [], "runners": [], "unit": ""}
        out["passed"] += got["passed"]
        out["failed"] += got["failed"]
        out["failures"] = _uniq(out["failures"] + got["failures"])
        out["runners"].append(runner)
        if got.get("unit") and not out["unit"]:
            out["unit"] = got["unit"]
    if out is not None:
        out["failures"] = out["failures"][:MAX_FAILURES]
        out["unit"] = out["unit"] or "tests"
    return out


# --------------------------------------------------------------------------- #
# Auto-detect: the worktree's files -> a command, "" when nothing is found
# --------------------------------------------------------------------------- #
def _read(path: str, limit: int = 1_000_000) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read(limit)
    except OSError:
        return ""


def _has_pytest(folder: str) -> bool:
    if os.path.isfile(os.path.join(folder, "pytest.ini")):
        return True
    if re.search(
        r"^\s*\[tool\.pytest(\.ini_options)?\]",
        _read(os.path.join(folder, "pyproject.toml")),
        re.M,
    ):
        return True
    for name, section in (("setup.cfg", "tool:pytest"), ("tox.ini", "pytest")):
        text = _read(os.path.join(folder, name))
        if not text:
            continue
        parser = configparser.ConfigParser(interpolation=None, strict=False)
        try:
            parser.read_string(text)
        except configparser.Error:
            continue
        if parser.has_section(section):
            return True
    return False


def _pytest_command(folder: str) -> str:
    if os.path.isfile(os.path.join(folder, "uv.lock")):
        return "uv run pytest -q -rfE"
    venv = os.path.join(folder, ".venv", "bin", "python")
    if os.access(venv, os.X_OK):
        return ".venv/bin/python -m pytest -q -rfE"
    return "python3 -m pytest -q -rfE"


def _npm_test(folder: str) -> bool:
    try:
        pkg = json.loads(_read(os.path.join(folder, "package.json")) or "{}")
    except ValueError:
        return False
    scripts = pkg.get("scripts") if isinstance(pkg, dict) else None
    test = scripts.get("test") if isinstance(scripts, dict) else None
    # npm init's placeholder only says there are none.
    return bool(test) and "no test specified" not in str(test)


def detect_command(folder: str) -> str:
    """The test command ``folder``'s files point at: pytest when
    pytest.ini / pyproject.toml ``[tool.pytest…]`` / setup.cfg ``[tool:pytest]``
    / tox.ini ``[pytest]`` (run through uv when there is a uv.lock, the
    ``.venv`` when there is one), ``npm test`` when package.json has a test
    script, ``go test ./...`` with a go.mod, ``cargo test`` with a Cargo.toml;
    "" when none."""
    if not folder or not os.path.isdir(folder):
        return ""
    if _has_pytest(folder):
        return _pytest_command(folder)
    if _npm_test(folder):
        return "npm test"
    if os.path.isfile(os.path.join(folder, "go.mod")):
        return "go test ./..."
    if os.path.isfile(os.path.join(folder, "Cargo.toml")):
        return "cargo test"
    return ""


# --------------------------------------------------------------------------- #
# The kind
# --------------------------------------------------------------------------- #
def _config(raw: dict, strict: bool, where: str) -> dict:
    expect = _flights._text(raw.get("expect"), 10).lower() or "pass"
    if expect not in EXPECT:
        if strict:
            raise _flights.PlanError("expect is pass or fail", where + ".expect")
        expect = "pass"
    return {
        "command": _flights._text(raw.get("command"), 4000),
        "expect": expect,
        "rerun_failures": _flights._bool(raw.get("rerun_failures"), True),
        "timeout_minutes": _flights._int(raw.get("timeout_minutes"), 30, 1, 24 * 60),
    }


def _title(config: dict) -> str:
    if config.get("expect") == "fail":
        return "Run the tests (expect a failure)"
    return "Run the tests"


def receipt_of(counts: Optional[dict], expect: str, runs: int) -> str:
    """The box's line once the stop has flown: "312 passed", "2 failed",
    "failed as expected"."""
    if expect == "fail":
        return "failed as expected"
    if counts is None:
        return "passed"
    word = "packages ok" if counts.get("unit") == "packages" else "passed"
    out = "%d %s" % (counts["passed"], word)
    return out + " after a rerun" if runs > 1 else out


def _tail(output: str) -> str:
    return (output or "")[-TAIL:]


def _reason(head: str, counts: Optional[dict], output: str) -> str:
    """A fail reason an agent can act on: the failing names first, then the
    output's tail, ≤ :data:`TAIL` chars in all."""
    names = (counts or {}).get("failures") or []
    if names:
        head = "%s: %s" % (head, ", ".join(names))
    tail = (output or "").strip()
    room = TAIL - len(head) - 1
    if tail and room > 40:
        return (head + "\n" + tail[-room:])[:TAIL]
    return head[:TAIL]


def _judge(flight, step, ss, obs, now) -> dict:
    job = obs.get("test")
    cfg = step["config"]
    det = ss["detail"]
    if not job:
        return {"action": "run", "note": "running the tests again (interrupted)"}
    if job.get("failed"):
        return {"action": "fail", "reason": str(job["failed"])}
    if job.get("running"):
        cmd = str(det.get("command") or cfg.get("command") or "the tests")
        return _flights._wait("running: %s" % cmd.splitlines()[0][:80])
    output = str(job.get("output") or "")
    code = job.get("exit")
    counts = parse_output(output)
    runs = int(det.get("runs") or 1)
    failed = counts["failed"] if counts else 0
    detail = {
        "passed": counts["passed"] if counts else None,
        "failed": failed if counts else None,
        "failures": list(counts["failures"]) if counts else [],
        "runs": runs,
        "exit": code,
        "output": _tail(output),
    }
    if cfg.get("expect") == "fail":
        # A timeout or a command that could not start (exit -1) is not the
        # failure a "write the failing test first" stop asks for.
        if failed or (code not in (0, None, -1)):
            detail["receipt"] = receipt_of(counts, "fail", runs)
            return {"action": "done", "note": "failed as expected", "detail": detail}
        if code == -1:
            return {
                "action": "fail",
                "reason": _reason("the tests did not finish", counts, output),
                "detail": detail,
            }
        return {
            "action": "fail",
            "reason": _reason("passed unexpectedly", counts, output),
            "detail": detail,
        }
    if code == 0 and not failed:
        detail["receipt"] = receipt_of(counts, "pass", runs)
        return {"action": "done", "note": detail["receipt"], "detail": detail}
    if (
        cfg.get("rerun_failures", True)
        and runs < 2
        and 1 <= failed <= RERUN_AT_MOST
        and code != -1
    ):
        return {
            "action": "run",
            "phase": "rerun",
            "note": "%d failed: running the tests once more" % failed,
            "detail": {
                "runs": 2,
                "first": {k: detail[k] for k in ("passed", "failed", "failures")},
            },
        }
    if failed:
        head = "%d failed" % failed + (" (again on the rerun)" if runs > 1 else "")
    elif code == -1:
        head = "the tests did not finish"
    else:
        head = "exited %s" % code
    return {"action": "fail", "reason": _reason(head, counts, output), "detail": detail}


async def _run(flight, step, action, now) -> dict:
    from backend.web.core import flight_driver as D

    folder = await asyncio.to_thread(D.worktree_of, flight)
    if not folder:
        raise _kinds.StepFailed("the session has no worktree to test in")
    cfg = step["config"]
    cmd = str(cfg.get("command") or "").strip()
    env = {"NO_COLOR": "1", "FORCE_COLOR": "0"}
    if not cmd:
        cmd = await asyncio.to_thread(detect_command, folder)
        if not cmd:
            raise _kinds.StepFailed(
                "no test command: no pytest config, package.json test script, "
                "go.mod or Cargo.toml in %s; set the stop's command" % folder
            )
        # Watch-mode runners (vitest, jest) run once under CI.
        env["CI"] = "1"
    D.run_shell(
        flight,
        step,
        cmd,
        float(cfg.get("timeout_minutes") or 30) * 60.0,
        folder,
        env_extra=env,
        keep=KEEP,
    )
    runs = 2 if action.get("phase") == "rerun" else 1
    return dict(action, detail=dict(action.get("detail") or {}, command=cmd, runs=runs))


async def _observe(flight, step, ss, sess, now):
    from backend.web.core import flight_driver as D

    return D.job_of(flight, step)


KIND = _kinds.register(
    _kinds.Kind(
        kind="test",
        spec={
            "label": "Test",
            "actor": "robot",
            "family": "work",
            "risk": [],
            "summary": (
                "Run the project's tests in the session's worktree; failures "
                "fail the stop (or, expecting a failure, a pass does)."
            ),
            "default_title": "Run the tests",
            "fields": [
                _flights._f(
                    "command",
                    "textarea",
                    "Command (empty = find the project's tests)",
                    default="",
                    placeholder="uv run pytest -q",
                ),
                _flights._f(
                    "expect", "select", "Expect", default="pass", options=EXPECT
                ),
                _flights._f(
                    "rerun_failures",
                    "bool",
                    "Run once more when 1–3 tests fail",
                    default=True,
                ),
                _flights._f(
                    "timeout_minutes", "int", "Give up after (minutes)", default=30
                ),
            ],
        },
        config=_config,
        judge=_judge,
        run=_run,
        observe=_observe,
        default_title=_title,
    )
)
