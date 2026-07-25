"""Scheduled window-refresh (roadmap E): config persistence, due-time logic,
and the HTTP config endpoint."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.web import server
from backend.web.core import window_refresh as wr

client = TestClient(server.app)


@pytest.fixture(autouse=True)
def _isolated_store(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDFLOCK_WINDOW_REFRESH_FILE", str(tmp_path / "wr.json"))


def test_defaults_disabled():
    cfg = wr.get_config()
    assert cfg["enabled"] is False
    assert cfg["providers"] == []


def test_set_and_get_roundtrip():
    wr.set_config(enabled=True, interval_hours=5, providers=["claude", "codex"])
    cfg = wr.get_config()
    assert cfg["enabled"] is True
    assert cfg["interval_hours"] == 5.0
    assert cfg["providers"] == ["claude", "codex"]


def test_interval_is_clamped():
    assert wr.set_config(interval_hours=0.01)["interval_hours"] == 0.25  # floor 15m
    assert wr.set_config(interval_hours=9999)["interval_hours"] == 168.0  # ceil 1wk


def test_due_providers_respects_enabled_and_interval():
    wr.set_config(enabled=False, providers=["claude"])
    assert wr.due_providers(now=1_000_000.0) == []  # disabled → never due

    wr.set_config(enabled=True, interval_hours=5, providers=["claude"])
    # Never fired → due now.
    assert "claude" in wr.due_providers(now=1_000_000.0)
    # Just fired → not due until a full interval passes.
    wr.record_fired("claude", now=1_000_000.0)
    assert wr.due_providers(now=1_000_000.0 + 60) == []
    assert "claude" in wr.due_providers(now=1_000_000.0 + 5 * 3600 + 1)


def test_next_fire_at():
    wr.set_config(enabled=True, interval_hours=5, providers=["claude"])
    wr.record_fired("claude", now=1000.0)
    assert wr.next_fire_at("claude") == 1000.0 + 5 * 3600
    assert wr.next_fire_at("codex") is None  # not scheduled


def test_api_get_and_post():
    r = client.post(
        "/api/window-refresh",
        json={"enabled": True, "interval_hours": 5, "providers": ["claude"]},
    )
    assert r.status_code == 200
    assert r.json()["enabled"] is True

    body = client.get("/api/window-refresh").json()
    assert body["enabled"] is True
    assert body["providers"] == ["claude"]
    # Each option carries the provider's window knowledge.
    claude = next(o for o in body["options"] if o["name"] == "claude")
    assert claude["window"]["kind"] == "rolling"
    assert claude["next_fire"] is not None


def test_anchor_time_validation_and_clear():
    assert wr.set_config(anchor_time="09:30")["anchor_time"] == "09:30"
    assert (
        wr.set_config(anchor_time="25:00")["anchor_time"] == "09:30"
    )  # invalid → kept
    assert (
        wr.set_config(anchor_time="9:5")["anchor_time"] == "09:30"
    )  # bad minute → kept
    assert wr.set_config(anchor_time="")["anchor_time"] == ""  # cleared


def test_daily_anchor_due_and_next_fire():
    wr.set_config(enabled=True, anchor_time="09:00", providers=["claude"])
    base = 1_700_000_000.0
    anchor = wr._today_anchor_epoch(base, 9, 0)
    before, after = anchor - 3600, anchor + 60  # 08:00 / 09:01 local, same day
    # Before the anchor: not due; next fire is today's anchor.
    assert wr.due_providers(now=before) == []
    assert wr.next_fire_at("claude", now=before) == anchor
    # At/after the anchor, not yet fired today: due.
    assert "claude" in wr.due_providers(now=after)
    # Once fired today, held until tomorrow's anchor.
    wr.record_fired("claude", now=after)
    assert wr.due_providers(now=after + 120) == []
    assert wr.next_fire_at("claude", now=after) == anchor + 86400.0


def test_anchor_time_takes_precedence_over_interval():
    # A short interval would say "due", but the daily anchor governs instead.
    wr.set_config(
        enabled=True, interval_hours=0.25, anchor_time="09:00", providers=["claude"]
    )
    anchor = wr._today_anchor_epoch(1_700_000_000.0, 9, 0)
    assert (
        wr.due_providers(now=anchor - 3600) == []
    )  # interval ignored; anchor not reached


def test_api_anchor_time_roundtrips():
    r = client.post(
        "/api/window-refresh",
        json={"enabled": True, "anchor_time": "08:15", "providers": ["claude"]},
    )
    assert r.status_code == 200 and r.json()["anchor_time"] == "08:15"
    assert client.get("/api/window-refresh").json()["anchor_time"] == "08:15"


def test_settings_ui_present():
    html = client.get("/").text
    js = client.get("/app.js").text
    for el in ("wr-enabled", "wr-interval", "wr-anchor", "wr-providers", "wr-status"):
        assert '"' + el + '"' in js, el
    js = client.get("/app.js").text
    assert "/api/window-refresh" in js
    assert "WindowRefresh" in js
    assert "anchor_time" in js
