"""Intake's concurrent-session cap (``engine.max_sessions``).

Dropping 30 tickets into the ingest state used to launch 30 agents back to
back: an engine-mode launch returns as soon as the session is live, so the drain
loop never waited for anything. These tests pin the cap end to end:

  * **config** — ``[mindflock].max_sessions`` parses (0 = no limit, the
    default), bad values are config errors, and the settings store's value wins
    over the TOML's — including an explicit 0;
  * **counting** — a ticket occupies a slot exactly while its tmux session is
    alive, under either session spelling, and only if a session was launched
    for it;
  * **holding** — the drain loop waits for a slot, re-reads the cap while it
    waits, mirrors the hold to the beacon, and never wedges on a broken tmux.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from backend.config.settings import EngineSettings
from backend.ticket_ingestion import orchestrator as orch
from backend.ticket_ingestion.config import (
    ConfigError,
    EngineConfig,
    PipelineConfig,
    TicketProviderConfig,
    load_config,
)
from backend.ticket_ingestion.models import ProcessingRecord
from backend.ticket_ingestion.state import record_processed_story

COMMON = """
[ticketing]
provider = "github_issues"

[repository]
url = "git@github.com:org/repo.git"
"""


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "config.toml"
    p.write_text(body, encoding="utf-8")
    return p


# --------------------------------------------------------------------------- #
# Config.
# --------------------------------------------------------------------------- #
def test_no_cap_by_default(tmp_path):
    cfg = load_config(_write(tmp_path, COMMON))
    assert cfg.engine is not None and cfg.engine.max_sessions == 0


def test_toml_cap_parses(tmp_path):
    cfg = load_config(_write(tmp_path, COMMON + "\n[mindflock]\nmax_sessions = 5\n"))
    assert cfg.engine is not None and cfg.engine.max_sessions == 5


@pytest.mark.parametrize("bad", ["-1", '"five"', "true", "2.5"])
def test_bad_toml_cap_is_a_config_error(tmp_path, bad):
    with pytest.raises(ConfigError, match="max_sessions"):
        load_config(_write(tmp_path, COMMON + f"\n[mindflock]\nmax_sessions = {bad}\n"))


def test_settings_round_trip_and_reject_negatives():
    assert EngineSettings.from_dict({"max_sessions": 4}).to_dict() == {
        "max_sessions": 4
    }
    # The Intake field posts the <input>'s string.
    assert EngineSettings.from_dict({"max_sessions": "3"}).max_sessions == 3
    # 0 is an answer ("no limit"), kept distinct from unset.
    assert EngineSettings.from_dict({"max_sessions": 0}).to_dict() == {
        "max_sessions": 0
    }
    assert EngineSettings.from_dict({"max_sessions": -2}).max_sessions is None
    assert EngineSettings.from_dict({}).to_dict() == {}


@pytest.fixture
def settings_file(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    monkeypatch.setenv("MINDFLOCK_SETTINGS_FILE", str(path))
    monkeypatch.delenv("MINDFLOCK_INGESTION_MAX_SESSIONS", raising=False)
    return path


def _layered(raw: dict) -> dict:
    from backend.ticket_ingestion.config import _merge_layers

    return _merge_layers(raw).get("mindflock", {})


def test_settings_cap_overrides_toml(settings_file):
    settings_file.write_text(json.dumps({"engine": {"max_sessions": 3}}))
    assert _layered({"mindflock": {"max_sessions": 8}})["max_sessions"] == 3


def test_settings_zero_lifts_a_toml_cap(settings_file):
    settings_file.write_text(json.dumps({"engine": {"max_sessions": 0}}))
    assert _layered({"mindflock": {"max_sessions": 8}})["max_sessions"] == 0


def test_env_cap_wins(settings_file, monkeypatch):
    settings_file.write_text(json.dumps({"engine": {"max_sessions": 3}}))
    monkeypatch.setenv("MINDFLOCK_INGESTION_MAX_SESSIONS", "7")
    assert _layered({})["max_sessions"] == 7


# --------------------------------------------------------------------------- #
# Counting live ticket sessions.
# --------------------------------------------------------------------------- #
def _ledger(state_dir: Path, slug: str, status: str) -> None:
    record_processed_story(
        state_dir,
        ProcessingRecord(
            story_id=slug,
            branch=slug,
            status=status,
            processed_at=datetime.now(timezone.utc),
        ),
    )


def test_counts_only_launched_tickets_with_a_live_session(tmp_path, monkeypatch):
    for slug, status in [
        ("sc-1", "completed"),  # engine session alive
        ("sc-2", "in_flight"),  # standalone session alive
        ("jira-P.3", "completed"),  # engine name: tmux turns "." into "_"
        ("sc-4", "completed"),  # session closed -> slot is free
        ("sc-5", "failed"),  # never got a session
        ("sc-6", "skipped"),
    ]:
        _ledger(tmp_path, slug, status)
    live = {
        "mindflock_sc-1",
        "sc-2",
        "mindflock_jira-P_3",
        "mindflock_sc-5",
        "sc-6",
        "mindflock_my-own-session",
    }
    monkeypatch.setattr(orch, "_live_tmux_sessions", lambda: live)
    assert orch.live_ticket_sessions(tmp_path) == 3


def test_unprobeable_tmux_counts_as_unknown(tmp_path, monkeypatch):
    _ledger(tmp_path, "sc-1", "completed")
    monkeypatch.setattr(orch, "_live_tmux_sessions", lambda: None)
    assert orch.live_ticket_sessions(tmp_path) is None


# --------------------------------------------------------------------------- #
# Holding the queue.
# --------------------------------------------------------------------------- #
@pytest.fixture
def pipeline(tmp_path, monkeypatch):
    monkeypatch.setattr(orch, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(orch, "_SLOT_POLL_SECONDS", 0)
    cfg = PipelineConfig(
        ticketing=TicketProviderConfig(provider="github_issues"),
        repo_url="git@github.com:org/repo.git",
        workspace_dir=tmp_path / "workspaces",
        engine=EngineConfig(enabled=True, max_sessions=2),
    )
    return orch.PipelineOrchestrator(cfg)


def _beacon(tmp_path: Path) -> dict:
    return json.loads((tmp_path / orch._ACTIVITY_FILE).read_text())


def test_no_cap_never_probes_tmux(pipeline, monkeypatch):
    monkeypatch.setattr(orch, "max_sessions_now", lambda fallback=0: 0)

    def boom(_state_dir):
        raise AssertionError("tmux probed with no cap set")

    monkeypatch.setattr(orch, "live_ticket_sessions", boom)
    asyncio.run(pipeline._wait_for_slot())


def test_holds_until_a_session_ends(pipeline, monkeypatch, tmp_path):
    monkeypatch.setattr(orch, "max_sessions_now", lambda fallback=0: 2)
    counts = iter([2, 3, 2, 1])
    seen_beacon: list = []

    def live(_state_dir):
        n = next(counts)
        if n == 1 and (tmp_path / orch._ACTIVITY_FILE).exists():
            seen_beacon.append(_beacon(tmp_path)["held_for_slot"])
        return n

    monkeypatch.setattr(orch, "live_ticket_sessions", live)
    asyncio.run(pipeline._wait_for_slot())
    # Every reading was consumed: it only let go once the count dropped.
    assert next(counts, None) is None
    # While held, the UI's beacon said so (last reading: 2 of 2) ...
    assert seen_beacon == [{"live": 2, "max": 2}]
    # ... and it is cleared once the ticket goes.
    assert _beacon(tmp_path)["held_for_slot"] is None


def test_raising_the_cap_releases_the_queue(pipeline, monkeypatch):
    caps = iter([2, 2, 5])
    monkeypatch.setattr(orch, "max_sessions_now", lambda fallback=0: next(caps))
    monkeypatch.setattr(orch, "live_ticket_sessions", lambda _d: 2)
    asyncio.run(pipeline._wait_for_slot())
    assert next(caps, None) is None


def test_broken_tmux_does_not_wedge_ingestion(pipeline, monkeypatch):
    monkeypatch.setattr(orch, "max_sessions_now", lambda fallback=0: 1)
    monkeypatch.setattr(orch, "live_ticket_sessions", lambda _d: None)
    asyncio.run(asyncio.wait_for(pipeline._wait_for_slot(), timeout=5))


def test_drain_loop_waits_before_processing(pipeline, monkeypatch):
    """The wait sits between dequeue and process_story — after dequeuing, so a
    slot can't be stolen while the queue was empty; before processing, so the
    ticket keeps its pending marker (crash-safe, and no re-enqueue) while held."""
    order: list[str] = []
    released = asyncio.Event()

    async def wait_for_slot():
        order.append("wait")
        await released.wait()

    async def process_story(item):
        order.append(f"process:{item}")
        raise asyncio.CancelledError  # stop the endless drain loop

    pipeline.config.tickets_enabled = False
    monkeypatch.setattr(pipeline, "_wait_for_slot", wait_for_slot)
    monkeypatch.setattr(pipeline, "process_story", process_story)
    monkeypatch.setattr(orch, "prune_stale_workspaces", lambda _d: None)
    monkeypatch.setattr(orch, "reap_stale_in_flight", lambda *_a, **_k: [])

    async def drive():
        await pipeline._queue.put("sc-9")
        task = asyncio.create_task(pipeline.run())
        await asyncio.sleep(0.05)
        assert order == ["wait"]  # dequeued, held, not processed
        released.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(drive())
    assert order == ["wait", "process:sc-9"]
