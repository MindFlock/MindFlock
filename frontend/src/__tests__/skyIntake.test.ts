/** The Sky's intake lane as data (lib/skyIntake.ts): one block per configured
 * source — ticket sources, PR repos, issue repos — with its waiting items, its
 * auto-start chip, its default flyway, and the sessions it already started. */

import { describe, expect, it } from "vitest";
import type { Instance } from "../api/types";
import { collectQueued, type QueuePayloads } from "../components/intake/queue";
import type { FlightPlan } from "../lib/flights";
import {
  everyWords,
  laneCount,
  repoSlugName,
  shownWaiting,
  sourceBlocks,
  ticketSourceLabel,
  titlePrefix,
  type LaneWaiting,
} from "../lib/skyIntake";
import { INTAKE_KEY, INTAKE_OPEN_W, INTAKE_W, SKY_GAP, SKY_MARGIN, assignSpots, estimateLine, intakeSize, patchSkyLine, pinnedPositions, readSkyLayout, tidySkyLayout } from "../lib/skyLayout";

const plan = (id: string, name: string, builtin = id.startsWith("builtin:")) => ({ id, name, builtin }) as FlightPlan;
const PLANS = [plan("builtin:ticket", "Ticket to merge"), plan("builtin:pr", "Review a PR"), plan("p_mine", "Mine")];

const SETTINGS = {
  ticketing: {
    sources: [
      { id: "sc", provider: "shortcut", label: "quickpay", plan: "p_mine" },
      { id: "jira", provider: "jira", label: "", poll_interval_seconds: 300 },
    ],
  },
  github: {
    repos: ["acme/app", "acme/site"],
    issue_repos: ["acme/app"],
    poll_interval_seconds: 120,
    repo_settings: { "acme/site": { plan: "p_mine" } },
  },
};
const PROVIDERS = [
  { id: "shortcut", label: "Shortcut" },
  { id: "jira", label: "Jira" },
];

const PAYLOADS: QueuePayloads = {
  tickets: {
    tickets: [
      { source: "sc", id: "1", slug: "sc-1", name: "Fix login", eligible: true, created_at: "2026-01-02T00:00:00Z" },
      { source: "sc", id: "2", slug: "sc-2", name: "Old one", eligible: true, created_at: "2026-01-01T00:00:00Z" },
      { source: "sc", id: "3", slug: "sc-3", name: "Started", eligible: true, has_session: true },
      { source: "jira", id: "9", slug: "J-9", name: "Not eligible", eligible: false },
    ],
  },
  prs: { prs: [{ repo: "acme/app", number: 7, title: "Bump deps", eligible: true }] },
  issues: { issues: [{ repo: "acme/app", number: 4, title: "Crash on boot", eligible: true }] },
};
const waiting = (p: QueuePayloads = PAYLOADS, auto = false): LaneWaiting[] => collectQueued(p).map((item) => ({ item, auto }));

const row = (title: string, over: Partial<Instance> = {}) => ({ title, branch: "", ...over }) as Instance;

describe("sourceBlocks: one block per source, in pipeline order", () => {
  const blocks = sourceBlocks(SETTINGS, waiting(), [], { plans: PLANS, providers: PROVIDERS });

  it("groups by source kind: ticket sources, then PR repos, then issue repos", () => {
    expect(blocks.map((b) => b.key)).toEqual(["tickets:sc", "tickets:jira", "prs:acme/app", "prs:acme/site", "issues:acme/app"]);
    expect(blocks.map((b) => b.label)).toEqual(["Shortcut · quickpay", "Jira", "PRs · acme/app", "PRs · acme/site", "Issues · acme/app"]);
    expect(blocks.map((b) => b.target)).toEqual([
      "tickets:sc",
      "tickets:jira",
      "prs:acme/app",
      "prs:acme/site",
      "issues:acme/app",
    ]);
  });

  it("names each block without the kind its badge says (the label keeps it for tooltips)", () => {
    expect(blocks.map((b) => b.name)).toEqual(["Shortcut · quickpay", "Jira", "acme/app", "acme/site", "acme/app"]);
  });

  it("counts the eligible items without a session, oldest first, into their own block", () => {
    const sc = blocks[0];
    expect(sc.waiting.map((w) => w.item.reference)).toEqual(["sc-2", "sc-1"]);
    expect(laneCount(sc)).toBe(2);
    expect(laneCount(blocks[1])).toBe(0);
    expect(blocks[2].waiting.map((w) => w.item.reference)).toEqual(["acme/app#7"]);
    expect(laneCount(blocks[3])).toBe(0);
    expect(blocks[4].waiting.map((w) => w.item.reference)).toEqual(["acme/app#4"]);
  });

  it("resolves the default flyway: the source's own, else the kind's built-in", () => {
    expect(blocks[0].flyway).toEqual({ id: "p_mine", name: "Mine", own: true, words: "Mine" });
    expect(blocks[1].flyway).toEqual({ id: "builtin:ticket", name: "Ticket to merge", own: false, words: "Stop after the agent" });
    expect(blocks[2].flyway).toEqual({ id: "builtin:pr", name: "Review a PR", own: false, words: "Stop after the agent" });
    expect(blocks[3].flyway).toEqual({ id: "p_mine", name: "Mine", own: true, words: "Mine" });
    // No builtin:issue on this server: an issue is worked like a ticket.
    expect(blocks[4].flyway.id).toBe("builtin:ticket");
  });

  it("a flyway the list no longer has: the server goes by the depth, and so do the words", () => {
    const b = sourceBlocks({ ticketing: { sources: [{ id: "sc", plan: "p_gone", depth: "push" }] } }, [], [], { plans: PLANS });
    expect(b[0].flyway).toMatchObject({ id: "builtin:ticket", own: false, words: "Push" });
    // Plans not loaded yet: a named one is trusted, by its id.
    const early = sourceBlocks({ ticketing: { sources: [{ id: "sc", plan: "p_gone" }] } }, [], [], { plans: null });
    expect(early[0].flyway).toEqual({ id: "p_gone", name: "p_gone", own: true, words: "p_gone" });
  });

  it("names where a source goes in the card's words: its depth's rung", () => {
    const b = sourceBlocks({ ticketing: { sources: [{ id: "sc", depth: "pr" }] }, github: { repos: ["acme/app"], repo_settings: { "acme/app": { depth: "commit" } } } }, [], [], { plans: PLANS });
    expect(b.map((x) => x.flyway.words)).toEqual(["Open PR", "Commit"]);
  });

  it("an item from a source settings doesn't name still gets a block", () => {
    const b = sourceBlocks({}, waiting(), [], { plans: PLANS });
    expect(b.map((x) => [x.key, !!x.stray])).toEqual([
      ["tickets:sc", true],
      ["prs:acme/app", true],
      ["issues:acme/app", true],
    ]);
    expect(b[0].waiting).toHaveLength(2);
    // Its name is the repo alone, as a configured block's: a stray issues
    // block for another repo reads as that repo, not as a second copy.
    expect(b.map((x) => x.name)).toEqual(["sc", "acme/app", "acme/app"]);
  });

  it("an issue repo settings names is one block, however its items spell it", () => {
    const b = sourceBlocks(
      { github: { issue_repos: ["Acme/App", "acme/app"] } },
      waiting({ issues: { issues: [{ repo: "acme/app", number: 4, title: "Crash", eligible: true }] } }),
      [],
      { plans: PLANS }
    );
    expect(b.map((x) => [x.key, x.waiting.length, !!x.stray])).toEqual([["issues:Acme/App", 1, false]]);
  });
});

describe("the auto-start chip", () => {
  it("reads the switches the Board reads, and how often each source polls", () => {
    const status = { available: true, desired: true, pr_enabled: true, issues_enabled: false };
    const b = sourceBlocks(SETTINGS, [], [], { plans: PLANS, status });
    expect(b.map((x) => x.autoText)).toEqual([
      "auto-start on · every 20 s",
      "auto-start on · every 5 min",
      "auto-start on · every 2 min",
      "auto-start on · every 2 min",
      "manual",
    ]);
  });

  it("everything is manual with the engine stopped and no status", () => {
    expect(sourceBlocks(SETTINGS, [], []).every((b) => b.autoText === "manual" && !b.auto)).toBe(true);
  });

  it("words: seconds, a minute, minutes", () => {
    expect([everyWords(20), everyWords(60), everyWords(300)]).toEqual(["every 20 s", "every minute", "every 5 min"]);
  });
});

describe("started sessions: which rows a block started", () => {
  it("matches the session title the panel names for an item", () => {
    const panels = {
      tickets: { tickets: [{ source: "sc", id: "3", slug: "sc-3", session: "sc-3", has_session: true }] },
      prs: { prs: [{ repo: "acme/site", number: 2, session: "pr-site-2", has_session: true }] },
    };
    const b = sourceBlocks(SETTINGS, [], [row("sc-3"), row("pr-site-2"), row("my-own-thing")], { panels });
    expect(b.find((x) => x.key === "tickets:sc")!.started).toEqual([{ title: "sc-3", reference: "sc-3" }]);
    expect(b.find((x) => x.key === "prs:acme/site")!.started).toEqual([{ title: "pr-site-2", reference: "acme/site#2" }]);
    expect(b.flatMap((x) => x.started.map((s) => s.title))).not.toContain("my-own-thing");
  });

  it("falls back to the server's title shape for PRs and issues the panel no longer lists", () => {
    const b = sourceBlocks(SETTINGS, [], [row("pr-app-7"), row("issue-app-4"), row("pr-app-notanumber")]);
    expect(b.find((x) => x.key === "prs:acme/app")!.started).toEqual([{ title: "pr-app-7", reference: "acme/app#7" }]);
    expect(b.find((x) => x.key === "issues:acme/app")!.started).toEqual([{ title: "issue-app-4", reference: "acme/app#4" }]);
    expect(b.find((x) => x.key === "prs:acme/site")!.started).toEqual([]);
  });

  it("a ticket session on the source's branch prefix; never a hidden or locked row", () => {
    const b = sourceBlocks(SETTINGS, [], [
      row("sc-44", { branch: "feature/sc-44/fix" }),
      row("sc-45", { branch: "feature/sc-45/x", hidden_by: "verify" }),
      row("notes", { branch: "feature/sc-46/notes" }),
    ]);
    expect(b[0].started.map((s) => s.title)).toEqual(["sc-44"]);
  });

  it("a row on another device matches under its bare title and keeps its namespaced one", () => {
    const b = sourceBlocks(SETTINGS, [], [row("mini::pr-app-7")]);
    expect(b.find((x) => x.key === "prs:acme/app")!.started).toEqual([{ title: "mini::pr-app-7", reference: "acme/app#7" }]);
  });

  it("a row is claimed by one block only", () => {
    const b = sourceBlocks({ github: { repos: ["a/app", "b/app"] } }, [], [row("pr-app-1")]);
    expect(b.map((x) => x.started.length)).toEqual([1, 0]);
  });
});

describe("the small helpers", () => {
  it("title prefixes mirror the server's pr_slug / issue_slug", () => {
    expect(repoSlugName("acme/my app!")).toBe("my-app-");
    expect(titlePrefix("prs", "acme/app")).toBe("pr-app-");
    expect(titlePrefix("issues", "acme/app/")).toBe("issue-app-");
  });

  it("a ticket source's label: provider · label, said once", () => {
    expect(ticketSourceLabel({ provider: "jira", label: "Jira payments" }, PROVIDERS)).toBe("Jira payments");
    expect(ticketSourceLabel({ provider: "jira", label: "payments" }, PROVIDERS)).toBe("Jira · payments");
    expect(ticketSourceLabel({ provider: "linear" }, PROVIDERS, "Linear")).toBe("Linear");
  });

  it("six chips, then n more", () => {
    const many: QueuePayloads = {
      tickets: { tickets: Array.from({ length: 9 }, (_, i) => ({ source: "sc", id: String(i), slug: "sc-" + i, eligible: true })) },
    };
    const [b] = sourceBlocks(SETTINGS, waiting(many), []);
    const { shown, more } = shownWaiting(b);
    expect(shown).toHaveLength(6);
    expect(more).toBe(3);
  });
});

describe("the lane as a sky object (round 12): layout key \"intake\"", () => {
  const mem = () => {
    const m = new Map<string, string>();
    return { getItem: (k: string) => m.get(k) ?? null, setItem: (k: string, v: string) => void m.set(k, v) };
  };

  it("its size: what it measured, else its width folded or expanded and a guess at its height", () => {
    expect(INTAKE_KEY).toBe("intake");
    expect(intakeSize(undefined)).toEqual({ w: INTAKE_W, h: 320 });
    expect(intakeSize({ open: true })).toEqual({ w: INTAKE_OPEN_W, h: 520 });
    expect(INTAKE_OPEN_W).toBeGreaterThan(INTAKE_W);
    expect(intakeSize({ open: true }, { w: 430, h: 900 })).toEqual({ w: 430, h: 900 });
    expect(intakeSize(undefined, { w: 0, h: 0 })).toEqual({ w: INTAKE_W, h: 320 });
    // Never a line's estimate: an expanded lane is not an open pane.
    expect(estimateLine({ w: 240, h: 300 }, { open: true }).h).toBeGreaterThan(700);
  });

  it("x / y / pinned and open are stored like a line's; a drop pins it, Tidy forgets the spot and keeps it expanded", () => {
    const store = mem();
    let l = patchSkyLine({}, INTAKE_KEY, { open: true }, store);
    expect(l.intake).toEqual({ open: true });
    l = patchSkyLine(l, INTAKE_KEY, { x: 900, y: 300, pinned: true }, store);
    expect(pinnedPositions(l)).toEqual({ intake: { x: 900, y: 300 } });
    expect(readSkyLayout(store.getItem("mf_sky_layout"))).toEqual({ intake: { x: 900, y: 300, pinned: true, open: true } });
    expect(tidySkyLayout(l, undefined, store)).toEqual({ intake: { open: true } });
    expect(tidySkyLayout(l, INTAKE_KEY, store)).toEqual({ intake: { open: true } });
  });

  it("placed first: unpinned it takes the corner and the lines flow to its right; pinned away, the corner is the lines'", () => {
    const area = { width: 2000, left: SKY_MARGIN, top: SKY_MARGIN };
    const lines = [
      { id: "a", w: 400, h: 200 },
      { id: "b", w: 400, h: 200 },
    ];
    const spots = assignSpots([], [{ id: INTAKE_KEY, w: INTAKE_W, h: 320 }, ...lines], area);
    expect(spots.intake).toEqual({ x: SKY_MARGIN, y: SKY_MARGIN });
    expect(spots.a).toEqual({ x: SKY_MARGIN + INTAKE_W + SKY_GAP, y: SKY_MARGIN });
    expect(spots.b.x).toBe(spots.a.x + 400 + SKY_GAP);
    const away = assignSpots([{ id: INTAKE_KEY, x: 1500, y: 900, w: INTAKE_W, h: 320 }], lines, area);
    expect(away.a).toEqual({ x: SKY_MARGIN, y: SKY_MARGIN });
  });
});
