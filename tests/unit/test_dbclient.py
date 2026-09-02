"""The Database Client extension (backend/web/addons/dbclient), sqlite-backed.

Every test points BOTH stores at tmp files — ``MINDFLOCK_SETTINGS_FILE`` and
``MINDFLOCK_DBCLIENT_FILE`` — and runs the addon's router on a private FastAPI
app, so nothing here can read or write the developer's ``~/.mindflock/``.
The one class that uses the real server app (``TestServerIntegration``) does so
only to prove the extension is registered and its static module is served.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import sqlite3
import subprocess
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.config import settings as S
from backend.web.addons import AppContext
from backend.web.addons.base import SECRET_MASK
from backend.web.addons.dbclient import (
    DbClientAddon,
    adapters as adapters_mod,
    installer as installer_mod,
    service as svc,
    store,
)
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
        payload = client.get(BASE + "/drivers").json()
        drivers = {d["engine"]: d for d in payload["drivers"]}
        assert set(drivers) == {"sqlite", "postgres", "mysql"}
        assert drivers["sqlite"]["available"] is True
        # sqlite is stdlib: never a candidate for the Install button.
        assert drivers["sqlite"]["can_install"] is False
        assert payload["target"] == sys.prefix
        for engine in ("postgres", "mysql"):
            d = drivers[engine]
            assert d["driver"]
            if not d["available"]:
                assert "uv pip install" in d["install_hint"]
                assert "mindflock" in d["install_hint"]
                # Exactly one of the two is offered: a button or an excuse.
                assert bool(d["can_install"]) != bool(d["install_blocked"])


class TestDriverInstall:
    """POST /drivers/install — the Install button behind the "driver is not
    installed" note. Every test stubs the subprocess: nothing here may install
    a package into the developer's environment."""

    @pytest.fixture
    def fake_pg(self, monkeypatch):
        """A missing postgres driver that appears once an install "succeeds"."""
        state = {"installed": False, "calls": []}
        monkeypatch.setattr(
            adapters_mod.PostgresAdapter,
            "available",
            classmethod(lambda cls: state["installed"]),
        )
        monkeypatch.setattr(installer_mod, "_uv_path", lambda: "/fake/bin/uv")
        return state

    def _stub_run(self, monkeypatch, state, code=0, out="done"):
        def run(argv):
            state["calls"].append(list(argv))
            if code == 0:
                state["installed"] = True
            return code, out

        monkeypatch.setattr(installer_mod, "_run", run)

    def test_unknown_engine_is_400(self, client):
        r = client.post(BASE + "/drivers/install", json={"engine": "oracle"})
        assert r.status_code == 400
        assert "oracle" in r.json()["error"]

    def test_sqlite_needs_nothing(self, client):
        r = client.post(BASE + "/drivers/install", json={"engine": "sqlite"}).json()
        assert r["ok"] is True and r["already"] is True

    def test_already_installed_runs_no_install(self, client, monkeypatch, fake_pg):
        fake_pg["installed"] = True
        self._stub_run(monkeypatch, fake_pg)
        r = client.post(BASE + "/drivers/install", json={"engine": "postgres"}).json()
        assert r["ok"] is True and r["already"] is True
        assert fake_pg["calls"] == []

    def test_uv_install_targets_this_interpreter(self, client, monkeypatch, fake_pg):
        self._stub_run(monkeypatch, fake_pg)
        r = client.post(BASE + "/drivers/install", json={"engine": "postgres"}).json()
        assert r["ok"] is True, r
        assert r["method"] == "uv pip"
        assert r["driver"] == "psycopg[binary]"
        assert r["target"] == sys.prefix
        # The venv that serves the app, named explicitly — never a bare `uv pip
        # install` that would land wherever uv felt like.
        assert fake_pg["calls"] == [
            [
                "/fake/bin/uv",
                "pip",
                "install",
                "--python",
                sys.executable,
                "psycopg[binary]",
            ]
        ]

    def test_failure_reports_the_output_and_the_manual_command(
        self, client, monkeypatch, fake_pg
    ):
        self._stub_run(monkeypatch, fake_pg, code=1, out="no matching distribution")
        r = client.post(BASE + "/drivers/install", json={"engine": "postgres"}).json()
        assert r["ok"] is False
        assert "no matching distribution" in r["output"]
        assert "uv pip install" in r["install_hint"]

    def test_every_attempt_is_reported_when_all_of_them_fail(
        self, client, monkeypatch, fake_pg
    ):
        """uv first, then pip: the report must carry both outputs, since the
        first failure is usually the one that explains the second."""
        monkeypatch.setattr(installer_mod, "_has_pip", lambda: True)

        def run(argv):
            fake_pg["calls"].append(list(argv))
            return 1, "uv exploded" if "uv" in argv[0] else "pip exploded"

        monkeypatch.setattr(installer_mod, "_run", run)
        r = client.post(BASE + "/drivers/install", json={"engine": "postgres"}).json()
        assert r["ok"] is False
        assert "uv exploded" in r["output"] and "pip exploded" in r["output"]
        assert len(fake_pg["calls"]) == 2

    def test_pip_fallback_when_uv_is_absent(self, client, monkeypatch, fake_pg):
        monkeypatch.setattr(installer_mod, "_uv_path", lambda: None)
        monkeypatch.setattr(installer_mod, "_has_pip", lambda: True)
        self._stub_run(monkeypatch, fake_pg)
        r = client.post(BASE + "/drivers/install", json={"engine": "postgres"}).json()
        assert r["ok"] is True and r["method"] == "pip"
        assert fake_pg["calls"] == [
            [sys.executable, "-m", "pip", "install", "psycopg[binary]"]
        ]

    def test_a_venv_without_pip_gets_ensurepip_first(
        self, client, monkeypatch, fake_pg
    ):
        monkeypatch.setattr(installer_mod, "_uv_path", lambda: None)
        monkeypatch.setattr(installer_mod, "_has_pip", lambda: False)
        monkeypatch.setattr(installer_mod, "_has_ensurepip", lambda: True)
        self._stub_run(monkeypatch, fake_pg)
        r = client.post(BASE + "/drivers/install", json={"engine": "postgres"}).json()
        assert r["ok"] is True and r["method"] == "ensurepip + pip"
        assert [c[1:3] for c in fake_pg["calls"]] == [
            ["-m", "ensurepip"],
            ["-m", "pip"],
        ]

    def test_system_python_is_refused_not_broken(self, client, monkeypatch, fake_pg):
        """PEP 668 outside a venv: no subprocess at all, and the report says why
        the UI must keep showing the command instead of a button."""
        monkeypatch.setattr(installer_mod, "_in_venv", lambda: False)
        monkeypatch.setattr(installer_mod, "_externally_managed", lambda: True)
        self._stub_run(monkeypatch, fake_pg)
        r = client.post(BASE + "/drivers/install", json={"engine": "postgres"}).json()
        assert r["ok"] is False
        assert "system-managed Python" in r["error"]
        assert r["install_hint"]
        assert fake_pg["calls"] == []
        row = {d["engine"]: d for d in client.get(BASE + "/drivers").json()["drivers"]}[
            "postgres"
        ]
        assert row["can_install"] is False
        assert "system-managed Python" in row["install_blocked"]

    def test_a_second_install_does_not_race_the_first(
        self, client, monkeypatch, fake_pg
    ):
        self._stub_run(monkeypatch, fake_pg)
        assert installer_mod._LOCK.acquire(blocking=False)
        try:
            r = client.post(
                BASE + "/drivers/install", json={"engine": "postgres"}
            ).json()
        finally:
            installer_mod._LOCK.release()
        assert r["ok"] is False and r["busy"] is True
        assert fake_pg["calls"] == []

    def test_env_does_not_carry_another_venv(self, monkeypatch):
        monkeypatch.setenv("VIRTUAL_ENV", "/somewhere/else")
        monkeypatch.setenv("PYTHONPATH", "/dev/tree")
        env = installer_mod._child_env()
        assert "VIRTUAL_ENV" not in env and "PYTHONPATH" not in env
        assert env["UV_PYTHON_DOWNLOADS"] == "never"

    def test_a_body_that_names_no_engine_is_refused(self, client, monkeypatch, fake_pg):
        """The one field this endpoint reads is the one field a caller controls,
        so every degenerate spelling of "nothing" has to land on the same 400 —
        never a traceback, and never an install."""
        self._stub_run(monkeypatch, fake_pg)
        for body in ({}, {"engine": ""}, {"engine": None}, {"other": "postgres"}):
            r = client.post(BASE + "/drivers/install", json=body)
            assert r.status_code == 400, body
            assert r.json()["error"]
        # A body that is not an object at all is rejected by the schema itself.
        assert (
            client.post(BASE + "/drivers/install", json=["postgres"]).status_code == 422
        )
        assert fake_pg["calls"] == []

    def test_the_package_spec_can_never_come_from_the_request(
        self, client, monkeypatch, fake_pg
    ):
        """The whole security model in one test: the body names an ENGINE, and
        the package is looked up from that engine's adapter. So anything that
        looks like a package spec — extra requirements, an index URL, a path — is
        simply an engine nobody has, and no subprocess is spawned for it."""
        self._stub_run(monkeypatch, fake_pg)
        for engine in (
            "postgres; evil-pkg",
            "postgres --index-url http://attacker.invalid/simple",
            "psycopg[binary]",
            "../../../etc/passwd",
        ):
            r = client.post(BASE + "/drivers/install", json={"engine": engine})
            assert r.status_code == 400, engine
        assert fake_pg["calls"] == []

    def test_a_pep_668_interpreter_offers_no_button_for_any_driver(
        self, client, monkeypatch
    ):
        """One refusal, reported in every place the UI reads: the top-level
        capability AND each row, because the driver cache the UI keeps is a plain
        list of rows with no envelope around it."""
        monkeypatch.setattr(installer_mod, "_in_venv", lambda: False)
        monkeypatch.setattr(installer_mod, "_externally_managed", lambda: True)
        payload = client.get(BASE + "/drivers").json()
        assert payload["installer"] == ""
        assert "system-managed Python" in payload["install_blocked"]
        assert payload["target"] == sys.prefix
        assert all(d["can_install"] is False for d in payload["drivers"])
        assert set(payload) == {"drivers", "installer", "install_blocked", "target"}

    def test_a_timed_out_installer_fails_and_leaves_the_lock_free(
        self, client, monkeypatch, fake_pg
    ):
        """A stalled index must not hold the request forever — and, more
        importantly, must not leave the button saying "busy" for the rest of the
        server's life. The timeout is a reported failure like any other."""
        monkeypatch.setattr(installer_mod, "_has_pip", lambda: False)
        monkeypatch.setattr(installer_mod, "_has_ensurepip", lambda: False)

        def _timeout(*a, **kw):
            raise subprocess.TimeoutExpired(
                cmd="uv", timeout=installer_mod.INSTALL_TIMEOUT_S
            )

        monkeypatch.setattr(installer_mod.subprocess, "run", _timeout)
        r = client.post(BASE + "/drivers/install", json={"engine": "postgres"}).json()
        assert r["ok"] is False
        assert "timed out after %ds" % installer_mod.INSTALL_TIMEOUT_S in r["output"]
        assert r["install_hint"]
        # The next click gets a real attempt, not "another install is running".
        assert installer_mod._LOCK.acquire(blocking=False)
        installer_mod._LOCK.release()

    def test_a_huge_installer_log_is_trimmed_to_its_tail(
        self, client, monkeypatch, fake_pg
    ):
        """A source build can print megabytes. The tail is the part that explains
        the failure, so that is what survives into the JSON response."""
        monkeypatch.setattr(installer_mod, "_has_pip", lambda: False)
        monkeypatch.setattr(installer_mod, "_has_ensurepip", lambda: False)
        noise = "x" * (installer_mod.OUTPUT_TAIL_CHARS * 3) + "THE ACTUAL ERROR"
        self._stub_run(monkeypatch, fake_pg, code=1, out=noise)
        r = client.post(BASE + "/drivers/install", json={"engine": "postgres"}).json()
        assert r["ok"] is False
        assert len(r["output"]) == installer_mod.OUTPUT_TAIL_CHARS + 1
        assert r["output"].startswith("\u2026")
        assert r["output"].endswith("THE ACTUAL ERROR")

    def test_an_exit_zero_that_leaves_it_unimportable_is_not_a_success(
        self, client, monkeypatch, fake_pg
    ):
        """The install lands in a LIVE process, so "pip said ok" is not the claim
        the UI makes — "you can query postgres now" is. The finders' caches are
        dropped and the adapter is re-asked, and if it still says no, so does the
        report."""
        invalidated: list = []
        monkeypatch.setattr(
            installer_mod.importlib, "invalidate_caches", lambda: invalidated.append(1)
        )
        # Exits 0 but never flips availability (a wheel for another interpreter,
        # a --target install, a resolver that satisfied the spec from elsewhere).
        monkeypatch.setattr(
            installer_mod,
            "_run",
            lambda argv: (
                fake_pg["calls"].append(list(argv)),
                (0, "Successfully installed"),
            )[1],
        )
        r = client.post(BASE + "/drivers/install", json={"engine": "postgres"}).json()
        assert r["ok"] is False
        assert "still not importable" in r["error"]
        assert r["install_hint"]
        assert invalidated == [1]

    def test_neither_driver_route_runs_on_the_event_loop(self, client, monkeypatch):
        """An install is minutes of subprocess and a ``du``-grade catalog read;
        both routes hand the work to a thread, or one click freezes every other
        session's polling for the duration."""
        loops: list = []

        def _note(*a, **kw):
            try:
                asyncio.get_running_loop()
                loops.append(True)
            except RuntimeError:
                loops.append(False)
            return {
                "ok": True,
                "already": True,
                "engine": "sqlite",
                "driver": "",
                "target": sys.prefix,
            }

        def _report():
            _note()
            return {
                "drivers": [],
                "installer": "",
                "install_blocked": "",
                "target": sys.prefix,
            }

        monkeypatch.setattr(installer_mod, "install_driver", _note)
        monkeypatch.setattr(installer_mod, "drivers_payload", _report)
        client.post(BASE + "/drivers/install", json={"engine": "sqlite"})
        client.get(BASE + "/drivers")
        assert loops == [False, False]


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

    def test_tree_items_normalizes_names_and_dicts(self):
        # Adapters may return bare names or {name, size_bytes} dicts (the
        # postgres/mysql size statistics); the wire shape is always a dict.
        from backend.web.addons.dbclient.service import tree_items

        assert tree_items(["a", "b"]) == [{"name": "a"}, {"name": "b"}]
        assert tree_items([{"name": "public", "size_bytes": 123}]) == [
            {"name": "public", "size_bytes": 123}
        ]
        assert tree_items([{"name": "empty", "size_bytes": None}]) == [
            {"name": "empty", "size_bytes": None}
        ]
        assert tree_items([{"size_bytes": 1}]) == [{"size_bytes": 1, "name": ""}]
        # A present-but-falsy name is a NAME, not a missing one: the sqlite
        # unnamed-schema case must not be rewritten, and an unknown key an
        # adapter adds rides through untouched.
        assert tree_items([{"name": "", "size_bytes": 0, "note": "x"}]) == [
            {"name": "", "size_bytes": 0, "note": "x"}
        ]
        # A bare name is stringified, so the wire shape never carries a number.
        assert tree_items([7]) == [{"name": "7"}]
        assert tree_items([]) == []

    def test_tree_reports_no_size_for_an_engine_that_has_no_statistics(
        self, client, conn_id
    ):
        """The contract widened for postgres/mysql; sqlite has no cheap size
        statistics, so it keeps the legacy shape — the same ``level`` value it
        always had, and no ``size_bytes`` invented for it."""
        body = client.get(_url(conn_id, "/tree")).json()
        assert body["level"] == "tables"
        assert all(i.get("size_bytes") is None for i in body["items"])
        assert all(isinstance(i["name"], str) for i in body["items"])

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
        assert [b["command"] for b in spec["buttons"]] == ["dbclient.explorer"]
        # The SQL button left the bar (SQL rides the palette and the explorer
        # now), but the COMMAND behind it is what both of those invoke — dropping
        # it with the button would orphan the whole surface silently.
        commands = {c["id"] for c in spec["commands"]}
        assert "dbclient.sql" in commands
        assert {"dbclient.explorer", "dbclient.new-query"} <= commands
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


# --------------------------------------------------------------------------- #
# Size statistics on the tree's upper levels (postgres / mysql)
# --------------------------------------------------------------------------- #
class _FakeCursor:
    def __init__(self, conn) -> None:
        self._conn = conn
        self._rows: list = []

    def execute(self, sql, *args) -> None:
        self._conn.sql.append(sql)
        self._rows = self._conn.answer(sql)

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _FakeConn:
    """A DB-API shape thin enough to drive the catalog queries directly: the
    adapters' size statistics are pure SQL + shaping, and a real server is not
    installable in CI."""

    def __init__(self, answer) -> None:
        self.sql: list = []
        self.rollbacks = 0
        self._answer = answer

    def cursor(self):
        return _FakeCursor(self)

    def answer(self, sql):
        return self._answer(sql)

    def rollback(self) -> None:
        self.rollbacks += 1


class TestSizeStatistics:
    """The tree's databases/schemas grew a ``size_bytes`` badge. Sizes are
    DECORATION: an engine that will not answer must still list every name."""

    def test_postgres_databases_carry_their_size(self):
        def answer(sql):
            if "pg_database_size" in sql:
                return [("app", 4096), ("locked_out", None)]
            return []

        conn = _FakeConn(answer)
        rows = adapters_mod.PostgresAdapter().list_databases(conn)
        assert rows == [
            {"name": "app", "size_bytes": 4096},
            # has_database_privilege said no: a name with no size, not a
            # missing database.
            {"name": "locked_out", "size_bytes": None},
        ]

    def test_postgres_schemas_carry_their_size(self):
        def answer(sql):
            if "pg_total_relation_size" in sql:
                return [("public", 8192), ("empty", None)]
            return []

        rows = adapters_mod.PostgresAdapter().list_schemas(_FakeConn(answer))
        assert rows == [
            {"name": "public", "size_bytes": 8192},
            {"name": "empty", "size_bytes": None},
        ]

    def test_the_size_query_is_bounded_and_the_bound_is_always_lifted(self):
        """``pg_database_size`` stats every file of every database and runs on the
        POOLED connection under its lock, so a hang there wedges the whole tree.
        The cap is applied to the statement itself — and released in a ``finally``,
        or the next query on this connection inherits it."""
        ms = int(adapters_mod.SIZE_STATS_TIMEOUT_S * 1000)

        def answer(sql):
            if "pg_database_size" in sql:
                raise RuntimeError("canceling statement due to statement timeout")
            return [("app",)]

        conn = _FakeConn(answer)
        rows = adapters_mod.PostgresAdapter().list_databases(conn)
        assert conn.sql[0] == "SET statement_timeout = %d" % ms
        assert "pg_database_size" in conn.sql[1]
        assert "RESET statement_timeout" in conn.sql
        # ...and the timeout is not merely wrapped in a try: the names survive it.
        assert rows == ["app"]
        assert conn.rollbacks == 1

    def test_postgres_falls_back_to_bare_names_and_lists_them_all(self):
        """A role without CONNECT on some database, or a catalog too big to size
        inside the cap: the tree must still show every database."""

        def answer(sql):
            if "pg_database_size" in sql or "pg_total_relation_size" in sql:
                raise RuntimeError("permission denied")
            return [("app",), ("other",)]

        pg = adapters_mod.PostgresAdapter()
        assert pg.list_databases(_FakeConn(answer)) == ["app", "other"]
        assert pg.list_schemas(_FakeConn(answer)) == ["app", "other"]

    def test_mysql_sums_information_schema_lengths(self):
        def answer(sql):
            if sql == "SHOW DATABASES":
                return [("app",), ("sys",)]
            if "information_schema.tables" in sql:
                return [("app", 1024)]  # sys reports nothing
            return []

        rows = adapters_mod.MysqlAdapter().list_databases(_FakeConn(answer))
        assert rows == [
            {"name": "app", "size_bytes": 1024},
            {"name": "sys", "size_bytes": None},
        ]

    def test_mysql_degrades_to_plain_names_when_information_schema_refuses(self):
        def answer(sql):
            if sql == "SHOW DATABASES":
                return [("app",), ("sys",)]
            if "information_schema.tables" in sql:
                raise RuntimeError("SELECT command denied")
            return []

        assert adapters_mod.MysqlAdapter().list_databases(_FakeConn(answer)) == [
            "app",
            "sys",
        ]

    def test_sqlite_keeps_the_legacy_bare_name_shape(self):
        """The widening is backward compatible by construction: an adapter with
        no statistics keeps returning names, and ``tree_items`` lifts them."""
        assert adapters_mod.SqliteAdapter().list_databases(None) == ["main"]
        assert svc.tree_items(adapters_mod.SqliteAdapter().list_databases(None)) == [
            {"name": "main"}
        ]
