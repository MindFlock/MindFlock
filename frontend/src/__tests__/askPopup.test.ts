/** "A session is asking you something" (lib/ask.ts, components/dialogs/
 * AskDialog.tsx, lib/askVoice.ts and the channels around them).
 *
 * What is pinned: who is asking and in what order (one pop-up, "1 of N",
 * oldest first); when the pop-up must NOT open by itself (the setting off,
 * already dealt with, you typing to it in its pane, another dialog up); the
 * keys; a prose answer goes out on /send with Enter, a dialog answer carries
 * the option's own `keys`; the 409s; the OS notification and the toast for a
 * question; a prose question reads Question everywhere; the voice flow (read
 * out only with replies on, approve off is said, dictation into the answer
 * box, "submit" sends with `say`); and the Settings switch. The vitest
 * environment is node: views through react-dom/server, the rest pure. */

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Dialog, Instance, OutboxWaiting } from "../api/types";
import { code, src } from "./sourceText";
import NOTIFY_JS from "../../../backend/web/static/addons/notify.js?raw";
import MAIN_JS from "../../../electron/main.js?raw";

const api = vi.fn();
const instApi = vi.fn();
const toast = vi.fn();
const selectSession = vi.fn();

vi.mock("../api/client", async (orig) => ({
  ...((await orig()) as object),
  api: (...a: unknown[]) => api(...a),
  instApi: (...a: unknown[]) => instApi(...a),
}));
vi.mock("../lib/toast", () => ({ toast: (...a: unknown[]) => toast(...a) }));
vi.mock("../lib/sessionActions", () => ({ selectSession: (...a: unknown[]) => selectSession(...a) }));

const { ApiError } = await import("../api/client");
const ask = await import("../lib/ask");
const { askQueue, askSuppressed, AskMemory, askingOf, askKindLabel, nextAfter, stepAsk, openAsk, TYPED_RECENTLY_MS, EVENT_ASK_GRACE_MS } = ask;
const { askKeyPick, letterOption, sendFailure } = await import("../components/dialogs/AskDialog");
const { desktopNoteFor, desktopNotify, openNotifyTarget, SESSION_TARGET } = await import("../lib/desktopNotify");
const { deriveSessionState } = await import("../lib/sessionState");
const { attentionItems, askedQuestion } = await import("../components/sidebar/ordering");
const { needsAttention } = await import("../components/outbox/outbox");
const { AskedInWordsView, primaryIndex } = await import("../components/AnswerStrip");
const { answerFresh, normDialog } = await import("../lib/flockActions");
const { HandsFree, LiveDictation, APPROVE_OFF_LINE, runVoiceCommand } = await import("../lib/voiceDesktop");
const { useUi } = await import("../state/store");
const { queryClient } = await import("../state/queries");

const NOW = 1_800_000_000_000;

function inst(over: Partial<Instance> & { title: string }): Instance {
  return {
    status: "running",
    activity: "idle",
    activity_since: 100,
    workspace_missing: false,
    stage: "idle",
    diff_stat: null,
    ...over,
  } as Instance;
}

const q = (title: string, ts: number, text = "Which one?") =>
  inst({ title, asking: { kind: "question", text, ts, dialog_id: null, parsed: false, source: null } });
const dlg = (title: string, ts: number, id = "d-" + title) =>
  inst({ title, activity: "clarify", asking: { kind: "dialog", text: "Allow?", ts, dialog_id: id, parsed: true, source: null } });

const DIALOG: Dialog = {
  id: "d1",
  parsed: true,
  question: "Do you want to proceed?",
  command: "git push",
  options: [
    { key: "1", label: "Yes", kind: "yes" },
    { key: "2", label: "Yes, and don't ask again", kind: "always" },
    { key: "3", label: "No, and tell Claude what to do differently (esc)", kind: "no" },
  ],
};

beforeEach(() => {
  api.mockReset();
  instApi.mockReset();
  toast.mockReset();
  selectSession.mockReset();
  useUi.setState({ openDialog: null, dialogTarget: null, railOrder: [], aliases: {} });
});

// --- Who is asking --------------------------------------------------------------- //

describe("askingOf: the one reading of an ask", () => {
  it("a clarify row is a dialog even from a server that sends no `asking`", () => {
    expect(askingOf(inst({ title: "a", activity: "clarify", activity_since: 42 }), "clarify")).toMatchObject({
      kind: "dialog",
      ts: 42,
      dialog_id: null,
    });
  });

  it("a question in words, until the agent works again; never on a paused, missing or verify row", () => {
    const row = q("a", 5);
    expect(askingOf(row, "idle")?.kind).toBe("question");
    expect(askingOf(row, "working")).toBeNull();
    expect(askingOf({ ...row, status: "paused" }, "idle")).toBeNull();
    expect(askingOf({ ...row, workspace_missing: true }, "idle")).toBeNull();
    expect(askingOf({ ...row, hidden_by: "verify" }, "idle")).toBeNull();
  });

  it("malformed `asking` reads as nothing", () => {
    expect(askingOf(inst({ title: "a", asking: { kind: "huh" } as never }), "idle")).toBeNull();
  });
});

describe("askQueue: one pop-up, oldest first", () => {
  const act = (i: Instance) => i.activity;

  it("orders by when the ask began, then by title", () => {
    const rows = [q("c", 30), dlg("b", 10), q("a", 10), inst({ title: "idle" })];
    expect(askQueue(rows, null, null, { activity: act }).map((i) => i.title)).toEqual(["a", "b", "c"]);
  });

  it("an outbox question row and an event fill in for a row a poll behind — not for one that moved on", () => {
    const rows = [inst({ title: "w" }), inst({ title: "busy", activity: "working" })];
    const waiting: OutboxWaiting[] = [
      { title: "w", kind: "question", text: "Ship it?", since: 7 },
      { title: "busy", kind: "question", text: "stale", since: 1 },
      { title: "w2", kind: "approve", since: 2 },
    ];
    const events = [
      { title: "busy", kind: "question" as const, text: "old", ts: 3, at: NOW },
      { title: "w", kind: "question" as const, text: "dup", ts: 9, at: NOW },
    ];
    const out = askQueue(rows, waiting, events, { now: NOW, activity: act });
    expect(out).toEqual([{ title: "w", kind: "question", text: "Ship it?", ts: 7, dialogId: null, key: "q:7" }]);
  });

  it("an event's ask is trusted only for EVENT_ASK_GRACE_MS without a row behind it", () => {
    const rows = [inst({ title: "e" })];
    const ev = [{ title: "e", kind: "question" as const, text: "Go?", ts: 3, at: NOW - EVENT_ASK_GRACE_MS - 1 }];
    expect(askQueue(rows, null, ev, { now: NOW, activity: act })).toEqual([]);
    expect(askQueue(rows, null, [{ ...ev[0], at: NOW }], { now: NOW, activity: act }).map((i) => i.text)).toEqual(["Go?"]);
  });

  it("each ask has its own key: a new ask on the same session is a new key", () => {
    const [a] = askQueue([q("a", 1)], null, null, { activity: act });
    const [b] = askQueue([q("a", 2)], null, null, { activity: act });
    expect(a.key).not.toBe(b.key);
    const [d] = askQueue([dlg("x", 4, "id9")], null, null, { activity: act });
    expect(d.key).toBe("d:id9@4");
  });
});

describe("the stepper", () => {
  const items = askQueue([q("a", 1), q("b", 2), q("c", 3)], null, null, { activity: (i) => i.activity });

  it("steps both ways and wraps", () => {
    expect(stepAsk(items, "a", 1)).toBe("b");
    expect(stepAsk(items, "a", -1)).toBe("c");
    expect(stepAsk(items, "c", 1)).toBe("a");
    expect(stepAsk(items, "zzz", 1)).toBe("a");
    expect(stepAsk([], "a", 1)).toBeNull();
  });

  it("after an answer: the one that followed it, else any other, else close", () => {
    const before = ["a", "b", "c"];
    const rest = items.filter((i) => i.title !== "b");
    expect(nextAfter(rest, "b", before)).toBe("c");
    expect(nextAfter(items.filter((i) => i.title === "a"), "c", before)).toBe("a");
    expect(nextAfter([], "a", before)).toBeNull();
  });
});

// --- When it must not open --------------------------------------------------------- //

describe("askSuppressed: the open / suppress rules", () => {
  const item = askQueue([dlg("s", 1)], null, null, { activity: (i) => i.activity })[0];
  const prose = askQueue([q("s", 1)], null, null, { activity: (i) => i.activity })[0];
  const base = { popupOn: true, memory: new AskMemory(), focused: null, typedAt: 0, otherDialog: false, now: NOW };

  it("opens by default", () => {
    expect(askSuppressed(item, base)).toBe("");
  });

  it("never with the setting off", () => {
    expect(askSuppressed(item, { ...base, popupOn: false })).toBe("off");
  });

  it("a question in plain words never opens it by itself — only a real dialog does", () => {
    expect(askSuppressed(prose, base)).toBe("prose");
    expect(askSuppressed(item, base)).toBe("");
    // A prose ask is no dialog, even when the word "question" is all over it.
    const [word] = askQueue([q("w", 1, "Quick question: which one do you want?")], null, null, { activity: (i) => i.activity });
    expect(askSuppressed(word, base)).toBe("prose");
  });

  it("…and counts in the '1 of N' only once you opened it (Reply, a toast, a notification)", () => {
    const m = new AskMemory();
    const both = askQueue([dlg("d", 1), q("p", 2)], null, null, { activity: (i) => i.activity });
    expect(ask.cardQueue(both, m).map((i) => i.title)).toEqual(["d"]);
    m.invite("p");
    expect(ask.cardQueue(both, m).map((i) => i.title)).toEqual(["d", "p"]);
    // Answered: gone, and the session's NEXT question waits for its own Reply.
    m.handle(both[1], "answered");
    expect(ask.cardQueue(both, m).map((i) => i.title)).toEqual(["d"]);
    const [next] = askQueue([q("p", 9)], null, null, { activity: (i) => i.activity });
    expect(ask.cardQueue([next], m)).toEqual([]);
    // openAsk is the explicit open: it invites.
    openAsk("p");
    expect(ask.cardQueue([next], ask.askMemory).map((i) => i.title)).toEqual(["p"]);
    expect(useUi.getState().dialogTarget).toBe("p");
  });

  it("not for the focused session you typed into in the last 10 s — but yes for another session", () => {
    const typing = { ...base, focused: "s", typedAt: NOW - TYPED_RECENTLY_MS + 500 };
    expect(askSuppressed(item, typing)).toBe("typing");
    expect(askSuppressed(item, { ...typing, typedAt: NOW - TYPED_RECENTLY_MS - 1 })).toBe("");
    expect(askSuppressed(item, { ...typing, focused: "other" })).toBe("");
  });

  it("waits behind another dialog", () => {
    expect(askSuppressed(item, { ...base, otherDialog: true })).toBe("other-dialog");
  });

  it("never again for an ask answered, put off, or opened in its pane", () => {
    for (const how of ["answered", "later", "pane", "typing"] as const) {
      const m = new AskMemory();
      m.handle(item, how);
      expect(askSuppressed(item, { ...base, memory: m })).toBe("handled");
    }
  });

  it("a dialog put off stays put off while its id holds, even re-stamped; a new id asks again", () => {
    const m = new AskMemory();
    const [d1] = askQueue([dlg("x", 1, "idA")], null, null, { activity: (i) => i.activity });
    m.handle(d1, "later");
    const [restamped] = askQueue([dlg("x", 9, "idA")], null, null, { activity: (i) => i.activity });
    expect(m.handled(restamped)).toBe(true);
    const [fresh] = askQueue([dlg("x", 9, "idB")], null, null, { activity: (i) => i.activity });
    expect(m.handled(fresh)).toBe(false);
  });

  it("a Reply / toast / notification forgets what was put off, and opens the pop-up on it", () => {
    ask.askMemory.handle(item, "later");
    openAsk("s");
    expect(ask.askMemory.handled(item)).toBe(false);
    expect(useUi.getState().openDialog).toBe("ask");
    expect(useUi.getState().dialogTarget).toBe("s");
  });

  it("the pop-up is a centred `.modal`, keeps Esc in the capture phase, ignores backdrop presses and waits for quiet keys", () => {
    const c = code("components/dialogs/AskDialog.tsx");
    expect(c).toMatch(/<Modal[\s\S]*?escapeCapture[\s\S]*?closeOnBackdrop=\{false\}/);
    expect(c).not.toContain("top-dialog");
    expect(c).toContain("KEYS_QUIET_MS");
    expect(c).toMatch(/requestIdleCallback/);
    expect(code("App.tsx")).toContain("<AskDialog />");
  });
});

// --- The keys ---------------------------------------------------------------------- //

describe("keys while it is up", () => {
  const k = (key: string, mods: Partial<KeyboardEvent> = {}) => ({ key, ctrlKey: false, metaKey: false, altKey: false, ...mods });

  it("1–9 pick by the option's own key; Y / N / A by kind", () => {
    expect(askKeyPick(DIALOG, k("1"), false)).toBe("1");
    expect(askKeyPick(DIALOG, k("3"), false)).toBe("3");
    expect(askKeyPick(DIALOG, k("4"), false)).toBeNull();
    expect(askKeyPick(DIALOG, k("y"), false)).toBe("1");
    expect(askKeyPick(DIALOG, k("N"), false)).toBe("3");
    expect(askKeyPick(DIALOG, k("a"), false)).toBe("2");
    expect(letterOption(DIALOG.options, "x")).toBeNull();
  });

  it("nothing with a modifier, while typing in a box, or on an unparsed dialog", () => {
    expect(askKeyPick(DIALOG, k("1", { ctrlKey: true }), false)).toBeNull();
    expect(askKeyPick(DIALOG, k("y", { altKey: true }), false)).toBeNull();
    expect(askKeyPick(DIALOG, k("1"), true)).toBeNull();
    expect(askKeyPick({ ...DIALOG, parsed: false }, k("1"), false)).toBeNull();
    expect(askKeyPick(null, k("1"), false)).toBeNull();
  });

  it("Enter presses the highlighted option, which is never 'always'", () => {
    expect(primaryIndex(DIALOG.options)).toBe(0);
    expect(primaryIndex([DIALOG.options[1], DIALOG.options[0]])).toBe(-1);
    const c = code("components/dialogs/AskDialog.tsx");
    expect(c).toMatch(/ref=\{i === prim \? primaryBtn : undefined\}/);
  });

  it("Esc is Later — unless the mic was listening, which it stops first", () => {
    const c = code("components/dialogs/AskDialog.tsx");
    expect(c).toMatch(/onEscape=\{\(\) => askMic\(\)\?\.cancel\(\) === true\}/);
    expect(c).toMatch(/onClose=\{\(\) => laterRef\.current\?\.\(\)\}/);
    // Enter sends the box, Shift+Enter is a newline.
    expect(c).toMatch(/e\.key !== "Enter" \|\| e\.shiftKey/);
  });
});

// --- The server calls -------------------------------------------------------------- //

describe("answering", () => {
  it("a prose answer is typed and sent: /send {text, dialog_safe: true} (never `enter` with text), with say only when asked", async () => {
    instApi.mockResolvedValue({});
    await ask.sendAnswer("s", "the second one");
    expect(instApi).toHaveBeenLastCalledWith("s", "/send", { json: { text: "the second one", dialog_safe: true } });
    await ask.sendAnswer("s", "yes", true);
    expect(instApi).toHaveBeenLastCalledWith("s", "/send", { json: { text: "yes", dialog_safe: true, say: true } });
  });

  it("Later on a question: POST /asking/dismiss {}", async () => {
    instApi.mockResolvedValue({});
    await ask.dismissQuestion("s");
    expect(instApi).toHaveBeenCalledWith("s", "/asking/dismiss", { json: {} });
  });

  it("a refused send says why; in_dialog shows the dialog instead", () => {
    const e = (body: unknown) => new ApiError(409, "conflict", body);
    expect(sendFailure(e({ in_dialog: true }), "bot")).toEqual({ line: "bot is showing a dialog now — answer that first", inDialog: true });
    expect(sendFailure(e({ waking: true }), "bot").line).toMatch(/waking up/);
    expect(sendFailure(e({ paused: true }), "bot").line).toMatch(/paused/);
    expect(sendFailure(new Error("boom"), "bot")).toEqual({ line: "Couldn't send: boom", inDialog: false });
    expect(src("components/dialogs/AskDialog.tsx")).toMatch(/forceActivity\(title, "clarify"\)/);
  });

  it("an option's own `keys` go to /answer; a plain one sends its key", async () => {
    const d = normDialog({
      id: "p1",
      parsed: true,
      question: "Pick",
      options: [
        { key: "1", label: "A", kind: "other", keys: ["Down", "Enter"] },
        { key: "2", label: "B", kind: "other" },
      ],
    });
    expect(d.options[0].keys).toEqual(["Down", "Enter"]);
    instApi.mockResolvedValue({});
    await answerFresh("s", "1", d, NOW, NOW);
    expect(instApi).toHaveBeenLastCalledWith("s", "/answer", { json: { keys: ["Down", "Enter"], dialog_id: "p1", by: "user" } });
    await answerFresh("s", "2", d, NOW, NOW);
    expect(instApi).toHaveBeenLastCalledWith("s", "/answer", { json: { keys: ["2"], dialog_id: "p1", by: "user" } });
  });

  it("a 409 dialog_answered reads as already answered; dialog_changed shows the new prompt", async () => {
    instApi.mockRejectedValueOnce(new ApiError(409, "x", { dialog_answered: true }));
    expect((await answerFresh("s", "1", DIALOG, NOW, NOW)).kind).toBe("already");
    instApi
      .mockRejectedValueOnce(new ApiError(409, "x", { dialog_changed: true }))
      .mockResolvedValueOnce({ id: "d2", parsed: true, question: "Other?", command: "rm -rf x", options: DIALOG.options });
    const out = await answerFresh("s", "1", DIALOG, NOW, NOW);
    expect(out).toMatchObject({ kind: "changed", dialog: { id: "d2" } });
  });

  it("a /dialog body for a prose question never offers options", () => {
    const d = normDialog({ id: "q:5", kind: "question", parsed: true, question: "Which?", options: DIALOG.options, ts: 5 });
    expect(d).toMatchObject({ id: "q:5", kind: "question", parsed: false, options: [] });
  });

  it("the kind label comes from the dialog", () => {
    expect(askKindLabel("question")).toBe("Question");
    expect(askKindLabel("dialog", DIALOG)).toBe("Permission");
    expect(askKindLabel("dialog", { ...DIALOG, command: null, heading: "Ready to code? Here is Claude's plan" })).toBe("Plan approval");
    expect(askKindLabel("dialog", { question: "Pick a colour", command: null, options: [{ key: "1", label: "Red", kind: "other" }] })).toBe(
      "Question"
    );
  });
});

// --- The Question state everywhere -------------------------------------------------- //

describe("a question in words reads Question, like a dialog", () => {
  it("in the one session state — the chip, the toast and the bell", () => {
    const ss = deriveSessionState(q("s", 1), { activity: "idle", now: NOW });
    expect(ss).toMatchObject({ state: "needs_you", reason: "question", label: "Question", inBell: true });
    // A dialog is still Question; truly idle stays Idle.
    expect(deriveSessionState(inst({ title: "d" }), { activity: "clarify" }).label).toBe("Question");
    expect(deriveSessionState(inst({ title: "i" }), { activity: "idle", now: NOW }).label).toBe("Idle");
  });

  it("outranks the 20-minute idle-on-uncommitted-work rule", () => {
    const stale = {
      ...q("s", 1),
      activity_since: NOW / 1000 - 3600,
      diff_stat: { uncommitted: { additions: 3, deletions: 0 } } as never,
    };
    expect(deriveSessionState(stale, { activity: "idle", now: NOW }).reason).toBe("question");
    expect(deriveSessionState({ ...stale, asking: null }, { activity: "idle", now: NOW }).reason).toBe("stuck");
  });

  it("the bell: one answer row with the question, whether from the rows or the outbox", () => {
    const rows = [q("s", 1, "Merge it now?")];
    const attn = attentionItems(rows);
    expect(attn[0]).toMatchObject({ p: 0, title: "s", question: "Merge it now?", snippet: "Merge it now?" });
    expect(askedQuestion(rows[0])).toBe("Merge it now?");
    const merged = needsAttention(attn, [{ title: "s", kind: "question", text: "Merge it now?", since: 1 }]);
    expect(merged).toHaveLength(1);
    // The rows a poll behind: the outbox row stands in, ranked as an answer.
    const only = needsAttention([], [{ title: "t", kind: "question", text: "Which?", since: 1 }, { title: "u", kind: "approve", since: 1 }]);
    expect(only[0]).toMatchObject({ key: "t", rank: 0, attn: { p: 0, question: "Which?" } });
  });

  it("the strip shows the question and Reply — no buttons to press", () => {
    const html = renderToStaticMarkup(createElement(AskedInWordsView, { question: "Which branch?", variant: "bell", onReply() {}, onOpen() {} }));
    expect(html).toContain("Which branch?");
    expect(html).toContain(">Reply<");
    expect(html).toContain('data-asking="question"');
    expect(html).not.toContain('class="k"');
    const bell = code("components/NotificationsBell.tsx");
    expect(bell).toMatch(/question=\{a\.question\}/);
    expect(bell).toMatch(/openAsk\(it\.title\)/);
    expect(code("components/sidebar/SidebarRow.tsx")).toMatch(/askedWords = !answering && !onScreen/);
  });
});

// --- Notifications ------------------------------------------------------------------ //

describe("the OS notification and the toast", () => {
  it("a dialog and a question: '[3] name needs you: Question', the question as the body, the click opens its ask", () => {
    useUi.setState({ railOrder: ["x", "y", "bot"] });
    expect(desktopNoteFor("session.question", { text: "Use Postgres or SQLite?" }, { session: "bot" })).toEqual({
      title: "[3] bot needs you: Question",
      body: "Use Postgres or SQLite?",
      target: SESSION_TARGET + "bot",
    });
    expect(desktopNoteFor("session.activity_changed", { question: "Allow git push?" }, { session: "bot", new: "clarify" })).toMatchObject({
      title: "[3] bot needs you: Question",
      body: "Allow git push?",
    });
    expect(desktopNoteFor("session.activity_changed", {}, { session: "bot", new: "idle" })).toBeNull();
    expect(desktopNoteFor("session.activity_changed", {}, { session: "bot", new: "clarify" })?.body).toMatch(/dialog/);
  });

  it("shown only unfocused in the desktop app, once per session per 30 s", () => {
    const show = vi.fn();
    vi.stubGlobal("window", { mfnotify: { show, onClick: () => () => {} } });
    vi.stubGlobal("document", { hasFocus: () => false });
    try {
      expect(desktopNotify("session.question", { text: "A?" }, { session: "once" })).toBe(true);
      expect(desktopNotify("session.question", { text: "B?" }, { session: "once" })).toBe(false);
      expect(show).toHaveBeenCalledTimes(1);
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("the click selects the session and opens the pop-up on it", () => {
    openNotifyTarget(SESSION_TARGET + "bot");
    expect(selectSession).toHaveBeenCalledWith("bot", { noKeyboard: true });
    expect(useUi.getState()).toMatchObject({ openDialog: "ask", dialogTarget: "bot" });
  });

  it("the shell accepts a session target and flashes; the browser addon leaves these to it", () => {
    expect(MAIN_JS).toMatch(/SESSION_TARGET = \/\^session:/);
    expect(MAIN_JS).toMatch(/const target = notifyTarget\(opts\.target\)/);
    expect(MAIN_JS).toMatch(/flashFrame\(true\)/);
    expect(NOTIFY_JS).toMatch(/DESKTOP_OWNED = new Set\(\[[^\]]*"session\.question"/);
    expect(NOTIFY_JS).toMatch(/env\.event === "session\.activity_changed" && env\.new === "clarify"/);
    expect(NOTIFY_JS).toMatch(/window\.mfnotify && desktopOwned\(env\)/);
  });

  it("a toast for session.question like clarify, behind needs_input; the click opens the ask", () => {
    const c = code("components/EventToasts.tsx");
    expect(c).toMatch(/ev\.subscribe\("session\.question"/);
    expect(c).toMatch(/notifyOnce\(env\.session, "question"/);
    expect(c).toMatch(/openAskFromToast\(env\.session\)/);
    expect(c.match(/desktopNotify\(env\.event, [^)]*env\)/g)?.length).toBeGreaterThanOrEqual(2);
  });
});

// --- The voice flow ------------------------------------------------------------------ //

describe("voice: the pop-up is the one hands-free entry", () => {
  function deps(over: Record<string, unknown> = {}) {
    const said: string[] = [];
    const heard: Array<[string, string]> = [];
    const d = {
      replies: () => true,
      focused: () => "", // the pop-up doesn't need the session focused
      name: (t: string) => (t === "bot" ? "Bot" : t),
      busy: () => false,
      fetchDialog: async () => DIALOG,
      approveDialogs: async () => true,
      speak: async (l: string) => void said.push(l),
      caption: () => {},
      listen: async (title: string, hint: string) => {
        heard.push([title, hint]);
        return "acted" as const;
      },
      ...over,
    };
    return { d, said, heard };
  }

  it("reads a dialog name first and listens for that session's answer, focused or not", async () => {
    const x = deps();
    await new HandsFree(x.d).ask("bot", { dialog: DIALOG });
    expect(x.said[0]).toBe("Bot: Do you want to proceed? It wants to run: git push. Say yes, no, or always.");
    expect(x.heard).toEqual([["bot", "Bot: Say yes, no, or always."]]);
  });

  it("nothing is spoken with replies off", async () => {
    const x = deps({ replies: () => false });
    await new HandsFree(x.d).ask("bot", { dialog: DIALOG });
    await new HandsFree(x.d).ask("bot", { question: "Which?" });
    expect(x.said).toEqual([]);
    expect(x.heard).toEqual([]);
  });

  it("approving by voice off: it says to answer on the screen and does not listen", async () => {
    const x = deps({ approveDialogs: async () => false });
    await new HandsFree(x.d).ask("bot", { dialog: DIALOG });
    expect(x.said[x.said.length - 1]).toBe("Answer it on the screen.");
    expect(x.heard).toEqual([]);
  });

  it("a question already being read out (its reply) is not read twice", async () => {
    const x = deps();
    let done!: () => void;
    const said = new Promise<void>((r) => (done = r));
    const run = new HandsFree(x.d).ask("bot", { question: "Which one?", said });
    expect(x.heard).toEqual([]);
    done();
    await run;
    expect(x.said).toEqual([]);
    expect(x.heard).toEqual([["bot", "Answer Bot — say “over” to send, “over and out” to stop, “disregard” to start over"]]);
  });

  it("a miss retries once; stop() ends it without the retry", async () => {
    const x = deps({ listen: async () => "missed" as const });
    await new HandsFree(x.d).ask("bot", { question: "Which?" });
    expect(x.said).toEqual(["Bot: Which?", "Still there? Bot: Which?"]);
    const y = deps();
    const hf = new HandsFree({
      ...y.d,
      listen: async () => {
        hf.stop("bot");
        return "missed" as const;
      },
    });
    await hf.ask("bot", { question: "Which?" });
    expect(y.said).toEqual(["Bot: Which?"]);
  });

  it("the mic's answer for a dialog goes to /answer with the router's keys — or says approve is off", async () => {
    instApi.mockResolvedValue({});
    const cmdDeps = {
      railOrder: () => [],
      focused: () => "bot",
      select: () => {},
      name: () => "Bot",
      caption: vi.fn(),
      announce: vi.fn(),
      route: async () => ({ kind: "prompt" as const }),
      approveDialogs: async () => true,
      activity: () => "clarify",
      replies: () => false,
      submitted: () => {},
    };
    const reply = { kind: "answer" as const, title: "bot", choice: "yes" as const, dialog_id: "d1", keys: ["1"] };
    await runVoiceCommand(reply, "yes", cmdDeps);
    expect(instApi).toHaveBeenCalledWith("bot", "/answer", { json: { keys: ["1"], dialog_id: "d1", by: "user" } });
    instApi.mockReset();
    await runVoiceCommand(reply, "yes", { ...cmdDeps, approveDialogs: async () => false });
    expect(instApi).not.toHaveBeenCalled();
    expect(cmdDeps.announce).toHaveBeenLastCalledWith(APPROVE_OFF_LINE);
  });

  it("a question: the words go into the answer box, 'disregard' takes them back, 'over' sends with say when replies are on", async () => {
    let box = "";
    const submit = vi.fn(async (say: boolean) => ({ ok: true, line: "Sent to Bot. " + say }));
    const target = {
      kind: "field" as const,
      title: "bot",
      name: "Bot",
      type: (t: string) => {
        box += t;
        return true;
      },
      erase: (t: string) => {
        if (!box.endsWith(t)) return false;
        box = box.slice(0, box.length - t.length);
        return true;
      },
      submit,
    };
    const submitted: Array<[string, boolean]> = [];
    const hooks = {
      caption: () => {},
      halt: () => {},
      route: vi.fn(async () => false),
      activity: () => "idle",
      replies: () => true,
      submitted: (t: string, s: boolean) => void submitted.push([t, s]),
    };
    const d = new LiveDictation(target, hooks);
    await d.heard("the second one");
    expect(box).toBe("the second one ");
    await d.heard("disregard");
    expect(box).toBe("");
    await d.heard("use sqlite over");
    expect(box).toBe("use sqlite ");
    expect(submit).toHaveBeenCalledWith(true);
    expect(submitted).toEqual([["bot", true]]);
    // A text box never goes past the router.
    expect(hooks.route).not.toHaveBeenCalled();
  });

  it("a stop phrase stops and leaves the text; 'submit' is typed like any word", async () => {
    let box = "";
    const target = { kind: "field" as const, title: "bot", name: "Bot", type: (t: string) => ((box += t), true), erase: () => true, submit: vi.fn() };
    const d = new LiveDictation(target, {
      caption: () => {},
      halt: () => {},
      route: async () => false,
      activity: () => "idle",
      replies: () => false,
      submitted: () => {},
    });
    await d.heard("maybe later submit");
    await d.heard("stop recording");
    expect(box).toBe("maybe later submit ");
    expect(d.active).toBe(false);
    expect(target.submit).not.toHaveBeenCalled();
  });

  it("continuous: 'over' sends the field and listening goes on — onto the focused session once the card moved on; 'over and out' sends and stops", async () => {
    let box = "";
    const submit = vi.fn(async () => ({ ok: true, line: "Sent to Bot." }));
    const field = { kind: "field" as const, title: "bot", name: "Bot", type: (t: string) => ((box += t), true), erase: () => true, submit };
    const typed: string[] = [];
    const session = { kind: "session" as const, title: "t1", name: "Login fix", type: (t: string) => typed.push(t) > 0, erase: () => true };
    instApi.mockReset();
    instApi.mockResolvedValue({ ok: true });
    const routed: string[] = [];
    const d = new LiveDictation(
      field,
      {
        caption: () => {},
        halt: () => {},
        route: async (t: string) => (routed.push(t), false),
        activity: () => "idle",
        replies: () => false,
        submitted: () => {},
        retarget: () => ({ target: session }),
      },
      { continuous: true }
    );
    await d.heard("use sqlite over");
    expect(box).toBe("use sqlite ");
    expect(submit).toHaveBeenCalledTimes(1);
    expect(d.active).toBe(true);
    await d.heard("now run the tests over and out");
    expect(typed).toEqual(["now run the tests "]);
    expect(routed).toEqual(["now run the tests"]); // a new message: past the router again
    expect(instApi).toHaveBeenCalledWith("t1", "/send", { json: { enter: true, say: false } });
    expect(d.active).toBe(false);
  });

  it("the mic routes to the pop-up's ask, and leaves read-outs to it while it is on", () => {
    const mic = code("components/TopBarMic.tsx");
    expect(mic).toMatch(/const ask = askVoice\(\);\s*if \(ask && !opts\.title && \(!into \|\| into === ask\.field\(\)\)\)/);
    expect(mic).toMatch(/env\.new === "clarify" && !askPopupOn\(\)/);
    // A reply's question never opens the pop-up: the mic listens itself
    // (for a prompt it sent), and a Reply meanwhile waits for the read-out.
    expect(mic).toContain("asksSaid.set(title, { done: said, at: Date.now() });");
    expect(mic).toContain("if (listen) void said.then(() => hands.current?.question(title, ask));");
    expect(mic).not.toMatch(/askPopupOn\(\)\) asksSaid/);
    expect(mic).toMatch(/registerAskMic\(/);
    const dlgSrc = code("components/dialogs/AskDialog.tsx");
    expect(dlgSrc).toMatch(/new CustomEvent\(TALK_EVENT, \{ detail: \{ phase \} \}\)/);
    expect(dlgSrc).toContain('"Hotkey: " + key');
  });
});

// --- Settings ------------------------------------------------------------------------ //

describe("Settings → Notifications", () => {
  it("the switch reads and writes notifications.ask_popup through the notification view", async () => {
    api.mockResolvedValueOnce({ ask_popup: false });
    expect(await ask.saveAskPopup(false)).toBe(false);
    expect(api).toHaveBeenCalledWith("/api/notify/ntfy", { json: { ask_popup: false } });
    expect(ask.askPopupOn()).toBe(false);
    queryClient.setQueryData(ask.NOTIFY_VIEW_KEY, {});
    expect(ask.askPopupOn()).toBe(true); // default on: an older server sends nothing
  });

  it("the row, its words and the needs-input note are on the screen, and Search finds it", () => {
    const c = code("components/settings/screens/Notifications.tsx");
    expect(c).toContain('id="notif-ask-popup"');
    expect(c).toMatch(/<AskPopupRow \/>/);
    expect(c).toMatch(/rule\.id === "needs_input" &&/);
    const copy = src("components/settings/screens/notifyCopy.ts");
    expect(copy).toContain("Pop up a session's permission and choice prompts in the middle of the screen");
    expect(copy).toMatch(/question the agent asks in plain words/);
    const search = src("components/palette/settingsSearch.ts");
    expect(search).toMatch(/"question"[\s\S]*"pop up"[\s\S]*"ask"[\s\S]*anchor: "#notif-ask-row"/);
  });
});
