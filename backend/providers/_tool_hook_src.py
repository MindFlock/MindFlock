"""Fire-time source for the per-tool red-zone guard + tool feed.

This module is NOT run in-process. Its whole source text is read (via
``inspect.getsource``) by :func:`backend.providers.activity_markers.hook_command`,
embedded as a string literal into the ``python3 -c`` command the CLI's hooks
run, and executed there with ``exec(compile(SRC, "<mf-tool-hook>", "exec"),
ns)``. The command then calls ``ns['_mf_tool_hook'](p, s, ev)``.

Because the source runs under whatever system ``python3`` the CLI has on PATH —
never the MindFlock venv — it is stdlib-only, imports lazily inside functions,
and uses Python 3.8-compatible syntax. It contains ONLY function definitions and
constant assignments (no top-level side effects, no ``from __future__``); an AST
test in ``test_tool_hook`` enforces that so the whole module stays safe to embed.

The guard is fail-open on its own bugs (a wrapped ``exec`` and per-function
``try`` mean a hook error never blocks a tool), and reads a **guard file** —
located by path ancestors, keyed ``sha1(realpath(root))[:20]`` — resolved from
the firing shell's ``MINDFLOCK_RED_ZONE_DIR`` at fire time, so a zone added
mid-flight takes effect on the very next tool call.
"""

# Guard REVISION: bump whenever this source changes behaviour. The hooks-file
# tag carries it next to the source hash (``activity_markers.TOOL_HOOK_TAG``):
# a build heals a hook of an OLDER revision and leaves a NEWER one alone, so
# two MindFlock builds on one worktree (the uv-tool copy + a dev server)
# converge instead of rewriting settings.local.json every tick.
_MF_HOOK_REV = 4

# The firing session's tmux name, set by ``_mf_tool_hook`` for this one fire
# (the hook process handles exactly one tool call). Per-session fences in a
# shared folder (a same-folder split's pieces) are looked up by it.
_MF_SESSION = ""

# A per-session fence that says "MindFlock commits for you" refuses these git
# subcommands: each one moves the shared index, HEAD or branch under the
# other agents working in the same folder.
_MF_SHARED_GIT = (
    "add",
    "commit",
    "stash",
    "reset",
    "switch",
    "merge",
    "rebase",
    "cherry-pick",
    "revert",
    "am",
    "push",
)

_MF_SHARED_GIT_REASON = (
    "MindFlock: other agents share this folder, and MindFlock commits exactly "
    "your paths when you report done. Don't run `git {sub}` here — leave the "
    "changes in the tree."
)

# Deny-reason template. MUST stay byte-identical to
# ``backend.config.red_zones._DENY_REASON_TMPL`` — ``test_tool_hook`` asserts the
# two are equal, so the reason the model sees on a live block matches the one the
# server renders elsewhere.
_MF_DENY_TMPL = (
    'MindFlock red zone: {rel} is protected by "{label}". The user has made '
    "this path off-limits for edits. Find an approach that leaves it unchanged, "
    "or stop and ask the user — don't route around the block (copies, wrappers, "
    "shell edits)."
)

# The green-zone ("only here") deny reason. MUST stay byte-identical to
# ``backend.config.red_zones._GREEN_DENY_TMPL`` (asserted by test_tool_hook).
_MF_GREEN_TMPL = (
    "MindFlock scope: {rel} is outside the green zone(s) the user scoped this "
    "task to ({label}). Finish the in-scope work and list any out-of-scope "
    "files you need in your reply instead of editing them."
)

# At most this many zones named in a green reason (then "(+N more)").
_MF_LABEL_CAP = 5

# The pattern a green breach / deny carries (there is no one zone to name).
_MF_GREEN_OUTSIDE = "outside green"

# For GREEN only an explicit write verb makes an MCP tool a write: the red
# list's upload/push/save/... turn reads (an MCP tool uploading a local file)
# into denied writes of whatever `path` names.
_MF_GREEN_MCP_VERBS = ("write", "create", "update", "edit", "delete", "move")

# A revert (`git checkout -- p`, `git restore p`, `rm p`) of a path the
# session's own backstop flagged this long ago at most is let through.
_MF_REVERT_WINDOW_S = 900

# The green backstop's `git status`: per-call timeout and entry cap.
_MF_STATUS_TIMEOUT = 3
_MF_STATUS_CAP = 5000

_MF_PROTECT_REASON = (
    "MindFlock guard file: {rel} controls the red-zone guard and cannot be "
    "edited from an agent tool. Leave it alone; ask the user to change red-zone "
    "settings through MindFlock."
)

# Substrings that, if they appear anywhere in a Bash command, mean the command
# is reaching for a control file — deny it outright (the shlex heuristic cannot
# be trusted to spot every spelling).
_MF_PROTECT_MARKERS = (
    "settings.local.json",
    ".claude/settings.json",
    "hooks.json",
    "red_zones.json",
    ".red-zones",
    ".tool-feed",
    "disableAllHooks",
    "/red-zones",
    "override_red_zones",
)

# mcp__* whose tool name contains one of these verbs writes something. Beyond
# the filesystem server's write/edit/move this has to cover the code-editing
# servers' vocabulary — Serena (replace_symbol_body, insert_after_symbol,
# replace_content), JetBrains (replace_text_in_file), rename refactors.
_MF_MCP_WRITE_VERBS = (
    "write",
    "create",
    "update",
    "edit",
    "delete",
    "move",
    "push",
    "put",
    "upload",
    "patch",
    "replace",
    "insert",
    "rename",
    "remove",
    "save",
    "append",
    "modify",
)

# String args of an MCP write tool that name a file it writes. The filesystem
# server's move_file takes {source, destination}; Serena takes relative_path;
# JetBrains takes pathInProject. Relative values resolve against the cwd.
_MF_MCP_PATH_KEYS = (
    "path",
    "file_path",
    "filepath",
    "filePath",
    "notebook_path",
    "source",
    "destination",
    "dest",
    "target",
    "new_path",
    "old_path",
    "relative_path",
    "relativePath",
    "pathInProject",
    "uri",
)

# The GitHub MCP write tools blocked while committed breaches exist.
_MF_PUSH_MCP = (
    "mcp__github__push_files",
    "mcp__github__create_or_update_file",
    "mcp__github__delete_file",
    "mcp__github__create_pull_request",
    "mcp__github__merge_pull_request",
)

# Feed-record schema version.
_MF_FEED_V = 1

_MF_FEED_MAX = 16 * 1024

# Bash simple-command separators (with shlex punctuation_chars the operators
# arrive as their own tokens). Newline is punctuation too — see _mf_tokenize —
# so a multi-line command splits into its lines' commands; shlex glues a run of
# punctuation (";\n", "&&\n", ")\n"), which _mf_is_sep also accepts.
_MF_BASH_SEP = (";", "&&", "||", "|", "&", "\n", ";;", "|&", "(", ")")

# The shlex punctuation set: the defaults plus NEWLINE, which shlex otherwise
# treats as plain whitespace (making `ls\nrm zone/x` one command whose program
# is `ls`).
_MF_PUNCT = ";&|()<>\n"

# Command wrappers that run their argument vector as a command, mapped to the
# short/long flags that consume a SEPARATE value (`nice -n 5 git push`,
# `sudo -u bob git push`, `timeout -s KILL 60 git push`). Unwrapping them is how
# `GIT_TERMINAL_PROMPT=0 timeout 120 git push` is still seen as a push.
_MF_WRAPPERS = {
    "env": ("-u", "--unset", "-C", "--chdir"),
    "command": (),
    "builtin": (),
    "exec": ("-a",),
    "nohup": (),
    "time": ("-f", "--format", "-o", "--output"),
    "nice": ("-n", "--adjustment"),
    "timeout": ("-s", "--signal", "-k", "--kill-after"),
    "stdbuf": ("-i", "-o", "-e", "--input", "--output", "--error"),
    "sudo": ("-u", "-g", "-h", "-p", "-C", "-D", "-r", "-t", "-U", "--user"),
    "doas": ("-u", "-C"),
    "xargs": ("-n", "-I", "-P", "-d", "-L", "-s", "-a", "-E"),
    "ionice": ("-c", "-n", "-p"),
    "chronic": (),
    "unbuffer": (),
}

# Global git options that take a separate value (skipped to find the real
# subcommand: `git -C sub push`, `git -c k=v push`).
_MF_GIT_VALUE_OPTS = (
    "-C",
    "-c",
    "--git-dir",
    "--work-tree",
    "--namespace",
    "--config-env",
    "--super-prefix",
)

# Path segments a Bash command creates as a side effect of RUNNING code (an
# import, a test run, Finder) rather than editing it. A new one inside a zone
# is not a breach the agent should be told to revert.
_MF_GENERATED = (
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".DS_Store",
)

_MF_REDIR = (">", ">>", ">|", "&>", "&>>")

_MF_WRITE_ALL = (
    "rm",
    "rmdir",
    "unlink",
    "truncate",
    "shred",
    "touch",
    "chmod",
    "chown",
    "mv",
    "mkdir",
)
_MF_WRITE_LAST = ("cp", "install", "ln", "rsync")


# --------------------------------------------------------------------------- #
# environment / paths (resolved at FIRE time from the firing shell's env)
# --------------------------------------------------------------------------- #
def _mf_guard_dir():
    import os

    return os.environ.get("MINDFLOCK_RED_ZONE_DIR") or os.path.join(
        os.environ.get("MINDFLOCK_ASSISTANT_DIR")
        or os.path.join(os.path.expanduser("~"), ".mindflock-assistant"),
        ".red-zones",
    )


def _mf_feed_dir():
    import os

    return os.environ.get("MINDFLOCK_TOOL_FEED_DIR") or os.path.join(
        os.environ.get("MINDFLOCK_ASSISTANT_DIR")
        or os.path.join(os.path.expanduser("~"), ".mindflock-assistant"),
        ".tool-feed",
    )


def _mf_guard_path(root):
    import hashlib
    import os

    h = hashlib.sha1(os.path.realpath(root).encode("utf-8", "replace")).hexdigest()
    return os.path.join(_mf_guard_dir(), h[:20] + ".json")


def _mf_load_guard(path):
    import json

    try:
        with open(path, encoding="utf-8") as f:
            g = json.load(f)
    except Exception:
        return None
    return _mf_session_view(g) if isinstance(g, dict) else None


def _mf_session_view(g):
    """The guard as THIS session sees it. A folder several agents share can
    fence each one to its own paths (``sessions``, keyed by the sanitized
    tmux name): the firing session's entry becomes its green scope — no
    companions, nothing outside its paths — and the other entries become
    ``siblings`` (paths another agent is working on right now: the backstop
    never blames this session for them). A session with no entry sees the
    folder's own zones unchanged."""
    import re

    sess = g.get("sessions")
    if not isinstance(sess, dict) or not sess:
        return g
    me = re.sub(r"[^A-Za-z0-9_.-]", "_", _MF_SESSION or "")
    mine = sess.get(me) if me else None
    if not isinstance(mine, dict) or not (
        mine.get("green_rules") or mine.get("red_rules")
    ):
        return g
    v = dict(g)
    if mine.get("green_rules"):
        v["green_rules"] = list(mine.get("green_rules") or [])
        v["companions"] = list(mine.get("companions") or [])
    if mine.get("red_rules"):
        # Keep-out for this session only, on top of the folder's red zones.
        v["rules"] = list(g.get("rules") or []) + list(mine["red_rules"])
        for k in ("files", "dirs", "sym"):
            v[k] = list(g.get(k) or []) + list(mine.get("red_" + k) or [])
    sib = []
    for k, x in sess.items():
        if k != me and isinstance(x, dict):
            sib.extend(x.get("green_rules") or [])
    v["siblings"] = sib
    v["no_commit"] = bool(mine.get("no_commit"))
    return v


def _mf_find_guard(abs_path, cache):
    """The nearest ancestor of ``abs_path`` that has a guard file, or None.
    Stat-only (bounded); results cached within the one process."""
    import os

    d = abs_path
    depth = 0
    while depth < 60:
        gp = _mf_guard_path(d)
        if gp in cache:
            g = cache[gp]
        else:
            g = _mf_load_guard(gp)
            cache[gp] = g
        if g is not None:
            return g
        parent = os.path.dirname(d)
        if not parent or parent == d:
            break
        d = parent
        depth += 1
    return None


def _mf_cmd_guard(cwd, proj, cache):
    """The governing guard for a command with no path target: the nearest guard
    of the payload cwd, else of ``$CLAUDE_PROJECT_DIR``."""
    g = _mf_find_guard(cwd, cache) if cwd else None
    if g is None and proj:
        g = _mf_find_guard(proj, cache)
    return g


# --------------------------------------------------------------------------- #
# path matching against a guard
# --------------------------------------------------------------------------- #
def _mf_abs(target, cwd):
    import os

    if os.path.isabs(target):
        return os.path.normpath(target)
    return os.path.normpath(os.path.join(cwd or os.getcwd(), target))


def _mf_relto(abs_path, base):
    import os

    base = os.path.normpath(base)
    if abs_path == base:
        return ""
    if abs_path.startswith(base + os.sep):
        return abs_path[len(base) + 1 :].replace(os.sep, "/")
    return None


def _mf_match_rel(rel, rules, ci):
    import re

    cands = [rel]
    m = re.match(r"\.claude/worktrees/[^/]+/(.+)$", rel)
    if m:
        cands.append(m.group(1))
    for r in rules:
        src = r.get("re")
        if not src:
            continue
        for c in cands:
            subj = c.lower() if ci else c
            try:
                if re.match(src, subj, re.IGNORECASE if ci else 0):
                    return r
            except re.error:
                continue
    return None


def _mf_ancestor_hit(rel, guard):
    """When ``rel`` is a directory that CONTAINS a zoned path (rm -rf backend
    with zone backend/athena; git clean at root), return (rule, zoned_rel)."""
    rules = guard.get("rules") or []
    zoned = list(guard.get("files") or []) + list(guard.get("dirs") or [])
    prefix = "" if rel == "" else rel.rstrip("/") + "/"
    for f in zoned:
        if rel == "" or f == rel or f.startswith(prefix):
            hit = _mf_match_rel(f, rules, guard.get("ci"))
            if hit:
                return hit, f
    return None, None


def _mf_under(p, entry):
    import os

    e = entry.rstrip("/")
    return p == e or p.startswith(e + os.sep)


def _mf_control_hit(target, cwd, guard):
    """A hit for ``target`` against ``guard``'s ``protect`` / ``sym`` entries
    only, or None. These are ABSOLUTE paths that mostly live OUTSIDE the guard's
    root (the zone store, the guard dir, ``~/.claude/settings.json``, the real
    target of a zoned symlink), so they must be checked whatever guard governs
    the target's own path — a target outside every worktree has none at all."""
    import os

    lroot = guard.get("lroot") or guard.get("root") or ""
    rules = guard.get("rules") or []
    lex_abs = _mf_abs(target, cwd)
    try:
        real_abs = os.path.realpath(lex_abs)
    except Exception:
        real_abs = lex_abs
    for pr in guard.get("protect") or []:
        if _mf_under(lex_abs, pr) or _mf_under(real_abs, pr):
            return {"protect": True, "rel": os.path.basename(lex_abs)}
    for sy in guard.get("sym") or []:
        if _mf_under(lex_abs, sy) or _mf_under(real_abs, sy):
            rel = _mf_relto(lex_abs, lroot) or os.path.basename(lex_abs)
            hit = _mf_match_rel(rel, rules, bool(guard.get("ci")))
            return {"rule": hit or (rules[0] if rules else {}), "rel": rel}
    return None


def _mf_zone_hit(target, cwd, guard, ancestor=False):
    """Return a hit dict for a target against ``guard`` (protect / sym / rule /
    ancestor), or None. ``target`` may be relative (joined against ``cwd``)."""
    import os

    root = guard.get("root") or ""
    lroot = guard.get("lroot") or root
    ci = bool(guard.get("ci"))
    rules = guard.get("rules") or []

    hit = _mf_control_hit(target, cwd, guard)
    if hit:
        return hit

    lex_abs = _mf_abs(target, cwd)
    try:
        real_abs = os.path.realpath(lex_abs)
    except Exception:
        real_abs = lex_abs

    for base in (lroot, root):
        rel = _mf_relto(lex_abs, base)
        if rel is not None:
            hit = _mf_match_rel(rel, rules, ci)
            if hit:
                return {"rule": hit, "rel": rel}
            if ancestor:
                rule, zoned = _mf_ancestor_hit(rel, guard)
                if rule:
                    return {"rule": rule, "rel": zoned}
    rel = _mf_relto(real_abs, root)
    if rel is not None:
        hit = _mf_match_rel(rel, rules, ci)
        if hit:
            return {"rule": hit, "rel": rel}
        if ancestor:
            rule, zoned = _mf_ancestor_hit(rel, guard)
            if rule:
                return {"rule": rule, "rel": zoned}
    return None


def _mf_enforcing(g):
    """``enforcing(g) = rules or green_rules`` — THE gate for every hook
    branch (control-file protection, Bash heuristic, backstop). Gating on
    ``rules`` alone left a green-only guard's control files writable."""
    return bool(g) and bool(g.get("rules") or g.get("green_rules"))


def _mf_rule_hit(rules, rel, ci):
    import re

    for r in rules or ():
        src = r.get("re") if isinstance(r, dict) else None
        if not src:
            continue
        try:
            if re.match(src, rel, re.IGNORECASE if ci else 0):
                return r
        except re.error:
            continue
    return None


def _mf_green_one(rel, g, ci):
    """ "ok" / "companion" / "outside" for one representation (the mirror of
    ``red_zones._green_one``)."""
    import re

    green = g.get("green_rules") or []
    comps = g.get("companions") or []
    art = r"^\.mindflock_[^/]*(?:/.*)?$"
    if _mf_rule_hit(green, rel, ci) or re.match(art, rel):
        return "ok"
    m = re.match(r"^\.claude/worktrees/[^/]+(?:/(.*))?$", rel)
    inner = None
    if m:
        inner = m.group(1)
        if not inner:
            return "ok"
        if _mf_rule_hit(green, inner, ci) or re.match(art, inner):
            return "ok"
    if _mf_rule_hit(comps, rel, ci) or (inner and _mf_rule_hit(comps, inner, ci)):
        return "companion"
    return "outside"


def _mf_classify(rel_real, rel_lex, g):
    """The hook's copy of ``red_zones.classify`` over a guard dict: blocked
    (red on any representation) / outside / companion / ok. Every in-root
    representation must be writable for green."""
    import re

    ci = bool(g.get("ci"))
    cands = []
    for c in (rel_real, rel_lex):
        if c is not None and c not in cands:
            cands.append(c)
    if not cands:
        return "ok"
    for c in cands:
        m = re.match(r"^\.claude/worktrees/[^/]+(?:/(.*))?$", c)
        subs = [c] + ([m.group(1)] if m and m.group(1) else [])
        for x in subs:
            if _mf_rule_hit(g.get("rules"), x, ci):
                return "blocked"
    if not g.get("green_rules"):
        return "ok"
    verdicts = [_mf_green_one(c, g, ci) for c in cands]
    if "outside" in verdicts:
        return "outside"
    if "companion" in verdicts:
        return "companion"
    return "ok"


def _mf_green_label(g):
    labels = []
    for r in g.get("green_rules") or []:
        lab = r.get("name") or r.get("pattern") or ""
        if lab and lab not in labels:
            labels.append(lab)
    shown = ", ".join(labels[:_MF_LABEL_CAP])
    if len(labels) > _MF_LABEL_CAP:
        shown += " (+%d more)" % (len(labels) - _MF_LABEL_CAP)
    return shown or "the green zones"


def _mf_green_hit(target, cwd, guard):
    """A green hit dict when ``target`` lies inside the guard's root and is
    OUTSIDE the green scope (judged on the realpath AND the path as written —
    a symlink inside the scope pointing outside it is outside), else None.
    Paths outside the root are not governed."""
    import os

    if not guard or not guard.get("green_rules"):
        return None
    root = guard.get("root") or ""
    lroot = guard.get("lroot") or root
    lex_abs = _mf_abs(target, cwd)
    try:
        real_abs = os.path.realpath(lex_abs)
    except Exception:
        real_abs = lex_abs
    rel_lex = _mf_relto(lex_abs, lroot) if lroot else None
    if rel_lex is None and root:
        rel_lex = _mf_relto(lex_abs, root)
    rel_real = _mf_relto(real_abs, root) if root else None
    if rel_lex is None and rel_real is None:
        return None
    if _mf_classify(rel_real, rel_lex, guard) != "outside":
        return None
    shown = rel_lex
    if rel_real is not None and _mf_classify(rel_real, None, guard) == "outside":
        shown = rel_real
    return {"green": True, "rel": shown if shown is not None else rel_real}


# --------------------------------------------------------------------------- #
# classification
# --------------------------------------------------------------------------- #
def _mf_str_values(obj, out=None):
    if out is None:
        out = []
    if isinstance(obj, str):
        out.append(obj)
    elif isinstance(obj, dict):
        for v in obj.values():
            _mf_str_values(v, out)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _mf_str_values(v, out)
    return out


def _mf_patch_paths(ti):
    import re

    values = _mf_str_values(ti)
    if not any("*** Begin Patch" in v for v in values):
        return []
    text = "\n".join(values)
    out = []
    for line in text.splitlines():
        line = line.strip()
        m = re.match(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$", line)
        if m:
            out.append(m.group(1).strip())
            continue
        m = re.match(r"^\*\*\* Move to: (.+)$", line)
        if m:
            out.append(m.group(1).strip())
    return out


def _classify(tool, ti):
    """``(kind, writes, reads, extra)``. writes/reads are raw path strings."""
    if not isinstance(ti, dict):
        ti = {}
    if tool in ("Edit", "Write", "MultiEdit"):
        fp = ti.get("file_path")
        return "edit", [fp] if fp else [], [], {}
    if tool == "NotebookEdit":
        fp = ti.get("notebook_path")
        return "edit", [fp] if fp else [], [], {}
    patch = _mf_patch_paths(ti)
    if patch:
        return "edit", patch, [], {}
    if tool in ("Read", "NotebookRead"):
        fp = ti.get("file_path") or ti.get("notebook_path")
        return "read", [], [fp] if fp else [], {}
    if tool in ("Grep", "Glob", "LS"):
        fp = ti.get("path")
        return "read", [], [fp] if fp else [], {}
    if tool == "Bash":
        return "bash", [], [], {"cmd": ti.get("command") or ""}
    if tool in ("exec_command", "shell", "local_shell"):
        cmd = ti.get("command")
        if cmd is None:
            cmd = ti.get("cmd") or ""
        if isinstance(cmd, (list, tuple)):
            # Quote each argv element so `["bash", "-lc", "git push"]` stays
            # ONE -c script for the heuristic instead of three loose words.
            import shlex

            cmd = " ".join(shlex.quote(str(x)) for x in cmd)
        return "bash", [], [], {"cmd": cmd}
    if tool == "ExitPlanMode":
        return "plan", [], [], {"plan": (ti.get("plan") or "")[:12000]}
    if tool in ("Agent", "Task"):
        # The Map names the helper bird after this call: "<type> · <description>".
        return (
            "agent",
            [],
            [],
            {
                "desc": str(ti.get("description") or "")[:120],
                "atype": str(ti.get("subagent_type") or "")[:60],
            },
        )
    if isinstance(tool, str) and tool.startswith("mcp__"):
        low = tool.lower()
        if any(v in low for v in _MF_MCP_WRITE_VERBS):
            w = []
            for k in _MF_MCP_PATH_KEYS:
                v = ti.get(k)
                if isinstance(v, str) and v:
                    if k == "uri":
                        if not v.startswith("file://"):
                            continue
                        v = v[len("file://") :]
                    w.append(v)
            files = ti.get("files")
            if isinstance(files, list):
                for f in files:
                    if isinstance(f, dict) and isinstance(f.get("path"), str):
                        w.append(f["path"])
            paths = ti.get("paths")
            if isinstance(paths, list):
                for v in paths:
                    if isinstance(v, str) and v:
                        w.append(v)
            return "mcp_write", w, [], {}
        return "other", [], [], {}
    return "other", [], [], {}


# --------------------------------------------------------------------------- #
# Bash heuristic (intentionally narrow; the stat-diff backstop is the real net)
# --------------------------------------------------------------------------- #
def _mf_strip_heredocs(cmd):
    """``cmd`` with every heredoc BODY removed (the lines after ``<<WORD`` up to
    the closing ``WORD``; ``<<-`` also allows leading tabs).

    A body is data, not shell: left in, an apostrophe in prose ("it's") breaks
    the shlex parse (so the command fell to the literal-substring fallback and
    was denied for mentioning a zone's parent dir), and once newline splits
    commands each body line would be read as a command of its own. The command
    line itself (``cat > f <<'EOF'``) is kept, so its redirect is still seen.
    An unterminated or mis-detected marker (``<<`` inside a quoted string)
    leaves the text untouched."""
    import re

    if "<<" not in cmd or "\n" not in cmd:
        return cmd
    out = []
    pending = []
    for line in cmd.split("\n"):
        if pending:
            delim, dash = pending[0]
            if (line.lstrip("\t") if dash else line) == delim:
                pending.pop(0)
            continue
        out.append(line)
        for m in re.finditer(
            r"(?<!<)<<(?!<)(-?)[ \t]*\\?(['\"]?)([A-Za-z0-9_.\-]+)\2", line
        ):
            pending.append((m.group(3), m.group(1) == "-"))
    if pending:
        return cmd
    return "\n".join(out)


def _mf_tokenize(cmd):
    """shlex tokens with NEWLINE as a command separator (bash's own rule).

    Line continuations (backslash-newline) are folded first so ``rm \\\n x``
    stays one command. ``commenters`` is empty because shlex would start a
    comment at a ``#`` mid-word (``echo a#b > f``); when that leaves a real
    comment's apostrophe unbalanced (``git push # it's done``), a retry drops
    comments that start a word."""
    import re
    import shlex

    def _lex(text):
        lx = shlex.shlex(text, posix=True, punctuation_chars=_MF_PUNCT)
        lx.whitespace = " \t\r"
        lx.whitespace_split = True
        lx.commenters = ""
        return list(lx)

    text = _mf_strip_heredocs(cmd).replace("\\\n", " ")
    try:
        return _lex(text)
    except ValueError:
        if "#" not in text:
            raise
        return _lex(re.sub(r"(^|[ \t;&|()])#[^\n]*", r"\1", text, flags=re.M))


def _mf_is_sep(t):
    """Whether a token separates simple commands. shlex glues adjacent
    punctuation, so ``;\\n``, ``&&\\n`` and ``)\\n`` arrive as one token; any
    token made only of ``;&|()`` and newline is a separator (a redirect such as
    ``>&`` or ``&>`` carries ``<``/``>`` and is not)."""
    if t in _MF_BASH_SEP:
        return True
    if not t:
        return False
    for ch in t:
        if ch not in ";&|()\n":
            return False
    return True


def _mf_simple_cmds(tokens):
    cur = []
    out = []
    for t in tokens:
        if _mf_is_sep(t):
            if cur:
                out.append(cur)
                cur = []
        else:
            cur.append(t)
    if cur:
        out.append(cur)
    return out


def _mf_unwrap(argv):
    """``argv`` with leading ``NAME=VALUE`` assignments, ``!``/``{``/``}`` and
    command wrappers (env, timeout, nice, sudo, nohup, command, xargs…) — with
    their own flags — stripped, so the real program comes first."""
    import os
    import re

    a = list(argv)
    n = len(a)
    i = 0
    assign = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
    for _ in range(12):
        while i < n and (a[i] in ("!", "{", "}") or assign.match(a[i])):
            i += 1
        if i >= n:
            break
        prog = os.path.basename(a[i])
        vals = _MF_WRAPPERS.get(prog)
        if vals is None:
            break
        i += 1
        while i < n:
            t = a[i]
            if t == "--":
                i += 1
                break
            if prog == "env" and assign.match(t):
                i += 1
                continue
            if t.startswith("-") and len(t) > 1:
                i += 1
                if t in vals and i < n:
                    i += 1
                continue
            break
        if prog == "timeout" and i < n and re.match(r"^\d+(\.\d+)?[smhd]?$", a[i]):
            i += 1
    return a[i:]


def _mf_dash_c_script(args):
    """The script of ``sh -c SCRIPT`` / ``bash -lc SCRIPT`` (any short-flag
    cluster carrying ``c``), or None."""
    for j, a in enumerate(args):
        if a.startswith("-") and not a.startswith("--") and "c" in a[1:]:
            return args[j + 1] if j + 1 < len(args) else None
    return None


def _mf_is_redir(tok):
    import re

    if tok in _MF_REDIR:
        return True
    return (
        re.match(r"^\d*>>?$", tok) is not None or re.match(r"^&>>?$", tok) is not None
    )


def _bash_targets(cmd, cwd, depth=0, tags=None):
    """``(writes, reads, parse_ok)`` — abs write/read paths from a shell command.

    Narrow by design: redirects, tee, the destructive file commands, sed/perl
    in-place, cp/ln/rsync destinations, git pathspecs, and ``sh -c`` recursion,
    tracking an in-command ``cd``. A parse failure returns empty sets and
    ``parse_ok=False`` (the caller then denies only on a literal zone prefix).

    ``tags`` (a dict, optional) collects WHY each write target is one:
    ``{abs: {"rm"|"gitrevert"|"mkdir"|"clean"|"dyn"|"w", …}}`` — the revert
    allowance lets a path through only when every reason it is written is a
    revert, and the green check ignores ``mkdir`` (an empty directory is
    nothing git carries), ``clean`` (``git clean`` removes only untracked
    files) and ``dyn`` (a word the shell would still expand — ``$out``,
    ``${X:-y}``, a backtick — is not a path; the git-status backstop covers
    it). A leading ``~`` is expanded.
    """
    import os

    try:
        tokens = _mf_tokenize(cmd)
    except Exception:
        return set(), set(), False
    writes = set()
    reads = set()
    eff_cwd = cwd

    def _abs(word, base):
        if word == "~" or word.startswith("~/"):
            word = os.path.expanduser(word)
        return _mf_abs(word, base)

    def _w(p, why="w"):
        writes.add(p)
        if tags is not None:
            tags.setdefault(p, set()).add(why)
            if "$" in p or "`" in p:
                tags[p].add("dyn")

    for c in _mf_simple_cmds(tokens):
        argv = []
        i = 0
        while i < len(c):
            t = c[i]
            if _mf_is_redir(t):
                if i + 1 < len(c):
                    _w(_abs(c[i + 1], eff_cwd))
                    i += 2
                    continue
            argv.append(t)
            i += 1
        argv = _mf_unwrap(argv)
        if not argv:
            continue
        prog = os.path.basename(argv[0])
        args = argv[1:]
        nonflag = [a for a in args if not a.startswith("-")]

        # in-command cd updates the cwd for subsequent simple commands
        if prog == "cd" and nonflag:
            eff_cwd = _abs(nonflag[0], eff_cwd)
            continue

        if prog in ("bash", "sh", "dash", "zsh") and depth < 3:
            script = _mf_dash_c_script(args)
            if script is not None:
                w, r, _ok = _bash_targets(script, eff_cwd, depth + 1, tags)
                writes |= w
                reads |= r

        if prog in _MF_WRITE_ALL:
            why = (
                "rm"
                if prog in ("rm", "unlink")
                else "mkdir" if prog == "mkdir" else "w"
            )
            for a in nonflag:
                _w(_abs(a, eff_cwd), why)
        if prog == "tee":
            for a in nonflag:
                _w(_abs(a, eff_cwd))
        if prog in _MF_WRITE_LAST and nonflag:
            _w(_abs(nonflag[-1], eff_cwd))
        if prog == "sed" and any(
            a.startswith("-i")
            or a.startswith("--in-place")
            or ("i" in a[1:] and a.startswith("-") and not a.startswith("--"))
            for a in args
        ):
            # sed -i / -Ei / -i.bak: the file args are everything after the script
            for a in nonflag[1:]:
                _w(_abs(a, eff_cwd))
        if prog == "perl" and any(
            a.startswith("-i") or a.startswith("-pi") or a.startswith("-ni")
            for a in args
        ):
            for a in nonflag:
                _w(_abs(a, eff_cwd))
        if prog == "git" and args:
            sub = args[0]
            if sub in ("checkout", "restore", "rm", "mv", "clean"):
                why = "gitrevert" if sub in ("checkout", "restore") else "w"
                if sub == "clean":
                    why = "clean"
                    dry = any(
                        a == "--dry-run"
                        or (a.startswith("-") and not a.startswith("--") and "n" in a)
                        for a in args[1:]
                    )
                    if dry:
                        continue  # `git clean -n` lists; it writes nothing
                    _w("", why)  # git clean anywhere -> ancestor of every zone
                # A revert restores from the index or HEAD only: any other
                # tree-ish (`git checkout <ref> [--] p`, `git restore
                # --source=<ref> p`) writes that ref's content, not a revert.
                seen_dd = False
                paths = []
                rest = args[1:]
                j = 0
                while j < len(rest):
                    a = rest[j]
                    j += 1
                    if a == "--" and not seen_dd:
                        seen_dd = True
                        continue
                    if not seen_dd and a.startswith("-"):
                        if sub == "restore" and a in ("-s", "--source"):
                            src = rest[j] if j < len(rest) else ""
                            j += 1
                            if src != "HEAD":
                                why = "w"
                        elif sub == "restore" and a.startswith("--source="):
                            if a[len("--source=") :] != "HEAD":
                                why = "w"
                        elif sub == "restore" and a.startswith("-s") and len(a) > 2:
                            if a[2:] != "HEAD":
                                why = "w"
                        continue
                    p = _abs(a, eff_cwd)
                    if seen_dd:
                        paths.append(p)
                    elif os.path.exists(p):
                        paths.append(p)  # a pathspec that exists on disk
                    elif sub == "checkout" and a != "HEAD":
                        why = "w"  # a tree-ish: not a restore from HEAD
                for p in paths:
                    _w(p, why)

        # reads: other existing tokens under any dir (server relativizes/filters)
        for a in nonflag:
            p = _abs(a, eff_cwd)
            if p not in writes and len(reads) < 40:
                try:
                    if os.path.exists(p):
                        reads.add(p)
                except Exception:
                    pass
    return writes, reads, True


def _mf_zone_literals(guard):
    """The zone-derived literals a raw command can be searched for: every zoned
    file, plus each rule pattern's literal stem (the text before its first glob
    character, minus slashes). NOT ``dirs`` — those are the PARENTS of zoned
    files (``src`` for ``src/app/keys.secret``), not zones."""
    import re

    out = [f for f in (guard.get("files") or []) if f]
    for r in guard.get("rules") or []:
        stem = re.split(r"[*?\[]", str(r.get("pattern") or ""), maxsplit=1)[0]
        stem = stem.strip("/")
        if stem and stem not in out:
            out.append(stem)
    return out


def _mf_zone_literal_in(cmd, guard):
    """On a parse failure: whether a zone literal appears in the raw command as
    a PATH TOKEN (bounded by anything but a word char, ``.`` or ``-``, so zone
    ``db`` is not found in "feedback"), so we can still deny with a "rephrase"
    reason."""
    import re

    flags = re.IGNORECASE if guard.get("ci") else 0
    for lit in _mf_zone_literals(guard):
        pat = r"(?<![\w.\-])" + re.escape(lit) + r"(?![\w.\-])"
        if re.search(pat, cmd, flags):
            return lit
    return None


def _mf_git_subcmd(args):
    """The git subcommand in ``git [global opts] <sub> …`` (None when absent)."""
    i = 0
    while i < len(args):
        t = args[i]
        if t in _MF_GIT_VALUE_OPTS:
            i += 2
            continue
        if t.startswith("-"):
            i += 1
            continue
        return t
    return None


def _mf_git_subs(cmd, depth=0):
    """Every git SUBCOMMAND a shell command runs (``git -C x commit`` →
    commit), seen through wrappers, ``sh -c`` / ``eval`` and separators."""
    import os

    found = set()
    try:
        tokens = _mf_tokenize(cmd)
    except Exception:
        return found
    for c in _mf_simple_cmds(tokens):
        argv = []
        i = 0
        while i < len(c):
            if _mf_is_redir(c[i]):
                i += 2
                continue
            argv.append(c[i])
            i += 1
        argv = _mf_unwrap(argv)
        if not argv:
            continue
        prog = os.path.basename(argv[0])
        args = argv[1:]
        if prog in ("bash", "sh", "dash", "zsh"):
            script = _mf_dash_c_script(args)
            if script is not None and depth < 3:
                found |= _mf_git_subs(script, depth + 1)
            continue
        if prog == "eval":
            if depth < 3:
                found |= _mf_git_subs(" ".join(args), depth + 1)
            continue
        if prog == "git":
            sub = _mf_git_subcmd(args)
            if sub:
                found.add(sub)
    return found


def _mf_is_push_cmd(cmd, depth=0):
    """Whether a shell command's simple commands include ``git … push``,
    ``gh pr create`` or ``gh pr merge`` — seen through env assignments,
    wrappers (``timeout 120 git push``), subshells, ``sh -c`` / ``eval`` and
    newlines. The git SUBCOMMAND must be push, so ``git stash push`` and
    ``git log --grep push`` are not."""
    import os

    try:
        tokens = _mf_tokenize(cmd)
    except Exception:
        return False
    for c in _mf_simple_cmds(tokens):
        argv = []
        i = 0
        while i < len(c):
            if _mf_is_redir(c[i]):
                i += 2
                continue
            argv.append(c[i])
            i += 1
        argv = _mf_unwrap(argv)
        if not argv:
            continue
        prog = os.path.basename(argv[0])
        args = argv[1:]
        if prog in ("bash", "sh", "dash", "zsh"):
            script = _mf_dash_c_script(args)
            if script is not None and depth < 3 and _mf_is_push_cmd(script, depth + 1):
                return True
            continue
        if prog == "eval":
            if depth < 3 and _mf_is_push_cmd(" ".join(args), depth + 1):
                return True
            continue
        if prog == "git" and _mf_git_subcmd(args) == "push":
            return True
        if prog == "gh":
            rest = []
            j = 0
            while j < len(args):
                if args[j] in ("-R", "--repo"):
                    j += 2
                    continue
                if not args[j].startswith("-"):
                    rest.append(args[j])
                j += 1
            if len(rest) >= 2 and rest[0] == "pr" and rest[1] in ("create", "merge"):
                return True
    return False


# --------------------------------------------------------------------------- #
# stat-diff backstop
# --------------------------------------------------------------------------- #
def _mf_snap_path(tuid):
    import os

    return os.path.join(_mf_feed_dir(), ".snap", (tuid or "none") + ".json")


def _mf_stat_one(path):
    import os

    try:
        st = os.stat(path)
        return [st.st_mtime_ns, st.st_size, st.st_ino]
    except OSError:
        return None


def _mf_generated(rel):
    """Whether ``rel`` is (or is inside) a byproduct of RUNNING code — a
    ``__pycache__`` an import wrote, a test cache — not an edit."""
    for seg in rel.split("/"):
        if seg in _MF_GENERATED:
            return True
    return rel.endswith((".pyc", ".pyo"))


def _mf_backstop_bases(guard, cwd):
    """``[[prefix, abs_base], …]`` the backstop snapshots: the guard's root, plus
    — when the command runs inside an EnterWorktree / Agent-isolation worktree
    (``<root>/.claude/worktrees/<n>``) that has no guard of its own yet — that
    nested checkout, whose files sit at the SAME rel paths. Its entries are
    keyed ``.claude/worktrees/<n>/<rel>``, a prefix ``_mf_match_rel`` strips."""
    import os
    import re

    root = guard.get("root") or ""
    bases = [["", root]]
    if not cwd:
        return bases
    here = os.path.normpath(cwd)
    for base in (guard.get("lroot") or root, root):
        rel = _mf_relto(here, base) if base else None
        if not rel:
            continue
        m = re.match(r"(\.claude/worktrees/[^/]+)(?:/|$)", rel)
        if m:
            nested = os.path.join(root, m.group(1))
            if os.path.isdir(nested):
                bases.append([m.group(1) + "/", nested])
            break
    return bases


def _mf_git_status(base, prefix):
    """``{"t": {key: stat|None}, "u": [key]}`` — the dirty set of ``base`` from
    ``git --no-optional-locks status --porcelain=v1 -z -unormal`` (keys are
    ``prefix + rel``), or None on failure/timeout.

    Mechanics that matter (each was a real bug in the naive version):
    ``--no-optional-locks`` — a plain status refreshes the index under
    ``index.lock`` and collides with the human's or another session's
    ``git commit``; ``-unormal`` — an untracked tree is ONE entry, not 8000;
    R/C entries carry their ORIGIN as a second NUL field (consumed here; the
    origin counts as a write — it was deleted); a submodule is one path; the
    nested ``.claude/worktrees/`` sandboxes and any embedded repo (an
    untracked dir holding ``.git``) are skipped. Capped at
    ``_MF_STATUS_CAP`` entries."""
    import os
    import subprocess

    try:
        cp = subprocess.run(
            [
                "git",
                "--no-optional-locks",
                "-C",
                base,
                "status",
                "--porcelain=v1",
                "-z",
                "-unormal",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=_MF_STATUS_TIMEOUT,
        )
    except Exception:
        return None
    if cp.returncode != 0:
        return None
    fields = cp.stdout.decode("utf-8", "replace").split("\0")
    tracked = {}
    untracked = []
    i = 0
    n = 0
    while i < len(fields) and n < _MF_STATUS_CAP:
        f = fields[i]
        i += 1
        if len(f) < 4:
            continue
        xy, rel = f[:2], f[3:]
        paths = [rel]
        if xy[0] in "RC" or xy[1] in "RC":
            if i < len(fields) and fields[i]:
                paths.append(fields[i])  # the origin of a rename/copy
            i += 1
        for p in paths:
            if not prefix and (
                p.startswith(".claude/worktrees/")
                or p in (".claude/", ".claude/worktrees", ".claude")
            ):
                continue  # sandbox bookkeeping (-unormal folds it into .claude/)
            n += 1
            if xy == "!!":
                continue
            if xy == "??":
                if p.endswith("/") and os.path.exists(os.path.join(base, p, ".git")):
                    continue  # an embedded repo: its own business
                untracked.append(prefix + p)
                continue
            tracked[prefix + p] = _mf_stat_one(os.path.join(base, p.rstrip("/")))
    return {"t": tracked, "u": untracked}


def _mf_green_inside(key, guard):
    """Whether a green rule's pattern lies strictly INSIDE ``key`` (git status
    reports a submodule as one path: a scope inside it must not turn every
    edit there into a breach of the pointer)."""
    pre = key.rstrip("/") + "/"
    for r in guard.get("green_rules") or []:
        pat = str(r.get("pattern") or "").lstrip("/")
        if pat.startswith(pre):
            return True
    return False


def _mf_git_head(base):
    """``git rev-parse HEAD`` of ``base``, or None (unborn / failure)."""
    import subprocess

    try:
        cp = subprocess.run(
            ["git", "--no-optional-locks", "-C", base, "rev-parse", "-q", "HEAD"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=_MF_STATUS_TIMEOUT,
        )
    except Exception:
        return None
    sha = cp.stdout.decode("utf-8", "replace").strip()
    return sha if cp.returncode == 0 and sha else None


def _mf_head_moved(base, prefix, old, new):
    """Keys (``prefix + rel``) of the paths a command COMMITTED in the same
    call (``git commit``, ``cherry-pick``, a merge of a local branch) — clean
    again at post, so never in its dirty set. Only a FORWARD move counts, and
    only commits no remote-tracking ref already has: a pull / rebase / reset
    onto upstream is not this session's edit (the monitor and the push gate
    judge the branch). Empty on failure; capped at ``_MF_STATUS_CAP``."""
    import subprocess

    if not old or not new or old == new:
        return []
    git = ["git", "--no-optional-locks", "-C", base]
    try:
        anc = subprocess.run(
            git + ["merge-base", "--is-ancestor", old, new],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=_MF_STATUS_TIMEOUT,
        )
        if anc.returncode != 0:
            return []
        cp = subprocess.run(
            git
            + [
                "log",
                "--no-merges",
                "--no-renames",
                "--name-only",
                "-z",
                "--format=",
                old + ".." + new,
                "--not",
                "--remotes",
                "--",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=_MF_STATUS_TIMEOUT,
        )
    except Exception:
        return []
    if cp.returncode != 0:
        return []
    out = []
    seen = set()
    for f in cp.stdout.decode("utf-8", "replace").split("\0"):
        f = f.strip("\n")
        if f and f not in seen and len(out) < _MF_STATUS_CAP:
            seen.add(f)
            out.append(prefix + f)
    return out


def _mf_dirty_snapshot(guard, bases):
    """``{"t": …, "u": …, "h": {prefix: HEAD}}`` merged over every base, or
    None when ANY base's status failed (no baseline → post skips: fail open,
    the monitor and the push gate still see the change)."""
    out = {"t": {}, "u": [], "h": {}}
    for prefix, base in bases:
        st = _mf_git_status(base, prefix)
        if st is None:
            return None
        out["t"].update(st["t"])
        out["u"].extend(st["u"])
        out["h"][prefix] = _mf_git_head(base)
    return out


def _mf_was_clean(key, dirty):
    """Whether ``key`` had NO uncommitted change at pre (not dirty itself and
    not inside an untracked dir entry). False when there is no baseline."""
    if not isinstance(dirty, dict):
        return False
    if key in (dirty.get("t") or {}):
        return False
    for u in dirty.get("u") or []:
        if key == u.rstrip("/") or (u.endswith("/") and key.startswith(u)):
            return False
    return True


def _mf_backstop_pre(guard, tuid, cwd=""):
    """Snapshot ``[mtime_ns, size, ino]`` of every guarded (red) file and dir,
    AND the child listing of every guarded dir — so post can tell a child that
    was ADDED or REMOVED from one that merely shares a dir whose mtime moved (a
    sibling's ``sed -i`` temp file, a new ``__pycache__``) — plus the git
    DIRTY set of each base: it tells post which breached paths were clean
    before the command (the only ones a ``git checkout --`` may be suggested
    for, and the only ones the revert allowance lets the agent restore) and
    is the whole baseline of the green backstop."""
    import json
    import os

    files = list(guard.get("files") or [])
    dirs = list(guard.get("dirs") or [])
    bases = _mf_backstop_bases(guard, cwd)
    stat = {}
    ls = {}
    for prefix, base in bases:
        for rel in files + dirs:
            s = _mf_stat_one(os.path.join(base, rel))
            if s is not None:
                stat[prefix + rel] = s
        for rel in dirs:
            try:
                ls[prefix + rel] = os.listdir(os.path.join(base, rel))
            except OSError:
                pass
    dirty = _mf_dirty_snapshot(guard, bases)
    try:
        p = _mf_snap_path(tuid)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "v": 3,
                    "root": guard.get("root") or "",
                    "bases": bases,
                    "stat": stat,
                    "ls": ls,
                    "dirty": dirty,
                },
                f,
            )
    except OSError:
        pass


def _mf_backstop_post(guard, tuid, out=None):
    """Diff the pre snapshot; return a list of breaches ``{path, pattern,
    kind, clean_at_pre}`` (``new: True`` marks a path that did not exist
    before — the block message says to delete those, since ``git checkout``
    cannot). ``out`` (a dict, optional) receives ``artifact``: new untracked
    files outside the green scope — a soft flag, never a breach.

    RED: a guarded file whose OWN stat changed (modified, removed, created),
    or a zone-matching child ADDED to / REMOVED from a guarded dir. An
    unchanged zoned file is never reported just because its dir's mtime
    moved.

    GREEN (``green_rules``): every base is re-read with ``git status``; a
    tracked path that became dirty — or was dirty and whose stat moved —
    and is outside the scope (or blocked) is a breach. So is a path the
    command COMMITTED or a HEAD move brought in (``pre HEAD..post HEAD``,
    clean again at post; ``committed: True`` — never offered a checkout).
    With no pre baseline (status timed out) the green diff is skipped.

    The guard is the one the PRE snapshot was taken under (its root is in the
    snap): the post payload's cwd follows the command's ``cd``, so
    ``cd /tmp && python3 -c …`` would otherwise lose its own snapshot.
    ``guard`` (the post cwd's) is the fallback for an older snap."""
    import json
    import os

    p = _mf_snap_path(tuid)
    try:
        with open(p, encoding="utf-8") as f:
            snap = json.load(f)
    except Exception:
        snap = None
    try:
        os.unlink(p)
    except OSError:
        pass
    if not isinstance(snap, dict):
        return []
    if snap.get("root"):
        g2 = _mf_load_guard(_mf_guard_path(snap["root"]))
        if _mf_enforcing(g2):
            guard = g2
    if not _mf_enforcing(guard):
        return []
    root = guard.get("root") or ""
    if snap.get("v") in (2, 3):
        bases = snap.get("bases") or [["", root]]
        stat = snap.get("stat") or {}
        ls = snap.get("ls") or {}
    else:  # a pre-listing snapshot: diff files only (never guess at dirs)
        bases, stat, ls = [["", root]], snap, None
    dirty = snap.get("dirty") if snap.get("v") == 3 else None
    rules = guard.get("rules") or []
    files = list(guard.get("files") or [])
    dirs = list(guard.get("dirs") or [])
    breaches = []
    seen = set()

    def _record(rel, new):
        if rel in seen or _mf_generated(rel):
            return
        hit = _mf_match_rel(rel, rules, guard.get("ci"))
        if hit:
            seen.add(rel)
            b = {
                "path": rel,
                "pattern": hit.get("pattern"),
                "kind": "red",
                "clean_at_pre": True if new else _mf_was_clean(rel, dirty),
            }
            if new:
                b["new"] = True
            breaches.append(b)

    if rules:
        for prefix, base in bases:
            for rel in files:
                key = prefix + rel
                now = _mf_stat_one(os.path.join(base, rel))
                before = stat.get(key)
                if now != before:  # changed, removed, or newly created
                    _record(key, before is None)
            if ls is None:
                continue
            for rel in dirs:
                key = prefix + rel
                before_ls = ls.get(key)
                if before_ls is None and stat.get(key) is not None:
                    continue  # existed but was unlistable at pre: nothing to diff
                if _mf_stat_one(os.path.join(base, rel)) == stat.get(key):
                    continue
                try:
                    now_ls = set(os.listdir(os.path.join(base, rel)))
                except OSError:
                    now_ls = set()
                prev = set(before_ls or ())
                for child in sorted(now_ls - prev):
                    _record(key + "/" + child, True)
                for child in sorted(prev - now_ls):
                    _record(key + "/" + child, False)

    if guard.get("green_rules") and isinstance(dirty, dict):
        post = _mf_dirty_snapshot(guard, bases)
        if post is not None:
            pre_t = dirty.get("t") or {}
            # Paths the command COMMITTED (or a HEAD move brought in) are
            # clean again at post: diff the pre HEAD against the post HEAD.
            pre_h = dirty.get("h") or {}
            post_h = post.get("h") or {}
            committed = {}
            for prefix, base in bases:
                for key in _mf_head_moved(
                    base, prefix, pre_h.get(prefix), post_h.get(prefix)
                ):
                    if key not in post["t"]:
                        rel = key[len(prefix) :]
                        committed[key] = _mf_stat_one(os.path.join(base, rel))
            for key in sorted(set(post["t"]) | set(committed)):
                if key in seen or _mf_generated(key):
                    continue
                now = post["t"][key] if key in post["t"] else committed[key]
                if key in pre_t and pre_t[key] == now:
                    continue  # dirty before, untouched since (maybe committed)
                v = _mf_classify(key, None, guard)
                if v not in ("outside", "blocked"):
                    continue
                if v == "outside" and _mf_green_inside(key, guard):
                    continue  # a submodule whose inside the scope covers
                if v == "outside" and _mf_rule_hit(
                    guard.get("siblings"), key, guard.get("ci")
                ):
                    continue  # another agent in this shared folder owns it
                seen.add(key)
                if v == "blocked":
                    hit = _mf_match_rel(key, rules, guard.get("ci")) or {}
                    pat, kind = hit.get("pattern"), "red"
                else:
                    pat, kind = _MF_GREEN_OUTSIDE, "green"
                b = {
                    "path": key,
                    "pattern": pat,
                    "kind": kind,
                    "clean_at_pre": key not in pre_t and _mf_was_clean(key, dirty),
                }
                if key in committed:
                    b["committed"] = True  # `git checkout --` can't undo it
                breaches.append(b)
            pre_u = set(dirty.get("u") or [])
            arts = []
            for key in post["u"]:
                if key in pre_u or _mf_generated(key.rstrip("/")):
                    continue
                if _mf_classify(key.rstrip("/"), None, guard) == "outside":
                    if _mf_rule_hit(
                        guard.get("siblings"), key.rstrip("/"), guard.get("ci")
                    ):
                        continue
                    arts.append(key)
            if arts and out is not None:
                out["artifact"] = arts[:50]
    return breaches


def _mf_breach_reason(breach):
    """The PostToolUse block reason for backstop breaches.

    NON-DESTRUCTIVE by construction: the pre→post window can't tell whose
    write it was (the human's IDE, a second session on the worktree), so a
    ``git checkout --`` is suggested ONLY for a path that was clean before
    this command — restoring exactly the pre-command state — and ``delete``
    only for a path the command created. A path that already had changes is
    named with "don't revert it". Green breaches ask the agent to stop and
    report, never to revert."""
    import shlex

    red = [b for b in breach if b.get("kind") != "green"]
    green = [b for b in breach if b.get("kind") == "green"]
    out = []
    if red:
        shown = ", ".join(b["path"] for b in red[:3])
        if len(red) > 3:
            shown += " (+%d more)" % (len(red) - 3)
        old = [b["path"] for b in red if not b.get("new") and b.get("clean_at_pre")]
        new = [b["path"] for b in red if b.get("new")]
        kept = [
            b["path"] for b in red if not b.get("new") and not b.get("clean_at_pre")
        ]
        steps = []
        if old:
            steps.append(
                "git checkout -- " + " ".join(shlex.quote(x) for x in old[:10])
            )
        if new:
            steps.append(
                "delete the newly created " + " ".join(shlex.quote(x) for x in new[:10])
            )
        msg = (
            "MindFlock red zone: this command modified %s, which the user has "
            "made off-limits." % shown
        )
        if steps:
            msg += (
                " Revert those changes now (e.g. %s), then continue without "
                % ("; ".join(steps))
                + "touching them."
            )
        if kept:
            msg += (
                " %s already had uncommitted changes before this command — don't "
                "revert it; stop and tell the user." % ", ".join(kept[:5])
            )
        out.append(msg)
    if green:
        shown = ", ".join(b["path"] for b in green[:5])
        if len(green) > 5:
            shown += " (+%d more)" % (len(green) - 5)
        msg = (
            "MindFlock scope: these files outside your green zone(s) changed "
            "while your command ran: %s. If you changed them, stop and tell the "
            "user; don't revert files you didn't intend to change." % shown
        )
        clean = [
            b["path"] for b in green if b.get("clean_at_pre") and not b.get("committed")
        ]
        if clean:
            msg += (
                " If this command changed them by mistake, they were clean "
                "before it, so `git checkout -- %s` restores them exactly."
                % " ".join(shlex.quote(x) for x in clean[:10])
            )
        out.append(msg)
    return " ".join(out)


def _mf_recent_breaches(s, now):
    """``{path: breach}`` of this session's own backstop breaches in the last
    ``_MF_REVERT_WINDOW_S`` (newest wins), read from the tail of its feed."""
    import json
    import os
    import re

    out = {}
    if not s:
        return out
    try:
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", s)
        path = os.path.join(_mf_feed_dir(), safe + ".jsonl")
        with open(path, "rb") as f:
            try:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - 262144))
            except OSError:
                pass
            data = f.read()
    except Exception:
        return out
    for line in data.decode("utf-8", "replace").splitlines():
        if '"breach"' not in line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        ts = rec.get("ts")
        if not isinstance(ts, (int, float)) or now - ts > _MF_REVERT_WINDOW_S:
            continue
        for b in rec.get("breach") or []:
            if isinstance(b, dict) and b.get("path"):
                out[str(b["path"])] = b
    return out


def _mf_revert_ok(target, tags, guard, cwd, s):
    """Whether a write target is a legitimate REVERT of this session's own
    flagged change: every reason it is written is a revert verb, and the path
    is in a recent backstop breach of this session that says the revert is
    exact — ``git checkout``/``restore`` of a path CLEAN at that command's
    pre snapshot, ``rm`` of a path that command CREATED. Without this the
    backstop told the agent to ``git checkout`` a file and the pre-hook then
    denied the checkout."""
    import os
    import time

    why = tags.get(target) or set()
    if not why or not why <= {"gitrevert", "rm"}:
        return False
    root = guard.get("root") or ""
    lroot = guard.get("lroot") or root
    lex = _mf_abs(target, cwd)
    try:
        real = os.path.realpath(lex)
    except Exception:
        real = lex
    rels = set()
    for p, base in ((lex, lroot), (lex, root), (real, root)):
        if base:
            r = _mf_relto(p, base)
            if r:
                rels.add(r)
    if not rels:
        return False
    recent = _mf_recent_breaches(s, time.time())
    for r in rels:
        b = recent.get(r)
        if not b:
            continue
        if "gitrevert" in why and (
            b.get("new") or b.get("committed") or not b.get("clean_at_pre")
        ):
            return False
        if "rm" in why and not b.get("new"):
            return False
        return True
    return False


# --------------------------------------------------------------------------- #
# output + feed
# --------------------------------------------------------------------------- #
def _mf_print_deny(reason):
    import json
    import sys

    sys.stdout.write(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            }
        )
    )
    sys.stdout.write("\n")


def _mf_print_block(reason):
    import json
    import sys

    sys.stdout.write(json.dumps({"decision": "block", "reason": reason}))
    sys.stdout.write("\n")


def _mf_deny_reason_for(hit, guard=None):
    rule = hit.get("rule") or {}
    if hit.get("protect"):
        return _MF_PROTECT_REASON.format(rel=hit.get("rel") or "this path")
    if hit.get("green"):
        return _MF_GREEN_TMPL.format(
            rel=hit.get("rel") or "this path", label=_mf_green_label(guard or {})
        )
    label = rule.get("name") or rule.get("pattern") or hit.get("rel") or "a red zone"
    return _MF_DENY_TMPL.format(rel=hit.get("rel") or "this path", label=label)


def _mf_deny_record(hit, guard=None):
    """The feed's ``deny`` record. A green one carries ``kind: "green"`` and
    ``request: true`` — the UI shows it as a scope request with [Allow this
    file] (the agent was told to list what it needs, so this IS the ask)."""
    rule = hit.get("rule") or {}
    if hit.get("green"):
        return {
            "path": hit.get("rel"),
            "pattern": _MF_GREEN_OUTSIDE,
            "name": "",
            "zone_id": None,
            "reason": _mf_deny_reason_for(hit, guard),
            "kind": "green",
            "request": True,
        }
    rec = {
        "path": hit.get("rel"),
        "pattern": rule.get("pattern"),
        "name": rule.get("name") or "",
        "zone_id": rule.get("id"),
        "reason": _mf_deny_reason_for(hit),
    }
    if not hit.get("protect"):
        rec["kind"] = "red"
    return rec


def _mf_push_reason(breaches, guard=None):
    paths = ", ".join(breaches[:3])
    if guard and guard.get("green_rules"):
        return (
            "zone breaches are committed on this branch (%s — red-zone files or "
            "files outside the green zone(s)); pushing is blocked until they are "
            "resolved — tell the user" % paths
        )
    return (
        "red zone files are changed on this branch (%s); pushing is blocked "
        "until they are reverted" % paths
    )


def _mf_push_deny(breaches, guard=None):
    """``(reason, deny record)`` for a refused push / PR / GitHub-MCP write.
    The record matters: a denied tool fires no Post*, so a pre WITHOUT ``deny``
    reads as a command still running (the Map showed "running: git push" for
    30 minutes) and the monitor never counted the block. ``push: True`` lets a
    reader word it as a push rather than an edit."""
    reason = _mf_push_reason(breaches, guard)
    return reason, {
        "path": breaches[0] if breaches else "",
        "pattern": None,
        "name": "",
        "zone_id": None,
        "reason": reason,
        "push": True,
    }


def _mf_fit_line(record):
    """``record`` serialized to at most ``_MF_FEED_MAX`` bytes — ALWAYS valid
    JSON. Slicing the serialized string (the old way) made the line
    unparseable, so the reader dropped exactly the biggest records (a mass
    breach) and a Bash pre never saw its post. Fields are shed least-useful
    first: reads, the plan text, writes; the deny and breach — what the monitor
    and the Map act on — go last and only down to their first entries."""
    import json

    r = dict(record)
    line = json.dumps(r)
    if len(line) <= _MF_FEED_MAX:
        return line

    def _cap(k, n):
        v = r.get(k)
        if isinstance(v, list) and len(v) > n:
            r[k] = v[:n]
            r[k + "_total"] = len(v)

    if isinstance(r.get("plan"), str):
        r["plan"] = r["plan"][:1000]
    for k in ("reads", "writes", "breach", "artifact"):
        _cap(k, 50)
    line = json.dumps(r)
    if len(line) <= _MF_FEED_MAX:
        return line
    for k in ("reads", "plan", "artifact", "writes", "tp", "err"):
        if k in r:
            r.pop(k)
            line = json.dumps(r)
            if len(line) <= _MF_FEED_MAX:
                return line
    _cap("breach", 5)
    if isinstance(r.get("breach"), list):
        r["breach"] = [
            {
                k: (str(b.get(k))[:300] if k == "path" else b.get(k))
                for k in ("path", "pattern", "kind", "new", "clean_at_pre", "committed")
                if k in b
            }
            for b in r["breach"]
            if isinstance(b, dict)
        ]
    if isinstance(r.get("deny"), dict):
        d = dict(r["deny"])
        for k in ("path", "reason", "name", "pattern"):
            if isinstance(d.get(k), str):
                d[k] = d[k][:500]
        r["deny"] = d
    if isinstance(r.get("cmd"), str):
        r["cmd"] = r["cmd"][:120]
    line = json.dumps(r)
    if len(line) <= _MF_FEED_MAX:
        return line
    keep = {
        k: r[k]
        for k in ("v", "ts", "ev", "kind", "agent", "agent_type", "desc", "atype")
        if k in r
    }
    keep["tool"] = str(r.get("tool") or "")[:200]
    keep["id"] = str(r.get("id") or "")[:200]
    if isinstance(r.get("deny"), dict):
        keep["deny"] = {
            k: (v[:200] if isinstance(v, str) else v) for k, v in r["deny"].items()
        }
    if isinstance(r.get("breach"), list):
        keep["breach"] = r["breach"][:1]
    return json.dumps(keep)


def _mf_feed(record, s):
    import os
    import re

    if not s:
        return
    try:
        line = _mf_fit_line(record)
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", s)
        d = _mf_feed_dir()
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, safe + ".jsonl")
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, (line + "\n").encode("utf-8", "replace"))
        finally:
            os.close(fd)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #
def _mf_green_mcp(tool):
    low = str(tool or "").lower()
    return any(v in low for v in _MF_GREEN_MCP_VERBS)


def _mf_pre_deny(kind, writes, tool, cwd, proj, extra, cache, s=""):
    """The first applicable deny for a pre-tool call, or None.

    Every branch is gated on :func:`_mf_enforcing` (red OR green rules), and
    a path is judged red first (red always wins, with the more specific
    reason), then against the green scope."""
    import os

    if kind in ("edit", "mcp_write"):
        cg = _mf_cmd_guard(cwd, proj, cache)
        green_ok = kind == "edit" or _mf_green_mcp(tool)
        for w in writes:
            if not isinstance(w, str) or not w:
                continue
            # The guard of the path AS WRITTEN and of its REALPATH: a symlink
            # outside every worktree (`/tmp/x.py -> <root>/lib/other.py`)
            # has no lexical guard, yet the write lands inside a root. Both
            # hit functions judge the realpath against the guard's root.
            lex = _mf_abs(w, cwd)
            g = _mf_find_guard(lex, cache)
            gs = [g]
            try:
                real = os.path.realpath(lex)
            except Exception:
                real = lex
            if real != lex:
                g2 = _mf_find_guard(real, cache)
                if g2 is not None and g2 is not g:
                    gs.append(g2)
            for gx in gs:
                if not _mf_enforcing(gx):
                    continue
                hit = _mf_zone_hit(w, cwd, gx)
                if hit:
                    return _mf_deny_reason_for(hit), _mf_deny_record(hit)
                if green_ok:
                    gh = _mf_green_hit(w, cwd, gx)
                    if gh:
                        return _mf_deny_reason_for(gh, gx), _mf_deny_record(gh, gx)
            # The session's control files (the guard file, the zone store,
            # ~/.claude/settings.json) and the real targets of zoned symlinks
            # live OUTSIDE the worktree, where no guard governs the path; the
            # session's own guard (cwd / $CLAUDE_PROJECT_DIR) still protects
            # them. Its zone RULES stay scoped to its own root.
            if cg is not None and all(cg is not x for x in gs) and _mf_enforcing(cg):
                hit = _mf_control_hit(w, cwd, cg)
                if hit:
                    return _mf_deny_reason_for(hit), _mf_deny_record(hit)
    if isinstance(tool, str) and tool in _MF_PUSH_MCP:
        g = _mf_cmd_guard(cwd, proj, cache)
        if g and g.get("breaches"):
            return _mf_push_deny(g["breaches"], g)
    if kind == "bash":
        cmd = extra.get("cmd") or ""
        g = _mf_cmd_guard(cwd, proj, cache)
        if _mf_enforcing(g):
            for mk in _MF_PROTECT_MARKERS:
                if mk in cmd:
                    return (
                        _MF_PROTECT_REASON.format(rel=mk),
                        {
                            "path": mk,
                            "pattern": None,
                            "name": "",
                            "zone_id": None,
                            "reason": _MF_PROTECT_REASON.format(rel=mk),
                        },
                    )
            if g.get("no_commit"):
                bad = sorted(_mf_git_subs(cmd) & set(_MF_SHARED_GIT))
                if bad:
                    reason = _MF_SHARED_GIT_REASON.format(sub=bad[0])
                    return reason, {
                        "path": "git " + bad[0],
                        "pattern": None,
                        "name": "",
                        "zone_id": None,
                        "reason": reason,
                    }
            tags = {}
            w, _r, ok = _bash_targets(cmd, cwd, tags=tags)
            if not ok:
                # Red only: a literal zone prefix in an unparseable command.
                # GREEN never denies on a parse failure (the git-status
                # backstop covers it) — "everything outside" has no literal.
                lit = _mf_zone_literal_in(cmd, g) if g.get("rules") else None
                if lit:
                    reason = (
                        "MindFlock could not parse this shell command to check it "
                        "against the red zone %r. Rephrase it so the file it "
                        "touches is unambiguous, or leave that path alone." % lit
                    )
                    return reason, {
                        "path": lit,
                        "pattern": None,
                        "name": "",
                        "zone_id": None,
                        "reason": reason,
                    }
            else:
                for wt in sorted(w):
                    if _mf_revert_ok(wt, tags, g, cwd, s):
                        continue
                    hit = _mf_zone_hit(wt, cwd, g, ancestor=True)
                    if hit:
                        return _mf_deny_reason_for(hit), _mf_deny_record(hit)
                if g.get("green_rules"):
                    for wt in sorted(w):
                        why = tags.get(wt) or set()
                        if not wt or why == {"mkdir"}:
                            continue  # an empty dir is nothing git carries
                        if why & {"dyn", "clean"}:
                            # An unexpanded shell word is no path; `git
                            # clean` removes only untracked files. The
                            # git-status backstop covers both.
                            continue
                        if _mf_revert_ok(wt, tags, g, cwd, s):
                            continue
                        gh = _mf_green_hit(wt, cwd, g)
                        if gh:
                            return _mf_deny_reason_for(gh, g), _mf_deny_record(gh, g)
        if g and g.get("breaches") and _mf_is_push_cmd(cmd):
            return _mf_push_deny(g["breaches"], g)
    return None


def _mf_tool_hook(p, s, ev):
    """Entry point. ``ev`` is ``"pre"`` / ``"post"`` / ``"fail"``. Never raises;
    prints at most one JSON object to stdout."""
    import os

    global _MF_SESSION
    _MF_SESSION = s if isinstance(s, str) else ""
    try:
        if not isinstance(p, dict):
            return
        tool = p.get("tool_name") or ""
        ti = p.get("tool_input") or {}
        cwd = p.get("cwd") or os.getcwd()
        proj = os.environ.get("CLAUDE_PROJECT_DIR") or ""
        agent = p.get("agent_id") or ""
        tuid = p.get("tool_use_id") or ""
        kind, writes, reads, extra = _classify(tool, ti)
    except Exception:
        return

    cache = {}
    deny_rec = None
    breach = None
    post_extra = {}

    try:
        if ev == "pre":
            res = _mf_pre_deny(kind, writes, tool, cwd, proj, extra, cache, s)
            if res is not None:
                reason, deny_rec = res
                _mf_print_deny(reason)
            elif kind == "bash":
                g = _mf_cmd_guard(cwd, proj, cache)
                if _mf_enforcing(g):
                    _mf_backstop_pre(g, tuid, cwd)
        else:
            if kind == "bash":
                breach = _mf_backstop_post(
                    _mf_cmd_guard(cwd, proj, cache), tuid, post_extra
                )
                if breach and ev == "post":
                    _mf_print_block(_mf_breach_reason(breach))
    except Exception:
        pass

    # Feed record (best-effort; skipped when the session is unknown).
    try:
        import time

        rec = {
            "v": _MF_FEED_V,
            "ts": time.time(),
            "ev": ev,
            "tool": tool,
            "kind": kind,
            "id": tuid,
        }
        if agent:
            # a call made INSIDE a subagent: the Map gives each one its own bird
            rec["agent"] = agent
            at = p.get("agent_type")
            if at:
                rec["agent_type"] = str(at)[:60]
        if kind == "agent":
            for k in ("desc", "atype"):
                if extra.get(k):
                    rec[k] = extra[k]
        w_abs = [_mf_abs(x, cwd) for x in writes]
        r_abs = [_mf_abs(x, cwd) for x in reads]
        if kind == "bash":
            try:
                bw, br, _ok = _bash_targets(extra.get("cmd") or "", cwd)
                w_abs = [x for x in bw if x]
                r_abs = list(br)
            except Exception:
                pass
            rec["cmd"] = (extra.get("cmd") or "")[:300]
        if w_abs:
            rec["writes"] = w_abs
        if r_abs:
            rec["reads"] = r_abs
        if kind == "plan" and extra.get("plan"):
            rec["plan"] = extra["plan"]
        if deny_rec is not None:
            rec["deny"] = deny_rec
        if breach:
            rec["breach"] = [
                {
                    k: b.get(k)
                    for k in (
                        "path",
                        "pattern",
                        "kind",
                        "new",
                        "clean_at_pre",
                        "committed",
                    )
                    if k in b
                }
                for b in breach
            ]
        if post_extra.get("artifact"):
            rec["artifact"] = post_extra["artifact"]
        if ev == "fail":
            err = p.get("error")
            if err is not None:
                rec["err"] = str(err)[:300]
            rec["intr"] = bool(p.get("is_interrupt"))
        if ev == "pre":
            tp = p.get("transcript_path")
            if tp:
                rec["tp"] = tp
        _mf_feed(rec, s)
    except Exception:
        pass
