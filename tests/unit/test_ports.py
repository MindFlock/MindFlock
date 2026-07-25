"""O4 per-session dev-server port allocation (``backend.web.core.ports``).

Hermetic: the allocation store is pointed at a tmp file via
``MINDFLOCK_PORTS_FILE`` (set for every test by conftest's autouse fixture;
re-pointed here per test for clarity).
"""

from __future__ import annotations

import json

import pytest

from backend.web.core import ports


@pytest.fixture
def pfile(tmp_path, monkeypatch):
    p = tmp_path / "ports.json"
    monkeypatch.setenv("MINDFLOCK_PORTS_FILE", str(p))
    return p


def test_allocate_is_deterministic_and_idempotent(pfile):
    a = ports.allocate("webapp")
    assert a == ports.base_for("webapp")
    assert ports.allocate("webapp") == a  # idempotent
    assert ports.get("webapp") == a
    assert ports.BASE <= a < ports.BASE + ports.BLOCKS * ports.BLOCK_SIZE
    assert (a - ports.BASE) % ports.BLOCK_SIZE == 0


def test_collision_probes_to_next_block(pfile):
    a = ports.allocate("session-a")
    # Force a second title into the same hash slot by pre-seeding the store.
    data = json.loads(pfile.read_text())
    data["squatter"] = ports.base_for("session-b")
    pfile.write_text(json.dumps(data))
    b = ports.allocate("session-b")
    assert b != ports.base_for("session-b")  # probed past the squatter
    assert b != a
    assert (b - ports.BASE) % ports.BLOCK_SIZE == 0


def test_release_and_prune(pfile):
    ports.allocate("gone")
    ports.allocate("kept")
    ports.release("gone")
    assert ports.get("gone") is None
    assert ports.get("kept") is not None
    ports.allocate("stale")
    ports.prune(["kept"])
    assert ports.get("stale") is None
    assert ports.get("kept") is not None
    ports.release("never-allocated")  # no-op, no raise


def test_env_for_exposes_block(pfile):
    env = ports.env_for("webapp")
    base = ports.get("webapp")
    assert env["PORT"] == str(base)
    assert env["MINDFLOCK_PORT_BASE"] == str(base)
    assert env["MINDFLOCK_PORT_COUNT"] == str(ports.BLOCK_SIZE)


def test_corrupt_store_recovers(pfile):
    pfile.write_text("{not json")
    assert ports.allocate("t") == ports.base_for("t")
