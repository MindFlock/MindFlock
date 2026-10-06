import { describe, it, expect, afterEach } from "vitest";
import {
  KEYMAP,
  comboLabel,
  comboProblem,
  sameCombo,
  effBindings,
  defaultCombosFor,
  chordKeyFor,
  chordForKey,
  chordShadowedBy,
  setChordKey,
  setKeyCombos,
  resetAllOverrides,
  modalOpen,
} from "../lib/keymap";
import { useUi } from "../state/store";

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

describe("a newer chord whose letter an older rebinding took", () => {
  it("the user's own binding wins, and the newer chord says it is taken", () => {
    // Rebound before Ctrl+K S existed: Commit moved to "s".
    setChordKey("c", "s");
    expect(chordForKey("s")).toBe("c");
    expect(chordShadowedBy("s")).toBe("c");
    expect(chordShadowedBy("c")).toBeNull();
    // Given a free key, Message… is reachable again and nothing is taken.
    setChordKey("s", "m");
    expect(chordForKey("m")).toBe("s");
    expect(chordShadowedBy("s")).toBeNull();
  });

  it("with no overrides every default letter is its own chord", () => {
    for (const k of ["s", "f", "t", "c"]) {
      expect(chordForKey(k)).toBe(k);
      expect(chordShadowedBy(k)).toBeNull();
    }
  });
});

describe("Alt+O opens the Outbox", () => {
  it("is bound, on the sheet, and taken by nothing else", () => {
    const o = byId("outbox");
    expect(o).toMatchObject({ key: "o", alt: true });
    expect(o.help?.[1]).toBe("Alt+O");
    const combo = defaultCombosFor("outbox")[0];
    const clash = KEYMAP.filter((e) => e !== o && e.key.toLowerCase() === "o" && sameCombo(combo, { key: e.key, mod: e.mod, shift: e.shift, alt: e.alt }));
    expect(clash).toEqual([]);
    // Not a Ctrl+K chord: O there is still "Open / focus IDE".
    expect(chordForKey("o")).toBe("o");
  });

  it("guards like Alt+I: it never eats a keystroke meant for a text field", () => {
    expect(typeof byId("outbox").when).toBe("function");
  });
});

describe("modalOpen", () => {
  afterEach(() => useUi.getState().closeDialog());

  it("counts the Outbox: its Commit / Retry / Skip rows are about OTHER sessions", () => {
    useUi.getState().openDialogFor("outbox");
    expect(modalOpen()).toBe(true);
  });

  it("counts the Red zones dialog: Delete on a zone's × or Ctrl+W in its input must not close the session behind", () => {
    // The store slot answers before any DOM lookup, so this runs without a DOM.
    useUi.getState().openDialogFor("red-zones");
    expect(modalOpen()).toBe(true);
  });
});

describe("Delete never ends the agent from inside a Map tab", () => {
  const g = globalThis as Record<string, unknown>;
  const hadDoc = "document" in g;
  const prevDoc = g.document;
  afterEach(() => {
    useUi.setState({ focused: null } as never);
    if (hadDoc) g.document = prevDoc;
    else delete g.document;
  });

  const withActive = (el: unknown) => {
    g.document = { activeElement: el, getElementById: () => null };
  };
  const button = (inMap: boolean) => ({
    tagName: "BUTTON",
    className: "cm-row-act cm-x quiet",
    closest: (sel: string) => (inMap && sel === ".cm-root" ? {} : null),
  });

  it("a focused Map card / zone × button: the Delete alias stands down", () => {
    useUi.setState({ focused: "sess-A" } as never);
    withActive(button(true));
    expect(aliasFor("close").when!()).toBe(false);
  });

  it("elsewhere (a sidebar button) Delete still ends the focused session", () => {
    useUi.setState({ focused: "sess-A" } as never);
    withActive(button(false));
    expect(aliasFor("close").when!()).toBe(true);
  });
});
