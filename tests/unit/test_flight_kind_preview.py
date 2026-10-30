"""The ``preview`` stop (``backend.web.core.flight_kinds.preview``,
docs/flights.md "Round 15"): the URL read from the command's output (last
line or a MINDFLOCK_PREVIEW_URL= line), a failing / URL-less command, a
restart mid-deploy, the receipt, ``{preview}``'s source and ``stop_all`` —
against a tmp store and fake sessions, never the real engine."""

import asyncio
import time

import pytest

from backend.web.core import flight_driver as D
from backend.web.core import flights as F
from backend.web.core.flight_kinds import preview as P

NOW = time.time()


@pytest.fixture(autouse=True)
def _store(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDFLOCK_FLIGHTS_DIR", str(tmp_path / "flights"))
    monkeypatch.setenv("MINDFLOCK_AUTOPILOT_FILE", str(tmp_path / "autopilot.json"))
    monkeypatch.setattr(D, "_JOBS", {})
    monkeypatch.setattr(D, "wake", lambda: None)
    yield


def test_find_url_last_line_and_marker():
    assert P.find_url("building…\ndone\nhttps://app-x1.vercel.app\n\n") == (
        "https://app-x1.vercel.app"
    )
    # A line ending in the URL (netlify's "Website draft URL: …").
    assert (
        P.find_url("Website draft URL: https://abc--site.netlify.app.\n")
        == "https://abc--site.netlify.app"
    )
    # The marker wins over the last line, wherever it is.
    out = "MINDFLOCK_PREVIEW_URL=http://localhost:5173/x?y=1\nhttps://other.dev\n"
    assert P.find_url(out) == "http://localhost:5173/x?y=1"
    # Colour codes are stripped.
    assert P.find_url("\x1b[32mhttps://green.dev\x1b[0m\n") == "https://green.dev"


def test_find_url_refuses_what_is_not_a_url():
    assert P.find_url("") == ""
    assert P.find_url("https://ok.dev\nall done\n") == "", "the LAST line counts"
    assert P.find_url("ftp://files.dev\n") == ""
    assert P.find_url("https://\n") == ""
    assert P.find_url("MINDFLOCK_PREVIEW_URL=not-a-url\nhttps://x.dev\n") == ""
    assert P.valid_url("http://host:99999/") == ""


def test_host_of_is_the_receipt():
    assert P.host_of("https://app-x1.vercel.app/path?q=1") == "app-x1.vercel.app"
    assert P.host_of("http://localhost:5173/") == "localhost:5173"


def test_kind_is_registered_with_its_spec():
    row = next(k for k in F.kinds_dto()["kinds"] if k["kind"] == "preview")
    assert row["family"] == "outside" and row["risk"] == ["remote"]
    assert row["actor"] == "robot"
    assert [f["name"] for f in row["fields"]] == [
        "command",
        "timeout_minutes",
        "stop_command",
    ]
    assert "preview" in F.SESSION_KINDS


def test_config_requires_a_command_when_strict():
    with pytest.raises(F.PlanError) as err:
        F.normalize_step({"kind": "preview"}, 0)
    assert err.value.field == "steps[0].config.command"
    lenient = F.normalize_step({"kind": "preview"}, 0, False)
    assert lenient["config"] == {
        "command": "",
        "timeout_minutes": 20,
        "stop_command": "",
    }
    step = F.normalize_step(
        {"kind": "preview", "config": {"command": "make preview", "stop_command": "x"}},
        0,
    )
    assert step["config"]["command"] == "make preview"
    assert step["title"] == "Deploy a preview"


def _flight(session="s", **config):
    config.setdefault("command", "make preview")
    plan = F.adhoc_plan([{"kind": "preview", "config": config}], name="t")
    f = F.take_off(plan, session=session, now=NOW)
    step = f["steps"][0]
    ss = f["step_states"][step["id"]]
    ss["state"] = "running"
    return f, step, ss


def _obs(job):
    return {"session": {"present": True}, "preview": job}


def _done(output, code=0):
    return {"running": False, "exit": code, "output": output}


def test_done_with_the_url_and_its_host():
    f, step, ss = _flight()
    out = F.next_action(f, _obs(_done("deploying\nhttps://pr-12.example.dev/\n")), NOW)
    assert out["action"] == "done"
    assert out["detail"]["url"] == "https://pr-12.example.dev/"
    assert out["detail"]["receipt"] == "pr-12.example.dev"
    assert out["detail"]["output"].endswith("https://pr-12.example.dev/\n")
    F.apply(f, out, NOW)
    assert P.latest_url(f) == "https://pr-12.example.dev/"


def test_running_waits_and_interrupted_runs_again():
    f, step, ss = _flight()
    out = F.next_action(f, _obs({"running": True, "exit": None, "output": ""}), NOW)
    assert out == {"action": "wait", "note": "deploying the preview"}
    out = F.next_action(f, _obs(None), NOW)
    assert out["action"] == "run"
    assert out["note"] == "starting the preview again (interrupted)"


def test_a_failing_command_or_no_url_fails_with_the_tail():
    f, step, ss = _flight()
    out = F.next_action(f, _obs(_done("x" * 5000 + "\nError: no token", 1)), NOW)
    assert out["action"] == "fail"
    assert out["reason"].startswith("the preview command exited 1\n")
    assert out["reason"].endswith("Error: no token") and len(out["reason"]) <= P.TAIL
    assert len(out["detail"]["output"]) == P.TAIL
    out = F.next_action(f, _obs(_done("(timed out after 20 min)", -1)), NOW)
    assert out["reason"].startswith("the preview command did not finish")
    out = F.next_action(f, _obs(_done("deployed!\n", 0)), NOW)
    assert out["action"] == "fail"
    assert out["reason"].startswith("the preview command printed no URL")
    assert out["reason"].endswith("deployed!")


def test_latest_url_is_the_last_done_preview():
    plan = F.adhoc_plan(
        [
            {"kind": "preview", "config": {"command": "a"}},
            {"kind": "preview", "config": {"command": "b"}},
        ],
        name="t",
    )
    f = F.take_off(plan, session="s", now=NOW)
    assert P.latest_url(f) == ""
    a, b = (f["step_states"][s["id"]] for s in f["steps"])
    a.update(state="done", detail={"url": "https://a.dev"})
    b.update(state="failed", detail={"url": "https://b.dev"})
    assert P.latest_url(f) == "https://a.dev"
    b["state"] = "done"
    assert P.latest_url(f) == "https://b.dev"


def test_run_starts_the_command_in_the_worktree_with_the_branch(tmp_path, monkeypatch):
    monkeypatch.setattr(
        D,
        "_session_obs",
        lambda flight, now, agent: {"folder": str(tmp_path), "branch": "feat/x"},
    )
    seen = {}

    def fake_shell(flight, step, cmd, timeout_s, folder, env_extra=None, keep=2000):
        seen.update(cmd=cmd, folder=folder, env=env_extra, keep=keep, t=timeout_s)

    monkeypatch.setattr(D, "run_shell", fake_shell)
    f, step, ss = _flight(timeout_minutes=5)
    out = asyncio.run(D._perform(f, {"action": "run", "note": ""}, NOW))
    assert out["detail"] == {"command": "make preview"}
    assert seen == {
        "cmd": "make preview",
        "folder": str(tmp_path),
        "env": {"MINDFLOCK_BRANCH": "feat/x"},
        "keep": P.KEEP,
        "t": 300.0,
    }
    monkeypatch.setattr(D, "_session_obs", lambda flight, now, agent: {})
    out = asyncio.run(D._perform(f, {"action": "run", "note": ""}, NOW))
    assert out["action"] == "fail" and "no worktree" in out["reason"]


def _wait_job(f, step):
    for _ in range(200):
        job = D.job_of(f, step)
        if job and not job["running"]:
            return job
        time.sleep(0.02)
    raise AssertionError("the job never ended")


def test_a_real_run_is_observed_and_judged(tmp_path, monkeypatch):
    monkeypatch.setattr(
        D,
        "_session_obs",
        lambda flight, now, agent: {
            "present": True,
            "folder": str(tmp_path),
            "branch": "feat/y",
        },
    )
    f, step, ss = _flight(
        command='echo "branch $MINDFLOCK_BRANCH"; '
        'echo "MINDFLOCK_PREVIEW_URL=https://$(echo $MINDFLOCK_BRANCH | tr / -).dev"; '
        "echo bye"
    )
    F.apply(f, asyncio.run(D._perform(f, {"action": "run", "note": ""}, NOW)), NOW)
    _wait_job(f, step)
    out = F.next_action(f, asyncio.run(D.observe(f, NOW)), NOW)
    assert out["action"] == "done", out
    assert out["detail"]["url"] == "https://feat-y.dev"
    assert out["detail"]["receipt"] == "feat-y.dev"


def test_stop_all_runs_each_done_previews_stop_command(tmp_path, monkeypatch):
    monkeypatch.setattr(
        D,
        "_session_obs",
        lambda flight, now, agent: {"folder": str(tmp_path), "branch": "b"},
    )
    plan = F.adhoc_plan(
        [
            {
                "kind": "preview",
                "config": {
                    "command": "a",
                    "stop_command": 'echo "$MINDFLOCK_PREVIEW_URL $MINDFLOCK_BRANCH" >> stopped.txt',
                },
            },
            {"kind": "preview", "config": {"command": "b", "stop_command": "exit 3"}},
            {"kind": "preview", "config": {"command": "c", "stop_command": "touch c"}},
            {"kind": "preview", "config": {"command": "d"}},
        ],
        name="t",
    )
    f = F.take_off(plan, session="s", now=NOW)
    states = [f["step_states"][s["id"]] for s in f["steps"]]
    states[0].update(state="done", detail={"url": "https://a.dev"})
    states[1].update(state="done", detail={"url": "https://b.dev"})
    states[2]["state"] = "failed"  # never up: nothing to take down
    states[3].update(state="done", detail={"url": "https://d.dev"})
    got = P.stop_all(f)
    assert [(r["step"], r["exit"]) for r in got] == [
        (f["steps"][0]["id"], 0),
        (f["steps"][1]["id"], 3),
    ]
    assert (tmp_path / "stopped.txt").read_text() == "https://a.dev b\n"
    assert not (tmp_path / "c").exists()
    assert P.stop_all(None) == []


def test_stop_all_survives_a_gone_session(tmp_path, monkeypatch):
    def boom(flight, now, agent):
        raise RuntimeError("no server")

    monkeypatch.setattr(D, "_session_obs", boom)
    plan = F.adhoc_plan(
        [{"kind": "preview", "config": {"command": "a", "stop_command": "true"}}],
        name="t",
    )
    f = F.take_off(plan, session="s", now=NOW)
    f["step_states"][f["steps"][0]["id"]]["state"] = "done"
    assert [r["exit"] for r in P.stop_all(f)] == [0]
