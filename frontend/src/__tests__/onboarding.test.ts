/// <reference types="vite/client" />
/** First-run has ONE surface: the five-step checklist. These tests pin the
 * decisions around it (components/setup/setupModel.ts) and, by reading
 * source, that nothing auto-opens the welcome tour or the Setup modal. */
import { describe, it, expect } from "vitest";
import {
  REQUIRED_STEPS,
  STEP_ORDER,
  chipLabel,
  consumeSetupIntent,
  installedLogins,
  loginLine,
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
      done,
      at: done ? "2026-10-10T00:00:00+00:00" : null,
      skipped,
      status: done ? "done" : skipped ? "skipped" : "todo",
      ...o,
    };
  });
  const todo = steps.filter((s) => s.status === "todo");
  return {
    steps,
    summary: {
      done: steps.length - todo.length,
      total: steps.length,
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

describe("the Setup N/5 chip", () => {
  const partly = status({ tools: { done: true }, agent_login: { done: true }, repo: { done: true } });

  it("shows after the card has gone, until every step is done or skipped", () => {
    expect(showSetupChip({ sessions: 0, status: partly, onboarded: true })).toBe(true);
    expect(showSetupChip({ sessions: 3, status: status(), onboarded: false })).toBe(true);
    expect(chipLabel(partly)).toBe("Setup 3/5");
  });

  it("counts a skipped step as done", () => {
    const s = status({
      tools: { done: true },
      agent_login: { done: true },
      repo: { done: true },
      tracker: { skipped: true },
      phone: { skipped: true },
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
  it("draws the five steps in the server's order, filling gaps as to-do", () => {
    const ids = orderedSteps({ ...status(), steps: [] }).map((s) => s.id);
    expect(ids).toEqual(["tools", "agent_login", "repo", "tracker", "phone"]);
    expect(orderedSteps(undefined).every((s) => s.status === "todo")).toBe(true);
  });

  it("offers a sign-in button only for installed CLIs, default first", () => {
    const rows = installedLogins([
      { name: "codex", installed: true, logged_in: false, login_cmd: "codex login", default: false },
      { name: "aider", installed: false, logged_in: null, login_cmd: "", default: false },
      { name: "claude", installed: true, logged_in: true, login_cmd: "claude", default: true },
    ]);
    expect(rows.map((r) => r.name)).toEqual(["claude", "codex"]);
    expect(loginLine(rows[0]).ok).toBe(true);
    expect(loginLine(rows[1]).ok).toBe(false);
    expect(loginLine({ ...rows[1], logged_in: null }).ok).toBeNull();
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

  it("nothing opens the welcome tour by itself", () => {
    // openTour() is a user action: the palette's Tour, the ? sheet, Settings →
    // General's Replay. An effect or a load-time call that opened it is the
    // old auto-tour coming back on top of the checklist card.
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
    expect(callers).toEqual([
      "components/palette/CommandPalette.tsx",
      "components/palette/ShortcutsSheet.tsx",
      "components/settings/screens/General.tsx",
    ]);
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
