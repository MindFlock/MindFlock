/** Into the Map from the normal flow (0.10 "The map in the flow"): a hands-on
 * walk found nobody reached the Map (⋯ → More → Code map), and every way in
 * that did exist dropped you at the top of it.
 *
 *  - the Map tab is a default tab for git sessions; the calm first hour folds
 *    it like Queue only while the session has nothing for it to show;
 *  - its badge is the rail's zone chip, else a quiet changed-files count;
 *  - every "Open map" goes through store.openMap with the file and the part
 *    of the Map it is about (lib/mapEntry); a green-scope block's toast can
 *    Allow its one file;
 *  - Diff ⇄ Map per file, and the Diff tab follows the session's changes,
 *    reads Unified in a narrow pane and has readable file headers. */

import { describe, expect, it, vi } from "vitest";
import { code, css, cssRule } from "./sourceText";

const storage = new Map<string, string>();
vi.stubGlobal("localStorage", {
  getItem: (k: string) => (storage.has(k) ? storage.get(k)! : null),
  setItem: (k: string, v: string) => void storage.set(k, String(v)),
  removeItem: (k: string) => void storage.delete(k),
  clear: () => storage.clear(),
});

const { allowableBlock, baseName, breachFocus, mapHasState, mapTabBadge, redZoneEventFocus, zoneChipFocus } = await import(
  "../lib/mapEntry"
);
const { headerItems, resolvePaneHeader, togglePaneItem } = await import("../lib/paneHeader");
const { diffModeFor, pickedDiffMode, SPLIT_MIN_PX } = await import("../lib/diffFit");
const { redZoneChip } = await import("../lib/codemapSeen");

const ctx = (o: Partial<Parameters<typeof headerItems>[1]> = {}) => ({
  active: "agent",
  sharedFolder: false,
  git: true,
  thread: false,
  calm: false,
  queueBusy: false,
  ftBusy: false,
  ...o,
});

describe("the Map tab is on by default", () => {
  it("shows for a git session, never without git", () => {
    expect(resolvePaneHeader(null).shown.has("map")).toBe(true);
    expect(headerItems(null, ctx()).tabs).toContain("map");
    expect(headerItems(null, ctx({ git: false })).tabs).not.toContain("map");
  });

  it("a saved choice that hid it keeps it hidden", () => {
    const hidden = togglePaneItem(null, "map");
    expect(hidden.hidden).toContain("map");
    expect(headerItems(hidden, ctx()).tabs).not.toContain("map");
    expect(headerItems(hidden, ctx({ calm: true, mapBusy: true })).tabs).not.toContain("map");
  });

  it("calm folds it until the session has changes or zone state", () => {
    expect(headerItems(null, ctx({ calm: true })).tabs).not.toContain("map");
    expect(headerItems(null, ctx({ calm: true, mapBusy: true })).tabs).toContain("map");
    // …and never the tab you are on.
    expect(headerItems(null, ctx({ calm: true, active: "map" })).tabs).toContain("map");
  });

  it("has state once a file changed or a zone exists, blocked or breached", () => {
    expect(mapHasState({}, null)).toBe(false);
    expect(mapHasState({ diff_stat: { files: 0 }, redzone: { zones: 0, breaches: 0, last_block_ts: null } }, null)).toBe(false);
    expect(mapHasState({ diff_stat: { files: 2 } }, null)).toBe(true);
    expect(mapHasState({ redzone: { zones: 1 } }, null)).toBe(true);
    expect(mapHasState({ redzone: { zones: 0, breaches: 0, last_block_ts: 5 } }, null)).toBe(true);
    expect(mapHasState({}, { cls: "rz-plan" })).toBe(true);
  });

  it("the pane passes it the Map's state and draws a tablist", () => {
    const pane = code("components/grid/Pane.tsx");
    expect(pane).toContain("mapBusy: mapHasState(inst, rzChip),");
    expect(pane).toContain('role="tablist"');
    for (const t of ["agent", "shell", "diff", "queue", "map", "thread"])
      expect(pane, t).toContain(`data-tab="${t}"\n        role="tab"\n        aria-selected={tab === "${t}"}`);
  });
});

describe("the Map tab's badge", () => {
  const base = { zones: 1, breaches: 0, last_block_ts: null, guard: "guarded" };

  it("is the zone chip when it needs you", () => {
    expect(mapTabBadge(redZoneChip({ ...base, breaches: 2 }, 0), 5)?.label).toBe("⛔2");
    expect(mapTabBadge(redZoneChip({ ...base, last_block_ts: 10 }, 5), 5)?.label).toBe("⛔");
    expect(mapTabBadge(redZoneChip({ ...base, guard: "off" }, 0), 5)?.label).toBe("!");
  });

  it("else a quiet changed-files count, else the calm chip, else nothing", () => {
    const shield = redZoneChip(base, 0);
    expect(mapTabBadge(shield, 3)).toEqual({ cls: "map-changed", label: "3", title: "3 changed files" });
    expect(mapTabBadge(null, 1)?.title).toBe("1 changed file");
    expect(mapTabBadge(null, 250)?.label).toBe("99+");
    expect(mapTabBadge(shield, 0)?.label).toBe("🛡");
    expect(mapTabBadge(null, 0)).toBeNull();
  });

  it("is drawn with an accessible name", () => {
    const pane = code("components/grid/Pane.tsx");
    expect(pane).toContain("const rzChip = redZoneChip(inst.redzone, mapSeen);");
    expect(pane).toContain('aria-label={mapBadge ? "Map, " + mapBadge.title : undefined}');
    expect(pane).toContain('className={"queue-tab-badge map-badge " + mapBadge.cls} aria-hidden="true"');
  });
});

describe("every Open map lands on the spot", () => {
  it("a breach list lands on Breaches and its first file", () => {
    expect(breachFocus([{ path: "a/x.ts" }, { path: "b" }])).toEqual({ section: "breaches", path: "a/x.ts" });
    expect(breachFocus([])).toEqual({ section: "breaches", path: undefined });
  });

  it("a red-zone event: breach → Breaches, green block → Scope, keep-out block → Zones", () => {
    expect(redZoneEventFocus("session.red_zone_breached", { paths: ["s/k"], kind: "red" })).toEqual({ section: "breaches", path: "s/k" });
    expect(redZoneEventFocus("session.red_zone_blocked", { paths: ["d/x.md"], kind: "green" })).toEqual({ section: "scope", path: "d/x.md" });
    expect(redZoneEventFocus("session.red_zone_blocked", { paths: ["s/k"], kind: "red" })).toEqual({ section: "zones", path: "s/k" });
    expect(redZoneEventFocus("session.red_zone_blocked", { paths: ["s/k"], kind: "red", push: true })).toEqual({ section: "breaches", path: "s/k" });
    expect(redZoneEventFocus("session.red_zone_tampered", {})).toEqual({ section: "zones" });
    expect(redZoneEventFocus("session.red_zone_blocked", null)).toEqual({ section: "zones", path: undefined });
  });

  it("only a one-file green block offers Allow", () => {
    expect(allowableBlock("session.red_zone_blocked", { paths: ["docs/x.md"], kind: "green" })).toBe("docs/x.md");
    expect(allowableBlock("session.red_zone_blocked", { paths: ["a", "b"], kind: "green" })).toBeNull();
    expect(allowableBlock("session.red_zone_blocked", { paths: ["a"], kind: "red" })).toBeNull();
    expect(allowableBlock("session.red_zone_blocked", { paths: ["a"], kind: "green", push: true })).toBeNull();
    expect(allowableBlock("session.red_zone_breached", { paths: ["a"], kind: "green" })).toBeNull();
    expect(baseName("docs/guide/x.md")).toBe("x.md");
    expect(baseName("x")).toBe("x");
  });

  it("the zone chip lands where it points", () => {
    expect(zoneChipFocus({ breaches: 2 }, { cls: "rz-breach" })).toEqual({ section: "breaches" });
    expect(zoneChipFocus({ breaches: 0, mode: "green" }, { cls: "rz-ok" })).toEqual({ section: "scope" });
    expect(zoneChipFocus({ breaches: 0 }, { cls: "rz-plan" })).toEqual({ section: "plan" });
    expect(zoneChipFocus({ breaches: 0 }, { cls: "rz-warn" })).toEqual({ section: "zones" });
  });

  it("no caller switches to the Map or Diff tab by hand any more", () => {
    for (const f of [
      "components/EventToasts.tsx",
      "components/dialogs/CommitDialog.tsx",
      "lib/sessionActions.ts",
      "lib/sessionActionTable.ts",
      "lib/sessionMenu.ts",
      "components/sidebar/SidebarRow.tsx",
      "components/grid/ThreadTab.tsx",
      "components/grid/CodeMapTab.tsx",
    ]) {
      expect(code(f), f).not.toMatch(/setLastTab\([^,]+, "map"\)/);
      expect(code(f), f).not.toMatch(/setLastTab\([^,]+, "diff"\)/);
    }
    expect(code("components/dialogs/CommitDialog.tsx")).toContain("useUi.getState().openMap(title, breachFocus(breaches))");
    expect(code("lib/sessionActions.ts")).toContain('useUi.getState().openMap(title, { section: "breaches", path: breaches[0]?.path })');
  });

  it("the green-block toast carries Allow <file>, and its body opens the Map", () => {
    const t = code("components/EventToasts.tsx");
    expect(t).toContain("onClick: () => openMap(env),");
    expect(t).toContain('label: "Allow " + baseName(allow),');
    expect(t).toContain("onClick: () => void allowFromToast(env.session, allow),");
    expect(t).toContain("await allowPath(session, path);");
    expect(t).toContain('toast("Allowed " + path + " for " + displayName(session), {');
  });
});

describe("Diff ⇄ Map per file", () => {
  const diff = code("components/grid/DiffTab.tsx");

  it("each file header has a Map button that lands the Map on that file", () => {
    expect(diff).toContain("<MapButton path={seg.file} onClick={() => showOnMap(seg.file)} />");
    expect(diff).toContain("const showOnMap = (path: string) => useUi.getState().openMap(title, { path });");
    expect(diff).toContain("aria-label={`Show ${path} on the map`}");
    // Its click never collapses the file.
    expect(diff).toContain('closest(".sd-file, .diff-file-map")');
  });

  it("follows store.openDiff to a file, and the Map's file card passes its file", () => {
    expect(diff).toContain("s.diffFocus?.title === title ? s.diffFocus : null");
    expect(diff).toContain("setFile(focus.path || null);");
    expect(code("components/grid/CodeMapTab.tsx")).toContain(
      'const openDiff = (path?: unknown) => useUi.getState().openDiff(title, typeof path === "string" ? path : undefined);'
    );
    expect(code("components/grid/codemap/CodeTree.tsx")).toContain("onDiff={() => p.onOpenDiff(f.path)}");
  });

  it("reloads when the session's diff stats move, and on ↻", () => {
    expect(diff).toContain("}, [active, load, stamp]);");
    expect(code("components/grid/Pane.tsx")).toContain("<DiffTab title={title} active={tab === \"diff\"} stamp={diffStamp} />");
    expect(diff).toContain('aria-label="Reload the diff"');
  });

  it("reads Unified in a narrow pane unless Split was picked", () => {
    expect(diffModeFor(null, 0)).toBe("split");
    expect(diffModeFor(null, SPLIT_MIN_PX - 1)).toBe("unified");
    expect(diffModeFor(null, SPLIT_MIN_PX)).toBe("split");
    expect(diffModeFor("split", 300)).toBe("split");
    expect(diffModeFor("unified", 1600)).toBe("unified");
    storage.clear();
    expect(pickedDiffMode()).toBeNull();
    storage.set("cs_diffmode", "split");
    expect(pickedDiffMode()).toBe("split");
    storage.set("cs_diffmode", "sideways");
    expect(pickedDiffMode()).toBeNull();
  });

  it("file headers read in body text, not the faint purple", () => {
    const c = css("components/grid/DiffTab.css");
    expect(cssRule(c, ".diff-file-head .sd-file")).toContain("color: var(--text)");
    expect(cssRule(c, ".diff-file-head .sd-file")).not.toContain("#c4b3ff");
    expect(cssRule(c, ".diff-file-head .diff-file-caret")).not.toContain("#fff");
  });

  it("a worker row in the Thread opens its Map on what it changed", () => {
    expect(code("components/grid/ThreadTab.tsx")).toContain('useUi.getState().openMap(row.title, { section: "changed" })');
  });
});
