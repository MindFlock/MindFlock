/** "A session is asking you something" — the pop-up in the middle of the
 * screen (lib/ask.ts holds the rules; this is the surface).
 *
 * It opens BY ITSELF when a session shows a dialog — a permission or choice
 * prompt, Claude's question prompt included (activity clarify) — while
 * Settings → Notifications' "Pop up a session's permission and choice prompts" is on, and never
 * for an ask you already dealt with, one you are typing to in its pane, or
 * on top of another dialog (`askSuppressed`). A reply that ended with a
 * question in plain words never opens it by itself: its Reply does. It
 * waits for the keyboard to go quiet and an idle frame, so a word being
 * typed elsewhere is not cut in half. Several asking: ONE pop-up, "1 of N",
 * oldest first (`cardQueue`: the dialogs, and a question you opened).
 *
 * What it shows, per ask:
 *  - a parsed dialog: its question, its command and its own options — the
 *    AnswerStrip's machinery (`useAnswerStrip`: the fresh-read rule, the 409
 *    re-read, the answered latch), 1–9 and Y / N / A pick, Enter presses the
 *    highlighted option ("Always" is never the highlighted one);
 *  - Claude's question prompt (kind "select", lib/askSelect): its tabs, the
 *    question, then the options with their descriptions — one press picks
 *    and moves on; a multi-select ticks boxes and Next sends them; "Type
 *    something" / "Chat about this" open a field; the review lists the
 *    answers with Submit. Each step is read again and shown in place: the
 *    pop-up stays open on the session while the dialog id changes step by
 *    step (`selectFlows`);
 *  - an unparsed dialog: its question and "Open the pane ↗" — there is
 *    nothing it can safely press;
 *  - a question in plain words: an answer box (Enter sends with /send
 *    {text, dialog_safe: true}; Shift+Enter is a new line) and "Open the pane ↗".
 * "Later" (Esc) puts it off: a question is dismissed on the server, a dialog
 * snoozed here until its id changes. After an answer it steps to the next
 * one still asking, or closes.
 *
 * The mic (lib/askVoice): its button — and the Talk hotkey, and the top-bar
 * mic — listen for THIS session's answer while the pop-up is up; with "Read
 * replies aloud" on, the pop-up reads the ask out and listens on its own.
 * A hand on the card (`byHand`) stops that: the ask's read-out and its
 * hands-free listen end, and a step that comes up within HAND_QUIET_MS of a
 * hand is not read out. */

import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import type { Dialog, DialogOption } from "../../api/types";
import { ApiError } from "../../api/client";
import { refreshInstances, useInstances, type EventEnvelope } from "../../state/queries";
import { useOutbox } from "../../state/runs";
import { useUi } from "../../state/store";
import { Modal } from "../Modal";
import { MicGlyph, talkHotkey } from "../TopBarMic";
import { optionLabel, primaryIndex, questionText, useAnswerStrip } from "../AnswerStrip";
import {
  askKindLabel,
  askMemory,
  askQueue,
  askSuppressed,
  cardQueue,
  dismissQuestion,
  nextAfter,
  sendAnswer,
  stepAsk,
  TYPED_RECENTLY_MS,
  useAskPopup,
  type AskItem,
  type EventAsk,
} from "../../lib/ask";
import { askMic, askVoice, clearAskVoice, getMicState, setAskVoice, subscribeMicState, type AskVoice } from "../../lib/askVoice";
import { answerHint, dialogChoices, sayOnSend, TALK_EVENT } from "../../lib/voiceDesktop";
import {
  answerSelect,
  fieldPhrase,
  isBox,
  isSelect,
  selectFlows,
  selectKey,
  selectStage,
  SELECT_POLL_MS,
  takesWords,
  type SelectAct,
  type SelectFlow,
} from "../../lib/askSelect";
import { fetchDialog } from "../../lib/flockActions";
import { meterRef } from "../../lib/voiceMic";
import { forceActivity } from "../../lib/sessionState";
import { overlayOpen } from "../../lib/keymap";
import { lastTypedAt } from "../../lib/terminals";
import { selectSession } from "../../lib/sessionActions";
import { slotNumber, windowName } from "../../lib/windowName";
import { errMsg } from "../../lib/format";
import { toast } from "../../lib/toast";
import "./AskDialog.css";

/** The keyboard must have been quiet this long before the pop-up opens by
 * itself (a word half-typed elsewhere is not cut in two). */
export const KEYS_QUIET_MS = 1200;
/** While an ask waits behind another dialog, how often to look again. */
const RECHECK_MS = 1500;

/** The option a letter picks: Y yes, N no, A always (else null). */
export function letterOption(options: DialogOption[], key: string): DialogOption | null {
  const k = key.toLowerCase();
  const kind = k === "y" ? "yes" : k === "n" ? "no" : k === "a" ? "always" : "";
  return (kind && options.find((o) => o.kind === kind)) || null;
}

/** What a key does in the pop-up showing `d` (pure, for the tests): the
 * option key to press, or null. Digits pick by the option's own key; Y/N/A by
 * kind. Never with a modifier, never while a text box has the keyboard. */
export function askKeyPick(d: Dialog | null, e: Pick<KeyboardEvent, "key" | "ctrlKey" | "metaKey" | "altKey">, typing: boolean): string | null {
  if (!d || !d.parsed || typing || e.ctrlKey || e.metaKey || e.altKey) return null;
  if (/^[1-9]$/.test(e.key)) return d.options.some((o) => o.key === e.key) ? e.key : null;
  return letterOption(d.options, e.key)?.key ?? null;
}

/** The caption line for a failed /send of an answer. */
export function sendFailure(err: unknown, name: string): { line: string; inDialog: boolean } {
  const body = (err instanceof ApiError ? err.body : (err as { body?: unknown })?.body) as
    | { in_dialog?: boolean; waking?: boolean; paused?: boolean }
    | null
    | undefined;
  const status = (err as { status?: number })?.status;
  if (status === 409 && body?.in_dialog) return { line: name + " is showing a dialog now — answer that first", inDialog: true };
  if (status === 409 && body?.waking) return { line: name + " is waking up — send it again in a moment", inDialog: false };
  if (status === 409 && body?.paused) return { line: name + " is paused — resume it first", inDialog: false };
  return { line: "Couldn't send: " + errMsg(err), inDialog: false };
}

/** "[3] name" — the rail slot first, like every notification. */
function slotName(title: string): { slot: string; name: string } {
  return { slot: slotNumber(title), name: windowName(title) };
}

/** Run `fn` on the next idle frame (requestIdleCallback where there is one). */
function onIdle(fn: () => void): () => void {
  const w = window as unknown as {
    requestIdleCallback?: (cb: () => void, o?: { timeout: number }) => number;
    cancelIdleCallback?: (id: number) => void;
  };
  if (typeof w.requestIdleCallback === "function") {
    const id = w.requestIdleCallback(fn, { timeout: 600 });
    return () => w.cancelIdleCallback?.(id);
  }
  const id = window.setTimeout(fn, 0);
  return () => clearTimeout(id);
}

/** Read the ask out (with "Read replies aloud" on) once per ask. */
const readOut = new Set<string>();

/** A step that comes up this soon after a hand on the card is not read out:
 * a person clicking through is not narrated at; a voice-driven flow is. */
export const HAND_QUIET_MS = 3000;
/** When each session's card was last acted on by hand (ms). */
const handAt = new Map<string, number>();

/** Read this step out? Not within {@link HAND_QUIET_MS} of a hand on the card. */
export function narrate(lastHand: number | undefined, now: number): boolean {
  return !lastHand || now - lastHand >= HAND_QUIET_MS;
}

/** Keys that are no act on the card: moving focus, a lone modifier, a
 * shortcut (the Talk hotkey), and Escape (it is "Later" — the pop-up moving
 * on stops the read-out anyway). */
export function handKey(e: Pick<KeyboardEvent, "key" | "ctrlKey" | "metaKey" | "altKey">): boolean {
  if (e.ctrlKey || e.metaKey || e.altKey) return false;
  return !["Tab", "Shift", "Control", "Alt", "Meta", "CapsLock", "Escape"].includes(e.key);
}

/** A press on the card that answers it by hand: an option, a box, a nav
 * button, a field — never the card's own mic (that is barge-in: the mic
 * stops the speech itself and listens). */
function handTarget(el: EventTarget | null): boolean {
  const t = el as Element | null;
  if (!t || typeof t.closest !== "function" || t.closest(".ask-voice")) return false;
  return !!t.closest("button, textarea, input, [role=checkbox]");
}

/** No pop-up opens by itself before this (ms): after a "No…" the person is
 * telling that session what to do instead, in its pane. */
let autoOpenPausedUntil = 0;

export function AskDialog() {
  const open = useUi((s) => s.openDialog === "ask");
  const target = useUi((s) => (s.openDialog === "ask" ? s.dialogTarget : null));
  const focused = useUi((s) => s.focused);
  const popupOn = useAskPopup();
  const { data: instances } = useInstances();
  const { data: outbox } = useOutbox();
  // Asks announced by events, before the rows poll shows them.
  const events = useRef<EventAsk[]>([]);
  const [tick, setTick] = useState(0);
  const bump = useCallback(() => setTick((n) => n + 1), []);
  // The last key press anywhere (ms), for KEYS_QUIET_MS.
  const lastKey = useRef(0);

  useEffect(() => {
    const onKey = () => {
      lastKey.current = Date.now();
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, []);

  useEffect(() => {
    let unsubs: Array<() => void> = [];
    let retry: ReturnType<typeof setTimeout> | undefined;
    const attach = () => {
      const ev = window.mindflock?.events;
      if (!ev) {
        retry = setTimeout(attach, 1000); // events.js not loaded yet
        return;
      }
      const replay = (env: EventEnvelope) => typeof ev.isReplay === "function" && ev.isReplay(env);
      unsubs = [
        ev.subscribe("session.question", (env) => {
          if (replay(env) || !env.session) return;
          const d = env.data || {};
          events.current = events.current.filter((e) => e.title !== env.session);
          events.current.push({
            title: env.session,
            kind: "question",
            text: String(d.text || ""),
            ts: Number(d.ts) || Number(env.ts) || 0,
            at: Date.now(),
          });
          bump();
        }),
        ev.subscribe("session.activity_changed", (env) => {
          if (env.new === "working") events.current = events.current.filter((e) => e.title !== env.session);
          bump();
        }),
      ];
    };
    attach();
    return () => {
      clearTimeout(retry);
      unsubs.forEach((u) => u());
    };
  }, [bump]);

  // `tick` re-reads the memory and the events (neither is React state).
  const all = useMemo(
    () => askQueue(instances || [], outbox?.groups?.waiting, events.current),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [instances, outbox, tick]
  );
  // A select flow's step posted or read: re-render (it holds the pop-up open).
  useSyncExternalStore(selectFlows.subscribe, () => selectFlows.get(target || ""), () => null);
  // Not memoized: the memory changes outside React (a Reply forgets, an
  // answer remembers); the effects below key on `queueKey`.
  const queue = cardQueue(all, askMemory);
  const queueKey = queue.map((q) => q.title + "|" + q.key).join("\n");
  const queueRef = useRef(queue);
  queueRef.current = queue;
  useEffect(() => {
    const asking = new Set(all.map((i) => i.title));
    askMemory.prune(asking);
    selectFlows.prune(asking);
  }, [all]);

  // --- opening by itself ------------------------------------------------- //
  useEffect(() => {
    if (open || !popupOn) return;
    let cancelIdle: (() => void) | null = null;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const pick = (): AskItem | null => {
      const now = Date.now();
      const other = overlayOpen();
      if (now < autoOpenPausedUntil) return null;
      let found: AskItem | null = null;
      for (const item of queueRef.current) {
        const why = askSuppressed(item, {
          popupOn,
          memory: askMemory,
          focused: useUi.getState().focused,
          typedAt: lastTypedAt(item.title),
          otherDialog: other,
          now,
        });
        // You are handling it in its pane: that ask never pops up.
        if (why === "typing") askMemory.handle(item, "typing");
        else if (!why && !found) found = item;
      }
      return found;
    };
    const attempt = () => {
      if (!pick()) return;
      const wait = lastKey.current + KEYS_QUIET_MS - Date.now();
      if (wait > 0) {
        timer = setTimeout(attempt, wait);
        return;
      }
      cancelIdle = onIdle(() => {
        const item = pick();
        if (!item || useUi.getState().openDialog) return;
        useUi.getState().openDialogFor("ask", item.title);
      });
    };
    attempt();
    // Behind another dialog (or a popover): look again until it closes.
    const again = queueKey ? setInterval(bump, RECHECK_MS) : undefined;
    return () => {
      clearTimeout(timer);
      clearInterval(again);
      cancelIdle?.();
    };
  }, [open, popupOn, queueKey, focused, bump]);

  // --- open: follow the queue ---------------------------------------------- //
  const shown = useRef<string[]>([]);
  // Between two steps of Claude's question prompt the rows may say it is
  // not asking for a moment: the flow keeps the pop-up on it.
  const flowing = !!target && selectFlows.active(target);
  const current = open && target ? queue.find((q) => q.title === target) || (flowing ? flowItem(target) : null) : null;
  if (current) shown.current = queue.map((q) => q.title);
  const currentKey = current?.key || "";
  useEffect(() => {
    if (!open || !target || currentKey) return;
    // Answered, put off, or answered elsewhere: the next one, or close.
    const next = nextAfter(queueRef.current, target, shown.current);
    if (next) useUi.getState().openDialogFor("ask", next);
    else useUi.getState().closeDialog();
  }, [open, target, currentKey, queueKey]);

  if (!open || !current) return null;

  // A flow between steps (not in the queue this render): no stepper.
  const at = queue.findIndex((q) => q.title === current.title);
  const done = (how: "answered" | "later" | "pane", dialogId?: string | null) => {
    askMemory.handle(current, how, dialogId);
    bump();
  };
  return (
    <Modal
      id="ask-dialog"
      className="ask-modal"
      role="dialog"
      aria-modal="true"
      aria-labelledby="ask-title"
      escapeCapture
      closeOnBackdrop={false}
      onEscape={() => askMic()?.cancel() === true}
      onClose={() => laterRef.current?.()}
    >
      <AskPanel
        key={current.title + "|" + current.key}
        item={current}
        index={Math.max(0, at)}
        count={at < 0 ? 1 : queue.length}
        onStep={(delta) => {
          const t = stepAsk(queue, current.title, delta);
          if (t) useUi.getState().openDialogFor("ask", t);
        }}
        onDone={done}
        laterRef={laterRef}
      />
    </Modal>
  );
}

/** A session walking through Claude's question prompt while its row is a
 * poll behind (see `selectFlows.active`). */
function flowItem(title: string): AskItem {
  const f = selectFlows.get(title);
  return { title, kind: "dialog", text: f?.dialog.question || "", ts: 0, dialogId: f?.dialog.id || null, key: "sel:" + title, parsed: true };
}

/** The panel's Later, for the modal's Escape. */
const laterRef: { current: (() => void) | null } = { current: null };

interface PanelProps {
  item: AskItem;
  index: number;
  count: number;
  onStep(delta: number): void;
  onDone(how: "answered" | "later" | "pane", dialogId?: string | null): void;
  laterRef: { current: (() => void) | null };
}

function AskPanel({ item, index, count, onStep, onDone, laterRef }: PanelProps) {
  const { title } = item;
  const { slot, name } = slotName(title);
  const isDialog = item.kind === "dialog";
  const strip = useAnswerStrip(title, isDialog, {
    activity: item.key,
    onAnswered: (_key, kind) => {
      if (kind === "already") toast("Already answered — " + name);
      onDone("answered", strip.state.dialog?.id);
      if (kind === "no") {
        // "No…": the agent now wants to hear what to do instead — say it in
        // its pane (the keyboard goes there), with no pop-up in the way.
        autoOpenPausedUntil = Date.now() + TYPED_RECENTLY_MS;
        selectSession(title);
        useUi.getState().closeDialog();
      }
    },
  });
  const stripD = isDialog ? strip.state.dialog : null;
  // Claude's question prompt: the flow (lib/askSelect) holds the step shown,
  // across re-mounts; the strip's read starts it or moves it on.
  const flow = useSyncExternalStore(
    selectFlows.subscribe,
    () => selectFlows.get(title),
    () => null
  );
  useEffect(() => {
    if (stripD && isSelect(stripD)) selectFlows.offer(title, stripD, strip.state.loadedAt || Date.now());
  }, [stripD, title, strip.state.loadedAt]);
  const sel = flow && flow.ended !== "handoff" ? flow : null;
  // A hand-off reads the strip again: until then its select read is stale.
  const handingOff = flow?.ended === "handoff" && isSelect(stripD);
  const d = sel ? sel.dialog : handingOff ? null : stripD;
  const ended = flow?.ended || "";
  useEffect(() => {
    if (ended === "done") onDone("answered", selectFlows.get(title)?.dialog.id);
    else if (ended === "handoff") strip.reload(selectFlows.get(title)?.note || undefined);
    // Once per ending: `strip` and `onDone` are new every render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ended, title]);
  // While a step is shown, read it again now and then (a voice answer, the
  // pane, a redraw moved it).
  const polling = !!flow && !flow.ended && !flow.busy;
  useEffect(() => {
    if (!polling) return;
    const ctl = new AbortController();
    const timer = window.setInterval(() => {
      const at = Date.now();
      fetchDialog(title, ctl.signal).then(
        (fresh) => !ctl.signal.aborted && selectFlows.polled(title, fresh, at),
        () => {}
      );
    }, SELECT_POLL_MS);
    return () => {
      ctl.abort();
      clearInterval(timer);
    };
  }, [polling, title]);
  /** A hand on the card: the voice stops reading this ask (and its
   * hands-free listen ends); the next step is read only if no hand came
   * within {@link HAND_QUIET_MS}. */
  const byHand = useCallback(() => {
    handAt.set(title, Date.now());
    askMic()?.quiet(title);
  }, [title]);
  const field = useRef<HTMLTextAreaElement | null>(null);
  const selField = useRef<HTMLTextAreaElement | null>(null);
  const [hasText, setHasText] = useState(false);
  const [sending, setSending] = useState(false);
  const [note, setNote] = useState("");

  const later = useCallback(() => {
    if (item.kind === "question") {
      dismissQuestion(title).catch((err) => toast("Couldn't put it off: " + errMsg(err)));
    }
    onDone("later", d?.id);
  }, [item.kind, title, onDone, d?.id]);
  laterRef.current = later;

  const openPane = () => {
    onDone("pane", d?.id);
    selectSession(title);
    useUi.getState().closeDialog();
  };

  /** Send the answer box (Enter, the Send button, a spoken "over"). */
  const send = useCallback(
    async (say = false): Promise<{ ok: boolean; line: string }> => {
      const text = (field.current?.value || "").trim();
      if (!text) return { ok: false, line: "Nothing to send yet" };
      setSending(true);
      setNote("");
      try {
        const r = await sendAnswer(title, text, say || sayOnSend(false));
        const line = (r?.queued ? "Queued for " : "Sent to ") + name + ".";
        toast(line);
        onDone("answered");
        return { ok: true, line };
      } catch (err) {
        const f = sendFailure(err, name);
        setNote(f.line);
        if (f.inDialog) {
          // A dialog came up since: show THAT (the queue reads it as one).
          forceActivity(title, "clarify");
          void refreshInstances();
        }
        return { ok: false, line: f.line };
      } finally {
        setSending(false);
      }
    },
    [title, name, onDone]
  );

  /** One answer to Claude's question prompt (lib/askSelect's sequences);
   * `say` for the words of "Chat about this" sent by voice. */
  const act = useCallback(
    (a: SelectAct, say?: boolean) =>
      selectFlows.act(title, a, {
        answer: (keys, id, text) => answerSelect(title, keys, id, text),
        send: (text) => sendAnswer(title, text, say ?? sayOnSend(false)),
        fetch: () => fetchDialog(title),
        sleep: (ms) => new Promise((r) => setTimeout(r, ms)),
      }),
    [title]
  );
  /** The open field's words go: "Type something" or "Chat about this". */
  const sendField = useCallback(
    async (say?: boolean): Promise<{ ok: boolean; line: string }> => {
      const f = selectFlows.get(title);
      const opt = f?.dialog.options.find((o) => o.key === f.field);
      const text = (f?.text || "").trim();
      if (!f || !opt || !text) return { ok: false, line: "Nothing to send yet" };
      await act({ type: opt.kind === "chat" ? "chat" : "text", key: opt.key, text }, say);
      const after = selectFlows.get(title);
      if (after?.note && !after.ended) return { ok: false, line: after.note };
      return { ok: true, line: (opt.kind === "chat" ? "Sent to " : "Answered ") + name + "." };
    },
    [title, name, act]
  );

  // The mic answers THIS session while the panel is up.
  const hint = d?.parsed ? name + ": " + dialogChoices(d) : isDialog ? "Answer " + name + " on the screen" : answerHint(name);
  const sendRef = useRef(send);
  sendRef.current = sel?.field ? sendField : send;
  const hintRef = useRef(hint);
  hintRef.current = hint;
  useEffect(() => {
    const voice: AskVoice = {
      title,
      kind: isDialog ? "dialog" : "question",
      hint: () => hintRef.current,
      field: () => field.current || selField.current,
      submit: (say) => sendRef.current(say),
      // "Type something" / "Chat about this" said: open that field.
      phrase: (text) => {
        const want = fieldPhrase(text);
        const f = selectFlows.get(title);
        const opt = want && f && !f.ended ? f.dialog.options.find((o) => o.kind === want) : undefined;
        if (!opt) return false;
        selectFlows.openField(title, opt.key);
        return true;
      },
    };
    setAskVoice(voice);
    return () => {
      clearAskVoice(voice);
      // Stop listening for it only if the pop-up really moved on — a remount
      // (StrictMode's, or a re-key) registers the same session again at once.
      setTimeout(() => {
        if (askVoice()?.title !== title) askMic()?.stop(title);
      }, 0);
    };
  }, [title, isDialog]);

  // Read it out once (only with "Read replies aloud" on — the mic decides);
  // each step of Claude's question prompt once.
  const dialogReady = !!d && (sel ? !sel.busy : strip.state.phase === "ready");
  const readKey = sel && d ? "sel:" + title + "|" + (d.step ?? 0) + "|" + d.question : item.key;
  useEffect(() => {
    if (readOut.has(readKey)) return;
    if (!narrate(handAt.get(title), Date.now())) {
      // Clicked through by hand: this step is not read out (ever).
      if (item.kind === "question" || dialogReady) readOut.add(readKey);
      return;
    }
    if (item.kind === "dialog") {
      if (!dialogReady || !d) return;
      readOut.add(readKey);
      askMic()?.handsFree(title, { dialog: d });
    } else {
      readOut.add(item.key);
      askMic()?.handsFree(title, { question: item.text });
    }
    // d is read through dialogReady's render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [readKey, item.kind, dialogReady, title]);

  // Keys: 1–9 and Y / N / A pick an option.
  const pickRef = useRef(strip.pick);
  pickRef.current = strip.pick;
  useEffect(() => {
    if (!d?.parsed) return;
    const onKey = (e: KeyboardEvent) => {
      const el = document.activeElement;
      const typing = !!el && (el.tagName === "TEXTAREA" || el.tagName === "INPUT");
      if (isSelect(d)) {
        // Claude's question prompt: a digit presses, ticks, or opens a field.
        const k = selectKey(d, e, typing);
        if (!k) return;
        e.preventDefault();
        e.stopPropagation();
        byHand();
        if (k.type === "toggle") selectFlows.toggle(title, k.key);
        else if (k.type === "field") selectFlows.openField(title, k.key);
        else void act({ type: "pick", key: k.key });
        return;
      }
      const key = askKeyPick(d, e, typing);
      if (!key) return;
      e.preventDefault();
      e.stopPropagation();
      byHand();
      pickRef.current(key);
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [d, title, act, byHand]);

  // The keyboard lands on the highlighted option (never "Always"), or the
  // answer box.
  const primaryBtn = useRef<HTMLButtonElement | null>(null);
  useEffect(() => {
    if (d?.parsed) primaryBtn.current?.focus({ preventScroll: true });
  }, [d?.id, d?.parsed]);
  useEffect(() => {
    if (!isDialog) setTimeout(() => field.current?.focus({ preventScroll: true }), 0);
  }, [isDialog]);
  // A select option's field opened (a click, its digit, the mic): type there.
  const openField = sel?.field || "";
  useEffect(() => {
    if (openField) setTimeout(() => selField.current?.focus({ preventScroll: true }), 0);
  }, [openField]);

  const kind = askKindLabel(item.kind, d);
  // Before the first read lands (the fetch starts after the first paint).
  const loading = isDialog && !d && (strip.state.phase !== "ready" || handingOff);
  const question = isDialog
    ? (d ? questionText(d, "thread") : "") || item.text
    : item.text || "It asked you a question in its reply";
  const busy = strip.state.phase === "sending" || strip.state.phase === "answered";
  const prim = d ? primaryIndex(d.options) : -1;

  return (
    <div
      className="ask-card"
      data-ask={item.kind}
      data-title={title}
      onPointerDownCapture={(e) => {
        if (handTarget(e.target)) byHand();
      }}
      onKeyDownCapture={(e) => {
        if (handKey(e) && !(e.target as Element | null)?.closest?.(".ask-voice")) byHand();
      }}
    >
      <div className="ask-head">
        {slot && (
          <span className="ask-slot" title={"Row " + slot + " on the rail"}>
            {slot}
          </span>
        )}
        <h2 id="ask-title" title={title}>
          {name}
        </h2>
        <span className="ask-kind" data-kind={kind.toLowerCase().replace(/\s+/g, "-")}>
          {kind}
        </span>
        {count > 1 && (
          <span className="ask-step" aria-label={"Ask " + (index + 1) + " of " + count}>
            <button type="button" className="ghost" aria-label="Previous" title="Previous" onClick={() => onStep(-1)}>
              ‹
            </button>
            <span className="ask-step-n">
              {index + 1} of {count}
            </span>
            <button type="button" className="ghost" aria-label="Next" title="Next" onClick={() => onStep(1)}>
              ›
            </button>
          </span>
        )}
      </div>
      {d?.source && <div className="ask-source">Asked by the {d.source}</div>}

      {loading && <p className="ask-q muted">Reading the prompt…</p>}
      {question && !loading && (
        <p className="ask-q" id="ask-question">
          {question}
        </p>
      )}
      {d?.command && <pre className="ask-cmd">{d.command}</pre>}

      {sel && d && isSelect(d) && (
        <SelectView
          flow={sel.ended ? { ...sel, busy: true } : sel}
          primaryRef={primaryBtn}
          fieldRef={selField}
          onAct={(a) => void act(a)}
          onToggle={(k) => selectFlows.toggle(title, k)}
          onField={(k) => selectFlows.openField(title, k)}
          onText={(t) => selectFlows.setText(title, t)}
          onCloseField={() => selectFlows.closeField(title)}
          onSendField={() => void sendField()}
        />
      )}
      {d?.parsed && !isSelect(d) && (
        <div className="ask-opts" role="group" aria-label="Answers">
          {(strip.state.phase === "answered" ? d.options.filter((o) => o.key === strip.state.picked) : d.options).map((o, i) => (
            <button
              key={o.key}
              ref={i === prim ? primaryBtn : undefined}
              type="button"
              className={"ask-opt" + (i === prim ? " primary" : "") + (strip.state.picked === o.key ? " picked" : "")}
              data-key={o.key}
              data-kind={o.kind}
              disabled={busy}
              title={"Press " + o.key + ": " + o.label}
              onClick={() => strip.pick(o.key)}
            >
              <span className="k">{o.key}</span>
              <span className="ask-opt-label">{optionLabel(o, "thread")}</span>
            </button>
          ))}
          {strip.state.phase === "sending" && <span className="ask-state">sending…</span>}
        </div>
      )}
      {isDialog && !d?.parsed && !loading && (
        <p className="ask-unparsed">This prompt needs the terminal — open the pane to answer it.</p>
      )}

      {!isDialog && (
        <textarea
          ref={field}
          id="ask-answer"
          className="ask-answer"
          rows={3}
          placeholder="Your answer…"
          aria-labelledby="ask-question"
          spellCheck
          disabled={sending}
          onInput={(e) => setHasText(!!e.currentTarget.value.trim())}
          onKeyDown={(e) => {
            if (e.key !== "Enter" || e.shiftKey || e.nativeEvent.isComposing) return;
            e.preventDefault();
            void send(false);
          }}
        />
      )}

      {(strip.state.note || note || sel?.note) && (
        <div className="ask-note" role="status">
          {note || sel?.note || strip.state.note}
        </div>
      )}

      <AskMicRow title={title} />

      <div className="modal-actions">
        <span className="ask-keys">
          {d && isSelect(d)
            ? selectStage(d) === "multi" && /^submit$/i.test(String(d.button || "").trim())
              ? "1–9 ticks · Enter submits · Esc later"
              : SELECT_KEYS[selectStage(d)]
            : d?.parsed
              ? "1–9 or Y / N / A · Enter · Esc later"
              : !isDialog
                ? "Enter sends · Shift+Enter new line · Esc later"
                : "Esc later"}
        </span>
        <button type="button" id="ask-open" onClick={openPane}>
          Open the pane ↗
        </button>
        <button type="button" id="ask-later" onClick={later} title="Put it off — the bell keeps it">
          Later
        </button>
        {!isDialog && (
          <button type="submit" id="ask-send" disabled={!hasText || sending} onClick={() => void send(false)}>
            {sending ? "Sending…" : "Send"}
          </button>
        )}
      </div>
    </div>
  );
}

/** The keys line under Claude's question prompt, by stage. */
export const SELECT_KEYS: Record<ReturnType<typeof selectStage>, string> = {
  single: "1–9 picks · Esc later",
  multi: "1–9 ticks · Enter next · Esc later",
  review: "Enter submits · Esc later",
};

export interface SelectViewProps {
  flow: Pick<SelectFlow, "dialog" | "checked" | "field" | "text" | "busy">;
  primaryRef?: { current: HTMLButtonElement | null };
  fieldRef?: { current: HTMLTextAreaElement | null };
  onAct(a: SelectAct): void;
  onToggle(key: string): void;
  onField(key: string): void;
  onText(text: string): void;
  onCloseField(): void;
  onSendField(): void;
}

/** The multi-select's primary, from the prompt's own button: "Next ›", or
 * "Submit" on the last question (no arrow: nothing comes after it). */
export function navLabel(button: string | null | undefined): string {
  const b = String(button || "").trim() || "Next";
  return /^submit$/i.test(b) ? b : b + " ›";
}

/** Claude's question prompt, one step: its tabs (done ✓, the shown one
 * lit, Submit last), the review's answers, the options with their
 * descriptions — a press (single), a box (multi), a field ("Type
 * something", "Chat about this") — then the nav row in the house
 * `.modal-actions` style: Back on the left, and the step's ONE primary on the
 * right — Next (or the prompt's own `button`, "Submit" on the last question)
 * for a multi-select, "Submit answers" beside a plain Cancel on the review
 * (a confirmation, not a list of options: no digit badges there, though 1 / 2
 * still press them) — with the small "Cancel the question" elsewhere. Pure:
 * the panel owns the flow. */
export function SelectView({ flow, primaryRef, fieldRef, onAct, onToggle, onField, onText, onCloseField, onSendField }: SelectViewProps) {
  const d = flow.dialog;
  const stage = selectStage(d);
  const steps = d.steps || [];
  const step = d.step ?? 0;
  const busy = flow.busy;
  const words = flow.field ? d.options.find((o) => o.key === flow.field) : undefined;
  // The review's two answers are its nav buttons; anything else it offers
  // stays an option.
  const submit = stage === "review" ? d.options.find((o) => o.kind === "yes") : undefined;
  const cancel = stage === "review" ? d.options.find((o) => o.kind === "no") : undefined;
  const opts = d.options.filter((o) => o !== submit && o !== cancel);
  // The one the keyboard lands on: Next (multi), Submit answers (review),
  // else the first option that is a press.
  const prim = stage === "single" ? opts.find((o) => !takesWords(o))?.key || "" : "";
  const label = (o: DialogOption) => (
    <span className="ask-opt-text">
      <span className="ask-opt-label">{o.label}</span>
      {o.desc && <span className="ask-opt-desc">{o.desc}</span>}
    </span>
  );
  return (
    <div className="ask-select" data-stage={stage} aria-busy={busy || undefined}>
      {steps.length > 1 && (
        <ol className="ask-tabs" aria-label="Questions">
          {steps.map((s, i) => (
            <li
              key={i}
              className={"ask-tab" + (i === step ? " active" : "") + (s.done ? " done" : "")}
              aria-current={i === step ? "step" : undefined}
            >
              {s.done && i !== step ? "✓ " : ""}
              {s.label || (s.submit ? "Submit" : "Question " + (i + 1))}
            </li>
          ))}
        </ol>
      )}
      {stage === "review" && !!d.review?.length && (
        <dl className="ask-review">
          {d.review.map((r, i) => (
            <div key={i} style={{ display: "contents" }}>
              <dt>{r.question}</dt>
              <dd>{r.answer || "—"}</dd>
            </div>
          ))}
        </dl>
      )}
      {opts.length > 0 && (
        <div className="ask-opts" role="group" aria-label="Answers">
          {opts.map((o) => {
            const box = stage === "multi" && isBox(o) && !takesWords(o);
            const on = !!flow.checked[o.key];
            return (
              <button
                key={o.key}
                ref={o.key === prim ? primaryRef : undefined}
                type="button"
                role={box ? "checkbox" : undefined}
                aria-checked={box ? on : undefined}
                className={"ask-opt" + (o.key === prim ? " primary" : "") + (flow.field === o.key ? " picked" : "")}
                data-key={o.key}
                data-kind={o.kind}
                disabled={busy}
                title={"Press " + o.key + ": " + o.label}
                onClick={() => (takesWords(o) ? onField(o.key) : box ? onToggle(o.key) : onAct({ type: "pick", key: o.key }))}
              >
                <span className="k">{o.key}</span>
                {box && <span className="ask-box" aria-hidden="true" />}
                {label(o)}
              </button>
            );
          })}
        </div>
      )}
      {words && (
        <div className="ask-sel-field">
          <textarea
            ref={fieldRef}
            id="ask-select-field"
            className="ask-answer"
            rows={2}
            placeholder={words.kind === "chat" ? "What to talk about — it goes to the session…" : "Your answer…"}
            aria-label={words.label}
            spellCheck
            disabled={busy}
            value={flow.text}
            onChange={(e) => onText(e.currentTarget.value)}
            onKeyDown={(e) => {
              if (e.key === "Escape" && !flow.text) {
                e.preventDefault();
                e.stopPropagation();
                onCloseField();
                return;
              }
              if (e.key !== "Enter" || e.shiftKey || e.nativeEvent.isComposing) return;
              e.preventDefault();
              onSendField();
            }}
          />
          <button type="button" id="ask-select-send" disabled={busy || !flow.text.trim()} onClick={onSendField}>
            {words.kind === "chat" ? "Send" : "Answer"}
          </button>
        </div>
      )}
      <div className="ask-sel-nav modal-actions">
        {step > 0 && (
          <button type="button" id="ask-select-back" disabled={busy} onClick={() => onAct({ type: "prev" })}>
            ‹ Back
          </button>
        )}
        {!cancel && (
          <button type="button" className="linklike ask-cancel-q" disabled={busy} onClick={() => onAct({ type: "cancel" })}>
            Cancel the question
          </button>
        )}
        <span className="ask-sel-sp" />
        {busy && <span className="ask-state">sending…</span>}
        {cancel && (
          <button type="button" id="ask-select-cancel" disabled={busy} title={"Press " + cancel.key} onClick={() => onAct({ type: "pick", key: cancel.key })}>
            {cancel.label}
          </button>
        )}
        {submit && (
          <button
            type="submit"
            id="ask-select-submit"
            ref={primaryRef}
            disabled={busy}
            title={"Enter, or press " + submit.key}
            onClick={() => onAct({ type: "pick", key: submit.key })}
          >
            {submit.label}
          </button>
        )}
        {stage === "multi" && (
          <button type="submit" id="ask-select-next" ref={primaryRef} disabled={busy} onClick={() => onAct({ type: "next" })}>
            {navLabel(d.button)}
          </button>
        )}
      </div>
    </div>
  );
}

/** The pop-up's mic: push to talk for THIS session's answer (the same Talk
 * event the hotkey dispatches, so the top-bar mic does the listening), with
 * what it hears underneath. */
function AskMicRow({ title }: { title: string }) {
  const st = useSyncExternalStore(subscribeMicState, getMicState, getMicState);
  // An answer by voice is likely: the mic opens now, not at the press.
  useEffect(() => askMic()?.warm?.(), [title]);
  const mic = askMic();
  if (!mic || !mic.supported()) return null;
  const mine = st.title === title;
  const listening = st.listening && mine;
  const key = talkHotkey();
  const talk = (phase: "down" | "up") => window.dispatchEvent(new CustomEvent(TALK_EVENT, { detail: { phase } }));
  return (
    <div className="ask-voice">
      <button
        type="button"
        id="ask-mic"
        ref={meterRef}
        className={"ask-mic tb-item mic-ring" + (listening ? " listening" : "")}
        aria-pressed={listening}
        aria-label={listening ? "Listening — stop" : "Answer by voice"}
        title={listening ? "Listening — click to stop" : "Answer by voice" + (key ? " (" + key + ")" : "")}
        onPointerDown={(e) => {
          if (e.button !== 0) return;
          e.preventDefault();
          try {
            e.currentTarget.setPointerCapture(e.pointerId);
          } catch {
            /* synthetic */
          }
          talk("down");
        }}
        onPointerUp={() => talk("up")}
        onClick={(e) => {
          // Keyboard activation (Enter / Space) has no pointer events.
          if (e.detail === 0) talk("down");
        }}
      >
        <MicGlyph />
      </button>
      <span className="ask-voice-text">
        {listening || (mine && st.caption) ? (
          <>
            {st.caption}
            {st.unsure && <span className="ask-unsure"> {st.unsure}</span>}
          </>
        ) : (
          <span className="muted">{key ? "Hotkey: " + key : "Answer by voice"}</span>
        )}
      </span>
    </div>
  );
}
