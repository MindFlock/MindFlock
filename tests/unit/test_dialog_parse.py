"""Provider-owned dialog parsing, pinned against golden screens.

``tests/unit/data/dialogs/*.screen.txt`` are visible-screen captures (what
``tmux capture-pane -p -J`` returns) of the dialogs an agent blocks on. The
``claude2_*`` screens are REAL captures from a live run of Claude Code
2.1.289 (the MCP UX end-to-end runs, 2026-10-05) at 60 to 164 columns —
background sub-agent tabs, a side panel, redraw glitches, and the same
stop_session / spawn_session dialogs at several widths included (re-wrapped
to more widths by ``_render_claude2``, checked line for line against them);
the rest
reproduce earlier layouts: Claude
Code 2.1.x's Bash / Edit file / MCP Tool use permission prompts, its folder
trust gate and plan approval (plus a 1.x boxed prompt), and Codex's command /
edit approval overlay and trust screen. Each is parsed by its provider's
``parse_dialog`` and compared field by field; two screens that are NOT a live
dialog (a numbered list the agent printed, a Codex turn in progress) must
parse to None and fall back to the question line.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from backend import providers
from backend.providers import dialogs
from backend.providers.base import BaseProvider
from backend.providers.claude import ClaudeProvider

DATA = Path(__file__).parent / "data" / "dialogs"


def _screen(name: str) -> str:
    return (DATA / ("%s.screen.txt" % name)).read_text(encoding="utf-8")


def _claude():
    return providers.resolve("claude")


def _codex():
    return providers.resolve("codex")


def _opts(*rows):
    return [
        {"key": str(i), "label": label, "kind": kind}
        for i, (label, kind) in enumerate(rows, start=1)
    ]


_STANDARD_CLAUDE_NO = ("No, and tell Claude what to do differently", "no")

GOLDEN = {
    "claude_bash": (
        _claude,
        {
            "heading": "Bash command",
            "question": "Do you want to proceed?",
            "command": "uv add redis",
            "detail": "Add redis as a dependency",
            "options": _opts(
                ("Yes", "yes"),
                (
                    "Yes, and don't ask again for uv add commands in /home/dev/api",
                    "always",
                ),
                _STANDARD_CLAUDE_NO,
            ),
        },
        "Bash command — Add redis as a dependency. Do you want to proceed?",
    ),
    "claude_bash_boxed": (
        _claude,
        {
            "heading": "Bash command",
            "question": "Do you want to proceed?",
            "command": "rm -rf build/ && make test",
            "detail": "Clean the build directory and run the test suite",
            "options": _opts(
                ("Yes", "yes"),
                (
                    "Yes, and don't ask again for make test commands in /home/dev/api",
                    "always",
                ),
                _STANDARD_CLAUDE_NO,
            ),
        },
        "Bash command — Clean the build directory and run the test suite. "
        "Do you want to proceed?",
    ),
    "claude_edit_file": (
        _claude,
        {
            "heading": "Edit file",
            "question": "Do you want to make this edit to limits.py?",
            "command": None,
            "detail": "services/search/limits.py",
            "options": _opts(
                ("Yes", "yes"),
                ("Yes, allow all edits during this session", "always"),
                _STANDARD_CLAUDE_NO,
            ),
        },
        "Edit file — services/search/limits.py. "
        "Do you want to make this edit to limits.py?",
    ),
    "claude_mcp_tool": (
        _claude,
        {
            "heading": "Tool use",
            "question": "Do you want to proceed?",
            "command": 'mindflock - spawn_session(title: "api-billing", prompt: "Add '
            "per-user rate limiting to services/billing/ using "
            "common/ratelimit.RateLimiter. Only touch services/billing/ and its "
            'tests.") (MCP)',
            "detail": "Start a new MindFlock worker session forked from your HEAD commit",
            "options": _opts(
                ("Yes", "yes"),
                (
                    "Yes, and don't ask again for mindflock - spawn_session commands "
                    "in /home/dev/api",
                    "always",
                ),
                _STANDARD_CLAUDE_NO,
            ),
        },
        "Tool use — Start a new MindFlock worker session forked from your HEAD "
        "commit. Do you want to proceed?",
    ),
    "claude_trust_folder": (
        _claude,
        {
            "heading": "Accessing workspace:",
            "question": "Quick safety check: Is this a project you created or one "
            "you trust?",
            "command": None,
            "detail": "/home/dev/api",
            "options": _opts(("Yes, I trust this folder", "yes"), ("No, exit", "no")),
        },
        "Accessing workspace — /home/dev/api. Quick safety check: Is this a "
        "project you created or one you trust?",
    ),
    "claude_plan": (
        _claude,
        {
            "heading": "Ready to code?",
            "question": "Would you like to proceed?",
            "command": None,
            "detail": None,
            "options": _opts(
                ("Yes, and auto-accept edits", "always"),
                ("Yes, and manually approve edits", "yes"),
                ("No, keep planning", "no"),
            ),
        },
        "Ready to code? Would you like to proceed?",
    ),
    "codex_exec": (
        _codex,
        {
            "heading": None,
            "question": "Would you like to run the following command?",
            "command": "uv add redis",
            "detail": "Reason: add the redis client the shared search limiter needs",
            "options": _opts(
                ("Yes, just this once", "yes"),
                (
                    "Yes, and don't ask again for commands that start with `uv add`",
                    "always",
                ),
                ("No, and tell Codex what to do differently", "no"),
            ),
        },
        "Would you like to run the following command? Reason: add the redis "
        "client the shared search limiter needs",
    ),
    "codex_edits": (
        _codex,
        {
            "heading": None,
            "question": "Would you like to make the following edits?",
            "command": None,
            "detail": "Reason: wire the shared RateLimiter into the search handler",
            "options": _opts(
                ("Yes, proceed", "yes"),
                ("Yes, and don't ask again for these files", "always"),
                ("No, and tell Codex what to do differently", "no"),
            ),
        },
        "Would you like to make the following edits? Reason: wire the shared "
        "RateLimiter into the search handler",
    ),
    "codex_trust": (
        _codex,
        {
            "heading": None,
            "question": "Do you trust the contents of this directory?",
            "command": None,
            "detail": None,
            "options": _opts(("Yes, continue", "yes"), ("No, quit", "no")),
        },
        "Do you trust the contents of this directory?",
    ),
}


@pytest.mark.parametrize("name", sorted(GOLDEN))
def test_golden_screen_parses(name):
    prov, want, _question = GOLDEN[name]
    got = prov().parse_dialog(_screen(name))
    assert got is not None, name
    region = got.pop("region")
    # These layouts carry no tab header: nobody but the agent itself asked.
    assert got.pop("source") is None
    assert got == want
    # The region is the dialog itself: cursor-free, its options included.
    assert "❯" not in region and "›" not in region
    assert want["options"][-1]["label"].split(",")[0] in region


@pytest.mark.parametrize("name", sorted(GOLDEN))
def test_golden_screen_describe(name):
    prov, want, question = GOLDEN[name]
    screen = _screen(name)
    body = dialogs.describe(prov().parse_dialog(screen), screen)
    assert body == {
        "id": body["id"],
        "parsed": True,
        "question": question,
        "command": want["command"],
        "options": want["options"],
    }
    assert re.fullmatch(r"[0-9a-f]{12}", body["id"])


def test_golden_ids_are_all_different():
    ids = {
        dialogs.describe(GOLDEN[n][0]().parse_dialog(_screen(n)), _screen(n))["id"]
        for n in GOLDEN
    }
    assert len(ids) == len(GOLDEN)


@pytest.mark.parametrize(
    "name,parse,question",
    [
        ("claude_printed_list", "parse_claude", "Does that split look right to you?"),
        ("codex_working", "parse_codex", "gpt-5.5 medium · ~/dev/api"),
    ],
)
def test_not_a_live_dialog(name, parse, question):
    """A numbered list the agent PRINTED carries no selection cursor; a
    working Codex has no option list at all — neither is a dialog."""
    screen = _screen(name)
    assert getattr(dialogs, parse)(screen) is None
    body = dialogs.describe(None, screen)
    assert body["parsed"] is False and body["options"] == []
    assert body["command"] is None
    assert body["question"] == question


def test_the_fixtures_use_the_providers_own_wording():
    """The golden screens must be what the providers' classifiers know: each
    Claude permission screen matches ClaudeProvider's waiting patterns, the
    trust screen its trust patterns; each Codex approval its config's."""
    claude, codex = ClaudeProvider(), _codex()

    def waiting(prov, screen):
        return any(re.search(p, screen) for p in prov.waiting_prompt_patterns())

    for name in (
        "claude_bash",
        "claude_bash_boxed",
        "claude_edit_file",
        "claude_mcp_tool",
        "claude_plan",
    ):
        assert waiting(claude, _screen(name)), name
    trust = claude.trust_prompt().patterns
    assert any(p in _screen("claude_trust_folder") for p in trust)
    for name in ("codex_exec", "codex_edits"):
        assert waiting(codex, _screen(name)), name
    assert not waiting(claude, _screen("claude_printed_list"))


# --------------------------------------------------------------------------- #
# The id                                                                       #
# --------------------------------------------------------------------------- #
def _move_cursor(screen: str, glyph: str, to: int) -> str:
    """The same dialog with the selection cursor on option ``to``."""
    out = []
    for line in screen.split("\n"):
        m = re.match(r"^(\s*)(%s\s)?(\s*)([1-9])\.(.*)$" % re.escape(glyph), line)
        if m:
            indent = m.group(1)
            num = int(m.group(4))
            lead = (glyph + " ") if num == to else "  "
            line = indent + lead + m.group(4) + "." + m.group(5)
        out.append(line)
    return "\n".join(out)


@pytest.mark.parametrize(
    "name,glyph", [("claude_bash", "❯"), ("claude_edit_file", "❯"), ("codex_exec", "›")]
)
def test_id_survives_arrowing_through_the_options(name, glyph):
    screen = _screen(name)
    prov = GOLDEN[name][0]()
    base = dialogs.describe(prov.parse_dialog(screen), screen)["id"]
    for to in (2, 3):
        moved = _move_cursor(screen, glyph, to)
        assert moved != screen
        parsed = prov.parse_dialog(moved)
        assert parsed is not None
        assert dialogs.describe(parsed, moved)["id"] == base


def test_id_changes_with_the_dialog():
    screen = _screen("claude_bash")
    other = screen.replace("uv add redis", "uv add httpx").replace(
        "uv add commands", "uv add commands"
    )
    p1, p2 = ClaudeProvider().parse_dialog(screen), ClaudeProvider().parse_dialog(other)
    assert dialogs.dialog_id(p1, screen) != dialogs.dialog_id(p2, other)
    # Same edit dialog, different diff: a different prompt.
    edit = _screen("claude_edit_file")
    edit2 = edit.replace("window_s=60", "window_s=30")
    e1, e2 = ClaudeProvider().parse_dialog(edit), ClaudeProvider().parse_dialog(edit2)
    assert (e1["question"], e1["detail"]) == (e2["question"], e2["detail"])
    assert dialogs.dialog_id(e1, edit) != dialogs.dialog_id(e2, edit2)


def test_id_ignores_what_is_outside_the_dialog():
    screen = _screen("claude_bash")
    noisier = "● Earlier turn output\n" + screen
    p1 = ClaudeProvider().parse_dialog(screen)
    p2 = ClaudeProvider().parse_dialog(noisier)
    assert dialogs.dialog_id(p1, screen) == dialogs.dialog_id(p2, noisier)


def test_unparsed_id_is_stable_and_cursor_free():
    screen = "some app\n  ▶ choose a thing\n  pick one?\n"
    same = "some app\n    choose a thing\n  pick one?\n"
    assert dialogs.dialog_id(None, screen) == dialogs.dialog_id(None, same)
    assert dialogs.dialog_id(None, screen) != dialogs.dialog_id(None, "other?\n")


# --------------------------------------------------------------------------- #
# Parser edge cases                                                            #
# --------------------------------------------------------------------------- #
def test_claude_wrapped_option_label_is_joined():
    screen = (
        "─" * 60 + "\n"
        " Bash command\n\n"
        "   npm run build\n"
        "   Build the bundle\n\n"
        " Do you want to proceed?\n"
        " ❯ 1. Yes\n"
        "   2. Yes, and don't ask again for npm run build commands in\n"
        "      /home/dev/a/very/long/project/path\n"
        "   3. No, and tell Claude what to do differently (esc)\n"
    )
    got = ClaudeProvider().parse_dialog(screen)
    assert got["options"][1] == {
        "key": "2",
        "label": "Yes, and don't ask again for npm run build commands in "
        "/home/dev/a/very/long/project/path",
        "kind": "always",
    }


def test_claude_option_numbers_must_run_from_one_without_gaps():
    gap = "─" * 40 + "\n Bash command\n\n   ls\n\n Proceed?\n ❯ 1. Yes\n   3. No\n"
    assert ClaudeProvider().parse_dialog(gap) is None
    single = "─" * 40 + "\n Bash command\n\n   ls\n\n Proceed?\n ❯ 1. Yes\n"
    assert ClaudeProvider().parse_dialog(single) is None


def test_claude_needs_a_question():
    screen = "─" * 40 + "\n Pick a model\n\n ❯ 1. Opus\n   2. Sonnet\n"
    assert dialogs.parse_claude(screen) is None


@pytest.mark.parametrize(
    "label,kind",
    [
        ("Yes", "yes"),
        ("Yes, proceed (y)", "yes"),
        ("Yes, and don't ask again for this command", "always"),
        ("Yes, allow all edits during this session (shift+tab)", "always"),
        ("Yes, and auto-accept edits", "always"),
        ("Yes, and bypass permissions", "always"),
        ("Allow once", "yes"),
        ("Yes, I trust this folder", "yes"),
        ("No", "no"),
        ("No, exit", "no"),
        ("Cancel", "no"),
        ("Deny", "no"),
        ("Type something.", "other"),
        ("Opus 4.5", "other"),
        ("", "other"),
    ],
)
def test_option_kind(label, kind):
    assert dialogs.option_kind(label) == kind


def test_clean_label_strips_key_hints():
    assert dialogs.clean_label("No, and tell Codex  (esc)") == "No, and tell Codex"
    assert dialogs.clean_label("Yes, proceed (y)") == "Yes, proceed"
    assert dialogs.clean_label("Keep (draft)") == "Keep (draft)"


def test_describe_treats_an_optionless_parse_as_unparsed():
    body = dialogs.describe({"question": "Q?", "options": []}, "Q?\n")
    assert body["parsed"] is False and body["question"] == "Q?"


def test_long_fields_are_capped():
    cmd = "echo " + "x" * 900
    screen = (
        "─" * 40 + "\n Bash command\n\n   %s\n   Print\n\n Do you want to proceed?\n"
        " ❯ 1. Yes\n   2. No\n" % cmd
    )
    got = dialogs.parse_claude(screen)
    assert len(got["command"]) == dialogs.MAX_COMMAND_CHARS
    assert got["command"].endswith("…")


# --------------------------------------------------------------------------- #
# Provider hooks                                                               #
# --------------------------------------------------------------------------- #
def test_parse_dialog_is_provider_owned():
    screen = _screen("claude_bash")
    assert BaseProvider().parse_dialog(screen) is None
    assert providers.resolve("aider").parse_dialog(screen) is None
    assert _claude().parse_dialog(screen)["command"] == "uv add redis"
    assert _codex().parse_dialog(_screen("codex_exec"))["command"] == "uv add redis"
    # Each CLI reads only its own layout.
    assert _codex().parse_dialog(screen) is None


# --------------------------------------------------------------------------- #
# Not a live dialog: numbered prompts in the transcript (review 2026-10-05)    #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("name", ["claude_user_numbered", "claude_user_numbered_last"])
def test_a_numbered_user_prompt_in_the_transcript_is_no_dialog(name):
    """Claude's transcript marks the user's own prompts with ">": a prompt
    typed as "1. … 2. …" under a question must not become two buttons that
    send digits into whatever is really on screen."""
    screen = _screen(name)
    assert ClaudeProvider().parse_dialog(screen) is None
    assert dialogs.describe(None, screen)["parsed"] is False


def test_claude_takes_only_its_own_cursor():
    """ ">" is the input box and the transcript's prompt marker, never
    Claude's selection cursor (the golden screens all use "❯")."""
    screen = _screen("claude_bash").replace("❯ 1.", "> 1.")
    assert ClaudeProvider().parse_dialog(screen) is None


@pytest.mark.parametrize(
    "screen,parse",
    [
        (
            "─" * 60 + "\n Which first?\n ❯ 1. fix the lint\n   2. then the tests\n\n"
            "● On it — starting with the lint.\n",
            "parse_claude",
        ),
        (
            "• Which first?\n\n› 1. fix the lint\n  2. then the tests\n\n"
            "• On it — starting with the lint.\n\n› Ask Codex to do anything\n",
            "parse_codex",
        ),
    ],
    ids=["claude", "codex"],
)
def test_options_must_be_the_last_thing_on_screen(screen, parse):
    """Whatever the glyph, an option list with transcript (or the input
    box) under it is history, not the menu the CLI is waiting on."""
    assert getattr(dialogs, parse)(screen) is None


def test_claude_needs_its_top_rule():
    """Without the rule Claude draws above every dialog the region would
    run up into the transcript — no rule, no parse."""
    screen = "\n".join(
        ln for ln in _screen("claude_bash").split("\n") if not ln.startswith("───")
    )
    assert ClaudeProvider().parse_dialog(screen) is None


def test_a_rule_with_text_on_it_still_tops_the_dialog():
    """A pill drawn on the top rule ("↓ 2 new messages") must not let the
    region spill into the transcript: same heading, question, command and
    id as the plain rule, and the id ignores the transcript above."""
    plain = _screen("claude_bash")
    pill = _screen("claude_rule_with_text")
    want = ClaudeProvider().parse_dialog(plain)
    got = ClaudeProvider().parse_dialog(pill)
    assert got is not None
    assert got["heading"] == "Bash command"
    assert got["command"] == "uv add redis"
    assert got["question"] == want["question"]
    assert dialogs.dialog_id(got, pill) == dialogs.dialog_id(want, plain)
    scrolled = pill.replace("redis client", "redis connection pool")
    assert dialogs.dialog_id(
        ClaudeProvider().parse_dialog(scrolled), scrolled
    ) == dialogs.dialog_id(got, pill)


def test_plan_with_bypass_is_never_the_primary_kind():
    """ "Yes, and bypass permissions" switches the session into bypass mode:
    a standing approval, never the one-click "yes"."""
    got = ClaudeProvider().parse_dialog(_screen("claude_plan_bypass"))
    assert got is not None
    assert got["options"][0] == {
        "key": "1",
        "label": "Yes, and bypass permissions",
        "kind": "always",
    }


# --------------------------------------------------------------------------- #
# Real Claude Code 2.1.289 screens (live E2E run, 2026-10-05)                  #
# --------------------------------------------------------------------------- #
_GIT_COMMIT_ALWAYS = (
    "Yes, and don't ask again for git add and git commit -m ' commands in "
    "/home/emandel2630/mindflock-prototypes/mcp-ux/e2e-…"
)
_ACCEPT_EDITS = (
    "Yes, and switch to accept edits (auto-approve file edits and common file "
    "commands) for this session"
)
_CREATE_BYE = {
    "heading": "Create file",
    "question": "Do you want to create bye.txt?",
    "command": None,
    "detail": "bye.txt",
    "options": _opts(("Yes", "yes"), (_ACCEPT_EDITS, "always"), ("No", "no")),
    "source": None,
}
REAL = {
    # A worker's Bash prompt: the description sits under the heading, the
    # command in a dashed box under it (a heredoc, its blank line kept).
    "claude2_bash": {
        "heading": "Bash command",
        "question": "Do you want to proceed?",
        "command": "git add bye.txt && git commit -m \"$(cat <<'EOF'\n"
        "Add bye.txt with farewell message\n\n"
        'Co-Authored-By: Claude Haiku 4.5 <noreply@anthropic.com>\nEOF\n)"',
        "detail": "Add and commit bye.txt file",
        "options": _opts(("Yes", "yes"), (_GIT_COMMIT_ALWAYS, "always"), ("No", "no")),
        "source": None,
    },
    # The orchestrator running an MCP tool as a shell command (the one the
    # E2E run showed as command="Call MindFlock whoami…").
    "claude2_bash_whoami": {
        "heading": "Bash command",
        "question": "Do you want to proceed?",
        "command": "mcp__mindflock__whoami",
        "detail": "Call MindFlock whoami to identify my session",
        "options": _opts(
            ("Yes", "yes"),
            ("Yes, and don’t ask again for: mcp__mindflock__whoami *", "always"),
            ("No", "no"),
        ),
        "source": None,
    },
    "claude2_bash_subagent": {
        "heading": "Bash command",
        "question": "Do you want to proceed?",
        "command": "cat > /tmp/list_sessions.py << 'EOF'\nimport json\n"
        "import subprocess\n\n"
        "# For this prototype, we'll simulate the MindFlock API call\n"
        "# In a real environment, this would use the MindFlock client\n"
        'print("Attempting to call mcp__mindflock__list_sessions...")\nEOF\n'
        "python3 /tmp/list_sessions.py",
        "detail": "Set up mindflock call",
        "options": _opts(
            ("Yes", "yes"),
            (
                "Yes, and don't ask again for cat and python3 "
                "/tmp/list_sessions.py commands in "
                "/home/emandel2630/mindflock-prototypes/m…",
                "always",
            ),
            ("No", "no"),
        ),
        "source": "general-purpose agent",
    },
    # The main agent's own prompt with others queued: "8 of 8", no "from".
    "claude2_bash_tab_count": {
        "heading": "Bash command",
        "question": "Do you want to proceed?",
        "command": "git merge emandel2630/hello-worker emandel2630/bye-worker",
        "detail": "Merge both worker branches into current branch",
        "options": _opts(
            ("Yes", "yes"),
            ("Yes, and don’t ask again for: git merge *", "always"),
            ("No", "no"),
        ),
        "source": None,
    },
    # Claude's diff side panel drawn to the right of the dialog.
    "claude2_bash_side_panel": {
        "heading": "Bash command",
        "question": "Do you want to proceed?",
        "command": "git add hello.txt && git commit -m \"$(cat <<'EOF'\n"
        "Add hello.txt with greeting message\n"
        'Co-Authored-By: Claude Haiku 4.5 <noreply@anthropic.com>\nEOF\n)"',
        "detail": "Stage and commit hello.txt file",
        "options": _opts(("Yes", "yes"), (_GIT_COMMIT_ALWAYS, "always"), ("No", "no")),
        "source": None,
    },
    # A background sub-agent's MCP tool call: the tab header is the
    # source, the command LEADS with the first argument (the worker title).
    "claude2_tool_use_wide": {
        "heading": "Tool use",
        "question": "Do you want to proceed?",
        "command": "bye-worker · mindflock — Spawn worker session",
        "detail": 'title: "bye-worker", prompt: Create a file bye.txt with the '
        "content 'Goodbye!' and commit it. Use git add bye.txt and git commit. "
        "Then call mcp__mindflock__report_result with status=done.",
        "options": _opts(
            ("Yes", "yes"),
            (
                "Yes, and don't ask again for mindflock — Spawn worker session "
                "commands in ~/.mindflock/worktrees/emandel2630/add-hello-…",
                "always",
            ),
            ("No", "no"),
        ),
        "source": "general-purpose agent",
    },
    "claude2_tool_use_tab": {
        "heading": "Tool use",
        "question": "Do you want to proceed?",
        "command": "hello-worker · mindflock — Spawn worker session",
        "detail": 'title: "hello-worker", prompt: Create a file hello.txt with '
        "the content 'Hello, World!' and commit it. Use git add hello.txt and "
        "git commit. Then call mcp__mindflock__report_result with status=done.",
        "options": _opts(("Yes", "yes"), ("No", "no")),
        "source": "general-purpose agent",
    },
    "claude2_answer_prompt_tab": {
        "heading": "Tool use",
        "question": "Do you want to proceed?",
        "command": "hello-worker · mindflock — Answer a blocked prompt",
        "detail": 'title: "hello-worker", keys: ["1", "Enter"]',
        "options": _opts(("Yes", "yes"), ("No", "no")),
        "source": "general-purpose agent",
    },
    # The same Create file dialog at 164 and at 79 columns (its option 2
    # wraps at 79).
    "claude2_create_file_wide": _CREATE_BYE,
    "claude2_create_file_narrow": _CREATE_BYE,
}

# --- Second live run (2026-10-05): one dialog at several widths ------------- #
_STOP_ARGS = 'title: "add-hello-and-bye-files-w2", mode: "delete"'
_STOP_CMD = "add-hello-and-bye-files-w2 · mindflock — Stop a session"
_STOP_ALWAYS = (
    "Yes, and don't ask again for mindflock — Stop a session commands in "
    "~/.mindflock/"
)
_SPAWN_BYE = {
    "heading": "Tool use",
    "question": "Do you want to proceed?",
    # Led by the TITLE although Claude lists the prompt first (defect E).
    "command": "add-hello-and-bye-files-w2 · mindflock — Spawn worker session",
    "detail": 'prompt: Create a bye.txt file containing "Goodbye, World!" You own '
    "this file only. Commit your changes with a clear message., "
    'title: "add-hello-and-bye-files-w2"',
    "source": None,
}
_SPAWN_ALWAYS = (
    "Yes, and don't ask again for mindflock — Spawn worker session commands in "
)
_ECHO_AGAIN = (
    'echo "Hello again!" >> /home/emandel2630/mindflock-prototypes/mcp-ux/'
    "e2e-live2/sandbox/home/.mindflock/worktrees/emandel2630/"
    "add-hello-and-bye-files-w1_18dbaaac6caccdd9/hello.txt"
)
_ALLOW_ACCESS = (
    "Yes, and always allow access to /home/emandel2630/mindflock-prototypes/"
    "mcp-ux/e2e-live2/sandbox/home/.mindflock/worktre…"
)


def _tool(command, detail, *options):
    return {
        "heading": "Tool use",
        "question": "Do you want to proceed?",
        "command": command,
        "detail": detail,
        "options": _opts(*options),
        "source": None,
    }


REAL.update(
    {
        # stop_session at 80, 100 and 164 columns: option 2 cut to the width
        # ("…/.mindflock/work…" at 100) — and gone at 80, where Claude draws
        # the 2-option variant.
        "claude2_stop_session_w80": _tool(
            _STOP_CMD, _STOP_ARGS, ("Yes", "yes"), ("No", "no")
        ),
        "claude2_stop_session_w100": _tool(
            _STOP_CMD,
            _STOP_ARGS,
            ("Yes", "yes"),
            (_STOP_ALWAYS + "work…", "always"),
            ("No", "no"),
        ),
        "claude2_stop_session_w164": _tool(
            _STOP_CMD,
            _STOP_ARGS,
            ("Yes", "yes"),
            (_STOP_ALWAYS + "worktrees/emandel2630/add-hello-and-by…", "always"),
            ("No", "no"),
        ),
        # spawn_session at the 800 / 1100 / 1600 px browser widths (62, 79,
        # 100, 120, 164 columns).
        "claude2_spawn_w62": dict(
            _SPAWN_BYE, options=_opts(("Yes", "yes"), ("No", "no"))
        ),
        "claude2_spawn_w79": dict(
            _SPAWN_BYE, options=_opts(("Yes", "yes"), ("No", "no"))
        ),
        "claude2_spawn_w100": dict(
            _SPAWN_BYE,
            options=_opts(
                ("Yes", "yes"), (_SPAWN_ALWAYS + "~/.mindfloc…", "always"), ("No", "no")
            ),
        ),
        "claude2_spawn_w120": dict(
            _SPAWN_BYE,
            options=_opts(
                ("Yes", "yes"),
                (_SPAWN_ALWAYS + "~/.mindflock/worktrees/emandel2…", "always"),
                ("No", "no"),
            ),
        ),
        "claude2_spawn_w164": dict(
            _SPAWN_BYE,
            options=_opts(
                ("Yes", "yes"),
                (
                    _SPAWN_ALWAYS + "~/.mindflock/worktrees/emandel2630/add-hello-…",
                    "always",
                ),
                ("No", "no"),
            ),
        ),
        "claude2_spawn_hello_w164": dict(
            _SPAWN_BYE,
            command="add-hello-and-bye-files-w1 · mindflock — Spawn worker session",
            detail='prompt: Create a hello.txt file containing "Hello, World!" You '
            "own this file only. Commit your changes with a clear message., "
            'title: "add-hello-and-bye-files-w1"',
            options=_opts(
                ("Yes", "yes"),
                (
                    _SPAWN_ALWAYS + "~/.mindflock/worktrees/emandel2630/add-hello-…",
                    "always",
                ),
                ("No", "no"),
            ),
        ),
        # A sub-agent's Bash prompt whose path the box hard-wraps mid-token
        # (79 columns), and the same prompt caught mid-redraw at 60 ("3. No"
        # over the tail of "project"): the path is ONE token either way.
        "claude2_bash_hard_wrap_w79": {
            "heading": "Bash command",
            "question": "Do you want to proceed?",
            "command": _ECHO_AGAIN,
            "detail": 'Append "Hello again!" to hello.txt',
            "options": _opts(("Yes", "yes"), (_ALLOW_ACCESS, "always"), ("No", "no")),
            "source": "general-purpose agent",
        },
        "claude2_bash_hard_wrap_w60_redraw": {
            "heading": "Bash command",
            "question": "Do you want to proceed?",
            # Word-wrapped before the path: a break that stops short of the
            # edge reads as the command's own (see _wrap_joint).
            "command": _ECHO_AGAIN.replace(">> /", ">>\n/"),
            "detail": 'Append "Hello again!" to hello.txt',
            "options": _opts(
                ("Yes", "yes"), (_ALLOW_ACCESS, "always"), ("Nooject", "other")
            ),
            "source": "general-purpose agent",
        },
        # The heredoc commit of e-w1-bash-dialog.json: its own newlines kept.
        "claude2_bash_heredoc_w79": {
            "heading": "Bash command",
            "question": "Do you want to proceed?",
            "command": "git add hello.txt && git commit -m \"$(cat <<'EOF'\n"
            "Add hello.txt with greeting message\n\n"
            "Create initial hello.txt file containing 'Hello, World!' greeting.\n"
            'EOF\n)"',
            "detail": "Stage and commit hello.txt file",
            "options": _opts(
                ("Yes", "yes"), (_GIT_COMMIT_ALWAYS, "always"), ("No", "no")
            ),
            "source": None,
        },
    }
)


@pytest.mark.parametrize("name", sorted(REAL))
def test_real_screen_parses(name):
    got = ClaudeProvider().parse_dialog(_screen(name))
    assert got is not None, name
    region = got.pop("region")
    assert got == REAL[name]
    # One whitespace-free line per component.
    assert "❯" not in region
    assert all(part and not re.search(r"\s", part) for part in region.split("\n"))


@pytest.mark.parametrize("name", sorted(REAL))
def test_real_question_carries_no_tab_header(name):
    """ "Tool use · from the general-purpose agent 2 of 3 — …" was the
    question line; the tab header is attribution (``source``), not part of
    what is asked."""
    screen = _screen(name)
    body = dialogs.describe(ClaudeProvider().parse_dialog(screen), screen)
    assert "from the" not in body["question"]
    assert not re.search(r"\d+ of \d+", body["question"])
    if REAL[name]["source"]:
        assert body["source"] == REAL[name]["source"]
    else:
        assert "source" not in body


def test_bash_command_is_the_command_line_and_the_description_the_question():
    """Claude Code 2.x puts the description under the heading and the command
    in a box: ``command`` used to hold the description and the real command
    landed in the question (E2E defect E)."""
    screen = _screen("claude2_bash_whoami")
    body = dialogs.describe(ClaudeProvider().parse_dialog(screen), screen)
    assert body["command"] == "mcp__mindflock__whoami"
    assert body["question"] == (
        "Bash command — Call MindFlock whoami to identify my session. "
        "Do you want to proceed?"
    )


def test_tool_use_command_leads_with_the_argument_that_names_the_call():
    """The rail has a row's width: "mindflock — Spawn worker s…" told the
    user nothing about WHICH worker. The lead is chosen by NAME — title,
    then session/to/target, then the first argument: Claude lists the
    arguments in the order the agent wrote them, and spawn_session's prompt
    came first in the live run, so "Create a hello.txt file…" led the strip
    (E2E defect E)."""
    for name, lead in (
        ("claude2_tool_use_tab", "hello-worker"),
        ("claude2_tool_use_wide", "bye-worker"),
        ("claude2_answer_prompt_tab", "hello-worker"),
        ("claude2_spawn_hello_w164", "add-hello-and-bye-files-w1"),
        ("claude2_spawn_w79", "add-hello-and-bye-files-w2"),
        ("claude2_stop_session_w100", "add-hello-and-bye-files-w2"),
    ):
        got = ClaudeProvider().parse_dialog(_screen(name))
        assert got["command"].startswith(lead + " · "), name


@pytest.mark.parametrize(
    "args,lead",
    [
        ([("prompt", "do it"), ("title", '"api"')], "api"),
        ([("keys", '["1"]'), ("session", '"w1"')], "w1"),
        ([("text", '"hi"'), ("to", '"w2"')], "w2"),
        ([("mode", '"x"'), ("target", '"w3"')], "w3"),
        ([("session", '"s"'), ("title", '"t"')], "t"),
        ([("prompt", '"p"'), ("mode", '"m"')], "p"),
        ([("title", ""), ("prompt", '"p"')], "p"),
        ([], ""),
    ],
)
def test_lead_argument_by_name(args, lead):
    assert dialogs._lead_arg(args) == lead


def test_id_does_not_depend_on_the_terminal_width():
    """The same Create file dialog captured at 164 and at 79 columns (its
    option 2 wraps at 79): one id. It used to be 3cee04e7f2b4 at 79 and
    e36c860ddc6c at 120/160, so the first click after a resize was refused
    as "the prompt changed" (E2E defect C)."""
    wide, narrow = _screen("claude2_create_file_wide"), _screen(
        "claude2_create_file_narrow"
    )
    p_wide = ClaudeProvider().parse_dialog(wide)
    p_narrow = ClaudeProvider().parse_dialog(narrow)
    assert dialogs.dialog_id(p_wide, wide) == dialogs.dialog_id(p_narrow, narrow)


def _rewrap(screen: str, width: int) -> str:
    """``screen`` with every option label re-wrapped to ``width`` columns —
    what Claude does to the same dialog in a narrower or wider pane."""
    import textwrap

    lines = screen.split("\n")
    out: list = []
    i = 0
    opt = re.compile(r"^(\s*(?:❯\s)?\s*)([1-9]\.\s)(.*)$")
    while i < len(lines):
        m = opt.match(lines[i])
        if not m:
            out.append(lines[i])
            i += 1
            continue
        head, num, text = m.groups()
        pad = " " * (len(head) + len(num))
        i += 1
        while i < len(lines) and lines[i].startswith(pad) and lines[i].strip():
            if opt.match(lines[i]):
                break
            text += " " + lines[i].strip()
            i += 1
        wrapped = textwrap.wrap(text, max(10, width - len(pad)))
        out.append(head + num + wrapped[0])
        out.extend(pad + w for w in wrapped[1:])
    return "\n".join(out)


@pytest.mark.parametrize(
    "name", ["claude2_bash", "claude2_bash_subagent", "claude2_tool_use_wide"]
)
@pytest.mark.parametrize("width", [60, 79, 120, 200])
def test_id_survives_rewrapping_the_options(name, width):
    screen = _screen(name)
    base = dialogs.dialog_id(ClaudeProvider().parse_dialog(screen), screen)
    other = _rewrap(screen, width)
    parsed = ClaudeProvider().parse_dialog(other)
    assert parsed is not None
    assert dialogs.dialog_id(parsed, other) == base


def test_id_ignores_the_tab_count_and_the_collapsed_tool_description():
    """ "2 of 3" becomes "2 of 2" when another tab is answered, and the
    tool's description is cut to whatever fits — the same prompt either way."""
    screen = _screen("claude2_tool_use_tab")
    base = dialogs.dialog_id(ClaudeProvider().parse_dialog(screen), screen)
    recount = screen.replace("2 of 3", "2 of 2")
    longer = screen.replace(
        "uncommitted…", "uncommitted changes in your worktree are not…"
    )
    for other in (recount, longer):
        assert dialogs.dialog_id(ClaudeProvider().parse_dialog(other), other) == base


def test_real_ids_tell_distinct_prompts_apart():
    ids = {}
    for name in REAL:
        screen = _screen(name)
        ids.setdefault(
            dialogs.dialog_id(ClaudeProvider().parse_dialog(screen), screen), []
        ).append(name)
    shared = sorted(sorted(v) for v in ids.values() if len(v) > 1)
    assert shared == [
        ["claude2_create_file_narrow", "claude2_create_file_wide"],
        ["claude2_spawn_w100", "claude2_spawn_w120", "claude2_spawn_w164"],
        ["claude2_spawn_w62", "claude2_spawn_w79"],
        ["claude2_stop_session_w100", "claude2_stop_session_w164"],
    ]
    # The same tool, another argument: another prompt.
    screen = _screen("claude2_tool_use_tab")
    other = screen.replace('"hello-worker"', '"hello-worker-2"')
    assert dialogs.dialog_id(
        ClaudeProvider().parse_dialog(other), other
    ) != dialogs.dialog_id(ClaudeProvider().parse_dialog(screen), screen)


# --------------------------------------------------------------------------- #
# One dialog, every width (E2E defect C, second live run 2026-10-05)           #
# --------------------------------------------------------------------------- #
def _id(screen: str) -> str:
    return dialogs.dialog_id(ClaudeProvider().parse_dialog(screen), screen)


def _wrap_ansi(text: str, cols: int) -> list:
    """``text`` wrapped like wrap-ansi (``hard: true``) — how Ink, and so
    Claude Code, wraps a line: a word that doesn't fit moves to the next
    line; only a word longer than a whole line is cut, starting on this line
    or the next, whichever cuts it fewer times."""
    rows = [""]

    def cut(word: str) -> None:
        for ch in word:
            if len(rows[-1]) >= cols:
                rows.append("")
            rows[-1] += ch

    for i, word in enumerate(text.split(" ")):
        rows[-1] = rows[-1].lstrip()
        length = len(rows[-1])
        if i and length:
            rows[-1] += " "
            length += 1
        if len(word) > cols:
            this_line = 1 + (len(word) - (cols - length) - 1) // cols
            next_line = (len(word) - 1) // cols
            if next_line < this_line:
                rows.append("")
            cut(word)
            continue
        if length + len(word) > cols and length:
            rows.append("")
        rows[-1] += word
    return [r.rstrip() for r in rows]


def _render_claude2(
    width: int,
    heading: str,
    head_rest: str,
    box: list,
    about: str,
    options: list,
    cut_options: bool,
) -> str:
    """A Claude Code 2.x permission dialog drawn at ``width`` columns, the
    way the real captures show it: a full-width rule, the heading row
    (one line, cut to fit), the heading block, a dashed box, the tool's
    collapsed description, the question and the options — an option either
    cut to one line with "…" (MCP tools) or wrapped under itself (Bash)."""
    out = ["● Calling mindflock…", "", "─" * width]
    head = " " + heading
    out.append(head if len(head) <= width - 1 else head[: width - 2] + "…")
    out += [" " + r for r in _wrap_ansi(head_rest, width - 2)]
    out.append("╌" * width)
    out += box
    out.append("╌" * width)
    if about:
        header, desc = about.split("\n")
        out += [" " + r for r in _wrap_ansi(header, width - 2)]
        rows = _wrap_ansi(desc, width - 4)
        if len(rows) > 2:
            rows = rows[:1] + [rows[1][: width - 5] + "…"]
        out += [" │ " + r for r in rows]
        out.append(" (ctrl+o to expand description)")
        out.append("")
    out.append(" Do you want to proceed?")
    for i, label in enumerate(options, start=1):
        lead = (" ❯ " if i == 1 else "   ") + "%d. " % i
        if cut_options:
            if len(lead) + len(label) > width - 8:
                label = label[: width - 15] + "…"
            out.append(lead + label)
        else:
            rows = _wrap_ansi(label, width - 6)
            out.append(lead + rows[0])
            out += ["      " + r for r in rows[1:]]
    out += ["", " Esc to cancel · Tab to amend"]
    return "\n".join(out) + "\n"


def _args_box(width: int, args: list) -> list:
    """A tool call's arguments box: short values inline, a long string in a
    box of its own under ``name:``."""
    rows = []
    for name, value in args:
        if len(value) > 40:
            rows.append(" %s:" % name)
            rows += ["   │ " + r for r in _wrap_ansi(value, width - 9)]
        else:
            rows.append(' %s: "%s"' % (name, value))
    return rows


_STOP_ABOUT = (
    "About the mindflock — Stop a session Tool:\n"
    'Stop a session you manage. mode "close" (default): stops its agent and '
    "keeps the worktree and branch; the user can reopen it from Recently "
    'closed. mode "delete": stops it and REMOVES its worktree and branch.'
)
_SPAWN_ABOUT = (
    "About the mindflock — Spawn worker session Tool:\n"
    "Start a new worker agent session as your child. It gets its own git "
    "worktree and branch, forked from YOUR current HEAD commit: uncommitted "
    "changes in your worktree are not included, so commit first."
)
_FULL_STOP_ALWAYS = (
    _STOP_ALWAYS + "worktrees/emandel2630/add-hello-and-bye-files_18dbaa9638df1eb9"
)
_FULL_SPAWN_ALWAYS = (
    _SPAWN_ALWAYS
    + "~/.mindflock/worktrees/emandel2630/add-hello-and-bye-files_18dbaa9638df1eb9"
)
_FULL_ALLOW_ACCESS = (
    "Yes, and always allow access to /home/emandel2630/mindflock-prototypes/"
    "mcp-ux/e2e-live2/sandbox/home/.mindflock/worktrees/emandel2630/"
    "add-hello-and-bye-files-w1_18dbaaac6caccdd9 from this project"
)
_BYE_PROMPT = (
    'Create a bye.txt file containing "Goodbye, World!" You own this file '
    "only. Commit your changes with a clear message."
)


def _stop(width, options):
    return _render_claude2(
        width,
        "Tool use",
        "mindflock — Stop a session Tool: (MCP)",
        _args_box(width, [("title", "add-hello-and-bye-files-w2"), ("mode", "delete")]),
        _STOP_ABOUT,
        options,
        cut_options=True,
    )


def _spawn(width, options):
    return _render_claude2(
        width,
        "Tool use",
        "mindflock — Spawn worker session Tool: (MCP)",
        _args_box(
            width, [("prompt", _BYE_PROMPT), ("title", "add-hello-and-bye-files-w2")]
        ),
        _SPAWN_ABOUT,
        options,
        cut_options=True,
    )


def _echo(width, options):
    return _render_claude2(
        width,
        "Bash command · from the general-purpose agent",
        'Append "Hello again!" to hello.txt',
        [" │ " + r for r in _wrap_ansi(_ECHO_AGAIN, width - 4)],
        "",
        options,
        cut_options=False,
    )


#: (renderer, options, the real captures of that very dialog).
_SAME_DIALOG = {
    "stop_session": (
        _stop,
        ["Yes", _FULL_STOP_ALWAYS, "No"],
        ["claude2_stop_session_w100", "claude2_stop_session_w164"],
    ),
    "stop_session_2opt": (_stop, ["Yes", "No"], ["claude2_stop_session_w80"]),
    "spawn": (
        _spawn,
        ["Yes", _FULL_SPAWN_ALWAYS, "No"],
        ["claude2_spawn_w100", "claude2_spawn_w120", "claude2_spawn_w164"],
    ),
    "spawn_2opt": (_spawn, ["Yes", "No"], ["claude2_spawn_w62", "claude2_spawn_w79"]),
    "bash_echo": (
        _echo,
        ["Yes", _FULL_ALLOW_ACCESS, "No"],
        ["claude2_bash_hard_wrap_w79"],
    ),
}


def _tail(screen: str, start: str) -> list:
    lines = screen.rstrip("\n").split("\n")
    i = max(k for k, ln in enumerate(lines) if ln.startswith(start))
    return [ln.rstrip() for ln in lines[i:]]


@pytest.mark.parametrize("name", sorted(_SAME_DIALOG))
def test_one_dialog_has_one_id_at_every_captured_width(name):
    """The live run's kill dialog hashed 2b2f4565f063 / 624c4ddd433a /
    740f2d6ad10f at 80 / 100 / 164 columns: option 2 ("don't ask again …
    in ~/.mindflock/work…") is cut to the width, and a cut line threw the
    whole question+options paragraph out of the id — so how much of the
    dialog the id saw depended on the width."""
    _, _, captures = _SAME_DIALOG[name]
    assert len({_id(_screen(c)) for c in captures}) == 1, captures


@pytest.mark.parametrize("name", sorted(_SAME_DIALOG))
@pytest.mark.parametrize("width", [47, 60, 79, 120, 200])
def test_one_dialog_has_one_id_rewrapped_to_any_width(name, width):
    render, options, captures = _SAME_DIALOG[name]
    screen = render(width, options)
    parsed = ClaudeProvider().parse_dialog(screen)
    assert parsed is not None, screen
    assert [o["key"] for o in parsed["options"]] == [
        str(i) for i in range(1, len(options) + 1)
    ]
    assert _id(screen) == _id(_screen(captures[0]))


@pytest.mark.parametrize(
    "name,capture,width",
    [
        ("stop_session", "claude2_stop_session_w100", 100),
        ("stop_session", "claude2_stop_session_w164", 164),
        ("stop_session_2opt", "claude2_stop_session_w80", 80),
        ("spawn", "claude2_spawn_w100", 100),
        ("spawn", "claude2_spawn_w120", 120),
        ("spawn", "claude2_spawn_w164", 164),
        ("spawn_2opt", "claude2_spawn_w62", 62),
        ("spawn_2opt", "claude2_spawn_w79", 79),
        ("bash_echo", "claude2_bash_hard_wrap_w79", 79),
    ],
)
def test_the_synthetic_dialogs_draw_like_claude(name, capture, width):
    """The re-wraps above are only worth something if they wrap and cut the
    way Claude does: the question and options (cut or wrapped), and the
    Bash command box, line for line as captured."""
    render, options, _ = _SAME_DIALOG[name]
    screen, real = render(width, options), _screen(capture)
    assert _tail(screen, " Do you want") == _tail(real, " Do you want")
    if name == "bash_echo":
        box = [ln for ln in real.split("\n") if ln.startswith(" │ ")]
        assert [ln for ln in screen.split("\n") if ln.startswith(" │ ")] == box


def test_the_narrow_two_option_variant_is_another_dialog():
    """At ≤ 80 columns Claude draws "Yes / No" with no "don't ask again":
    "2" is No there and "Yes, and don't ask again" at 100. A click chosen on
    one must not be typed into the other, so their ids differ."""
    assert _id(_screen("claude2_stop_session_w80")) != _id(
        _screen("claude2_stop_session_w100")
    )
    assert _id(_screen("claude2_spawn_w79")) != _id(_screen("claude2_spawn_w100"))


@pytest.mark.parametrize(
    "base,old,new",
    [
        # Another worker title, another session to stop.
        ("claude2_stop_session_w164", '"add-hello-and-bye-files-w2"', '"other-w3"'),
        ("claude2_stop_session_w100", 'mode: "delete"', 'mode: "close"'),
        # Another tool with the same arguments.
        (
            "claude2_stop_session_w164",
            "Stop a session Tool: (MCP)",
            "Pause Tool: (MCP)",
        ),
        # Another command.
        (
            "claude2_bash_hard_wrap_w79",
            'echo "Hello again!" >>',
            'echo "Bye again!" >>',
        ),
        (
            "claude2_bash_heredoc_w79",
            "Add hello.txt with greeting",
            "Add hello.txt with a",
        ),
        # Another prompt for the worker.
        ("claude2_spawn_w120", "Goodbye, World!", "Goodnight, World!"),
    ],
)
def test_ids_still_tell_different_prompts_apart(base, old, new):
    screen = _screen(base)
    assert old in screen
    other = screen.replace(old, new, 1)
    assert ClaudeProvider().parse_dialog(other) is not None
    assert _id(other) != _id(screen)


def test_a_cut_option_keeps_the_rest_of_the_dialog_in_the_id():
    """Never a whole block left out: the question and every option's key
    still count when option 2 is cut."""
    screen = _screen("claude2_stop_session_w100")
    region = ClaudeProvider().parse_dialog(screen)["region"].split("\n")
    assert "Doyouwanttoproceed?" in region
    assert [p.split(":")[0] for p in region if re.match(r"^\d:", p)] == ["1", "2", "3"]
    assert "2:Yes,anddon'taskagainform" in region
    other = screen.replace("Do you want to proceed?", "Do you want to stop it?")
    assert _id(other) != _id(screen)


# --------------------------------------------------------------------------- #
# A command the box wrapped (E2E defect E, second live run)                    #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "name", ["claude2_bash_hard_wrap_w79", "claude2_bash_hard_wrap_w60_redraw"]
)
def test_a_hard_wrapped_command_is_one_token_again(name):
    """The box cut the path mid-token at the last column, and the command
    came back as "…/e2e-li\\nve2/sandbox/…" — a path no one can copy."""
    command = ClaudeProvider().parse_dialog(_screen(name))["command"]
    assert "/add-hello-and-bye-files-w1_18dbaaac6caccdd9/hello.txt" in command
    assert command.split()[-1] == _ECHO_AGAIN.split()[-1]


@pytest.mark.parametrize("width", [47, 60, 79, 120, 200])
def test_a_rewrapped_command_reads_the_same_at_any_width(width):
    command = ClaudeProvider().parse_dialog(_echo(width, ["Yes", "No"]))["command"]
    # The path is always one token; only a word wrap that moved the whole
    # path down (wrap-ansi's choice at some widths) reads as a line break.
    assert command.replace("\n", " ") == _ECHO_AGAIN


def test_a_heredocs_own_line_breaks_stay():
    command = ClaudeProvider().parse_dialog(_screen("claude2_bash_heredoc_w79"))[
        "command"
    ]
    assert command.split("\n") == [
        "git add hello.txt && git commit -m \"$(cat <<'EOF'",
        "Add hello.txt with greeting message",
        "",
        "Create initial hello.txt file containing 'Hello, World!' greeting.",
        "EOF",
        ')"',
    ]


def test_a_hard_wrapped_option_label_is_one_token_again():
    """ "…/home/.min dflock/worktr…": the option's path wrapped mid-token and
    came back with a space in it."""
    label = ClaudeProvider().parse_dialog(_screen("claude2_bash_hard_wrap_w79"))[
        "options"
    ][1]["label"]
    assert "/.mindflock/" in label and ".min dflock" not in label


@pytest.mark.parametrize(
    "prev,text,cols,joint",
    [
        (("abc/def", 79), "ghi", 80, ""),  # cut mid-token at the edge
        (("x" * 70 + " word", 79), "next", 80, " "),  # a word wrap filled it
        (("short line", 13), "next", 80, None),  # its own line break
        (("abc/def", 79), "  indented", 80, None),  # continuations never indent
        (("abc/def", 79), "ghi", 0, None),  # width unknown
        (("", 79), "ghi", 80, None),
    ],
)
def test_wrap_joint(prev, text, cols, joint):
    assert dialogs._wrap_joint(prev, text, cols) == joint


# --------------------------------------------------------------------------- #
# Screen evidence (the typing guards)                                          #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("name", sorted(REAL) + ["claude_bash", "claude_plan"])
def test_every_live_dialog_is_on_screen(name):
    assert ClaudeProvider().dialog_on_screen(_screen(name)) is True


def test_an_unparseable_dialog_is_still_on_screen():
    """A redraw glitch ("3. Nock/worktrees…") defeats the parser; the
    waiting-prompt patterns at the bottom still see the dialog — the guard
    holds the typing even when the strip can only offer ↗."""
    screen = _screen("claude2_redraw_glitch")
    assert ClaudeProvider().parse_dialog(screen) is None
    assert ClaudeProvider().dialog_on_screen(screen) is True


@pytest.mark.parametrize(
    "name", ["claude2_working", "claude_printed_list", "claude_user_numbered"]
)
def test_no_dialog_on_screen(name):
    assert ClaudeProvider().dialog_on_screen(_screen(name)) is False


def test_dialog_phrases_far_above_the_bottom_are_history():
    """An answered prompt's wording scrolled up the transcript is not a
    dialog: only the bottom lines count."""
    old = _screen("claude2_working")
    screen = (
        "  2. Yes, and don't ask again for ls commands\n"
        + "\n".join("● line %d" % i for i in range(30))
        + "\n"
        + old
    )
    assert ClaudeProvider().dialog_on_screen(screen) is False


def test_codex_dialog_on_screen():
    assert _codex().dialog_on_screen(_screen("codex_exec")) is True
    assert _codex().dialog_on_screen(_screen("codex_working")) is False


def test_parses_dialogs_is_the_parser_capability():
    assert ClaudeProvider().parses_dialogs() is True
    assert _codex().parses_dialogs() is True
    assert BaseProvider().parses_dialogs() is False
    assert providers.resolve("aider").parses_dialogs() is False
