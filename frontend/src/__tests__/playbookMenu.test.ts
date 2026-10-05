/** The playbook menu's pure half (lib/playbooks): who gets the fork button,
 * why it is blocked, the New dialog's Split gate and create body, the menu's
 * shape, the Ask list and the "suggested" heuristic. Also the store's
 * threadOpen and the Ctrl+K S / F / T chords. */
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import type { Caps, Instance, Playbook } from "../api/types";
import {
  ANSWER_FIRST_REASON,
  NO_TOOLS_REASON,
  RESTART_REASON,
  askTargets,
  forkBlockReason,
  isWorkerPlaybook,
  letterOf,
  liveChildren,
  mcpCapable,
  menuModel,
  reportedLabel,
  splitGate,
  splitSuggestion,
  suggestionPill,
  withSplit,
} from "../lib/playbooks";
import { CHORDS } from "../lib/keymap";
import { selectSession } from "../lib/sessionActions";
import { setSessionSelector, useUi } from "../state/store";

(globalThis as Record<string, unknown>).requestAnimationFrame ??= () => 0;

const caps = (agent_mcp?: { enabled: boolean; providers: string[] }): Partial<Caps> =>
  ({ git: true, agent_mcp }) as Partial<Caps>;
const inst = (o: Partial<Instance>): Instance => ({ title: "x", ...o }) as Instance;
const pb = (o: Partial<Playbook>): Playbook =>
  ({
    id: "x",
    label: "X",
    desc: "",
    letter: "x",
    args: [],
    available: true,
    disabled_reason: null,
    ...o,
  }) as Playbook;

describe("mcpCapable: who gets the fork button", () => {
  const on = caps({ enabled: true, providers: ["claude", "codex"] });

  it("a CLI the server attaches the tools to", () => {
    expect(mcpCapable(on, inst({ provider: "claude" }))).toBe(true);
    expect(mcpCapable(on, inst({ provider: "codex" }))).toBe(true);
  });

  it("not a CLI outside the list (aider), and not with attach switched off", () => {
    expect(mcpCapable(on, inst({ provider: "aider" }))).toBe(false);
    expect(
      mcpCapable(caps({ enabled: false, providers: ["claude"] }), inst({ provider: "claude" }))
    ).toBe(false);
  });

  it("an older server that reports no cap gets no button", () => {
    expect(mcpCapable(caps(undefined), inst({ provider: "claude" }))).toBe(false);
    expect(mcpCapable(undefined, inst({ provider: "claude" }))).toBe(false);
  });

  it("falls back to the program when the row carries no provider", () => {
    expect(mcpCapable(on, inst({ provider: "", program: "claude" }))).toBe(true);
    expect(mcpCapable(on, inst({ provider: "", program: "" }))).toBe(false);
  });
});

describe("forkBlockReason: why the button is disabled right now", () => {
  it("an agent launched without the attach args must be restarted", () => {
    expect(forkBlockReason(inst({ mcp_attached: false, activity: "idle" }))).toBe(RESTART_REASON);
  });

  it("a permission dialog on screen would take the paste as its answer", () => {
    expect(forkBlockReason(inst({ mcp_attached: true, activity: "clarify" }))).toBe(
      ANSWER_FIRST_REASON
    );
    expect(forkBlockReason(inst({ mcp_attached: true, activity: "limit" }))).toBe(
      ANSWER_FIRST_REASON
    );
  });

  it("unknown attach state (null, or an older server) does not block", () => {
    expect(forkBlockReason(inst({ mcp_attached: null, activity: "working" }))).toBe("");
    expect(forkBlockReason(inst({ activity: "idle" }))).toBe("");
  });
});

describe("splitGate: the New dialog's Split across workers box", () => {
  it("the cap missing is unknown, not off: the box stays usable", () => {
    expect(splitGate(undefined, "claude")).toEqual({ ok: true, reason: "" });
    expect(splitGate(caps(undefined), "aider")).toEqual({ ok: true, reason: "" });
  });

  it("switched off says where to switch it on", () => {
    const g = splitGate(caps({ enabled: false, providers: ["claude"] }), "claude");
    expect(g.ok).toBe(false);
    expect(g.reason).toMatch(/switched off/);
  });

  it("a CLI that doesn't get the tools names the ones that do", () => {
    const g = splitGate(caps({ enabled: true, providers: ["claude", "codex"] }), "aider");
    expect(g).toEqual({
      ok: false,
      reason: "Needs a CLI that gets the MindFlock tools — Claude or Codex",
    });
    expect(splitGate(caps({ enabled: true, providers: [] }), "aider").reason).toBe(
      NO_TOOLS_REASON
    );
  });

  it("an attachable CLI is fine", () => {
    expect(splitGate(caps({ enabled: true, providers: ["claude"] }), "claude").ok).toBe(true);
  });
});

describe("withSplit: the create body", () => {
  it("unticked leaves the body alone", () => {
    const b = { title: "t", in_place: true };
    expect(withSplit(b, false)).toBe(b);
  });

  it("ticked sends playbook split and turns in-place into a worktree", () => {
    expect(withSplit({ title: "t", in_place: true }, true)).toEqual({
      title: "t",
      in_place: false,
      playbook: "split",
    });
  });

  it("a provisioned body has no in_place and doesn't grow one", () => {
    const out = withSplit({ title: "t", provisioned: true }, true);
    expect(out).toEqual({ title: "t", provisioned: true, playbook: "split" });
    expect("in_place" in out).toBe(false);
  });
});

describe("the family and the menu's shape", () => {
  const rows = [
    inst({ title: "api" }),
    inst({ title: "w1", parent: "api", last_report: { id: "m", status: "done", summary: "", ts: 1 } }),
    inst({ title: "w2", parent: "api" }),
    inst({ title: "w3", parent: "api", pending: true }),
    inst({ title: "other", parent: "elsewhere" }),
  ];
  const list = [
    pb({ id: "split", letter: "s", when: "any" }),
    pb({ id: "ask", letter: "a", when: "any" }),
    pb({ id: "workers", letter: "c", when: "has_children" }),
    pb({ id: "wrapup", letter: "w", when: "has_children" }),
  ];

  it("live children are this session's non-pending rows", () => {
    expect(liveChildren("api", rows).map((r) => r.title)).toEqual(["w1", "w2"]);
    expect(liveChildren("w1", rows)).toEqual([]);
  });

  it("n of N reported counts workers with a last_report", () => {
    expect(reportedLabel(liveChildren("api", rows))).toBe("1 of 2 reported");
    expect(reportedLabel([])).toBe("0 of 0 reported");
  });

  it("worker playbooks by `when`, or by id from a server that doesn't send it", () => {
    expect(isWorkerPlaybook(pb({ id: "zzz", when: "has_children" }))).toBe(true);
    expect(isWorkerPlaybook(pb({ id: "wrapup", when: undefined }))).toBe(true);
    expect(isWorkerPlaybook(pb({ id: "split", when: "any" }))).toBe(false);
  });

  it("with workers: the general items, then a workers section with its count", () => {
    const m = menuModel(list, liveChildren("api", rows));
    expect(m.general.map((p) => p.id)).toEqual(["split", "ask"]);
    expect(m.workers).toEqual({
      count: 2,
      reported: "1 of 2 reported",
      items: [list[2], list[3]],
    });
  });

  it("no workers and no worker playbooks: no section at all", () => {
    expect(menuModel(list.slice(0, 2), []).workers).toBeNull();
  });

  it("letters are the registry's, one upper-case character", () => {
    expect(letterOf({ letter: "s" })).toBe("S");
    expect(letterOf({ letter: "wx" })).toBe("W");
    expect(letterOf({ letter: "" })).toBe("");
  });
});

describe("askTargets: the Ask a session › list", () => {
  const rows = [
    inst({ title: "lead" }),
    inst({ title: "me", parent: "lead" }),
    inst({ title: "sib", parent: "lead" }),
    inst({ title: "kid", parent: "me" }),
    inst({ title: "solo" }),
    inst({ title: "remote", device: "laptop" } as Partial<Instance>),
    inst({ title: "cloning", pending: true }),
  ];
  const rail = ["solo", "lead", "me", "sib", "kid", "remote", "cloning"];

  it("family first (parent, workers, siblings), then the rest in rail order", () => {
    const t = askTargets("me", rows, rail, (x) => x.toUpperCase());
    expect(t.map((x) => [x.title, x.rel])).toEqual([
      ["lead", "parent"],
      ["kid", "worker"],
      ["sib", "sibling"],
      ["solo", ""],
    ]);
    expect(t[0].name).toBe("LEAD");
  });

  it("slots are the rail numbers; past nine or off the rail there is none", () => {
    const t = askTargets("me", rows, rail, (x) => x);
    expect(Object.fromEntries(t.map((x) => [x.title, x.slot]))).toEqual({
      lead: "2",
      kid: "5",
      sib: "4",
      solo: "1",
    });
    expect(askTargets("me", rows, [], (x) => x).every((x) => x.slot === "")).toBe(true);
  });

  it("never offers itself, another device's session, or one still being created", () => {
    const titles = askTargets("me", rows, rail, (x) => x).map((x) => x.title);
    expect(titles).not.toContain("me");
    expect(titles).not.toContain("remote");
    expect(titles).not.toContain("cloning");
  });
});

describe("splitSuggestion: the New dialog's pill", () => {
  it("a list of three pieces is suggested, with the pieces", () => {
    expect(splitSuggestion("rate-limit billing, search and upload per user")).toEqual({
      pieces: ["billing", "search", "upload"],
    });
    expect(splitSuggestion("port the auth, the billing, and the upload services")).toEqual({
      pieces: ["auth", "billing", "upload"],
    });
  });

  it("saying it outright is suggested without pieces", () => {
    expect(splitSuggestion("fix these tests in parallel")).toEqual({ pieces: [] });
    expect(splitSuggestion("one worker per service, please")).toEqual({ pieces: [] });
  });

  it("an ordinary sentence is not", () => {
    expect(splitSuggestion("fix the login bug in acme-api")).toBeNull();
    expect(splitSuggestion("add search and upload")).toBeNull();
    expect(splitSuggestion("")).toBeNull();
  });

  it("the pill reads suggested · piece · piece", () => {
    expect(suggestionPill({ pieces: ["billing", "search", "upload"] })).toBe(
      "suggested · billing · search · upload"
    );
    expect(suggestionPill({ pieces: [] })).toBe("suggested");
  });
});

describe("threadOpen", () => {
  const picked: Array<[string, unknown]> = [];
  beforeEach(() => {
    picked.length = 0;
    setSessionSelector((t, o) => picked.push([t, o]));
    useUi.setState({ playbookMenu: { title: "api" }, threadComposeTarget: null });
  });
  afterEach(() => setSessionSelector(selectSession));

  it("selects the session WITHOUT the keyboard, on its Thread tab", () => {
    useUi.getState().threadOpen("api");
    expect(picked).toEqual([["api", { noKeyboard: true }]]);
    expect(useUi.getState().lastTab.api).toBe("thread");
  });

  it("addresses the composer (default: the session itself) and closes the fork menu", () => {
    useUi.getState().threadOpen("api");
    const a = useUi.getState().threadComposeTarget!;
    expect(a.title).toBe("api");
    expect(a.to).toBe("api");
    expect(useUi.getState().playbookMenu).toBeNull();
    useUi.getState().threadOpen("api", { composeTo: "api-search" });
    const b = useUi.getState().threadComposeTarget!;
    expect(b.to).toBe("api-search");
    // A second open of the same target still moves the caret: seq bumps.
    expect(b.seq).toBeGreaterThan(a.seq);
  });

  it("an empty title does nothing", () => {
    useUi.getState().threadOpen("");
    expect(picked).toEqual([]);
    expect(useUi.getState().threadComposeTarget).toBeNull();
  });

  it("lastSeen is per title", () => {
    useUi.getState().setThreadLastSeen("api", 1234);
    useUi.getState().setThreadLastSeen("web", 99);
    expect(useUi.getState().threadLastSeen).toMatchObject({ api: 1234, web: 99 });
  });
});

describe("Ctrl+K S / F / T", () => {
  it("are bound, and to the letters the spec reserves", () => {
    expect(CHORDS.s.desc).toBe("Message…");
    expect(CHORDS.f.desc).toBe("Work with other sessions…");
    expect(CHORDS.t.desc).toMatch(/^Thread/);
  });

  it("S opens the Thread composer addressed to the focused session; T just the Thread", () => {
    const picked: string[] = [];
    setSessionSelector((t) => picked.push(t));
    try {
      CHORDS.s.run("api");
      expect(useUi.getState().threadComposeTarget).toMatchObject({ title: "api", to: "api" });
      CHORDS.t.run("web");
      expect(useUi.getState().lastTab.web).toBe("thread");
      expect(picked).toEqual(["api", "web"]);
    } finally {
      setSessionSelector(selectSession);
    }
  });
});
