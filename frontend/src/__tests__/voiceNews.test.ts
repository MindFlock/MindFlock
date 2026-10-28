/** What the desktop mic says by itself (lib/voiceDesktop.ts, the twin of
 * mobile.js's voiceIsNews / voiceContextLead): only news — a reply that
 * ended after the listener could have heard it and no more than ten minutes
 * ago — and, when the reply is from a session you were not just talking to,
 * where it is from first ("In Login fix, on fix the pin bar: …"). The rules
 * are pure tables with a clock; TopBarMic's wiring is pinned against its
 * source. */

import { describe, expect, it } from "vitest";
import {
  CONTEXT_GAP_MS,
  HeardClock,
  NEWS_MS,
  ReplyTracker,
  contextLead,
  isNews,
  replyText,
  turnEndedAt,
} from "../lib/voiceDesktop";
import { code } from "./sourceText";

const M = 60_000;
const NOW = 100 * M;

describe("isNews: nothing old is read by itself", () => {
  it.each([
    ["ended a moment ago, after the listener was last here", NOW - 5_000, NOW - M, true],
    ["ended before the listener was last here (page, replies on, a look)", NOW - 2 * M, NOW - M, false],
    ["ended exactly then", NOW - M, NOW - M, false],
    ["never heard from, recent", NOW - 3 * M, 0, true],
    ["nine minutes old", NOW - 9 * M, NOW - 20 * M, true],
    ["ten minutes old", NOW - 10 * M, NOW - 20 * M, true],
    ["eleven minutes old: done for a while", NOW - 11 * M, NOW - 20 * M, false],
    ["an hour old, never heard", NOW - 60 * M, 0, false],
    ["unknown end", 0, 0, false],
  ])("%s", (_label, ended, heard, want) => {
    expect(isNews(ended, heard, NOW)).toBe(want);
  });

  it("the window is ten minutes on both surfaces", () => {
    expect(NEWS_MS).toBe(10 * M);
    expect(CONTEXT_GAP_MS).toBe(10 * M);
  });
});

describe("turnEndedAt: the event's stamp less turn_ended's dwell", () => {
  it.each([
    [1000, undefined, 1_000_000],
    [1000, 12, 988_000],
    [1000, "12.5", 987_500],
    [1000, -3, 1_000_000],
    [0, 12, 0],
    [null, 12, 0],
  ])("ts %s, idle_for %s → %s ms", (ts, idle, want) => {
    expect(turnEndedAt(ts as number | null, idle)).toBe(want);
  });

  it("a turn_ended for a session idle an hour is not news", () => {
    const at = turnEndedAt(NOW / 1000, 3600);
    expect(isNews(at, 0, NOW)).toBe(false);
  });
});

describe("contextLead: where a reply is from, when that is not obvious", () => {
  const base = {
    title: "a",
    focused: "a",
    name: "Login fix",
    headline: "Fix the pin bar",
    lastAt: NOW - M,
    now: NOW,
    text: "Fixed it.",
  };
  it.each([
    ["the session you are talking to, just now", {}, ""],
    ["the same session, not heard from in eleven minutes", { lastAt: NOW - 11 * M }, "In Login fix, on fix the pin bar: "],
    ["the same session, never heard from", { lastAt: 0 }, "In Login fix, on fix the pin bar: "],
    ["another session than the one on screen", { focused: "b" }, "In Login fix, on fix the pin bar: "],
    ["nothing focused", { focused: null }, "In Login fix, on fix the pin bar: "],
    ["no headline", { focused: "b", headline: null }, "In Login fix: "],
    ["an acronym keeps its case, the full stop goes", { focused: "b", headline: "API docs cleanup." }, "In Login fix, on API docs cleanup: "],
    ["a line that names it already", { focused: "b", text: "Login fix: fixed it." }, ""],
    ["a line that already says In <name>", { focused: "b", text: "In Login fix, all done." }, ""],
    ["no name", { focused: "b", name: "" }, ""],
  ])("%s", (_label, o, want) => {
    expect(contextLead({ ...base, ...o })).toBe(want);
  });

  it("replyText: the lead, then the words", () => {
    expect(
      replyText({
        ...base,
        focused: "b",
        text: "  We were working on the pin bar, and it now sits above the terminal.  ",
      })
    ).toBe("In Login fix, on fix the pin bar: We were working on the pin bar, and it now sits above the terminal.");
    expect(replyText({ ...base, text: " Done. " })).toBe("Done.");
  });
});

describe("HeardClock: since when anything is news, per session", () => {
  it("the later of the page / replies-on restart and the session's own mark", () => {
    const c = new HeardClock(10 * M);
    expect(c.heardAt("a")).toBe(10 * M);
    expect(c.last("a")).toBe(0);
    c.mark("a", 12 * M);
    expect(c.heardAt("a")).toBe(12 * M);
    expect(c.last("a")).toBe(12 * M);
    c.restart(15 * M);
    expect(c.heardAt("a")).toBe(15 * M);
    expect(c.last("a")).toBe(12 * M); // the context rule keeps the real mark
    c.mark(null, 20 * M);
    c.mark("", 20 * M);
    c.clear();
    expect(c.last("a")).toBe(0);
  });

  it("switching to a session whose reply is old says nothing; one that ended after you left is news", () => {
    const c = new HeardClock(0);
    c.mark("a", 10 * M); // looked away from a at 10 min
    expect(isNews(9 * M, c.heardAt("a"), 12 * M)).toBe(false);
    expect(isNews(11 * M, c.heardAt("a"), 12 * M)).toBe(true);
    c.mark("a", 12 * M); // looked at it again
    expect(isNews(11 * M, c.heardAt("a"), 13 * M)).toBe(false);
  });
});

describe("ReplyTracker.sentAt: when you last talked to a session", () => {
  it("is kept after its reply is taken, and for a say:false send too", () => {
    const t = new ReplyTracker();
    expect(t.sentAt("a")).toBe(0);
    t.sent("a", true, 5_000);
    expect(t.take("a", "Done.", 6, 6_000)).toBe("Done.");
    expect(t.sentAt("a")).toBe(5_000);
    t.sent("b", false, 7_000);
    expect(t.sentAt("b")).toBe(7_000);
    t.clear();
    expect(t.sentAt("a")).toBe(0);
  });
});

describe("TopBarMic: every reply it reads by itself is news, worded with its context", () => {
  const mic = code("components/TopBarMic.tsx");

  it("the gate comes before anything is said; a prompt sent from here is the tracker's", () => {
    const gate = "if (!ours && !isNews(turnEndedAt(ts, data.idle_for), clock.heardAt(title))) return;";
    expect(mic).toContain(gate);
    expect(mic.indexOf(gate)).toBeLessThan(mic.indexOf("const speak = (words: string | null"));
  });

  it("the line is worded when its turn comes, and marks the session heard", () => {
    expect(mic).toContain("lastAt: Math.max(clock.last(title), tracker.sentAt(title)),");
    expect(mic).toContain("headline: taskHeadline(title),");
    expect(mic).toContain("clock.mark(title);\n          return line;");
  });

  it("a focus change marks both sessions; replies turned on restart the clock", () => {
    expect(mic).toContain("clock.mark(prev.focused);\n      clock.mark(s.focused);");
    expect(mic).toContain("if (repliesOn) clock.restart();");
  });
});
