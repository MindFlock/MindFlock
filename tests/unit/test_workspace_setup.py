"""Unit tests for the generic workspace setup / cache-seed primitive."""

import asyncio
import subprocess
import textwrap
from pathlib import Path

import pytest
import tomli

from backend import workspace_setup as ws
from backend.ticket_ingestion.config import ConfigError, load_config
from backend.workspace_setup import (
    CacheSeed,
    SetupCommandError,
    WorkspaceConfigError,
    auto_setup_commands,
    exclude_artifacts,
    is_refresher_dirname,
    merged_cache_env,
    parse_caches,
    parse_setup_commands,
    pin_cache_env,
    refresher_dirname,
    resolve_setup_commands,
    run_setup_commands,
    run_setup_commands_async,
    seed_caches,
)


def _raw(toml_text: str) -> dict:
    return tomli.loads(textwrap.dedent(toml_text))


def _git_repo(path: Path) -> Path:
    """A minimal initialized git repo (for hook/exclude-path resolution).

    Pins ``core.hooksPath`` back to this repo's own ``.git/hooks`` so the hook
    tests are hermetic: the session's isolated gitconfig points hooksPath at a
    shared directory, which these tests must neither read from nor write into.
    """
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@x"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "t"], check=True)
    subprocess.run(
        ["git", "-C", str(path), "config", "core.hooksPath", str(path / ".git/hooks")],
        check=True,
    )
    return path


# ---------------------------------------------------------------------------
# Config parsing
# ---------------------------------------------------------------------------
class TestParseSetupCommands:
    def test_unset_means_auto(self):
        assert parse_setup_commands({}) is None

    def test_explicit_list(self):
        raw = _raw("""
            [workspace]
            setup_commands = ["npm ci", "npm run build"]
        """)
        assert parse_setup_commands(raw) == ["npm ci", "npm run build"]

    def test_explicit_empty_list_disables(self):
        raw = _raw("""
            [workspace]
            setup_commands = []
        """)
        assert parse_setup_commands(raw) == []

    def test_non_list_rejected(self):
        with pytest.raises(WorkspaceConfigError):
            parse_setup_commands({"workspace": {"setup_commands": "npm ci"}})


class TestParseCaches:
    def test_explicit_cache_entry(self, tmp_path):
        raw = _raw("""
            [[workspace.cache]]
            name = "gobuild"
            seed_path = "./.cache/gobuild.tar"
            workspace_path = ".gocache/build.tar"
            refresh_command = "go build ./..."
            refresh_branch = "develop"
            refresh_interval_seconds = 600
            env = { GOCACHE_KEY = "shared" }
        """)
        caches = parse_caches(raw, tmp_path)
        assert len(caches) == 1
        c = caches[0]
        assert c.name == "gobuild"
        assert c.seed_path == tmp_path / ".cache/gobuild.tar"
        assert c.workspace_path == ".gocache/build.tar"
        assert c.refresh_command == "go build ./..."
        assert c.refresh_branch == "develop"
        assert c.refresh_interval_seconds == 600
        assert c.env == {"GOCACHE_KEY": "shared"}

    def test_unknown_testmon_section_is_ignored(self, tmp_path):
        """The retired [testmon] alias no longer produces a cache."""
        raw = _raw("""
            [testmon]
            seed_path = "./.cache/testmondata"
            refresh_branch = "main"
        """)
        assert parse_caches(raw, tmp_path) == []

    def test_workspace_path_may_not_escape(self, tmp_path):
        raw = _raw("""
            [[workspace.cache]]
            name = "evil"
            seed_path = "./seed"
            workspace_path = "../outside"
        """)
        with pytest.raises(WorkspaceConfigError):
            parse_caches(raw, tmp_path)

    def test_missing_required_field_rejected(self, tmp_path):
        raw = _raw("""
            [[workspace.cache]]
            name = "incomplete"
            seed_path = "./seed"
        """)
        with pytest.raises(WorkspaceConfigError):
            parse_caches(raw, tmp_path)

    def test_no_config_means_no_caches(self):
        assert parse_caches({}) == []

    def test_single_table_is_wrapped_as_one_entry(self, tmp_path):
        """A ``[workspace.cache]`` table (not an array) is accepted as one entry."""
        raw = _raw("""
            [workspace.cache]
            name = "solo"
            seed_path = "./seed"
            workspace_path = ".solo"
        """)
        caches = parse_caches(raw, tmp_path)
        assert [c.name for c in caches] == ["solo"]

    def test_scalar_cache_rejected(self):
        with pytest.raises(WorkspaceConfigError, match="array of tables"):
            parse_caches({"workspace": {"cache": "nope"}})

    def test_non_table_entry_rejected(self):
        with pytest.raises(WorkspaceConfigError, match=r"cache\[0\] must be a table"):
            parse_caches({"workspace": {"cache": ["not-a-table"]}})

    def test_missing_name_rejected(self):
        raw = {"workspace": {"cache": [{"seed_path": "s", "workspace_path": "w"}]}}
        with pytest.raises(WorkspaceConfigError, match="name is required"):
            parse_caches(raw)

    def test_missing_seed_path_rejected(self):
        raw = {"workspace": {"cache": [{"name": "n", "workspace_path": "w"}]}}
        with pytest.raises(WorkspaceConfigError, match="seed_path is required"):
            parse_caches(raw)

    def test_non_string_env_value_rejected(self):
        raw = {
            "workspace": {
                "cache": [
                    {
                        "name": "n",
                        "seed_path": "s",
                        "workspace_path": "w",
                        "env": {"K": 5},
                    }
                ]
            }
        }
        with pytest.raises(WorkspaceConfigError, match="env must be a table"):
            parse_caches(raw)

    def test_non_string_refresh_command_rejected(self):
        raw = {
            "workspace": {
                "cache": [
                    {
                        "name": "n",
                        "seed_path": "s",
                        "workspace_path": "w",
                        "refresh_command": 5,
                    }
                ]
            }
        }
        with pytest.raises(WorkspaceConfigError, match="refresh_command must be"):
            parse_caches(raw)


class TestLoadConfigIntegration:
    def _write_config(self, tmp_path, extra: str) -> Path:
        path = tmp_path / "config.toml"
        path.write_text(textwrap.dedent("""
            [[ticketing.source]]
            provider = "shortcut"
            api_token = "t"
            member_id = "m"

            [repository]
            url = "git@example.com:org/repo.git"
            workspace_dir = "./workspaces"

            [validation]
            min_description_length = 20

            [logging]
            log_file = "./logs/p.log"
            log_level = "INFO"
        """) + textwrap.dedent(extra))
        return path

    def test_workspace_section_parsed(self, tmp_path):
        path = self._write_config(
            tmp_path,
            """
            [workspace]
            setup_commands = ["make deps"]

            [[workspace.cache]]
            name = "testmon"
            seed_path = "./.cache/testmondata"
            workspace_path = ".testmondata"
        """,
        )
        config = load_config(path)
        assert config.setup_commands == ["make deps"]
        assert [c.name for c in config.caches] == ["testmon"]
        assert config.caches[0].seed_path == tmp_path / ".cache/testmondata"

    def test_unknown_testmon_section_is_ignored(self, tmp_path):
        path = self._write_config(
            tmp_path,
            """
            [testmon]
            seed_path = "./.cache/testmondata"
            refresh_branch = "staging"
        """,
        )
        config = load_config(path)
        assert config.setup_commands is None
        assert config.caches == []

    def test_invalid_workspace_section_raises_config_error(self, tmp_path):
        path = self._write_config(
            tmp_path,
            """
            [[workspace.cache]]
            name = "broken"
            seed_path = "./seed"
        """,
        )
        with pytest.raises(ConfigError):
            load_config(path)


# ---------------------------------------------------------------------------
# Cache seeding
# ---------------------------------------------------------------------------
def _cache(tmp_path, **overrides) -> CacheSeed:
    kwargs = dict(
        name="testmon",
        seed_path=tmp_path / "seed" / "testmondata",
        workspace_path=".testmondata",
    )
    kwargs.update(overrides)
    return CacheSeed(**kwargs)


class TestSeedCaches:
    def test_seeds_fresh_workspace(self, tmp_path):
        cache = _cache(tmp_path)
        cache.seed_path.parent.mkdir(parents=True)
        cache.seed_path.write_bytes(b"warm-cache")
        workspace = tmp_path / "ws"
        workspace.mkdir()

        seed_caches([cache], workspace)

        assert (workspace / ".testmondata").read_bytes() == b"warm-cache"

    def test_never_clobbers_existing_cache(self, tmp_path):
        """An evolving per-workspace cache must survive re-provision."""
        cache = _cache(tmp_path)
        cache.seed_path.parent.mkdir(parents=True)
        cache.seed_path.write_bytes(b"stale-baseline")
        workspace = tmp_path / "ws"
        workspace.mkdir()
        (workspace / ".testmondata").write_bytes(b"evolved-local")

        seed_caches([cache], workspace)

        assert (workspace / ".testmondata").read_bytes() == b"evolved-local"

    def test_missing_seed_starts_cold(self, tmp_path):
        cache = _cache(tmp_path)  # seed file never created
        workspace = tmp_path / "ws"
        workspace.mkdir()

        seed_caches([cache], workspace)

        assert not (workspace / ".testmondata").exists()

    def test_nested_workspace_path_creates_parents(self, tmp_path):
        cache = _cache(tmp_path, name="build", workspace_path=".cache/build/warm.tar")
        cache.seed_path.parent.mkdir(parents=True)
        cache.seed_path.write_bytes(b"tarball")
        workspace = tmp_path / "ws"
        workspace.mkdir()

        seed_caches([cache], workspace)

        assert (workspace / ".cache/build/warm.tar").read_bytes() == b"tarball"

    def test_copy_failure_is_swallowed(self, tmp_path, monkeypatch):
        """A failed copy (disk full, permissions) must not raise — the
        workspace just starts cold for that cache."""
        cache = _cache(tmp_path)
        cache.seed_path.parent.mkdir(parents=True)
        cache.seed_path.write_bytes(b"warm")
        workspace = tmp_path / "ws"
        workspace.mkdir()

        def boom(src, dst):
            raise OSError("no space left on device")

        monkeypatch.setattr(ws.shutil, "copy2", boom)
        seed_caches([cache], workspace)  # must not raise
        assert not (workspace / ".testmondata").exists()


# ---------------------------------------------------------------------------
# Setup commands
# ---------------------------------------------------------------------------
class TestAutoSetupCommands:
    def test_empty_workspace_gets_no_commands(self, tmp_path):
        assert auto_setup_commands(tmp_path) == []

    def test_uv_project_gets_uv_sync(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
        assert auto_setup_commands(tmp_path) == ["uv sync --all-groups"]

    def test_uv_project_with_precommit_gets_hook_install(self, tmp_path):
        (tmp_path / "uv.lock").write_text("")
        (tmp_path / ".pre-commit-config.yaml").write_text("repos: []\n")
        assert auto_setup_commands(tmp_path) == [
            "uv sync --all-groups",
            "uv run --all-groups pre-commit install",
        ]

    def test_explicit_config_overrides_auto(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
        assert resolve_setup_commands(["npm ci"], tmp_path) == ["npm ci"]
        assert resolve_setup_commands([], tmp_path) == []
        assert resolve_setup_commands(None, tmp_path) == ["uv sync --all-groups"]


class TestRunSetupCommands:
    def test_commands_run_in_workspace(self, tmp_path):
        run_setup_commands(["touch made-by-setup"], tmp_path)
        assert (tmp_path / "made-by-setup").exists()

    def test_failure_continues_by_default(self, tmp_path):
        run_setup_commands(["false", "touch still-ran"], tmp_path)
        assert (tmp_path / "still-ran").exists()

    def test_strict_failure_raises_with_command_and_tail(self, tmp_path):
        with pytest.raises(SetupCommandError) as exc:
            run_setup_commands(["echo boom-marker && exit 3"], tmp_path, strict=True)
        msg = str(exc.value)
        assert "rc=3" in msg
        assert "boom-marker" in msg  # captured tail is surfaced


class TestRunSetupCommandsAsync:
    def test_commands_run_in_workspace(self, tmp_path):
        asyncio.run(run_setup_commands_async(["touch async-made"], tmp_path))
        assert (tmp_path / "async-made").exists()

    def test_failure_continues_by_default(self, tmp_path):
        asyncio.run(
            run_setup_commands_async(["false", "touch async-still-ran"], tmp_path)
        )
        assert (tmp_path / "async-still-ran").exists()

    def test_strict_failure_raises(self, tmp_path):
        with pytest.raises(SetupCommandError) as exc:
            asyncio.run(
                run_setup_commands_async(
                    ["echo async-boom && exit 4"], tmp_path, strict=True
                )
            )
        msg = str(exc.value)
        assert "rc=4" in msg
        assert "async-boom" in msg


# ---------------------------------------------------------------------------
# Misc helpers
# ---------------------------------------------------------------------------
class TestHelpers:
    def test_testmon_cache_keeps_historic_refresher_dirname(self):
        assert refresher_dirname("testmon") == "_testmon_refresher"

    def test_is_refresher_dirname(self):
        assert is_refresher_dirname("_testmon_refresher")
        assert is_refresher_dirname("_gobuild_refresher")
        assert not is_refresher_dirname("pr-12")
        assert not is_refresher_dirname("feature-sc-1-story")

    def test_merged_cache_env_later_wins(self, tmp_path):
        a = _cache(tmp_path, name="a", env={"K": "1", "ONLY_A": "x"})
        b = _cache(tmp_path, name="b", env={"K": "2"})
        assert merged_cache_env([a, b]) == {"K": "2", "ONLY_A": "x"}


# ---------------------------------------------------------------------------
# exclude_artifacts: best-effort, never hangs on / raises from a wedged git
# ---------------------------------------------------------------------------
class TestExcludeArtifacts:
    def test_git_call_carries_a_timeout(self, tmp_path, monkeypatch):
        import subprocess

        from backend import workspace_setup as ws

        seen = {}

        def fake_run(cmd, **kwargs):
            seen.update(kwargs)
            return subprocess.CompletedProcess(cmd, 1, stdout=b"", stderr=b"")

        monkeypatch.setattr(ws.subprocess, "run", fake_run)
        exclude_artifacts(tmp_path)
        assert seen.get("timeout") == 10

    def test_git_timeout_degrades_instead_of_raising(self, tmp_path, monkeypatch):
        import subprocess

        from backend import workspace_setup as ws

        def hang(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=10)

        monkeypatch.setattr(ws.subprocess, "run", hang)
        # Best-effort function: a hung git must be swallowed, not raised.
        exclude_artifacts(tmp_path)

    def test_empty_git_path_output_is_a_noop(self, tmp_path, monkeypatch):
        """rc=0 with no path (a degenerate git) must not create an exclude file."""

        def blank(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 0, stdout=b"  \n", stderr=b"")

        monkeypatch.setattr(ws.subprocess, "run", blank)
        exclude_artifacts(tmp_path)
        assert not (tmp_path / ".git" / "info" / "exclude").exists()

    def test_appends_to_existing_exclude_without_leading_blank_gap(self, tmp_path):
        """An existing exclude with no trailing newline gets one before the
        artifacts are appended (so the last existing line isn't merged)."""
        repo = _git_repo(tmp_path)
        exclude = repo / ".git" / "info" / "exclude"
        exclude.parent.mkdir(parents=True, exist_ok=True)
        exclude.write_text("*.pyc")  # no trailing newline

        exclude_artifacts(repo)

        text = exclude.read_text()
        assert text.startswith("*.pyc\n")
        assert ".mindflock_prompt.md\n" in text
        assert ".testmondata\n" in text

    def test_only_missing_artifacts_are_appended(self, tmp_path):
        """Re-running never duplicates entries already present."""
        repo = _git_repo(tmp_path)
        exclude_artifacts(repo)
        exclude = repo / ".git" / "info" / "exclude"
        first = exclude.read_text()
        exclude_artifacts(repo)  # second pass
        assert exclude.read_text() == first

    def test_write_oserror_is_swallowed(self, tmp_path, monkeypatch):
        """A write failure (e.g. exclude path is a directory) degrades quietly."""
        repo = _git_repo(tmp_path)
        exclude = repo / ".git" / "info" / "exclude"
        if exclude.exists():
            exclude.unlink()
        exclude.mkdir()  # now open(exclude, "a") raises IsADirectoryError (OSError)
        exclude_artifacts(repo)  # must not raise


# ---------------------------------------------------------------------------
# pin_cache_env: export cache env from the (untracked) pre-commit hook
# ---------------------------------------------------------------------------
class TestPinCacheEnv:
    def _install_hook(self, repo: Path, body: str) -> Path:
        # Resolve where git actually looks for the hook (honors core.hooksPath),
        # so the test is hermetic regardless of ambient git config.
        hook = ws._git_hook_path(repo)
        assert hook is not None
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_text(body)
        return hook

    def test_exports_inserted_after_shebang_sorted(self, tmp_path):
        repo = _git_repo(tmp_path)
        hook = self._install_hook(repo, "#!/bin/sh\necho original\n")
        cache = _cache(tmp_path, env={"TESTMON_ENV": "shared", "AAA": "one"})

        pin_cache_env(repo, [cache])

        lines = hook.read_text().splitlines()
        assert lines[0] == "#!/bin/sh"  # shebang stays first
        # Sorted by key: AAA before TESTMON_ENV, both before the original body.
        assert lines[1] == "export AAA=one"
        assert lines[2] == "export TESTMON_ENV=shared"
        assert lines[3] == "echo original"

    def test_value_is_shell_quoted(self, tmp_path):
        repo = _git_repo(tmp_path)
        hook = self._install_hook(repo, "#!/bin/sh\n")
        cache = _cache(tmp_path, env={"K": "a b;c"})
        pin_cache_env(repo, [cache])
        assert "export K='a b;c'" in hook.read_text()

    def test_no_shebang_inserts_at_top(self, tmp_path):
        repo = _git_repo(tmp_path)
        hook = self._install_hook(repo, "echo hi\n")
        pin_cache_env(repo, [_cache(tmp_path, env={"K": "v"})])
        assert hook.read_text().splitlines()[0] == "export K=v"

    def test_idempotent_second_call_is_noop(self, tmp_path):
        repo = _git_repo(tmp_path)
        hook = self._install_hook(repo, "#!/bin/sh\necho hi\n")
        cache = _cache(tmp_path, env={"K": "v"})
        pin_cache_env(repo, [cache])
        after_first = hook.read_text()
        pin_cache_env(repo, [cache])
        assert hook.read_text() == after_first

    def test_empty_env_is_a_noop(self, tmp_path):
        repo = _git_repo(tmp_path)
        hook = self._install_hook(repo, "#!/bin/sh\necho hi\n")
        pin_cache_env(repo, [_cache(tmp_path, env={})])
        assert hook.read_text() == "#!/bin/sh\necho hi\n"

    def test_missing_hook_is_a_noop(self, tmp_path):
        repo = _git_repo(tmp_path)  # no hook installed
        # Must not raise even though there is nothing to pin into.
        pin_cache_env(repo, [_cache(tmp_path, env={"K": "v"})])
        hook = ws._git_hook_path(repo)
        assert hook is not None and not hook.is_file()

    def test_tracked_hook_is_left_untouched(self, tmp_path, monkeypatch):
        """A git-tracked hook must never be dirtied by env pinning."""
        repo = _git_repo(tmp_path)
        tracked = repo / "tracked-hook.sh"
        tracked.write_text("#!/bin/sh\necho hi\n")
        subprocess.run(["git", "-C", str(repo), "add", "tracked-hook.sh"], check=True)
        subprocess.run(
            ["git", "-C", str(repo), "commit", "-q", "-m", "add hook"], check=True
        )
        monkeypatch.setattr(ws, "_git_hook_path", lambda directory: tracked)
        pin_cache_env(repo, [_cache(tmp_path, env={"K": "v"})])
        assert tracked.read_text() == "#!/bin/sh\necho hi\n"  # unchanged


class TestGitHookPath:
    def test_resolves_inside_a_repo(self, tmp_path):
        repo = _git_repo(tmp_path)
        hook = ws._git_hook_path(repo)
        assert hook is not None
        assert hook.is_absolute()
        assert hook.name == "pre-commit"

    def test_none_outside_a_repo(self, tmp_path):
        # A bare non-git directory: rev-parse fails, so no hook path.
        plain = tmp_path / "plain"
        plain.mkdir()
        assert ws._git_hook_path(plain) is None
