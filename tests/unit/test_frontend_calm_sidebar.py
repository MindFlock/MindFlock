"""Calm surface, the sidebar and rail (WP2): source-level pins.

The sidebar is your sessions first. Each automation bar has ONE name — the
Intake tab it opens — and that name is the button; the first run has one less
banner; and the native dialogs that do nothing in the desktop app (Electron has
no confirm/alert/prompt) are gone from the rail and from Recently closed.

Source-level, comments stripped (the comments explain WHY a call is gone and so
quote it). The behaviour is unit-tested in frontend/src/__tests__ (barDefs,
recentRows, runs/flockRail SSR-render the real Sidebar).
"""

from __future__ import annotations

import re
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "frontend" / "src"


def _src(rel: str) -> str:
    return (_SRC / rel).read_text(encoding="utf-8")


def _code(rel: str) -> str:
    """The file with block, JSX and line comments removed."""
    code = re.sub(r"/\*.*?\*/", "", _src(rel), flags=re.S)
    return re.sub(r"(?<!:)//[^\n]*", "", code)


# --- No native dialogs -------------------------------------------------------------

_NATIVE_FREE = {
    "components/sidebar/AutomationBar.tsx": (
        'alert(`MindFlock ${start ? "start" : "stop"} failed: `',
    ),
    "components/sidebar/useGithubToggleBar.ts": (
        'alert(`${toggleLabel} ${enable ? "on" : "off"} failed: `',
    ),
    "components/sidebar/SidebarRow.tsx": (
        "PERMANENTLY remove its worktree directory?",
        'alert("Cleanup failed: "',
    ),
    "components/dialogs/RecentDialog.tsx": (
        "if (!confirm(msg)) return;",
        "alert(nothingMessage(pre));",
        "if (!confirm(pruneMessage(pre))) return;",
        "includeDirty = confirm(dirtyMessage(pre));",
        'alert("Reopen failed: "',
        'alert("Delete failed: "',
        'alert("Forget failed: "',
    ),
}


def test_the_rail_and_recently_closed_never_call_a_native_dialog():
    for rel, old_sites in _NATIVE_FREE.items():
        code = _code(rel)
        for absent in ("confirm(", "alert(", "prompt("):
            assert absent not in code, (rel, absent)
        # The exact old call sites, so a rename of the helper can't hide one.
        for old in old_sites:
            assert old not in _src(rel), (rel, old)
        # Failures go to an error card instead.
        assert "errorPop(" in code, rel


def test_switch_failures_are_titled_with_the_intake_switch_they_mirror():
    assert 'errorPop("Automated ingestion failed", errMsg(err));' in _code(
        "components/sidebar/AutomationBar.tsx"
    )
    assert "errorPop(`${toggleLabel} failed`, errMsg(err));" in _code(
        "components/sidebar/useGithubToggleBar.ts"
    )
    assert 'toggleLabel: "Automated review"' in _code(
        "components/sidebar/PrReviewBar.tsx"
    )
    assert 'toggleLabel: "Automated handling"' in _code(
        "components/sidebar/GitIssueBar.tsx"
    )


def test_delete_and_wipe_asks_inline_in_two_steps():
    row = _code("components/sidebar/SidebarRow.tsx")
    assert "const [wipeArmed, setWipeArmed] = useState(false);" in row
    assert "Delete + wipe worktree" in row  # the first click arms it
    assert "and its folder?" in row
    assert re.search(r">\s*Delete \+ wipe\s*</button>", row)
    assert re.search(r">\s*Keep\s*</button>", row)
    # Focus lands on Keep, never on the wipe: a second Enter can't delete.
    assert "autoFocus onClick={() => setWipeArmed(false)}" in row
    armed = row[row.index("(wipeArmed ? (") : row.index("Delete + wipe worktree")]
    assert armed.count("autoFocus") == 1
    assert 'errorPop("Delete failed", errMsg(err));' in row


def test_recently_closed_asks_in_one_inline_bar():
    dlg = _code("components/dialogs/RecentDialog.tsx")
    assert (
        "const [ask, setAskState] = useState<(Ask & { seq: number }) | null>(null);"
        in dlg
    )
    assert 'id="recent-ask"' in dlg
    # The sweep keeps its id (capability gating) and its two-step question,
    # the second answered by buttons rather than OK / Cancel.
    assert 'id="recent-prune"' in dlg
    assert "dirtyChoices(pre)" in dlg
    assert "run: () => sweep(true)," in dlg
    assert "run: () => sweep(false)" in dlg
    assert "OK — delete all" not in _src("components/dialogs/recentRows.ts")


# --- Bars: one name each, and the name is the door ----------------------------------


def test_bar_labels_are_the_intake_tab_names():
    defs = _code("components/sidebar/barDefs.ts")
    assert '{ key: "ingestion", label: "Tickets" }' in defs
    assert '{ key: "pr-review", label: "Pull requests" }' in defs
    assert '{ key: "issue-handling", label: "Issues" }' in defs
    for old in ("Ticket Ingestion", "PR Review", "Issue Handling"):
        assert old not in defs, old
    # The owner's out-of-the-box set is unchanged.
    assert 'DEFAULT_VISIBLE_BARS = ["usage", "ingestion", "assistant"]' in defs


def test_each_bar_label_is_one_door_button_keeping_its_old_id():
    for rel, bid, label, tab in (
        (
            "components/sidebar/AutomationBar.tsx",
            "mindflock-tickets-btn",
            "Tickets",
            "tickets",
        ),
        (
            "components/sidebar/PrReviewBar.tsx",
            "pr-review-prs-btn",
            "Pull requests",
            "prs",
        ),
        (
            "components/sidebar/GitIssueBar.tsx",
            "git-issue-repos-btn",
            "Issues",
            "issues",
        ),
    ):
        code = _code(rel)
        at = code.index(f'id="{bid}"')
        button = code[code.rindex("<button", 0, at) : code.index("</button>", at)]
        assert 'className="dc-label dc-open"' in button, rel
        assert f'title="Open Intake → {label}"' in button, rel
        assert f'openDialogFor("intake", "{tab}")' in button, rel
        assert button.rstrip().endswith(label), rel
        # No second label span, and no separate open button beside the switch.
        assert '<span className="dc-label">' not in code, rel
        assert 'className="dc-toggle"' not in code, rel
        assert 'className="dc-switch"' in code, rel


def test_the_door_style_exists():
    css = _src("components/sidebar/toolbars.css")
    assert ".dc-open" in css
    assert "#mindflock-bar .dc-open:focus-visible" in css


def test_the_assistant_bar_is_chat_todo_and_agent():
    bars = _code("components/sidebar/SidebarBars.tsx")
    assert 'id="assistant-chat-btn"' in bars
    assert 'id="assistant-todo-btn"' in bars
    assert 'id="assistant-agent-btn"' in bars
    assert 'openDialogFor("assistant-agent")' in bars


# --- Sidebar: fewer banners, one link, one picker ------------------------------------


def test_no_welcome_hint_and_the_customize_hint_points_at_prompts():
    side = _code("components/sidebar/Sidebar.tsx")
    assert 'id="welcome"' not in side
    assert "Welcome to MindFlock." not in side
    assert "More sidebar bars" in side and "Prompts" in side
    assert "Outbox" not in side
    footer = _code("components/sidebar/FooterCustomize.tsx")
    assert "⚙ Customize" in footer
    assert "Choose which bars the sidebar shows" in footer


def test_doctor_chip_waits_out_the_first_run_card():
    side = _code("components/sidebar/Sidebar.tsx")
    assert (
        "const firstRunCard = instances.length === 0 && config?.onboarded === false;"
        in side
    )
    assert "doctorWarn.failing && !doctorWarn.dismissed && !firstRunCard" in side
    for bid in (
        'id="doctor-warn"',
        'id="doctor-warn-open"',
        'id="doctor-warn-dismiss"',
    ):
        assert bid in side, bid


def test_recently_closed_is_a_link_under_the_list_never_a_rail_row():
    side = _code("components/sidebar/Sidebar.tsx")
    at = side.index('id="recent-btn"')
    # It sits after the list closes, and its tag is a <button>, not an <li>
    # (the rail's SSR tests split the markup on '<li class="').
    assert side.index("</ul>") < at
    tag = side[side.rindex("<", 0, at) :]
    assert tag.startswith("<button"), tag[:40]
    assert "{closedCount > 0 && (" in side
    assert 'className="foot-link recent-link"' in side
    assert 'onClick={() => ui.openDialogFor("recent")}' in side
    # Same key and endpoint as the Verify dialog, refreshed when a session closes.
    assert 'queryKey: ["recently-closed"]' in side
    assert '"/api/recently-closed"' in side
    assert 'ev.subscribe("session.deleted"' in side
    assert "window.mindflock?.events" in side


def test_the_view_buttons_and_footer_icons_are_back():
    """The owner missed them: Auto / 1 / 2 / 4 / 9 as buttons, and the ⚙ / ⌨
    glyphs on Customize and Shortcuts."""
    side = _code("components/sidebar/Sidebar.tsx")
    assert 'id="view-modes"' in side
    assert 'className={"vm" + (ui.viewMode === v ? " active" : "")}' in side
    assert "onClick={() => ui.setViewMode(v)}" in side
    assert 'id="view-mode-select"' not in side
    assert "⌨ Shortcuts" in side
    css = _src("components/sidebar/SidebarRow.css")
    assert "#view-modes .vm.active {" in css
    assert "#view-modes select" not in css
    side_css = _src("components/sidebar/Sidebar.css")
    assert "#foot-customize-menu" not in side_css
    assert ".fc-item" not in side_css


# --- Rail rows ------------------------------------------------------------------------


def test_a_lane_line_waiting_on_you_opens_the_bell():
    row = _code("components/sidebar/SidebarRow.tsx")
    # Only lines the bell holds a row for: an approval, or a group line that
    # needs you / failed. A halted fast-track is "escalated" too, but the bell
    # has nothing for it, so it stays plain text.
    assert '(ship.state === "escalated" || ship.state === "approve")' not in row
    assert 'ship.state === "approve"' in row
    assert '(runTaskState === "needs_you" || runTaskState === "failed")' in row
    assert 'new CustomEvent("mf-open-bell", { detail: { title } })' in row
    assert 'role: "button"' in row
    assert "onMouseDown: (e: MouseEvent) => e.stopPropagation()," in row
    assert ".inst .lineage.opens-bell" in _src("components/sidebar/SidebarRow.css")


def test_merge_is_named_for_what_it_merges():
    row = _src("components/sidebar/SidebarRow.tsx")
    assert "Merge to staging" not in row
    assert "Merge PR{" in row


def test_only_a_lane_line_waiting_on_you_is_a_door():
    row = _code("components/sidebar/SidebarRow.tsx")
    # The class, the tooltip line and the button props are all gated on the
    # one flag, so a running/merged/queued line stays plain text.
    assert '(shipOpensBell ? " opens-bell" : "")' in row
    assert 'shipOpensBell ? "Click to open it in the bell" : ""' in row
    assert "{...(shipOpensBell\n" in row
    assert "tabIndex: 0," in row
    # Keyboard: Enter and Space both open it, and neither reaches the row.
    keys = row[row.index("onKeyDown: (e: React.KeyboardEvent)") :]
    keys = keys[: keys.index("},\n")]
    assert 'e.key !== "Enter" && e.key !== " "' in keys
    assert "e.preventDefault();" in keys and "e.stopPropagation();" in keys
    assert "openBell();" in keys
    # The click is the line's own (act() stops it), never the row's select.
    assert "onClick: (e: MouseEvent) => act(openBell, e)," in row
    assert "onDoubleClick: (e: MouseEvent) => e.stopPropagation()," in row


def test_the_wipe_only_fires_from_the_armed_state_and_closing_disarms():
    row = _code("components/sidebar/SidebarRow.tsx")
    assert "if (!expanded) setWipeArmed(false);" in row
    armed = row.index("(wipeArmed ? (")
    unarmed = row.index(
        '<button className="danger" onClick={() => setWipeArmed(true)}>', armed
    )
    # The one /cleanup call lives in the armed branch; the unarmed button only arms.
    cleanup = row.index('instApi(title, "/cleanup", { method: "POST" })')
    assert row.count('"/cleanup"') == 1
    assert armed < cleanup < unarmed
    assert "Delete <b>{shown}</b> and its folder?" in row
    # A failed wipe still un-hides the row and refreshes, after the card.
    after = row[cleanup : row.index("Delete + wipe\n", cleanup)]
    assert (
        after.index('errorPop("Delete failed"')
        < after.index("useUi.getState().setHidden(title, false);")
        < after.index("await refreshInstances();")
    )


def test_merge_keeps_its_browser_fallback_mark():
    row = _code("components/sidebar/SidebarRow.tsx")
    assert 'Merge PR{prSupport ? "" : " ↗"}' in row


# --- Recently closed: one question at a time -----------------------------------------


def test_closed_sessions_are_sent_to_recently_closed_by_its_one_name():
    """The top-bar "Recent…" button is gone; every pointer at it names the
    surviving door, "Recently closed" under the session list."""
    for rel in _SRC.rglob("*.ts*"):
        if "__tests__" in rel.parts:
            continue
        assert "Recent…" not in rel.read_text(encoding="utf-8"), rel
    actions = _code("lib/sessionActions.ts")
    assert (
        'toast("Session ended — reopen it from Recently closed, under the session list '
        '(or Ctrl+Z / Ctrl+Shift+T)");' in actions
    )
    server = (_SRC.parents[1] / "backend" / "web" / "server.py").read_text(
        encoding="utf-8"
    )
    assert "from Recent to finish" not in server
    assert "from Recently closed to finish or discard that work" in server


def test_the_sweep_never_runs_before_it_is_answered():
    dlg = _code("components/dialogs/RecentDialog.tsx")
    ask = dlg.index("text: pruneMessage(pre),")
    # Every sweep( call sits inside an answer, after the first question.
    assert all(m.start() > ask for m in re.finditer(r"\bsweep\((?:true|false)\)", dlg))
    assert "if (!pre.dirty_count) return sweep(false);" in dlg
    # All-dirty: no third answer, so "Delete anyway" or Cancel.
    assert (
        "alt: choice.clean ? { label: choice.clean, run: () => sweep(false) } : undefined,"
        in dlg
    )
    # Nothing to remove is a notice: no run, one button.
    assert 'setAsk({ text: nothingMessage(pre), okLabel: "OK" });' in dlg


def test_a_follow_up_question_refocuses_its_harmless_answer():
    """The dirty follow-up replaces the first question in place. Unkeyed, React
    kept the bar mounted: autoFocus never re-ran and focus stayed on the button
    just pressed — by position now "Delete all N", so a second Enter took the
    worktrees with uncommitted work. Each question keys a fresh bar."""
    dlg = _code("components/dialogs/RecentDialog.tsx")
    assert "setAskState(a && { ...a, seq: ++askSeq.current })" in dlg
    bar = dlg[dlg.index('id="recent-ask"') :]
    assert bar.index("key={ask.seq}") < bar.index("</div>")
    assert "autoFocus onClick={() => setAsk(null)}" in bar


def test_row_failures_raise_a_titled_card():
    dlg = _code("components/dialogs/RecentDialog.tsx")
    for title in ("Reopen failed", "Delete failed", "Forget failed"):
        assert f'errorPop("{title}", (err as Error).message);' in dlg, title


# --- Bars and sidebar ----------------------------------------------------------------


def test_the_rename_kept_every_persisted_bar_key():
    """Saved hiddenBars / barOrder in localStorage are keyed, not labelled."""
    defs = _code("components/sidebar/barDefs.ts")
    block = defs[
        defs.index("SIDEBAR_BARS: BarDef[] = [") : defs.index(
            "];", defs.index("SIDEBAR_BARS")
        )
    ]
    keys = re.findall(r'key: "([^"]+)"', block)
    assert keys == [
        "usage",
        "ingestion",
        "pr-review",
        "issue-handling",
        "verify",
        "assistant",
        "prompts",
    ]


def test_the_assistant_bar_no_longer_takes_a_dialog_opener():
    bars = _code("components/sidebar/SidebarBars.tsx")
    cbs = bars[
        bars.index("interface ContentCbs {") : bars.index(
            "}", bars.index("interface ContentCbs {")
        )
    ]
    assert "openDialogFor" not in cbs
    assert "DialogName" not in bars
    assert "assistant-instructions" not in bars and ">Instructions<" not in bars
    assert "barContent(key, { onOpenChat, onOpenTodo })" in _code(
        "components/sidebar/Sidebar.tsx"
    )


def test_the_view_buttons_offer_the_five_modes():
    side = _code("components/sidebar/Sidebar.tsx")
    assert (
        'const VIEW_MODES: ViewMode[] = ["auto", "1" as ViewMode, "2", "4", "9"];'
        in side
    )
    assert '{v === "auto" ? "Auto" : v}' in side
