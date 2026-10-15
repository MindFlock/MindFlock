/** Intake, round 5: the pure rules behind Test-saves-nothing, one home for
 * auto-start, rows that say where they go, and schedules counted. */

/// <reference types="vite/client" />
import { describe, expect, it } from "vitest";
import { friendlyTestError, siteHost, TOKEN_URLS } from "../components/intake/vocab";
import { everyText, pollSeconds } from "../components/intake/AutoStartSwitches";
import { nextScheduledText, scheduledRows, scheduleLine, skippedText } from "../components/intake/ScheduledTemplates";
import { repoSlugOf } from "../components/intake/kit";
import { stalledText } from "../components/intake/QueueTab";
import { advancedSummaryOf } from "../components/intake/TicketsTab";
import { code } from "./sourceText";

const NOW = new Date(2026, 9, 14, 9, 0);
const at = (h: number, m = 0, dayOffset = 0) => new Date(2026, 9, 14 + dayOffset, h, m).getTime() / 1000;

describe("Jira's test errors name the field to fix", () => {
  const url = "https://acme.atlassian.net";
  it("404 → the Site URL", () => {
    expect(friendlyTestError("jira", "Jira API returned HTTP 404", url)).toBe(
      "No Jira site at acme.atlassian.net — check the Site URL"
    );
  });
  it("401/403 → the email + token pair", () => {
    expect(friendlyTestError("jira", "Jira rejected the credentials (HTTP 401)", url)).toBe(
      "Jira rejected the email + token pair"
    );
    expect(friendlyTestError("jira", "Jira API returned HTTP 403", url)).toBe("Jira rejected the email + token pair");
  });
  it("network → can't reach the host", () => {
    expect(friendlyTestError("jira", "network error reaching Jira: Cannot connect to host", url)).toBe(
      "Can't reach acme.atlassian.net from this machine"
    );
  });
  it("other trackers keep their own words", () => {
    expect(friendlyTestError("shortcut", "Shortcut rejected the token (HTTP 401)")).toBe(
      "Shortcut rejected the token (HTTP 401)"
    );
  });
  it("reads a bare host too", () => {
    expect(siteHost("acme.atlassian.net/")).toBe("acme.atlassian.net");
    expect(siteHost("")).toBe("");
  });
  it("links a token page for every tracker but GitHub", () => {
    expect(Object.keys(TOKEN_URLS).sort()).toEqual(["asana", "jira", "linear", "shortcut"]);
    for (const u of Object.values(TOKEN_URLS)) expect(u).toMatch(/^https:\/\//);
  });
});

describe("auto-start says when it checks", () => {
  it("in words", () => {
    expect(everyText(20)).toBe("checks every 20 s");
    expect(everyText(60)).toBe("checks every minute");
    expect(everyText(300)).toBe("checks every 5 min");
  });
  it("from the settings, else the pipeline's defaults", () => {
    expect(pollSeconds({ ticketSources: [], github: {} })).toEqual({ tickets: 20, prs: 60, issues: 60 });
    expect(
      pollSeconds({
        ticketSources: [{ poll_interval_seconds: 90 }, {}],
        github: { poll_interval_seconds: 120, issue_poll_interval_seconds: "300" },
      })
    ).toEqual({ tickets: 20, prs: 120, issues: 300 });
  });
});

describe("schedules count as starting on their own", () => {
  const list = [
    { name: "nightly tests", repo_path: "/r", schedule: { at: "07:30", days: "daily" as const } },
    { name: "weekly deps", repo_path: "/r", schedule: { at: "21:00", days: "weekdays" as const, paused: true } },
  ];
  const status = {
    runs: { "nightly tests": { next_at: at(7, 30, 2), skipping_at: at(7, 30, 1), tz: "EDT" } },
    runs_here: true,
    runs_on: "",
  };
  it("names the next one, with a skipped slot", () => {
    expect(nextScheduledText(list, status as never, NOW)).toMatch(/^nightly tests, \w{3} 07:30 — tomorrow's run skipped$/);
    expect(skippedText(at(7, 30, 1), NOW)).toBe("tomorrow's run skipped");
  });
  it("says nothing when another device fires them", () => {
    expect(nextScheduledText(list, { runs: status.runs, runs_here: false, runs_on: "mac-mini" } as never, NOW)).toBe("");
  });
  it("one line per template, the zone and the skip included", () => {
    const [row] = scheduledRows(list, status as never, NOW);
    expect(scheduleLine(row)).toMatch(/^Every day at 07:30 EDT · next \w{3} 07:30 · tomorrow's run skipped$/);
  });
});

describe("rows say where they go", () => {
  it("as owner/repo for a GitHub remote, else the folder", () => {
    expect(repoSlugOf("git@github.com:quickpay/quickpay.git")).toBe("quickpay/quickpay");
    expect(repoSlugOf("https://github.com/acme/api")).toBe("acme/api");
    expect(repoSlugOf("/home/me/src/ledger/")).toBe("ledger");
  });
  it("the uses line leads with the route; Open session › closes Intake", () => {
    const kit = code("components/intake/kit.tsx");
    expect(kit).toMatch(/const uses = \[\s*where,/);
    expect(kit).toContain("useUi.getState().closeDialog();");
    expect(kit).not.toContain("Session open");
    expect(kit).not.toMatch(/: "Started"/);
  });
});

describe("the Auto-start sentence follows the chip", () => {
  const base = { label: "Tickets", switchName: "Tickets", runsOn: "", broken: "" };
  it("off names the switch above", () => {
    expect(stalledText({ ...base, state: "off-switch" }, true)).toBe(
      "They won't start on their own: Tickets is off (above). You can still start any of them here, with Start."
    );
  });
  it("not set up never says auto-start is off", () => {
    expect(stalledText({ ...base, state: "unset" }, false)).not.toMatch(/is off/);
  });
  it("can't connect wins", () => {
    expect(stalledText({ ...base, state: "on", broken: "Can't connect — Jira: …" }, true)).toBe("Can't connect — Jira: …");
  });
});

describe("the Advanced fold says what it holds", () => {
  const jira = {
    id: "jira",
    label: "Jira",
    fields: [
      { key: "api_token", label: "API token", secret: true, required: true },
      { key: "member_id", label: "Account ID", advanced: true, auto: true },
      { key: "project", label: "Project key", advanced: true },
      { key: "claim_label", label: "Claim label", advanced: true },
    ],
  };
  it("lists what is set, else nothing", () => {
    expect(advancedSummaryOf(jira, { id: "jira", provider: "jira", member_id: "abc" })).toBe("");
    expect(
      advancedSummaryOf(jira, { id: "jira", provider: "jira", label: "Jira – EU", effort: "high", project: "PAY", claim_label: "mf" })
    ).toBe("Name: Jira – EU · Effort: high · Project key: PAY · +1 more");
  });
});
