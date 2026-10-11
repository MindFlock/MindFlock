"""Journeys: the first ten minutes of MindFlock, as a user sees them.

Each test boots its own sandboxed server (stub agent, scratch home, own tmux)
and drives the real UI in headless Chrome. Assertions are on visible text and
DOM state the way a person would check them — a row with the name, a chip with
a label, a dialog that is there or not — so a UX rough edge cannot come back
silently. See ``conftest.py`` for the fixtures and ``docs/development.md`` →
"Journey tests" for how to run them.
"""

from __future__ import annotations

import json
import time
import urllib.request

import pytest

expect = pytest.importorskip("playwright.sync_api").expect

SESSION_UP_TIMEOUT_MS = 30_000


def _create_session(srv, title: str) -> None:
    """Start a session through the API (the New dialog's own call) and wait
    for it to leave ``loading`` — for journeys that start AFTER creation."""
    req = urllib.request.Request(
        srv.base_url + "/api/instances",
        data=json.dumps({"title": title, "repo_path": str(srv.repo)}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        assert r.status in (200, 202), r.status
    deadline = time.time() + 30
    while time.time() < deadline:
        row = next((s for s in srv.state() if s.get("title") == title), None)
        if row and row.get("status") not in (None, "", "loading"):
            return
        time.sleep(0.25)
    raise AssertionError("session %r never left loading: %r" % (title, srv.state()))


def _titles(srv) -> list:
    return [s.get("title") for s in srv.state()]


# --------------------------------------------------------------------------- #
# first_run
# --------------------------------------------------------------------------- #
@pytest.mark.mf_server(onboarded=False, stub_agent=False)
def test_first_run(mf_server, page, open_app):
    """A fresh machine (never onboarded, no agent CLI) lands on the setup card:
    it lists the doctor items, offers to install what is missing, and offers a
    New session — with at most one overlay on top of it."""
    open_app(mf_server)

    card = page.locator("#empty[data-mode='setup'] .setup-card")
    expect(card).to_be_visible(timeout=15_000)
    expect(card).to_contain_text("Get set up")

    # Nothing may stack on the card beyond a single overlay (the welcome tour
    # is allowed; a tour AND a modal is not).
    overlays = page.locator(".modal")
    expect(overlays.first).to_be_visible(timeout=5_000) if overlays.count() else None
    assert overlays.count() <= 1, "more than one overlay on first run: %r" % (
        [overlays.nth(i).get_attribute("id") for i in range(overlays.count())]
    )
    if overlays.count() == 1:
        assert overlays.first.get_attribute("id") == "welcome-tour"
        page.locator("#welcome-tour button.wt-skip").click()
        expect(page.locator("#welcome-tour")).to_have_count(0)
    expect(card).to_be_visible()

    # Doctor items: real check rows with labels (doctor takes a few seconds).
    items = card.locator("ul.doctor-list li.doctor-check")
    expect(items.first).to_be_visible(timeout=30_000)
    labels = [t.strip() for t in items.locator(".doctor-label").all_inner_texts()]
    assert labels and all(labels), labels
    assert any("tmux" in t for t in labels), labels
    assert any("agent" in t.lower() for t in labels), labels

    # The install button names what is missing (the agent CLI here).
    install = card.locator("#doctor-install-btn")
    expect(install).to_be_visible(timeout=10_000)
    expect(install).to_contain_text("Install")

    # ...and the way forward.
    new_btn = card.locator("button.setup-new")
    expect(new_btn).to_be_visible()
    expect(new_btn).to_contain_text("New session")
    # `general.onboarded` is derived from the setup checklist now; a fresh
    # store may omit it entirely. Never true before the first session.
    assert mf_server.settings().get("general", {}).get("onboarded") is not True


# --------------------------------------------------------------------------- #
# new_session
# --------------------------------------------------------------------------- #
def test_new_session(mf_server, page, open_app):
    """Ctrl+N → name it → Create: a sidebar row with the name appears, its
    pane shows a terminal, and the row's state chip has a label."""
    title = "journey-alpha"
    open_app(mf_server)

    page.keyboard.press("Control+n")
    dialog = page.locator("#new-dialog")
    expect(dialog).to_be_visible(timeout=10_000)
    expect(dialog).to_contain_text("New")

    # The dialog opens on its "describe it" page; the form is one click away.
    skip = dialog.locator("#new-describe-skip")
    if skip.count():
        skip.click()
    name = dialog.locator("#new-title")
    expect(name).to_be_visible(timeout=10_000)
    name.fill(title)
    folder = dialog.locator("#new-repo-path")
    folder.fill(str(mf_server.repo))
    expect(dialog.locator("#new-program")).to_have_value("claude")

    create = dialog.locator("#new-form button[type='submit']")
    expect(create).to_contain_text("Create")
    create.click()
    expect(dialog).to_have_count(0, timeout=SESSION_UP_TIMEOUT_MS)

    row = page.locator("li.inst[data-title='%s']" % title)
    expect(row).to_be_visible(timeout=SESSION_UP_TIMEOUT_MS)
    expect(row.locator("span.title")).to_contain_text(title)

    pane = page.locator("section.pane[data-title='%s']" % title)
    expect(pane).to_be_visible(timeout=SESSION_UP_TIMEOUT_MS)
    expect(pane.locator(".term-container .xterm").first).to_be_visible(
        timeout=SESSION_UP_TIMEOUT_MS
    )

    chip = row.locator("span.stagechip").first
    expect(chip).to_be_visible(timeout=SESSION_UP_TIMEOUT_MS)
    expect(chip).not_to_have_text("", timeout=SESSION_UP_TIMEOUT_MS)
    assert chip.inner_text().strip(), "state chip has no label"

    assert title in _titles(mf_server)
    assert mf_server.settings().get("general", {}).get("onboarded") is True


# --------------------------------------------------------------------------- #
# close_and_reopen
# --------------------------------------------------------------------------- #
def test_close_and_reopen(mf_server, page, open_app):
    """✕ on the row ends the session: the row goes, Recently closed lists it,
    and Reopen brings it back under the same name."""
    title = "journey-close"
    _create_session(mf_server, title)
    open_app(mf_server)

    row = page.locator("li.inst[data-title='%s']" % title)
    expect(row).to_be_visible(timeout=SESSION_UP_TIMEOUT_MS)
    row.hover()
    row.locator("button.kill").click()
    expect(row).to_have_count(0, timeout=SESSION_UP_TIMEOUT_MS)
    assert title not in _titles(mf_server)

    recent_btn = page.locator("#recent-btn")
    expect(recent_btn).to_be_visible(timeout=SESSION_UP_TIMEOUT_MS)
    expect(recent_btn).to_contain_text("Recently closed")
    recent_btn.click()

    dialog = page.locator("#recent-dialog")
    expect(dialog).to_be_visible(timeout=10_000)
    expect(dialog).to_contain_text("Recently closed")
    entry = dialog.locator(".recent-row", has_text=title)
    expect(entry.first).to_be_visible(timeout=15_000)
    entry.first.locator("button.recent-reopen").click()

    expect(dialog).to_have_count(0, timeout=SESSION_UP_TIMEOUT_MS)
    row = page.locator("li.inst[data-title='%s']" % title)
    expect(row).to_be_visible(timeout=SESSION_UP_TIMEOUT_MS)
    expect(row.locator("span.title")).to_contain_text(title)
    assert title in _titles(mf_server)


# --------------------------------------------------------------------------- #
# connections
# --------------------------------------------------------------------------- #
def test_connections(mf_server, page, open_app):
    """Settings → Connections: every row names a service and shows a status
    word. The words are read from the DOM and only asserted non-empty, so a
    rename of the labels does not break this."""
    open_app(mf_server)

    page.locator("#settings-btn").click()
    dialog = page.locator("#settings-dialog")
    expect(dialog).to_be_visible(timeout=10_000)
    dialog.locator(".set-nav-item[data-screen='connections']").click()
    screen = dialog.locator(".set-screen[data-screen='connections']")
    expect(screen).to_be_visible()

    rows = screen.locator(".conn-row")
    expect(rows.first).to_be_visible(timeout=45_000)
    n = rows.count()
    assert n >= 1
    seen = []
    for i in range(n):
        r = rows.nth(i)
        name = r.locator(".conn-name").inner_text().strip()
        status = r.locator(".conn-pill").inner_text().strip()
        seen.append((name, status))
        assert name, "connection row %d has no name" % i
        assert status, "connection %r has no status word" % name
    assert any(name.lower().startswith("coding") for name, _ in seen), seen


# The Ctrl+W / Delete guard (frontend/src/lib/keymap.ts endShortcutAllowed)
# refuses while ANY dialog or overlay is open, the palette included.
def test_shortcuts_guard(mf_server, page, open_app):
    """With the command palette open, Ctrl+W must not end the focused session."""
    title = "journey-guard"
    _create_session(mf_server, title)
    open_app(mf_server)

    row = page.locator("li.inst[data-title='%s']" % title)
    expect(row).to_be_visible(timeout=SESSION_UP_TIMEOUT_MS)
    row.locator(".inst-row").click()
    expect(row).to_have_class(__import__("re").compile(r"\bactive\b"), timeout=10_000)

    page.keyboard.press("Control+p")
    palette = page.locator("#palette")
    expect(palette).to_be_visible(timeout=10_000)
    expect(palette.locator("#palette-input")).to_be_focused()

    page.keyboard.press("Control+w")
    page.wait_for_timeout(2_000)

    expect(row).to_be_visible()
    assert title in _titles(mf_server), "Ctrl+W closed the session behind the palette"
