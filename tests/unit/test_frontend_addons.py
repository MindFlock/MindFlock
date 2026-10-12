"""Structural contract tests for the addon frontend modules.

These lock the wiring of the Connections addon ES module and the
shared modal-a11y helper (served as static files), so a refactor that silently
drops the a11y hookup, the API calls, or the self-registration is caught by the
suite instead of only by a manual browser check.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from backend.web import server

client = TestClient(server.app)


def _js(path: str) -> str:
    r = client.get(path)
    assert r.status_code == 200, path
    assert r.headers["content-type"].startswith(
        ("application/javascript", "text/javascript")
    ), path
    return r.text


def test_addon_modal_helper_contract():
    js = _js("/core/addon-modal.js")
    assert "export function activateModalA11y(" in js
    # dialog semantics + focus management
    assert '"role", "dialog"' in js
    assert '"aria-modal", "true"' in js
    assert 'e.key !== "Tab"' in js  # Tab trap
    assert "opener.focus()" in js  # focus returned to opener on release


def test_templates_has_no_vanilla_module():
    """The templates manager is the SPA's own TemplatesDialog now (React, inline
    confirm rows, no window.confirm — Electron has none). The vanilla ES module
    it replaced must stay gone, and the addon must not point slots.js at it."""
    r = client.get("/addons/templates.js")
    assert r.status_code == 404
    data = client.get("/api/addons").json()
    tpl = next(a for a in data["addons"] if a["id"] == "templates")
    fe = tpl["frontend"][0]
    assert fe["module"] is None
    assert fe["builtin_ui"] is True
    # The paste-into-all lives in the SPA's Prompts panel, as before — pinned
    # against the source in frontend/src/__tests__/promptsPanelTargets.test.ts.
