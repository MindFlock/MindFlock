import { describe, it, expect, afterEach } from "vitest";
import {
  KEYMAP,
  comboLabel,
  comboProblem,
  sameCombo,
  effBindings,
  defaultCombosFor,
  chordKeyFor,
  setKeyCombos,
  resetAllOverrides,
} from "../lib/keymap";

afterEach(() => resetAllOverrides());

const byId = (id: string) => KEYMAP.find((e) => e.id === id)!;
const aliasFor = (id: string) => KEYMAP.find((e) => e.aliasOf === id)!;
const pairFor = (id: string) => KEYMAP.find((e) => e.pairOf === id)!;

describe("comboLabel", () => {
  it("renders modifiers and remapped key names", () => {
    expect(comboLabel({ key: "p", mod: true, shift: true })).toBe("Ctrl+Shift+P");
    expect(comboLabel({ key: "n", alt: true })).toBe("Alt+N");
    expect(comboLabel({ key: "PageDown", mod: "ctrl" })).toBe("Ctrl+PgDn");
    expect(comboLabel({ key: "Tab", mod: "ctrl", shift: true })).toBe("Ctrl+Shift+Tab");
  });
});

describe("sameCombo", () => {
  it("treats missing shift/alt as false but keeps 'any' distinct", () => {
    expect(sameCombo({ key: "p", mod: true }, { key: "p", mod: true, shift: false })).toBe(true);
    expect(sameCombo({ key: "p", mod: true }, { key: "q", mod: true })).toBe(false);
    expect(sameCombo({ key: "p", shift: "any" }, { key: "p", shift: false })).toBe(false);
    expect(sameCombo({ key: "p", shift: "any" }, { key: "p", shift: "any" })).toBe(true);
  });
});

describe("comboProblem", () => {
  it("rejects a bare printable key", () => {
    expect(comboProblem({ key: "x" }, "palette")).toMatch(/Include Ctrl or Alt/);
  });

  it("reserves Shift on an action that owns a Shift-inverse pair", () => {
    // "cycle" has a pairOf partner (Ctrl+Shift+Tab = previous).
    expect(comboProblem({ key: "j", mod: true, shift: true }, "cycle")).toMatch(/Shift is reserved/);
  });

  it("detects a collision with an existing binding and names it", () => {
    // Ctrl+B is the sidebar toggle.
    expect(comboProblem({ key: "b", mod: true }, "palette")).toBe("Ctrl+B is already Toggle sidebar");
  });

  it("passes a free combo", () => {
    expect(comboProblem({ key: "j", mod: true }, "palette")).toBeNull();
  });
});

describe("effBindings", () => {
  it("returns the entry itself by default", () => {
    const primary = byId("new");
    expect(effBindings(primary)).toEqual([primary]);
    expect(effBindings(aliasFor("new"))).toEqual([aliasFor("new")]);
  });

  it("routes a customized action to its override and retires the alias", () => {
    const primary = byId("new");
    setKeyCombos("new", [{ key: "F3", mod: true }]);
    expect(effBindings(primary)).toEqual([{ key: "F3", mod: true }]);
    expect(effBindings(aliasFor("new"))).toEqual([]);
  });

  it("follows the primary's custom combo on its Shift-inverse pair", () => {
    setKeyCombos("cycle", [{ key: "F4", mod: true }]);
    const eff = effBindings(pairFor("cycle"));
    expect(eff).toHaveLength(1);
    expect(eff[0]).toMatchObject({ key: "F4", mod: true, shift: true });
  });
});

describe("defaultCombosFor", () => {
  it("collects the primary plus browser-safe aliases", () => {
    const combos = defaultCombosFor("new");
    expect(combos).toHaveLength(2);
    expect(combos.map((c) => ({ key: c.key, mod: !!c.mod, alt: !!c.alt }))).toEqual([
      { key: "n", mod: true, alt: false },
      { key: "n", mod: false, alt: true },
    ]);
  });
});

describe("chordKeyFor", () => {
  it("returns the default letter with no override", () => {
    expect(chordKeyFor("c")).toBe("c");
    expect(chordKeyFor("p")).toBe("p");
  });
});
