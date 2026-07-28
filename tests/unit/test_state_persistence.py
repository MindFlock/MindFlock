"""Durability of the persisted state files (P0 production-readiness fixes).

Covers:
  * atomic replace in ``config._write_file`` (no truncated file, mode applied,
    no temp litter)
  * ``LoadState`` preserving a corrupt ``state.json`` as a ``.corrupt-*``
    backup instead of silently wiping the session list
  * ``state_file_lock`` reentrancy (LoadState/SaveState nest inside a caller's
    lock without deadlocking)
  * the ingestion ledger's atomic write + corrupt-file backup
"""

from __future__ import annotations

import json
import os
import stat

import pytest

from backend.config import config as cfg
from backend.config import state as state_mod
from backend.ticket_ingestion import state as ing_state


def _raise_oserror(*_a, **_k):
    raise OSError("simulated config-dir failure")


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Point ~ (and therefore ~/.mindflock) at a scratch dir."""
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


class TestWriteFileAtomic:
    def test_writes_content_and_mode(self, tmp_path):
        path = str(tmp_path / "out.json")
        cfg._write_file(path, b'{"a":1}', 0o644)
        with open(path, "rb") as f:
            assert f.read() == b'{"a":1}'
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o644

    def test_overwrites_existing(self, tmp_path):
        path = str(tmp_path / "out.json")
        cfg._write_file(path, b"old", 0o644)
        cfg._write_file(path, b"new", 0o644)
        with open(path, "rb") as f:
            assert f.read() == b"new"

    def test_no_temp_litter(self, tmp_path):
        path = str(tmp_path / "out.json")
        cfg._write_file(path, b"data", 0o644)
        assert os.listdir(tmp_path) == ["out.json"]


class TestLoadStateCorruption:
    def test_corrupt_state_is_backed_up_not_wiped(self, home):
        config_dir = cfg.GetConfigDir()
        os.makedirs(config_dir, exist_ok=True)
        state_path = os.path.join(config_dir, state_mod.StateFileName)
        with open(state_path, "w") as f:
            f.write('{"help_screens_seen": 1, "instances": [truncated')

        st = state_mod.LoadState()

        # Fresh default state returned...
        assert st.help_screens_seen == 0
        # ...and the corrupt bytes preserved for recovery.
        backups = [
            n
            for n in os.listdir(config_dir)
            if n.startswith(state_mod.StateFileName + ".corrupt-")
        ]
        assert len(backups) == 1
        with open(os.path.join(config_dir, backups[0])) as f:
            assert "truncated" in f.read()
        # The corrupt file itself was moved aside, not left in place.
        assert not os.path.exists(state_path)

    def test_valid_state_round_trips(self, home):
        st = state_mod.State(help_screens_seen=3, instances_data=b"[]")
        state_mod.SaveState(st)
        loaded = state_mod.LoadState()
        assert loaded.help_screens_seen == 3


class TestStateFileLock:
    def test_reentrant(self, home):
        # SaveState takes the lock itself; nesting it inside a caller-held
        # lock (the Engine.save / Storage.UpdateInstance pattern) must not
        # deadlock.
        with state_mod.state_file_lock():
            with state_mod.state_file_lock():
                state_mod.SaveState(state_mod.DefaultState())
        lock_path = os.path.join(cfg.GetConfigDir(), state_mod.StateFileName + ".lock")
        assert os.path.exists(lock_path)

    def test_released_after_exit(self, home):
        with state_mod.state_file_lock():
            pass
        assert state_mod._LOCK_DEPTH == 0
        assert state_mod._LOCK_FD is None


class TestIngestionLedger:
    def test_write_is_atomic_no_tmp_litter(self, tmp_path):
        ing_state._write_state(tmp_path, {"processed_stories": []})
        names = os.listdir(tmp_path)
        assert names == ["state.json"]
        data = json.loads((tmp_path / "state.json").read_text())
        assert data == {"processed_stories": []}

    def test_corrupt_ledger_backed_up(self, tmp_path):
        (tmp_path / "state.json").write_text("{not json")
        assert ing_state._read_state(tmp_path) == {}
        backups = [
            n for n in os.listdir(tmp_path) if n.startswith("state.json.corrupt-")
        ]
        assert len(backups) == 1
        assert (tmp_path / backups[0]).read_text() == "{not json"

    def test_missing_and_empty_still_empty_dict(self, tmp_path):
        assert ing_state._read_state(tmp_path) == {}
        (tmp_path / "state.json").write_text("")
        assert ing_state._read_state(tmp_path) == {}


class TestStateSchemaVersion:
    """state.json schema versioning + migration ladder (upgrade safety)."""

    def test_missing_key_parses_as_v1(self):
        st = state_mod.State.from_bytes(b'{"help_screens_seen":0,"instances":[]}')
        assert st.schema_version == 1

    def test_explicit_key_is_kept(self):
        st = state_mod.State.from_bytes(
            b'{"schema_version":1,"help_screens_seen":2,"instances":[]}'
        )
        assert st.schema_version == 1
        assert st.help_screens_seen == 2

    def test_v1_never_emits_the_key(self):
        # Emit-on-deviation: ordinary v1 files never carry the key.
        assert b"schema_version" not in state_mod.State().marshal_indent()

    def test_ordinary_file_round_trips_byte_identically(self):
        data = state_mod.State(
            help_screens_seen=3, instances_data=b'[{"title":"x"}]'
        ).marshal_indent()
        assert state_mod.State.from_bytes(data).marshal_indent() == data

    def test_future_version_raises_too_new(self):
        with pytest.raises(state_mod.StateSchemaTooNew) as ei:
            state_mod.State.from_bytes(
                b'{"schema_version":99,"help_screens_seen":0,"instances":[]}'
            )
        assert ei.value.version == 99

    def test_non_int_version_is_a_parse_error(self):
        with pytest.raises(ValueError):
            state_mod.State.from_bytes(
                b'{"schema_version":"two","help_screens_seen":0,"instances":[]}'
            )

    def test_migration_ladder_applied_for_older_files(self, monkeypatch):
        # Pretend the current build is v2 with a 1->2 migration that renames
        # an old key; a v1 (key-less) file must be upgraded through it.
        def migrate_1_to_2(obj):
            obj = dict(obj)
            obj["help_screens_seen"] = obj.pop("seen_screens", 0)
            return obj

        monkeypatch.setattr(state_mod, "CURRENT_SCHEMA_VERSION", 2)
        monkeypatch.setitem(state_mod._MIGRATIONS, 1, migrate_1_to_2)

        st = state_mod.State.from_bytes(b'{"seen_screens":7,"instances":[]}')
        assert st.help_screens_seen == 7
        assert st.schema_version == 2
        # And a v2 state now emits the key (deviates from the default).
        assert b'"schema_version": 2' in st.marshal_indent()

    def test_missing_migration_step_is_a_parse_error(self, monkeypatch):
        monkeypatch.setattr(state_mod, "CURRENT_SCHEMA_VERSION", 2)
        # No _MIGRATIONS[1] registered -> refuse rather than parse wrongly.
        with pytest.raises(ValueError):
            state_mod.State.from_bytes(b'{"help_screens_seen":0,"instances":[]}')

    def test_loadstate_preserves_future_version_file(self, home):
        """Downgrade scenario: a newer build's state.json is moved aside as a
        .newer- backup — never parsed lossily, never overwritten in place."""
        config_dir = cfg.GetConfigDir()
        os.makedirs(config_dir, exist_ok=True)
        state_path = os.path.join(config_dir, state_mod.StateFileName)
        newer_doc = (
            '{"schema_version": 2, "help_screens_seen": 5, "instances": [],'
            ' "future_field": {"x": 1}}'
        )
        with open(state_path, "w") as f:
            f.write(newer_doc)

        st = state_mod.LoadState()

        # Fresh default state returned (this build refuses the newer doc)...
        assert st.help_screens_seen == 0
        # ...the newer bytes are preserved verbatim under .newer-...
        backups = [
            n
            for n in os.listdir(config_dir)
            if n.startswith(state_mod.StateFileName + ".newer-")
        ]
        assert len(backups) == 1
        with open(os.path.join(config_dir, backups[0])) as f:
            assert f.read() == newer_doc
        # ...and it was moved aside, not clobbered in place.
        assert not os.path.exists(state_path)
        # It is NOT treated as corrupt.
        assert not [
            n
            for n in os.listdir(config_dir)
            if n.startswith(state_mod.StateFileName + ".corrupt-")
        ]


class TestDowngradeNotice:
    """The downgrade is non-destructive, but it empties the session list — so
    it has to be visible somewhere the user actually looks, not only in a log.
    """

    @pytest.fixture(autouse=True)
    def _clear(self):
        # Process-global and deliberately sticky, so isolate it per test.
        state_mod.clear_downgrade_notice()
        yield
        state_mod.clear_downgrade_notice()

    def _write_newer(self, home):
        config_dir = cfg.GetConfigDir()
        os.makedirs(config_dir, exist_ok=True)
        path = os.path.join(config_dir, state_mod.StateFileName)
        with open(path, "w") as f:
            f.write('{"schema_version": 99, "help_screens_seen": 0, "instances": []}')
        return path

    def test_no_notice_on_a_normal_load(self, home):
        state_mod.LoadState()
        assert state_mod.downgrade_notice() is None

    def test_notice_records_versions_and_backup(self, home):
        self._write_newer(home)

        state_mod.LoadState()

        notice = state_mod.downgrade_notice()
        assert notice is not None
        assert notice["file_version"] == 99
        assert notice["supported_version"] == state_mod.CURRENT_SCHEMA_VERSION
        # The path is real and holds the preserved bytes.
        assert ".newer-" in notice["backup_path"]
        assert os.path.exists(notice["backup_path"])

    def test_notice_is_a_copy(self, home):
        self._write_newer(home)
        state_mod.LoadState()

        state_mod.downgrade_notice()["file_version"] = 1

        assert state_mod.downgrade_notice()["file_version"] == 99

    def test_clear_drops_it(self, home):
        self._write_newer(home)
        state_mod.LoadState()

        state_mod.clear_downgrade_notice()

        assert state_mod.downgrade_notice() is None

    def test_a_corrupt_file_raises_no_downgrade_notice(self, home):
        """Corruption already has its own .corrupt- path and message."""
        config_dir = cfg.GetConfigDir()
        os.makedirs(config_dir, exist_ok=True)
        with open(os.path.join(config_dir, state_mod.StateFileName), "w") as f:
            f.write("{ not json")

        state_mod.LoadState()

        assert state_mod.downgrade_notice() is None

    def test_doctor_surfaces_it(self, home):
        from backend import doctor

        assert doctor.check_state_schema().status == "ok"

        self._write_newer(home)
        state_mod.LoadState()

        check = doctor.check_state_schema()
        assert check.status == "warn"
        assert "newer MindFlock" in check.detail
        assert ".newer-" in check.detail
        # A warn must not make `mindflock doctor` exit 1 — install.sh runs it.
        assert doctor.to_payload([check])["ok"] is True

    def test_doctor_payload_carries_the_notice_and_version(self, home):
        from backend import __version__, doctor

        payload = doctor.to_payload([])
        assert payload["version"] == __version__
        assert payload["state_notice"] is None

        self._write_newer(home)
        state_mod.LoadState()

        assert doctor.to_payload([])["state_notice"]["file_version"] == 99


class TestIngestionLedgerUpdate:
    """update_processed_story: the in-flight -> terminal status handshake."""

    def _record(self, tmp_path, status="in_flight", story_id="sc-9"):
        from datetime import datetime, timezone

        from backend.ticket_ingestion.models import ProcessingRecord

        ing_state.record_processed_story(
            tmp_path,
            ProcessingRecord(
                story_id=story_id,
                branch=story_id,
                status=status,
                processed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            ),
        )

    def test_in_flight_counts_as_processed(self, tmp_path):
        self._record(tmp_path, status="in_flight")
        assert "sc-9" in ing_state.load_processed_story_ids(tmp_path)

    def test_update_flips_entry_in_place(self, tmp_path):
        self._record(tmp_path, status="in_flight")
        updated = ing_state.update_processed_story(
            tmp_path, "sc-9", status="completed", branch="feature/sc-9/fix"
        )
        assert updated is True
        entries = ing_state._read_state(tmp_path)["processed_stories"]
        assert len(entries) == 1  # updated, not appended
        assert entries[0]["status"] == "completed"
        assert entries[0]["branch"] == "feature/sc-9/fix"

    def test_update_round_trips_through_state_file(self, tmp_path):
        self._record(tmp_path, status="in_flight")
        ing_state.update_processed_story(
            tmp_path, "sc-9", status="failed", failure_reason="boom"
        )
        # Re-read from disk: the new status survives the round-trip and the
        # id still blocks re-ingestion.
        entries = ing_state._read_state(tmp_path)["processed_stories"]
        assert entries[0]["status"] == "failed"
        assert entries[0]["failure_reason"] == "boom"
        assert "sc-9" in ing_state.load_processed_story_ids(tmp_path)

    def test_update_appends_when_no_prior_record(self, tmp_path):
        updated = ing_state.update_processed_story(
            tmp_path, "sc-77", status="completed", branch="feature/sc-77/x"
        )
        assert updated is False
        entries = ing_state._read_state(tmp_path)["processed_stories"]
        assert len(entries) == 1
        assert entries[0]["story_id"] == "sc-77"
        assert entries[0]["status"] == "completed"

    def test_update_targets_most_recent_entry(self, tmp_path):
        self._record(tmp_path, status="skipped")
        self._record(tmp_path, status="in_flight")
        ing_state.update_processed_story(tmp_path, "sc-9", status="completed")
        entries = ing_state._read_state(tmp_path)["processed_stories"]
        assert [e["status"] for e in entries] == ["skipped", "completed"]


class TestStateSerializationEdges:
    """Parse/serialize corners of State not covered by the schema-version tests."""

    def test_nil_instances_marshals_as_null(self):
        # Go emits `null` for a nil RawMessage; an empty instances_data mirrors it.
        data = state_mod.State(instances_data=b"").marshal_indent()
        assert b'"instances": null' in data

    def test_from_bytes_rejects_non_object_root(self):
        with pytest.raises(ValueError):
            state_mod.State.from_bytes(b"[1, 2, 3]")

    def test_from_bytes_rejects_non_int_help_screens(self):
        with pytest.raises(ValueError):
            state_mod.State.from_bytes(b'{"help_screens_seen": "x", "instances": []}')

    def test_missing_instances_key_yields_empty_bytes(self):
        st = state_mod.State.from_bytes(b'{"help_screens_seen": 0}')
        assert st.GetInstances() == b""

    def test_tombstones_parse_and_drop_malformed_entries(self):
        # Only {str: number} survives; bool/str values (and non-str keys) drop.
        st = state_mod.State.from_bytes(
            b'{"help_screens_seen":0,"instances":[],'
            b'"tombstones":{"good":123.5,"bad":"x","flag":true}}'
        )
        assert st.tombstones == {"good": 123.5}

    def test_tombstones_emitted_only_when_non_empty(self):
        assert b"tombstones" not in state_mod.State().marshal_indent()
        data = state_mod.State(tombstones={"dead": 1.0}).marshal_indent()
        assert b'"tombstones"' in data and b'"dead"' in data


class TestStateInstanceStoragePersists:
    """The StateManager write methods persist through SaveState -> disk."""

    def test_set_help_screens_seen_persists(self, home):
        state_mod.State().SetHelpScreensSeen(5)
        assert state_mod.LoadState().help_screens_seen == 5

    def test_save_and_get_instances_round_trip(self, home):
        state_mod.State().SaveInstances(b'[{"title":"x"}]')
        assert state_mod.LoadState().GetInstances() == b'[{"title":"x"}]'

    def test_delete_all_instances_resets_to_empty_array(self, home):
        st = state_mod.State(instances_data=b'[{"title":"x"}]')
        st.SaveInstances(b'[{"title":"x"}]')
        st.DeleteAllInstances()
        assert state_mod.LoadState().GetInstances() == b"[]"


class TestLoadStateErrorPaths:
    def test_config_dir_error_returns_default(self, monkeypatch):
        monkeypatch.setattr(state_mod, "GetConfigDir", _raise_oserror)
        assert state_mod.LoadState().help_screens_seen == 0

    def test_read_oserror_returns_default(self, home):
        config_dir = cfg.GetConfigDir()
        os.makedirs(config_dir, exist_ok=True)
        # A directory where state.json should be -> open() raises OSError (not
        # FileNotFoundError), exercising the "other read error" branch.
        os.mkdir(os.path.join(config_dir, state_mod.StateFileName))
        assert state_mod.LoadState().help_screens_seen == 0

    def test_missing_file_writes_and_returns_default(self, home):
        # No state.json yet -> a default is created on disk and returned.
        st = state_mod.LoadState()
        assert st.help_screens_seen == 0
        assert os.path.exists(os.path.join(cfg.GetConfigDir(), state_mod.StateFileName))


class TestSaveStateErrorPaths:
    def test_config_dir_error_is_wrapped(self, monkeypatch):
        monkeypatch.setattr(state_mod, "GetConfigDir", _raise_oserror)
        with pytest.raises(OSError) as ei:
            state_mod.SaveState(state_mod.DefaultState())
        assert "failed to get config directory" in str(ei.value)

    def test_makedirs_error_is_wrapped(self, home, monkeypatch):
        monkeypatch.setattr(state_mod.os, "makedirs", _raise_oserror)
        with pytest.raises(OSError) as ei:
            state_mod.SaveState(state_mod.DefaultState())
        assert "failed to create config directory" in str(ei.value)


class TestSettingsSchemaVersion:
    """settings.json version-key treatment (same emit-on-deviation pattern)."""

    @pytest.fixture(autouse=True)
    def _isolated_settings(self, tmp_path, monkeypatch):
        from backend.config import settings as settings_mod

        monkeypatch.setenv("MINDFLOCK_SETTINGS_FILE", str(tmp_path / "settings.json"))
        settings_mod.invalidate()
        yield
        settings_mod.invalidate()

    def test_missing_key_parses_as_v1_and_is_not_emitted(self):
        from backend.config import settings as settings_mod

        s = settings_mod.Settings.from_dict({"repository": {"url": "x"}})
        assert s.schema_version == 1
        assert "schema_version" not in s.to_dict()

    def test_future_version_loads_best_effort_and_round_trips_stamp(self):
        from backend.config import settings as settings_mod

        s = settings_mod.Settings.from_dict(
            {"schema_version": 3, "repository": {"url": "x"}, "unknown_group": {}}
        )
        assert s.schema_version == 3
        assert s.repository.url == "x"  # known fields still read
        assert s.to_dict()["schema_version"] == 3  # stamp preserved on save

    def test_migration_ladder_applied(self, monkeypatch):
        from backend.config import settings as settings_mod

        def migrate_1_to_2(d):
            d = dict(d)
            d["repository"] = {"url": d.pop("repo_url", "")}
            return d

        monkeypatch.setattr(settings_mod, "SETTINGS_SCHEMA_VERSION", 2)
        monkeypatch.setitem(settings_mod._SETTINGS_MIGRATIONS, 1, migrate_1_to_2)

        s = settings_mod.Settings.from_dict({"repo_url": "git@x:y.git"})
        assert s.repository.url == "git@x:y.git"
        assert s.schema_version == 2

    def test_missing_migration_step_is_tolerant(self, monkeypatch):
        from backend.config import settings as settings_mod

        # Unlike state.json (which raises), the settings overlay is tolerant: a
        # missing migration step must NOT raise. The document loads best-effort,
        # known fields still read, and the version stays where the ladder
        # stalled (an unknown field simply falls through to config.toml).
        monkeypatch.setattr(settings_mod, "SETTINGS_SCHEMA_VERSION", 2)
        # No _SETTINGS_MIGRATIONS[1] registered.
        s = settings_mod.Settings.from_dict({"repository": {"url": "x"}})
        assert s.repository.url == "x"
        assert s.schema_version == 1
