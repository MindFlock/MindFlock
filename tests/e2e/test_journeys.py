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

    # The install button names what is missing (the agent CLI here). Round 5:
    # the row it fixes is not repeated above it — the button IS the row.
    install = card.locator("#doctor-install-btn")
    expect(install).to_be_visible(timeout=30_000)
    expect(install).to_contain_text("Install")
    # Every check that blocks getting started, tmux included, is one click
    # away under "All checks" — never the phone / tailnet extras.
    card.locator(".setup-allchecks > summary").first.click()
    items = card.locator(".setup-allchecks ul.doctor-list li.doctor-check")
    expect(items.first).to_be_visible()
    labels = [t.strip() for t in items.locator(".doctor-label").all_inner_texts()]
    assert labels and all(labels), labels
    assert any("tmux" in t for t in labels), labels
    assert any("agent" in t.lower() or "claude" in t.lower() for t in labels), labels
    assert not any(
        "tailscale" in t.lower() or "mtu" in t.lower() for t in labels
    ), labels

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
    expect(skip).to_be_visible(timeout=10_000)
    skip.click()
    folder = dialog.locator("#new-repo-path")
    expect(folder).to_be_visible(timeout=10_000)
    folder.fill(str(mf_server.repo))
    # The name is optional (a blank one is named from the task), so it sits
    # under More options: open it the way a user would to pick one.
    advanced = dialog.locator("details#new-advanced")
    if advanced.get_attribute("open") is None:
        advanced.locator("summary").first.click()
    name = dialog.locator("#new-title")
    expect(name).to_be_visible(timeout=10_000)
    name.fill(title)
    expect(dialog.locator("#new-program")).to_have_value("claude")

    create = dialog.locator("#new-form button[type='submit']")
    expect(create).to_contain_text("Start")  # round 5: one verb, Start
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
    # Rows past the everyday ones sit in collapsed folds ("Other agents",
    # Advanced), and a hidden row's inner_text is "": open every fold so every
    # row is checked.
    folds = screen.locator("details")
    for k in range(folds.count()):
        fold = folds.nth(k)
        if fold.get_attribute("open") is None and fold.locator("summary").is_visible():
            fold.locator("summary").click()
            expect(fold).to_have_attribute("open", "")
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
    # The agent rows: "Agent · Claude Code" (the server's own name still reads
    # "Coding agent · …" on older builds).
    assert any(name.lower().startswith(("agent ·", "coding")) for name, _ in seen), seen


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


# --------------------------------------------------------------------------- #
# intake: add a repository once, edit it where it shows
# --------------------------------------------------------------------------- #
def _wait_settings(srv, pred, timeout: float = 10.0):
    """The settings file once ``pred`` holds (a save is one round trip)."""
    deadline = time.time() + timeout
    st = srv.settings()
    while not pred(st) and time.time() < deadline:
        time.sleep(0.2)
        st = srv.settings()
    return st


def test_intake_add_a_repository_once_and_edit_it_where_it_shows(
    mf_server, page, open_app
):
    """Intake → GitHub PRs with nothing watched: the add flow leads. One slug,
    two uses ticked, "Finish with defaults" → ONE save puts it on the PR list
    (switched OFF — a never-chosen setup) and Verify's list; its heading shows
    even though GitHub can't be listed here, and its Edit → Remove (arm, then
    confirm) takes it off the PR list only."""
    repo = "acme/widgets"
    open_app(mf_server)

    page.locator("#intake-btn").click()
    panel = page.locator("#intake-panel")
    expect(panel).to_be_visible(timeout=10_000)
    page.locator("#intake-tab-prs").click()

    # Round 5: an empty PRs tab shows one setup card (Connect GitHub → Pick a
    # repo → Turn on); the add flow opens from its "Pick a repo…", never by itself.
    expect(panel.locator("#ik-add-flow")).to_have_count(0)
    panel.get_by_role("button", name="Pick a repo…").click()
    flow = panel.locator("#ik-add-flow")
    expect(flow).to_be_visible(timeout=10_000)
    expect(flow.locator(".ik-kind[data-kind='repo']")).to_have_attribute(
        "aria-checked", "true"
    )
    flow.locator("#ik-add-repo").fill(repo)
    pr_tick = flow.locator(".ik-use[data-use='pr'] input")
    expect(pr_tick).to_be_checked()
    flow.locator(".ik-use[data-use='verify'] input").check()
    flow.get_by_role("button", name="Finish with defaults").click()
    expect(flow).to_have_count(0, timeout=10_000)

    st = _wait_settings(
        mf_server,
        lambda s: s.get("repository", {}).get("verify_repos") == [repo],
    )
    assert st.get("github", {}).get("repos") == [repo], st.get("github")
    assert st.get("github", {}).get("enabled") is False, st.get("github")
    assert st.get("repository", {}).get("verify_repos") == [repo], st.get("repository")

    head = panel.locator(".ik-source-group[data-group-key='%s']" % repo)
    expect(head).to_be_visible(timeout=10_000)
    head.locator(".ik-head-action").click()
    editor = head.locator(".ik-editor")
    expect(editor).to_be_visible(timeout=5_000)
    expect(editor.locator(".ik-use[data-use='verify'] input")).to_be_checked()
    remove = editor.locator("button.ik-arm")
    remove.click()
    expect(remove).to_contain_text("Confirm")
    remove.click()
    expect(head).to_have_count(0, timeout=10_000)

    # An empty list is the default, so the store may omit the key entirely.
    st = _wait_settings(mf_server, lambda s: not s.get("github", {}).get("repos"))
    assert not st.get("github", {}).get("repos"), st.get("github")
    # Verify keeps watching it: Remove took it off THIS list only.
    assert st.get("repository", {}).get("verify_repos") == [repo], st.get("repository")


# --------------------------------------------------------------------------- #
# intake: adding a ticket source never picks the tracker for you
# --------------------------------------------------------------------------- #
def test_intake_add_a_ticket_source_makes_you_pick_the_tracker(
    mf_server, page, open_app
):
    """Intake → Tickets with no source: the add flow leads with NO tile picked
    and Continue disabled, titled "Add a source" — not the first tracker's name
    (v0.8.6 opened a card titled GitHub Issues over a select set to it). A
    tile only marks the choice; Continue drafts it, and only then does the
    title name it. Cancel leaves nothing saved."""
    open_app(mf_server)

    page.locator("#intake-btn").click()
    panel = page.locator("#intake-panel")
    expect(panel).to_be_visible(timeout=10_000)

    flow = panel.locator("#ik-add-flow")
    expect(flow).to_be_visible(timeout=10_000)
    title = flow.locator(".ik-add-title")
    expect(title).to_have_text("Add a source")
    expect(flow.locator(".ik-kind[aria-checked='true']")).to_have_count(0)
    expect(flow.locator(".ik-kind[data-kind='github_issues']")).to_contain_text(
        "as tickets"
    )
    expect(flow.locator(".ik-kind[data-kind='repo']")).to_contain_text("in a repo")
    cont = flow.locator("#ik-add-continue")
    expect(cont).to_be_disabled()
    expect(flow.locator("#ik-add-pick-hint")).to_contain_text("Pick your tracker")

    # A tile marks the choice — nothing is drafted, the title still names nothing.
    flow.locator(".ik-kind[data-kind='jira']").click()
    expect(flow.locator(".ik-kind[data-kind='jira']")).to_have_attribute(
        "aria-checked", "true"
    )
    expect(title).to_have_text("Add a source")
    expect(flow.locator(".tk-source-editor")).to_have_count(0)
    expect(cont).to_be_enabled()

    cont.click()
    expect(title).to_have_text("Add Jira")
    expect(flow.locator(".tk-source-editor[data-section='connect']")).to_be_visible()

    flow.locator("#ik-add-cancel").click()
    expect(flow).to_have_count(0, timeout=10_000)
    sources = mf_server.api("/api/settings/ticketing/sources").get("sources")
    assert not sources, sources
