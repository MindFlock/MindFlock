/** The Sky's weather: clouds by the plan window (one bank past 60 %, a thicker
 * one past 85 %), night in the notification quiet hours (wrapping midnight),
 * the title bar's word, what it reads from /api/usage and the settings doc,
 * the clouds' drift (in from the left, out to the right, still under reduced
 * motion), and the canvas staying out of the way of every click. */

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { css, src } from "./sourceText";

const { CLEAR, cloudCount, fitClouds, inQuietHours, quietHours, seedClouds, stepClouds, weatherFor, windowUsagePct } = await import("../lib/weather");
const { WeatherLabel, WeatherWord } = await import("../components/flights/SkyFlock");

/** A seeded random, so a sky is the same every run. */
function rng(seed = 7) {
  let s = seed;
  return () => {
    s = (s * 16807) % 2147483647;
    return (s - 1) / 2147483646;
  };
}

const NIGHT = { from: 22, to: 7 };

describe("weatherFor", () => {
  it("clouds drift in past 60 % and thicken past 85 %", () => {
    const at = (usagePct: number | null) => weatherFor({ usagePct, hour: 12, quiet: null }).clouds;
    expect([at(null), at(0), at(60), at(60.1), at(85), at(85.1), at(100)]).toEqual([0, 0, 0, 1, 1, 2, 2]);
    expect(at(Number.NaN)).toBe(0);
  });

  it("is night in the quiet hours, wrapping midnight", () => {
    const night = (hour: number, quiet = NIGHT as { from: number; to: number } | null) => weatherFor({ usagePct: null, hour, quiet }).night;
    expect([night(21.9), night(22), night(23.5), night(0), night(6.99), night(7), night(12)]).toEqual([false, true, true, true, true, false, false]);
    // A window inside one day.
    expect([night(12, { from: 13, to: 14 }), night(13.5, { from: 13, to: 14 }), night(14, { from: 13, to: 14 })]).toEqual([false, true, false]);
    // No quiet hours set, or an empty window: never night.
    expect(night(23, null)).toBe(false);
    expect(inQuietHours(5, { from: 5, to: 5 })).toBe(false);
  });

  it("names the reason in one word: usage first, then quiet hours, else nothing", () => {
    expect(weatherFor({ usagePct: 87.4, hour: 12, quiet: NIGHT }).word).toBe("usage 87 %");
    expect(weatherFor({ usagePct: 87.4, hour: 23, quiet: NIGHT })).toEqual({ clouds: 2, night: true, word: "usage 87 %", drift: true });
    expect(weatherFor({ usagePct: 40, hour: 23, quiet: NIGHT }).word).toBe("quiet hours");
    expect(weatherFor({ usagePct: 40, hour: 12, quiet: NIGHT })).toEqual(CLEAR);
  });

  it("never drifts under prefers-reduced-motion (the clouds stay, still)", () => {
    const w = weatherFor({ usagePct: 70, hour: 12, quiet: null, reducedMotion: true });
    expect([w.clouds, w.drift]).toEqual([1, false]);
    expect(weatherFor({ usagePct: 70, hour: 12, quiet: null }).drift).toBe(true);
  });
});

describe("reading the server", () => {
  const usage = (...providers: unknown[]) => ({ providers });

  it("takes the fullest live plan window from /api/usage (the top bar's estimate)", () => {
    const end = 1_900_000_000;
    expect(windowUsagePct(usage({ name: "claude", mode: "windowed", window: { end, percent_used: 64 } }, { name: "codex", mode: "windowed", window: { end, percent_used: 91 } }))).toBe(91);
    // Metered providers, idle windows (no end) and missing numbers don't count.
    expect(windowUsagePct(usage({ mode: "metered", window: { end, percent_used: 99 } }, { mode: "windowed", window: { end: null, percent_used: 99 } }, { mode: "windowed", window: { end } }))).toBeNull();
    expect(windowUsagePct(null)).toBeNull();
    expect(windowUsagePct({})).toBeNull();
  });

  it("reads notifications.quiet_hours as hours or HH:MM, else none", () => {
    expect(quietHours({ notifications: { quiet_hours: { from: 22, to: 7 } } })).toEqual({ from: 22, to: 7 });
    expect(quietHours({ notifications: { quiet_hours: { from: "22:30", to: "06:45" } } })).toEqual({ from: 22.5, to: 6.75 });
    expect(quietHours({ notifications: { quiet_hours: { from: "25:00", to: 7 } } })).toBeNull();
    expect(quietHours({ notifications: { quiet_hours: { from: 7, to: 7 } } })).toBeNull();
    expect(quietHours({ notifications: {} })).toBeNull();
    expect(quietHours(undefined)).toBeNull();
  });

  it("reuses the top bar's usage query and the settings doc — no new endpoint", () => {
    const hook = src("components/flights/useWeather.ts");
    expect(hook).toMatch(/useUsage\(\)/);
    expect(hook).toMatch(/useSettingsDoc\(/);
    expect(hook).not.toMatch(/api\(|fetch\(/);
  });
});

describe("the clouds", () => {
  it("a bank is 3 clouds, a storm 6", () => {
    expect([cloudCount(0), cloudCount(1), cloudCount(2)]).toEqual([0, 3, 6]);
  });

  it("drift in from the left, wrap round, and leave off the right when the weather clears", () => {
    const r = rng();
    let bank = fitClouds([], 3, 800, 600, true, r);
    expect(bank).toHaveLength(3);
    // Waiting off the left edge, so they drift in rather than appear.
    for (const c of bank) expect(c.x + c.span).toBeLessThanOrEqual(0);
    // Drifting rightwards, never fast.
    const x0 = bank.map((c) => c.x);
    bank = stepClouds(bank, 800);
    bank.forEach((c, i) => expect(c.x - x0[i]).toBeGreaterThan(0));
    bank.forEach((c, i) => expect(c.x - x0[i]).toBeLessThan(0.2));
    // A staying cloud past the right edge comes round again.
    bank[0].x = 800 + bank[0].span + 0.01;
    bank = stepClouds(bank, 800);
    expect(bank[0].x).toBe(-bank[0].span);
    // The weather clears to one bank of… none: they are leaving, not gone.
    bank = fitClouds(bank, 0, 800, 600, true, r);
    expect(bank).toHaveLength(3);
    expect(bank.every((c) => c.leaving)).toBe(true);
    bank[1].x = 800 + bank[1].span;
    bank = stepClouds(bank, 800);
    expect(bank).toHaveLength(2);
    // Thickening again keeps who is there and adds the rest.
    bank = fitClouds(bank, 6, 800, 600, true, r);
    expect(bank.filter((c) => !c.leaving)).toHaveLength(6);
  });

  it("without drift (reduced motion) are simply there, in the sky, or simply gone", () => {
    const r = rng(3);
    let bank = fitClouds([], 6, 800, 600, false, r);
    for (const c of bank) expect(c.x).toBeGreaterThanOrEqual(0);
    for (const c of bank) expect(c.x).toBeLessThanOrEqual(800);
    bank = fitClouds(bank, 3, 800, 600, false, r);
    expect(bank).toHaveLength(3);
    expect(fitClouds(bank, 0, 800, 600, false, r)).toEqual([]);
  });

  it("a resize keeps the bank (same count = same clouds)", () => {
    const bank = seedClouds(3, 800, 600, false, rng(5));
    expect(fitClouds(bank, 3, 400, 300, true)).toEqual(bank);
  });
});

describe("the sky and the word", () => {
  it("paints the clouds before (behind) the birds and dims with a tint under the flock", () => {
    const sky = src("components/flights/SkyFlock.tsx");
    expect(sky).toMatch(/paintClouds\(g, clouds, thick, light\);\s*for \(const b of birds\)/);
    expect(sky.indexOf('className="sky-night"')).toBeLessThan(sky.indexOf('className="sky-flock"'));
    const style = css("components/flights/SkyFlock.css");
    expect(style).toMatch(/\.sky-night \{[^}]*pointer-events: none/);
    expect(style).toMatch(/\.sky-flock \{[^}]*pointer-events: none/);
    // The dusk fades in, except under reduced motion.
    expect(style).toMatch(/@media \(prefers-reduced-motion: reduce\) \{\s*\.sky-night \{\s*animation: none/);
  });

  it("the word: ☁ for usage (attention past 85 %), ☾ for quiet hours, nothing in clear weather", () => {
    const html = (usagePct: number, hour: number) => renderToStaticMarkup(createElement(WeatherLabel, { weather: weatherFor({ usagePct, hour, quiet: NIGHT }) }));
    expect(html(87, 12)).toContain('class="sky-weather-word is-storm"');
    expect(html(87, 12)).toContain("☁</span> usage 87 %");
    expect(html(70, 12)).toContain('class="sky-weather-word is-cloudy"');
    expect(html(10, 23)).toContain("☾</span> quiet hours");
    expect(html(10, 12)).toBe("");
    // Outside the app's query client (a static render) it says nothing.
    expect(renderToStaticMarkup(createElement(WeatherWord))).toBe("");
  });
});
