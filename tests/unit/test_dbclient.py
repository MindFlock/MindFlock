"""The Database Client extension (backend/web/addons/dbclient), sqlite-backed.

Every test points BOTH stores at tmp files — ``MINDFLOCK_SETTINGS_FILE`` and
``MINDFLOCK_DBCLIENT_FILE`` — and runs the addon's router on a private FastAPI
app, so nothing here can read or write the developer's ``~/.mindflock/``.
The one class that uses the real server app (``TestServerIntegration``) does so
only to prove the extension is registered and its static module is served.
"""

from __future__ import annotations

import base64
import json
import os
import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.config import settings as S
from backend.web.addons import AppContext
from backend.web.addons.base import SECRET_MASK
from backend.web.addons.dbclient import DbClientAddon, service as svc, store
from backend.web.core import events as events_mod

BASE = "/api/dbclient"


def _url(cid: str, suffix: str = "") -> str:
    return BASE + "/connections/" + cid + suffix


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDFLOCK_SETTINGS_FILE", str(tmp_path / "settings.json"))
    monkeypatch.setenv("MINDFLOCK_DBCLIENT_FILE", str(tmp_path / "dbclient.json"))
    S.invalidate()
    yield tmp_path
    S.invalidate()


@pytest.fixture
def db_path(env):
    """A seeded sqlite file: a pk table with a NULL and a BLOB cell, a no-pk
    table, a table whose TEXT primary key holds a NULL (sqlite allows it),
    and a view."""
    p = env / "demo.sqlite"
    con = sqlite3.connect(p)
    con.executescript("""
        CREATE TABLE people (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            age INT,
            photo BLOB,
            note TEXT
        );
        CREATE INDEX idx_people_name ON people(name);
        CREATE TABLE nopk (a TEXT, b TEXT);
        CREATE TABLE weird (k TEXT PRIMARY KEY, v TEXT);
        CREATE VIEW v_people AS SELECT id, name FROM people;
        """)
    con.execute(
        "INSERT INTO people (id, name, age, photo, note) VALUES (1, 'Ada', 36, X'0102', 'first')"
    )
    con.execute(
        "INSERT INTO people (id, name, age, photo, note) VALUES (2, 'Bob', NULL, NULL, '')"
    )
    con.execute(
        "INSERT INTO people (id, name, age, photo, note) VALUES (3, 'Cy', 51, NULL, NULL)"
    )
    con.execute("INSERT INTO nopk VALUES ('x', 'y')")
    con.execute("INSERT INTO weird (k, v) VALUES (NULL, 'nullkey')")
    con.execute("INSERT INTO weird (k, v) VALUES ('x', 'ex')")
    con.commit()
    con.close()
    return p


def _rows(db_path, sql):
    con = sqlite3.connect(db_path)
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


@pytest.fixture
def ctx():
    return AppContext(engine=None, register_task=lambda coro: None)


@pytest.fixture
def addon(env, ctx):
    a = DbClientAddon(ctx)
    yield a
    a.service.close_all()


@pytest.fixture
def client(addon):
    app = FastAPI()
    app.include_router(addon.router)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def conn_id(client, db_path):
    r = client.post(
        BASE + "/connections",
        json={"id": "demo", "name": "Demo", "engine": "sqlite", "file": str(db_path)},
    )
    assert r.status_code == 200, r.text
    return "demo"


@pytest.fixture
def ro_conn_id(client, db_path):
    r = client.post(
        BASE + "/connections",
        json={
            "id": "ro",
            "name": "RO",
            "engine": "sqlite",
            "file": str(db_path),
            "read_only": True,
        },
    )
    assert r.status_code == 200, r.text
    return "ro"


# --------------------------------------------------------------------------- #
# Profiles
# --------------------------------------------------------------------------- #


class TestProfiles:
    def test_crud_masks_and_keeps_the_password(self, client, env):
        r = client.post(
            BASE + "/connections",
            json={
                "id": "pg",
                "name": "PG",
                "engine": "postgres",
                "host": "db.example",
                "user": "me",
                "password": "s3cret",  # pragma: allowlist secret
                "database": "app",
            },
        )
        assert r.status_code == 200, r.text
        assert r.json()["connection"]["password"] == SECRET_MASK
        listed = client.get(BASE + "/connections").json()["connections"]
        assert [c["id"] for c in listed] == ["pg"]
        assert listed[0]["password"] == SECRET_MASK
        assert listed[0]["port"] == 5432  # default filled in
        assert (
            store.get_profile("pg")["password"] == "s3cret"  # pragma: allowlist secret
        )  # pragma: allowlist secret
        # Writing the mask back (what the edit form sends) keeps the secret.
        r = client.post(
            BASE + "/connections",
            json={
                "id": "pg",
                "name": "PG renamed",
                "engine": "postgres",
                "password": SECRET_MASK,
            },
        )
        assert r.status_code == 200
        assert (
            store.get_profile("pg")["password"] == "s3cret"  # pragma: allowlist secret
        )  # pragma: allowlist secret
        assert store.get_profile("pg")["name"] == "PG renamed"
        # The store never touches settings.json, and is owner-only.
        assert not (env / "settings.json").exists()
        assert oct(os.stat(env / "dbclient.json").st_mode & 0o777) == "0o600"
        assert client.delete(_url("pg")).status_code == 200
        assert client.delete(_url("pg")).status_code == 404
        assert client.get(BASE + "/connections").json()["connections"] == []

    def test_new_id_with_masked_password_is_400_naming_the_field(self, client):
        r = client.post(
            BASE + "/connections",
            json={
                "id": "fresh",
                "name": "F",
                "engine": "mysql",
                "password": SECRET_MASK,
            },
        )
        assert r.status_code == 400
        assert r.json()["field"] == "password"
        assert "password" in r.json()["error"]

    def test_sqlite_without_a_file_is_400(self, client):
        r = client.post(
            BASE + "/connections", json={"id": "s", "name": "S", "engine": "sqlite"}
        )
        assert r.status_code == 400
        assert r.json()["field"] == "file"

    def test_unknown_engine_is_400(self, client):
        r = client.post(
            BASE + "/connections", json={"id": "o", "name": "O", "engine": "oracle"}
        )
        assert r.status_code == 400
        assert r.json()["field"] == "engine"

    def test_unknown_connection_is_404(self, client):
        assert client.get(_url("nope", "/tree")).status_code == 404
        assert client.post(_url("nope", "/test")).status_code == 404
        assert (
            client.post(_url("nope", "/query"), json={"sql": "select 1"}).status_code
            == 404
        )


class TestDrivers:
    def test_report_shape(self, client):
        drivers = {
            d["engine"]: d for d in client.get(BASE + "/drivers").json()["drivers"]
        }
        assert set(drivers) == {"sqlite", "postgres", "mysql"}
        assert drivers["sqlite"]["available"] is True
        for engine in ("postgres", "mysql"):
            d = drivers[engine]
            assert d["driver"]
            if not d["available"]:
                assert "uv pip install" in d["install_hint"]
                assert "mindflock" in d["install_hint"]


# --------------------------------------------------------------------------- #
# Test / tree / table info
# --------------------------------------------------------------------------- #


class TestIntrospection:
    def test_test_connection(self, client, conn_id, db_path):
        r = client.post(_url(conn_id, "/test")).json()
        assert r["ok"] is True
        assert r["server"].startswith("SQLite")
        client.post(
            BASE + "/connections",
            json={
                "id": "bad",
                "name": "Bad",
                "engine": "sqlite",
                "file": str(db_path) + ".missing",
            },
        )
        r = client.post(_url("bad", "/test")).json()
        assert r["ok"] is False
        assert "not found" in r["error"]

    def test_tree_is_one_flat_tables_level_for_sqlite(self, client, conn_id):
        r = client.get(_url(conn_id, "/tree")).json()
        assert r["level"] == "tables"
        by_name = {i["name"]: i for i in r["items"]}
        assert set(by_name) == {"people", "nopk", "weird", "v_people"}
        assert by_name["v_people"]["kind"] == "view"
        assert by_name["people"]["kind"] == "table"
        assert by_name["people"]["approx_rows"] is None  # never a COUNT(*) scan

    def test_tree_on_a_broken_connection_is_502(self, client, db_path):
        client.post(
            BASE + "/connections",
            json={
                "id": "bad",
                "name": "Bad",
                "engine": "sqlite",
                "file": str(db_path) + ".missing",
            },
        )
        r = client.get(_url("bad", "/tree"))
        assert r.status_code == 502
        assert "not found" in r.json()["error"]

    def test_table_info(self, client, conn_id):
        r = client.get(_url(conn_id, "/table?table=people")).json()
        cols = {c["name"]: c for c in r["columns"]}
        assert list(cols) == ["id", "name", "age", "photo", "note"]
        assert cols["id"]["pk"] is True and cols["id"]["autoinc"] is True
        assert cols["name"]["nullable"] is False
        assert cols["age"]["pk"] is False
        assert r["pk"] == ["id"]
        assert r["kind"] == "table"
        assert "CREATE TABLE" in r["ddl"]
        idx = {i["name"]: i for i in r["indexes"]}
        assert idx["idx_people_name"]["columns"] == ["name"]
        assert idx["idx_people_name"]["unique"] is False

    def test_view_and_no_pk_table(self, client, conn_id):
        assert (
            client.get(_url(conn_id, "/table?table=v_people")).json()["kind"] == "view"
        )
        assert client.get(_url(conn_id, "/table?table=nopk")).json()["pk"] == []

    def test_unknown_table_is_400(self, client, conn_id):
        r = client.get(_url(conn_id, "/table?table=nope"))
        assert r.status_code == 400
        assert "no such table" in r.json()["error"]


# --------------------------------------------------------------------------- #
# Query
# --------------------------------------------------------------------------- #


class TestQuery:
    def _q(self, client, cid, sql, **extra):
        r = client.post(_url(cid, "/query"), json={"sql": sql, **extra})
        assert r.status_code == 200, r.text
        return r.json()

    def test_select_codec_null_bytes_and_empty_string(self, client, conn_id):
        r = self._q(
            client, conn_id, "SELECT id, name, age, photo, note FROM people ORDER BY id"
        )
        assert r["ok"] is True
        assert [c["name"] for c in r["columns"]] == [
            "id",
            "name",
            "age",
            "photo",
            "note",
        ]
        assert r["row_count"] == 3 and r["truncated"] is False
        ada, bob, cy = r["rows"]
        assert ada[3] == {"$type": "bytes", "len": 2}
        assert bob[2] is None  # NULL → null …
        assert bob[4] == ""  # … distinct from the empty string
        assert cy[4] is None
        assert isinstance(r["elapsed_ms"], (int, float))

    def test_row_cap_and_truncated_flag(self, client, conn_id):
        r = self._q(client, conn_id, "SELECT * FROM people", max_rows=2)
        assert r["row_count"] == 2 and r["truncated"] is True
        # The hard ceiling clamps, it does not reject.
        r = self._q(client, conn_id, "SELECT * FROM people", max_rows=10_000_000)
        assert r["row_count"] == 3

    def test_sql_error_is_200_ok_false(self, client, conn_id):
        r = self._q(client, conn_id, "SELECT * FROM nope")
        assert r["ok"] is False
        assert "no such table" in r["error"]

    def test_single_statement_enforced_server_side(self, client, conn_id):
        r = self._q(client, conn_id, "SELECT 1; SELECT 2")
        assert r["ok"] is False
        assert "one statement" in r["error"]
        # A ';' inside a literal or a comment is not a statement boundary.
        r = self._q(client, conn_id, "SELECT 'a;b' AS x -- ; trailing\n")
        assert r["ok"] is True and r["rows"] == [["a;b"]]

    def test_no_where_update_needs_confirm(self, client, conn_id, db_path):
        r = self._q(client, conn_id, "UPDATE people SET note = 'x'")
        assert r["needs_confirm"] is True
        assert _rows(db_path, "SELECT note FROM people WHERE id = 1") == [("first",)]
        r = self._q(client, conn_id, "UPDATE people SET note = 'x'", confirm=True)
        assert r["ok"] is True and r["affected"] == 3
        assert r["columns"] == [] and r["rows"] == []

    def test_where_inside_a_literal_or_comment_does_not_count(self, client, conn_id):
        assert (
            self._q(client, conn_id, "UPDATE people SET note = 'where'")[
                "needs_confirm"
            ]
            is True
        )
        assert (
            self._q(client, conn_id, "UPDATE people SET note = 'y' -- where id = 1")[
                "needs_confirm"
            ]
            is True
        )
        assert (
            self._q(client, conn_id, "DELETE FROM people /* where */")["needs_confirm"]
            is True
        )

    def test_write_with_where_runs_without_confirm(self, client, conn_id, db_path):
        r = self._q(client, conn_id, "DELETE FROM people WHERE id = 2")
        assert r["ok"] is True and r["affected"] == 1
        assert _rows(db_path, "SELECT count(*) FROM people") == [(2,)]

    def test_read_only_profile_rejects_writes_via_the_engine(
        self, client, ro_conn_id, db_path
    ):
        assert self._q(client, ro_conn_id, "SELECT count(*) FROM people")["rows"] == [
            [3]
        ]
        r = self._q(client, ro_conn_id, "UPDATE people SET note = 'z' WHERE id = 1")
        assert r["ok"] is False
        assert "readonly" in r["error"].replace("-", "").lower()
        assert _rows(db_path, "SELECT note FROM people WHERE id = 1") == [("first",)]

    def test_long_strings_are_truncated_with_a_marker(self, client, conn_id):
        big = "x" * 9000
        r = self._q(
            client,
            conn_id,
            "INSERT INTO people (id, name, note) VALUES (9, 'Big', '%s')" % big,
        )
        assert r["ok"] is True
        r = self._q(client, conn_id, "SELECT note FROM people WHERE id = 9")
        cell = r["rows"][0][0]
        assert cell["$type"] == "truncated" and cell["len"] == 9000
        assert len(cell["text"]) == svc.CELL_TEXT_LIMIT

    def test_query_event_carries_no_sql(self, client, conn_id, ctx):
        seen = []
        unsub = ctx.subscribe("addon.dbclient.query", seen.append)
        try:
            self._q(client, conn_id, "SELECT 1")
        finally:
            unsub()
        assert len(seen) == 1
        data = seen[0]["data"]
        assert data["connection"] == conn_id and data["ok"] is True
        assert "elapsed_ms" in data and "sql" not in json.dumps(seen[0])

    def test_empty_sql_is_400(self, client, conn_id):
        assert (
            client.post(_url(conn_id, "/query"), json={"sql": "  "}).status_code == 400
        )


# --------------------------------------------------------------------------- #
# Table data
# --------------------------------------------------------------------------- #


class TestTableData:
    def _page(self, client, cid, **body):
        payload = {
            "table": "people",
            "page": 1,
            "page_size": 50,
            "sort": [],
            "filters": [],
        }
        payload.update(body)
        r = client.post(_url(cid, "/table-data"), json=payload)
        assert r.status_code == 200, r.text
        return r.json()

    def _ids(self, res):
        return [row[0] for row in res["rows"]]

    def test_default_page_shape(self, client, conn_id):
        r = self._page(client, conn_id)
        assert r["ok"] is True
        assert [c["name"] for c in r["columns"]] == [
            "id",
            "name",
            "age",
            "photo",
            "note",
        ]
        assert r["pk"] == ["id"] and r["kind"] == "table"
        assert r["page"] == 1 and r["page_size"] == 50
        assert r["has_more"] is False and r["total_approx"] is None
        assert r["rows"][0][3] == {"$type": "bytes", "len": 2}
        assert r["rows"][1][2] is None and r["rows"][1][4] == ""

    def test_sort(self, client, conn_id):
        assert self._ids(
            self._page(client, conn_id, sort=[{"column": "age", "dir": "desc"}])
        ) == [3, 1, 2]
        assert self._ids(
            self._page(client, conn_id, sort=[{"column": "age", "dir": "asc"}])
        ) == [2, 1, 3]

    def test_filters(self, client, conn_id):
        f = lambda column, op, value=None: [
            {"column": column, "op": op, "value": value}
        ]  # noqa: E731
        assert self._ids(
            self._page(client, conn_id, filters=f("name", "eq", "Bob"))
        ) == [2]
        assert self._ids(
            self._page(client, conn_id, filters=f("name", "ne", "Bob"))
        ) == [1, 3]
        assert self._ids(
            self._page(client, conn_id, filters=f("name", "contains", "a"))
        ) == [1]
        assert self._ids(self._page(client, conn_id, filters=f("age", "null"))) == [2]
        assert self._ids(self._page(client, conn_id, filters=f("age", "notnull"))) == [
            1,
            3,
        ]
        # Typed as text by the grid, coerced by the column's INT type.
        assert self._ids(self._page(client, conn_id, filters=f("age", "gt", "40"))) == [
            3
        ]
        assert self._ids(self._page(client, conn_id, filters=f("age", "lt", "40"))) == [
            1
        ]
        # LIKE metacharacters in the needle are literal.
        assert (
            self._ids(self._page(client, conn_id, filters=f("name", "contains", "%")))
            == []
        )

    def test_paging_has_more_without_count(self, client, conn_id):
        r = self._page(client, conn_id, page_size=2)
        assert self._ids(r) == [1, 2] and r["has_more"] is True
        r = self._page(client, conn_id, page_size=2, page=2)
        assert self._ids(r) == [3] and r["has_more"] is False

    def test_bad_column_dir_and_op_are_400(self, client, conn_id):
        base = {"table": "people", "page": 1, "page_size": 50}
        r = client.post(
            _url(conn_id, "/table-data"),
            json={**base, "sort": [{"column": "nope", "dir": "asc"}]},
        )
        assert r.status_code == 400 and "nope" in r.json()["error"]
        r = client.post(
            _url(conn_id, "/table-data"),
            json={**base, "sort": [{"column": "id", "dir": "asc; DROP"}]},
        )
        assert r.status_code == 400 and "asc or desc" in r.json()["error"]
        r = client.post(
            _url(conn_id, "/table-data"),
            json={**base, "filters": [{"column": "id", "op": "regex", "value": "x"}]},
        )
        assert r.status_code == 400 and "regex" in r.json()["error"]
        r = client.post(
            _url(conn_id, "/table-data"),
            json={**base, "filters": [{"column": "age", "op": "gt", "value": "abc"}]},
        )
        assert r.status_code == 400 and "integer" in r.json()["error"]

    def test_view_pages_as_read_only_kind(self, client, conn_id):
        r = self._page(client, conn_id, table="v_people")
        assert r["kind"] == "view" and r["pk"] == []
        assert self._ids(r) == [1, 2, 3]


# --------------------------------------------------------------------------- #
# Rows
# --------------------------------------------------------------------------- #


class TestRows:
    def _rows(self, client, cid, operations, preview=False, table="people"):
        r = client.post(
            _url(cid, "/rows"),
            json={"table": table, "operations": operations, "preview": preview},
        )
        return r

    def test_preview_returns_statements_and_changes_nothing(
        self, client, conn_id, db_path
    ):
        ops = [
            {"action": "update", "values": {"note": "n"}, "where_pk": {"id": 1}},
            {"action": "insert", "values": {"name": "Dee", "age": "7"}},
            {"action": "delete", "where_pk": {"id": 2}},
        ]
        r = self._rows(client, conn_id, ops, preview=True)
        assert r.status_code == 200, r.text
        stmts = r.json()["statements"]
        assert [s["action"] for s in stmts] == ["update", "insert", "delete"]
        assert stmts[0]["sql"] == 'UPDATE "people" SET "note" = ? WHERE "id" = ?'
        assert stmts[0]["params"] == ["n", 1]
        assert stmts[1]["sql"] == 'INSERT INTO "people" ("name", "age") VALUES (?, ?)'
        assert stmts[1]["params"] == ["Dee", 7]  # "7" coerced by the INT column
        assert stmts[2]["sql"] == 'DELETE FROM "people" WHERE "id" = ?'
        assert _rows(db_path, "SELECT count(*) FROM people") == [(3,)]

    def test_execute_batch_in_one_transaction(self, client, conn_id, db_path):
        ops = [
            {
                "action": "update",
                "values": {"note": {"$null": True}},
                "where_pk": {"id": 1},
            },
            {"action": "insert", "values": {"name": "Dee", "age": "7"}},
            {"action": "delete", "where_pk": {"id": 2}},
        ]
        r = self._rows(client, conn_id, ops)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True and body["stale"] == []
        assert [x["affected"] for x in body["results"]] == [1, 1, 1]
        assert _rows(db_path, "SELECT id, name, age, note FROM people ORDER BY id") == [
            (1, "Ada", 36, None),
            (3, "Cy", 51, None),
            (4, "Dee", 7, None),
        ]

    def test_mid_batch_failure_rolls_everything_back(self, client, conn_id, db_path):
        ops = [
            {"action": "update", "values": {"note": "z"}, "where_pk": {"id": 3}},
            {"action": "insert", "values": {"age": 1}},  # name is NOT NULL
        ]
        body = self._rows(client, conn_id, ops).json()
        assert body["ok"] is False and body["failed_index"] == 1
        assert "NOT NULL" in body["error"]
        assert _rows(db_path, "SELECT note FROM people WHERE id = 3") == [(None,)]

    def test_stale_row_rolls_everything_back(self, client, conn_id, db_path):
        ops = [
            {"action": "update", "values": {"note": "a"}, "where_pk": {"id": 1}},
            {"action": "update", "values": {"note": "b"}, "where_pk": {"id": 999}},
        ]
        body = self._rows(client, conn_id, ops).json()
        assert body["ok"] is False
        assert [s["index"] for s in body["stale"]] == [1]
        assert body["stale"][0]["affected"] == 0
        assert (
            body["results"][1]["stale"] is True and body["results"][0]["stale"] is False
        )
        assert _rows(db_path, "SELECT note FROM people WHERE id = 1") == [("first",)]

    def test_null_primary_key_matches_with_is_null(self, client, conn_id, db_path):
        ops = [
            {
                "action": "update",
                "values": {"v": "changed"},
                "where_pk": {"k": {"$null": True}},
            }
        ]
        r = self._rows(client, conn_id, ops, preview=True, table="weird")
        assert (
            r.json()["statements"][0]["sql"]
            == 'UPDATE "weird" SET "v" = ? WHERE "k" IS NULL'
        )
        body = self._rows(client, conn_id, ops, table="weird").json()
        assert body["ok"] is True
        assert _rows(db_path, "SELECT v FROM weird WHERE k IS NULL") == [("changed",)]
        assert _rows(db_path, "SELECT v FROM weird WHERE k = 'x'") == [("ex",)]

    def test_no_pk_table_and_view_are_read_only(self, client, conn_id):
        r = self._rows(
            client, conn_id, [{"action": "insert", "values": {"a": "1"}}], table="nopk"
        )
        assert r.status_code == 400 and "primary key" in r.json()["error"]
        r = self._rows(
            client,
            conn_id,
            [{"action": "delete", "where_pk": {"id": 1}}],
            table="v_people",
        )
        assert r.status_code == 400 and "read only" in r.json()["error"]

    def test_read_only_profile_refuses_execute_but_previews(
        self, client, ro_conn_id, db_path
    ):
        ops = [{"action": "delete", "where_pk": {"id": 1}}]
        assert self._rows(client, ro_conn_id, ops, preview=True).json()["ok"] is True
        body = self._rows(client, ro_conn_id, ops).json()
        assert body["ok"] is False and "read-only" in body["error"]
        assert _rows(db_path, "SELECT count(*) FROM people") == [(3,)]

    def test_request_validation(self, client, conn_id):
        r = self._rows(
            client,
            conn_id,
            [{"action": "update", "values": {"nope": 1}, "where_pk": {"id": 1}}],
        )
        assert r.status_code == 400 and "nope" in r.json()["error"]
        r = self._rows(client, conn_id, [{"action": "update", "values": {"note": 1}}])
        assert r.status_code == 400 and "where_pk" in r.json()["error"]
        r = self._rows(
            client,
            conn_id,
            [{"action": "update", "values": {"note": 1}, "where_pk": {"name": "Ada"}}],
        )
        assert r.status_code == 400 and "primary key" in r.json()["error"]
        r = self._rows(client, conn_id, [{"action": "upsert", "values": {}}])
        assert r.status_code == 400 and "upsert" in r.json()["error"]
        r = self._rows(client, conn_id, [])
        assert r.status_code == 400
        # The read-only cell markers can never be written back.
        r = self._rows(
            client,
            conn_id,
            [
                {
                    "action": "update",
                    "values": {"photo": {"$type": "bytes", "len": 2}},
                    "where_pk": {"id": 1},
                }
            ],
        )
        assert r.status_code == 400 and "read-only" in r.json()["error"]


# --------------------------------------------------------------------------- #
# Export
# --------------------------------------------------------------------------- #


class TestExport:
    def test_csv_download(self, client, conn_id):
        r = client.get(_url(conn_id, "/export?table=people&format=csv"))
        assert r.status_code == 200, r.text
        assert r.headers["content-type"].startswith("text/csv")
        cd = r.headers["content-disposition"]
        assert 'filename="people.csv"' in cd and "filename*=UTF-8''people.csv" in cd
        lines = r.text.strip().splitlines()
        assert lines[0] == "id,name,age,photo,note"
        assert lines[1] == "1,Ada,36,AQI=,first"  # bytes → base64, full fidelity
        assert lines[2] == "2,Bob,,,"  # NULL and "" both empty in CSV
        assert len(lines) == 4

    def test_json_download(self, client, conn_id):
        r = client.get(_url(conn_id, "/export?table=people&format=json"))
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("application/json")
        rows = json.loads(r.text)
        assert [x["id"] for x in rows] == [1, 2, 3]
        assert base64.b64decode(rows[0]["photo"]) == b"\x01\x02"
        assert rows[1]["age"] is None and rows[1]["note"] == ""

    def test_bad_requests(self, client, conn_id):
        assert (
            client.get(_url(conn_id, "/export?table=people&format=xml")).status_code
            == 400
        )
        assert (
            client.get(_url(conn_id, "/export?table=nope&format=csv")).status_code
            == 400
        )
        assert client.get(_url("nope", "/export?table=people")).status_code == 404

    def test_adhoc_sql_export(self, client, conn_id):
        r = client.post(
            _url(conn_id, "/export"),
            json={"sql": "SELECT id, name FROM people WHERE id < 3", "format": "csv"},
        )
        assert r.status_code == 200, r.text
        assert 'filename="query.csv"' in r.headers["content-disposition"]
        assert r.text.strip().splitlines() == ["id,name", "1,Ada", "2,Bob"]
        # Guarded like any other statement: a no-WHERE write is refused as JSON.
        r = client.post(
            _url(conn_id, "/export"),
            json={"sql": "DELETE FROM people", "format": "json"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is False and "confirmation" in body["error"]

    def test_content_disposition_non_ascii(self):
        cd = svc.content_disposition("données.csv")
        assert cd.startswith('attachment; filename="donn_es.csv"')
        assert cd.endswith("filename*=UTF-8''donn%C3%A9es.csv")


# --------------------------------------------------------------------------- #
# Pure helpers: scanner + codec
# --------------------------------------------------------------------------- #


class TestScanner:
    def test_split_respects_quotes_comments_and_dollar_quotes(self):
        sql = (
            "select 'a;b'; -- c;\n"
            'select "x;y"; /* z; /* nested; */ still */\n'
            "create function f() returns void as $body$ begin; end; $body$ language plpgsql;\n"
            "select `q;r`"
        )
        # A comment stays attached to the statement that follows it (parity
        # with sql.js — the engine ignores it); comment-only segments vanish.
        assert svc.split_statements(sql) == [
            "select 'a;b'",
            '-- c;\nselect "x;y"',
            "/* z; /* nested; */ still */\ncreate function f() returns void as $body$ begin; end; $body$ language plpgsql",
            "select `q;r`",
        ]
        assert svc.split_statements("-- only a comment\n/* and another */") == []

    def test_mysql_dialect(self):
        # '#' opens a comment and a backslash escapes the quote — mysql only.
        assert svc.split_statements(
            "select 1; # c;\nselect 'a\\';b'", dialect="mysql"
        ) == [
            "select 1",
            "# c;\nselect 'a\\';b'",
        ]
        # Backslash is NOT an escape in the standard dialect.
        assert svc.split_statements("select 'a\\'; select 2") == [
            "select 'a\\'",
            "select 2",
        ]

    def test_classify(self):
        assert svc.classify_statement("UPDATE t SET a = 'where'") == ("UPDATE", False)
        assert svc.classify_statement("  (delete from t where x)") == ("DELETE", True)
        assert svc.classify_statement("select $$ where $$") == ("SELECT", False)
        assert svc.classify_statement("-- where\nselect 1") == ("SELECT", False)


class TestCodec:
    def test_encode(self):
        import datetime
        import decimal

        assert svc.encode_value(None) is None
        assert svc.encode_value(True) is True
        assert svc.encode_value(b"\x00" * 3) == {"$type": "bytes", "len": 3}
        assert svc.encode_value(b"\x00\x01", full=True) == "AAE="
        assert svc.encode_value(decimal.Decimal("1.50")) == "1.50"
        assert svc.encode_value(datetime.date(2026, 8, 28)) == "2026-08-28"
        assert svc.encode_value(float("nan")) == "nan"
        assert svc.encode_value("x" * 8193)["len"] == 8193
        assert svc.encode_value("x" * 8193, full=True) == "x" * 8193

    def test_decode_by_type_family(self):
        assert svc.decode_value({"$null": True}, "TEXT", "c") is None
        assert svc.decode_value("42", "BIGINT", "c") == 42
        assert svc.decode_value("1.5", "double precision", "c") == 1.5
        assert svc.decode_value("yes", "boolean", "c") is True
        assert (
            svc.decode_value("1.50", "NUMERIC(10,2)", "c") == "1.50"
        )  # precision stays with the engine
        assert svc.decode_value("x", "interval", "c") == "x"  # INTERVAL is not INT
        with pytest.raises(svc.RequestError):
            svc.decode_value("abc", "INT", "c")
        with pytest.raises(svc.RequestError):
            svc.decode_value("maybe", "bool", "c")


# --------------------------------------------------------------------------- #
# Through the real server: registration + static module
# --------------------------------------------------------------------------- #


class TestServerIntegration:
    @pytest.fixture
    def server_client(self, env):
        from backend.web.server import app

        with TestClient(app) as c:
            yield c

    def test_manifest_static_module_and_disable_toggle(self, server_client):
        addons = {a["id"]: a for a in server_client.get("/api/addons").json()["addons"]}
        ext = addons["dbclient"]
        assert ext["label"] == "Database Client"
        assert ext["origin"] == "builtin" and ext["enabled"] is True
        spec = ext["extension"]
        assert spec["module"] == "/extensions/dbclient/index.js"
        assert spec["bar_label"] == "Database" and spec["stylesheet"] is True
        assert [b["command"] for b in spec["buttons"]] == [
            "dbclient.explorer",
            "dbclient.sql",
        ]
        assert {s["id"]: s["kind"] for s in spec["surfaces"]} == {
            "main": "dialog",
            "query": "pane",
            "table": "pane",
        }
        # The built-in module rides the main static mount — no per-extension mount.
        r = server_client.get("/extensions/dbclient/index.js")
        assert r.status_code == 200 and "javascript" in r.headers["content-type"]
        assert "activate(" in r.text
        r = server_client.get("/extensions/dbclient/style.css")
        assert r.status_code == 200 and "css" in r.headers["content-type"]
        # Disable → enabled flips on the next manifest fetch; re-enable restores.
        server_client.post(
            "/api/settings", json={"extensions": {"disabled": ["dbclient"]}}
        )
        addons = {a["id"]: a for a in server_client.get("/api/addons").json()["addons"]}
        assert addons["dbclient"]["enabled"] is False
        server_client.post("/api/settings", json={"extensions": {"disabled": []}})
        addons = {a["id"]: a for a in server_client.get("/api/addons").json()["addons"]}
        assert addons["dbclient"]["enabled"] is True
