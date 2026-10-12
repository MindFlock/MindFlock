"""Code Map outlines (``backend.web.core.code_outline``): outlines, the
index, the file view and search.

Every test builds a small REAL git repo under ``tmp_path`` (the index reads
``git ls-files`` and fingerprints the worktree with ``git status``), and the
server is replaced by a tiny namespace carrying only what ``code_map`` reads
for the fork-point diff — so nothing here touches the live engine, the
owner's worktrees or ``~/.mindflock``.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from backend.web.core import code_map as cm
from backend.web.core import code_outline as co
from backend.web.core import snapshot


def _git(*args: str, cwd) -> str:
    cp = subprocess.run(
        ["git", "-C", str(cwd), "-c", "user.email=t@t", "-c", "user.name=t", *args],
        capture_output=True,
        text=True,
    )
    assert cp.returncode == 0, cp.stderr + cp.stdout
    return cp.stdout.strip()


def _write(root: Path, rel: str, text: str = "") -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def _repo(root: Path, files: dict) -> str:
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    for rel, text in files.items():
        _write(root, rel, text)
    _git("add", "-A", cwd=root)
    _git("commit", "-q", "-m", "init", "--allow-empty", cwd=root)
    return str(root)


def _body(n: int = 45) -> str:
    """Filler that keeps a file above the trivial-file threshold."""
    return "".join(f"# line {i}\n" for i in range(n))


class _FakeSrv:
    """The slice of ``backend.web.server`` code_map reads."""

    def __init__(self, fork: str):
        self.fork = fork
        self._DIFF_STAT_CACHE: dict = {}

    def _session_fork_point(self, inst, wt):
        return self.fork

    def _worktree_fingerprint(self, wt, base):
        return snapshot._worktree_fingerprint(wt, base)


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Fresh caches for every test (module-level memos would otherwise carry
    one test's repo state into the next)."""
    monkeypatch.setenv("MINDFLOCK_TOOL_FEED_DIR", str(tmp_path / "feed"))
    for name in (
        "_LIST_CACHE",
        "_CHANGED_CACHE",
        "_GRAPH_CACHE",
        "_SCAN_MEMO",
        "_MEMO_LOADED",
    ):
        getattr(cm, name).clear()
    for name in ("_INDEX", "_INFO_MEMO", "_FULL_MEMO"):
        getattr(co, name).clear()
    yield


# --------------------------------------------------------------------------- #
# Interface ranking
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# JVM source-set merge
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# Outlines
# --------------------------------------------------------------------------- #


def test_quick_outlines_per_language():
    py = co.quick_outline(
        '"""Doc.\n\nclass NotAClass in prose\n"""\n'
        "__all__ = ['Pub']\n\n"
        "class Pub(Protocol):\n    x: int\n    def m(self):\n        pass\n\n"
        "def hidden():\n    pass\n\nLIMIT = 3\n",
        "a.py",
    )
    assert [(s["name"], s["kind"], s["public"]) for s in py] == [
        ("Pub", "interface", True),
        ("hidden", "function", False),
        ("LIMIT", "const", False),
    ]
    assert py[0]["children"][0]["name"] == "m" and py[0]["end"] >= 10
    ts = co.quick_outline(
        "export default function App() {}\n"
        "export const useUi = () => 1\n"
        "const local = 2\n"
        "function inner() {}\n"
        "export class Store {\n  get(key: string) {\n    return 1\n  }\n  #secret() {}\n}\n"
        "export { inner }\n",
        "a.tsx",
    )
    by = {s["name"]: s for s in ts}
    assert set(by) == {"App", "useUi", "inner", "Store"}
    assert by["useUi"]["kind"] == "function" and by["inner"]["public"] is True
    assert [(c["name"], c["public"]) for c in by["Store"]["children"]] == [
        ("get", True),
        ("#secret", False),
    ]
    go = co.quick_outline(
        "package x\n\ntype Store struct {\n\tdb int\n}\n\n"
        "func (s *Store) Get() int { return 1 }\n\nfunc helper() {}\n",
        "x.go",
    )
    assert [(s["name"], s["public"]) for s in go] == [
        ("Store", True),
        ("helper", False),
    ]
    assert go[0]["children"][0]["name"] == "Get"
    rs = co.quick_outline(
        "pub struct S;\nimpl S {\n    pub fn new() -> S { S }\n    fn p() {}\n}\nfn main() {}\n",
        "m.rs",
    )
    assert [(s["name"], s["public"]) for s in rs] == [("S", True), ("main", False)]
    assert [c["name"] for c in rs[0]["children"]] == ["new", "p"]
    tf = co.quick_outline('variable "cidr" {}\nresource "aws_vpc" "main" {}\n', "m.tf")
    assert [(s["name"], s["public"]) for s in tf] == [("cidr", True), ("main", False)]


def test_full_outline_python_fields_and_java_multiline_params():
    py = co.full_outline(
        "from dataclasses import dataclass\n\n@dataclass\nclass Row:\n"
        "    id: int\n    name: str = ''\n    def label(self, sep: str = '-') -> str:\n"
        "        return ''\n\nclass Color(Enum):\n    RED = 1\n",
        "m.py",
    )
    row = py[0]
    assert [(c["name"], c["kind"]) for c in row["children"]] == [
        ("id", "field"),
        ("name", "field"),
        ("label", "method"),
    ]
    assert row["children"][2]["sig"] == "label(self, sep: str='-') -> str"
    assert py[1]["kind"] == "enum" and py[1]["children"][0]["name"] == "RED"
    jv = co.full_outline(
        "package a;\n\npublic class Api {\n  private final Repo repo;\n\n"
        "  public List<Brand> getAllBrands(\n      @RequestParam int page,\n"
        "      @RequestParam int size) {\n    return null;\n  }\n}\n",
        "Api.java",
    )
    kids = {c["name"]: c for c in jv[0]["children"]}
    assert kids["repo"]["kind"] == "field" and kids["repo"]["public"] is False
    assert "int size" in kids["getAllBrands"]["sig"]
    assert kids["getAllBrands"]["public"] is True
    assert jv[0]["end"] == 11


# --------------------------------------------------------------------------- #
# File view
# --------------------------------------------------------------------------- #


def _view_repo(tmp_path, monkeypatch) -> tuple:
    wt = _repo(
        tmp_path / "r",
        {
            "lib/core.py": (
                "class Thing:\n    name: str\n\n    def run(self):\n        return 1\n\n"
                "def helper():\n    return 2\n"
            ),
            "lib/__init__.py": "",
            "app/a.py": "from lib.core import Thing\nfrom lib import core\ncore.helper()\n",
            "tests/test_core.py": "from lib.core import helper\n",
            "api/routes.py": "".join(
                f'@app.get("/api/r{i // 10}/x{i}")\ndef h{i}():\n    return {i}\n\n'
                for i in range(30)
            ),
        },
    )
    fork = _git("rev-parse", "HEAD", cwd=wt)
    monkeypatch.setattr(cm, "_server", lambda: _FakeSrv(fork))
    return wt, fork


def test_file_view_imports_used_by_and_changes(tmp_path, monkeypatch):
    wt, _fork = _view_repo(tmp_path, monkeypatch)
    _write(
        Path(wt),
        "lib/core.py",
        "class Thing:\n    name: str\n\n    def run(self):\n        return 1\n\n"
        "def helper():\n    return 3\n",
    )
    v = co.file_view(object(), wt, "lib/core.py")
    assert v["lang"] == "py" and v["loc"] == 8
    assert [s["name"] for s in v["symbols"]] == ["Thing", "helper"]
    assert v["changed_lines"] == [[8, 8]]
    assert v["changed_symbols"] == ["helper"]
    assert v["symbols"][1].get("changed") is True and "changed" not in v["symbols"][0]
    used = {u["path"]: u for u in v["used_by"]}
    assert set(used) == {"app/a.py"}
    assert set(used["app/a.py"]["names"]) == {"Thing", "helper"}
    assert used["app/a.py"]["folder"] == "app" and used["app/a.py"]["name"] == "a.py"
    assert v["tested_by"] == ["tests/test_core.py"]
    assert v["zones"] == {"red": False, "green": None}
    assert v["role"] == "code"
    a = co.file_view(object(), wt, "app/a.py")
    assert [i["path"] for i in a["imports"]["internal"]] == ["lib/core.py"]
    assert a["changed_lines"] == [] and a["changed_symbols"] == []


def test_file_view_new_file_and_member_change(tmp_path, monkeypatch):
    wt, _fork = _view_repo(tmp_path, monkeypatch)
    _write(Path(wt), "lib/new.py", "def fresh():\n    return 1\n")
    v = co.file_view(object(), wt, "lib/new.py")
    assert v["changed_lines"] == [[1, 2]] and v["changed_symbols"] == ["fresh"]
    _write(
        Path(wt),
        "lib/core.py",
        "class Thing:\n    name: str\n\n    def run(self):\n        return 9\n\n"
        "def helper():\n    return 2\n",
    )
    v = co.file_view(object(), wt, "lib/core.py")
    assert v["changed_symbols"] == ["Thing", "Thing.run"]


def test_file_view_groups_many_routes_changed_first(tmp_path, monkeypatch):
    wt, _fork = _view_repo(tmp_path, monkeypatch)
    text = (Path(wt) / "api/routes.py").read_text().replace("return 25", "return -25")
    _write(Path(wt), "api/routes.py", text)
    v = co.file_view(object(), wt, "api/routes.py")
    assert len(v["entry"]) == 30
    assert v["entry"][0]["handler"] == "h25" and v["entry"][0]["changed"] is True
    assert not any(e["changed"] for e in v["entry"][1:])
    groups = v["entry_groups"]
    assert [g["prefix"] for g in groups][:1] == ["/api/r2"]
    assert groups[0]["changed"] is True
    assert sorted(g["count"] for g in groups) == [10, 10, 10]


@pytest.mark.parametrize(
    "bad", ["/etc/passwd", "../x.py", "lib/../../x", "missing.py", "", "lib"]
)
def test_file_view_rejects_bad_paths(tmp_path, monkeypatch, bad):
    wt, _fork = _view_repo(tmp_path, monkeypatch)
    with pytest.raises(ValueError):
        co.file_view(object(), wt, bad)


def test_file_view_rejects_a_symlink_out_of_the_worktree(tmp_path, monkeypatch):
    wt, _fork = _view_repo(tmp_path, monkeypatch)
    outside = tmp_path / "secret.py"
    outside.write_text("VALUE = 1\n")
    (Path(wt) / "link.py").symlink_to(outside)
    with pytest.raises(ValueError):
        co.file_view(object(), wt, "link.py")


# --------------------------------------------------------------------------- #
# Search / budgets / caching
# --------------------------------------------------------------------------- #


def test_search_ranks_exact_prefix_words_and_skips_nothing_useful(tmp_path):
    wt = _repo(
        tmp_path / "r",
        {
            "frontend/components/CodeMapTab.tsx": "export function CodeMapTab() {}\n",
            "backend/code_map.py": "def build_graph():\n    pass\n\nclass MapIndex:\n    pass\n",
            "tests/test_map.py": "def test_map_things():\n    pass\n",
        },
    )
    names = [i["name"] for i in co.search(wt, "CodeMapTab")["items"]]
    assert names[0] == "CodeMapTab"
    hits = co.search(wt, "map")["items"]
    got = [i["name"] for i in hits]
    assert {"CodeMapTab", "code_map.py", "MapIndex"} <= set(got)
    assert got.index("MapIndex") < got.index("test_map_things")  # tests rank lower
    assert co.search(wt, "cmt")["items"][0]["name"] in ("CodeMapTab", "CodeMapTab.tsx")
    comp = [i for i in co.search(wt, "components")["items"] if i["kind"] == "dir"]
    assert comp and comp[0]["path"] == "frontend/components"
    assert co.search(wt, "")["items"] == []
    assert len(co.search(wt, "a", limit=2)["items"]) == 2


def test_budget_goes_partial_then_completes(tmp_path, monkeypatch):
    wt = _repo(
        tmp_path / "r",
        {
            "a/x.py": "from b.y import f\n" + _body(),
            "b/y.py": "def f():\n    pass\n" + _body(),
        },
    )
    monkeypatch.setattr(co, "INFO_BUDGET_S", -1.0)
    monkeypatch.setattr(co, "_MIN_FRESH_INFO", 0)
    monkeypatch.setattr(co, "GRAPH_SHARE_S", -1.0)
    assert co.search(wt, "f")["partial"] is True
    assert ("a/x.py", "b/y.py") not in co._index(wt)["edges"]
    monkeypatch.setattr(co, "INFO_BUDGET_S", 4.0)
    monkeypatch.setattr(co, "GRAPH_SHARE_S", 6.0)
    co.forget(wt)  # a partial index is only cached for a moment
    assert co.search(wt, "f")["partial"] is False
    assert ("a/x.py", "b/y.py") in co._index(wt)["edges"]


def test_index_follows_the_worktree_content(tmp_path):
    wt = _repo(
        tmp_path / "r", {"a/x.py": _body(), "b/y.py": "def f():\n    pass\n" + _body()}
    )
    first = co._index(wt)
    assert co._index(wt) is first  # cached index
    _write(Path(wt), "a/x.py", "from b.y import f\n" + _body())
    second = co._index(wt)
    assert second is not first
    assert ("a/x.py", "b/y.py") in second["edges"]
    co.forget(wt)
    assert wt not in co._INDEX
    assert not any(k.startswith(os.path.join(wt, "")) for k in co._INFO_MEMO)


def test_public_functions_never_raise_on_a_non_repo(tmp_path):
    d = str(tmp_path / "nope")
    assert co.search(d, "x")["items"] == []
    co.forget(d)


@pytest.mark.parametrize(
    "rel,text",
    [
        ("a.c", "static int " + "a " * 100000 + "\n"),
        ("A.java", "class A {\n  public " + "List<A> " * 30000 + "\n}\n"),
        ("a.ts", "export const x = " + "(a) => " * 30000 + "\n"),
        ("a.py", "def f(" + "a, " * 50000 + "):\n    pass\n"),
        ("a.go", "type (\n" + "\tX int\n" * 20000),
    ],
    ids=[
        "c-long-line",
        "java-long-line",
        "ts-long-line",
        "py-long-def",
        "go-open-block",
    ],
)
def test_outlines_stay_fast_on_pathological_files(rel, text):
    import time

    t0 = time.monotonic()
    co.quick_outline(text, rel)
    co.full_outline(text, rel)
    assert time.monotonic() - t0 < 3.0


def test_quick_py_a_triple_quote_literal_opens_no_string():
    """A scanner that spells ``'\"\"\"'`` as a literal (code_outline itself)
    must not read the rest of its file as one docstring."""
    text = (
        "def scan(line):\n"
        "    for q in ('" + '"""' + "', \"" + "'''" + '"):\n'
        "        pass\n"
        "\n\n"
        "def after():\n"
        "    return 1\n"
    )
    names = [s["name"] for s in co.quick_outline(text, "m.py")]
    assert names == ["scan", "after"]


# --------------------------------------------------------------------------- #
# v3 review fixes
# --------------------------------------------------------------------------- #


def test_index_is_rebuilt_once_the_fingerprint_turns_unknown(tmp_path, monkeypatch):
    """An index built under a real fingerprint never expires; reusing it once
    the fingerprint became unknown (>5000 dirty paths) froze search and
    the file view on the old snapshot forever."""
    wt = _repo(tmp_path / "r", {"pkg/a.py": "def alpha():\n    pass\n" + _body()})
    assert co.search(wt, "alpha")["items"]
    monkeypatch.setattr(co, "_content_fp", lambda wt: None)
    _write(Path(wt), "pkg/new_mod.py", "def gamma():\n    pass\n" + _body())
    names = [i["name"] for i in co.search(wt, "gamma")["items"]]
    assert "gamma" in names


def test_js_full_outline_survives_regex_literals_and_templates():
    """A regex literal holding `{` or `'` (or a multi-line template) used to
    desync the brace depth and drop every later top-level declaration."""
    src = (
        "export function a(s) {\n"
        '  return s.replace(/\\s*\\{?\\s*$/, "");\n'
        "}\n"
        'function shq(s) { return "\'" + String(s).replace(/\'/g, "x") + "\'" }\n'
        "const t = `multi\n{ line ${x}\n`;\n"
        "export function d(x) { return x / 2 / 3; }\n"
        "export function e() { return [/\\//g, /[/]/]; }\n"
        "export const C = 2;\n"
        "export class K {\n  m() { return 1; }\n}\n"
    )
    full = co.full_outline(src, "a.ts")
    assert [x["name"] for x in full] == ["a", "shq", "d", "e", "C", "K"]
    assert [c["name"] for c in full[-1]["children"]] == ["m"]
    assert {x["name"] for x in co.quick_outline(src, "a.ts")} <= {
        x["name"] for x in full
    } | {"t"}


def test_jvm_full_outline_keeps_members_of_a_wrapped_or_next_line_brace_header():
    java = (
        "package a;\n\n"
        "public class AntiBot extends\n"
        "    BaseCategorizer<String, Dto> {\n"
        "  private int count;\n"
        "  public Dto fromDto(Dto d) {\n    return d;\n  }\n"
        "  enum Category {\n    A, B\n  }\n"
        "}\n"
    )
    (cls,) = co.full_outline(java, "AntiBot.java")
    assert cls["name"] == "AntiBot" and cls["end"] == 12
    assert [c["name"] for c in cls["children"]] == ["count", "fromDto", "Category"]
    cs = (
        "namespace X\n{\n    public class Svc\n    {\n"
        "        private int count;\n"
        "        public int Get(int a)\n        {\n            return a;\n        }\n"
        "    }\n}\n"
    )
    (svc,) = co.full_outline(cs, "Svc.cs")
    assert [c["name"] for c in svc["children"]] == ["count", "Get"]
    kt = "data class P(val a: Int)\nclass Q {\n    fun go(): Int { return 1 }\n}\n"
    out = co.full_outline(kt, "a.kt")
    assert [x["name"] for x in out] == ["P", "Q"]
    assert [c["name"] for c in out[1]["children"]] == ["go"]


def test_jvm_same_package_twin_in_a_sibling_module_is_not_a_dependency(tmp_path):
    body = "package com.acme.config;\n\npublic class LocalUnleashConfig {}\n"
    wt = _repo(
        tmp_path / "r",
        {
            "api/src/main/java/com/acme/config/LocalUnleashConfig.java": body,
            "public-api/src/main/java/com/acme/config/LocalUnleashConfig.java": body,
            "api/src/main/java/com/acme/config/Uses.java": (
                "package com.acme.config;\n\nclass Uses { Helper h; }\n"
            ),
            "api/src/main/java/com/acme/config/Helper.java": (
                "package com.acme.config;\n\nclass Helper {}\n"
            ),
            "public-api/src/main/java/com/acme/config/Helper.java": (
                "package com.acme.config;\n\nclass Helper {}\n"
            ),
        },
    )
    rows, _t = cm.list_files(wt, ())
    g = cm.build_graph(wt, rows)
    rels = [r[0] for r in rows]
    edges = {(rels[a], rels[b]) for a, b in (tuple(e[:2]) for e in g["edges"])}
    assert not any(
        a.split("/")[0] != b.split("/")[0] for a, b in edges
    ), edges  # nothing crosses the modules
    assert (
        "api/src/main/java/com/acme/config/Uses.java",
        "api/src/main/java/com/acme/config/Helper.java",
    ) in edges


def test_paths_with_edge_whitespace_or_a_backslash_open(tmp_path, monkeypatch):
    wt = _repo(
        tmp_path / "r",
        {
            " lead/l.py": "def lf():\n    pass\n" + _body(),
            "back\\slash/b.py": "def bf():\n    pass\n" + _body(),
            " spacefile.py": "def sf():\n    pass\n" + _body(),
            "normal/n.py": _body(),
        },
    )
    fork = _git("rev-parse", "HEAD", cwd=wt)
    monkeypatch.setattr(cm, "_server", lambda: _FakeSrv(fork))
    for f in (" lead/l.py", "back\\slash/b.py", " spacefile.py"):
        assert co.file_view(object(), wt, f)["loc"] > 0, f


def test_outside_symlinks_never_surface_in_search(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret_module.py").write_text(
        "API_TOKEN = 1\n\ndef secret_fn():\n    pass\n" + _body()
    )
    (outside / "routes.py").write_text(
        '@app.get("/hidden/admin")\ndef hidden_admin():\n    pass\n' + _body()
    )
    root = tmp_path / "r"
    root.mkdir()
    (root / "leak.py").symlink_to(outside / "secret_module.py")
    (root / "leakroutes.py").symlink_to(outside / "routes.py")
    (root / "inside.py").write_text("def ok_fn():\n    pass\n" + _body())
    (root / "link_in.py").symlink_to(root / "inside.py")
    wt = _repo(root, {})
    assert co.search(wt, "secret")["items"] == []
    assert co.search(wt, "API_TOKEN")["items"] == []
    assert co.search(wt, "hidden_admin")["items"] == []
    # A link that stays inside the worktree still counts.
    assert {i["path"] for i in co.search(wt, "ok_fn")["items"]} >= {"inside.py"}


def test_file_view_zone_flags_follow_the_realpath(tmp_path, monkeypatch):
    from backend.config import red_zones as rz

    wt = _repo(
        tmp_path / "r",
        {"secrets/config.py": "X = 1\n", "app/main.py": "Y = 1\n"},
    )
    os.symlink("../secrets/config.py", os.path.join(wt, "app/link.py"))
    fork = _git("rev-parse", "HEAD", cwd=wt)
    monkeypatch.setattr(cm, "_server", lambda: _FakeSrv(fork))
    rid = rz.repo_identity(wt)[0]
    rz.add_zone("worktree", os.path.realpath(wt), "/secrets/**", repo_id=rid)
    assert co.file_view(object(), wt, "app/link.py")["zones"]["red"] is True
    wt2 = _repo(
        tmp_path / "r2",
        {"secrets/config.py": "X = 1\n", "app/main.py": "Y = 1\n"},
    )
    os.symlink("../secrets/config.py", os.path.join(wt2, "app/link.py"))
    rid2 = rz.repo_identity(wt2)[0]
    rz.add_zone(
        "worktree", os.path.realpath(wt2), "/app/**", repo_id=rid2, kind="green"
    )
    z = co.file_view(object(), wt2, "app/link.py")["zones"]
    assert z == {"red": False, "green": False}


def test_file_view_of_an_unreadable_file_is_neutral_not_a_crash(tmp_path, monkeypatch):
    if os.geteuid() == 0:
        pytest.skip("root reads mode-000 files")
    wt = _repo(tmp_path / "r", {"ok.py": "X = 1\n", "noperm.py": "Y = 1\n"})
    fork = _git("rev-parse", "HEAD", cwd=wt)
    monkeypatch.setattr(cm, "_server", lambda: _FakeSrv(fork))
    p = os.path.join(wt, "noperm.py")
    os.chmod(p, 0)
    try:
        v = co.file_view(object(), wt, "noperm.py")
        assert v["symbols"] == []
    finally:
        os.chmod(p, 0o644)


def test_search_finds_a_route_by_the_label_the_ui_shows(tmp_path):
    wt = _repo(
        tmp_path / "r",
        {
            "app/main.py": (
                '@app.get("/api/instances")\ndef list_instances():\n    pass\n'
            )
        },
    )
    for q in ("/api/instances", "GET /api/instances", "get /api"):
        items = [i for i in co.search(wt, q)["items"] if i["kind"] == "route"]
        assert [i["name"] for i in items] == ["GET /api/instances"], q
