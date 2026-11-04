/** Held offline after a crash on the rail (lib/heldOffline): the row's chip
 * says "Offline · Start" and is the Start button, the Start all bar counts
 * it, and the bell does not (one rail notice says it instead). Rendered like
 * sleepingState.test.ts. */

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
const { attentionItems } = await import("../components/sidebar/ordering");


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

const chip = (html: string) => /<(span|button)[^>]*class="stagechip ([^"]*)"[^>]*>(.*?)<\/\1>/.exec(html);

const HELD = { held_offline: true, held_since: 1_800_000_000, held_reason: "out of memory (killed)" };

describe("the rail row of a held session", () => {
  it("is an Offline · Start button whose tooltip says why", () => {
    const html = render(row({ activity: "offline", ...HELD } as Partial<Instance>));
    const c = chip(html)!;
    expect(c[1]).toBe("button");
    expect(c[2]).toContain("chip-held");
    expect(c[3].replace(/<[^>]+>/g, "")).toBe("Offline·Start");
    expect(html).toContain("Offline since out of memory (killed).");
    expect(html).not.toContain("↻ Restart");
  });

  it("a plain crashed agent keeps its Restart", () => {
    const html = render(row({ title: "dead", activity: "offline" }));
    expect(html).toContain("↻ Restart");
    expect(html).not.toContain("chip-held");
  });
});

describe("offlineRows — the Start all bar", () => {
  it("counts held sessions with the crashed ones", () => {
    const rows = [
      row({ title: "h1", activity: "offline", ...HELD } as Partial<Instance>),
      row({ title: "dead", activity: "offline" }),
      row({ title: "ok", activity: "idle" }),
      row({ title: "rig::h", device: "rig", activity: "offline", ...HELD } as Partial<Instance>),
    ];
    expect(offlineRows(rows).map((i) => i.title)).toEqual(["h1", "dead"]);
  });
});

describe("the bell", () => {
  it("leaves held sessions out — after a crash that would be every session at once", () => {
    const items = attentionItems([
      row({ title: "dead", activity: "offline" }),
      row({ title: "h1", activity: "offline", ...HELD } as Partial<Instance>),
    ]);
    expect(items.map((i) => i.title)).toEqual(["dead"]);
  });
});
