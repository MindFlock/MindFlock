/** No "Pause intake" anywhere (the owner: "get rid of the tickets pause button
 * entirely — it is completely pointless").
 *
 * Pinned here:
 *  - the sidebar's Tickets bar is the dot, the label and the switch: no ⏸/▶;
 *  - the Intake dialog keeps its one-line "what auto-starts right now" but has
 *    no Pause/Resume button;
 *  - the per-kind switches still work (tickets → /api/mindflock/start|stop,
 *    PR comments → github.enabled, issues → github.issues_enabled);
 *  - a store still carrying an older build's `general.intake_paused` record is
 *    not a state any more: the switches that pause turned off just read OFF
 *    (no striped "paused" switch, no "auto-start paused" chip, no "Resume
 *    intake"), and turning one back on is the ordinary switch. */

/// <reference types="vite/client" />
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, describe, expect, it, vi } from "vitest";
import { code, src } from "./sourceText";

// The sidebar bar reads navigator.onLine through useSyncExternalStore with no
// server snapshot; a server render needs one (as intakeTrackerNoDefault does).
vi.mock("react", async (orig) => {
  const R = (await orig()) as typeof import("react");
  return {
    ...R,
    useSyncExternalStore: <T,>(sub: (cb: () => void) => () => void, get: () => T) =>
      R.useSyncExternalStore(sub, get, get),
  };
});

const { QueryClient, QueryClientProvider } = await import("@tanstack/react-query");
const { useUi } = await import("../state/store");
const { AutomationBar } = await import("../components/sidebar/AutomationBar");
const { IntakeDialog } = await import("../components/intake/IntakeDialog");
const { intakeSwitches, kindsText } = await import("../components/intake/switches");

/** A store as a paused older build left it: the record says tickets and PR
 * comments were turned off, and every switch is still off. */
const LEGACY_SETTINGS = {
  settings: {
    general: { intake_paused: ["tickets", "prs"] },
    github: { repos: ["acme/web"], enabled: false, issue_repos: ["acme/web"], issues_enabled: false },
    ticketing: { sources: [{ id: "sc", provider: "shortcut" }] },
  },
};

function client(opts: { desired: boolean; legacy?: boolean }) {
  const c = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  c.setQueryData(["config"], { caps: { ticketing: true, git: true } });
  c.setQueryData(["mindflock-status"], {
    available: true,
    running: opts.desired,
    desired: opts.desired,
  });
  c.setQueryData(["ticketing-sources"], [{ id: "sc", provider: "shortcut" }]);
  c.setQueryData(["github-settings"], opts.legacy ? LEGACY_SETTINGS.settings.github : { repos: ["acme/web"] });
  // What the old pause module cached the record under: seeded so a stray
  // reader would find it — nothing may.
  if (opts.legacy) c.setQueryData(["intake-paused"], ["tickets", "prs"]);
  return c;
}

function renderBar(opts: { desired: boolean; legacy?: boolean }) {
  return renderToStaticMarkup(
    createElement(QueryClientProvider, { client: client(opts) }, createElement(AutomationBar))
  );
}

function renderIntake(opts: { desired: boolean; legacy?: boolean }) {
  useUi.setState({ openDialog: "intake", dialogTarget: "tickets" } as never);
  Object.assign(useUi.getInitialState(), useUi.getState());
  return renderToStaticMarkup(
    createElement(QueryClientProvider, { client: client(opts) }, createElement(IntakeDialog))
  );
}

afterEach(() => {
  useUi.setState({ openDialog: null, dialogTarget: null } as never);
  Object.assign(useUi.getInitialState(), useUi.getState());
});

const PAUSE_WORDS = /Pause intake|Resume intake|intake is paused|auto-start paused|⏸|▶/i;

describe("the sidebar's Tickets bar has no pause button", () => {
  it("dot, label and switch — nothing else", () => {
    const html = renderBar({ desired: true });
    expect(html).toContain('id="mindflock-bar"');
    expect(html).toContain('id="mindflock-toggle"');
    expect(html).toContain('id="mindflock-tickets-btn"');
    expect(html).not.toContain("intake-pause-btn");
    expect(html).not.toContain("dc-pause");
    expect(html).not.toMatch(PAUSE_WORDS);
    // The switch is on, and drawn as a plain switch.
    // (React emits `checked` last; the switch's accessible name sits between.)
    expect(html).toMatch(/<input type="checkbox" id="mindflock-toggle"[^>]*checked=""/);
    expect(html).toContain('aria-label="Auto-start tickets"');
  });

  it("an older build's pause record reads as plain OFF", () => {
    const html = renderBar({ desired: false, legacy: true });
    expect(html).toContain('id="mindflock-toggle"');
    expect(html).not.toMatch(/id="mindflock-toggle"[^>]*checked/);
    expect(html).not.toContain(" paused");
    expect(html).not.toMatch(PAUSE_WORDS);
  });
});

describe("the Intake dialog has no Pause/Resume", () => {
  it("keeps the one line saying what auto-starts, without a button", () => {
    const html = renderIntake({ desired: true });
    expect(html).toContain('id="ik-autostart-status"');
    expect(html).toContain("Auto-start is on for tickets and PR comments.");
    expect(html).not.toContain("ik-pause");
    expect(html).not.toMatch(PAUSE_WORDS);
    // Round 5: the switch lives on the Auto-start tab; the Tickets tab says
    // where it stands and links there.
    expect(html).toMatch(/id="ik-autostart-link-tickets"[^>]*data-on="1"/);
    expect(html).toContain("checks every 20 s");
    expect(html).not.toContain('id="tk-ingestion-enabled"');
  });

  it("the Auto-start tab holds the three switches, one per source, and no Pause all", () => {
    const auto = code("components/intake/AutoStartSwitches.tsx");
    for (const id of ["tk-ingestion-enabled", "gh-pr-enabled", "gh-issues-enabled"]) {
      expect(auto).toContain(id);
      expect(auto.split('"' + id + '"').length - 1).toBe(1);
    }
    // The owner removed "pause everything": the code matches ./switches.ts.
    expect(auto).not.toContain("ik-pause-all");
    expect(auto).not.toMatch(/Pause all|pauseAll|All auto-start is off/);
    expect(auto).not.toMatch(/intake_paused|Resume|Pause intake/);
    expect(code("components/intake/switches.ts")).not.toMatch(/Pause all|pauseAll/);
  });

  it("a tab's status line is just a status: the header line above it links to the switches", () => {
    const auto = code("components/intake/AutoStartSwitches.tsx");
    const link = auto.slice(auto.indexOf("export function AutoStartLink"));
    expect(link).not.toContain("<button");
    expect(link).not.toMatch(/Change ›|Turn on ›/);
  });

  it("legacy paused: the switches just read off, and Turn on… leads to them", () => {
    const html = renderIntake({ desired: false, legacy: true });
    expect(html).toContain("Auto-start is off everywhere.");
    expect(html).toContain('id="ik-turn-on-btn"');
    // It names where it goes — it never turned anything on by itself.
    expect(html).toContain("Auto-start switches ›");
    expect(html).not.toContain("Turn on ›");
    // Said once: no second "Auto-start: off" box under the header line.
    expect(html).not.toContain('id="ik-autostart-link-tickets"');
    expect(html).not.toMatch(PAUSE_WORDS);
    expect(html).not.toContain("ik-switch paused");
    expect(html).not.toContain("data-paused");
  });
});

describe("nothing reads or writes the old pause record", () => {
  const FILES = [
    "components/intake/switches.ts",
    "components/intake/IntakeDialog.tsx",
    "components/intake/TicketsTab.tsx",
    "components/intake/PullRequestsTab.tsx",
    "components/intake/IssuesTab.tsx",
    "components/intake/QueueTab.tsx",
    "components/intake/AutoStartSwitches.tsx",
    "components/intake/kit.tsx",
    "components/dialogs/NewTicketPane.tsx",
    "components/sidebar/AutomationBar.tsx",
    "components/sidebar/PrReviewBar.tsx",
    "components/sidebar/GitIssueBar.tsx",
  ];
  it.each(FILES)("%s", (f) => {
    const c = code(f);
    expect(c).not.toContain("intake_paused");
    expect(c).not.toContain("intake-paused");
    expect(c).not.toContain("useIntakePause");
    expect(c).not.toContain("pauseButton.css");
    expect(c).not.toMatch(/Pause intake|Resume intake|auto-start paused/);
  });

  it("the pause module and its stylesheet are gone", async () => {
    const mods = import.meta.glob("../components/intake/*.{ts,css}");
    expect(Object.keys(mods)).not.toContain("../components/intake/pause.ts");
    expect(Object.keys(mods)).not.toContain("../components/intake/pauseButton.css");
  });
});

describe("the switches still do the job, one at a time", () => {
  it("each kind keeps its own switch and endpoint", () => {
    expect(src("components/sidebar/AutomationBar.tsx")).toContain(
      'api(`/api/mindflock/${start ? "start" : "stop"}`, { method: "POST" })'
    );
    // Intake's three switches sit together on the Auto-start tab (round 5).
    const auto = code("components/intake/AutoStartSwitches.tsx");
    expect(auto).toContain('api(`/api/mindflock/${on ? "start" : "stop"}`, { method: "POST" })');
    expect(auto).toContain("{ enabled: next }");
    expect(auto).toContain("{ issues_enabled: next }");
  });

  it("the header line reads the three switches together", () => {
    const sw = intakeSwitches({
      ticketsDesired: true,
      ticketSources: 1,
      gh: { repos: ["o/r"], enabled: false, issue_repos: ["o/r"], issues_enabled: true },
    });
    expect(sw.on).toEqual({ tickets: true, prs: false, issues: true });
    expect(kindsText((["tickets", "prs", "issues"] as const).filter((k) => sw.on[k]))).toBe(
      "tickets and issues"
    );
  });
});
