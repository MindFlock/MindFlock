"""Named sample-data sets for the preview, seeded through the server's own
HTTP API (``POST /api/instances``, ``/api/flights/plans``,
``/api/flights/start``, ``/api/todo/items``, a worker's result message) so
what the UI shows is what the real code paths produce — not hand-made JSON.

Only the git repositories are made directly (with the scratch git config):
two small projects, ``acme-api`` and ``acme-web``, with a few commits and a
feature branch. Each session's stub agent plays the role written for it in
``roles.json`` (:mod:`mfpreview.stub_agent`).

A scenario = a list of session names (from :data:`SESSIONS`) + a list of
route names (from :data:`ROUTES`) + extras. :func:`seed` returns a summary
of what it made, including anything that did not reach its intended state —
a scenario never fails the preview for one stubborn row.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Dict, List, Optional

from mfpreview.sandbox import PreviewError, Sandbox, call

# --------------------------------------------------------------------------- #
# Repositories
# --------------------------------------------------------------------------- #
REPOS: Dict[str, List[tuple]] = {
    "acme-api": [
        (
            "Initial API skeleton",
            {
                "README.md": "# acme-api\n\nThe Acme accounts and billing API.\n",
                "app/__init__.py": "",
                "app/auth.py": "def login(user, password):\n    return user.check(password)\n",
                "tests/test_auth.py": "def test_login():\n    assert True\n",
            },
        ),
        (
            "Add invoices",
            {"app/billing.py": "def invoices(account):\n    return account.invoices\n"},
        ),
        (
            "Search endpoint",
            {"app/search.py": "def search(q):\n    return []\n"},
        ),
    ],
    "acme-web": [
        (
            "Initial web app",
            {
                "README.md": "# acme-web\n\nThe Acme customer dashboard.\n",
                "package.json": '{"name": "acme-web", "private": true}\n',
                "src/App.tsx": "export function App() {\n  return <h1>Acme</h1>;\n}\n",
                "docs/getting-started.md": "# Getting started\n\nRun `npm start`.\n",
            },
        ),
        ("Release notes draft", {"CHANGELOG.md": "# Changelog\n\n## 1.4.0\n"}),
    ],
}


def make_repos(sb: Sandbox) -> Dict[str, str]:
    paths = {}
    for name, commits in REPOS.items():
        repo = sb.repos_dir / name
        repo.mkdir(parents=True, exist_ok=True)
        sb.git(repo, "init", "-q", "-b", "main")
        for msg, files in commits:
            for rel, text in files.items():
                p = repo / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(text)
            sb.git(repo, "add", "-A")
            sb.git(repo, "commit", "-qm", msg)
        paths[name] = str(repo)
    api = sb.repos_dir / "acme-api"
    sb.git(api, "checkout", "-q", "-b", "feature/rate-limit")
    (api / "app" / "ratelimit.py").write_text("LIMIT = 100\n")
    sb.git(api, "add", "-A")
    sb.git(api, "commit", "-qm", "Rate limit sketch")
    sb.git(api, "checkout", "-q", "main")
    return paths


# --------------------------------------------------------------------------- #
# Sessions: title → how it is created and the screen its stub plays
# --------------------------------------------------------------------------- #
SESSIONS: Dict[str, Dict[str, Any]] = {
    "api-auth": {
        "repo": "acme-api",
        "prompt": "Add a password reset flow to the auth API",
        "role": {
            "screen": "working",
            "lines": [
                "I'll add a reset token model, an email step and two endpoints.",
                "Wrote app/reset.py and tests/test_reset.py",
            ],
            "files": {
                "app/reset.py": "def request_reset(email):\n    ...\n",
                "tests/test_reset.py": "def test_reset():\n    assert True\n",
            },
            "commit": "Add password reset endpoints",
        },
    },
    "billing-export": {
        "repo": "acme-api",
        "prompt": "Export invoices as CSV with a background job",
        "role": {
            "screen": "ask",
            "lines": ["The export needs a job queue; I'll add redis."],
            "ask": {
                "tool": "Bash command",
                "command": "uv add redis",
                "why": "Add redis as a dependency",
            },
        },
    },
    "docs-refresh": {
        "repo": "acme-web",
        "prompt": "Rewrite the getting-started guide",
        "role": {
            "screen": "idle",
            "lines": [
                "Rewrote docs/getting-started.md with install, run and deploy steps."
            ],
            "files": {
                "docs/getting-started.md": "# Getting started\n\n1. npm ci\n2. npm start\n"
            },
            "commit": "Rewrite the getting-started guide",
        },
    },
    "search-index": {
        "repo": "acme-api",
        "prompt": "Rebuild the search index nightly",
        "role": {
            "screen": "idle",
            "lines": ["Added a nightly reindex task and its test."],
            "files": {"app/reindex.py": "def reindex():\n    ...\n"},
            "commit": "Nightly search reindex",
        },
    },
    "flaky-ci": {
        "repo": "acme-web",
        "prompt": "Find out why the dashboard test is flaky",
        "role": {
            "screen": "idle",
            "lines": [
                "The test races the clock; I started a fix but have not committed it.",
            ],
            "files": {
                "src/clock.ts": "export const now = () => Date.now();\n",
                "src/App.test.tsx": "test('renders', () => {});\n",
            },
        },
    },
    "release-notes": {
        "repo": "acme-web",
        "prompt": "Draft the 1.4 release notes",
        "role": {
            "screen": "idle",
            "lines": ["Drafted CHANGELOG.md for 1.4.0."],
            "files": {"CHANGELOG.md": "# Changelog\n\n## 1.4.0\n\n- Password reset\n"},
            "commit": "Release notes for 1.4.0",
        },
    },
    "db-migration": {
        "repo": "acme-api",
        "prompt": "Migrate the invoices table to the new schema",
        "role": {"screen": "crash"},
    },
    "orchestrator": {
        "repo": "acme-web",
        "prompt": "Ship the new dashboard: split it across workers",
        "role": {
            "screen": "working",
            "lines": [
                "Split into worker-ui and worker-api; waiting for their reports."
            ],
        },
    },
    "worker-ui": {
        "repo": "acme-web",
        "parent": "orchestrator",
        "prompt": "Build the dashboard cards",
        "role": {
            "screen": "working",
            "lines": ["Building src/Cards.tsx"],
            "files": {"src/Cards.tsx": "export const Cards = () => null;\n"},
            "commit": "Dashboard cards",
        },
    },
    "worker-api": {
        "repo": "acme-web",
        "parent": "orchestrator",
        "prompt": "Add the dashboard stats endpoint client",
        "role": {
            "screen": "idle",
            "lines": [
                "The stats endpoint returns 500 on the fixture data; stopping here."
            ],
        },
    },
    "old-spike": {
        "repo": "acme-api",
        "prompt": "Spike: try a GraphQL layer",
        "role": {"screen": "idle", "lines": ["Spike done."]},
    },
}

# --------------------------------------------------------------------------- #
# Routes: name → (session or None for a session-less route, steps, target
# state, an optional action after it starts)
# --------------------------------------------------------------------------- #
ROUTES: Dict[str, Dict[str, Any]] = {
    "running": {
        "session": "api-auth",
        "name": "Ship password reset",
        "steps": [
            {
                "kind": "command",
                "title": "Run the tests",
                "config": {"command": "true"},
            },
            {"kind": "wait", "title": "Let CI settle", "config": {"seconds": 3600}},
            {
                "kind": "you",
                "title": "Check the PR",
                "config": {"text": "Look over the PR"},
            },
        ],
        "want": ("running",),
    },
    "waiting": {
        "session": "docs-refresh",
        "name": "Docs review",
        "steps": [
            {
                "kind": "you",
                "title": "Look over the new guide",
                "config": {
                    "text": "Read docs/getting-started.md",
                    "items": "Install\nRun\nDeploy",
                },
            },
            {
                "kind": "command",
                "title": "Build the docs",
                "config": {"command": "true"},
            },
        ],
        "want": ("waiting_you",),
    },
    "halted": {
        "session": "search-index",
        "name": "Reindex and push",
        "steps": [
            {
                "kind": "command",
                "title": "Rebuild the index",
                "config": {"command": "true"},
            },
            {"kind": "ship", "title": "Push", "halt": True, "config": {"lane": "push"}},
        ],
        "want": ("paused", "halted"),
    },
    "failed": {
        "session": "flaky-ci",
        "name": "Fix the flaky test",
        "steps": [
            {
                "kind": "command",
                "title": "Run the tests",
                "config": {
                    "command": "echo 'FAIL src/App.test.tsx: 2 failed, 41 passed'; exit 1"
                },
            },
            {"kind": "you", "title": "Check the fix", "config": {"text": "Re-run CI"}},
        ],
        "want": ("halted",),
    },
    "finished": {
        "session": "release-notes",
        "name": "Lint the release notes",
        "steps": [
            {"kind": "command", "title": "Lint", "config": {"command": "true"}},
            {
                "kind": "notify",
                "title": "Tell me",
                "config": {"text": "Release notes are ready"},
            },
        ],
        "want": ("done",),
    },
    "gone": {
        "session": "old-spike",
        "name": "Spike review",
        "steps": [
            {
                "kind": "you",
                "title": "Read the spike notes",
                "config": {"text": "Decide yes/no"},
            },
        ],
        "want": ("waiting_you",),
        "then": "delete_session",
    },
    "sessionless": {
        "session": None,
        "name": "Nightly dependency bump",
        "steps": [
            {
                "kind": "agent",
                "title": "Bump dependencies",
                "config": {"prompt": "Update the dependencies"},
            },
            {
                "kind": "command",
                "title": "Run the tests",
                "config": {"command": "make test"},
            },
        ],
        "want": ("grounded",),
    },
}

TODOS = [
    {
        "text": "Try the password reset email on Safari",
        "kind": "manual_test",
        "priority": "high",
        "session": "api-auth",
        "source": "agent",
    },
    {
        "text": "Decide which invoice columns the CSV export keeps",
        "kind": "needs_you",
        "session": "billing-export",
        "source": "agent",
    },
]

ALL_SESSIONS = list(SESSIONS)
ROUTE_SESSIONS = [r["session"] for r in ROUTES.values() if r["session"]]

SCENARIOS: Dict[str, Dict[str, Any]] = {
    "hub": {
        "summary": "Shared files (the ⋯ menu, #hub): a screenshot, a log, an SQL "
        "snippet with a note, a PDF, a phone screenshot and a picture an agent "
        "put — beside three sessions to send them to.",
        "sessions": ["api-auth", "billing-export", "docs-refresh"],
        "routes": [],
        "hub": True,
    },
    "hub-full": {
        "summary": "Shared files with 30 items (the find box, a long list).",
        "sessions": ["api-auth", "docs-refresh"],
        "routes": [],
        "hub": "full",
    },
    "hub-empty": {
        "summary": "Two sessions and empty Shared files (its first-run state).",
        "sessions": ["api-auth", "docs-refresh"],
        "routes": [],
    },
    "sky": {
        "summary": "Everything: sessions in every state, a parent with workers, "
        "routes in every state, to-dos and bell items.",
        "sessions": ALL_SESSIONS,
        "routes": list(ROUTES),
        "todos": True,
        "worker_report": True,
    },
    "sessions": {
        "summary": "Sessions only (working, idle, a question, crashed, uncommitted "
        "files, a parent with workers); no routes.",
        "sessions": [s for s in ALL_SESSIONS if s != "old-spike"],
        "routes": [],
        "todos": True,
        "worker_report": True,
    },
    "routes": {
        "summary": "One route in each state: running at a stop, waiting for me, "
        "paused at a halt point, a failed stop, finished, a gone session's "
        "route and a session-less one.",
        "sessions": ROUTE_SESSIONS,
        "routes": list(ROUTES),
        "todos": False,
        "worker_report": False,
    },
    "routes-failed": {
        "summary": "A failed stop (with its remedies) and a halt point, nothing else.",
        "sessions": ["flaky-ci", "search-index"],
        "routes": ["failed", "halted"],
        "todos": False,
        "worker_report": False,
    },
    "empty": {
        "summary": "Onboarded, no sessions: the empty Sky / grid.",
        "sessions": [],
        "routes": [],
        "todos": False,
        "worker_report": False,
    },
    "empty-first-run": {
        "summary": "The very first launch: not onboarded, nothing made.",
        "sessions": [],
        "routes": [],
        "todos": False,
        "worker_report": False,
        "onboarded": False,
    },
}


def get(name: str) -> Dict[str, Any]:
    if name not in SCENARIOS:
        raise PreviewError(
            "unknown scenario %r; choose one of: %s" % (name, ", ".join(SCENARIOS))
        )
    return SCENARIOS[name]


# --------------------------------------------------------------------------- #
# Seeding
# --------------------------------------------------------------------------- #
def _rows(sb: Sandbox) -> List[dict]:
    status, body = call(sb.base_url, "GET", "/api/instances")
    if isinstance(body, list):
        return body
    return list(body.get("instances") or []) if isinstance(body, dict) else []


def _wait(pred: Callable[[], Any], timeout: float, step: float = 0.5) -> Any:
    deadline = time.time() + timeout
    while time.time() < deadline:
        got = pred()
        if got:
            return got
        time.sleep(step)
    return None


def create_session(
    sb: Sandbox, title: str, repos: Dict[str, str], log: List[str]
) -> bool:
    spec = SESSIONS[title]
    body = {"title": title, "repo_path": repos[spec["repo"]], "prompt": spec["prompt"]}
    if spec.get("parent"):
        body["parent"] = spec["parent"]
    status, resp = call(sb.base_url, "POST", "/api/instances", body)
    if status >= 400:
        log.append("session %s: create refused %s %s" % (title, status, resp))
        return False

    def up():
        row = next((r for r in _rows(sb) if r.get("title") == title), None)
        return row if row and row.get("status") not in (None, "", "loading") else None

    if not _wait(up, 60):
        log.append("session %s: never came up" % title)
        return False
    return True


def _flight(sb: Sandbox, fid: str) -> dict:
    status, body = call(sb.base_url, "GET", "/api/flights/" + fid)
    return (body.get("flight") or body) if isinstance(body, dict) else {}


def start_route(sb: Sandbox, key: str, log: List[str]) -> Optional[str]:
    spec = ROUTES[key]
    status, body = call(
        sb.base_url,
        "POST",
        "/api/flights/plans",
        {"name": spec["name"], "steps": spec["steps"]},
    )
    plan_id = (
        ((body or {}).get("plan") or {}).get("id") if isinstance(body, dict) else None
    )
    if status >= 400 or not plan_id:
        log.append("route %s: plan refused %s %s" % (key, status, body))
        return None
    if spec["session"] is None:
        req = {"grounded": True, "plan_id": plan_id, "name": spec["name"]}
    else:
        req = {"plan_id": plan_id, "title": spec["session"]}
    status, body = call(sb.base_url, "POST", "/api/flights/start", req)
    if status >= 400:
        log.append("route %s: start refused %s %s" % (key, status, body))
        return None
    return ((body or {}).get("flight") or {}).get("id")


def seed(sb: Sandbox, name: str) -> Dict[str, Any]:
    """Make scenario ``name`` on a booted sandbox; returns a summary."""
    sc = get(name)
    log: List[str] = []
    made: Dict[str, Any] = {
        "scenario": name,
        "sessions": [],
        "routes": {},
        "problems": log,
    }
    if not sc["sessions"] and not sc["routes"]:
        return made
    repos = make_repos(sb)
    sb.set_roles(
        {
            t: dict(SESSIONS[t]["role"], prompt=SESSIONS[t]["prompt"])
            for t in sc["sessions"]
        }
    )
    # Parents before their workers; one at a time (a launch is a tmux
    # session + a worktree; batching them gains nothing at this size).
    order = sorted(sc["sessions"], key=lambda t: 1 if SESSIONS[t].get("parent") else 0)
    for title in order:
        if create_session(sb, title, repos, log):
            made["sessions"].append(title)
    flights: Dict[str, str] = {}
    for key in sc["routes"]:
        spec = ROUTES[key]
        if spec["session"] and spec["session"] not in made["sessions"]:
            log.append("route %s: its session %s is missing" % (key, spec["session"]))
            continue
        fid = start_route(sb, key, log)
        if fid:
            flights[key] = fid
    for key, fid in flights.items():
        spec = ROUTES[key]
        got = _wait(
            lambda: _flight(sb, fid).get("state") in spec["want"] and _flight(sb, fid),
            45,
        )
        state = (got or _flight(sb, fid)).get("state")
        if not got:
            log.append(
                "route %s: state %s, wanted %s" % (key, state, "/".join(spec["want"]))
            )
        if spec.get("then") == "delete_session":
            status, _ = call(sb.base_url, "DELETE", "/api/instances/" + spec["session"])
            if status >= 400:
                log.append(
                    "route %s: could not delete %s (%s)"
                    % (key, spec["session"], status)
                )
            else:
                made["sessions"].remove(spec["session"])
                state = "gone"
        made["routes"][key] = {"id": fid, "name": spec["name"], "state": state}
    if sc.get("todos"):
        items = [t for t in TODOS if t["session"] in made["sessions"]]
        if items:
            status, body = call(
                sb.base_url, "POST", "/api/todo/items", {"items": items}
            )
            if status >= 400:
                log.append("todos refused %s %s" % (status, body))
    if sc.get("hub"):
        from mfpreview.hubseed import seed_hub

        made["hub"] = seed_hub(sb, made["sessions"], log, full=sc["hub"] == "full")
    if sc.get("worker_report") and {"orchestrator", "worker-api"} <= set(
        made["sessions"]
    ):
        status, body = call(
            sb.base_url,
            "POST",
            "/api/instances/orchestrator/messages",
            {
                "text": "Couldn't finish: the stats endpoint returns 500 on the fixture data.",
                "from": "worker-api",
                "kind": "result",
                "data": {"status": "failed"},
                "delivery": "inbox",
            },
        )
        if status >= 400:
            log.append("worker report refused %s %s" % (status, body))
    return made
