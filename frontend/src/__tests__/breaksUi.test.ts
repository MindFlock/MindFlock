/** Take a break, the idle flock, and "a duplicate lands under its source"
 * (moved from tests/unit/test_frontend_breaks.py, which grepped the built
 * bundle; the bird.png route and the docs check stay in Python).
 *
 * Three features whose whole point is *where and when something appears on
 * screen*, which a unit test can't watch. What it can pin:
 *
 * - the break screen renders both of its buttons (a break screen with only one
 *   way out is a trap, not a reminder);
 * - the break clock only runs while the app does (the heartbeat + the gap rule,
 *   including the mid-run check that catches a laptop that slept open);
 * - every global shortcut stands down behind the card, and the card counts as
 *   a modal for Ctrl+W / Delete;
 * - the flock streams out of the logo and folds back into it;
 * - the idle overlay is explicitly not a surface — fixed, full-bleed and
 *   `pointer-events: none`, or it would eat every click the moment somebody
 *   stopped typing — and only deliberate input (never a mousemove, never the
 *   window regaining focus) dismisses it;
 * - the sidebar order helper that puts a copy beneath its source is wired into
 *   the duplicate action, not merely exported. */

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, describe, expect, it, vi } from "vitest";
import BREAKS_SRC from "../components/breaks/Breaks.tsx?raw";
import BREAKS_CSS from "../components/breaks/breaks.css?raw";
// The comfort rows (Take a break, Idle flock, Reduce motion) moved from
// General to Appearance in the round-2 Settings pass.
import APPEARANCE_SRC from "../components/settings/screens/Appearance.tsx?raw";
import TOPBAR_SRC from "../components/TopBar.tsx?raw";
import BREAK_TIMER_SRC from "../lib/breakTimer.ts?raw";
import FLOCK_SRC from "../lib/flock.ts?raw";
import KEYMAP_SRC from "../lib/keymap.ts?raw";
import SESSION_ACTIONS_SRC from "../lib/sessionActions.ts?raw";
import { AWAY_GAP_MS, BREAK_SEEN_KEY, wasReload } from "../lib/breakTimer";
import { EMERGE_MS, HATCH_FLIGHT_MS } from "../lib/flock";
import { ACTIVITY } from "../components/breaks/useIdle";
import { useUi } from "../state/store";
import { SRC, code, squash, src } from "./sourceText";

const { BreakScreen, SEEN_EVERY_MS } = await import("../components/breaks/Breaks");

/** A top-level function's text, from its header to the closing brace at
 * column 0. */
function fnText(src: string, header: string): string {
  const at = src.indexOf(header);
  expect(at, header).toBeGreaterThanOrEqual(0);
  const end = src.indexOf("\n}\n", at);
  expect(end, header).toBeGreaterThan(at);
  return src.slice(at, end + 2);
}

/** The declarations of the rule whose selector is exactly `sel`, at the start
 * of a line, whitespace-collapsed. */
function cssRule(css: string, sel: string): string {
  const head = "\n" + sel + " {";
  const at = css.indexOf(head);
  expect(at, sel).toBeGreaterThanOrEqual(0);
  const end = css.indexOf("\n}", at + head.length);
  return css.slice(at + head.length, end).replace(/\s+/g, " ");
}

describe("Take a break", () => {
  it("renders the break screen with both of its answers", () => {
    // The card measures the logo while rendering (its flock emerges from it).
    const g = globalThis as Record<string, unknown>;
    const prev = g.document;
    g.document = { getElementById: () => null };
    let html = "";
    try {
      html = renderToStaticMarkup(
        createElement(BreakScreen, {
          away: 0,
          leaving: false,
          flockRef: { current: null },
          onSnooze: () => {},
          onResume: () => {},
        })
      );
    } finally {
      if (prev === undefined) delete g.document;
      else g.document = prev;
    }
    expect(html).toContain('id="break-screen"');
    // Snooze pushes it back; "Resume Working" restarts the whole interval.
    expect(html).toContain("Snooze 5 min");
    expect(html).toContain("Resume Working");
  });

  it("puts the row in Appearance, as the sentence it configures, persisted under two keys", () => {
    expect(APPEARANCE_SRC).toContain("Take a break");
    expect(APPEARANCE_SRC).toContain("Reminder to take a break every");
    const store = squash(src("state/store.ts"));
    expect(store).toContain('load<boolean>("mf_break_on", false)');
    expect(store).toContain('load<number>("mf_break_every", BREAK_DEFAULT_MINUTES)');
    expect(store).toContain('save("mf_break_on", on)');
    expect(store).toContain('save("mf_break_every", every)');
  });

  it("only runs the break clock while the app does — the tick catches a sleeping machine", () => {
    // A deadline is wall-clock, so on its own it counts through a shut-down
    // machine; the heartbeat tells an absence from a refresh.
    expect(BREAK_SEEN_KEY).toBe("mf_break_seen");
    expect(BREAK_TIMER_SRC).toContain("export function wasAway(");
    // Mid-run, not only at mount: the 1 Hz tick re-arms and takes a stale card
    // down — the only thing that notices a laptop that slept with the window open.
    const breaks = fnText(BREAKS_SRC, "export function Breaks(");
    const tick = breaks.slice(breaks.indexOf("window.setInterval("));
    expect(tick).toContain("if (wasAway(t, lastSeen.current)) {");
  });

  it("tells an open (fresh interval) from a refresh (keeps the deadline) by navigation type", () => {
    expect(BREAK_TIMER_SRC).toContain('performance.getEntriesByType("navigation")');
    const g = globalThis as Record<string, unknown>;
    const prev = g.performance;
    const nav = (type: string) =>
      Object.defineProperty(globalThis, "performance", {
        configurable: true,
        value: { getEntriesByType: (k: string) => (k === "navigation" ? [{ type }] : []) },
      });
    try {
      nav("reload");
      expect(wasReload()).toBe(true);
      nav("navigate");
      expect(wasReload()).toBe(false);
    } finally {
      Object.defineProperty(globalThis, "performance", { configurable: true, value: prev });
    }
  });

  it("writes the heartbeat coarsely and measures the away gap in minutes", () => {
    // The stamp is a localStorage (disk) write, so 1 Hz buys nothing.
    expect(SEEN_EVERY_MS).toBe(15_000);
    // Above a hidden tab's throttled interval (~1 tick/min), below a real absence.
    expect(AWAY_GAP_MS).toBeGreaterThanOrEqual(2 * 60_000);
    expect(AWAY_GAP_MS).toBeLessThanOrEqual(15 * 60_000);
    // Frequent enough that the gap is never an artefact of the writer's cadence.
    expect(SEEN_EVERY_MS).toBeLessThan(AWAY_GAP_MS / 4);
  });

  it("says the same thing in the Settings copy and on the button", () => {
    // A rename that lands in only one of the two describes a control that no
    // longer exists.
    expect(APPEARANCE_SRC).toContain("Resume Working");
    for (const [path, text] of Object.entries(SRC)) {
      expect(text.includes("Resumed work"), path).toBe(false);
    }
  });

  it("binds Escape on the document (capture), not on the overlay", () => {
    // A React onKeyDown only fires while focus is inside the card; one click on
    // the scrim drops focus to <body>.
    const screen = fnText(BREAKS_SRC, "export function BreakScreen(");
    const effect = screen.slice(screen.indexOf("const onKey = (e: KeyboardEvent) =>"));
    expect(effect).toContain("resumeNow.current();");
    expect(effect).toContain('document.addEventListener("keydown", onKey, true)');
  });
});

describe("the keymap stands down behind the break card", () => {
  const g = globalThis as Record<string, unknown>;
  const prevDoc = g.document;
  afterEach(() => {
    if (prevDoc === undefined) delete g.document;
    else g.document = prevDoc;
  });

  /** A document holding (or not) a #break-screen with the given classes. */
  function fakeDoc(classes: string[] | null) {
    const listeners: Array<(e: KeyboardEvent) => void> = [];
    const el = {
      hidden: false,
      hasAttribute: () => false,
      closest: () => null,
      classList: { contains: (c: string) => classes?.includes(c) ?? false },
    };
    g.document = {
      activeElement: null,
      body: {},
      documentElement: {},
      querySelectorAll: () => [],
      getElementById: (id: string) => (id === "break-screen" && classes ? el : null),
      addEventListener: (_t: string, fn: (e: KeyboardEvent) => void) => listeners.push(fn),
      removeEventListener: () => {},
    };
    return listeners;
  }

  it("counts the break card as a modal for Ctrl+W / Delete", async () => {
    const { overlayOpen } = await import("../lib/keymap");
    useUi.setState({ openDialog: null });
    fakeDoc(null);
    expect(overlayOpen()).toBe(false);
    fakeDoc(["modal", "break-screen"]);
    expect(overlayOpen()).toBe(true);
    expect(KEYMAP_SRC).toMatch(/const POPOVER_IDS = \[[^\]]*"break-screen"/);
  });

  it("never dispatches a global shortcut while the card is up (but does while it leaves)", async () => {
    const { installKeymap } = await import("../lib/keymap");
    const run = (classes: string[] | null) => {
      const listeners = fakeDoc(classes);
      const host = {
        togglePalette: vi.fn(),
        toggleShortcuts: vi.fn(),
        focusFilter: vi.fn(),
        cycleWindow: vi.fn(),
        rowAt: () => null,
        openDoctor: vi.fn(),
      };
      const off = installKeymap(host);
      const e = {
        key: "p",
        ctrlKey: true,
        metaKey: false,
        shiftKey: false,
        altKey: false,
        preventDefault: vi.fn(),
        stopPropagation: vi.fn(),
      };
      listeners[0](e as unknown as KeyboardEvent);
      off();
      return { opened: host.togglePalette.mock.calls.length, prevented: e.preventDefault.mock.calls.length };
    };
    // Ctrl+P under an opaque card would open a palette that swallows every key.
    expect(run(["modal", "break-screen"])).toEqual({ opened: 0, prevented: 0 });
    // The leaving state is clickable again, so it is typeable again too.
    expect(run(["modal", "break-screen", "break-leaving"])).toEqual({ opened: 1, prevented: 1 });
    expect(run(null)).toEqual({ opened: 1, prevented: 1 });
    expect(fnText(KEYMAP_SRC, "function _dispatch(")).toContain("if (breakScreenUp()) return;");
  });
});

describe("the flock is a round trip through the logo", () => {
  it("streams out of the mark, and both surfaces measure the mark", () => {
    expect(FLOCK_SRC).toContain("beginHatch(opts.emergeFrom.x, opts.emergeFrom.y);");
    // BOTH surfaces arm it, measured off the real element rather than assumed.
    expect(BREAKS_SRC.split("emergeFrom: logoPoint()").length - 1).toBe(2);
    expect(BREAKS_SRC).toContain('document.getElementById("brand-logo")');
    expect(TOPBAR_SRC).toContain('id="brand-logo"');
  });

  it("is a slow stream of short flights, not one long interpolation", () => {
    // The window is long; each bird's own flight inside it is short, and launches
    // spread across the window minus one flight so the last lands on EMERGE_MS.
    expect(EMERGE_MS).toBe(20_000);
    expect(HATCH_FLIGHT_MS).toBeLessThanOrEqual(5_000);
    expect(FLOCK_SRC).toContain("const launchWindow = Math.max(0, EMERGE_MS - HATCH_FLIGHT_MS);");
    // step() and hatchStep() run in the SAME frame, on different birds.
    expect(FLOCK_SRC).toContain("if (hatchFrom) hatchStep(now);");
    expect(FLOCK_SRC).toContain("if (b.hatchAt) continue;");
  });

  it("flies home to the logo on dismissal, and the leaving overlay lets the pointer through", () => {
    expect(BREAKS_SRC).toContain("gather(home.x, home.y)");
    // The overlay outlives the click that dismissed it by the length of the flight.
    expect(cssRule(BREAKS_CSS, ".modal.break-screen.break-leaving")).toContain("pointer-events: none;");
  });

  it("keeps your view under the break card — no scrim — and paints the card above the canvas", () => {
    const screen = cssRule(BREAKS_CSS, ".modal.break-screen");
    expect(screen).toContain("z-index");
    expect(screen).toContain("background: transparent;");
    // Anchored on the rule itself, not the leaving state's `.break-card`.
    expect(cssRule(BREAKS_CSS, ".break-card")).toContain("position: relative;");
  });

  it("draws the sprite from /bird.png", () => {
    expect(FLOCK_SRC).toContain('const SPRITE_URL = "/bird.png";');
  });
});

describe("the idle flock", () => {
  it("covers the window without capturing it", () => {
    const rule = cssRule(BREAKS_CSS, ".idle-flock");
    expect(rule).toContain("position: fixed;");
    expect(rule).toContain("inset: 0;");
    expect(rule).toContain("pointer-events: none;");
  });

  it("is dismissed by deliberate input only — never a hovering cursor or a focus change", () => {
    for (const evt of ["pointerdown", "keydown", "touchstart", "wheel"]) expect(ACTIVITY).toContain(evt);
    expect(ACTIVITY).not.toContain("pointermove");
    expect(ACTIVITY).not.toContain("mousemove");
    expect(ACTIVITY).not.toContain("focus");
    // The hook's own effect must not re-arm on a visibility change either.
    expect(code("components/breaks/useIdle.ts")).not.toContain("visibilitychange");
  });

  it("is a switch that ships on, with a delay", () => {
    expect(APPEARANCE_SRC).toContain("Idle flock");
    expect(APPEARANCE_SRC).toContain("Fly the flock over your grid after");
    const store = squash(src("state/store.ts"));
    expect(store).toContain('load<boolean>("mf_idle_flock", true)');
    expect(store).toContain('load<number>("mf_idle_after", IDLE_DEFAULT_MINUTES)');
    expect(useUi.getState().idleFlock).toBe(true);
  });

  it("stands down for exactly two things: the switch and the break card", () => {
    expect(BREAKS_SRC).toContain("useIdle(flockAfter * 60_000, flockOn && !onBreak)");
  });

  it("ships no test scaffolding", () => {
    for (const [path, text] of Object.entries(SRC)) {
      expect(text.includes("Show the birds now"), path).toBe(false);
      expect(text.includes("idleFlockNow"), path).toBe(false);
    }
  });
});

describe("a duplicate lands under its source", () => {
  it("places the copy after its source, for the guess AND the server's title", () => {
    expect(SESSION_ACTIONS_SRC).toContain("const next = orderWithAfter(merged, title, after);");
    const copy = fnText(SESSION_ACTIONS_SRC, "export async function copySession(");
    expect(copy).toContain("placeAfter(guess, title);");
    expect(copy).toContain("placeAfter(inst.title, title);");
  });
});
