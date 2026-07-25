"""Stage B: the CodingProvider for Claude reproduces today's behaviour exactly.

These assert the provider abstraction builds the expected launch commands and
workspace launchers, and that the registry resolves programs correctly.
"""

from __future__ import annotations

import json

import pytest

from backend import providers
from backend.providers import LaunchContext
from backend.providers.claude import claude_launch_command
from backend.session import provisioned


def test_registry_resolves_claude_by_default():
    assert providers.resolve("").name == "claude"
    assert providers.resolve("claude").name == "claude"
    assert providers.resolve("/usr/local/bin/claude").name == "claude"
    # An unknown program is claimed by the catch-all generic fallback (which
    # runs it bare with the default trust/idle handling).
    assert providers.resolve("totally-unknown-cli").name == "generic"


def test_build_launch_command_fresh():
    ctx = LaunchContext(program="claude", resume=False, session_name="mindflock_x")
    got = providers.resolve("claude").build_launch_command(ctx)
    assert got == claude_launch_command("claude", resume=False)
    assert got == "claude"


def test_build_launch_command_resume():
    ctx = LaunchContext(program="claude", resume=True, session_name="mindflock_x")
    got = providers.resolve("claude").build_launch_command(ctx)
    assert got == claude_launch_command("claude", resume=True)
    assert got == (
        "claude --continue || { sleep 3; claude --continue; } || "
        "{ echo '[mindflock] resume failed twice; starting a fresh session"
        " WITHOUT re-sending the task prompt'; claude; }"
    )


def test_custom_program_falls_through_to_generic():
    # A custom program's provider runs it bare / with --continue.
    p = providers.resolve("mycli --flag")
    assert (
        p.build_launch_command(LaunchContext(program="mycli --flag", resume=False))
        == "mycli --flag"
    )
    assert (
        p.build_launch_command(LaunchContext(program="mycli --flag", resume=True))
        == "mycli --flag --continue || mycli --flag"
    )


def test_write_launcher_delegates_byte_identical(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    ctx = LaunchContext(
        program="claude",
        workdir=str(tmp_path / "a"),
        prompt="hi",
        skip_permissions=True,
    )
    via_provider = providers.resolve("claude").write_launcher(ctx)
    via_module = provisioned.write_launcher(
        str(tmp_path / "b"), "hi", program="claude", skip_permissions=True
    )
    a = open(via_provider).read().replace(str((tmp_path / "a").resolve()), "/D")
    b = open(via_module).read().replace(str((tmp_path / "b").resolve()), "/D")
    assert a == b


def test_is_natural_exit_policy():
    p = providers.resolve("claude")
    assert p.is_natural_exit(0) and p.is_natural_exit(130)
    assert not p.is_natural_exit(137) and not p.is_natural_exit(None)


# --------------------------------------------------------------------------- #
# Stage F: the new pluggable providers + classification parity.
# --------------------------------------------------------------------------- #
def test_named_providers_resolve():
    assert providers.resolve("aider").name == "aider"
    assert providers.resolve("codex").name == "codex"
    # An unknown program falls through to the catch-all fallback.
    assert providers.resolve("some-random-cli").name == "generic"
    # claude/empty still resolve to claude (registered first).
    assert providers.resolve("claude").name == "claude"
    assert providers.resolve("").name == "claude"


def test_generic_launch_command():
    aider = providers.resolve("aider")
    # Plain launch (no skip-permissions on the plain path) runs the program bare.
    assert aider.build_launch_command(LaunchContext(program="aider")) == "aider"
    # aider resumes by replaying its per-repo chat history (opt-in flag, verified
    # against aider 0.86.2); no fresh fallback (harmless when there's no history).
    assert (
        aider.build_launch_command(LaunchContext(program="aider", resume=True))
        == "aider --restore-chat-history"
    )
    codex = providers.resolve("codex")
    # codex resumes via its resume subcommand with a fresh fallback.
    assert (
        codex.build_launch_command(LaunchContext(program="codex", resume=True))
        == "codex resume --last || codex"
    )
    # skip_permissions applies the configured flag.
    assert (
        codex.build_launch_command(
            LaunchContext(program="codex", skip_permissions=True)
        )
        == "codex --dangerously-bypass-approvals-and-sandbox"
    )


def test_antigravity_seeds_prompt_interactively(tmp_path, monkeypatch):
    # agy --prompt-interactive seeds an interactive session with the start prompt
    # (verified against agy 1.1.3). Parity with codex's positional seeding.
    monkeypatch.setenv("MINDFLOCK_SEED_PROMPT_DIR", str(tmp_path))
    agy = providers.resolve("agy")
    ctx = LaunchContext(
        program="agy", prompt="fix the bug", session_name="mindflock_seed"
    )
    cmd = agy.build_launch_command(ctx)
    assert cmd.startswith('agy --prompt-interactive "$(cat ')
    assert cmd.endswith('.md)"')
    assert (tmp_path / "mindflock_seed.md").read_text() == "fix the bug"


def test_antigravity_no_prompt_launches_bare(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDFLOCK_SEED_PROMPT_DIR", str(tmp_path))
    agy = providers.resolve("agy")
    assert agy.build_launch_command(LaunchContext(program="agy")) == "agy"


def test_antigravity_resume_by_recorded_id(tmp_path, monkeypatch):
    # A recorded per-window thread id targets `--conversation <id>` with a fresh
    # fallback — so siblings on one workdir don't all resume the newest thread.
    monkeypatch.setenv("MINDFLOCK_THREAD_MARKER_DIR", str(tmp_path / "th"))
    monkeypatch.setenv("MINDFLOCK_SEED_PROMPT_DIR", str(tmp_path / "seed"))
    from backend.providers import thread_markers

    thread_markers.record("mindflock_r", "conv-9")
    agy = providers.resolve("agy")
    cmd = agy.build_launch_command(
        LaunchContext(program="agy", resume=True, session_name="mindflock_r")
    )
    assert cmd == "agy --conversation conv-9 || agy"


def test_classification_per_provider():
    # Claude: the per-folder trust + MCP prompts, dismissed with Enter.
    claude = providers.resolve("claude")
    spec = claude.trust_prompt()
    assert "Do you trust the files in this folder?" in spec.patterns
    assert "new MCP server" in spec.patterns
    assert spec.keystroke == b"\r"
    assert claude.idle_prompt_pattern() == "No, and tell Claude what to do differently"

    # aider's idle prompt matches the upstream CLI's current string.
    assert (
        providers.resolve("aider").idle_prompt_pattern()
        == "(Y)es/(N)o/(D)on't ask again"
    )

    # Unknown program -> the generic fallback: dismiss an "Open documentation
    # url" gate with 'D' then Enter, and no idle prompt.
    fb = providers.resolve("some-random-cli")
    fspec = fb.trust_prompt()
    assert fspec.patterns == ("Open documentation url for more info",)
    assert fspec.keystroke == b"\x44\x0d"
    assert fb.idle_prompt_pattern() is None


def test_owns_launcher_only_for_claude():
    assert (
        providers.resolve("claude").owns_launcher(LaunchContext(program="claude"))
        is True
    )
    assert (
        providers.resolve("claude").owns_launcher(
            LaunchContext(program="claude", in_place=True)
        )
        is False
    )
    assert (
        providers.resolve("aider").owns_launcher(LaunchContext(program="aider"))
        is False
    )
    assert (
        providers.resolve("some-random-cli").owns_launcher(LaunchContext(program="x"))
        is False
    )
