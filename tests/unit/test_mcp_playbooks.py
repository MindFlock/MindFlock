"""Playbooks: the named orchestration prompts the UI pastes into an agent.

``backend.mcp.playbooks`` (registry, argument validation, rendering, the New
Session decoration) and the two routes over it: ``GET /api/playbooks`` (the
menu, with each item's availability) and ``POST /api/playbooks/{id}/render``.
The registry is pure; the routes run against a private registry with the
activity probe, the attach capability and the launch record stubbed.
"""

from __future__ import annotations

import re
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend.mcp import playbooks
from backend.mcp.tools import build_tools
from backend.providers import mcp_attach
from backend.session import tmux
from backend.web import server
from backend.web.core import mailbox
from backend.web.server import app
from tests.unit._mcp_fakes import make_box, row

client = TestClient(app)

_TOOL_REF = re.compile(r"mcp__mindflock__([a-z_]+)")


def _all_renders():
    """Every playbook rendered with empty and with filled arguments, for
    both tool spellings and a context with reported workers."""
    ctx_children = [
        {"title": "api-billing", "reported": True},
        {"title": "api-search", "reported": False},
    ]
    filled = {
        "split": {"task": "add per-user rate limiting"},
        "ask": {"session": "api-search", "question": "which limiter class?"},
        "workers": {},
        "wrapup": {"only": "api-billing"},
    }
    empty = {"split": {}, "ask": {"session": "api-search"}, "workers": {}, "wrapup": {}}
    for provider in ("claude", "codex", ""):
        ctx = {"provider": provider, "branch": "api", "children": ctx_children}
        for pid in playbooks.IDS:
            for args in (empty[pid], filled[pid]):
                yield pid, provider, args, playbooks.render(pid, args, ctx)


# --------------------------------------------------------------------------- #
# Registry                                                                     #
# --------------------------------------------------------------------------- #
def test_v1_registry_ids_letters_and_whens():
    assert playbooks.IDS == ("split", "ask", "workers", "wrapup")
    reg = {p["id"]: p for p in playbooks.registry()}
    assert {k: (v["letter"], v["when"]) for k, v in reg.items()} == {
        "split": ("S", "any"),
        "ask": ("A", "any"),
        "workers": ("C", "has_children"),
        "wrapup": ("W", "has_children"),
    }
    assert reg["split"]["label"] == "Split across workers…"
    assert reg["ask"]["label"] == "Ask a session…"
    assert reg["workers"]["label"] == "Check on workers"
    assert reg["wrapup"]["label"] == "Wrap up workers"


def test_registry_arg_shapes():
    reg = {p["id"]: p for p in playbooks.registry()}
    assert reg["split"]["args"] == [
        {"name": "task", "label": "Task", "kind": "text", "required": False}
    ]
    assert reg["ask"]["args"] == [
        {"name": "session", "label": "Session", "kind": "session", "required": True},
        {"name": "question", "label": "Question", "kind": "text", "required": False},
    ]
    assert reg["workers"]["args"] == []
    assert reg["wrapup"]["args"] == [
        {
            "name": "only",
            "label": "Only this worker",
            "kind": "session",
            "required": False,
        }
    ]
    for pb in reg.values():
        assert set(pb) == {"id", "label", "desc", "letter", "args", "when"}
        for a in pb["args"]:
            assert a["kind"] in playbooks.ARG_KINDS


def test_registry_returns_copies_and_fills_the_title():
    one = playbooks.registry("api")
    one[0]["args"].append({"name": "x"})
    one[1]["label"] = "mutated"
    two = playbooks.registry("api")
    assert len(two[0]["args"]) == 1 and two[1]["label"] == "Ask a session…"
    assert "api waits" in two[1]["desc"]
    assert "this session waits" in playbooks.registry()[1]["desc"]
    assert playbooks.get("nope") is None
    assert playbooks.get("split")["id"] == "split"


@pytest.mark.parametrize(
    "pid,has_children,shown",
    [
        ("split", False, True),
        ("ask", False, True),
        ("workers", False, False),
        ("wrapup", False, False),
        ("workers", True, True),
        ("wrapup", True, True),
        ("nope", True, False),
    ],
)
def test_when_filtering(pid, has_children, shown):
    assert playbooks.visible(pid, has_children) is shown


# --------------------------------------------------------------------------- #
# Templates                                                                    #
# --------------------------------------------------------------------------- #
def test_tool_names_are_exactly_the_servers_tools():
    box, _, _ = make_box([row("orch")])
    assert set(playbooks.TOOL_NAMES) == {t.name for t in build_tools(box)}


def test_every_tool_a_template_names_exists():
    real = set(playbooks.TOOL_NAMES)
    for pid, provider, _args, text in _all_renders():
        named = set(_TOOL_REF.findall(text))
        if provider == "claude":
            assert named, pid
            assert named <= real, (pid, named - real)
        else:
            # Other CLIs get bare names — and never Claude's spelling.
            assert not named, (pid, provider)
            assert any(re.search(r"\b%s\b" % t, text) for t in real), pid


@pytest.mark.parametrize("pid", playbooks.IDS)
def test_templates_are_one_short_paragraph(pid):
    """≤ 600 characters with the arguments empty (Claude collapses a longer
    paste into "[Pasted text]"), and never a newline in any render."""
    ctx = {
        "provider": "claude",
        "branch": "b" * playbooks.MAX_SESSION_CHARS,
        "children": [{"title": "w" * 40, "reported": True}] * 8,
    }
    args = {"session": "s" * playbooks.MAX_SESSION_CHARS} if pid == "ask" else {}
    text = playbooks.render(pid, args, ctx)
    assert len(text) <= playbooks.MAX_TEMPLATE_CHARS, (pid, len(text))
    for _pid, _prov, _args, rendered in _all_renders():
        assert "\n" not in rendered and "\r" not in rendered


def test_rendering_is_idempotent():
    first = list(_all_renders())
    assert first == list(_all_renders())


def test_empty_text_args_end_on_the_lead_in():
    assert playbooks.render("split", {}).endswith("The task: ")
    assert playbooks.render("split", {"task": "   "}).endswith("The task: ")
    assert playbooks.render("ask", {"session": "w"}).endswith("The question: ")
    filled = playbooks.render("split", {"task": "add a limiter"})
    assert filled.endswith("The task: add a limiter")


def test_split_semantics():
    text = playbooks.render("split", {}, {"provider": "claude"})
    order = [
        "mcp__mindflock__whoami",
        "groundwork",
        "disjoint files",
        "mcp__mindflock__spawn_session",
        "mcp__mindflock__wait_for_session",
        "read-only prompts",
        "mcp__mindflock__get_diff",
        "merge",
        "tests",
        "Ask me before mcp__mindflock__kill_session with mode delete",
    ]
    pos = [text.index(s) for s in order]
    assert pos == sorted(pos), text


def test_ask_semantics_quote_the_session():
    text = playbooks.render(
        "ask", {"session": 'api "search"', "question": "q?"}, {"provider": "codex"}
    )
    assert text.index("send_message") < text.index("wait_for_message")
    assert "\"api 'search'\"" in text
    assert text.endswith("The question: q?")


def test_workers_semantics():
    text = playbooks.render("workers", {}, {"provider": "claude"})
    assert "mcp__mindflock__list_sessions with filter children" in text
    assert "one line per worker" in text
    assert "flag every other prompt to me" in text


def test_wrapup_names_reported_workers_and_the_branch():
    ctx = {
        "provider": "claude",
        "branch": "feature/api",
        "children": [
            {"title": "api-billing", "reported": True},
            {"title": "api-search", "reported": False},
            {"title": "api-upload", "reported": True},
        ],
    }
    text = playbooks.render("wrapup", {}, ctx)
    assert "(api-billing, api-upload)" in text and "api-search" not in text
    assert "into feature/api" in text
    assert "full test suite" in text and "Stop and tell me" in text
    assert text.index("mcp__mindflock__get_diff") < text.index("kill_session")
    only = playbooks.render("wrapup", {"only": "api-search"}, ctx)
    assert 'worker "api-search"' in only and "api-billing" not in only
    none = playbooks.render("wrapup", {}, {"provider": "claude"})
    assert "each one that has reported" in none and "your branch" in none


def test_wrapup_summarizes_a_long_worker_list():
    ctx = {
        "children": [
            {"title": "w%02d-" % i + "x" * 20, "reported": True} for i in range(9)
        ]
    }
    text = playbooks.render("wrapup", {}, ctx)
    assert "(9 workers)" in text


def test_tool_name_spelling():
    assert playbooks.tool_name("get_diff", "claude") == "mcp__mindflock__get_diff"
    assert playbooks.tool_name("get_diff", " Claude ") == "mcp__mindflock__get_diff"
    assert playbooks.tool_name("get_diff", "codex") == "get_diff"
    with pytest.raises(ValueError):
        playbooks.tool_name("rm_rf", "claude")


# --------------------------------------------------------------------------- #
# Arguments                                                                    #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "pid,args,msg",
    [
        ("nope", {}, "unknown playbook"),
        ("split", "task", "args must be an object"),
        ("split", {"bogus": "x"}, "takes no argument bogus"),
        ("workers", {"only": "x"}, "it takes: nothing"),
        ("split", {"task": 3}, "task must be a string"),
        ("ask", {}, "session is required"),
        ("ask", {"session": "  "}, "session is required"),
        ("ask", {"session": "s" * 257}, "too long"),
        ("split", {"task": "t" * 4001}, "too long"),
    ],
)
def test_bad_args(pid, args, msg):
    with pytest.raises(playbooks.PlaybookError, match=msg):
        playbooks.validate_args(pid, args)


def test_args_are_flattened_to_one_safe_line():
    clean = playbooks.validate_args(
        "split", {"task": "line one\nline two\r\n\tthree\x1b[2J‮"}
    )
    assert clean == {"task": "line one line two three[2J"}
    assert playbooks.validate_args("split", None) == {"task": ""}
    text = playbooks.render("split", {"task": "a\nb"})
    assert text.endswith("The task: a b")


# --------------------------------------------------------------------------- #
# decorate_prompt (the New Session "Split across workers")                     #
# --------------------------------------------------------------------------- #
def test_decorate_prompt_keeps_the_task_first_and_is_idempotent():
    once = playbooks.decorate_prompt(
        "split", "add rate limiting", {"provider": "claude"}
    )
    first, _, rest = once.partition("\n\n")
    assert first == playbooks.SPLIT_HEADER + "add rate limiting"
    assert "mcp__mindflock__spawn_session" in rest
    assert playbooks.decorate_prompt("split", once, {"provider": "claude"}) == once
    codex = playbooks.decorate_prompt("split", "x", {"provider": "codex"})
    assert "mcp__" not in codex and "spawn_session" in codex


def test_decorate_prompt_refusals():
    with pytest.raises(playbooks.PlaybookError, match="only the split"):
        playbooks.decorate_prompt("wrapup", "x")
    with pytest.raises(playbooks.PlaybookError, match="needs a task"):
        playbooks.decorate_prompt("split", "   ")


# --------------------------------------------------------------------------- #
# Routes                                                                       #
# --------------------------------------------------------------------------- #
class _Inst:
    def __init__(self, title, parent="", program="claude", branch="b"):
        self.Title = title
        self.Parent = parent
        self.Program = program
        self.Branch = branch
        self.CreatedAt = None

    def GetWorktreePath(self):  # noqa: N802
        return ""


@pytest.fixture
def env(monkeypatch):
    instances: dict = {}
    monkeypatch.setattr(server.ENGINE, "instances", instances)
    state = SimpleNamespace(
        activity="idle",
        live=None,
        caps={"enabled": True, "providers": ["claude", "codex"]},
    )
    monkeypatch.setattr(server, "_agent_activity_cached", lambda i, t: state.activity)
    monkeypatch.setattr(
        server, "_agent_activity", lambda i, t: state.live or state.activity
    )
    monkeypatch.setattr(server, "_agent_mcp_caps", lambda: dict(state.caps))
    launches = {}
    monkeypatch.setattr(mcp_attach, "_LAUNCHED", launches)

    def add(title, **kw):
        instances[title] = _Inst(title, **kw)
        return instances[title]

    def launched(title, attached):
        launches[tmux.to_mindflock_tmux_name(title)] = attached

    add("api")
    return SimpleNamespace(instances=instances, state=state, add=add, launched=launched)


def _menu(title="api"):
    r = client.get("/api/playbooks", params={"title": title})
    assert r.status_code == 200, r.text
    return r.json()["playbooks"]


def test_list_without_title_is_the_whole_registry(env):
    r = client.get("/api/playbooks")
    assert r.status_code == 200
    items = r.json()["playbooks"]
    assert [p["id"] for p in items] == list(playbooks.IDS)
    assert all(p["available"] is True and p["disabled_reason"] is None for p in items)
    assert items[0]["args"][0]["name"] == "task"


def test_list_unknown_title_is_404(env):
    assert client.get("/api/playbooks", params={"title": "ghost"}).status_code == 404


def test_menu_omits_worker_playbooks_without_live_children(env):
    assert [p["id"] for p in _menu()] == ["split", "ask"]
    env.add("api-billing", parent="api")
    items = _menu()
    assert [p["id"] for p in items] == ["split", "ask", "workers", "wrapup"]
    assert all(p["available"] for p in items)
    # A child of SOMEONE ELSE does not count.
    env.add("other")
    env.add("o-w", parent="other")
    assert [p["id"] for p in _menu("api-billing")] == ["split", "ask"]


def test_menu_desc_names_the_session(env):
    ask = next(p for p in _menu() if p["id"] == "ask")
    assert "api waits" in ask["desc"]


@pytest.mark.parametrize(
    "setup,reason",
    [
        ({"caps": {"enabled": False, "providers": ["claude"]}}, server._PB_NO_TOOLS),
        ({"caps": {"enabled": True, "providers": ["codex"]}}, server._PB_NO_TOOLS),
        ({"attached": False}, server._PB_NOT_ATTACHED),
        ({"activity": "clarify"}, server._PB_IN_DIALOG),
        ({"activity": "limit"}, server._PB_IN_DIALOG),
    ],
)
def test_menu_disabled_reasons(env, setup, reason):
    if "caps" in setup:
        env.state.caps = setup["caps"]
    if "attached" in setup:
        env.launched("api", setup["attached"])
    if "activity" in setup:
        env.state.activity = setup["activity"]
    items = _menu()
    assert items and all(
        p["available"] is False and p["disabled_reason"] == reason for p in items
    )


def test_menu_reason_precedence_and_unknown_attach(env):
    # Unknown (None: launched by another process) never disables.
    assert all(p["available"] for p in _menu())
    env.launched("api", True)
    assert all(p["available"] for p in _menu())
    # A CLI with no tools at all outranks "restart it" and "answer first".
    env.launched("api", False)
    env.state.activity = "clarify"
    assert _menu()[0]["disabled_reason"] == server._PB_NOT_ATTACHED
    env.state.caps = {"enabled": True, "providers": []}
    assert _menu()[0]["disabled_reason"] == server._PB_NO_TOOLS


def test_menu_for_an_aider_session(env):
    env.add("aid", program="aider")
    items = _menu("aid")
    assert all(p["disabled_reason"] == server._PB_NO_TOOLS for p in items)


def _render(pid, body):
    return client.post("/api/playbooks/%s/render" % pid, json=body)


def test_render_split_for_claude_and_codex(env):
    r = _render("split", {"title": "api", "args": {}})
    assert r.status_code == 200, r.text
    assert r.json()["text"] == playbooks.render("split", {}, {"provider": "claude"})
    assert r.json()["text"].endswith("The task: ")
    env.add("cx", program="codex")
    text = _render("split", {"title": "cx"}).json()["text"]
    assert "mcp__" not in text and "spawn_session" in text


def test_render_wrapup_uses_branch_and_reports(env, monkeypatch):
    env.instances["api"].Branch = "feature/api"
    env.add("api-billing", parent="api")
    env.add("api-search", parent="api")
    mailbox.post(
        "api",
        "billing done",
        sender="api-billing",
        kind="result",
        data={"status": "done"},
    )
    text = _render("wrapup", {"title": "api"}).json()["text"]
    assert "(api-billing)" in text and "into feature/api" in text
    only = _render("wrapup", {"title": "api", "args": {"only": "api-search"}})
    assert only.status_code == 200 and '"api-search"' in only.json()["text"]


@pytest.mark.parametrize(
    "pid,body,status,msg",
    [
        ("nope", {"title": "api"}, 400, "unknown playbook"),
        ("split", {}, 400, "title is required"),
        ("split", {"title": "ghost"}, 404, "instance not found"),
        ("split", {"title": "api", "args": {"x": "1"}}, 400, "takes no argument"),
        ("ask", {"title": "api", "args": {}}, 400, "session is required"),
        ("ask", {"title": "api", "args": {"session": "ghost"}}, 400, "live session"),
        ("ask", {"title": "api", "args": {"session": "api"}}, 400, "live session"),
        ("wrapup", {"title": "api", "args": {"only": "stranger"}}, 400, "workers"),
    ],
)
def test_render_errors(env, pid, body, status, msg):
    env.add("stranger")
    r = _render(pid, body)
    assert r.status_code == status, r.text
    assert msg in r.json()["error"]


def test_render_ask_another_session(env):
    env.add("api-search")
    r = _render("ask", {"title": "api", "args": {"session": "api-search"}})
    assert r.status_code == 200
    text = r.json()["text"]
    assert '"api-search"' in text and text.endswith("The question: ")


# --------------------------------------------------------------------------- #
# Review 2026-10-05                                                            #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "setup,reason",
    [
        ({"live": "clarify"}, server._PB_IN_DIALOG),
        ({"live": "limit"}, server._PB_IN_DIALOG),
        ({"attached": False}, server._PB_NOT_ATTACHED),
        ({"caps": {"enabled": False, "providers": []}}, server._PB_NO_TOOLS),
    ],
)
def test_render_refuses_a_paste_that_cannot_go_in_now(env, setup, reason):
    """The menu's view lags (memoized activity, a cached list): the render
    right before a paste probes LIVE and refuses — rendered text holds
    digits from titles ("api-billing-2"), and a digit picks a dialog option."""
    env.add("api-billing-2", parent="api")
    if "live" in setup:
        env.state.live = setup["live"]  # the memoized read still says idle
    if "attached" in setup:
        env.launched("api", setup["attached"])
    if "caps" in setup:
        env.state.caps = setup["caps"]
    r = _render("wrapup", {"title": "api"})
    assert r.status_code == 409, r.text
    assert r.json() == {"error": reason, "disabled_reason": reason}


def test_long_and_oddly_spaced_titles_are_matched_exactly(env):
    long = "Intake: " + "rate limit the billing service " * 3 + "(SC-1234)"
    assert len(long) > 80
    spaced = "api  search"
    env.add(long)
    env.add(spaced, parent="api")
    r = _render("ask", {"title": "api", "args": {"session": long}})
    assert r.status_code == 200, r.text
    text = r.json()["text"]
    assert len(text) <= playbooks.MAX_TEMPLATE_CHARS
    r = _render("wrapup", {"title": "api", "args": {"only": spaced}})
    assert r.status_code == 200, r.text
    assert '"api  search"' in r.json()["text"]


def test_a_long_title_is_shortened_only_in_the_text():
    title = "w" * 200
    assert playbooks.validate_args("ask", {"session": title}) == {
        "session": title,
        "question": "",
    }
    text = playbooks.render("ask", {"session": title})
    assert '"%s…"' % ("w" * (playbooks.MAX_SESSION_CHARS - 1)) in text


@pytest.mark.parametrize(
    "pid,args,name",
    [
        ("split", {"task": "t" * 200}, "task"),
        ("ask", {"session": "w", "question": "q" * 400}, "question"),
    ],
)
def test_a_text_argument_may_not_push_the_paste_past_600(pid, args, name):
    """Claude collapses a paste over 600 characters into "[Pasted text]"."""
    with pytest.raises(playbooks.PlaybookError, match="%s is too long to paste" % name):
        playbooks.render(pid, args, {"provider": "claude"})
    short = dict(args, **{name: "x"})
    assert len(playbooks.render(pid, short, {"provider": "claude"})) <= 600


def test_render_route_rejects_an_over_budget_question(env):
    env.add("api-search")
    r = _render(
        "ask",
        {"title": "api", "args": {"session": "api-search", "question": "q" * 400}},
    )
    assert r.status_code == 400 and "too long to paste" in r.json()["error"]
