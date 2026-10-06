/** Split and one-for-all groups (SPEC §3.4, §7.C.4): the lead's Thread tab
 * sentences, the plan card, the ship card, each piece's status, the rail's
 * `→ PR?` chip and lead line, the Outbox's group-level rows and their ONE
 * horizontal row of buttons, the bell's rows, and the list mode's "Browse…"
 * for the folder tasks start in. */
import { describe, it, expect } from "vitest";
import type { Instance, OutboxWaiting, RunDTO, RunTask } from "../api/types";
import * as S from "../lib/splitRun";
import { shipLine } from "../lib/agentMessages";
import { needsRunDetail, runNote, splitRail, TOGETHER_DETAIL_S } from "../lib/runs";
import { waitingActions, waitingChip } from "../components/outbox/outbox";
import { BROWSE_VALUE, runRepoOptions } from "../lib/runStart";
import dialogSrc from "../components/dialogs/NewSessionDialog.tsx?raw";
import leadPanelSrc from "../components/grid/RunLeadPanel.tsx?raw";
import splitRunSrc from "../lib/splitRun.ts?raw";

const piece = (id: string, state: string, extra: Partial<RunTask> = {}): RunTask => ({
  id,
  kind: "piece",
  text: "do " + id,
  title: "auth-cleanup-" + id,
  state,
  reason: "",
  ...extra,
});

const run = (extra: Partial<RunDTO> = {}): RunDTO => ({
  id: "r_ac",
  name: "Auth cleanup",
  state: "running",
  paused: false,
  pause_reason: "",
  policy: { lane: "pr", ask_first: false, grouping: "together", release: "ask" },
  counts: { queued: 0, active: 3, needs_you: 0, shipped: 0, failed: 0, total: 3 },
  cost_usd: 0,
  created_at: 1,
  split: true,
  lead: { title: "auth-cleanup-lead", branch: "run/auth-cleanup", base_branch: "main" },
  plan: {
    state: "approved",
    pieces: [
      { title: "tokens", prompt: "Rotate refresh tokens on privilege change", paths: ["quickpay/auth/tokens*"] },
      { title: "sessions", prompt: "One session store interface", paths: ["quickpay/auth/session*"] },
      { title: "scopes", prompt: "Scope checks in one decorator", paths: ["quickpay/auth/scopes.py"] },
    ],
  },
  tasks: [piece("tokens", "working"), piece("sessions", "working"), piece("scopes", "working")],
  check: { state: "pending" },
  release: { state: "none" },
  ...extra,
});

const text = (parts: S.SubPart[]) => parts.map((p) => p.text).join("");

describe("the lead's sub-line", () => {
  it("planning, then the plan waiting for you", () => {
    expect(text(S.leadSubline(run({ state: "planning", plan: null, tasks: [] }), "lead"))).toMatch(
      /^Waiting for lead to propose the pieces/
    );
    const pr = S.leadSubline(run({ state: "plan_ready", plan: { state: "proposed", pieces: run().plan!.pieces }, tasks: [] }), "lead");
    expect(text(pr)).toBe("lead proposed 3 pieces with separate paths — approve them and MindFlock starts the workers.");
    expect(pr.find((p) => p.b)?.text).toBe("3 pieces with separate paths");
  });

  it("the mockup's sentence once everything merged back and the check passed", () => {
    const r = run({
      state: "release_ready",
      tasks: [piece("tokens", "integrated"), piece("sessions", "integrated"), piece("scopes", "integrated")],
      check: { state: "ok", tests: 212 },
    });
    expect(text(S.leadSubline(r, "lead"))).toBe(
      "Split into 3 pieces with separate paths · all 3 merged back into run/auth-cleanup · 212 tests pass after the last merge"
    );
  });

  it("names a conflict with the lead, and a failed check in red", () => {
    const r = run({
      tasks: [piece("tokens", "integrated"), piece("sessions", "integrating", { reason: "conflict", conflict: { files: ["a.py"] } }), piece("scopes", "working")],
    });
    const parts = S.leadSubline(r, "lead");
    expect(text(parts)).toContain("1 of 3 merged back into run/auth-cleanup");
    expect(parts.find((p) => p.text.includes("conflict"))?.cls).toBe("needs");
    const f = S.leadSubline(run({ state: "checking", check: { state: "failed" } }), "lead");
    expect(f.find((p) => p.cls === "bad")?.text).toContain("the check failed");
  });

  it("one for all says lines, one PR", () => {
    const r = run({ split: false, plan: null, tasks: [piece("a", "working", { kind: "task" }), piece("b", "working", { kind: "task" })] });
    expect(text(S.leadSubline(r, "lead"))).toMatch(/^2 lines, one PR · merging back into run\/auth-cleanup as each finishes/);
  });
});

describe("the plan card", () => {
  it("rows carry the fence as one code chip; the button counts the workers", () => {
    const rows = S.planRows(run().plan!.pieces);
    expect(rows.map((r) => r.pathsText)).toEqual(["quickpay/auth/tokens*", "quickpay/auth/session*", "quickpay/auth/scopes.py"]);
    expect(S.startWorkersLabel(3)).toBe("Start 3 workers");
    expect(S.startWorkersLabel(1)).toBe("Start 1 worker");
  });

  it("an inline edit replaces only that piece, paths split on commas/spaces", () => {
    const next = S.editPlan(run().plan!.pieces, 1, { title: " store ", prompt: " One store ", paths: "a/b*, c/d.py\n e" });
    expect(next[1]).toEqual({ title: "store", prompt: "One store", paths: ["a/b*", "c/d.py", "e"] });
    expect(next[0]).toEqual(run().plan!.pieces[0]);
  });

  it("a 422's problems read as lines", () => {
    expect(S.planProblems({ problems: [{ piece: "scopes", error: "overlaps sessions on quickpay/auth/session_scopes.py" }] })).toEqual([
      "scopes: overlaps sessions on quickpay/auth/session_scopes.py",
    ]);
    expect(S.planProblems({ error: "this group is not a split" })).toEqual(["this group is not a split"]);
  });
});

describe("the ship card", () => {
  it("title, into, commits (+ the lead's conflict fix) and body", () => {
    const c = S.releaseCard(
      run({
        state: "release_ready",
        release: {
          state: "ready",
          title: "Auth cleanup: rotate refresh tokens, one session store, one scope decorator",
          base: "main",
          branch: "run/auth-cleanup",
          files: 14,
          add: 412,
          del: 188,
          conflict_fixes: 1,
        },
      })
    );
    expect(c.title).toBe("Auth cleanup: rotate refresh tokens, one session store, one scope decorator");
    expect(c.into).toBe("main ← run/auth-cleanup");
    expect(c.stat).toBe("14 files +412 −188");
    expect(c.commits).toBe("one per piece, kept as written + 1 conflict fix by the lead");
    expect(c.body).toBe("a section per piece: what changed, the tests it ran");
  });

  it("the fast-track note at the right", () => {
    expect(S.laneNote({ target: "pr", ask_first: true })).toBe("fast-track: → PR, asks first");
    expect(S.laneNote({ target: "leave", ask_first: false })).toBe("fast-track: off");
    expect(S.laneNote(null)).toBe("");
  });

  it("after the click: opening, the PR, the hand-off link, nothing pushed", () => {
    expect(S.releaseOutcome(run({ state: "releasing" }))!.text).toBe("Opening the PR…");
    const done = S.releaseOutcome(run({ state: "done", release: { state: "done", pr_url: "https://x/pull/318" } }))!;
    expect(done.text).toBe("✓ PR #318 opened");
    expect(done.url).toBe("https://x/pull/318");
    const ho = S.releaseOutcome(run({ state: "done", release: { state: "handoff", compare_url: "https://x/compare/main...b" } }))!;
    expect(ho.link).toBe("Open the compare page ↗");
    const local = S.releaseOutcome(run({ state: "done", policy: { lane: "commit", ask_first: false, grouping: "together" } }))!;
    expect(local.text).toMatch(/nothing pushed \(this group is fast-tracked to Commit\)/);
  });

  it("a lead whose origin is a folder on this machine: push only, and the outcome never claims a PR", () => {
    const choices = S.releaseChoices("pr", "/home/u/app");
    expect(choices.map((c) => [c.label, c.merge])).toEqual([["Push to the local folder", false]]);
    expect(choices[0].title).toMatch(/\/home\/u\/app — a folder on this machine, not GitHub: no PR can be opened/);
    expect(S.releaseChoices("pr").map((c) => c.label)).toEqual(["Open the PR", "Open it, merge when checks pass"]);
    expect(S.releaseOutcome(run({ state: "releasing", release: { state: "releasing", local_origin: "/home/u/app" } }))!.text).toBe(
      "Pushing the branch to /home/u/app…"
    );
    const ho = S.releaseOutcome(
      run({ state: "done", release: { state: "handoff", local_origin: "/home/u/app", branch: "feature/sc-1/x", title: "T" } })
    )!;
    expect(ho.text).toMatch(/^Pushed feature\/sc-1\/x to \/home\/u\/app — a folder on this machine, not GitHub, so no PR was opened/);
    expect(ho.text).not.toMatch(/no gh or token/);
    expect(ho.url).toBe("");
  });
});

describe("a piece's status", () => {
  it("merged back with its test count, merging, conflict with the lead, needs you", () => {
    expect(S.pieceStatus(piece("t", "integrated", { tests: "pytest tests/auth -q — 24 passed" }), "lead")).toEqual({
      word: "merged back ✓",
      cls: "ok",
      detail: "24 tests",
    });
    expect(S.pieceStatus(piece("t", "integrating"), "lead").word).toBe("merging…");
    const c = S.pieceStatus(piece("t", "integrating", { reason: "conflict", conflict: { files: ["a.py", "b.py"] } }), "lead");
    expect(c).toEqual({ word: "conflict", cls: "needs", detail: "lead is resolving a.py, b.py" });
    expect(S.pieceStatus(piece("t", "needs_you", { reason: "conflict", detail: "conflict in a red-zone file" }), "lead").word).toBe(
      "conflict — needs you"
    );
  });

  it("names the piece from the plan: 'tokens — Rotate refresh tokens…'", () => {
    const r = run();
    expect(S.pieceLabel(r.tasks[0], r)).toEqual({ name: "tokens", what: "Rotate refresh tokens on privilege change" });
    const t = piece("x", "working", { kind: "task", title: "per-user-rate-limit", text: "Per-user rate limit on /webhooks" });
    expect(S.pieceLabel(t, { tasks: [t], plan: null })).toEqual({ name: "per-user-rate-limit", what: "Per-user rate limit on /webhooks" });
  });

  it("test counts from a report's Tests: line", () => {
    expect(S.testsLabel("Tests: 31 passed, 2 skipped")).toBe("31 tests");
    expect(S.testsLabel("ran 1 test")).toBe("1 test");
    expect(S.testsLabel("not run")).toBe("");
    expect(S.testsLabel(212)).toBe("212 tests");
  });
});

describe("the rail", () => {
  it("the lead's chip asks for YOUR click only: plan?, → PR?", () => {
    expect(S.leadChip(run({ state: "release_ready" }))!.label).toBe("→ PR?");
    expect(S.leadChip(run({ state: "release_ready", policy: { lane: "merge", ask_first: false, grouping: "together" } }))!.label).toBe("→ merge?");
    expect(S.leadChip(run({ state: "plan_ready" }))!.label).toBe("plan?");
    expect(S.leadChip(run())).toBeNull();
    expect(S.leadChip(null)).toBeNull();
  });

  it("a merged-back piece and a lead asking for its release keep ONE chip", () => {
    const ok = { cls: "s-committed" };
    const shield = { cls: "rz-ok" };
    expect(S.railExtraChips(ok, shield, { integrated: true, leadAsks: false })).toEqual({ check: null, rz: null });
    expect(S.railExtraChips(ok, null, { integrated: false, leadAsks: true })).toEqual({ check: null, rz: null });
    // Anything that still needs you survives: a failed check, a breach, a guard not holding.
    const failed = { cls: "s-interrupt" };
    const breach = { cls: "rz-breach" };
    expect(S.railExtraChips(failed, breach, { integrated: true, leadAsks: false })).toEqual({ check: failed, rz: breach });
    expect(S.railExtraChips(ok, { cls: "rz-warn" }, { integrated: false, leadAsks: true }).rz).toEqual({ cls: "rz-warn" });
    // Any other row: untouched.
    expect(S.railExtraChips(ok, shield, { integrated: false, leadAsks: false })).toEqual({ check: ok, rz: shield });
  });

  it("a FINISHED one-for-all group keeps its details on the rail for a week", () => {
    const now = 1_000_000_000;
    const together = { state: "done", policy: { lane: "pr", ask_first: false, grouping: "together" }, created_at: now - 3600 };
    expect(needsRunDetail(together, now)).toBe(true);
    expect(needsRunDetail({ ...together, created_at: now - TOGETHER_DETAIL_S - 1 }, now)).toBe(false);
    expect(needsRunDetail({ ...together, policy: { ...together.policy, grouping: "each" } }, now)).toBe(false);
    expect(needsRunDetail({ ...together, state: "running", policy: { ...together.policy, grouping: "each" } }, now)).toBe(true);
  });

  it("a finished lead says how it ended: ✓ PR #N, or pushed — open the PR (never a red halt)", () => {
    const done = S.leadLine(run({ state: "done", release: { state: "done", pr_url: "https://x/pull/42" } }))!;
    expect(done).toEqual({ text: "✓ PR #42", cls: "ok", url: "https://x/pull/42" });
    const ho = S.leadLine(run({ state: "done", tasks: [piece("a", "integrated")], release: { state: "handoff", compare_url: "https://x/compare/main...b" } }))!;
    expect(ho).toEqual({ text: "⇡ pushed — open the PR", cls: "", url: "https://x/compare/main...b" });
    expect(S.leadLine(run({ state: "done", release: { state: "handoff" } }))!.url).toBeUndefined();
  });

  it("the lead's line says how the group is going", () => {
    const all = run({ tasks: [piece("a", "integrated"), piece("b", "integrated"), piece("c", "integrated")] });
    expect(S.leadLine(all)).toEqual({ text: "3 of 3 merged back", cls: "ok" });
    expect(S.leadLine(run({ state: "planning", tasks: [] }))!.text).toBe("proposing the pieces…");
    expect(S.leadLine(run({ state: "plan_ready", tasks: [] }))).toEqual({ text: "plan ready — approve it", cls: "needs" });
    expect(S.leadLine(run({ state: "done", release: { state: "done", pr_url: "https://x/pull/9" } }))!.text).toBe("✓ PR #9");
  });

  it("a piece's ship line: merged back beats its commit lane; merging; conflict", () => {
    const row = { title: "p", lane: { target: "commit", ask_first: false }, run: { id: "r", name: "", task: "t", role: "piece", grouping: "together" } } as unknown as Instance;
    expect(shipLine(row, { act: "idle", task: { state: "integrated" } })!.lead).toBe("✓ merged back");
    expect(shipLine(row, { act: "idle", task: { state: "integrating" } })!.lead).toBe("⇄ merging");
    const c = shipLine(row, { act: "idle", task: { state: "integrating", reason: "conflict" } })!;
    expect(c.lead + c.rest).toBe("! conflict — open the Thread");
  });

  it("a split / one-for-all group is a family, never a header", () => {
    const lead = { key: "L", inst: { title: "L", run: { id: "r_ac", name: "Auth", task: "", role: "lead", grouping: "together" } } as Instance };
    const p1 = { key: "p1", inst: { title: "p1", parent: "L", run: { id: "r_ac", name: "Auth", task: "t1", role: "piece", grouping: "together" } } as Instance };
    const p2 = { key: "p2", inst: { title: "p2", parent: "L", run: { id: "r_ac", name: "Auth", task: "t2", role: "piece", grouping: "together" } } as Instance };
    const solo = { key: "solo", inst: { title: "solo" } as Instance };
    const out = splitRail([lead, solo, p1, p2], [run()]);
    expect(out.groups).toEqual([]);
    // A family is a group of its own: never filed under "On their own" (live L7).
    expect(out.families.map((e) => e.key)).toEqual(["L", "p1", "p2"]);
    expect(out.own.map((e) => e.key)).toEqual(["solo"]);
  });
});

describe("the Outbox's group-level rows", () => {
  const w = (kind: string, actions: string[], extra: Partial<OutboxWaiting> = {}): OutboxWaiting => ({ title: "auth-cleanup-lead", kind, actions, ...extra });
  const can = { row: true, run: true, task: true };

  it("an escalation's buttons sit in ONE row, primary first: Retry · Open ↗ · Skip", () => {
    const acts = waitingActions(w("ship_halted", ["retry", "open", "skip"]), can);
    expect(acts.map((a) => a.label)).toEqual(["Retry", "Open ↗", "Skip"]);
    expect(acts.map((a) => a.primary)).toEqual([true, false, false]);
    expect(waitingActions(w("restart", ["retry", "retry_fresh", "skip"]), can).map((a) => a.key)).toEqual([
      "retry",
      "retry_fresh",
      "open",
      "skip",
    ]);
  });

  it("plan: Start N workers + the Thread; release: Open the PR; check_failed: run it again", () => {
    const plan = w("plan", ["approve", "open"], { preview: { pieces: [{ title: "a", paths: ["x"] }, { title: "b", paths: ["y"] }] } });
    expect(waitingActions(plan, can).map((a) => a.label)).toEqual(["Start 2 workers", "Open the Thread ↗"]);
    expect(waitingActions(w("release", ["release", "open"], { preview: { lane: "pr" } }), can).map((a) => a.label)).toEqual([
      "Open the PR",
      "Open it, merge when checks pass",
      "Open the Thread ↗",
    ]);
    expect(waitingActions(w("check_failed", ["open", "retry_check"]), can).map((a) => a.label)).toEqual([
      "Run the check again",
      "Open the Thread ↗",
    ]);
    // Retry puts a conflicted piece back in the merge queue.
    expect(waitingActions(w("conflict", ["retry", "open", "skip"]), can).map((a) => a.label)).toEqual([
      "Retry",
      "Open the Thread ↗",
      "Skip",
    ]);
  });

  it("release buttons follow the group's lane — 'Open the PR' never merges", () => {
    const keys = (lane: string) =>
      waitingActions(w("release", ["release", "open"], { preview: { lane } }), can).map((a) => [a.key, a.label, a.primary]);
    // A merge group: the primary merges (its lane); "Open the PR only" does not.
    expect(keys("merge")).toEqual([
      ["release_merge", "Open the PR, merge when checks pass", true],
      ["release", "Open the PR only", false],
      ["open", "Open the Thread ↗", false],
    ]);
    // A push group pushes its branch and opens nothing.
    expect(keys("push")).toEqual([
      ["release", "Push the branch", true],
      ["open", "Open the Thread ↗", false],
    ]);
  });

  it("a group whose lead is gone offers to cancel it (sessions and branches kept)", () => {
    const acts = waitingActions(w("lead_gone", ["cancel_group"]), { row: false, run: true, task: false });
    expect(acts.map((a) => [a.key, a.label])).toEqual([["cancel_group", "Cancel the group"]]);
  });

  it("only what the row can address: no run → no run buttons", () => {
    expect(waitingActions(w("release", ["release", "open"]), { row: true, run: false, task: false }).map((a) => a.key)).toEqual(["open"]);
  });

  it("the chips: plan in gold, release neutral, a failed check in red", () => {
    expect(waitingChip(w("plan", [])).cls).toBe("warn");
    expect(waitingChip(w("release", [])).text).toBe("one PR is ready to open");
    expect(waitingChip(w("check_failed", [])).cls).toBe("bad");
  });
});

describe("the bell", () => {
  it("plan and release rows open the lead's Thread; a failed check opens the Outbox", () => {
    const plan = runNote("run.needs_you", { run: "r_ac", name: "Auth cleanup", title: "auth-cleanup-lead", reason: "plan", round: 1 })!;
    expect(plan.text).toBe("Auth cleanup: the lead proposed the pieces — approve them");
    expect(plan.lead).toBe("auth-cleanup-lead");
    expect(plan.rule).toBe("run_needs_you");
    const rel = runNote("run.needs_you", { run: "r_ac", name: "Auth cleanup", title: "auth-cleanup-lead", reason: "release", text: "one PR is ready to open" })!;
    expect(rel.text).toBe("Auth cleanup: one PR is ready to open");
    expect(rel.lead).toBe("auth-cleanup-lead");
    const chk = runNote("run.needs_you", { run: "r_ac", name: "Auth cleanup", title: "auth-cleanup-lead", reason: "check_failed" })!;
    expect(chk.lead).toBeUndefined();
    expect(chk.run).toBe("r_ac");
  });
});

describe("list mode: tasks start in ANY folder", () => {
  it("the picker ends with Browse…, and a browsed-to folder shows as itself", () => {
    const sug = [{ path: "/r/a", name: "a" }, { path: "/r/b" }];
    expect(runRepoOptions("/r/a", sug)).toEqual([
      { value: "/r/a", label: "a" },
      { value: "/r/b", label: "b" },
      { value: BROWSE_VALUE, label: "Browse…" },
    ]);
    expect(runRepoOptions("/home/u/elsewhere/proj", sug)[0]).toEqual({ value: "/home/u/elsewhere/proj", label: "proj" });
  });

  it("Browse… opens the inline folder browser, never a prompt", () => {
    const src = dialogSrc;
    expect(src).toContain("BROWSE_VALUE");
    const at = src.indexOf("{runMode && runBrowse && (");
    expect(at).toBeGreaterThan(0);
    const block = src.slice(at, src.indexOf("{!runMode && (", at));
    expect(block).toContain("<FolderBrowser");
    expect(block).not.toMatch(/window\.prompt|\bprompt\(|confirm\(/);
  });
});

describe("new components ask nothing through a browser dialog and paste nothing", () => {
  for (const [f, src] of [
    ["RunLeadPanel.tsx", leadPanelSrc],
    ["splitRun.ts", splitRunSrc],
  ] as const) {
    it(f, () => {
      for (const bad of ["window.prompt", "confirm(", "pastes the prompt", "pastePlaybook("]) expect(src).not.toContain(bad);
    });
  }
});

describe("the ship card's check row (live L6)", () => {
  it("always says what was checked — including that nothing was", () => {
    expect(S.checkLine({ check: { state: "none" } } as never)).toMatch(/no check configured/);
    expect(S.checkLine({ check: { state: "ok", command: "pytest", tests: 7 } } as never)).toBe("`pytest` passed (7 tests)");
  });
});

describe("where the pieces run (the plan card's mode choice)", () => {
  it("offers separate worktrees first, then this folder, one short line each", () => {
    const c = S.modeChoices(run({ state: "plan_ready" }), "auth-lead");
    expect(c.map((x) => x.mode)).toEqual(["worktrees", "same_folder"]);
    expect(c[0].label).toBe("In separate worktrees (merge back) — default");
    expect(c[1].label).toBe("In this folder (no merge)");
    expect(c[0].hint).toBe("Each piece in its own worktree, merged back into run/auth-cleanup; conflicts go to auth-lead.");
    expect(c[1].hint).toMatch(/runs in auth-lead's folder; MindFlock commits each piece's paths .* no per-piece undo\.$/);
    expect(c.every((x) => x.blocked === "")).toBe(true);
    // One vocabulary: no "lane" / "ship" words on the card.
    for (const x of c) expect(x.label + x.hint).not.toMatch(/\blane|\bship/i);
  });

  it("an in-place or trunk lead: worktrees get a new lead, this folder needs a branch first", () => {
    const inPlace = S.modeChoices(run({ lead: { title: "mine", branch: "feat", in_place: true } }), "mine");
    expect(inPlace[0].hint).toBe(
      "MindFlock starts mine-split from mine's last commit and merges the pieces there — mine itself is left as it is.",
    );
    expect(inPlace[1].blocked).toBe("");
    const trunk = S.modeChoices(run({ lead: { title: "mine", branch: "main", in_place: true, trunk: true } }), "mine");
    expect(trunk[1].blocked).toBe("mine is on main — the pieces would commit onto it");
    expect(trunk[0].blocked).toBe("");
  });

  it("a same-folder group says committed, never merged back", () => {
    const sf = run({ mode: "same_folder", tasks: [piece("a", "integrated"), piece("b", "integrating"), piece("c", "working")] });
    expect(S.pieceStatus(sf.tasks[0], "lead", sf.mode).word).toBe("committed ✓");
    expect(S.pieceStatus(sf.tasks[1], "lead", sf.mode).word).toBe("committing…");
    expect(S.pieceStatus(sf.tasks[0], "lead").word).toBe("merged back ✓");
    expect(S.leadLine(sf)).toEqual({ text: "1 of 3 committed", cls: "" });
    expect(text(S.leadSubline(sf, "lead"))).toBe(
      "Split into 3 pieces with separate paths in lead's folder · 1 of 3 committed on run/auth-cleanup",
    );
    expect(S.releaseCard(sf).commits).toBe("one per piece, committed by MindFlock with only its paths");
    expect(S.checkLine({ check: { state: "pending" }, mode: "same_folder" } as never)).toBe(
      "runs once every piece is committed",
    );
    expect(S.startedText(2, "same_folder", "lead")).toBe("Starting 2 workers in lead's folder — each fenced to its paths");
  });

  it("the plan card posts the mode, and offers the branch action inline", () => {
    expect(leadPanelSrc).toContain('json: { mode }');
    expect(leadPanelSrc).toContain('"/lead/branch"');
    expect(leadPanelSrc).toContain("Start a branch here first");
    expect(leadPanelSrc).toContain("Use separate worktrees");
    expect(leadPanelSrc).not.toMatch(/window\.(prompt|confirm|alert)|\balert\(/);
  });

  it("the Outbox's stray row opens the lead's Thread", () => {
    const w: OutboxWaiting = { title: "lead", kind: "stray", reason: "changes no piece owns", actions: ["open"] };
    expect(waitingChip(w)).toEqual({ text: "changes no piece owns", cls: "warn" });
    expect(waitingActions(w, { row: true, run: true, task: false }).map((a) => a.key)).toEqual(["open"]);
  });
});

describe("a same-folder piece on the rail", () => {
  it("reads committed / committing, never merged back", () => {
    const row = { title: "q-tokens", run: { id: "r", name: "x", task: "t1", role: "piece", grouping: "together" } } as unknown as Instance;
    expect(shipLine(row, { act: "idle", task: { state: "integrated", sameFolder: true } })!.lead).toBe("✓ committed");
    expect(shipLine(row, { act: "idle", task: { state: "integrating", sameFolder: true } })!.lead).toBe("⇡ committing");
    expect(shipLine(row, { act: "idle", task: { state: "integrated" } })!.lead).toBe("✓ merged back");
  });
});
