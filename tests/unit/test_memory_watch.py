"""Per-session memory (``core.memory_watch``) against a fake ``/proc`` tree:
attribution (agent / mcp / other), PSS vs RSS, peak, a session with no pane,
the PSS budget, history rotation, the slow-tick skip, the row field and the
two routes (``GET /api/memory``, ``GET /api/memory/history``).

``server.ENGINE`` is the developer's REAL state: the sampler's session list
and the tmux pane query are stubbed at their seams (``_sessions``,
``pane_pids``), never enumerated from ENGINE.
"""

from __future__ import annotations

import asyncio
import json
import os
import time

import pytest

from backend.web.core import memory_watch as mw

PAGE_KB = mw._PAGE_KB


# --------------------------------------------------------------------------- #
# the fake /proc
# --------------------------------------------------------------------------- #
class FakeProc:
    def __init__(self, root):
        self.root = root
        root.mkdir(exist_ok=True)

    def add(self, pid, ppid, comm, argv, rss_mb, pss_mb=None, start=None):
        d = self.root / str(pid)
        d.mkdir(exist_ok=True)
        rss_pages = rss_mb * 1024 // PAGE_KB
        # fields 3.. after "(comm)": state ppid pgrp session tty tpgid flags
        # minflt cminflt majflt cmajflt utime stime cutime cstime priority
        # nice threads itrealvalue starttime vsize rss
        rest = ["S", str(ppid)] + ["0"] * 17 + [str(start or pid), "0", str(rss_pages)]
        (d / "stat").write_text("%d (%s) %s\n" % (pid, comm, " ".join(rest)))
        (d / "cmdline").write_bytes(b"".join(a.encode() + b"\0" for a in argv))
        if pss_mb is not None:
            (d / "smaps_rollup").write_text(
                "00400000-7fff [rollup]\nRss:  %d kB\nPss:  %d kB\n"
                % (rss_mb * 1024, pss_mb * 1024)
            )

    def remove(self, pid):
        d = self.root / str(pid)
        for f in d.iterdir():
            f.unlink()
        d.rmdir()


@pytest.fixture
def fake(tmp_path, monkeypatch):
    fp = FakeProc(tmp_path / "proc")
    monkeypatch.setattr(mw, "PROC_ROOT", str(fp.root))
    monkeypatch.setattr(mw, "_CACHES", mw._Caches())
    monkeypatch.setattr(mw, "_PEAKS", {})
    monkeypatch.setattr(mw, "HISTORY_PATH", str(tmp_path / "logs" / "memory.jsonl"))
    monkeypatch.setattr(mw, "_HIST", {"last": 0.0, "failed": False})
    monkeypatch.setattr(mw, "_SIG_CACHE", {"at": -1e18, "mtimes": None, "sigs": ()})
    monkeypatch.setattr(mw, "_system", lambda: {"level": "ok", "total_mb": 20000})
    monkeypatch.setattr(
        mw,
        "_STATE",
        {
            "at": 0.0,
            "sessions": {},
            "others": [],
            "system": {},
            "tick_ms": None,
            "tick_cpu_ms": None,
            "source": "",
        },
    )
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    return fp


def _claude_session(fp, base, agent_mb=400, scale=1):
    """A launcher ``sh -c`` pane → claude → its MCP servers and a Bash-tool
    shell running pytest. Returns the pane pid."""
    pane = base
    fp.add(pane, 1, "sh", ["/usr/bin/sh", "-c", "env X=1 claude --mcp-config=x"], 2, 1)
    fp.add(
        base + 1,
        pane,
        "claude",
        ["claude", "--mcp-config=/r/x.json"],
        agent_mb * scale,
        agent_mb,
    )
    fp.add(
        base + 2,
        base + 1,
        "python",
        ["/v/bin/python", "-P", "-m", "backend.mcp"],
        30,
        25,
    )
    fp.add(base + 3, base + 1, "node", ["node", "/g/bin/gitnexus", "mcp"], 120, 100)
    # npm exec → sh -c → node server: the whole subtree is the MCP server.
    fp.add(
        base + 4,
        base + 1,
        "npm exec @upst",
        ["npm exec @upstash/context7-mcp@latest"],
        80,
        60,
    )
    fp.add(base + 5, base + 4, "sh", ["sh", "-c", "context7-mcp"], 2, 1)
    fp.add(base + 6, base + 5, "node", ["node", "/n/.bin/context7-mcp"], 70, 50)
    # The Bash tool's shell and what it runs: other.
    fp.add(base + 7, base + 1, "zsh", ["/usr/bin/zsh", "-c", "pytest -q"], 4, 3)
    fp.add(
        base + 8, base + 7, "python", ["/v/bin/python", "-m", "pytest", "-q"], 300, 290
    )
    return pane


# --------------------------------------------------------------------------- #
# attribution
# --------------------------------------------------------------------------- #
def test_attribution_agent_mcp_other(fake):
    pane = _claude_session(fake, 100)
    fake.add(500, 1, "python", ["python", "voicelab.py"], 4000, 3900)  # not a session
    res = mw.sample([("s1", "mindflock_s1")], {"mindflock_s1": [pane]})
    r = res["sessions"]["s1"]
    assert r["agent_mb"] == 400
    assert r["mcp_mb"] == 25 + 100 + 60 + 1 + 50
    assert r["other_mb"] == 1 + 3 + 290  # launcher sh + zsh + pytest
    assert r["mb"] == r["agent_mb"] + r["mcp_mb"] + r["other_mb"]
    assert r["procs"] == 9
    assert r["peak_mb"] == r["mb"]
    assert set(r) == {"mb", "peak_mb", "agent_mb", "mcp_mb", "other_mb", "procs", "at"}
    # The process outside every tree is an "other", with its name and cmd.
    assert res["others"][0]["pid"] == 500
    assert res["others"][0]["name"] == "voicelab"
    assert res["others"][0]["cmd"] == "python voicelab.py"
    assert res["others"][0]["mb"] == 3900
    assert all(o["pid"] not in range(100, 109) for o in res["others"])


def test_rss_where_no_pss(fake):
    fake.add(10, 1, "claude", ["claude"], 200)  # no smaps_rollup
    res = mw.sample([("s", "n")], {"n": [10]})
    assert res["sessions"]["s"]["mb"] == 200
    assert res["sessions"]["s"]["agent_mb"] == 200


def test_codex_wrapper_and_its_binary_are_the_agent(fake):
    fake.add(10, 1, "bash", ["bash", "/run/launch.sh"], 3, 3)
    fake.add(
        11,
        10,
        "node",
        ["node", "/usr/lib/node_modules/@openai/codex/bin/codex.js"],
        60,
        60,
    )
    fake.add(12, 11, "codex", ["/usr/lib/x/codex-linux/codex", "--yolo"], 200, 200)
    fake.add(13, 12, "python", ["python", "-P", "-m", "backend.mcp"], 30, 30)
    r = mw.sample([("c", "n")], {"n": [10]})["sessions"]["c"]
    assert (r["agent_mb"], r["mcp_mb"], r["other_mb"]) == (260, 30, 3)


def test_a_configured_server_without_mcp_in_its_name(fake, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".claude.json").write_text(
        json.dumps(
            {
                "mcpServers": {"pg": {"command": "/opt/pgproxy", "args": []}},
                "projects": {
                    "/r": {
                        "mcpServers": {
                            "x": {
                                "command": "npx",
                                "args": ["-y", "@acme/tools-server"],
                            }
                        }
                    }
                },
            }
        )
    )
    fake.add(10, 1, "claude", ["claude"], 300, 300)
    fake.add(11, 10, "pgproxy", ["/opt/pgproxy", "db1"], 40, 40)
    fake.add(12, 10, "npm exec @acme", ["npm exec @acme/tools-server"], 50, 50)
    fake.add(13, 10, "rg", ["rg", "foo"], 10, 10)  # something the CLI ran
    r = mw.sample([("s", "n")], {"n": [10]})["sessions"]["s"]
    assert (r["agent_mb"], r["mcp_mb"], r["other_mb"]) == (300, 90, 10)


def test_a_bare_shell_pane_is_all_other(fake):
    fake.add(10, 1, "zsh", ["-zsh"], 5, 5)
    fake.add(11, 10, "vim", ["vim", "x"], 20, 20)
    r = mw.sample([("s", "n")], {"n": [10]})["sessions"]["s"]
    # vim is the shallowest non-shell: it reads as the pane's program.
    assert r["mb"] == 25 and r["mcp_mb"] == 0
    fake.remove(11)
    r = mw.sample([("s", "n")], {"n": [10]})["sessions"]["s"]
    assert (r["agent_mb"], r["other_mb"]) == (0, 5)


def test_is_mcp_argv():
    assert mw.is_mcp_argv(["python", "-P", "-m", "backend.mcp"])
    assert mw.is_mcp_argv(["node", "/x/gitnexus", "mcp"])
    assert mw.is_mcp_argv(["/x/srv"], sigs=("srv",))
    assert not mw.is_mcp_argv(["python", "-m", "pytest"])
    assert not mw.is_mcp_argv([])


# --------------------------------------------------------------------------- #
# peak, no pane, the row
# --------------------------------------------------------------------------- #
def test_peak_holds_and_resets_with_a_new_pane(fake):
    pane = _claude_session(fake, 100)
    first = mw.sample([("s", "n")], {"n": [pane]})["sessions"]["s"]["mb"]
    fake.remove(108)  # the test run ended
    r = mw.sample([("s", "n")], {"n": [pane]})["sessions"]["s"]
    assert r["mb"] == first - 290 and r["peak_mb"] == first
    # A relaunch (new pane pid) is a new incarnation: the peak starts over.
    fake.add(900, 1, "claude", ["claude"], 50, 50)
    r = mw.sample([("s", "n")], {"n": [900]})["sessions"]["s"]
    assert r["mb"] == 50 and r["peak_mb"] == 50


def test_a_session_with_no_pane_reads_null(fake):
    pane = _claude_session(fake, 100)
    res = mw.sample(
        [("live", "n1"), ("stopped", "n2"), ("gone", "n3")],
        {"n1": [pane], "n3": [4242]},  # n3's pane pid is not in /proc
    )
    assert set(res["sessions"]) == {"live"}
    assert mw.row("stopped") is None and mw.row("gone") is None
    assert mw.row("live")["mb"] == res["sessions"]["live"]["mb"]
    # A stale reading is not served.
    assert mw.row("live", now=time.time() + mw.STALE_SECONDS + 1) is None


def test_row_field_on_both_snapshot_paths(fake, monkeypatch):
    from backend.session.storage import Status
    from backend.web import server

    class _Inst:
        Program = "claude"
        Path = ""
        Branch = ""
        BaseBranch = ""
        InPlace = False
        Task = ""

        def __init__(self, title):
            self.Title = title
            self.Status = Status.Running

        def Started(self):  # noqa: N802
            return True

        def GetWorktreePath(self):  # noqa: N802
            return ""

    inst = _Inst("memrow-1")
    assert server._session_snapshot_cheap(inst, {})["memory"] is None
    pane = _claude_session(fake, 100)
    mw.sample([("memrow-1", "n")], {"n": [pane]})
    want = mw.row("memrow-1")
    assert server._session_snapshot_cheap(inst, {})["memory"] == want
    # The tick's full path (what GET /api/instances and /api/events serve).
    monkeypatch.setattr(server.ENGINE, "instances", {inst.Title: inst})
    monkeypatch.setattr(server, "_agent_activity", lambda i, t: "idle")
    server._ACTIVITY_CACHE.pop(inst.Title, None)
    try:
        rows = [
            d for d in server._build_instances_snapshot() if d["title"] == inst.Title
        ]
        assert rows and rows[0]["memory"] == want
    finally:
        server._forget_probes(inst.Title)


# --------------------------------------------------------------------------- #
# cost
# --------------------------------------------------------------------------- #
def test_one_scan_per_tick_and_the_cpu_budget(fake, monkeypatch, capsys):
    panes = {}
    sessions = []
    for i in range(20):
        base = 1000 + i * 100
        panes["n%d" % i] = [_claude_session(fake, base)]
        sessions.append(("s%d" % i, "n%d" % i))
    for i in range(200):  # the rest of a busy box
        fake.add(9000 + i, 1, "daemon", ["/usr/bin/daemon%d" % i], 5, 5)
    scans = []
    real_scan = mw._scan_proc
    monkeypatch.setattr(
        mw, "_scan_proc", lambda root: scans.append(root) or real_scan(root)
    )
    mw.sample(sessions, panes)  # first sighting: every smaps_rollup read
    times = []
    for _ in range(5):
        c0 = time.thread_time()
        res = mw.sample(sessions, panes)
        times.append((time.thread_time() - c0) * 1000.0)
    assert len(scans) == 6  # one /proc scan per tick, never one per session
    assert len(res["sessions"]) == 20
    best = min(times)
    with capsys.disabled():
        print(
            "\nmemory_watch tick, 20 sessions / 380 procs (fake /proc): %.1f ms CPU"
            % best
        )
    assert best < 50.0


def test_pss_reads_stay_within_the_budget(fake, monkeypatch):
    for i in range(10):
        fake.add(100 + i, 1, "claude", ["claude"], 1000, 500)
    reads = []
    real = mw._pss_kb
    monkeypatch.setattr(
        mw, "_pss_kb", lambda root, pid: reads.append(pid) or real(root, pid)
    )
    monkeypatch.setattr(mw, "PSS_BUDGET_MB", 2500)
    sess = [("s%d" % i, "n%d" % i) for i in range(10)]
    panes = {"n%d" % i: [100 + i] for i in range(10)}
    mw.sample(sess, panes)
    assert len(reads) == 10  # never-read: all of them, once
    reads.clear()
    res = mw.sample(sess, panes)
    assert len(reads) == 3  # 3 × 1000 MB walks reach the 2500 MB budget
    # The rest are RSS × their last ratio: still PSS-sized.
    assert all(r["mb"] == 500 for r in res["sessions"].values())
    reads.clear()
    mw.sample(sess, panes)
    assert len(reads) == 3 and not set(reads) & set(range(100, 103))  # stalest next


# --------------------------------------------------------------------------- #
# history
# --------------------------------------------------------------------------- #
def test_history_lines_rotation_and_window(fake, monkeypatch):
    pane = _claude_session(fake, 100)
    now = time.time()
    path = mw.history_path()
    for k in range(6):
        res = mw.sample([("s", "n")], {"n": [pane]}, now=now - 3600 + k * 600)
        assert mw.append_history(res)
        if k == 0:  # a file holds two lines: rotation every other append
            monkeypatch.setattr(mw, "ROTATE_BYTES", int(os.path.getsize(path) * 1.5))
    assert os.path.exists(path) and os.path.exists(path + ".1")
    assert not os.path.exists(path + ".2")  # one old file kept
    line = json.loads(open(path).read().splitlines()[-1])
    assert set(line) == {"at", "system", "sessions", "others"}
    assert line["sessions"] == {"s": res["sessions"]["s"]["mb"]}
    got = mw.history(minutes=25, now=now)
    assert [round(s["at"]) for s in got] == [round(now - 1200), round(now - 600)]
    everything = mw.history(minutes=120, now=now)
    assert len(everything) >= 3 and everything == sorted(
        everything, key=lambda s: s["at"]
    )


def test_history_write_failure_is_logged_once(fake, monkeypatch, tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    monkeypatch.setattr(mw, "HISTORY_PATH", str(blocker / "logs" / "memory.jsonl"))
    said = []
    monkeypatch.setattr(mw, "_info", lambda fmt, *a: said.append(fmt % a))
    res = {"at": 1.0, "sessions": {}, "others": [], "system": {}}
    assert mw.append_history(res) is False
    assert mw.append_history(res) is False
    assert len(said) == 1


def test_tick_writes_history_every_30s(fake, monkeypatch):
    pane = _claude_session(fake, 100)
    monkeypatch.setattr(mw, "_sessions", lambda srv: [("s", "n")])
    monkeypatch.setattr(mw, "pane_pids", lambda: {"n": [pane]})
    mw.tick(object())
    mw.tick(object())  # within 30 s: no second line
    lines = open(mw.history_path()).read().splitlines()
    assert len(lines) == 1
    assert mw._STATE["tick_cpu_ms"] is not None


# --------------------------------------------------------------------------- #
# the loop
# --------------------------------------------------------------------------- #
def test_a_slow_tick_skips_the_next(monkeypatch):
    calls = []

    def _tick(srv):
        calls.append(time.monotonic())

    said = []
    monkeypatch.setattr(mw, "tick", _tick)
    monkeypatch.setattr(mw, "_info", lambda fmt, *a: said.append(fmt))
    monkeypatch.setattr(mw, "SLOW_TICK_SECONDS", -1.0)  # every tick is "slow"

    class _Srv:
        pass

    async def drive():
        task = asyncio.ensure_future(mw.run_loop(_Srv(), interval=0.01))
        await asyncio.sleep(0.25)
        task.cancel()

    asyncio.run(drive())
    # ~25 loop turns, every other one skipped.
    assert 4 <= len(calls) <= 16
    assert len(said) == 1  # logged once per episode


# --------------------------------------------------------------------------- #
# the routes
# --------------------------------------------------------------------------- #
@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("MINDFLOCK_SETTINGS_FILE", str(tmp_path / "settings.json"))
    monkeypatch.setenv("MINDFLOCK_AUTH", "0")
    from fastapi.testclient import TestClient

    from backend.web.server import app

    return TestClient(app)


def test_get_memory_shape(fake, monkeypatch, client):
    pane = _claude_session(fake, 100)
    small = _claude_session(fake, 300, agent_mb=50)
    fake.add(500, 1, "python", ["python", "voicelab.py"], 4000, 3900)
    monkeypatch.setattr(
        mw, "_sessions", lambda srv: [("big", "n1"), ("small", "n2"), ("off", "n3")]
    )
    monkeypatch.setattr(mw, "pane_pids", lambda: {"n1": [pane], "n2": [small]})
    body = client.get("/api/memory").json()  # samples on demand: no tick yet
    assert set(body) >= {"system", "sessions", "others", "at"}
    assert body["system"]["level"] == "ok"
    assert [s["title"] for s in body["sessions"]] == ["big", "small"]
    assert set(body["sessions"][0]) == {
        "title",
        "mb",
        "peak_mb",
        "agent_mb",
        "mcp_mb",
        "other_mb",
        "procs",
        "at",
    }
    assert body["others"][0]["pid"] == 500
    assert set(body["others"][0]) >= {"pid", "name", "cmd", "mb"}
    assert len(body["others"]) <= mw.OTHERS_TOP
    assert not os.path.exists(mw.history_path())  # on demand writes no line


def test_get_memory_history(fake, client):
    now = time.time()
    for k, at in enumerate((now - 7200, now - 1800, now - 60)):
        mw.append_history(
            {"at": at, "sessions": {"s": {"mb": k}}, "others": [], "system": {}}
        )
    got = client.get("/api/memory/history").json()["samples"]
    assert [s["sessions"]["s"] for s in got] == [1, 2]
    got = client.get("/api/memory/history?minutes=180").json()["samples"]
    assert len(got) == 3
    assert client.get("/api/memory/history?minutes=abc").status_code == 400
    assert client.get("/api/memory/history?minutes=nan").status_code == 400
