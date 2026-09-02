"""Extension discovery (Addon API v3): the tmp-tree contract.

Every test builds a PRIVATE FastAPI app and calls ``register_addons`` against
a tmp extensions tree — never the developer's real ``~/.mindflock/extensions/``
(conftest pins ``MINDFLOCK_EXTENSIONS_DIR`` at an empty dir for the rest of the
suite, which is also what keeps the server-module manifests extension-free).
"""

from __future__ import annotations

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from backend.config import settings as S
from backend.web.addons import AppContext, discover_extensions, register_addons

#: A minimal, valid extension module. The addon id defaults to the dir name,
#: but tests override it to provoke reserved/duplicate/regex rejections.
_EXT_TEMPLATE = '''\
"""Test extension (written by tests/unit/test_extension_discovery.py)."""
from fastapi import APIRouter

from backend.web.addons.base import (
    Addon,
    ExtensionButton,
    ExtensionCommand,
    ExtensionSpec,
    ExtensionSurface,
)


class _Ext(Addon):
    id = {ext_id!r}
    label = {label!r}

    @property
    def router(self):
        return APIRouter()

    def extension(self):
        return ExtensionSpec(
            module="/extensions/" + self.id + "/index.js",
            bar_label=self.label,
            buttons=[ExtensionButton(command=self.id + ".open", label="Open")],
            commands=[
                ExtensionCommand(id=self.id + ".open", title="Open", surface="main")
            ],
            surfaces=[ExtensionSurface(id="main", kind="dialog", title=self.label)],
        )


def build(ctx):
    return _Ext(ctx)
'''


def _write_ext(root, dirname, ext_id=None, label=None, body=None):
    d = root / dirname
    d.mkdir(parents=True)
    src = (
        body
        if body is not None
        else _EXT_TEMPLATE.format(
            ext_id=ext_id or dirname, label=label or dirname.title()
        )
    )
    (d / "extension.py").write_text(src, encoding="utf-8")
    return d


@pytest.fixture
def ctx():
    return AppContext(engine=None, register_task=lambda coro: None)


@pytest.fixture
def ext_root(tmp_path, monkeypatch):
    """A tmp extensions tree, already pointed at by the env var."""
    root = tmp_path / "extensions"
    root.mkdir()
    monkeypatch.setenv("MINDFLOCK_EXTENSIONS_DIR", str(root))
    return root


def _client(ctx) -> TestClient:
    app = FastAPI()
    register_addons(app, ctx)
    return TestClient(app)


def _addons_by_id(client) -> dict:
    return {a["id"]: a for a in client.get("/api/addons").json()["addons"]}


def test_good_extension_lands_in_manifest(ext_root, ctx):
    _write_ext(ext_root, "goodext")
    addons = _addons_by_id(_client(ctx))
    ext = addons["goodext"]
    assert ext["origin"] == "user"
    assert ext["enabled"] is True
    assert ext["extension"]["bar_label"] == "Goodext"
    assert ext["extension"]["module"] == "/extensions/goodext/index.js"
    assert ext["extension"]["buttons"][0]["command"] == "goodext.open"
    # Built-ins carry the additive keys too, with their builtin identity.
    assert addons["mindflock"]["origin"] == "builtin"
    assert addons["mindflock"]["extension"] is None
    assert addons["mindflock"]["enabled"] is True


def test_user_extensions_ordered_after_builtins_by_dirname(ext_root, ctx):
    _write_ext(ext_root, "zeta")
    _write_ext(ext_root, "alpha")
    ids = [a["id"] for a in _client(ctx).get("/api/addons").json()["addons"]]
    assert ids[-2:] == ["alpha", "zeta"]  # dir-name order, after every built-in
    assert ids.index("mindflock") < ids.index("alpha")


def test_broken_extension_is_skipped(ext_root, ctx):
    _write_ext(ext_root, "goodext")
    _write_ext(ext_root, "broken", body="raise RuntimeError('boom at import')\n")
    addons = _addons_by_id(_client(ctx))
    assert "goodext" in addons  # containment is per directory
    assert "broken" not in addons


def test_extension_without_build_is_skipped(ext_root, ctx):
    _write_ext(ext_root, "nobuild", body="x = 1\n")
    assert "nobuild" not in _addons_by_id(_client(ctx))


def test_reserved_id_is_skipped(ext_root, ctx):
    # The dir name is free; the CLAIMED id ("settings") is a core namespace.
    _write_ext(ext_root, "sneaky", ext_id="settings", label="Sneaky")
    addons = [a for a in _client(ctx).get("/api/addons").json()["addons"]]
    settings_rows = [a for a in addons if a["id"] == "settings"]
    assert len(settings_rows) == 1
    assert settings_rows[0]["origin"] == "builtin"  # the real one, untouched


def test_bad_id_regex_is_skipped(ext_root, ctx):
    _write_ext(ext_root, "badid", ext_id="Bad_Id", label="Bad")
    addons = _addons_by_id(_client(ctx))
    assert "Bad_Id" not in addons and "badid" not in addons


def test_duplicate_id_first_dir_wins(ext_root, ctx):
    _write_ext(ext_root, "aaa", ext_id="dupe", label="First")
    _write_ext(ext_root, "bbb", ext_id="dupe", label="Second")
    addons = [a for a in _client(ctx).get("/api/addons").json()["addons"]]
    dupes = [a for a in addons if a["id"] == "dupe"]
    assert len(dupes) == 1
    assert dupes[0]["label"] == "First"  # earlier dir name claims the id


def test_collision_with_registered_id_is_skipped(ext_root, ctx):
    _write_ext(ext_root, "claimed")
    assert discover_extensions(ctx, taken_ids={"claimed"}) == []
    # Without the claim the same tree loads — the id itself is fine.
    assert [a.id for a in discover_extensions(ctx)] == ["claimed"]


def test_invalid_spec_extension_is_skipped(ext_root, ctx):
    # Valid id, but the spec's command carries a foreign prefix.
    _write_ext(
        ext_root,
        "badspec",
        body=_EXT_TEMPLATE.format(ext_id="badspec", label="Bad").replace(
            'self.id + ".open"', '"other.open"'
        ),
    )
    assert "badspec" not in _addons_by_id(_client(ctx))


def test_frontend_dir_is_mounted(ext_root, ctx):
    d = _write_ext(ext_root, "goodext")
    fe = d / "frontend"
    fe.mkdir()
    (fe / "index.js").write_text("export function activate(api) {}\n", encoding="utf-8")
    _write_ext(ext_root, "nofrontend")  # backend-only: no mount
    client = _client(ctx)
    r = client.get("/extensions/goodext/index.js")
    assert r.status_code == 200
    assert "activate" in r.text
    assert client.get("/extensions/nofrontend/index.js").status_code == 404


def test_disabled_list_flips_enabled(ext_root, ctx, tmp_path, monkeypatch):
    _write_ext(ext_root, "goodext")
    monkeypatch.setenv("MINDFLOCK_SETTINGS_FILE", str(tmp_path / "settings.json"))
    S.invalidate()
    S.update_settings(extensions={"disabled": ["goodext"]})
    client = _client(ctx)
    addons = _addons_by_id(client)
    assert addons["goodext"]["enabled"] is False
    assert addons["mindflock"]["enabled"] is True  # only the named id flips
    # Re-enabling lands on the next manifest fetch — no rediscovery, no restart.
    S.update_settings(extensions={"disabled": []})
    assert _addons_by_id(client)["goodext"]["enabled"] is True


def test_missing_extensions_dir_is_quiet(tmp_path, monkeypatch, ctx):
    monkeypatch.setenv("MINDFLOCK_EXTENSIONS_DIR", str(tmp_path / "nowhere"))
    assert discover_extensions(ctx) == []


def test_builtin_bad_spec_raises_at_registration(monkeypatch, ctx):
    """A built-in's invalid spec is developer error — the import must die, not
    serve a manifest the SPA host can't act on."""
    import backend.web.addons as addons_mod
    from backend.web.addons.base import Addon, ExtensionCommand, ExtensionSpec

    class _Bad(Addon):
        id = "badbuiltin"
        label = "Bad"

        @property
        def router(self):
            return APIRouter()

        def extension(self):
            return ExtensionSpec(
                module="/x.js",
                bar_label="X",
                commands=[ExtensionCommand(id="wrong.id", title="T")],
            )

    monkeypatch.setattr(addons_mod, "build_addons", lambda ctx: [_Bad(ctx)])
    with pytest.raises(ValueError, match="badbuiltin"):
        register_addons(FastAPI(), ctx)
