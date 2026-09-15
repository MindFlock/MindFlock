"""Settings → Advanced → "Update to the newest version".

The interesting parts of :mod:`backend.web.core.self_update` are the ones that
run when nobody is watching: a generated shell script that has to survive the
venv it is replacing, a state file that has to outlive the restart it triggers,
and one refusal — a dev checkout — that exists to stop a settings button from
quietly replacing someone's working tree with a release build.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from backend.web.core import self_update


# --------------------------------------------------------------------------- #
# Versions
# --------------------------------------------------------------------------- #
def test_parse_version_tolerates_the_tags_people_actually_cut():
    assert self_update.parse_version("v0.3.2") == (0, 3, 2)
    assert self_update.parse_version("0.3.2") == (0, 3, 2)
    # A pre-release suffix is not a reason to raise in a settings screen.
    assert self_update.parse_version("1.2.3rc1") == (1, 2, 3)
    assert self_update.parse_version("") == (0,)


def test_is_newer_compares_numerically_not_as_strings():
    assert self_update.is_newer("0.10.0", "0.9.9")  # the string compare says no
    assert self_update.is_newer("1.0", "0.9.9")
    assert not self_update.is_newer("0.3.2", "0.3.2")
    assert not self_update.is_newer("0.3.1", "0.3.2")


def test_an_unreadable_version_never_offers_an_update():
    # "I can't tell what you're running" is not grounds for replacing it.
    assert not self_update.is_newer("0.4.0", "")
    assert not self_update.is_newer("", "0.3.2")


# --------------------------------------------------------------------------- #
# What this install is, and whether it may be replaced
# --------------------------------------------------------------------------- #
def test_a_dev_checkout_is_refused_rather_than_clobbered(monkeypatch):
    # The whole reason install_kind exists: `uv tool install --force` over an
    # editable install swaps the developer's checkout for a release build.
    monkeypatch.setattr(self_update, "install_kind", lambda: "editable")
    reason = self_update.blocked_reason()
    assert "development checkout" in reason
    assert "git pull" in reason
    assert self_update.start_update("v9.9.9") == {"ok": False, "error": reason}


def test_an_install_we_dont_recognise_is_refused_too(monkeypatch):
    monkeypatch.setattr(self_update, "install_kind", lambda: "other")
    assert "install.sh" in self_update.blocked_reason()


def test_a_uv_tool_install_with_uv_on_path_is_updatable(monkeypatch):
    monkeypatch.setattr(self_update, "install_kind", lambda: "uv-tool")
    monkeypatch.setattr(self_update.shutil, "which", lambda name: "/usr/bin/" + name)
    assert self_update.blocked_reason() == ""


def test_a_uv_tool_install_without_uv_says_so(monkeypatch):
    monkeypatch.setattr(self_update, "install_kind", lambda: "uv-tool")
    monkeypatch.setattr(self_update.shutil, "which", lambda name: None)
    assert "`uv` is not on the server's PATH" in self_update.blocked_reason()


def test_install_kind_reads_where_the_running_package_actually_is():
    # No monkeypatching: whatever this test run is, the answer must be one of
    # the three the callers branch on — never an exception, and never None.
    assert self_update.install_kind() in {"editable", "uv-tool", "other"}


# --------------------------------------------------------------------------- #
# The state file: it has to outlive the restart it causes
# --------------------------------------------------------------------------- #
@pytest.fixture()
def statedir(tmp_path, monkeypatch):
    """Point the module's state + log at a tmp dir (never the real ~/.mindflock)."""
    monkeypatch.setattr(self_update, "_state_dir", lambda: tmp_path)
    return tmp_path


def test_no_state_file_reads_as_idle(statedir):
    assert self_update.read_state() == {"state": "idle"}


def test_corrupt_state_reads_as_idle_rather_than_raising(statedir):
    self_update.state_path().write_text("{not json", encoding="utf-8")
    assert self_update.read_state() == {"state": "idle"}


def test_finish_state_asks_for_the_restart_exactly_once(statedir):
    # Every open settings screen polls this route, and the restart it triggers
    # is a re-exec — so the second poll must not re-exec the fresh process.
    self_update.write_state(state="done", ref="v9.9.9", code=0)
    first, restart_now = self_update.finish_state()
    assert restart_now and first["state"] == "done"
    _, again = self_update.finish_state()
    assert not again


def test_a_failed_update_never_asks_for_a_restart(statedir):
    self_update.write_state(state="failed", ref="v9.9.9", code=1)
    state, restart_now = self_update.finish_state()
    assert state["state"] == "failed"
    assert not restart_now


def test_a_stale_started_marker_stops_counting_as_running(statedir):
    # A machine that slept (or a script that was killed) must not leave the
    # button disabled for ever.
    self_update.write_state(state="started", started_at=0.0)
    assert not self_update.running()


# --------------------------------------------------------------------------- #
# The generated installer
# --------------------------------------------------------------------------- #
def _run_script(statedir, *, uv_exit: int) -> subprocess.CompletedProcess:
    """Run the real generated script with a STUB `uv` and a throwaway HOME.

    The throwaway HOME matters: the script prepends ``$HOME/.local/bin`` to PATH
    (the installer's own PATH fix), which on a developer's machine is where the
    real uv lives — and this test must never reach it.
    """
    home = statedir / "home"
    (home / ".local" / "bin").mkdir(parents=True)
    bindir = statedir / "bin"
    bindir.mkdir()
    uv = bindir / "uv"
    uv.write_text(
        '#!/bin/sh\necho "uv called: $*"\nexit %d\n' % uv_exit, encoding="utf-8"
    )
    uv.chmod(0o755)

    script = statedir / "gen.sh"
    script.write_text(self_update._script("v9.9.9", "a" * 40), encoding="utf-8")
    env = dict(os.environ, HOME=str(home), PATH="%s:/usr/bin:/bin" % bindir)
    return subprocess.run(
        ["/bin/sh", str(script)], env=env, capture_output=True, text=True, timeout=60
    )


def test_the_generated_script_is_valid_sh(statedir):
    script = statedir / "gen.sh"
    script.write_text(self_update._script("v9.9.9", "a" * 40), encoding="utf-8")
    # -n parses without executing: no uv is run, nothing is installed.
    assert subprocess.run(["/bin/sh", "-n", str(script)]).returncode == 0


def test_a_successful_install_writes_done_and_the_version(statedir):
    cp = _run_script(statedir, uv_exit=0)
    assert cp.returncode == 0
    state = json.loads(self_update.state_path().read_text(encoding="utf-8"))
    assert state["state"] == "done"
    assert state["ref"] == "v9.9.9"
    # The `v` is stripped for display, so the screen can say "you're on 9.9.9".
    assert state["version"] == "9.9.9"
    assert state["code"] == 0
    # And the spec it installed is the pinned COMMIT, not the moving tag.
    log = self_update.log_path().read_text(encoding="utf-8")
    assert "git+" in log and "a" * 40 in log


def test_a_failed_install_writes_failed_with_the_exit_code(statedir):
    cp = _run_script(statedir, uv_exit=7)
    assert cp.returncode == 7
    state = json.loads(self_update.state_path().read_text(encoding="utf-8"))
    assert state["state"] == "failed"
    assert state["code"] == 7
    # Which is exactly the state finish_state must NOT restart on.
    _, restart_now = self_update.finish_state()
    assert not restart_now


def test_log_tail_is_bounded_and_survives_a_missing_file(statedir):
    assert self_update.log_tail() == []
    self_update.log_path().write_text(
        "\n".join(str(i) for i in range(500)), encoding="utf-8"
    )
    tail = self_update.log_tail(limit=10)
    assert tail == [str(i) for i in range(490, 500)]


# --------------------------------------------------------------------------- #
# Starting one
# --------------------------------------------------------------------------- #
def test_start_refuses_a_ref_that_resolves_to_nothing(statedir, monkeypatch):
    monkeypatch.setattr(self_update, "blocked_reason", lambda: "")
    monkeypatch.setattr(self_update, "_resolve_commit", lambda ref: "")
    out = self_update.start_update("v0.0.0-nope")
    assert not out["ok"] and "could not resolve" in out["error"]
    # Nothing was spawned, so nothing claimed the state file either.
    assert self_update.read_state() == {"state": "idle"}


def test_start_refuses_while_one_is_already_running(statedir, monkeypatch):
    monkeypatch.setattr(self_update, "blocked_reason", lambda: "")
    monkeypatch.setattr(self_update, "running", lambda: True)
    out = self_update.start_update("v9.9.9")
    assert not out["ok"] and "already running" in out["error"]


def test_start_marks_the_state_before_it_spawns(statedir, monkeypatch):
    # The window between "spawned" and "the script's first write" is real, and a
    # UI polling in it must see `started`, not `idle`.
    monkeypatch.setattr(self_update, "blocked_reason", lambda: "")
    monkeypatch.setattr(self_update, "_resolve_commit", lambda ref: "b" * 40)
    spawned = {}

    class _Popen:
        def __init__(self, argv, **kw):
            spawned["argv"] = argv
            spawned["session"] = kw.get("start_new_session")

    monkeypatch.setattr(self_update.subprocess, "Popen", _Popen)
    out = self_update.start_update("v9.9.9")
    assert out["ok"] and out["commit"] == "b" * 40
    state = self_update.read_state()
    assert state["state"] == "started" and state["commit"] == "b" * 40
    # Detached on purpose: the update ends by restarting this very server.
    assert spawned["session"] is True
    assert str(self_update.state_path().parent / "update.sh") in spawned["argv"]


def test_the_installer_script_is_not_world_readable(statedir, monkeypatch):
    monkeypatch.setattr(self_update, "blocked_reason", lambda: "")
    monkeypatch.setattr(self_update, "_resolve_commit", lambda ref: "c" * 40)
    monkeypatch.setattr(self_update.subprocess, "Popen", lambda *a, **k: None)
    self_update.start_update("v9.9.9")
    mode = (Path(statedir) / "update.sh").stat().st_mode & 0o777
    assert mode == 0o700


@pytest.mark.skipif(shutil.which("git") is None, reason="git is required")
def test_resolve_commit_answers_empty_for_a_ref_that_isnt_there(monkeypatch, tmp_path):
    # Pointed at a local empty repo so the test never touches the network.
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    monkeypatch.setattr(self_update, "INSTALL_REPO", str(tmp_path))
    assert self_update._resolve_commit("v1.2.3") == ""


# --------------------------------------------------------------------------- #
# The injection boundary
# --------------------------------------------------------------------------- #
# `_script` composes a shell script that `/bin/sh` then runs, and `ref` is the
# only part of it that comes from outside this module (a tag name off GitHub, or
# a `ref` in the POST body). Everything interpolated into that script is
# single-quoted by `_sh_quote`, and `start_update` refuses a ref with whitespace
# in it outright — so the two together are what stop a tag name from becoming a
# command. These tests drive the REAL generated script, because the property is
# "sh does not execute it", not "the string looks quoted".
_NASTY_REFS = [
    "v1.0.0; touch pwned",
    "v1.0.0`touch pwned`",
    "v1.0.0$(touch pwned)",
    "v1.0.0'; touch pwned; '",
    "v1.0.0 && touch pwned",
    "v1.0.0\ntouch pwned",
    "$(touch pwned)",
    "'",
]


@pytest.mark.parametrize("ref", _NASTY_REFS)
def test_a_ref_cannot_smuggle_a_command_into_the_installer(statedir, ref):
    """The generated script must parse, run, and leave the payload unexecuted."""
    home = statedir / ("home-%d" % abs(hash(ref)))
    (home / ".local" / "bin").mkdir(parents=True)
    bindir = statedir / ("bin-%d" % abs(hash(ref)))
    bindir.mkdir()
    uv = bindir / "uv"
    # A `uv` that records exactly what it was asked to install, so the ref can
    # be followed all the way through the quoting to where it lands.
    uv.write_text(
        '#!/bin/sh\nprintf \'%s\\n\' "$*" > "$UV_ARGS_FILE"\nexit 0\n', encoding="utf-8"
    )
    uv.chmod(0o755)

    script = statedir / ("gen-%d.sh" % abs(hash(ref)))
    script.write_text(self_update._script(ref, "a" * 40), encoding="utf-8")
    # It is still a syntactically valid script — a quoting bug that merely
    # BREAKS the installer would show up here rather than as a silent no-update.
    assert subprocess.run(["/bin/sh", "-n", str(script)]).returncode == 0

    args_file = statedir / ("uv-args-%d" % abs(hash(ref)))
    env = dict(
        os.environ,
        HOME=str(home),
        PATH="%s:/usr/bin:/bin" % bindir,
        UV_ARGS_FILE=str(args_file),
    )
    cp = subprocess.run(
        ["/bin/sh", str(script)], env=env, capture_output=True, text=True, timeout=60
    )
    assert cp.returncode == 0

    # The payload never ran — not in the script's cwd, not in its HOME.
    assert not (statedir / "pwned").exists()
    assert not (home / "pwned").exists()
    assert not Path.cwd().joinpath("pwned").exists()
    # `uv` still ran, and still against the pinned commit — proving the script
    # did its real job rather than merely failing safely.
    assert ("a" * 40) in args_file.read_text()


@pytest.mark.parametrize("ref", ["v1.0.0; touch pwned", "v1 && touch pwned", "a b"])
def test_start_update_refuses_a_ref_with_whitespace_before_anything_is_written(
    statedir, monkeypatch, ref
):
    """Belt to the quoting's braces, and the earlier of the two.

    Every ref this feature legitimately handles is a git tag, and a tag name
    cannot contain whitespace. Refusing here means the dangerous-looking shapes
    never reach the script generator, the state file or a subprocess at all.
    """
    monkeypatch.setattr(self_update, "blocked_reason", lambda: "")
    monkeypatch.setattr(
        self_update, "_resolve_commit", lambda r: pytest.fail("resolved " + r)
    )
    monkeypatch.setattr(
        self_update.subprocess, "Popen", lambda *a, **k: pytest.fail("spawned")
    )
    out = self_update.start_update(ref)
    assert out == {"ok": False, "error": "no release to update to"}
    assert not (Path(statedir) / "update.sh").exists()
    assert self_update.read_state() == {"state": "idle"}


def test_quoting_survives_a_state_dir_with_a_quote_in_its_name(tmp_path, monkeypatch):
    """The other interpolated strings are paths, and a path is not under this
    module's control either (``GetConfigDir`` follows ``$HOME``)."""
    odd = tmp_path / "it's a dir"
    odd.mkdir()
    monkeypatch.setattr(self_update, "_state_dir", lambda: odd)
    script = tmp_path / "gen.sh"
    script.write_text(self_update._script("v9.9.9", "a" * 40), encoding="utf-8")
    assert subprocess.run(["/bin/sh", "-n", str(script)]).returncode == 0


# --------------------------------------------------------------------------- #
# The release lookup's cache — the "Check again" button's whole contract
# --------------------------------------------------------------------------- #
@pytest.fixture()
def fresh_release_cache():
    """The module-level cache is process-wide; don't leak it between tests."""
    before = dict(self_update._release_cache)
    self_update._release_cache.update({"at": 0.0, "value": None})
    yield
    self_update._release_cache.update(before)


def _stub_github(monkeypatch, calls, tag="v9.9.9"):
    """Stand in for the aiohttp GET inside ``latest_release``."""

    class _Resp:
        status = 200

        async def json(self, content_type=None):
            return {"tag_name": tag, "html_url": "https://x/y", "body": "notes"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class _Session:
        def __init__(self, *a, **kw):
            pass

        def get(self, url, headers=None):
            calls.append(url)
            return _Resp()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    import aiohttp

    monkeypatch.setattr(aiohttp, "ClientSession", _Session)


@pytest.mark.asyncio
async def test_the_release_lookup_is_cached_inside_its_ttl(
    fresh_release_cache, monkeypatch
):
    """Every open settings screen polls this, and GitHub allows 60
    unauthenticated calls an hour — so the second ask inside the window is
    answered from memory."""
    calls: list = []
    _stub_github(monkeypatch, calls)
    first = await self_update.latest_release()
    second = await self_update.latest_release()
    assert first == second
    assert first["tag"] == "v9.9.9" and first["version"] == "9.9.9"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_check_again_bypasses_the_cache(fresh_release_cache, monkeypatch):
    calls: list = []
    _stub_github(monkeypatch, calls)
    await self_update.latest_release()
    await self_update.latest_release(force=True)
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_an_expired_entry_is_re_fetched(fresh_release_cache, monkeypatch):
    calls: list = []
    _stub_github(monkeypatch, calls)
    await self_update.latest_release()
    # Age the entry past the TTL rather than sleeping through it.
    self_update._release_cache["at"] -= self_update.RELEASE_TTL_S + 1
    await self_update.latest_release()
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_a_failed_lookup_is_not_cached_as_an_answer(
    fresh_release_cache, monkeypatch
):
    """ "Couldn't tell" must not stick for fifteen minutes: the next ask has to
    be free to succeed, or one flaky moment silently hides a release."""
    import aiohttp

    class _Boom:
        def __init__(self, *a, **kw):
            raise OSError("no route to host")

    monkeypatch.setattr(aiohttp, "ClientSession", _Boom)
    assert await self_update.latest_release() is None
    assert self_update._release_cache["value"] is None

    calls: list = []
    _stub_github(monkeypatch, calls)
    assert (await self_update.latest_release())["tag"] == "v9.9.9"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_a_release_with_no_tag_is_no_answer_at_all(
    fresh_release_cache, monkeypatch
):
    _stub_github(monkeypatch, [], tag="")
    assert await self_update.latest_release() is None


# --------------------------------------------------------------------------- #
# The stuck-install timeout, and a version we can't read
# --------------------------------------------------------------------------- #
def test_an_install_that_outlives_the_timeout_stops_blocking_the_button(statedir):
    """``running()`` is what the "already running" refusal is built on, so an
    install that was killed (or a machine that slept) must age out of it rather
    than disabling the update for ever."""
    import time as _time

    self_update.write_state(state="started", started_at=_time.time())
    assert self_update.running() is True
    # One second past the cap.
    self_update.write_state(
        state="started", started_at=_time.time() - self_update.INSTALL_TIMEOUT_S - 1
    )
    assert self_update.running() is False


def test_a_timed_out_install_can_be_started_again(statedir, monkeypatch):
    import time as _time

    self_update.write_state(
        state="started", started_at=_time.time() - self_update.INSTALL_TIMEOUT_S - 1
    )
    monkeypatch.setattr(self_update, "blocked_reason", lambda: "")
    monkeypatch.setattr(self_update, "_resolve_commit", lambda ref: "d" * 40)
    monkeypatch.setattr(self_update.subprocess, "Popen", lambda *a, **k: None)
    assert self_update.start_update("v9.9.9")["ok"] is True
    assert self_update.read_state()["state"] == "started"


def test_a_stale_started_marker_never_earns_a_restart(statedir):
    """And it never turns into one either: only ``done`` does."""
    self_update.write_state(state="started", started_at=0.0)
    state, restart_now = self_update.finish_state()
    assert state["state"] == "started" and restart_now is False


def test_an_unreadable_installed_version_answers_empty(monkeypatch):
    """Package metadata missing (a partial install, a vendored tree) must cost
    the version LINE, not the screen — and ``is_newer`` then refuses to offer an
    update against it, so the two together fail closed."""
    import backend

    monkeypatch.delattr(backend, "__version__", raising=False)
    assert self_update.installed_version() == ""
    assert self_update.is_newer("9.9.9", self_update.installed_version()) is False


def test_installed_version_never_raises_when_the_import_itself_fails(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def boom(name, *a, **kw):
        if name == "backend":
            raise ImportError("no metadata")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", boom)
    assert self_update.installed_version() == ""
