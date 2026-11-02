/** Round 15 `when-ui` — a stop's "Only when" (docs/flights.md "`when`: a
 * condition on every stop"): the field in the stop form (and so in the stop
 * dialog that mounts it) with its ⓘ grammar, validation through `GET
 * /api/flights/when` 300 ms after the last keystroke, the canonical form on
 * blur, an older server's 404 tolerated (no live validation, just the
 * field), `when` carried through the flyway helpers, a skipped stop's note,
 * `kindName` for `someone`, and the kind's `default_on_fail`. */

/// <reference types="vite/client" />
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { code } from "./sourceText";
import type { FlightStep, StepKindDef } from "../lib/flights";
import type { DiagramNode } from "../lib/flightDiagram";

class FakeApiError extends Error {
  status: number;
  body: unknown;
  constructor(status: number, message: string, body: unknown) {
    super(message);
    this.status = status;
    this.body = body;
  }
}
const api = vi.fn(async (_path: string): Promise<unknown> => ({}));
vi.mock("../api/client", () => ({ api: (p: string) => api(p), ApiError: FakeApiError }));
vi.mock("../lib/toast", () => ({ toast: vi.fn() }));
vi.mock("../lib/errorPop", () => ({ errorPop: vi.fn() }));

const { StopForm, StopInspector, kindName } = await import("../components/flights/StopParts");
const { StopDialog } = await import("../components/flights/StopDialog");
const W = await import("../lib/whenHelp");
const F = await import("../lib/flights");

(globalThis as Record<string, unknown>).document ??= {
  hidden: false,
  addEventListener: () => {},
  removeEventListener: () => {},
  getElementById: () => null,
  querySelector: () => null,
};

const CHECKS: StepKindDef = {
  kind: "checks",
  label: "Checks",
  actor: "robot",
  summary: "Waits for CI",
  fields: [{ name: "minutes", type: "int", label: "Wait (minutes)", default: 30 }],
  default_title: "Checks",
  default_on_fail: "agent",
};
const step = (over: Partial<FlightStep> = {}): FlightStep => ({ id: "s1", kind: "checks", title: "Checks", config: {}, on_fail: "halt", ...over });
const form = (s: FlightStep, def: StepKindDef | undefined = CHECKS, readOnly = false) =>
  renderToStaticMarkup(createElement(StopForm, { step: s, def, onChange: () => {}, readOnly }));
const pressed = (html: string) => [...html.matchAll(/<button type="button" class="on" aria-pressed="true"[^>]*>([^<]+)<\/button>/g)].map((m) => m[1]);

describe("the Only when field", () => {
  it("draws under the kind's fields, before If it fails, with its ⓘ", () => {
    const html = form(step({ when: "diff touches frontend/**" }));
    const at = (s: string) => html.indexOf(s);
    expect(html).toContain('aria-label="Only when"');
    expect(html).toContain('value="diff touches frontend/**"');
    expect(at(">Wait<")).toBeLessThan(at(">Only when<"));
    expect(at(">Only when<")).toBeLessThan(at(">If it fails<"));
    expect(html).toMatch(/<button type="button" class="fl-icon-btn fl-when-help-btn" aria-label="What can go here"[^>]*aria-expanded="false"[^>]*>ⓘ<\/button>/);
    expect(html).toContain('maxLength="300"');
    // Closed until ⓘ is pressed; no error before anything was checked.
    expect(html).not.toContain("fl-when-help\"");
    expect(html).not.toContain("fl-when-error");
  });

  it("is empty for a stop with no condition, read-only on one that can't change", () => {
    expect(form(step())).toContain('aria-label="Only when"');
    expect(form(step())).toMatch(/aria-label="Only when"[^>]*value=""/);
    expect(form(step({ when: "weekday" }), CHECKS, true)).toMatch(/aria-label="Only when"[^>]*readOnly=""/);
  });

  it("is in the stop dialog (which mounts the form)", () => {
    const node: DiagramNode = { id: "stop:s1", kind: "stop", row: "main", title: "Checks", sub: "", state: "pending", icon: "checks", step: step({ when: "weekend" }), index: 1, editable: true };
    const html = renderToStaticMarkup(createElement(StopDialog, { node, def: CHECKS, portal: false, onClose: () => {}, onSave: () => {} } as never));
    expect(html).toContain('value="weekend"');
  });

  it("the ⓘ popover is inline DOM listing the grammar — never a native alert", () => {
    const src = code("components/flights/StopParts.tsx");
    expect(src).toContain("{help && <WhenHelp");
    expect(src).toMatch(/WHEN_GRAMMAR\.map/);
    expect(src).not.toMatch(/\b(window\.)?(confirm|prompt|alert)\(/);
    const conds = W.WHEN_GRAMMAR.map((r) => r.condition).join("\n");
    for (const w of ["diff touches", "diff lines", "files >", "weekday", "hour between", "branch matches", "label <name>", "judge is", "always"]) expect(conds).toContain(w);
    expect(W.WHEN_GRAMMAR.every((r) => r.meaning)).toBe(true);
  });
});

describe("validation", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    api.mockReset();
  });
  afterEach(() => vi.useRealTimers());

  const ok = (canonical: string) => ({ ok: true, canonical, error: "", field: "" });
  const rig = (check: (e: string) => Promise<unknown>) => {
    const results: Array<[string, unknown]> = [];
    const canon: string[] = [];
    const calls: string[] = [];
    const c = W.whenChecker({
      check: (e) => {
        calls.push(e);
        return check(e) as never;
      },
      onResult: (t, r) => results.push([t, r]),
      onCanonical: (x) => canon.push(x),
    });
    return { c, results, canon, calls };
  };

  it("checkWhen asks GET /api/flights/when with the expression", async () => {
    api.mockResolvedValueOnce({ ok: false, canonical: "", error: "unknown word 'fridays'", field: "when" });
    const r = await W.checkWhen("fridays & weekday");
    expect(api).toHaveBeenCalledWith("/api/flights/when?expr=fridays%20%26%20weekday");
    expect(r).toEqual({ ok: false, canonical: "", error: "unknown word 'fridays'", field: "when" });
  });

  it("asks once, 300 ms after the last keystroke, with the latest text", async () => {
    const { c, calls, results } = rig(async (e) => ok(e));
    c.input("w");
    vi.advanceTimersByTime(200);
    c.input("we");
    vi.advanceTimersByTime(200);
    c.input("weekday");
    vi.advanceTimersByTime(299);
    expect(calls).toEqual([]);
    vi.advanceTimersByTime(1);
    expect(calls).toEqual(["weekday"]);
    await vi.runAllTimersAsync();
    expect(results.at(-1)).toEqual(["weekday", ok("weekday")]);
    expect(W.WHEN_DEBOUNCE_MS).toBe(300);
  });

  it("the server's error goes under the field; a stale answer is dropped", async () => {
    let release: (v: unknown) => void = () => {};
    const { c, results } = rig((e) => (e === "slow" ? new Promise((r) => (release = r)) : Promise.resolve({ ok: false, canonical: "", error: "bad " + e, field: "when" })));
    c.input("slow");
    vi.advanceTimersByTime(300);
    c.input("hour between 9");
    vi.advanceTimersByTime(300);
    await vi.runAllTimersAsync();
    release(ok("slow"));
    await vi.runAllTimersAsync();
    expect(results.map((r) => r[0])).toEqual(["hour between 9"]);
    const res = results[0][1] as Parameters<typeof W.whenError>[1];
    expect(W.whenError("hour between 9", res)).toBe("bad hour between 9");
  });

  it("the canonical form replaces the text on blur, never while typing", async () => {
    const { c, canon, calls } = rig(async () => ok("diff touches frontend/** and weekday"));
    c.input("DIFF touches frontend/**  AND weekday");
    await vi.advanceTimersByTimeAsync(300);
    expect(canon).toEqual([]);
    c.blur("DIFF touches frontend/**  AND weekday");
    await vi.runAllTimersAsync();
    expect(canon).toEqual(["diff touches frontend/** and weekday"]);
    // The answer for that text is reused, not asked again.
    expect(calls).toHaveLength(1);
  });

  it("a blur before the wait is over asks at once and still settles the text", async () => {
    const { c, canon, calls } = rig(async () => ok("weekend"));
    c.input("Weekend");
    c.blur("Weekend");
    await vi.runAllTimersAsync();
    expect(calls).toEqual(["Weekend"]);
    expect(canon).toEqual(["weekend"]);
  });

  it("a canonical answer that arrives after the field is typed in again is ignored", async () => {
    let release: (v: unknown) => void = () => {};
    const { c, canon } = rig(() => new Promise((r) => (release = r)));
    c.blur("Weekend");
    c.focus();
    release(ok("weekend"));
    await vi.runAllTimersAsync();
    expect(canon).toEqual([]);
  });

  it("an older server without the route (404): no live validation, just the field", async () => {
    api.mockRejectedValue(new FakeApiError(404, "MindFlock couldn't find that", { detail: "Not Found" }));
    expect(await W.checkWhen("weekday")).toBeNull();
    const { c, calls, results, canon } = rig(W.checkWhen);
    c.input("weekday");
    await vi.runAllTimersAsync();
    expect(results.at(-1)).toEqual(["weekday", null]);
    c.input("nonsense words");
    c.blur("nonsense words");
    await vi.runAllTimersAsync();
    // Asked once; never again once the server said it has no validator.
    expect(calls).toEqual(["weekday"]);
    expect(canon).toEqual([]);
    expect(W.whenError("nonsense words", null)).toBe("");
  });

  it("a server error is not the user's: no error under the field", async () => {
    api.mockRejectedValueOnce(new Error("network down"));
    expect(await W.checkWhen("weekday")).toBeNull();
    api.mockRejectedValueOnce(new FakeApiError(500, "MindFlock hit an error", null));
    expect(await W.checkWhen("weekday")).toBeNull();
    // A refusal that carries the contract's shape is the answer.
    api.mockRejectedValueOnce(new FakeApiError(400, "bad", { ok: false, canonical: "", error: "expected a number", field: "when" }));
    expect((await W.checkWhen("files > x"))?.error).toBe("expected a number");
  });

  it("empty text is no condition (never asked); more than one line is refused locally", async () => {
    const { c, calls, results } = rig(async () => ok("x"));
    c.input("");
    c.input("weekday\nweekend");
    await vi.runAllTimersAsync();
    expect(calls).toEqual([]);
    expect(results).toEqual([
      ["", null],
      ["weekday\nweekend", null],
    ]);
    expect(W.whenError("weekday\nweekend", null)).toBe("One line, please.");
    expect(W.whenError("x".repeat(301), null)).toMatch(/300 characters/);
    expect(W.whenError("  ", null)).toBe("");
  });

  it("looksLikeWhen: non-empty, one line, not too long", () => {
    expect(W.looksLikeWhen("weekday")).toBe(true);
    expect(W.looksLikeWhen("")).toBe(false);
    expect(W.looksLikeWhen("   ")).toBe(false);
    expect(W.looksLikeWhen(null)).toBe(false);
    expect(W.looksLikeWhen("a\nb")).toBe(false);
    expect(W.looksLikeWhen("a\rb")).toBe(false);
    expect(W.looksLikeWhen("x".repeat(300))).toBe(true);
    expect(W.looksLikeWhen("x".repeat(301))).toBe(false);
  });

  it("unmounted: nothing more is asked or answered", async () => {
    const { c, calls, results } = rig(async () => ok("weekday"));
    c.input("weekday");
    c.cancel();
    await vi.runAllTimersAsync();
    expect(calls).toEqual([]);
    expect(results).toEqual([]);
  });
});

describe("when survives the flyway helpers", () => {
  it("planFromSteps keeps it (trimmed), and leaves it out when there is none", () => {
    const p = F.planFromSteps("x", [step({ when: "  weekday " }), step({ id: "s2" })]);
    expect(p.steps[0].when).toBe("weekday");
    expect("when" in p.steps[1]).toBe(false);
  });

  it("duplicateStep and toggleHalt carry it", () => {
    const steps = [step({ when: "weekend" })];
    expect(F.duplicateStep(steps, 0)[1].when).toBe("weekend");
    expect(F.toggleHalt(steps, 0)[0].when).toBe("weekend");
  });
});

describe("a skipped stop", () => {
  it("the inspector reads its note in the muted tone", () => {
    const html = renderToStaticMarkup(
      createElement(StopInspector, {
        step: step({ when: "weekend" }),
        def: CHECKS,
        onChange: () => {},
        readOnly: true,
        run: { state: "skipped", note: "skipped: weekend — it is Thursday" },
      })
    );
    expect(html).toContain('<p class="fl-note muted fl-skipped-note">skipped: weekend — it is Thursday</p>');
  });

  it("only a skipped stop, with the bare word when there is no note", () => {
    expect(W.skippedNote({ state: "skipped", note: "" })).toBe("skipped");
    expect(W.skippedNote({ state: "done", note: "skipped: x" })).toBeNull();
    expect(W.skippedNote(undefined)).toBeNull();
    const html = renderToStaticMarkup(createElement(StopInspector, { step: step(), def: CHECKS, onChange: () => {}, run: { state: "done", note: "312 passed" } }));
    expect(html).not.toContain("fl-skipped-note");
  });
});

describe("kind names", () => {
  it("someone is Someone, you is a Perch, the rest their label", () => {
    expect(kindName({ kind: "someone", label: "Someone else" })).toBe("Someone");
    expect(kindName(undefined, "someone")).toBe("Someone");
    expect(kindName({ kind: "you", label: "You" })).toBe("Perch");
    expect(kindName(CHECKS)).toBe("Checks");
    expect(kindName(undefined, "judge")).toBe("judge");
  });
});

describe("the kind's default_on_fail", () => {
  it("a new stop of the kind takes it; an older server or an unknown word is halt", () => {
    expect(F.newStep(CHECKS, []).on_fail).toBe("agent");
    expect(F.newStep({ ...CHECKS, default_on_fail: undefined }, []).on_fail).toBe("halt");
    expect(F.defaultOnFail({ default_on_fail: "explode" })).toBe("halt");
    expect(F.defaultOnFail({ default_on_fail: "toString" })).toBe("halt");
    expect(F.defaultOnFail({ default_on_fail: "skip" })).toBe("skip");
    expect(F.defaultOnFail(undefined)).toBe("halt");
  });

  it("the form presses it when the stop says nothing, and names it", () => {
    const html = form({ ...step(), on_fail: undefined as never });
    expect(pressed(html)).toEqual(["Hand it to the agent"]);
    expect(html).toContain("Default for Checks: Hand it to the agent");
    expect(html).toMatch(/title="The default for this kind of stop"[^>]*>Hand it to the agent</);
  });

  it("the stop's own choice still wins", () => {
    expect(pressed(form(step({ on_fail: "skip" })))).toEqual(["Skip it"]);
  });

  it("no hint from an older server that sends none", () => {
    expect(form(step(), { ...CHECKS, default_on_fail: undefined })).not.toContain("Default for");
  });
});
