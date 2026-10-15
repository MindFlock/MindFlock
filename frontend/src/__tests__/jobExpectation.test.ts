/** lib/jobExpectation — a background job with an expected duration (round-6
 * JOB-EXPECTATIONS contract). A 6-hour job must not read "stuck" while it is
 * inside its 6 hours.
 *
 * Pinned: the chip's words in every state (running / waiting inside an
 * expectation / past it / stuck / waiting by its own rhythm); spans and
 * duration parsing (the server's rules: 1 minute … 14 days); the menu's POST
 * and DELETE; the menu's items (Stop kept, no prompt()); the bell row and
 * toast for session.job_past_expected. */

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { BackgroundJob } from "../api/types";
import {
  EXPECT_PRESETS,
  etaClock,
  formatSpan,
  jobChipText,
  jobPastExpectedNote,
  parseDuration,
} from "../lib/jobExpectation";
import { backgroundChip, backgroundHint } from "../lib/backgroundJobs";
import { notifFromEvent } from "../components/NotificationsBell";
import type { EventEnvelope } from "../state/queries";
import { dropComments, normCode, src } from "./sourceText";

const toasts: string[] = [];
vi.mock("../lib/toast", () => ({ toast: (m: string) => void toasts.push(m) }));

const H = 3600;
/** A fixed "now" (epoch seconds) at local 12:00 today, so clock times are stable. */
const NOW = (() => {
  const d = new Date();
  d.setHours(12, 0, 0, 0);
  return d.getTime() / 1000;
})();

const job = (state: string, o: Partial<BackgroundJob> = {}): BackgroundJob => ({
  id: "bk0m2qds7",
  pid: 100,
  command: "python train.py --epochs 40",
  state,
  reason: "no CPU or output for 4m",
  started: NOW - (2 * H + 10 * 60),
  quiet_s: 4 * 60,
  ...o,
});
/** Expected to take 6h, set by you, 2h 10m in. */
const sixH = (state: string, o: Partial<BackgroundJob> = {}) =>
  job(state, { expect_s: 6 * H, expected_by: "you", eta: NOW - (2 * H + 10 * 60) + 6 * H, past_expected: false, ...o });

describe("formatSpan", () => {
  it("says a span the way people do", () => {
    expect(formatSpan(0)).toBe("<1m");
    expect(formatSpan(20)).toBe("<1m");
    expect(formatSpan(45 * 60)).toBe("45m");
    expect(formatSpan(2 * H + 10 * 60)).toBe("2h 10m");
    expect(formatSpan(6 * H)).toBe("6h");
    expect(formatSpan(24 * H)).toBe("24h");
    expect(formatSpan(51 * H)).toBe("2d 3h");
    expect(formatSpan(3 * 24 * H)).toBe("3d");
  });
});

describe("parseDuration", () => {
  it("reads what the Custom… field accepts", () => {
    expect(parseDuration("6h")).toBe(6 * H);
    expect(parseDuration("90m")).toBe(90 * 60);
    expect(parseDuration("1h30m")).toBe(90 * 60);
    expect(parseDuration(" 1h 30m ")).toBe(90 * 60);
    expect(parseDuration("2d")).toBe(48 * H);
    expect(parseDuration("90s")).toBe(90);
    expect(parseDuration("1.5h")).toBe(90 * 60);
    expect(parseDuration("6H")).toBe(6 * H);
    // A bare number is seconds (the contract's rule, same as the server's).
    expect(parseDuration("600")).toBe(600);
    expect(parseDuration(3600)).toBe(3600);
  });

  it("refuses junk and anything outside 1 minute … 14 days", () => {
    for (const bad of ["", "  ", "six hours", "6x", "h", "30m1h", "-5m", "6h6h"]) expect(parseDuration(bad)).toBeNull();
    expect(parseDuration("30s")).toBeNull();
    expect(parseDuration("15d")).toBeNull();
    expect(parseDuration("14d")).toBe(14 * 24 * H);
    expect(parseDuration("1m")).toBe(60);
  });

  it("the presets are the menu's four", () => {
    expect(EXPECT_PRESETS.map((p) => p.label)).toEqual(["30 min", "1 h", "6 h", "24 h"]);
    expect(EXPECT_PRESETS.map((p) => p.secs)).toEqual([1800, H, 6 * H, 24 * H]);
  });
});

describe("jobChipText", () => {
  it("waiting inside its expectation reads as waiting, never running, with the ETA as a clock time", () => {
    const t = jobChipText(sixH("waiting"), NOW);
    expect(t.label).toBe("waiting on job · 2h 10m of ~6h");
    expect(t.label).not.toMatch(/running/);
    expect(t.cls).toBe("bgchip bg-waiting");
    expect(t.title).toContain("Expected to take ~6h, set by you");
    expect(t.title).toContain("done about 15:50");
    expect(t.title).toContain("python train.py");
  });

  it("past its expectation, not yet stuck: 'job past ~6h' with the amber dot", () => {
    const t = jobChipText(sixH("waiting", { started: NOW - 7 * H, past_expected: true, threshold_s: 9 * H, quiet_s: 25 * 60 }), NOW);
    expect(t.label).toBe("job past ~6h");
    expect(t.cls).toBe("bgchip bg-waiting bg-past");
    expect(t.kind).toBe("past");
    expect(t.title).toContain("Past its expected ~6h");
    expect(t.title).toContain("7h in, quiet 25m");
    expect(t.title).toContain("called stuck after 9h quiet");
    // An older server sends no past_expected: it is worked out from started.
    expect(jobChipText(sixH("waiting", { started: NOW - 7 * H, past_expected: undefined }), NOW).label).toBe("job past ~6h");
  });

  it("stuck stays 'job stuck ✕', its tooltip the server's reason", () => {
    const reason = "expected ~6h, now 9h 10m in and quiet for 3h";
    const t = jobChipText(sixH("stuck", { reason }), NOW);
    expect(t.label).toBe("job stuck ✕");
    expect(t.cls).toBe("bgchip bg-stuck");
    expect(t.title).toContain(reason);
  });

  it("waiting by its own rhythm says why it isn't stuck yet", () => {
    const reason = "quiet 22m — it has resumed after 41m before";
    const t = jobChipText(job("waiting", { reason, longest_gap_s: 41 * 60, quiet_s: 22 * 60 }), NOW);
    expect(t.label).toBe("job waiting");
    expect(t.title).toContain(reason);
    // No learned rhythm: the plain quiet time, as before.
    expect(jobChipText(job("waiting", { quiet_s: 300 }), NOW).title).toContain("(quiet 5m)");
  });

  it("running: no chip on the rail, but the pane's tooltip carries the progress", () => {
    expect(backgroundChip({ background: [sixH("running")] }, NOW)).toBeNull();
    expect(backgroundHint({ background: [sixH("running")] }, NOW)).toBe(
      "Turn ended; a background job is still running (2h 10m of ~6h, done about 15:50): python train.py --epochs 40"
    );
    expect(backgroundHint({ background: [job("running")] }, NOW)).toBe(
      "Turn ended; a background job is still running: python train.py --epochs 40"
    );
  });

  it("names who set it", () => {
    expect(jobChipText(sixH("waiting", { expected_by: "agent" }), NOW).title).toContain("set by the agent");
    expect(jobChipText(sixH("waiting", { expected_by: "command" }), NOW).title).toContain("set by its command");
  });

  it("an ETA on another day carries the weekday", () => {
    expect(etaClock(NOW + 2 * 24 * H, NOW)).toMatch(/^\S+ 12:00$/);
    expect(etaClock(NOW + H, NOW)).toBe("13:00");
  });
});

describe("the rail chip", () => {
  it("a job past its expectation outranks a plain waiting one; stuck outranks both", () => {
    const past = sixH("waiting", { id: "b2", pid: 2, started: NOW - 7 * H, past_expected: true });
    const c = backgroundChip({ background: [job("waiting", { pid: 1 }), past] }, NOW)!;
    expect(c.cls).toContain("bg-past");
    expect(c.job?.id).toBe("b2");
    expect(c.label).toBe("2 jobs waiting");
    const s = backgroundChip({ background: [past, job("stuck", { pid: 3 })] }, NOW)!;
    expect(s.cls).toContain("bg-stuck");
    expect(s.stopPid).toBe(3);
  });

  it("this device's rows open the menu; a proxied row has none", () => {
    expect(backgroundChip({ background: [sixH("waiting")] }, NOW)!.job?.id).toBe("bk0m2qds7");
    expect(backgroundChip({ background: [sixH("waiting")] }, NOW)!.title).toContain("click to say how long it should take");
    const remote = backgroundChip({ device: "rig", background: [sixH("waiting")] }, NOW)!;
    expect(remote.job).toBeUndefined();
    expect(remote.title).not.toContain("click");
  });
});

describe("the chip's menu", () => {
  const calls: { url: string; method: string; body: unknown }[] = [];
  const realFetch = globalThis.fetch;
  beforeEach(() => {
    calls.length = 0;
    toasts.length = 0;
    globalThis.fetch = ((url: string, init?: RequestInit) => {
      calls.push({ url: String(url), method: (init?.method || "GET").toUpperCase(), body: init?.body ? JSON.parse(String(init.body)) : null });
      return Promise.resolve({
        ok: true,
        status: 200,
        text: () => Promise.resolve(JSON.stringify({ ok: true, expect_s: 21600, eta: 0 })),
      } as unknown as Response);
    }) as typeof fetch;
  });
  afterEach(() => {
    globalThis.fetch = realFetch;
  });

  it("Expected to take… POSTs the duration to the job's route; Clear DELETEs it", async () => {
    const { expectBackgroundJob } = await import("../lib/stopBackgroundJob");
    expect(await expectBackgroundJob("my win", "bk0m2qds7", 6 * H)).toBe(true);
    expect(calls[0]).toEqual({ url: "/api/instances/my%20win/jobs/bk0m2qds7/expect", method: "POST", body: { duration: 21600 } });
    expect(toasts[0]).toContain("expected to take ~6h");
    expect(await expectBackgroundJob("my win", "bk0m2qds7", null)).toBe(true);
    expect(calls[1]).toMatchObject({ url: "/api/instances/my%20win/jobs/bk0m2qds7/expect", method: "DELETE" });
  });

  it("draws Stop (stuck), Expected to take…, and Clear only when one is set", async () => {
    const { JobMenu } = await import("../components/sidebar/SidebarRow");
    const stuck = renderToStaticMarkup(createElement(JobMenu, { title: "t", job: sixH("stuck"), stopPid: 100, onDone: () => {} }));
    expect(stuck).toContain("Stop the job");
    expect(stuck).toContain("Expected to take…");
    expect(stuck).toContain("Now ~6h, set by you");
    expect(stuck).toContain("Clear expectation");
    const plain = renderToStaticMarkup(createElement(JobMenu, { title: "t", job: job("waiting"), onDone: () => {} }));
    expect(plain).not.toContain("Stop the job");
    expect(plain).toContain("Expected to take…");
    expect(plain).not.toContain("Clear expectation");
  });

  it("Custom… is one inline field (never prompt()), and the chip opens the menu", () => {
    const row = dropComments(src("components/sidebar/SidebarRow.tsx"));
    const menu = row.slice(row.indexOf("export function JobMenu"));
    expect(menu).not.toMatch(/\bprompt\(/);
    expect(menu).toContain('placeholder="6h, 90m or 1h30m"');
    expect(menu).toContain("parseDuration(text)");
    expect(menu).toContain("EXPECT_PRESETS.map");
    expect(normCode("components/sidebar/SidebarRow.tsx")).toContain("<JobMenu title={title} job={bg.job} stopPid={bg.stopPid}");
  });
});

describe("session.job_past_expected", () => {
  const env = (e: Partial<EventEnvelope>): EventEnvelope => ({ event: "", session: "s", seq: 1, ts: 0, ...e }) as EventEnvelope;

  it("is a bell row, once per job", () => {
    const n = notifFromEvent(
      env({ event: "session.job_past_expected", session: "train", data: { job_id: "bk1", expect_s: 6 * H, elapsed_s: 6 * H + 60 } })
    );
    expect(n).toEqual({ text: "job is past its expected ~6h — still quiet", cls: "n-warn", dedupe: "job-past:train:bk1" });
  });

  it("toasts the same words, named", () => {
    expect(jobPastExpectedNote("[2] train", 6 * H)).toBe("Job on [2] train is past its expected ~6h — still quiet");
    const toasts = normCode("components/EventToasts.tsx");
    expect(toasts).toContain('ev.subscribe("session.job_past_expected"');
    expect(toasts).toContain("jobPastExpectedNote(namedSlot(env.session)");
  });
});
