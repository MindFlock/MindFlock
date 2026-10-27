/** The talk hotkey (Ctrl+T; Alt+Space as the browser-safe alias): push-to-talk
 * for the top-bar mic.
 *
 * The keymap only reports the key — `mindflock:voice-talk` on window with
 * detail.phase "down" / "up" — and the mic reads it through its own
 * push-to-talk mode. This pins the registry's half: one down per physical
 * press, an up on release (key or modifier) or window blur, never an up
 * without a down, working while a terminal has focus, and rebinds applying
 * to the release as well as the press. Runs in the node environment against
 * a fake document, like the other dispatcher tests. */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  KEYMAP,
  VOICE_TALK_EVENT,
  comboLabel,
  comboProblem,
  defaultCombosFor,
  installKeymap,
  resetAllOverrides,
  sameCombo,
  setKeyCombos,
  shortcutLabels,
} from "../lib/keymap";
import { code } from "./sourceText";

const g = globalThis as Record<string, unknown>;
const hadDoc = "document" in g;
const prevDoc = g.document;
const hadWin = "window" in g;
const prevWin = g.window;

type Handler = (e: KeyboardEvent) => void;
let listeners: Record<string, Handler[]>;
let phases: string[];
let details: unknown[];
let off: () => void;

const host = {
  togglePalette: vi.fn(),
  toggleShortcuts: vi.fn(),
  focusFilter: vi.fn(),
  cycleWindow: vi.fn(),
  rowAt: () => null,
  openDoctor: vi.fn(),
};

/** A focused element: xterm's hidden helper textarea, or a plain input. */
const helperTextarea = {
  tagName: "TEXTAREA",
  classList: { contains: (c: string) => c === "xterm-helper-textarea" },
  closest: (sel: string) => (sel.includes(".xterm") ? {} : null),
};

function install(active: unknown = helperTextarea) {
  listeners = {};
  g.document = {
    activeElement: active,
    getElementById: () => null,
    querySelectorAll: () => [],
    addEventListener: (t: string, fn: Handler) => (listeners[t] ||= []).push(fn),
    removeEventListener: (t: string, fn: Handler) => {
      listeners[t] = (listeners[t] || []).filter((f) => f !== fn);
    },
  };
  const win = new EventTarget();
  g.window = win;
  phases = [];
  details = [];
  win.addEventListener(VOICE_TALK_EVENT, (ev) => {
    const d = (ev as CustomEvent).detail;
    details.push(d);
    phases.push(d.phase);
  });
  off = installKeymap(host);
  return win;
}

interface Fake {
  defaultPrevented: boolean;
}

function fire(type: "keydown" | "keyup", key: string, mods: Partial<KeyboardEvent> = {}): Fake {
  const ev = {
    key,
    ctrlKey: false,
    metaKey: false,
    shiftKey: false,
    altKey: false,
    repeat: false,
    ...mods,
    defaultPrevented: false,
    preventDefault() {
      ev.defaultPrevented = true;
    },
    stopPropagation() {},
  };
  for (const fn of listeners[type] || []) fn(ev as unknown as KeyboardEvent);
  return ev;
}

const ALT = { altKey: true };
const CTRL = { ctrlKey: true };

beforeEach(() => install());

afterEach(() => {
  off();
  resetAllOverrides();
  if (hadDoc) g.document = prevDoc;
  else delete g.document;
  if (hadWin) g.window = prevWin;
  else delete g.window;
});

describe("the talk action", () => {
  it("is a rebindable, holdable Navigation row bound to Ctrl+T, with Alt+Space as the browser-safe alias", () => {
    const talk = KEYMAP.find((e) => e.id === "talk")!;
    expect(talk).toBeTruthy();
    expect(talk.holdable).toBe(true);
    expect(talk.help?.[0]).toBe("Navigation");
    expect(talk.help?.[1]).toBe("Ctrl+T");
    expect(talk.help?.[2]).toMatch(/^Talk to the focused session/);
    const alias = KEYMAP.find((e) => e.aliasOf === "talk")!;
    expect(alias.holdable).toBe(true);
    expect(alias.help).toBeUndefined();
    expect(defaultCombosFor("talk")).toEqual([
      { key: "t", mod: true, shift: undefined, alt: undefined },
      { key: " ", mod: undefined, shift: undefined, alt: true },
    ]);
    expect(shortcutLabels("talk")).toEqual(["Ctrl+T", "Alt+Space"]);
    // Nothing else already claims either — and Ctrl+Shift+T (reopen) is distinct.
    expect(comboProblem({ key: "t", mod: true }, "palette")).toMatch(/already Talk to the focused session/);
    expect(comboProblem({ key: " ", alt: true }, "palette")).toMatch(/already Talk to the focused session/);
    expect(comboProblem({ key: "t", mod: true, shift: true }, "palette")).toMatch(/already Reopen/);
  });

  it("Ctrl+T presses and releases like the alias, and ⌘T on a Mac is the same key", () => {
    fire("keydown", "t", CTRL);
    fire("keydown", "t", { ...CTRL, repeat: true });
    fire("keyup", "t", CTRL);
    expect(phases).toEqual(["down", "up"]);
    fire("keydown", "t", { metaKey: true });
    fire("keyup", "Meta");
    expect(phases).toEqual(["down", "up", "down", "up"]);
    // Ctrl+Shift+T stays Reopen, plain t types.
    const plain = fire("keydown", "t");
    fire("keydown", "T", { ...CTRL, shiftKey: true });
    expect(phases).toHaveLength(4);
    expect(plain.defaultPrevented).toBe(false);
  });

  it("macOS's Option+Space (a non-breaking space) is the same key as Space", () => {
    expect(comboLabel({ key: "\u00a0", alt: true })).toBe("Alt+Space");
    expect(sameCombo({ key: "\u00a0", alt: true }, { key: " ", alt: true })).toBe(true);
    fire("keydown", "\u00a0", ALT);
    fire("keyup", "\u00a0", ALT);
    expect(phases).toEqual(["down", "up"]);
  });
});

describe("press and release", () => {
  it("keydown sends one down — auto-repeats while held send nothing — and keyup sends up", () => {
    const down = fire("keydown", " ", ALT);
    fire("keydown", " ", { ...ALT, repeat: true });
    fire("keydown", " ", { ...ALT, repeat: true });
    expect(phases).toEqual(["down"]);
    const up = fire("keyup", " ", ALT);
    expect(phases).toEqual(["down", "up"]);
    // The terminal never sees the keystroke.
    expect(down.defaultPrevented).toBe(true);
    expect(up.defaultPrevented).toBe(true);
  });

  it("the event's detail is exactly { phase }", () => {
    fire("keydown", " ", ALT);
    fire("keyup", " ", ALT);
    expect(details).toEqual([{ phase: "down" }, { phase: "up" }]);
  });

  it("works while xterm's helper textarea has focus (a terminal is where you talk from)", () => {
    // beforeEach installed with the helper textarea focused.
    const ev = fire("keydown", " ", ALT);
    expect(phases).toEqual(["down"]);
    expect(ev.defaultPrevented).toBe(true);
  });

  it("letting go of Alt first ends the hold, and the later Space keyup sends nothing more", () => {
    fire("keydown", " ", ALT);
    fire("keyup", "Alt", { altKey: false });
    expect(phases).toEqual(["down", "up"]);
    fire("keyup", " ");
    expect(phases).toEqual(["down", "up"]);
  });

  it("never sends an up without a down", () => {
    fire("keyup", " ", ALT);
    fire("keyup", "Alt");
    expect(phases).toEqual([]);
  });

  it("each physical press is its own down (toggle mode counts them)", () => {
    fire("keydown", " ", ALT);
    fire("keyup", " ", ALT);
    fire("keydown", " ", ALT);
    fire("keyup", " ", ALT);
    expect(phases).toEqual(["down", "up", "down", "up"]);
  });

  it("plain Space and Ctrl+Space are not the hotkey", () => {
    const plain = fire("keydown", " ");
    fire("keydown", " ", { ctrlKey: true, altKey: true });
    expect(phases).toEqual([]);
    expect(plain.defaultPrevented).toBe(false);
  });
});

describe("window blur", () => {
  it("releases a held press", () => {
    fire("keydown", " ", ALT);
    (g.window as EventTarget).dispatchEvent(new Event("blur"));
    expect(phases).toEqual(["down", "up"]);
    // The keyup lands elsewhere; if it does arrive here, nothing more is sent.
    fire("keyup", " ", ALT);
    expect(phases).toEqual(["down", "up"]);
  });

  it("does nothing when nothing is held", () => {
    (g.window as EventTarget).dispatchEvent(new Event("blur"));
    expect(phases).toEqual([]);
  });
});

describe("a rebound talk key", () => {
  it("drives both the press and the release — from a terminal too — and Ctrl+T and Alt+Space retire", () => {
    setKeyCombos("talk", [{ key: "F9", mod: false, shift: false, alt: false }]);
    fire("keydown", " ", ALT);
    fire("keydown", "t", CTRL);
    expect(phases).toEqual([]);
    fire("keydown", "F9");
    fire("keydown", "F9", { repeat: true });
    fire("keyup", "F9");
    expect(phases).toEqual(["down", "up"]);
    expect(shortcutLabels("talk")).toEqual(["F9"]);
  });

  it("a bare-key rebind still stands down in a real text field", () => {
    off();
    install({ tagName: "INPUT", classList: { contains: () => false }, closest: () => null });
    setKeyCombos("talk", [{ key: "F9", mod: false, shift: false, alt: false }]);
    const ev = fire("keydown", "F9");
    expect(phases).toEqual([]);
    expect(ev.defaultPrevented).toBe(false);
  });

  it("a modifier combo releases on its modifier too", () => {
    setKeyCombos("talk", [{ key: "m", mod: true, shift: true, alt: false }]);
    fire("keydown", "M", { ctrlKey: true, shiftKey: true });
    fire("keyup", "Shift", { ctrlKey: true });
    expect(phases).toEqual(["down", "up"]);
  });
});

describe("the dispatcher's wiring", () => {
  it("listens for keyup and window blur, and removes both on cleanup", () => {
    expect(listeners.keyup).toHaveLength(1);
    off();
    expect(listeners.keyup).toHaveLength(0);
    off = () => {};
    const src = code("lib/keymap.ts");
    expect(src).toContain('addEventListener("blur", _releaseAll)');
    expect(src).toContain('removeEventListener("blur", _releaseAll)');
  });
});
