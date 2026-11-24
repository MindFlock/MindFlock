/// <reference types="vite/client" />
/** "Ask how something works": the place registry (lib/helpPlaces) — every
 * target's hook exists in a component, the Settings search is all in it, the
 * words find the right place without a model, and only registry ids from the
 * server become pointers (lib/helpAsk toAnswer) — plus the pop-up's answer
 * text and its entries (? Help's first row, the palette), never for a guest. */

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import HELP_MENU_SRC from "../components/sidebar/HelpMenu.tsx?raw";
import PALETTE_SRC from "../components/palette/CommandPalette.tsx?raw";
import ASK_SRC from "../components/helpAsk/AskHelp.tsx?raw";
import APP_SRC from "../App.tsx?raw";

vi.mock("../api/client", () => ({ api: vi.fn(async () => ({})), ApiError: Error }));
vi.mock("../lib/toast", () => ({ toast: vi.fn() }));

const storage = new Map<string, string>();
(globalThis as Record<string, unknown>).localStorage = {
  getItem: (k: string) => (storage.has(k) ? storage.get(k)! : null),
  setItem: (k: string, v: string) => void storage.set(k, String(v)),
  removeItem: (k: string) => void storage.delete(k),
  clear: () => storage.clear(),
};

const { HELP_PLACES, placeById, placesForModel, matchPlaces, pointersFor, isWhereQuestion, selectorHooks } = await import(
  "../lib/helpPlaces"
);
const { SETTINGS_SEARCH } = await import("../components/palette/settingsSearch");
const { selectorParts } = await import("../components/onboarding/CoachTour");
const { toAnswer, takeAskSlot, ASK_GAP_MS, STARTERS } = await import("../components/helpAsk/askApi");
const { AnswerText } = await import("../components/helpAsk/AskHelp");

const SOURCES = import.meta.glob(["../components/**/*.tsx", "../App.tsx"], {
  query: "?raw",
  import: "default",
  eager: true,
}) as Record<string, string>;
const ALL_SRC = Object.values(SOURCES);

const esc = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

/** Whether a component renders this hook: an id or class as a string token,
 * an attribute with its value (or the attribute plus the value as a string
 * literal in the same file — a value from a registry, `data-screen={s.key}`). */
function hookExists(h: { kind: "id" | "class" | "attr"; name: string; value?: string }): boolean {
  if (h.kind === "id") {
    // id="x", or a built one: id={"intake-tab-" + t.key} with the rest a literal
    const cut = h.name.split("-");
    const built = cut.slice(1).map((_, i) => [cut.slice(0, i + 1).join("-") + "-", cut.slice(i + 1).join("-")]);
    return ALL_SRC.some(
      (s) =>
        new RegExp(`id=["{]*"${esc(h.name)}"`).test(s) ||
        built.some(([pre, rest]) => s.includes(`id={"${pre}" +`) && new RegExp(`["']${esc(rest)}["']`).test(s))
    );
  }
  if (h.kind === "class") return ALL_SRC.some((s) => new RegExp(`["'\\s]${esc(h.name)}["'\\s]`).test(s));
  const v = h.value ?? "";
  return ALL_SRC.some(
    (s) => s.includes(`${h.name}="${v}"`) || (s.includes(`${h.name}=`) && new RegExp(`["']${esc(v)}["']`).test(s))
  );
}

describe("the place registry", () => {
  it("has unique, well-formed ids the server accepts, and says what each place is", () => {
    const ids = HELP_PLACES.map((p) => p.id);
    expect(new Set(ids).size).toBe(ids.length);
    expect(HELP_PLACES.length).toBeGreaterThanOrEqual(50);
    for (const p of HELP_PLACES) {
      expect(p.id, p.id).toMatch(/^[a-z0-9][a-z0-9._:-]*$/);
      expect(p.id.length, p.id).toBeLessThanOrEqual(64);
      expect(p.label.trim(), p.id).not.toBe("");
      expect(p.caption.trim(), p.id).not.toBe("");
      expect(p.caption, p.id).not.toContain("\n");
      expect(p.how.trim(), p.id).not.toBe("");
      expect(p.keywords.length, p.id).toBeGreaterThan(0);
      // the GLOSSARY the tours keep to
      expect(p.caption + " " + p.how, p.id).not.toMatch(/walkthrough|tutorial|orchestrator|\{|`/i);
    }
    for (const m of placesForModel()) expect(m.label.length).toBeLessThanOrEqual(90);
  });

  it("names a target for every place, as a selector list that parses", () => {
    for (const p of HELP_PLACES) {
      expect(p.target, p.id).not.toBe("");
      expect(selectorParts(p.target).length, p.id).toBeGreaterThan(0);
      expect((p.target.match(/"/g) || []).length % 2, p.target).toBe(0);
      expect((p.target.match(/\[/g) || []).length, p.target).toBe((p.target.match(/\]/g) || []).length);
    }
  });

  it("points only at hooks a component really renders — so a renamed id fails here, not in front of someone", () => {
    const missing: string[] = [];
    for (const p of HELP_PLACES)
      for (const h of selectorHooks(p.target)) if (!hookExists(h)) missing.push(`${p.id}: ${h.kind} ${h.name}${h.value ? "=" + h.value : ""}`);
    expect(missing).toEqual([]);
  });

  it("holds every Settings search entry, on its screen, with its words", () => {
    for (const e of SETTINGS_SEARCH) {
      const p = HELP_PLACES.find((x) => x.label === "Settings → " + e.label);
      expect(p, e.label).toBeDefined();
      expect(p!.reach).toEqual({ kind: "settings", screen: e.key, advanced: e.advanced });
      for (const t of e.terms) expect(p!.keywords, e.label).toContain(t);
      if (e.anchor) expect(p!.target).toBe("#settings-screens " + e.anchor);
    }
  });

  it("takes a toured surface's target from its tour", async () => {
    const { TOURS } = await import("../lib/tours");
    expect(placeById("intake.autostart")!.target).toBe(TOURS.intake.steps.find((s) => s.target.includes("intake-autostart"))!.target);
    // the step that rings the hook first — not the opening step that names the top-bar button first
    expect(placeById("schedule.new")!.target.split(",")[0]).toBe('[data-tour="schedule-new"]');
    // every place's own hook comes first: a top-bar fallback never wins over a dialog's control
    for (const p of HELP_PLACES)
      if (p.reach.kind === "dialog") expect(p.target.split(",")[0], p.id).not.toMatch(/-btn$/);
    expect(placeById("topbar.mic")!.target).toBe(TOURS.voice.steps[0].target);
  });
});

describe("matching a question to places, without a model", () => {
  const top = (q: string) => matchPlaces(q).map((p) => p.id);
  it.each([
    ["where do I change the theme", "topbar.theme"],
    ["how do I turn on dark mode", "topbar.theme"],
    ["how do I schedule a template", "topbar.schedule"],
    ["where is the bell", "topbar.bell"],
    ["how do I connect GitHub", "settings.connections.github"],
    ["how do I send feedback", "sidebar.help"],
    ["how do I reopen a closed session", "sidebar.recent"],
    ["where do my worktrees live", "settings.workspace.where-worktrees-live"],
    ["how do I hide the sidebar", "topbar.sidebar"],
    ["where are the ticket sources", "intake.sources"],
    ["how do I start a new session", "topbar.new"],
    ["where are shared files", "hub.panel"],
    ["how many sessions can run at once", "settings.coding.sessions-running-at-once"],
    ["where is the command palette", "topbar.palette"],
  ])("%s → %s", (q, id) => {
    expect(top(q), q).toContain(id);
  });

  it("finds nothing for a question that isn't about the app", () => {
    for (const q of ["how do I bake bread", "what is the capital of France", "write me a poem"]) expect(top(q), q).toEqual([]);
  });

  it("gives at most three", () => {
    for (const q of ["settings", "session", "how do I start"]) expect(matchPlaces(q).length).toBeLessThanOrEqual(3);
  });

  it("knows a where-question", () => {
    for (const q of ["Where is the bell?", "where's Intake", "where do I change the theme", "show me the schedule", "how do I find shared files"])
      expect(isWhereQuestion(q), q).toBe(true);
    for (const q of ["how do I schedule a template", "what does Goes as far as do", "why is my session idle"]) expect(isWhereQuestion(q), q).toBe(false);
  });
});

describe("pointers from the server are registry places, or nothing", () => {
  it("keeps the model's registry ids, in its order, once each", () => {
    expect(pointersFor("x", ["made.up", "topbar.bell", "topbar.bell", "sidebar.help"]).map((p) => p.id)).toEqual(["topbar.bell", "sidebar.help"]);
  });

  it("falls back to the words when the model named none of ours", () => {
    expect(pointersFor("where is the bell", ["javascript:alert(1)", 3, null]).map((p) => p.id)).toContain("topbar.bell");
    expect(pointersFor("where is the bell", undefined).map((p) => p.id)).toContain("topbar.bell");
  });

  it("toAnswer reads only strings and booleans, and resolves pointers through the registry", () => {
    const a = toAnswer("where is the bell", {
      text: "Click the bell.",
      found: true,
      source: { doc: "web-ui.md", heading: "Notifications", path: "Web UI › Notifications", excerpt: 5 },
      model: true,
      pointers: ["topbar.bell", "<img src=x onerror=alert(1)>", { id: "topbar.new" }],
      limited: "yes",
    });
    expect(a.found).toBe(true);
    expect(a.limited).toBe(false);
    expect(a.source).toEqual({ doc: "web-ui.md", heading: "Notifications", path: "Web UI › Notifications", excerpt: "" });
    expect(a.pointers).toEqual(["topbar.bell", "<img src=x onerror=alert(1)>"]);
    expect(a.places.map((p) => p.id)).toEqual(["topbar.bell"]);
    const junk = toAnswer("q", null);
    expect(junk).toMatchObject({ text: "", found: false, source: null, model: false, pointers: [], limited: false });
  });

  it("spaces questions: one asked inside the gap waits out the rest of it, never dropped", () => {
    const t = 10_000_000;
    expect(takeAskSlot(t)).toBe(0);
    expect(takeAskSlot(t + 200)).toBe(ASK_GAP_MS - 200);
    // the next one queues behind that one
    expect(takeAskSlot(t + 300)).toBe(2 * ASK_GAP_MS - 300);
    expect(takeAskSlot(t + 10 * ASK_GAP_MS)).toBe(0);
  });

  it("holding Enter sends nothing more while one is asked, nor the same question again", () => {
    expect(ASK_SRC).toMatch(/if \(!text \|\| phase\.kind === "asking"\) return;/);
    expect(ASK_SRC).toContain('phase.kind === "answer" && phase.answer.question.toLowerCase() === text.toLowerCase()');
  });
});

describe("the pop-up", () => {
  it("draws the answer as text: steps as a list, `code` as code, never HTML", () => {
    const html = renderToStaticMarkup(
      createElement(AnswerText, { text: "Open `Settings`.\n1. Click <b>Appearance</b>\n2) Pick a `Mode`\nDone." })
    );
    expect(html).toContain("<p>Open <code>Settings</code>.</p>");
    expect(html).toContain("<ol><li>Click &lt;b&gt;Appearance&lt;/b&gt;</li><li>Pick a <code>Mode</code></li></ol>");
    expect(html).toContain("<p>Done.</p>");
    expect(html).not.toContain("<b>");
  });

  it("a pointer is a one-step coach mark: no '1 of 1', no dots, no Back — just the card and Done", async () => {
    const { CoachTour } = await import("../components/onboarding/CoachTour");
    const one = renderToStaticMarkup(
      createElement(CoachTour, { steps: [{ target: "", title: "The bell", body: "What needs you." }], onClose: () => {}, skipLabel: "Close" })
    );
    expect(one).not.toContain("1 of 1");
    expect(one).not.toContain(">Back<");
    expect(one).not.toContain("<i ");
    expect(one).toContain(">Done<");
    expect(one).toContain(">Close<");
    const three = renderToStaticMarkup(
      createElement(CoachTour, {
        steps: [1, 2, 3].map((k) => ({ target: "", title: "T" + k, body: "B" })),
        onClose: () => {},
      })
    );
    expect(three).toContain("1 of 3");
    expect(three).toContain(">Back<");
  });

  it("offers a few starter questions", () => {
    expect(STARTERS.length).toBeGreaterThanOrEqual(3);
    expect(STARTERS.length).toBeLessThanOrEqual(5);
    for (const s of STARTERS) expect(s).toMatch(/\?$/);
  });

  it("points with the tours' coach mark and says once what answers", () => {
    expect(ASK_SRC).toContain('import { CoachTour } from "../onboarding/CoachTour"');
    expect(ASK_SRC).toContain("<CoachTour");
    expect(ASK_SRC).toContain("Answers come from MindFlock's own docs");
    expect(ASK_SRC).toContain("Send feedback…");
    // a guest gets nothing
    expect(ASK_SRC).toMatch(/if \(guest\) return null;/);
    expect(APP_SRC).toContain("<AskHelp />");
  });

  it("is ? Help's first row and a palette command — neither for a guest", () => {
    const rows = HELP_MENU_SRC.slice(HELP_MENU_SRC.indexOf("const rows = (["));
    const ask = rows.indexOf('label: "Ask how something works…"');
    expect(ask).toBeGreaterThan(0);
    expect(ask).toBeLessThan(rows.indexOf('label: "Send feedback…"'));
    expect(rows.slice(0, ask)).toMatch(/!guest && \{\s*key: "ask",\s*$/);
    expect(PALETTE_SRC).toMatch(/if \(!config\?\.guest\)\s*acts\.push\(\{\s*label: "Ask how something works…"/);
  });
});
