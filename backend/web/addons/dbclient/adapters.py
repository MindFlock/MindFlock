"""Engine adapters for the Database Client extension.

One :class:`Adapter` per engine — ``sqlite`` (stdlib), ``postgres`` (psycopg
v3, falling back to psycopg2) and ``mysql`` (pymysql) — behind one small
interface the service chokepoint (service.py) drives: connect, introspect
(databases → schemas → tables → table_info), quote identifiers, apply a
statement timeout, and the transaction verbs. The service never speaks an
engine's dialect directly; every ``if engine == …`` lives here.

Drivers are import-detected, never required: the sqlite adapter always works,
and :func:`driver_report` tells the UI which of the others are missing along
with a best-effort install hint aimed at the venv that serves the app (the
``uv tool`` install is the usual one, which is why the hint resolves the
interpreter through ``command -v mindflock`` instead of naming a path).

Thread-safety contract: a connection is used by ONE thread at a time — the
service holds a per-connection lock for the whole duration of any use — so
``sqlite3.connect(check_same_thread=False)`` is safe here and nowhere else.
Every connection runs in autocommit; the multi-statement row batch opens its
own transaction through :meth:`Adapter.begin` / ``commit`` / ``rollback``.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

#: Seconds a connect attempt may take before it is called a failure. Short on
#: purpose: the UI's Test button and a tree expansion both block on it.
CONNECT_TIMEOUT_S = 5

#: Table-kind vocabulary shared by every adapter (the UI renders anything but
#: ``"table"`` read-only).
KIND_TABLE = "table"
KIND_VIEW = "view"

#: Cap on the size-statistics catalog queries (pg_database_size walks files;
#: information_schema.tables can crawl on huge catalogs). They run on the
#: pooled connection under its lock, so a hang here would wedge the whole
#: tree — on timeout the except falls back to plain names.
SIZE_STATS_TIMEOUT_S = 5.0


class DriverMissing(RuntimeError):
    """The engine's Python driver is not importable in the serving venv.
    ``install_hint`` is the command that would fix it (best-effort)."""

    def __init__(self, engine: str, driver: str, install_hint: str) -> None:
        super().__init__(
            "the %s driver (%s) is not installed in the server's environment — %s"
            % (engine, driver, install_hint)
        )
        self.engine = engine
        self.driver = driver
        self.install_hint = install_hint


def _install_hint(package: str) -> str:
    """The pip command for the venv that serves the app — the manual fallback
    shown when ``installer.py`` cannot run the install itself (and the thing a
    user can paste into a shell). Best-effort guidance: it assumes the
    ``mindflock`` entry point on PATH belongs to that venv (true for the ``uv
    tool install`` layout, which is the documented install)."""
    return (
        'uv pip install --python "$(command -v mindflock | xargs dirname)/python" %s'
        % package
    )


class Adapter:
    """The engine-neutral interface. Subclasses fill in the dialect; the
    docstrings here are the contract the service relies on."""

    engine: str = ""
    #: Which lazy tree levels exist above the tables: ``("database", "schema")``
    #: for PostgreSQL, ``("database",)`` for MySQL, ``()`` for SQLite.
    hierarchy: Tuple[str, ...] = ()
    #: pip name of the driver (for the report + the install hint).
    driver: str = ""
    #: DB-API paramstyle placeholder the driver expects in SQL text.
    placeholder: str = "?"
    #: The LIKE operator to use for a case-insensitive "contains" filter.
    like_op: str = "LIKE"
    #: The ESCAPE clause for a LIKE whose pattern escapes ``%``/``_`` with a
    #: backslash — spelled per engine because MySQL string literals treat a
    #: backslash as an escape character themselves.
    like_escape: str = "ESCAPE '\\'"
    #: Which text-scanner dialect (service.split_statements) fits this engine.
    sql_dialect: str = "standard"

    # --- availability ------------------------------------------------------ #
    @classmethod
    def available(cls) -> bool:
        return True

    @classmethod
    def install_hint(cls) -> str:
        return ""

    # --- connections ------------------------------------------------------- #
    def default_database(self, profile: dict) -> Optional[str]:
        """The database a connection lands in when the request names none."""
        return None

    def connect(self, profile: dict, database: Optional[str] = None) -> Any:
        """Open a connection (autocommit, read-only enforced when the profile
        says so). Raises the driver's own error on failure."""
        raise NotImplementedError

    def close(self, conn: Any) -> None:
        try:
            conn.close()
        except Exception:  # noqa: BLE001 — closing a dead connection is fine
            pass

    def is_alive(self, conn: Any) -> bool:
        """False when the driver knows the connection is gone (the pool drops
        it so the next call reconnects). Default: assume alive."""
        return True

    def server_version(self, conn: Any) -> str:
        return ""

    # --- introspection ----------------------------------------------------- #
    def list_databases(self, conn: Any) -> List[Any]:
        """Names, or ``{name, size_bytes}`` dicts where the engine can answer
        the size from cheap statistics (the service normalizes either shape).
        ``size_bytes`` may be ``None`` when the engine cannot say."""
        return []

    def list_schemas(self, conn: Any) -> List[Any]:
        """Same contract as :meth:`list_databases`, one level down."""
        return []

    def list_tables(self, conn: Any, schema: Optional[str] = None) -> List[dict]:
        """``[{name, kind, approx_rows}]`` — approx_rows from the engine's cheap
        statistics, ``None`` when it has none. NEVER a ``COUNT(*)`` scan."""
        raise NotImplementedError

    def table_info(self, conn: Any, table: str, schema: Optional[str] = None) -> dict:
        """``{columns: [{name, type, nullable, default, pk, autoinc}],
        indexes: [{name, columns, unique}], ddl, kind}``."""
        raise NotImplementedError

    # --- SQL text ---------------------------------------------------------- #
    def quote_ident(self, name: str) -> str:
        return '"' + str(name).replace('"', '""') + '"'

    def qualified(self, table: str, schema: Optional[str] = None) -> str:
        q = self.quote_ident(table)
        return self.quote_ident(schema) + "." + q if schema else q

    def cast_text(self, expr: str) -> str:
        """``expr`` as text, so a "contains" filter works on numeric columns."""
        return "CAST(%s AS TEXT)" % expr

    def insert_defaults_sql(self, qualified: str) -> str:
        """An INSERT that supplies no column at all (every value defaulted)."""
        return "INSERT INTO %s DEFAULT VALUES" % qualified

    def describe_columns(self, cursor: Any) -> List[dict]:
        """``[{name, type}]`` for a cursor with a result set. Type names are
        best-effort — sqlite reports none at all for computed columns."""
        out: List[dict] = []
        for d in cursor.description or ():
            out.append({"name": str(d[0]), "type": self._type_name(d[1])})
        return out

    def _type_name(self, type_code: Any) -> str:
        return "" if type_code is None else str(type_code)

    # --- session state ----------------------------------------------------- #
    def use_schema(self, conn: Any, schema: Optional[str]) -> None:
        """Point unqualified names at ``schema`` for the statements that follow
        (PostgreSQL's search_path). No-op where the engine has no schemas."""

    def set_statement_timeout(self, conn: Any, seconds: float) -> None:
        """Abort any single statement running longer than ``seconds``."""

    def clear_statement_timeout(self, conn: Any) -> None:
        """Undo :meth:`set_statement_timeout` (the connection is pooled)."""

    # --- transactions ------------------------------------------------------ #
    def begin(self, conn: Any) -> None:
        conn.cursor().execute("BEGIN")

    def commit(self, conn: Any) -> None:
        conn.cursor().execute("COMMIT")

    def rollback(self, conn: Any) -> None:
        # The DB-API verb: a no-op when no transaction is open, which is what
        # the pool's "rollback on any handler error" needs.
        conn.rollback()


# --------------------------------------------------------------------------- #
# SQLite
# --------------------------------------------------------------------------- #


class SqliteAdapter(Adapter):
    engine = "sqlite"
    hierarchy = ()
    driver = "sqlite3 (stdlib)"
    placeholder = "?"
    # sqlite's LIKE is already case-insensitive for ASCII.
    like_op = "LIKE"

    def connect(self, profile: dict, database: Optional[str] = None) -> Any:
        raw = str(profile.get("file") or "").strip()
        if not raw:
            raise FileNotFoundError("sqlite connections need a database file path")
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = path.resolve()
        # mode=rw (not the default rwc): a typo in the path must fail here, on
        # Test, instead of silently creating an empty database at the typo.
        if not path.is_file():
            raise FileNotFoundError("sqlite file not found: %s" % path)
        mode = "ro" if profile.get("read_only") else "rw"
        uri = path.as_uri() + "?mode=" + mode
        conn = sqlite3.connect(
            uri,
            uri=True,
            timeout=CONNECT_TIMEOUT_S,
            # Safe ONLY because the service serializes every use under a lock.
            check_same_thread=False,
            # Legacy autocommit mode: no implicit BEGIN, so the row batch's
            # explicit BEGIN/COMMIT is the only transaction that ever opens.
            isolation_level=None,
        )
        try:
            # Touch the schema now so "file is not a database" surfaces at
            # connect (Test) rather than on the first tree expansion.
            conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchall()
        except sqlite3.Error:
            conn.close()
            raise
        return conn

    def is_alive(self, conn: Any) -> bool:
        try:
            conn.execute("SELECT 1").fetchall()
            return True
        except sqlite3.Error:
            return False

    def server_version(self, conn: Any) -> str:
        return "SQLite " + sqlite3.sqlite_version

    def list_databases(self, conn: Any) -> List[str]:
        return ["main"]

    def list_tables(self, conn: Any, schema: Optional[str] = None) -> List[dict]:
        rows = conn.execute(
            "SELECT name, type FROM sqlite_master "
            "WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite\\_%' ESCAPE '\\' "
            "ORDER BY name"
        ).fetchall()
        return [
            {
                "name": name,
                "kind": KIND_VIEW if kind == "view" else KIND_TABLE,
                "approx_rows": None,
            }
            for name, kind in rows
        ]

    def table_info(self, conn: Any, table: str, schema: Optional[str] = None) -> dict:
        row = conn.execute(
            "SELECT type, sql FROM sqlite_master WHERE name = ? AND type IN ('table', 'view')",
            (table,),
        ).fetchone()
        if row is None:
            raise LookupError("no such table: %s" % table)
        kind = KIND_VIEW if row[0] == "view" else KIND_TABLE
        ddl = row[1] or ""
        q = self.quote_ident(table)
        columns: List[dict] = []
        for _cid, name, ctype, notnull, dflt, pk in conn.execute(
            "PRAGMA table_info(%s)" % q
        ):
            columns.append(
                {
                    "name": name,
                    "type": ctype or "",
                    "nullable": not notnull,
                    "default": dflt,
                    "pk": bool(pk),
                    "autoinc": False,
                }
            )
        pk_cols = [c for c in columns if c["pk"]]
        # A lone INTEGER PRIMARY KEY is the rowid alias: sqlite assigns it when
        # an INSERT omits it, which is what "autoinc" means to the UI.
        if (
            kind == KIND_TABLE
            and len(pk_cols) == 1
            and pk_cols[0]["type"].upper() == "INTEGER"
        ):
            pk_cols[0]["autoinc"] = True
        indexes: List[dict] = []
        if kind == KIND_TABLE:
            for idx in conn.execute("PRAGMA index_list(%s)" % q).fetchall():
                # (seq, name, unique, origin, partial)
                name, unique = idx[1], bool(idx[2])
                cols = [
                    r[2]
                    for r in conn.execute(
                        "PRAGMA index_info(%s)" % self.quote_ident(name)
                    )
                    if r[2] is not None
                ]
                indexes.append({"name": name, "columns": cols, "unique": unique})
        return {"columns": columns, "indexes": indexes, "ddl": ddl, "kind": kind}

    def set_statement_timeout(self, conn: Any, seconds: float) -> None:
        deadline = time.monotonic() + float(seconds)

        def _tick() -> int:
            # Non-zero aborts the running statement with "interrupted".
            return 1 if time.monotonic() > deadline else 0

        conn.set_progress_handler(_tick, 10_000)

    def clear_statement_timeout(self, conn: Any) -> None:
        conn.set_progress_handler(None, 0)


# --------------------------------------------------------------------------- #
# PostgreSQL
# --------------------------------------------------------------------------- #


def _import_pg() -> Tuple[Optional[str], Any]:
    """(driver name, module) — psycopg 3 preferred, psycopg2 accepted."""
    try:
        import psycopg  # type: ignore

        return "psycopg", psycopg
    except ImportError:
        pass
    try:
        import psycopg2  # type: ignore

        return "psycopg2", psycopg2
    except ImportError:
        return None, None


_PG_KINDS = {
    "r": KIND_TABLE,
    "p": KIND_TABLE,
    "v": KIND_VIEW,
    "m": "matview",
    "f": "foreign",
}


class PostgresAdapter(Adapter):
    engine = "postgres"
    hierarchy = ("database", "schema")
    driver = "psycopg[binary]"
    placeholder = "%s"
    like_op = "ILIKE"

    @classmethod
    def available(cls) -> bool:
        return _import_pg()[0] is not None

    @classmethod
    def install_hint(cls) -> str:
        return _install_hint('"psycopg[binary]"')

    def default_database(self, profile: dict) -> Optional[str]:
        return str(profile.get("database") or "") or "postgres"

    def connect(self, profile: dict, database: Optional[str] = None) -> Any:
        name, mod = _import_pg()
        if mod is None:
            raise DriverMissing(self.engine, self.driver, self.install_hint())
        kwargs: Dict[str, Any] = {
            "host": profile.get("host") or "localhost",
            "port": int(profile.get("port") or 5432),
            "user": profile.get("user") or None,
            "password": profile.get("password") or None,
            "dbname": database or self.default_database(profile),
            "connect_timeout": CONNECT_TIMEOUT_S,
        }
        if profile.get("read_only"):
            # Every implicit (autocommit) transaction is read-only, so a write
            # fails with the server's own "cannot execute … in a read-only
            # transaction" — no client-side SQL sniffing needed.
            kwargs["options"] = "-c default_transaction_read_only=on"
        if name == "psycopg":
            return mod.connect(autocommit=True, **kwargs)
        conn = mod.connect(**kwargs)
        conn.autocommit = True
        return conn

    def is_alive(self, conn: Any) -> bool:
        return not getattr(conn, "closed", False) and not getattr(conn, "broken", False)

    def server_version(self, conn: Any) -> str:
        cur = conn.cursor()
        cur.execute("SELECT version()")
        row = cur.fetchone()
        return str(row[0]).split(" on ")[0] if row else "PostgreSQL"

    def list_databases(self, conn: Any) -> List[Any]:
        cur = conn.cursor()
        try:
            # Size from the catalog, guarded per row: pg_database_size raises
            # for a database this role cannot CONNECT to. Bounded — it stats
            # every file of every database.
            self.set_statement_timeout(conn, SIZE_STATS_TIMEOUT_S)
            try:
                cur.execute(
                    "SELECT datname, CASE WHEN has_database_privilege(datname, 'CONNECT') "
                    "THEN pg_database_size(datname) END "
                    "FROM pg_database WHERE NOT datistemplate AND datallowconn ORDER BY datname"
                )
                rows = cur.fetchall()
            finally:
                self.clear_statement_timeout(conn)
            return [
                {"name": r[0], "size_bytes": None if r[1] is None else int(r[1])}
                for r in rows
            ]
        except Exception:  # noqa: BLE001 — sizes are decoration, names are not
            self.rollback(conn)
            cur = conn.cursor()
            cur.execute(
                "SELECT datname FROM pg_database WHERE NOT datistemplate AND datallowconn ORDER BY datname"
            )
            return [r[0] for r in cur.fetchall()]

    def list_schemas(self, conn: Any) -> List[Any]:
        cur = conn.cursor()
        try:
            # pg_total_relation_size (heap + toast + indexes) summed over the
            # schema's plain/partition/materialized relations — never a row
            # scan, but it stats files, so it gets the same bound. NULL (no
            # relations) stays NULL.
            self.set_statement_timeout(conn, SIZE_STATS_TIMEOUT_S)
            try:
                cur.execute(
                    "SELECT n.nspname, SUM(pg_total_relation_size(c.oid))::bigint "
                    "FROM pg_namespace n "
                    "LEFT JOIN pg_class c ON c.relnamespace = n.oid AND c.relkind IN ('r', 'p', 'm') "
                    "WHERE n.nspname NOT LIKE 'pg\\_%' AND n.nspname <> 'information_schema' "
                    "GROUP BY n.nspname "
                    "ORDER BY (n.nspname <> 'public'), n.nspname"
                )
                rows = cur.fetchall()
            finally:
                self.clear_statement_timeout(conn)
            return [
                {"name": r[0], "size_bytes": None if r[1] is None else int(r[1])}
                for r in rows
            ]
        except Exception:  # noqa: BLE001
            self.rollback(conn)
            cur = conn.cursor()
            cur.execute(
                "SELECT nspname FROM pg_namespace "
                "WHERE nspname NOT LIKE 'pg\\_%' AND nspname <> 'information_schema' "
                "ORDER BY (nspname <> 'public'), nspname"
            )
            return [r[0] for r in cur.fetchall()]

    def list_tables(self, conn: Any, schema: Optional[str] = None) -> List[dict]:
        cur = conn.cursor()
        cur.execute(
            "SELECT c.relname, c.relkind, c.reltuples "
            "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = %s AND c.relkind IN ('r', 'p', 'v', 'm', 'f') "
            "ORDER BY c.relname",
            (schema or "public",),
        )
        out: List[dict] = []
        for name, relkind, reltuples in cur.fetchall():
            # reltuples is the planner's estimate; -1 means "never analyzed".
            approx = (
                int(reltuples)
                if reltuples is not None and float(reltuples) >= 0
                else None
            )
            out.append(
                {
                    "name": name,
                    "kind": _PG_KINDS.get(relkind, KIND_TABLE),
                    "approx_rows": approx,
                }
            )
        return out

    def table_info(self, conn: Any, table: str, schema: Optional[str] = None) -> dict:
        schema = schema or "public"
        cur = conn.cursor()
        cur.execute(
            "SELECT c.oid, c.relkind FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = %s AND c.relname = %s",
            (schema, table),
        )
        head = cur.fetchone()
        if head is None:
            raise LookupError("no such table: %s.%s" % (schema, table))
        oid, relkind = head
        kind = _PG_KINDS.get(relkind, KIND_TABLE)
        cur.execute(
            "SELECT a.attname, format_type(a.atttypid, a.atttypmod), NOT a.attnotnull, "
            "pg_get_expr(d.adbin, d.adrelid), "
            "(a.attidentity <> '' OR a.attgenerated <> '' "
            " OR COALESCE(pg_get_expr(d.adbin, d.adrelid), '') LIKE 'nextval(%%') "
            "FROM pg_attribute a "
            "LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum "
            "WHERE a.attrelid = %s AND a.attnum > 0 AND NOT a.attisdropped "
            "ORDER BY a.attnum",
            (oid,),
        )
        columns = [
            {
                "name": name,
                "type": ctype or "",
                "nullable": bool(nullable),
                "default": default,
                "pk": False,
                "autoinc": bool(autoinc),
            }
            for name, ctype, nullable, default, autoinc in cur.fetchall()
        ]
        cur.execute(
            "SELECT a.attname FROM pg_index i "
            "JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey) "
            "WHERE i.indrelid = %s AND i.indisprimary",
            (oid,),
        )
        pk_names = {r[0] for r in cur.fetchall()}
        for c in columns:
            c["pk"] = c["name"] in pk_names
        cur.execute(
            "SELECT ic.relname, i.indisunique, "
            "ARRAY(SELECT a.attname FROM unnest(i.indkey) WITH ORDINALITY AS k(attnum, ord) "
            "      JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.attnum "
            "      ORDER BY k.ord) "
            "FROM pg_index i JOIN pg_class ic ON ic.oid = i.indexrelid "
            "WHERE i.indrelid = %s ORDER BY ic.relname",
            (oid,),
        )
        indexes = [
            {"name": name, "columns": list(cols or []), "unique": bool(unique)}
            for name, unique, cols in cur.fetchall()
        ]
        return {
            "columns": columns,
            "indexes": indexes,
            "ddl": self._ddl(cur, oid, kind, schema, table, columns, indexes),
            "kind": kind,
        }

    def _ddl(self, cur, oid, kind, schema, table, columns, indexes) -> str:
        """PostgreSQL has no server-side "show create table"; reconstruct one
        from the catalog (views use the server's own pg_get_viewdef)."""
        qualified = self.qualified(table, schema)
        if kind in (KIND_VIEW, "matview"):
            cur.execute("SELECT pg_get_viewdef(%s, true)", (oid,))
            row = cur.fetchone()
            body = str(row[0]).rstrip().rstrip(";") if row else ""
            head = "CREATE MATERIALIZED VIEW" if kind == "matview" else "CREATE VIEW"
            return "%s %s AS\n%s;" % (head, qualified, body)
        lines = []
        for c in columns:
            line = "  %s %s" % (self.quote_ident(c["name"]), c["type"])
            if not c["nullable"]:
                line += " NOT NULL"
            if c["default"] is not None:
                line += " DEFAULT %s" % c["default"]
            lines.append(line)
        pk = [c["name"] for c in columns if c["pk"]]
        if pk:
            lines.append(
                "  PRIMARY KEY (%s)" % ", ".join(self.quote_ident(n) for n in pk)
            )
        out = "-- reconstructed from the catalog\nCREATE TABLE %s (\n%s\n);" % (
            qualified,
            ",\n".join(lines),
        )
        cur.execute(
            "SELECT indexdef FROM pg_indexes WHERE schemaname = %s AND tablename = %s ORDER BY indexname",
            (schema, table),
        )
        defs = [
            str(r[0]) + ";" for r in cur.fetchall() if r[0] and "_pkey" not in str(r[0])
        ]
        if defs:
            out += "\n" + "\n".join(defs)
        return out

    def use_schema(self, conn: Any, schema: Optional[str]) -> None:
        cur = conn.cursor()
        if schema:
            cur.execute("SET search_path TO %s, public" % self.quote_ident(schema))
        else:
            cur.execute("RESET search_path")

    def set_statement_timeout(self, conn: Any, seconds: float) -> None:
        conn.cursor().execute("SET statement_timeout = %d" % int(seconds * 1000))

    def clear_statement_timeout(self, conn: Any) -> None:
        conn.cursor().execute("RESET statement_timeout")

    def _type_name(self, type_code: Any) -> str:
        if type_code is None:
            return ""
        name, mod = _import_pg()
        try:
            if name == "psycopg":
                t = mod.postgres.types.get(type_code)
                return t.name if t is not None else str(type_code)
            if name == "psycopg2":
                t = mod.extensions.string_types.get(type_code)
                return t.name.lower() if t is not None else str(type_code)
        except Exception:  # noqa: BLE001 — the type name is decoration
            pass
        return str(type_code)


# --------------------------------------------------------------------------- #
# MySQL / MariaDB
# --------------------------------------------------------------------------- #


def _import_mysql() -> Any:
    try:
        import pymysql  # type: ignore

        return pymysql
    except ImportError:
        return None


class MysqlAdapter(Adapter):
    engine = "mysql"
    hierarchy = ("database",)
    driver = "pymysql"
    placeholder = "%s"
    like_op = "LIKE"
    like_escape = "ESCAPE '\\\\'"
    sql_dialect = "mysql"

    @classmethod
    def available(cls) -> bool:
        return _import_mysql() is not None

    @classmethod
    def install_hint(cls) -> str:
        return _install_hint("pymysql")

    def default_database(self, profile: dict) -> Optional[str]:
        return str(profile.get("database") or "") or None

    def connect(self, profile: dict, database: Optional[str] = None) -> Any:
        mod = _import_mysql()
        if mod is None:
            raise DriverMissing(self.engine, self.driver, self.install_hint())
        from pymysql.constants import CLIENT  # type: ignore

        conn = mod.connect(
            host=profile.get("host") or "localhost",
            port=int(profile.get("port") or 3306),
            user=profile.get("user") or None,
            password=str(profile.get("password") or ""),
            database=database or self.default_database(profile),
            connect_timeout=CONNECT_TIMEOUT_S,
            autocommit=True,
            charset="utf8mb4",
            # rowcount = MATCHED rows, not changed rows: an UPDATE that writes
            # the value already there must still read as "1 row" or the stale
            # check in the row batch would call it a conflict.
            client_flag=CLIENT.FOUND_ROWS,
        )
        if profile.get("read_only"):
            conn.cursor().execute("SET SESSION TRANSACTION READ ONLY")
        return conn

    def is_alive(self, conn: Any) -> bool:
        return bool(getattr(conn, "open", True))

    def server_version(self, conn: Any) -> str:
        cur = conn.cursor()
        cur.execute("SELECT VERSION()")
        row = cur.fetchone()
        return "MySQL " + str(row[0]) if row else "MySQL"

    def list_databases(self, conn: Any) -> List[Any]:
        cur = conn.cursor()
        cur.execute("SHOW DATABASES")
        names = [r[0] for r in cur.fetchall()]
        sizes: Dict[str, int] = {}
        try:
            # information_schema statistics (same source as table_rows): data +
            # index length per schema, one grouped read, never a scan — but it
            # can crawl on a huge catalog, so it gets the size-stats bound.
            self.set_statement_timeout(conn, SIZE_STATS_TIMEOUT_S)
            try:
                cur.execute(
                    "SELECT table_schema, SUM(data_length + index_length) "
                    "FROM information_schema.tables GROUP BY table_schema"
                )
                rows = cur.fetchall()
            finally:
                self.clear_statement_timeout(conn)
            for schema_name, size in rows:
                if size is not None:
                    sizes[str(schema_name)] = int(size)
        except Exception:  # noqa: BLE001 — sizes are decoration, names are not
            return names
        return [{"name": n, "size_bytes": sizes.get(str(n))} for n in names]

    def list_tables(self, conn: Any, schema: Optional[str] = None) -> List[dict]:
        cur = conn.cursor()
        cur.execute(
            "SELECT table_name, table_type, table_rows FROM information_schema.tables "
            "WHERE table_schema = DATABASE() ORDER BY table_name"
        )
        out: List[dict] = []
        for name, ttype, trows in cur.fetchall():
            is_view = "VIEW" in str(ttype).upper()
            out.append(
                {
                    "name": name,
                    "kind": KIND_VIEW if is_view else KIND_TABLE,
                    "approx_rows": None if is_view or trows is None else int(trows),
                }
            )
        return out

    def table_info(self, conn: Any, table: str, schema: Optional[str] = None) -> dict:
        cur = conn.cursor()
        cur.execute(
            "SELECT table_type FROM information_schema.tables "
            "WHERE table_schema = DATABASE() AND table_name = %s",
            (table,),
        )
        head = cur.fetchone()
        if head is None:
            raise LookupError("no such table: %s" % table)
        kind = KIND_VIEW if "VIEW" in str(head[0]).upper() else KIND_TABLE
        cur.execute(
            "SELECT column_name, column_type, is_nullable, column_default, column_key, extra "
            "FROM information_schema.columns "
            "WHERE table_schema = DATABASE() AND table_name = %s ORDER BY ordinal_position",
            (table,),
        )
        columns = [
            {
                "name": name,
                "type": ctype or "",
                "nullable": str(nullable).upper() == "YES",
                "default": default,
                "pk": str(key).upper() == "PRI",
                "autoinc": "auto_increment" in str(extra or "").lower(),
            }
            for name, ctype, nullable, default, key, extra in cur.fetchall()
        ]
        q = self.quote_ident(table)
        indexes: List[dict] = []
        if kind == KIND_TABLE:
            cur.execute("SHOW INDEX FROM %s" % q)
            by_name: Dict[str, dict] = {}
            for row in cur.fetchall():
                # Table, Non_unique, Key_name, Seq_in_index, Column_name, …
                non_unique, key_name, col = row[1], row[2], row[4]
                entry = by_name.setdefault(
                    key_name,
                    {"name": key_name, "columns": [], "unique": not non_unique},
                )
                if col is not None:
                    entry["columns"].append(col)
            indexes = list(by_name.values())
        cur.execute("SHOW CREATE TABLE %s" % q)
        row = cur.fetchone()
        ddl = str(row[1]) if row and len(row) > 1 else ""
        return {"columns": columns, "indexes": indexes, "ddl": ddl, "kind": kind}

    def quote_ident(self, name: str) -> str:
        return "`" + str(name).replace("`", "``") + "`"

    def cast_text(self, expr: str) -> str:
        return "CAST(%s AS CHAR)" % expr

    def insert_defaults_sql(self, qualified: str) -> str:
        return "INSERT INTO %s () VALUES ()" % qualified

    def set_statement_timeout(self, conn: Any, seconds: float) -> None:
        # MySQL 5.7.8+: SELECT-only, and MariaDB spells it differently (in
        # seconds). Best-effort by contract — a server that lacks the variable
        # simply runs without the cap.
        ms = int(seconds * 1000)
        cur = conn.cursor()
        try:
            cur.execute("SET SESSION max_execution_time = %d" % ms)
        except Exception:  # noqa: BLE001
            try:
                cur.execute(
                    "SET SESSION max_statement_time = %d" % max(1, int(seconds))
                )
            except Exception:  # noqa: BLE001
                pass

    def clear_statement_timeout(self, conn: Any) -> None:
        cur = conn.cursor()
        for stmt in (
            "SET SESSION max_execution_time = 0",
            "SET SESSION max_statement_time = 0",
        ):
            try:
                cur.execute(stmt)
                return
            except Exception:  # noqa: BLE001
                continue

    def _type_name(self, type_code: Any) -> str:
        if type_code is None:
            return ""
        mod = _import_mysql()
        try:
            from pymysql.constants import FIELD_TYPE  # type: ignore

            for attr in dir(FIELD_TYPE):
                if attr.isupper() and getattr(FIELD_TYPE, attr) == type_code:
                    return attr.lower()
        except Exception:  # noqa: BLE001 — decoration only
            pass
        return str(type_code) if mod else ""


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #

ADAPTERS: Dict[str, Adapter] = {
    SqliteAdapter.engine: SqliteAdapter(),
    PostgresAdapter.engine: PostgresAdapter(),
    MysqlAdapter.engine: MysqlAdapter(),
}


def get_adapter(engine: str) -> Adapter:
    try:
        return ADAPTERS[str(engine or "").lower()]
    except KeyError:
        raise ValueError("unknown engine %r" % (engine,)) from None


def driver_report() -> List[dict]:
    """``[{engine, available, driver, install_hint}]`` for ``GET /drivers``."""
    out: List[dict] = []
    for engine, adapter in ADAPTERS.items():
        available = adapter.available()
        out.append(
            {
                "engine": engine,
                "available": available,
                "driver": adapter.driver,
                "install_hint": "" if available else adapter.install_hint(),
            }
        )
    return out
