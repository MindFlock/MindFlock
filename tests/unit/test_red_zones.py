"""Hermetic tests for :mod:`backend.config.red_zones` — the red-zone store and
guard-file builder.

Every store/guard path is redirected into ``tmp_path`` by conftest's autouse
``_redirect_tempfiles`` (``MINDFLOCK_RED_ZONES_FILE`` /
``MINDFLOCK_RED_ZONE_DIR`` / ``MINDFLOCK_TOOL_FEED_DIR``), so nothing here
touches the owner's real ``~/.mindflock``.
"""

from __future__ import annotations

import json
import os
import subprocess

import pytest

from backend.config import red_zones as rz


def _git(*args, cwd):
    subprocess.run(
        ["git", "-C", str(cwd), "-c", "user.email=t@t", "-c", "user.name=t", *args],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _init_repo(path, origin=None):
    path = str(path)
    os.makedirs(path, exist_ok=True)
    subprocess.run(["git", "init", "-q", path], check=True)
    _git("commit", "-q", "--allow-empty", "-m", "init", cwd=path)
    if origin:
        _git("remote", "add", "origin", origin, cwd=path)
    return path


# --------------------------------------------------------------------------- #
# repo identity
# --------------------------------------------------------------------------- #
def test_identity_ssh_https_worktree_clone_agree(tmp_path):
    base = _init_repo(tmp_path / "base", origin="git@github.com:Owner/Repo.git")
    # a worktree of it
    _git("worktree", "add", "-q", str(tmp_path / "wt"), "-b", "feat", cwd=base)
    # a clone (origin = local path)
    subprocess.run(["git", "clone", "-q", base, str(tmp_path / "clone")], check=True)
    # a checkout with the HTTPS spelling of the same origin
    https = _init_repo(tmp_path / "https", origin="https://github.com/Owner/Repo.git")

    ids = {
        rz.repo_identity(base)[0],
        rz.repo_identity(str(tmp_path / "wt"))[0],
        rz.repo_identity(str(tmp_path / "clone"))[0],
        rz.repo_identity(https)[0],
    }
    assert ids == {"github.com/owner/repo"}
    assert rz.repo_identity(base)[1] == "Owner/Repo"


def test_identity_no_origin_is_path_id(tmp_path):
    repo = _init_repo(tmp_path / "local")  # no origin
    rid, label = rz.repo_identity(repo)
    assert rid.startswith("path:")
    assert rid.endswith(os.path.realpath(repo))
    assert label == "local"


def test_identity_ssh_port_form(tmp_path):
    repo = _init_repo(tmp_path / "p", origin="ssh://git@host:22/a/b.git")
    assert rz.repo_identity(repo)[0] == "host/a/b"


def test_identity_non_git_is_none(tmp_path):
    d = tmp_path / "plain"
    d.mkdir()
    assert rz.repo_identity(str(d)) is None


# --------------------------------------------------------------------------- #
# patterns
# --------------------------------------------------------------------------- #
_PATTERN_TABLE = [
    # (pattern, rel, should_match)
    ("config.toml", "config.toml", True),
    ("config.toml", "backend/config.toml", True),  # basename -> any depth
    ("config.toml", "config.toml.bak", False),
    ("/config.toml", "config.toml", True),
    ("/config.toml", "sub/config.toml", False),  # anchored
    ("cfg/", "cfg/secret.toml", True),
    ("cfg/", "x/cfg/secret.toml", True),
    ("backend/athena", "backend/athena", True),
    ("backend/athena", "backend/athena/x.py", True),
    ("backend/athena", "backend/other.py", False),
    ("*athena*", "srcv2/rules/athena.py", True),
    ("*athena*", "srcv2/rules/other.py", False),
    ("src/**/*.py", "src/a/b/c.py", True),
    # gitignore semantics: a ``**/`` segment spans ZERO or more directories.
    ("src/**/*.py", "src/a.py", True),
    ("backend/**/secret.py", "backend/secret.py", True),
    ("backend/**/secret.py", "xbackend/secret.py", False),
    ("**/cfg", "cfg/x.toml", True),
    ("**/cfg", "a/b/cfg", True),
    ("**/cfg", "acfg", False),
    ("a/**", "a/b", True),
    ("a/**", "b/a", False),
    ("a?b", "axb", True),
    ("a?b", "axxb", False),
    ("[Aa]thena", "athena", True),
    ("[Aa]thena", "Athena", True),
]


@pytest.mark.parametrize("pat, rel, exp", _PATTERN_TABLE)
def test_pattern_matches(pat, rel, exp):
    assert rz.matches(rz.compile_pattern(pat), rel) is exp


def test_compile_pattern_is_js_safe_and_python_valid():
    import re

    forbidden = ("(?P", "\\Z", "\\A", "(?#", "(?i)", "(?s)", "(?m)")
    for pat, _rel, _exp in _PATTERN_TABLE:
        src = rz.compile_pattern(pat)
        # Python must accept it…
        re.compile(src)
        # …and it must carry no Python-only / inline-flag syntax the JS RegExp
        # engine would reject.
        for f in forbidden:
            assert f not in src, (pat, src, f)


@pytest.mark.parametrize(
    "bad", ["", "   ", "..", "a/../b", "/", "\x00x", "x" * 401, "~/secret", "C:\\x"]
)
def test_normalize_rejects_bad(bad):
    with pytest.raises(ValueError):
        rz.normalize_pattern(bad)


def test_normalize_leading_slash_anchors_not_absolute():
    # A leading '/' is gitignore-style anchoring, not an absolute path.
    norm, anchored = rz.normalize_pattern("/etc/passwd")
    assert anchored is True and norm == "etc/passwd"


def test_matches_ci():
    src = rz.compile_pattern("cfg/secret.toml")
    assert rz.matches(src, "CFG/SECRET.TOML", ci=True)
    assert not rz.matches(src, "CFG/SECRET.TOML", ci=False)


# --------------------------------------------------------------------------- #
# CRUD / dedupe / waive
# --------------------------------------------------------------------------- #
def test_add_dedupe_and_effective(tmp_path):
    z1 = rz.add_zone("repo", "github.com/o/r", "config.toml", name="cfg", label="o/r")
    z2 = rz.add_zone("repo", "github.com/o/r", "./config.toml")  # same normalized
    assert z1["id"] == z2["id"]  # dedupe
    wt = str(tmp_path / "wt")
    eff = rz.effective_zones(wt, "github.com/o/r")
    assert len(eff) == 1
    assert eff[0]["scope"] == "repo" and eff[0]["re"]
    assert eff[0]["waived"] is False


def test_worktree_zone_and_repo_union(tmp_path):
    wt = str(tmp_path / "wt")
    os.makedirs(wt)
    rz.add_zone("repo", "rid", "athena", name="Athena")
    rz.add_zone("worktree", wt, "notes.md", repo_id="rid")
    eff = rz.effective_zones(wt, "rid")
    scopes = sorted(z["scope"] for z in eff)
    assert scopes == ["repo", "worktree"]


def test_waiver_flags_repo_zone_only(tmp_path):
    wt = str(tmp_path / "wt")
    os.makedirs(wt)
    z = rz.add_zone("repo", "rid", "athena")
    rz.set_waiver(wt, z["id"], True)
    eff = rz.effective_zones(wt, "rid")
    (repo_z,) = [e for e in eff if e["scope"] == "repo"]
    assert repo_z["waived"] is True
    rz.set_waiver(wt, z["id"], False)
    assert rz.effective_zones(wt, "rid")[0]["waived"] is False


def test_remove_zone_returns_scope_owner():
    z = rz.add_zone("repo", "rid", "athena")
    out = rz.remove_zone(z["id"])
    assert out["scope"] == "repo" and out["owner"] == "rid"
    assert rz.remove_zone(z["id"]) is None  # gone


def test_plan_first_flag():
    assert rz.plan_first("rid") is False
    rz.set_plan_first("rid", True, label="o/r")
    assert rz.plan_first("rid") is True
    assert rz.all_repos()["rid"]["plan_first"] is True


# --------------------------------------------------------------------------- #
# zone_files (ignored config becomes visible)
# --------------------------------------------------------------------------- #
def test_zone_files_includes_ignored(tmp_path):
    repo = _init_repo(tmp_path / "r")
    (tmp_path / "r" / ".gitignore").write_text("config.toml\n")
    (tmp_path / "r" / "config.toml").write_text("secret=1\n")
    (tmp_path / "r" / "tracked.py").write_text("x=1\n")
    _git("add", ".gitignore", "tracked.py", cwd=repo)
    _git("commit", "-q", "-m", "files", cwd=repo)
    rules = [{"re": rz.compile_pattern("config.toml")}]
    files, dirs, ignored, trunc = rz.zone_files(repo, rules)
    assert "config.toml" in files
    assert "config.toml" in ignored  # the gitignored config is guarded
    assert trunc is False


# --------------------------------------------------------------------------- #
# sync_guard
# --------------------------------------------------------------------------- #
def test_sync_guard_written_unchanged_healed_skipped(tmp_path):
    repo = _init_repo(tmp_path / "repo", origin="git@github.com:o/r.git")
    rid = rz.repo_identity(repo)[0]
    rz.add_zone("repo", rid, "config.toml", label="o/r")
    root = os.path.realpath(repo)

    assert rz.sync_guard(root, rid) == "written"
    assert rz.sync_guard(root, rid) == "unchanged"

    # External tamper (change a field) with the store unchanged -> healed.
    gp = rz.guard_path(root)
    doc = json.loads(open(gp).read())
    doc["rules"] = []  # someone stripped the rules
    open(gp, "w").write(json.dumps(doc))
    assert rz.sync_guard(root, rid) == "healed"

    # A non-git dir cannot be identified -> skipped, existing file untouched.
    plain = tmp_path / "plain"
    plain.mkdir()
    before = None
    assert rz.sync_guard(str(plain)) == "skipped"


def test_sync_guard_protect_only_with_rules(tmp_path):
    repo = _init_repo(tmp_path / "repo", origin="git@github.com:o/r.git")
    rid = rz.repo_identity(repo)[0]
    root = os.path.realpath(repo)
    # No zones -> guard has empty rules AND empty protect.
    rz.sync_guard(root, rid)
    doc = json.loads(open(rz.guard_path(root)).read())
    assert doc["rules"] == [] and doc["protect"] == []
    # Add a zone -> protect set is now populated (control files).
    rz.add_zone("repo", rid, "config.toml", label="o/r")
    rz.sync_guard(root, rid)
    doc = json.loads(open(rz.guard_path(root)).read())
    assert doc["rules"] and doc["protect"]
    assert any("settings.local.json" in p for p in doc["protect"])
    assert (
        rz.store_path() in doc["protect"]
        or os.path.abspath(rz.store_path()) in doc["protect"]
    )


def test_sync_guard_symlink_target_outside_root(tmp_path):
    repo = _init_repo(tmp_path / "repo", origin="git@github.com:o/r.git")
    outside = tmp_path / "shared" / "config.toml"
    outside.parent.mkdir()
    outside.write_text("k=1\n")
    link = tmp_path / "repo" / "config.toml"
    os.symlink(str(outside), str(link))
    _git("add", "config.toml", cwd=repo)
    _git("commit", "-q", "-m", "link", cwd=repo)
    rid = rz.repo_identity(repo)[0]
    rz.add_zone("repo", rid, "config.toml", label="o/r")
    root = os.path.realpath(repo)
    rz.sync_guard(root, rid)
    doc = json.loads(open(rz.guard_path(root)).read())
    assert os.path.realpath(str(outside)) in doc["sym"]


def test_remove_and_gc_guards(tmp_path):
    repo = _init_repo(tmp_path / "repo", origin="git@github.com:o/r.git")
    rid = rz.repo_identity(repo)[0]
    root = os.path.realpath(repo)
    rz.sync_guard(root, rid)
    gp = rz.guard_path(root)
    assert os.path.exists(gp)
    # gc keeps a live root, drops an unknown old one.
    stale = os.path.join(rz.guard_dir(), "deadbeefdeadbeefdead.json")
    open(stale, "w").write("{}")
    old = os.stat(stale).st_mtime - 999999
    os.utime(stale, (old, old))
    removed = rz.gc_guards([root])
    assert removed == 1 and os.path.exists(gp) and not os.path.exists(stale)
    rz.remove_guard(root)
    assert not os.path.exists(gp)


# --------------------------------------------------------------------------- #
# prompt texts + decoration
# --------------------------------------------------------------------------- #
def test_deny_reason_matches_hook_template():
    from backend.providers import _tool_hook_src as th

    assert rz._DENY_REASON_TMPL == th._MF_DENY_TMPL


def test_go_message_with_and_without_zones():
    empty = rz.go_message([])
    assert empty.startswith("Go ahead with your plan.")
    assert "If you need a file that isn't in your plan" in empty
    msg = rz.go_message([{"pattern": "config.toml", "name": "cfg"}])
    assert "`config.toml` (cfg)" in msg and "red zones" in msg


def test_decorate_prompt_idempotent(tmp_path):
    repo = _init_repo(tmp_path / "repo", origin="git@github.com:o/r.git")
    rid = rz.repo_identity(repo)[0]
    rz.add_zone("repo", rid, "config.toml", label="o/r")
    p0 = "Do the ticket."
    p1 = rz.decorate_prompt(p0, repo, hard_guard=True, plan_first=True)
    assert rz.PLAN_PROMPT in p1
    assert "Red zones (MindFlock blocks edits here)" in p1
    # Idempotent — a second pass adds nothing.
    p2 = rz.decorate_prompt(p1, repo, hard_guard=True, plan_first=True)
    assert p2 == p1


def test_decorate_prompt_soft_guard_wording(tmp_path):
    repo = _init_repo(tmp_path / "repo", origin="git@github.com:o/r.git")
    rid = rz.repo_identity(repo)[0]
    rz.add_zone("repo", rid, "config.toml", label="o/r")
    p = rz.decorate_prompt("go", repo, hard_guard=False)
    assert "flagged and block pushes" in p


def test_decorate_prompt_empty_is_unchanged(tmp_path):
    assert rz.decorate_prompt("", str(tmp_path), hard_guard=True) == ""


# --------------------------------------------------------------------------- #
# ignored zoned files are never crowded out by a big ignored tree (F3/F18a)
# --------------------------------------------------------------------------- #
def _venv_repo(tmp_path, n):
    """A repo whose .gitignore hides a `.venv/` of ``n`` files plus the
    user's real config files — `.venv/` sorts BEFORE them in ls-files."""
    repo = _init_repo(tmp_path / "venv")
    (tmp_path / "venv" / ".gitignore").write_text(
        ".venv/\nconfig/local.toml\nsettings.local.toml\n*.secret\n"
    )
    lib = tmp_path / "venv" / ".venv" / "lib"
    lib.mkdir(parents=True)
    for i in range(n):
        (lib / ("m%05d.py" % i)).write_text("")
    (tmp_path / "venv" / "config").mkdir()
    (tmp_path / "venv" / "config" / "local.toml").write_text("k=1\n")
    (tmp_path / "venv" / "config" / "example.toml").write_text("k=0\n")
    (tmp_path / "venv" / "settings.local.toml").write_text("s=1\n")
    (tmp_path / "venv" / "sub").mkdir()
    (tmp_path / "venv" / "sub" / "api.secret").write_text("x\n")
    _git("add", ".gitignore", "config/example.toml", cwd=repo)
    _git("commit", "-q", "-m", "files", cwd=repo)
    return repo


@pytest.mark.parametrize(
    "pattern, want",
    [
        ("config/local.toml", "config/local.toml"),
        ("config", "config/local.toml"),
        ("settings.local.toml", "settings.local.toml"),
        ("/settings.local.toml", "settings.local.toml"),
        ("*.secret", "sub/api.secret"),
    ],
)
def test_zone_files_finds_ignored_file_behind_a_big_venv(tmp_path, pattern, want):
    # 20500 ignored .venv files sort first; the old 20000-entry SCAN cap sliced
    # the listing before matching, so the zoned config was silently absent
    # (files [], truncated False): no backstop, no Map tile, no monitor alert.
    repo = _venv_repo(tmp_path, 20500)
    rules = [{"pattern": pattern, "re": rz.compile_pattern(pattern)}]
    files, dirs, ignored, trunc = rz.zone_files(repo, rules)
    assert want in files and want in ignored, (pattern, files)
    assert trunc is False
    # The guard the hook reads carries it too.
    rid = rz.repo_identity(repo)[0]
    rz.add_zone("repo", rid, pattern)
    rz.sync_guard(os.path.realpath(repo), rid)
    doc = json.loads(open(rz.guard_path(os.path.realpath(repo))).read())
    assert want in doc["files"]


def test_zone_files_regex_only_rule_still_scans_everything(tmp_path):
    # A bare-regex rule (the Map preview's shape) can't be scoped by pathspec:
    # the whole ignored tree is listed — uncapped, so it is still found.
    repo = _venv_repo(tmp_path, 20500)
    files, _d, ignored, trunc = rz.zone_files(
        repo, [{"re": rz.compile_pattern("config/local.toml")}]
    )
    assert "config/local.toml" in files and "config/local.toml" in ignored
    assert trunc is False


def test_ignored_pathspecs_are_a_superset_scope():
    def specs(*pats):
        return rz._ignored_pathspecs([{"pattern": p} for p in pats])

    assert specs("config/local.toml") == [":(literal)config/local.toml"]
    assert specs("backend/**/secret.py") == [":(literal)backend"]
    assert specs("/secrets") == [":(literal)secrets"]
    assert specs("*.pem") == [":(glob)**/*.pem", ":(glob)**/*.pem/**"]
    # Unscopable -> None (the caller lists everything).
    assert specs("[Aa]thena") is None
    assert specs("/*.toml") is None
    assert rz._ignored_pathspecs([{"re": "^x$"}]) is None


# --------------------------------------------------------------------------- #
# a leading '/' anchor survives saving (F8)
# --------------------------------------------------------------------------- #
def test_add_zone_keeps_the_anchor(tmp_path):
    z = rz.add_zone("repo", "rid", "/config")
    assert z["pattern"] == "/config"
    # `/config` and `config` are different zones, not a dedupe.
    z2 = rz.add_zone("repo", "rid", "config")
    assert z2["id"] != z["id"]
    # Re-adding the anchored form dedupes onto the first.
    assert rz.add_zone("repo", "rid", " /config ")["id"] == z["id"]
    (anch,) = [
        e for e in rz.effective_zones(str(tmp_path), "rid") if e["id"] == z["id"]
    ]
    assert rz.matches(anch["re"], "config/settings.toml")
    assert not rz.matches(anch["re"], "backend/config/loader.py")  # root only


def test_anchored_zone_is_enforced_at_the_root_only(tmp_path):
    repo = _init_repo(tmp_path / "repo", origin="git@github.com:o/r.git")
    for rel in ("config/settings.toml", "backend/config/loader.py"):
        p = tmp_path / "repo" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x\n")
    rid = rz.repo_identity(repo)[0]
    rz.add_zone("repo", rid, "/config", label="o/r")
    rz.sync_guard(os.path.realpath(repo), rid)
    doc = json.loads(open(rz.guard_path(os.path.realpath(repo))).read())
    assert doc["files"] == ["config/settings.toml"]


# --------------------------------------------------------------------------- #
# the identity memo notices an origin added mid-run (F43)
# --------------------------------------------------------------------------- #
def test_repo_identity_memo_revalidates_on_origin_change(tmp_path):
    repo = _init_repo(tmp_path / "memo_t")  # no origin yet
    first = rz.repo_identity(repo)
    assert first[0].startswith("path:")
    assert rz.repo_identity(repo) == first  # memo hit
    _git("remote", "add", "origin", "git@github.com:Owner/NewRepo.git", cwd=repo)
    # Same process, no manual memo clear: the id moves with the config.
    assert rz.repo_identity(repo) == ("github.com/owner/newrepo", "Owner/NewRepo")
    # A worktree created afterwards agrees (one id per repo, never two).
    _git("worktree", "add", "-q", str(tmp_path / "wt2"), "-b", "b2", cwd=repo)
    assert rz.repo_identity(str(tmp_path / "wt2"))[0] == "github.com/owner/newrepo"


def test_repo_identity_memo_hit_runs_no_git(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "hit", origin="git@github.com:o/hit.git")
    assert rz.repo_identity(repo)[0] == "github.com/o/hit"
    calls = []
    monkeypatch.setattr(rz, "_git", lambda *a, **k: calls.append(a))
    assert rz.repo_identity(repo)[0] == "github.com/o/hit"
    assert calls == []  # a hit re-validates with stat() only


# --------------------------------------------------------------------------- #
# suite hygiene (F50): user event hooks never run from the tests
# --------------------------------------------------------------------------- #
def test_conftest_points_user_event_hooks_at_tmp(tmp_path):
    # events._hooks_root() falls back to the REAL ~/.mindflock/hooks when this
    # is unset, so every emit in the suite would run a developer's own
    # Slack/desktop hooks with fake red-zone envelopes.
    hooks = os.environ.get("MINDFLOCK_HOOKS_DIR")
    assert hooks and hooks.startswith(str(tmp_path))
    from backend.web.core import events

    assert str(events._hooks_root()).startswith(str(tmp_path))


def test_zone_files_ignores_case_on_a_case_insensitive_root(tmp_path):
    """On a case-insensitive filesystem (macOS APFS, /mnt/c) a ``Config/`` zone
    must cover ``config/…`` for the Bash backstop and the Map too, not only in
    the hook — zone_files is what fills the guard's ``files`` list."""
    repo = tmp_path / "repo"
    (repo / "config").mkdir(parents=True)
    (repo / "config" / "settings.toml").write_text("a=1\n")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    rules = [{"re": rz.compile_pattern("Config/"), "pattern": "Config/"}]
    files, _d, _i, _t = rz.zone_files(str(repo), rules, ci=True)
    assert files == ["config/settings.toml"]
    files, _d, _i, _t = rz.zone_files(str(repo), rules, ci=False)
    assert files == []


# --------------------------------------------------------------------------- #
# v3 green zones ("only here") — the store, the one predicate, the guard.
# Critic findings (scratchpad v3_green-critic.md) are named per test.
# --------------------------------------------------------------------------- #
_CASES_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "fixtures", "zone_classify_cases.json"
)


def _cases():
    with open(_CASES_PATH, encoding="utf-8") as f:
        return json.load(f)


@pytest.mark.parametrize("case", _cases(), ids=lambda c: c["name"])
def test_classify_shared_cases(case):
    """H1: ONE predicate. The same fixture drives the hook's mirror
    (test_tool_hook) and the frontend's classifyPath."""
    got = rz.classify(case["path"], None, case["zones"], case["ci"])
    assert got == case["expect"]


def test_classify_every_representation_must_be_writable():
    """H2: a path whose REALPATH is outside the scope is outside even when
    the path as written is inside (a symlink in src/green → src/other)."""
    doc = {"green": ["src/green"]}
    assert rz.classify("src/other/b.py", "src/green/b.py", doc) == "outside"
    assert rz.classify("src/green/b.py", "src/green/b.py", doc) == "ok"
    # Red: ANY representation hitting a red zone blocks.
    doc = {"red": ["secret"]}
    assert rz.classify("secret/k", "src/link", doc) == "blocked"


def _wt_repo(tmp_path, name="repo"):
    return _init_repo(tmp_path / name, origin="git@github.com:o/%s.git" % name)


def test_green_zones_live_under_their_own_store_keys(tmp_path):
    """C1: green zones never land where a v2 server/hook reads red ones."""
    wt = _wt_repo(tmp_path)
    rid = rz.repo_identity(wt)[0]
    rz.add_zone("repo", rid, "config/")
    g = rz.add_zone(
        "worktree", os.path.realpath(wt), "src/green", repo_id=rid, kind="green"
    )
    data = json.load(open(rz.store_path()))
    entry = data["worktrees"][os.path.realpath(wt)]
    assert [z["pattern"] for z in entry["green"]] == ["src/green"]
    assert entry.get("zones") == []
    assert [z["pattern"] for z in data["repos"][rid]["zones"]] == ["config/"]
    kinds = {z["pattern"]: z["kind"] for z in rz.effective_zones(wt, rid)}
    assert kinds == {"config/": "red", "src/green": "green"}
    assert rz.worktree_zones(wt) == []  # the red-only reader
    assert rz.remove_zone(g["id"])["kind"] == "green"


def test_green_zone_is_worktree_scope_only():
    """C6: repo-scope green would leak into Verify/intake sessions."""
    with pytest.raises(ValueError):
        rz.add_zone("repo", "rid", "src", kind="green")
    with pytest.raises(ValueError):
        rz.add_zone("worktree", "/x", "src", kind="blue")


def test_same_pattern_as_both_kinds_is_a_conflict(tmp_path):
    """M6: a contradiction is refused (ZoneConflict → the routes' 409), in
    either order and across the repo/worktree scopes."""
    wt = os.path.realpath(_wt_repo(tmp_path))
    rid = rz.repo_identity(wt)[0]
    rz.add_zone("worktree", wt, "src/green", repo_id=rid, kind="green")
    with pytest.raises(rz.ZoneConflict):
        rz.add_zone("worktree", wt, "/src/green/", repo_id=rid)
    with pytest.raises(rz.ZoneConflict):
        rz.add_zone("repo", rid, "src/green")
    rz.add_zone("repo", rid, "config")
    with pytest.raises(rz.ZoneConflict):
        rz.add_zone("worktree", wt, "config", repo_id=rid, kind="green")
    assert isinstance(rz.ZoneConflict("x"), LookupError)


def test_sync_guard_v2_keeps_green_out_of_the_red_keys(tmp_path):
    """C1 + C2: the guard carries green in `green_rules` (v: 2) — `rules`,
    `files`, `dirs`, `sym` stay red-only — and `protect` is filled by a
    green-only guard too."""
    wt = _wt_repo(tmp_path)
    os.makedirs(os.path.join(wt, "src", "green"))
    open(os.path.join(wt, "src", "green", "a.py"), "w").write("x=1\n")
    rid = rz.repo_identity(wt)[0]
    rz.add_zone("worktree", wt, "src/green", repo_id=rid, kind="green")
    assert rz.sync_guard(wt, rid) == "written"
    g = json.load(open(rz.guard_path(wt)))
    assert g["v"] == 2
    assert g["rules"] == [] and g["files"] == [] and g["dirs"] == []
    assert [r["pattern"] for r in g["green_rules"]] == ["src/green"]
    assert rz.store_path() in g["protect"] and rz.guard_dir() in g["protect"]
    pats = [c["pattern"] for c in g["companions"]]
    assert "uv.lock" in pats and "*.snap" in pats


def test_red_only_guard_has_no_companions(tmp_path):
    wt = _wt_repo(tmp_path)
    rid = rz.repo_identity(wt)[0]
    rz.add_zone("repo", rid, "config/")
    rz.sync_guard(wt, rid)
    g = json.load(open(rz.guard_path(wt)))
    assert g["green_rules"] == [] and g["companions"] == []
    assert [r["pattern"] for r in g["rules"]] == ["config/"]


def test_test_files_importing_the_scope_are_companions(tmp_path):
    """H4: a change to a scoped module comes with its tests — a test file
    that imports a green file is writable (read off the import graph)."""
    wt = _wt_repo(tmp_path)
    for rel, text in {
        "pkg/__init__.py": "",
        "pkg/core.py": "X = 1\n",
        "pkg/other.py": "Y = 1\n",
        "tests/test_core.py": "from pkg import core\n",
        "tests/test_other.py": "from pkg import other\n",
    }.items():
        p = os.path.join(wt, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, "w").write(text)
    rid = rz.repo_identity(wt)[0]
    rz.add_zone("worktree", wt, "/pkg/core.py", repo_id=rid, kind="green")
    doc = rz.zones_doc(wt, rid)
    assert doc["mode"] == "green"
    assert rz.classify("tests/test_core.py", None, doc) == "companion"
    assert rz.classify("tests/test_other.py", None, doc) == "outside"
    assert rz.classify("pkg/other.py", None, doc) == "outside"
    assert rz.classify("uv.lock", None, doc) == "companion"


def test_test_named_production_code_is_never_a_companion(tmp_path):
    """ADDENDUM A.2 (shared with the Atlas): a test-named file that non-test
    code imports is CODE — test_plans.py imported by server.py,
    web/testimonials/card.py imported by web/main.py. It used to classify as
    a companion: writable outside the scope and never a breach."""
    wt = _wt_repo(tmp_path)
    for rel, text in {
        "src/__init__.py": "",
        "src/app.py": "X = 1\n",
        "pkg/__init__.py": "",
        "pkg/test_plans.py": "from src import app\n",
        "pkg/server.py": "from pkg import test_plans\n",
        "pkg/testing/__init__.py": "",
        "pkg/testing/helpers.py": "from src import app\n",
        "pkg/core.py": "from pkg.testing import helpers\n",
        "web/__init__.py": "",
        "web/testimonials/__init__.py": "",
        "web/testimonials/card.py": "from src import app\n",
        "web/main.py": "from web.testimonials import card\n",
        "tests/test_app.py": "from src import app\n",
    }.items():
        p = os.path.join(wt, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, "w").write(text)
    rid = rz.repo_identity(wt)[0]
    rz.add_zone("worktree", wt, "/src/", repo_id=rid, kind="green")
    doc = rz.zones_doc(wt, rid)
    tests = sorted(c["pattern"] for c in doc["companions"] if c["source"] == "tests")
    assert tests == ["/tests/test_app.py"]
    assert rz.classify("tests/test_app.py", None, doc) == "companion"
    for rel in (
        "pkg/test_plans.py",
        "pkg/testing/helpers.py",
        "web/testimonials/card.py",
    ):
        assert rz.classify(rel, None, doc) == "outside", rel


def test_repo_companions_config_roundtrip_and_validation():
    assert rz.companions_config("rid") == []
    out = rz.set_companions("rid", ["/backend/web/static/app.js", "dist/"])
    assert out == ["/backend/web/static/app.js", "dist/"]
    assert rz.all_repos()["rid"]["companions"] == out
    with pytest.raises(ValueError):
        rz.set_companions("rid", ["../escape"])
    assert rz.companions_config("rid") == out  # nothing saved on a bad list
    doc = {
        "green": ["/src"],
        "companions": [{"pattern": p} for p in out],
    }
    assert rz.classify("backend/web/static/app.js", None, doc) == "companion"


def test_green_exempt_keeps_the_first_sha_and_clears_with_the_scope(tmp_path):
    """C5: the exemption records the content at scoping time; re-recording
    would absolve a later edit. Removing the last green zone drops it."""
    wt = os.path.realpath(_wt_repo(tmp_path))
    g = rz.add_zone("worktree", wt, "src", kind="green")
    assert rz.set_green_exempt(wt, {"a.py": "s1"}) == {"a.py": "s1"}
    assert rz.set_green_exempt(wt, {"a.py": "s2", "b.py": "s3"}) == {
        "a.py": "s1",
        "b.py": "s3",
    }
    assert rz.drop_green_exempt(wt, ["b.py"]) == {"a.py": "s1"}
    rz.remove_zone(g["id"])
    assert rz.green_exempt(wt) == {}


def test_breach_verdicts_honour_exemptions_until_the_content_moves(tmp_path):
    """C5: an exempt path is skipped while its blob equals the recorded one;
    edited again, it is a breach."""
    wt = os.path.realpath(_wt_repo(tmp_path))
    open(os.path.join(wt, "other.py"), "w").write("a\n")
    sha = rz.worktree_blobs(wt, ["other.py"])["other.py"]
    cp = subprocess.run(
        ["git", "-C", wt, "hash-object", "other.py"], capture_output=True, text=True
    )
    assert sha == cp.stdout.strip()
    doc = {"green": ["src"], "exempt": {"other.py": sha}}
    assert rz.breach_verdicts(doc, ["other.py"], root=wt) == {}
    open(os.path.join(wt, "other.py"), "w").write("b\n")
    got = rz.breach_verdicts(doc, ["other.py"], root=wt)
    assert got["other.py"]["kind"] == "green"
    assert got["other.py"]["pattern"] == rz.GREEN_OUTSIDE
    # A deleted exempt path recorded as "deleted" stays exempt while gone.
    os.unlink(os.path.join(wt, "other.py"))
    doc["exempt"]["other.py"] = "deleted"
    assert rz.breach_verdicts(doc, ["other.py"], root=wt) == {}


def test_rev_blobs_reads_the_committed_content(tmp_path):
    wt = os.path.realpath(_wt_repo(tmp_path))
    open(os.path.join(wt, "f.txt"), "w").write("x\n")
    _git("add", "f.txt", cwd=wt)
    _git("commit", "-qm", "f", cwd=wt)
    blobs = rz.rev_blobs(wt, "HEAD", ["f.txt", "gone.txt"])
    assert blobs["gone.txt"] == "deleted"
    assert blobs["f.txt"] == rz.worktree_blobs(wt, ["f.txt"])["f.txt"]


def test_ci_probe_swaps_the_basename_not_the_whole_path(tmp_path, monkeypatch):
    """M3: on WSL /mnt/c the full-path swap hits /MNT (ext4) and said
    "case-sensitive" for an NTFS folder; the basename swap is right."""
    root = str(tmp_path / "Repo")
    os.makedirs(root)
    real_stat = os.stat

    def fake_stat(p, *a, **kw):
        p = str(p)
        if p == os.path.join(str(tmp_path), "rEPO"):
            return real_stat(root)  # the NTFS folder answers to any case
        if p.upper() == p and p != p.lower():
            raise FileNotFoundError(p)  # "/MNT/C/…": the ext4 part doesn't
        return real_stat(p, *a, **kw)

    monkeypatch.setattr(rz.os, "stat", fake_stat)
    assert rz._probe_case_insensitive(root) is True


def test_ci_probe_falls_back_to_git_core_ignorecase(tmp_path):
    wt = _init_repo(tmp_path / "123")
    # Set it explicitly both ways: `git init` on macOS already writes
    # core.ignorecase=true (APFS), so "fresh repo = False" only holds on Linux.
    _git("config", "core.ignorecase", "false", cwd=wt)
    assert rz._probe_case_insensitive(os.path.realpath(wt)) is False
    _git("config", "core.ignorecase", "true", cwd=wt)
    assert rz._probe_case_insensitive(os.path.realpath(wt)) is True


def test_green_deny_reason_matches_hook_and_caps_the_list():
    """M5: the same template in the hook; at most 5 zones named."""
    from backend.providers import _tool_hook_src as th

    assert rz._GREEN_DENY_TMPL == th._MF_GREEN_TMPL
    zones = [{"pattern": "p%d" % i} for i in range(7)]
    zones[0]["name"] = "core"
    r = rz.green_deny_reason("x/y.py", zones)
    assert r.startswith("MindFlock scope: x/y.py is outside the green zone(s)")
    assert "(core, p1, p2, p3, p4 (+2 more))" in r
    assert "list any out-of-scope files you need in your reply" in r
    guard = {"green_rules": zones}
    assert th._mf_green_label(guard) == rz.green_label(zones)


def test_green_messages_never_say_revert_and_soften_without_a_hard_guard():
    """M5: a green notice keeps earlier work; a detect-only CLI is told the
    truth (flagged, not blocked)."""
    z = {"pattern": "src/green", "kind": "green"}
    hard = rz.zone_added_message(z, hard=True, green=[z])
    soft = rz.zone_added_message(z, hard=False, green=[z])
    for m in (hard, soft):
        assert "revert" not in m.lower()
        assert "`src/green`" in m and "Keep what you already changed" in m
    assert "edits outside are blocked" in hard
    assert "flagged and block pushes" in soft
    red_soft = rz.zone_added_message({"pattern": "cfg"}, hard=False)
    assert "flagged and block pushes" in red_soft and "blocked." not in red_soft
    widened = rz.green_scope_message(None, [z, {"pattern": "/docs/a.md"}])
    assert "you may now also edit `/docs/a.md`" in widened
    gone = rz.green_scope_message(z, [])
    assert "anywhere in the repo again" in gone
    narrowed = rz.green_scope_message(z, [{"pattern": "/docs"}])
    assert "`src/green` is no longer in your scope" in narrowed


def test_go_message_with_a_planned_scope():
    msg = rz.go_message([], scope=[{"pattern": "/a.py"}, {"pattern": "/lib/"}])
    assert "only modify the planned files — `/a.py`, `/lib/`" in msg
    assert "list it in your reply" in msg


def test_decorate_prompt_adds_the_green_scope_note(tmp_path):
    wt = _wt_repo(tmp_path)
    rid = rz.repo_identity(wt)[0]
    rz.add_zone("worktree", wt, "/src/green", repo_id=rid, kind="green")
    p = rz.decorate_prompt("do it", wt, hard_guard=True)
    assert "Scope (MindFlock green zones): only modify files under `/src/green`" in p
    assert "Red zones" not in p
    assert rz.decorate_prompt(p, wt, hard_guard=True) == p  # idempotent


def test_zones_doc_without_zones_and_a_bad_doc_never_raise(tmp_path):
    wt = _wt_repo(tmp_path)
    doc = rz.zones_doc(wt, rz.repo_identity(wt)[0])
    assert doc["mode"] is None and doc["red"] == [] and doc["green"] == []
    assert rz.classify("a", None, {"green": [{"nonsense": 1}]}) == "outside"
    assert rz.classify("a", None, None) == "ok"


@pytest.mark.parametrize(
    "pattern, yes, no",
    [
        ("[]x]", ["]", "x", "src/]"], ["a", "ax]"]),
        ("[!]x]", ["a"], ["]", "x", "ax]"]),
        ("a[]]b", ["a]b"], ["ab", "a]]b"]),
        ("/" + "app/[[]slug[]]/page.tsx", ["app/[slug]/page.tsx"], ["app/s/page.tsx"]),
    ],
)
def test_class_with_a_leading_bracket_means_the_same_in_python_and_js(pattern, yes, no):
    """``[]x]`` used to compile to ``[]x]`` — a class holding ']' in Python
    but an EMPTY class in JS (and ``[^]x]`` = any char in JS): the hook
    enforced one path set while the Map drew another."""
    import shutil

    src = rz.compile_pattern(pattern)
    import re as _re

    assert not _re.search(r"(?<!\\)\[\^?\]", src), src  # no `[]` / `[^]` class
    for p in yes:
        assert rz.matches(src, p), (pattern, p)
    for p in no:
        assert not rz.matches(src, p), (pattern, p)
    node = shutil.which("node")
    if not node:
        return
    script = (
        "const [src, ...ps] = process.argv.slice(1);"
        "const r = new RegExp(src);"
        "console.log(JSON.stringify(ps.map((p) => r.test(p))));"
    )
    cp = subprocess.run(
        [node, "-e", script, src, *yes, *no], stdout=subprocess.PIPE, check=True
    )
    assert json.loads(cp.stdout) == [True] * len(yes) + [False] * len(no)


def test_glob_escape_round_trips_literal_paths():
    for rel in ("app/[slug]/page.tsx", "a*b?.py", "x/[...rest]/+page.svelte", "p]q"):
        src = rz.compile_pattern("/" + rz.glob_escape(rel))
        assert rz.matches(src, rel), rel
        assert rz.matches(src, rel + "/child"), rel


def test_case_insensitive_root_still_scopes_the_ignored_scan(tmp_path, monkeypatch):
    """macOS (case-insensitive by default): the ignored-file walk must stay
    scoped by the zone's pathspec (with git's `icase` magic) — dropping the
    scoping let a big ignored `.venv` fill the scan bound, so a gitignored
    zoned config file previewed as 0 files and went unguarded."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    # what `git init` sets on macOS: ignore rules then match case-insensitively
    subprocess.run(
        ["git", "-C", str(repo), "config", "core.ignorecase", "true"], check=True
    )
    (repo / ".gitignore").write_text(".venv/\nconfig/local.toml\n")
    for i in range(60):
        p = repo / ".venv" / "lib" / f"pkg{i:03d}.py"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("")
    (repo / "config").mkdir()
    (repo / "config" / "Local.toml").write_text("secret = 1\n")
    monkeypatch.setattr(rz, "_IGNORED_SCAN_MAX", 50, raising=False)
    rules = [
        {"re": rz.compile_pattern("config/local.toml"), "pattern": "config/local.toml"}
    ]
    _f, _d, ignored, _t = rz.zone_files(str(repo), rules, ci=True)
    assert ignored == ["config/Local.toml"]


def test_icase_pathspec_keeps_the_existing_magic():
    assert rz._icase_pathspec(":(literal)config") == ":(literal,icase)config"
    assert rz._icase_pathspec(":(glob)**/x") == ":(glob,icase)**/x"
    assert rz._icase_pathspec("plain") == ":(icase)plain"
