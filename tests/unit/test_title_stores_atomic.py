"""The title-keyed web stores write atomically: a failed save keeps the old file.

aliases, hidden_sessions, person_sessions, ownership (owners) and shared_branch
used to rewrite their JSON in place with ``open(path, "w")``, so a crash or a
full disk mid-write left a truncated file and every entry was lost on the next
load. They now go through :mod:`backend.web.core.title_store`.
"""

import json
import os

import pytest

from backend import fsutil
from backend.web.core import (
    aliases,
    hidden_sessions,
    ownership,
    person_sessions,
    shared_branch,
)

P1 = "11111111-1111-4111-8111-111111111111"
P2 = "22222222-2222-4222-8222-222222222222"

# (module, cache global, write #1, write #2, the on-disk doc after write #1)
STORES = [
    (
        aliases,
        "_ALIASES",
        lambda: aliases.set_alias("t", "one"),
        lambda: aliases.set_alias("t", "two"),
        {"t": "one"},
    ),
    (
        hidden_sessions,
        "_HIDDEN",
        lambda: hidden_sessions.set_hidden_by("t", "verify"),
        lambda: hidden_sessions.set_hidden_by("u", "verify"),
        {"t": "verify"},
    ),
    (
        person_sessions,
        "_PERSON",
        lambda: person_sessions.set_person("t", P1),
        lambda: person_sessions.set_person("t", P2),
        {"t": P1},
    ),
    (
        ownership,
        "_OWNERS",
        lambda: ownership.set_owner("t", P1),
        lambda: ownership.set_owner("t", P2),
        {"t": P1},
    ),
    (
        shared_branch,
        "_SHARED",
        lambda: shared_branch.set_shared("t", "origin", "main"),
        lambda: shared_branch.set_shared("t", "origin", "dev"),
        {"t": {"remote": "origin", "branch": "main"}},
    ),
]


@pytest.fixture(params=STORES, ids=lambda s: s[0].__name__.rsplit(".", 1)[-1])
def store(request, tmp_path, monkeypatch):
    mod, cache, first, second, doc = request.param
    path = tmp_path / "store.json"
    monkeypatch.setattr(mod, "_path", lambda: str(path))
    monkeypatch.setattr(mod, cache, None)
    monkeypatch.setattr(ownership, "_presence_changed", lambda title: None)
    yield mod, cache, first, second, doc, path
    monkeypatch.setattr(mod, cache, None)


def test_save_writes_the_same_bytes_json_dump_did(store):
    _mod, _cache, first, _second, doc, path = store
    first()
    assert path.read_text() == json.dumps(doc)


def test_a_failed_write_leaves_the_previous_file_intact(store, monkeypatch):
    mod, cache, first, second, doc, path = store
    first()
    before = path.read_bytes()

    def boom(src, dst):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(fsutil.os, "replace", boom)
    second()  # logged, never raised
    assert path.read_bytes() == before
    assert sorted(os.listdir(path.parent)) == ["store.json"]  # no temp left
    # A "restart" still reads the old entries.
    setattr(mod, cache, None)
    loader = getattr(mod, "_owners", None) or mod._load
    assert loader() == doc


def test_a_truncated_file_reads_as_empty(store):
    mod, cache, _first, _second, _doc, path = store
    path.write_text('{"t": "on')
    setattr(mod, cache, None)
    loader = getattr(mod, "_owners", None) or mod._load
    assert loader() == {}
