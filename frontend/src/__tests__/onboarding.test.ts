/// <reference types="vite/client" />
/** First-run has ONE surface: the setup checklist. These tests pin the
 * decisions around it (components/setup/setupModel.ts) and, by reading
 * source, that nothing auto-opens the welcome tour or the Setup modal. */
import { describe, it, expect } from "vitest";
import {
  REQUIRED_STEPS,
  STEP_GROUP,
  STEP_ORDER,
  chipLabel,
  isEssentialCheck,
  nextStepId,
  stepGroup,
  stepNumber,
  consumeSetupIntent,
  orderedSteps,
  showSetupCard,
  showSetupChip,
  tailscaleMissing,
} from "../components/setup/setupModel";
import type { SetupStatus } from "../api/types";

function status(over: Partial<Record<string, Partial<SetupStatus["steps"][number]>>> = {}): SetupStatus {
  const steps = STEP_ORDER.map((id) => {
    const o = over[id] || {};
    const done = !!o.done;
    const skipped = !!o.skipped && !done;
    return {
      id,
      title: id,
      required: REQUIRED_STEPS.includes(id),
      group: STEP_GROUP[id],
      done,
      at: done ? "2026-10-10T00:00:00+00:00" : null,
      skipped,
      status: done ? "done" : skipped ? "skipped" : "todo",
      ...o,
    };
  });
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

describe("the empty grid's checklist card", () => {
  it("shows while a required step is to do and nothing is running", () => {
    expect(showSetupCard({ sessions: 0, status: status(), onboarded: false })).toBe(true);
    expect(
      showSetupCard({ sessions: 0, status: status({ tools: { done: true } }), onboarded: true })
    ).toBe(true);
  });

  it("goes once the three required steps are done, whatever the optional ones say", () => {
    const s = status({ tools: { done: true }, agent_login: { done: true }, repo: { done: true } });
    expect(showSetupCard({ sessions: 0, status: s, onboarded: true })).toBe(false);
  });

  it("never over a grid with sessions, and never before the first poll", () => {
    expect(showSetupCard({ sessions: 2, status: status(), onboarded: false })).toBe(false);
    expect(showSetupCard({ sessions: -1, status: status(), onboarded: false })).toBe(false);
  });

  it("falls back to the server's onboarded flag until /api/setup lands", () => {
    expect(showSetupCard({ sessions: 0, status: undefined, onboarded: false })).toBe(true);
    expect(showSetupCard({ sessions: 0, status: undefined, onboarded: true })).toBe(false);
    expect(showSetupCard({ sessions: 0, status: undefined, onboarded: undefined })).toBe(false);
  });
});

describe("the Setup chip (one count: N of 3)", () => {
  const partly = status({ tools: { done: true }, agent_login: { done: true }, repo: { done: true } });

  it("shows after the card has gone, until every step is done or skipped", () => {
    expect(showSetupChip({ sessions: 0, status: partly, onboarded: true })).toBe(true);
    expect(showSetupChip({ sessions: 3, status: status(), onboarded: false })).toBe(true);
    // ONE count everywhere: the three required steps, then what optional is
    // left — never a "/5" beside the checklist's "of 3".
    expect(chipLabel(partly)).toBe("Setup 3 of 3 · 2 optional");
    expect(chipLabel(status({ tools: { done: true } }))).toBe("Setup 1 of 3");
  });

  it("counts a skipped step as done", () => {
    const s = status({
      tools: { done: true },
      agent_login: { done: true },
      repo: { done: true },
      github: { done: true },
      tracker: { skipped: true },
    });
    expect(s.summary.complete).toBe(true);
    expect(showSetupChip({ sessions: 3, status: s, onboarded: true })).toBe(false);
  });

  it("never doubles the card, and never before the payload", () => {
    expect(showSetupChip({ sessions: 0, status: status(), onboarded: false })).toBe(false);
    expect(showSetupChip({ sessions: 3, status: undefined, onboarded: false })).toBe(false);
  });
});

describe("step helpers", () => {
  it("draws the steps in the server's order, filling gaps as to-do", () => {
    const ids = orderedSteps({ ...status(), steps: [] }).map((s) => s.id);
    expect(ids).toEqual(["tools", "agent_login", "repo", "github", "tracker", "phone"]);
    expect(orderedSteps(undefined).every((s) => s.status === "todo")).toBe(true);
    expect(orderedSteps(undefined).map((s) => s.group)).toEqual([
      "start",
      "start",
      "start",
      "connect",
      "connect",
      "later",
    ]);
  });

  it("groups: required is exactly Get started; the phone is a later extra", () => {
    for (const id of STEP_ORDER) expect(STEP_GROUP[id] === "start").toBe(REQUIRED_STEPS.includes(id));
    expect(stepGroup({ id: "phone" })).toBe("later");
    expect(stepGroup({ id: "tracker", group: "connect" })).toBe("connect");
    expect(stepGroup({ id: "mystery" })).toBe("later");
    expect(stepNumber("tools")).toBe("①");
    expect(stepNumber("tracker")).toBe("⑤");
    expect(stepNumber("phone")).toBe("");
  });

  it("the phone never keeps the chip up", () => {
    const s = status({
      tools: { done: true },
      agent_login: { done: true },
      repo: { done: true },
      github: { skipped: true },
      tracker: { done: true },
    });
    expect(s.steps.find((x) => x.id === "phone")?.status).toBe("todo");
    expect(s.summary.complete).toBe(true);
    expect(showSetupChip({ sessions: 2, status: s, onboarded: true })).toBe(false);
    expect(nextStepId(s)).toBe("");
    expect(nextStepId(undefined)).toBe("tools");
  });

  it("Install the tools shows only problems getting started needs", () => {
    expect(isEssentialCheck({ id: "tmux", status: "fail" })).toBe(true);
    expect(isEssentialCheck({ id: "git-identity", status: "warn" })).toBe(true);
    expect(isEssentialCheck({ id: "git", status: "ok" })).toBe(false);
    for (const id of ["tailscale", "bwrap", "cloudflared", "local-model", "cache-seeds", "tailnet-mtu", "gh"])
      expect(isEssentialCheck({ id, status: "warn" })).toBe(false);
    // Sign-in is step ②'s, not a missing tool.
    expect(isEssentialCheck({ id: "agent-auth", status: "warn" })).toBe(false);
  });

  it("offers Install Tailscale only when the doctor says it is missing", () => {
    expect(tailscaleMissing(status())).toBe(false);
    expect(
      tailscaleMissing({ ...status(), tailscale: { status: "ok", detail: "", fix: "", docs: "" } })
    ).toBe(false);
    expect(
      tailscaleMissing({
        ...status(),
        tailscale: { status: "warn", detail: "not found", fix: "curl …", docs: "" },
      })
    ).toBe(true);
  });
});

describe("?setup= opens the modal on load", () => {
  const hist = () => {
    const calls: string[] = [];
    return { calls, replaceState: (_d: unknown, _u: string, url?: string) => calls.push(String(url)) };
  };

  it("honours setup=1 (the desktop app's first-launch hand-off) and strips it", () => {
    const h = hist();
    expect(consumeSetupIntent({ search: "?setup=1&x=2", pathname: "/", hash: "" }, h)).toBe(true);
    expect(h.calls).toEqual(["/?x=2"]);
  });

  it("still honours the older setup=install spelling", () => {
    expect(consumeSetupIntent({ search: "?setup=install", pathname: "/", hash: "" }, hist())).toBe(true);
  });

  it("ignores anything else", () => {
    const h = hist();
    expect(consumeSetupIntent({ search: "?setup=0", pathname: "/", hash: "" }, h)).toBe(false);
    expect(consumeSetupIntent({ search: "", pathname: "/", hash: "" }, h)).toBe(false);
    expect(h.calls).toEqual([]);
  });
});

/** Source with its comments removed.
 *
 * The modules below deliberately name the retired endpoint and flags in prose so
 * nobody reinvents them, so a raw substring search would flag the very
 * documentation that keeps them retired. The `[^:"'`]` guard is what stops a
 * `https://` inside a string from being read as the start of a comment. */
function code(text: string): string {
  return text.replace(/\/\*[\s\S]*?\*\//g, "").replace(/(^|[^:"'`\\])\/\/.*$/gm, "$1");
}

/** Every .ts/.tsx module the app itself ships, as source keyed by path. The glob
 * is Vite's rather than a directory walk because this project installs no node
 * types, and @types/node is not worth adding for one readdir. */
const sources = Object.entries(
  import.meta.glob<string>(["../**/*.{ts,tsx}", "!../__tests__/**"], {
    query: "?raw",
    import: "default",
    eager: true,
  })
).map(([path, text]) => ({ path: path.replace(/^\.\.\//, ""), text: code(text) }));

describe("first-run state has exactly one authority", () => {
  // These read source rather than behaviour because the regressions were side
  // effects inside components, and this suite runs without a DOM.

  it("finds sources to scan at all", () => {
    expect(sources.length).toBeGreaterThan(20);
    expect(sources.map((f) => f.path)).toContain("App.tsx");
  });

  it("the welcome tour opens by itself once, from one place, on a synced flag", () => {
    // openTour() is a user action: the palette's Replay welcome tour, the ?
    // sheet, Settings → General → Tours. The ONE automatic opening is WelcomeTour's
    // useFirstLaunchTour: a first launch (lib/calm firstLaunchTour), marked
    // seen in the synced `welcome_tour` pref the moment it opens — never a
    // per-browser flag, never from App.
    const tour = sources.find((f) => f.path === "components/onboarding/WelcomeTour.tsx")!;
    expect(tour.text).toContain("firstLaunchTour(");
    expect(tour.text).toContain("markWelcomeTourSeen()");
    expect(tour.text).toContain("firstPrefsPull");
    const autoOpeners = sources
      .filter((f) => /tourOpen:\s*true/.test(f.text))
      .map((f) => f.path)
      .sort();
    expect(autoOpeners).toEqual(["components/onboarding/WelcomeTour.tsx", "state/store.ts"]);
    const app = sources.find((f) => f.path === "App.tsx")!;
    expect(app.text).not.toMatch(/openTour\(\)/);
    expect(app.text).not.toContain("tourDecision");
    const store = sources.find((f) => f.path === "state/store.ts")!;
    expect(store.text).not.toContain("tourDone");
    expect(store.text).not.toContain("mf_tour_done");
    const callers = sources
      .filter((f) => /(\.openTour\(\)|\bopenTour\(\);)/.test(f.text))
      .map((f) => f.path)
      .sort();
    // Round 5: the palette's "Replay welcome tour" and Settings → General →
    // Tours go through onboarding/replayTour (which opens the welcome card);
    // the ? sheet's "Tour" link still opens it directly.
    expect(callers).toEqual([
      "components/onboarding/replayTour.ts",
      "components/palette/ShortcutsSheet.tsx",
    ]);
  });

  it("each dialog's tour opens by itself from ONE hook, in the calm first hour, on a synced flag", () => {
    // The files that may open a coach tour at all: the store (the action),
    // the "?" button, Replay (palette / Settings → General → Tours — and
    // People's own replay, a Settings screen rather than a top-bar dialog),
    // the welcome card's "Let's go" — and useAutoTour, the ONE automatic opener.
    const openers = sources
      .filter((f) => /\bopenCoachTour\(/.test(f.text))
      .map((f) => f.path)
      .sort();
    expect(openers).toEqual([
      "components/onboarding/TourButton.tsx",
      "components/onboarding/WelcomeTour.tsx",
      "components/onboarding/replayTour.ts",
      "components/onboarding/useAutoTour.ts",
      "components/settings/screens/peer/replayPeopleTour.ts",
      "state/store.ts",
    ]);
    const auto = sources.find((f) => f.path === "components/onboarding/useAutoTour.ts")!;
    expect(auto.text).toContain("calmDecision(");
    expect(auto.text).toContain("firstPrefsPull");
    expect(auto.text).toMatch(/seen\.includes\(o\.id\)/);
    // Exactly the five top-bar dialogs call it, each for its own tour.
    const users = sources
      .filter((f) => /\buseAutoTour\("/.test(f.text))
      .map((f) => [f.path, (f.text.match(/useAutoTour\("([a-z]+)"/) || [])[1]])
      .sort();
    expect(users).toEqual([
      ["components/dialogs/NewSessionDialog.tsx", "new"],
      ["components/dialogs/TemplatesDialog.tsx", "schedule"],
      ["components/dialogs/VerifyDialog.tsx", "verify"],
      ["components/intake/IntakeDialog.tsx", "intake"],
      ["components/settings/SettingsDialog.tsx", "settings"],
    ]);
    // ...and each puts its "?" beside Close.
    for (const [path, id] of users) {
      expect(sources.find((f) => f.path === path)!.text, path).toContain(`<TourButton tour="${id}" />`);
    }
    // Seen is the synced pref, never a per-browser first-run flag.
    const store = sources.find((f) => f.path === "state/store.ts")!;
    expect(store.text).toContain('save("mf_tours_seen"');
  });

  it("the Setup modal opens only on request (chip, doctor chip, palette, ?setup=)", () => {
    const offenders = sources.filter((f) => f.text.includes("shouldAutoShowSetup"));
    expect(offenders.map((f) => f.path)).toEqual([]);
  });

  it("never tells the server the welcome tour happened", () => {
    const offenders = sources.filter((f) => f.text.includes("/api/onboarded"));
    expect(offenders.map((f) => f.path)).toEqual([]);
  });

  it("never marks a checklist step done from the browser", () => {
    // Every step is marked by the backend hook that observes the thing itself;
    // the only write the UI may ask for is a skip.
    const offenders = sources.filter((f) => /\/api\/setup\/[^"']*\/done/.test(f.text));
    expect(offenders.map((f) => f.path)).toEqual([]);
  });

  it("keeps the writer-less per-browser setup flags out", () => {
    const offenders = sources.filter(
      (f) => f.text.includes("mf_setup_done") || f.text.includes("mf_ever_created")
    );
    expect(offenders.map((f) => f.path)).toEqual([]);
  });
});
