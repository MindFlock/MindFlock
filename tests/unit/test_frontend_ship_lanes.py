"""Ship lanes, UI part 2 (SPEC §7.C.2-3, §8): structural checks on the shipped
bundle for the rail's run group headers, queued lines and lane-first status
lines, the Outbox (top-bar entry, Alt+O, dialog) and the bell's run rows.

The wording and the grouping arithmetic are unit-tested in
frontend/src/__tests__/{lanes,runs,outbox}.test.ts (runs.test.ts also
server-renders the real Sidebar to pin that a header or a queued line is never
numbered and that folding a group closes the numbering up); the rendering was
checked with the screenshot harness. These pin what only the bundle can show:
that the pieces are wired where the contract says, and that the things the
owner has been bitten by before are ABSENT.
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

from backend.web import server
from tests._bundle import in_bundle, squash

client = TestClient(server.app)

_SRC = Path(__file__).resolve().parents[2] / "frontend" / "src"


def _js() -> str:
    return client.get("/app.js").text


def _css() -> str:
    return client.get("/style.css").text


def _fn(js: str, name: str) -> str:
    """The bundled body of top-level ``function name(`` up to the next one."""
    start = js.index("function " + name + "(")
    nxt = re.search(r"\n(?:async )?function \w+\(|\nvar \w+ = ", js[start + 1 :])
    return js[start : start + 1 + (nxt.start() if nxt else len(js))]


# --- The rail: railOrder stays the numbering authority ---------------------------


def test_the_rail_publishes_the_grouped_sequence_as_railorder():
    """Grouping re-sequences rows, so railOrder must be the grouped sequence —
    and a folded device or a folded group must drop out of it the same way."""
    js = _js()
    sidebar = squash(_fn(js, "Sidebar"))
    # Rolldown inlines the single-use `localRail` into the call, so pin the
    # call and its options rather than the variable name.
    assert "const localSplit = splitRail(" in sidebar
    assert (
        "runs, { collapsed: ui.collapsedRuns, act: effectiveActivity, filtering: !!ui.filter });"
        in sidebar
    )
    assert '(ui.collapsedDevices.has("__self") ? [] : splitKeys(localSplit))' in sidebar
    assert ": splitKeys(localSplit);" in sidebar
    assert "useUi.getState().setRailOrder(displayedKeys);" in sidebar
    # Headers and queued lines render beside the rows, never through renderRail
    # (which is what numbers a row) — and the numbering is still rowIdx.
    assert "rowIdx += 1;" in sidebar
    assert "renderRail(g.entries, g.taskOf)" in sidebar


def test_splitkeys_never_emits_a_header_or_a_queued_line():
    body = squash(_fn(_js(), "splitKeys"))
    assert "if (!g.collapsed) for (const e of g.entries) out.push(e.key);" in body
    assert "for (const e of split.own) out.push(e.key);" in body
    assert "queued" not in body


def test_a_new_group_member_is_placed_after_its_group_by_merge():
    js = _js()
    body = squash(_fn(js, "placeNewRunMembers"))
    assert "!seen.has(r.title)" in body, "only titles the order never held"
    assert "orderWithAfter(order, r.title, rest[at])" in body
    sidebar = squash(_fn(js, "Sidebar"))
    # Layered on the worker placement, which keeps its own pinned call.
    assert (
        "placeNewRunMembers(placeNewWorkers(ui.order, listed.filter((i) => !i.device)), listed.filter((i) => !i.device))"
        in sidebar
    )


def test_a_queued_line_has_no_number_no_drag_and_no_kill():
    body = squash(_fn(_js(), "QueuedRow"))
    assert 'className: "inst run-queued"' in body
    assert 'className: "idx" })' in body or 'className: "idx" }' in body
    for absent in ("rowDndProps", "data-title", "killSession", "draggable"):
        assert absent not in body, absent


def test_the_group_header_is_the_device_header_family_and_folds_by_run():
    js = _js()
    body = squash(_fn(js, "RunGroupHeader"))
    assert '"device-group run-group-head"' in body
    assert "onClick: () => toggle(group.id)" in body
    assert "shippedBadge(group)" in body
    assert "group.needs > 0" in body
    # A fold persists like a device fold, under its own key.
    assert '"mf_runcollapse"' in js


def test_header_badges_never_wrap_and_the_lane_lead_never_shrinks():
    css = _css()
    assert in_bundle(
        ".run-group-head .dev-badge { margin-left: 0; padding: 1px 6px; flex: none; white-space: nowrap;",
        css,
    )
    assert in_bundle(".inst .lineage.ship-line .sl-lead { flex: 0 0 auto;", css)
    assert in_bundle(
        ".inst .lineage.ship-line .sl-rest { flex: 0 1 auto; min-width: 0;", css
    )
    assert in_bundle(".inst.run-queued { opacity: 0.62;", css)
    # Pane heads still carry no status chip — lanes live on the rail row.
    assert in_bundle(".pane-head .stagechip { display: none; }", css)


def test_every_lane_row_leads_with_its_lane():
    js = _js()
    flat = squash(js)
    assert 'className: "sl-lead", children: ship.lead' in flat
    body = squash(_fn(js, "shipLine"))
    # (The ⇡ verb is a single-use const Rolldown folds into the call, so the
    # glyph is pinned on its own.)
    for phrase in (
        '"? needs your answer"',
        '"⇡ " + (',
        '" — open the Outbox"',
        '" · working "',
        '", asks first"',
    ):
        assert phrase in body, phrase
    assert '"opening PR"' in body


# --- The Outbox ------------------------------------------------------------------


def test_the_outbox_sits_between_intake_and_verify_with_a_waiting_badge():
    js = _js()
    top = squash(_fn(js, "TopBar"))
    i, o, v = (
        top.index('id: "intake-btn"'),
        top.index('id: "outbox-btn"'),
        top.index('id: "verify-btn"'),
    )
    assert i < o < v
    # Hidden at zero — a "0" reads as "nothing to do" while it is still loading.
    assert (
        'waiting > 0 && /* @__PURE__ */ (0, import_jsx_runtime.jsx)("span", { className: "tb-count", children: waiting })'
        in top
        or ("waiting > 0 &&" in top and '"tb-count"' in top)
    )
    assert "const waiting = waitingCount(outbox);" in top


def test_alt_o_opens_the_outbox_and_is_on_the_shortcuts_sheet():
    flat = squash(_js())
    assert 'key: "o", alt: true, id: "outbox",' in flat
    assert (
        'help: [ "Navigation", "Alt+O", "Outbox — what\'s shipping, and what\'s waiting on you" ]'
        in flat
    )
    assert 'run: () => useUi.getState().openDialogFor("outbox")' in flat
    # A modal: Delete / Ctrl+W must not end the session behind it.
    assert '"outbox-dialog"' in flat


def test_the_outbox_reads_one_response_and_renders_the_four_sections():
    js = _js()
    assert '"/api/outbox?group=all"' in js
    body = squash(_fn(js, "OutboxDialog"))
    for heading in (
        '"Waiting on you"',
        '"Shipping now"',
        '"Shipped today"',
        '"Queued"',
    ):
        assert heading in body, heading
    assert '"answer or approve — nothing else needs you"' in body
    assert '"MindFlock is doing these — no action"' in body
    assert "outboxTabs(data, rowOf, runName)" in body
    assert "viewFor(data, tab, rowOf)" in body


def test_a_prompt_row_reuses_the_answer_strip_and_an_approval_shows_the_message_first():
    js = _js()
    waiting = squash(_fn(js, "WaitingRow"))
    assert 'variant: "thread"' in waiting
    preview = squash(_fn(js, "ApprovePreview"))
    assert '"Message"' in preview and '"Then"' in preview
    # The server says where the lane stops (`lane` on the approve item); the
    # row's own lane is the fallback.
    assert "thenText(w.step, lane)" in preview
    assert "w.lane ||" in preview
    # Only a commit-step approval can change the message (a push-step one has
    # committed already), and an absent preview still offers the edit.
    assert '(w.step || "commit") === "commit"' in preview
    assert "written from the diff when it commits" in preview
    ship = squash(_fn(js, "ApproveButtons"))
    assert '"/ship-now"' in ship
    assert "commit_message: msg" in ship


def test_a_budget_item_raises_by_resuming_and_stops_by_cancelling():
    """The server's budget item carries actions ["raise_budget","stop"]: raise
    is `resume {budget_usd}` (one call), stop is `cancel` — no other route."""
    js = _js()
    row = squash(_fn(js, "BudgetRow"))
    assert 'includes("raise_budget")' in row and 'includes("stop")' in row
    assert "raiseBudget(runId, amount, name)" in row
    assert "cancelRun(runId, name)" in row
    assert squash(_fn(js, "WaitingRow")).count('w.kind === "budget"') == 1
    raise_ = squash(_fn(js, "raiseBudget"))
    assert 'runPath(runId, "/resume"), { budget_usd: usd }' in raise_
    assert 'runPath(runId, "/cancel")' in squash(_fn(js, "cancelRun"))


def test_split_and_one_for_all_wait_on_the_servers_caps():
    """caps.team_runs gates the One-for-all choice, the split box and the
    Split… item; nothing else decides. Phase 3 shipped both, so nothing says
    "coming next" any more — an OLDER server's refusal says to update it."""
    js = _js()
    caps = squash(_fn(js, "teamRunCaps"))
    assert "t?.split === true" in caps and "t?.together === true" in caps
    assert "teamRunCaps(caps).split" in squash(_fn(js, "splitBlockReason"))
    assert "coming next" not in js
    assert "this MindFlock server can't split a line into pieces — update it" in js
    assert "this MindFlock server can't make one PR for a group — update it" in js


def test_rows_dedupe_on_repo_branch():
    body = squash(_fn(_js(), "dedupe"))
    assert "const k = it.key || it.title" in body


def test_no_native_dialogs_and_no_paste_playbooks_in_the_new_ui():
    """Electron has no window.prompt / confirm; ship lanes act, never paste."""
    js = _js()
    for name in (
        "OutboxDialog",
        "WaitingRow",
        "ApproveButtons",
        "ApprovePreview",
        "QueuedItem",
        "SummaryCard",
        "RunGroupHeader",
        "RunGroupMenu",
        "QueuedRow",
        "splitRail",
        "shipLine",
    ):
        body = _fn(js, name)
        for absent in (
            "window.prompt",
            "prompt(",
            "confirm(",
            "alert(",
            "pastes the prompt",
            "pastePlaybook(",
        ):
            assert absent not in body, (name, absent)
    # The cancel confirmation is an inline row in the menu.
    menu = squash(_fn(js, "RunGroupMenu"))
    assert (
        '"Stop starting new work and stop shipping? Sessions and branches are kept."'
        in menu
    )
    assert '"Cancel group"' in menu


def test_new_source_files_have_no_native_dialog_calls():
    """Source-level twin of the above, comments stripped (they explain why)."""
    for rel in (
        "components/outbox/OutboxDialog.tsx",
        "components/outbox/outbox.ts",
        "components/sidebar/RunGroupHeader.tsx",
        "lib/runs.ts",
        "state/runs.ts",
    ):
        src = (_SRC / rel).read_text(encoding="utf-8")
        code = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
        code = re.sub(r"//[^\n]*", "", code)
        for absent in ("window.prompt", "confirm(", "alert(", "pastePlaybook("):
            assert absent not in code, (rel, absent)


# --- Notifications: suppressed at the emitter, gated in the bell ------------------


def test_the_bell_phrases_run_events_behind_the_rule_switches_and_dedupes():
    js = _js()
    bell = squash(_fn(js, "notifFromEvent"))
    for ev in (
        'case "run.needs_you":',
        'case "run.task_shipped":',
        'case "run.finished":',
    ):
        assert ev in bell, ev
    assert "run.changed" not in bell, "run.changed is a refetch, never a row"
    flat = squash(js)
    assert "if (n.rule && !ruleOn(n.rule)) return;" in flat
    assert (
        "if (n.dedupe && prev.some((x) => x.dedupe === n.dedupe)) return prev;" in flat
    )
    note = squash(_fn(js, "runNote"))
    assert 'if (reason === "prompt") return null;' in note


def test_run_toasts_are_throttled_to_one_per_30s_and_skip_replays():
    flat = squash(_js())
    assert 'notifyOnce("*run", "run", n.text, {' in flat
    assert 'for (const name of ["run.needs_you", "run.finished"])' in flat


# --- Phase 3: split and one for all ----------------------------------------------
def test_the_leads_thread_shows_plan_ship_card_and_pieces():
    """SPEC §7.C.4: the lead's Thread has the plan card (Start N workers / Ask
    for a different split), the ship card (.rb-card) with the three release
    buttons, and a row per piece — every button posts to the run routes."""
    js = _js()
    panel = squash(_fn(js, "RunLeadPanel"))
    for s in (
        '"Ask for a different split"',
        # The release buttons come from releaseChoices(run.policy.lane): the
        # group's own lane is the primary, "Open the PR" never merges.
        "releaseChoices(run.policy",
        '"Review the diff"',
        '"Run the check again"',
        '"/plan/approve"',
        '"/plan/reject"',
        '"/release"',
        '"/check"',
        "merge_when_green: merge",
        "rb-card rb-ship",
        "rb-card rb-plan",
        '"only here: "',
    ):
        assert s in panel, s
    choices = squash(_fn(js, "releaseChoices"))
    for s in (
        '"Open the PR"',
        '"Open it, merge when checks pass"',
        '"Open the PR, merge when checks pass"',
        '"Open the PR only"',
        '"Push the branch"',
    ):
        assert s in choices, s
    # The release card is not a "Ship" anything (one name: fast-track).
    assert '"Release · one PR"' in panel
    assert "Ship · one PR" not in js
    assert '"planned by the lead · started and merged by MindFlock"' in panel
    # The Thread tab hands a run lead to the panel and drops the paste buttons.
    tab = squash(_fn(js, "ThreadTab"))
    assert 'me?.run?.role === "lead" ? me.run : null' in tab
    assert "hasWorkers && !leadOf &&" in tab


def test_the_lead_chip_asks_for_your_click_only():
    chip = squash(_fn(js := _js(), "leadChip"))
    assert '"→ PR?"' in chip and '"plan?"' in chip
    assert 'run.state === "release_ready"' in chip
    row = squash(_fn(js, "SidebarRow"))
    assert "leadChip(leadRun)" in row
    assert "stagechip wrapchip leadchip" in row
    # A run lead never offers the paste-the-wrap-up chip.
    assert "kids.length && !pending && !missing && !isLead" in row


def test_split_ui_has_no_native_dialogs_and_pastes_nothing():
    js = _js()
    for name in (
        "RunLeadPanel",
        "leadChip",
        "leadLine",
        "pieceStatus",
        "releaseCard",
        "waitingActions",
        "doWaitAction",
    ):
        body = _fn(js, name)
        for absent in (
            "window.prompt",
            "prompt(",
            "confirm(",
            "alert(",
            "pastes the prompt",
            "pastePlaybook(",
        ):
            assert absent not in body, (name, absent)


def test_a_push_lane_is_never_nagged_ready_for_pr():
    body = squash(_fn(_js(), "attentionItems"))
    assert 'laneOf(inst)?.target !== "push"' in body
    assert '"pushed — ready for PR"' in body


def test_a_merged_piece_keeps_one_chip_and_its_name_is_never_overlaid():
    """Real-server defect (a 4-session split, long slugs): the "merged" chip
    drew ACROSS the piece's name, beside a redundant "✓ checks" and "✓". The
    status column now holds the title's floor and clips; a merged-back piece
    and a lead asking for its release drop the quiet chips."""
    row = squash(_fn(_js(), "SidebarRow"))
    assert 'integrated: runTask?.state === "integrated"' in row
    assert "leadAsks: !!lchip" in row
    assert "extra.check &&" in row
    css = squash(
        (
            Path(__file__).resolve().parents[2] / "backend/web/static/style.css"
        ).read_text()
    )
    i = css.find(".inst .meta.has-lineage {")
    assert i >= 0
    block = css[i : css.find("}", i)]
    assert "min-width: 34px" in block and "overflow: hidden" in block


def test_a_finished_one_for_all_group_keeps_its_family_lines():
    """Real-server defect: once a one-for-all trio FINISHED the rail fetched
    only its summary, so its pieces fell back to "→ commit · idle" and its
    lead to a red "fast-track stopped" over the no-gh hand-off — which is
    how a release is meant to end without gh. A recent together group keeps
    its details; the lead says "✓ PR #N" or "⇡ pushed — open the PR"."""
    js = _js()
    fetch = squash(_fn(js, "fetchRuns"))
    assert "needsRunDetail(r, nowS)" in fetch
    need = squash(_fn(js, "needsRunDetail"))
    assert 'r.policy?.grouping === "together"' in need
    line = squash(_fn(js, "leadLine"))
    assert '"⇡ pushed — open the PR"' in line
    assert 'run.release?.state === "handoff"' in line
    row = squash(_fn(js, "SidebarRow"))
    assert "RUN_DONE_STATES.has(leadRun.state)" in row


def test_a_new_session_with_fast_track_gets_its_own_worktree():
    """Live L2: two single sessions with a lane were created IN PLACE on main;
    one approval card committed both sessions' files. With fast-track on the
    New dialog never sends in_place, and says why on the radio — which now
    turns fast-track off rather than sitting disabled."""
    js = _js()
    assert "inPlace: inPlace && !laneNeedsWorktree" in js
    assert "turns fast-track off — it commits for this session" in js
    assert "not with a lane" not in js
