import { describe, expect, it } from "vitest";
import { hideFoldedFamilies, type FoldEntry } from "../lib/familyFold";
import sidebarSrc from "../components/sidebar/Sidebar.tsx?raw";
import rowSrc from "../components/sidebar/SidebarRow.tsx?raw";

const e = (title: string, parent = "", device = ""): FoldEntry => ({
  key: title,
  inst: { title, parent, device: device || undefined },
});
// orch → a, b;  a → a1 (a grand-child);  solo stands alone;  w is a window row.
const RAIL: FoldEntry[] = [e("orch"), e("a", "orch"), e("a1", "a"), e("b", "orch"), e("solo"), { key: "w" }];
const keys = (l: FoldEntry[]) => l.map((x) => x.key);

describe("folding an orchestrator's sub-sessions", () => {
  it("is the same list when nothing is folded", () => {
    expect(hideFoldedFamilies(RAIL, new Set())).toBe(RAIL);
  });

  it("hides every descendant of a folded parent, keeping the parent", () => {
    expect(keys(hideFoldedFamilies(RAIL, new Set(["orch"])))).toEqual(["orch", "solo", "w"]);
  });

  it("folds one level down without touching the siblings", () => {
    expect(keys(hideFoldedFamilies(RAIL, new Set(["a"])))).toEqual(["orch", "a", "b", "solo", "w"]);
  });

  it("never hides the focused session", () => {
    expect(keys(hideFoldedFamilies(RAIL, new Set(["orch"]), "a1"))).toEqual(["orch", "a1", "solo", "w"]);
  });

  it("ignores a parent that is not on this rail, and another device's rows", () => {
    const rail = [e("x", "gone"), e("r", "orch", "laptop"), e("orch")];
    expect(keys(hideFoldedFamilies(rail, new Set(["gone", "orch"])))).toEqual(["x", "r", "orch"]);
  });

  it("survives a parent cycle", () => {
    const rail = [e("p", "q"), e("q", "p")];
    expect(keys(hideFoldedFamilies(rail, new Set(["z"])))).toEqual(["p", "q"]);
  });

  it("is applied before the rail is numbered, and not while searching", () => {
    const src = sidebarSrc as string;
    const at = src.indexOf("const localRail = hideFoldedFamilies(");
    expect(at).toBeGreaterThan(-1);
    expect(at).toBeLessThan(src.indexOf("const localSplit = splitRail(localRail"));
    expect(src).toContain("ui.filter ? NO_FOLDS : ui.collapsedFamilies");
  });

  it("is a worded toggle, not another chevron (› is the row's actions)", () => {
    const row = rowSrc as string;
    expect(row).toContain('className="fold-kids"');
    expect(row).toContain('{folded ? "show " + kids.length : "hide"}');
    expect(row).toContain("toggleFamilyCollapsed(title)");
  });
});
