/** The one-click answer strip (components/AnswerStrip.tsx) and the human's
 * side of the MindFlock MCP routes it and the rail call (lib/flockActions.ts).
 *
 * What is pinned: the buttons are the dialog's OWN options with "Always"
 * never primary; every answer carries the dialog id and `by: "user"`; a
 * changed prompt is refetched rather than answered; "answered" latches until
 * the activity moves on; an unparsed dialog offers nothing to press; and a
 * paste action renders server-side and types with `submit: false`, never
 * sending. The vitest environment is node, so the view is checked through
 * react-dom/server and the container's logic through its reducer. */

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Dialog } from "../api/types";
import type { AnswerVariant, StripState } from "../components/AnswerStrip";

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
const pastePlaybook = vi.fn();
vi.mock("../lib/playbooks", () => ({ pastePlaybook: (...a: unknown[]) => pastePlaybook(...a) }));

const { ApiError } = await import("../api/client");
const {
  ANSWERED_HOLD_MS,
  ANSWER_RECHECK_MS,
  AnswerStripView,
  STRIP_INITIAL,
  answerKey,
  optionLabel,
  primaryIndex,
  questionText,
  stripReducer,
} = await import("../components/AnswerStrip");
const {
  DIALOG_FRESH_MS,
  answerDialog,
  answerFresh,
  fetchDialog,
  isDialogAnswered,
  isDialogChanged,
  normDialog,
  openThread,
  pasteWrapup,
  sameDialogShape,
} = await import("../lib/flockActions");
const { useUi } = await import("../state/store");

const DIALOG: Dialog = {
  id: "d1",
  parsed: true,
  question: "Do you want to proceed?",
  command: "uv add redis",
  options: [
    { key: "1", label: "Yes", kind: "yes" },
    { key: "2", label: "Yes, and don't ask again for uv add commands", kind: "always" },
    { key: "3", label: "No, and tell Claude what to do differently (esc)", kind: "no" },
  ],
};

beforeEach(() => {
  api.mockReset();
  instApi.mockReset();
  toast.mockReset();
  pastePlaybook.mockReset();
  selectSession.mockReset();
});

describe("normDialog", () => {
  it("keeps a parsed dialog's pressable options", () => {
    const d = normDialog({ ...DIALOG, options: [...DIALOG.options, { key: "", label: "x" }, { key: "10" }] });
    expect(d.options.map((o) => o.key)).toEqual(["1", "2", "3"]);
    expect(d.parsed).toBe(true);
  });

  it("never offers options for an unparsed dialog, whatever the server sent", () => {
    const d = normDialog({ id: "x", parsed: false, question: "Continue?", options: DIALOG.options });
    expect(d).toEqual({ id: "x", parsed: false, question: "Continue?", command: null, options: [] });
    // Parsed but with nothing pressable reads as unparsed.
    expect(normDialog({ id: "y", parsed: true, question: "?", options: [] }).parsed).toBe(false);
  });

  it("survives junk", () => {
    expect(normDialog(null)).toEqual({ id: "", parsed: false, question: "", command: null, options: [] });
    expect(normDialog({ options: [{ key: "1" }], parsed: true }).options[0]).toEqual({
      key: "1",
      label: "1",
      kind: "other",
    });
  });
});

describe("the routes", () => {
  it("fetches /dialog quietly: no longer on a prompt is simply no dialog", async () => {
    instApi.mockResolvedValueOnce(DIALOG);
    expect((await fetchDialog("api-search"))!.id).toBe("d1");
    expect(instApi).toHaveBeenCalledWith("api-search", "/dialog?quiet=1", { signal: undefined });
    // ?quiet=1: "not waiting" is a 204 with no body — never a logged 409…
    instApi.mockResolvedValueOnce(null);
    expect(await fetchDialog("api-search")).toBeNull();
    // …and an older server's 409 still reads as no dialog.
    instApi.mockRejectedValueOnce(new ApiError(409, "not waiting", { error: "not waiting" }));
    expect(await fetchDialog("api-search")).toBeNull();
    instApi.mockRejectedValueOnce(new ApiError(500, "boom", null));
    await expect(fetchDialog("api-search")).rejects.toThrow("boom");
  });

  it("answers as the user, pinned to the dialog it showed", async () => {
    instApi.mockResolvedValueOnce({ ok: true });
    await answerDialog("api-search", "2", "d1");
    expect(instApi).toHaveBeenCalledWith("api-search", "/answer", {
      json: { keys: ["2"], dialog_id: "d1", by: "user" },
    });
  });

  it("recognises 'the prompt changed' and nothing else", () => {
    expect(isDialogChanged(new ApiError(409, "the prompt changed", { dialog_changed: true }))).toBe(true);
    expect(isDialogChanged(new ApiError(409, "not waiting", { error: "x" }))).toBe(false);
    expect(isDialogChanged(new ApiError(500, "x", { dialog_changed: true }))).toBe(false);
    expect(isDialogChanged(new Error("x"))).toBe(false);
  });
});

describe("pasteWrapup (the rail's wrap up chip)", () => {
  it("pastes the Wrap up prompt once through the shared paste path", async () => {
    let done!: (v: boolean) => void;
    pastePlaybook.mockReturnValueOnce(new Promise<boolean>((r) => (done = r)));
    const a = pasteWrapup("api");
    // A double-click while the first paste is in flight types nothing twice.
    expect(await pasteWrapup("api")).toBe(false);
    done(true);
    expect(await a).toBe(true);
    expect(pastePlaybook).toHaveBeenCalledTimes(1);
    expect(pastePlaybook).toHaveBeenCalledWith("api", { id: "wrapup", label: "Wrap up workers" });
  });

  it("passes a failed paste through and frees the chip for a retry", async () => {
    pastePlaybook.mockResolvedValueOnce(false);
    expect(await pasteWrapup("api")).toBe(false);
    pastePlaybook.mockResolvedValueOnce(true);
    expect(await pasteWrapup("api")).toBe(true);
  });
});

describe("openThread", () => {
  it("opens the real Thread tab through the store's threadOpen", () => {
    const real = useUi.getState().threadOpen;
    const threadOpen = vi.fn();
    useUi.setState({ threadOpen } as never);
    try {
      openThread("api", "api-search");
      expect(threadOpen).toHaveBeenCalledWith("api", { composeTo: "api-search" });
      openThread("api");
      expect(threadOpen).toHaveBeenLastCalledWith("api", undefined);
      // No more "select the pane instead" fallback: the tab exists.
      expect(selectSession).not.toHaveBeenCalled();
    } finally {
      useUi.setState({ threadOpen: real } as never);
    }
  });
});

describe("wording", () => {
  it("never makes 'Always' the primary button", () => {
    expect(primaryIndex(DIALOG.options)).toBe(0);
    expect(primaryIndex([DIALOG.options[1], DIALOG.options[0]])).toBe(-1);
    expect(primaryIndex([])).toBe(-1);
  });

  it("labels by kind, with room for 'always' in the Thread", () => {
    const [yes, always, no] = DIALOG.options;
    expect([yes, always, no].map((o) => optionLabel(o, "rail"))).toEqual(["Yes", "Always", "No…"]);
    expect(optionLabel(always, "thread")).toBe("Yes, don't ask again");
    expect(optionLabel({ key: "4", label: "Show the diff first (esc)", kind: "other" }, "rail")).toBe("Show the…");
    expect(optionLabel({ key: "4", label: "Show the diff first (esc)", kind: "other" }, "thread")).toBe(
      "Show the diff first"
    );
  });

  it("drops the 'Do you want to' preamble on the rail only", () => {
    expect(questionText(DIALOG, "rail")).toBe("proceed?");
    expect(questionText(DIALOG, "bell")).toBe("Do you want to proceed?");
    expect(questionText(DIALOG, "thread")).toBe("Do you want to proceed?");
  });
});

describe("stripReducer", () => {
  const ready = stripReducer(stripReducer(STRIP_INITIAL, { type: "loading" }), { type: "loaded", dialog: DIALOG });

  it("loads, sends, then latches 'answered'", () => {
    expect(ready.phase).toBe("ready");
    const sending = stripReducer(ready, { type: "sending", key: "1" });
    expect(sending).toMatchObject({ phase: "sending", picked: "1" });
    // A second press while one is in flight is ignored.
    expect(stripReducer(sending, { type: "sending", key: "3" })).toBe(sending);
    const answered = stripReducer(sending, { type: "answered" });
    expect(answered.phase).toBe("answered");
    // Rechecks that still see the SAME prompt (or none yet) change nothing…
    expect(stripReducer(answered, { type: "loaded", dialog: DIALOG })).toBe(answered);
    expect(stripReducer(answered, { type: "loaded", dialog: null })).toBe(answered);
    // …a NEW prompt is shown fresh and pressable.
    const next = stripReducer(answered, { type: "loaded", dialog: { ...DIALOG, id: "d2" } });
    expect(next).toMatchObject({ phase: "ready", picked: "" });
    // An activity change resets everything.
    expect(stripReducer(answered, { type: "reset" })).toBe(STRIP_INITIAL);
  });

  it("the SAME prompt asked again is answerable once the server's hold has passed", () => {
    // An agent re-running the same command gets an identical prompt (same
    // id). The activity never leaves clarify between polls, so without this
    // the strip would sit on "answered" while the worker waits.
    const sending = stripReducer(ready, { type: "sending", key: "1" });
    const answered = stripReducer(sending, { type: "answered", now: 10_000 });
    expect(answered.answeredAt).toBe(10_000);
    // Inside the hold: the keys just went out — nothing changes.
    expect(stripReducer(answered, { type: "loaded", dialog: DIALOG, now: 10_000 + ANSWER_RECHECK_MS })).toBe(answered);
    // Past it: the buttons are back, saying why.
    const again = stripReducer(answered, { type: "loaded", dialog: DIALOG, now: 10_000 + ANSWERED_HOLD_MS });
    expect(again).toMatchObject({ phase: "ready", picked: "", note: "It's asking again", answeredAt: 0 });
    expect(again.dialog).toBe(DIALOG);
    // The hold matches the server's (agent_io.ANSWERED_HOLD_S = 4.0).
    expect(ANSWERED_HOLD_MS).toBe(4000);
  });

  it("a second strip's 409 dialog_answered reads as answered, not as a failure", () => {
    expect(isDialogAnswered({ status: 409, body: { error: "x", dialog_answered: true } })).toBe(true);
    expect(isDialogAnswered({ status: 409, body: { dialog_changed: true } })).toBe(false);
    expect(isDialogChanged({ status: 409, body: { dialog_answered: true } })).toBe(false);
  });

  it("a failed answer re-arms the buttons with a note", () => {
    const failed = stripReducer(stripReducer(ready, { type: "sending", key: "1" }), {
      type: "failed",
      note: "Couldn't answer: boom",
    });
    expect(failed).toMatchObject({ phase: "ready", picked: "", note: "Couldn't answer: boom" });
  });

  it("can't send from a strip that has nothing loaded", () => {
    expect(stripReducer(STRIP_INITIAL, { type: "sending", key: "1" })).toBe(STRIP_INITIAL);
  });
});

describe("AnswerStripView", () => {
  const render = (
    state: StripState = { ...STRIP_INITIAL, dialog: DIALOG, phase: "ready" },
    variant: AnswerVariant = "rail",
    onOpen?: () => void
  ) => renderToStaticMarkup(createElement(AnswerStripView, { state, variant, onPick() {}, onOpen }));

  it("shows the question with the command, the dialog's own buttons and ↗", () => {
    const html = render(undefined, "rail", () => {});
    expect(html).toContain('class="flock-answer fa-rail"');
    expect(html).toContain("<code>uv add redis</code> — proceed?");
    expect(html.match(/class="fa-opt[^"]*"/g)).toEqual(['class="fa-opt primary"', 'class="fa-opt"', 'class="fa-opt"']);
    expect(html).toContain('data-key="3" data-kind="no"');
    expect(html).toContain("↗");
  });

  it("leaves an 'always'-first dialog with no primary at all", () => {
    const d = { ...DIALOG, options: [DIALOG.options[1], DIALOG.options[0]] };
    const html = render({ ...STRIP_INITIAL, dialog: d, phase: "ready" });
    expect(html).not.toContain("primary");
  });

  it("offers only ↗ for an unparsed dialog", () => {
    const d = normDialog({ id: "u", parsed: false, question: "Trust this folder?" });
    const html = render({ ...STRIP_INITIAL, dialog: d, phase: "ready" }, "rail", () => {});
    expect(html).toContain("is-unparsed");
    expect(html).toContain("Trust this folder?");
    expect(html).not.toContain("fa-opt");
    expect(html).toContain("fa-open");
  });

  it("reads 'answered' with the pressed option only, disabled", () => {
    const html = render({ dialog: DIALOG, phase: "answered", picked: "1", note: "", answeredAt: 1, loadedAt: 1 });
    expect(html).toContain("answered");
    expect(html.match(/data-key=/g)).toHaveLength(1);
    expect(html).toMatch(/<button[^>]*disabled=""[^>]*data-key="1"|<button[^>]*data-key="1"[^>]*disabled=""/);
  });

  it("renders nothing until a dialog is loaded", () => {
    expect(render({ ...STRIP_INITIAL })).toBe("");
  });

  it("gives the Thread the full wording and right-aligned extras", () => {
    const html = renderToStaticMarkup(
      createElement(
        AnswerStripView,
        { state: { ...STRIP_INITIAL, dialog: DIALOG, phase: "ready" }, variant: "thread", onPick() {}, onOpen() {} },
        createElement("button", { className: "th-btn" }, "Let api decide")
      )
    );
    expect(html).toContain("Yes, don&#x27;t ask again");
    expect(html).toContain("Do you want to proceed?");
    expect(html).toContain("Let api decide");
    expect(html).toContain("Open ↗");
  });
});

describe("answerKey", () => {
  it("is false when no strip for the session is mounted", () => {
    expect(answerKey("nobody", "1")).toBe(false);
  });
});

// --- One prompt, redrawn (E2E defect C, second live run) ---------------------
/** The stop_session dialog as /dialog served it at 100 and at 164 columns:
 * option 2 cut to each width. */
const STOP_100: Dialog = {
  id: "s100",
  parsed: true,
  question: "Tool use — title: \"w2\", mode: \"delete\". Do you want to proceed?",
  command: "add-hello-and-bye-files-w2 · mindflock — Stop a session",
  options: [
    { key: "1", label: "Yes", kind: "yes" },
    { key: "2", label: "Yes, and don't ask again for mindflock — Stop a session commands in ~/.mindflock/work…", kind: "always" },
    { key: "3", label: "No", kind: "no" },
  ],
};
const STOP_164: Dialog = {
  ...STOP_100,
  id: "s164",
  options: [
    STOP_100.options[0],
    {
      key: "2",
      label: "Yes, and don't ask again for mindflock — Stop a session commands in ~/.mindflock/worktrees/emandel2630/add-hello-and-by…",
      kind: "always",
    },
    STOP_100.options[2],
  ],
};
const STOP_80: Dialog = { ...STOP_100, id: "s80", options: [STOP_100.options[0], { key: "2", label: "No", kind: "no" }] };

/** instApi as the server: /dialog serves `reads` in turn (the last one
 * repeats), /answer answers `answers` in turn. */
function serve(reads: (Dialog | null)[], answers: unknown[]) {
  const sent: string[] = [];
  instApi.mockImplementation(async (_t: string, suffix: string, opts?: { json?: { dialog_id: string } }) => {
    if (suffix.startsWith("/dialog")) return reads.length > 1 ? reads.shift() : reads[0];
    sent.push(opts!.json!.dialog_id);
    const a = answers.shift();
    if (a instanceof Error) throw a;
    return a ?? { ok: true };
  });
  return sent;
}
const changed = () => new ApiError(409, "the prompt changed", { error: "the prompt changed", dialog_changed: true });

describe("sameDialogShape", () => {
  it("is the same prompt whatever width cut its labels", () => {
    expect(sameDialogShape(STOP_100, STOP_164)).toBe(true);
    expect(sameDialogShape(STOP_164, { ...STOP_100, command: "add-hello-and-bye-files-w2 ·\nmindflock — Stop a session" })).toBe(true);
    // A redraw left "oject" behind "No" (the live run's 60-column capture).
    const smeared = { ...STOP_100, options: [...STOP_100.options.slice(0, 2), { key: "3", label: "Nooject", kind: "other" }] };
    expect(sameDialogShape(STOP_100, smeared)).toBe(true);
  });

  it("is another prompt with other keys, labels or command", () => {
    expect(sameDialogShape(STOP_100, STOP_80)).toBe(false); // "2" is No there
    expect(sameDialogShape(STOP_100, { ...STOP_100, command: "other-w3 · mindflock — Stop a session" })).toBe(false);
    const swapped = { ...STOP_100, options: [STOP_100.options[2], STOP_100.options[1], STOP_100.options[0]].map((o, i) => ({ ...o, key: String(i + 1) })) };
    expect(sameDialogShape(STOP_100, swapped)).toBe(false);
    expect(sameDialogShape(STOP_100, normDialog({ id: "u", parsed: false, question: "?" }))).toBe(false);
  });
});

describe("answerFresh", () => {
  const T0 = 100_000;

  it("answers a fresh read as it is, without reading again", async () => {
    const sent = serve([STOP_164], []);
    expect(await answerFresh("w", "1", STOP_100, T0, T0 + DIALOG_FRESH_MS)).toEqual({ kind: "answered", dialog: STOP_100 });
    expect(sent).toEqual(["s100"]);
    expect(instApi).toHaveBeenCalledTimes(1);
  });

  it("reads an old read again first, and answers the same prompt under its new id", async () => {
    const sent = serve([STOP_164], []);
    const out = await answerFresh("w", "1", STOP_100, T0, T0 + DIALOG_FRESH_MS + 1);
    expect(out).toEqual({ kind: "answered", dialog: STOP_164 });
    expect(sent).toEqual(["s164"]);
  });

  it("never answers a different prompt it found on the re-read", async () => {
    const sent = serve([STOP_80], []);
    expect(await answerFresh("w", "2", STOP_100, T0, T0 + 10_000)).toEqual({ kind: "changed", dialog: STOP_80 });
    serve([null], []);
    expect(await answerFresh("w", "2", STOP_100, T0, T0 + 10_000)).toEqual({ kind: "changed", dialog: null });
    expect(sent).toEqual([]);
  });

  it("retries a 409 'the prompt changed' once when the fresh read is the same prompt", async () => {
    const sent = serve([STOP_164], [changed()]);
    expect(await answerFresh("w", "1", STOP_100, T0, T0)).toEqual({ kind: "answered", dialog: STOP_164 });
    expect(sent).toEqual(["s100", "s164"]);
  });

  it("only once — and never into another prompt", async () => {
    let sent = serve([STOP_164], [changed(), changed()]);
    expect(await answerFresh("w", "1", STOP_100, T0, T0)).toEqual({ kind: "changed", dialog: STOP_164 });
    expect(sent).toEqual(["s100", "s164"]);
    sent = serve([STOP_80], [changed()]);
    expect(await answerFresh("w", "2", STOP_100, T0, T0)).toEqual({ kind: "changed", dialog: STOP_80 });
    expect(sent).toEqual(["s100"]);
  });

  it("passes 'just answered' and other failures through", async () => {
    serve([STOP_100], [new ApiError(409, "x", { dialog_answered: true })]);
    expect(await answerFresh("w", "1", STOP_100, T0, T0)).toEqual({ kind: "already" });
    const boom = new ApiError(502, "tmux refused", null);
    serve([STOP_100], [boom]);
    expect(await answerFresh("w", "1", STOP_100, T0, T0)).toEqual({ kind: "failed", error: boom });
  });
});

describe("stripReducer: reads and re-reads", () => {
  const ready = stripReducer(STRIP_INITIAL, { type: "loaded", dialog: STOP_100, now: 5 });

  it("stamps when the dialog was read", () => {
    expect(ready.loadedAt).toBe(5);
    expect(stripReducer(ready, { type: "refreshed", dialog: STOP_164, now: 9 })).toMatchObject({
      dialog: STOP_164,
      phase: "ready",
      loadedAt: 9,
    });
  });

  it("a re-read never disturbs a click in flight or the answered latch", () => {
    const sending = stripReducer(ready, { type: "sending", key: "1" });
    expect(stripReducer(sending, { type: "refreshed", dialog: STOP_164 })).toBe(sending);
    const answered = stripReducer(sending, { type: "answered" });
    expect(stripReducer(answered, { type: "refreshed", dialog: null })).toBe(answered);
    // Gone while the buttons were up: nothing left to press.
    expect(stripReducer(ready, { type: "refreshed", dialog: null })).toBe(STRIP_INITIAL);
  });

  it("latches on the read the key went into", () => {
    const sending = stripReducer(ready, { type: "sending", key: "1" });
    const answered = stripReducer(sending, { type: "answered", dialog: STOP_164, now: 1000 });
    expect(answered.dialog).toBe(STOP_164);
    // A recheck still seeing that prompt inside the hold changes nothing.
    expect(stripReducer(answered, { type: "loaded", dialog: STOP_164, now: 2000 })).toBe(answered);
  });
});
