"""Per-CLI usage-window knowledge (roadmap E).

MindFlock needs to know how each major agent CLI's usage limits reset in time,
so the scheduled window-refresh can anchor the right cadence and the UI can
explain it. These lock that knowledge for the bundled providers and the
user-TOML escape hatch.
"""

from __future__ import annotations

from backend import providers
from backend.providers import config as pcfg


def _window(program: str) -> dict:
    return providers.resolve(program).usage_window()


def test_claude_is_rolling_five_hours():
    w = _window("claude")
    assert w["kind"] == "rolling"
    assert w["hours"] == 5.0
    assert w["weekly_hours"] == 168.0
    assert "5-hour" in w["note"]


def test_codex_rolling_plus_weekly():
    w = _window("codex")
    assert w["kind"] == "rolling"
    assert w["hours"] == 5.0
    assert w["weekly_hours"] == 168.0


def test_aider_has_no_managed_window():
    w = _window("aider")
    assert w["kind"] == ""
    assert w["hours"] == 0.0
    assert w["note"]  # still explains WHY (own API key)


def test_unknown_provider_defaults_to_no_window():
    # The fallback provider for an unrecognised CLI reports "unknown".
    w = _window("some-random-cli")
    assert w["kind"] == ""
    assert w["hours"] == 0.0


def test_user_toml_can_declare_a_window():
    raw = {
        "provider": {"name": "mycli", "program": "mycli"},
        "usage": {
            "window_kind": "rolling",
            "window_hours": 3,
            "weekly_hours": 100,
            "note": "my plan",
        },
    }
    cfg = pcfg._config_from_toml(raw)
    w = cfg.usage_window()
    assert w["kind"] == "rolling"
    assert w["hours"] == 3.0
    assert w["weekly_hours"] == 100.0
    assert w["note"] == "my plan"
