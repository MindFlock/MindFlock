"""The voice assistant's catalog (``core.voice_assistant_catalog``).

The client surfaces it lists are hand-mirrored from the web UI; these tests
parse the TypeScript sources and fail on drift in either direction, so a new
dialog, Settings screen or session action can't silently be unreachable by
voice (or a removed one offered to the model).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from backend.config import settings as settings_store
from backend.web.core import voice_assistant_catalog as cat

ROOT = Path(__file__).resolve().parents[2]
STORE_TS = ROOT / "frontend/src/state/store.ts"
SETTINGS_TSX = ROOT / "frontend/src/components/settings/SettingsDialog.tsx"
ACTIONS_TS = ROOT / "frontend/src/lib/sessionActionTable.ts"


def _block(text: str, start: str, end_re: str) -> str:
    i = text.index(start)
    m = re.compile(end_re, re.M).search(text, i)
    assert m, start
    return text[i : m.end()]


def ts_dialog_names() -> list:
    text = STORE_TS.read_text(encoding="utf-8")
    block = _block(text, "export type DialogName =", r";\s*$")
    block = re.sub(r"//[^\n]*", "", block)
    return re.findall(r'\|\s*"([^"]+)"', block)


def ts_screens() -> dict:
    text = SETTINGS_TSX.read_text(encoding="utf-8")
    block = _block(text, "const SCREENS = [", r"^\] as const")
    return dict(re.findall(r'\{\s*key:\s*"([^"]+)",\s*label:\s*"([^"]+)"', block))


def ts_session_actions() -> list:
    text = ACTIONS_TS.read_text(encoding="utf-8")
    block = _block(
        text, "export const SESSION_ACTIONS = {", r"^\} (?:as const|satisfies)|^\};"
    )
    return re.findall(r"^  ([A-Za-z]+): \{", block, re.M)


def test_dialogs_mirror_store_ts():
    assert list(cat.DIALOGS) == ts_dialog_names()


def test_settings_screens_mirror_settings_dialog():
    assert cat.SETTINGS_SCREENS == ts_screens()


def test_session_actions_mirror_the_table():
    assert list(cat.SESSION_ACTIONS) == ts_session_actions()


def test_open_dialogs_are_dialogs_with_labels():
    assert set(cat.OPEN_DIALOGS) <= set(cat.DIALOGS)
    for d in cat.OPEN_DIALOGS:
        assert cat.DIALOG_LABELS.get(d), d
    for needs_target in (
        "commit",
        "rename",
        "extension",
        "ask",
        "flights",
        "templates",
    ):
        assert needs_target not in cat.OPEN_DIALOGS


def test_open_targets():
    t = cat.open_targets()
    assert "intake" in t and "settings" in t and "settings:voice" in t
    assert "settings:traffic" not in t


def test_destructive_markers():
    assert cat.DESTRUCTIVE_SESSION_ACTIONS == {"delete", "close", "merge"}
    assert cat.DESTRUCTIVE_SESSION_ACTIONS <= set(cat.SESSION_ACTIONS)
    for label in (
        "Delete",
        "Remove folder",
        "Merge PR",
        "Discard changes",
        "Reset to defaults",
    ):
        assert cat.is_destructive_click(label), label
    for label in ("Save", "Open", "Merged view", "Resettle"):
        assert not cat.is_destructive_click(label), label


# --- settings -------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _fresh():
    settings_store.invalidate()
    yield
    settings_store.invalidate()


def test_settings_catalog_shape_and_values():
    settings_store.update_settings(voice={"assistant_talk": "quiet"})
    settings_store.invalidate()
    rows = {r["path"]: r for r in cat.settings()}
    talk = rows["voice.assistant_talk"]
    assert talk == {
        "path": "voice.assistant_talk",
        "label": "Assistant talk",
        "type": "select",
        "value": "quiet",
        "options": ["quiet", "brief", "chatty"],
    }
    assert rows["notifications.ask_popup"]["type"] == "bool"
    assert (
        rows["notifications.ask_popup"]["label"] == "Question pop-up"
    )  # settings_sync.LABELS
    assert rows["general.sleep_idle_minutes"]["type"] == "int"
    assert rows["prefs.theme"]["options"] == ["", "light", "dark"]
    assert "claude" in rows["coding_cli.default_provider"]["options"]
    for r in rows.values():
        assert r["type"] in ("bool", "str", "int", "select")


def test_settings_catalog_excludes_secrets_and_guarded_fields():
    paths = {r["path"] for r in cat.settings()}
    for p in (
        "github.token",
        "general.auth_token",
        "notifications.ntfy_token",
        "voice.stt_api_key",
        "voice.tts_api_key",
        "general.auth_mode",
        "general.remote_control",
        "general.serve_mode",
        "general.shared_link",
        "general.tailnet_trusted_logins",
        "engine.skip_permissions",
        "general.agent_mcp_scope",
        "voice.tts_url",
        "local_model.base_url",
        "person.id",
        "repository.url",
        "notifications.ntfy_topic",
    ):
        assert p not in paths, p
    assert not any(
        p.startswith(("peer.", "platform.", "auth_profiles.", "ticketing."))
        for p in paths
    )
    # Lists and dicts are no voice settings.
    assert "voice.words" not in paths and "github.repos" not in paths


@pytest.mark.parametrize(
    "path, value, want",
    [
        ("notifications.ask_popup", "on", True),
        ("notifications.ask_popup", False, False),
        ("general.sleep_idle_minutes", "30", 30),
        ("general.sleep_idle_minutes", 30.0, 30),
        ("voice.assistant_talk", "BRIEF", "brief"),
        ("person.name", "  Sam  ", "Sam"),
    ],
)
def test_check_setting_coerces(path, value, want):
    _, _, got, err = cat.check_setting(path, value)
    assert err is None
    assert got == want


@pytest.mark.parametrize(
    "path, value",
    [
        ("notifications.ask_popup", "sometimes"),
        ("general.sleep_idle_minutes", -1),
        ("general.sleep_idle_minutes", True),
        ("voice.assistant_talk", "loud"),
        ("person.name", "x" * 300),
        ("person.name", "line\nbreak"),
        ("github.token", "ghp"),
        ("general.auth_token", "t"),
        ("nope", 1),
        ("made.up", 1),
    ],
)
def test_check_setting_refuses(path, value):
    *_, err = cat.check_setting(path, value)
    assert err


def test_catalog_body():
    body = cat.catalog()
    assert body["dialogs"] == list(cat.OPEN_DIALOGS)
    assert "traffic" not in body["settings_screens"]
    assert body["session_actions"] == list(cat.SESSION_ACTIONS)
