/** Sleeping agents on the rail (backend/web/core/sleep.py): a session whose
 * agent was stopped on purpose, idle, to give its memory back. Its activity
 * reads "offline", but it is not broken — the row says Sleeping (calm, muted,
 * a Wake action), it is outside the "N agents offline · Restart all" bar,
 * and it never lands in the bell. Rendered like sidebarRowState.test.ts. */

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { QueryClientProvider } from "@tanstack/react-query";
import { describe, expect, it, vi } from "vitest";
import type { Instance } from "../api/types";

vi.mock("../api/client", async (orig) => ({
  ...((await orig()) as object),
  api: vi.fn(() => Promise.resolve({})),
  instApi: vi.fn(() => Promise.resolve({})),
}));
vi.mock("../lib/toast", () => ({ toast: vi.fn() }));
vi.mock("../lib/sessionActions", () => ({
  PR_FALLBACK_HINT: "",
  cleanupMissing: vi.fn(),
  commitSession: vi.fn(),
  copySession: vi.fn(),
  hasPrSupport: () => true,
  hideSession: vi.fn(),
  ideSession: vi.fn(),
  closeSession: vi.fn(),
  makePrSession: vi.fn(),
  mergeSession: vi.fn(),
  pauseSession: vi.fn(),
  pushSession: vi.fn(),
  resumeSession: vi.fn(),
  selectSession: vi.fn(),
}));
vi.mock("../lib/flockActions", () => ({ openThread: vi.fn(), pasteWrapup: vi.fn() }));
vi.mock("../lib/keymap", () => ({ isEditingTarget: () => false }));
vi.mock("react", async (orig) => {
  const r = (await orig()) as typeof import("react");
  return {
    ...r,
    useSyncExternalStore: <T,>(sub: (cb: () => void) => () => void, get: () => T, getServer?: () => T) =>
      r.useSyncExternalStore(sub, get, getServer ?? get),
  };
});

const { queryClient } = await import("../state/queries");
const { SidebarRow } = await import("../components/sidebar/SidebarRow");
const { offlineRows } = await import("../components/sidebar/OfflineBar");
const { deriveSessionState, effectiveActivity, forceActivity, noteActivity, notifyRuleFor } = await import(
  "../lib/sessionState"
);
const { attentionItems } = await import("../components/sidebar/ordering");
const { chipState } = await import("../lib/stage");

const NOW = 1_800_000_000_000; // epoch ms
const asleep = { since: NOW / 1000 - 50 * 60, reason: "idle", freed_mb: 780 };

const row = (o: Partial<Instance>): Instance =>
  ({
    title: "s",
    branch: "feat/x",
    repo: "r",
    folder: "/tmp/r",
    program: "claude",
    status: "running",
    stage: "agent",
    activity: "idle",
    activity_since: 0,
    diff_stat: null,
    queue: null,
    setup: null,
    check: null,
    last_turn: null,
    ...o,
  }) as unknown as Instance;

function render(inst: Instance): string {
  return renderToStaticMarkup(
    createElement(
      QueryClientProvider,
      { client: queryClient },
      createElement(SidebarRow, {
        inst,
        idx: 0,
        onScreen: false,
        dropCue: null,
        onDragState: () => {},
        onDropCue: () => {},
        onDropRow: () => {},
      })
    )
  );
}

const dotClass = (html: string) => /class="dot ([^"]*)"/.exec(html)?.[1] || "";
const chip = (html: string) => /<(span|button)[^>]*class="stagechip ([^"]*)"[^>]*>(.*?)<\/\1>/.exec(html);

describe("deriveSessionState — a sleeping agent", () => {
  it("is Sleeping: calm, not in the bell, no Restart — though its activity reads offline", () => {
    const ss = deriveSessionState(row({ activity: "offline", sleeping: asleep }), { now: NOW, activity: "offline" });
    expect(ss.label).toBe("Sleeping");
    expect(ss.state).toBe("idle");
    expect(ss.tone).toBe("muted");
    expect(ss.inBell).toBe(false);
    expect(ss.restartable).toBe(false);
    expect(notifyRuleFor(ss)).toBe("");
  });

  it("says since when, why, and how much memory it gave back", () => {
    const hint = deriveSessionState(row({ activity: "offline", sleeping: asleep }), { now: NOW }).hint || "";
    expect(hint).toContain("50m");
    expect(hint).toContain("idle");
    expect(hint).toContain("780 MB");
    expect(hint).toContain("wakes it");
    const mem = deriveSessionState(
      row({ activity: "offline", sleeping: { since: NOW / 1000 - 300, reason: "memory", freed_mb: 2048 } }),
      { now: NOW }
    ).hint;
    expect(mem).toContain("memory was low");
    expect(mem).toContain("2.0 GB");
  });

  it("paused and missing still win (neither is a sleep)", () => {
    expect(deriveSessionState(row({ status: "paused", sleeping: asleep })).label).toBe("Paused");
    expect(deriveSessionState(row({ workspace_missing: true, sleeping: asleep })).label).toBe("Missing");
  });

  it("the chip keeps its own word, not the ship stage", () => {
    const c = chipState(row({ activity: "offline", stage: "committed", sleeping: asleep }));
    expect(c.label).toBe("Sleeping");
    expect(c.cls).toContain("s-sleeping");
    expect(c.cls).toContain("tone-muted");
  });
});

describe("the rail row", () => {
  it("paints a muted dot and a Sleeping chip whose action is Wake", () => {
    const html = render(row({ title: "zz", activity: "offline", sleeping: asleep }));
    expect(dotClass(html)).toContain("tone-muted");
    expect(dotClass(html)).not.toContain("tone-broken");
    const c = chip(html)!;
    expect(c[1]).toBe("button");
    expect(c[2]).toContain("chip-wake");
    expect(c[3]).toContain("Sleeping");
    expect(c[3]).toContain("Wake");
    expect(c[3]).not.toContain("Restart");
  });

  it("a remote device's sleeping row is read-only here: no Wake button", () => {
    const html = render(row({ title: "rig::zz", device: "rig", activity: "offline", sleeping: asleep } as Partial<Instance>));
    const c = chip(html)!;
    expect(c[1]).toBe("span");
    expect(c[3]).toBe("Sleeping");
  });
});

describe("offlineRows — the Restart all bar", () => {
  it("counts crashed agents and never a sleeping one", () => {
    const rows = [
      row({ title: "dead1", activity: "offline" }),
      row({ title: "dead2", activity: "offline" }),
      row({ title: "zz1", activity: "offline", sleeping: asleep }),
      row({ title: "zz2", activity: "offline", sleeping: asleep }),
      row({ title: "ok", activity: "idle" }),
    ];
    expect(offlineRows(rows).map((i) => i.title)).toEqual(["dead1", "dead2"]);
  });

  it("an all-asleep flock shows no bar at all", () => {
    expect(offlineRows([row({ title: "a", activity: "offline", sleeping: asleep }), row({ title: "b", activity: "offline", sleeping: asleep })])).toEqual([]);
  });
});

describe("the bell", () => {
  it("lists a crashed agent but not a sleeping one", () => {
    const items = attentionItems([
      row({ title: "dead", activity: "offline" }),
      row({ title: "zz", activity: "offline", sleeping: asleep }),
    ]);
    expect(items.map((i) => i.title)).toEqual(["dead"]);
  });
});

describe("the activity debounce across a wake", () => {
  it("a sleeping row drops the debounced reading, so the wake shows at once", () => {
    forceActivity("wk", "offline");
    noteActivity(row({ title: "wk", activity: "offline", sleeping: asleep }));
    // Woken: the first row is shown as-is, not held on "offline" two polls.
    noteActivity(row({ title: "wk", activity: "idle" }));
    expect(effectiveActivity(row({ title: "wk", activity: "idle" }))).toBe("idle");
  });
});
