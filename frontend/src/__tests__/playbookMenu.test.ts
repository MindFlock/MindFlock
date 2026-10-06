/** What is left of lib/playbooks after Ship & split replaced the paste menu:
 * who gets the tools, why a paste is blocked (the Thread's worker buttons),
 * the New dialog's split gate and its "suggested" heuristic, and the family
 * rule. Also the store's threadOpen and the Ctrl+K S / F / L / T chords. The
 * menu itself is pinned in shipMenu.test.ts. */
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import type { Caps, Instance } from "../api/types";
import {
  ANSWER_FIRST_REASON,
  NO_TOOLS_REASON,
  RESTART_REASON,
  forkBlockReason,
  liveChildren,
  mcpCapable,
  splitGate,
  splitSuggestion,
  suggestionPill,
} from "../lib/playbooks";
import { CHORDS } from "../lib/keymap";
import { selectSession } from "../lib/sessionActions";
import { setSessionSelector, useUi } from "../state/store";

(globalThis as Record<string, unknown>).requestAnimationFrame ??= () => 0;

const caps = (agent_mcp?: { enabled: boolean; providers: string[] }): Partial<Caps> =>
  ({ git: true, agent_mcp }) as Partial<Caps>;
const inst = (o: Partial<Instance>): Instance => ({ title: "x", ...o }) as Instance;

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

describe("splitGate: the New dialog's Split box", () => {
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

describe("the family", () => {
  it("live children are this session's non-pending rows", () => {
    const rows = [
      inst({ title: "api" }),
      inst({ title: "w1", parent: "api" }),
      inst({ title: "w2", parent: "api" }),
      inst({ title: "w3", parent: "api", pending: true }),
      inst({ title: "other", parent: "elsewhere" }),
    ];
    expect(liveChildren("api", rows).map((r) => r.title)).toEqual(["w1", "w2"]);
    expect(liveChildren("w1", rows)).toEqual([]);
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

describe("Ctrl+K S / F / L / T", () => {
  it("are bound, and to the letters the spec reserves", () => {
    expect(CHORDS.s.desc).toBe("Message…");
    expect(CHORDS.f.desc).toBe("Ship & split…");
    expect(CHORDS.l.desc).toMatch(/^When it's done/);
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
