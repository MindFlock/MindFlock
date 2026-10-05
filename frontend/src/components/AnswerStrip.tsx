/** The one-click answer to a blocked agent's prompt — shared by the rail row,
 * the bell's attention item and the Thread tab's worker row.
 *
 * While the session sits in `clarify` it fetches the dialog the agent is
 * stuck on (GET /dialog, which the provider parses), shows the question and
 * the dialog's OWN options, and a click presses that key as you (POST /answer
 * with the dialog's id). Three rules keep a stray click from doing damage:
 *  - "Always" (yes-and-don't-ask-again) is never the primary button;
 *  - every answer carries `dialog_id`, so if the prompt changed between the
 *    fetch and the click the server refuses (409) and the new one is shown.
 *    The same prompt merely redrawn (a resize re-cuts its lines) is not a
 *    change: the strip re-reads the dialog when its row resizes and right
 *    before a click on a read older than DIALOG_FRESH_MS, and a 409 whose
 *    fresh read is the same prompt (same keys, labels and command) is
 *    retried once under the fresh id (`answerFresh`);
 *  - after a click the strip reads "answered" and stays disabled until the
 *    activity moves on, a NEW dialog id appears, or — the same prompt asked
 *    again (an identical command has an identical id) — the server's
 *    one-answer hold has passed (ANSWERED_HOLD_MS); a double-click cannot
 *    answer the next prompt, and the server refuses a second answer to the
 *    same one inside the hold (409 `dialog_answered`) too.
 * An unparsed dialog shows its question and ↗ only: there is nothing it can
 * safely press. Nothing is fetched while the row's activity isn't
 * "clarify", and "not waiting" reads as no dialog, quietly (`?quiet=1`). Keys 1–9 are the HOST's job (only while its row is focused),
 * through the `answerKey` handle on `ref`. */

import { useEffect, useImperativeHandle, useReducer, useRef, type ReactNode, type Ref } from "react";
import type { Dialog, DialogOption } from "../api/types";
import { useInstances } from "../state/queries";
import { displayName } from "../state/store";
import { errMsg } from "../lib/format";
import { answerFresh, fetchDialog } from "../lib/flockActions";

export type AnswerVariant = "rail" | "bell" | "thread";

export interface AnswerStripProps {
  title: string;
  /** The session's live activity; the strip only exists while it is "clarify". */
  activity: string;
  variant: AnswerVariant;
  /** ↗ — open the session's pane. */
  onOpen?: () => void;
  /** Called after a "No…" (kind "no") answer went through: the agent now
   * wants to hear what to do instead. */
  onRedirect?: () => void;
  /** Extra controls at the end of the button row (the Thread's "Let api
   * decide"). */
  children?: ReactNode;
  ref?: Ref<AnswerStripHandle>;
}

export interface AnswerStripHandle {
  /** Press option `key` ("1".."9") as if its button were clicked. false when
   * there is no such option or the strip can't answer right now. */
  answerKey(key: string): boolean;
}

/** While answered, how often to look for a NEW prompt: an agent can go
 * straight from one dialog to the next between two instance polls, and the
 * activity (still "clarify") would never tell us. */
export const ANSWER_RECHECK_MS = 3000;
/** The server's one-answer hold (agent_io.ANSWERED_HOLD_S): inside it a
 * second answer to the same dialog id is refused. Past it, a recheck that
 * still finds that id means the agent asked the SAME prompt again (or the
 * key was lost) — the strip offers its buttons again rather than sitting on
 * "answered" while the worker waits. Measured from when the answer came
 * back, so the server's hold has always run out first. */
export const ANSWERED_HOLD_MS = 4000;
/** After the row (or the window) resizes, how long to let the pane follow
 * and the CLI redraw before reading the dialog again. */
export const RESIZE_SETTLE_MS = 600;

// --- State ------------------------------------------------------------------

export interface StripState {
  dialog: Dialog | null;
  phase: "idle" | "loading" | "ready" | "sending" | "answered";
  /** The option key that was sent (shown while answered). */
  picked: string;
  /** One line under the buttons: the prompt changed, or the answer failed. */
  note: string;
  /** When the answer went through (ms; 0 = not answered). */
  answeredAt: number;
  /** When `dialog` was read (ms; 0 = never): a click on an older read
   * reads it again first. */
  loadedAt: number;
}

export type StripAction =
  | { type: "reset" }
  | { type: "loading" }
  | { type: "loaded"; dialog: Dialog | null; note?: string; now?: number }
  /** A re-read while the buttons are up (a resize): applied only while
   * "ready", so it never disturbs a click in flight or the answered latch. */
  | { type: "refreshed"; dialog: Dialog | null; now?: number }
  | { type: "sending"; key: string }
  /** `dialog`: the read the key went into, when a fresh one replaced the
   * shown one — the latch must compare rechecks against THAT id. */
  | { type: "answered"; now?: number; dialog?: Dialog }
  | { type: "failed"; note: string };

export const STRIP_INITIAL: StripState = {
  dialog: null,
  phase: "idle",
  picked: "",
  note: "",
  answeredAt: 0,
  loadedAt: 0,
};

export function stripReducer(s: StripState, a: StripAction): StripState {
  switch (a.type) {
    case "reset":
      return STRIP_INITIAL;
    case "loading":
      return { ...s, phase: s.dialog ? s.phase : "loading" };
    case "loaded":
      if (s.phase === "answered" && a.dialog && s.dialog && a.dialog.id === s.dialog.id) {
        // The SAME prompt while answered: inside the hold the keys already
        // went out and the screen just hasn't moved yet — change nothing.
        // Past it, the agent is asking that prompt again.
        const now = a.now ?? Date.now();
        if (!s.answeredAt || now - s.answeredAt < ANSWERED_HOLD_MS) return s;
        return { ...STRIP_INITIAL, dialog: a.dialog, phase: "ready", note: "It's asking again", loadedAt: now };
      }
      if (s.phase === "answered" && !a.dialog) return s;
      return {
        ...STRIP_INITIAL,
        dialog: a.dialog,
        phase: a.dialog ? "ready" : "idle",
        note: a.note || "",
        loadedAt: a.dialog ? (a.now ?? Date.now()) : 0,
      };
    case "refreshed":
      if (s.phase !== "ready") return s;
      // Gone: the prompt was answered elsewhere (the pane, the agent).
      if (!a.dialog) return STRIP_INITIAL;
      // Another id is the same prompt redrawn as often as a new one: keep
      // the note only while the id holds.
      return {
        ...s,
        dialog: a.dialog,
        note: s.dialog && a.dialog.id === s.dialog.id ? s.note : "",
        loadedAt: a.now ?? Date.now(),
      };
    case "sending":
      return s.phase === "ready" ? { ...s, phase: "sending", picked: a.key, note: "" } : s;
    case "answered":
      return s.phase === "sending"
        ? { ...s, dialog: a.dialog ?? s.dialog, phase: "answered", answeredAt: a.now ?? Date.now() }
        : s;
    case "failed":
      return { ...s, phase: s.dialog ? "ready" : "idle", picked: "", note: a.note };
    default:
      return s;
  }
}

// --- Wording ----------------------------------------------------------------

/** The primary option: the first, unless it is "always" — never primary, so
 * Enter-happy muscle memory can't grant a standing permission. -1 = none. */
export function primaryIndex(options: DialogOption[]): number {
  return options.length && options[0].kind !== "always" ? 0 : -1;
}

/** A button's label. The rail and the bell are narrow, so they say it in one
 * word by kind; the Thread row has room for the meaning of "always". The
 * dialog's full wording always rides in the tooltip. */
export function optionLabel(o: DialogOption, variant: AnswerVariant): string {
  if (o.kind === "yes") return "Yes";
  if (o.kind === "always") return variant === "thread" ? "Yes, don't ask again" : "Always";
  if (o.kind === "no") return "No…";
  const words = o.label.replace(/\s*\(esc\)\s*$/i, "").trim();
  const max = variant === "thread" ? 28 : 10;
  return words.length > max ? words.slice(0, max - 1).trimEnd() + "…" : words;
}

/** The question line. The rail drops the "Do you want to" preamble every
 * permission dialog opens with — next to the command, "proceed?" is the
 * whole question, and the rail has a row's width to say it in. The bell and
 * the Thread have room for the dialog's own words. */
export function questionText(d: Pick<Dialog, "question">, variant: AnswerVariant): string {
  const q = d.question.trim();
  if (variant !== "rail") return q;
  return q.replace(/^(do you want to|would you like to)\s+/i, "") || q;
}

// --- View -------------------------------------------------------------------

export interface AnswerStripViewProps {
  state: StripState;
  /** The strip's root element (the container watches its size). */
  rootRef?: Ref<HTMLDivElement>;
  variant: AnswerVariant;
  hint?: string;
  onPick(key: string): void;
  onOpen?: () => void;
  children?: ReactNode;
}

/** Pure render of a strip state (the container below owns the fetches). */
export function AnswerStripView({ state, rootRef, variant, hint, onPick, onOpen, children }: AnswerStripViewProps) {
  const d = state.dialog;
  if (!d) return null;
  const q = questionText(d, variant);
  const busy = state.phase === "sending" || state.phase === "answered";
  const prim = primaryIndex(d.options);
  const shown = state.phase === "answered" ? d.options.filter((o) => o.key === state.picked) : d.options;
  return (
    <div
      ref={rootRef}
      className={"flock-answer fa-" + variant + (busy ? " is-busy" : "") + (d.parsed ? "" : " is-unparsed")}
      data-dialog={d.id}
      // The host row / bell item navigates on click; a strip click is an
      // answer, never also a "select this session".
      onClick={(e) => e.stopPropagation()}
      onDoubleClick={(e) => e.stopPropagation()}
    >
      {(q || d.command) && (
        <div
          className="fa-q"
          title={[d.command, d.question, d.source ? "Asked by the " + d.source : ""].filter(Boolean).join("\n")}
        >
          {d.command && <code>{d.command}</code>}
          {d.command && q ? " — " : ""}
          {q}
        </div>
      )}
      <div className="fa-box">
        <div className="fa-btns">
          {d.parsed &&
            shown.map((o) => (
              <button
                key={o.key}
                type="button"
                className={
                  "fa-opt" +
                  (d.options.indexOf(o) === prim ? " primary" : "") +
                  (state.picked === o.key ? " picked" : "")
                }
                data-key={o.key}
                data-kind={o.kind}
                disabled={busy}
                title={"Press " + o.key + ": " + o.label}
                onClick={() => onPick(o.key)}
              >
                <span className="k">{o.key}</span>
                {optionLabel(o, variant)}
              </button>
            ))}
          {state.phase === "answered" && <span className="fa-state">answered</span>}
          {state.phase === "sending" && <span className="fa-state">sending…</span>}
          {/* The Thread row right-aligns its extra controls with ↗. */}
          {variant === "thread" && <span className="fa-sp" />}
          {children}
          {onOpen && (
            <button
              type="button"
              className="fa-open ghost"
              title="Open its pane"
              onClick={() => onOpen()}
            >
              {variant === "thread" ? "Open ↗" : "↗"}
            </button>
          )}
        </div>
        {hint && <div className="fa-hint">{hint}</div>}
        {state.note && <div className="fa-note">{state.note}</div>}
      </div>
    </div>
  );
}

// --- Keys -------------------------------------------------------------------

/** Every mounted strip, by session: what `answerKey` presses through. */
const mounted = new Map<string, Set<AnswerStripHandle>>();

/** Press option `key` on a mounted strip showing `title`'s prompt, exactly as
 * a click on its button would (same dialog id, same "answered" latch). For a
 * host that handles 1–9 itself and has no ref to hand — deciding WHEN a key
 * counts (only while its row is focused) stays the host's job. false when no
 * strip for `title` can answer right now. */
export function answerKey(title: string, key: string): boolean {
  for (const h of mounted.get(title) ?? []) if (h.answerKey(key)) return true;
  return false;
}

// --- Container --------------------------------------------------------------

export function AnswerStrip({ title, activity, variant, onOpen, onRedirect, children, ref }: AnswerStripProps) {
  const [state, dispatch] = useReducer(stripReducer, STRIP_INITIAL);
  const live = useRef(state);
  live.current = state;
  // The newest callbacks, read at answer time (the answer outlives renders).
  const redirect = useRef(onRedirect);
  redirect.current = onRedirect;
  const { data: instances } = useInstances();
  const waiting = activity === "clarify";

  // (Re)load whenever the session or its activity changes; anything else
  // clears the strip — including the "answered" latch, which is exactly what
  // "disabled until the activity changes" means.
  useEffect(() => {
    dispatch({ type: "reset" });
    if (!waiting) return;
    const ctl = new AbortController();
    dispatch({ type: "loading" });
    fetchDialog(title, ctl.signal).then(
      (dialog) => !ctl.signal.aborted && dispatch({ type: "loaded", dialog }),
      () => !ctl.signal.aborted && dispatch({ type: "loaded", dialog: null })
    );
    return () => ctl.abort();
  }, [title, waiting, activity]);

  // Answered but still in clarify: watch for the NEXT prompt.
  const answered = state.phase === "answered";
  useEffect(() => {
    if (!answered || !waiting) return;
    const ctl = new AbortController();
    const timer = window.setInterval(() => {
      fetchDialog(title, ctl.signal).then(
        (dialog) => !ctl.signal.aborted && dispatch({ type: "loaded", dialog }),
        () => {}
      );
    }, ANSWER_RECHECK_MS);
    return () => {
      ctl.abort();
      clearInterval(timer);
    };
  }, [answered, waiting, title]);

  // The row or the window resized: the pane most likely did too, and the
  // CLI re-cut the dialog's lines — read it again once things settle, so a
  // click goes out under the id the server will see.
  const root = useRef<HTMLDivElement>(null);
  const shown = !!state.dialog;
  useEffect(() => {
    if (!waiting || !shown) return;
    const ctl = new AbortController();
    let timer = 0;
    let first = true;
    const settle = () => {
      clearTimeout(timer);
      timer = window.setTimeout(() => {
        if (live.current.phase !== "ready") return;
        fetchDialog(title, ctl.signal).then(
          (dialog) => !ctl.signal.aborted && dispatch({ type: "refreshed", dialog }),
          () => {}
        );
      }, RESIZE_SETTLE_MS);
    };
    window.addEventListener("resize", settle);
    const ro =
      typeof ResizeObserver !== "undefined" && root.current
        ? new ResizeObserver(() => {
            // Its first report is the size it mounted with, not a resize.
            if (first) first = false;
            else settle();
          })
        : null;
    if (ro && root.current) ro.observe(root.current);
    return () => {
      ctl.abort();
      clearTimeout(timer);
      window.removeEventListener("resize", settle);
      ro?.disconnect();
    };
  }, [waiting, shown, title]);

  const pick = (key: string): boolean => {
    const s = live.current;
    const d = s.dialog;
    const opt = d?.parsed ? d.options.find((o) => o.key === key) : undefined;
    if (!d || !opt || s.phase !== "ready") return false;
    dispatch({ type: "sending", key });
    answerFresh(title, key, d, s.loadedAt).then((out) => {
      if (out.kind === "answered") {
        dispatch({ type: "answered", dialog: out.dialog });
        if (opt.kind === "no") redirect.current?.();
      } else if (out.kind === "already") {
        // Someone got there first (the bell's strip, the orchestrator):
        // the prompt has its answer — this one is answered too.
        dispatch({ type: "answered" });
      } else if (out.kind === "changed") {
        // Never re-send into a prompt nobody has read: show the new one.
        dispatch({
          type: "loaded",
          dialog: out.dialog,
          note: out.dialog ? "The prompt changed — this is the new one" : "",
        });
      } else {
        dispatch({ type: "failed", note: "Couldn't answer: " + errMsg(out.error) });
      }
    });
    return true;
  };

  useImperativeHandle(ref, () => ({ answerKey: pick }));
  // The registry holds one stable handle per mount; it reads the newest
  // `pick` (and through it the newest state) at press time.
  const pickNow = useRef(pick);
  pickNow.current = pick;
  useEffect(() => {
    const h: AnswerStripHandle = { answerKey: (k) => pickNow.current(k) };
    let set = mounted.get(title);
    if (!set) mounted.set(title, (set = new Set()));
    set.add(h);
    return () => {
      set.delete(h);
      if (!set.size && mounted.get(title) === set) mounted.delete(title);
    };
  }, [title]);

  if (!waiting) return null;
  let hint: string | undefined;
  if (variant === "bell") {
    const parent = instances?.find((i) => i.title === title)?.parent;
    if (parent) hint = displayName(parent) + " is waiting on this worker too";
  }
  return (
    <AnswerStripView state={state} rootRef={root} variant={variant} hint={hint} onPick={pick} onOpen={onOpen}>
      {children}
    </AnswerStripView>
  );
}
