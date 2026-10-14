/** Intake's wording and its Pause control (round-2 fresh-eyes pass):
 * components/intake/vocab.ts and components/intake/pause.ts. */

/// <reference types="vite/client" />
import { describe, expect, it } from "vitest";
import {
  filterSummary,
  friendlySourceError,
  reasonText,
  savedFilter,
  stateName,
} from "../components/intake/vocab";
import {
  intakeSwitches,
  isPaused,
  kindsText,
  prsOn,
  resumeKinds,
} from "../components/intake/pause";
import { routeText } from "../components/intake/kit";
import type { TicketingSource } from "../api/types";

const src = (extra: Record<string, string> = {}): TicketingSource =>
  ({ id: "sc", provider: "shortcut", ...extra }) as TicketingSource;

describe("reason chips say auto-start, never ingest or ledger", () => {
  it("translates the server's pipeline phrasings", () => {
    expect(reasonText("not in an ingest state — won't auto-ingest")).toBe("not in an auto-start state");
    expect(reasonText("missing an ingest label (brainflight) — won't auto-ingest")).toBe(
      "missing an auto-start label (brainflight)"
    );
    expect(reasonText("already ingested (recorded completed in the ledger)")).toBe("already started before");
    expect(reasonText("queued for ingestion (pending)")).toBe("already queued to start");
    expect(reasonText("already reviewed (recorded in the processed ledger)")).toBe("already handled before");
  });

  it("passes anything else through untouched", () => {
    expect(reasonText("Ada is on this")).toBe("Ada is on this");
  });
});

describe("a source that can't be listed is one sentence, by name, without JSON", () => {
  it("a rejected token", () => {
    const raw = 'Shortcut API returned 401: {"message":"Unauthorized","tag":"unauthorized"}';
    const out = friendlySourceError("Shortcut", raw);
    expect(out).toBe("Shortcut rejected the token — open its card below to fix it.");
    expect(out).not.toContain("{");
  });

  it("network trouble and unknowns", () => {
    expect(friendlySourceError("Jira — EU", "Connection timed out")).toContain("can't be reached");
    const odd = friendlySourceError("Linear", 'weird failure: {"a":1}');
    expect(odd).toBe("Linear can't be listed — weird failure.");
  });
});

describe("the saved safety filter never depends on the tracker answering", () => {
  it("reads the filter from the saved source", () => {
    expect(savedFilter(src({ workflow_state: "500002, 500003" }))).toEqual(["500002", "500003"]);
    expect(savedFilter(src())).toEqual([]);
  });

  it("names the states when it can, counts them when it can't — never 'not set'", () => {
    const states = [{ id: "500002", name: "Ready for MindFlock" }];
    expect(stateName("500002", undefined, states)).toBe("Ready for MindFlock");
    expect(filterSummary({ ids: ["500002"], names: ["Ready for MindFlock"], labels: [] })).toEqual({
      kind: "states",
      text: "Ready for MindFlock",
      named: true,
    });
    // Tracker unreachable: the filter is still there.
    expect(filterSummary({ ids: ["500002"], names: [], labels: [] })).toEqual({
      kind: "states",
      text: "1 saved state",
      named: false,
    });
    expect(filterSummary({ ids: [], names: [], labels: [] }).kind).toBe("none");
    expect(filterSummary({ ids: [], names: [], labels: ["mindflock"] }).text).toBe("label mindflock");
  });
});

describe("the repo line on a several-repo ticket", () => {
  it("names the repo and why, or says the agent picks", () => {
    expect(routeText({ repos: ["billing-service"], reason: "label 'billing'", method: "label" })).toBe(
      "→ billing-service · label 'billing'"
    );
    expect(routeText({ repos: [], method: "pending" })).toBe("→ repo picked by the agent when it starts");
    expect(routeText(null)).toBe("");
  });
});

describe("Pause intake", () => {
  it("PR comments are off by default — but saved settings keep their meaning", () => {
    expect(prsOn({})).toBe(false);
    expect(prsOn({ repos: ["o/r"], enabled: false })).toBe(false);
    expect(prsOn({ repos: ["o/r"], enabled: true })).toBe(true);
    // Unset with repos already watched = on, as before (existing users).
    expect(prsOn({ repos: ["o/r"] })).toBe(true);
  });

  it("paused = Pause was pressed and nothing switched back on", () => {
    const off = intakeSwitches({ ticketsDesired: false, ticketSources: 1, gh: { repos: ["o/r"], enabled: false } });
    expect(isPaused(off, ["tickets"])).toBe(true);
    // Never switched on is OFF, not paused: there is nothing to resume.
    expect(isPaused(off, null)).toBe(false);
    const on = intakeSwitches({ ticketsDesired: true, ticketSources: 1, gh: {} });
    expect(isPaused(on, ["tickets"])).toBe(false);
    const nothing = intakeSwitches({ ticketsDesired: false, ticketSources: 0, gh: {} });
    expect(isPaused(nothing, ["tickets"])).toBe(false);
  });

  it("resume restores what was on, else everything configured", () => {
    const configured = { tickets: true, prs: true, issues: false };
    expect(resumeKinds(["prs"], configured)).toEqual(["prs"]);
    expect(resumeKinds(null, configured)).toEqual(["tickets", "prs"]);
    // A kind no longer configured is not switched on.
    expect(resumeKinds(["issues", "tickets"], configured)).toEqual(["tickets"]);
  });

  it("names the kinds in one phrase", () => {
    expect(kindsText(["issues", "tickets"])).toBe("tickets and issues");
    expect(kindsText(["tickets", "prs", "issues"])).toBe("tickets, PR comments and issues");
  });
});
