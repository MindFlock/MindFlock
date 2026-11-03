/** The landing strip (components/flights/LandingStrip.tsx; docs/flights.md
 * "As built: round 15 objects"): its words, the rows it shows per repo, the
 * fold and expand, the head it drags by, the corner, the measure — and the
 * route it reads (api/skyApi). Server-rendered, with the handlers pinned in
 * the source where the node environment can't press them. */

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import { code, css as cssOf } from "./sourceText";
import type { LandingData, LandingRepo } from "../api/skyApi";

vi.mock("../api/client", () => ({ api: vi.fn(() => new Promise(() => {})), instApi: vi.fn(() => new Promise(() => {})), ApiError: class extends Error {} }));
vi.mock("../lib/toast", () => ({ toast: vi.fn() }));

const { LandingStrip, LANDED_FOLDED, checksWords, mergeWords, shownLanded, landedCount } = await import("../components/flights/LandingStrip");
const { useUi } = await import("../state/store");

const NOW = 1_000_000;
const repo = (over: Partial<LandingRepo> = {}): LandingRepo => ({
  root: "/home/me/app",
  name: "app",
  branch: "main",
  head: "0123456789abcdef",
  checks: "green",
  last_merge_at: NOW - 7200,
  landed: [
    { flight: "f1", title: "fix-login", pr_url: "https://github.com/o/r/pull/12", at: NOW - 600 },
    { flight: "f2", title: "bump-deps", pr_url: "", at: NOW - 3600 },
    { flight: "f3", title: "sc-9", pr_url: "https://github.com/o/r/pull/9", at: NOW - 100 },
    { flight: "f4", title: "sc-8", pr_url: "", at: NOW - 9000 },
  ],
  ...over,
});
const DATA: LandingData = { repos: [repo(), repo({ root: "/home/me/site", name: "site", branch: "master", checks: "red", last_merge_at: null, landed: [] })], at: NOW };

const render = (props: Record<string, unknown> = {}) =>
  renderToStaticMarkup(createElement(LandingStrip, { data: DATA, now: NOW, onHeadDown() {}, onResize() {}, onToggleOpen() {}, ...props }));

describe("the words", () => {
  it("checks, merges, counts", () => {
    expect(["green", "red", "pending", "none"].map((t) => checksWords(t as never))).toEqual(["checks green", "checks red", "checks running", "no checks"]);
    // The server's time is a merge commit's, or — none yet (a fresh repo) —
    // the last commit's: the words claim a merge only when it says so.
    expect(mergeWords(NOW - 7200, NOW, "merge")).toBe("merged 2h ago");
    expect(mergeWords(NOW - 600, NOW, "commit")).toBe("last commit 10m ago");
    expect(mergeWords(NOW - 600, NOW)).toBe("updated 10m ago");
    expect(mergeWords(null)).toBe("no merges yet");
    expect(landedCount(DATA)).toBe(4);
    expect(landedCount(null)).toBe(0);
  });

  it("folded shows the newest three landed rows, expanded every one", () => {
    expect(LANDED_FOLDED).toBe(3);
    const folded = shownLanded(repo(), false);
    expect(folded.shown.map((l) => l.flight)).toEqual(["f3", "f1", "f2"]);
    expect(folded.more).toBe(1);
    expect(shownLanded(repo(), true).shown.map((l) => l.flight)).toEqual(["f3", "f1", "f2", "f4"]);
  });
});

describe("the strip", () => {
  it("one block per repo: the dot, the name, the branch, the merge's age, the landed rows with PR links", () => {
    useUi.setState({ aliases: { "fix-login": "Login fix" } });
    const html = render();
    expect(html).toMatch(/^<nav class="sky-lane sky-landing" aria-label="Landing">/);
    expect(html).toContain('<span class="sky-lane-title">Landing</span>');
    expect(html).toMatch(/<span class="fl-count" title="4 landed today">4<\/span>/);
    const repos = [...html.matchAll(/data-repo="([^"]+)"/g)].map((m) => m[1]);
    expect(repos).toEqual(["/home/me/app", "/home/me/site"]);
    expect(html).toContain('<span class="sky-landing-dot is-green" title="checks green · 0123456" aria-label="checks green"></span>');
    expect(html).toContain('<span class="sky-landing-dot is-red"');
    expect(html).toContain('<strong class="sky-landing-name" title="/home/me/app">app</strong><code class="sky-landing-branch">main</code>');
    const merge = (w: string) => `<div class="sky-landing-merge muted" title="The default branch&#x27;s last merge (or, with none yet, its last commit)">${w}</div>`;
    expect(html).toContain(merge("updated 2h ago"));
    expect(html).toContain(merge("no merges yet"));
    // A fresh repo never reads "merged …" (round-consolidate judge).
    const fresh = render({ data: { repos: [repo({ last_merge_at: NOW - 600, last_merge_kind: "commit", landed: [] } as never)], at: NOW } });
    expect(fresh).toContain(merge("last commit 10m ago"));
    expect(fresh).not.toContain("merged");
    expect(render({ data: { repos: [repo({ last_merge_kind: "merge" } as never)], at: NOW } })).toContain(merge("merged 2h ago"));
    // The session's NAME, newest first, three folded, the PR a link.
    const names = [...html.matchAll(/class="sky-landing-flight-name" title="([^"]+)">([^<]+)</g)].map((m) => m[1] + "=" + m[2]);
    expect(names).toEqual(["sc-9=sc-9", "fix-login=Login fix", "bump-deps=bump-deps"]);
    expect(html).toContain('<a class="sky-landing-pr" href="https://github.com/o/r/pull/12" target="_blank" rel="noreferrer" title="https://github.com/o/r/pull/12" data-no-drag="true">PR ↗</a>');
    expect(html).toContain('<li class="sky-landing-more muted">+1 more — expand ▸</li>');
    expect(html).not.toContain("sc-8");
    useUi.setState({ aliases: {} });
  });

  it("expanded: every landed row, the ▾ on the head", () => {
    const html = render({ open: true });
    expect(html).toContain('<nav class="sky-lane sky-landing" aria-label="Landing" data-open="">');
    expect(html).toContain("sc-8");
    expect(html).not.toContain("more — expand");
    expect(html).toContain('aria-expanded="true" aria-label="Fold the landing strip&#x27;s rows"');
    expect(render()).toContain('aria-expanded="false" aria-label="Expand the landing strip: every landed flight"');
  });

  it("the head drags it (when the host lets it), ‹ folds it to a rail with the count, the corner sizes it", () => {
    const html = render();
    expect(html).toContain('<div class="sky-lane-head" data-sky-head="true" title="Drag to move the landing strip · double-click to expand">');
    expect(render({ draggable: false })).toContain('<div class="sky-lane-head" data-sky-head="true" title="double-click to expand">');
    expect(html).toContain('<span class="sky-lane-resize skl-resize" role="separator" tabindex="0" data-no-drag="true" aria-label="Resize the landing strip"');
    expect(html).toContain('title="Fold the landing strip"');
    const src = code("components/flights/LandingStrip.tsx");
    expect(src).toContain("if (draggable && onHeadDown && headPress(e)) onHeadDown(e);");
    expect(src).toContain('closest("button, a, input, summary, [data-no-drag]")');
    expect(src).toContain("setLive({ w: Math.max(LANDING_MIN.w, Math.round(g.w + (e.clientX - g.sx) / z)), h: Math.max(LANDING_MIN.h, Math.round(g.h + (e.clientY - g.sy) / z)) });");
    expect(src).toContain("if (live && onResize) onResize(live.w, live.h);");
    expect(src).toContain("e.currentTarget.setPointerCapture(e.pointerId);");
    // The rail: ‹ remembered per device.
    expect(src).toContain('const FOLDED_KEY = "mf_sky_landing_folded";');
    expect(src).toContain('className="sky-lane sky-lane-rail sky-landing" aria-label="Landing" data-collapsed="true"');
    expect(src).toContain("const ro = new ResizeObserver(read);");
    expect(src).toContain("measureRef.current?.(w, h);");
  });

  it("the frame the host keeps, else its own size; nothing loaded / no repos / an error", () => {
    expect(render({ width: 400, height: 300 })).toContain('style="width:400px;height:300px"');
    expect(render({ data: null })).toContain("Looking at the repos…");
    expect(render({ data: { repos: [], at: NOW } })).toContain("No repo in the sky yet.");
    expect(render({ data: null, error: "MindFlock hit an error (details in the log)." })).toContain("MindFlock hit an error");
  });

  it("no native dialogs", () => {
    expect(code("components/flights/LandingStrip.tsx")).not.toMatch(/\bwindow\.(prompt|alert|confirm)\(/);
  });

  it("the CSS: the dot's tones, the left edge (it sits right of the lines)", () => {
    const c = cssOf("components/flights/LandingStrip.css");
    expect(c).toMatch(/\.sky-landing-dot\.is-green \{[^}]*background: var\(--ok/);
    expect(c).toMatch(/\.sky-landing-dot\.is-red \{[^}]*background: var\(--danger/);
    expect(c).toMatch(/\.sky-landing \{[^}]*border-left: 1px solid var\(--border\);/);
  });
});

describe("the route it reads", () => {
  it("GET /api/sky/landing → {repos, at}; the host polls it every minute, not in This session", () => {
    const api = code("api/skyApi.ts");
    expect(api).toContain('return api<LandingData>("/api/sky/landing");');
    const lib = code("lib/skyObjects.ts");
    expect(lib).toContain('return useQuery({ queryKey: ["sky-landing"], queryFn: fetchLanding, refetchInterval: 60_000, retry: false, enabled });');
    const v = code("components/flights/SkyView.tsx");
    expect(v).toContain('const landingQ = useLandingData(!narrow && mode !== "one");');
    expect(v).toContain("landing: landingQ.data || null,");
  });
});
