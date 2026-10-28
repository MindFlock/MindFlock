/** Claude's question prompt in the ask pop-up (lib/askSelect.ts and the
 * select view in components/dialogs/AskDialog.tsx).
 *
 * What is pinned: the /dialog body keeps the select fields; the exact posts
 * per state (single → its keys; multi → the changed boxes' keys in one post
 * then Right; review → Submit's keys; Type something → its keys, a beat, then
 * {text, keys: ["Enter"]}; Chat about this → its keys, a beat, then /send);
 * every post pinned to the same dialog id (a later refusal retried once
 * unpinned); the flow steps without
 * closing (a dialog_changed of the same kind is just the next step, another
 * kind hands off with "The prompt changed"); the keys; the spoken words;
 * and the view. The vitest environment is node: the view through
 * react-dom/server, the rest pure. */

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Dialog } from "../api/types";
import { code } from "./sourceText";

const instApi = vi.fn();
vi.mock("../api/client", async (orig) => ({
  ...((await orig()) as object),
  instApi: (...a: unknown[]) => instApi(...a),
}));

const { ApiError } = await import("../api/client");
const { normDialog } = await import("../lib/flockActions");
const sel = await import("../lib/askSelect");
const { selectPosts, runSelect, selectKey, selectStage, fieldPhrase, SelectFlows, answerSelect, SELECT_GRACE_MS } = sel;
const { SelectView, SELECT_KEYS, navLabel } = await import("../components/dialogs/AskDialog");
const { askKindLabel } = await import("../lib/ask");
const { dialogChoices, dialogRetryLine, HandsFree } = await import("../lib/voiceDesktop");

const opt = (key: string, label: string, over: Record<string, unknown> = {}) => ({
  key,
  label,
  desc: null,
  kind: "other",
  checked: null,
  keys: [key],
  ...over,
});
const steps = [
  { label: "Auth", done: false },
  { label: "Tests", done: false },
  { label: "Submit", done: false, submit: true },
];
const nav = { next: ["Right"], prev: ["Left"], submit: ["Enter"] };

/** The server's body for one step. */
function body(over: Record<string, unknown> = {}) {
  return {
    id: "q1",
    kind: "select",
    parsed: true,
    heading: "Auth",
    question: "Which auth method?",
    multi: false,
    steps,
    step: 0,
    options: [
      opt("1", "OAuth", { desc: "Sign in with GitHub" }),
      opt("2", "Token"),
      opt("3", "Type something", { kind: "text" }),
      opt("4", "Chat about this", { kind: "chat" }),
    ],
    nav,
    ...over,
  };
}
const single = () => normDialog(body());
const multi = () =>
  normDialog(
    body({
      id: "q2",
      heading: "Tests",
      question: "Which suites?",
      multi: true,
      step: 1,
      steps: [{ ...steps[0], done: true }, steps[1], steps[2]],
      options: [
        opt("1", "Unit", { checked: true }),
        opt("2", "Integration", { checked: false }),
        opt("3", "E2E", { checked: false }),
        opt("4", "Type something", { kind: "text" }),
      ],
    })
  );
const review = () =>
  normDialog(
    body({
      id: "q3",
      heading: "Submit",
      question: "Review your answers",
      step: 2,
      steps: [
        { ...steps[0], done: true },
        { ...steps[1], done: true },
        steps[2],
      ],
      review: [
        { question: "Which auth method?", answer: "OAuth" },
        { question: "Which suites?", answer: "Unit, E2E" },
      ],
      options: [opt("1", "Submit answers", { kind: "yes" }), opt("2", "Cancel", { kind: "no" })],
    })
  );

beforeEach(() => {
  instApi.mockReset();
});

describe("the /dialog body for Claude's question prompt", () => {
  it("keeps kind select, its steps, the step, multi, the review, nav, and each option's desc and box", () => {
    const d = multi();
    expect(d.kind).toBe("select");
    expect(d.multi).toBe(true);
    expect(d.step).toBe(1);
    expect(d.steps?.map((s) => s.label)).toEqual(["Auth", "Tests", "Submit"]);
    expect(d.steps?.[2].submit).toBe(true);
    expect(d.nav).toEqual(nav);
    expect(d.options.map((o) => o.checked)).toEqual([true, false, false, undefined]);
    expect(single().options[0].desc).toBe("Sign in with GitHub");
    expect(review().review).toHaveLength(2);
    // A server that leaves the keys out: Claude Code's own.
    expect(normDialog(body({ nav: undefined })).nav).toEqual(nav);
    // A permission dialog carries none of it.
    expect(normDialog({ id: "p", parsed: true, question: "Allow?", options: [opt("1", "Yes", { kind: "yes" })] }).steps).toBeUndefined();
  });

  it("stages: single, multi, the review", () => {
    expect([selectStage(single()), selectStage(multi()), selectStage(review())]).toEqual(["single", "multi", "review"]);
  });

  it("reads Question in the pop-up's head, whatever its tab is called", () => {
    expect(askKindLabel("dialog", normDialog(body({ heading: "Plan", question: "Which plan?" })))).toBe("Question");
  });
});

describe("what each answer posts", () => {
  it("single-select: the option's digit (it selects and moves on)", () => {
    expect(selectPosts(single(), { type: "pick", key: "2" })).toEqual([{ to: "answer", keys: ["2"] }]);
    expect(selectPosts(single(), { type: "pick", key: "9" })).toEqual([]);
  });

  it("multi-select: the keys of the boxes ticked differently from the server, in order, in one post, then Right", () => {
    const d = multi();
    // Unit was on (now off), E2E was off (now on); Integration unchanged.
    expect(selectPosts(d, { type: "next" }, { "1": false, "2": false, "3": true })).toEqual([
      { to: "answer", keys: ["1", "3"] },
      { to: "answer", keys: ["Right"] },
    ]);
    // Nothing changed: just Right.
    expect(selectPosts(d, { type: "next" }, sel.initialChecked(d))).toEqual([{ to: "answer", keys: ["Right"] }]);
    // Back flushes the ticks first too.
    expect(selectPosts(d, { type: "prev" }, { "1": true, "2": true, "3": false })).toEqual([
      { to: "answer", keys: ["2"] },
      { to: "answer", keys: ["Left"] },
    ]);
  });

  it("the review: Submit answers is its digit; Back is Left; Cancel the question is Escape", () => {
    expect(selectPosts(review(), { type: "pick", key: "1" })).toEqual([{ to: "answer", keys: ["1"] }]);
    expect(selectPosts(review(), { type: "prev" })).toEqual([{ to: "answer", keys: ["Left"] }]);
    expect(selectPosts(single(), { type: "prev" })).toEqual([]); // the first step has no Back
    expect(selectPosts(review(), { type: "cancel" })).toEqual([{ to: "answer", keys: ["Escape"] }]);
  });

  it("Type something: its digit, a beat, then the words with Enter", () => {
    expect(selectPosts(single(), { type: "text", key: "3", text: "  SAML via Okta " })).toEqual([
      { to: "answer", keys: ["3"] },
      { to: "settle" },
      { to: "answer", keys: ["Enter"], text: "SAML via Okta" },
    ]);
    expect(selectPosts(single(), { type: "text", key: "3", text: "  " })).toEqual([]);
  });

  it("always the option's own keys — with the cursor on the text row they are ['Up', '1'], never a bare digit", () => {
    const d = normDialog(body({ cursor: "3", options: [opt("1", "OAuth", { keys: ["Up", "Up", "1"] }), opt("2", "Token", { keys: ["Up", "2"] }), opt("3", "Type something", { kind: "text" })] }));
    expect(d.cursor).toBe("3");
    expect(selectPosts(d, { type: "pick", key: "2" })).toEqual([{ to: "answer", keys: ["Up", "2"] }]);
    const m = normDialog(body({ multi: true, button: "Submit", options: [opt("1", "A", { checked: false, keys: ["Up", "1"] }), opt("2", "B", { checked: false })] }));
    expect(m.button).toBe("Submit");
    expect(selectPosts(m, { type: "next" }, { "1": true, "2": true })[0]).toEqual({ to: "answer", keys: ["Up", "1", "2"] });
  });

  it("Chat about this: its digit, a beat, then the words go to the session as a prompt", () => {
    expect(selectPosts(single(), { type: "chat", key: "4", text: "Which is cheaper?" })).toEqual([
      { to: "answer", keys: ["4"] },
      { to: "settle" },
      { to: "send", text: "Which is cheaper?" },
    ]);
  });
});

function deps(over: Partial<Parameters<typeof runSelect>[2]> = {}) {
  const calls: unknown[][] = [];
  const d = {
    answer: vi.fn(async (keys: string[], id: string | null, text?: string) => {
      calls.push(text ? ["answer", keys, id, text] : ["answer", keys, id]);
    }),
    send: vi.fn(async (text: string) => {
      calls.push(["send", text]);
    }),
    fetch: vi.fn(async () => null as Dialog | null),
    sleep: vi.fn(async (ms: number) => {
      calls.push(["sleep", ms]);
    }),
    ...over,
  };
  return { d, calls };
}

describe("runSelect: in order, every post pinned to the id you saw", () => {
  it("multi: the ticks in one post, then Right — both under the dialog's id (it holds across toggles)", async () => {
    const next = review();
    const { d, calls } = deps({ fetch: vi.fn(async () => next) });
    const out = await runSelect(multi(), selectPosts(multi(), { type: "next" }, { "1": false, "2": false, "3": true }), d);
    expect(out).toEqual({ kind: "done", dialog: next });
    expect(calls).toEqual([
      ["answer", ["1", "3"], "q2"],
      ["sleep", sel.SELECT_KEY_GAP_MS],
      ["answer", ["Right"], "q2"],
      ["sleep", sel.SELECT_REDRAW_MS],
    ]);
  });

  it("Type something: its keys, a beat, then the words + Enter — the SAME id (it holds while typed into)", async () => {
    const { d, calls } = deps();
    await runSelect(single(), selectPosts(single(), { type: "text", key: "3", text: "SAML" }), d);
    expect(calls).toEqual([
      ["answer", ["3"], "q1"],
      ["sleep", sel.SELECT_SETTLE_MS],
      ["sleep", sel.SELECT_KEY_GAP_MS],
      ["answer", ["Enter"], "q1", "SAML"],
      ["sleep", sel.SELECT_REDRAW_MS],
    ]);
  });

  it("a later post refused as changed / just answered (an older server) goes once more unpinned; a first one never", async () => {
    const seen: Array<string | null> = [];
    const { d } = deps({
      answer: vi.fn(async (_k: string[], id: string | null) => {
        seen.push(id);
        if (seen.length === 2) throw new ApiError(409, "just answered", { dialog_answered: true });
      }),
    });
    const out = await runSelect(single(), selectPosts(single(), { type: "text", key: "3", text: "SAML" }), d);
    expect(out.kind).toBe("done");
    expect(seen).toEqual(["q1", "q1", null]);
  });

  it("Chat about this: a /send refused in_dialog (the prompt not gone yet) is tried again", async () => {
    let n = 0;
    const { d, calls } = deps({
      send: vi.fn(async (text: string) => {
        calls.push(["send", text]);
        if (n++ === 0) throw new ApiError(409, "in a dialog", { in_dialog: true });
      }),
    });
    const out = await runSelect(single(), selectPosts(single(), { type: "chat", key: "4", text: "Hm?" }), d);
    expect(out.kind).toBe("done");
    expect(calls.filter((c) => c[0] === "send")).toEqual([
      ["send", "Hm?"],
      ["send", "Hm?"],
    ]);
  });

  it("a 409 dialog_changed on the first post reads the new prompt; nothing else is pressed", async () => {
    const fresh = multi();
    const { d, calls } = deps({
      answer: vi.fn(async () => {
        throw new ApiError(409, "the prompt changed", { dialog_changed: true });
      }),
      fetch: vi.fn(async () => fresh),
    });
    const out = await runSelect(single(), selectPosts(single(), { type: "text", key: "3", text: "x" }), d);
    expect(out).toEqual({ kind: "changed", dialog: fresh });
    expect(calls).toEqual([]);
    expect(d.answer).toHaveBeenCalledTimes(1);
  });

  it("any other failure stops the sequence", async () => {
    const { d } = deps({
      answer: vi.fn(async () => {
        throw new Error("boom");
      }),
    });
    const out = await runSelect(single(), [{ to: "answer", keys: ["1"] }], d);
    expect(out.kind).toBe("failed");
  });

  it("the route: POST /answer {keys, by: user} — dialog_id only when pinned, text only with words", async () => {
    await answerSelect("s", ["1"], "q1");
    expect(instApi).toHaveBeenLastCalledWith("s", "/answer", { json: { keys: ["1"], by: "user", dialog_id: "q1" } });
    await answerSelect("s", ["Enter"], null, "SAML");
    expect(instApi).toHaveBeenLastCalledWith("s", "/answer", { json: { keys: ["Enter"], by: "user", text: "SAML" } });
  });
});

describe("the flow: steps without closing", () => {
  const noSleep = { sleep: async () => {} };

  it("a pick shows the next step in place; the last one gone ends it (answered)", async () => {
    const f = new SelectFlows();
    f.offer("s", single(), 1);
    const m = multi();
    const { d } = deps({ ...noSleep, fetch: vi.fn(async () => m) });
    await f.act("s", { type: "pick", key: "1" }, d, () => 100);
    expect(f.get("s")?.dialog.id).toBe("q2");
    expect(f.get("s")?.checked).toEqual({ "1": true, "2": false, "3": false });
    expect(f.get("s")?.ended).toBe("");
    expect(f.active("s", 100 + SELECT_GRACE_MS - 1)).toBe(true);
    // Submit on the review: the prompt is gone.
    f.offer("s", review(), 200);
    const { d: d2 } = deps({ ...noSleep, fetch: vi.fn(async () => null) });
    await f.act("s", { type: "pick", key: "1" }, d2, () => 300);
    expect(f.get("s")?.ended).toBe("done");
    expect(f.active("s", 300)).toBe(false);
  });

  it("dialog_changed into another step of the same prompt is the normal step — no 'changed' note", async () => {
    const f = new SelectFlows();
    f.offer("s", single(), 1);
    const { d } = deps({
      ...noSleep,
      answer: vi.fn(async () => {
        throw new ApiError(409, "changed", { dialog_changed: true });
      }),
      fetch: vi.fn(async () => multi()),
    });
    await f.act("s", { type: "pick", key: "1" }, d);
    expect(f.get("s")?.dialog.id).toBe("q2");
    expect(f.get("s")?.note).toBe("");
  });

  it("…but another KIND of prompt hands off, saying so", async () => {
    const f = new SelectFlows();
    f.offer("s", single(), 1);
    const perm = normDialog({ id: "p1", parsed: true, question: "Allow git push?", options: [opt("1", "Yes", { kind: "yes" })] });
    const { d } = deps({
      ...noSleep,
      answer: vi.fn(async () => {
        throw new ApiError(409, "changed", { dialog_changed: true });
      }),
      fetch: vi.fn(async () => perm),
    });
    await f.act("s", { type: "pick", key: "1" }, d);
    expect(f.get("s")?.ended).toBe("handoff");
    expect(f.get("s")?.note).toBe("The prompt changed — this is the new one");
  });

  it("ticks stay local until Next; a poll keeps them, a poll begun before a post is dropped", async () => {
    const f = new SelectFlows();
    f.offer("s", multi(), 10);
    f.toggle("s", "3");
    expect(f.get("s")?.checked["3"]).toBe(true);
    f.polled("s", multi(), 20, 30); // the same step, read again
    expect(f.get("s")?.checked["3"]).toBe(true);
    // A poll that started before the last post: older than what the post read.
    f.offer("s", review(), 100);
    f.polled("s", null, 50, 120);
    expect(f.get("s")?.ended).toBe("");
  });

  it("a finished prompt read again (a poll behind) does not restart it; a new prompt does", () => {
    const f = new SelectFlows();
    f.offer("s", review(), 1);
    f.took("s", null, 2);
    f.offer("s", review(), 3);
    expect(f.get("s")?.ended).toBe("done");
    f.offer("s", normDialog(body({ id: "new" })), 4);
    expect(f.get("s")?.ended).toBe("");
    expect(f.get("s")?.dialog.id).toBe("new");
  });

  it("a field being typed in survives the same step redrawn under another id", () => {
    const f = new SelectFlows();
    f.offer("s", single(), 1);
    f.openField("s", "3");
    f.setText("s", "SAML");
    f.polled("s", normDialog(body({ id: "q1-redrawn" })), 5, 6);
    expect(f.get("s")?.field).toBe("3");
    expect(f.get("s")?.text).toBe("SAML");
  });

  it("the pop-up holds the session open while the flow walks it, then lets go", () => {
    const c = code("components/dialogs/AskDialog.tsx");
    expect(c).toContain("const flowing = !!target && selectFlows.active(target);");
    expect(c).toMatch(/queue\.find\(\(q\) => q\.title === target\) \|\| \(flowing \? flowItem\(target\) : null\)/);
    // Ended: remembered as answered (the pop-up moves on), or the strip reads the new kind.
    expect(c).toContain('if (ended === "done") onDone("answered"');
    expect(c).toContain('else if (ended === "handoff") strip.reload(');
  });
});

describe("keys and words", () => {
  const k = (key: string) => ({ key, ctrlKey: false, metaKey: false, altKey: false });

  it("a digit presses (single, review), ticks (multi), or opens a field (Type something / Chat)", () => {
    expect(selectKey(single(), k("2"), false)).toEqual({ type: "pick", key: "2" });
    expect(selectKey(multi(), k("2"), false)).toEqual({ type: "toggle", key: "2" });
    expect(selectKey(multi(), k("4"), false)).toEqual({ type: "field", key: "4" });
    expect(selectKey(single(), k("4"), false)).toEqual({ type: "field", key: "4" });
    expect(selectKey(review(), k("1"), false)).toEqual({ type: "pick", key: "1" });
    expect(selectKey(single(), k("8"), false)).toBeNull();
    expect(selectKey(single(), k("1"), true)).toBeNull(); // typing in a box
    expect(selectKey(single(), { ...k("1"), ctrlKey: true }, false)).toBeNull();
  });

  it("'type something' / 'chat about this' open the field", () => {
    expect(fieldPhrase("Type something.")).toBe("text");
    expect(fieldPhrase("write my own")).toBe("text");
    expect(fieldPhrase("chat about this")).toBe("chat");
    expect(fieldPhrase("two")).toBeNull();
  });

  it("read aloud: the numbers, then 'next' for a multi-select; 'submit' on the review", () => {
    expect(dialogChoices(single())).toBe(
      "one: OAuth. two: Token. three: Type something. four: Chat about this. Say one, two, three, or four."
    );
    expect(dialogChoices(multi())).toMatch(/one: Unit\. .*\. Say the numbers to tick, then next\.$/);
    expect(dialogChoices(review())).toBe("Say submit, or cancel.");
    expect(dialogRetryLine(review())).toBe("I didn't catch that: say submit, or cancel.");
  });

  it("the keys line, by stage", () => {
    expect(SELECT_KEYS).toEqual({
      single: "1–9 picks · Esc later",
      multi: "1–9 ticks · Enter next · Esc later",
      review: "Enter submits · Esc later",
    });
  });
});

describe("the view", () => {
  const view = (d: Dialog, over: Record<string, unknown> = {}) =>
    renderToStaticMarkup(
      createElement(SelectView, {
        flow: { dialog: d, checked: sel.initialChecked(d), field: "", text: "", busy: false, ...over },
        onAct: () => {},
        onToggle: () => {},
        onField: () => {},
        onText: () => {},
        onCloseField: () => {},
        onSendField: () => {},
      })
    );

  it("the tabs like Claude's: done ✓, the shown one lit, Submit last", () => {
    const html = view(multi());
    expect(html).toContain('<ol class="ask-tabs" aria-label="Questions">');
    expect(html).toContain('<li class="ask-tab done">✓ Auth</li>');
    expect(html).toContain('<li class="ask-tab active" aria-current="step">Tests</li>');
    expect(html).toContain('<li class="ask-tab">Submit</li>');
  });

  it("single: option buttons, the description under each, the first one highlighted", () => {
    const html = view(single());
    expect(html).toMatch(/class="ask-opt primary" data-key="1"[^>]*>.*OAuth<\/span><span class="ask-opt-desc">Sign in with GitHub<\/span>/);
    expect(html).not.toContain('role="checkbox"');
    expect(html).not.toContain("ask-select-next");
    expect(html).toContain("Cancel the question");
  });

  it("multi: boxes ticked as the card has them, Next, Back from the second step", () => {
    const html = view(multi(), { checked: { "1": true, "2": false, "3": true } });
    expect(html.match(/role="checkbox" aria-checked="true"/g)).toHaveLength(2);
    expect(html.match(/role="checkbox" aria-checked="false"/g)).toHaveLength(1);
    expect(html).toContain('id="ask-select-next"');
    expect(html).toContain('id="ask-select-back"');
    // "Type something" is no box.
    expect(html).toMatch(/data-key="4" data-kind="text"/);
    expect(html).not.toMatch(/role="checkbox"[^>]*data-key="4"/);
  });

  it("the review reads as a confirmation: the answers, then ONE primary Submit answers beside a plain Cancel — no badges", () => {
    const html = view(review());
    expect(html).toContain("<dt>Which suites?</dt><dd>Unit, E2E</dd>");
    // No option rows (and so no digit badges) on the review.
    expect(html).not.toContain("ask-opt");
    expect(html).not.toContain('class="k"');
    // The house .modal-actions row: Back, then Cancel, then the one primary.
    expect(html).toContain('<div class="ask-sel-nav modal-actions">');
    expect(html).toMatch(/id="ask-select-back".*id="ask-select-cancel"[^>]*>Cancel<\/button><button type="submit" id="ask-select-submit"[^>]*>Submit answers<\/button>/);
    expect(html.match(/type="submit"/g)).toHaveLength(1);
    // Its Cancel IS the question's cancel: no second "Cancel the question".
    expect(html).not.toContain("Cancel the question");
  });

  it("the review's buttons press the prompt's own options (Enter lands on Submit answers)", () => {
    const acts: unknown[] = [];
    const primary = { current: null as HTMLButtonElement | null };
    const el = SelectView({
      flow: { dialog: review(), checked: {}, field: "", text: "", busy: false },
      primaryRef: primary,
      onAct: (a) => acts.push(a),
      onToggle: () => {},
      onField: () => {},
      onText: () => {},
      onCloseField: () => {},
      onSendField: () => {},
    });
    const buttons: Array<{ props: Record<string, unknown> }> = [];
    const walk = (n: unknown) => {
      if (!n || typeof n !== "object") return;
      if (Array.isArray(n)) return n.forEach(walk);
      const e = n as { type?: unknown; props?: Record<string, unknown> };
      if (e.type === "button" && e.props) buttons.push(e as { props: Record<string, unknown> });
      walk(e.props?.children);
    };
    walk(el);
    const byId = (id: string) => buttons.find((b) => b.props.id === id)!;
    (byId("ask-select-submit").props.onClick as () => void)();
    (byId("ask-select-cancel").props.onClick as () => void)();
    expect(acts).toEqual([
      { type: "pick", key: "1" },
      { type: "pick", key: "2" },
    ]);
    expect(byId("ask-select-submit").props.ref).toBe(primary);
  });

  it("multi: the nav's primary is labelled from the prompt's button — Next ›, or Submit on the last question", () => {
    expect(navLabel("Next")).toBe("Next ›");
    expect(navLabel(null)).toBe("Next ›");
    expect(navLabel("Submit")).toBe("Submit");
    const next = view(multi());
    expect(next).toMatch(/<button type="submit" id="ask-select-next"[^>]*>Next ›<\/button>/);
    const last = view({ ...multi(), button: "Submit" });
    expect(last).toMatch(/<button type="submit" id="ask-select-next"[^>]*>Submit<\/button>/);
    // Back on the left, the primary last.
    expect(last.indexOf("ask-select-back")).toBeLessThan(last.indexOf("ask-select-next"));
  });

  it("an open field: a text box with its words and a send", () => {
    const html = view(single(), { field: "3", text: "SAML" });
    expect(html).toContain('id="ask-select-field"');
    expect(html).toContain(">SAML</textarea>");
    expect(html).toContain('id="ask-select-send"');
    const chat = view(single(), { field: "4", text: "" });
    expect(chat).toContain("it goes to the session");
  });
});

describe("hands-free: one voice at a time", () => {
  it("the mic opens only once the queue has drained; the focused session's lines are bare", async () => {
    const events: string[] = [];
    let drain: () => void = () => {};
    const x = new HandsFree({
      replies: () => true,
      focused: () => "t1",
      name: () => "Login fix",
      voiceName: (t) => (t === "t1" ? "" : "Login fix"),
      busy: () => false,
      fetchDialog: async () => null,
      approveDialogs: async () => true,
      speak: async (line) => {
        events.push("say " + line);
      },
      drained: () =>
        new Promise<void>((r) => {
          events.push("wait");
          drain = r;
        }),
      caption: () => {},
      listen: async () => {
        events.push("listen");
        return "acted";
      },
    });
    const run = x.ask("t1", { question: "Push it?" });
    await new Promise((r) => setTimeout(r, 0));
    expect(events).toEqual(["say Push it?", "wait"]);
    drain();
    await run;
    expect(events).toEqual(["say Push it?", "wait", "listen"]);
  });
});

describe("the wiring around it", () => {
  it("the rail / bell strip sends a multi-select or a review to the pop-up instead of pressing a box", () => {
    const c = code("components/AnswerStrip.tsx");
    expect(c).toContain('if (sd && isSelect(sd) && (sd.multi || selectStage(sd) === "review")) {');
    expect(c).toMatch(/asking="select" onReply=\{onReply \?\? \(\(\) => openAsk\(title\)\)\}/);
  });

  it("Read everything: every session's turn with words, once, worded when its turn comes; typed sends get say only then", () => {
    const mic = code("components/TopBarMic.tsx");
    expect(mic).toContain('if (mode === "all" && inline) {');
    expect(mic).toContain("if (!heard.fresh(title, inline, Number(data.ts) || null)) return;");
    // Where it is from ("In Login fix, on …:") only when that session isn't
    // the focused one or hasn't been heard from lately — decided at speak time.
    expect(mic).toContain("const said = queue.say(() => {");
    expect(mic).toContain("const line = replyText({");
    expect(mic).toContain("focused: useUi.getState().focused,");
    // The mic listens for a question only on a prompt it sent ("mine").
    expect(mic).toContain("speak(inline, data.has_question ? inline : \"\", mine);");
    expect(mic).toContain("drained: () => queue.drained(),");
    const dlg = code("components/dialogs/AskDialog.tsx");
    expect(dlg).toContain("sendAnswer(title, text, say || sayOnSend(false))");
  });
});
