import { describe, it, expect } from "vitest";
import { byGroup, collectQueued, isQueued, queuedOf, runState } from "../components/intake/queue";

/** A tickets payload shaped like /api/tickets/assigned: every assigned ticket,
 * annotated with the server's own eligibility verdict. */
const tickets = {
  source_labels: { sc: "Shortcut — core", jira: "Jira – EU" },
  tickets: [
    // Queued, and the older of the two — so it must lead.
    { source: "sc", id: "9", slug: "sc-9", name: "Old one", eligible: true, created_at: "2026-08-01T00:00:00Z", bucket: "Ready" },
    { source: "sc", id: "10", slug: "sc-10", name: "New one", eligible: true, created_at: "2026-08-09T00:00:00Z", bucket: "Ready" },
    // Not eligible — the pipeline would skip it, so it is not "waiting".
    { source: "sc", id: "11", slug: "sc-11", name: "Wrong state", eligible: false, reasons: ["not in an ingest state — won't auto-ingest"], created_at: "2026-07-01T00:00:00Z" },
    // Eligible but already being worked: PR/issue eligibility doesn't consult
    // sessions, and neither should be listed as still waiting.
    { source: "jira", id: "5", slug: "EU-5", name: "In flight", eligible: true, has_session: true, created_at: "2026-07-02T00:00:00Z" },
    { source: "jira", id: "6", slug: "EU-6", name: "Jira queued", eligible: true, created_at: "2026-08-05T00:00:00Z" },
  ],
};

const prs = {
  repos: ["org/api"],
  enabled: true,
  prs: [
    { repo: "org/api", number: 42, title: "Fix retry", eligible: true, created_at: "2026-08-08T00:00:00Z" },
    { repo: "org/api", number: 43, title: "Draft-ish", eligible: false, reasons: ["in the min-age grace period (3 min left)"], created_at: "2026-08-09T00:00:00Z" },
  ],
};

const issues = {
  repos: ["org/api"],
  enabled: true,
  issues: [{ repo: "org/api", number: 7, title: "Crash on boot", eligible: true, created_at: "2026-08-02T00:00:00Z" }],
};

describe("isQueued", () => {
  it("requires the server's explicit eligible verdict", () => {
    expect(isQueued({ eligible: true })).toBe(true);
    expect(isQueued({ eligible: false })).toBe(false);
  });

  it("treats a missing verdict as unknown, never as queued", () => {
    // A server that predates the field omits it. Claiming a queue that isn't
    // there is the one answer this must never give.
    expect(isQueued({})).toBe(false);
    expect(isQueued({ eligible: undefined })).toBe(false);
  });

  it("drops anything that already has a session", () => {
    // PR and issue eligibility only consult the processed ledger, so an item
    // started by hand a moment ago still reads as eligible upstream.
    expect(isQueued({ eligible: true, has_session: true })).toBe(false);
  });
});

describe("queuedOf", () => {
  it("keeps only queued tickets, oldest first, with their source label", () => {
    const rows = queuedOf("tickets", { tickets });
    // Oldest first across sources: sc-9 (Aug 1), EU-6 (Aug 5), sc-10 (Aug 9).
    expect(rows.map((r) => r.reference)).toEqual(["sc-9", "EU-6", "sc-10"]);
    expect(rows[0].group).toBe("Shortcut — core");
    expect(rows[1].group).toBe("Jira – EU");
    expect(rows[0].state).toBe("Ready");
  });

  it("names whoever else's ticket it is, and nobody when it's yours", () => {
    // A source set to ingest anyone's tickets puts other people's work in this
    // queue by design; a row that doesn't say so reads as your own.
    const rows = queuedOf("tickets", {
      tickets: {
        tickets: [
          { source: "sc", id: "1", slug: "sc-1", eligible: true, mine: false, assignee: "Mauricio" },
          { source: "sc", id: "2", slug: "sc-2", eligible: true, mine: false },
          { source: "sc", id: "3", slug: "sc-3", eligible: true, mine: true, assignee: "Ethan" },
          { source: "sc", id: "4", slug: "sc-4", eligible: true },
        ],
      },
    });
    const by = Object.fromEntries(rows.map((r) => [r.reference, r.assignee]));
    expect(by["sc-1"]).toBe("Mauricio");
    // Not yours, but the provider didn't name the owner.
    expect(by["sc-2"]).toBe("someone else");
    expect(by["sc-3"]).toBeUndefined();
    // Older payloads have no `mine` at all — those queues were all yours.
    expect(by["sc-4"]).toBeUndefined();
  });

  it("names PRs and issues as repo#number", () => {
    expect(queuedOf("prs", { prs }).map((r) => r.reference)).toEqual(["org/api#42"]);
    expect(queuedOf("issues", { issues }).map((r) => r.reference)).toEqual(["org/api#7"]);
  });

  it("survives an absent or failed panel", () => {
    // A panel that 502s (unconfigured integration) leaves its query with no
    // data at all, which the tab passes straight through as null.
    expect(queuedOf("tickets", {})).toEqual([]);
    expect(queuedOf("prs", { prs: null })).toEqual([]);
    expect(queuedOf("issues", { issues: { issues: [] } })).toEqual([]);
  });

  it("sorts undated rows last instead of wherever they landed", () => {
    const rows = queuedOf("prs", {
      prs: {
        prs: [
          { repo: "r", number: 1, eligible: true },
          { repo: "r", number: 2, eligible: true, created_at: "2026-08-01T00:00:00Z" },
        ],
      },
    });
    expect(rows.map((r) => r.reference)).toEqual(["r#2", "r#1"]);
  });
});

describe("collectQueued", () => {
  it("returns every kind in tab order", () => {
    const all = collectQueued({ tickets, prs, issues });
    expect(all.map((r) => r.kind)).toEqual([
      "tickets",
      "tickets",
      "tickets",
      "prs",
      "issues",
    ]);
    // The badge is this length; five queued out of eight annotated rows.
    expect(all.length).toBe(5);
  });

  it("is empty, not broken, before any panel has loaded", () => {
    expect(collectQueued({})).toEqual([]);
    expect(collectQueued({ tickets: null, prs: null, issues: null })).toEqual([]);
  });

  it("gives every row a key unique across kinds", () => {
    // A PR and an issue can share a repo AND a number, so the kind has to be
    // part of the key or React reuses one row's DOM for the other.
    const all = collectQueued({
      prs: { prs: [{ repo: "org/api", number: 7, eligible: true }] },
      issues: { issues: [{ repo: "org/api", number: 7, eligible: true }] },
    });
    expect(new Set(all.map((r) => r.key)).size).toBe(2);
  });
});

describe("start targets", () => {
  it("gives a ticket the provider id, not the slug", () => {
    // /api/tickets/start takes the provider's own id; the slug is a display
    // name, and posting it silently starts nothing.
    const row = queuedOf("tickets", { tickets }).find((r) => r.reference === "sc-9")!;
    expect(row.target).toEqual({ kind: "tickets", source: "sc", id: "9" });
  });

  it("gives PRs and issues their repo and number, tagged by kind", () => {
    // The kind decides the endpoint (prs/review vs issues/start), so it has to
    // travel with the target rather than being re-derived at the call site.
    expect(queuedOf("prs", { prs })[0].target).toEqual({
      kind: "prs",
      repo: "org/api",
      number: 42,
    });
    expect(queuedOf("issues", { issues })[0].target).toEqual({
      kind: "issues",
      repo: "org/api",
      number: 7,
    });
  });
});

describe("runState", () => {
  const base = { configured: true, engineAvailable: true, engineOn: true, switchOn: true };

  it("is on only when the engine runs and the kind's switch is on", () => {
    expect(runState(base)).toBe("on");
  });

  it("reports the ENGINE when the pipeline is stopped, whatever the switch says", () => {
    // The bug this exists to prevent: PRMonitor and IssueMonitor are built by
    // PipelineOrchestrator, so github.enabled=true with the engine stopped means
    // nothing is coming. Reading the switch alone would say "auto review is on"
    // over a row nothing will ever pick up.
    expect(runState({ ...base, engineOn: false })).toBe("off-engine");
  });

  it("reports the kind's own switch when the engine is running", () => {
    expect(runState({ ...base, switchOn: false })).toBe("off-switch");
  });

  it("prefers the switch over the engine when both are off", () => {
    // Turning the engine on wouldn't start these; the switch is the nearer fix.
    expect(runState({ ...base, switchOn: false, engineOn: false })).toBe("off-switch");
  });

  it("says unset — not paused — when nothing is configured", () => {
    // "Paused" invites you to flip a switch that would still have no repos or
    // sources behind it.
    expect(runState({ ...base, configured: false })).toBe("unset");
    expect(runState({ ...base, engineAvailable: false })).toBe("unset");
  });
});

describe("byGroup", () => {
  it("groups in first-seen order, keeping queue order inside each group", () => {
    const groups = byGroup(queuedOf("tickets", { tickets }));
    expect(groups.map((g) => g.group)).toEqual(["Shortcut — core", "Jira – EU"]);
    expect(groups[0].items.map((i) => i.reference)).toEqual(["sc-9", "sc-10"]);
  });

  it("has nothing to group when nothing is queued", () => {
    expect(byGroup([])).toEqual([]);
  });
});
