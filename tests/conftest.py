"""Shared test fixtures for the ticket-ingestion-pipeline test suite."""

import os
import subprocess
import sys
import tempfile
import warnings
from pathlib import Path

import pytest

# Make the unified ``backend`` package (at the repo root, not pip-installed)
# importable under a plain ``pytest`` / ``uv run pytest`` invocation, so the
# engine parity tests and the pipeline tests run from one environment without a
# bespoke PYTHONPATH.
_REPO_ROOT = Path(__file__).resolve().parent.parent
for _p in (_REPO_ROOT,):
    _ps = str(_p)
    if _p.is_dir() and _ps not in sys.path:
        sys.path.insert(0, _ps)


@pytest.fixture(autouse=True)
def _redirect_tempfiles(tmp_path, monkeypatch):
    """Keep every test's real filesystem writes inside pytest's ``tmp_path``.

    Several pipeline paths write real files (e.g. ``ClaudeCodeRunner.invoke``
    writes a prompt ``.md``). Without this the suite accretes stray files — and
    the ingestion pipeline's testmon refresher runs the full suite on every
    start, so they accumulate forever. We (1) point ``tempfile`` at ``tmp_path``
    and (2) point ``MINDFLOCK_ASSISTANT_DIR`` (prompts, pricing/usage caches, exit
    markers, scroll-speed, etc.) at ``tmp_path`` so both land in auto-cleaned space.
    """
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setenv("MINDFLOCK_ASSISTANT_DIR", str(tmp_path / "assistant"))
    # Point Claude pre-trust seeding (providers.claude.pre_trust_workdir, F2)
    # at a per-test file so no test can ever read or write the user's real
    # ``~/.claude.json``.
    monkeypatch.setenv("MINDFLOCK_CLAUDE_JSON", str(tmp_path / "claude-user.json"))
    # Point the user settings store (``~/.mindflock/settings.json``) at a per-test
    # tmp path so (a) no test reads or writes the real user store, and (b) the
    # settings layer stays empty for config-parsing tests unless a test
    # explicitly populates it — keeping config.toml-only behaviour deterministic.
    monkeypatch.setenv(
        "MINDFLOCK_SETTINGS_FILE", str(tmp_path / "mindflock" / "settings.json")
    )
    # Point the O4 port-block allocation store (``~/.mindflock/ports.json``) at a
    # per-test tmp file — create_instance allocates a block for every session,
    # so any test that creates one would otherwise write the real user store.
    monkeypatch.setenv(
        "MINDFLOCK_PORTS_FILE", str(tmp_path / "mindflock" / "ports.json")
    )
    # Point the Verify test-plan store (``~/.mindflock/test_plans.json``) at a
    # per-test tmp file. This one is not merely hygiene: the lifespan registers
    # ``_test_plans_due_loop``, which does its first pass *immediately* (work
    # first, sleep after), so every ``with TestClient(server.app)`` in the suite
    # would otherwise read the developer's real plans — and, for any plan whose
    # branch had reached the live branch, write ``state="due"`` back to that real
    # file and fire a real ntfy push at their phone.
    monkeypatch.setenv(
        "MINDFLOCK_TEST_PLANS_FILE", str(tmp_path / "mindflock" / "test_plans.json")
    )
    # The autopilot store, for the same reason as the rest: server code that
    # consults _autopilot.get() (the turn-end announcement gate does) must read
    # an empty per-test store, never the developer's live ~/.mindflock/
    # autopilot.json — a real armed run on this machine would silently gate a
    # test's events. Tests that fake their own store re-set this themselves.
    monkeypatch.setenv(
        "MINDFLOCK_AUTOPILOT_FILE", str(tmp_path / "mindflock" / "autopilot.json")
    )
    # Neutralize the web auth gate's enable signals so the suite never depends on
    # the ambient shell. auth.auth_enabled() turns on when CS_WEB_MODE is a
    # non-local mode (a dev shell often exports CS_WEB_MODE=tailscale), when an
    # env token is set, or when MINDFLOCK_AUTH is truthy — any of which makes
    # every TestClient call 401. The auth module's own contract says the test
    # suite must run with the gate OFF (see auth._exposed_mode); enforce it here
    # instead of relying on the inherited environment. A test that needs the gate
    # on sets these itself.
    for _var in ("CS_WEB_MODE", "MINDFLOCK_AUTH", "MINDFLOCK_AUTH_TOKEN"):
        monkeypatch.delenv(_var, raising=False)
    try:
        from backend.config import settings as _settings

        _settings.invalidate()  # drop any cached parse from a prior test
    except Exception:  # noqa: BLE001 — settings module import is best-effort here
        pass


@pytest.fixture(autouse=True)
def _no_tailnet_side_effects(monkeypatch):
    """No test machine has a tailnet, and no test fires a phone-URL push.

    Two escapes this closes, both introduced by the "here's your phone URL"
    notification (backend.web.core.mobile_announce):

    * the probes shell out to ``tailscale``, so on a developer's own machine the
      suite would pick up their real tailnet name — and take seconds doing it;
    * ``announce_soon`` is fire-and-forget on a thread of its own, so a test that
      merely enables ntfy could outlive its own monkeypatches and land a *real*
      push (or a stray call into the next test's fake transport).

    Tests that exercise either path re-patch these themselves — see
    tests/unit/test_mobile_announce.py.
    """
    from backend.web import server
    from backend.web.core import mobile_announce

    monkeypatch.setattr(server, "_tailscale_info", lambda: (None, None))
    monkeypatch.setattr(server, "_tailscale_serves_port", lambda port: False)
    monkeypatch.setattr(mobile_announce, "announce_soon", lambda reason: None)
    monkeypatch.setattr(mobile_announce, "_refresh_cache_soon", lambda: None)
    monkeypatch.setattr(mobile_announce, "_CACHED_URL", None)
    monkeypatch.setattr(mobile_announce, "_CACHED_AT", 0.0)


@pytest.fixture(autouse=True)
def _no_boot_quiet(monkeypatch):
    """Tests run within seconds of importing the server module, which is
    exactly the post-launch quiet window that swallows *_changed events and the
    boot budget re-announce (server._BOOT_QUIET_SECONDS) — so every test would
    silently sit inside it. Disable it; the quiet-window tests re-arm it
    themselves (see tests/unit/test_events.py)."""
    from backend.web import server

    monkeypatch.setattr(server, "_BOOT_QUIET_SECONDS", 0.0)


@pytest.fixture
def isolate_settings_store(tmp_path, monkeypatch):
    """Point the user settings store at a flat per-test ``settings.json`` and
    drop the parse cache around the test.

    The single, canonical settings-store isolation, shared by the settings-
    focused test modules (each requests it from a thin autouse shim) instead of
    every file re-declaring the same ``_isolate_store`` fixture. The autouse
    ``_redirect_tempfiles`` above already isolates the store under a
    ``mindflock/`` subdir; this re-points it straight at ``tmp_path`` because a
    few tests read or write the store *at its exact path*
    (``tmp_path / "settings.json"``). It runs after ``_redirect_tempfiles`` (it
    is requested by a module-local autouse fixture), so this flat path wins.
    """
    from backend.config import settings as _settings

    monkeypatch.setenv("MINDFLOCK_SETTINGS_FILE", str(tmp_path / "settings.json"))
    _settings.invalidate()
    yield
    _settings.invalidate()


@pytest.fixture(scope="session")
def _isolated_gitconfig_path(tmp_path_factory):
    """A private, static global git config shared by the whole session.

    Kept in a session-scoped directory of its own — deliberately *not* under any
    test's ``tmp_path`` — so it never litters a directory a test enumerates
    (workspace-cleanup / no-temp-litter assertions scan ``tmp_path`` children).
    """
    base = tmp_path_factory.mktemp("gitconfig-isolation")
    gitconfig = base / "gitconfig"
    gitconfig.write_text(
        "[user]\n"
        "\tname = MindFlock Test\n"
        "\temail = test@example.invalid\n"
        "[commit]\n"
        "\tgpgsign = false\n"
        "[init]\n"
        "\tdefaultBranch = main\n"
        "[core]\n"
        # Point at a path with no hook files so no ambient global/system hook runs.
        "\thooksPath = %s\n" % (base / "no-git-hooks"),
    )
    return gitconfig


@pytest.fixture(autouse=True)
def _isolate_git_config(_isolated_gitconfig_path, monkeypatch):
    """Insulate every real-``git`` subprocess from ambient, mutable git state.

    A large share of the suite (worktree setup, diff-stat, preflight, the wave4
    backend) creates real throwaway repos and shells out to ``git``. Those
    subprocesses would otherwise read the developer's ``~/.gitconfig`` and the
    system ``/etc/gitconfig`` — both shared, mutable, and outside the test's
    control. A global ``commit.gpgsign = true``, a ``core.hooksPath`` pointing at
    a slow or failing hook, an ``includeIf`` rule, or a *concurrent* process
    rewriting global config (the ingestion pipeline's testmon refresher runs the
    full suite on every start, so two runs can overlap) can make these otherwise
    hermetic tests flake en masse.

    Pin ``git`` to a private, empty global config that carries only a committer
    identity, and skip system config entirely, so ``git`` behaviour becomes a pure
    function of each test's own repo. A test that needs different global config
    (e.g. ``test_git_ops`` probing origin state) still overrides these via its own
    ``monkeypatch.setenv`` in the test body — which runs after this fixture.

    Config does not only arrive in FILES. ``git -c k=v`` re-exports its options
    to every child process as ``GIT_CONFIG_PARAMETERS``, and
    ``GIT_CONFIG_COUNT``/``KEY_n``/``VALUE_n`` inject config the same way — so a
    suite launched from inside any git process (a hook, a ``!`` alias, a wrapper)
    silently inherits that repo's overrides no matter what the file variables
    say. That hole has bitten once already: an inherited
    ``safe.bareRepository=explicit`` makes every ``git -C <bare repo>`` in the
    suite fail, which surfaced as five provisioning tests reporting that a push
    never reached its (bare) test forge.
    """
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(_isolated_gitconfig_path))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    # A stray process/shell must not be able to redirect these subprocesses at the
    # repo, index, object-store or ref-namespace level either — nor inject config
    # through the environment, which bypasses the file variables set above.
    for _var in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_CONFIG",
        "GIT_CONFIG_PARAMETERS",
        "GIT_NAMESPACE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_QUARANTINE_PATH",
        "GIT_TEMPLATE_DIR",
    ):
        monkeypatch.delenv(_var, raising=False)
    # GIT_CONFIG_COUNT=n is read together with GIT_CONFIG_KEY_i/VALUE_i; dropping
    # the count alone disarms the pairs, but they are cheap to clear too.
    _count = os.environ.get("GIT_CONFIG_COUNT", "")
    monkeypatch.delenv("GIT_CONFIG_COUNT", raising=False)
    if _count.isdigit():
        for _i in range(int(_count)):
            monkeypatch.delenv("GIT_CONFIG_KEY_{}".format(_i), raising=False)
            monkeypatch.delenv("GIT_CONFIG_VALUE_{}".format(_i), raising=False)


def _real_repo_worktree_paths():
    """Set of worktree paths registered in the *real* repo, or ``None``.

    Returns ``None`` when the working directory is not a git repo or ``git`` is
    unavailable — the guard simply no-ops in that case.
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(_REPO_ROOT), "worktree", "list", "--porcelain"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    paths = set()
    for line in out.stdout.decode("utf-8", "replace").splitlines():
        if line.startswith("worktree "):
            paths.add(line[len("worktree ") :].strip())
    return paths


@pytest.fixture(scope="session", autouse=True)
def _guard_real_repo_worktrees():
    """Detect (and self-heal) worktrees leaked into the real repo by the suite.

    Every git test is supposed to be hermetic — its repos and its
    ``$HOME/.mindflock/worktrees`` live under ``tmp_path``. If a regression ever
    lets a test register a worktree in the *actual* checkout, that stale entry is
    exactly what makes later runs (and the running MindFlock app, which iterates
    real worktrees) flake. Snapshot the real repo's worktrees at session start;
    at teardown, prune and report any newly-appeared paths.

    This is intentionally a *warning*, not a hard failure: the user's stated goal
    is resilience against a concurrent MindFlock app, and that app can legitimately
    add worktrees mid-run — hard-failing on those would defeat the purpose. A real
    test leak still surfaces loudly in the run output, and the prune keeps the
    checkout clean for the next run.
    """
    before = _real_repo_worktree_paths()
    yield
    if before is None:
        return
    # Prune administrative refs for worktrees whose dirs are already gone, then
    # compare what remains against the starting snapshot.
    subprocess.run(
        ["git", "-C", str(_REPO_ROOT), "worktree", "prune"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=15,
    )
    after = _real_repo_worktree_paths()
    if after is None:
        return
    leaked = after - before
    if leaked:
        warnings.warn(
            "Test suite left new worktree(s) registered in the real repo — a test "
            "is not fully isolated (or a concurrent MindFlock app added them): "
            + ", ".join(sorted(leaked)),
            stacklevel=1,
        )
