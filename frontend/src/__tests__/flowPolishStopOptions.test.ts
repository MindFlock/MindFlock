/** Flow polish — stop options that explain themselves: every option of every
 * stop kind has a plain-words hint (with its default and range), number
 * options keep inside the server's range, "If it fails" says what each pick
 * does, an Agent's CLI / Model show only for a new session, a Wait's time
 * wins over its duration, the stop form reads its options back as one
 * sentence (a Verify stop's from `verifySentence`), the Verify editor
 * explains the self-verify design, and the menus say Pause / Add halt point
 * / Remove halt point / Add a stop / Jump here. */

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import type { FlightStep, OnFail, StepKindDef } from "../lib/flights";
import type { DiagramNode } from "../lib/flightDiagram";

vi.mock("../api/client", () => ({ api: vi.fn(async () => ({})), ApiError: class extends Error {} }));
vi.mock("../lib/toast", () => ({ toast: vi.fn() }));
vi.mock("../lib/errorPop", () => ({ errorPop: vi.fn() }));

const V = await import("../lib/verifyStop");
const E = await import("../lib/stopEditing");
const { StopForm, fieldState } = await import("../components/flights/StopParts");
const { whereHint, WHERE_TYPED_HINT } = await import("../components/flights/VerifyStopFields");
const { SkyGuide } = await import("../components/flights/SkyGuide");
const { queryClient } = await import("../state/queries");

const ENVS = {
  repos: {
    "acme/shop": [
      { name: "live", reach: "https://shop.example.com", branch: "release", deploy_minutes: 5 },
      { name: "sandbox", reach: "https://sandbox.example.com", branch: "sandbox", deploy_minutes: 10 },
    ],
  },
  names: ["live", "sandbox"],
};

// --- The Verify sentence ---------------------------------------------------------

describe("verifySentence: the Verify stop's options in one sentence", () => {
  const say = (c: Record<string, unknown>, onFail: OnFail = "halt") => V.verifySentence(c, onFail, ENVS);

  it("the brief's own example: this branch, right away, agent then me, the agent fixes", () => {
    expect(say({}, "agent")).toBe("Its agent verifies on this branch as soon as the route gets here; you confirm the steps only a person can do; a failure goes back to the agent, up to 2 rounds.");
  });

  it("every who", () => {
    expect(say({ who: "both" })).toContain("; you confirm the steps only a person can do;");
    expect(say({ who: "agent" })).toContain("; it does every step itself, a person's too, and goes on without you when they pass;");
    expect(say({ who: "you" })).toContain("; then the route waits for your sign-off, even when every step passes;");
    expect(say({ who: "nonsense" })).toContain("you confirm the steps only a person can do");
  });

  it("every If it fails", () => {
    expect(say({}, "halt")).toMatch(/; a failure halts the route\.$/);
    expect(say({}, "agent")).toMatch(/; a failure goes back to the agent, up to 2 rounds\.$/);
    expect(say({}, "skip")).toMatch(/; a failure is ignored and the route goes on\.$/);
    expect(say({}, "you")).toMatch(/; a failure waits on you\.$/);
  });

  it("every where and ready when", () => {
    expect(say({ where: "branch", ready: "now" })).toContain("verifies on this branch as soon as the route gets here;");
    expect(say({ where: "branch", ready: "after_prev", ready_minutes: 15 })).toContain("on this branch 15 min after the stop before;");
    expect(say({ where: "branch", ready: "after_prev", ready_minutes: 0 })).toContain("on this branch as soon as the route gets here;");
    // A branch has no environment branch to reach: on_branch reads as now.
    expect(say({ where: "branch", ready: "on_branch" })).toContain("as soon as the route gets here");
    expect(say({ where: "sandbox", ready: "now" })).toContain("verifies on sandbox as soon as the route gets here;");
    expect(say({ where: "live", ready: "on_branch" })).toContain("on live once the work reaches release and live's deploy time has passed;");
    expect(say({ where: "live", ready: "on_branch", ready_minutes: 90 })).toContain("once the work reaches release and 1 h 30 min more;");
    expect(say({ where: "sandbox", ready: "after_prev", ready_minutes: 0 })).toContain("sandbox's deploy time after the stop before");
    // An environment the list doesn't know: its own name's branch.
    expect(V.verifySentence({ where: "qa", ready: "on_branch" }, "halt", null)).toContain("once the work reaches qa's branch");
  });

  it("post to the PR is said; the old design's words never are", () => {
    expect(say({ post_to_pr: true })).toMatch(/; the results go on the PR\.$/);
    for (const who of ["both", "agent", "you"])
      for (const k of ["halt", "agent", "skip", "you"] as OnFail[])
        for (const where of ["branch", "live"]) expect(say({ who, where }, k)).not.toMatch(/behavior|checklist|checker/i);
  });

  it("durations read like the rest of the app", () => {
    expect(V.durationWords(3)).toBe("3 min");
    expect(V.durationWords(60)).toBe("1 h");
    expect(V.durationWords(90)).toBe("1 h 30 min");
  });

  it("the face says the sign-off", () => {
    expect(V.verifyFace({ who: "you" }, ENVS)).toBe("Verify on this branch · agent, then your sign-off · right away");
  });
});

// --- If it fails -----------------------------------------------------------------

describe("If it fails: a hint that follows the pick", () => {
  it("each choice says what the route does", () => {
    expect(E.onFailHint("halt")).toBe("The route stops here, red; you Retry or Skip.");
    expect(E.onFailHint("agent")).toBe("The failure goes to the agent; the stop runs again when its turn ends (up to 2 rounds, then the route halts).");
    expect(E.onFailHint("skip")).toBe("The route goes on as if this stop was not there.");
    expect(E.onFailHint("you")).toBe("The route waits on you, amber, showing the failure.");
    expect(V.FIX_ROUNDS).toBe(2);
  });

  it("a Verify stop's failure in its own words", () => {
    expect(E.onFailHint("agent", "verify")).toBe("The failed steps and their evidence go back to the session's agent, then it verifies again (up to 2 rounds, then the route halts).");
    expect(E.onFailHint("halt", "verify")).toContain("with the failed steps");
    expect(E.onFailHint("you", "verify")).toContain("showing the failed steps");
  });
});

// --- The hint map ----------------------------------------------------------------

/** Every field the server's kinds serve (backend flights.py STEP_KINDS and
 * flight_kinds/), but Verify's (its own editor) and the Handoff's two board
 * pickers (their own hints). */
const SERVER_FIELDS: Record<string, string[]> = {
  agent: ["prompt", "fresh", "join", "program", "model"],
  split: ["prompts", "count", "model", "fence_only", "report_back"],
  join: ["of", "on_failed"],
  ship: ["lane", "ask_first", "message"],
  you: ["text", "items", "notify"],
  wait: ["seconds", "until"],
  reviews: ["quiet_minutes", "max_rounds", "include_bots", "reply_declined", "each_round"],
  checks: ["timeout_minutes"],
  command: ["command", "timeout_minutes"],
  ticket: ["state", "comment"],
  notify: ["text"],
  loop: ["back_to", "times", "every_seconds", "wait_limits", "fresh", "until", "prompt"],
  budget: ["max_usd", "max_minutes", "max_tokens", "scope"],
  call: ["method", "url", "headers", "body", "expect", "timeout_minutes"],
  checkpoint: ["note"],
  handoff: ["wait"],
  judge: ["question", "options", "by"],
  land: ["delete_branch", "remote_branch", "close"],
  listen: ["match", "timeout_minutes"],
  preview: ["command", "timeout_minutes", "stop_command"],
  review: ["program", "model", "focus", "block", "keep"],
  screenshot: ["urls", "widths", "timeout_minutes", "comment_pr"],
  someone: ["who", "text", "items", "webhook"],
  sync: ["with", "how", "fetch"],
  test: ["command", "expect", "rerun_failures", "timeout_minutes"],
};

describe("FIELD_HINT: every option of every stop kind", () => {
  it("has a plain-words hint, and no hint names a field that isn't there", () => {
    for (const [kind, fields] of Object.entries(SERVER_FIELDS)) for (const f of fields) expect(E.fieldHint(kind, f), kind + "." + f).not.toBe("");
    const known = new Set(Object.entries(SERVER_FIELDS).flatMap(([k, fs]) => fs.map((f) => k + "." + f)));
    for (const key of Object.keys(E.FIELD_HINT)) expect(known.has(key), key).toBe(true);
    expect(E.fieldHint("agent", "nope")).toBe("");
  });

  it("a minutes range is said in the hint, the server's own (e.g. CI checks: 1 to 1440 min, default 60)", () => {
    expect(E.fieldHint("checks", "timeout_minutes")).toContain("1 to 1440 min, default 60");
    for (const [key, [lo, hi]] of Object.entries(E.FIELD_RANGE)) {
      expect(E.FIELD_HINT[key], key).toBeTruthy();
      if (key.endsWith("_minutes") && key !== "budget.max_minutes" && key !== "listen.timeout_minutes") expect(E.FIELD_HINT[key], key).toContain(lo + " to " + hi);
    }
  });

  it("a number keeps inside its range; empty stays empty; a field without one is left alone", () => {
    expect(E.clampField("checks", "timeout_minutes", 5000)).toBe(1440);
    expect(E.clampField("checks", "timeout_minutes", 0)).toBe(1);
    expect(E.clampField("checks", "timeout_minutes", "")).toBe("");
    expect(E.clampField("split", "count", 99)).toBe(20);
    expect(E.clampField("loop", "times", 0)).toBe(0);
    expect(E.clampField("call", "timeout_minutes", 61)).toBe(60);
    expect(E.clampField("wait", "seconds", 60 * 60 * 24 * 30)).toBe(60 * 60 * 24 * 14);
    expect(E.clampField("command", "nope", 123456)).toBe(123456);
  });
});

// --- The forms ---------------------------------------------------------------------

const KIND = (kind: string, fields: StepKindDef["fields"], extra: Partial<StepKindDef> = {}): StepKindDef => ({ kind, label: kind, actor: "robot", summary: "", default_title: kind, fields, ...extra });
const AGENT = KIND("agent", [
  { name: "prompt", type: "textarea", label: "Prompt", default: "" },
  { name: "fresh", type: "bool", label: "Start a new session for this stop", default: false },
  { name: "join", type: "bool", label: "Wait for its workers", default: true },
  { name: "program", type: "text", label: "CLI for a fresh session (empty = the same)", default: "" },
  { name: "model", type: "text", label: "Model for a fresh session (empty = the same)", default: "" },
]);
const WAIT = KIND("wait", [
  { name: "seconds", type: "duration", label: "How long", default: 600 },
  { name: "until", type: "text", label: "Or until (HH:MM)", default: "", placeholder: "18:00" },
]);
const CHECKS = KIND("checks", [{ name: "timeout_minutes", type: "int", label: "Give up after (minutes)", default: 60 }]);
const VERIFY = KIND(
  "verify",
  [
    { name: "where", type: "text", label: "Where", default: "branch" },
    { name: "ready", type: "select", label: "Ready when", options: ["now", "on_branch", "after_prev"] },
    { name: "ready_minutes", type: "int", label: "Minutes" },
    { name: "who", type: "select", label: "Who checks", options: ["both", "agent", "you"], option_labels: { you: "Only me" } },
    { name: "notes", type: "textarea", label: "Notes for this check" },
    { name: "post_to_pr", type: "bool", label: "Post the results to the PR" },
  ],
  { default_on_fail: "halt" }
);
const step = (kind: string, config: Record<string, unknown> = {}, extra: Partial<FlightStep> = {}): FlightStep => ({ id: "s1", kind, title: kind, config, on_fail: "halt", ...extra });
const form = (s: FlightStep, def: StepKindDef) => renderToStaticMarkup(createElement(StopForm, { step: s, def, onChange: () => {} }));

describe("the stop form explains itself", () => {
  it("an Agent's CLI and Model show only with “Start a new session” on", () => {
    expect(fieldState(step("agent"), { name: "program" })).toEqual({ hidden: true });
    expect(fieldState(step("agent", { fresh: true }), { name: "model" })).toEqual({});
    const off = form(step("agent", { program: "codex" }), AGENT);
    expect(off).not.toContain("CLI for a fresh session");
    expect(off).not.toContain("Model for a fresh session");
    const on = form(step("agent", { fresh: true }), AGENT);
    expect(on).toContain("CLI for a fresh session");
    expect(on).toContain("Model for a fresh session");
    expect(on).toContain("a new session in this repo takes the prompt");
  });

  it("a Wait says a time wins, and greys How long once a time is typed", () => {
    const plain = form(step("wait", { seconds: 600 }), WAIT);
    expect(plain).toContain("A time wins over How long.");
    expect(plain).not.toContain("fl-dim ");
    const timed = form(step("wait", { seconds: 600, until: "18:00" }), WAIT);
    expect(timed).toContain('class="fl-dim-wrap fl-dim"');
    expect(timed).toContain("opacity:0.45");
    expect(timed).toContain("Not used: a time is set below, and a time wins over How long.");
    expect(fieldState(step("wait", { until: " " }), { name: "seconds" })).toEqual({});
  });

  it("a Reviews stop's “Also, every round” is a text box that shows what is typed, with its hint", () => {
    const REVIEWS = KIND("reviews", [
      { name: "reply_declined", type: "bool", label: "Reply on the PR to comments it declines", default: false },
      { name: "each_round", type: "textarea", label: "Also, every round", default: "", placeholder: "e.g. re-run your code review" },
    ]);
    const html = form(step("reviews", { each_round: "Re-run the security review." }), REVIEWS);
    expect(html).toContain("Also, every round");
    expect(html).toMatch(/<textarea[^>]*>Re-run the security review\.<\/textarea>/);
    expect(html).toContain("Added to every round&#x27;s prompt, after the comments");
    expect(html).toContain("Reply on the PR to comments it declines");
  });

  it("a number field shows its range and default", () => {
    expect(form(step("checks"), CHECKS)).toContain("The stop fails if CI hasn&#x27;t finished by then; a failing check fails it at once. 1 to 1440 min, default 60.");
  });

  it("If it fails: the hint follows the pick, and names the kind's default when it isn't the pick", () => {
    for (const k of ["halt", "agent", "skip", "you"] as OnFail[]) {
      const html = form(step("checks", {}, { on_fail: k }), CHECKS);
      expect(html).toContain('data-on-fail="' + k + '"');
      expect(html).toContain(E.onFailHint(k).replace(/'/g, "&#x27;"));
    }
    expect(form(step("checks", {}, { on_fail: "skip" }), CHECKS)).toContain("Default for checks: Halt.");
    expect(form(step("checks", {}, { on_fail: "halt" }), CHECKS)).not.toContain("Default for");
  });

  it("reads the stop back in one sentence at the top: halt point, Only when, If it fails", () => {
    expect(E.stopSentence({ kind: "checks", on_fail: "halt" })).toBe("If it fails, the route halts, red.");
    expect(E.stopSentence({ kind: "checks", on_fail: "agent", halt: true, when: "weekday" })).toBe(
      "The route pauses here first (a halt point) — Carry on to run it. It runs only when weekday — otherwise it is skipped. If it fails, the agent gets the failure and the stop runs again, up to 2 rounds."
    );
    expect(E.stopSentence({ kind: "x", on_fail: undefined as never }, "skip")).toBe("If it fails, the route goes on without it.");
    expect(E.stopSentence({ kind: "x", on_fail: "you" })).toBe("If it fails, the route waits on you.");
    const html = form(step("checks", {}, { when: "weekday" }), CHECKS);
    expect(html.indexOf("fl-stop-sentence")).toBeLessThan(html.indexOf("Stop title"));
    expect(html).toContain("It runs only when weekday — otherwise it is skipped. If it fails, the route halts, red.");
  });

  it("a Verify stop's form opens on its sentence, and its If it fails says what a Verify failure does", () => {
    const html = form(step("verify", { who: "agent" }, { on_fail: "agent", halt: true }), VERIFY);
    expect(html).toContain('class="fl-form-note fl-stop-sentence fl-fld-wide vf-sentence"');
    expect(html).toContain("The route pauses here first (a halt point) — Carry on to run it. Its agent verifies on this branch as soon as the route gets here; it does every step itself, a person&#x27;s too, and goes on without you when they pass; a failure goes back to the agent, up to 2 rounds.");
    expect(html).toContain("The failed steps and their evidence go back to the session&#x27;s agent, then it verifies again");
    // Who: the UI's words, never the older server's “Only me”.
    expect(html).toContain("<strong>Agent, then my sign-off</strong>");
    expect(html).not.toContain("Only me");
  });
});

describe("Verify's Where: a pick from the repo's environments, never an unknown name", () => {
  it("with the list: no free text, an unknown saved name is flagged", () => {
    queryClient.setQueryData(["test-plan-environments"], ENVS);
    try {
      const html = form(step("verify", { where: "qa" }), VERIFY);
      expect(html).not.toContain("Other…");
      expect(html).not.toContain('placeholder="an environment&#x27;s name"');
      expect(html).toContain(">qa (unknown)<");
      expect(html).toContain("vf-where-unknown");
      expect(html).toContain("is not an environment of any watched repo");
      const ok = form(step("verify", { where: "sandbox" }), VERIFY);
      expect(ok).not.toContain("vf-where-unknown");
      expect(ok).toContain("The deployed copy: acme/shop:");
    } finally {
      queryClient.removeQueries({ queryKey: ["test-plan-environments"] });
    }
  });

  it("the words for each case", () => {
    expect(whereHint("branch", null, false)).toContain("A fresh checkout of this branch — nothing deployed.");
    expect(whereHint("qa", ENVS, true)).toContain("The stop fails while it is unknown.");
    expect(whereHint("sandbox", null, false)).toBe("");
    expect(WHERE_TYPED_HINT).toContain("must match an environment in this repo's Verify settings");
  });
});

// --- Menus ---------------------------------------------------------------------

const node = (over: Partial<DiagramNode>): DiagramNode => ({ id: "stop:s1", kind: "stop", row: "main", title: "Push", sub: "", state: "pending", icon: "ship", index: 1, ...over }) as DiagramNode;

describe("menu words: one name per action", () => {
  const s = step("ship");
  it("Add halt point / Remove halt point (pressed); Carry on only resumes", () => {
    expect(E.haltToggle(node({ step: s }))).toMatchObject({ label: "Add halt point" });
    expect(E.haltToggle(node({ step: s })).pressed).toBeFalsy();
    expect(E.haltToggle(node({ step: { ...s, halt: true } }))).toMatchObject({ label: "Remove halt point", pressed: true });
    expect(E.flightHold(node({ state: "current" }))).toMatchObject({ key: "pause", label: "Pause" });
    expect(E.flightHold(node({ state: "paused" }))).toMatchObject({ key: "resume", label: "Carry on" });
    expect(E.birdAction("working", true)).toMatchObject({ key: "pause", label: "Pause" });
  });

  it("Add a stop, Jump here, Open Verify", () => {
    const all = { can: { edit: true, move: true, duplicate: true, remove: true, open: true, halt: true } };
    expect(E.menuItemsFor(node({ kind: "add", id: "add" }), all).map((i) => i.label)).toEqual(["Add a stop"]);
    expect(E.menuItemsFor(node({ editable: true, step: s }), { ...all, jump: true }).find((i) => i.key === "jump")!.label).toBe("Jump here");
    const v = E.menuItemsFor(node({ state: "perched", step: step("verify") }), { ...all, live: true });
    expect(v.find((i) => i.key === "behaviors")!.label).toBe("Open Verify");
    for (const i of v) expect(i.label + i.title).not.toMatch(/behavior/i);
  });
});

// --- The guide ---------------------------------------------------------------------

describe("the Sky guide says the current product", () => {
  const html = (section: string) => renderToStaticMarkup(createElement(SkyGuide, { open: true, onClose: () => {}, section, portal: false }));
  it("Verify is the session's own agent checking its work; pause, halt points and halted are three things", () => {
    const stops = html("stops");
    expect(stops).toContain("<b>Verify</b> has the session&#x27;s own agent check its work");
    expect(stops).not.toMatch(/behaviors/);
    expect(stops).toContain("<b>Let the agent fix it</b> hands the failure to the agent and runs the stop again (up to 2 rounds, then it halts)");
    const halt = html("halt");
    expect(halt).toContain("<b>⏸ Pause</b>");
    expect(halt).toContain("<b>Add halt point</b>");
    expect(halt).toContain("<b>Remove halt point</b>");
    expect(halt).toContain("A <b>halted</b> route is different");
    expect(halt).not.toContain("Halt now");
    expect(html("bird")).toContain("pause the route where it is");
  });
});

describe("select chips in words, matching their hints", () => {
  it("Join and Budget choices say what they are", async () => {
    const { CHOICE_WORDS } = await import("../components/flights/StopParts");
    const JOIN = KIND("join", [
      { name: "of", type: "select", label: "Wait for", default: "all", options: ["all", "spawned"] },
      { name: "on_failed", type: "select", label: "When a worker failed", default: "halt", options: ["halt", "continue"] },
    ]);
    const html = form(step("join"), JOIN);
    for (const w of [">All workers<", ">Its Split&#x27;s workers<", ">Fail this stop<", ">Go on<"]) expect(html).toContain(w);
    for (const [key, words] of Object.entries(CHOICE_WORDS)) for (const w of Object.values(words)) expect(E.FIELD_HINT[key], key).toContain(w);
  });
});
