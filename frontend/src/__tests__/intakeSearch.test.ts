/** The Intake lists' Ctrl+F filter, tested as the rule it is.
 *
 * Written against the QUESTIONS people type — a repo name and half a number, a
 * branch they remember, "queued" for what is about to start on its own — rather
 * than against the field list, because the field list is the implementation and
 * the questions are the contract. Every case here is one somebody would
 * actually type into a list of a hundred open PRs.
 */

import { describe, it, expect } from "vitest";
import {
  issueMatches,
  prMatches,
  queuedMatches,
  ticketMatches,
} from "../components/intake/search";

const t = (q: string) => q.toLowerCase().split(/\s+/).filter(Boolean);

describe("filtering open issues", () => {
  const issue = {
    repo: "MindFlock/sitecheck-bot",
    number: 4217,
    title: "Parked pages should stop alerting",
    author: "emandel2630",
    eligible: false,
    reasons: ["already handled", "too new (3m old)"],
  };

  it("takes the repo and part of the number together", () => {
    expect(issueMatches(issue, t("sitecheck 4217"))).toBe(true);
    expect(issueMatches(issue, t("#4217"))).toBe(true);
    expect(issueMatches(issue, t("sitecheck 9999"))).toBe(false);
  });

  it("searches the words the row prints about why it was skipped", () => {
    expect(issueMatches(issue, t("already handled"))).toBe(true);
    expect(issueMatches(issue, t("too new"))).toBe(true);
  });

  it("narrows to what auto handling has queued", () => {
    expect(issueMatches(issue, t("queued"))).toBe(false);
    expect(issueMatches({ ...issue, eligible: true, reasons: [] }, t("queued"))).toBe(true);
  });

  it("matches everything when nothing is typed", () => {
    expect(issueMatches(issue, [])).toBe(true);
  });
});

describe("filtering open pull requests", () => {
  const pr = {
    repo: "MindFlock/app",
    number: 73,
    title: "Answer “are the downloads new users?”",
    author: "emandel2630",
    head_ref: "feature/traffic-visitor-funnel-and-axes",
    base_ref: "main",
    eligible: true,
    reasons: [],
  };

  it("finds a PR by the branch it is off, which is how they are remembered", () => {
    expect(prMatches(pr, t("visitor funnel"))).toBe(true);
    expect(prMatches(pr, t("traffic axes"))).toBe(true);
  });

  it("finds the ones aimed at a particular base", () => {
    expect(prMatches(pr, t("main"))).toBe(true);
    expect(prMatches({ ...pr, base_ref: "staging" }, t("main"))).toBe(false);
  });
});

describe("filtering assigned tickets", () => {
  const ticket = {
    source: "shortcut",
    source_label: "Shortcut",
    id: "21255",
    slug: "sc-21255",
    name: "Classifier collage needs to stop alerting",
    bucket: "Ready for Dev",
    assignee: "Someone Else",
    eligible: false,
    reasons: ["not in an ingest state"],
  };

  it("takes the source and the workflow state, which are HEADINGS not row text", () => {
    // The state is the group a row sits under; "shortcut ready for dev" is a
    // question people have, and neither word is printed on the row itself.
    expect(ticketMatches(ticket, t("shortcut ready"))).toBe(true);
    expect(ticketMatches(ticket, t("ready for dev"))).toBe(true);
  });

  it("finds a ticket by its number with or without the prefix", () => {
    expect(ticketMatches(ticket, t("21255"))).toBe(true);
    expect(ticketMatches(ticket, t("sc-21255"))).toBe(true);
  });

  it("finds whose work it is, on a source that ingests anyone's", () => {
    expect(ticketMatches(ticket, t("someone else"))).toBe(true);
  });
});

describe("filtering the auto-start roll-up", () => {
  const queued = {
    reference: "MindFlock/app#73",
    title: "Answer “are the downloads new users?”",
    group: "MindFlock/app",
    kind: "prs",
    state: "",
    assignee: "",
  };

  it("tells the three kinds apart, which only this list mixes", () => {
    expect(queuedMatches(queued, t("prs"))).toBe(true);
    expect(queuedMatches(queued, t("pull request"))).toBe(true);
    expect(queuedMatches(queued, t("tickets"))).toBe(false);
  });

  it("takes the reference whole, dashes and hashes included", () => {
    expect(queuedMatches(queued, t("mindflock/app#73"))).toBe(true);
    expect(queuedMatches(queued, t("app 73"))).toBe(true);
  });
});
