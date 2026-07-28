"""Cache refresher: it must NOT run auto-detected setup commands.

Regression for a live failure: the refresher cloned a repo, ran the
auto-detected `uv sync --all-groups`, and then its `uv run --group test pytest`
refresh command transitioned the env to a narrower group set and unlinked pytest
mid-run ("Failed to spawn: pytest"). The refresh command provisions its own
environment (as CI does), so the refresher skips auto setup and only runs
EXPLICITLY-configured setup_commands.
"""

import asyncio
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from backend.ticket_ingestion import cache_refresher as cr
from backend.ticket_ingestion.config import PipelineConfig
from backend.workspace_setup import CacheSeed


def _make_sqlite(path: Path, rows=(("a",),)) -> None:
    con = sqlite3.connect(str(path))
    con.execute("CREATE TABLE t(x TEXT)")
    con.executemany("INSERT INTO t VALUES (?)", rows)
    con.commit()
    con.close()


def _corrupt_sqlite(path: Path) -> None:
    """A file that LOOKS like sqlite (valid magic header) but won't open."""
    path.write_bytes(cr._SQLITE_MAGIC + b"\x00garbage-not-a-real-db" * 8)


def _refresher(setup_commands, tmp_path, monkeypatch):
    cfg = PipelineConfig(repo_url="git@x:o/r.git", setup_commands=setup_commands)
    cache = CacheSeed(
        name="testmon",
        seed_path=tmp_path / "seed",
        workspace_path=".testmondata",
        refresh_command="uv run --group test pytest --testmon -q",
    )
    r = cr.CacheRefresher(cfg, cache)
    # Isolate the workspace to tmp so the real _heal_cache_artifact (now run by
    # _refresh_once) never touches the real ./workspaces checkout.
    r.directory = tmp_path / "ws"
    r.directory.mkdir(parents=True, exist_ok=True)
    calls = {"setup": [], "refresh": 0}

    async def _noop():
        return None

    async def _fake_setup(commands, directory, **kw):
        calls["setup"].append(list(commands))

    async def _fake_refresh():
        calls["refresh"] += 1

    monkeypatch.setattr(r, "_ensure_workspace", _noop)
    monkeypatch.setattr(r, "_sync_to_branch", _noop)
    monkeypatch.setattr(r, "_publish_seed", _noop)
    monkeypatch.setattr(r, "_run_refresh_command", _fake_refresh)
    monkeypatch.setattr(cr, "run_setup_commands_async", _fake_setup)
    return r, calls


def test_refresher_skips_auto_setup_when_unset(tmp_path, monkeypatch):
    r, calls = _refresher(None, tmp_path, monkeypatch)  # None => auto-detect
    asyncio.run(r._refresh_once())
    # Auto setup (uv sync --all-groups / pre-commit) is NOT run — that's the bug.
    assert calls["setup"] == []
    # The refresh command still runs.
    assert calls["refresh"] == 1


def test_refresher_runs_explicit_setup(tmp_path, monkeypatch):
    r, calls = _refresher(["make deps"], tmp_path, monkeypatch)
    asyncio.run(r._refresh_once())
    assert calls["setup"] == [["make deps"]]
    assert calls["refresh"] == 1


def test_refresher_honours_explicit_empty_setup(tmp_path, monkeypatch):
    # An explicit empty list is honoured (still "no commands", but distinct from
    # auto-detect None) and must not fall back to auto-detection.
    r, calls = _refresher([], tmp_path, monkeypatch)
    asyncio.run(r._refresh_once())
    assert calls["setup"] == [[]]
    assert calls["refresh"] == 1


# --------------------------------------------------------------------------- #
# sqlite helpers
# --------------------------------------------------------------------------- #
def test_looks_like_sqlite(tmp_path):
    db = tmp_path / "db"
    _make_sqlite(db)
    assert cr._looks_like_sqlite(db) is True
    txt = tmp_path / "notes.txt"
    txt.write_text("hello")
    assert cr._looks_like_sqlite(txt) is False
    assert cr._looks_like_sqlite(tmp_path / "missing") is False


def test_sqlite_intact_true_false(tmp_path):
    good = tmp_path / "good.db"
    _make_sqlite(good)
    assert cr._sqlite_intact(good) is True
    bad = tmp_path / "bad.db"
    _corrupt_sqlite(bad)
    assert cr._sqlite_intact(bad) is False


def test_remove_sqlite_sidecars(tmp_path):
    art = tmp_path / ".testmondata"
    art.write_bytes(cr._SQLITE_MAGIC)
    for suffix in ("-wal", "-shm", "-journal"):
        (tmp_path / (".testmondata" + suffix)).write_text("stale")
    cr._remove_sqlite_sidecars(art)
    assert art.exists()  # main file untouched
    for suffix in ("-wal", "-shm", "-journal"):
        assert not (tmp_path / (".testmondata" + suffix)).exists()


# --------------------------------------------------------------------------- #
# _heal_cache_artifact
# --------------------------------------------------------------------------- #
def _heal_refresher(tmp_path):
    cfg = PipelineConfig(repo_url="git@x:o/r.git")
    cache = CacheSeed(
        name="testmon", seed_path=tmp_path / "seed.db", workspace_path=".testmondata"
    )
    r = cr.CacheRefresher(cfg, cache)
    r.directory = tmp_path / "ws"
    r.directory.mkdir(parents=True, exist_ok=True)
    return r


def test_heal_clears_orphaned_sidecars(tmp_path):
    r = _heal_refresher(tmp_path)
    art = r.directory / ".testmondata"
    _make_sqlite(art)
    (r.directory / ".testmondata-wal").write_text("orphan")
    (r.directory / ".testmondata-shm").write_text("orphan")
    r._heal_cache_artifact()
    assert art.exists()  # good DB kept
    assert not (r.directory / ".testmondata-wal").exists()
    assert not (r.directory / ".testmondata-shm").exists()


def test_heal_discards_corrupt_artifact_and_restores_seed(tmp_path):
    r = _heal_refresher(tmp_path)
    _make_sqlite(r.cache.seed_path, rows=(("seed-value",),))  # last-good seed
    art = r.directory / ".testmondata"
    _corrupt_sqlite(art)  # looks like sqlite, won't open
    r._heal_cache_artifact()
    # Corrupt artifact replaced by the seed's contents.
    assert cr._sqlite_intact(art)
    con = sqlite3.connect(str(art))
    assert con.execute("SELECT x FROM t").fetchone()[0] == "seed-value"
    con.close()


def test_heal_leaves_good_artifact_untouched(tmp_path):
    r = _heal_refresher(tmp_path)
    art = r.directory / ".testmondata"
    _make_sqlite(art, rows=(("keep-me",),))
    r._heal_cache_artifact()
    con = sqlite3.connect(str(art))
    assert con.execute("SELECT x FROM t").fetchone()[0] == "keep-me"
    con.close()


def test_heal_noop_for_nonsqlite_artifact(tmp_path):
    r = _heal_refresher(tmp_path)
    art = r.directory / ".testmondata"
    art.write_text("just a build cache, not sqlite")
    r._heal_cache_artifact()
    assert art.read_text() == "just a build cache, not sqlite"


# --------------------------------------------------------------------------- #
# _publish_seed integrity guard
# --------------------------------------------------------------------------- #
def test_publish_skips_corrupt_artifact_keeping_old_seed(tmp_path):
    r = _heal_refresher(tmp_path)
    _make_sqlite(r.cache.seed_path, rows=(("old-good-seed",),))  # existing seed
    _corrupt_sqlite(r.directory / ".testmondata")  # crashed run left a bad DB
    asyncio.run(r._publish_seed())
    # The good seed must NOT be overwritten by the corrupt artifact.
    assert cr._sqlite_intact(r.cache.seed_path)
    con = sqlite3.connect(str(r.cache.seed_path))
    assert con.execute("SELECT x FROM t").fetchone()[0] == "old-good-seed"
    con.close()


def test_publish_writes_good_artifact(tmp_path):
    r = _heal_refresher(tmp_path)
    _make_sqlite(r.directory / ".testmondata", rows=(("fresh",),))
    asyncio.run(r._publish_seed())
    assert r.cache.seed_path.is_file()
    con = sqlite3.connect(str(r.cache.seed_path))
    assert con.execute("SELECT x FROM t").fetchone()[0] == "fresh"
    con.close()


def test_publish_no_artifact_is_noop(tmp_path):
    # No refreshed artifact at all -> skip the publish (don't touch the seed).
    r = _heal_refresher(tmp_path)
    asyncio.run(r._publish_seed())
    assert not r.cache.seed_path.exists()


# --------------------------------------------------------------------------- #
# run_forever guard
# --------------------------------------------------------------------------- #
def test_run_forever_disabled_without_refresh_command(tmp_path):
    cfg = PipelineConfig(repo_url="git@x:o/r.git")
    cache = CacheSeed(
        name="nocmd", seed_path=tmp_path / "seed", workspace_path=".data"
    )  # no refresh_command
    r = cr.CacheRefresher(cfg, cache)
    # Returns immediately (no clone, no infinite loop) because it's disabled.
    asyncio.run(r.run_forever())


# --------------------------------------------------------------------------- #
# git plumbing: _check_run / _ensure_workspace / _sync_to_branch
# --------------------------------------------------------------------------- #
def _cmd_refresher(tmp_path):
    cfg = PipelineConfig(repo_url="https://example/repo.git")
    cache = CacheSeed(
        name="testmon",
        seed_path=tmp_path / "seed.db",
        workspace_path=".testmondata",
        refresh_branch="main",
        refresh_command="run-tests",
    )
    r = cr.CacheRefresher(cfg, cache)
    r.directory = tmp_path / "ws"
    r.directory.mkdir(parents=True, exist_ok=True)
    return r


class TestCheckRun:
    async def test_raises_on_nonzero(self, tmp_path, monkeypatch):
        r = _cmd_refresher(tmp_path)

        async def fake_run(*args, cwd=None, timeout=None):
            return 2, b"", b"fatal: boom"

        monkeypatch.setattr(r, "_run", fake_run)
        with pytest.raises(
            RuntimeError, match="git fetch failed \\(rc=2\\): fatal: boom"
        ):
            await r._check_run("git", "fetch", cwd=None, err="git fetch")

    async def test_ok_on_zero(self, tmp_path, monkeypatch):
        r = _cmd_refresher(tmp_path)

        async def fake_run(*args, cwd=None, timeout=None):
            return 0, b"", b""

        monkeypatch.setattr(r, "_run", fake_run)
        await r._check_run("git", "status", cwd=None, err="git status")


class TestEnsureWorkspace:
    async def test_existing_git_checkout_short_circuits(self, tmp_path, monkeypatch):
        r = _cmd_refresher(tmp_path)
        (r.directory / ".git").mkdir()

        async def boom(*a, **k):
            raise AssertionError("should not clone when .git exists")

        monkeypatch.setattr(r, "_check_run", boom)
        await r._ensure_workspace()  # returns without cloning

    async def test_clones_and_seeds(self, tmp_path, monkeypatch):
        r = _cmd_refresher(tmp_path)
        _make_sqlite(r.cache.seed_path, rows=(("seeded",),))
        calls = []

        async def fake_check_run(*args, **kwargs):
            calls.append(args)

        monkeypatch.setattr(r, "_check_run", fake_check_run)
        await r._ensure_workspace()
        # A clone was issued against the configured repo_url.
        assert calls and calls[0][0] == "git" and calls[0][1] == "clone"
        assert "https://example/repo.git" in calls[0]
        # The last-published seed was copied into the workspace path.
        target = r.directory / r.cache.workspace_path
        assert target.is_file()


class TestSyncToBranch:
    async def test_runs_fetch_checkout_reset_clean(self, tmp_path, monkeypatch):
        r = _cmd_refresher(tmp_path)
        calls = []

        async def fake_check_run(*args, cwd=None, err="", timeout=None):
            calls.append(args)

        monkeypatch.setattr(r, "_check_run", fake_check_run)
        await r._sync_to_branch()
        verbs = [a[1] for a in calls]
        assert verbs == ["fetch", "checkout", "reset", "clean"]
        # checkout / reset target the fetched remote branch.
        assert ("git", "checkout", "-B", "main", "origin/main") == calls[1]
        assert ("git", "reset", "--hard", "origin/main") == calls[2]
        # clean preserves the venv and the cache artifact.
        assert ".venv" in calls[3] and ".testmondata" in calls[3]


# --------------------------------------------------------------------------- #
# _run_refresh_command (subprocess shell mocked)
# --------------------------------------------------------------------------- #
class _ShellProc:
    def __init__(self, returncode=0, stderr=b""):
        self.returncode = returncode
        self._stderr = stderr
        self.killed = False

    def kill(self):
        self.killed = True

    async def communicate(self):
        return None, self._stderr


def _patch_shell(proc):
    from unittest.mock import AsyncMock

    return patch(
        "asyncio.create_subprocess_shell",
        new_callable=AsyncMock,
        return_value=proc,
    )


class TestRunRefreshCommand:
    async def test_success(self, tmp_path):
        r = _cmd_refresher(tmp_path)
        with _patch_shell(_ShellProc(returncode=0)):
            await r._run_refresh_command()  # no raise

    async def test_nonzero_with_artifact_is_tolerated(self, tmp_path, caplog):
        # e.g. pytest --testmon exits 1 on test failures but still writes the DB.
        r = _cmd_refresher(tmp_path)
        (r.directory / r.cache.workspace_path).write_bytes(b"artifact")
        with _patch_shell(_ShellProc(returncode=1, stderr=b"1 failed")):
            with caplog.at_level("WARNING"):
                await r._run_refresh_command()  # no raise: artifact exists
        assert any("exited rc=1" in rec.getMessage() for rec in caplog.records)

    async def test_nonzero_without_artifact_raises(self, tmp_path):
        r = _cmd_refresher(tmp_path)  # no artifact written
        with _patch_shell(_ShellProc(returncode=1, stderr=b"boom")):
            with pytest.raises(RuntimeError, match="produced no artifact"):
                await r._run_refresh_command()

    async def test_timeout_raises(self, tmp_path):
        r = _cmd_refresher(tmp_path)
        proc = _ShellProc(returncode=0)

        async def raise_timeout(coro, timeout):
            coro.close()  # avoid "coroutine never awaited"
            raise asyncio.TimeoutError

        with (
            _patch_shell(proc),
            patch(
                "backend.ticket_ingestion.cache_refresher.asyncio.wait_for",
                raise_timeout,
            ),
        ):
            with pytest.raises(RuntimeError, match="timed out"):
                await r._run_refresh_command()
        assert proc.killed is True
