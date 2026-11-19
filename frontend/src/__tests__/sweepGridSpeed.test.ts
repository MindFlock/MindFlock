/** UI sweep P1: grid render speed, pane tabs and their shortcuts.
 *
 *  - #1  App, the top bar, the sidebar and the grid subscribe to the store
 *        fields they read (no bare useUi()); a pane is memoized and reads its
 *        family off the instances cache, re-rendering only when it changes
 *  - #5  the pane header re-measures only when what it holds changes
 *  - #7  the Diff tab: parsed once per payload, never shows another request's
 *        payload under the current heading, drops late responses
 *  - #16 the history overlay and the chat poll rest while the page is hidden
 *  - #23 the Thread tab: Retry on a failed load, a stale note on a failed refresh
 *  - #31 Thread composer: Alt+Enter = When idle
 *  - #32 Ctrl+K G (Diff) and Ctrl+K Q (Queue); every chorded tab names its chord */

/// <reference types="vite/client" />
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Instance } from "../api/types";
import { code } from "./sourceText";

const { derivePaneFamily, familySnapshot, samePaneFamily } = await import("../components/grid/paneFamily");
const { diffForKey } = await import("../components/grid/DiffTab");
const { pollWhileVisible } = await import("../components/grid/visiblePoll");
const { threadLoadNote } = await import("../components/grid/ThreadTab");
const { CHORDS, chordLabel, setChordKey, resetChordOverride } = await import("../lib/keymap");
const { SESSION_ACTIONS, CHORD_ACTIONS, runChordAction } = await import("../lib/sessionActionTable");
const { queryClient } = await import("../state/queries");
const { useUi } = await import("../state/store");

const row = (title: string, o: Partial<Instance> = {}) => ({ title, status: "ready", ...o }) as Instance;

describe("#1 subscriptions: field selectors, not the whole store", () => {
  it("no bare useUi() in App, the top bar, the sidebar or the grid", () => {
    for (const f of [
      "App.tsx",
      "components/TopBar.tsx",
      "components/sidebar/Sidebar.tsx",
      "components/grid/TerminalGrid.tsx",
      "components/grid/Pane.tsx",
    ])
      expect(code(f), f).not.toContain("useUi()");
  });

  it("a pane is memoized and no longer reads the whole instances list", () => {
    const pane = code("components/grid/Pane.tsx");
    expect(pane).toContain("export const Pane = memo(PaneImpl);");
    expect(pane).not.toContain("useInstances(");
    expect(pane).toContain("const family = usePaneFamily(title);");
    expect(pane).toContain("memLocal ? family.memRank : -1");
  });

  it("the family snapshot keeps its identity while the family's rows are unchanged", () => {
    const lead = row("lead");
    const w1 = row("w1", { parent: "lead" } as Partial<Instance>);
    const other = row("other");
    let rows: Instance[] = [lead, w1, other];
    const get = familySnapshot("lead", () => rows);
    const first = get();
    expect(first.parent).toBe("");
    expect(first.children.map((c) => c.title)).toEqual(["w1"]);
    // A poll that only moved an unrelated row: same object, no re-render.
    rows = [lead, w1, row("other", { activity: "working" } as Partial<Instance>)];
    expect(get()).toBe(first);
    // The worker's row changed: a new value.
    const w1b = row("w1", { parent: "lead", activity: "working" } as Partial<Instance>);
    rows = [lead, w1b, other];
    const next = get();
    expect(next).not.toBe(first);
    expect(next.children[0]).toBe(w1b);
  });

  it("the memory rank is part of the family value", () => {
    const mem = (mb: number) => ({ memory: { mb } }) as unknown as Partial<Instance>;
    const rows = [row("a", mem(100)), row("b", mem(900)), row("c", mem(400))];
    expect(derivePaneFamily("a", rows).memRank).toBe(2);
    expect(derivePaneFamily("b", rows).memRank).toBe(0);
    const a = derivePaneFamily("a", rows);
    expect(samePaneFamily(a, { ...a, memRank: 1 })).toBe(false);
    expect(samePaneFamily(a, { ...a, children: [...a.children] })).toBe(true);
  });
});

describe("#5 the header re-measures only when what it holds changes", async () => {
  const { headMeasureSig } = await import("../components/grid/Pane");
  const head = (fitKey: string, icons: boolean, text: string, n = 3) => ({
    dataset: { fitKey },
    classList: { contains: (c: string) => icons && c === "head-icons" },
    childElementCount: n,
    textContent: text,
  });

  it("same contents, same signature; a key, icons, child or text change differs", () => {
    const base = headMeasureSig(head("k", false, "Agent Diff $1.20"));
    expect(headMeasureSig(head("k", false, "Agent Diff $1.20"))).toBe(base);
    expect(headMeasureSig(head("k2", false, "Agent Diff $1.20"))).not.toBe(base);
    expect(headMeasureSig(head("k", true, "Agent Diff $1.20"))).not.toBe(base);
    expect(headMeasureSig(head("k", false, "Agent Diff $10.20"))).not.toBe(base);
    expect(headMeasureSig(head("k", false, "Agent Diff $1.20", 4))).not.toBe(base);
  });
});

describe("#7 the Diff tab", () => {
  it("draws a payload only under the request it was loaded for", () => {
    const shown = { key: "s\0fork\0", payload: { content: "diff --git a b" }, error: "" };
    expect(diffForKey(shown, "s\0fork\0")).toEqual({ payload: shown.payload, error: "", switching: false });
    // Switched to Uncommitted: the old diff is not shown under the new heading.
    expect(diffForKey(shown, "s\0head\0")).toEqual({ payload: null, error: "", switching: true });
    // First load: nothing to switch from.
    expect(diffForKey(null, "s\0fork\0")).toEqual({ payload: null, error: "", switching: false });
  });

  it("drops a late response, parses once per payload, and is memoized", () => {
    const diff = code("components/grid/DiffTab.tsx");
    expect(diff).toContain("const seq = ++reqSeq.current;");
    expect(diff.match(/if \(seq !== reqSeq\.current\) return;/g)?.length).toBe(2);
    expect(diff).toContain("export const DiffTab = memo(DiffTabImpl);");
    expect(diff).toContain("const OneDiff = memo(function OneDiff(");
    expect(diff).toMatch(/const segments = useMemo\(\s*\(\) => \(payload\?\.content \? splitDiffByFile\(payload\.content\) : \[\]\),\s*\[payload\?\.content\]/);
    // A switch still on its way keeps the toolbar and says so.
    const switching = diff.split("if (!payload && !failure) {", 2)[1].split("if (failure || !payload) {", 1)[0];
    expect(switching).toContain("{reloadButton}");
    expect(switching).toContain("{modeButtons}");
    expect(switching).toContain("Loading diff…");
  });
});

describe("#16 polls rest while the page is hidden", () => {
  afterEach(() => vi.useRealTimers());

  it("skips ticks while hidden and catches up once when shown", () => {
    vi.useFakeTimers();
    const handlers: Array<() => void> = [];
    const doc = {
      hidden: false,
      addEventListener: (_: string, h: () => void) => handlers.push(h),
      removeEventListener: (_: string, h: () => void) => handlers.splice(handlers.indexOf(h), 1),
    };
    const run = vi.fn();
    const stop = pollWhileVisible(run, 3000, doc as unknown as Document);
    vi.advanceTimersByTime(3000);
    expect(run).toHaveBeenCalledTimes(1);
    doc.hidden = true;
    handlers.forEach((h) => h());
    vi.advanceTimersByTime(9000);
    expect(run).toHaveBeenCalledTimes(1);
    doc.hidden = false;
    handlers.forEach((h) => h());
    expect(run).toHaveBeenCalledTimes(2);
    stop();
    expect(handlers).toHaveLength(0);
    vi.advanceTimersByTime(9000);
    expect(run).toHaveBeenCalledTimes(2);
  });

  it("the history overlay's live tail and the chat poll use it", () => {
    expect(code("components/grid/HistoryOverlay.tsx")).toContain("return pollWhileVisible(() => {");
    expect(code("components/chat/ChatPanel.tsx")).toContain("return pollWhileVisible(pollChats, CHAT_POLL_MS);");
  });
});

describe("#23 the Thread tab says when a load failed, with a Retry", () => {
  it("failed with nothing shown, stale under messages, else nothing", () => {
    expect(threadLoadNote(false, "HTTP 500")).toBe("failed");
    expect(threadLoadNote(true, "HTTP 500")).toBe("stale");
    expect(threadLoadNote(true, "")).toBeNull();
    expect(threadLoadNote(false, "")).toBeNull();
  });

  it("both carry Retry; the stale note reads like the Map's", () => {
    const t = code("components/grid/ThreadTab.tsx");
    expect(t).toContain("Couldn't load the thread: {loadErr}");
    expect(t).toContain("thread may be out of date ·");
    expect(t.match(/className="th-retry" onClick=\{\(\) => void reload\(\)\}/g)?.length).toBe(2);
  });
});

describe("#31 Thread composer: Alt+Enter = When idle", () => {
  it("sends idle on Alt+Enter, and the hint and the button name the key", () => {
    const t = code("components/grid/ThreadTab.tsx");
    expect(t).toMatch(/else if \(composerKey\(e\) === "add"\) \{\s*e\.preventDefault\(\);\s*void send\("idle"\);/);
    expect(t).toContain("Ctrl+Enter sends now · Alt+Enter when idle");
    expect(t).toContain("the Queue tab's queue) (Alt+Enter)");
  });
});

describe("#32 Ctrl+K G opens Diff, Ctrl+K Q opens Queue", () => {
  afterEach(() => {
    queryClient.setQueryData(["instances"], []);
    resetChordOverride("g");
  });

  it("are table rows beside the Map's, so the ? sheet and rebinding list them", () => {
    expect(SESSION_ACTIONS.diff).toMatchObject({ label: "Diff", keys: "Ctrl+K G", chord: "g" });
    expect(SESSION_ACTIONS.queue).toMatchObject({ keys: "Ctrl+K Q", chord: "q" });
    expect(CHORD_ACTIONS.slice(CHORD_ACTIONS.indexOf("map"), CHORD_ACTIONS.indexOf("map") + 3)).toEqual([
      "map",
      "diff",
      "queue",
    ]);
    expect(CHORDS.g.desc).toBe("Diff — what this session changed");
    expect(CHORDS.q.desc).toBe("Queue prompt…");
    // No letter used twice.
    const letters = CHORD_ACTIONS.map((id) => SESSION_ACTIONS[id as keyof typeof SESSION_ACTIONS] as { chord?: string });
    const used = letters.map((a) => a.chord);
    expect(new Set(used).size).toBe(used.length);
  });

  it("Ctrl+K G lands the pane on its Diff tab", () => {
    queryClient.setQueryData(["instances"], [row("s1")]);
    runChordAction("diff", "s1");
    expect(useUi.getState().lastTab.s1).toBe("diff");
    expect(useUi.getState().diffFocus?.title).toBe("s1");
  });

  it("the tab tooltips name the chord, under the user's rebinding", () => {
    expect(chordLabel("g")).toBe("Ctrl+K G");
    setChordKey("g", "x");
    expect(chordLabel("g")).toBe("Ctrl+K X");
    const pane = code("components/grid/Pane.tsx");
    expect(pane).toContain('title={"Diff — what this session changed" + chordTip("g")}');
    expect(pane).toContain('blast radius" + chordTip("m")');
    expect(pane).toContain('keeps the session going across usage limits" + chordTip("q")');
    expect(pane).toContain('write to any of them as you" + chordTip("t")');
    // A memoized pane still redraws its tooltips on a rebinding.
    expect(pane).toContain("useSyncExternalStore(subscribeKeymap, keymapVersion, keymapVersion);");
  });
});
