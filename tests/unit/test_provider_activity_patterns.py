"""Activity classification + cost-panel visibility for the bundled providers.

The working/waiting pane snippets below are verbatim captures from the real
CLIs (codex 0.142.5, agy 1.0.16, opencode 1.17.15 driven live in tmux; cline
3.0.38, goose 1.41.0, aider 0.86.2 from their shipped binaries/source), so a
CLI UI change that breaks detection shows up here as a string diff.
"""

import re

from backend import providers
from backend.providers.base import LaunchContext
from backend.providers.config import BUILTIN_CONFIGS

# provider -> (a working-state pane, a paused-for-the-human pane)
PANES = {
    "codex": (
        "• Working (2s • esc to interrupt)",
        "Would you like to run the following command?\n"
        "  ● Yes, just this once\n"
        "    No, and tell Codex what to do differently",
    ),
    "antigravity": (
        "⣯  Generating...\n>\nesc to cancel      Gemini 3.5 Flash (Medium)",
        "  Requesting permission for:\n     touch t.txt\n\n"
        "Do you want to proceed?\n> 1. Yes\n  2. Yes, and always allow in this conversation",
    ),
    "aider": (
        "Waiting for LLM",
        "Add file to the chat? (Y)es/(N)o/(D)on't ask again [Yes]:",
    ),
    "opencode": (
        "⬝⬝⬝⬝  esc interrupt      tab agents  ctrl+p commands",
        "Permission required\n  Allow once\n  Allow always",
    ),
    "cline": (
        "Esc cancels turn",
        "Cline needs permission",
    ),
    "goose": (
        "⠹ Thinking... (Ctrl+C to interrupt)",
        "Goose would like to call the above tool, do you allow?\nAllow the tool call once",
    ),
}

IDLE_PANES = (
    "› Write tests for @filename\n  gpt-5.5 medium · ~",
    ">\n? for shortcuts       Gemini 3.5 Flash (Medium)",
    " >   Type your message or @path/to/file",
)


def test_every_bundled_provider_detects_working_and_clarify():
    for name, (working, waiting) in PANES.items():
        p = providers.resolve(name)
        assert p.name == name
        wp, qp = p.working_pane_patterns(), p.waiting_prompt_patterns()
        assert wp, f"{name} has no working patterns"
        assert qp, f"{name} has no waiting patterns"
        assert any(re.search(pat, working) for pat in wp), f"{name} working miss"
        assert any(re.search(pat, waiting) for pat in qp), f"{name} waiting miss"


# agy 1.0.16's clarifying-question menu, captured live: the footer keeps
# showing "esc to cancel" (a working pattern) while the menu waits for a
# selection, so the waiting patterns must match this pane for the poller's
# clarify-first check to rescue it from "working".
ANTIGRAVITY_QUESTION_PANE = (
    "? What kind of task are we working on today?\n"
    "Question 1/1: What kind of task are we working on today?\n"
    "> 1. I want to start a new web application from scratch.\n"
    "  2. I want to debug or modify an existing codebase.\n"
    "  4. Write-in...\n"
    "  ↑/↓ Navigate · enter Select · esc Skip\n"
    "esc to cancel                          Gemini 3.5 Flash (Medium)"
)


def test_antigravity_clarifying_question_is_waiting():
    p = providers.resolve("antigravity")
    assert any(
        re.search(pat, ANTIGRAVITY_QUESTION_PANE) for pat in p.waiting_prompt_patterns()
    ), "antigravity clarifying-question menu not detected as waiting"


def _bundled(name):
    """The BUNDLED provider under ``name`` — built from BUILTIN_CONFIGS
    directly, never through providers.resolve(): the registry is populated at
    import time from the developer's own ~/.mindflock-assistant/providers/
    TOMLs, and a user TOML overriding a bundled name (a codex.toml tweaking
    launch flags, say) would flip these roster assertions per-machine while CI
    stays green."""
    from backend.providers import _CONFIG_PROVIDER_CLASSES, GenericProvider
    from backend.providers.claude import ClaudeProvider

    if name == "claude":
        return ClaudeProvider()
    cfg = next(c for c in BUILTIN_CONFIGS if c.name == name)
    cls = _CONFIG_PROVIDER_CLASSES.get(cfg.name, GenericProvider)
    return cls(cfg)


def test_every_bundled_provider_can_arm_a_turn_end():
    """Every CLI here can still say "the agent has finished" — and by which
    route.

    Since a ``working`` reading alone stopped being enough to announce a turn
    (agent_state._verdict's ``arms``), a provider needs at least one signal that
    CORROBORATES work: its own hook report, or a status-line pattern that
    matches a real working pane. A provider with neither is not broken — it
    falls back to a sustained-CPU backstop — but it is the weakest rung, and
    nothing bundled should land there by accident.
    """
    for name, (working, _waiting) in PANES.items():
        p = _bundled(name)
        on_pane = any(re.search(pat, working) for pat in p.working_pane_patterns())
        assert p.reports_activity() or on_pane, (
            f"{name} can no longer corroborate work: no hook report and its "
            f"status-line patterns miss a real working pane"
        )
    # claude is not in PANES (it is exercised all over the activity tests); pin
    # it here anyway, since it is the CLI the arming rule was written against.
    claude = _bundled("claude")
    assert claude.reports_activity()
    assert any(
        re.search(pat, "\u283b Thinking… (esc to interrupt)")
        for pat in claude.working_pane_patterns()
    )


def test_only_hook_reporting_clis_skip_the_cpu_backstop():
    # The two halves of the ladder, as a roster. A CLI that reports for itself
    # has two independent signals and needs no CPU backstop — and must not have
    # one, because a CPU spike on a PARKED session of exactly this kind is what
    # announced a turn that never happened. Everyone else keeps the backstop.
    #
    # BUNDLED providers only, built hermetically (see _bundled): both
    # all_providers() AND resolve() read the live registry, which user TOMLs
    # feed — a test reading the developer's own config passes or fails by
    # accident.
    bundled = {"claude"} | {c.name for c in BUILTIN_CONFIGS}
    reporting = {n for n in bundled if _bundled(n).reports_activity()}
    assert reporting == {"claude", "codex"}


def test_reports_activity_is_a_capability_with_a_conservative_default():
    # reports_activity() answers CAPABILITY ("can this CLI speak for itself?"),
    # not the moment-to-moment activity_state(...) — the web layer keys the
    # turn-end arming rules off it, so the default must be the weakest claim.
    from dataclasses import replace

    from backend.providers import GenericProvider
    from backend.providers.base import BaseProvider
    from backend.providers.claude import ClaudeProvider

    assert BaseProvider().reports_activity() is False, "default: no self-report"
    assert ClaudeProvider().reports_activity() is True
    # For a generic CLI the declared hooks file IS the capability: with it the
    # CLI writes markers, without it pane inspection is all there is.
    codex_cfg = next(c for c in BUILTIN_CONFIGS if c.name == "codex")
    assert codex_cfg.activity_hooks_file
    assert GenericProvider(codex_cfg).reports_activity() is True
    hookless = replace(codex_cfg, activity_hooks_file="")
    assert GenericProvider(hookless).reports_activity() is False


def test_no_false_positives_on_idle_prompts():
    for name in PANES:
        p = providers.resolve(name)
        pats = p.working_pane_patterns() + p.waiting_prompt_patterns()
        for idle in IDLE_PANES:
            assert not any(
                re.search(pat, idle) for pat in pats
            ), f"{name} misreads an idle prompt as active"


def test_new_provider_launch_commands():
    # antigravity: agy with Claude-style skip flag and --continue resume.
    ag = providers.resolve("agy")
    assert ag.name == "antigravity"
    assert ag.build_launch_command(LaunchContext(program="agy")) == "agy"
    assert (
        ag.build_launch_command(LaunchContext(program="agy", resume=True))
        == "agy --continue || agy"
    )
    assert (
        ag.build_launch_command(LaunchContext(program="agy", skip_permissions=True))
        == "agy --dangerously-skip-permissions"
    )
    # opencode: --continue resume, --auto permission bypass.
    oc = providers.resolve("opencode")
    assert (
        oc.build_launch_command(LaunchContext(program="opencode", resume=True))
        == "opencode --continue || opencode"
    )
    assert (
        oc.build_launch_command(
            LaunchContext(program="opencode", skip_permissions=True)
        )
        == "opencode --auto"
    )
    # cline: interactive TUI, no resume-last flag -> relaunch fresh.
    cl = providers.resolve("cline")
    assert cl.build_launch_command(LaunchContext(program="cline")) == "cline -i"
    assert (
        cl.build_launch_command(LaunchContext(program="cline", resume=True))
        == "cline -i"
    )
    # goose: session subcommand with -r resume + fresh fallback.
    gs = providers.resolve("goose")
    assert gs.build_launch_command(LaunchContext(program="goose")) == "goose session"
    assert (
        gs.build_launch_command(LaunchContext(program="goose", resume=True))
        == "goose session -r || goose session"
    )


def test_cost_panel_default_visibility():
    # Only the bundled default CLIs appear in the sidebar cost panel.
    visible = [p.name for p in providers.all_providers() if p.usage_panel_visible()]
    assert visible == ["claude", "codex", "antigravity", "aider"]
    for name in ("opencode", "cline", "goose"):
        assert not providers.resolve(name).usage_panel_visible()
