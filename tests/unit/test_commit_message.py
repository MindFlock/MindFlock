"""Model-written commit messages: prompt assembly, answer cleaning, and the
one-shot argv every provider contributes.

The CLI itself is never run here — it is stubbed — because what breaks in this
feature is not "does claude work" but the plumbing around it: a wrong flag, a
fenced code block landing in git history, a clean tree being described anyway, or
a failure that doesn't fall back.
"""

import subprocess

import pytest

from backend.providers import base as pbase
from backend.web.core import commit_message as cm


def _git(wt, *args):
    subprocess.run(
        ["git", "-C", str(wt), *args],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


@pytest.fixture
def repo(tmp_path):
    """A repo with one commit and uncommitted work — tracked and untracked."""
    d = tmp_path / "wt"
    d.mkdir()
    _git(d, "init", "-q")
    _git(d, "config", "user.email", "t@example.com")
    _git(d, "config", "user.name", "T")
    (d / "app.py").write_text("print('hi')\n")
    _git(d, "add", "-A")
    _git(d, "commit", "-q", "-m", "init")
    (d / "app.py").write_text("import os\nprint(os.environ['GREETING'])\n")
    (d / "brand_new.py").write_text("VALUE = 1\n")
    return d


# --- the diff we describe ---------------------------------------------------- #


def test_collect_diff_includes_untracked_files(repo):
    """An untracked file is part of the next commit (the commit path stages
    everything), so it has to be part of what the model reads — otherwise a
    session whose whole contribution is new files gets described as no change."""
    stat, patch = cm.collect_diff(str(repo))
    assert "brand_new.py" in stat
    assert "app.py" in stat
    assert "VALUE = 1" in patch


def test_collect_diff_baseline_is_head_not_the_fork_point(repo):
    """Committing the current work must leave nothing to describe: the baseline
    is HEAD, so a second commit is never handed the first one's changes."""
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "work")
    stat, patch = cm.collect_diff(str(repo))
    assert (stat, patch) == ("", "")


def test_collect_diff_works_before_the_first_commit(tmp_path):
    """A repo with an unborn HEAD has no `HEAD` to diff against; the empty tree
    stands in, so the first commit gets a written message like every other."""
    d = tmp_path / "fresh"
    d.mkdir()
    _git(d, "init", "-q")
    (d / "a.txt").write_text("hello\n")
    stat, patch = cm.collect_diff(str(d))
    assert "a.txt" in stat
    assert "hello" in patch


def test_collect_diff_truncates_the_patch_but_not_the_summary(repo):
    (repo / "big.txt").write_text("x" * 5000 + "\n")
    stat, patch = cm.collect_diff(str(repo), budget=500)
    assert len(patch) < 900  # budget + the truncation note
    assert "truncated" in patch
    assert "big.txt" in stat  # the map survives


# --- the prompt -------------------------------------------------------------- #


def test_build_prompt_carries_everything_the_model_needs():
    p = cm.build_prompt("a.py | 2 +-", "@@ -1 +1 @@", branch="fix/login", hint="sc-12")
    assert "a.py | 2 +-" in p and "@@ -1 +1 @@" in p
    assert "fix/login" in p and "sc-12" in p
    # The delimiter clean_message extracts on — the two have to agree.
    assert "<commit>" in p and "</commit>" in p


def test_build_prompt_forbids_attribution_trailers():
    """The owner's history carries no Co-authored-by lines; the prompt says so
    (and clean_message strips them anyway, belt and braces)."""
    assert "Co-authored-by" in cm.build_prompt("s", "d")


# --- cleaning a CLI's answer ------------------------------------------------- #


@pytest.mark.parametrize(
    "raw,expected",
    [
        # THE REGRESSION THIS DELIMITER EXISTS FOR: told to reply with only the
        # message, a real CLI still explained itself first, and that sentence
        # became the commit subject.
        (
            "The staged change adds retry-with-backoff to `fetch`, so the message\n"
            "should describe that.\n\n<commit>\nRetry fetch with exponential backoff"
            "\n\nSleep 2^i seconds between tries.\n</commit>",
            "Retry fetch with exponential backoff\n\nSleep 2^i seconds between tries.",
        ),
        # A CLI that echoes the instruction writes the empty example first.
        (
            "Use <commit></commit> tags, like this:\n<commit>Add retries</commit>",
            "Add retries",
        ),
        # Truncated mid-answer: the opening tag still says where the message began.
        ("Reasoning here.\n<commit>\nAdd retries", "Add retries"),
        # Untagged answers still have to work — the prompt is a request, not a
        # guarantee, and every heuristic below is the no-tags path.
        ("Fix the login redirect\n", "Fix the login redirect"),
        # A fenced block is the single most common shape and the worst one to
        # commit verbatim.
        ("```\nFix the login redirect\n```", "Fix the login redirect"),
        (
            "```text\nAdd retries\n\nBecause flaky.\n```",
            "Add retries\n\nBecause flaky.",
        ),
        # Chatty lead-in.
        ("Here's a commit message:\n\nAdd retries", "Add retries"),
        ("Sure! Add retries", "Sure! Add retries"),  # one line only -> kept as-is
        # ANSI colour from a CLI that thinks it owns a terminal.
        ("\x1b[32mAdd retries\x1b[0m", "Add retries"),
        # Attribution the repo does not want.
        ("Add retries\n\nCo-Authored-By: Someone <a@b.c>", "Add retries"),
        ("Add retries\n\n🤖 Generated with Claude", "Add retries"),
        # Blank-line runs read as a mistake in `git log`.
        ("Add retries\n\n\n\nBody text", "Add retries\n\nBody text"),
        ("", ""),
        ("   \n\n ", ""),
    ],
)
def test_clean_message(raw, expected):
    assert cm.clean_message(raw) == expected


def test_clean_message_keeps_a_preamble_word_inside_the_body():
    """ "Here is" only means chatter on the FIRST line; the same words in a body
    are prose the model chose to write."""
    out = cm.clean_message("Add retries\n\nHere is why: the API is flaky.")
    assert out == "Add retries\n\nHere is why: the API is flaky."


def test_clean_message_caps_length():
    assert len(cm.clean_message("x" * (cm.MESSAGE_MAX + 500))) == cm.MESSAGE_MAX


# --- one-shot argv ----------------------------------------------------------- #


def test_oneshot_command_splices_the_prompt():
    assert pbase.oneshot_command("claude", ("-p", "{prompt}"), "hello") == [
        "claude",
        "-p",
        "hello",
    ]


def test_oneshot_command_drops_an_interactive_subcommand():
    """goose's base command is `goose session`, but its one-shot is `goose run` —
    the template hangs off the executable, never off the base command."""
    assert pbase.oneshot_command("goose session", ("run", "-t", "{prompt}"), "hi") == [
        "goose",
        "run",
        "-t",
        "hi",
    ]


@pytest.mark.parametrize("args,prompt", [((), "hi"), (("-p", "{prompt}"), "")])
def test_oneshot_command_none_when_unsupported(args, prompt):
    assert pbase.oneshot_command("cli", args, prompt) is None


@pytest.mark.parametrize(
    "name,expected",
    [
        (
            "claude",
            # MCP servers stripped: one of the user's wrote its logfile into the
            # worktree, and the commit that followed staged it.
            [
                "claude",
                "--strict-mcp-config",
                "--mcp-config",
                '{"mcpServers":{}}',
                "-p",
                "P",
            ],
        ),
        (
            "codex",
            ["codex", "exec", "--sandbox", "read-only", "--skip-git-repo-check", "P"],
        ),
        ("antigravity", ["agy", "--print", "P"]),
        ("opencode", ["opencode", "run", "P"]),
        ("goose", ["goose", "run", "-t", "P"]),
        ("cline", ["cline", "--auto-approve", "false", "P"]),
    ],
)
def test_bundled_providers_expose_a_oneshot(name, expected):
    from backend import providers

    assert providers.get(name).oneshot_argv("P") == expected


def test_aider_has_no_oneshot():
    """Its non-interactive mode EDITS and can commit on its own, so it is opted
    out rather than pointed at the user's worktree."""
    from backend import providers

    assert providers.get("aider").oneshot_argv("P") is None


def test_a_plain_program_falls_back_to_the_default_cli():
    """A session running `bash` still deserves a written commit message."""
    argv = cm.pick_argv("P", "bash", fallback_program="claude")
    assert argv[0] == "claude" and argv[-1] == "P"


def test_pick_argv_raises_when_nothing_can_answer():
    with pytest.raises(cm.CommitMessageError) as err:
        cm.pick_argv("P", "aider", fallback_program="aider")
    assert "headless" in str(err.value)


# --- the orchestrator -------------------------------------------------------- #


def _stub_run(monkeypatch, stdout="", returncode=0, capture=None):
    """Stub the CLI turn while letting real git through.

    The module runs both through ``subprocess.run``, so a blanket patch would
    also fake the diff collection — and then "nothing was staged" would be proved
    against a git that never ran.
    """
    real = subprocess.run

    def fake(argv, **kw):
        if list(argv)[:1] == ["git"]:
            return real(argv, **kw)
        if capture is not None:
            capture.append((list(argv), kw))
        return subprocess.CompletedProcess(argv, returncode, stdout.encode(), b"nope")

    monkeypatch.setattr(cm.subprocess, "run", fake)


def test_suggest_returns_the_cleaned_answer(repo, monkeypatch):
    monkeypatch.setattr(
        cm, "collect_diff", lambda *a, **k: ("app.py | 2 +-", "@@ diff @@")
    )
    calls: list = []
    _stub_run(monkeypatch, "```\nUse GREETING from the environment\n```", capture=calls)
    assert cm.suggest(str(repo), program="claude") == (
        "Use GREETING from the environment"
    )
    argv, kw = calls[0]
    assert argv[0] == "claude" and "-p" in argv
    assert "@@ diff @@" in argv[-1]
    # It asks, it does not edit: the CLI runs with stdin closed, in the worktree.
    assert kw["cwd"] == str(repo)
    assert kw["stdin"] is subprocess.DEVNULL


def test_suggest_refuses_a_clean_tree(repo, monkeypatch):
    monkeypatch.setattr(cm, "collect_diff", lambda *a, **k: ("", ""))
    with pytest.raises(cm.CommitMessageError) as err:
        cm.suggest(str(repo), program="claude")
    assert "clean" in str(err.value)


def test_suggest_reports_a_nonzero_exit(repo, monkeypatch):
    monkeypatch.setattr(cm, "collect_diff", lambda *a, **k: ("s", "d"))
    _stub_run(monkeypatch, "", returncode=1)
    with pytest.raises(cm.CommitMessageError) as err:
        cm.suggest(str(repo), program="claude")
    assert "exited 1" in str(err.value)


def test_suggest_reports_an_empty_answer(repo, monkeypatch):
    monkeypatch.setattr(cm, "collect_diff", lambda *a, **k: ("s", "d"))
    _stub_run(monkeypatch, "   \n")
    with pytest.raises(cm.CommitMessageError):
        cm.suggest(str(repo), program="claude")


def test_suggest_reports_a_timeout(repo, monkeypatch):
    monkeypatch.setattr(cm, "collect_diff", lambda *a, **k: ("s", "d"))

    def boom(argv, **kw):
        raise subprocess.TimeoutExpired(argv, 45)

    monkeypatch.setattr(cm.subprocess, "run", boom)
    with pytest.raises(cm.CommitMessageError) as err:
        cm.suggest(str(repo), program="claude", timeout=45)
    assert "did not answer" in str(err.value)


def test_suggest_or_none_swallows_every_failure(repo, monkeypatch):
    """The unattended contract: a subject is never worth failing a commit for."""
    monkeypatch.setattr(cm, "collect_diff", lambda *a, **k: ("s", "d"))
    _stub_run(monkeypatch, "", returncode=1)
    assert cm.suggest_or_none(str(repo), program="claude") is None

    def boom(*a, **k):
        raise RuntimeError("something entirely unexpected")

    monkeypatch.setattr(cm, "collect_diff", boom)
    assert cm.suggest_or_none(str(repo), program="claude") is None


class TestTheSuggestRoute:
    """POST /api/instances/{title}/commit-message/suggest — what the ✨ button
    calls. A failure has to arrive as a SENTENCE with a non-2xx status, because
    the dialog shows it verbatim next to a message box it must not have touched."""

    @pytest.fixture
    def inst(self, repo, monkeypatch):
        from datetime import datetime, timezone

        from backend.session.instance import FromInstanceData
        from backend.session.storage import GitWorktreeData, InstanceData, Status
        from backend.web import server

        t = datetime.now(timezone.utc)
        data = InstanceData(
            title="cm-session",
            path=str(repo),
            branch="b",
            status=Status.Running,
            created_at=t,
            updated_at=t,
            program="claude",
            worktree=GitWorktreeData(
                repo_path=str(repo),
                worktree_path=str(repo),
                session_name="cm-session",
                branch_name="b",
            ),
        )
        i = FromInstanceData(data, attach=False)
        monkeypatch.setitem(server.ENGINE.instances, "cm-session", i)
        return i

    def _post(self, title, payload=None):
        import asyncio
        import json

        from backend.web import server

        resp = asyncio.run(server.instance_suggest_commit_message(title, payload))
        return resp.status_code, json.loads(resp.body)

    def test_it_answers_with_the_written_message(self, inst, monkeypatch):
        from backend.web import server

        seen: dict = {}

        def fake(wt, **kw):
            seen.update(kw, wt=wt)
            return "Read GREETING from the environment"

        monkeypatch.setattr(server._commit_message, "suggest", fake)
        status, body = self._post("cm-session", {"hint": "env stuff"})
        assert status == 200
        assert body["message"] == "Read GREETING from the environment"
        # The session's own CLI is asked first, with the box's contents as context.
        assert seen["program"] == "claude"
        assert seen["hint"] == "env stuff"

    def test_a_failure_is_a_502_with_the_reason(self, inst, monkeypatch):
        from backend.web import server

        def boom(*a, **k):
            raise server._commit_message.CommitMessageError("claude is not installed")

        monkeypatch.setattr(server._commit_message, "suggest", boom)
        status, body = self._post("cm-session")
        assert status == 502
        assert body["error"] == "claude is not installed"

    def test_an_unexpected_error_is_still_not_a_500(self, inst, monkeypatch):
        from backend.web import server

        def boom(*a, **k):
            raise RuntimeError("something entirely unexpected")

        monkeypatch.setattr(server._commit_message, "suggest", boom)
        status, body = self._post("cm-session")
        assert status == 502 and body["error"]

    def test_an_overlong_hint_is_clamped(self, inst, monkeypatch):
        """The hint is user text on its way into a prompt; the box has no limit."""
        from backend.web import server

        seen: dict = {}
        monkeypatch.setattr(
            server._commit_message,
            "suggest",
            lambda wt, **kw: (seen.update(kw), "Subject")[1],
        )
        self._post("cm-session", {"hint": "x" * 5000})
        assert len(seen["hint"]) == 500

    def test_unknown_session_is_404(self, inst):
        status, _ = self._post("nope")
        assert status == 404


def test_suggest_never_commits_or_stages(repo, monkeypatch):
    """It reads. `add -N` records intent-to-add (what the Diff tab already does)
    but nothing is staged for real and HEAD does not move."""
    head_before = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], stdout=subprocess.PIPE
    ).stdout
    _stub_run(monkeypatch, "Subject line")
    cm.suggest(str(repo), program="claude")
    head_after = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], stdout=subprocess.PIPE
    ).stdout
    assert head_before == head_after
    staged = subprocess.run(
        ["git", "-C", str(repo), "diff", "--cached", "--numstat"],
        stdout=subprocess.PIPE,
    ).stdout.decode()
    # intent-to-add shows as an all-zero row, never as staged content
    assert all(row.startswith("0\t0\t") for row in staged.splitlines() if row)
