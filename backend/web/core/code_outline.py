"""Code Atlas: symbol outlines and the semantic drill-down the Map tab reads —
repo → directory → … → file → symbol — plus search and the entry-point list.

WHY THIS EXISTS. The v2 treemap drew every import as a line and nobody could
read it ("I'm afraid of all of the lines"). What people asked for instead was
swagger / UML / "zoom in and see imports and functions, zoom out and see
interfaces". So a level here is a list of CARDS (the children of one
directory), each carrying:

- its INTERFACE — the symbols code outside it actually imports, ranked by how
  many files import them (the "parts others use"), falling back to its
  most-used symbols inside and then to what it declares public;
- its sibling relations (``deps_out`` / ``deps_in``) and a TIER: siblings
  are layered so each row only uses the rows below it (callers on top,
  foundations at the bottom). Cycles are common in real code (lazy imports),
  so the layering is a weighted feedback-arc-set order — the lighter edge of
  each cycle is dropped and reported in ``back_edges`` ("both ways"), not
  collapsed into one giant tier;
- a ROLE: tests and non-code files are counted in a strip, never drawn as
  cards, and edges FROM tests never shape the layering (a test imports
  everything; one misread test folder would claim to "use" the whole repo).

WHERE THE FACTS COME FROM. :mod:`code_map` owns reading and resolving imports
(one memoized scan per file gives specifiers, imported NAMES and entry
points, see ``code_map.graph_detail``). This module owns what the Atlas adds
on top: per-file symbol outlines in two tiers — a cheap line-regex pass over
every file for the atlas (top-level symbols only; ~20x cheaper than
``ast.parse`` on a big Python repo) and a detailed pass (Python ``ast``,
brace-depth scanners elsewhere) only for the ONE file a file view opens.

CACHING. Every per-file result is memoized on ``(mtime_ns, size)``; the
assembled index (file list + outlines + graph) is cached per worktree on the
worktree's CONTENT fingerprint (HEAD + dirty set + their stats — see
``snapshot._worktree_fingerprint``), so an idle worktree costs one ``git
status`` per call and a level is a few milliseconds.

NEVER RAISES. Like the rest of the Map: the public functions return a
neutral value on any failure — except :func:`file_view` (and :func:`atlas`)
raising ``ValueError`` for a path that is absolute, escapes the worktree or
does not exist, so the route can answer 400 instead of an empty view that
looks like a real, empty file.
"""

from __future__ import annotations

import ast
import os
import posixpath
import re
import stat
import threading
import time
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from backend.web.core import code_map as cm

_LOCK = threading.RLock()

# Per-call budget for the read/outline pass (the graph has its own share,
# see :data:`GRAPH_SHARE_S`). Past it, un-memoized files count by size only
# and the level is ``partial``; the next call resumes from the memo.
INFO_BUDGET_S = 4.0
GRAPH_SHARE_S = 6.0
_MIN_FRESH_INFO = 64  # progress floor per call, like code_map's
MAX_NODES = 60
MAX_INTERFACE = 8
TRIVIAL_LOC = 40
MAX_ENTRY = 500
MAX_SEARCH = 200
_MAX_READ = 2 * 1024 * 1024
_FULL_MAX_BYTES = 768 * 1024  # detailed (char-level) scanners stop here
_INDEX_MAX = 6
_UNKNOWN_FP_TTL = 5.0
_PARTIAL_TTL = 1.0
_INFO_MEMO: Dict[str, tuple] = {}  # abs -> (mtime_ns, size, info)
_INFO_MEMO_MAX = 200_000
_FULL_MEMO: Dict[str, tuple] = {}  # abs -> (mtime_ns, size, outline)
_FULL_MEMO_MAX = 256
_INDEX: Dict[str, tuple] = {}  # wt -> (key, expires|None, index)
_BUILD_LOCKS: Dict[str, threading.Lock] = {}

_BINARY_EXT = frozenset(
    "png jpg jpeg gif ico webp bmp tiff pdf zip gz tgz bz2 xz 7z jar war class pcap "
    "woff woff2 ttf otf eot mp3 mp4 mov webm wav ogg parquet xlsx xls docx pptx db "
    "sqlite sqlite3 so dylib dll exe bin icns wasm pyc o a lib".split()
)

# extension -> outline language
LANG = {
    "py": "py",
    "pyi": "py",
    "ts": "js",
    "tsx": "js",
    "js": "js",
    "jsx": "js",
    "mjs": "js",
    "cjs": "js",
    "mts": "js",
    "cts": "js",
    "go": "go",
    "java": "java",
    "kt": "kotlin",
    "kts": "kotlin",
    "cs": "csharp",
    "scala": "java",
    "groovy": "java",
    "rs": "rust",
    "c": "c",
    "h": "c",
    "cc": "c",
    "cpp": "c",
    "cxx": "c",
    "hpp": "c",
    "hh": "c",
    "hxx": "c",
    "rb": "ruby",
    "php": "php",
    "swift": "swift",
    "tf": "hcl",
    "hcl": "hcl",
}
# Extensions that make a folder "code" for the role test (beyond LANG).
_CODE_EXTS = frozenset(LANG) | frozenset(
    "css scss sass less vue svelte sql sh bash zsh ps1 proto graphql lua pl r ex exs "
    "erl hs ml dart zig nim jl m mm".split()
)
_HEADER_EXTS = ("h", "hh", "hpp", "hxx")
_JVM_SRC = ("java", "kotlin", "scala", "groovy")


def _ext(rel: str) -> str:
    b = rel.rsplit("/", 1)[-1]
    return b.rsplit(".", 1)[-1].lower() if "." in b[1:] else ""


def _sig(s: str, n: int = 140) -> str:
    s = re.sub(r"\s+", " ", s or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def _sym(
    name: str,
    kind: str,
    line: int,
    end: int,
    public: bool,
    sig: str = "",
    parent: Optional[str] = None,
) -> dict:
    return {
        "name": name,
        "kind": kind,
        "line": line,
        "end": end,
        "public": bool(public),
        "sig": _sig(sig),
        "parent": parent,
        "children": [],
    }


def _count_syms(syms: Sequence[dict]) -> int:
    return sum(1 + _count_syms(x.get("children") or ()) for x in syms)


# =========================================================================== #
# Quick outlines (atlas tier): line regexes over top-level declarations
# =========================================================================== #

_PYQ_DEF = re.compile(r"(async[ \t]+)?def[ \t]+([A-Za-z_]\w*)[ \t]*\(")
_PYQ_CLASS = re.compile(r"class[ \t]+([A-Za-z_]\w*)[ \t]*(?:\(([^)\n]*)\)?)?")
_PYQ_CONST = re.compile(r"([A-Z][A-Z0-9_]*)[ \t]*(?::[^=\n]*)?=(?!=)")
_PYQ_ALL = re.compile(r"^__all__[ \t]*(?::[^=\n]*)?\+?=[ \t]*[\[(]([^\])]*)[\])]", re.M)


def _py_all(text: str) -> Optional[Set[str]]:
    names: Optional[Set[str]] = None
    for m in _PYQ_ALL.finditer(text):
        names = (names or set()) | set(re.findall(r"""["'](\w+)["']""", m.group(1)))
    return names


def _py_class_kind(bases: str) -> str:
    bl = [b.strip() for b in (bases or "").split(",")]
    if any(b in ("Protocol", "ABC", "typing.Protocol", "abc.ABC") for b in bl):
        return "interface"
    if any(b.endswith("Enum") or b.endswith("Flag") for b in bl):
        return "enum"
    if any(b in ("TypedDict", "NamedTuple") for b in bl):
        return "struct"
    return "class"


#: ``'"""'`` and ``"'''"``: a triple quote as a plain string literal.
_PY_TQ_LIT = ("'" + '"""' + "'", '"' + "'''" + '"')


def _quick_py(text: str) -> List[dict]:
    """Top-level ``def``/``class``/``UPPER_CASE`` constants, class methods as
    children. Triple-quoted strings are skipped so prose at column 0 in a
    docstring never reads as a declaration."""
    all_names = _py_all(text)

    def pub(n: str) -> bool:
        return (n in all_names) if all_names is not None else not n.startswith("_")

    syms: List[dict] = []
    cur: Optional[dict] = None
    cls: Optional[dict] = None
    cls_indent: Optional[int] = None
    last = 0
    in_str: Optional[str] = None
    for i, line in enumerate(text.split("\n"), 1):
        # A triple quote spelled as a literal in the other quote char (a
        # scanner's own ``'"""'``) opens nothing; counting it would swallow
        # the rest of the file as one string.
        probe = line.replace(_PY_TQ_LIT[0], "").replace(_PY_TQ_LIT[1], "")
        if in_str:
            if probe.count(in_str) % 2 == 1:
                in_str = None
            last = i
            continue
        stripped = line.strip()
        if not stripped:
            continue
        opened = None
        for q in ('"""', "'''"):
            if probe.count(q) % 2 == 1:
                opened = q
                break
        c0 = line[0]
        if c0 in " \t":
            if cls is not None and not stripped.startswith(("#", "@")):
                ind = len(line) - len(line.lstrip(" \t"))
                if cls_indent is None:
                    cls_indent = ind
                if ind == cls_indent:
                    m = _PYQ_DEF.match(stripped)
                    if m:
                        n = m.group(2)
                        cls["children"].append(
                            _sym(
                                n,
                                "method",
                                i,
                                i,
                                not n.startswith("_")
                                or (n.startswith("__") and n.endswith("__")),
                                stripped.rstrip(":"),
                                cls["name"],
                            )
                        )
            last = i
            if opened:
                in_str = opened
            continue
        if c0 in "#@":
            continue  # a comment / decorator belongs to what FOLLOWS it
        if c0 in ")]}":
            last = i  # the tail of a multi-line statement
            continue
        if c0 in "\"'rbfuRBFU" and (stripped[:1] in "\"'" or stripped[1:2] in "\"'"):
            last = i
            if opened:
                in_str = opened
            continue
        if cur is not None:
            cur["end"] = max(cur["line"], last)
            cur = None
            cls = None
            cls_indent = None
        m = _PYQ_DEF.match(line)
        if m:
            n = m.group(2)
            cur = _sym(n, "function", i, i, pub(n), line.rstrip(":"))
            syms.append(cur)
        else:
            m = _PYQ_CLASS.match(line)
            if m:
                n = m.group(1)
                cur = cls = _sym(
                    n, _py_class_kind(m.group(2) or ""), i, i, pub(n), line.rstrip(":")
                )
                syms.append(cur)
            else:
                m = _PYQ_CONST.match(line)
                if m and not any(x["name"] == m.group(1) for x in syms[-50:]):
                    n = m.group(1)
                    syms.append(_sym(n, "const", i, i, pub(n), n))
        last = i
        if opened:
            in_str = opened
    if cur is not None:
        cur["end"] = max(cur["line"], last)
    return syms


_JSQ_DECL = re.compile(
    r"(export[ \t]+)?(default[ \t]+)?(?:declare[ \t]+)?(?:abstract[ \t]+)?(async[ \t]+)?"
    r"(function\*?|class|interface|type|enum|const|let|var|namespace)[ \t]+"
    r"([A-Za-z_$][\w$]*)(.*)$"
)
_JSQ_EXPORT_LIST = re.compile(r"^export[ \t]*(?:type[ \t]*)?\{([^}]*)\}")
_JSQ_EXPORT_DEFAULT = re.compile(
    r"^export[ \t]+default[ \t]+([A-Za-z_$][\w$]*)[ \t]*;?[ \t]*$"
)
_JS_METHOD = re.compile(
    r"^(?:(?:public|private|protected|static|readonly|async|get|set|override|abstract|declare)\s+)*"
    r"(#?[A-Za-z_$][\w$]*)\s*[?!]?\s*(<[^>]*>)?\s*\("
)
_JS_FIELD = re.compile(
    r"^(?:(?:public|private|protected|static|readonly|override|declare)\s+)*"
    r"(#?[A-Za-z_$][\w$]*)\s*[?!]?\s*[:=]"
)
_JS_KW = frozenset(
    "if for while switch return catch function else do try new typeof super await "
    "constructor_".split()
)
_JS_ARROW = re.compile(
    r"^\s*(?::[^=]*)?=\s*(?:async\s*)?(?:\([^)]*\)|[\w$]+)\s*(?::[^=]*)?=>"
    r"|^\s*(?::[^=]*)?=\s*(?:async\s+)?function\b"
    r"|^\s*(?::[^=]*)?=\s*(?:React\.)?(?:memo|forwardRef|lazy)\s*\("
)


def _js_kind(kw: str, name: str, rest: str) -> Optional[str]:
    kind = {
        "function": "function",
        "function*": "function",
        "class": "class",
        "interface": "interface",
        "type": "type",
        "enum": "enum",
        "namespace": "module",
    }.get(kw)
    if kind:
        return kind
    if _JS_ARROW.match(rest):
        return "function"
    return "const"


def _quick_js(text: str) -> List[dict]:
    """Column-0 declarations (exported = public; non-exported functions,
    classes and Capitalized consts kept as private — local lowercase
    ``let``/``const`` are noise), ``export { a, b as c }`` / ``export default
    X`` marking, and class members at the class body's first indent."""
    syms: List[dict] = []
    byname: Dict[str, dict] = {}
    cls: Optional[dict] = None
    member_indent: Optional[int] = None
    for i, line in enumerate(text.split("\n"), 1):
        if not line:
            continue
        c0 = line[0]
        if c0 in " \t":
            if cls is not None:
                s = line.lstrip(" \t")
                ind = len(line) - len(s)
                if s.startswith(("//", "/*", "*", "@", "}")):
                    continue
                if member_indent is None:
                    member_indent = ind
                if ind == member_indent:
                    m = _JS_METHOD.match(s)
                    if m and m.group(1) not in _JS_KW:
                        n = m.group(1)
                        cls["children"].append(
                            _sym(
                                n,
                                "method",
                                i,
                                i,
                                not n.startswith(("#", "_")) and "private " not in s,
                                s.split("{")[0].strip(),
                                cls["name"],
                            )
                        )
            continue
        if c0 == "}":
            if cls is not None:
                cls["end"] = i
                cls = None
                member_indent = None
            continue
        m = _JSQ_DECL.match(line)
        if m:
            exported = bool(m.group(1))
            kw, name, rest = m.group(4), m.group(5), m.group(6)
            kind = _js_kind(kw, name, rest) or "const"
            if kind == "const" and not exported and not name[:1].isupper():
                continue
            x = _sym(name, kind, i, i, exported, line.split("{")[0].strip())
            syms.append(x)
            byname.setdefault(name, x)
            if kind in ("class", "interface") and "{" in line and "}" not in line:
                cls = x
                member_indent = None
            continue
        m = _JSQ_EXPORT_LIST.match(line)
        if m:
            for part in m.group(1).split(","):
                bits = part.strip().split()
                if bits and bits[0] == "type" and len(bits) > 1:
                    bits = bits[1:]
                if bits and bits[0] in byname:
                    byname[bits[0]]["public"] = True
            continue
        m = _JSQ_EXPORT_DEFAULT.match(line)
        if m and m.group(1) in byname:
            byname[m.group(1)]["public"] = True
    return syms


_GO_FUNC = re.compile(
    r"^func\s+(\(\s*\w*\s*\*?\s*(\w+)[^)]*\)\s*)?(\w+)\s*(\[[^\]]*\])?\s*\("
)
_GO_TYPE = re.compile(
    r"^type\s+(\w+)(\[[^\]]*\])?\s+(struct|interface|func|map|\[\]|\*?[\w.]+)"
)
_GO_TYPE_IN_BLOCK = re.compile(r"^\s+(\w+)(\[[^\]]*\])?\s+(struct|interface|\S+)")
_GO_VAR = re.compile(r"^(?:var|const)\s+(\w+)")


def _quick_go(text: str) -> List[dict]:
    """gofmt puts every top-level declaration at column 0: funcs, methods
    (attached to their receiver type), types (also in ``type ( … )``
    blocks) and exported vars/consts. Public = Capitalized."""
    syms: List[dict] = []
    types: Dict[str, dict] = {}
    methods: List[Tuple[str, dict]] = []
    block: Optional[str] = None
    cur: Optional[dict] = None
    lines = text.split("\n")
    for i, raw in enumerate(lines, 1):
        t = raw.rstrip()
        if block:
            if t.startswith(")"):
                block = None
                continue
            if block == "type":
                m = _GO_TYPE_IN_BLOCK.match(t)
                if m and m.group(1)[:1].isalpha():
                    k = {"struct": "struct", "interface": "interface"}.get(
                        m.group(3), "type"
                    )
                    x = _sym(m.group(1), k, i, i, m.group(1)[0].isupper(), t.strip())
                    types[m.group(1)] = x
                    syms.append(x)
            else:
                m = re.match(r"^\t(\w+)", t)
                if m and m.group(1)[0].isupper():
                    syms.append(_sym(m.group(1), "const", i, i, True, t.strip()))
            continue
        if not t or t[0] in " \t/":
            continue
        if cur is not None and t == "}":
            cur["end"] = i
            cur = None
            continue
        if re.match(r"^(type|var|const)\s*\($", t):
            block = t.split()[0].split("(")[0]
            continue
        m = _GO_TYPE.match(t)
        if m:
            k = {"struct": "struct", "interface": "interface"}.get(m.group(3), "type")
            x = _sym(
                m.group(1), k, i, i, m.group(1)[0].isupper(), t.rstrip("{").strip()
            )
            types[m.group(1)] = x
            syms.append(x)
            cur = x if t.endswith("{") else None
            continue
        m = _GO_FUNC.match(t)
        if m:
            recv, name = m.group(2), m.group(3)
            x = _sym(
                name,
                "method" if recv else "function",
                i,
                i,
                name[0].isupper(),
                t.rstrip("{").strip(),
                recv,
            )
            if recv:
                methods.append((recv, x))
            else:
                syms.append(x)
            cur = x if t.endswith("{") else None
            continue
        m = _GO_VAR.match(t)
        if m and m.group(1)[0].isupper():
            syms.append(_sym(m.group(1), "const", i, i, True, t.strip()))
    for recv, x in methods:
        if recv in types:
            types[recv]["children"].append(x)
        else:
            syms.append(x)
    return syms


_JV_MODS = (
    r"(?:(?:public|protected|private|static|final|abstract|sealed|non-sealed|data|open|"
    r"internal|inner|enum|annotation|value|partial|readonly|unsafe|new|override|"
    r"virtual|strictfp)\s+)*"
)
_JV_TYPE = re.compile(
    r"^\s*((?:@[\w.]+(?:\([^)]*\))?\s+)*" + _JV_MODS + r")"
    r"(class|interface|enum|record|object|@interface|struct|trait)\s+(\w+)(.*)$"
)
_JV_METHOD = re.compile(
    r"^\s*((?:@\w+(?:\([^)]*\))?\s+)*)"
    r"((?:public|protected|private|static|final|abstract|synchronized|default|native|"
    r"override|suspend|open|internal|virtual|async|sealed|new|extern|unsafe)\s+)*"
    r"(?:<[^>]+>\s+)?([\w<>\[\],.?]+(?:\s*<[^>]*>)?)\s+(\w+)\s*\("
)
_KT_FUN = re.compile(
    r"^\s*((?:public|protected|private|internal|override|suspend|open|inline|operator|"
    r"infix|abstract|final|tailrec|external)\s+)*fun\s+(?:<[^>]+>\s+)?(?:[\w.]+\.)?(\w+)\s*\("
)
_JV_NOT_METHOD = frozenset(
    "if for while switch return catch new else throw synchronized try do case".split()
)


def _jv_public(mods: str, lang: str, owner_kind: str = "") -> bool:
    if lang == "kotlin":
        return not re.search(r"\b(private|internal)\b", mods or "")
    if owner_kind == "interface":
        return "private" not in (mods or "")
    return "public" in (mods or "")


def _quick_jvm(text: str, lang: str) -> List[dict]:
    """Types declared at column 0 (C#: ≤ 4, inside a namespace) and their
    members at the body's first indent — methods (Java/C# ``type name(``,
    Kotlin ``fun name(``) and nested types."""
    syms: List[dict] = []
    top_indent = 4 if lang == "csharp" else 0
    cur: Optional[dict] = None
    member_indent: Optional[int] = None
    depth_start = 0
    for i, line in enumerate(text.split("\n"), 1):
        s = line.lstrip(" \t")
        if not s or s.startswith(("//", "/*", "*", "import ", "package ", "using ")):
            continue
        ind = len(line) - len(s)
        if cur is not None and ind <= depth_start and s.startswith("}"):
            cur["end"] = i
            cur = None
            member_indent = None
            continue
        if cur is None or ind <= depth_start:
            if ind <= top_indent:
                m = _JV_TYPE.match(line)
                if m:
                    kw = m.group(2)
                    kind = {
                        "class": "class",
                        "interface": "interface",
                        "trait": "trait",
                        "enum": "enum",
                        "record": "struct",
                        "struct": "struct",
                        "object": "class",
                        "@interface": "type",
                    }[kw]
                    cur = _sym(
                        m.group(3),
                        kind,
                        i,
                        i,
                        _jv_public(m.group(1), lang),
                        (
                            kw + " " + m.group(3) + " " + m.group(4).split("{")[0]
                        ).strip(),
                    )
                    syms.append(cur)
                    depth_start = ind
                    member_indent = None
                    continue
                if lang == "kotlin" and ind == 0:
                    km = _KT_FUN.match(line)
                    if km:
                        syms.append(
                            _sym(
                                km.group(2),
                                "function",
                                i,
                                i,
                                _jv_public(km.group(1) or "", lang),
                                s.split("{")[0].split("=")[0].strip(),
                            )
                        )
            continue
        if s.startswith(("@", "}", ")")):
            continue
        if member_indent is None:
            member_indent = ind
        if ind != member_indent:
            continue
        tm = _JV_TYPE.match(line)
        if tm:
            cur["children"].append(
                _sym(
                    tm.group(3),
                    "class" if tm.group(2) != "interface" else "interface",
                    i,
                    i,
                    _jv_public(tm.group(1), lang, cur["kind"]),
                    s.split("{")[0].strip(),
                    cur["name"],
                )
            )
            continue
        name = None
        mods = ""
        if lang == "kotlin":
            km = _KT_FUN.match(line)
            if km:
                name, mods = km.group(2), km.group(1) or ""
        else:
            jm = _JV_METHOD.match(line)
            if (
                jm
                and jm.group(4) not in _JV_NOT_METHOD
                and jm.group(3).split()[-1] not in _JV_NOT_METHOD
                and "=" not in line.split("(")[0]
            ):
                name, mods = jm.group(4), jm.group(2) or ""
            elif re.match(
                r"^\s*(?:public|protected|private)?\s*"
                + re.escape(cur["name"])
                + r"\s*\(",
                line,
            ):
                name, mods = cur["name"], s
        if name:
            cur["children"].append(
                _sym(
                    name,
                    "method",
                    i,
                    i,
                    _jv_public(mods, lang, cur["kind"]),
                    s.split("{")[0].strip(),
                    cur["name"],
                )
            )
    return syms


_RS_ITEM = re.compile(
    r"^(pub(?:\([\w:\s]+\))?\s+)?(?:(?:async|unsafe|const|extern(?:\s+\"C\")?|default)\s+)*"
    r"(fn|struct|enum|trait|mod|type|union|static|const)\s+([A-Za-z_]\w*)"
)
_RS_IMPL = re.compile(r"^impl\b(?:\s*<[^{]*?>)?\s+(?:[\w:<>, ]+?\s+for\s+)?([\w:]+)")
_RS_METHOD = re.compile(
    r"^\s+(pub(?:\([\w:\s]+\))?\s+)?(?:(?:async|unsafe|const|default)\s+)*fn\s+(\w+)"
)


def _quick_rust(text: str) -> List[dict]:
    """Column-0 items (``pub`` = public) and ``impl`` blocks' methods,
    attached to the implemented type."""
    syms: List[dict] = []
    byname: Dict[str, dict] = {}
    impl_of: Optional[str] = None
    pending: List[Tuple[str, dict]] = []
    for i, line in enumerate(text.split("\n"), 1):
        if not line or line.startswith(("//", "#", " ", "\t", "}")):
            if impl_of and line.startswith((" ", "\t")):
                m = _RS_METHOD.match(line)
                if m:
                    pending.append(
                        (
                            impl_of,
                            _sym(
                                m.group(2),
                                "method",
                                i,
                                i,
                                bool(m.group(1)),
                                line.strip().split("{")[0],
                                impl_of,
                            ),
                        )
                    )
            elif line.startswith("}"):
                impl_of = None
            continue
        m = _RS_IMPL.match(line)
        if m:
            impl_of = m.group(1).split("::")[-1]
            continue
        m = _RS_ITEM.match(line)
        if m:
            kind = {
                "fn": "function",
                "struct": "struct",
                "enum": "enum",
                "trait": "trait",
                "mod": "module",
                "type": "type",
                "union": "struct",
                "static": "const",
                "const": "const",
            }[m.group(2)]
            x = _sym(
                m.group(3), kind, i, i, bool(m.group(1)), line.split("{")[0].strip()
            )
            syms.append(x)
            byname.setdefault(m.group(3), x)
    for owner, x in pending:
        if owner in byname:
            byname[owner]["children"].append(x)
    return syms


_C_FUNC = re.compile(
    r"^(?!(?:if|for|while|switch|return|else|do|case|typedef|struct|enum|union)\b)"
    r"([A-Za-z_][\w\s\*&:<>,]*?[\s\*&])(~?[A-Za-z_][\w:]*)\s*\(([^;{]*)\)?\s*(?:const)?\s*\{?\s*$"
)
_C_PROTO = re.compile(
    r"^(?!(?:if|for|while|switch|return|else|do|case|typedef)\b)"
    r"([A-Za-z_][\w\s\*&:<>,]*?[\s\*&])(~?[A-Za-z_][\w:]*)\s*\(([^;{]*)\)\s*;"
)
_C_TYPE = re.compile(r"^(typedef\s+)?(struct|enum|union|class)\s+(\w+)\s*(\{|;|:|$)")
_C_TYPEDEF_END = re.compile(r"^\}\s*(\w+)\s*;")
_C_DEFINE = re.compile(r"^#\s*define\s+([A-Za-z_]\w*)")


def _quick_c(text: str, rel: str) -> List[dict]:
    """Column-0 function definitions (GNU two-line style included), header
    prototypes, struct/enum/union/class and typedef names, header macros.
    Public = declared in a header (a ``.c`` function is re-marked public at
    index time when some header declares it)."""
    is_h = _ext(rel) in _HEADER_EXTS
    syms: List[dict] = []
    prev = ""
    in_comment = False
    for i, raw in enumerate(text.split("\n"), 1):
        t = raw.rstrip()
        if in_comment:
            if "*/" in t:
                in_comment = False
            prev = ""
            continue
        if t.startswith("/*") and "*/" not in t:
            in_comment = True
            continue
        if not t or t[0] in " \t{}/*":
            prev = ""
            continue
        if t.startswith("#"):
            m = _C_DEFINE.match(t)
            if m and is_h:
                syms.append(_sym(m.group(1), "const", i, i, True, t))
            prev = ""
            continue
        m = _C_TYPE.match(t)
        if m:
            k = {
                "struct": "struct",
                "enum": "enum",
                "union": "struct",
                "class": "class",
            }[m.group(2)]
            syms.append(_sym(m.group(3), k, i, i, is_h, t.strip()))
            prev = ""
            continue
        m = _C_TYPEDEF_END.match(t)
        if m:
            syms.append(_sym(m.group(1), "type", i, i, is_h, "typedef " + m.group(1)))
            prev = ""
            continue
        line = (prev + " " + t).strip() if prev else t
        m = _C_PROTO.match(line)
        if m:
            if is_h:
                syms.append(_sym(m.group(2), "function", i, i, True, line))
            prev = ""
            continue
        m = _C_FUNC.match(line)
        if m and not t.endswith(";"):
            static = "static" in m.group(1).split()
            syms.append(
                _sym(
                    m.group(2), "function", i, i, is_h and not static, line.rstrip("{ ")
                )
            )
            prev = ""
            continue
        prev = t if re.match(r"^[A-Za-z_][\w\s\*]*$", t) and not t.endswith(";") else ""
    return syms


def _quick_ruby(text: str) -> List[dict]:
    syms = []
    for i, line in enumerate(text.split("\n"), 1):
        m = re.match(r"^(\s*)(class|module|def)\s+(self\.)?([\w:?!=]+)", line)
        if m and len(m.group(1)) <= 4:
            kind = {"class": "class", "module": "module", "def": "function"}[m.group(2)]
            syms.append(_sym(m.group(4), kind, i, i, True, line.strip()))
    return syms


def _quick_php(text: str) -> List[dict]:
    syms = []
    cur = None
    for i, line in enumerate(text.split("\n"), 1):
        m = re.match(
            r"^\s*(?:abstract\s+|final\s+|readonly\s+)*(class|interface|trait|enum)\s+(\w+)",
            line,
        )
        if m:
            cur = _sym(
                m.group(2),
                m.group(1) if m.group(1) != "trait" else "trait",
                i,
                i,
                True,
                line.strip(),
            )
            syms.append(cur)
            continue
        m = re.match(
            r"^(\s*)((?:public|protected|private|static|abstract|final)\s+)*function\s+&?(\w+)",
            line,
        )
        if m:
            x = _sym(
                m.group(3),
                "method" if (cur and m.group(1)) else "function",
                i,
                i,
                not re.search(r"\b(private|protected)\b", m.group(0)),
                line.strip().split("{")[0],
                cur["name"] if (cur and m.group(1)) else None,
            )
            (cur["children"] if (cur and m.group(1)) else syms).append(x)
    return syms


def _quick_swift(text: str) -> List[dict]:
    syms = []
    for i, line in enumerate(text.split("\n"), 1):
        m = re.match(
            r"^(\s*)(?:@\w+\s+)*(?:(public|open|private|fileprivate|internal)\s+)?(?:final\s+)?"
            r"(class|struct|protocol|enum|extension|func|actor)\s+(\w+)",
            line,
        )
        if m and len(m.group(1)) <= 4:
            kind = {
                "protocol": "interface",
                "extension": "type",
                "func": "function",
                "actor": "class",
            }.get(m.group(3), m.group(3))
            syms.append(
                _sym(
                    m.group(4),
                    kind,
                    i,
                    i,
                    m.group(2) not in ("private", "fileprivate"),
                    line.strip().split("{")[0],
                )
            )
    return syms


def _quick_hcl(text: str) -> List[dict]:
    """A Terraform module's interface is its inputs and outputs: ``variable``
    and ``output`` blocks are the public symbols; resources/data/modules are
    listed as private implementation."""
    syms = []
    for i, line in enumerate(text.split("\n"), 1):
        m = re.match(
            r'^(resource|data|module|variable|output|provider)\s+"([^"]+)"(?:\s+"([^"]+)")?',
            line,
        )
        if m:
            k = {
                "variable": "const",
                "output": "const",
                "module": "module",
                "provider": "module",
            }.get(m.group(1), "type")
            syms.append(
                _sym(
                    m.group(3) or m.group(2),
                    k,
                    i,
                    i,
                    m.group(1) in ("variable", "output"),
                    line.strip().rstrip("{").strip(),
                )
            )
    return syms


_LONG_LINE_RE = re.compile(r"^[^\n]{%d,}$" % 2000, re.M)


def _blank_long_lines(text: str) -> str:
    """Lines past 2000 chars blanked (line numbers kept): declarations are
    never that long, and the per-line regexes must stay linear on a
    minified bundle or a generated one-line blob."""
    return _LONG_LINE_RE.sub("", text)


def quick_outline(text: str, rel: str) -> List[dict]:
    """Top-level symbols of one file by line regexes (the atlas tier).
    ``[]`` for languages without an extractor or on any failure."""
    lang = LANG.get(_ext(rel))
    text = _blank_long_lines(text)
    try:
        if lang == "py":
            return _quick_py(text)
        if lang == "js":
            return _quick_js(text)
        if lang == "go":
            return _quick_go(text)
        if lang in ("java", "kotlin", "csharp"):
            return _quick_jvm(text, lang)
        if lang == "rust":
            return _quick_rust(text)
        if lang == "c":
            return _quick_c(text, rel)
        if lang == "ruby":
            return _quick_ruby(text)
        if lang == "php":
            return _quick_php(text)
        if lang == "swift":
            return _quick_swift(text)
        if lang == "hcl":
            return _quick_hcl(text)
    except Exception:  # noqa: BLE001 — one odd file, not the atlas
        return []
    return []


# =========================================================================== #
# Detailed outlines (file-view tier)
# =========================================================================== #


def _py_full(text: str) -> Optional[List[dict]]:
    """``ast`` outline: classes (bases; Protocol/ABC → interface, Enum →
    enum), their FIELDS (annotated / dataclass attributes and enum members —
    what makes a model read like a database table) and methods with full
    signatures; functions; UPPER_CASE constants. ``__all__`` decides public
    when present. None on a syntax error (the caller falls back to the quick
    pass)."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return None
    all_names = _py_all(text)

    def pub(n: str) -> bool:
        return (n in all_names) if all_names is not None else not n.startswith("_")

    def unparse(node: Any) -> str:
        try:
            return ast.unparse(node)
        except Exception:  # noqa: BLE001
            return "…"

    def fsig(fn: Any) -> str:
        r = (" -> " + unparse(fn.returns)) if fn.returns is not None else ""
        pre = "async " if isinstance(fn, ast.AsyncFunctionDef) else ""
        return pre + fn.name + "(" + unparse(fn.args) + ")" + r

    syms: List[dict] = []
    seen_const: Set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            syms.append(
                _sym(
                    node.name,
                    "function",
                    node.lineno,
                    node.end_lineno or node.lineno,
                    pub(node.name),
                    fsig(node),
                )
            )
        elif isinstance(node, ast.ClassDef):
            bases = [unparse(b) for b in node.bases]
            kind = _py_class_kind(", ".join(bases))
            c = _sym(
                node.name,
                kind,
                node.lineno,
                node.end_lineno or node.lineno,
                pub(node.name),
                node.name + ("(" + ", ".join(bases) + ")" if bases else ""),
            )
            for m in node.body:
                if isinstance(m, ast.AnnAssign) and isinstance(m.target, ast.Name):
                    n = m.target.id
                    ann = unparse(m.annotation)
                    c["children"].append(
                        _sym(
                            n,
                            "field",
                            m.lineno,
                            m.end_lineno or m.lineno,
                            not n.startswith("_"),
                            n + ": " + ann,
                            node.name,
                        )
                    )
                elif isinstance(m, ast.Assign) and kind == "enum":
                    for t in m.targets:
                        if isinstance(t, ast.Name):
                            c["children"].append(
                                _sym(
                                    t.id,
                                    "field",
                                    m.lineno,
                                    m.end_lineno or m.lineno,
                                    not t.id.startswith("_"),
                                    t.id + " = " + unparse(m.value)[:60],
                                    node.name,
                                )
                            )
                elif isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    n = m.name
                    c["children"].append(
                        _sym(
                            n,
                            "method",
                            m.lineno,
                            m.end_lineno or m.lineno,
                            not n.startswith("_")
                            or (n.startswith("__") and n.endswith("__")),
                            fsig(m),
                            node.name,
                        )
                    )
                elif isinstance(m, ast.ClassDef):
                    c["children"].append(
                        _sym(
                            m.name,
                            "class",
                            m.lineno,
                            m.end_lineno or m.lineno,
                            not m.name.startswith("_"),
                            m.name,
                            node.name,
                        )
                    )
            syms.append(c)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                if (
                    isinstance(t, ast.Name)
                    and re.match(r"^[A-Z][A-Z0-9_]*$", t.id)
                    and t.id not in seen_const
                ):
                    seen_const.add(t.id)
                    syms.append(
                        _sym(
                            t.id,
                            "const",
                            node.lineno,
                            node.end_lineno or node.lineno,
                            pub(t.id),
                            t.id,
                        )
                    )
    return syms


_STR_CHARS = "\"'`"


_RE_PRE_CHARS = frozenset("(,=:[!&|?{};+-*%~^")
_RE_PRE_WORDS = frozenset(
    "return typeof case in of void delete throw new yield await else do".split()
)


def _js_regex_end(line: str, i: int, out: List[str]) -> int:
    """When the ``/`` at ``line[i]`` opens a JS regex literal (it stands in
    EXPRESSION position: line start, after an operator/opening punctuation or
    a keyword like ``return``), the index just past its closing ``/`` — else
    -1 (a division). Character classes may hold a bare ``/``."""
    prev = "".join(out).rstrip()
    if prev:
        last = prev[-1]
        if last not in _RE_PRE_CHARS:
            m = re.search(r"([A-Za-z_$][\w$]*)$", prev)
            if not m or m.group(1) not in _RE_PRE_WORDS:
                return -1
    j, n, in_class = i + 1, len(line), False
    while j < n:
        ch = line[j]
        if ch == "\\":
            j += 2
            continue
        if in_class:
            if ch == "]":
                in_class = False
        elif ch == "[":
            in_class = True
        elif ch == "/":
            return j + 1
        j += 1
    return -1


def _strip_line(line: str, state: dict) -> str:
    """One line with comments and string contents removed (``state["block"]``
    carries an open ``/* */`` across lines) — so braces inside strings and
    comments never move the depth. With ``state["js"]``, regex literals
    (``/\\s*\\{?$/``, ``/'/g``) are blanked too and a backtick template
    literal carries across lines (``state["tpl"]``): either one used to open
    a phantom brace or string that hid every later declaration."""
    out = []
    i, n = 0, len(line)
    js = state.get("js")
    while i < n:
        if state["block"]:
            j = line.find("*/", i)
            if j < 0:
                return "".join(out)
            state["block"] = False
            i = j + 2
            continue
        if state.get("tpl"):
            j = i
            while j < n and line[j] != "`":
                j += 2 if line[j] == "\\" else 1
            if j >= n:
                return "".join(out)
            state["tpl"] = False
            out.append("``")
            i = j + 1
            continue
        c = line[i]
        if c == "/" and line.startswith("//", i):
            break
        if c == "/" and line.startswith("/*", i):
            state["block"] = True
            i += 2
            continue
        if c == "/" and js:
            end = _js_regex_end(line, i, out)
            if end > 0:
                out.append("/./")
                i = end
                continue
        if c in _STR_CHARS:
            j = i + 1
            while j < n and line[j] != c:
                j += 2 if line[j] == "\\" else 1
            if j >= n and c == "`" and js:
                state["tpl"] = True  # a template literal spanning lines
                out.append("``")
                return "".join(out)
            out.append(c + c)
            i = j + 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _brace_lines(text: str, js: bool = False) -> List[Tuple[int, str, str, int]]:
    """``[(lineno, raw, stripped, depth_at_start)]``."""
    st = {"block": False, "js": js, "tpl": False}
    depth = 0
    out = []
    for i, raw in enumerate(text.split("\n"), 1):
        s = _strip_line(raw, st)
        out.append((i, raw, s, depth))
        depth = max(0, depth + s.count("{") - s.count("}"))
    return out


def _js_full(text: str) -> List[dict]:
    """Brace-depth JS/TS outline: depth-0 declarations and class members at
    depth 1 (methods and fields), with real ``end`` lines."""
    syms: List[dict] = []
    byname: Dict[str, dict] = {}
    stack: List[Tuple[dict, int]] = []
    for ln, raw, s, depth in _brace_lines(text, js=True):
        while stack and depth < stack[-1][1]:
            stack[-1][0]["end"] = ln - 1  # the line that closed it
            stack.pop()
        t = s.strip()
        if not t:
            continue
        if depth == 0:
            m = _JSQ_DECL.match(t)
            if m:
                exported = bool(m.group(1))
                kw, name, rest = m.group(4), m.group(5), m.group(6)
                kind = _js_kind(kw, name, rest) or "const"
                if kind == "const" and not exported and not name[:1].isupper():
                    continue
                x = _sym(name, kind, ln, ln, exported, raw.strip().split("{")[0])
                syms.append(x)
                byname.setdefault(name, x)
                if s.count("{") > s.count("}"):
                    stack.append((x, 1))
                continue
            m = _JSQ_EXPORT_LIST.match(t)
            if m:
                for part in m.group(1).split(","):
                    bits = part.strip().split()
                    if bits and bits[0] == "type" and len(bits) > 1:
                        bits = bits[1:]
                    if bits and bits[0] in byname:
                        byname[bits[0]]["public"] = True
                continue
            m = _JSQ_EXPORT_DEFAULT.match(t)
            if m and m.group(1) in byname:
                byname[m.group(1)]["public"] = True
        elif depth == 1 and stack and stack[-1][0]["kind"] in ("class", "interface"):
            c = stack[-1][0]
            m = _JS_METHOD.match(t)
            kind = "method"
            if not m or m.group(1) in _JS_KW:
                m = _JS_FIELD.match(t)
                kind = "field"
            if m and m.group(1) not in _JS_KW:
                n = m.group(1)
                c["children"].append(
                    _sym(
                        n,
                        kind,
                        ln,
                        ln,
                        not n.startswith(("#", "_")) and "private " not in t,
                        raw.strip().split("{")[0].rstrip(";"),
                        c["name"],
                    )
                )
    return syms


def _join_parens(
    lines: List[Tuple[int, str, str, int]], k: int, limit: int = 15
) -> str:
    """Line ``k``'s stripped text joined with the following lines until its
    parentheses balance — a Java signature split across lines
    (``getAll(\\n  @RequestParam …)``) otherwise loses its parameters."""
    s = lines[k][2]
    j = k
    while s.count("(") > s.count(")") and j + 1 < len(lines) and j - k < limit:
        j += 1
        s += " " + lines[j][2].strip()
    return s


def _jvm_full(text: str, lang: str) -> List[dict]:
    """Brace-depth Java/Kotlin/C# outline: types at any depth (nested types
    become children), methods / constructors / Kotlin ``fun`` at each type's
    body depth with multi-line signatures joined, and fields."""
    syms: List[dict] = []
    stack: List[Tuple[dict, int]] = []
    lines = _brace_lines(text)
    # A type header whose ``{`` is on a LATER line (``class X extends`` /
    # ``    Base<…> {``, C#'s brace-on-its-own-line) is PENDING: its body
    # opens only when a line raises the depth. Popping it at once (the next
    # line is still at the header's depth) orphaned every member.
    pending: Optional[list] = None  # [sym, header depth, header k, text]
    for k, (ln, raw, s, depth) in enumerate(lines):
        t = s.strip()
        if pending is not None:
            px, hd, k0, acc = pending
            if depth > hd:
                stack.append((px, hd + 1))  # the body opened
                pending = None
            elif depth == hd and k - k0 <= 15 and t:
                cont = (
                    acc.count("(") > acc.count(")")
                    or t.startswith(
                        ("{", "extends", "implements", "permits", ":", "where", ",")
                    )
                    or t.startswith(")")
                    or re.search(r"(?:extends|implements|permits|where|[,:<&])$", acc)
                )
                if cont:
                    pending[3] = acc + " " + t
                    continue
                pending = None  # no body (`data class X(…)`, `record R(…);`)
            elif not t and depth == hd:
                continue
            else:
                pending = None
        while stack and depth < stack[-1][1]:
            stack[-1][0]["end"] = ln - 1  # the line that closed it
            stack.pop()
        if not t or t.startswith(("import ", "package ", "using ")):
            continue
        if t.startswith("@") and _JV_TYPE.match(t) is None:
            continue
        m = _JV_TYPE.match(t)
        if m and depth <= (stack[-1][1] if stack else depth):
            kw = m.group(2)
            kind = {
                "class": "class",
                "interface": "interface",
                "trait": "trait",
                "enum": "enum",
                "record": "struct",
                "struct": "struct",
                "object": "class",
                "@interface": "type",
            }[kw]
            owner = stack[-1][0] if stack else None
            x = _sym(
                m.group(3),
                kind,
                ln,
                ln,
                _jv_public(m.group(1), lang, owner["kind"] if owner else ""),
                (kw + " " + m.group(3) + " " + m.group(4).split("{")[0]).strip(),
                owner["name"] if owner else None,
            )
            (owner["children"] if owner else syms).append(x)
            if "{" in s or t.endswith(";"):
                stack.append((x, depth + 1))
            else:
                pending = [x, depth, k, t]
            continue
        if lang == "kotlin" and depth == 0:
            km = _KT_FUN.match(t)
            if km:
                syms.append(
                    _sym(
                        km.group(2),
                        "function",
                        ln,
                        ln,
                        _jv_public(km.group(1) or "", lang),
                        _join_parens(lines, k).split("{")[0].split("=")[0].strip(),
                    )
                )
            continue
        if not stack or depth != stack[-1][1]:
            continue
        owner = stack[-1][0]
        joined = _join_parens(lines, k) if t.count("(") > t.count(")") else t
        name = None
        mods = ""
        if lang == "kotlin":
            km = _KT_FUN.match(joined)
            if km:
                name, mods = km.group(2), km.group(1) or ""
        else:
            jm = _JV_METHOD.match(joined)
            if (
                jm
                and jm.group(4) not in _JV_NOT_METHOD
                and jm.group(3).split()[-1] not in _JV_NOT_METHOD
                and "=" not in joined.split("(")[0]
            ):
                name, mods = jm.group(4), jm.group(2) or ""
            elif re.match(
                r"^\s*(?:public|protected|private)?\s*"
                + re.escape(owner["name"])
                + r"\s*\(",
                joined,
            ):
                name, mods = owner["name"], joined
        if name:
            sig = raw.strip() if joined == t else joined
            owner["children"].append(
                _sym(
                    name,
                    "method",
                    ln,
                    ln,
                    _jv_public(mods, lang, owner["kind"]),
                    sig.split("{")[0].strip(),
                    owner["name"],
                )
            )
            continue
        fm = re.match(
            r"^((?:public|protected|private|static|final|readonly|const|val|var|lateinit|override|internal)\s+)*"
            r"(?:([\w<>\[\],.? ]+?)\s+)?(\w+)\s*(?::\s*([\w<>\[\],.? ]+))?\s*(?:=|;)",
            t,
        )
        if (
            fm
            and fm.group(3) not in _JV_NOT_METHOD
            and (fm.group(2) or fm.group(4) or fm.group(1))
        ):
            typ = (fm.group(4) or fm.group(2) or "").strip()
            owner["children"].append(
                _sym(
                    fm.group(3),
                    "field",
                    ln,
                    ln,
                    _jv_public(fm.group(1) or "", lang, owner["kind"]),
                    fm.group(3) + (": " + typ if typ else ""),
                    owner["name"],
                )
            )
    return syms


def _rust_full(text: str) -> List[dict]:
    """Brace-depth Rust outline: depth-0 items and every ``impl`` block's
    methods attached to the type (``impl Trait for Type`` included)."""
    syms: List[dict] = []
    impls: Dict[str, List[dict]] = {}
    stack: List[Tuple[Any, int]] = []
    for ln, raw, s, depth in _brace_lines(text):
        while stack and depth < stack[-1][1]:
            stack.pop()
        t = s.strip()
        if not t:
            continue
        if depth == 0:
            m = _RS_IMPL.match(t)
            if m:
                name = m.group(1).split("::")[-1]
                stack.append((impls.setdefault(name, []), 1))
                continue
            m = _RS_ITEM.match(t)
            if m:
                kind = {
                    "fn": "function",
                    "struct": "struct",
                    "enum": "enum",
                    "trait": "trait",
                    "mod": "module",
                    "type": "type",
                    "union": "struct",
                    "static": "const",
                    "const": "const",
                }[m.group(2)]
                syms.append(
                    _sym(
                        m.group(3),
                        kind,
                        ln,
                        ln,
                        bool(m.group(1)),
                        raw.strip().split("{")[0],
                    )
                )
        elif stack and depth == stack[-1][1]:
            m = _RS_METHOD.match(" " + t)
            if m:
                stack[-1][0].append(
                    _sym(
                        m.group(2),
                        "method",
                        ln,
                        ln,
                        bool(m.group(1)),
                        raw.strip().split("{")[0],
                    )
                )
    byname = {x["name"]: x for x in syms}
    for k, ms in impls.items():
        if k in byname:
            for x in ms:
                x["parent"] = k
            byname[k]["children"].extend(ms)
    return syms


def full_outline(text: str, rel: str) -> List[dict]:
    """The detailed outline of one file (file-view tier); falls back to
    :func:`quick_outline` for languages without a detailed extractor, for a
    Python file that does not parse, and past :data:`_FULL_MAX_BYTES` for the
    char-level scanners (a minified bundle must not stall the view)."""
    lang = LANG.get(_ext(rel))
    try:
        if lang != "py":
            text = _blank_long_lines(text)
        if lang == "py":
            got = _py_full(text)
            if got is not None:
                return got
        elif len(text) <= _FULL_MAX_BYTES:
            if lang == "js":
                return _js_full(text)
            if lang in ("java", "kotlin", "csharp"):
                return _jvm_full(text, lang)
            if lang == "rust":
                return _rust_full(text)
    except Exception:  # noqa: BLE001
        pass
    return quick_outline(text, rel)


# =========================================================================== #
# Per-file info (line count + quick outline), memoized
# =========================================================================== #


def _file_info(abs_path: str, rel: str, over: bool, reads: List[int]) -> Optional[dict]:
    """``{"loc", "syms"}`` for one file, memoized on ``(mtime_ns, size)``.
    ``None`` = not memoized and the call is over budget. Binary files (by
    extension or a NUL in the first 8 KB) count 0 lines; a file past
    :data:`_MAX_READ` has its line count extrapolated from the prefix read.
    Only REGULAR files are opened (``code_map._open_regular``: a symlinked
    FIFO must not park the worker), and never a symlink resolving OUTSIDE
    the worktree (``code_map.link_escapes`` — the rule the file view
    applies: its names would surface on cards and in search)."""
    if cm.link_escapes(abs_path, rel):
        return {"loc": 0, "syms": []}
    try:
        st = os.stat(abs_path)
    except OSError:
        return {"loc": 0, "syms": []}
    if not stat.S_ISREG(st.st_mode):
        return {"loc": 0, "syms": []}
    with _LOCK:
        memo = _INFO_MEMO.get(abs_path)
    if memo is not None and memo[0] == st.st_mtime_ns and memo[1] == st.st_size:
        return memo[2]
    if over:
        return None
    reads[0] += 1
    ext = _ext(rel)
    info: dict = {"loc": 0, "syms": []}
    if ext not in _BINARY_EXT:
        try:
            fh = cm._open_regular(abs_path)
            data = b""
            if fh is not None:
                with fh:
                    data = fh.read(_MAX_READ)
        except OSError:
            data = b""
        if data and b"\0" not in data[:8192]:
            loc = data.count(b"\n") + (0 if data.endswith(b"\n") else 1)
            if st.st_size > len(data):
                loc = int(loc * st.st_size / max(1, len(data)))
            info["loc"] = loc
            if ext in LANG:
                info["syms"] = quick_outline(data.decode("utf-8", "replace"), rel)
    with _LOCK:
        if len(_INFO_MEMO) >= _INFO_MEMO_MAX:
            _INFO_MEMO.clear()
        _INFO_MEMO[abs_path] = (st.st_mtime_ns, st.st_size, info)
    return info


def _read_text(abs_path: str) -> str:
    fh = cm._open_regular(abs_path)
    if fh is None:
        return ""
    with fh:
        data = fh.read(_MAX_READ)
    if b"\0" in data[:8192]:
        return ""
    return data.decode("utf-8", "replace")


def _full_for(abs_path: str, rel: str) -> Tuple[str, List[dict]]:
    """``(text, detailed outline)`` for the file view, the outline memoized
    on ``(mtime_ns, size)``."""
    try:
        st = os.stat(abs_path)
        text = _read_text(abs_path)
    except OSError:
        return "", []  # unreadable (mode 000, EIO): a neutral view, never a 500
    with _LOCK:
        memo = _FULL_MEMO.get(abs_path)
    if memo is not None and memo[0] == st.st_mtime_ns and memo[1] == st.st_size:
        return text, memo[2]
    syms = full_outline(text, rel) if _ext(rel) in LANG else []
    with _LOCK:
        if len(_FULL_MEMO) >= _FULL_MEMO_MAX:
            _FULL_MEMO.pop(next(iter(_FULL_MEMO)))
        _FULL_MEMO[abs_path] = (st.st_mtime_ns, st.st_size, syms)
    return text, syms


def _copy_syms(syms: Sequence[dict]) -> List[dict]:
    return [dict(x, children=_copy_syms(x.get("children") or ())) for x in syms]


# =========================================================================== #
# The index: file list + outlines + graph for one worktree state
# =========================================================================== #


def _content_fp(wt: str) -> Optional[str]:
    """The worktree's CONTENT fingerprint (HEAD + dirty paths + their stats).
    Unlike the session fingerprint it does not depend on a fork point, so
    :func:`search` / :func:`entry_points` (which have no session) and
    :func:`atlas` share one cached index."""
    try:
        from backend.web.core import snapshot

        return snapshot._worktree_fingerprint(wt, "code-outline")
    except Exception:  # noqa: BLE001
        return None


def _build_lock(wt: str) -> threading.Lock:
    with _LOCK:
        lk = _BUILD_LOCKS.get(wt)
        if lk is None:
            lk = _BUILD_LOCKS[wt] = threading.Lock()
            while len(_BUILD_LOCKS) > 4 * _INDEX_MAX:
                _BUILD_LOCKS.pop(next(iter(_BUILD_LOCKS)))
        return lk


def _cached_index(wt: str, key: Optional[str]) -> Optional[dict]:
    now = time.time()
    with _LOCK:
        hit = _INDEX.get(wt)
    if hit is None:
        return None
    hkey, expires, ix = hit
    if expires is not None and expires <= now:
        return None
    # Exact key match, None included: an index built under a real
    # fingerprint never expires, so reusing it once the fingerprint turns
    # unknown (>5000 dirty paths) served a frozen snapshot forever. An index
    # built under None carries the short _UNKNOWN_FP_TTL.
    if hkey == key:
        return ix
    return None


def _index(wt: str) -> dict:
    """The (cached) index for ``wt``'s current content. Builds are serialized
    per worktree: the Map fires the level, the entry-point count and a search
    together on open, and three cold builds of one repo would triple the
    wait instead of sharing it."""
    key = _content_fp(wt)
    ix = _cached_index(wt, key)
    if ix is not None:
        return ix
    with _build_lock(wt):
        ix = _cached_index(wt, key)
        if ix is not None:
            return ix
        ix = _build_index(wt, key)
        now = time.time()
        if ix["partial"]:
            expires: Optional[float] = now + _PARTIAL_TTL
        elif key is None:
            expires = now + _UNKNOWN_FP_TTL
        else:
            expires = None
        with _LOCK:
            _INDEX.pop(wt, None)
            _INDEX[wt] = (key, expires, ix)
            while len(_INDEX) > _INDEX_MAX:
                _INDEX.pop(next(iter(_INDEX)))
        return ix


def _build_index(wt: str, key: Optional[str]) -> dict:
    rows, truncated = cm.list_files(wt, (), fp=key)
    rels = [str(r[0]) for r in rows]
    flags = {str(r[0]): int(r[2]) for r in rows}
    sizes = {str(r[0]): int(r[1]) for r in rows}
    info: Dict[str, dict] = {}
    deadline = time.monotonic() + INFO_BUDGET_S
    reads = [0]
    partial = False
    for rel in rels:
        over = reads[0] >= _MIN_FRESH_INFO and time.monotonic() > deadline
        inf = _file_info(os.path.join(wt, rel), rel, over, reads)
        if inf is None:
            partial = True
            inf = {"loc": sizes.get(rel, 0) // 40, "syms": []}
        info[rel] = inf
    g = cm.graph_detail(wt, rows, fp=key, budget=GRAPH_SHARE_S)
    partial = partial or bool(g.get("partial"))
    n = len(rels)
    edges: List[Tuple[str, str]] = []
    for a, b in g.get("edges") or ():
        if 0 <= a < n and 0 <= b < n:
            edges.append((rels[a], rels[b]))
    names: Dict[Tuple[str, str], tuple] = {}
    for (a, b), v in (g.get("names") or {}).items():
        if 0 <= a < n and 0 <= b < n:
            names[(rels[a], rels[b])] = v
    entry: Dict[str, List[dict]] = {}
    for i, v in (g.get("entry") or {}).items():
        if 0 <= i < n:
            entry[rels[i]] = v
    # A test-looking file that non-test code imports is code (test_plans.py).
    flagged = {r for r in rels if flags.get(r, 0) & cm.FLAG_TEST}
    test = frozenset(cm.effective_tests(flagged, edges))
    children: Dict[str, Tuple[Set[str], Set[str]]] = {}
    for r in rels:
        parts = r.split("/")
        for k in range(len(parts) - 1):
            children.setdefault("/".join(parts[:k]), (set(), set()))[0].add(parts[k])
        children.setdefault("/".join(parts[:-1]), (set(), set()))[1].add(parts[-1])
    hdr: Set[str] = set()
    for r in rels:
        if _ext(r) in _HEADER_EXTS:
            hdr.update(
                x["name"]
                for x in info[r]["syms"]
                if x["kind"] in ("function", "type", "struct")
            )
    return {
        "wt": wt,
        "key": key,
        "rels": rels,
        "idx_of": {r: i for i, r in enumerate(rels)},
        "flags": flags,
        "info": info,
        "edges": edges,
        "names": names,
        "entry": entry,
        "test": test,
        "children": children,
        "hdr": frozenset(hdr),
        "partial": partial,
        "truncated": bool(truncated),
        "levels": {},
        "search": None,
    }


def _is_public(ix: dict, rel: str, x: dict) -> bool:
    """A symbol's public flag, with C's rule applied at index level: a
    non-static ``.c`` function is public only when some header declares it."""
    if not x.get("public"):
        return False
    if LANG.get(_ext(rel)) == "c" and _ext(rel) not in _HEADER_EXTS:
        return x["name"] in ix["hdr"]
    return True


# =========================================================================== #
# Atlas levels
# =========================================================================== #


def _norm_pair(path: Any) -> Tuple[str, str]:
    """``(literal, lenient)`` forms of a worktree-relative path, or
    ValueError when it is absolute or climbs out (``..``) in EITHER form.
    ``literal`` splits the exact string on ``/`` only (git tracks
    `` lead/``, ``trail /`` and ``back\\slash/`` on Linux); ``lenient``
    also strips whitespace and reads ``\\`` as ``/`` (Windows-style
    input). Callers prefer the literal form when it names something."""
    if path is None:
        return "", ""
    if not isinstance(path, str):
        raise ValueError("path must be a string")
    raw = path.replace("\\", "/").strip()
    if raw.startswith("/") or re.match(r"^[A-Za-z]:", raw) or path.startswith("/"):
        raise ValueError("absolute paths are not allowed")
    parts = [p for p in raw.split("/") if p not in ("", ".")]
    lit = [p for p in path.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts) or any(p == ".." for p in lit):
        raise ValueError("path escapes the worktree")
    return "/".join(lit), "/".join(parts)


def _norm_dir(path: Any) -> str:
    """The lenient form of :func:`_norm_pair` (ValueError as there)."""
    return _norm_pair(path)[1]


def _is_source_set(ix: dict, p: str) -> bool:
    """Whether ``p`` is a real JVM source-set dir: its ``main`` (or ``test``)
    holds a ``java``/``kotlin``/``scala``/``groovy`` root. A Vite ``src`` with
    a ``test/setup.ts`` is an ordinary directory (merging it lost its card
    count, duplicated its tests node and dropped its breadcrumb)."""
    ch = ix["children"]
    subs = ch.get(p, ((), ()))[0]
    for s in ("main", "test"):
        if s in subs and set(ch.get(p + "/" + s, ((), ()))[0]) & set(_JVM_SRC):
            return True
    return False


def _collapse(ix: dict, path: str) -> str:
    """Follow a single-child directory chain (``backend`` holding only
    ``web`` → ``backend/web``), stopping before a JVM ``src`` source set so a
    module whose only child is ``src`` stays the module."""
    ch = ix["children"]
    for _ in range(128):
        subs, files = ch.get(path, ((), ()))
        if len(subs) == 1 and not files:
            nxt = next(iter(subs))
            if nxt == "src" and _is_source_set(ix, path + "/src"):
                break
            path = path + "/" + nxt
            continue
        break
    return path


def _jvm_root(ix: dict, module: str, lang: str) -> str:
    base = (module + "/" if module else "") + "src/main/" + lang
    return _collapse(ix, base)


def _transparent(ix: dict, p: str) -> bool:
    """Whether directory ``p`` is part of a JVM source-set chain the Atlas
    shows through (``mod/src``, ``mod/src/main``, ``mod/src/main/java`` and
    the single-child package chain below it) — such dirs get no breadcrumb."""
    segs = p.split("/")
    for k, seg in enumerate(segs):
        if seg != "src":
            continue
        module = "/".join(segs[:k])
        src = (module + "/" if module else "") + "src"
        if not _is_source_set(ix, src):
            continue
        tail = segs[k + 1 :]
        if not tail:
            return True
        if tail[0] == "main":
            if len(tail) == 1:
                return True
            if tail[1] in _JVM_SRC:
                root = _jvm_root(ix, module, tail[1])
                return p == root or root.startswith(p + "/")
    return False


def _crumbs(ix: dict, d: str) -> List[dict]:
    name = posixpath.basename(ix["wt"].rstrip("/")) or ix["wt"]
    out = [{"path": "", "name": name}]
    if not d:
        return out
    segs = d.split("/")
    for k in range(1, len(segs) + 1):
        p = "/".join(segs[:k])
        if k < len(segs) and _transparent(ix, p):
            continue
        out.append({"path": p, "name": segs[k - 1]})
    return out


def _owners(ix: dict, d: str) -> Tuple[Dict[str, str], Dict[str, dict]]:
    """``(owner, nodes)``: which child node of level ``d`` each file under it
    belongs to, and the node skeletons. JVM source sets are merged:
    ``mod/src/main/java/<package chain>`` is transparent (the module shows its
    packages directly), ``src/test/**`` is ONE tests node and
    ``src/main/resources`` one files node."""
    pre = d + "/" if d else ""
    owner: Dict[str, str] = {}
    nodes: Dict[str, dict] = {}
    roots: Dict[str, str] = {}
    src_set = None
    for r in ix["rels"]:
        if pre and not r.startswith(pre):
            continue
        rest = r[len(pre) :]
        parts = rest.split("/")
        kind = "dir"
        role = None
        if parts[0] == "src" and len(parts) >= 2 and src_set is None:
            src_set = _is_source_set(ix, pre + "src")
        if (
            parts[0] == "src"
            and len(parts) >= 2
            and (len(parts) == 2 or parts[1] not in ("main", "test"))
            and src_set
        ):
            # A source set's other children (src/integrationTest, a loose
            # file) get their OWN nodes: a catch-all `src` card would drill
            # into main/test again and disagree with its own count.
            if len(parts) > 2:
                npath = _collapse(ix, pre + "src/" + parts[1])
                name = npath[len(pre) :]
            else:
                npath, kind, name = r, "file", rest
        elif (
            parts[0] == "src"
            and len(parts) >= 3
            and parts[1] in ("main", "test")
            and src_set
        ):
            if parts[1] == "test":
                npath, name, role = pre + "src/test", "tests (src/test)", "tests"
            elif len(parts) == 3:
                npath, name = pre + "src/main", "main"
            elif parts[2] in _JVM_SRC:
                root = roots.get(parts[2])
                if root is None:
                    root = roots[parts[2]] = _jvm_root(ix, d, parts[2])
                inner = r[len(root) + 1 :] if r.startswith(root + "/") else rest
                if "/" in inner:
                    npath = _collapse(ix, root + "/" + inner.split("/", 1)[0])
                    name = npath[len(root) + 1 :]
                else:
                    npath, kind, name = r, "file", inner
            elif parts[2] == "resources":
                npath, name, role = pre + "src/main/resources", "resources", "files"
            else:
                npath, name = pre + "src/main/" + parts[2], parts[2]
        elif len(parts) > 1:
            npath = _collapse(ix, pre + parts[0])
            name = npath[len(pre) :]
        else:
            npath, kind, name = r, "file", rest
        owner[r] = npath
        nd = nodes.get(npath)
        if nd is None:
            nd = nodes[npath] = {
                "path": npath,
                "name": name,
                "kind": kind,
                "_role": role,
                "langs": {},
                "files": 0,
                "loc": 0,
                "symbols": 0,
                "public": 0,
                "tests": 0,
                "entry": 0,
                "_files": [],
            }
        nd["_files"].append(r)
    return owner, nodes


def _tiering(
    names: Sequence[str], w: Dict[Tuple[str, str], int]
) -> Tuple[Dict[str, int], List[Tuple[str, str]]]:
    """Weighted Eades–Lin–Smyth feedback-arc-set order over the sibling
    graph, then longest-path layering from the sources (tier 0 = nothing
    among the siblings uses it). Returns ``(tier, back_edges)``; nodes with
    no relation at all get tier -1 (standalone).

    WHY NOT SCC CONDENSATION. Real packages are cyclic through a handful of
    lazy imports (``session → web`` ×1 against ``web → session`` ×40):
    condensing strongly-connected components folded MindFlock's whole
    backend (and a Java service's layered packages) into ONE tier. Greedy
    FAS drops the lighter direction of each cycle instead, which keeps the
    reading "each row uses the rows below it" true for all but the dropped
    edges — and those are returned so the UI can say "both ways"."""
    out: Dict[str, Dict[str, int]] = {v: {} for v in names}
    inn: Dict[str, Dict[str, int]] = {v: {} for v in names}
    for (a, b), x in w.items():
        if a in out and b in out and a != b:
            out[a][b] = x
            inn[b][a] = x
    iso = {v for v in names if not out[v] and not inn[v]}
    alive = set(names) - iso
    o = {v: dict(out[v]) for v in alive}
    i_ = {v: dict(inn[v]) for v in alive}
    s1: List[str] = []
    s2: List[str] = []

    def rm(v: str) -> None:
        alive.discard(v)
        for b in o[v]:
            i_[b].pop(v, None)
        for a in i_[v]:
            o[a].pop(v, None)

    while alive:
        changed = True
        while changed:
            changed = False
            for v in sorted(alive):
                if v in alive and not o[v]:
                    s2.insert(0, v)
                    rm(v)
                    changed = True
            for v in sorted(alive):
                if v in alive and not i_[v]:
                    s1.append(v)
                    rm(v)
                    changed = True
        if alive:
            v = max(
                sorted(alive),
                key=lambda v: sum(o[v].values()) - sum(i_[v].values()),
            )
            s1.append(v)
            rm(v)
    order = {v: k for k, v in enumerate(s1 + s2)}
    back = sorted(
        (a, b) for (a, b) in w if a in order and b in order and order[a] > order[b]
    )
    tier: Dict[str, int] = {}
    for v in s1 + s2:
        preds = [a for a in inn[v] if order.get(a, 1 << 30) < order[v]]
        tier[v] = 0 if not preds else 1 + max(tier[a] for a in preds)
    for v in iso:
        tier[v] = -1
    return tier, back


_KIND_RANK = {"class": 0, "interface": 0, "struct": 0, "trait": 0, "enum": 1, "type": 1}


def _interface(
    ix: dict,
    files: Sequence[str],
    used: Dict[Tuple[str, str], Set[str]],
    internal: Dict[Tuple[str, str], Set[str]],
    test: Optional[frozenset] = None,
) -> Tuple[List[dict], int]:
    """The node's interface (≤ :data:`MAX_INTERFACE` items) and how many
    distinct names outside code imports from it.

    Ranking: names imported from OUTSIDE the node (``scope: "external"``),
    merged by name across its files (a C header and its ``.c``, a package
    ``__init__`` re-export and the module defining it), by distinct non-test
    importing files, ``_private`` names last. Fewer than 4 → filled with the
    most-used names INSIDE the node (≥ 2 importers, ``"internal"``), then
    with declared-public symbols by size (``"declared"``)."""
    syms_by: Dict[Tuple[str, str], dict] = {}
    by_name: Dict[str, Tuple[str, dict]] = {}
    test = ix["test"] if test is None else test
    for f in files:
        if f in test:
            continue
        for x in ix["info"].get(f, {}).get("syms") or ():
            syms_by[(f, x["name"])] = x
            by_name.setdefault(x["name"], (f, x))
        for e in ix["entry"].get(f) or ():
            if e.get("kind") == "http":
                nm = (
                    (e.get("method") or "ANY").split("/")[0]
                    + " "
                    + (e.get("route") or "")
                )
                x = {
                    "name": nm,
                    "kind": "route",
                    "line": e.get("line") or 0,
                    "end": 0,
                    "public": True,
                    "children": [],
                }
                syms_by[(f, nm)] = x
                by_name.setdefault(nm, (f, x))

    def lookup(f: str, nm: str) -> Optional[Tuple[str, dict]]:
        x = syms_by.get((f, nm))
        if x is not None:
            return f, x
        return by_name.get(nm)

    merged: Dict[str, dict] = {}
    for (f, nm), imps in used.items():
        hit = lookup(f, nm)
        if hit is None:
            continue
        fx, x = hit
        cur = merged.get(nm)
        if cur is None:
            cur = merged[nm] = {
                "name": nm,
                "kind": x["kind"],
                "path": fx,
                "line": x.get("line") or 0,
                "_imps": set(),
                "_best": -1,
            }
        cur["_imps"] |= imps
        if len(imps) > cur["_best"]:
            cur.update(
                path=fx, line=x.get("line") or 0, kind=x["kind"], _best=len(imps)
            )
    ranked = []
    for z in merged.values():
        ranked.append(
            {
                "name": z["name"],
                "kind": z["kind"],
                "path": z["path"],
                "line": z["line"],
                "used_by": len(z["_imps"]),
                "scope": "external",
            }
        )
    ranked.sort(key=lambda z: (z["name"].startswith("_"), -z["used_by"], z["name"]))
    total = len(ranked)
    if len(ranked) < 4:
        seen = {z["name"] for z in ranked}
        hubs: Dict[str, dict] = {}
        for (f, nm), imps in internal.items():
            if nm in seen:
                continue
            hit = lookup(f, nm)
            if hit is None:
                continue
            fx, x = hit
            cur = hubs.get(nm)
            if cur is None:
                cur = hubs[nm] = {
                    "name": nm,
                    "kind": x["kind"],
                    "path": fx,
                    "line": x.get("line") or 0,
                    "_imps": set(),
                }
            cur["_imps"] |= imps
        hl = [
            {
                "name": z["name"],
                "kind": z["kind"],
                "path": z["path"],
                "line": z["line"],
                "used_by": len(z["_imps"]),
                "scope": "internal",
            }
            for z in hubs.values()
            if len(z["_imps"]) >= 2
        ]
        hl.sort(key=lambda z: (z["name"].startswith("_"), -z["used_by"], z["name"]))
        ranked += hl[: MAX_INTERFACE - len(ranked)]
    if len(ranked) < 4:
        seen = {z["name"] for z in ranked}
        pubs = []
        for (f, nm), x in syms_by.items():
            if nm in seen or x["kind"] == "route" or not _is_public(ix, f, x):
                continue
            size = (
                (x.get("end") or x.get("line") or 0)
                - (x.get("line") or 0)
                + 5 * len(x.get("children") or ())
            )
            pubs.append((-size, _KIND_RANK.get(x["kind"], 2), nm, f, x))
        pubs.sort(key=lambda t: t[:4])
        for _s, _k, nm, f, x in pubs:
            if len(ranked) >= MAX_INTERFACE:
                break
            if nm in seen:
                continue
            seen.add(nm)
            ranked.append(
                {
                    "name": nm,
                    "kind": x["kind"],
                    "path": f,
                    "line": x.get("line") or 0,
                    "used_by": 0,
                    "scope": "declared",
                }
            )
    return ranked[:MAX_INTERFACE], total


def _level(ix: dict, d: str) -> dict:
    owner, nodes = _owners(ix, d)
    test = ix["test"]
    in_tests = bool(d) and cm._is_test_path(d + "/")
    if in_tests:
        # Drilled INTO a test tree on purpose: its files are the subject
        # here, so they are cards with relations like any code (tests
        # elsewhere stay tests).
        pre = d + "/"
        test = frozenset(r for r in test if not r.startswith(pre))
    info = ix["info"]
    for nd in nodes.values():
        for r in nd["_files"]:
            inf = info.get(r) or {}
            nd["files"] += 1
            nd["loc"] += int(inf.get("loc") or 0)
            if r in test:
                nd["tests"] += 1
            else:
                nd["entry"] += len(ix["entry"].get(r) or ())
            e = _ext(r)
            if e:
                nd["langs"][e] = nd["langs"].get(e, 0) + 1
            syms = inf.get("syms") or ()
            nd["symbols"] += _count_syms(syms)
            nd["public"] += sum(1 for x in syms if _is_public(ix, r, x))
    deps_out: Dict[str, Set[str]] = {}
    deps_in: Dict[str, Set[str]] = {}
    weight: Dict[Tuple[str, str], int] = {}
    ext_out: Dict[str, int] = {}
    ext_in: Dict[str, int] = {}
    tested_by: Dict[str, Set[str]] = {}
    used: Dict[str, Dict[Tuple[str, str], Set[str]]] = {}
    internal: Dict[str, Dict[Tuple[str, str], Set[str]]] = {}
    names = ix["names"]
    for s, t in ix["edges"]:
        a, b = owner.get(s), owner.get(t)
        if a is None and b is None:
            continue
        if s in test:
            if b is not None and a != b and t not in test:
                tested_by.setdefault(b, set()).add(s)
            continue
        if a is not None and b is not None:
            if a != b:
                deps_out.setdefault(a, set()).add(b)
                deps_in.setdefault(b, set()).add(a)
                weight[(a, b)] = weight.get((a, b), 0) + 1
        elif a is not None:
            ext_out[a] = ext_out.get(a, 0) + 1
        else:
            ext_in[b] = ext_in.get(b, 0) + 1
        nm = names.get((s, t))
        if b is not None and nm:
            bucket = (used if a != b else internal).setdefault(b, {})
            for n in nm:
                bucket.setdefault((t, n), set()).add(s)
    for p, nd in nodes.items():
        langs = nd["langs"]
        if nd["_role"]:
            role = nd["_role"]
        elif nd["tests"] and nd["tests"] >= max(1, 0.8 * nd["files"]):
            role = "tests"
        elif (
            not any(e in _CODE_EXTS for e in langs)
            and not deps_out.get(p)
            and not deps_in.get(p)
        ):
            role = "files"
        elif (
            nd["kind"] == "file"
            and nd["loc"] < TRIVIAL_LOC
            and not deps_in.get(p)
            and not ext_in.get(p)
            and not nd["entry"]
        ):
            role = "files"
        else:
            role = "code"
        nd["role"] = role
    code = sorted(p for p, nd in nodes.items() if nd["role"] == "code")
    cset = set(code)
    w_code = {(a, b): x for (a, b), x in weight.items() if a in cset and b in cset}
    tier, back = _tiering(code, w_code)
    ordered = sorted(
        nodes.values(), key=lambda nd: (nd["role"] != "code", -nd["loc"], nd["name"])
    )
    hidden = 0
    more = None
    if len(ordered) > MAX_NODES:
        rest = ordered[MAX_NODES - 1 :]
        ordered = ordered[: MAX_NODES - 1]
        hidden = len(rest)
        more = {
            "path": (d + "/" if d else "") + "…",
            "name": "+%d more" % hidden,
            "kind": "more",
            "role": "files",
            "lang": "",
            "langs": {},
            "files": sum(nd["files"] for nd in rest),
            "loc": sum(nd["loc"] for nd in rest),
            "symbols": 0,
            "public": 0,
            "interface": [],
            "interface_total": 0,
            "deps_out": [],
            "deps_in": [],
            "ext_out": 0,
            "ext_in": 0,
            "tier": -3,
            "entry": sum(nd["entry"] for nd in rest),
            "tested_by": 0,
            "tests": sum(nd["tests"] for nd in rest),
        }
    visible = {nd["path"] for nd in ordered}
    out_nodes = []
    for nd in ordered:
        p = nd["path"]
        itf, total = _interface(
            ix, nd["_files"], used.get(p, {}), internal.get(p, {}), test
        )
        langs = dict(
            sorted(
                nd["langs"].items(),
                key=lambda kv: (-(kv[0] in _CODE_EXTS), -kv[1], kv[0]),
            )[:4]
        )
        role = nd["role"]
        out_nodes.append(
            {
                "path": p,
                "name": nd["name"],
                "kind": nd["kind"],
                "role": role,
                "lang": next(iter(langs), ""),
                "langs": langs,
                "files": nd["files"],
                "loc": nd["loc"],
                "symbols": nd["symbols"],
                "public": nd["public"],
                "interface": itf,
                "interface_total": total,
                "deps_out": sorted(x for x in deps_out.get(p, ()) if x in visible),
                "deps_in": sorted(x for x in deps_in.get(p, ()) if x in visible),
                "ext_out": ext_out.get(p, 0),
                "ext_in": ext_in.get(p, 0),
                "tier": (
                    tier.get(p, -1)
                    if role == "code"
                    else (-2 if role == "tests" else -3)
                ),
                "entry": nd["entry"],
                "tested_by": len(tested_by.get(p, ())),
                "tests": nd["tests"],
            }
        )
    if more is not None:
        out_nodes.append(more)
    tiers = max([x["tier"] for x in out_nodes if x["role"] == "code"] + [-1]) + 1
    extras_files = [
        {"path": x["path"], "name": x["name"], "files": x["files"]}
        for x in out_nodes
        if x["role"] == "files" and x["kind"] != "more"
    ]
    return {
        "path": d,
        "crumbs": _crumbs(ix, d),
        "nodes": out_nodes,
        "tiers": tiers,
        "partial": bool(ix["partial"]),
        "truncated": bool(ix["truncated"]),
        "hidden": hidden,
        "back_edges": [[a, b] for a, b in back if a in visible and b in visible],
        "extras": {
            "tests": sum(x["files"] for x in out_nodes if x["role"] == "tests"),
            "files": extras_files,
        },
        "fingerprint": ix["key"],
    }


def _empty_level(d: str, partial: bool = False) -> dict:
    return {
        "path": d,
        "crumbs": [{"path": "", "name": ""}],
        "nodes": [],
        "tiers": 0,
        "partial": partial,
        "truncated": False,
        "hidden": 0,
        "back_edges": [],
        "extras": {"tests": 0, "files": []},
        "fingerprint": None,
    }


def atlas(inst: Any, wt: str, path: str = "", fp: Optional[str] = None) -> dict:
    """One Atlas level: the children of directory ``path`` (``""`` = repo
    root) as cards, single-child chains collapsed, JVM source sets merged.

    ``{"path", "crumbs": [{"path", "name"}], "nodes": [Node], "tiers": n,
    "partial", "truncated", "hidden", "back_edges": [[from, to]], "extras":
    {"tests": n, "files": [{"path", "name", "files"}]}, "fingerprint"}``.
    Node = ``{"path", "name", "kind": "dir"|"file"|"more", "role":
    "code"|"tests"|"files", "lang", "langs", "files", "loc", "symbols",
    "public", "interface": [{"name", "kind", "path", "line", "used_by",
    "scope"}], "interface_total", "deps_out", "deps_in", "ext_out",
    "ext_in", "tier", "entry", "tested_by", "tests"}``.

    ``tier``: 0 = top (callers) … increasing = more depended upon; -1 =
    standalone code (no sibling relation either way); -2 = tests; -3 =
    project files / the "+N more" fold. ``tiers`` = number of code tiers (0 =
    no relations at all → render a plain grid). Tests, non-code files and
    trivial unimported files (< 40 lines) are ``role != "code"`` — the strip,
    never cards (``extras`` summarizes them).

    ``inst`` / ``fp`` are accepted for the route's convenience; the index is
    keyed on the worktree's content fingerprint (see :func:`_content_fp`).
    ``ValueError`` for an absolute or ``..`` path; a directory that no
    longer exists is an empty level. Never raises otherwise."""
    lit, d = _norm_pair(path)
    try:
        ix = _index(wt)
        if lit != d and (lit in ix["children"] or lit in ix["info"]):
            d = lit  # the exact name git tracks (` lead`, `back\\slash`)
        with _LOCK:
            hit = ix["levels"].get(d)
        if hit is not None:
            return hit
        lvl = _level(ix, d)
        if not ix["partial"]:
            with _LOCK:
                if len(ix["levels"]) > 256:
                    ix["levels"].clear()
                ix["levels"][d] = lvl
        return lvl
    except Exception:  # noqa: BLE001
        return _empty_level(d, partial=True)


# =========================================================================== #
# File view
# =========================================================================== #

_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", re.M)
_ENTRY_GROUP_AFTER = 24


def _check_rel(wt: str, rel: Any) -> str:
    """``rel`` normalized, or ValueError: absolute, ``..``, a symlink that
    resolves outside the worktree (its outline would leak text from outside
    it), or not an existing regular file."""
    lit, clean = _norm_pair(rel)
    if lit != clean and lit and os.path.lexists(os.path.join(wt, lit)):
        clean = lit  # the exact name (` spacefile.py`, `back\\slash/b.py`)
    if not clean:
        raise ValueError("path is required")
    full = os.path.join(wt, clean)
    real = os.path.realpath(full)
    root = os.path.realpath(wt)
    if not (real == root or real.startswith(root.rstrip(os.sep) + os.sep)):
        raise ValueError("path resolves outside the worktree")
    if not os.path.isfile(real):
        raise ValueError("no such file")
    return clean


def _short_folder(folder: str) -> str:
    """A folder label for grouped lists: JVM source-set chains shortened
    (``svc/src/main/java/com/acme/svc/api/v1`` → ``svc/…/api/v1``)."""
    m = re.match(
        r"^(?:(.*)/)?src/(main|test)/(?:java|kotlin|scala|groovy)/(.+)$", folder
    )
    if not m:
        return folder
    head, pkg = m.group(1) or "", m.group(3).split("/")
    tail = "/".join(pkg[-2:])
    lead = (head + "/") if head else ""
    mid = "…/" if len(pkg) > 2 else ""
    return lead + mid + tail + (" (test)" if m.group(2) == "test" else "")


def _grouped(items: List[dict]) -> List[dict]:
    for it in items:
        folder = posixpath.dirname(it["path"])
        it["name"] = posixpath.basename(it["path"])
        it["folder"] = _short_folder(folder)
        it["_sort"] = folder
    items.sort(key=lambda it: (it["_sort"], it["name"]))
    for it in items:
        it.pop("_sort", None)
    return items


def _fork_diff(
    inst: Any, wt: str, rel: str, loc: int
) -> Tuple[List[List[int]], List[int]]:
    """``(changed_ranges, deletion_points)`` of ``rel`` vs the session's fork
    point, new-side line numbers. A file ``changed_files`` reports as added
    whose diff shows nothing (not yet intent-to-added) is changed as a
    whole. ``([], [])`` on any git trouble."""
    try:
        srv = cm._server()
        fork = cm._rev(srv._session_fork_point(inst, wt))
        if fork is None:
            return [], []
        status = {c["path"]: c.get("status") for c in cm.changed_files(inst, wt)}
        if rel not in status:
            return [], []
        out = cm._git(
            wt,
            "diff",
            "-U0",
            "--no-color",
            "--no-ext-diff",
            "--no-renames",
            fork,
            "--",
            rel,
        )
        if out is None:
            return [], []
        ranges: List[List[int]] = []
        dels: List[int] = []
        for m in _HUNK_RE.finditer(out.decode("utf-8", "replace")):
            c = int(m.group(1))
            n = int(m.group(2)) if m.group(2) is not None else 1
            if n > 0:
                ranges.append([c, c + n - 1])
            else:
                dels.append(max(1, c))
        if not ranges and not dels and status.get(rel) == "A" and loc:
            ranges.append([1, loc])
        return ranges, dels
    except Exception:  # noqa: BLE001
        return [], []


def _overlaps(
    line: int, end: int, ranges: Sequence[Sequence[int]], dels: Sequence[int]
) -> bool:
    end = max(end or line, line)
    for a, b in ranges:
        if a <= end and b >= line:
            return True
    return any(line <= p <= end for p in dels)


def _mark_changed(syms: List[dict], ranges, dels, parent: str = "") -> List[str]:
    out: List[str] = []
    for x in syms:
        q = (parent + "." if parent else "") + x["name"]
        if _overlaps(x["line"], x.get("end") or x["line"], ranges, dels):
            x["changed"] = True
            out.append(q)
        out.extend(_mark_changed(x.get("children") or [], ranges, dels, q))
    return out


def _zones_for(wt: str, rel: str) -> dict:
    """``{"red": bool, "green": bool|None}`` for one path through the one
    zone predicate every consumer shares (``red_zones.classify`` over
    ``zones_doc``): ``red`` = blocked by an enforced red zone, ``green`` =
    writable under the green scope (a companion counts) — None when the
    worktree has no green zone. Best effort; never raises."""
    try:
        from backend.config import red_zones as rz

        ident = rz.repo_identity(wt)
        doc = rz.zones_doc(wt, ident[0] if ident else None)
        # Judged like the hook: the REALPATH and the path as written (a
        # link into a red zone is red; a link out of the scope is outside).
        root = os.path.realpath(wt)
        real = os.path.realpath(os.path.join(wt, rel))
        rel_real: Optional[str] = None
        if real.startswith(root.rstrip(os.sep) + os.sep):
            rel_real = os.path.relpath(real, root).replace(os.sep, "/")
        v = rz.classify(rel_real, rel, doc, bool(doc.get("ci")))
        green = v in ("ok", "companion") if doc.get("green") else None
        return {"red": v == "blocked", "green": green}
    except Exception:  # noqa: BLE001
        return {"red": False, "green": None}


def _external_imports(
    ix: dict, rel: str, text: str, internal: Sequence[str]
) -> List[str]:
    """Package-level names of the imports that did NOT resolve inside the
    repo (``requests``, ``react``, ``org.springframework``, ``<stdio.h>``)."""
    lang = LANG.get(_ext(rel))
    out: Dict[str, None] = {}
    try:
        if lang == "py":
            idx = ix.get("py_idx")
            if idx is None:
                idx = ix["py_idx"] = cm._py_index(
                    [r for r in ix["rels"] if r.endswith(".py")]
                )
            for spec in cm._py_specs(text):
                if spec[0] == "from" and spec[1]:
                    continue
                if not cm._py_targets(spec, rel, idx, ix["idx_of"]):
                    mod = spec[1] if spec[0] == "imp" else spec[2]
                    if mod != "__future__":
                        out[mod.split(".")[0]] = None
        elif lang == "js":
            cfg: Dict[str, Any] = {}
            dcfg: Dict[str, Any] = {}
            for spec in cm._js_specs(text):
                if spec.startswith((".", "/")) or "://" in spec:
                    continue
                if cm._js_targets(spec, rel, ix["idx_of"], ix["wt"], cfg, dcfg):
                    continue
                bits = spec.split("/")
                out["/".join(bits[:2]) if spec.startswith("@") else bits[0]] = None
        elif lang == "go":
            mods = []
            for r in ix["rels"]:
                if r == "go.mod" or r.endswith("/go.mod"):
                    mm = re.search(
                        r"^module\s+(\S+)", _read_text(os.path.join(ix["wt"], r)), re.M
                    )
                    if mm:
                        mods.append(mm.group(1))
            specs = re.findall(
                r'^\s*(?:[\w.]+\s+)?"([^"\n]+)"',
                "\n".join(re.findall(r"^import\s*\(([^)]*)\)", text, re.M)),
                re.M,
            )
            specs += re.findall(r'^import\s+(?:[\w.]+\s+)?"([^"\n]+)"', text, re.M)
            for p in specs:
                if not any(p == m or p.startswith(m + "/") for m in mods):
                    out[p] = None
        elif lang in ("java", "kotlin"):
            stems = {posixpath.basename(t).rsplit(".", 1)[0] for t in internal}
            for m in re.finditer(r"^\s*import\s+(?:static\s+)?([\w.]+)", text, re.M):
                segs = m.group(1).split(".")
                if any(s in stems for s in segs):
                    continue
                pkg = []
                for seg in segs:
                    if seg[:1].isupper():
                        break
                    pkg.append(seg)
                out[".".join(pkg[:2] or segs[:1])] = None
        elif lang == "rust":
            for m in re.finditer(r"^\s*(?:pub\s+)?use\s+(\w+)::", text, re.M):
                if m.group(1) not in ("crate", "super", "self"):
                    out[m.group(1)] = None
        elif lang == "c":
            inc_names = {posixpath.basename(t) for t in internal}
            for m in re.finditer(r'^\s*#\s*include\s*[<"]([^>"\n]+)[>"]', text, re.M):
                if posixpath.basename(m.group(1)) not in inc_names:
                    out[m.group(1)] = None
    except Exception:  # noqa: BLE001
        pass
    return sorted(out)[:40]


def _route_prefix(route: str) -> str:
    segs = [s for s in (route or "").split("/") if s and not re.match(r"^[{<:*\[]", s)]
    return "/" + "/".join(segs[:2]) if segs else "/"


def file_view(inst: Any, wt: str, rel: str) -> dict:
    """The drilled-into file: its detailed outline, imports, entry points,
    who uses it, and what changed.

    ``{"path", "lang", "loc", "role", "symbols": [Symbol], "imports":
    {"internal": [{"path", "name", "folder", "names"}], "external": [str]},
    "entry": [{"kind", "method", "route", "line", "handler", "changed"}],
    "entry_groups": [{"prefix", "count", "changed", "items"}] (only past 24
    routes), "used_by": [{"path", "name", "folder", "names"}], "tested_by":
    [path], "changed_lines": [[a, b]], "changed_symbols": ["Class.method"],
    "zones": {"red", "green"}, "partial"}``.

    Symbol = ``{"name", "kind", "line", "end", "public", "sig", "parent",
    "children", "changed"?}``, kind ∈ class|interface|struct|trait|enum|type|
    function|method|field|const|module. ``internal`` and ``used_by`` are
    sorted by folder (``folder`` is the display label — JVM chains
    shortened) so the client can group them; edges from test files go to
    ``tested_by``, not ``used_by``. Changed entry points (and groups) sort
    first. ``ValueError`` for an absolute / ``..`` / escaping / missing
    path."""
    rel = _check_rel(wt, rel)
    try:
        ix = _index(wt)
    except Exception:  # noqa: BLE001
        ix = None
    abs_path = os.path.join(wt, rel)
    text, outline = _full_for(abs_path, rel)
    syms = _copy_syms(outline)
    loc = text.count("\n") + (1 if text and not text.endswith("\n") else 0)
    internal: List[dict] = []
    used_by: List[dict] = []
    tested_by: List[str] = []
    entries: List[dict] = []
    role = "code"
    if ix is not None:
        test = ix["test"]
        names = ix["names"]
        for s, t in ix["edges"]:
            if s == rel:
                internal.append({"path": t, "names": sorted(names.get((s, t), ()))})
            elif t == rel:
                if s in test:
                    tested_by.append(s)
                else:
                    used_by.append({"path": s, "names": sorted(names.get((s, t), ()))})
        entries = [dict(e) for e in ix["entry"].get(rel) or ()]
        if rel in test:
            role = "tests"
        elif _ext(rel) not in _CODE_EXTS:
            role = "files"
    ranges, dels = _fork_diff(inst, wt, rel, loc)
    changed = _mark_changed(syms, ranges, dels) if (ranges or dels) else []
    changed_set = set(changed)
    for e in entries:
        e["changed"] = bool(
            (ranges or dels)
            and (
                _overlaps(e.get("line") or 0, e.get("line") or 0, ranges, dels)
                or (
                    e.get("handler")
                    and any(
                        c == e["handler"] or c.endswith("." + e["handler"])
                        for c in changed_set
                    )
                )
            )
        )
    entries.sort(key=lambda e: (not e["changed"], e.get("line") or 0))
    groups: List[dict] = []
    http = [e for e in entries if e.get("kind") == "http"]
    if len(http) > _ENTRY_GROUP_AFTER:
        by: Dict[str, List[dict]] = {}
        for e in http:
            by.setdefault(_route_prefix(e.get("route") or ""), []).append(e)
        for pfx, items in by.items():
            groups.append(
                {
                    "prefix": pfx,
                    "count": len(items),
                    "changed": any(e["changed"] for e in items),
                    "items": items,
                }
            )
        groups.sort(key=lambda g: (not g["changed"], -g["count"], g["prefix"]))
    return {
        "path": rel,
        "lang": LANG.get(_ext(rel), _ext(rel)),
        "loc": loc,
        "role": role,
        "symbols": syms,
        "imports": {
            "internal": _grouped(internal),
            "external": (
                _external_imports(ix, rel, text, [i["path"] for i in internal])
                if ix is not None
                else []
            ),
        },
        "entry": entries,
        "entry_groups": groups,
        "used_by": _grouped(used_by),
        "tested_by": sorted(set(tested_by)),
        "changed_lines": ranges,
        "changed_symbols": changed,
        "zones": _zones_for(wt, rel),
        "partial": bool(ix["partial"]) if ix is not None else True,
    }


# =========================================================================== #
# Search / entry points / forget
# =========================================================================== #

_WORD_RE = re.compile(r"[A-Z]+(?=[A-Z][a-z]|[0-9]|\b|_)|[A-Z]?[a-z]+|[A-Z]+|[0-9]+")


def _words(name: str) -> Tuple[str, ...]:
    """camelCase / snake_case / kebab words of a name, lowercased."""
    return tuple(w.lower() for w in _WORD_RE.findall(name))


def _search_rows(ix: dict) -> List[tuple]:
    rows = ix.get("search")
    if rows is not None:
        return rows
    rows = []
    for d in ix["children"]:
        if d:
            base = d.rsplit("/", 1)[-1]
            is_test = cm._is_test_path(d + "/x")
            rows.append((base.lower(), base, "dir", d, 0, is_test, _words(base)))
    for rel in ix["rels"]:
        is_test = rel in ix["test"]
        base = rel.rsplit("/", 1)[-1]
        stem = base.rsplit(".", 1)[0] if "." in base[1:] else base
        rows.append((stem.lower(), base, "file", rel, 1, is_test, _words(stem)))

        def walk(syms: Sequence[dict], depth: int) -> None:
            for x in syms:
                if depth and x["name"] == x.get("parent"):
                    continue  # a constructor: the class row already covers it
                rows.append(
                    (
                        x["name"].lower(),
                        x["name"],
                        x["kind"],
                        rel,
                        x["line"],
                        is_test,
                        _words(x["name"]),
                    )
                )
                if depth < 1:
                    walk(x.get("children") or (), depth + 1)

        walk(ix["info"].get(rel, {}).get("syms") or (), 0)
        for e in ix["entry"].get(rel) or ():
            if e.get("kind") == "http":
                nm = (
                    (e.get("method") or "ANY").split("/")[0]
                    + " "
                    + (e.get("route") or "")
                )
                # Keyed on the bare route AND on the label the UI shows
                # ("GET /api/x"): typing what you see must find it.
                for key in ((e.get("route") or "").lower(), nm.lower()):
                    rows.append(
                        (key, nm, "route", rel, e.get("line") or 1, is_test, ())
                    )
    ix["search"] = rows
    return rows


def search(wt: str, q: str, limit: int = 40) -> dict:
    """``{"items": [{"path", "name", "kind", "line", "score"}], "partial"}``
    — files (by name) and symbols (top-level + members) matching ``q``:
    exact > prefix > word-prefix (camel/snake aware: ``cm`` finds
    ``code_map``, ``map`` finds ``CodeMapTab``) > substring; a query with
    ``/`` also matches paths. Tests and ``_private`` rank lower. Never
    raises."""
    try:
        q = (q or "").strip()
        lim = max(1, min(MAX_SEARCH, int(limit or 40)))
        if not q:
            return {"items": [], "partial": False}
        ql = q.lower()
        qwords = _words(q) or (ql,)
        initials = ql if len(ql) >= 2 and ql.isalpha() else ""
        ix = _index(wt)
        scored = []
        for lname, name, kind, path, line, is_test, words in _search_rows(ix):
            if lname == ql:
                s = 100.0
            elif lname.startswith(ql):
                s = 80.0
            elif (
                words
                and len(qwords) > 1
                and all(any(w.startswith(qw) for w in words) for qw in qwords)
            ):
                s = 70.0
            elif words and any(w.startswith(ql) for w in words):
                s = 65.0
            elif (
                initials
                and len(words) >= len(initials)
                and "".join(w[0] for w in words).startswith(initials)
            ):
                s = 55.0
            elif ql in lname:
                s = 45.0
            elif "/" in ql and ql in path.lower():
                s = 35.0
            else:
                continue
            s += max(0.0, 8.0 - (len(lname) - len(ql)) * 0.5)
            if is_test:
                s -= 25
            if name.startswith("_"):
                s -= 6
            # A symbol outranks the file named after it; types outrank calls.
            if kind in ("class", "interface", "struct", "trait", "enum", "route"):
                s += 3
            elif kind in ("function", "method", "const", "type", "module"):
                s += 2
            elif kind == "dir":
                s += 1
            scored.append((-s, len(path), path, line, name, kind))
        scored.sort()
        items = []
        seen = set()
        for ns, _lp, p, ln, nm, k in scored:
            if (p, ln, nm, k) in seen:
                continue  # a route's second key: the best score is first
            seen.add((p, ln, nm, k))
            items.append(
                {"path": p, "name": nm, "kind": k, "line": ln, "score": round(-ns, 1)}
            )
            if len(items) >= lim:
                break
        return {"items": items, "partial": bool(ix["partial"])}
    except Exception:  # noqa: BLE001
        return {"items": [], "partial": True}


_KIND_ORDER = {"http": 0, "event": 1, "cli": 2, "main": 3}


def entry_points(wt: str) -> dict:
    """``{"items": [{"kind", "method", "route", "path", "folder", "line",
    "handler"}], "total", "dropped", "counts": {kind: n}, "partial"}`` —
    every entry point outside test files (``dropped`` = how many were in
    tests), http first, then event / cli / main, capped at
    :data:`MAX_ENTRY` (``total`` is the uncapped count). ``folder`` groups
    mains in the drawer. Never raises."""
    try:
        ix = _index(wt)
        items = []
        dropped = 0
        for rel in ix["rels"]:
            es = ix["entry"].get(rel)
            if not es:
                continue
            if rel in ix["test"]:
                dropped += len(es)
                continue
            folder = posixpath.dirname(rel)
            for e in es:
                items.append(dict(e, path=rel, folder=folder))
        items.sort(
            key=lambda e: (
                _KIND_ORDER.get(e.get("kind"), 9),
                e["path"],
                e.get("line") or 0,
            )
        )
        counts: Dict[str, int] = {}
        for e in items:
            counts[e["kind"]] = counts.get(e["kind"], 0) + 1
        return {
            "items": items[:MAX_ENTRY],
            "total": len(items),
            "dropped": dropped,
            "counts": counts,
            "partial": bool(ix["partial"]),
        }
    except Exception:  # noqa: BLE001
        return {"items": [], "total": 0, "dropped": 0, "counts": {}, "partial": True}


def forget(wt: str) -> None:
    """Drop everything cached for ``wt`` (the index, its levels and search
    rows, and the per-file memos under it) — for a removed worktree. Never
    raises."""
    try:
        root = os.path.join(wt, "")
        with _LOCK:
            _INDEX.pop(wt, None)
            _BUILD_LOCKS.pop(wt, None)
            for memo in (_INFO_MEMO, _FULL_MEMO):
                for k in [k for k in memo if k.startswith(root)]:
                    memo.pop(k, None)
    except Exception:  # noqa: BLE001
        pass
