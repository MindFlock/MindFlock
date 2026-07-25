"""Structural contract tests for the addon frontend modules.

These lock the wiring of the Connections + Templates addon ES modules and the
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


def test_templates_module_wiring():
    js = _js("/addons/templates.js")
    assert 'import { activateModalA11y } from "/core/addon-modal.js"' in js
    assert "window.mindflockAddons.templates" in js
    # opened from the + New dialog; focus returns to whatever opened it
    assert "window.mindflockAddons.templates.open = show" in js
    assert "activateModalA11y(modal, opener" in js
    # dynamic run/import messages announced to screen readers
    assert 'aria-live", "polite"' in js
    # cold-open loading placeholder (not a blank box)
    assert "Loading templates" in js
    # launching reuses the existing create endpoint
    assert '"/api/instances"' in js
    assert 'const API = "/api/templates"' in js
    # edit loads a template back into the form; starters seed the empty state
    assert "formApi = {" in js and "fill(t)" in js
    assert "STARTERS" in js
    # send a recipe's prompt to a running session (reuses the /send endpoint),
    # including a broadcast to all running sessions
    assert "populateSend" in js
    assert '"/send"' in js
    assert "All running sessions" in js
    assert 'target === "*"' in js
    # duplicate = fork a recipe into a new, distinctly-named one
    assert '"-copy"' in js
    # progressive filter box, shown only past a threshold (like the session filter)
    assert "FILTER_MIN" in js
    assert "mft-filter-input" in js
    # narrow-viewport: action buttons stack below the name so it isn't crushed
    assert "@media (max-width: 600px)" in js
    # Run suggests a non-colliding session name for repeat runs
    assert "async function uniqueTitle(" in js
    # import / export sharing
    assert "Import / export" in js
    assert '"mindflock-templates.json"' in js
    assert "URL.createObjectURL" in js
