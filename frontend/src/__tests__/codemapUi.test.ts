/** Code map (the Code Tree) + zones frontend wiring: structural pins, moved
 * from tests/unit/test_frontend_codemap.py (which grepped the built bundle).
 *
 * The Map tab (components/grid/CodeMapTab.tsx and its codemap/ parts), the
 * tree (lib/codetree/*: layout search in a Web Worker, canvas renderer, live
 * birds, zones on the tree), its pure math (lib/codemap.ts), its fetchers
 * (lib/codemapApi.ts), the rail chip and the Zones dialog are verified
 * headlessly with screenshots; these pin the markup hooks, routes and guard
 * rails in the SOURCE module that owns each one — and the one property a
 * screenshot can't: nothing on the zone path asks through
 * `window.prompt`/`confirm`/`alert`, which the Electron app never implements
 * (a dead prompt fails silently: the button just does nothing).
 *
 * Still in Python (they quote strings a MindFlock guard hook refuses in any
 * agent edit): the fetchers' route list, the push/PR/merge override re-post,
 * and the Zones dialog's API routes. The committed layout worker file is a
 * static-server contract and stays there too. */

import { describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/client";
import { css, dropComments, src } from "./sourceText";

const errorPop = vi.fn();
vi.mock("../lib/errorPop", async (orig) => ({
  ...((await orig()) as object),
  errorPop: (...a: unknown[]) => errorPop(...a),
}));

const { offerRedZoneOverride, redZoneBreaches } = await import("../lib/sessionActions");
const { SESSION_ACTIONS } = await import("../lib/sessionActionTable");
const { redZoneChip } = await import("../lib/codemapSeen");
const { WARM_RUN_MAX } = await import("../lib/codetree/engine");

/** A top-level function's text, header to the closing brace at column 0. */
function fnText(s: string, header: string): string {
  const at = s.indexOf(header);
  expect(at, header).toBeGreaterThanOrEqual(0);
  const end = s.indexOf("\n}\n", at);
  expect(end, header).toBeGreaterThan(at);
  return s.slice(at, end + 2);
}

const CODEMAP_MODULES = [
  "lib/codemap.ts",
  "lib/codemapApi.ts",
  "lib/codemapSeen.ts",
  "components/grid/CodeMapTab.tsx",
  "components/grid/codemap/CodeTree.tsx",
  "components/grid/codemap/TreeCards.tsx",
  "components/grid/codemap/BirdIndex.tsx",
  "components/grid/codemap/treeCtl.ts",
  "components/grid/codemap/SearchBox.tsx",
  "components/grid/codemap/SessionPanel.tsx",
  "components/grid/codemap/AddZoneRow.tsx",
  "components/dialogs/RedZonesDialog.tsx",
  "lib/codetree/layout.ts",
  "lib/codetree/model.ts",
  "lib/codetree/input.ts",
  "lib/codetree/draw.ts",
  "lib/codetree/palette.ts",
  "lib/codetree/live.ts",
  "lib/codetree/zones.ts",
  "lib/codetree/blast.ts",
  "lib/codetree/engine.ts",
  "lib/codetree/cache.ts",
];

/** Calls that are silent no-ops in Electron. Not preceded by an identifier
 * character, `.` or `$` (so `sendMessagePrompt(` doesn't count), with an
 * optional `window.` because both spellings reach the same dead API. */
const DEAD_DIALOG = /(?<![\w.$])(?:window\.)?(?:prompt|confirm|alert)\(/g;

const TAB = () => src("components/grid/CodeMapTab.tsx");

describe("the Map pane tab", () => {
  it("is a first-class pane tab, mounted like Diff and git-gated exactly like it", () => {
    const pane = src("components/grid/Pane.tsx");
    expect(pane).toContain('data-tab="map"');
    expect(pane).toContain('<div className={"pane-map" + (tab !== "map" ? " hidden" : "")}>');
    expect(pane).toContain('title={"Code map — the worktree as a tree: agents, zones, plan, blast radius"');
    // Mounted like DiffTab: {title, active}, active only while the tab shows.
    expect(pane).toContain('<CodeMapTab title={title} active={tab === "map"} />');
    // One function (paneTab) over one set drives BOTH the saved-tab and the
    // programmatic-switch fallbacks, so they can't disagree.
    expect(pane).toContain('const GIT_TABS: ReadonlySet<string> = new Set(["diff", "map"]);');
    expect(fnText(pane, "function paneTab(")).toContain("if (GIT_TABS.has(t) && !git)");
    expect(pane).toContain("useState<Tab>(paneTab(savedTab, caps.git))");
    expect(pane).toContain("const t = paneTab(lastTab, caps.git)");
  });

  it("polls only while active", () => {
    const tab = TAB();
    expect(tab).toContain("document.hidden");
    expect(tab).toContain('apiAskPlan(title, midFlight ? "remaining" : "plan")');
    // Go carries the zones added since the plan.
    expect(tab).toContain("goZones.map((z) => z.id)");
    // The add route gets the kind; green is always worktree scope.
    expect(tab).toContain('scope: kind === "green" ? "worktree" : add.scope');
    expect(tab).toContain("tell_agent:");
  });

  it("explains its empty states and green mode in its own words", () => {
    const m = CODEMAP_MODULES.filter((p) => !p.includes("RedZonesDialog"))
      .map(src)
      .join("\n");
    for (const s of [
      "Agent hasn't used any tools since MindFlock armed the map.",
      "No changes yet.",
      "Detect-only",
      "Go — with ",
      "Go — only the planned files",
      "Ask what's left",
      "Ask for plan",
      "answer the prompt in the terminal first",
      // the tree's own words (the prototype's legend and cards)
      "Growing the tree…",
      "leaf = file · branch = folder",
      "root tip = a test file, under the code it tests",
      "scaffold = docs, CI and config: a lift per folder, a plank per file",
      "bird = an agent · small bird = its helper · nest = where it mostly works",
      // subagents are birds of their own; a card click follows its bird
      "sent out a helper",
      "Click to follow this bird",
      "in the nest — ",
      "what could break: hover or click a bright leaf — the files that import it light up gold",
      "red area = keep out (agents may read, never edit)",
      "lit branch = the path from the trunk to a change",
      "could break",
      "⛔ Keep out",
      "✓ Only here",
      "⛔ Keep agents out",
      "Allow this file",
      "peeked outside scope",
      "Scope requests",
      "Keep exempt",
      "Treat as breaches",
      "Whole tree",
      "Nothing here — scroll out, or press ⌂ Whole tree",
      "Paint a branch, not the trunk",
      "Undo",
      // the bird index's CHANGES: what could break is on demand, blocking is one click per changed folder
      "⚠ Could break",
      "What could break: show on the map which folders import the changed files, and rank the changed files",
      // only-here binds this worktree: this session's earlier changes are exempt, another session's are untouched
      "already changed · exempt from ✓",
      "other worktree · not affected",
      "⛔ kept out · agents may only read",
      "Collapse to the rail",
    ]) {
      expect(m, s).toContain(s);
    }
  });

  it("keeps the lists reachable when the first reads fail — the note shows only with NOTHING to show", () => {
    const tab = TAB();
    expect(tab).toContain("const failed = !!err && !snap && !live && !model");
    expect(tab).toContain('"cm-note cm-note-err"');
  });

  it("takes the breach set from the server's current set, never feed history", () => {
    // A feed record's `breach` outlives the revert; only `live.breaches` says
    // what is breached now.
    const tab = TAB();
    expect(tab).toContain("const breachList = live?.breaches || EMPTY_BREACHES");
    expect(tab).toContain("currentBreaches(breachList)");
    expect(tab).not.toContain("feedBreachKey");
    expect(tab).not.toContain("f.breach)");
  });

  it("keeps its client-contract fixes", () => {
    const tab = TAB();
    // Running needs the session to be working (a refused call leaves a pre).
    expect(tab).toContain("feedState(feed, now, activity)");
    // Case-insensitive filesystems: the map classifies like the guard does.
    expect(tab).toContain("zoneDoc(zones, live?.companions, live?.companion_files, ci)");
    expect(tab).toContain('new RegExp(z.re, ci ? "i" : "")');
    expect(tab).toContain("treeZones(model, zones, ci)");
    // ONE predicate for every zone decision on the map.
    expect(tab).toContain("classifyPath(p, zdoc)");
    // "Seen" is stamped on the server's clock, never the browser's.
    expect(tab).toContain("markCodemapSeen(title, serverNow(");
    expect(tab).not.toContain("markCodemapSeen(title)");
    // A partial import graph keeps being re-asked until it completes.
    expect(tab).toContain("PARTIAL_REFRESH_MS");
    expect(tab).toContain("snapStale(");
    // A refused push is its own badge.
    expect(src("components/grid/codemap/SessionPanel.tsx")).toContain('"blocked push"');
    // The pill label follows state; the tooltip is the server's sentence.
    expect(tab).toContain("guardPill(live?.guard");
    // Tree-painted paths are anchored ("/name") so a root leaf means that path.
    const zones = src("lib/codetree/zones.ts");
    expect(zones).toContain("anchoredZonePath(n.path)");
    expect(zones).toContain("anchoredZonePath(target.file.path)");
    // Painting is one POST to the server; the tree draws the server's answer.
    expect(tab).toContain("patternFor(M, { node, file })");
    expect(tab).toContain('scope: kind === "green" ? "worktree" : "repo"');
    expect(src("components/grid/codemap/SessionPanel.tsx")).toContain('a.openAdd("red", anchoredZonePath(b.path))');
  });
});

describe("the tree", () => {
  it("is a labelled, focusable canvas with a text twin, painted from the palette", () => {
    const tree = src("components/grid/codemap/CodeTree.tsx");
    expect(tree).toContain('className="ct-canvas"');
    expect(tree).toContain('role="application"');
    expect(tree).toContain("tabIndex={0}");
    expect(tree).toContain('className="cm-sr"');
    // The renderer takes every colour from the palette (app tokens + --ct-*).
    const pal = src("lib/codetree/palette.ts");
    for (const tok of ['"--bg"', '"--accent"', '"--red"', '"--green"', '"--gold"', '"--ct-bark"', '"--ct-sky-top"']) {
      expect(pal, tok).toContain(tok);
    }
    expect(dropComments(src("lib/codetree/draw.ts"))).not.toMatch(/fillStyle = ["'`]#[0-9a-fA-F]{3,6}["'`]/);
    // A theme / accent / surface flip repaints in the new palette.
    const ctl = src("components/grid/codemap/treeCtl.ts");
    expect(ctl).toContain("new MutationObserver(");
    expect(ctl).toContain("attributeFilter:");
    expect(ctl).toContain('"data-accent"');
    expect(ctl).toContain('"data-surface"');
    // Paint tools disarm after one use (Shift keeps them armed).
    expect(ctl).toContain('if (!shift) this.setTool("explore")');
    // DPR is capped at 2.
    expect(ctl).toContain("Math.min(2, window.devicePixelRatio || 1)");
  });

  it("runs its layout search off the UI thread, cached per repo and warm-started", () => {
    // ~43 s on a 4.8k-file repo in the prototype: a module Web Worker (built to
    // a stable file next to app.js — that file is pinned in Python), cached in
    // IndexedDB, warm-started from the previous layout.
    const eng = src("lib/codetree/engine.ts");
    expect(eng).toContain('new Worker(new URL("./layout.worker.ts", import.meta.url), { type: "module"');
    expect(eng).toContain("loadRecords(repo)");
    expect(eng).toContain("saveRecord(repo, sig, rec)");
    expect(eng).toContain("r.rec.warmRun < WARM_RUN_MAX");
    expect(WARM_RUN_MAX).toBeGreaterThan(0);
    const worker = src("lib/codetree/layout.worker.ts");
    expect(worker).toContain("self.onmessage");
    expect(worker).toContain("buildModel");
    const cache = src("lib/codetree/cache.ts");
    expect(cache).toContain('"mindflock-codetree"');
    expect(cache).toContain("indexedDB.open(");
  });
});

describe("no dead dialogs on the zone path", () => {
  it("never calls window.prompt / confirm / alert in a Map or zones module", () => {
    for (const mod of CODEMAP_MODULES) {
      expect(dropComments(src(mod)).match(DEAD_DIALOG), mod + " calls a dialog that is a no-op in Electron").toBeNull();
    }
  });

  it("offers the push / PR / merge override as an inline card with two ways out", () => {
    const override = fnText(src("lib/sessionActions.ts"), "export function offerRedZoneOverride(");
    expect(dropComments(override).match(DEAD_DIALOG)).toBeNull();
    const retry = vi.fn();
    errorPop.mockReset();
    offerRedZoneOverride("s", "Push", [{ path: "secrets/a.key", pattern: "secrets/" } as never], retry);
    expect(errorPop).toHaveBeenCalledTimes(1);
    const actions = errorPop.mock.calls[0][2] as Array<{ label: string; primary?: boolean; run: () => void }>;
    expect(actions.map((a) => a.label)).toEqual(["Open map", "Push anyway"]);
    expect(actions[1].primary).toBe(true);
    actions[1].run();
    expect(retry).toHaveBeenCalledTimes(1);
    // errorPop grew buttons for it.
    const pop = src("lib/errorPop.ts");
    expect(pop).toContain('row.className = "cs-error-actions"');
    expect(pop).toContain('b.className = "cs-error-act" + (a.primary ? " primary" : "")');
  });

  it("recognizes a 409 carrying red_zone_breaches, and nothing else", () => {
    const hit = { path: "a", pattern: "a" };
    expect(redZoneBreaches(new ApiError(409, "x", { red_zone_breaches: [hit] }))).toEqual([hit]);
    expect(redZoneBreaches(new ApiError(400, "x", { red_zone_breaches: [hit] }))).toBeNull();
    expect(redZoneBreaches(new ApiError(409, "x", { error: "diverged" }))).toBeNull();
    expect(redZoneBreaches(new Error("x"))).toBeNull();
  });
});

describe("entry points", () => {
  it("binds Ctrl+K M and the palette to the Map tab, and the palette opens Zones…", () => {
    // The table's "map" row, which the chord and the palette both read.
    const row = SESSION_ACTIONS.map;
    expect(row.label).toBe("Code map");
    expect(row.keys).toBe("Ctrl+K M");
    expect(row.chord).toBe("m");
    const table = src("lib/sessionActionTable.ts");
    const block = table.slice(table.indexOf("  map: {"));
    expect(block.slice(0, block.indexOf("\n  },"))).toContain("useUi.getState().openMap(t)");
    const palette = src("components/palette/CommandPalette.tsx");
    expect(palette).toContain('  "map",');
    expect(palette).toContain('label: "Zones…"');
    expect(palette).toContain('ui.openDialogFor("red-zones")');
  });

  it("marks the rail chip by state and opens the Map from it", () => {
    expect(src("lib/codemapSeen.ts")).toContain('const KEY = "mf_codemap_seen"');
    const base = { zones: 1, breaches: 0, last_block_ts: null, guard: "guarded" };
    expect(redZoneChip({ ...base, breaches: 2 }, 0)?.cls).toBe("rz-breach");
    expect(redZoneChip({ ...base, last_block_ts: 10 }, 5)?.cls).toBe("rz-breach");
    expect(redZoneChip({ ...base, guard: "off" }, 0)?.cls).toBe("rz-warn");
    expect(redZoneChip(base, 0)?.cls).toBe("rz-ok");
    const row = src("components/sidebar/SidebarRow.tsx");
    expect(row).toContain('className={"stagechip rzchip " + rz.cls}');
    // Row field from the server snapshot.
    expect(row).toContain("redZoneChip(inst.redzone, mapSeen)");
    // Clicking the chip opens the Map, on the part the chip is about.
    expect(row).toContain("useUi.getState().openMap(title, zoneChipFocus(inst.redzone, rz))");
  });

  it("routes zone events to both the bell and a toast", () => {
    const bell = src("components/NotificationsBell.tsx");
    const toasts = src("components/EventToasts.tsx");
    for (const ev of ["session.red_zone_blocked", "session.red_zone_breached", "session.red_zone_tampered"]) {
      expect(bell, ev).toContain(`case "${ev}":`);
      expect(toasts, ev).toContain(`"${ev}"`);
    }
    // The three cases join the existing warns.
    expect(bell.split('cls: "n-warn"').length - 1).toBeGreaterThanOrEqual(5);
    expect(toasts).toContain("isReplay(env)");
  });
});

describe("the Zones dialog and Plan first", () => {
  it("is a modal: the end shortcuts stand down and Escape closes it with focus anywhere", () => {
    // Delete on a zone's × button, or Ctrl+W in its pattern input, must not
    // end the session behind the dialog.
    const km = src("lib/keymap.ts");
    expect(km).toContain("export function overlayOpen()");
    expect(km).toContain("OVERLAY_SELECTOR");
    expect(src("components/dialogs/RedZonesDialog.tsx")).toContain('<Modal id="red-zones-dialog" onClose={closeDialog}>');
    // The shared Modal shell owns Escape: a window listener, whatever is focused.
    expect(src("components/Modal.tsx")).toContain('window.addEventListener("keydown", onKey, escapeCapture)');
  });

  it("disables Plan first on a CLI without plan support, and never sends it", () => {
    // It would leave the agent waiting on a Go the Map can't show.
    expect(src("components/dialogs/newSession/PromptFold.tsx")).toContain("(needs an agent with plan support — Claude)");
    expect(src("lib/newSessionSubmit.ts")).toContain("if (planFirst && planOk) body.plan_first = true");
    expect(src("components/dialogs/NewSessionDialog.tsx")).toContain("prov.plan_supported !== false");
  });
});

describe("the Map's stylesheet", () => {
  it("ships the Map, rail-chip, zones-dialog and error-card rules", () => {
    const map = css("components/grid/CodeMapTab.css");
    for (const sel of [
      ".pane-map",
      ".cm-toolbar",
      ".ct-stage",
      ".ct-canvas",
      ".ct-left",
      // the bird index: a left column in wide panes, a slim left rail in small ones (never a top strip)
      ".ct-bix",
      ".ct-bix-row",
      ".ct-bix-subs",
      ".ct-bix-rail",
      ".ct-rail-row",
      ".ct-rail-tile",
      ".ct-bix-rules",
      // CHANGES: where the changes are, what could break (on demand), one-click ⛔ / ✓ per changed folder
      ".ct-bix-changes",
      ".ct-bix-atrisk",
      ".ct-bix-ranked",
      ".ct-bix-folder",
      ".ct-bix-zb",
      ".ct-legend",
      ".ct-mini",
      ".ct-activity",
      ".ct-info",
      ".ct-tip",
      ".ct-toast",
      ".ct-growing",
      ".ct-drawer",
      ".cm-side",
      ".cm-add",
      ".cm-guard.g-guarded",
      ".cm-guard.g-green",
      ".stagechip.rzchip.rz-breach",
    ]) {
      expect(map, sel).toContain(sel);
    }
    expect(css("components/dialogs/RedZonesDialog.css")).toContain("#red-zones-panel");
    expect(css("components/ConnBanner.css")).toContain("#cs-errors .cs-error-actions");
    // …and every one of those sheets reaches the bundle.
    const index = css("styles/index.css");
    for (const sheet of [
      '"../components/grid/CodeMapTab.css"',
      '"../components/dialogs/RedZonesDialog.css"',
      '"../components/ConnBanner.css"',
      '"./theme-light.css"',
    ]) {
      expect(index, sheet).toContain("@import " + sheet);
    }
  });

  it("derives the tree's colours from the app tokens, with a light set and a narrow-pane toolbar", () => {
    const map = css("components/grid/CodeMapTab.css");
    expect(map).toMatch(/--ct-sky-top:\s*color-mix\(in srgb, var\(--bg\)/);
    const light = css("styles/theme-light.css");
    expect(light).toContain(".light .ct-root");
    expect(light).toContain("--ct-fol-a");
    expect(map).toContain("@container (max-width: 760px)");
    const narrow = map.slice(map.indexOf("@container (max-width: 760px)"));
    // icon-only buttons keep their words as accessible names: visually hidden, never display: none
    expect(narrow).toMatch(/\.ct-toolbar \.lbl\s*\{\s*position: absolute;[^}]*clip-path: inset\(50%\)/);
    expect(narrow).not.toMatch(/\.ct-toolbar \.lbl\s*\{\s*display: none/);
  });
});
