/** Intake's wording and its three switches read together (round-2 fresh-eyes
 * pass): components/intake/vocab.ts and components/intake/switches.ts. */

/// <reference types="vite/client" />
import { describe, expect, it } from "vitest";
import {
  filterSummary,
  friendlySourceError,
  savedFilter,
  stateName,
} from "../components/intake/vocab";
import { intakeSwitches, kindsText, prsOn } from "../components/intake/switches";
import { routeText } from "../components/intake/kit";
import type { TicketingSource } from "../api/types";

const src = (extra: Record<string, string> = {}): TicketingSource =>
  ({ id: "sc", provider: "shortcut", ...extra }) as TicketingSource;

describe("a source that can't be listed is one sentence, by name, without JSON", () => {
  it("a rejected token", () => {
    const raw = 'Shortcut API returned 401: {"message":"Unauthorized","tag":"unauthorized"}';
    const out = friendlySourceError("Shortcut", raw);
    expect(out).toBe("Shortcut rejected the token — press Fix on its heading.");
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

describe("the auto-start switches", () => {
  it("PR comments are off by default — but saved settings keep their meaning", () => {
    expect(prsOn({})).toBe(false);
    expect(prsOn({ repos: ["o/r"], enabled: false })).toBe(false);
    expect(prsOn({ repos: ["o/r"], enabled: true })).toBe(true);
    // Unset with repos already watched = on, as before (existing users).
    expect(prsOn({ repos: ["o/r"] })).toBe(true);
  });

  it("reads the three switches together", () => {
    const off = intakeSwitches({ ticketsDesired: false, ticketSources: 1, gh: { repos: ["o/r"], enabled: false } });
    expect(off.on).toEqual({ tickets: false, prs: false, issues: false });
    expect(off.configured).toEqual({ tickets: true, prs: true, issues: false });
    const on = intakeSwitches({ ticketsDesired: true, ticketSources: 1, gh: {} });
    expect(on.on.tickets).toBe(true);
    // No ticket source: the ticket switch can't be "on" whatever the engine says.
    const nothing = intakeSwitches({ ticketsDesired: true, ticketSources: 0, gh: {} });
    expect(nothing.on.tickets).toBe(false);
  });

  it("names the kinds in one phrase", () => {
    expect(kindsText(["issues", "tickets"])).toBe("tickets and issues");
    expect(kindsText(["tickets", "prs", "issues"])).toBe("tickets, PR comments and issues");
  });
});
