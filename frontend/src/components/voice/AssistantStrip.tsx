/** The voice assistant's strip: a small card under the top bar's mic while
 * you talk to MindFlock itself (lib/voiceAssistant).
 *
 * It says, always, where your words go: "Talking to MindFlock" in the
 * accent (dictating into a session is the mic's red, and shows no box at
 * all) — then the words heard, "Thinking…", the reply's caption, a chip per
 * thing it did ("Opened Intake", "Started fix-login" — a session's chip
 * focuses it, a dialog's opens it again) and, for a confirm, Yes / No
 * buttons beside the spoken question. It fades STRIP_FADE_MS after its last
 * change unless assistant mode is on, a turn is under way or a question
 * waits. Its × ends assistant mode (or just hides it).
 *
 * Calm-surface rules: no animation but a fade and three still dots that
 * step; nothing moves the bar; one line each. */

import { useEffect, useState, useSyncExternalStore } from "react";
import { createPortal } from "react-dom";
import { placeUnder } from "../../lib/menuPlace";
import {
  LISTENING_LINE,
  STRIP_FADE_MS,
  THINKING_LINE,
  assistantView,
  stripVisible,
  type AssistantChip,
  type AssistantView,
} from "../../lib/voiceAssistant";
import "./AssistantStrip.css";

/** The strip's width (px), as the mic's options. */
export const STRIP_W = 320;
/** Room left under the mic for its state pill (#mic-pill). */
const PILL_ROOM = 18;

/** The head line: where the words go, and what the assistant is doing. */
export function stripHead(v: Pick<AssistantView, "mode" | "phase">): string {
  if (v.phase === "thinking") return THINKING_LINE;
  if (v.mode) return LISTENING_LINE;
  return "MindFlock";
}

/** The strip's body, without the portal or the timer (rendered to a string
 * by the tests). */
export function AssistantStripBody({
  v,
  onChip,
  onAnswer,
  onClose,
}: {
  v: AssistantView;
  onChip?(c: AssistantChip): void;
  onAnswer?(yes: boolean): void;
  onClose?(): void;
}) {
  const confirm = v.question?.kind === "confirm";
  return (
    <>
      <div className="assist-head">
        <span className={"assist-dot" + (v.mode ? " on" : "")} aria-hidden="true" />
        <span className="assist-where" id="assist-where">
          {stripHead(v)}
        </span>
        {v.phase === "thinking" && (
          <span className="assist-thinking" aria-hidden="true">
            <i />
            <i />
            <i />
          </span>
        )}
        <button
          type="button"
          className="assist-close"
          id="assist-close"
          aria-label={v.mode ? "Stop talking to MindFlock" : "Close"}
          title={v.mode ? "Stop talking to MindFlock (Alt+M)" : "Close"}
          onClick={() => onClose?.()}
        >
          ×
        </button>
      </div>
      {v.heard && (
        <p className="assist-heard" id="assist-heard">
          {"“" + v.heard + "”"}
        </p>
      )}
      {v.caption && (
        <p className={"assist-caption" + (v.error ? " is-error" : "")} id="assist-caption">
          {v.caption}
        </p>
      )}
      {v.chips.length > 0 && (
        <div className="assist-chips" id="assist-chips">
          {v.chips.map((c, i) => {
            const jumps = c.ok && !!(c.session || c.open);
            return jumps ? (
              <button type="button" key={i} className="assist-chip" title={c.session ? "Go to it" : "Open it again"} onClick={() => onChip?.(c)}>
                {c.text}
              </button>
            ) : (
              <span key={i} className={"assist-chip" + (c.ok ? "" : " is-error")}>
                {c.text}
              </span>
            );
          })}
        </div>
      )}
      {confirm && (
        <div className="assist-confirm" id="assist-confirm">
          <button type="button" className="assist-yes" id="assist-yes" onClick={() => onAnswer?.(true)}>
            Yes
          </button>
          <button type="button" className="assist-no" id="assist-no" onClick={() => onAnswer?.(false)}>
            No
          </button>
          <span className="assist-or">or say it</span>
        </div>
      )}
    </>
  );
}

/** The strip stays on screen (no fade) while this holds. */
export function stripHolds(v: AssistantView): boolean {
  return v.mode || v.phase === "thinking" || !!v.question;
}

export function AssistantStrip({
  anchor,
  onChip,
  onAnswer,
  onClose,
}: {
  /** The mic button: the strip hangs under it. */
  anchor: HTMLElement | null;
  onChip(c: AssistantChip): void;
  onAnswer(yes: boolean): void;
  onClose(): void;
}) {
  const v = useSyncExternalStore(assistantView.subscribe, assistantView.get, assistantView.get);
  const [, setTick] = useState(0);
  // The fade: STRIP_FADE_MS after the last change, unless it holds.
  useEffect(() => {
    if (!stripVisible(v) || stripHolds(v)) return;
    const t = setTimeout(() => {
      assistantView.clear();
      setTick((n) => n + 1);
    }, STRIP_FADE_MS);
    return () => clearTimeout(t);
  }, [v]);

  if (!stripVisible(v)) return null;
  const rect = anchor?.getBoundingClientRect();
  const w = Math.min(STRIP_W, window.innerWidth - 32);
  const place = rect ? placeUnder({ left: rect.left, right: rect.right, bottom: rect.bottom + PILL_ROOM }, w, window.innerWidth, window.innerHeight) : null;
  const style = place ? { top: place.top + "px", left: place.left + "px", width: w + "px" } : { top: "48px", right: "16px", width: w + "px" };
  return createPortal(
    <div
      className={"assist-strip" + (v.mode ? " is-mode" : "") + (v.question ? " is-asking" : "")}
      id="assist-strip"
      style={style}
      role="status"
      aria-live="polite"
      aria-label="MindFlock assistant"
    >
      <AssistantStripBody v={v} onChip={onChip} onAnswer={onAnswer} onClose={onClose} />
    </div>,
    document.body
  );
}
