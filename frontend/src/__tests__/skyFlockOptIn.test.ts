/** The Sky's ambient flock is opt-in: off by default (a new `mf_sky_flock`
 * key — the old `mf_sky_birds`, saved when the default was on, is never
 * read), Settings → Appearance → Birds in the Sky turns it on, and with the
 * birds off the weather still shows — clouds on the canvas, the night tint —
 * while a clear, bird-less sky draws nothing and asks for no frame. */

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { QueryClientContext } from "@tanstack/react-query";
import { describe, expect, it, vi } from "vitest";
import type { Weather } from "../lib/weather";
import { code, src } from "./sourceText";

/** A Map-backed localStorage, installed BEFORE the store module loads, with
 * the OLD key saying "on" (as it would for anyone who saw the old default). */
const storage = new Map<string, string>([["mf_sky_birds", "true"]]);
vi.stubGlobal("localStorage", {
  getItem: (k: string) => (storage.has(k) ? storage.get(k)! : null),
  setItem: (k: string, v: string) => void storage.set(k, String(v)),
  removeItem: (k: string) => void storage.delete(k),
  clear: () => storage.clear(),
});
vi.mock("../api/client", () => ({ api: vi.fn(), ApiError: class extends Error {} }));
vi.mock("../lib/toast", () => ({ toast: vi.fn() }));

/** The live weather, steered by each test. */
let weather: Weather = { clouds: 0, night: false, word: "", drift: false };
vi.mock("../components/flights/useWeather", () => ({
  useWeather: () => weather,
  useReducedMotion: () => false,
}));

const { useUi } = await import("../state/store");
const { SkyFlock, skyMotion } = await import("../components/flights/SkyFlock");

/** SkyFlock under a query client (so it reads the mocked live weather). */
function sky(w: Weather, birds?: boolean): string {
  weather = w;
  const props = { width: 800, height: 600, zoom: 1, ...(birds === undefined ? {} : { birds }) };
  return renderToStaticMarkup(createElement(QueryClientContext.Provider, { value: {} as never }, createElement(SkyFlock, props)));
}
const CANVAS = '<canvas class="sky-flock" style="width:800px;height:600px" aria-hidden="true"></canvas>';
const NIGHT = '<div class="sky-night" style="width:800px;height:600px" aria-hidden="true"></div>';

describe("the setting", () => {
  it("is off by default, and an old mf_sky_birds=true is ignored", () => {
    expect(useUi.getState().skyBirds).toBe(false);
    expect(code("state/store.ts")).not.toContain('load<boolean>("mf_sky_birds"');
  });

  it("turning it on saves the new key, never the old one", () => {
    useUi.getState().setSkyBirds(true);
    expect(useUi.getState().skyBirds).toBe(true);
    expect(storage.get("mf_sky_flock")).toBe("true");
    useUi.getState().setSkyBirds(false);
    expect(storage.get("mf_sky_flock")).toBe("false");
    expect(storage.get("mf_sky_birds")).toBe("true");
  });

  it("Settings says so, under the same label", () => {
    const row = src("components/settings/screens/Appearance.tsx");
    expect(row).toContain('<span className="set-label">Birds in the Sky</span>');
    expect(row).toContain("Off by default; the birds on your routes always stay.");
  });

  it("the Sky always mounts SkyFlock and hands it the switch", () => {
    const view = code("components/flights/SkyView.tsx");
    expect(view).not.toContain("skyBirds && <SkyFlock");
    expect(view).toMatch(/<SkyFlock [^>]*birds=\{skyBirds\}/);
  });
});

describe("what the sky draws", () => {
  it("birds on: they fly, on a canvas, every frame", () => {
    expect(skyMotion({ birds: true, reduced: false, clouds: 0, drift: false })).toEqual({ flying: true, canvas: true, loop: true });
  });

  it("birds off, clear: nothing — no canvas, no rAF", () => {
    expect(skyMotion({ birds: false, reduced: false, clouds: 0, drift: false })).toEqual({ flying: false, canvas: false, loop: false });
  });

  it("birds off, clouds drifting: the canvas and a rAF for the clouds alone", () => {
    expect(skyMotion({ birds: false, reduced: false, clouds: 2, drift: true })).toEqual({ flying: false, canvas: true, loop: true });
  });

  it("birds off, still clouds (reduced motion): a canvas painted once, no rAF", () => {
    expect(skyMotion({ birds: false, reduced: true, clouds: 1, drift: false })).toEqual({ flying: false, canvas: true, loop: false });
    // Reduced motion drops the birds even when they are on.
    expect(skyMotion({ birds: true, reduced: true, clouds: 0, drift: false })).toEqual({ flying: false, canvas: false, loop: false });
  });

  it("the frame loop steps birds only while they fly", () => {
    const flock = src("components/flights/SkyFlock.tsx");
    expect(flock).toContain("if (flying) stepBirds(birds.current, v.width, v.height);");
    expect(flock).toContain("birds.current = !flying ? [] : fitBirds(");
    expect(flock).toMatch(/useEffect\(\(\) => \{\s*if \(!loop\) return;/);
  });
});

describe("the rendered sky with the birds off", () => {
  it("clear weather renders nothing", () => {
    expect(sky({ clouds: 0, night: false, word: "", drift: false }, false)).toBe("");
  });

  it("clouds still get their canvas", () => {
    expect(sky({ clouds: 1, night: false, word: "usage 70 %", drift: true }, false)).toBe(CANVAS);
  });

  it("the night tint still shows, with no canvas when the sky is clear", () => {
    expect(sky({ clouds: 0, night: true, word: "quiet hours", drift: false }, false)).toBe(NIGHT);
    expect(sky({ clouds: 2, night: true, word: "usage 90 %", drift: true }, false)).toBe(NIGHT + CANVAS);
  });

  it("birds on (the prop's default) keeps the canvas in clear weather", () => {
    expect(sky({ clouds: 0, night: false, word: "", drift: false })).toBe(CANVAS);
  });
});
