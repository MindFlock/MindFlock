/** The pop-out window (popout/, lib/popout.ts): what it must never do to the
 * main window it shares a localStorage and an event stream with — write the
 * saved layout, close the session on Ctrl+W, fire the main window's
 * shortcuts, raise a second set of notifications — and what it draws (the
 * window bar, the "was closed" notice), server-rendered. */

/// <reference types="vite/client" />
import { createElement, type ReactElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { code, css } from "./sourceText";
import type { Instance } from "../api/types";

vi.mock("../api/client", () => ({
  api: vi.fn(() => new Promise(() => {})),
  instApi: vi.fn(() => new Promise(() => {})),
  ApiError: class extends Error {},
}));
vi.mock("../lib/toast", () => ({ toast: vi.fn(), publishToast: vi.fn() }));
vi.mock("../lib/titleBadge", () => ({
  clearAttention: vi.fn(),
  updateTitleBadge: vi.fn(),
  useTitleBadge: vi.fn(),
  redrawFavicon: vi.fn(),
}));
vi.mock("../lib/popout", async (orig) => ({
  ...(await orig<typeof import("../lib/popout")>()),
  closeThisPopout: vi.fn(),
  focusMainWindow: vi.fn(),
}));

const g = globalThis as Record<string, unknown>;
const KEYS = ["location", "localStorage", "document", "window"] as const;
const saved = Object.fromEntries(KEYS.map((k) => [k, { had: k in g, v: g[k] }]));

function restore() {
  for (const k of KEYS) {
    if (saved[k].had) g[k] = saved[k].v;
    else delete g[k];
  }
}

// Modules that read the document at import (lib/titleBadge, lib/terminals,
// the store's theme) get a quiet one; each test sets its own.
g.document ??= {
  title: "MindFlock",
  hidden: false,
  documentElement: { classList: { contains: () => false } },
  addEventListener: () => {},
  removeEventListener: () => {},
  getElementById: () => null,
  querySelector: () => null,
};

const { QueryClient, QueryClientProvider } = await import("@tanstack/react-query");
const { useUi } = await import("../state/store");
const popout = await import("../lib/popout");
const { KEYMAP, installKeymap } = await import("../lib/keymap");
const sessionActionTable = await import("../lib/sessionActionTable");
const { desktopNotify } = await import("../lib/desktopNotify");
const { EventToasts } = await import("../components/EventToasts");
const { useTitleBadge } = await import("../lib/titleBadge");
const { PopoutApp, PopoutView, POPOUT_DIALOGS, searchWithTitle } = await import("../popout/PopoutApp");
const { adoptStorageChange } = await import("../popout/boot");

restore();

const inPopout = (title = "fix-login") => {
  g.location = { search: "?popout=" + encodeURIComponent(title), origin: "http://h", pathname: "/", hash: "" };
};
const inMain = () => {
  g.location = { search: "", origin: "http://h", pathname: "/", hash: "" };
};

/** A localStorage that records every write. */
function recordingStorage() {
  const data = new Map<string, string>();
  const writes: string[] = [];
  g.localStorage = {
    getItem: (k: string) => data.get(k) ?? null,
    setItem: (k: string, v: string) => {
      writes.push(k);
      data.set(k, v);
    },
    removeItem: (k: string) => {
      writes.push("-" + k);
      data.delete(k);
    },
  };
  return writes;
}

afterEach(() => {
  restore();
  vi.clearAllMocks();
  useUi.setState({ focused: null, openDialog: null } as never);
});

// --------------------------------------------------------------------------- //

describe("the saved layout is the main window's", () => {
  it("a pop-out's layout changes stay in memory: no localStorage write at all", () => {
    inPopout();
    const writes = recordingStorage();
    const s = useUi.getState();
    s.setGridRows([["fix-login"]]);
    s.setViewMode("2");
    s.setOrder(["fix-login"]);
    s.setHidden("other", true);
    s.setLastTab("fix-login", "diff");
    s.touchMru("fix-login");
    expect(writes).toEqual([]);
    // …but this window still sees them.
    expect(useUi.getState().gridRows).toEqual([["fix-login"]]);
    expect(useUi.getState().lastTab["fix-login"]).toBe("diff");
  });

  it("the main window still saves (the control)", () => {
    inMain();
    const writes = recordingStorage();
    useUi.getState().setGridRows([["a", "b"]]);
    useUi.getState().setViewMode("4");
    expect(writes).toEqual(["cs_gridrows", "cs_viewmode"]);
  });
});

// --------------------------------------------------------------------------- //

describe("the keymap in a pop-out", () => {
  /** Install the dispatcher on a fake document and hand back its keydown. */
  function keydown(active: unknown = null) {
    const listeners: Record<string, (e: unknown) => void> = {};
    g.document = {
      activeElement: active,
      addEventListener: (t: string, f: (e: unknown) => void) => (listeners[t] = f),
      removeEventListener: () => {},
      getElementById: () => null,
      querySelectorAll: () => [],
    };
    g.window = { addEventListener: () => {}, removeEventListener: () => {} };
    const off = installKeymap({
      togglePalette: vi.fn(),
      toggleShortcuts: vi.fn(),
      focusFilter: vi.fn(),
      cycleWindow: vi.fn(),
      rowAt: () => "other",
      openDoctor: vi.fn(),
    });
    const press = (key: string, mods: { ctrl?: boolean; alt?: boolean; shift?: boolean; meta?: boolean } = {}) => {
      const e = {
        key,
        ctrlKey: !!mods.ctrl,
        metaKey: !!mods.meta,
        altKey: !!mods.alt,
        shiftKey: !!mods.shift,
        repeat: false,
        defaultPrevented: false,
        preventDefault() {
          this.defaultPrevented = true;
        },
        stopPropagation() {},
      };
      listeners.keydown(e);
      return e.defaultPrevented;
    };
    return { press, off };
  }

  // The table's Close, watched (it would end the session).
  let closeRun: ReturnType<typeof vi.fn>;
  const real = sessionActionTable.sessionAction;
  beforeEach(() => {
    closeRun = vi.fn();
    vi.spyOn(sessionActionTable, "sessionAction").mockImplementation((id) =>
      id === "close" ? ({ ...real(id), run: closeRun } as never) : real(id)
    );
  });
  afterEach(() => vi.restoreAllMocks());

  it("Ctrl+W (and ⌘W) is Back to main window — never Close", () => {
    inPopout();
    useUi.setState({ focused: "fix-login" } as never);
    const { press, off } = keydown();
    expect(press("w", { ctrl: true })).toBe(true);
    expect(popout.closeThisPopout).toHaveBeenCalledTimes(1);
    expect(popout.focusMainWindow).toHaveBeenCalledTimes(1);
    expect(press("w", { meta: true })).toBe(true);
    expect(popout.closeThisPopout).toHaveBeenCalledTimes(2);
    expect(closeRun).not.toHaveBeenCalled();
    off();
  });

  it("…and Delete, Close's alias, does nothing there (the key falls through)", () => {
    inPopout();
    useUi.setState({ focused: "fix-login" } as never);
    const { press, off } = keydown();
    expect(press("Delete")).toBe(false);
    expect(closeRun).not.toHaveBeenCalled();
    expect(popout.closeThisPopout).not.toHaveBeenCalled();
    off();
  });

  it("Ctrl+W typed at the terminal stays the shell's delete-word, as in the main window", () => {
    inPopout();
    useUi.setState({ focused: "fix-login" } as never);
    const term = { tagName: "TEXTAREA", closest: (s: string) => (s.includes("xterm") ? {} : null) };
    const { press, off } = keydown(term);
    expect(press("w", { ctrl: true })).toBe(false);
    expect(popout.closeThisPopout).not.toHaveBeenCalled();
    off();
  });

  it("the main window's shortcuts don't fire: palette, new, views, rail rows, dialogs", () => {
    inPopout();
    useUi.setState({ focused: "fix-login" } as never);
    const { press, off } = keydown();
    expect(press("p", { ctrl: true })).toBe(false);
    expect(press("n", { ctrl: true })).toBe(false);
    expect(press("n", { alt: true })).toBe(false);
    expect(press("y", { ctrl: true })).toBe(false);
    expect(press("b", { ctrl: true })).toBe(false);
    expect(press("Tab", { ctrl: true })).toBe(false);
    expect(press("2", { ctrl: true })).toBe(false);
    expect(press("i", { alt: true })).toBe(false);
    expect(press("s", { alt: true })).toBe(false);
    expect(press("t", { ctrl: true, shift: true })).toBe(false);
    expect(useUi.getState().openDialog).toBeNull();
    off();
  });

  it("the main window keeps every one of them (the control), and Ctrl+W there is Close", () => {
    inMain();
    useUi.setState({ focused: "fix-login" } as never);
    const { press, off } = keydown();
    expect(press("i", { alt: true })).toBe(true);
    useUi.getState().closeDialog();
    expect(press("w", { ctrl: true })).toBe(true);
    expect(closeRun).toHaveBeenCalledWith("fix-login");
    expect(popout.closeThisPopout).not.toHaveBeenCalled();
    off();
  });

  it("the table says which entries a pop-out keeps: this pane's, nothing else", () => {
    const kept = KEYMAP.filter((e) => e.popout).map((e) => e.id || e.label);
    expect(kept.sort()).toEqual(["close", "flock", "reload", "the Ctrl+K chord prefix", "workers"].sort());
    expect(typeof KEYMAP.find((e) => e.id === "close")!.popout).toBe("function");
  });
});

// --------------------------------------------------------------------------- //

describe("no duplicate notifications", () => {
  function shellWithNotify() {
    const show = vi.fn();
    g.window = { mfnotify: { show, onClick: () => () => {} } };
    g.document = { hasFocus: () => false };
    return show;
  }
  const ask = ["session.question", { question: "Ship it?" }, { session: "fix-login" }] as const;

  it("a pop-out raises no OS notification", () => {
    inPopout();
    const show = shellWithNotify();
    expect(desktopNotify(...ask)).toBe(false);
    expect(show).not.toHaveBeenCalled();
  });

  it("the main window does (the control)", () => {
    inMain();
    const show = shellWithNotify();
    expect(desktopNotify(...ask)).toBe(true);
    expect(show).toHaveBeenCalledTimes(1);
  });

  it("EventToasts mounts nothing in a pop-out — not even its title badge", () => {
    g.document = { title: "x", hidden: false, addEventListener: () => {}, removeEventListener: () => {} };
    inPopout();
    expect(renderToStaticMarkup(createElement(EventToasts))).toBe("");
    expect(useTitleBadge).not.toHaveBeenCalled();
    inMain();
    renderToStaticMarkup(createElement(EventToasts));
    expect(useTitleBadge).toHaveBeenCalledTimes(1);
  });
});

// --------------------------------------------------------------------------- //

describe("following the main window's prefs (storage events)", () => {
  it("a theme flipped there repaints here; an unknown key is ignored", () => {
    const toggles: Array<[string, boolean]> = [];
    g.document = {
      documentElement: {
        classList: { toggle: (c: string, on: boolean) => toggles.push([c, on]), contains: () => false },
      },
      dispatchEvent: () => true,
    };
    expect(adoptStorageChange("cs_theme", "light")).toBe(true);
    expect(useUi.getState().themeLight).toBe(true);
    expect(adoptStorageChange("cs_theme", "dark")).toBe(true);
    expect(useUi.getState().themeLight).toBe(false);
    expect(toggles).toEqual([
      ["light", true],
      ["light", false],
    ]);
    expect(adoptStorageChange("cs_gridrows", "[]")).toBe(false);
  });

  it("aliases and reduce-motion follow too", () => {
    expect(adoptStorageChange("mf_aliases", JSON.stringify({ a: "Alpha" }))).toBe(true);
    expect(useUi.getState().aliases).toEqual({ a: "Alpha" });
    adoptStorageChange("mf_reduce_motion", "true");
    expect(useUi.getState().reduceMotion).toBe(true);
    adoptStorageChange("mf_reduce_motion", null);
    expect(useUi.getState().reduceMotion).toBe(false);
  });

  it("main.tsx: a pop-out boots PopoutApp and skips the main window's wiring", () => {
    const src = code("main.tsx");
    expect(src).toContain("{popout ? <PopoutApp /> : <App />}");
    expect(src).toContain("if (!popout) requestAnimationFrame(() => installPrefsSync());");
    expect(src).toContain("if (!popout) requestAnimationFrame(() => installJoinLink());");
    expect(src).toMatch(/if \(!popout\) requestAnimationFrame\(\(\) => \{\s+const s = document\.createElement\("script"\);/);
  });
});

// --------------------------------------------------------------------------- //

const rowOf = (title: string) =>
  ({
    title,
    status: "running",
    activity: "idle",
    stage: "agent",
    task_headline: "Do " + title,
    parent: "",
    branch: title,
    flight: null,
    autopilot: null,
  }) as unknown as Instance;

function withClient(el: ReactElement, instances?: Instance[]) {
  const c = new QueryClient({ defaultOptions: { queries: { retry: false, enabled: false } } });
  if (instances) c.setQueryData(["instances"], instances);
  c.setQueryData(["config"], { caps: { git: true, tailscale: false, ticketing: false, github: false } });
  return renderToStaticMarkup(createElement(QueryClientProvider, { client: c }, el));
}

describe("PopoutApp (server-rendered)", () => {
  beforeEach(() => {
    // The queries' poll intervals read `document.hidden`.
    g.document = {
      hidden: false,
      addEventListener: () => {},
      removeEventListener: () => {},
      getElementById: () => null,
      querySelector: () => null,
    };
  });

  it("the window bar: drag handle, the name, Back to main window", () => {
    inPopout("fix-login");
    const html = withClient(createElement(PopoutApp), [rowOf("fix-login")]);
    expect(html).toContain('id="popout-bar"');
    expect(html).toMatch(/class="popout-grip" draggable="true"/);
    expect(html).toContain('class="popout-name" title="fix-login">fix-login<');
    expect(html).toContain(">Back to main window</button>");
    expect(html).toMatch(/class="popout-dot tone-/);
    // The session itself, in the chrome-less pane.
    expect(html).toContain('class="pane pane-embedded');
    expect(html).not.toContain("popout-gone");
  });

  it("a session that isn't there (once the list has loaded): the calm notice", () => {
    inPopout("gone-one");
    const html = withClient(createElement(PopoutApp), [rowOf("someone-else")]);
    expect(html).toContain("“gone-one” was closed.");
    expect(html).toContain(">Close</button>");
    expect(html).toContain('id="popout-bar"');
    expect(html).not.toContain("pane-embedded");
  });

  it("before the list has loaded it is not gone — just loading", () => {
    inPopout("fix-login");
    const html = withClient(createElement(PopoutApp));
    expect(html).toContain("Loading…");
    expect(html).not.toContain("was closed");
  });

  it("room for the shell's window buttons or the macOS lights, when asked", () => {
    const view = (o: { macLights?: boolean; shellButtons?: boolean }) =>
      withClient(createElement(PopoutView, { title: "t", name: "t", gone: true, ...o }));
    expect(view({ shellButtons: true })).toContain("data-shell-buttons");
    expect(view({ macLights: true })).toContain("data-mac-lights");
    expect(view({})).not.toMatch(/data-(mac-lights|shell-buttons)/);
    const sheet = css("popout/PopoutApp.css");
    expect(sheet).toContain("#popout-bar[data-shell-buttons]");
    expect(sheet).toMatch(/#popout-bar \{[^}]*-webkit-app-region: drag/);
    expect(sheet).toMatch(/#popout-bar \.popout-grip \{[^}]*cursor: grab/);
    expect(sheet).toMatch(/#popout-bar button,\s*#popout-bar \.popout-grip \{\s*-webkit-app-region: no-drag/);
  });

  it("the drag handle carries the session to the main window's drop target", () => {
    const src = code("popout/PopoutApp.tsx");
    expect(src).toContain("e.dataTransfer.setData(SESSION_DRAG_MIME, title);");
    expect(src).toContain('e.dataTransfer.setData("text/plain", title);');
    expect(src).toContain('e.dataTransfer.effectAllowed = "move";');
  });

  it("a rename keeps the rest of the URL", () => {
    expect(searchWithTitle("?popout=old&x=1", "new name")).toBe("?popout=new+name&x=1");
    expect(popout.popoutTitle(searchWithTitle("?popout=old", "a+b&c"))).toBe("a+b&c");
  });

  it("only this session's dialogs open here", () => {
    expect([...POPOUT_DIALOGS].sort()).toEqual(["commit", "make-pr", "move"]);
  });
});
