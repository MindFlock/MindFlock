/// <reference types="vite/client" />
/** Round 5's tours: the registry (lib/tours), the coach mark
 * (onboarding/CoachTour), when one opens by itself (onboarding/useAutoTour),
 * that every exit marks it seen, the welcome card that hands over to the
 * Welcome tour, and the top bar's calm first hour (lib/calm topBarShows). */

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import COACH_SRC from "../components/onboarding/CoachTour.tsx?raw";
import AUTO_SRC from "../components/onboarding/useAutoTour.ts?raw";
import WELCOME_SRC from "../components/onboarding/WelcomeTour.tsx?raw";
import TOPBAR_SRC from "../components/TopBar.tsx?raw";
import GENERAL_SRC from "../components/settings/screens/General.tsx?raw";
import PALETTE_SRC from "../components/palette/CommandPalette.tsx?raw";

vi.mock("../api/client", () => ({ api: vi.fn(async () => ({})) }));
vi.mock("../lib/toast", () => ({ toast: vi.fn() }));

const storage = new Map<string, string>();
(globalThis as Record<string, unknown>).localStorage = {
  getItem: (k: string) => (storage.has(k) ? storage.get(k)! : null),
  setItem: (k: string, v: string) => void storage.set(k, String(v)),
  removeItem: (k: string) => void storage.delete(k),
  clear: () => storage.clear(),
};

const { TOURS, TOUR_IDS, isTourId } = await import("../lib/tours");
const { CoachTour, placeCard, findTarget } = await import("../components/onboarding/CoachTour");
const { TourButton } = await import("../components/onboarding/TourButton");
const { autoTourDue } = await import("../components/onboarding/useAutoTour");
const { topBarShows } = await import("../lib/calm");
const { useUi } = await import("../state/store");

describe("the tours registry", () => {
  it("has the six tours, each under its own id", () => {
    expect([...TOUR_IDS]).toEqual(["welcome", "new", "intake", "verify", "schedule", "settings"]);
    expect(Object.keys(TOURS).sort()).toEqual([...TOUR_IDS].sort());
    for (const id of TOUR_IDS) expect(TOURS[id].id).toBe(id);
    expect(isTourId("intake")).toBe(true);
    expect(isTourId("map")).toBe(false);
  });

  it("keeps every tour short: 3–6 steps of one plain sentence each", () => {
    for (const id of TOUR_IDS) {
      const { steps, title } = TOURS[id];
      expect(title, id).toMatch(/^[A-Z]/);
      expect(steps.length, id).toBeGreaterThanOrEqual(3);
      expect(steps.length, id).toBeLessThanOrEqual(6);
      for (const s of steps) {
        expect(s.title.trim(), id).not.toBe("");
        expect(s.body.trim(), id).not.toBe("");
        expect(s.body, id).not.toContain("\n");
        // one sentence: no full stop before the last character
        expect(s.body.replace(/\.$/, ""), `${id}: ${s.title}`).not.toMatch(/\.\s/);
        expect(s.body.length, `${id}: ${s.title}`).toBeLessThanOrEqual(130);
        // GLOSSARY: "tour" is the name; nothing internal leaks into copy
        expect(s.title + s.body, id).not.toMatch(/walkthrough|tutorial|engine|orchestrator|\{|`/i);
      }
    }
  });

  it("names every target as a selector or \"\" — never whitespace, never a broken selector", () => {
    for (const id of TOUR_IDS)
      for (const s of TOURS[id].steps) {
        expect(s.target === "" || s.target.trim() === s.target, `${id}: ${s.title}`).toBe(true);
        if (s.target) expect(s.target.trim().length, id).toBeGreaterThan(0);
        // balanced quotes and brackets: querySelector would throw otherwise
        expect((s.target.match(/"/g) || []).length % 2, s.target).toBe(0);
        expect((s.target.match(/\[/g) || []).length, s.target).toBe((s.target.match(/\]/g) || []).length);
      }
  });

  it("points the Welcome tour at the Get set up checklist by its data-tour hooks", () => {
    const targets = TOURS.welcome.steps.map((s) => s.target).join(" ");
    for (const hook of ["setup-next", "setup-step-tools", "setup-step-agent_login", "setup-step-repo"])
      expect(targets).toContain(`[data-tour="${hook}"]`);
    expect(TOURS.welcome.steps.length).toBeLessThanOrEqual(5);
  });

  it("uses the dialogs' stable ids where they exist", () => {
    const all = (id: keyof typeof TOURS) => TOURS[id].steps.map((s) => s.target).join(" ");
    expect(all("new")).toContain("#new-describe-text");
    expect(all("intake")).toContain("#intake-tabs");
    expect(all("intake")).toContain("#intake-add-source");
    expect(all("verify")).toContain("#verify-title");
    expect(all("schedule")).toContain("#templates-new");
    expect(all("settings")).toContain("#settings-search");
  });
});

describe("the coach mark", () => {
  const steps = [
    { target: "", title: "One", body: "First." },
    { target: "#x", title: "Two", body: "Second." },
    { target: "#y", title: "Three", body: "Third." },
  ];
  const render = (step: number) =>
    renderToStaticMarkup(
      createElement(CoachTour, { steps, step, onStep: () => {}, onClose: () => {}, rootRef: { current: null } })
    );

  it("is a non-modal dialog with a step counter, Back / Next and Skip", () => {
    const html = render(1);
    expect(html).toContain('role="dialog"');
    expect(html).toContain('aria-modal="false"');
    expect(html).toContain("2 of 3");
    expect(html).toContain(">Back<");
    expect(html).toContain(">Next<");
    expect(html).toContain("Skip tour");
    expect(html).toContain("coach-card");
  });

  it("says Done on the last step, and Back is off on the first", () => {
    expect(render(2)).toContain(">Done<");
    expect(render(0)).toMatch(/<button[^>]*disabled=""[^>]*>Back</);
  });

  it("places the card beside a tall column, below a control, above one at the bottom, centred with nothing", () => {
    const W = 1200,
      H = 800,
      cardH = 170;
    expect(placeCard({ x: 0, y: 40, w: 220, h: 700 }, W, H, cardH)).toEqual({ left: 232, top: 40 });
    expect(placeCard({ x: 500, y: 60, w: 120, h: 28 }, W, H, cardH)).toEqual({ left: 500, top: 100 });
    expect(placeCard({ x: 500, y: 740, w: 120, h: 30 }, W, H, cardH).top).toBe(740 - 12 - cardH);
    // a big block filling the height (a checklist in a short window): beside it
    expect(placeCard({ x: 300, y: 10, w: 500, h: 780 }, W, H, cardH).left).toBe(812);
    expect(placeCard(null, W, H, cardH)).toEqual({ left: 440, top: 315 });
    // phone width: inside the screen with 8px to spare
    const narrow = placeCard({ x: 350, y: 60, w: 60, h: 28 }, 420, 800, cardH);
    expect(narrow.left).toBeGreaterThanOrEqual(8);
    expect(narrow.left + Math.min(320, 420 - 16)).toBeLessThanOrEqual(420 - 8);
  });

  it("treats a missing or broken target as nothing to point at", () => {
    const root = {
      querySelectorAll: (sel: string) => {
        if (sel === "((") throw new Error("SyntaxError");
        return [] as unknown as NodeListOf<HTMLElement>;
      },
    } as unknown as ParentNode;
    expect(findTarget(root, "")).toBeNull();
    expect(findTarget(root, "((")).toBeNull();
    expect(findTarget(root, "#absent")).toBeNull();
  });

  it("skips a hidden match in favour of a visible one", () => {
    const el = (w: number) => ({ getBoundingClientRect: () => ({ width: w, height: w }) });
    const hidden = el(0);
    const shown = el(10);
    const root = { querySelectorAll: () => [hidden, shown] } as unknown as ParentNode;
    expect(findTarget(root, ".a, .b")).toBe(shown);
  });

  it("sits above the open dialog and keeps its target clickable", () => {
    // .modal has no z-index; the layer is fixed, portalled to <body>, and only
    // the card takes pointer events.
    expect(COACH_SRC).toContain("createPortal(layer, document.body)");
    const css = import.meta.glob<string>("../components/onboarding/CoachTour.css", {
      query: "?raw",
      import: "default",
      eager: true,
    });
    const sheet = Object.values(css)[0];
    expect(sheet).toMatch(/\.coach-layer \{[^}]*z-index: 2500;[^}]*pointer-events: none;/);
    expect(sheet).toMatch(/\.coach-card \{[^}]*pointer-events: auto;/);
  });
});

describe("every exit marks the tour seen", () => {
  it("Skip, Esc and Done all call onClose; the host's onClose is closeCoachTour", () => {
    expect(COACH_SRC).toMatch(/className=\{c\("skip"\)\} onClick=\{onClose\}/);
    expect(COACH_SRC).toMatch(/e\.key === "Escape"[\s\S]{0,80}onClose\(\)/);
    expect(COACH_SRC).toContain("last ? onClose() : go(i + 1)");
    expect(COACH_SRC).toContain("onClose={closeCoachTour}");
    // its dialog closing is an exit too
    expect(AUTO_SRC).toMatch(/!open && useUi\.getState\(\)\.coachTour\?\.id === id\) useUi\.getState\(\)\.closeCoachTour\(\)/);
  });

  it("closeCoachTour remembers the tour in the synced list, once", () => {
    storage.clear();
    useUi.setState({ coachTour: null, toursSeen: [] });
    useUi.getState().openCoachTour("intake");
    expect(useUi.getState().coachTour).toEqual({ id: "intake", step: 0 });
    useUi.getState().setCoachStep(2);
    expect(useUi.getState().coachTour?.step).toBe(2);
    useUi.getState().closeCoachTour();
    expect(useUi.getState().coachTour).toBeNull();
    expect(useUi.getState().toursSeen).toEqual(["intake"]);
    expect(JSON.parse(storage.get("mf_tours_seen")!)).toEqual(["intake"]);
    useUi.getState().openCoachTour("intake");
    useUi.getState().closeCoachTour();
    expect(useUi.getState().toursSeen).toEqual(["intake"]);
  });

  it("the welcome card is seen by any exit, and only Let's go starts the Welcome tour", () => {
    expect(WELCOME_SRC).toMatch(/else if \(wasOpen\)[\s\S]{0,120}markWelcomeTourSeen\(\)/);
    expect(WELCOME_SRC).toContain('openCoachTour("welcome")');
    expect(WELCOME_SRC).toContain("Let's go");
    // the old exits that left it half-finished and re-armed are gone
    expect(WELCOME_SRC).not.toContain("Set up now");
    expect(WELCOME_SRC).not.toContain("Open Setup");
    expect(WELCOME_SRC).not.toContain("SLIDES");
  });
});

describe("a tour opens by itself only in the calm first hour, and only once", () => {
  const base = { open: true, calm: true as boolean | null, seen: [] as string[], id: "new" as const, pulled: true, hints: true, busy: false };
  it("opens on a first open in the first hour", () => {
    expect(autoTourDue(base)).toBe(true);
  });
  it("never once seen, on any device", () => {
    expect(autoTourDue({ ...base, seen: ["new"] })).toBe(false);
    expect(autoTourDue({ ...base, seen: ["intake"] })).toBe(true);
  });
  it("never outside the first hour, and never while the server hasn't said", () => {
    expect(autoTourDue({ ...base, calm: false })).toBe(false);
    expect(autoTourDue({ ...base, calm: null })).toBe(false);
  });
  it("never before the prefs pull, with hints off, over another tour, or with the dialog shut", () => {
    expect(autoTourDue({ ...base, pulled: false })).toBe(false);
    expect(autoTourDue({ ...base, hints: false })).toBe(false);
    expect(autoTourDue({ ...base, busy: true })).toBe(false);
    expect(autoTourDue({ ...base, open: false })).toBe(false);
  });
});

describe("the ? button and the replays", () => {
  it("the ? says what it does", () => {
    const html = renderToStaticMarkup(createElement(TourButton, { tour: "intake" }));
    expect(html).toContain('class="tour-btn"');
    expect(html).toContain('aria-label="Show me around"');
    expect(html).toContain('title="Replay the Intake tour"');
  });

  it("Settings → General → Tours lists one Replay per tour plus the hints switch", () => {
    expect(GENERAL_SRC).toContain('<h3 className="set-section-title">Tours</h3>');
    expect(GENERAL_SRC).not.toContain("Getting started");
    expect(GENERAL_SRC).toContain("TOUR_IDS.map(");
    expect(GENERAL_SRC).toContain("replayTour(id)");
    expect(GENERAL_SRC).toContain("Show hints");
    // the four controls a first-hour user can't use yet wait in the fold
    const fold = GENERAL_SRC.slice(GENERAL_SRC.indexOf('<AdvancedFold id="general-advanced"'));
    for (const key of ['"session_budget_usd"', "<ResumeOnUsageResetRow />", "<ReviveOnStartRow />", "<SleepIdleRow />"])
      expect(fold.slice(0, fold.indexOf("</AdvancedFold>")), key).toContain(key);
  });

  it("the palette has a Replay <X> tour row per tour, found by tour / help / how", () => {
    expect(PALETTE_SRC).toContain("for (const id of TOUR_IDS)");
    expect(PALETTE_SRC).toContain("`Replay ${id === \"welcome\" ? \"welcome\" : t.title} tour`");
    expect(PALETTE_SRC).toMatch(/keywords: "tour help how/);
  });
});

describe("the top bar in the calm first hour", () => {
  it("hides Verify until a checklist exists and Schedule until a template does", () => {
    expect(topBarShows({ calm: true, checklists: 0, templates: 0 })).toEqual({ verify: false, schedule: false });
    expect(topBarShows({ calm: true, checklists: 1, templates: 0 })).toEqual({ verify: true, schedule: false });
    expect(topBarShows({ calm: true, checklists: 0, templates: 2 })).toEqual({ verify: false, schedule: true });
  });

  it("shows both whenever the first hour is over", () => {
    expect(topBarShows({ calm: false, checklists: 0, templates: 0 })).toEqual({ verify: true, schedule: true });
  });

  it("wires the rule to the two buttons only (Alt+V / Alt+S and the palette don't depend on them)", () => {
    expect(TOPBAR_SRC).toMatch(/\{shows\.verify && \(\s*<button\s+id="verify-btn"/);
    expect(TOPBAR_SRC).toMatch(/\{shows\.schedule && \(\s*<button\s+id="schedule-btn"/);
    expect(TOPBAR_SRC).toContain("calm: useCalm(),");
  });
});
