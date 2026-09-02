"""The Database Client's service chokepoint: ONE execution path for every
statement the extension runs (query pad, table page, row batch, export).

Everything that touches a database goes through :class:`DbClientService`:

* **Connection pool + concurrency.** Connections are cached per
  ``(profile id, database, profile fingerprint)`` and each is guarded by a
  ``threading.Lock`` held for the whole duration of any use — the only reason
  ``sqlite3.connect(check_same_thread=False)`` in adapters.py is safe. The
  fingerprint in the key means an out-of-band edit to dbclient.json simply
  misses the cache instead of reusing stale credentials; a handler error rolls
  back and drops a dead connection so the next call reconnects. Handlers call
  in via ``asyncio.to_thread``, never on the event loop.
* **Guards on user SQL.** A quote/comment-aware scanner (the Python twin of the
  frontend's sql.js) counts statements — more than one is rejected server-side
  — and powers the no-WHERE guard: an ``UPDATE``/``DELETE`` without ``WHERE``
  (judged AFTER stripping strings and comments, so ``SET note = 'where'``
  cannot fool it) answers ``needs_confirm`` until the client resends with
  ``confirm: true``. Statement timeouts are clamped to 1–300 s, result rows to
  a hard ceiling of 10 000.
* **Value codec.** Outbound cells are made JSON-safe (Decimal → str, dates →
  isoformat, bytes → ``{"$type": "bytes", "len": n}``, strings past 8 KB →
  ``{"$type": "truncated", "text": head, "len": n}``, NULL → ``null``, which
  stays distinct from ``""``). Inbound edits are coerced by the column's
  declared type and ``{"$null": true}`` binds SQL NULL.
* **Identifier safety.** Every table and column a client names is checked
  against the introspected schema BEFORE any SQL is generated; sort
  directions and filter operators come from whitelists; every identifier is
  emitted through the adapter's ``quote_ident``. Values are always bound
  parameters — this module never interpolates a value into SQL text.
"""

from __future__ import annotations

import base64
import csv
import datetime as _dt
import decimal
import io
import json
import math
import re
import threading
import time
import urllib.parse
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

from . import store
from .adapters import Adapter, KIND_TABLE, get_adapter

#: Statement timeout defaults and clamps (seconds).
DEFAULT_TIMEOUT_S = 30.0
MIN_TIMEOUT_S = 1.0
MAX_TIMEOUT_S = 300.0

#: Result-row caps for user SQL. The hard ceiling also bounds ad-hoc exports.
DEFAULT_MAX_ROWS = 500
HARD_MAX_ROWS = 10_000

#: Longest string a cell carries to the browser before it is truncated (chars).
CELL_TEXT_LIMIT = 8192

#: Table-page size clamps.
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 1000

#: Whitelists the table-data endpoint validates against.
FILTER_OPS = ("eq", "ne", "contains", "gt", "lt", "null", "notnull")
SORT_DIRS = ("asc", "desc")
ROW_ACTIONS = ("insert", "update", "delete")
EXPORT_FORMATS = ("csv", "json")

#: Rows per fetchmany() while draining a cursor.
FETCH_CHUNK = 500


class RequestError(ValueError):
    """The client asked for something the schema or the whitelists refuse
    (unknown column, bad sort direction, write to a view…). The router turns
    it into a 400 — it is never a database error."""


def error_text(err: BaseException) -> str:
    """A human message for any driver error (some carry an empty ``str``)."""
    msg = str(err).strip()
    return msg or type(err).__name__


# --------------------------------------------------------------------------- #
# SQL text scanner — the Python twin of static/extensions/dbclient/sql.js.
# Keep the two grammars in step: '…' ('' doubling; backslash escapes only for
# the mysql dialect and E'…'), "…", `…`, -- and # (mysql) line comments,
# nesting /* */ (flat for mysql), $tag$…$tag$ dollar quoting.
# --------------------------------------------------------------------------- #

_IDENT_CHAR = re.compile(r"[A-Za-z0-9_]")
_TAG_START = re.compile(r"[A-Za-z_]")


def sql_regions(text: str, dialect: str = "standard") -> List[Tuple[str, int, int]]:
    """Partition ``text`` into ``(type, start, end)`` regions with type in
    ``code`` | ``comment`` | ``string`` | ``dollar``. Unterminated constructs
    swallow the rest of the text."""
    src = text or ""
    mysql = dialect == "mysql"
    out: List[Tuple[str, int, int]] = []
    n = len(src)
    i = 0
    code_start = 0

    def close_code(at: int) -> None:
        if at > code_start:
            out.append(("code", code_start, at))

    while i < n:
        c = src[i]
        nxt = src[i + 1] if i + 1 < n else ""
        if (c == "-" and nxt == "-") or (mysql and c == "#"):
            close_code(i)
            end = _skip_line(src, i + (1 if c == "#" else 2))
            out.append(("comment", i, end))
            i = code_start = end
            continue
        if c == "/" and nxt == "*":
            close_code(i)
            end = _skip_block_comment(src, i + 2, nest=not mysql)
            out.append(("comment", i, end))
            i = code_start = end
            continue
        if c in ("'", '"', "`"):
            escapes = False
            if c == "'":
                prev = src[i - 1] if i > 0 else ""
                before = src[i - 2] if i > 1 else ""
                is_e = prev in ("E", "e") and not _IDENT_CHAR.match(before)
                escapes = mysql or is_e
            elif c == '"':
                escapes = mysql
            close_code(i)
            end = _skip_quoted(src, i + 1, c, escapes)
            out.append(("string", i, end))
            i = code_start = end
            continue
        if c == "$" and not _IDENT_CHAR.match(src[i - 1] if i > 0 else ""):
            tag = _dollar_tag(src, i)
            if tag is not None:
                close_code(i)
                close_at = src.find(tag, i + len(tag))
                end = n if close_at < 0 else close_at + len(tag)
                out.append(("dollar", i, end))
                i = code_start = end
                continue
        i += 1
    close_code(n)
    return out


def _skip_line(src: str, i: int) -> int:
    nl = src.find("\n", i)
    return len(src) if nl < 0 else nl


def _skip_block_comment(src: str, i: int, nest: bool) -> int:
    n = len(src)
    depth = 1
    while i < n and depth > 0:
        if nest and src[i] == "/" and i + 1 < n and src[i + 1] == "*":
            depth += 1
            i += 2
            continue
        if src[i] == "*" and i + 1 < n and src[i + 1] == "/":
            depth -= 1
            i += 2
            continue
        i += 1
    return i


def _skip_quoted(src: str, i: int, q: str, backslash: bool) -> int:
    n = len(src)
    while i < n:
        c = src[i]
        if backslash and c == "\\":
            i += 2
            continue
        if c == q:
            if i + 1 < n and src[i + 1] == q:
                i += 2  # a doubled delimiter is a literal one
                continue
            return i + 1
        i += 1
    return n


def _dollar_tag(src: str, i: int) -> Optional[str]:
    j = i + 1
    if j < len(src) and src[j] == "$":
        return "$$"
    if j >= len(src) or not _TAG_START.match(src[j]):
        return None
    while j < len(src) and _IDENT_CHAR.match(src[j]):
        j += 1
    return src[i : j + 1] if j < len(src) and src[j] == "$" else None


def split_statements(sql: str, dialect: str = "standard") -> List[str]:
    """The statements in ``sql`` (trimmed; comment-only segments dropped)."""
    src = sql or ""
    out: List[str] = []
    seg_start = 0
    has_code = False

    def flush(end: int) -> None:
        nonlocal has_code
        if has_code:
            stmt = src[seg_start:end].strip()
            if stmt:
                out.append(stmt)
        has_code = False

    for rtype, start, end in sql_regions(src, dialect):
        if rtype == "comment":
            continue
        if rtype != "code":
            has_code = True
            continue
        for i in range(start, end):
            c = src[i]
            if c == ";":
                flush(i)
                seg_start = i + 1
            elif not has_code and not c.isspace():
                has_code = True
    flush(len(src))
    return out


def strip_literals(sql: str, dialect: str = "standard") -> str:
    """``sql`` with comments blanked and every string/dollar body emptied
    (same length as the input) — the text a keyword search must run over."""
    src = sql or ""
    out: List[str] = []
    for rtype, start, end in sql_regions(src, dialect):
        chunk = src[start:end]
        if rtype == "code":
            out.append(chunk)
        elif rtype == "comment":
            out.append(" " * len(chunk))
        elif rtype == "string":
            q = chunk[0]
            closed = len(chunk) > 1 and chunk[-1] == q
            out.append(
                q
                + " " * max(0, len(chunk) - (2 if closed else 1))
                + (q if closed else "")
            )
        else:
            out.append(" " * len(chunk))
    return "".join(out)


_VERB_RE = re.compile(r"^\s*\(*\s*([A-Za-z]+)")
_WHERE_RE = re.compile(r"\bWHERE\b", re.IGNORECASE)


def classify_statement(sql: str, dialect: str = "standard") -> Tuple[str, bool]:
    """``(VERB, has_where)`` judged on the literal-stripped text."""
    bare = strip_literals(sql, dialect)
    m = _VERB_RE.match(bare)
    verb = m.group(1).upper() if m else ""
    return verb, bool(_WHERE_RE.search(bare))


# --------------------------------------------------------------------------- #
# Value codec
# --------------------------------------------------------------------------- #


def encode_value(v: Any, full: bool = False) -> Any:
    """A JSON-safe rendering of one cell. ``full`` (exports) keeps whole
    strings and base64-encodes bytes instead of emitting the UI markers."""
    if v is None or isinstance(v, bool):
        return v
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return v if math.isfinite(v) else str(v)
    if isinstance(v, str):
        if not full and len(v) > CELL_TEXT_LIMIT:
            return {"$type": "truncated", "text": v[:CELL_TEXT_LIMIT], "len": len(v)}
        return v
    if isinstance(v, (bytes, bytearray, memoryview)):
        raw = bytes(v)
        if full:
            return base64.b64encode(raw).decode("ascii")
        return {"$type": "bytes", "len": len(raw)}
    if isinstance(v, decimal.Decimal):
        return str(v)
    if isinstance(v, (_dt.datetime, _dt.date, _dt.time)):
        return v.isoformat()
    if isinstance(v, uuid.UUID):
        return str(v)
    if isinstance(v, (list, tuple)):
        return [encode_value(x, full) for x in v]
    if isinstance(v, dict):
        return {str(k): encode_value(x, full) for k, x in v.items()}
    return str(v)


def type_family(col_type: Optional[str]) -> str:
    """Coarse family of a declared column type: ``bool`` | ``int`` | ``float``
    | ``text`` (text = bind the string and let the engine coerce — NUMERIC
    and DECIMAL deliberately land here so precision is never lost)."""
    t = (col_type or "").upper()
    if "BOOL" in t:
        return "bool"
    if ("INT" in t or "SERIAL" in t) and "INTERVAL" not in t and "POINT" not in t:
        return "int"
    if any(k in t for k in ("REAL", "FLOA", "DOUB")):
        return "float"
    return "text"


_TRUE = {"1", "t", "true", "y", "yes", "on"}
_FALSE = {"0", "f", "false", "n", "no", "off"}


def decode_value(v: Any, col_type: Optional[str], column: str) -> Any:
    """One inbound cell → a bindable Python value. Strings (what the grid
    sends) are coerced by the column's declared type family; the explicit
    ``{"$null": true}`` marker binds NULL; the read-only markers are refused."""
    if isinstance(v, dict):
        if v.get("$null") is True:
            return None
        if v.get("$type") in ("bytes", "truncated"):
            raise RequestError(
                "column %s: binary and truncated cells are read-only" % column
            )
        return json.dumps(v)
    if isinstance(v, list):
        return json.dumps(v)
    if v is None or isinstance(v, (bool, int, float)):
        return v
    s = str(v)
    fam = type_family(col_type)
    if fam == "int":
        try:
            return int(s.strip())
        except ValueError:
            raise RequestError(
                "column %s expects an integer, got %r" % (column, s)
            ) from None
    if fam == "float":
        try:
            return float(s.strip())
        except ValueError:
            raise RequestError(
                "column %s expects a number, got %r" % (column, s)
            ) from None
    if fam == "bool":
        low = s.strip().lower()
        if low in _TRUE:
            return True
        if low in _FALSE:
            return False
        raise RequestError("column %s expects true/false, got %r" % (column, s))
    return s


# --------------------------------------------------------------------------- #
# Clamps + small request helpers
# --------------------------------------------------------------------------- #


def clamp_timeout(v: Any) -> float:
    if v in (None, ""):
        return DEFAULT_TIMEOUT_S
    try:
        t = float(v)
    except (TypeError, ValueError):
        raise RequestError("timeout_s must be a number") from None
    return max(MIN_TIMEOUT_S, min(MAX_TIMEOUT_S, t))


def clamp_max_rows(v: Any) -> int:
    if v in (None, ""):
        return DEFAULT_MAX_ROWS
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise RequestError("max_rows must be an integer") from None
    return max(1, min(HARD_MAX_ROWS, n))


def clamp_page_size(v: Any) -> int:
    if v in (None, ""):
        return DEFAULT_PAGE_SIZE
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise RequestError("page_size must be an integer") from None
    return max(1, min(MAX_PAGE_SIZE, n))


def content_disposition(filename: str) -> str:
    """``attachment`` with an ASCII-safe ``filename`` and the real name in RFC
    5987 ``filename*`` (a table called ``données`` downloads under its name)."""
    ascii_name = re.sub(r"[^A-Za-z0-9._-]+", "_", filename).strip("_") or "export"
    return "attachment; filename=\"%s\"; filename*=UTF-8''%s" % (
        ascii_name,
        urllib.parse.quote(filename, safe=""),
    )


def tree_items(items: Iterable[Any]) -> List[dict]:
    """Normalize one tree level's items: adapters may return bare names or
    ``{name, …}`` dicts (``size_bytes`` when the engine has cheap statistics);
    the wire shape is always a dict with at least ``name``."""
    out: List[dict] = []
    for item in items:
        if isinstance(item, dict):
            out.append(item if "name" in item else {**item, "name": ""})
        else:
            out.append({"name": str(item)})
    return out


def _scope(
    adapter: Adapter, database: Any, schema: Any
) -> Tuple[Optional[str], Optional[str]]:
    """Normalize the (database, schema) a request names to what the engine's
    hierarchy actually has — sqlite ignores both, mysql the schema."""
    db = str(database).strip() if database else ""
    sc = str(schema).strip() if schema else ""
    if "database" not in adapter.hierarchy:
        db = ""
    if "schema" not in adapter.hierarchy:
        sc = ""
    return db or None, sc or None


def _execute(cur: Any, sql: str, params: Optional[List[Any]]) -> None:
    # No-params calls skip the args form entirely: pymysql would otherwise
    # run the text through %-interpolation with an empty tuple.
    if params:
        cur.execute(sql, tuple(params))
    else:
        cur.execute(sql)


def _rowcount(cur: Any) -> Optional[int]:
    rc = getattr(cur, "rowcount", None)
    return int(rc) if isinstance(rc, int) and rc >= 0 else None


def _fetch_capped(cur: Any, limit: int) -> Tuple[List[Any], bool]:
    """Drain up to ``limit`` rows in chunks; ``truncated`` when more existed."""
    out: List[Any] = []
    while True:
        want = min(FETCH_CHUNK, limit + 1 - len(out))
        if want <= 0:
            break
        batch = cur.fetchmany(want)
        if not batch:
            break
        out.extend(batch)
    if len(out) > limit:
        return out[:limit], True
    return out, False


# --------------------------------------------------------------------------- #
# The pool
# --------------------------------------------------------------------------- #


class _Entry:
    """One cached connection and the lock that serializes its use."""

    __slots__ = ("adapter", "profile_id", "database", "lock", "conn")

    def __init__(
        self, adapter: Adapter, profile_id: str, database: Optional[str]
    ) -> None:
        self.adapter = adapter
        self.profile_id = profile_id
        self.database = database
        self.lock = threading.Lock()
        self.conn: Any = None


@dataclass
class ExportPlan:
    """A validated table export, ready to stream. Owns a DEDICATED connection
    (not a pooled one): the response body is produced after the handler
    returned, by whatever thread Starlette iterates it on, so it must not
    hold — or be held to — a pool entry's lock."""

    adapter: Adapter
    conn: Any
    sql: str
    columns: List[str]
    filename: str
    fmt: str

    @property
    def content_type(self) -> str:
        return "text/csv; charset=utf-8" if self.fmt == "csv" else "application/json"


class DbClientService:
    def __init__(self) -> None:
        self._entries: Dict[Tuple[str, str, str], _Entry] = {}
        self._guard = threading.Lock()

    # --- pool ---------------------------------------------------------------- #
    @contextmanager
    def connection(
        self, profile: dict, database: Optional[str] = None
    ) -> Iterator[Tuple[Adapter, Any]]:
        """``with service.connection(profile, db) as (adapter, conn):`` — the
        connection is exclusively ours for the block. Any exception rolls
        back (a no-op outside a transaction) and, if the connection is dead,
        drops it from the pool before re-raising."""
        adapter = get_adapter(str(profile.get("engine") or ""))
        db = database or adapter.default_database(profile)
        key = (str(profile["id"]), db or "", store.fingerprint(profile))
        with self._guard:
            entry = self._entries.get(key)
            if entry is None:
                entry = _Entry(adapter, str(profile["id"]), db)
                self._entries[key] = entry
        with entry.lock:
            if entry.conn is None:
                entry.conn = adapter.connect(profile, db)
            try:
                yield adapter, entry.conn
            except Exception:
                try:
                    adapter.rollback(entry.conn)
                except Exception:  # noqa: BLE001 — the connection may be gone
                    pass
                if not adapter.is_alive(entry.conn):
                    self._discard(key, entry)
                raise

    def _discard(self, key: Tuple[str, str, str], entry: _Entry) -> None:
        with self._guard:
            if self._entries.get(key) is entry:
                del self._entries[key]
        if entry.conn is not None:
            entry.adapter.close(entry.conn)
            entry.conn = None

    def drop_profile(self, profile_id: str) -> None:
        """Close every cached connection of one profile (every store write
        calls this — the saved profile may now point somewhere else)."""
        with self._guard:
            victims = [
                (k, e) for k, e in self._entries.items() if e.profile_id == profile_id
            ]
            for k, _ in victims:
                del self._entries[k]
        for _, entry in victims:
            with entry.lock:
                if entry.conn is not None:
                    entry.adapter.close(entry.conn)
                    entry.conn = None

    def close_all(self) -> None:
        with self._guard:
            victims = list(self._entries.items())
            self._entries.clear()
        for _, entry in victims:
            with entry.lock:
                if entry.conn is not None:
                    entry.adapter.close(entry.conn)
                    entry.conn = None

    # --- introspection ------------------------------------------------------ #
    def test_connection(self, profile: dict) -> dict:
        try:
            with self.connection(profile) as (adapter, conn):
                return {"ok": True, "server": adapter.server_version(conn)}
        except Exception as err:  # noqa: BLE001 — the whole point is to report it
            return {"ok": False, "error": error_text(err)}

    def tree(self, profile: dict, database: Any = None, schema: Any = None) -> dict:
        """One lazy level following the adapter's hierarchy."""
        adapter = get_adapter(str(profile.get("engine") or ""))
        database, schema = _scope(adapter, database, schema)
        if "database" in adapter.hierarchy and not database:
            with self.connection(profile) as (adapter, conn):
                return {
                    "level": "databases",
                    "items": tree_items(adapter.list_databases(conn)),
                }
        with self.connection(profile, database) as (adapter, conn):
            if "schema" in adapter.hierarchy and not schema:
                return {
                    "level": "schemas",
                    "items": tree_items(adapter.list_schemas(conn)),
                }
            return {"level": "tables", "items": adapter.list_tables(conn, schema)}

    def table_info(self, profile: dict, database: Any, schema: Any, table: str) -> dict:
        adapter = get_adapter(str(profile.get("engine") or ""))
        database, schema = _scope(adapter, database, schema)
        with self.connection(profile, database) as (adapter, conn):
            return self._resolve_table(adapter, conn, schema, table)

    def _resolve_table(
        self, adapter: Adapter, conn: Any, schema: Optional[str], table: str
    ) -> dict:
        """table_info for a table that PROVABLY exists (matched by exact name
        against list_tables) — the identifier gate every writer passes."""
        table = str(table or "").strip()
        if not table:
            raise RequestError("table is required")
        items = adapter.list_tables(conn, schema)
        hit = next((t for t in items if t["name"] == table), None)
        if hit is None:
            raise RequestError(
                "no such table: %s" % (schema + "." + table if schema else table)
            )
        info = adapter.table_info(conn, table, schema)
        info["kind"] = hit.get("kind") or info.get("kind") or KIND_TABLE
        info["approx_rows"] = hit.get("approx_rows")
        info["pk"] = [c["name"] for c in info["columns"] if c.get("pk")]
        return info

    # --- user SQL ------------------------------------------------------------ #
    def run_sql(
        self,
        profile: dict,
        sql: str,
        database: Any = None,
        schema: Any = None,
        max_rows: Any = None,
        timeout_s: Any = None,
        confirm: bool = False,
        full_values: bool = False,
    ) -> dict:
        """Run ONE statement of user SQL. Database errors come back as
        ``{ok: false, error}`` (the query pad shows them inline); only a bad
        request (RequestError) raises."""
        adapter = get_adapter(str(profile.get("engine") or ""))
        database, schema = _scope(adapter, database, schema)
        stmts = split_statements(str(sql or ""), adapter.sql_dialect)
        if not stmts:
            return {"ok": False, "error": "nothing to run — the statement is empty"}
        if len(stmts) > 1:
            return {
                "ok": False,
                "error": "one statement at a time (got %d) — Run All sends them one by one"
                % len(stmts),
            }
        stmt = stmts[0]
        verb, has_where = classify_statement(stmt, adapter.sql_dialect)
        if verb in ("UPDATE", "DELETE") and not has_where and not confirm:
            return {
                "ok": False,
                "needs_confirm": True,
                "error": "%s without a WHERE clause affects every row — confirm to run it"
                % verb,
            }
        limit = clamp_max_rows(max_rows)
        timeout = clamp_timeout(timeout_s)
        t0 = time.monotonic()
        try:
            with self.connection(profile, database) as (adapter, conn):
                adapter.use_schema(conn, schema)
                adapter.set_statement_timeout(conn, timeout)
                cur = conn.cursor()
                try:
                    cur.execute(stmt)
                    if cur.description:
                        columns = adapter.describe_columns(cur)
                        raw, truncated = _fetch_capped(cur, limit)
                        rows = [[encode_value(v, full_values) for v in r] for r in raw]
                        return {
                            "ok": True,
                            "columns": columns,
                            "rows": rows,
                            "row_count": len(rows),
                            "affected": None,
                            "elapsed_ms": _elapsed_ms(t0),
                            "truncated": truncated,
                        }
                    return {
                        "ok": True,
                        "columns": [],
                        "rows": [],
                        "row_count": 0,
                        "affected": _rowcount(cur),
                        "elapsed_ms": _elapsed_ms(t0),
                        "truncated": False,
                    }
                finally:
                    adapter.clear_statement_timeout(conn)
                    try:
                        cur.close()
                    except Exception:  # noqa: BLE001
                        pass
        except RequestError:
            raise
        except Exception as err:  # noqa: BLE001 — every driver has its own hierarchy
            return {
                "ok": False,
                "error": error_text(err),
                "elapsed_ms": _elapsed_ms(t0),
            }

    # --- table page ---------------------------------------------------------- #
    def table_data(self, profile: dict, body: dict) -> dict:
        adapter = get_adapter(str(profile.get("engine") or ""))
        database, schema = _scope(adapter, body.get("database"), body.get("schema"))
        table = str(body.get("table") or "")
        try:
            page = int(body.get("page") or 1)
        except (TypeError, ValueError):
            raise RequestError("page must be an integer") from None
        if page < 1:
            raise RequestError("page starts at 1")
        page_size = clamp_page_size(body.get("page_size"))
        sort = body.get("sort") or []
        filters = body.get("filters") or []
        if not isinstance(sort, list) or not isinstance(filters, list):
            raise RequestError("sort and filters must be lists")
        timeout = clamp_timeout(body.get("timeout_s"))
        t0 = time.monotonic()
        with self.connection(profile, database) as (adapter, conn):
            info = self._resolve_table(adapter, conn, schema, table)
            col_types = {c["name"]: c["type"] for c in info["columns"]}
            where_parts, params = self._filter_sql(adapter, col_types, filters)
            order_parts = []
            for s in sort:
                if not isinstance(s, dict):
                    raise RequestError("each sort entry must be {column, dir}")
                col = s.get("column")
                direction = str(s.get("dir") or "asc").lower()
                if col not in col_types:
                    raise RequestError("unknown sort column %r" % (col,))
                if direction not in SORT_DIRS:
                    raise RequestError("sort dir must be asc or desc")
                order_parts.append(
                    "%s %s" % (adapter.quote_ident(col), direction.upper())
                )
            sql = "SELECT %s FROM %s" % (
                ", ".join(adapter.quote_ident(c["name"]) for c in info["columns"]),
                adapter.qualified(table, schema),
            )
            if where_parts:
                sql += " WHERE " + " AND ".join(where_parts)
            if order_parts:
                sql += " ORDER BY " + ", ".join(order_parts)
            # page_size + 1: one extra row answers has_more without a COUNT(*).
            sql += " LIMIT %d OFFSET %d" % (page_size + 1, (page - 1) * page_size)
            adapter.set_statement_timeout(conn, timeout)
            cur = conn.cursor()
            try:
                _execute(cur, sql, params)
                fetched = cur.fetchall()
            finally:
                adapter.clear_statement_timeout(conn)
                try:
                    cur.close()
                except Exception:  # noqa: BLE001
                    pass
        has_more = len(fetched) > page_size
        rows = [[encode_value(v) for v in r] for r in fetched[:page_size]]
        return {
            "ok": True,
            "columns": [
                {
                    "name": c["name"],
                    "type": c["type"],
                    "pk": bool(c["pk"]),
                    "nullable": bool(c["nullable"]),
                }
                for c in info["columns"]
            ],
            "pk": info["pk"],
            "kind": info["kind"],
            "rows": rows,
            "page": page,
            "page_size": page_size,
            "has_more": has_more,
            # Only a cheap estimate, only for the unfiltered table.
            "total_approx": info["approx_rows"] if not where_parts else None,
            "elapsed_ms": _elapsed_ms(t0),
        }

    def _filter_sql(
        self, adapter: Adapter, col_types: Dict[str, str], filters: list
    ) -> Tuple[List[str], List[Any]]:
        parts: List[str] = []
        params: List[Any] = []
        ph = adapter.placeholder
        for f in filters:
            if not isinstance(f, dict):
                raise RequestError("each filter must be {column, op, value}")
            col = f.get("column")
            op = str(f.get("op") or "").lower()
            if col not in col_types:
                raise RequestError("unknown filter column %r" % (col,))
            if op not in FILTER_OPS:
                raise RequestError("unknown filter op %r" % (op,))
            q = adapter.quote_ident(col)
            if op == "null":
                parts.append(q + " IS NULL")
            elif op == "notnull":
                parts.append(q + " IS NOT NULL")
            elif op == "contains":
                raw = f.get("value")
                text = "" if raw is None else str(raw)
                pattern = (
                    "%"
                    + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                    + "%"
                )
                parts.append(
                    "%s %s %s %s"
                    % (adapter.cast_text(q), adapter.like_op, ph, adapter.like_escape)
                )
                params.append(pattern)
            else:
                v = decode_value(f.get("value"), col_types[col], col)
                if v is None:
                    # "= NULL" is never true; the IS forms are what was meant.
                    if op == "eq":
                        parts.append(q + " IS NULL")
                    elif op == "ne":
                        parts.append(q + " IS NOT NULL")
                    else:
                        raise RequestError("filter %s on %s needs a value" % (op, col))
                    continue
                sym = {"eq": "=", "ne": "<>", "gt": ">", "lt": "<"}[op]
                parts.append("%s %s %s" % (q, sym, ph))
                params.append(v)
        return parts, params

    # --- row batch ----------------------------------------------------------- #
    def rows(self, profile: dict, body: dict) -> dict:
        """Insert/update/delete a batch in ONE transaction. ``preview`` returns
        the generated statements without running anything. Any update/delete
        that touches ≠ 1 row is *stale* (the row changed elsewhere) and rolls
        the whole batch back — nothing is ever half-saved."""
        adapter = get_adapter(str(profile.get("engine") or ""))
        database, schema = _scope(adapter, body.get("database"), body.get("schema"))
        table = str(body.get("table") or "")
        ops = body.get("operations")
        preview = bool(body.get("preview"))
        if not isinstance(ops, list) or not ops:
            raise RequestError("operations must be a non-empty list")
        if not preview and profile.get("read_only"):
            return {
                "ok": False,
                "error": "connection %s is read-only"
                % (profile.get("name") or profile.get("id")),
            }
        timeout = clamp_timeout(body.get("timeout_s"))
        with self.connection(profile, database) as (adapter, conn):
            info = self._resolve_table(adapter, conn, schema, table)
            if info["kind"] != KIND_TABLE:
                raise RequestError("%s is a %s — read only" % (table, info["kind"]))
            if not info["pk"]:
                raise RequestError("table %s has no primary key — read only" % table)
            plans = [
                self._plan_op(adapter, info, schema, table, i, op)
                for i, op in enumerate(ops)
            ]
            if preview:
                return {
                    "ok": True,
                    "statements": [
                        {
                            "action": action,
                            "sql": sql,
                            "params": [encode_value(p) for p in params],
                        }
                        for action, sql, params in plans
                    ],
                }
            results: List[dict] = []
            stale: List[dict] = []
            adapter.set_statement_timeout(conn, timeout)
            try:
                adapter.begin(conn)
                try:
                    for i, (action, sql, params) in enumerate(plans):
                        cur = conn.cursor()
                        _execute(cur, sql, params)
                        affected = _rowcount(cur)
                        is_stale = (
                            action != "insert"
                            and affected is not None
                            and affected != 1
                        )
                        entry = {
                            "index": i,
                            "action": action,
                            "affected": affected,
                            "stale": is_stale,
                        }
                        results.append(entry)
                        if is_stale:
                            stale.append(entry)
                except (
                    Exception
                ) as err:  # noqa: BLE001 — any driver error aborts the batch
                    _safe_rollback(adapter, conn)
                    return {
                        "ok": False,
                        "error": "operation %d failed: %s"
                        % (len(results), error_text(err)),
                        "failed_index": len(results),
                        "results": results,
                    }
                if stale:
                    _safe_rollback(adapter, conn)
                    return {
                        "ok": False,
                        "stale": stale,
                        "results": results,
                        "error": "%d row(s) were modified elsewhere — nothing was saved"
                        % len(stale),
                    }
                adapter.commit(conn)
            finally:
                adapter.clear_statement_timeout(conn)
        return {
            "ok": True,
            "results": results,
            "stale": [],
            "affected": sum(r["affected"] or 0 for r in results),
        }

    def _plan_op(
        self,
        adapter: Adapter,
        info: dict,
        schema: Optional[str],
        table: str,
        index: int,
        op: Any,
    ) -> Tuple[str, str, List[Any]]:
        if not isinstance(op, dict):
            raise RequestError("operation %d must be an object" % index)
        action = str(op.get("action") or "").lower()
        if action not in ROW_ACTIONS:
            raise RequestError("operation %d: unknown action %r" % (index, action))
        col_types = {c["name"]: c["type"] for c in info["columns"]}
        q = adapter.quote_ident
        ph = adapter.placeholder
        qualified = adapter.qualified(table, schema)
        values = op.get("values") or {}
        if not isinstance(values, dict):
            raise RequestError("operation %d: values must be an object" % index)
        for col in values:
            if col not in col_types:
                raise RequestError("operation %d: unknown column %r" % (index, col))
        set_cols = list(values.keys())
        set_params = [decode_value(values[c], col_types[c], c) for c in set_cols]
        if action == "insert":
            if not set_cols:
                return action, adapter.insert_defaults_sql(qualified), []
            sql = "INSERT INTO %s (%s) VALUES (%s)" % (
                qualified,
                ", ".join(q(c) for c in set_cols),
                ", ".join([ph] * len(set_cols)),
            )
            return action, sql, set_params
        where_pk = op.get("where_pk")
        if not isinstance(where_pk, dict):
            raise RequestError("operation %d: %s needs where_pk" % (index, action))
        missing = [c for c in info["pk"] if c not in where_pk]
        if missing:
            raise RequestError(
                "operation %d: where_pk is missing primary key column(s) %s"
                % (index, ", ".join(missing))
            )
        extra = [c for c in where_pk if c not in info["pk"]]
        if extra:
            raise RequestError(
                "operation %d: where_pk may only name primary key columns (got %s)"
                % (index, ", ".join(extra))
            )
        where_parts: List[str] = []
        where_params: List[Any] = []
        for c in info["pk"]:
            v = decode_value(where_pk[c], col_types[c], c)
            if v is None:
                where_parts.append("%s IS NULL" % q(c))  # NULL keys match with IS NULL
            else:
                where_parts.append("%s = %s" % (q(c), ph))
                where_params.append(v)
        where_sql = " AND ".join(where_parts)
        if action == "update":
            if not set_cols:
                raise RequestError("operation %d: update has no values" % index)
            sql = "UPDATE %s SET %s WHERE %s" % (
                qualified,
                ", ".join("%s = %s" % (q(c), ph) for c in set_cols),
                where_sql,
            )
            return action, sql, set_params + where_params
        return action, "DELETE FROM %s WHERE %s" % (qualified, where_sql), where_params

    # --- export -------------------------------------------------------------- #
    def prepare_table_export(
        self, profile: dict, database: Any, schema: Any, table: str, fmt: str
    ) -> ExportPlan:
        """Validate everything up front (so a bad request is a clean 400, not
        a broken stream) and open the dedicated connection the stream owns."""
        fmt = str(fmt or "csv").lower()
        if fmt not in EXPORT_FORMATS:
            raise RequestError("format must be csv or json")
        adapter = get_adapter(str(profile.get("engine") or ""))
        database, schema = _scope(adapter, database, schema)
        conn = adapter.connect(profile, database or adapter.default_database(profile))
        try:
            info = self._resolve_table(adapter, conn, schema, table)
            columns = [c["name"] for c in info["columns"]]
            sql = "SELECT %s FROM %s" % (
                ", ".join(adapter.quote_ident(c) for c in columns),
                adapter.qualified(table, schema),
            )
        except Exception:
            adapter.close(conn)
            raise
        return ExportPlan(adapter, conn, sql, columns, "%s.%s" % (table, fmt), fmt)

    def stream_export(self, plan: ExportPlan) -> Iterator[bytes]:
        """The response body: rows streamed in chunks straight off the cursor
        (no row cap — this is the whole-table download). Closes the plan's
        connection when the body ends, however it ends."""
        try:
            cur = plan.conn.cursor()
            cur.execute(plan.sql)

            def rows() -> Iterator[Any]:
                while True:
                    batch = cur.fetchmany(FETCH_CHUNK)
                    if not batch:
                        return
                    yield from batch

            yield from serialize_rows(plan.fmt, plan.columns, rows())
        finally:
            plan.adapter.close(plan.conn)

    def export_sql(
        self,
        profile: dict,
        sql: str,
        database: Any,
        schema: Any,
        fmt: str,
        timeout_s: Any = None,
    ) -> dict:
        """Ad-hoc SQL export through the chokepoint (statement guards, the 10k
        hard cap). Returns ``{ok, body (bytes), filename, content_type}`` or the
        chokepoint's ``{ok: false, error}``."""
        fmt = str(fmt or "csv").lower()
        if fmt not in EXPORT_FORMATS:
            raise RequestError("format must be csv or json")
        res = self.run_sql(
            profile,
            sql,
            database=database,
            schema=schema,
            max_rows=HARD_MAX_ROWS,
            timeout_s=timeout_s,
            confirm=False,
            full_values=True,
        )
        if not res.get("ok"):
            if res.get("needs_confirm"):
                res = dict(
                    res,
                    error="a statement that needs confirmation cannot be exported — run it in the query pad",
                )
            return res
        columns = [c["name"] for c in res["columns"]]
        body = b"".join(serialize_rows(fmt, columns, res["rows"], already_encoded=True))
        return {
            "ok": True,
            "body": body,
            "filename": "query.%s" % fmt,
            "content_type": (
                "text/csv; charset=utf-8" if fmt == "csv" else "application/json"
            ),
            "row_count": res["row_count"],
            "truncated": res["truncated"],
        }


def _elapsed_ms(t0: float) -> float:
    return round((time.monotonic() - t0) * 1000.0, 2)


def _safe_rollback(adapter: Adapter, conn: Any) -> None:
    try:
        adapter.rollback(conn)
    except Exception:  # noqa: BLE001 — a failed rollback on a dead connection
        pass


def serialize_rows(
    fmt: str, columns: List[str], rows: Iterable[Any], already_encoded: bool = False
) -> Iterator[bytes]:
    """CSV (header + rows) or a JSON array of objects, chunked. Values pass
    through the full-fidelity codec unless the caller already did."""

    def cell(v: Any) -> Any:
        return v if already_encoded else encode_value(v, full=True)

    if fmt == "csv":
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(columns)
        n = 0
        for r in rows:
            writer.writerow(["" if v is None else _csv_text(cell(v)) for v in r])
            n += 1
            if n % FETCH_CHUNK == 0:
                yield buf.getvalue().encode("utf-8")
                buf.seek(0)
                buf.truncate(0)
        yield buf.getvalue().encode("utf-8")
        return
    yield b"["
    first = True
    for r in rows:
        obj = {c: cell(v) for c, v in zip(columns, r)}
        chunk = ("\n" if first else ",\n") + json.dumps(obj, ensure_ascii=False)
        first = False
        yield chunk.encode("utf-8")
    yield b"\n]\n"


def _csv_text(v: Any) -> str:
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)
