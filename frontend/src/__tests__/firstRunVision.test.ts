/// <reference types="vite/client" />
/** The vision round's first run: the picture in one breath (lib/onboarding,
 * docs/vocabulary.md) on the welcome card, Settings → Help → How MindFlock
 * works, the empty Sky (onboarding/SkyEmpty) and the empty Grid
 * (onboarding/GridEmpty); the tours in the vocabulary's words; and the Setup
 * chip that no longer says "2 of 3" beside a running agent. */

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import SKYVIEW_SRC from "../components/flights/SkyView.tsx?raw";
import GRID_SRC from "../components/grid/TerminalGrid.tsx?raw";
import GENERAL_SRC from "../components/settings/screens/General.tsx?raw";
import CHIP_SRC from "../components/setup/SetupChip.tsx?raw";
import VISION_CSS from "../components/onboarding/Vision.css?raw";
import PICTURE_SRC from "../components/onboarding/VisionPicture.tsx?raw";
import WELCOME_SRC from "../components/onboarding/WelcomeTour.tsx?raw";

vi.mock("../api/client", () => ({ api: vi.fn(async () => ({})) }));
vi.mock("../lib/toast", () => ({ toast: vi.fn() }));

const storage = new Map<string, string>();
(globalThis as Record<string, unknown>).localStorage = {
  getItem: (k: string) => (storage.has(k) ? storage.get(k)! : null),
  setItem: (k: string, v: string) => void storage.set(k, String(v)),
  removeItem: (k: string) => void storage.delete(k),
  clear: () => storage.clear(),
};

const { VISION_BREATH, SKY_LINE, GRID_LINE, STOP_TEST, VISION_PARTS } = await import("../lib/onboarding");
const { VisionPicture } = await import("../components/onboarding/VisionPicture");
const { HowItWorks, HOW_IT_WORKS_TITLE } = await import("../components/onboarding/HowItWorks");
const { SkyEmpty, skyEmptyShows, SKY_EMPTY_TITLE } = await import("../components/onboarding/SkyEmpty");
const { GridEmpty } = await import("../components/onboarding/GridEmpty");
const { TOURS, TOUR_IDS } = await import("../lib/tours");
const { agentWorked, chipLabel, setupCount, showSetupChip, STEP_GROUP, STEP_ORDER, REQUIRED_STEPS } = await import(
  "../components/setup/setupModel"
);
const { QueryClient, QueryClientProvider } = await import("@tanstack/react-query");

const esc = (s: string) => s.replace(/&/g, "&amp;").replace(/'/g, "&#x27;").replace(/"/g, "&quot;");
const withQuery = (el: ReturnType<typeof createElement>) =>
  renderToStaticMarkup(createElement(QueryClientProvider, { client: new QueryClient() }, el));

describe("the picture in one breath", () => {
  it("is docs/vocabulary.md's, word for word", () => {
    expect(VISION_BREATH.join(" ")).toBe(
      "MindFlock runs your coding agents side by side. Each session is one agent working on its own branch. " +
        "Its route is the stops the work goes through until it is merged — the agent, its checks, your OK, the PR. " +
        "The Sky shows every route at once, and what needs you."
    );
    expect(SKY_LINE).toBe(
      "Air traffic control for your agents — every piece of work in flight, the route it's cleared to fly, and the ones that need you."
    );
    expect(GRID_LINE).toBe("Inside the work — your sessions side by side, talk to the agents.");
    expect(STOP_TEST).toBe("A stop moves the work, checks the work, or asks you.");
  });

  it("is drawn in three parts: session → route → Needs you", () => {
    expect(VISION_PARTS.map((p) => p.title)).toEqual(["Session", "Route", "Needs you"]);
    const html = renderToStaticMarkup(createElement(VisionPicture));
    expect(html.match(/<svg class="vp-sketch"/g)).toHaveLength(3);
    expect(html.match(/→/g)).toHaveLength(2);
    for (const p of VISION_PARTS) expect(html).toContain(`data-part="${p.id}"`);
    expect(html).toContain('aria-label="A session is one agent;');
  });

  it("draws in currentColor and theme tokens only (light and dark)", () => {
    expect(PICTURE_SRC).not.toMatch(/(fill|stroke)="#/);
    const sheet = VISION_CSS.replace(/rgba\([^)]*\)/g, "");
    expect(sheet).not.toMatch(/#[0-9a-f]{3,8}\b/i);
    expect(sheet).toContain("currentColor");
    expect(sheet).toContain("var(--accent)");
  });

  it("the welcome card leads with it and shows the picture", () => {
    expect(WELCOME_SRC).toContain("<VisionPicture />");
    expect(WELCOME_SRC).toContain("VISION_BREATH[0]");
  });
});

describe("How MindFlock works (Settings → Help)", () => {
  it("replays the picture: the sketch, the breath, the Sky and Grid lines, the stop test", () => {
    const html = renderToStaticMarkup(createElement(HowItWorks, { open: true, onClose: () => {}, portal: false }));
    expect(html).toContain('id="how-it-works"');
    expect(html).toContain(`>${HOW_IT_WORKS_TITLE}<`);
    expect(html).toContain('class="vision-picture"');
    expect(html).toContain("<b>MindFlock runs your coding agents side by side.</b>");
    expect(html).toContain(esc("air traffic control for your agents"));
    expect(html).toContain("inside the work");
    expect(html).toContain("Ctrl+Y");
    expect(html).toContain(STOP_TEST);
    expect(html).toContain(">Got it<");
  });

  it("closed renders nothing", () => {
    expect(renderToStaticMarkup(createElement(HowItWorks, { open: false }))).toBe("");
  });

  it("sits under Help, before the tours, and mounts its own card", () => {
    const help = GENERAL_SRC.slice(GENERAL_SRC.indexOf('<h3 className="set-section-title">Help</h3>'));
    expect(help.indexOf("{HOW_IT_WORKS_TITLE}")).toBeGreaterThan(-1);
    expect(help.indexOf("{HOW_IT_WORKS_TITLE}")).toBeLessThan(help.indexOf('<h4 className="set-subtitle">Tours</h4>'));
    expect(help).toContain("onClick={openHowItWorks}");
    expect(help).toContain("<HowItWorks />");
  });
});

describe("the empty Sky", () => {
  it("shows the calm card only on an empty, unfiltered sky with nothing arriving", () => {
    expect(skyEmptyShows({ blank: true, waiting: 0 })).toBe(true);
    expect(skyEmptyShows({ blank: true, waiting: 2 })).toBe(false);
    expect(skyEmptyShows({ blank: false, waiting: 0 })).toBe(false);
  });

  it("says what the Sky is for, with New session and Connect a source in Intake", () => {
    const html = withQuery(createElement(SkyEmpty, { blank: true, className: "sky-empty muted" }, "plain line"));
    expect(html).toContain('class="sky-empty-card"');
    expect(html).toContain('data-no-drag=""');
    expect(html).toContain(SKY_EMPTY_TITLE);
    expect(html).toContain(esc(SKY_LINE));
    expect(html).toContain('class="vision-picture is-compact"');
    expect(html).toContain(">New session<");
    expect(html).toContain(">Connect a source in Intake<");
    expect(html).not.toContain("plain line");
  });

  it("otherwise draws the plain line it was handed (a filter, Needs you, This session)", () => {
    const html = withQuery(createElement(SkyEmpty, { blank: false, className: "sky-empty muted" }, "Nothing needs you."));
    expect(html).toBe('<p class="sky-empty muted" role="status">Nothing needs you.</p>');
    expect(withQuery(createElement(SkyEmpty, { blank: false, className: "x" }, ""))).toBe("");
  });

  it("SkyView mounts it in both layouts, blank only with no sessions on an unfiltered sky", () => {
    expect(SKYVIEW_SRC).toContain('import { SkyEmpty } from "../onboarding/SkyEmpty";');
    expect(SKYVIEW_SRC.match(/<SkyEmpty blank=\{blank\}/g)).toHaveLength(2);
    expect(SKYVIEW_SRC).toContain(
      'const blank = lines.length === 0 && !query && mode !== "one" && mode !== "perched" && instQ.data?.length === 0;'
    );
  });
});

describe("the empty Grid", () => {
  it("says what the Grid is for and what a session is, with New session", () => {
    const html = renderToStaticMarkup(createElement(GridEmpty));
    expect(html).toContain("No sessions yet");
    expect(html).toContain(GRID_LINE);
    expect(html).toContain(VISION_BREATH[1]);
    expect(html).toContain(">New session<");
    expect(GRID_SRC).toContain("{noneAtAll ? <GridEmpty /> :");
  });
});

describe("every tour speaks the vocabulary", () => {
  it("no retired word in any tour's copy", () => {
    for (const id of TOUR_IDS) {
      for (const s of TOURS[id].steps) {
        // the Sky's one-liner is a phrase ("work in flight", "cleared to fly"), not a noun
        const copy = (s.title + " " + s.body).replaceAll("work in flight", "");
        for (const w of ["chain", "flyway", "perch", "Perched", "Fast-track", "block", "tile", "flight", "canvas", "inbox", "preset"])
          expect(copy, `${id}: ${s.title}`).not.toMatch(new RegExp("\\b" + w + "\\b", "i"));
        expect(copy, `${id}: ${s.title}`).not.toMatch(/\bFly\b/);
      }
    }
  });

  it("the Welcome tour is three steps; the Sky tour opens with what the Sky is for", () => {
    expect(TOURS.welcome.steps).toHaveLength(3);
    expect(TOURS.sky.steps[0].title).toBe("What the Sky is for");
    expect(TOURS.sky.steps[0].body).toBe(SKY_LINE);
  });
});

describe("the Setup chip never says 2 of 3 beside a running agent", () => {
  function status(done: string[]) {
    const steps = STEP_ORDER.map((id) => ({
      id,
      title: id,
      required: REQUIRED_STEPS.includes(id),
      group: STEP_GROUP[id],
      done: done.includes(id),
      at: null,
      skipped: false,
      status: done.includes(id) ? "done" : "todo",
    }));
    const counted = steps.filter((s) => s.group !== "later");
    const todo = counted.filter((s) => s.status === "todo");
    return {
      steps,
      summary: {
        done: counted.length - todo.length,
        total: counted.length,
        required_done: steps.filter((s) => s.required).every((s) => s.done),
        complete: todo.length === 0,
        next: todo[0]?.id || "",
      },
      doctor: null,
      tailscale: null,
      logins: [],
      default_provider: "claude",
      report: "",
    };
  }

  it("an agent at work is proof the required steps are done", () => {
    expect(agentWorked([{ activity: "idle" }, { activity: "working" }])).toBe(true);
    expect(agentWorked([{ activity: "idle" }])).toBe(false);
    expect(agentWorked(undefined)).toBe(false);
    const stale = status(["tools"]); // the sign-in happened in the session's terminal
    expect(chipLabel(stale)).toBe("Setup 1 of 3");
    expect(chipLabel(stale, true)).toBe("Setup 3 of 3 · 2 optional");
    expect(setupCount(stale, true)).toEqual({ done: 3, total: 3, optional: 2 });
  });

  it("the chip then stays only for optional steps", () => {
    const stale = status(["tools"]);
    expect(showSetupChip({ sessions: 1, status: stale, onboarded: true, worked: true })).toBe(true);
    const onlyRequiredLeft = status(["tools", "github", "tracker"]);
    expect(showSetupChip({ sessions: 1, status: onlyRequiredLeft, onboarded: true })).toBe(true);
    expect(showSetupChip({ sessions: 1, status: onlyRequiredLeft, onboarded: true, worked: true })).toBe(false);
  });

  it("the chip re-reads the checklist with a doctor re-probe the first time an agent works", () => {
    expect(CHIP_SRC).toContain("const workingNow = agentWorked(instances);");
    expect(CHIP_SRC).toMatch(/setWorked\(true\);\s*void refreshSetup\(true\);/);
    expect(CHIP_SRC).toContain("worked: worked || workingNow");
    expect(CHIP_SRC).toContain("chipLabel(status, worked || workingNow)");
  });
});
