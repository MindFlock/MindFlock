"""The default session (2026-11-02): a session is a place in the file tree; a
chain run with no session picked runs in the default one — made the first
time (in-place, "default", in the default folder) and remembered."""

import asyncio
import datetime as dt
from types import SimpleNamespace

import pytest

from backend.config.settings import load_settings, update_settings
from backend.web.core import flight_driver as D


@pytest.fixture(autouse=True)
def _settings(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDFLOCK_SETTINGS_FILE", str(tmp_path / "settings.json"))
    yield


def _srv(monkeypatch, instances):
    ns = SimpleNamespace(ENGINE=SimpleNamespace(instances=instances))
    monkeypatch.setattr(D, "_server", lambda: ns)
    return ns


def test_a_remembered_default_session_is_reused(monkeypatch):
    _srv(monkeypatch, {"default": SimpleNamespace(Path="/x")})
    update_settings(general={"default_session": "default"})
    made = []
    monkeypatch.setattr(D, "_create_session", lambda p: made.append(p))
    assert asyncio.run(D.ensure_default_session()) == "default"
    assert made == []


def test_a_missing_one_is_made_in_place_in_the_default_folder_and_remembered(
    monkeypatch, tmp_path
):
    _srv(monkeypatch, {})
    folder = tmp_path / "proj"
    folder.mkdir()
    update_settings(general={"default_folder": str(folder)})
    made = []

    async def create(payload):
        made.append(payload)
        return "default"

    monkeypatch.setattr(D, "_create_session", create)
    assert asyncio.run(D.ensure_default_session()) == "default"
    assert made == [
        {"title": "default", "repo_path": str(folder), "prompt": "", "in_place": True}
    ]
    assert load_settings().general.default_session == "default"


def test_the_default_folder_falls_back_to_the_newest_session_then_home(
    monkeypatch, tmp_path
):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    old = SimpleNamespace(Path=str(a), CreatedAt=dt.datetime(2026, 1, 1))
    new = SimpleNamespace(Path=str(b), CreatedAt=dt.datetime(2026, 6, 1))
    _srv(monkeypatch, {"old": old, "new": new})
    assert D._default_folder() == str(b)
    _srv(monkeypatch, {})
    import os

    assert D._default_folder() == os.path.expanduser("~")


def test_attach_names_it_by_at_default():
    assert D.DEFAULT_SESSION == "@default"
