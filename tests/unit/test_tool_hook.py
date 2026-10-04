"""Tests for the red-zone tool-hook: the pure classification/heuristic units in
:mod:`backend.providers._tool_hook_src`, plus END-TO-END runs of the real
generated ``python3 -c`` hook command via ``sh -c`` with a JSON payload on
stdin — exactly how Claude Code invokes it.

The guard file, tool feed and activity marker are all confined to ``tmp_path``:
conftest redirects ``MINDFLOCK_RED_ZONE_DIR`` / ``MINDFLOCK_TOOL_FEED_DIR`` and
each test sets ``MINDFLOCK_ACTIVITY_MARKER_DIR`` / ``MINDFLOCK_THREAD_MARKER_DIR``
for the subprocess. No tmux, no network.
"""

from __future__ import annotations

import ast
import inspect
import json
import os
import subprocess

import pytest

from backend.config import red_zones as rz
from backend.providers import _tool_hook_src as th
from backend.providers import activity_markers as am


def _git(*args, cwd):
    subprocess.run(
        ["git", "-C", str(cwd), "-c", "user.email=t@t", "-c", "user.name=t", *args],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


# --------------------------------------------------------------------------- #
# module hygiene (the AST contract)
# --------------------------------------------------------------------------- #
def test_source_is_defs_and_constants_only():
    src = inspect.getsource(th)
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            continue
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            continue  # module docstring
        pytest.fail(
            "forbidden top-level %s at line %s" % (type(node).__name__, node.lineno)
        )
    # No `from __future__` and no top-level imports (all imports are lazy so the
    # embedded source stays self-contained and side-effect-free).
    for node in tree.body:
        assert not isinstance(node, (ast.Import, ast.ImportFrom))


def test_source_exec_defines_entry():
    ns = {}
    exec(compile(inspect.getsource(th), "<mf-tool-hook>", "exec"), ns)
    assert callable(ns.get("_mf_tool_hook"))


# --------------------------------------------------------------------------- #
# classification
# --------------------------------------------------------------------------- #
def test_classify_edits_reads_bash_agent():
    assert th._classify("Write", {"file_path": "/a/b"})[0] == "edit"
    assert th._classify("Edit", {"file_path": "/a/b"})[0] == "edit"
    assert th._classify("NotebookEdit", {"notebook_path": "/a.ipynb"})[0] == "edit"
    assert th._classify("Read", {"file_path": "/a"})[0] == "read"
    assert th._classify("Grep", {"path": "/a"})[0] == "read"
    assert th._classify("Bash", {"command": "ls"})[0] == "bash"
    # Agent/Task are exact — TaskCreate/TaskUpdate are NOT agent (todo tools).
    assert th._classify("Agent", {})[0] == "agent"
    assert th._classify("Task", {})[0] == "agent"
    assert th._classify("TaskCreate", {})[0] == "other"
    assert th._classify("TaskUpdate", {})[0] == "other"
    assert th._classify("EnterWorktree", {})[0] == "other"


def test_classify_codex_patch_text():
    ti = {
        "input": "*** Begin Patch\n*** Update File: src/a.py\n+x\n*** Move to: src/b.py\n"
    }
    kind, writes, _r, _e = th._classify("apply_patch", ti)
    assert kind == "edit"
    assert "src/a.py" in writes and "src/b.py" in writes


def test_classify_mcp_write_targets():
    kind, writes, _r, _e = th._classify(
        "mcp__github__create_or_update_file",
        {"path": "cfg/x", "files": [{"path": "cfg/y"}]},
    )
    assert kind == "mcp_write"
    assert "cfg/x" in writes and "cfg/y" in writes
    # A read-only mcp tool is 'other'.
    assert th._classify("mcp__github__get_file_contents", {"path": "a"})[0] == "other"


@pytest.mark.parametrize(
    "tool, ti, target",
    [
        # filesystem server: move_file takes {source, destination}
        (
            "mcp__filesystem__move_file",
            {"source": "a", "destination": "cfg/x"},
            "cfg/x",
        ),
        (
            "mcp__filesystem__move_file",
            {"source": "cfg/x", "destination": "b"},
            "cfg/x",
        ),
        # Serena: relative_path, verbs replace/insert/create
        ("mcp__serena__replace_symbol_body", {"relative_path": "cfg/x"}, "cfg/x"),
        ("mcp__serena__insert_after_symbol", {"relative_path": "cfg/x"}, "cfg/x"),
        ("mcp__serena__replace_content", {"relative_path": "cfg/x"}, "cfg/x"),
        ("mcp__serena__create_text_file", {"relative_path": "cfg/x"}, "cfg/x"),
        # JetBrains: pathInProject
        ("mcp__jetbrains__replace_text_in_file", {"pathInProject": "cfg/x"}, "cfg/x"),
        ("mcp__jetbrains__create_new_file", {"pathInProject": "cfg/x"}, "cfg/x"),
        ("mcp__x__rename_file", {"paths": ["cfg/x", "cfg/y"]}, "cfg/x"),
        ("mcp__x__write_resource", {"uri": "file:///r/cfg/x"}, "/r/cfg/x"),
    ],
)
def test_classify_mcp_code_editing_servers(tool, ti, target):
    # These all edited zoned files with no deny: the verb wasn't listed or the
    # path arrived under a key the classifier never read.
    kind, writes, _r, _e = th._classify(tool, ti)
    assert kind == "mcp_write", tool
    assert target in writes, (tool, writes)


def test_classify_mcp_non_file_uri_is_not_a_target():
    _k, writes, _r, _e = th._classify("mcp__x__update_doc", {"uri": "https://x/y"})
    assert writes == []


def test_classify_codex_argv_keeps_the_script_whole():
    # ["bash","-lc","git push"] must stay ONE -c script, or the push gate (and
    # the write heuristic) sees `bash -lc git` + loose words.
    _k, _w, _r, extra = th._classify("shell", {"command": ["bash", "-lc", "git push"]})
    assert th._mf_is_push_cmd(extra["cmd"]) is True


def test_classify_codex_shell_list_cmd():
    kind, _w, _r, extra = th._classify("shell", {"command": ["bash", "-lc", "echo hi"]})
    assert kind == "bash" and "echo hi" in extra["cmd"]


# --------------------------------------------------------------------------- #
# Bash heuristic (the critic's cases)
# --------------------------------------------------------------------------- #
def test_bash_redirect_hash_not_comment():
    # commenters='' — `#` mid-word is NOT a comment, so the redirect survives.
    w, _r, ok = th._bash_targets("echo a#b > z/x", "/repo")
    assert ok is True
    assert "/repo/z/x" in w


def test_bash_cd_tracks_cwd():
    w, _r, ok = th._bash_targets("cd z && echo hi > f", "/repo")
    assert ok is True
    assert "/repo/z/f" in w


def test_bash_sed_combined_inplace_flag():
    w, _r, ok = th._bash_targets("sed -Ei 's/1/2/' cfg/secret.toml", "/repo")
    assert ok is True
    assert "/repo/cfg/secret.toml" in w


def test_bash_rm_rf_ancestor_dir():
    w, _r, ok = th._bash_targets("rm -rf backend", "/repo")
    assert ok is True
    assert "/repo/backend" in w


def test_bash_git_clean_marks_root():
    w, _r, ok = th._bash_targets("git clean -fdx", "/repo")
    assert ok is True
    assert "" in w  # root sentinel -> ancestor of every zone


def test_bash_heredoc_body_is_data_not_shell():
    # A heredoc BODY is stripped before tokenizing: an apostrophe in prose no
    # longer breaks the parse, and the command line's own redirect is still
    # the write target (so a zoned heredoc write is caught by the real check).
    w, _r, ok = th._bash_targets(
        "cat > cfg/secret.toml <<EOF\nowner = it's me\nEOF", "/repo"
    )
    assert ok is True
    assert w == {"/repo/cfg/secret.toml"}
    # Body lines are never read as commands of their own.
    w2, _r2, ok2 = th._bash_targets(
        "cat > notes.md <<'EOF'\nrm -rf cfg\nIt's fine\nEOF\necho done", "/repo"
    )
    assert ok2 is True and w2 == {"/repo/notes.md"}
    # `<<-` (tab-stripped terminator) and a command after the heredoc.
    w3, _r3, ok3 = th._bash_targets(
        "cat <<-X > a.txt\n\tbody's\n\tX\nrm cfg/k", "/repo"
    )
    assert ok3 is True and w3 == {"/repo/a.txt", "/repo/cfg/k"}
    # A here-STRING (<<<) is not a heredoc; an unterminated marker leaves the
    # text alone (parse failure -> the literal fallback, never fail-open).
    assert th._mf_strip_heredocs("cat <<< 'x'\nrm y") == "cat <<< 'x'\nrm y"
    _w4, _r4, ok4 = th._bash_targets("cat > f <<EOF\nit's\n", "/repo")
    assert ok4 is False


@pytest.mark.parametrize(
    "cmd, target",
    [
        ("ls\nrm cfg/secret.toml", "/repo/cfg/secret.toml"),
        ("git status\nsed -i s/x/y/ cfg/secret.toml", "/repo/cfg/secret.toml"),
        ("cd cfg\necho x > secret.toml", "/repo/cfg/secret.toml"),
        ("ls;\nrm cfg/secret.toml", "/repo/cfg/secret.toml"),
        ("ls &&\nrm cfg/secret.toml", "/repo/cfg/secret.toml"),
        ("rm \\\n cfg/secret.toml", "/repo/cfg/secret.toml"),
        ("(cd cfg && rm secret.toml)\nls", "/repo/cfg/secret.toml"),
        ("FOO=1 rm cfg/secret.toml", "/repo/cfg/secret.toml"),
        ("timeout 5 sed -i s/a/b/ cfg/secret.toml", "/repo/cfg/secret.toml"),
        ("bash -lc 'rm cfg/secret.toml'", "/repo/cfg/secret.toml"),
    ],
)
def test_bash_newline_separates_commands(cmd, target):
    # shlex treats '\n' as whitespace by default, which made a multi-line
    # command ONE simple command run by its first line's program: every write
    # on line 2+ escaped the pre-deny.
    w, _r, ok = th._bash_targets(cmd, "/repo")
    assert ok is True, cmd
    assert target in w, (cmd, w)
    # A line continuation keeps its command whole (not ['rm'] + ['cfg/x']).
    assert th._mf_simple_cmds(th._mf_tokenize("rm \\\n cfg/x")) == [["rm", "cfg/x"]]


def test_is_push_cmd():
    assert th._mf_is_push_cmd("git push origin HEAD") is True
    assert th._mf_is_push_cmd("gh pr create --fill") is True
    assert th._mf_is_push_cmd("gh pr merge 12") is True
    assert th._mf_is_push_cmd("git status") is False
    # Conservative: only a real `git … push` simple command, not `echo git push`.
    assert th._mf_is_push_cmd("echo git push") is False


@pytest.mark.parametrize(
    "cmd",
    [
        "GIT_TERMINAL_PROMPT=0 git push origin HEAD",
        "timeout 120 git push",
        "env git push",
        "env -u X GIT_DIR=.git git push",
        "bash -c 'git push origin HEAD'",
        "bash -lc 'git push'",
        "(git push origin HEAD)",
        "command gh pr merge 1",
        "sudo -u bob git push",
        "nice -n 5 git push",
        "nohup git push &",
        "echo done\ngit push",
        "cd sub\ngh pr create --fill",
        "git -C . push",
        "git -c push.default=current push",
        "gh -R o/r pr create",
        "eval git push",
        "git push # it's done",
    ],
)
def test_is_push_cmd_sees_through_wrappers(cmd):
    # Each of these used to pass the gate (argv[0] was not git/gh, or line 2
    # was swallowed as args of line 1) while committed zone breaches existed.
    assert th._mf_is_push_cmd(cmd) is True


@pytest.mark.parametrize(
    "cmd",
    [
        "git stash push -m x",  # `push` is stash's subcommand, not git's
        "git log --grep push",
        "timeout 5 pytest",
        "env | grep GIT",
        "gh pr view 1",
        "echo 'git push'",
        "command -v git",
    ],
)
def test_is_push_cmd_no_false_positives(cmd):
    assert th._mf_is_push_cmd(cmd) is False


def test_bash_tee_and_cp_dest():
    w, _r, ok = th._bash_targets("echo x | tee cfg/a", "/repo")
    assert ok and "/repo/cfg/a" in w
    w2, _r2, ok2 = th._bash_targets("cp src.txt cfg/dest.txt", "/repo")
    assert ok2 and "/repo/cfg/dest.txt" in w2


# --------------------------------------------------------------------------- #
# end-to-end: the real generated hook command
# --------------------------------------------------------------------------- #
@pytest.fixture
def zoned_repo(tmp_path):
    """A git repo with a ``config.toml`` red zone + a synced guard file. Returns
    ``(repo_path, session_name, env)`` where env points the hook's sidecar dirs
    into tmp."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    _git("remote", "add", "origin", "git@github.com:o/r.git", cwd=repo)
    (repo / "config.toml").write_text("secret=1\n")
    (repo / "app.py").write_text("x = 1\n")
    rid = rz.repo_identity(str(repo))[0]
    rz.add_zone("repo", rid, "config.toml", name="config", label="o/r")
    root = os.path.realpath(str(repo))
    assert rz.sync_guard(root, rid) == "written"
    env = {
        **os.environ,
        "MINDFLOCK_SESSION_NAME": "mindflock_t1",
        "MINDFLOCK_ACTIVITY_MARKER_DIR": str(tmp_path / "markers"),
        "MINDFLOCK_THREAD_MARKER_DIR": str(tmp_path / "threads"),
    }
    return str(repo), "mindflock_t1", env


def _run(payload, ev, env, record_thread=True):
    cmd = am.hook_command("working", record_thread=record_thread, tool_hook=ev)
    cp = subprocess.run(
        ["sh", "-c", cmd],
        input=json.dumps(payload).encode(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    return cp


def _feed(env):
    p = rz.feed_path(env["MINDFLOCK_SESSION_NAME"])
    if not os.path.exists(p):
        return []
    return [json.loads(x) for x in open(p).read().splitlines() if x.strip()]


def _marker_state(env):
    p = os.path.join(env["MINDFLOCK_ACTIVITY_MARKER_DIR"], "mindflock_t1.json")
    if not os.path.exists(p):
        return None
    return json.loads(open(p).read()).get("state")


def test_e2e_deny_zoned_edit(zoned_repo):
    repo, _s, env = zoned_repo
    payload = {
        "session_id": "sid",
        "cwd": repo,
        "tool_name": "Write",
        "tool_input": {"file_path": os.path.join(repo, "config.toml")},
        "tool_use_id": "tu1",
        "transcript_path": "/tmp/x.jsonl",
    }
    cp = _run(payload, "pre", env)
    assert cp.returncode == 0
    out = json.loads(cp.stdout.decode())
    hso = out["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"
    assert "config.toml" in hso["permissionDecisionReason"]
    # The activity marker was STILL written (guard runs, then the marker).
    assert _marker_state(env) == "working"
    # Feed record carries the deny with the shape the server reads.
    recs = _feed(env)
    assert recs and recs[-1]["ev"] == "pre" and recs[-1]["tool"] == "Write"
    deny = recs[-1]["deny"]
    assert (
        deny["path"] == "config.toml" and deny["zone_id"] and deny["name"] == "config"
    )
    assert recs[-1]["tp"] == "/tmp/x.jsonl"


def test_e2e_allow_unzoned_edit(zoned_repo):
    repo, _s, env = zoned_repo
    payload = {
        "session_id": "sid",
        "cwd": repo,
        "tool_name": "Write",
        "tool_input": {"file_path": os.path.join(repo, "app.py")},
        "tool_use_id": "tu2",
    }
    cp = _run(payload, "pre", env)
    assert cp.returncode == 0
    assert cp.stdout.decode().strip() == ""  # no deny -> allow
    assert _marker_state(env) == "working"
    recs = _feed(env)
    assert recs[-1]["kind"] == "edit" and "deny" not in recs[-1]


def test_e2e_protect_settings_file_denied(zoned_repo):
    repo, _s, env = zoned_repo
    payload = {
        "session_id": "sid",
        "cwd": repo,
        "tool_name": "Write",
        "tool_input": {
            "file_path": os.path.join(repo, ".claude", "settings.local.json")
        },
        "tool_use_id": "tu3",
    }
    cp = _run(payload, "pre", env)
    out = json.loads(cp.stdout.decode())
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "guard file" in out["hookSpecificOutput"]["permissionDecisionReason"]


def test_e2e_bash_settings_mention_denied(zoned_repo):
    repo, _s, env = zoned_repo
    payload = {
        "session_id": "sid",
        "cwd": repo,
        "tool_name": "Bash",
        "tool_input": {"command": "printf '{}' > .claude/settings.local.json"},
        "tool_use_id": "tu4",
    }
    cp = _run(payload, "pre", env)
    out = json.loads(cp.stdout.decode())
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_e2e_corrupt_guard_fails_open(zoned_repo):
    repo, _s, env = zoned_repo
    # Corrupt the guard file: a garbled guard means ALLOW (fail-open), marker
    # still written, exit 0, no deny.
    gp = rz.guard_path(os.path.realpath(repo))
    open(gp, "w").write("{ this is not json")
    payload = {
        "session_id": "sid",
        "cwd": repo,
        "tool_name": "Write",
        "tool_input": {"file_path": os.path.join(repo, "config.toml")},
        "tool_use_id": "tu5",
    }
    cp = _run(payload, "pre", env)
    assert cp.returncode == 0
    assert cp.stdout.decode().strip() == ""  # no deny
    assert _marker_state(env) == "working"


def test_e2e_posttooluse_failure_record(zoned_repo):
    repo, _s, env = zoned_repo
    payload = {
        "session_id": "sid",
        "cwd": repo,
        "tool_name": "Bash",
        "tool_input": {"command": "false"},
        "tool_use_id": "tu6",
        "error": "command failed",
        "is_interrupt": False,
    }
    cp = _run(payload, "fail", env)
    assert cp.returncode == 0
    recs = _feed(env)
    assert recs[-1]["ev"] == "fail"
    assert recs[-1]["err"] == "command failed" and recs[-1]["intr"] is False


def test_e2e_bash_stat_diff_breach(zoned_repo):
    repo, _s, env = zoned_repo
    fp = os.path.join(repo, "config.toml")
    # A python write the shlex heuristic does NOT catch -> allowed at pre, but
    # the stat-diff backstop catches the change at post.
    cmd = "python3 -c \"open('config.toml','w').write('x')\""
    pre = {
        "session_id": "sid",
        "cwd": repo,
        "tool_name": "Bash",
        "tool_input": {"command": cmd},
        "tool_use_id": "tu7",
    }
    cp_pre = _run(pre, "pre", env)
    assert cp_pre.stdout.decode().strip() == ""  # not denied by the heuristic
    # Simulate the command actually running (mtime/size change).
    import time

    time.sleep(0.01)
    with open(fp, "w") as f:
        f.write("changed=2\n")
    post = dict(pre)
    cp_post = _run(post, "post", env)
    out = json.loads(cp_post.stdout.decode())
    assert out["decision"] == "block"
    assert "config.toml" in out["reason"]
    recs = _feed(env)
    assert any(
        r.get("breach") and r["breach"][0]["path"] == "config.toml" for r in recs
    )


def test_e2e_push_blocked_when_breaches(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    _git("remote", "add", "origin", "git@github.com:o/r.git", cwd=repo)
    (repo / "config.toml").write_text("s=1\n")
    rid = rz.repo_identity(str(repo))[0]
    rz.add_zone("repo", rid, "config.toml", label="o/r")
    root = os.path.realpath(str(repo))
    # A committed breach is recorded in the guard.
    rz.sync_guard(root, rid, breaches=["config.toml"])
    env = {
        **os.environ,
        "MINDFLOCK_SESSION_NAME": "mindflock_p",
        "MINDFLOCK_ACTIVITY_MARKER_DIR": str(tmp_path / "m"),
        "MINDFLOCK_THREAD_MARKER_DIR": str(tmp_path / "t"),
    }
    payload = {
        "session_id": "sid",
        "cwd": str(repo),
        "tool_name": "Bash",
        "tool_input": {"command": "git push origin HEAD"},
        "tool_use_id": "tu8",
    }
    cp = _run(payload, "pre", env)
    out = json.loads(cp.stdout.decode())
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "pushing is blocked" in out["hookSpecificOutput"]["permissionDecisionReason"]


def test_e2e_reads_never_denied(zoned_repo):
    repo, _s, env = zoned_repo
    payload = {
        "session_id": "sid",
        "cwd": repo,
        "tool_name": "Read",
        "tool_input": {"file_path": os.path.join(repo, "config.toml")},
        "tool_use_id": "tu9",
    }
    cp = _run(payload, "pre", env)
    assert cp.stdout.decode().strip() == ""  # v1: one mode, reads allowed


def test_e2e_no_feed_when_session_unknown(zoned_repo, tmp_path):
    repo, _s, env = zoned_repo
    env = dict(env)
    env.pop("MINDFLOCK_SESSION_NAME", None)
    # Force the tmux fallback to fail (a fake `tmux` that exits non-zero) so the
    # session stays empty, while keeping python3 on PATH.
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    (fakebin / "tmux").write_text("#!/bin/sh\nexit 1\n")
    os.chmod(str(fakebin / "tmux"), 0o755)
    env["PATH"] = str(fakebin) + os.pathsep + env.get("PATH", "")
    payload = {
        "session_id": "sid",
        "cwd": repo,
        "tool_name": "Write",
        "tool_input": {"file_path": os.path.join(repo, "config.toml")},
        "tool_use_id": "tu10",
    }
    cp = _run(payload, "pre", env)
    # The guard still ran (deny printed) even though the feed is skipped.
    out = json.loads(cp.stdout.decode())
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


# --------------------------------------------------------------------------- #
# e2e helpers for the backstop / control-file / push-record cases below
# --------------------------------------------------------------------------- #
def _mk_zoned(tmp_path, files, patterns, commit=False, name="r"):
    """A git repo holding ``files`` ({rel: text}) with ``patterns`` as repo
    zones and a synced guard. Returns ``(repo, env)``."""
    repo = tmp_path / name
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    _git("remote", "add", "origin", "git@github.com:o/%s.git" % name, cwd=repo)
    for rel, text in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    if commit:
        _git("add", "-A", cwd=repo)
        _git("commit", "-q", "-m", "init", cwd=repo)
    rid = rz.repo_identity(str(repo))[0]
    for pat in patterns:
        rz.add_zone("repo", rid, pat, label="o/" + name)
    assert rz.sync_guard(os.path.realpath(str(repo)), rid) == "written"
    env = {
        **os.environ,
        "MINDFLOCK_SESSION_NAME": "mindflock_t1",
        "MINDFLOCK_ACTIVITY_MARKER_DIR": str(tmp_path / "markers"),
        "MINDFLOCK_THREAD_MARKER_DIR": str(tmp_path / "threads"),
    }
    return str(repo), env


def _bash_roundtrip(env, cwd, command, tuid, post_cwd=None):
    """Pre hook -> run ``command`` for real in ``cwd`` -> post hook. Returns
    ``(pre_stdout, post_stdout)``."""
    import time

    payload = {
        "session_id": "sid",
        "cwd": cwd,
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "tool_use_id": tuid,
    }
    pre = _run(payload, "pre", env).stdout.decode().strip()
    if pre:
        return pre, None
    time.sleep(0.02)  # let coarse fs timestamps move past the snapshot
    subprocess.run(
        ["sh", "-c", command],
        cwd=cwd,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    post_payload = dict(payload, cwd=post_cwd or cwd)
    post = _run(post_payload, "post", env).stdout.decode().strip()
    return pre, post


def _breach_records(env):
    return [r for r in _feed(env) if r.get("breach")]


# --------------------------------------------------------------------------- #
# Bash stat-diff backstop: never flag an UNCHANGED zoned file (F12/F20/F23)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "command",
    [
        "touch src/app/brand_new.py",
        "printf 'x\\n' > src/app/new_mod.py",
        # sed -i renames a temp file; the attached suffix is the one spelling
        # both GNU sed and macOS BSD sed accept (BSD needs `-i ''` otherwise).
        "sed -i.bak s/o/p/ src/app/other.py",
        "rm src/app/other.py",
    ],
)
def test_e2e_backstop_sibling_change_is_not_a_breach(tmp_path, command):
    # Zone *.secret covers src/app/keys.secret, so src/app is a guarded DIR.
    # Any add/remove/rename in it moves the dir's mtime; the old backstop then
    # reported EVERY zone-matching child — the untouched keys.secret — told the
    # agent to `git checkout` it (discarding the user's own edits) and fired a
    # breach alert.
    repo, env = _mk_zoned(
        tmp_path,
        {
            "src/app/keys.secret": "k=1\n",  # pragma: allowlist secret
            "src/app/other.py": "o\n",
        },
        ["*.secret"],
    )
    g = json.loads(open(rz.guard_path(os.path.realpath(repo))).read())
    assert "src/app" in g["dirs"]
    pre, post = _bash_roundtrip(env, repo, command, "tu-sib")
    assert pre == "" and post == "", (command, post)
    assert _breach_records(env) == []
    assert (tmp_path / "r" / "src/app/keys.secret").read_text() == "k=1\n"


def test_e2e_backstop_generated_cache_in_zoned_dir_is_not_a_breach(tmp_path):
    # Zone `athena` (a dir). Importing it only writes athena/__pycache__ — the
    # side effect of RUNNING code, not an edit; it must not block or alert.
    repo, env = _mk_zoned(
        tmp_path,
        {"athena/__init__.py": "", "athena/core.py": "X = 1\n"},
        ["athena"],
    )
    pre, post = _bash_roundtrip(env, repo, "python3 -c 'import athena.core'", "tu-pyc")
    assert os.path.isdir(os.path.join(repo, "athena", "__pycache__"))
    assert pre == "" and post == ""
    assert _breach_records(env) == []


def test_e2e_backstop_new_file_in_zoned_dir_names_only_it(tmp_path):
    # Zone config/: a NEW file there IS a breach — but only that file, and the
    # revert advice deletes it (git checkout can't revert an untracked path).
    repo, env = _mk_zoned(
        tmp_path,
        {"config/a.toml": "a=1\n", "config/b.toml": "b=1\n", "app.py": "x\n"},
        ["config/"],
        commit=True,
    )
    pre, post = _bash_roundtrip(
        env, repo, "python3 -c \"open('config/new.toml','w').write('n')\"", "tu-new"
    )
    assert pre == ""
    out = json.loads(post)
    assert out["decision"] == "block"
    assert "config/new.toml" in out["reason"]
    assert "config/a.toml" not in out["reason"]
    assert "config/b.toml" not in out["reason"]
    assert "delete the newly created config/new.toml" in out["reason"]
    assert "git checkout" not in out["reason"]
    (rec,) = _breach_records(env)
    assert rec["breach"] == [
        {
            "path": "config/new.toml",
            "pattern": "config/",
            "kind": "red",
            "new": True,
            "clean_at_pre": True,
        }
    ]


def test_e2e_backstop_modified_zoned_file_gets_git_checkout(tmp_path):
    repo, env = _mk_zoned(
        tmp_path,
        {"config/a.toml": "a=1\n", "config/b.toml": "b=1\n"},
        ["config/"],
        commit=True,
    )
    _pre, post = _bash_roundtrip(
        env, repo, "python3 -c \"open('config/a.toml','a').write('z')\"", "tu-mod"
    )
    out = json.loads(post)
    assert "git checkout -- config/a.toml" in out["reason"]
    assert "config/b.toml" not in out["reason"]


def test_e2e_backstop_follows_its_snapshot_when_post_cwd_moves(tmp_path):
    # payload.cwd follows the command's `cd`; the post must still diff the
    # snapshot its pre took (and clean it up) instead of losing it.
    repo, env = _mk_zoned(tmp_path, {"config.toml": "s=1\n"}, ["config.toml"])
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    _pre, post = _bash_roundtrip(
        env,
        repo,
        "python3 -c \"open('config.toml','w').write('x')\"",
        "tu-cd",
        post_cwd=str(elsewhere),
    )
    assert json.loads(post)["decision"] == "block"
    assert not os.path.exists(th_snap(env, "tu-cd"))


def th_snap(env, tuid):
    return os.path.join(rz.feed_dir(), ".snap", tuid + ".json")


def test_e2e_backstop_covers_a_nested_claude_worktree(tmp_path):
    # EnterWorktree / Agent isolation: <root>/.claude/worktrees/<n> is its own
    # checkout with no guard of its own yet; a write there that the heuristic
    # can't see must still be caught (keyed with the nested prefix).
    repo, env = _mk_zoned(
        tmp_path,
        {"config/d0/f0.py": "x\n", "app.py": "y\n"},
        ["config/"],
        commit=True,
    )
    nested = os.path.join(repo, ".claude", "worktrees", "wt1")
    _git("worktree", "add", "-q", "-b", "wt1", nested, cwd=repo)
    pre, post = _bash_roundtrip(
        env,
        nested,
        "python3 -c \"open('config/d0/f0.py','w').write('z')\"",
        "tu-nested",
    )
    assert pre == ""
    out = json.loads(post)
    assert out["decision"] == "block"
    assert ".claude/worktrees/wt1/config/d0/f0.py" in out["reason"]


# --------------------------------------------------------------------------- #
# protect / sym entries hold for targets OUTSIDE the worktree (F13)
# --------------------------------------------------------------------------- #
def _pre_write(env, repo, tool, ti, tuid):
    payload = {
        "session_id": "sid",
        "cwd": repo,
        "tool_name": tool,
        "tool_input": ti,
        "tool_use_id": tuid,
    }
    out = _run(payload, "pre", env).stdout.decode().strip()
    return json.loads(out)["hookSpecificOutput"] if out else None


def test_e2e_control_files_outside_the_worktree_are_protected(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))  # sync_guard's ~ -> tmp
    repo, env = _mk_zoned(tmp_path, {"config.toml": "s=1\n"}, ["config.toml"])
    gp = rz.guard_path(os.path.realpath(repo))
    cases = [
        ("Write", {"file_path": gp, "content": "{}"}),
        ("Edit", {"file_path": rz.store_path(), "old_string": "a", "new_string": "b"}),
        ("Write", {"file_path": str(home / ".claude" / "settings.json")}),
        ("Write", {"file_path": os.path.join(rz.feed_dir(), "mindflock_t1.jsonl")}),
        ("NotebookEdit", {"notebook_path": gp}),
        ("mcp__filesystem__write_file", {"path": gp, "content": "{}"}),
    ]
    for i, (tool, ti) in enumerate(cases):
        hso = _pre_write(env, repo, tool, ti, "tu-ctl%d" % i)
        assert hso and hso["permissionDecision"] == "deny", (tool, ti)
        assert "guard file" in hso["permissionDecisionReason"]
    # A write outside the worktree that is NOT a control file stays allowed.
    assert (
        _pre_write(env, repo, "Write", {"file_path": str(tmp_path / "x")}, "t") is None
    )
    # …and the guard file itself was never touched (nothing ran).
    assert json.loads(open(gp).read())["rules"]


def test_e2e_symlink_target_outside_the_worktree_is_zoned(tmp_path):
    shared = tmp_path / "shared" / "config.toml"
    shared.parent.mkdir()
    shared.write_text("k=1\n")
    repo = tmp_path / "r"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    _git("remote", "add", "origin", "git@github.com:o/r.git", cwd=repo)
    os.symlink(str(shared), str(repo / "config.toml"))
    rid = rz.repo_identity(str(repo))[0]
    rz.add_zone("repo", rid, "config.toml", label="o/r")
    rz.sync_guard(os.path.realpath(str(repo)), rid)
    env = {
        **os.environ,
        "MINDFLOCK_SESSION_NAME": "mindflock_t1",
        "MINDFLOCK_ACTIVITY_MARKER_DIR": str(tmp_path / "markers"),
        "MINDFLOCK_THREAD_MARKER_DIR": str(tmp_path / "threads"),
    }
    # Editing the link's real TARGET directly (outside the repo) is denied.
    hso = _pre_write(env, str(repo), "Edit", {"file_path": str(shared)}, "tu-sym")
    assert hso and hso["permissionDecision"] == "deny"


# --------------------------------------------------------------------------- #
# parse-failure fallback matches ZONE literals only, as path tokens (F15)
# --------------------------------------------------------------------------- #
def test_e2e_unparseable_command_not_denied_for_a_zones_parent_dir(tmp_path):
    repo, env = _mk_zoned(
        tmp_path,
        {
            "src/app/keys.secret": "k\n",  # pragma: allowlist secret
            "src/app/main.py": "m\n",
            "README.md": "r\n",
        },
        ["*.secret"],
    )

    def pre(cmd, tuid):
        return _pre_write(env, repo, "Bash", {"command": cmd}, tuid)

    # Heredoc bodies with apostrophes mentioning `src` (a PARENT of a zoned
    # file, not a zone) — these were denied as "the red zone 'src'".
    assert pre("cat > README.md <<'EOF'\nDon't forget the src tree\nEOF", "a") is None
    assert pre("cat > src/app/NOTES.md <<'EOF'\nIt's a note\nEOF", "b") is None
    # A heredoc INTO the zoned file is still denied (now by the real check).
    hso = pre("cat > src/app/keys.secret <<'EOF'\nIt's\nEOF", "c")
    assert hso and hso["permissionDecision"] == "deny"


def test_zone_literal_is_a_path_token_not_a_substring():
    g = {
        "files": ["db/migrations/0001.sql"],
        "rules": [{"pattern": "db/migrations/**"}],
    }
    # "db" inside "feedback" is not the zone; "db/migrations" as a token is.
    assert th._mf_zone_literal_in("echo it's feedback > /tmp/x", g) is None
    assert th._mf_zone_literal_in("echo it's > db/migrations/x", g) == "db/migrations"
    # Parent dirs of zoned files are never literals; a glob-first rule has none.
    g2 = {
        "files": ["src/app/keys.secret"],
        "dirs": ["src"],
        "rules": [{"pattern": "*.secret"}],
    }
    assert th._mf_zone_literal_in("it's the src tree", g2) is None
    assert (
        th._mf_zone_literal_in("it's src/app/keys.secret", g2) == "src/app/keys.secret"
    )


def test_e2e_multiline_bash_write_is_denied(zoned_repo):
    repo, _s, env = zoned_repo
    for i, cmd in enumerate(
        [
            "ls\nrm config.toml",
            "git status\nsed -i s/1/2/ config.toml",
            "ls;\nrm config.toml",
        ]
    ):
        hso = _pre_write(env, repo, "Bash", {"command": cmd}, "tu-nl%d" % i)
        assert hso and hso["permissionDecision"] == "deny", cmd


# --------------------------------------------------------------------------- #
# feed lines are always valid JSON (F29) + push denies are recorded (F31)
# --------------------------------------------------------------------------- #
def test_fit_line_never_emits_invalid_json():
    breach = [
        {"path": "config/env/svc_%04d.yaml" % i, "pattern": "config/**"}
        for i in range(300)
    ]
    rec = {
        "v": 1,
        "ts": 1.0,
        "ev": "post",
        "tool": "Bash",
        "kind": "bash",
        "id": "t1",
        "cmd": "x" * 300,
        "writes": ["/r/w%05d" % i for i in range(2000)],
        "reads": ["/r/r%05d" % i for i in range(2000)],
        "breach": breach,
    }
    line = th._mf_fit_line(rec)
    assert len(line) <= th._MF_FEED_MAX
    out = json.loads(line)  # parses — the old slice cut mid-string
    assert out["id"] == "t1" and out["ev"] == "post"
    assert out["breach"][0]["path"] == "config/env/svc_0000.yaml"
    assert out.get("breach_total") == 300
    # Even a pathological record fits and parses, keeping id + deny.
    huge = {
        "v": 1,
        "ts": 1.0,
        "ev": "pre",
        "tool": "T" * 40000,
        "kind": "edit",
        "id": "t2",
        "deny": {"path": "p" * 40000, "reason": "r" * 40000},
    }
    out2 = json.loads(th._mf_fit_line(huge))
    assert out2["id"] == "t2" and out2["deny"]["path"].startswith("p")
    assert len(th._mf_fit_line(huge)) <= th._MF_FEED_MAX


def test_e2e_mass_breach_feed_record_survives(tmp_path):
    files = {"config/env/svc_%04d.yaml" % i: "k: v\n" for i in range(300)}
    repo, env = _mk_zoned(tmp_path, files, ["config/**"], commit=True)
    _pre, post = _bash_roundtrip(
        env, repo, "python3 -c \"import shutil; shutil.rmtree('config')\"", "tu-mass"
    )
    assert json.loads(post)["decision"] == "block"
    p = rz.feed_path("mindflock_t1")
    lines = open(p).read().splitlines()
    recs = [json.loads(x) for x in lines]  # every line parses
    (rec,) = [r for r in recs if r.get("breach")]
    assert rec["id"] == "tu-mass" and rec["ev"] == "post"
    assert rec["breach_total"] >= 300


def test_e2e_push_deny_carries_a_feed_deny_record(tmp_path):
    repo, env = _mk_zoned(tmp_path, {"config.toml": "s=1\n"}, ["config.toml"])
    root = os.path.realpath(repo)
    rz.sync_guard(root, rz.repo_identity(repo)[0], breaches=["config.toml"])
    for tuid, tool, ti in (
        ("tp1", "Bash", {"command": "git push origin feature"}),
        ("tp2", "mcp__github__push_files", {"branch": "feature"}),
        ("tp3", "Bash", {"command": "GIT_TERMINAL_PROMPT=0 git push"}),
    ):
        hso = _pre_write(env, repo, tool, ti, tuid)
        assert hso and "pushing is blocked" in hso["permissionDecisionReason"]
        rec = [r for r in _feed(env) if r.get("id") == tuid][-1]
        # A denied tool fires no Post*: without `deny` the Map shows the push
        # as "running" for 30 minutes and the monitor never counts the block.
        assert rec["deny"]["push"] is True
        assert rec["deny"]["path"] == "config.toml"
        assert rec["deny"]["zone_id"] is None
        assert "pushing is blocked" in rec["deny"]["reason"]


def test_e2e_mcp_move_file_into_a_zone_is_denied(zoned_repo):
    repo, _s, env = zoned_repo
    hso = _pre_write(
        env,
        repo,
        "mcp__filesystem__move_file",
        {
            "source": os.path.join(repo, "app.py"),
            "destination": os.path.join(repo, "config.toml"),
        },
        "tu-mv",
    )
    assert hso and hso["permissionDecision"] == "deny"


# --------------------------------------------------------------------------- #
# the INSTALLED hooks: install via the real provider, run what it wrote (F49)
# --------------------------------------------------------------------------- #
def test_e2e_installed_claude_hooks_deny_only_in_pre(zoned_repo):
    # Every other e2e test builds its command with hook_command(tool_hook=ev)
    # directly, skipping the install's event->phase mapping: PreToolUse mapped
    # to "post" would leave the guard detect-only with the suite green.
    from backend.providers import claude

    repo, _s, env = zoned_repo
    claude.install_activity_hooks(repo, "mindflock_t1")
    data = json.loads(open(os.path.join(repo, ".claude", "settings.local.json")).read())
    payload = {
        "session_id": "sid",
        "cwd": repo,
        "tool_name": "Write",
        "tool_input": {"file_path": os.path.join(repo, "config.toml")},
        "tool_use_id": "tu-inst",
    }
    outs = {}
    for event in ("PreToolUse", "PostToolUse", "PostToolUseFailure"):
        (cmd,) = [
            h["command"]
            for e in data["hooks"][event]
            for h in e["hooks"]
            if am.TOOL_HOOK_TAG in h["command"]
        ]
        cp = subprocess.run(
            ["sh", "-c", cmd],
            input=json.dumps(payload).encode(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        assert cp.returncode == 0, event
        outs[event] = cp.stdout.decode().strip()
    hso = json.loads(outs["PreToolUse"])["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"
    assert "config.toml" in hso["permissionDecisionReason"]
    assert outs["PostToolUse"] == "" and outs["PostToolUseFailure"] == ""
    assert am.hooks_armed(os.path.join(repo, ".claude", "settings.local.json"))


# --------------------------------------------------------------------------- #
# v3: green zones + guard hardening. Every case runs the REAL generated hook
# command under `sh -c` (critic findings C1–C6, H1–H5, M1–M8 are named).
# --------------------------------------------------------------------------- #
_CASES = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "fixtures", "zone_classify_cases.json"
)


def _rules(entries):
    return [
        e if isinstance(e, dict) else {"pattern": e, "re": rz.compile_pattern(e)}
        for e in entries
    ]


def test_hook_classify_mirrors_the_shared_cases():
    """H1: the hook's `_mf_classify` answers every shared case exactly like
    `red_zones.classify` (and the frontend's classifyPath)."""
    with open(_CASES, encoding="utf-8") as f:
        cases = json.load(f)
    assert len(cases) >= 30
    for c in cases:
        g = {
            "rules": _rules(c["zones"]["red"]),
            "green_rules": _rules(c["zones"]["green"]),
            "companions": _rules(c["zones"]["companions"]),
            "ci": c["ci"],
        }
        assert th._mf_classify(c["path"], None, g) == c["expect"], c["name"]
        assert rz.classify(c["path"], None, c["zones"], c["ci"]) == c["expect"]


def _mk_green(tmp_path, files, green, red=(), commit=True, name="g"):
    """A committed repo with worktree GREEN zones (+ optional repo red zones)
    and a synced guard. Returns ``(repo, env)``."""
    repo = tmp_path / name
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    _git("remote", "add", "origin", "git@github.com:o/%s.git" % name, cwd=repo)
    for rel, text in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    if commit:
        _git("add", "-A", cwd=repo)
        _git("commit", "-q", "-m", "init", cwd=repo)
    root = os.path.realpath(str(repo))
    rid = rz.repo_identity(root)[0]
    for pat in red:
        rz.add_zone("repo", rid, pat, label="o/" + name)
    for pat in green:
        rz.add_zone("worktree", root, pat, repo_id=rid, kind="green")
    assert rz.sync_guard(root, rid) == "written"
    env = {
        **os.environ,
        "MINDFLOCK_SESSION_NAME": "mindflock_t1",
        "MINDFLOCK_ACTIVITY_MARKER_DIR": str(tmp_path / "markers"),
        "MINDFLOCK_THREAD_MARKER_DIR": str(tmp_path / "threads"),
    }
    return str(repo), env


_GFILES = {
    "src/green/a.py": "a\n",
    "src/other/b.py": "b\n",
    "src/other/c.py": "c\n",
    "secret/s.py": "s\n",
}


def _bash_pre(env, cwd, command, tuid):
    payload = {
        "session_id": "sid",
        "cwd": cwd,
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "tool_use_id": tuid,
    }
    out = _run(payload, "pre", env).stdout.decode().strip()
    return json.loads(out)["hookSpecificOutput"] if out else None


def test_e2e_green_guard_keeps_green_out_of_the_red_rules(tmp_path):
    """C1: the guard a v1 hook would read has NO green in `rules`, so an old
    hook can never deny inside the scope; the current hook enforces it."""
    repo, env = _mk_green(tmp_path, _GFILES, ["src/green"])
    g = json.load(open(rz.guard_path(os.path.realpath(repo))))
    assert g["rules"] == [] and g["v"] == 2
    # the v1 reading of the guard (rules only) finds nothing to deny
    assert th._mf_match_rel("src/green/a.py", g["rules"], False) is None
    inside = _pre_write(
        env, repo, "Write", {"file_path": os.path.join(repo, "src/green/a.py")}, "t1"
    )
    assert inside is None
    outside = _pre_write(
        env, repo, "Write", {"file_path": os.path.join(repo, "src/other/b.py")}, "t2"
    )
    assert outside["permissionDecision"] == "deny"
    assert outside["permissionDecisionReason"].startswith(
        "MindFlock scope: src/other/b.py is outside the green zone(s)"
    )
    rec = [r for r in _feed(env) if r.get("deny")][-1]["deny"]
    assert rec["kind"] == "green" and rec["request"] is True
    assert rec["pattern"] == "outside green" and rec["path"] == "src/other/b.py"


def test_e2e_green_only_guard_protects_the_control_files(tmp_path, monkeypatch):
    """C2: every enforcement branch is gated on `rules or green_rules` — a
    green-only guard used to leave the guard dir, the store and the settings
    file open."""
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    repo, env = _mk_green(tmp_path, _GFILES, ["src/green"])
    env["HOME"] = str(home)
    for i, cmd in enumerate(
        [
            "rm -rf %s" % rz.guard_dir(),
            "echo '{\"disableAllHooks\":true}' > ~/.claude/settings.json",
            "cp /dev/null %s" % rz.store_path(),
        ]
    ):
        hso = _bash_pre(env, repo, cmd, "tc%d" % i)
        assert hso and hso["permissionDecision"] == "deny", cmd
    hso = _pre_write(
        env,
        repo,
        "Edit",
        {"file_path": rz.store_path(), "old_string": "a", "new_string": "b"},
        "tc9",
    )
    assert hso and "guard file" in hso["permissionDecisionReason"]


def test_e2e_red_revert_of_a_flagged_clean_file_is_allowed(tmp_path):
    """C3: the backstop tells the agent to `git checkout` a file — the pre
    hook must then LET it (it used to deny the very command it suggested)."""
    repo, env = _mk_zoned(
        tmp_path, {"secret/s.py": "s\n", "secret/t.py": "t\n"}, ["secret"], commit=True
    )
    _pre, post = _bash_roundtrip(
        env, repo, "python3 -c \"open('secret/s.py','a').write('z')\"", "tu-a"
    )
    out = json.loads(post)
    assert "git checkout -- secret/s.py" in out["reason"]
    (rec,) = _breach_records(env)
    assert rec["breach"][0]["clean_at_pre"] is True
    assert _bash_pre(env, repo, "git checkout -- secret/s.py", "tu-b") is None
    assert _bash_pre(env, repo, "git restore secret/s.py", "tu-c") is None
    # Not flagged → still denied; a non-revert write to it → still denied.
    assert _bash_pre(env, repo, "git checkout -- secret/t.py", "tu-d")
    assert _bash_pre(env, repo, "echo x > secret/s.py", "tu-e")
    # A file the command CREATED may be removed with rm (not checked out).
    _pre, post = _bash_roundtrip(
        env, repo, "python3 -c \"open('secret/new.txt','w').write('n')\"", "tu-f"
    )
    assert "delete the newly created secret/new.txt" in json.loads(post)["reason"]
    assert _bash_pre(env, repo, "rm secret/new.txt", "tu-g") is None
    assert _bash_pre(env, repo, "rm secret/t.py", "tu-h")


def test_e2e_red_revert_is_not_offered_for_work_in_progress(tmp_path):
    """C4: a zoned file that ALREADY had uncommitted changes (the human's
    WIP) is never told to `git checkout` — that would throw the WIP away —
    and the revert allowance refuses it."""
    repo, env = _mk_zoned(tmp_path, {"secret/s.py": "s\n"}, ["secret"], commit=True)
    with open(os.path.join(repo, "secret/s.py"), "a") as f:
        f.write("human wip\n")
    _pre, post = _bash_roundtrip(
        env, repo, "python3 -c \"open('secret/s.py','a').write('z')\"", "tu-w"
    )
    reason = json.loads(post)["reason"]
    assert "git checkout" not in reason
    assert "already had uncommitted changes" in reason and "don't" in reason
    assert _bash_pre(env, repo, "git checkout -- secret/s.py", "tu-x")


def test_e2e_green_backstop_flags_tracked_changes_outside_non_destructively(
    tmp_path,
):
    """H3/C4: a Bash write outside the scope that the heuristic can't see is
    caught by the git-status diff; the feedback says stop-and-tell, and only
    offers `git checkout` for a path clean before the command."""
    repo, env = _mk_green(tmp_path, _GFILES, ["src/green"])
    with open(os.path.join(repo, "src/other/c.py"), "a") as f:
        f.write("wip\n")  # dirty before, untouched by the command
    pre, post = _bash_roundtrip(
        env, repo, "python3 -c \"open('src/other/b.py','a').write('z')\"", "tu-g1"
    )
    assert pre == ""
    out = json.loads(post)
    assert out["decision"] == "block"
    r = out["reason"]
    assert "outside your green zone(s) changed while your command ran" in r
    assert "src/other/b.py" in r and "src/other/c.py" not in r
    assert "don't revert files you didn't intend to change" in r
    assert "git checkout -- src/other/b.py" in r
    (rec,) = _breach_records(env)
    assert rec["breach"] == [
        {
            "path": "src/other/b.py",
            "pattern": "outside green",
            "kind": "green",
            "clean_at_pre": True,
        }
    ]
    # Changing the WIP file: a breach, but never a checkout suggestion.
    _pre, post = _bash_roundtrip(
        env, repo, "python3 -c \"open('src/other/c.py','a').write('y')\"", "tu-g2"
    )
    r = json.loads(post)["reason"]
    assert "src/other/c.py" in r and "git checkout" not in r
    # Inside the scope: nothing.
    _pre, post = _bash_roundtrip(
        env, repo, "python3 -c \"open('src/green/a.py','a').write('q')\"", "tu-g3"
    )
    assert post == ""


def test_e2e_green_backstop_parses_a_staged_rename(tmp_path):
    """H3: `R  new\\0orig\\0` — the origin is its own NUL field and counts as
    a write (a delete) outside the scope."""
    repo, env = _mk_green(tmp_path, _GFILES, ["src/green"])
    cmd = (
        "python3 -c \"import subprocess;subprocess.run(['git','mv',"
        "'src/other/c.py','src/green/c.py'],check=True)\""
    )
    _pre, post = _bash_roundtrip(env, repo, cmd, "tu-mv")
    (rec,) = _breach_records(env)
    assert [b["path"] for b in rec["breach"]] == ["src/other/c.py"]
    assert "src/green/c.py" not in json.loads(post)["reason"]


def test_e2e_green_backstop_takes_no_index_lock(tmp_path):
    """H3: plain `git status` refreshes .git/index under index.lock (and
    collides with a concurrent commit); the backstop passes
    --no-optional-locks, so the index is untouched."""
    repo, env = _mk_green(tmp_path, _GFILES, ["src/green"])
    index = os.path.join(repo, ".git", "index")
    past = os.stat(index).st_mtime - 10
    os.utime(os.path.join(repo, "src/other/b.py"), (past, past))  # stale stat
    before = os.stat(index).st_mtime_ns
    _bash_roundtrip(env, repo, "true", "tu-lock")
    assert os.stat(index).st_mtime_ns == before


def test_e2e_green_backstop_new_untracked_file_is_an_artifact_not_a_breach(
    tmp_path,
):
    repo, env = _mk_green(tmp_path, _GFILES, ["src/green"])
    pre, post = _bash_roundtrip(
        env, repo, "python3 -c \"open('notes.txt','w').write('n')\"", "tu-art"
    )
    assert post == ""
    rec = [r for r in _feed(env) if r.get("ev") == "post"][-1]
    assert rec.get("artifact") == ["notes.txt"] and "breach" not in rec


def test_e2e_green_backstop_ignores_nested_sandboxes_and_embedded_repos(tmp_path):
    """H3: a new `.claude/worktrees/<n>` sandbox shows as `?? .claude/…/`
    in the root status — never a breach or an artifact; nor is a nested
    repo's inside."""
    repo, env = _mk_green(tmp_path, _GFILES, ["src/green"])
    cmd = (
        "python3 -c \"import subprocess;subprocess.run(['git','worktree','add','-q',"
        "'-b','w1','.claude/worktrees/w1'],check=True);"
        "subprocess.run(['git','init','-q','vendored'],check=True)\""
    )
    _pre, post = _bash_roundtrip(env, repo, cmd, "tu-nest")
    assert post == ""
    rec = [r for r in _feed(env) if r.get("ev") == "post"][-1]
    assert "breach" not in rec and "artifact" not in rec


def test_e2e_green_backstop_skips_post_without_a_pre_baseline(tmp_path):
    """H3: a pre `git status` that fails/times out stores "no baseline" and
    the post skips the green diff (fail open — the monitor and the push
    gate still see the change)."""
    repo, env = _mk_green(tmp_path, _GFILES, ["src/green"])
    fake = tmp_path / "fakebin"
    fake.mkdir()
    (fake / "git").write_text("#!/bin/sh\nexit 1\n")
    (fake / "git").chmod(0o755)
    payload = {
        "session_id": "sid",
        "cwd": repo,
        "tool_name": "Bash",
        "tool_input": {"command": "python3 x.py"},
        "tool_use_id": "tu-nb",
    }
    bad_env = dict(env, PATH=str(fake) + os.pathsep + env.get("PATH", ""))
    assert _run(payload, "pre", bad_env).stdout.decode().strip() == ""
    snap = json.load(open(th_snap(env, "tu-nb")))
    assert snap["dirty"] is None
    with open(os.path.join(repo, "src/other/b.py"), "a") as f:
        f.write("z")
    assert _run(payload, "post", env).stdout.decode().strip() == ""


def test_e2e_green_symlink_into_the_scope_cannot_write_outside(tmp_path):
    """H2: `ln -s ../other/b.py src/green/b.py` is allowed (it creates a path
    inside the scope), but editing through it is judged on the realpath."""
    repo, env = _mk_green(tmp_path, _GFILES, ["src/green"])
    assert _bash_pre(env, repo, "ln -s ../other/b.py src/green/b.py", "tl1") is None
    os.symlink("../other/b.py", os.path.join(repo, "src/green/b.py"))
    hso = _pre_write(
        env, repo, "Edit", {"file_path": os.path.join(repo, "src/green/b.py")}, "tl2"
    )
    assert hso and hso["permissionDecision"] == "deny"
    assert "src/other/b.py is outside" in hso["permissionDecisionReason"]


def test_e2e_green_allows_companions_artifacts_and_reads(tmp_path):
    """H4 (companions: lockfiles, tests importing the scope), C6 (the
    MindFlock workspace artifacts a Verify run writes) and H5 (reads are
    never denied, only recorded)."""
    files = dict(_GFILES)
    files["src/__init__.py"] = ""
    files["src/green/__init__.py"] = ""
    files["tests/test_a.py"] = "from src.green import a\n"
    files["tests/test_b.py"] = "from src.other import b\n"
    repo, env = _mk_green(tmp_path, files, ["src/green"])
    for i, rel in enumerate(
        [
            "uv.lock",
            "web/package-lock.json",
            ".mindflock_verify.json",
            "tests/test_a.py",
        ]
    ):
        hso = _pre_write(
            env, repo, "Write", {"file_path": os.path.join(repo, rel)}, "tcomp%d" % i
        )
        assert hso is None, rel
    assert _pre_write(
        env, repo, "Write", {"file_path": os.path.join(repo, "tests/test_b.py")}, "tx"
    )
    payload = {
        "session_id": "sid",
        "cwd": repo,
        "tool_name": "Read",
        "tool_input": {"file_path": os.path.join(repo, "src/other/b.py")},
        "tool_use_id": "tread",
    }
    assert _run(payload, "pre", env).stdout.decode().strip() == ""
    rec = [r for r in _feed(env) if r.get("id") == "tread"][-1]
    assert rec["reads"] == [os.path.join(repo, "src/other/b.py")]


def test_e2e_green_bash_heuristic(tmp_path):
    """M4: high-confidence writes outside the scope are denied; mkdir,
    writes outside the ROOT (M7: not governed) and unparseable commands are
    not."""
    repo, env = _mk_green(tmp_path, _GFILES, ["src/green"])
    assert _bash_pre(env, repo, "echo x > src/other/new.py", "h1")
    assert _bash_pre(env, repo, "sed -i s/b/c/ src/other/b.py", "h2")
    assert _bash_pre(env, repo, "git checkout -- src/other/b.py", "h3")
    assert _bash_pre(env, repo, "echo x > src/green/new.py", "h4") is None
    assert _bash_pre(env, repo, "mkdir -p build/out", "h5") is None
    outside = str(tmp_path / "scratch.txt")
    assert _bash_pre(env, repo, "echo x > %s" % outside, "h6") is None
    assert _bash_pre(env, repo, "echo 'unterminated > src/other/b.py", "h7") is None
    assert _bash_pre(env, repo, "cat src/other/b.py > /dev/null", "h8") is None
    hso = _pre_write(env, repo, "Write", {"file_path": outside}, "h9")
    assert hso is None


def test_e2e_green_mcp_uses_only_explicit_write_verbs(tmp_path):
    """M8: an MCP tool that UPLOADS a local file reads it — under green only
    write/create/update/edit/delete/move count as writes."""
    repo, env = _mk_green(tmp_path, _GFILES, ["src/green"])
    target = os.path.join(repo, "src/other/b.py")
    assert _pre_write(env, repo, "mcp__x__upload_file", {"path": target}, "m1") is None
    hso = _pre_write(env, repo, "mcp__filesystem__write_file", {"path": target}, "m2")
    assert hso and hso["permissionDecision"] == "deny"


def test_e2e_red_wins_with_its_own_reason_and_green_reason_caps_names(tmp_path):
    """M6: red wins with the more specific red reason; M5: the green reason
    names at most five zones."""
    greens = ["src/green", "/g1", "/g2", "/g3", "/g4", "/g5", "/g6"]
    repo, env = _mk_green(
        tmp_path, _GFILES, greens, red=["src/green/keys.py"], name="rw"
    )
    hso = _pre_write(
        env,
        repo,
        "Write",
        {"file_path": os.path.join(repo, "src/green/keys.py")},
        "r1",
    )
    assert hso["permissionDecisionReason"].startswith("MindFlock red zone:")
    hso = _pre_write(
        env, repo, "Write", {"file_path": os.path.join(repo, "src/other/b.py")}, "r2"
    )
    assert "(+2 more)" in hso["permissionDecisionReason"]


def test_e2e_green_revert_of_its_own_flagged_change_is_allowed(tmp_path):
    """C3 for green: after a flagged clean-at-pre change outside the scope,
    the agent may restore exactly that file."""
    repo, env = _mk_green(tmp_path, _GFILES, ["src/green"])
    _bash_roundtrip(
        env, repo, "python3 -c \"open('src/other/b.py','a').write('z')\"", "tu-r1"
    )
    assert _bash_pre(env, repo, "git checkout -- src/other/b.py", "tu-r2") is None
    assert _bash_pre(env, repo, "git checkout -- src/other/c.py", "tu-r3")


def test_e2e_green_backstop_submodule_and_untracked_tree(tmp_path):
    """H3: a submodule is ONE status path — a scope inside it doesn't turn
    every edit there into a breach of the pointer (but a scope elsewhere
    does flag it); an untracked tree is one `-unormal` entry, not thousands."""
    sub = tmp_path / "subsrc"
    sub.mkdir()
    subprocess.run(["git", "init", "-q", str(sub)], check=True)
    (sub / "src").mkdir()
    (sub / "src" / "x.c").write_text("x\n")
    _git("add", "-A", cwd=sub)
    _git("commit", "-q", "-m", "s", cwd=sub)

    def _repo(name, green):
        repo, env = _mk_green(tmp_path, _GFILES, [], name=name)
        _git(
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            "-q",
            str(sub),
            "vendor/lib",
            cwd=repo,
        )
        _git("commit", "-q", "-m", "sub", cwd=repo)
        root = os.path.realpath(repo)
        rid = rz.repo_identity(root)[0]
        for pat in green:
            rz.add_zone("worktree", root, pat, repo_id=rid, kind="green")
        rz.sync_guard(root, rid)
        return repo, env

    cmd = "python3 -c \"open('vendor/lib/src/x.c','a').write('y')\""
    repo, env = _repo("sm1", ["/vendor/lib/src"])
    _pre, post = _bash_roundtrip(env, repo, cmd, "tu-sm1")
    assert post == ""
    repo, env = _repo("sm2", ["src/green"])
    _pre, post = _bash_roundtrip(env, repo, cmd, "tu-sm2")
    assert "vendor/lib" in json.loads(post)["reason"]
    many = "python3 -c \"import os;os.makedirs('big');[open('big/f%d'%i,'w') for i in range(300)]\""
    _pre, post = _bash_roundtrip(env, repo, many, "tu-big")
    rec = [r for r in _feed(env) if r.get("id") == "tu-big" and r["ev"] == "post"][-1]
    assert rec["artifact"] == ["big/"]


def test_hook_command_stays_under_the_single_argument_limit():
    """The whole guard source is ONE argv string (`python3 -c <src>` inside
    `sh -c <cmd>`); Linux caps a single argument at 128 KiB (MAX_ARG_STRLEN)
    and exec fails outright past it — which would silently disarm every
    guard. Keep headroom."""
    cmd = am.hook_command("working", tool_hook="pre")
    assert len(cmd.encode("utf-8")) < 110 * 1024


# --------------------------------------------------------------------------- #
# v3 review fixes
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("tool", ["Edit", "Write", "mcp__filesystem__write_file"])
def test_e2e_write_through_an_outside_symlink_is_judged_by_its_target(tmp_path, tool):
    """A symlink OUTSIDE every worktree pointing inside one has no lexical
    guard; the write still lands in the root, so the guard of its realpath
    judges it — red and green, file and directory links."""
    files = dict(_GFILES)
    repo, env = _mk_green(tmp_path, files, ["src/green"], red=["secret/**"])
    out = tmp_path / "outside"
    out.mkdir()
    os.symlink(os.path.join(repo, "src/other/b.py"), str(out / "b.py"))
    os.symlink(os.path.join(repo, "secret/s.py"), str(out / "s.py"))
    os.symlink(os.path.join(repo, "src/other"), str(out / "otherdir"))
    os.symlink(os.path.join(repo, "src/green/a.py"), str(out / "a.py"))
    key = "path" if tool.startswith("mcp__") else "file_path"
    for i, (lnk, want) in enumerate(
        [
            ("b.py", "outside the green zone"),
            ("otherdir/c.py", "outside the green zone"),
            ("s.py", "MindFlock red zone"),
        ]
    ):
        hso = _pre_write(env, repo, tool, {key: str(out / lnk)}, "ol%d" % i)
        assert hso and hso["permissionDecision"] == "deny", lnk
        assert want in hso["permissionDecisionReason"], lnk
    # A link to a file INSIDE the scope stays writable.
    assert _pre_write(env, repo, tool, {key: str(out / "a.py")}, "ol9") is None


def test_e2e_green_backstop_sees_a_change_committed_in_the_same_call(tmp_path):
    """A write + commit (or a cherry-pick) outside the scope is clean again
    at post; the pre→post HEAD diff still reports it — without offering a
    `git checkout --` that could not undo a commit."""
    repo, env = _mk_green(tmp_path, _GFILES, ["src/green"])
    _pre, post = _bash_roundtrip(
        env,
        repo,
        "python3 -c \"open('src/other/b.py','a').write('y')\" && "
        "git -c user.email=t@t -c user.name=t commit -qam y",
        "tu-c1",
    )
    out = json.loads(post)
    assert out["decision"] == "block"
    assert "src/other/b.py" in out["reason"]
    assert "git checkout" not in out["reason"]
    (rec,) = _breach_records(env)
    assert rec["breach"][0]["path"] == "src/other/b.py"
    assert rec["breach"][0]["committed"] is True
    # The revert allowance never covers a committed breach.
    assert _bash_pre(env, repo, "git checkout -- src/other/b.py", "tu-c1r")
    # cherry-pick of a local commit touching the outside path
    _git("checkout", "-q", "-b", "side", cwd=repo)
    with open(os.path.join(repo, "src/other/c.py"), "a") as f:
        f.write("side\n")
    _git("commit", "-qam", "side", cwd=repo)
    sha = (
        subprocess.run(
            ["git", "-C", repo, "rev-parse", "HEAD"], stdout=subprocess.PIPE, check=True
        )
        .stdout.decode()
        .strip()
    )
    _git("checkout", "-q", "-", cwd=repo)
    _pre, post = _bash_roundtrip(
        env,
        repo,
        "git -c user.email=t@t -c user.name=t cherry-pick %s" % sha,
        "tu-c2",
    )
    assert "src/other/c.py" in json.loads(post)["reason"]
    # Committing work inside the scope: nothing.
    _pre, post = _bash_roundtrip(
        env,
        repo,
        "python3 -c \"open('src/green/a.py','a').write('y')\" && "
        "git -c user.email=t@t -c user.name=t commit -qam g",
        "tu-c3",
    )
    assert post == ""
    # Committing pre-existing (pre-command) outside work unchanged: nothing.
    with open(os.path.join(repo, "src/other/c.py"), "a") as f:
        f.write("wip\n")
    _pre, post = _bash_roundtrip(
        env, repo, "git -c user.email=t@t -c user.name=t commit -qam wip", "tu-c4"
    )
    assert post == ""


@pytest.mark.parametrize(
    "cmd",
    [
        'out=$(mktemp); echo x > "$out"',
        "echo notes > ~/scratch.txt",
        'echo x > "$TMPDIR/log.txt" 2>&1',
        'echo x > "${OUT:-/tmp/o}"',
        'for f in src/green/*.py; do echo > "$f.bak"; done',
        "echo x > `mktemp`",
        "git clean -n src/",
        "git clean -n",
        "git clean --dry-run",
        "git clean -fdx src/other",
    ],
)
def test_e2e_green_bash_ignores_unexpanded_words_and_git_clean(
    tmp_path, monkeypatch, cmd
):
    """An unexpanded shell word is not a path under the root (it used to be
    joined onto the cwd and denied as 'outside green', then filed as a scope
    request for `$out`); `git clean` touches only untracked files."""
    home = tmp_path / "home"
    home.mkdir()
    repo, env = _mk_green(tmp_path, _GFILES, ["src/green"])
    env = dict(env, HOME=str(home))
    assert _bash_pre(env, repo, cmd, "dyn") is None
    # A literal write outside the scope is still denied.
    assert _bash_pre(env, repo, "echo x > src/other/new.py", "dyn2")


def test_bash_targets_tags_dyn_words_and_expands_tilde(monkeypatch):
    monkeypatch.setenv("HOME", "/h")
    tags = {}
    w, _r, ok = th._bash_targets('echo > "$out"; echo > ~/n.txt', "/repo", tags=tags)
    assert ok and "/repo/$out" in w and "/h/n.txt" in w
    assert "dyn" in tags["/repo/$out"] and "dyn" not in tags["/h/n.txt"]
    w, _r, _ok = th._bash_targets("git clean -nd", "/repo")
    assert "" not in w


@pytest.mark.parametrize(
    "cmd, why",
    [
        ("git checkout -- lib/x.py", "gitrevert"),
        ("git checkout lib/x.py", "gitrevert"),
        ("git checkout HEAD -- lib/x.py", "gitrevert"),
        ("git restore lib/x.py", "gitrevert"),
        ("git restore --source=HEAD lib/x.py", "gitrevert"),
        ("git checkout evil -- lib/x.py", "w"),
        ("git checkout evil lib/x.py", "w"),
        ("git restore --source=evil lib/x.py", "w"),
        ("git restore -s evil -- lib/x.py", "w"),
        ("git restore -sevil lib/x.py", "w"),
    ],
)
def test_bash_targets_revert_tag_only_for_index_or_head(tmp_path, cmd, why):
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "x.py").write_text("x")
    tags = {}
    w, _r, _ok = th._bash_targets(cmd, str(tmp_path), tags=tags)
    target = str(tmp_path / "lib" / "x.py")
    assert target in w
    assert tags[target] == {why}
    assert not any(t.endswith("/evil") for t in w)


def test_e2e_revert_allowance_refuses_another_tree_ish(tmp_path):
    """After a clean-at-pre breach, `git checkout <ref> -- p` is not a revert:
    it writes that ref's content, so pre still denies it."""
    repo, env = _mk_green(tmp_path, _GFILES, ["src/green"])
    _git("branch", "evil", cwd=repo)
    _bash_roundtrip(
        env, repo, "python3 -c \"open('src/other/b.py','a').write('z')\"", "tu-e1"
    )
    assert _bash_pre(env, repo, "git checkout evil -- src/other/b.py", "tu-e2")
    assert _bash_pre(env, repo, "git restore --source=evil src/other/b.py", "tu-e3")
    assert _bash_pre(env, repo, "git checkout -- src/other/b.py", "tu-e4") is None


# --------------------------------------------------------------------------- #
# subagents: the Map gives every helper its own bird
# --------------------------------------------------------------------------- #
def test_classify_agent_carries_description_and_type():
    kind, w, r, extra = th._classify(
        "Agent",
        {
            "description": "Find the auth flow " + "x" * 300,
            "prompt": "long prompt " * 50,
            "subagent_type": "Explore",
            "run_in_background": False,
        },
    )
    assert (kind, w, r) == ("agent", [], [])
    assert extra["atype"] == "Explore"
    assert extra["desc"].startswith("Find the auth flow") and len(extra["desc"]) == 120
    assert "prompt" not in extra
    # a Task with no input still classifies (and carries empty strings)
    assert th._classify("Task", None)[3] == {"desc": "", "atype": ""}


def test_e2e_subagent_records_carry_agent_type_and_parent_call_its_description(
    zoned_repo,
):
    repo, _s, env = zoned_repo
    call = {
        "session_id": "sid",
        "cwd": repo,
        "tool_name": "Agent",
        "tool_input": {
            "description": "Map the config loader",
            "prompt": "Read every config file and report back. " * 40,
            "subagent_type": "Explore",
            "run_in_background": False,
        },
        "tool_use_id": "tu-agent",
    }
    assert _run(call, "pre", env).returncode == 0
    inner = {
        "session_id": "sid",
        "cwd": repo,
        "tool_name": "Read",
        "tool_input": {"file_path": os.path.join(repo, "app.py")},
        "tool_use_id": "tu-inner",
        "agent_id": "a7f3",
        "agent_type": "Explore",
    }
    assert _run(inner, "pre", env).returncode == 0
    assert _run(inner, "post", env).returncode == 0
    assert _run(dict(call, tool_response={"ok": True}), "post", env).returncode == 0
    recs = _feed(env)
    pre_call, inner_pre, inner_post, post_call = recs[-4:]
    for rec in (pre_call, post_call):
        assert rec["kind"] == "agent" and rec["id"] == "tu-agent"
        assert rec["desc"] == "Map the config loader" and rec["atype"] == "Explore"
        # the parent's own call is not INSIDE a subagent, and the prompt stays out
        assert "agent" not in rec and "agent_type" not in rec
        assert "prompt" not in json.dumps(rec)
    assert inner_pre["ev"] == "pre" and inner_post["ev"] == "post"
    for rec in (inner_pre, inner_post):
        assert rec["agent"] == "a7f3" and rec["agent_type"] == "Explore"
        assert [os.path.basename(x) for x in rec["reads"]] == ["app.py"]
        assert "desc" not in rec and "atype" not in rec


def test_fit_line_keeps_the_subagent_identity_when_shedding():
    big = {
        "v": 1,
        "ts": 1.0,
        "ev": "post",
        "kind": "bash",
        "tool": "Bash",
        "id": "t",
        "agent": "a1",
        "agent_type": "general-purpose",
        "cmd": "x" * 400,
        "reads": ["/r/" + "y" * 200] * 400,
        "writes": ["/w/" + "z" * 200] * 400,
        "breach": [{"path": "/p" + "q" * 5000, "pattern": "p"}] * 50,
    }
    line = th._mf_fit_line(big)
    rec = json.loads(line)
    assert len(line) <= th._MF_FEED_MAX
    assert rec["agent"] == "a1" and rec["agent_type"] == "general-purpose"
