"""Customize (Sidebar · Prompts) and the bell as the one "needs you" list —
source-level pins, so they hold before the bundle is rebuilt.

The owner's ask: Prompts stops being a top-bar destination and becomes a tab
of a real Customize dialog, the Outbox is dropped as a place altogether, and
everything that waits on you lives in the bell. These pin the wiring that
makes that true — and that the old doors are ABSENT, since a leftover button
is exactly how a surface ends up with two names.
"""

from __future__ import annotations

import re
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "frontend" / "src"


def _src(rel: str) -> str:
    return (_SRC / rel).read_text(encoding="utf-8")


def _code(rel: str) -> str:
    """Source with comments stripped (they explain what is gone, by name)."""
    code = re.sub(r"/\*.*?\*/", "", _src(rel), flags=re.S)
    code = re.sub(r"\{/\*.*?\*/\}", "", code, flags=re.S)
    return re.sub(r"(?m)^\s*//[^\n]*|\s//[^\n]*", "", code)


def test_customize_is_the_sidebar_picker_and_prompts_is_its_own_dialog():
    """Prompts left Customize: it is a sidebar bar (switched on in Customize,
    like the Assistant) with its own Prompts dialog behind "Manage"."""
    src = _src("components/customize/CustomizeDialog.tsx")
    assert 'useUi((s) => s.openDialog === "customize")' in src
    assert 'id="customize-dialog"' in src
    assert 'id="customize-close"' in src
    assert "<SidebarBarsPicker />" in src
    assert "customize-tabs" not in src
    assert "PromptsPanel" not in src
    code = _code("components/customize/CustomizeDialog.tsx")
    assert "Outbox" not in code and '"outbox"' not in code
    assert "e.defaultPrevented" in src
    dialog = _src("components/dialogs/PromptsDialog.tsx")
    assert "export function PromptsDialog()" in dialog
    assert 'id="prompts-dialog"' in dialog
    assert 'useUi((s) => s.openDialog === "prompts")' in dialog
    assert "e.defaultPrevented" in dialog
    app = _code("App.tsx")
    assert "<CustomizeDialog />" in app and "<PromptsDialog />" in app
    assert "OutboxDialog" not in app
    keymap = _src("lib/keymap.ts")
    assert '"prompts-dialog",' in keymap
    store = _src("state/store.ts")
    assert '| "customize"' in store
    assert '| "outbox"' not in store
    assert not (_SRC / "components/outbox/OutboxDialog.tsx").exists()


def test_customize_css_is_registered_in_the_components_layer():
    index = _src("styles/index.css")
    outbox = index.index('@import "../components/outbox/Outbox.css" layer(components);')
    cust = index.index(
        '@import "../components/customize/CustomizeDialog.css" layer(components);'
    )
    assert outbox < cust
    assert "CustomizeDialog.css" not in _src("components/customize/CustomizeDialog.tsx")


def test_the_footer_customize_is_one_plain_button():
    src = _code("components/sidebar/FooterCustomize.tsx")
    assert 'id="foot-customize-btn"' in src
    assert 'onClick={() => openDialogFor("customize")}' in src
    assert "⚙ Customize" in src
    assert "foot-customize-menu" not in src
    assert "foot-customize-menu" not in _src("styles/regions.css")
    picker = _src("components/customize/SidebarBarsPicker.tsx")
    assert "orderedBars(barOrder, extBars)" in picker
    assert "toggleBarHidden(b.key)" in picker
    assert "Show in the sidebar" in picker
    assert 'link: { label: "Manage prompts", dialog: "prompts" }' in picker


def test_the_top_bar_has_no_outbox_prompts_or_recent_and_keeps_the_palette_title():
    src = _code("components/TopBar.tsx")
    for gone in ('id="outbox-btn"', 'id="prompts-btn"', 'id="recent-btn"'):
        assert gone not in src, gone
    assert 'title="Command palette — Ctrl+P / ⌘P"' in src
    assert 'aria-label="Open command palette"' in src
    assert src.index('id="settings-btn"') < src.index('id="palette-btn"')


def test_verify_always_shows_in_the_top_bar():
    """The owner: Verify should "always show" — no earn-its-slot rule, no
    per-device memory of it; the badge still says nothing on zero."""
    src = _code("components/TopBar.tsx")
    assert 'id="verify-btn"' in src
    assert "showVerify" not in src
    assert "useVerifySettings" not in src
    assert "mf_tb_verify" not in src
    assert '{due > 0 && <span className="tb-count">{due}</span>}' in src
    assert (
        src.index('id="intake-btn"')
        < src.index('id="verify-btn"')
        < src.index('id="settings-btn"')
    )


def test_the_bell_hosts_the_waiting_rows_and_drops_its_toggle():
    src = _code("components/NotificationsBell.tsx")
    assert 'import { WaitingRow } from "./outbox/WaitingRow";' in src
    assert "needsAttention(attentionItems(instances), outbox?.groups?.waiting)" in src
    assert "NotifToggle" not in src and "mf-notify-state" not in src
    assert '"mf-open-bell"' in src and '"mf-close-bell"' in src
    # The badge counts the merged list; the heading keeps its name.
    assert "attn.length > 99" in src and "attn.length === 0" in src
    assert "Needs attention" in src
    # Every clarify row answers in place; only a family's can redirect.
    assert 'variant="bell"' in src
    # A run toast that needs you opens the bell; a finished one shows its
    # group where it lives (its rail header, else its lead's Thread).
    toasts = _src("components/EventToasts.tsx")
    assert 'new CustomEvent("mf-open-bell"' in toasts
    assert "else showGroup(n.run);" in toasts
    assert "revealGroup(" not in _code("components/EventToasts.tsx")
    assert 'openDialogFor("outbox"' not in _code("components/EventToasts.tsx")


def test_a_click_that_replaces_its_own_control_keeps_the_bell_open():
    """The waiting rows brought controls that swap themselves out on click
    ("+N more", the preview's "edit" → a textarea). React commits before the
    document listener runs, so a `contains(target)` test saw a detached node
    and closed the bell under the user — the path at dispatch time doesn't."""
    src = _code("components/NotificationsBell.tsx")
    assert "e.composedPath()" in src
    assert "popRef.current?.contains(t)" not in src
    # A group-level escalation (no session) is sent as "run:<id>" and found by
    # its run id: a group can hold several session-less rows, each with the
    # server's own key.
    assert "setFlash(at >= 0 ? attnRef.current[at].key : title)" in src
    assert "r.waiting?.run?.id === runId" in src
    assert '"run:" + n.run' in _src("components/EventToasts.tsx")


def test_a_group_row_in_the_bell_reveals_its_header_on_the_rail():
    """No Outbox to open on a group: the bell's history row (and a needs-you
    row that was already answered) shows the group where it lives — its rail
    header, scrolled to and flashed; a split / one-for-all group has no
    header, so its lead's Thread (else a member row). Nothing left of the
    group is a quiet no-op."""
    bell = _code("components/NotificationsBell.tsx")
    assert 'openDialogFor("outbox"' not in bell
    assert bell.count("showGroup(n.run);") == 2
    assert "revealGroup(" not in bell
    assert "openThread(n.lead);" in bell
    show = _code("lib/showGroup.ts")
    assert "if (!runId || revealGroup(runId)) return;" in show
    assert 'if ("lead" in to) openThread(to.lead);' in show
    assert "else selectSession(to.row);" in show
    reveal = _code("lib/revealGroup.ts")
    assert (
        'document.querySelectorAll<HTMLElement>("li.run-group-head[data-run]")'
        in reveal
    )
    assert "el.dataset.run === runId" in reveal
    assert "if (!head) return false;" in reveal
    assert "head.classList.add(FLASH);" in reveal
    assert "data-run={group.id}" in _src("components/sidebar/RunGroupHeader.tsx")
    css = _src("components/sidebar/RunGroup.css")
    assert ".run-group-head.rg-flash {" in css
    assert "animation: notif-flash 1.4s ease-out;" in css


def test_the_moved_rows_keep_their_names():
    """Bundle pins find these by name — the move must not rename them."""
    src = _src("components/outbox/WaitingRow.tsx")
    for name in (
        "export function WaitingRow(",
        "function doWaitAction(",
        "function BudgetRow(",
        "function ApproveButtons(",
        "function ApprovePreview(",
        "const editedMessage = new Map",
        "export function RowMain(",
        "export function openSession(",
    ):
        assert name in src, name
    assert src.count('"/ship-now"') == 1


def test_no_native_dialog_calls_in_the_new_or_edited_files():
    """Electron has no window.prompt / confirm / alert: they do nothing there."""
    for rel in (
        "components/customize/CustomizeDialog.tsx",
        "components/customize/SidebarBarsPicker.tsx",
        "components/sidebar/FooterCustomize.tsx",
        "components/dialogs/PromptsDialog.tsx",
        "components/outbox/WaitingRow.tsx",
        "components/outbox/outbox.ts",
        "components/NotificationsBell.tsx",
        "components/EventToasts.tsx",
        "components/TopBar.tsx",
        "components/sidebar/VerifyBar.tsx",
        "components/palette/CommandPalette.tsx",
        "lib/keymap.ts",
        "lib/revealGroup.ts",
        "lib/showGroup.ts",
        "components/sidebar/RunGroupHeader.tsx",
        "components/grid/RunLeadPanel.tsx",
        "App.tsx",
    ):
        code = _code(rel)
        # A bare call or a window.* one; `pastePrompt(` and friends are fine.
        hits = re.findall(r"(?:(?<![\w.])|window\.)(?:confirm|alert|prompt)\(", code)
        assert not hits, (rel, hits)


def test_the_palette_lists_commands_before_focus_rows():
    src = _src("components/palette/CommandPalette.tsx")
    order = [
        'label: "New session…"',
        'label: "Open Intake"',
        'label: "Customize…"',
        "label: `Rename… — ${t}`",
        'label: "Focus: " + name',
    ]
    at = [src.index(x) for x in order]
    assert at == sorted(at), order
    for label in (
        'label: "Prompts…", hint: "manage saved prompts"',
        'label: "Assistant agent file…"',
        'label: "Recently closed…", hint: "reopen or clean up closed sessions"',
        "label: `Make PR — ${t}`",
        "label: `Merge PR — ${t}`",
        'label: "Verify — check what shipped"',
        'label: "Customize…", hint: "choose sidebar bars"',
    ):
        assert label in src, label
    for gone in (
        "Start several sessions…",
        "Create PR —",
        "Merge PR to staging",
        "New from Recently closed…",
        'hint: "session"',
        # Ctrl+Shift+T reopens the LAST session; it never opened this dialog.
        'label: "Recently closed…", hint: "Ctrl+Shift+T"',
        # The Outbox is gone as a place.
        "Outbox",
        'openDialogFor("outbox")',
    ):
        assert gone not in src, gone


def test_saved_prompts_own_pasting_into_running_sessions():
    """Saved prompts is the ONE place that pastes text into running sessions
    (the templates manager's send row is gone). Two surfaces — the sidebar
    Prompts bar (the daily door) and the Prompts dialog (Manage) — share ONE
    paste path, lib/promptPaste.ts, and one target picker model: the focused
    session, any other running one, or all of them."""
    paste = _code("lib/promptPaste.ts")
    assert paste.count('"/send"') == 1
    assert "submit: false, dialog_safe: true" in paste
    assert "pasteIntoAll(titles, send)" in paste
    assert "pastedAllToast(ok.length)" in paste
    assert "errorPop(" in paste
    assert "if (busy) return false;" in paste
    for path in (
        "components/dialogs/PromptsDialog.tsx",
        "components/sidebar/PromptsBar.tsx",
    ):
        src = _code(path)
        assert '"/send"' not in src, path
        assert "useInstances()" in src and '"/api/instances"' not in src, path
        assert "promptTargets(focused, running," in src, path
    bar = _code("components/sidebar/PromptsBar.tsx")
    assert 'id="prompts-bar-target"' in bar
    assert "pastePrompt(target, running, p.prompt)" in bar
    assert 'openDialogFor("prompts")' in bar
    assert "PRESETS_CHANGED" in bar
    assert 'case "prompts":' in _code("components/sidebar/SidebarBars.tsx")
    src = _code("components/dialogs/PromptsDialog.tsx")
    assert "<select" in src and 'id="prompts-target"' in src
    assert 'useUi.getState().openDialog === "prompts"' in src
    lib = _src("lib/promptTargets.ts")
    assert "All running sessions (${running.length})" in lib
    assert "running.length >= 2" in lib
    assert "press Enter in each to send" in lib
    # The ⋮ crash: React clears e.currentTarget once the handler returns, so
    # the rect is read BEFORE setPop, never inside its updater.
    assert "const anchor = e.currentTarget.getBoundingClientRect();" in src
    for upd in re.findall(r"setPop\(\(cur\).*?\);", src, flags=re.S):
        assert "currentTarget" not in upd, upd


def test_new_page_two_templates_strip_says_what_a_template_is():
    src = _src("components/dialogs/NewSessionDialog.tsx")
    assert '<span title="Saved New-session setups">Templates</span>' in src
