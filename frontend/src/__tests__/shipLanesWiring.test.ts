/** Ship lanes, UI part 2 (SPEC §7.C.2-3, §8): the structural pins for the
 * rail's run group headers, queued lines and lane-first status lines and
 * their ⋯ menu (queued lines, Copy summary), the bell's waiting rows and its
 * run rows — and that the Outbox, dropped as a place, is ABSENT.
 *
 * Moved from tests/unit/test_frontend_ship_lanes.py, which grepped the built
 * app.js / style.css. These read the SOURCE function or stylesheet that owns
 * each piece (comments stripped, layout-blind — see sourceText.ts), so a pin names
 * where it lives. The wording and the grouping arithmetic are unit-tested in
 * lanes/runs/outbox.test.ts (runs.test.ts also server-renders the real
 * Sidebar to pin that a header or a queued line is never numbered and that
 * folding a group closes the numbering up); the rendering was checked with
 * the screenshot harness. These pin that the pieces are wired where the
 * contract says, and that the things the owner has been bitten by before are
 * ABSENT. */

import { describe, expect, it } from "vitest";
import { SRC, allCode, normCode, fn, has, stripComments } from "./sourceText";

const SIDEBAR = "components/sidebar/Sidebar.tsx";
const RGH = "components/sidebar/RunGroupHeader.tsx";
const WAIT = "components/outbox/WaitingRow.tsx";
const OUTBOX = "components/outbox/outbox.ts";
const SPLIT = "lib/splitRun.ts";
const LEAD = "components/grid/RunLeadPanel.tsx";
const ROW = "components/sidebar/SidebarRow.tsx";

function expectAll(hay: string, needles: string[]) {
  for (const n of needles) expect(has(hay, n), n).toBe(true);
}

describe("the rail: railOrder stays the numbering authority", () => {
  it("publishes the grouped sequence as railOrder", () => {
    // Grouping re-sequences rows, so railOrder must be the grouped sequence —
    // and a folded device or a folded group must drop out of it the same way.
    const sidebar = fn(SIDEBAR, "Sidebar");
    expectAll(sidebar, [
      "const localSplit = splitRail(localRail, runs, { collapsed: ui.collapsedRuns, act: effectiveActivity, filtering: !!ui.filter });",
      '(ui.collapsedDevices.has("__self") ? [] : splitKeys(localSplit))',
      ": splitKeys(localSplit);",
      "useUi.getState().setRailOrder(displayedKeys);",
      // Headers and queued lines render beside the rows, never through
      // renderRail (which is what numbers a row) — and the numbering is
      // still rowIdx.
      "rowIdx += 1;",
      "renderRail(g.entries, g.taskOf)",
    ]);
  });

  it("splitKeys never emits a header or a queued line", () => {
    const body = fn("lib/runs.ts", "splitKeys");
    expectAll(body, [
      "if (!g.collapsed) for (const e of g.entries) out.push(e.key);",
      "for (const e of split.own) out.push(e.key);",
    ]);
    expect(body).not.toContain("queued");
  });

  it("places a new group member after its group, by merge", () => {
    const body = fn("components/sidebar/ordering.ts", "placeNewRunMembers");
    expect(has(body, "!seen.has(r.title)"), "only titles the order never held").toBe(true);
    expect(has(body, "orderWithAfter(order, r.title, rest[at])")).toBe(true);
    // Layered on the worker placement, which keeps its own pinned call.
    expect(
      has(fn(SIDEBAR, "Sidebar"), "placeNewRunMembers(placeNewWorkers(ui.order, listed), listed.filter((i) => !i.device))")
    ).toBe(true);
  });

  it("a queued line has no number, no drag and no kill", () => {
    const body = fn(RGH, "QueuedRow");
    expectAll(body, ['className="inst run-queued"', '<span className="idx" />']);
    for (const absent of ["rowDndProps", "data-title", "killSession", "draggable"]) {
      expect(body, absent).not.toContain(absent);
    }
  });

  it("the group header is the device header family and folds by run", () => {
    const body = fn(RGH, "RunGroupHeader");
    expectAll(body, [
      '"device-group run-group-head"',
      "onClick={() => toggle(group.id)}",
      "shippedBadge(group)",
      "group.needs > 0",
    ]);
    // A fold persists like a device fold, under its own key.
    expect(has(normCode("state/store.ts"), 'load<string[]>("mf_runcollapse", [])')).toBe(true);
    expect(has(normCode("state/store.ts"), 'save("mf_runcollapse", [...next])')).toBe(true);
  });

  it("header badges never wrap and the lane lead never shrinks", () => {
    const css = normCode("components/sidebar/RunGroup.css");
    expectAll(css, [
      ".run-group-head .dev-badge { margin-left: 0; padding: 1px 6px; flex: none; white-space: nowrap;",
      ".inst .lineage.ship-line .sl-lead { flex: 0 0 auto;",
      ".inst .lineage.ship-line .sl-rest { flex: 0 1 auto; min-width: 0;",
      ".inst.run-queued { opacity: 0.62;",
    ]);
    // Pane heads still carry no status chip — lanes live on the rail row.
    expect(has(normCode("components/grid/DiffTab.css"), ".pane-head .stagechip { display: none; }")).toBe(true);
  });

  it("every lane row leads with its lane", () => {
    expect(has(fn(ROW, "SidebarRow"), '<span className="sl-lead">{ship.lead}</span>')).toBe(true);
    const body = fn("lib/agentMessages.ts", "shipLine");
    // (The ⇡ verb is a const shared by the label and the tooltip, so the
    // glyph is pinned on its own.)
    expectAll(body, ['"? needs you · question"', '"⇡ " + verb', '" — open the bell"', '" · working "', '", asks first"']);
    expect(has(body, '"opening PR"')).toBe(true);
    // The tooltip names the step, never the umbrella "shipping it".
    expect(has(body, '"MindFlock is " + verb')).toBe(true);
    expect(body).not.toContain("shipping it");
    expect(body, "what waits on you is the bell's").not.toContain("Outbox");
  });
});

describe("no Outbox: the bell and the group header", () => {
  it("the top bar carries only the daily loop", () => {
    // The Outbox, Prompts and Recent buttons are gone (dropped, Customize, the
    // session list); Verify always shows; the palette is the last item, after
    // Settings.
    const top = fn("components/TopBar.tsx", "TopBar");
    for (const gone of ['"outbox-btn"', '"prompts-btn"', '"recent-btn"']) expect(top, gone).not.toContain(gone);
    const i = top.indexOf('id="intake-btn"');
    const v = top.indexOf('id="verify-btn"');
    const s = top.indexOf('id="settings-btn"');
    const p = top.indexOf('id="palette-btn"');
    expect(i).toBeGreaterThanOrEqual(0);
    expect(i < v && v < s && s < p).toBe(true);
    // The shortcut comes from the live keymap, never a hand-typed string.
    expect(has(top, 'title={"Command palette" + keys("palette")}')).toBe(true);
    expect(top).not.toContain("waitingCount");
    expect(top).not.toContain("useOutbox");
    // Always rendered: no earn-its-slot gate, no per-device memory of one.
    expect(top).not.toContain("showVerify");
    expect(allCode()).not.toContain("mf_tb_verify");
  });

  it("the Outbox is gone from every door", () => {
    // No tab, no dialog, no shortcut, no palette entry, no word on screen.
    const all = allCode();
    for (const gone of [
      '"outbox-btn"',
      'id="outbox-btn"',
      "Customize → Outbox",
      "Open in the Outbox",
      'label: "Outbox"',
      '"Alt+O"',
      "Outbox — what's on its way out",
      'openDialogFor("outbox"',
      '"outbox-dialog"',
      'id="outbox-dialog"',
      '"outbox-panel"',
      'id="outbox-panel"',
      '"Shipped today"',
      ">Shipped today<",
      "function OutboxPanel(",
      "function outboxTabs(",
      "function SummaryCard(",
      "function QueuedItem(",
    ]) {
      expect(all, gone).not.toContain(gone);
    }
    // Every "Outbox" left in the app's code is part of an identifier
    // (OutboxResponse, fetchOutbox, useOutbox…) or the stylesheet's import
    // path — never a word the user reads.
    for (const [file, text] of Object.entries(SRC)) {
      const c = stripComments(text);
      for (const m of c.matchAll(/\w*Outbox\w*/g)) {
        if (m[0] !== "Outbox") continue;
        throw new Error(`a bare "Outbox" in ${file}: …${c.slice(Math.max(0, m.index! - 40), m.index! + 40)}…`);
      }
    }
    // A modal: Delete / Ctrl+W must not end the session behind Customize.
    expect(has(fn("components/customize/CustomizeDialog.tsx", "CustomizeDialog"), 'id="customize-dialog"')).toBe(true);
  });

  it("the bell still reads the waiting list from one response", () => {
    expect(has(normCode("state/runs.ts"), 'api<OutboxResponse>("/api/outbox?group=all")')).toBe(true);
    const bell = fn("components/NotificationsBell.tsx", "NotificationsBell");
    expect(has(bell, "needsAttention(attentionItems(instances), outbox?.groups?.waiting)")).toBe(true);
    expect(has(bell, 'document.addEventListener("mf-open-bell", onOpen)')).toBe(true);
  });

  it("a finished group's menu copies its summary", () => {
    // The summary card's one job, moved to the group header's ⋯: copy the
    // group's Markdown. `/api/outbox` keeps a summary for a week, the header
    // stays while any member is open — so an older group's comes from its own
    // run record, and a missing one never claims it is "not yet" written. An
    // unfinished group has no such item — its queued lines, Pause and Cancel
    // are already in that menu, and what waits on you is the bell's.
    const menu = fn(RGH, "RunGroupMenu");
    expectAll(menu, [
      "summaryFor(outbox, group.id)",
      "useRun(group.done && !kept ? group.id : null)",
      "summaryText(outbox, group.id, record)",
      "group.done && ",
      "Copy summary <span",
      "disabled={!summary}",
      "copyText(summary)",
      '"Copied the summary as Markdown"',
      '"MindFlock has no summary for this team run"',
    ]);
    expect(allCode()).not.toContain("No summary for this group yet");
    // Queued lines keep their Start now / Take out of run here.
    expectAll(menu, ['taskPath(id, t.id, "start-now")', 'taskPath(id, t.id, "skip")']);
    expect(has(fn(OUTBOX, "summaryFor"), "x.run === runId")).toBe(true);
    expect(has(fn(OUTBOX, "summaryText"), "run?.summary?.text_md")).toBe(true);
  });

  it("a headerless group's menu items live on its lead's Thread", () => {
    // A split / one-for-all group is a family under its lead — no rail
    // header, so no ⋯. Its lead's Thread holds what that menu would: Start
    // now / Take out of run on a queued piece, Copy summary once finished.
    const panel = fn(LEAD, "RunLeadPanel");
    expectAll(panel, [
      "taskPath(id, t.id, verb)",
      '"start-now"',
      '"skip"',
      '"Start now"',
      '"Take out of run"',
      "summaryText(null, id, run)",
      "Copy summary",
      '"Copied the summary as Markdown"',
    ]);
    expect(panel).not.toContain('"Remove"');
    expect(panel).not.toContain("> Remove <");
  });

  it("a prompt row reuses the answer strip and an approval shows the message first", () => {
    expect(has(fn(WAIT, "WaitingRow"), 'variant="thread"')).toBe(true);
    const preview = fn(WAIT, "ApprovePreview");
    expectAll(preview, ["<span>Message</span>", "<span>Then</span>"]);
    // The server says where the lane stops (`lane` on the approve item); the
    // row's own lane is the fallback.
    expectAll(preview, ["thenText(w.step, lane)", "w.lane ||"]);
    // Only a commit-step approval can change the message (a push-step one has
    // committed already), and an absent preview still offers the edit.
    expectAll(preview, ['(w.step || "commit") === "commit"', "written from the diff when it commits"]);
    const ship = fn(WAIT, "ApproveButtons");
    expectAll(ship, ['"/ship-now"', "commit_message: msg"]);
  });

  it("a budget item raises by resuming and stops by cancelling", () => {
    // The server's budget item carries actions ["raise_budget","stop"]: raise
    // is `resume {budget_usd}` (one call), stop is `cancel` — no other route.
    const row = fn(WAIT, "BudgetRow");
    expectAll(row, ['includes("raise_budget")', 'includes("stop")', "raiseBudget(runId, amount, name)", "cancelRun(runId, name)"]);
    expect(fn(WAIT, "WaitingRow").split('w.kind === "budget"').length - 1).toBe(1);
    expect(has(fn("lib/runsApi.ts", "raiseBudget"), 'runPath(runId, "/resume"), { budget_usd: usd }')).toBe(true);
    expect(has(fn("lib/runsApi.ts", "cancelRun"), 'runPath(runId, "/cancel")')).toBe(true);
  });

  it("split and one-for-all wait on the server's caps", () => {
    // caps.team_runs gates the One-for-all choice, the split box and the
    // Split… item; nothing else decides. Phase 3 shipped both, so nothing
    // says "coming next" any more — an OLDER server's refusal says to update.
    const caps = fn("lib/laneActions.ts", "teamRunCaps");
    expectAll(caps, ["t?.split === true", "t?.together === true"]);
    expect(has(fn("lib/laneActions.ts", "splitBlockReason"), "teamRunCaps(caps).split")).toBe(true);
    const all = allCode();
    expect(all).not.toContain("coming next");
    expect(all).toContain("this MindFlock server can't split a line into pieces — update it");
    expect(all).toContain("this MindFlock server can't make one PR for a team run — update it");
  });

  it("rows dedupe on repo + branch", () => {
    expect(has(fn(OUTBOX, "dedupe"), 'const k = it.key || it.title')).toBe(true);
  });

  it("no native dialogs and no paste playbooks in the new UI", () => {
    // Electron has no window.prompt / confirm; ship lanes act, never paste.
    const bodies: Array<[string, string]> = [
      ["components/customize/CustomizeDialog.tsx", "CustomizeDialog"],
      ["components/customize/SidebarBarsPicker.tsx", "SidebarBarsPicker"],
      [WAIT, "WaitingRow"],
      [WAIT, "ApproveButtons"],
      [WAIT, "ApprovePreview"],
      [RGH, "RunGroupHeader"],
      [RGH, "RunGroupMenu"],
      [RGH, "QueuedRow"],
      ["lib/runs.ts", "splitRail"],
      ["lib/agentMessages.ts", "shipLine"],
    ];
    for (const [file, name] of bodies) {
      const body = fn(file, name);
      for (const absent of ["window.prompt", "prompt(", "confirm(", "alert(", "pastes the prompt", "pastePlaybook("]) {
        expect(body, name + ": " + absent).not.toContain(absent);
      }
    }
    // The cancel confirmation is an inline row in the menu.
    const menu = fn(RGH, "RunGroupMenu");
    expectAll(menu, [
      "<span>Stop starting new work and stop shipping? Sessions and branches are kept.</span>",
      "Cancel team run </button>",
    ]);
  });

  it("the new source files have no native dialog calls", () => {
    for (const rel of [
      "components/outbox/WaitingRow.tsx",
      "components/outbox/outbox.ts",
      "components/customize/CustomizeDialog.tsx",
      "components/customize/SidebarBarsPicker.tsx",
      "components/sidebar/RunGroupHeader.tsx",
      "lib/revealGroup.ts",
      "lib/showGroup.ts",
      "components/grid/RunLeadPanel.tsx",
      "lib/runs.ts",
      "state/runs.ts",
    ]) {
      const c = normCode(rel);
      for (const absent of ["window.prompt", "confirm(", "alert(", "pastePlaybook("]) {
        expect(c, rel + ": " + absent).not.toContain(absent);
      }
    }
  });
});

describe("notifications: suppressed at the emitter, gated in the bell", () => {
  it("the bell phrases run events behind the rule switches and dedupes", () => {
    const bell = fn("components/NotificationsBell.tsx", "notifFromEvent");
    expectAll(bell, ['case "run.needs_you":', 'case "run.task_shipped":', 'case "run.finished":']);
    expect(bell, "run.changed is a refetch, never a row").not.toContain("run.changed");
    const comp = fn("components/NotificationsBell.tsx", "NotificationsBell");
    expectAll(comp, [
      "if (n.rule && !ruleOn(n.rule)) return;",
      "if (n.dedupe && prev.some((x) => x.dedupe === n.dedupe)) return prev;",
    ]);
    expect(has(fn("lib/runs.ts", "runNote"), 'if (reason === "prompt") return null;')).toBe(true);
  });

  it("run toasts are throttled to one per 30s and skip replays", () => {
    const toasts = normCode("components/EventToasts.tsx");
    expectAll(toasts, ['notifyOnce("*run", "run", n.text, {', 'for (const name of ["run.needs_you", "run.finished"])']);
  });
});

describe("phase 3: split and one for all", () => {
  it("the lead's Thread shows the plan card, the ship card and the pieces", () => {
    // SPEC §7.C.4: the lead's Thread has the plan card (Start N workers / Ask
    // for a different split), the ship card (.rb-card) with the three release
    // buttons, and a row per piece — every button posts to the run routes.
    const panel = fn(LEAD, "RunLeadPanel");
    expectAll(panel, [
      "Ask for a different split",
      // The release buttons come from releaseChoices(run.policy.lane): the
      // group's own lane is the primary, "Open PR" never merges.
      "releaseChoices(run.policy",
      "Review the diff",
      "Run the check again",
      '"/plan/approve"',
      '"/plan/reject"',
      '"/release"',
      '"/check"',
      "merge_when_green: merge",
      "rb-card rb-ship",
      "rb-card rb-plan",
      "only here: ",
    ]);
    const choices = fn(SPLIT, "releaseChoices");
    expectAll(choices, [
      '"Open PR"',
      '"Open it, merge when checks pass"',
      '"Open PR, merge when checks pass"',
      '"Open PR only"',
      '"Push the branch"',
    ]);
    // The release card is not a "Ship" anything (one name: fast-track).
    expect(panel).toContain("Release · one PR");
    expect(allCode()).not.toContain("Ship · one PR");
    expect(panel).toContain("planned by the lead · started and merged by MindFlock");
    // The Thread tab hands a run lead to the panel and drops the paste buttons.
    const tab = fn("components/grid/ThreadTab.tsx", "ThreadTab");
    expect(has(tab, 'me?.run?.role === "lead" ? me.run : null')).toBe(true);
    // Neither the live workers' paste buttons nor the Finished list (the
    // panel already lists a group's pieces) render for a lead.
    expectAll(tab, ["workers.length > 0 && !leadOf &&", "finished.length > 0 && !leadOf &&"]);
  });

  it("the lead chip asks for your click only", () => {
    const chip = fn(SPLIT, "leadChip");
    expectAll(chip, ['"→ PR?"', '"plan?"', 'run.state === "release_ready"']);
    const row = fn(ROW, "SidebarRow");
    expectAll(row, ["leadChip(leadRun)", "stagechip wrapchip leadchip"]);
    // A run lead never offers the paste-the-wrap-up chip.
    expect(has(row, "kids.length && !pending && !missing && !isLead")).toBe(true);
  });

  it("the split UI has no native dialogs and pastes nothing", () => {
    const bodies: Array<[string, string]> = [
      [LEAD, "RunLeadPanel"],
      [SPLIT, "leadChip"],
      [SPLIT, "leadLine"],
      [SPLIT, "pieceStatus"],
      [SPLIT, "releaseCard"],
      [OUTBOX, "waitingActions"],
      [WAIT, "doWaitAction"],
    ];
    for (const [file, name] of bodies) {
      const body = fn(file, name);
      for (const absent of ["window.prompt", "prompt(", "confirm(", "alert(", "pastes the prompt", "pastePlaybook("]) {
        expect(body, name + ": " + absent).not.toContain(absent);
      }
    }
  });

  it("a push lane is never nagged ready for PR", () => {
    const body = fn("components/sidebar/ordering.ts", "attentionItems");
    expectAll(body, ['laneOf(inst)?.target !== "push"', '"pushed — ready for PR"']);
  });

  it("a merged piece keeps one chip and its name is never overlaid", () => {
    // Real-server defect (a 4-session split, long slugs): the "merged" chip
    // drew ACROSS the piece's name, beside a redundant "✓ checks" and "✓".
    // The status column now holds the title's floor and clips; a merged-back
    // piece and a lead asking for its release drop the quiet chips.
    const row = fn(ROW, "SidebarRow");
    expectAll(row, ['integrated: runTask?.state === "integrated"', "leadAsks: !!lchip", "extra.check &&"]);
    const css = normCode("components/sidebar/SidebarRow.css");
    const i = css.indexOf(".inst .meta.has-lineage {");
    expect(i).toBeGreaterThanOrEqual(0);
    const block = css.slice(i, css.indexOf("}", i));
    expect(block).toContain("min-width: 34px");
    expect(block).toContain("overflow: hidden");
  });

  it("a finished one-for-all group keeps its family lines", () => {
    // Real-server defect: once a one-for-all trio FINISHED the rail fetched
    // only its summary, so its pieces fell back to "→ commit · idle" and its
    // lead to a red "fast-track stopped" over the no-gh hand-off — which is
    // how a release is meant to end without gh. A recent together group keeps
    // its details; the lead says "✓ PR #N" or "⇡ pushed — open the PR".
    expect(has(fn("state/runs.ts", "fetchRuns"), "needsRunDetail(r, nowS)")).toBe(true);
    expect(has(fn("lib/runs.ts", "needsRunDetail"), 'r.policy?.grouping === "together"')).toBe(true);
    const line = fn(SPLIT, "leadLine");
    expectAll(line, ['"⇡ pushed — open the PR"', 'run.release?.state === "handoff"']);
    expect(has(fn(ROW, "SidebarRow"), "RUN_DONE_STATES.has(leadRun.state)")).toBe(true);
  });

  it("a new session with fast-track gets its own worktree", () => {
    // Live L2: two single sessions with a lane were created IN PLACE on main;
    // one approval card committed both sessions' files. With fast-track on
    // the New dialog never sends in_place, and says why on the radio — which
    // now turns fast-track off rather than sitting disabled.
    expect(has(normCode("lib/newSessionSubmit.ts"), "inPlace: inPlace && !laneNeedsWorktree")).toBe(true);
    expect(normCode("components/dialogs/newSession/WorkspaceFold.tsx")).toContain(
      "turns fast-track off (it commits for this session"
    );
    expect(allCode()).not.toContain("not with a lane");
  });

  it("the plan card asks where the pieces run", () => {
    // Split modes: the plan card offers "In separate worktrees (merge back)"
    // (default) and "In this folder (no merge)", one short line each, posts
    // the choice as `{mode}`, and — for a lead on its trunk — offers the
    // explicit "Start a branch here first" (POST /lead/branch) or separate
    // worktrees, inline (Electron has no browser dialogs). The words are the
    // one-name vocabulary: no lane / ship on the card.
    const panel = fn(LEAD, "RunLeadPanel");
    expectAll(panel, [
      "json: { mode }",
      '"/lead/branch"',
      "Start a branch here first",
      "Use separate worktrees",
      "Where the pieces run",
      "rb-modes",
      'type="radio"',
      "threadOpen(lead)",
    ]);
    const modes = fn(SPLIT, "modeChoices");
    expectAll(modes, [
      '"In separate worktrees (merge back) — default"',
      '"In this folder (no merge)"',
      "no per-piece undo.",
      "the pieces would commit onto it",
    ]);
    for (const body of [panel, modes]) {
      for (const absent of ["window.prompt", "prompt(", "confirm(", "alert("]) expect(body, absent).not.toContain(absent);
    }
    for (const absent of ["lane", "Ship", "ship"]) expect(modes, absent).not.toContain(absent);
    expect(normCode("components/grid/ThreadTab.css")).toContain(".rb-mode.on");
  });

  it("a same-folder piece is committed, not merged back", () => {
    const st = fn(SPLIT, "pieceStatus");
    expectAll(st, ['mode === "same_folder"', '"committed ✓"', '"committing…"']);
    expect(has(fn(LEAD, "RunLeadPanel"), "pieceStatus(t, myName, run.mode)")).toBe(true);
  });
});
