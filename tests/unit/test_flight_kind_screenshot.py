"""The ``screenshot`` stop (``backend.web.core.flight_kinds.screenshot``,
docs/flights.md "Round 15"): finding a browser, the shell script it runs,
``{preview}`` in the URLs, the shots listed from the store, the judge, the
route that serves them (and refuses everything else), the PR comment's
skips — against a tmp store and a fake browser, never a real Chrome."""

import asyncio
import os
import stat
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.web.core import flight_driver as D
from backend.web.core import flights as F
from backend.web.core.flight_kinds import screenshot as S

NOW = time.time()
PNG = b"\x89PNG\r\n\x1a\nfake"


@pytest.fixture(autouse=True)
def _store(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDFLOCK_FLIGHTS_DIR", str(tmp_path / "flights"))
    monkeypatch.setenv("MINDFLOCK_AUTOPILOT_FILE", str(tmp_path / "autopilot.json"))
    monkeypatch.delenv("MINDFLOCK_CHROME", raising=False)
    monkeypatch.setattr(D, "_JOBS", {})
    monkeypatch.setattr(D, "wake", lambda: None)
    monkeypatch.setattr(S, "_COMMENTED", {})
    yield


def _which(found):
    return lambda name: ("/usr/bin/" + name) if name in found else None


def test_find_browser_order_and_env(tmp_path, monkeypatch):
    monkeypatch.setattr(S.shutil, "which", _which({"chromium", "chromium-browser"}))
    assert S.find_browser() == "/usr/bin/chromium"
    monkeypatch.setattr(S.shutil, "which", _which({"google-chrome-stable", "chromium"}))
    assert S.find_browser() == "/usr/bin/google-chrome-stable"
    monkeypatch.setattr(S.shutil, "which", _which(set()))
    assert S.find_browser() == ""
    # MINDFLOCK_CHROME: an executable path, or a name on PATH.
    exe = tmp_path / "my-chrome"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("MINDFLOCK_CHROME", str(exe))
    assert S.find_browser() == str(exe)
    monkeypatch.setenv("MINDFLOCK_CHROME", "brave")
    monkeypatch.setattr(S.shutil, "which", _which({"brave", "chromium"}))
    assert S.find_browser() == "/usr/bin/brave"
    monkeypatch.setenv("MINDFLOCK_CHROME", "nope")
    assert S.find_browser() == "/usr/bin/chromium", "a bad override falls back"


def test_widths_and_shot_plan():
    assert S.parse_widths("1280, 390") == [1280, 390]
    assert S.parse_widths("") == [1280]
    assert S.parse_widths("50 99999 1280 1280") == [200, 4000, 1280]
    pairs = S.plan_shots(["https://a.dev", "https://b.dev"], [1280, 390])
    assert pairs == [
        {"n": 1, "url": "https://a.dev", "width": 1280},
        {"n": 2, "url": "https://a.dev", "width": 390},
        {"n": 3, "url": "https://b.dev", "width": 1280},
        {"n": 4, "url": "https://b.dev", "width": 390},
    ]
    many = S.plan_shots(["https://x.dev/%d" % i for i in range(30)], [1, 2])
    assert len(many) == S.MAX_SHOTS


def test_build_script_quotes_and_uses_a_throwaway_profile():
    pairs = S.plan_shots(["https://a.dev/?q=1&r='x'"], [390])
    script = S.build_script("/opt/my chrome", "/tmp/shots", pairs)
    assert 'profile="$(mktemp -d)"' in script
    assert "trap 'rm -rf \"$profile\"' EXIT" in script
    line = next(l for l in script.splitlines() if l.startswith("'/opt/my chrome'"))
    assert "--headless=new" in line and "--disable-gpu" in line
    assert "--hide-scrollbars" in line and '--user-data-dir="$profile"' in line
    assert "--window-size=390,900" in line
    assert "--screenshot=/tmp/shots/1.png" in line
    assert "'https://a.dev/?q=1&r='\"'\"'x'\"'\"''" in line
    assert line.endswith("|| echo 'shot 1 failed'")


def test_kind_is_registered_with_its_spec_and_route():
    row = next(k for k in F.kinds_dto()["kinds"] if k["kind"] == "screenshot")
    assert row["family"] == "outside" and row["risk"] == [] and row["actor"] == "robot"
    assert [f["name"] for f in row["fields"]] == [
        "urls",
        "widths",
        "timeout_minutes",
        "comment_pr",
    ]
    assert "screenshot" not in F.SESSION_KINDS
    assert S.KIND.router is not None


def test_config_defaults():
    step = F.normalize_step({"kind": "screenshot"}, 0)
    assert step["config"] == {
        "urls": "{preview}",
        "widths": "1280, 390",
        "timeout_minutes": 10,
        "comment_pr": False,
    }
    assert step["title"] == "Screenshots"


def _flight(session="s", **config):
    plan = F.adhoc_plan(
        [
            {"kind": "preview", "config": {"command": "x"}},
            {"kind": "screenshot", "config": config},
        ],
        name="t",
    )
    f = F.take_off(plan, session=session, now=NOW)
    pre, step = f["steps"]
    f["step_states"][pre["id"]].update(
        state="done", detail={"url": "https://pr-7.example.dev"}
    )
    ss = f["step_states"][step["id"]]
    ss["state"] = "running"
    f["cursor"] = 1
    return f, step, ss


def test_fill_urls_substitutes_preview_and_drops_non_urls():
    f, step, ss = _flight()
    got = S.fill_urls("{preview}\n\n{preview}/settings\nnot a url\n{branch}", f)
    assert got == ["https://pr-7.example.dev", "https://pr-7.example.dev/settings"]
    f["step_states"][f["steps"][0]["id"]]["state"] = "failed"
    assert S.fill_urls("{preview}/x", f) == []


def _write_shots(fid, sid, names):
    folder = S.shots_dir(fid, sid)
    os.makedirs(folder, exist_ok=True)
    for n in names:
        with open(os.path.join(folder, n), "wb") as fh:
            fh.write(PNG)
    return folder


def test_list_shots_from_the_store():
    pairs = S.plan_shots(["https://a.dev"], [1280, 390])
    folder = _write_shots("f_1", "s2", ["10.png", "2.png", "1.png", "notes.txt"])
    open(os.path.join(folder, "3.png"), "wb").close()  # empty: a failed shot
    got = S.list_shots("f_1", "s2", pairs)
    assert got == [
        {
            "name": "1.png",
            "url": "/api/flights/f_1/shots/s2/1.png",
            "width": 1280,
            "page": "https://a.dev",
        },
        {
            "name": "2.png",
            "url": "/api/flights/f_1/shots/s2/2.png",
            "width": 390,
            "page": "https://a.dev",
        },
        {
            "name": "10.png",
            "url": "/api/flights/f_1/shots/s2/10.png",
            "width": None,
            "page": "",
        },
    ]
    assert S.list_shots("f_none", "s1", pairs) == []


def test_shots_dir_never_walks_out():
    base = os.path.join(F.flights_dir(), "shots")
    assert S.shots_dir("../..", "a/b") == os.path.join(base, "_____", "a_b")


def test_route_serves_pngs_and_refuses_everything_else():
    app = FastAPI()
    app.include_router(S.KIND.router)
    client = TestClient(app)
    _write_shots("f_1", "s2", ["1.png"])
    with open(os.path.join(S.shots_dir("f_1", "s2"), "x.txt"), "w") as fh:
        fh.write("secret")
    r = client.get("/api/flights/f_1/shots/s2/1.png")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png" and r.content == PNG
    for bad in (
        "/api/flights/f_1/shots/s2/2.png",  # not there
        "/api/flights/f_1/shots/s2/x.txt",  # not a shot's name
        "/api/flights/f_1/shots/s2/1.png.bak",
        "/api/flights/f_1/shots/s2/..%2Fx.txt",
        "/api/flights/f.1/shots/s2/1.png",  # an id outside [A-Za-z0-9_-]
        "/api/flights/f_1/shots/s%2E%2E/1.png",
    ):
        assert client.get(bad).status_code == 404, bad


def _obs(job, shots=(), comment=""):
    return {
        "session": {"present": True},
        "screenshot": {"job": job, "shots": list(shots), "comment": comment},
    }


def test_judge():
    f, step, ss = _flight()
    out = F.next_action(f, _obs(None), NOW)
    assert out["action"] == "run"
    assert out["note"] == "taking the screenshots again (interrupted)"
    out = F.next_action(f, _obs({"running": True, "exit": None, "output": ""}), NOW)
    assert out == {"action": "wait", "note": "taking screenshots"}
    done = {"running": False, "exit": 0, "output": "shot 1 failed\n"}
    out = F.next_action(f, _obs(done), NOW)
    assert out["action"] == "fail"
    assert out["reason"] == "no screenshots were taken\nshot 1 failed"
    shots = [{"name": "1.png", "url": "/u/1.png", "width": 1280, "page": "p"}]
    out = F.next_action(f, _obs(done, shots, "skipped: the session has no PR"), NOW)
    assert out["action"] == "done" and out["detail"]["receipt"] == "1 shot"
    assert out["detail"]["shots"] == shots
    assert out["detail"]["comment"] == "skipped: the session has no PR"
    out = F.next_action(f, _obs(done, shots * 4), NOW)
    assert out["detail"]["receipt"] == "4 shots" and "comment" not in out["detail"]


def test_run_without_a_browser_or_a_url_fails(monkeypatch):
    monkeypatch.setattr(S.shutil, "which", _which(set()))
    f, step, ss = _flight()
    out = asyncio.run(D._perform(f, {"action": "run", "note": ""}, NOW))
    assert out["action"] == "fail" and out["reason"].startswith("no browser")
    monkeypatch.setattr(S.shutil, "which", _which({"chromium"}))
    f, step, ss = _flight("s2", urls="{preview}")
    f["step_states"][f["steps"][0]["id"]]["state"] = "failed"
    out = asyncio.run(D._perform(f, {"action": "run", "note": ""}, NOW))
    assert out["action"] == "fail" and out["reason"].startswith("no URL to shoot")


def test_run_builds_the_job(monkeypatch):
    monkeypatch.setattr(S.shutil, "which", _which({"chromium"}))
    seen = {}

    def fake_shell(flight, step, cmd, timeout_s, folder, env_extra=None, keep=2000):
        seen.update(cmd=cmd, folder=folder, t=timeout_s)

    monkeypatch.setattr(D, "run_shell", fake_shell)
    f, step, ss = _flight(urls="{preview}\n{preview}/a", widths="800")
    stale = _write_shots(f["id"], step["id"], ["1.png", "9.png"])
    out = asyncio.run(D._perform(f, {"action": "run", "note": ""}, NOW))
    assert out["action"] == "run"
    assert out["detail"]["browser"] == "/usr/bin/chromium"
    assert [(p["n"], p["url"], p["width"]) for p in out["detail"]["pairs"]] == [
        (1, "https://pr-7.example.dev", 800),
        (2, "https://pr-7.example.dev/a", 800),
    ]
    assert seen["folder"] == stale and seen["t"] == 600.0
    assert os.listdir(stale) == [], "an earlier run's shots are cleared"
    assert "--screenshot=%s/2.png" % stale in seen["cmd"]


FAKE_CHROME = """#!/bin/sh
# A stand-in for chrome --headless: write a PNG where --screenshot= says,
# and fail for a URL containing "broken".
out=""
for a in "$@"; do
  case "$a" in
    --screenshot=*) out="${a#--screenshot=}" ;;
    *broken*) echo "net::ERR_NAME_NOT_RESOLVED"; exit 1 ;;
  esac
done
printf 'PNG' > "$out"
echo "written to $out"
"""


def _wait_job(f, step):
    for _ in range(300):
        job = D.job_of(f, step)
        if job and not job["running"]:
            return job
        time.sleep(0.02)
    raise AssertionError("the job never ended")


def test_a_real_run_with_a_fake_browser(tmp_path, monkeypatch):
    exe = tmp_path / "chrome"
    exe.write_text(FAKE_CHROME)
    exe.chmod(0o755)
    monkeypatch.setenv("MINDFLOCK_CHROME", str(exe))
    monkeypatch.setattr(
        D,
        "_session_obs",
        lambda flight, now, agent: {"present": True, "folder": "", "pr_url": ""},
    )
    f, step, ss = _flight(
        urls="{preview}\nhttps://broken.dev", widths="1280, 390", comment_pr=True
    )
    F.apply(f, asyncio.run(D._perform(f, {"action": "run", "note": ""}, NOW)), NOW)
    job = _wait_job(f, step)
    assert "shot 3 failed" in job["output"] and "shot 4 failed" in job["output"]
    out = F.next_action(f, asyncio.run(D.observe(f, NOW)), NOW)
    assert out["action"] == "done", out
    assert out["detail"]["receipt"] == "2 shots"
    assert [(s["name"], s["width"]) for s in out["detail"]["shots"]] == [
        ("1.png", 1280),
        ("2.png", 390),
    ]
    assert out["detail"]["comment"] == "skipped: the session has no PR"


def test_comment_posts_through_the_rest_seam(monkeypatch):
    from backend.web.core import github_pr as G

    calls = []

    async def token():
        return "tok"

    async def request(method, path, *, token, params=None, body=None):
        calls.append((method, path, body))
        return 201, {}

    monkeypatch.setattr(
        G, "repo_ref", lambda wt: G.parse_remote("git@github.com:o/r.git")
    )
    monkeypatch.setattr(G, "api_token", token)
    monkeypatch.setattr(G, "_request", request)
    shots = [{"name": "1.png", "width": 390, "page": "https://a.dev"}]
    text = S.comment_text(shots)
    assert text == "Screenshots from MindFlock (1):\n- 1.png — https://a.dev at 390px"
    got = asyncio.run(S.post_comment("/wt", "https://github.com/o/r/pull/12", text))
    assert got == "posted"
    assert calls == [("POST", "/repos/o/r/issues/12/comments", {"body": text})]

    async def no_token():
        return ""

    monkeypatch.setattr(G, "api_token", no_token)
    got = asyncio.run(S.post_comment("/wt", "https://github.com/o/r/pull/12", text))
    assert got == "skipped: no GitHub token"
    assert asyncio.run(S.post_comment("/wt", "", text)) == (
        "skipped: the session has no PR"
    )
