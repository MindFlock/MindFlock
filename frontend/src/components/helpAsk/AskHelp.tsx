/** "Ask how something works" (lib/helpAsk): the small pop-up at the sidebar
 * foot, opened from ? Help's first row or the palette.
 *
 * Where: beside the sidebar's foot, its bottom on the window's — so it never
 * covers the sessions a pointer may ring; with the sidebar hidden (or a
 * phone-narrow window) at the window's bottom-left corner instead. Not a
 * modal: the app stays live behind it; ✕ or Esc closes it.
 *
 * Empty, it offers a few starter questions and says once what answers them.
 * Asked, it shows "Looking in the docs…" with Cancel; then the answer (text:
 * lines, numbered steps, `inline code`), the doc section it came from (the
 * passage itself one click away), and up to three "Show me" pointers. A
 * pointer is a one-step coach mark (onboarding/CoachTour — the tours' ring,
 * card and Esc): the box steps aside while it shows, the place is reached
 * first (Settings on its screen, a dialog, the sidebar), and a place that
 * still isn't on screen is said in words, never ringed. A question that
 * only asks WHERE something is shows its first pointer at once — and,
 * answered without a model, leads with that place's way there.
 *
 * The answer can make the UI do one thing: show registry places. Its text
 * is text (React-escaped); its pointers are ids looked up in lib/helpPlaces.
 * When the docs don't answer, it says so and offers Send feedback…. */

import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { useConfig } from "../../state/queries";
import { useUi } from "../../state/store";
import { openFeedback } from "../../lib/feedback";
import { CoachTour } from "../onboarding/CoachTour";
import { useAskHelp } from "../../lib/helpAsk";
import { askHelp, STARTERS, takeAskSlot, goToPlace, type HelpAnswer } from "./askApi";
import { isWhereQuestion, type HelpPlace } from "../../lib/helpPlaces";
import "./AskHelp.css";

/** Longest question typed (the server cuts at 400). */
const QUESTION_MAX = 300;

/** `a \`b\` c` → text with <code> spans; never HTML. */
export function inlineCode(line: string): ReactNode[] {
  return line.split("`").map((part, i) => (i % 2 ? <code key={i}>{part}</code> : part));
}

/** The answer's text as paragraphs and a numbered list where its lines are
 * steps ("1. …", "2) …"). */
export function AnswerText({ text }: { text: string }) {
  const lines = text.split("\n").map((l) => l.trim()).filter(Boolean);
  const out: ReactNode[] = [];
  let steps: string[] = [];
  const flush = () => {
    if (!steps.length) return;
    out.push(
      <ol key={"ol" + out.length}>
        {steps.map((s, i) => (
          <li key={i}>{inlineCode(s)}</li>
        ))}
      </ol>
    );
    steps = [];
  };
  for (const l of lines) {
    const m = /^\d+[.)]\s+(.*)$/.exec(l);
    if (m) steps.push(m[1]);
    else {
      flush();
      out.push(<p key={"p" + out.length}>{inlineCode(l)}</p>);
    }
  }
  flush();
  return <div className="ask-answer-text">{out}</div>;
}

type Phase = { kind: "idle" } | { kind: "asking"; question: string } | { kind: "error"; text: string } | { kind: "answer"; answer: HelpAnswer };

export function AskHelp() {
  const guest = !!useConfig().data?.guest;
  const open = useAskHelp((s) => s.open);
  const n = useAskHelp((s) => s.n);
  const pointing = useAskHelp((s) => s.pointing);
  const [q, setQ] = useState("");
  const [phase, setPhase] = useState<Phase>({ kind: "idle" });
  const [showPassage, setShowPassage] = useState(false);
  const [coach, setCoach] = useState<{ place: HelpPlace; ok: boolean } | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  /** The question now out: an older one's late end never touches the box. */
  const reqRef = useRef(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const boxRef = useRef<HTMLDivElement>(null);
  const sidebarHidden = useUi((s) => s.sidebarHidden);
  const sidebarWidth = useUi((s) => s.sidebarWidth);
  const narrow = useUi((s) => s.narrow);

  // Focus the box on each open.
  useEffect(() => {
    if (open && !pointing) inputRef.current?.focus({ preventScroll: true });
  }, [open, n, pointing]);

  // Closing cancels a question in flight.
  useEffect(() => {
    if (!open) abortRef.current?.abort();
  }, [open]);

  // Beside the sidebar's foot; the window's bottom-left without one.
  useLayoutEffect(() => {
    const place = () => {
      const box = boxRef.current;
      if (!box) return;
      const W = window.innerWidth;
      const side = document.getElementById("sidebar");
      const r = !sidebarHidden && !narrow && side ? side.getBoundingClientRect() : null;
      let left = r && r.width > 0 ? r.right + 8 : 8;
      const w = Math.min(380, W - 16);
      if (left + w > W - 8) left = Math.max(8, W - w - 8);
      box.style.left = Math.round(left) + "px";
      box.style.width = Math.round(w) + "px";
    };
    place();
    window.addEventListener("resize", place);
    return () => window.removeEventListener("resize", place);
  }, [open, pointing, sidebarHidden, sidebarWidth, narrow]);

  if (guest) return null;

  const close = () => {
    abortRef.current?.abort();
    useAskHelp.getState().close();
  };

  const showPlace = async (place: HelpPlace) => {
    useAskHelp.getState().setPointing(place);
    const ok = await goToPlace(place);
    setCoach({ place, ok });
  };

  const endPointer = () => {
    const place = coach?.place;
    setCoach(null);
    // Back to the answer — unless the pointer opened Settings, a dialog or
    // the drawer, which is where the person now is: the box waits, closed,
    // with its answer, for the next ? Help → Ask.
    const k = place?.reach.kind;
    if (k === "settings" || k === "dialog" || k === "hub") useAskHelp.getState().close();
    else useAskHelp.getState().setPointing(null);
  };

  const ask = async (question: string) => {
    const text = question.trim().slice(0, QUESTION_MAX);
    if (!text || phase.kind === "asking") return;
    // The answer on screen already is this question's: nothing to send.
    if (phase.kind === "answer" && phase.answer.question.toLowerCase() === text.toLowerCase()) return;
    const ctl = new AbortController();
    abortRef.current = ctl;
    const id = ++reqRef.current;
    setShowPassage(false);
    setPhase({ kind: "asking", question: text });
    try {
      const wait = takeAskSlot();
      if (wait > 0) await new Promise((r) => setTimeout(r, wait));
      if (ctl.signal.aborted) return;
      const answer = await askHelp(text, ctl.signal);
      if (ctl.signal.aborted) return;
      setPhase({ kind: "answer", answer });
      if (isWhereQuestion(text) && answer.places.length) void showPlace(answer.places[0]);
    } catch (err) {
      if ((err as Error)?.name === "AbortError" || ctl.signal.aborted) return;
      setPhase({ kind: "error", text: (err as Error)?.message || "Couldn't ask. Try again." });
    } finally {
      if (abortRef.current === ctl) abortRef.current = null;
      // Cancelled (Cancel, Esc, or the box closed): back to empty — unless a
      // newer question is out by now.
      if (ctl.signal.aborted && reqRef.current === id) setPhase({ kind: "idle" });
    }
  };

  const cancel = () => {
    abortRef.current?.abort();
    abortRef.current = null;
    setPhase({ kind: "idle" });
    inputRef.current?.focus();
  };

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Escape") {
      e.preventDefault();
      e.stopPropagation();
      if (phase.kind === "asking") cancel();
      else close();
    }
  };

  const coachLayer = coach && (
    <CoachTour
      steps={[
        {
          target: coach.ok ? coach.place.target : "",
          title: coach.place.label,
          body: coach.ok ? coach.place.caption : "It isn't on screen right now. " + coach.place.how,
        },
      ]}
      onClose={endPointer}
      skipLabel="Close"
      labelId="ask-help-coach-title"
    />
  );

  if (!open || pointing) return coachLayer || null;

  const answer = phase.kind === "answer" ? phase.answer : null;
  // "Where is X?" without a model: the place's own way there leads (the
  // registry knows where things are; the docs' nearest passage may not be
  // about it), and the passage is offered below as "Also in the docs".
  const lead = answer && !answer.model && answer.places.length && isWhereQuestion(answer.question) ? answer.places[0].how : "";
  const box = (
    <div
      id="ask-help"
      ref={boxRef}
      role="dialog"
      aria-modal="false"
      aria-labelledby="ask-help-title"
      onKeyDown={onKeyDown}
    >
      <div className="ask-head">
        <h3 id="ask-help-title">Ask how something works</h3>
        <button type="button" className="ask-x" aria-label="Close" title="Close (Esc)" onClick={close}>
          ✕
        </button>
      </div>
      <form
        className="ask-form"
        onSubmit={(e) => {
          e.preventDefault();
          void ask(q);
        }}
      >
        <input
          ref={inputRef}
          id="ask-help-input"
          type="text"
          value={q}
          maxLength={QUESTION_MAX}
          placeholder="How do I…? Where is…?"
          aria-label="Your question about MindFlock"
          autoComplete="off"
          onChange={(e) => setQ(e.target.value)}
        />
        <button type="submit" className="ask-go" disabled={!q.trim() || phase.kind === "asking"}>
          Ask
        </button>
      </form>

      {phase.kind === "idle" && (
        <div className="ask-empty">
          <div className="ask-starters" role="list" aria-label="Try one of these">
            {STARTERS.map((s) => (
              <button
                key={s}
                type="button"
                role="listitem"
                className="ask-starter"
                onClick={() => {
                  setQ(s);
                  void ask(s);
                }}
              >
                {s}
              </button>
            ))}
          </div>
          <p className="ask-note">
            Answers come from MindFlock's own docs, worded by Claude Haiku through your Claude sign-in, the way voice help
            is. Without it you get the docs' plain words.
          </p>
        </div>
      )}

      {phase.kind === "asking" && (
        <div className="ask-wait" role="status">
          <span className="ask-dots" aria-hidden="true">
            <i />
            <i />
            <i />
          </span>
          Looking in the docs…
          <button type="button" className="linklike ask-cancel" onClick={cancel}>
            Cancel
          </button>
        </div>
      )}

      {phase.kind === "error" && (
        <p className="ask-error" role="alert">
          {phase.text}
        </p>
      )}

      {answer && (
        <div className="ask-answer" aria-live="polite">
          {lead ? (
            <p className="ask-lead">{lead}</p>
          ) : answer.found ? (
            <AnswerText text={answer.text} />
          ) : (
            <p className="ask-unknown">
              {answer.text}{" "}
              <button type="button" className="linklike" onClick={() => openFeedback()}>
                Send feedback…
              </button>{" "}
              to ask for it.
            </p>
          )}
          {answer.places.length > 0 && (
            <div className="ask-places">
              <span className="ask-places-label">Show me</span>
              {answer.places.map((p) => (
                <button key={p.id} type="button" className="ask-place" title={p.caption} onClick={() => void showPlace(p)}>
                  {p.label}
                </button>
              ))}
            </div>
          )}
          {answer.source && (
            <div className="ask-source">
              <span title={answer.source.doc + " › " + answer.source.path}>
                {lead ? "Also in the docs: " : "From "}
                {answer.source.doc} › {answer.source.heading}
                {!answer.model && !lead && (answer.limited ? " (many questions at once: the docs' own words)" : " (the docs' own words)")}
              </span>
              {answer.source.excerpt && (
                <button
                  type="button"
                  className="linklike"
                  aria-expanded={showPassage}
                  onClick={() => setShowPassage((v) => !v)}
                >
                  {showPassage ? "Hide the section" : "Read the section"}
                </button>
              )}
              {showPassage && (
                <blockquote className="ask-passage">
                  <AnswerText text={answer.source.excerpt} />
                </blockquote>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
  return (
    <>
      {typeof document === "undefined" ? box : createPortal(box, document.body)}
      {coachLayer}
    </>
  );
}
