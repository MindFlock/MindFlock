#!/usr/bin/env python3
"""Route eval: can an agent with the MindFlock MCP turn a person's plain
description of a route into the right route on a session?

For each case in :mod:`cases` it

1. creates a session in a SANDBOX MindFlock server (fresh HOME, its own tmux
   socket dir, no tailnet, no cursor adoption, a stub agent — the ux-lab
   ``LabServer`` recipe; never the live server on :8765),
2. runs a headless agent — ``claude -p`` with ONLY the MindFlock MCP attached,
   identified as that session (the env ``providers/mcp_attach.py`` gives a
   launch) and no user settings or hooks — on what the person said,
3. if the agent read the route back and waited, sends the person's OK
   (``claude -p --resume``),
4. reads what it built (``GET /api/flights?all=1`` for that session, and any
   new saved route) and grades it against the case's rubric.

The server's own one-shot describe (``POST /api/flights/describe``) runs the
real ``claude -p`` too: the sandbox's stub ``claude`` hands print-mode calls to
the real CLI with the real HOME (for its login only — no settings sources).

Usage (from the repo root):

    uv run python scripts/route_eval/run_eval.py [--only id,id] [--jobs 3]
        [--model sonnet] [--out results.json]

Prints a markdown table; ``--out`` keeps every transcript's final text, the
route and the grade.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

from cases import CASES, OK  # noqa: E402
from tests.e2e._server import SandboxServer  # noqa: E402

REAL_HOME = os.path.expanduser("~")
REAL_CLAUDE = shutil.which("claude") or ""

#: The sandbox's ``claude``: print mode (the server's one-shot describe) goes
#: to the real CLI with the real HOME for its login, no settings sources and
#: no MCP; anything else (a session's agent) idles like the e2e stub.
STUB = """#!/bin/sh
for a in "$@"; do
  if [ "$a" = "-p" ] || [ "$a" = "--print" ]; then
    HOME="{home}" exec "{claude}" --setting-sources "" "$@"
  fi
done
echo "[stub-claude] started with args: $*"
while true; do sleep 60; done
"""


class EvalServer(SandboxServer):
    """The e2e sandbox cut off from the owner's machine (ux-lab's LabServer:
    a failing ``tailscale`` stub first on PATH, no tailnet status file, peer
    home in scratch, TMPDIR in scratch) with the forwarding ``claude`` stub."""

    def _lay_out_scratch(self) -> None:
        super()._lay_out_scratch()
        stub = self.scratch / "bin" / "claude"
        stub.write_text(STUB.format(home=REAL_HOME, claude=REAL_CLAUDE))
        stub.chmod(0o755)

    def _build_env(self) -> None:
        super()._build_env()
        bin_dir = self.scratch / "bin"
        for name in ("tailscale", "cloudflared"):
            p = bin_dir / name
            p.write_text("#!/bin/sh\necho 'not in the route eval' >&2\nexit 1\n")
            p.chmod(0o755)
        keep = [
            d
            for d in self.env["PATH"].split(os.pathsep)
            if d
            and (
                d == str(bin_dir)
                or not (
                    os.path.abspath(d).startswith(REAL_HOME + os.sep)
                    or d.startswith("/mnt/")
                )
            )
        ]
        self.env["PATH"] = os.pathsep.join(keep)
        tmp = self.scratch / "tmp"
        tmp.mkdir(exist_ok=True)
        pipe = self.scratch / "pipeline"
        pipe.mkdir(exist_ok=True)
        self.env.update(
            {
                "TMPDIR": str(tmp),
                "MINDFLOCK_REPO_ROOT": str(pipe),
                "MINDFLOCK_TAILSCALE_STATUS_FILE": str(self.scratch / "no-tailnet.json"),
                "MINDFLOCK_TAILSCALE_BIN": str(bin_dir / "tailscale"),
                "MINDFLOCK_PEER_HOME": str(self.home / ".mindflock-peer"),
                "MINDFLOCK_AGENT_MCP": "0",  # the stub sessions need no MCP
            }
        )


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def call(srv, method: str, path: str, body=None, timeout: float = 30.0):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        srv.base_url + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
    except urllib.error.HTTPError as err:
        raw = err.read()
    try:
        return json.loads(raw or b"{}")
    except ValueError:
        return {}


def create_session(srv, title: str) -> None:
    call(srv, "POST", "/api/instances", {"title": title, "repo_path": str(srv.repo)})
    deadline = time.time() + 40
    while time.time() < deadline:
        rows = call(srv, "GET", "/api/instances")
        rows = rows if isinstance(rows, list) else rows.get("instances", [])
        row = next((r for r in rows if r.get("title") == title), None)
        if row and row.get("status") not in (None, "", "loading"):
            return
        time.sleep(0.5)
    raise RuntimeError("session %s never came up" % title)


NIGHTLY = {
    "name": "Nightly chores",
    "description": "Bump the dependencies every night",
    "steps": [
        {"kind": "agent", "title": "Bump dependencies", "config": {"prompt": "Update the dependencies and fix what breaks"}},
        {"kind": "test", "title": "Tests pass", "on_fail": "agent"},
        {"kind": "ship", "title": "Open PR", "config": {"lane": "pr"}},
    ],
}


# --------------------------------------------------------------------------- #
# The agent
# --------------------------------------------------------------------------- #
def mcp_config(srv, title: str, path: Path) -> None:
    env = {
        "MINDFLOCK_HOST": "127.0.0.1",
        "MINDFLOCK_PORT": str(srv.port),
        "MINDFLOCK_MCP_SCOPE": "children",
        "MINDFLOCK_MCP_MANAGED": "1",
        "MINDFLOCK_SESSION_TITLE": title,
        "PYTHONPATH": str(ROOT),
    }
    cfg = {
        "mcpServers": {
            "mindflock": {
                "command": sys.executable,
                "args": ["-m", "backend.mcp"],
                "env": env,
            }
        }
    }
    path.write_text(json.dumps(cfg))


PERSONA = (
    "You are the coding agent in the MindFlock session \"{title}\" (a git "
    "worktree; MindFlock's MCP tools are attached). Right now your user only "
    "wants the ROUTE for this session set up — do not do the coding work "
    "itself in this turn. Use the MindFlock MCP. If something you need is "
    "missing or contradictory, ask your user rather than guess.\n\n"
    "Your user says: {text}"
)


def run_agent(prompt: str, cfg: Path, cwd: Path, model: str, resume: str = ""):
    env = {
        k: v
        for k, v in os.environ.items()
        if not (k.startswith("MINDFLOCK_") or k.startswith("CS_") or k.startswith("TMUX"))
    }
    argv = [
        REAL_CLAUDE,
        "-p",
        prompt,
        "--output-format",
        "json",
        "--setting-sources",
        "",
        "--mcp-config",
        str(cfg),
        "--strict-mcp-config",
        "--allowedTools",
        "mcp__mindflock",
        "--disallowedTools",
        "Bash,Edit,Write,NotebookEdit,WebFetch,WebSearch,Agent",
        "--model",
        model,
    ]
    if resume:
        argv += ["--resume", resume]
    t0 = time.time()
    cp = subprocess.run(
        argv, cwd=str(cwd), env=env, capture_output=True, text=True, timeout=900
    )
    try:
        out = json.loads(cp.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        out = {"result": cp.stdout[-2000:] + cp.stderr[-2000:], "session_id": ""}
    out["_secs"] = round(time.time() - t0)
    return out


# --------------------------------------------------------------------------- #
# What it built, and the grade
# --------------------------------------------------------------------------- #
def built(srv, title: str, plans_before: set) -> dict:
    flights = call(srv, "GET", "/api/flights?all=1").get("flights") or []
    # Its session's routes, and a grounded one it put on the Sky (by: agent:<it>).
    mine = [
        f
        for f in flights
        if f.get("session") == title
        or (not f.get("session") and f.get("by") == "agent:" + title)
    ]
    plans = call(srv, "GET", "/api/flights/plans").get("plans") or []
    new_plans = [p for p in plans if p.get("id") not in plans_before]
    return {"flights": mine, "plans": new_plans}


def token(step: dict) -> str:
    kind = str(step.get("kind") or "")
    cfg = step.get("config") or {}
    t = kind + (":" + str(cfg.get("lane") or "push") if kind == "ship" else "")
    return t + "!" + str(step.get("on_fail") or "halt")


def matches(pattern: str, step: dict) -> bool:
    tok = token(step)
    for alt in pattern.split("|"):
        want_fail = alt.split("!", 1)[1] if "!" in alt else ""
        base = alt.split("!", 1)[0]
        kindlane, fail = tok.split("!", 1)
        if (kindlane == base or kindlane.split(":")[0] == base) and (
            not want_fail or fail == want_fail
        ):
            return True
    return False


def subsequence(patterns, steps) -> bool:
    i = 0
    for s in steps:
        if i < len(patterns) and matches(patterns[i], s):
            i += 1
    return i == len(patterns)


def grade(case: dict, got: dict, final_text: str) -> dict:
    flights = got["flights"]
    plans = got["plans"]
    route = None
    where = ""
    if case.get("saved") and plans:
        route, where = plans[-1].get("steps") or [], "saved route"
    elif flights:
        f = max(flights, key=lambda f: float(f.get("updated") or 0))
        route, where = f.get("steps") or [], "route %s" % f.get("state")
    elif plans:
        route, where = plans[-1].get("steps") or [], "saved route"
    asked = (route is None) and "?" in (final_text or "")
    if route is None and case.get("ask_ok"):
        asked = "?" in (got.get("first") or final_text or "")
    why = []
    if case.get("ask"):
        ok = asked and not route
        if not ok:
            why.append("expected a question, got %s" % (where or "nothing"))
        for p in case.get("must_not", []):
            if route and any(matches(p, s) for s in route):
                why.append("has %s" % p)
                ok = False
        return {"ok": ok, "asked": asked, "where": where, "why": why, "route": route}
    if route is None:
        ok = bool(case.get("ask_ok")) and asked
        if not ok:
            why.append("no route built" + (" (asked a question)" if asked else ""))
        return {"ok": ok, "asked": asked, "where": where, "why": why, "route": route}
    ok = subsequence(case["must"], route) or any(
        subsequence(alt, route) for alt in case.get("alt_must", [])
    )
    if not ok:
        why.append("missing %s in order" % " → ".join(case["must"]))
    for p in case.get("must_not", []):
        if any(matches(p, s) for s in route):
            ok = False
            why.append("has %s" % p)
    for kind, want in (case.get("config") or {}).items():
        st = next((s for s in route if s.get("kind") == kind), None)
        for k, v in want.items():
            if not st or (st.get("config") or {}).get(k) != v:
                ok = False
                why.append("%s.%s != %r" % (kind, k, v))
    if case.get("saved"):
        if where != "saved route":
            ok = False
            why.append("not saved")
        else:
            trig = plans[-1].get("trigger") or {}
            want = case.get("trigger") or {}
            if want.get("kind") and trig.get("kind") != want["kind"]:
                ok = False
                why.append("trigger %s" % trig.get("kind"))
            sched = trig.get("schedule") or {}
            blob = json.dumps(sched)
            if want.get("at") and want["at"] not in blob and want["at"].lstrip("0") not in blob:
                # a cron "0 1 * * 1-5" says the same thing
                if not ("cron" in sched and "1-5" in blob):
                    ok = False
                    why.append("schedule %s" % blob)
    return {"ok": ok, "asked": asked, "where": where, "why": why, "route": route}


def short(route) -> str:
    if not route:
        return "—"
    return " → ".join(token(s).replace("!halt", "") for s in route)


# --------------------------------------------------------------------------- #
def run_case(srv, case: dict, work: Path, model: str) -> dict:
    title = "ev-" + case["id"].replace("_", "-")[:24]
    create_session(srv, title)
    plans_before = {p.get("id") for p in call(srv, "GET", "/api/flights/plans").get("plans") or []}
    cfg = work / (title + ".mcp.json")
    mcp_config(srv, title, cfg)
    cwd = work / title
    cwd.mkdir(exist_ok=True)
    turns = []
    r1 = run_agent(PERSONA.format(title=title, text=case["text"]), cfg, cwd, model)
    turns.append(r1)
    got = built(srv, title, plans_before)
    if (
        not got["flights"]
        and not got["plans"]
        and not case.get("ask")
        and not (case.get("ask_ok") and "?" in (r1.get("result") or ""))
        and r1.get("session_id")
    ):
        r2 = run_agent(case.get("reply") or OK, cfg, cwd, model, resume=r1["session_id"])
        turns.append(r2)
        got = built(srv, title, plans_before)
    final = turns[-1].get("result") or ""
    got["first"] = r1.get("result") or ""
    g = grade(case, got, final)
    for f in got["flights"]:
        if f.get("state") not in ("done", "cancelled"):
            call(srv, "POST", "/api/flights/%s/cancel" % f["id"], {})
    return {
        "id": case["id"],
        "style": case["style"],
        "text": case["text"],
        "turns": len(turns),
        "secs": sum(t.get("_secs", 0) for t in turns),
        "cost": round(sum(float(t.get("total_cost_usd") or 0) for t in turns), 3),
        "final": final[-1500:],
        "first": (r1.get("result") or "")[-1500:],
        **g,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    ap.add_argument("--jobs", type=int, default=3)
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    if not REAL_CLAUDE:
        print("no claude CLI on PATH", file=sys.stderr)
        return 2
    cases = [c for c in CASES if not a.only or c["id"] in a.only.split(",")]
    scratch = Path(tempfile.mkdtemp(prefix="route-eval-"))
    # One sandbox server per job: a saved route is graded by what appeared on
    # ITS server's shelf, so cases sharing a server would claim each other's.
    jobs = max(1, min(a.jobs, len(cases)))
    servers = []
    results = []
    try:
        for n in range(jobs):
            (scratch / ("lab%d" % n)).mkdir()
            srv = EvalServer(scratch / ("lab%d" % n), onboarded=True, stub_agent=True)
            srv.start(timeout=90)
            servers.append(srv)
            call(srv, "POST", "/api/flights/plans", NIGHTLY)
        work = scratch / "agents"
        work.mkdir()
        import queue
        import threading

        todo: "queue.Queue" = queue.Queue()
        for c in cases:
            todo.put(c)
        lock = threading.Lock()

        def worker(srv):
            while True:
                try:
                    c = todo.get_nowait()
                except queue.Empty:
                    return
                try:
                    r = run_case(srv, c, work, a.model)
                except Exception as err:  # noqa: BLE001
                    r = {"id": c["id"], "style": c["style"], "ok": False, "why": [repr(err)], "route": None, "where": "", "asked": False, "secs": 0, "turns": 0, "cost": 0}
                with lock:
                    results.append(r)
                    print("%s %s %s" % ("PASS" if r["ok"] else "FAIL", r["id"], "; ".join(r.get("why") or [])), flush=True)

        threads = [threading.Thread(target=worker, args=(srv,)) for srv in servers]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        for srv in servers:
            srv.stop()
    order = {c["id"]: i for i, c in enumerate(CASES)}
    results.sort(key=lambda r: order.get(r["id"], 99))
    print()
    print("| case | style | built | grade | why |")
    print("|---|---|---|---|---|")
    for r in results:
        built_txt = "asked a question" if r.get("asked") else short(r.get("route"))
        print(
            "| %s | %s | %s | %s | %s |"
            % (r["id"], r["style"], built_txt, "✅" if r["ok"] else "❌", "; ".join(r.get("why") or []) or "")
        )
    n = sum(1 for r in results if r["ok"])
    print("\n%d/%d right" % (n, len(results)))
    if a.out:
        Path(a.out).write_text(json.dumps(results, indent=1, ensure_ascii=False))
    shutil.rmtree(scratch, ignore_errors=True)
    return 0 if n >= len(results) - 1 else 1


if __name__ == "__main__":
    sys.exit(main())
