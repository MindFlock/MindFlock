"""Per-provider behaviour matrix — how each bundled CLI launches, resumes, is
trusted, and reports usage differs, and several coexist in one registry.

``test_provider_framework.py`` pins the claude/aider/codex/antigravity paths
one at a time. This file asserts the *distinctions between* the whole bundled
set (codex, antigravity, aider, opencode, cline, goose) as a single table, so a
config edit that silently changes one CLI's launch/resume/trust behaviour is
caught, plus the registry-level guarantees that let a fleet mix providers:

* fresh vs resume vs skip-permissions launch commands, per provider;
* natural-exit codes, trust prompts, and usage mode/window/panel visibility;
* a user TOML resolving, overriding a builtin, and staying ahead of the
  catch-all fallback in resolution order;
* several distinct programs resolving to their own providers in one registry;
* the resolve cache surviving lookups and being dropped on rebuild.
"""

from __future__ import annotations

import os

import pytest

from backend import providers
from backend.providers import LaunchContext


@pytest.fixture(autouse=True)
def _clean_registry(tmp_path, monkeypatch):
    """Point the user-providers dir at an empty tmp dir and rebuild, so the
    registry holds exactly the bundled set unless a test writes a TOML. Restore
    a clean registry afterwards for the rest of the suite."""
    monkeypatch.setenv("MINDFLOCK_PROVIDERS_DIR", str(tmp_path / "providers"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)  # claude -> windowed
    for var in list(os.environ):
        if var.startswith("MINDFLOCK_PROVIDER_BIN_"):
            monkeypatch.delenv(var, raising=False)
    providers.rebuild_registry()
    yield
    # Rebuild from a CLEAN providers dir: monkeypatch only undoes the env var
    # after this fixture tears down, so leaving it set here would rebuild the
    # registry with this test's user TOMLs still loaded and leak them into the
    # next file. Drop it first so the restored registry is the bundled set.
    os.environ.pop("MINDFLOCK_PROVIDERS_DIR", None)
    providers.rebuild_registry()


def _write_provider(tmp_path, name, body):
    d = tmp_path / "providers"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.toml").write_text(body)


# --------------------------------------------------------------------------- #
# launch-command matrix — fresh, resume, and skip-permissions per provider
# --------------------------------------------------------------------------- #
# (program, fresh, resume, skip_permissions_fresh)
_LAUNCH = [
    (
        "codex",
        "codex",
        "codex resume --last || codex",
        "codex --dangerously-bypass-approvals-and-sandbox",
    ),
    ("agy", "agy", "agy --continue || agy", "agy --dangerously-skip-permissions"),
    # aider restores chat history opt-in, with NO fresh fallback.
    ("aider", "aider", "aider --restore-chat-history", "aider --yes-always"),
    ("opencode", "opencode", "opencode --continue || opencode", "opencode --auto"),
    # cline resume needs a session id, so it relaunches fresh; no skip flag.
    ("cline", "cline -i", "cline -i", "cline -i"),
    # goose's chat lives under the `session` subcommand; resume is `-r`.
    ("goose", "goose session", "goose session -r || goose session", "goose session"),
]


@pytest.mark.parametrize("program,fresh,resume,skip", _LAUNCH)
def test_launch_command_matrix(program, fresh, resume, skip):
    p = providers.resolve(program)
    assert p.build_launch_command(LaunchContext(program=program)) == fresh
    assert p.build_launch_command(LaunchContext(program=program, resume=True)) == resume
    assert (
        p.build_launch_command(LaunchContext(program=program, skip_permissions=True))
        == skip
    )


def test_claude_launch_is_distinct_from_generic():
    # Claude owns a launcher script and a bespoke multi-attempt resume chain —
    # it must never behave like the generic `prog --continue || prog`.
    claude = providers.resolve("claude")
    assert claude.build_launch_command(LaunchContext(program="claude")) == "claude"
    resumed = claude.build_launch_command(LaunchContext(program="claude", resume=True))
    assert resumed.count("claude --continue") == 2  # two retries before a fresh start
    assert "starting a fresh session" in resumed
    assert claude.owns_launcher(LaunchContext(program="claude")) is True


# --------------------------------------------------------------------------- #
# resume-by-thread-id — codex/antigravity target one window's own conversation
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "program,expected",
    [
        ("codex", "codex resume conv-7 || codex"),
        ("agy", "agy --conversation conv-7 || agy"),
    ],
)
def test_resume_by_recorded_thread_id(program, expected, tmp_path, monkeypatch):
    monkeypatch.setenv("MINDFLOCK_THREAD_MARKER_DIR", str(tmp_path / "th"))
    monkeypatch.setenv("MINDFLOCK_SEED_PROMPT_DIR", str(tmp_path / "seed"))
    from backend.providers import thread_markers

    thread_markers.record("mindflock_win", "conv-7")
    p = providers.resolve(program)
    cmd = p.build_launch_command(
        LaunchContext(program=program, resume=True, session_name="mindflock_win")
    )
    assert cmd == expected


# --------------------------------------------------------------------------- #
# classification + exit policy differ per provider
# --------------------------------------------------------------------------- #
def test_trust_prompts_differ():
    # aider dismisses a documentation gate with 'D'+Enter...
    aider = providers.resolve("aider").trust_prompt()
    assert aider.keystroke == b"\x44\x0d"
    assert "Open documentation url for more info" in aider.patterns
    # ...antigravity accepts a per-project trust gate with a bare Enter...
    agy = providers.resolve("agy").trust_prompt()
    assert agy.keystroke == b"\r"
    assert "Do you trust the contents of this project?" in agy.patterns
    # ...and codex/opencode open straight in — no auto-dismiss prompt at all.
    assert providers.resolve("codex").trust_prompt() is None
    assert providers.resolve("opencode").trust_prompt() is None


def test_natural_exit_codes_shared_default():
    for program in ("codex", "aider", "opencode", "cline", "goose"):
        p = providers.resolve(program)
        assert p.is_natural_exit(0) and p.is_natural_exit(130)
        assert not p.is_natural_exit(137)


# --------------------------------------------------------------------------- #
# usage model differs — windowed (plan) vs metered (BYO key)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "program,mode,visible",
    [
        ("codex", "windowed", True),
        ("agy", "windowed", True),
        ("aider", "metered", True),  # BYO key -> metered, but panel-visible
        ("opencode", "metered", False),  # BYO key, not a default panel CLI
        ("goose", "metered", False),
    ],
)
def test_usage_mode_and_panel(program, mode, visible):
    p = providers.resolve(program)
    assert p.usage_mode() == mode
    assert p.usage_panel_visible() is visible


def test_windowed_providers_declare_a_window_kind():
    assert providers.resolve("codex").usage_window()["kind"] == "rolling"
    assert providers.resolve("agy").usage_window()["kind"] == "rolling"
    # Metered CLIs declare no MindFlock-managed window.
    assert providers.resolve("aider").usage_window()["kind"] == ""


# --------------------------------------------------------------------------- #
# registry: user TOMLs, overrides, order, and mixed resolution
# --------------------------------------------------------------------------- #
def test_user_toml_registers_and_launches(tmp_path):
    _write_provider(
        tmp_path,
        "mycli",
        '[provider]\nname = "mycli"\nprogram = "mycli"\n'
        '[launch]\nresume_flag = "--resume"\nskip_perms_flag = "--yolo"\n',
    )
    providers.rebuild_registry()
    p = providers.resolve("mycli")
    assert p.name == "mycli"
    assert (
        p.build_launch_command(LaunchContext(program="mycli", resume=True))
        == "mycli --resume || mycli"
    )
    assert (
        p.build_launch_command(LaunchContext(program="mycli", skip_permissions=True))
        == "mycli --yolo"
    )


def test_user_toml_can_override_a_builtin_name(tmp_path):
    # A user TOML re-registering "codex" replaces the bundled codex config.
    _write_provider(
        tmp_path,
        "codex",
        '[provider]\nname = "codex"\nprogram = "codex"\ncommand = "my-codex"\n'
        '[launch]\nresume_flag = "--pick-up"\n',
    )
    providers.rebuild_registry()
    p = providers.resolve("codex")
    assert p.build_launch_command(LaunchContext(program="codex")) == "my-codex"
    assert (
        p.build_launch_command(LaunchContext(program="codex", resume=True))
        == "my-codex --pick-up || my-codex"
    )


def test_fallback_is_always_last_and_unknowns_land_on_it(tmp_path):
    _write_provider(tmp_path, "zzz", '[provider]\nname = "zzz"\nprogram = "zzz"\n')
    providers.rebuild_registry()
    order = [p.name for p in providers.all_providers()]
    assert order[0] == "claude"  # claude first
    assert order[-1] == "generic"  # catch-all fallback last
    assert order.index("zzz") < order.index("generic")  # user TOML before fallback
    # An unclaimed program resolves to the fallback, run bare.
    fb = providers.resolve("totally-unknown-cli")
    assert fb.name == "generic"
    assert (
        fb.build_launch_command(LaunchContext(program="totally-unknown-cli"))
        == "totally-unknown-cli"
    )


def test_distinct_programs_resolve_to_their_own_providers():
    # A single registry drives a mixed fleet: each program hits its own provider.
    assert providers.resolve("claude").name == "claude"
    assert providers.resolve("codex").name == "codex"
    assert providers.resolve("aider").name == "aider"
    assert providers.resolve("agy").name == "antigravity"
    assert providers.resolve("opencode").name == "opencode"
    assert providers.resolve("").name == "claude"  # empty -> default


def test_resolve_cache_is_dropped_on_rebuild(tmp_path):
    # A name that is unknown now resolves to the fallback and is cached...
    assert providers.resolve("mycli").name == "generic"
    # ...then a rebuild that introduces it must NOT return the stale cached hit.
    _write_provider(
        tmp_path, "mycli", '[provider]\nname = "mycli"\nprogram = "mycli"\n'
    )
    providers.rebuild_registry()
    assert providers.resolve("mycli").name == "mycli"
