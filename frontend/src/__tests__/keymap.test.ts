import { describe, it, expect, afterEach, vi } from "vitest";
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

describe("Alt+O is gone with the Outbox", () => {
  it("binds nothing, and the sheet lists no Outbox", () => {
    expect(KEYMAP.some((e) => e.id === "outbox")).toBe(false);
    expect(KEYMAP.some((e) => e.help && /Outbox/.test(e.help[2]))).toBe(false);
    expect(defaultCombosFor("outbox")).toEqual([]);
    // Alt+O is free for a rebind again.
    expect(comboProblem({ key: "o", alt: true }, "palette")).toBeNull();
    // Not a Ctrl+K chord either: O there is still "Open / focus IDE".
    expect(chordForKey("o")).toBe("o");
  });

  it("drops a rebind saved for it in mf_keymap on load, and keeps the rest", async () => {
    const g = globalThis as Record<string, unknown>;
    const prev = g.localStorage;
    let saved: string | null = JSON.stringify({
      keys: { outbox: [{ key: "x", alt: true }], palette: [{ key: "j", mod: true }] },
      chords: {},
    });
    g.localStorage = {
      getItem: (k: string) => (k === "mf_keymap" ? saved : null),
      setItem: (k: string, v: string) => {
        if (k === "mf_keymap") saved = v;
      },
      removeItem: () => {},
    };
    try {
      vi.resetModules();
      const km = await import("../lib/keymap");
      expect(km.getKeyOverride("outbox")).toBeUndefined();
      expect(km.getKeyOverride("palette")).toEqual([{ key: "j", mod: true }]);
      // Written back without it, so the next load starts clean.
      expect(JSON.parse(saved || "{}").keys).toEqual({ palette: [{ key: "j", mod: true }] });
      // Its old combo is not "already" anything.
      expect(km.comboProblem({ key: "x", alt: true }, "verify")).toBeNull();
    } finally {
      if (prev === undefined) delete g.localStorage;
      else g.localStorage = prev;
      vi.resetModules();
    }
  });
});

describe("modalOpen", () => {
  afterEach(() => useUi.getState().closeDialog());

  it("counts Customize on either tab: its inputs are not the session behind", () => {
    for (const name of ["customize", "prompts"] as const) {
      useUi.getState().openDialogFor(name);
      expect(modalOpen()).toBe(true);
      useUi.getState().closeDialog();
    }
  });

  it("counts the Red zones dialog: Delete on a zone's × or Ctrl+W in its input must not close the session behind", () => {
    // The store slot answers before any DOM lookup, so this runs without a DOM.
    useUi.getState().openDialogFor("red-zones");
    expect(modalOpen()).toBe(true);
  });
});

describe("Delete / Ctrl+W never end the focused session from inside the bell", () => {
  // The waiting rows (Retry / Skip / Commit) live in the bell's popover; its
  // buttons are about OTHER sessions.
  const g = globalThis as Record<string, unknown>;
  const hadDoc = "document" in g;
  const prevDoc = g.document;
  afterEach(() => {
    useUi.setState({ focused: null } as never);
    if (hadDoc) g.document = prevDoc;
    else delete g.document;
  });
  const withBell = (open: boolean) => {
    const pop = { classList: { contains: () => false } };
    g.document = {
      activeElement: { tagName: "BUTTON", className: "attn-act", closest: () => null },
      getElementById: (id: string) => (open && id === "notif-pop" ? pop : null),
    };
  };

  it("the open bell counts as a modal", () => {
    withBell(true);
    expect(modalOpen()).toBe(true);
    withBell(false);
    expect(modalOpen()).toBe(false);
  });

  it("Delete and Ctrl+W stand down while the bell is open, and work again once it closes", () => {
    useUi.setState({ focused: "sess-A" } as never);
    withBell(true);
    expect(aliasFor("close").when!()).toBe(false);
    expect(byId("close").when!()).toBe(false);
    withBell(false);
    expect(aliasFor("close").when!()).toBe(true);
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
