/** The Sky's two tools (docs/flights.md "As built: round 12 sky"): drag mode
 * (a line moves by its bar, empty sky draws the marquee) and click mode (a
 * drag anywhere pans, a click focuses or selects, Shift+drag selects).
 * `gestureFor` is the one decision; the tool is remembered per device
 * (`mf_sky_tool`); the toolbar carries the two-way toggle; V / H switch it and
 * Space held is click mode for as long as it is held. */

import { createElement, type ReactElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, describe, expect, it, vi } from "vitest";
import { code, css, cssRule } from "./sourceText";

const storage = new Map<string, string>();
vi.stubGlobal("localStorage", {
  getItem: (k: string) => (storage.has(k) ? storage.get(k)! : null),
  setItem: (k: string, v: string) => void storage.set(k, String(v)),
  removeItem: (k: string) => void storage.delete(k),
  clear: () => storage.clear(),
});
vi.mock("../api/client", () => ({ api: vi.fn(() => new Promise(() => {})), ApiError: class extends Error {} }));
vi.mock("../lib/toast", () => ({ toast: vi.fn() }));

const { QueryClient, QueryClientProvider } = await import("@tanstack/react-query");
const { useUi } = await import("../state/store");
const { SKY_TOOLS, SKY_TOOL_KEY, gestureFor, loadSkyTool, saveSkyTool } = await import("../lib/skySelect");
const { SkyView, skyTargetOf, skyObjectOf } = await import("../components/flights/SkyView");

(globalThis as Record<string, unknown>).document ??= {
  hidden: false,
  addEventListener: () => {},
  removeEventListener: () => {},
  getElementById: () => null,
  querySelector: () => null,
};

afterEach(() => storage.clear());

describe("gestureFor: what a press becomes", () => {
  it("drag mode is the sky as it was: the bar moves its line, empty sky draws the marquee, a box is its own", () => {
    expect(gestureFor("drag", "ground")).toBe("marquee");
    expect(gestureFor("drag", "ground", { shift: true })).toBe("marquee");
    expect(gestureFor("drag", "line")).toBe("move-line");
    expect(gestureFor("drag", "box")).toBe("select");
    // A touch drag on empty sky pans, as before.
    expect(gestureFor("drag", "ground", { touch: true })).toBe("pan");
  });

  it("click mode: a drag anywhere pans — empty sky, a line, a box — and Shift+drag is the marquee", () => {
    for (const t of ["ground", "line", "box"] as const) {
      expect(gestureFor("click", t), t).toBe("pan");
      expect(gestureFor("click", t, { shift: true }), t).toBe("marquee");
    }
    // A finger has no Shift: it pans.
    expect(gestureFor("click", "box", { shift: true, touch: true })).toBe("pan");
  });

  it("Space held pans from anywhere in either tool; a control keeps its press otherwise", () => {
    for (const tool of SKY_TOOLS) {
      for (const t of ["ground", "line", "box", "control"] as const) expect(gestureFor(tool, t, { space: true })).toBe("pan");
      expect(gestureFor(tool, "control")).toBe("none");
      expect(gestureFor(tool, "control", { shift: true })).toBe("none");
    }
  });
});

describe("the tool is remembered per device", () => {
  it("drag unless mf_sky_tool says click; a broken store still answers", () => {
    expect(SKY_TOOL_KEY).toBe("mf_sky_tool");
    expect(SKY_TOOLS).toEqual(["drag", "click"]);
    expect(loadSkyTool()).toBe("drag");
    saveSkyTool("click");
    expect(storage.get("mf_sky_tool")).toBe("click");
    expect(loadSkyTool()).toBe("click");
    storage.set("mf_sky_tool", "lasso");
    expect(loadSkyTool()).toBe("drag");
    const broken = {
      getItem: () => {
        throw new Error("blocked");
      },
      setItem: () => {
        throw new Error("blocked");
      },
    };
    expect(loadSkyTool(broken)).toBe("drag");
    expect(() => saveSkyTool("click", broken)).not.toThrow();
  });
});

/** A stand-in element: `closest(sel)` finds the first of `on` (the element
 * then its ancestors, as selectors it matches) that any part of `sel` names. */
function el(on: Array<{ sel: string; attrs?: Record<string, string> }>): Element {
  const find = (sel: string): Element | null => {
    const parts = sel.split(",").map((s) => s.trim());
    for (let i = 0; i < on.length; i++) {
      const o = on[i];
      if (parts.some((p) => o.sel.split(" ").includes(p))) return { getAttribute: (k: string) => o.attrs?.[k] ?? null, closest: (s: string) => el(on.slice(i)).closest(s) } as unknown as Element;
    }
    return null;
  };
  return { closest: find, getAttribute: (k: string) => on[0]?.attrs?.[k] ?? null } as unknown as Element;
}

describe("what a press landed on", () => {
  const ground = (e: Element) => (e as unknown as { ground?: boolean }).ground === true;
  const slot = { sel: ".sky-line-slot[data-slot]", attrs: { "data-slot": "sc-12" } };

  it("a box, a line's bar, the intake lane's head, a control, empty sky", () => {
    expect(skyTargetOf(null, ground)).toBe("ground");
    expect(skyTargetOf(Object.assign(el([]), { ground: true }), ground)).toBe("ground");
    expect(skyTargetOf(el([{ sel: "[data-node]", attrs: { "data-node": "stop:s1" } }, slot]), ground)).toBe("box");
    expect(skyTargetOf(el([{ sel: ".skl-bar" }, slot]), ground)).toBe("line");
    expect(skyTargetOf(el([{ sel: ".sky-lane-head" }, { sel: ".sky-intake" }]), ground)).toBe("line");
    expect(skyTargetOf(el([{ sel: "button" }, { sel: ".skl-bar" }, slot]), ground)).toBe("control");
    expect(skyTargetOf(el([{ sel: ".skl-card" }, slot]), ground)).toBe("control");
    // The rest of a line (its padding, the canvas's own space) is a box's.
    expect(skyTargetOf(el([{ sel: ".skl" }, slot]), ground)).toBe("box");
  });

  it("the sky object under it: a line's slot id, the intake lane, or nothing", () => {
    expect(skyObjectOf(el([{ sel: "[data-node]" }, slot]))).toBe("sc-12");
    expect(skyObjectOf(el([{ sel: ".sky-lane-head" }, { sel: ".sky-intake" }]))).toBe("intake");
    expect(skyObjectOf(el([{ sel: ".sky-plane" }]))).toBe("");
    expect(skyObjectOf(null)).toBe("");
  });
});

describe("the toggle on the toolbar", () => {
  const withClient = (el: ReactElement) => {
    const c = new QueryClient({ defaultOptions: { queries: { retry: false, enabled: false } } });
    c.setQueryData(["instances"], []);
    c.setQueryData(["flights"], []);
    c.setQueryData(["flight-plans"], []);
    c.setQueryData(["flight-kinds"], []);
    c.setQueryData(["settings"], {});
    return renderToStaticMarkup(createElement(QueryClientProvider, { client: c }, el));
  };
  const render = () => {
    useUi.setState({ view: "sky", skyMode: "mine", focused: null } as never);
    Object.assign(useUi.getInitialState(), useUi.getState());
    return withClient(createElement(SkyView));
  };
  const chips = (html: string) => [...html.matchAll(/<button type="button" role="radio" aria-checked="(true|false)" class="fl-pick( on)?"[^>]*data-value="(drag|click)"[^>]*>([^<]*)</g)].map((m) => [m[3], m[1], m[4]]);

  it("Select (V) · Pan (H) in the mode chips' tier, the remembered one on; .sky carries it as data-tool", () => {
    const html = render();
    expect(html).toContain('class="fl-chiprow sky-modes sky-tools" role="radiogroup" aria-label="Tool"');
    expect(chips(html)).toEqual([
      ["drag", "true", "Select (V)"],
      ["click", "false", "Pan (H)"],
    ]);
    expect(html).toContain('<main id="sky-view" class="sky" data-tool="drag"');
    storage.set("mf_sky_tool", "click");
    const again = render();
    expect(chips(again).map(([v, on]) => [v, on])).toEqual([
      ["drag", "false"],
      ["click", "true"],
    ]);
    expect(again).toContain('data-tool="click"');
  });

  it("V and H switch it, and Space held is click mode", () => {
    const sky = code("components/flights/SkyView.tsx");
    expect(sky).toMatch(/k === "v" \|\| k === "V"\) \{\s*handled\(\);\s*setTool\("drag"\);/);
    expect(sky).toMatch(/k === "h" \|\| k === "H"\) \{\s*handled\(\);\s*setTool\("click"\);/);
    expect(sky).toContain('const liveTool: SkyTool = spaceHeld ? "click" : tool;');
    // Lines never start a move of their own in click mode.
    // The lines, the grounded lines, the intake lane and the floating palette.
    expect(sky.match(/draggable=\{liveTool === "drag"\}/g)?.length).toBe(4);
  });

  it("click mode shows the hand, closed while it pans", () => {
    const sheet = css("components/flights/Sky.css");
    expect(sheet).toMatch(/\.sky\[data-tool="click"\] \.sky-viewport,[^{]*\.fl-box[^{]*\{\s*cursor: grab;/);
    expect(cssRule(sheet, '.sky[data-tool="click"] .sky-viewport.is-panning')).toContain("cursor: grabbing !important");
  });
});
