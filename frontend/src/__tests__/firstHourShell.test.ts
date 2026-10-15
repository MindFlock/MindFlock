/** Round-2 "first hour" for the main screen and its glue: the calm rule,
 * the one setup count, the palette's ranking, the missing-agent state, the
 * install fallback and the browser-first shortcut sheet. */

import { describe, expect, it } from "vitest";
import type { SetupStatus, SetupStep } from "../api/types";
import { CALM_MAX_SESSIONS, NEWCOMER_DAYS, calmHidesBar, firstLaunchTour, isCalm, isNewcomer } from "../lib/calm";
import { agentMissing, agentMissingState } from "../lib/sessionState";
import { paletteCode, rankActions, type PaletteAction } from "../components/palette/paletteRank";
import { browserOrder, browserReserved } from "../components/palette/ShortcutsSheet";
import { fmtElapsed, manualCommands } from "../components/dialogs/InstallTerminal";
import { defaultHiddenBars, setCalmBarDefaults } from "../components/sidebar/barDefs";
import { code } from "./sourceText";

const NOW = Date.parse("2026-10-14T12:00:00Z");

function status(repo: Partial<SetupStep> | null): SetupStatus {
  const steps: SetupStep[] = repo
    ? [{ id: "repo", title: "Start your first agent", required: true, done: false, at: null, skipped: false, status: "todo", ...repo }]
    : [];
  return {
    steps,
    summary: { done: 0, total: 5, required_done: false, complete: false, next: "" },
    doctor: null,
    tailscale: null,
    logins: [],
    default_provider: "claude",
    report: "",
  };
}

describe("the calm first hour", () => {
  it("is a newcomer with no session yet, or a first session under a week old", () => {
    expect(isNewcomer(status({ done: false }), true, NOW)).toBe(true);
    const fresh = new Date(NOW - 2 * 86400_000).toISOString();
    expect(isNewcomer(status({ done: true, at: fresh }), true, NOW)).toBe(true);
    const old = new Date(NOW - (NEWCOMER_DAYS + 1) * 86400_000).toISOString();
    expect(isNewcomer(status({ done: true, at: old }), true, NOW)).toBe(false);
    // Done with no date: an install from before the checklist — a veteran.
    expect(isNewcomer(status({ done: true, at: null }), true, NOW)).toBe(false);
  });

  it("never guesses calm while the setup payload is in flight (a veteran never flashes empty)", () => {
    expect(isNewcomer(undefined, true, NOW)).toBeNull();
    expect(isNewcomer(undefined, undefined, NOW)).toBeNull();
    expect(isCalm({ sessions: 0, status: undefined, onboarded: true, now: NOW })).toBe(false);
    // …except the server's own "never onboarded": a genuinely fresh install.
    expect(isCalm({ sessions: 0, status: undefined, onboarded: false, now: NOW })).toBe(true);
  });

  it("ends at three sessions", () => {
    const s = status({ done: true, at: new Date(NOW - 3600_000).toISOString() });
    expect(isCalm({ sessions: CALM_MAX_SESSIONS - 1, status: s, now: NOW })).toBe(true);
    expect(isCalm({ sessions: CALM_MAX_SESSIONS, status: s, now: NOW })).toBe(false);
  });

  it("hides the usage strip, the Assistant and extension bars — by DEFAULT only", () => {
    expect(calmHidesBar("usage")).toBe(true);
    expect(calmHidesBar("assistant")).toBe(true);
    expect(calmHidesBar("ext:dbclient")).toBe(true);
    expect(calmHidesBar("ingestion")).toBe(false);
    setCalmBarDefaults(["usage", "assistant", "ext:dbclient"]);
    expect(defaultHiddenBars()).toEqual(expect.arrayContaining(["usage", "assistant", "ext:dbclient"]));
    setCalmBarDefaults([]);
    expect(defaultHiddenBars()).not.toContain("usage");
    expect(defaultHiddenBars()).not.toContain("assistant");
  });

  it("opens the welcome tour once, on a genuinely first launch", () => {
    const first = status({ done: false });
    expect(firstLaunchTour({ seen: false, sessions: 0, status: first })).toBe(true);
    expect(firstLaunchTour({ seen: true, sessions: 0, status: first })).toBe(false);
    expect(firstLaunchTour({ seen: false, sessions: 1, status: first })).toBe(false);
    expect(firstLaunchTour({ seen: false, sessions: 0, status: status({ done: true, at: null }) })).toBe(false);
    expect(firstLaunchTour({ seen: false, sessions: 0, status: undefined, onboarded: true })).toBe(false);
    expect(firstLaunchTour({ seen: false, sessions: 0, status: undefined, onboarded: false })).toBe(true);
  });
});

describe("a session whose agent isn't installed", () => {
  const logins = [
    { name: "claude", installed: false },
    { name: "codex", installed: true },
  ];

  it("says Failed: <agent> isn't installed instead of Idle/Offline", () => {
    expect(agentMissing({ title: "a", provider: "claude" }, logins, "idle")).toBe("claude");
    expect(agentMissing({ title: "a", provider: "claude" }, logins, "offline")).toBe("claude");
    const ss = agentMissingState("claude");
    expect(ss.label).toBe("Failed: claude isn't installed");
    expect(ss.restartable).toBe(false);
    expect(ss.inBell).toBe(true);
  });

  it("never flags an installed agent, a running one, a remote row or an unknown provider", () => {
    expect(agentMissing({ title: "a", provider: "codex" }, logins, "idle")).toBeNull();
    expect(agentMissing({ title: "a", provider: "claude" }, logins, "working")).toBeNull();
    expect(agentMissing({ title: "a", provider: "claude", device: "rig" }, logins, "idle")).toBeNull();
    expect(agentMissing({ title: "a", provider: "claudekiro" }, logins, "idle")).toBeNull();
    expect(agentMissing({ title: "a", provider: "claude" }, null, "idle")).toBeNull();
  });
});

describe("the palette", () => {
  const act = (label: string, extra: Partial<PaletteAction> = {}): PaletteAction => ({ label, run: () => {}, ...extra });

  it("never makes Push (or any outward verb) the Enter default for a typed session name", () => {
    const actions = [
      act("Push — readme-setup", { outward: true }),
      act("Delete… — readme-setup", { outward: true }),
      act("Focus: readme-setup"),
    ];
    const out = rankActions(actions, "readme");
    expect(out[0].label).toBe("Focus: readme-setup");
    expect(out.map((a) => a.label)).toContain("Push — readme-setup");
  });

  it("finds commands by plain synonyms", () => {
    const actions = [act("Keyboard shortcuts"), act("Tour"), act("Open Doctor"), act("Open Setup checklist"), act("Zones…")];
    expect(rankActions(actions, "help").map((a) => a.label)).toEqual(["Keyboard shortcuts", "Tour"]);
    expect(rankActions(actions, "install").map((a) => a.label).sort()).toEqual(["Open Doctor", "Open Setup checklist"]);
  });

  it("finds New scheduled template… by the words people use for it, phrases too", () => {
    const sched = act("New scheduled template…", { keywords: "schedule a session recurring cron every day nightly weekly" });
    const actions = [act("New session…"), sched, act("Templates…")];
    for (const q of ["recurring", "cron", "every day", "nightly", "weekly", "Every Day"])
      expect(rankActions(actions, q)[0]?.label, q).toBe("New scheduled template…");
    // The old label still finds it
    expect(rankActions(actions, "schedule a session")[0]?.label).toBe("New scheduled template…");
    expect(rankActions(actions, "every night").map((a) => a.label)).not.toContain("New scheduled template…");
    expect(code("components/palette/CommandPalette.tsx")).toContain(
      'keywords: "schedule a session recurring cron every day nightly weekly"'
    );
  });

  it("opens on a short menu plus All commands…, not the whole index", () => {
    const actions = [act("Zones…"), act("Tour", { top: 6 }), act("New session…", { top: 0 }), act("People…")];
    const all = act("All commands…", { keepOpen: true });
    const out = rankActions(actions, "", { allRow: () => all });
    expect(out.map((a) => a.label)).toEqual(["New session…", "Tour", "All commands…"]);
    expect(rankActions(actions, "", { allRow: () => all, showAll: true })).toHaveLength(4);
  });

  it("offers Join with code only for a real code shape", () => {
    for (const word of ["worktree", "parallel", "run template", "max sessions", "notification"])
      expect(paletteCode(word).kind, word).toBe("");
    expect(paletteCode("ABCD-EFGH").kind).toBe("device");
    expect(paletteCode("laptop ABCD-EFGH").kind).toBe("device");
  });
});

describe("install fallback", () => {
  it("lists the commands to run by hand, and times the run", () => {
    expect(
      manualCommands([
        { id: "claude-cli", label: "agent CLI (claude)", cmd: "curl -fsSL https://claude.ai/install.sh | bash" },
        { id: "x", label: "x", cmd: "" },
      ])
    ).toBe("curl -fsSL https://claude.ai/install.sh | bash");
    expect(fmtElapsed(42)).toBe("0:42");
    expect(fmtElapsed(125)).toBe("2:05");
  });
});

describe("the shortcut sheet in a browser tab", () => {
  it("knows the keys a browser keeps", () => {
    for (const k of ["Ctrl+N", "Ctrl+W", "Ctrl+Tab", "Ctrl+Shift+T", "Ctrl+1 … 9", "Ctrl+PgDn"]) expect(browserReserved(k), k).toBe(true);
    for (const k of ["Alt+N", "Delete", "Ctrl+K", "Alt+I", "Ctrl+B", "?"]) expect(browserReserved(k), k).toBe(false);
  });

  it("leads with what works in the tab and marks desktop-only rows", () => {
    const r = browserOrder("Ctrl+N / Alt+N", "New session", true);
    expect(r.combos.map((c) => c.combo)).toEqual(["Alt+N", "Ctrl+N"]);
    expect(r.combos[1].desktop).toBe(true);
    expect(r.desktopOnly).toBe(false);
    expect(browserOrder("Ctrl+Tab / Ctrl+Shift+Tab", "Next / previous window", true).desktopOnly).toBe(true);
    expect(browserOrder("F11 / ⌃⌘F", "Toggle fullscreen (desktop app)", true).desktopOnly).toBe(true);
    // In the desktop app nothing is reordered or marked.
    const d = browserOrder("Ctrl+N / Alt+N", "New session", false);
    expect(d.combos.map((c) => c.combo)).toEqual(["Ctrl+N", "Alt+N"]);
    expect(d.desktopOnly).toBe(false);
  });
});

describe("Verify toasts from the main screen's glue", () => {
  it("says a pre-merge check ran on the branch", async () => {
    const { checkedToastText } = await import("../state/queries");
    expect(checkedToastText({ title: "fix-login", before_merge: true })).toBe(
      "fix-login checked on the branch — everything passed"
    );
    expect(checkedToastText({ title: "fix-login", failed: 1 })).toBe("fix-login checked — 1 step failed");
  });
});
