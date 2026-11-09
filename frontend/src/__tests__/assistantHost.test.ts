/** The voice assistant's hands on the page (lib/assistantHost): each UiAction
 * executor against a fake env, the clickables (names, the scan, the match),
 * typing the way React sees it, the palette's commands with the palette
 * closed, and the browser-only prefs (lib/assistantPrefs). */

import { afterEach, describe, expect, it, vi } from "vitest";
import {
  ASSISTANT_DIALOGS,
  chipJump,
  ASSISTANT_SETTINGS_SCREENS,
  clickableName,
  clickableUsable,
  execUiAction,
  normLabel,
  paletteCommands,
  pickLabel,
  resolveSession,
  scanClickables,
  typeInto,
  type ElLike,
  type ExecEnv,
} from "../lib/assistantHost";
import { assistantPrefs, coercePref, prefSnapshot, setAssistantPref, type AssistantPref } from "../lib/assistantPrefs";
import { useUi } from "../state/store";
import type { PaletteAction } from "../components/palette/paletteRank";
import { src } from "./sourceText";

// --- a fake env ------------------------------------------------------------------ //

function fakeEnv(over: Partial<ExecEnv> = {}) {
  const log: string[] = [];
  let dialog: string | null = null;
  const cmds: PaletteAction[] = [
    { label: "Intake…", run: () => void log.push("cmd:intake") },
    { label: "Toggle sidebar", run: () => void log.push("cmd:sidebar") },
    { label: "Push — Login fix", run: () => void log.push("cmd:push") },
  ];
  const env: ExecEnv = {
    openDialog: (n, t) => {
      dialog = n;
      log.push("open:" + n + (t ? ":" + t : ""));
    },
    openSettings: (k) => {
      dialog = "settings";
      log.push("settings:" + k);
    },
    closeDialog: () => {
      log.push("close:" + dialog);
      dialog = null;
    },
    openDialogName: () => dialog,
    setView: (m) => void log.push("view:" + m),
    titles: () => ["fix-login", "docs-refresh"],
    name: (t) => (t === "fix-login" ? "Login fix" : t),
    focus: (t) => void log.push("focus:" + t),
    commands: () => cmds,
    sessionAction: (id, t) => (log.push("action:" + id + ":" + t), { ok: true, text: id + " — " + t, session: t }),
    click: (l) => (log.push("click:" + l), { ok: true, text: "Pressed " + l }),
    pref: (k, v) => (log.push("pref:" + k + "=" + String(v)), { ok: true, text: k }),
    type: (x) => (log.push("type:" + x), { ok: true, text: "Typed" }),
    ...over,
  };
  return { env, log, setDialog: (d: string | null) => void (dialog = d) };
}

describe("the executors", () => {
  it("open: a dialog by its DialogName, a Settings screen by settings:<key>; unknown targets refused", () => {
    const { env, log } = fakeEnv();
    expect(execUiAction({ do: "open", target: "intake" }, env)).toEqual({ ok: true, text: "Opened Intake", open: "intake" });
    expect(execUiAction({ do: "open", target: "settings:voice" }, env)).toEqual({ ok: true, text: "Opened Settings → Voice", open: "settings:voice" });
    expect(execUiAction({ do: "open", target: "settings:nope" }, env).ok).toBe(false);
    expect(execUiAction({ do: "open", target: "ask" }, env).ok).toBe(false);
    expect(log).toEqual(["open:intake", "settings:voice"]);
  });

  it("close: the open dialog, by its name; nothing open is fine", () => {
    const { env, log, setDialog } = fakeEnv();
    expect(execUiAction({ do: "close" }, env)).toEqual({ ok: true, text: "Nothing was open" });
    setDialog("schedule");
    expect(execUiAction({ do: "close" }, env)).toEqual({ ok: true, text: "Closed Schedule" });
    expect(log).toEqual(["close:schedule"]);
  });

  it("view: grid or sky", () => {
    const { env, log } = fakeEnv();
    expect(execUiAction({ do: "view", mode: "sky" }, env).text).toBe("Showing the Sky");
    expect(execUiAction({ do: "view", mode: "grid" }, env).text).toBe("Showing the grid");
    expect(log).toEqual(["view:sky", "view:grid"]);
  });

  it("focus: by title or by the rail's name, the chip jumps to it", () => {
    const { env, log } = fakeEnv();
    expect(execUiAction({ do: "focus", session: "Login fix" }, env)).toEqual({ ok: true, text: "Focused Login fix", session: "fix-login" });
    expect(execUiAction({ do: "focus", session: "docs-refresh" }, env).ok).toBe(true);
    expect(execUiAction({ do: "focus", session: "ghost" }, env)).toEqual({ ok: false, text: "No session called ghost" });
    expect(log).toEqual(["focus:fix-login", "focus:docs-refresh"]);
  });

  it("command: the palette's action with that label (exact, else the same words), run", () => {
    const { env, log } = fakeEnv();
    expect(execUiAction({ do: "command", label: "Intake…" }, env)).toEqual({ ok: true, text: "Intake" });
    expect(execUiAction({ do: "command", label: "toggle sidebar" }, env).ok).toBe(true);
    expect(execUiAction({ do: "command", label: "Launch rockets" }, env).ok).toBe(false);
    expect(log).toEqual(["cmd:intake", "cmd:sidebar"]);
  });

  it("session_action: a SESSION_ACTIONS id on a known session; anything else refused", () => {
    const { env, log } = fakeEnv();
    expect(execUiAction({ do: "session_action", session: "Login fix", action: "push" }, env)).toMatchObject({ ok: true, session: "fix-login" });
    expect(execUiAction({ do: "session_action", session: "fix-login", action: "launch" }, env).ok).toBe(false);
    expect(execUiAction({ do: "session_action", session: "ghost", action: "push" }, env).ok).toBe(false);
    expect(log).toEqual(["action:push:fix-login"]);
  });

  it("click / pref / type go to their env hooks", () => {
    const { env, log } = fakeEnv();
    execUiAction({ do: "click", label: "Save" }, env);
    execUiAction({ do: "pref", key: "sky_birds", value: false }, env);
    execUiAction({ do: "type", text: "fix the login" }, env);
    expect(log).toEqual(["click:Save", "pref:sky_birds=false", "type:fix the login"]);
  });

  it("an executor that throws is a failure chip, never a throw", () => {
    const { env } = fakeEnv({
      setView: () => {
        throw new Error("boom");
      },
    });
    expect(execUiAction({ do: "view", mode: "sky" }, env)).toEqual({ ok: false, text: "Couldn't do that: boom" });
  });

  it("knows every dialog and Settings screen the contract names", () => {
    for (const d of ["new-session", "settings", "intake", "customize", "verify", "schedule", "recent", "prompts", "setup", "red-zones", "palette", "shortcuts"])
      expect(ASSISTANT_DIALOGS).toHaveProperty(d);
    // Every SCREENS key of SettingsDialog (the maintainer-only traffic aside).
    const screens = [...src("components/settings/SettingsDialog.tsx").matchAll(/\{ key: "([a-z]+)", label:/g)].map((m) => m[1]).filter((k) => k !== "traffic");
    expect(screens.length).toBeGreaterThan(15);
    expect(Object.keys(ASSISTANT_SETTINGS_SCREENS).sort()).toEqual(screens.sort());
  });
});

describe("a chip, clicked", () => {
  it("opens the dialog or Settings screen it did again", () => {
    chipJump({ text: "Opened Intake", ok: true, open: "intake" });
    expect(useUi.getState()).toMatchObject({ openDialog: "intake" });
    chipJump({ text: "Opened Settings → Voice", ok: true, open: "settings:voice" });
    expect(useUi.getState()).toMatchObject({ openDialog: "settings", dialogTarget: "voice" });
    useUi.getState().closeDialog();
  });
});

describe("names and matches", () => {
  it("normLabel drops case, ellipses and punctuation", () => {
    expect(normLabel("Intake…")).toBe("intake");
    expect(normLabel("  Push — Login fix ")).toBe("push login fix");
    expect(normLabel("Don’t ask")).toBe("don't ask");
  });

  it("pickLabel: exact, then the same words, then a prefix, then a part; screen order breaks ties", () => {
    const names = ["Save", "Save draft", "Intake…", "Read replies aloud", "Close"];
    expect(pickLabel(names, "Save")).toBe(0);
    expect(pickLabel(names, "intake")).toBe(2);
    expect(pickLabel(names, "save draft")).toBe(1);
    expect(pickLabel(names, "Read replies")).toBe(3);
    expect(pickLabel(names, "replies")).toBe(3);
    expect(pickLabel(names, "x")).toBe(-1);
    expect(pickLabel(names, "Delete")).toBe(-1);
  });

  it("resolveSession: a title, or the rail's name for it", () => {
    const name = (t: string) => (t === "a1" ? "Login fix" : t);
    expect(resolveSession("a1", ["a1"], name)).toBe("a1");
    expect(resolveSession("login fix", ["a1"], name)).toBe("a1");
    expect(resolveSession("nope", ["a1"], name)).toBeNull();
  });
});

// --- clickables over fake elements ---------------------------------------------- //

function el(p: Partial<ElLike> & { attrs?: Record<string, string>; hiddenBy?: boolean; rects?: number; label?: string }): ElLike {
  const attrs = p.attrs || {};
  return {
    tagName: p.tagName || "BUTTON",
    textContent: p.textContent ?? "",
    getAttribute: (n: string) => (n in attrs ? attrs[n] : null),
    closest: (sel: string) => {
      if (sel.includes("label") && p.label) return { textContent: p.label } as ElLike;
      if (sel.includes("aria-hidden") && p.hiddenBy) return {} as ElLike;
      return null;
    },
    getClientRects: () => ({ length: p.rects ?? 1 }),
    disabled: p.disabled,
    type: p.type,
    value: p.value,
    labels: p.labels,
    ownerDocument: p.ownerDocument,
  };
}

describe("the clickables", () => {
  it("names: aria-label, aria-labelledby, a control's label, its text, its title", () => {
    expect(clickableName(el({ textContent: "Save", attrs: { "aria-label": "Save the template" } }))).toBe("Save the template");
    const doc = { getElementById: (id: string) => ({ textContent: id === "h" ? "Birds in the Sky" : "" }) };
    expect(clickableName(el({ attrs: { "aria-labelledby": "h" }, ownerDocument: doc }))).toBe("Birds in the Sky");
    expect(clickableName(el({ tagName: "INPUT", type: "checkbox", labels: [{ textContent: " Reduce   motion " }] }))).toBe("Reduce motion");
    expect(clickableName(el({ tagName: "INPUT", type: "checkbox", label: "Idle flock" }))).toBe("Idle flock");
    expect(clickableName(el({ tagName: "INPUT", type: "submit", value: "Start" }))).toBe("Start");
    expect(clickableName(el({ textContent: "\n  New   session\n" }))).toBe("New session");
    expect(clickableName(el({ attrs: { title: "Close (Esc)" } }))).toBe("Close (Esc)");
  });

  it("a name with no letter or digit is no name; a long one is cut", () => {
    expect(clickableName(el({ textContent: "×" }))).toBe("");
    expect(clickableName(el({ textContent: "›" }))).toBe("");
    const long = clickableName(el({ textContent: "word ".repeat(40) }));
    expect(long.length).toBeLessThanOrEqual(80);
    expect(long.endsWith("…")).toBe(true);
  });

  it("only usable things: shown, enabled, not aria-hidden / inert / the strip itself", () => {
    expect(clickableUsable(el({ textContent: "Go" }))).toBe(true);
    expect(clickableUsable(el({ textContent: "Go", disabled: true }))).toBe(false);
    expect(clickableUsable(el({ textContent: "Go", attrs: { "aria-disabled": "true" } }))).toBe(false);
    expect(clickableUsable(el({ textContent: "Go", rects: 0 }))).toBe(false);
    expect(clickableUsable(el({ textContent: "Go", hiddenBy: true }))).toBe(false);
  });

  it("the scan: usable, named, each name once, at most max", () => {
    const items = [
      el({ textContent: "Save" }),
      el({ textContent: "Save" }),
      el({ textContent: "Hidden", rects: 0 }),
      el({ textContent: "×" }),
      el({ textContent: "Cancel" }),
      ...Array.from({ length: 200 }, (_, i) => el({ textContent: "Row " + i })),
    ];
    const root = { querySelectorAll: vi.fn((_sel: string) => items) };
    const got = scanClickables(root);
    expect(root.querySelectorAll.mock.calls[0][0]).toContain('[role="tab"]');
    expect(root.querySelectorAll.mock.calls[0][0]).toContain('[role="switch"]');
    expect(got.length).toBe(150);
    expect(got.slice(0, 3).map((g) => g.name)).toEqual(["Save", "Cancel", "Row 0"]);
    expect(scanClickables(root, 2).map((g) => g.name)).toEqual(["Save", "Cancel"]);
  });
});

describe("typing", () => {
  it("goes in at the cursor through the prototype's value setter (what React's onChange sees), then an input event", () => {
    const writes: string[] = [];
    class Field {
      private v = "Fix the bug";
      selectionStart: number | null = 4;
      selectionEnd: number | null = 4;
      events: Event[] = [];
      get value() {
        return this.v;
      }
      set value(x: string) {
        writes.push("proto:" + x);
        this.v = x;
      }
      dispatchEvent(e: Event) {
        this.events.push(e);
        return true;
      }
    }
    const f = new Field();
    // React's tracker: an own `value` property on the element, which a plain
    // assignment would hit — and React would then think nothing changed.
    Object.defineProperty(f, "value", {
      configurable: true,
      get: () => Object.getOwnPropertyDescriptor(Field.prototype, "value")?.get?.call(f),
      set: (x: string) => void writes.push("own:" + x),
    });
    typeInto(f as never, "login ", () => new Event("input"));
    expect(writes).toEqual(["proto:Fix login the bug"]);
    expect(f.selectionStart).toBe(10);
    expect(f.events).toHaveLength(1);
  });

  it("replaces a selection", () => {
    const f = { value: "abcdef", selectionStart: 1, selectionEnd: 4, dispatchEvent: () => true };
    typeInto(f, "X", () => new Event("input"));
    expect(f.value).toBe("aXef");
  });
});

// --- the palette's commands, closed ---------------------------------------------- //

describe("the palette's commands with the palette closed", () => {
  afterEach(() => useUi.setState({ focused: null }));

  it("lists the palette's labels (no Focus rows, no duplicates) and each one runs", () => {
    const cmds = paletteCommands();
    const labels = cmds.map((c) => c.label);
    expect(labels).toContain("New session…");
    expect(labels).toContain("Intake…");
    expect(labels).toContain("Toggle sidebar");
    expect(labels).toContain("Open Settings");
    expect(labels.some((l) => l.startsWith("Focus: "))).toBe(false);
    expect(new Set(labels).size).toBe(labels.length);
    const intake = cmds.find((c) => c.label === "Intake…");
    intake?.run();
    expect(useUi.getState().openDialog).toBe("intake");
    useUi.getState().closeDialog();
  });

  it("the palette builds its rows with the same function (behaviour unchanged)", () => {
    const pal = src("components/palette/CommandPalette.tsx");
    expect(pal).toContain("export function buildPaletteActions(");
    expect(pal).toMatch(/if \(!open\) return \[\];\s*return buildPaletteActions\(\{ config, host, extensions \}\);/);
  });
});

// --- prefs ------------------------------------------------------------------------ //

describe("the browser-only prefs", () => {
  afterEach(() => useUi.getState().setSkyBirds(false));

  it("has the curated catalog: birds, sidebar, view, sky mode, theme and the rest — each with a label, type and value", () => {
    const list = assistantPrefs();
    const keys = list.map((p) => p.key);
    for (const k of ["sky_birds", "sidebar", "view", "sky_mode", "grid_panes", "theme", "reduce_motion", "idle_flock", "read_replies"]) expect(keys).toContain(k);
    const snap = prefSnapshot(list);
    expect(snap.find((p) => p.key === "sky_birds")).toEqual({ key: "sky_birds", label: "Birds in the Sky", type: "bool", value: false });
    expect(snap.find((p) => p.key === "view")).toMatchObject({ type: "select", options: ["grid", "sky"] });
    expect(snap.find((p) => p.key === "sky_mode")?.options).toEqual(["one", "all", "mine", "perched"]);
  });

  it("sets Birds in the Sky through the store, with a chip line", () => {
    expect(setAssistantPref("sky_birds", true)).toEqual({ ok: true, text: "Birds in the Sky: on" });
    expect(useUi.getState().skyBirds).toBe(true);
    expect(setAssistantPref("sky_birds", "off").ok).toBe(true);
    expect(useUi.getState().skyBirds).toBe(false);
  });

  it("coerces the way people say it, and refuses what a pref can't hold", () => {
    const b = { key: "x", label: "X", type: "bool", value: () => false, set: () => {} } as AssistantPref;
    expect(coercePref(b, "on")).toBe(true);
    expect(coercePref(b, "Hide")).toBe(false);
    expect(coercePref(b, "maybe")).toBeUndefined();
    const n = { key: "n", label: "N", type: "number", min: 5, max: 240, value: () => 30, set: () => {} } as AssistantPref;
    expect(coercePref(n, "500")).toBe(240);
    expect(coercePref(n, 2)).toBe(5);
    expect(coercePref(n, "lots")).toBeUndefined();
    const list = assistantPrefs();
    const sky = list.find((p) => p.key === "sky_mode") as AssistantPref;
    expect(coercePref(sky, "Needs you")).toBe("perched");
    expect(coercePref(sky, "ALL")).toBe("all");
    expect(coercePref(sky, "sideways")).toBeUndefined();
    expect(setAssistantPref("nope", 1).ok).toBe(false);
    expect(setAssistantPref("view", "list").ok).toBe(false);
  });
});
