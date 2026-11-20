/** The rail row paints its dot AND its chip from ONE session state
 * (lib/sessionState) — the drift this pins against is a green dot beside an
 * "offline" chip. The vitest environment is node, so the row is rendered
 * through react-dom/server with the browser-only seams mocked. */

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
// react-dom/server insists on a server snapshot for useSyncExternalStore; the
// row's terminal-socket and code-map subscriptions pass none (they only ever
// render in a browser). Reading the client snapshot is what a browser does.
vi.mock("react", async (orig) => {
  const r = (await orig()) as typeof import("react");
  return {
    ...r,
    useSyncExternalStore: <T,>(sub: (cb: () => void) => () => void, get: () => T, getServer?: () => T) =>
      r.useSyncExternalStore(sub, get, getServer ?? get),
  };
});

const { queryClient } = await import("../state/queries");
const { SidebarRow, UncommittedMenu } = await import("../components/sidebar/SidebarRow");
const { deriveSessionState } = await import("../lib/sessionState");

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
        onScreen: true, // an off-screen asking row would mount its answer strip
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

describe("SidebarRow — dot and chip from the same state", () => {
  it.each(["working", "clarify", "limit", "offline", "idle"])("activity %s", (activity) => {
    const inst = row({ title: "same-" + activity, activity });
    const ss = deriveSessionState(inst);
    const html = render(inst);
    // The dot carries the state's tone and key…
    expect(dotClass(html)).toContain("tone-" + ss.tone);
    expect(dotClass(html)).toContain("st-" + ss.state);
    // …and the chip carries the SAME tone and the state's own word.
    const c = chip(html);
    expect(c, html).toBeTruthy();
    expect(c![2]).toContain("tone-" + ss.tone);
    expect(c![3]).toContain(ss.label);
  });

  it("a crashed agent is red on BOTH: no green dot beside an Offline chip", () => {
    const html = render(row({ title: "dead", status: "running", activity: "offline" }));
    expect(dotClass(html)).toContain("tone-broken");
    expect(dotClass(html)).not.toContain("tone-working");
    const c = chip(html)!;
    expect(c[1]).toBe("button"); // the chip IS the Restart action
    expect(c[2]).toContain("chip-restart");
    expect(c[3]).toContain("Offline");
    expect(c[3]).toContain("Restart");
  });

  it("a limited agent is blue (waiting), not red, and offers no Restart", () => {
    const html = render(row({ title: "lim", activity: "limit" }));
    expect(dotClass(html)).toContain("tone-waiting");
    const c = chip(html)!;
    expect(c[1]).toBe("span");
    expect(c[3]).toContain("Waiting on limit");
  });

  it("an idle finished branch shows the ship stage beside a neutral dot", () => {
    const html = render(row({ title: "done", activity: "idle", stage: "committed" }));
    expect(dotClass(html)).toContain("tone-neutral");
    expect(chip(html)![3]).toBe("Committed");
  });

  it("a needs-you chip is a door: a button that says where it goes", () => {
    const pc = chip(render(row({ title: "pc", activity: "idle", stage: "interrupt" })))!;
    expect(pc[1]).toBe("button");
    expect(pc[2]).toContain("chip-needs");
    expect(pc[3]).toBe("Pre-commit failed");
    const html = render(row({ title: "q", activity: "clarify" }));
    expect(html).toContain('aria-label="Question — answer it in the bell"');
    // An idle row's chip is still just a word.
    expect(chip(render(row({ title: "calm", activity: "idle" })))![1]).toBe("span");
  });

  it("a row idle on uncommitted changes says what it is, calmly, and offers Commit / Carry on", () => {
    const now = Date.now() / 1000;
    const dirty = {
      activity: "idle",
      activity_since: now - 25 * 60,
      diff_stat: { files: 3, additions: 120, deletions: 4, uncommitted: { files: 3, additions: 120, deletions: 4 } } as never,
    };
    const html = render(row({ title: "left", ...dirty }));
    const c = chip(html)!;
    expect(c[1]).toBe("button");
    expect(c[3]).toBe("3 files uncommitted");
    expect(c[2]).toContain("chip-uncommitted");
    expect(c[2]).toContain("tone-neutral");
    expect(c[2]).not.toContain("tone-attention");
    expect(c[2]).not.toContain("chip-needs");
    expect(dotClass(html)).toContain("tone-neutral");
    expect(html).toContain('aria-label="3 files uncommitted · idle 25m — Commit or tell the agent to carry on"');
    expect(html).toContain('aria-haspopup="menu"');
    expect(html).not.toMatch(/unfinished work|possibly stuck/);
    // Another device's row: the words only (its actions are on that device).
    const remote = chip(render(row({ title: "rig::left", device: "rig", ...dirty } as Partial<Instance>)))!;
    expect(remote[1]).toBe("span");
    expect(remote[3]).toBe("3 files uncommitted");
  });

  it("the uncommitted chip's menu is Commit… and Carry on", () => {
    const html = renderToStaticMarkup(createElement(UncommittedMenu, { title: "left", text: "3 files uncommitted · idle 25m", onDone: () => {} }));
    expect(html).toContain('<div class="sm-head">3 files uncommitted · idle 25m</div>');
    expect(html).toContain('data-menu="commit"');
    expect(html).toContain("Commit…");
    expect(html).toContain('data-menu="carry-on"');
    expect(html).toContain("Carry on");
  });

  it("a starting row pulses grey", () => {
    const html = render(row({ title: "new", status: "loading" }));
    expect(dotClass(html)).toContain("tone-muted");
    expect(dotClass(html)).toContain("st-starting");
    expect(chip(html)![3]).toBe("Starting");
  });
});
