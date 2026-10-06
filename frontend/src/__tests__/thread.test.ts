/** The Thread tab (grid/ThreadTab.tsx) — its pure half, lib/thread.ts — and
 * the one keyboard rule it needs from lib/keymap: Delete and Ctrl+W typed in
 * its composer are text editing, never "end the focused session".
 *
 * What is pinned: the tab shows only for a family (or on purpose); the badge
 * is needs-you + reports newer than last seen; worker rows sort needs-you
 * first; the header's summary; the log's spawn grouping, Reports filter and
 * delivery wording (it only REPORTS a message's state); the To chips; and the
 * send routing — a recipient on a dialog or at the usage limit is queued,
 * never typed into, and with nobody free "Send now" becomes "When it's
 * free". */
import { afterEach, describe, expect, it, vi } from "vitest";
import type { FinishedChild, Instance, ThreadItem, ThreadMember } from "../api/types";

const killSession = vi.fn();
vi.mock("../lib/sessionActions", async (orig) => ({
  ...((await orig()) as object),
  killSession: (...a: unknown[]) => killSession(...a),
}));

const {
  ALL_WORKERS,
  NOT_FREE,
  chipFor,
  clockTime,
  codeSpans,
  composeChips,
  composePlaceholder,
  claimComposeRequest,
  decidePrompt,
  deliveryText,
  diffText,
  familyOf,
  quoteTitle,
  forkPoint,
  finishedStatus,
  headerSummary,
  logEntries,
  progressOf,
  mergeOlder,
  newestReportTs,
  normThread,
  reportedCount,
  sendPlan,
  threadBadge,
  threadTabShown,
  toolName,
  workerRows,
  workerStatus,
} = await import("../lib/thread");
const { KEYMAP, installKeymap, threadComposerFocused } = await import("../lib/keymap");
const { useUi } = await import("../state/store");

const NOW = 1_800_000_000;
const row = (o: Partial<Instance>): Instance => ({ title: "x", activity: "idle", ...o }) as Instance;
const act = (r: Partial<Instance>) => String(r.activity || "idle");

const FAMILY: Instance[] = [
  row({ title: "api", activity: "working" }),
  row({ title: "api-billing", parent: "api", activity: "idle", last_report: { id: "m1", status: "done", summary: "Added limits", ts: NOW - 240 } }),
  row({ title: "api-search", parent: "api", activity: "clarify", activity_since: NOW - 120 }),
  row({ title: "api-upload", parent: "api", activity: "working", activity_since: NOW - 360 }),
  row({ title: "web", activity: "idle" }),
];

describe("familyOf / threadTabShown", () => {
  it("finds the live parent and the workers, in rail order", () => {
    const f = familyOf("api", FAMILY);
    expect(f.parent).toBe("");
    expect(f.children.map((c) => c.title)).toEqual(["api-billing", "api-search", "api-upload"]);
    expect(familyOf("api-search", FAMILY)).toEqual({ parent: "api", children: [] });
  });

  it("ignores a dead parent link and another device's rows", () => {
    const rows = [row({ title: "w", parent: "gone" }), row({ title: "r", parent: "w", device: "laptop" })];
    expect(familyOf("w", rows)).toEqual({ parent: "", children: [] });
  });

  it("shows the tab for a family, or when opened on purpose — never otherwise", () => {
    const shown = (t: string, opened = false) => {
      const f = familyOf(t, FAMILY);
      return threadTabShown(!!f.parent || f.children.length > 0, opened);
    };
    expect(shown("api")).toBe(true);
    expect(shown("api-upload")).toBe(true);
    expect(shown("web")).toBe(false);
    expect(shown("web", true)).toBe(true);
  });
});

describe("threadBadge", () => {
  const kids = familyOf("api", FAMILY).children;

  it("counts needs-you plus reports newer than last seen (ms vs report seconds)", () => {
    expect(threadBadge(kids, 0, act)).toEqual({ count: 2, needs: 1, fresh: 1 });
    // Seen after the report landed: only the needs-you worker is left.
    expect(threadBadge(kids, (NOW - 60) * 1000, act)).toEqual({ count: 1, needs: 1, fresh: 0 });
  });

  it("is 0 for a session without workers", () => {
    expect(threadBadge([], 0, act).count).toBe(0);
  });

  it("moves newestReportTs when a report lands (the open tab re-marks itself seen)", () => {
    expect(newestReportTs(kids)).toBe(NOW - 240);
    expect(newestReportTs([])).toBe(0);
  });
});

const MEMBERS: ThreadMember[] = ["api-billing", "api-search", "api-upload"].map((t) => ({
  title: t,
  role: "child",
  status: "running",
  activity: "idle",
  activity_since: 0,
  branch: "you/" + t,
  diff_stat: null,
  created_at: NOW - 900,
  last_report: null,
  base_sha: "3f2c1a0d9e8b", // pragma: allowlist secret
}));

describe("worker rows and the header", () => {
  const rows = workerRows(familyOf("api", FAMILY).children, MEMBERS, act);

  it("puts needs-you first, then reported, then working", () => {
    expect(rows.map((r) => [r.title, r.state])).toEqual([
      ["api-search", "ask"],
      ["api-billing", "done"],
      ["api-upload", "working"],
    ]);
    expect(reportedCount(rows)).toBe(1);
  });

  it("summarises the family like the mockup", () => {
    expect(forkPoint(rows)).toBe("3f2c1a0");
    expect(headerSummary(rows).map((p) => p.text)).toEqual([
      "3 workers forked from ",
      "3f2c1a0",
      "1 needs your answer",
      "1 reported",
      "1 working",
    ]);
    expect(headerSummary(rows).find((p) => p.text.includes("needs"))!.cls).toBe("needs");
  });

  it("drops the fork point when the workers did not share one", () => {
    const mixed = MEMBERS.map((m, i) => ({ ...m, base_sha: i ? "aaaaaaa" : "bbbbbbb" }));
    expect(forkPoint(workerRows(familyOf("api", FAMILY).children, mixed, act))).toBe("");
  });

  it("words each status line", () => {
    const [ask, done, work] = rows;
    expect(workerStatus(ask, "api", NOW)).toEqual({
      word: "needs your answer",
      cls: "needs",
      detail: "asking for 2m · api is waiting on it",
    });
    expect(workerStatus(done, "api", NOW)).toMatchObject({ word: "reported done", cls: "ok", detail: "4m ago" });
    expect(workerStatus(work, "api", NOW)).toMatchObject({ word: "working", detail: "6m · no report yet" });
    expect(diffText({ files: 2, additions: 41, deletions: 3 } as never)).toBe("+41 −3 · 2 files");
    expect(diffText(null)).toBe("");
  });

  it("fills a worker's report from the thread when the row has none", () => {
    const kid = row({ title: "k", parent: "api", activity: "idle" });
    delete (kid as Partial<Instance>).last_report;
    const m = { ...MEMBERS[0], title: "k", last_report: { id: "r", status: "blocked", summary: "s", ts: NOW } };
    expect(workerRows([kid], [m], act)[0].state).toBe("blocked");
  });
});

const item = (o: Partial<ThreadItem>): ThreadItem => ({
  type: "message",
  id: "i" + Math.random(),
  ts: NOW,
  from: "api",
  to: "api-billing",
  text: "",
  status: null,
  state: null,
  base_sha: null,
  ...o,
});

describe("the Between sessions log", () => {
  const items = [
    item({ type: "spawn", id: "s1", ts: NOW, to: "api-billing", text: "Rate-limit `services/billing/`" }),
    item({ type: "spawn", id: "s2", ts: NOW + 5, to: "api-search" }),
    item({ type: "spawn", id: "s3", ts: NOW + 9, to: "api-upload" }),
    item({ id: "m1", ts: NOW + 30, from: "api", to: "api-search", text: "use redis?" , state: "read"}),
    item({ type: "result", id: "r1", ts: NOW + 600, from: "api-billing", to: "api", status: "done", state: "read" }),
  ];

  it("folds an orchestrator's burst of spawns into one card", () => {
    const e = logEntries(items, "all");
    expect(e.map((x) => [x.kind, x.to.join(",")])).toEqual([
      ["spawn", "api-billing,api-search,api-upload"],
      ["message", "api-search"],
      ["result", "api"],
    ]);
    expect(e[0].items).toHaveLength(3);
  });

  it("keeps spawns minutes apart on separate cards", () => {
    const e = logEntries([items[0], { ...items[1], ts: NOW + 600 }], "all");
    expect(e).toHaveLength(2);
  });

  it("Reports shows results only", () => {
    expect(logEntries(items, "reports").map((x) => x.key)).toEqual(["r1"]);
  });

  it("reports delivery without changing it", () => {
    expect(deliveryText("read", "api")).toBe("read by api");
    expect(deliveryText("delivered", "api")).toBe("delivered to api");
    expect(deliveryText("pending", "api")).toBe("waiting for api");
    expect(deliveryText("held", "api")).toContain("held for api");
    expect(deliveryText(null, "api")).toBe("");
  });

  it("sets backtick spans in mono", () => {
    expect(codeSpans("touch `a/b` only")).toEqual([
      { code: false, text: "touch " },
      { code: true, text: "a/b" },
      { code: false, text: " only" },
    ]);
  });

  it("prepends an older page without doubling a row", () => {
    const cur = [items[3], items[4]];
    expect(mergeOlder([items[2], items[3]], cur).map((i) => i.id)).toEqual(["s3", "m1", "r1"]);
  });

  it("formats today as a clock and older days with the date", () => {
    const now = new Date(2026, 9, 4, 12, 0);
    expect(clockTime(new Date(2026, 9, 4, 10, 3).getTime() / 1000, now)).toBe("10:03");
    expect(clockTime(new Date(2026, 9, 3, 9, 5).getTime() / 1000, now)).toBe("Oct 3 09:05");
    expect(clockTime(0, now)).toBe("");
  });
});

describe("normThread", () => {
  it("never throws on a half-built body", () => {
    expect(normThread(null, "t")).toEqual({ title: "t", parent: "", members: [], finished: [], items: [], more: false, order: null });
    const t = normThread({ members: [{ title: "a", base_sha: "" }, {}], items: [{ id: "x", type: "result" }, { id: "" }], more: true });
    expect(t.members.map((m) => [m.title, m.base_sha])).toEqual([["a", null]]);
    expect(t.items.map((i) => i.id)).toEqual(["x"]);
    expect(t.more).toBe(true);
  });
});

describe("the composer", () => {
  const chips = composeChips("api", "", ["api-billing", "api-search", "api-upload"], (t) => t);

  it("offers the session, each worker and all workers", () => {
    expect(chips.map((c) => c.label)).toEqual(["api", "api-billing", "api-search", "api-upload", "all workers"]);
    expect(chipFor(chips, ALL_WORKERS).titles).toHaveLength(3);
    // A worker's own Thread: itself and its parent, no "all workers".
    expect(composeChips("api-search", "api", [], (t) => t).map((c) => c.key)).toEqual(["api-search", "api"]);
  });

  it("addresses the session itself when the requested target is not in the family", () => {
    expect(chipFor(chips, "gone").key).toBe("api");
    expect(chipFor(chips, "api-search").key).toBe("api-search");
  });

  it("names the target in the placeholder", () => {
    expect(composePlaceholder(chips[0])).toBe("Message api — typed into its prompt as you");
    expect(composePlaceholder(chipFor(chips, ALL_WORKERS))).toBe(
      "Message all 3 workers — typed into each prompt as you"
    );
  });

  it("never types into a dialog: clarify / limit recipients are queued", () => {
    const actOf = (t: string) => FAMILY.find((r) => r.title === t)?.activity || "idle";
    expect([...NOT_FREE].sort()).toEqual(["clarify", "limit"]);
    expect(sendPlan(["api-search"], actOf)).toEqual({ now: [], later: ["api-search"], label: "When it's free" });
    expect(sendPlan(["api-upload"], actOf)).toEqual({ now: ["api-upload"], later: [], label: "Send now" });
    // All workers: the free ones get it now, the one on a prompt later.
    expect(sendPlan(["api-billing", "api-search", "api-upload"], actOf)).toEqual({
      now: ["api-billing", "api-upload"],
      later: ["api-search"],
      label: "Send now",
    });
    expect(sendPlan(["x"], () => "limit").label).toBe("When it's free");
  });

  it("asks the orchestrator to decide in one short paragraph naming real tools", () => {
    const p = decidePrompt("api-search", { question: "Do you want to proceed?", command: "uv add redis" }, "claude");
    expect(p).toContain('"api-search"');
    expect(p).toContain("uv add redis — Do you want to proceed?");
    expect(p).toContain("mcp__mindflock__read_output");
    expect(p).toContain("mcp__mindflock__answer_prompt");
    expect(p).not.toContain("\n");
    expect(p.length).toBeLessThanOrEqual(600);
    expect(decidePrompt("w", null, "codex")).toContain(" answer_prompt ");
    expect(toolName("get_diff", "codex")).toBe("get_diff");
    // A huge question is cut, so the paste never collapses to "[Pasted text]".
    expect(decidePrompt("w", { question: "x".repeat(5000) }, "claude").length).toBeLessThanOrEqual(600);
  });
});

// --- Delete / Ctrl+W in the composer --------------------------------------------------

describe("Delete and Ctrl+W typed in the Thread composer never end the session", () => {
  const g = globalThis as Record<string, unknown>;
  const hadDoc = "document" in g;
  const prevDoc = g.document;
  afterEach(() => {
    useUi.setState({ focused: null } as never);
    killSession.mockReset();
    if (hadDoc) g.document = prevDoc;
    else delete g.document;
  });

  /** A fake document whose keyboard is on `active`; captures the keymap's
   * capture-phase listener so a key can be dispatched through the REAL
   * dispatcher. */
  const fakeDoc = (active: unknown) => {
    const listeners: ((e: KeyboardEvent) => void)[] = [];
    g.document = {
      activeElement: active,
      getElementById: () => null,
      addEventListener: (_t: string, fn: (e: KeyboardEvent) => void) => listeners.push(fn),
      removeEventListener: () => {},
    };
    return listeners;
  };
  const el = (tag: string, inside: string | null) => ({
    tagName: tag,
    isContentEditable: false,
    closest: (sel: string) => (inside && sel === inside ? {} : null),
  });
  const host = {
    togglePalette() {},
    toggleShortcuts() {},
    focusFilter() {},
    cycleWindow() {},
    rowAt: () => null,
    openDoctor() {},
  };
  const press = (listeners: ((e: KeyboardEvent) => void)[], key: string, ctrl: boolean) => {
    const ev = {
      key,
      ctrlKey: ctrl,
      metaKey: false,
      shiftKey: false,
      altKey: false,
      defaultPrevented: false,
      preventDefault() {
        this.defaultPrevented = true;
      },
      stopPropagation() {},
    };
    for (const fn of listeners) fn(ev as unknown as KeyboardEvent);
    return ev;
  };

  it("the composer textarea counts as the Thread composer; a terminal or a plain button doesn't", () => {
    fakeDoc(el("TEXTAREA", ".thread-compose"));
    expect(threadComposerFocused()).toBe(true);
    fakeDoc(el("TEXTAREA", ".xterm"));
    expect(threadComposerFocused()).toBe(false);
    fakeDoc(el("BUTTON", ".thread-compose"));
    expect(threadComposerFocused()).toBe(false);
  });

  it("Ctrl+W and Delete go to the textarea, and killSession is never called", () => {
    useUi.setState({ focused: "api" } as never);
    const listeners = fakeDoc(el("TEXTAREA", ".thread-compose"));
    const off = installKeymap(host);
    try {
      const w = press(listeners, "w", true);
      const del = press(listeners, "Delete", false);
      expect(killSession).not.toHaveBeenCalled();
      // Not swallowed either: the keys reach the text box.
      expect(w.defaultPrevented).toBe(false);
      expect(del.defaultPrevented).toBe(false);
    } finally {
      off();
    }
  });

  it("…while Ctrl+W from the terminal still ends the focused session", () => {
    useUi.setState({ focused: "api" } as never);
    const listeners = fakeDoc(el("TEXTAREA", ".xterm"));
    const off = installKeymap(host);
    try {
      press(listeners, "w", true);
      expect(killSession).toHaveBeenCalledWith("api");
    } finally {
      off();
    }
  });

  it("anywhere in the Thread tab — a button that kept focus — Delete and Ctrl+W stand down", () => {
    // All/Reports, Show older, Show all N, a worker's Review diff: a click
    // leaves the focus on the button, and Delete must not end the orchestrator.
    useUi.setState({ focused: "api" } as never);
    const listeners = fakeDoc(el("BUTTON", ".thread-root"));
    const off = installKeymap(host);
    try {
      press(listeners, "Delete", false);
      press(listeners, "w", true);
      expect(killSession).not.toHaveBeenCalled();
    } finally {
      off();
    }
    expect(KEYMAP.find((b) => b.aliasOf === "close")!.when!()).toBe(false);
    expect(KEYMAP.find((b) => b.id === "close")!.when!()).toBe(false);
  });

  it("the close binding and its Delete alias both stand down", () => {
    useUi.setState({ focused: "api" } as never);
    fakeDoc(el("TEXTAREA", ".thread-compose"));
    expect(KEYMAP.find((b) => b.id === "close")!.when!()).toBe(false);
    expect(KEYMAP.find((b) => b.aliasOf === "close")!.when!()).toBe(false);
  });
});

describe("threadOpen addresses the composer", () => {
  it("bumps seq on every open so a second Ctrl+K S still takes the caret", () => {
    const ui = useUi.getState();
    ui.threadOpen("api", { composeTo: "api-search" });
    const a = useUi.getState().threadComposeTarget!;
    ui.threadOpen("api", { composeTo: "api-search" });
    const b = useUi.getState().threadComposeTarget!;
    expect([a.title, a.to, b.to]).toEqual(["api", "api-search", "api-search"]);
    expect(b.seq).toBeGreaterThan(a.seq);
    expect(useUi.getState().lastTab["api"]).toBe("thread");
  });
});

describe("a threadOpen request is acted on once", () => {
  it("not again when the tab comes back on screen or the pane remounts", () => {
    // Ctrl+K S on api, pick a worker's chip, peek at the Agent tab, come back:
    // the request must not readdress the composer (nor steal the caret).
    expect(claimComposeRequest("api", 7)).toBe(true);
    expect(claimComposeRequest("api", 7)).toBe(false);
    // A NEW request (another Ctrl+K S) is.
    expect(claimComposeRequest("api", 8)).toBe(true);
    expect(claimComposeRequest("api", 0)).toBe(false);
    // Per session.
    expect(claimComposeRequest("api-search", 7)).toBe(true);
  });
});

describe("quoteTitle (Let api decide, the server's playbook quoting)", () => {
  it("quotes the title exactly, as the server's playbooks do", () => {
    expect(quoteTitle("api  search")).toBe('"api  search"');
    expect(quoteTitle('say "hi"\nnow')).toBe("\"say 'hi' now\"");
    const long = quoteTitle("w".repeat(200));
    expect(long.length).toBe(122);
    expect(long.endsWith('…"')).toBe(true);
  });
});

describe("the shared children-of rule", () => {
  it("leaves out pending and remote rows everywhere", () => {
    const rows = [
      { title: "api" },
      { title: "w1", parent: "api" },
      { title: "w2", parent: "api", pending: true },
      { title: "dev::w3", parent: "api", device: "dev" },
    ] as Instance[];
    expect(familyOf("api", rows).children.map((r) => r.title)).toEqual(["w1"]);
  });
});


// --- Finished workers and progress ---------------------------------------------------

const gone = (title: string, status: string | null, how = "deleted"): FinishedChild => ({
  title,
  branch: "you/" + title,
  created_at: NOW - 3000,
  ended_at: NOW - 60,
  how,
  stage: "pushed",
  pr_url: "",
  diff_stat: null,
  last_report: status === null ? null : { id: "m1", status, summary: title + " done", ts: NOW - 100 },
});

describe("finished workers and progress", () => {
  const rows = workerRows(familyOf("api", FAMILY).children, MEMBERS, act);

  it("normalizes the finished list (and an older server without one)", () => {
    const body = normThread({ title: "api", members: [], items: [], finished: [gone("old", "done"), { title: "" }] });
    expect(body.finished.map((f) => f.title)).toEqual(["old"]);
    expect(body.finished[0].last_report?.status).toBe("done");
    expect(normThread({ title: "api", members: [], items: [] }).finished).toEqual([]);
  });

  it("counts finished workers as done unless they reported failed or blocked", () => {
    // live: 1 ask, 1 done, 1 working; finished: 2 done-ish (one never reported), 1 failed
    const p = progressOf(rows, [gone("a", "done"), gone("b", null, "closed"), gone("c", "failed")]);
    expect(p.total).toBe(6);
    expect(p.done).toBe(3);
    expect(p.text).toBe("3 of 6 done");
    expect(p.segs.map((s) => [s.key, s.n])).toEqual([
      ["done", 3],
      ["bad", 1],
      ["needs", 1],
      ["working", 1],
    ]);
  });

  it("has no bar for a session with no workers at all", () => {
    expect(progressOf([], [])).toEqual({ total: 0, done: 0, segs: [], text: "" });
  });

  it("names finished workers in the header, alone or beside live ones", () => {
    expect(headerSummary(rows, 2).at(-1)).toEqual({ text: "2 finished", cls: "ok" });
    expect(headerSummary([], 3)).toEqual([{ text: "All 3 workers finished", cls: "ok" }]);
    expect(headerSummary([], 0)).toEqual([]);
  });

  it("says how a finished worker ended", () => {
    expect(finishedStatus(gone("a", "done"))).toEqual({ word: "reported done", cls: "ok" });
    expect(finishedStatus(gone("a", "blocked"))).toEqual({ word: "reported blocked", cls: "bad" });
    expect(finishedStatus(gone("a", null))).toEqual({ word: "no report", cls: "" });
  });

  it("carries each live worker's git stage onto its row", () => {
    const kids = familyOf("api", FAMILY).children.map((c, i) => ({ ...c, stage: i ? "pushed" : "agent" }));
    const staged = workerRows(kids, MEMBERS, act);
    expect(new Set(staged.map((r) => r.stage))).toEqual(new Set(["agent", "pushed"]));
  });
});
