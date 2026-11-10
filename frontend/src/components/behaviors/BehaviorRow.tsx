/** One behavior on the Behaviors card (docs/behaviors.md §2): its text (+
 * expect), who checks it (🤖 agent / 🙋 you — a toggle), its result chip;
 * Pass · Fail · Can't check + a note for yours, the agent's note as evidence,
 * Undo, Edit / Remove (Remove only on one a person added). Keys while the row
 * is focused: 1/p pass, 2/f fail, 3/b can't check, u undo, n note. */

import { useEffect, useRef, useState, type JSX, type KeyboardEvent as ReactKeyboardEvent } from "react";
import type { TestPlan, TestStep, TestStepResult } from "../../api/types";
import { stepResult } from "../dialogs/verify";
import { errorPop } from "../../lib/errorPop";
import { errMsg, relTime } from "../../lib/format";
import { byWords, earlierSteps, needsChips, noteDraftAfter, rowAnswered, rowChip, rowKey, showsAnswers, walkedWords, whoOf } from "./cardModel";
import { NeedsPicker } from "./NeedsPicker";

/** Drafts survive the row remounting (a poll re-renders the card; the Sky
 * may re-mount it when the frame moves). Keyed plan id + behavior id. */
const NOTES = new Map<string, { open: boolean; text: string }>();
const noteKey = (plan: string, step: string) => plan + "\u0000" + step;

const ANSWERS: ReadonlyArray<{ value: Exclude<TestStepResult, "">; label: string; key: string }> = [
  { value: "pass", label: "Pass", key: "1" },
  { value: "fail", label: "Fail", key: "2" },
  { value: "blocked", label: "Can't check", key: "3" },
];

export interface BehaviorRowProps {
  plan: TestPlan;
  step: TestStep;
  index: number;
  /** Resolves when the server has the answer; rejects with its message. */
  onRecord(step: TestStep, result: TestStepResult, note: string): Promise<void>;
  /** Change the wording or who checks it; resolves false when it didn't take. Absent mid-run. */
  onEdit?(step: TestStep, fields: Partial<TestStep>): Promise<boolean>;
  /** Remove a behavior a person added. */
  onRemove?(step: TestStep): Promise<void>;
  /** Open a session to fix this failed behavior. */
  onFix?(step: TestStep): void;
  /** The card's Expand all / Collapse all: every change of `n` sets this
   * row open or closed (rows start collapsed — one short line each). */
  allOpen?: { open: boolean; n: number };
  /** The primary action asked for this row: focus (and scroll) to it. */
  focusNonce?: number;
}

export function BehaviorRow({ plan, step, index, onRecord, onEdit, onRemove, onFix, focusNonce, allOpen }: BehaviorRowProps): JSX.Element {
  const ref = useRef<HTMLLIElement>(null);
  const key = noteKey(plan.id, step.id);
  const draft = NOTES.get(key);
  const [pending, setPending] = useState<TestStepResult | null>(null);
  const [noteOpen, setNoteOpenRaw] = useState(!!draft?.open);
  const [note, setNoteRaw] = useState(draft?.text || "");
  const [editing, setEditing] = useState(false);
  const [confirmRemove, setConfirmRemove] = useState(false);
  const [busy, setBusy] = useState(false);
  // Collapsed by default: a behavior is one short line (its chip and the
  // start of its text) until you open it — a checklist of long, precise
  // behaviors was a wall of text in the Sky. Open shows the whole text, what
  // to expect, the agent's evidence and the answers.
  const [open, setOpen] = useState(!!allOpen?.open);
  useEffect(() => {
    if (allOpen && allOpen.n) setOpen(allOpen.open);
  }, [allOpen?.n]); // eslint-disable-line react-hooks/exhaustive-deps

  const setNoteOpen = (open: boolean, text = note) => {
    setNoteOpenRaw(open);
    if (open) NOTES.set(key, { open, text });
    else NOTES.delete(key);
  };
  const setNote = (text: string) => {
    setNoteRaw(text);
    NOTES.set(key, { open: true, text });
  };

  useEffect(() => {
    if (!focusNonce) return;
    setOpen(true);
    const el = ref.current;
    el?.scrollIntoView?.({ block: "nearest", behavior: "smooth" });
    el?.focus({ preventScroll: true });
  }, [focusNonce]);

  const entry = stepResult(plan, step.id);
  const chip = rowChip(plan, step.id);
  const who = whoOf(plan, step);
  const answered = rowAnswered(entry);
  const answers = showsAnswers(plan, step, entry);
  const shown = pending ?? entry?.result ?? "";
  const yourEntry = entry?.by === "human";
  // An agent answered this person's behavior (a walk-every-behavior run).
  const walked = walkedWords(entry);
  const needs = needsChips(plan, step);
  const locked = pending !== null || busy;
  // Something in progress keeps the row open whatever the toggle says.
  const isOpen = open || editing || noteOpen || confirmRemove;

  const record = async (result: TestStepResult) => {
    setPending(result);
    try {
      await onRecord(step, result, result === "" ? "" : note.trim());
      const next = noteDraftAfter(result, noteOpen, note);
      setNoteRaw(next.text);
      setNoteOpen(next.open, next.text);
    } catch (err) {
      errorPop("Couldn't record that answer", errMsg(err));
    } finally {
      setPending(null);
    }
  };

  /** Re-post your own answer with the note (never restamps the agent's). */
  const saveNote = async () => {
    if (!answered || !yourEntry) return;
    setBusy(true);
    try {
      await onRecord(step, entry!.result, note.trim());
      setNoteOpen(false);
    } catch (err) {
      errorPop("Couldn't save that note", errMsg(err));
    } finally {
      setBusy(false);
    }
  };

  const toggleWho = async () => {
    if (!onEdit || busy) return;
    setBusy(true);
    try {
      await onEdit(step, { actor: who.you ? "agent" : "human" });
    } finally {
      setBusy(false);
    }
  };

  return (
    <li
      ref={ref}
      className={"bh-row is-" + chip.state + (who.you ? " is-yours" : "") + (walked ? " is-walked" : "")}
      data-step-id={step.id}
      data-manual={step.manual ? "1" : undefined}
      tabIndex={0}
      aria-label={"Behavior " + (index + 1) + ": " + step.text + " — " + chip.label}
      onKeyDown={(e) => {
        const from = e.target as HTMLElement;
        if (from !== e.currentTarget && from.closest("input, textarea, select")) return;
        const act = rowKey(e.key, { answered, mods: e.metaKey || e.ctrlKey || e.altKey, repeat: e.repeat, busy: locked });
        if (!act) return;
        e.preventDefault();
        e.stopPropagation();
        if (act === "note") setNoteOpen(true);
        else if (act === "undo") void record("");
        else void record(act);
      }}
    >
      <span className="bh-chip" data-check={chip.state} title={chip.label}>
        <span aria-hidden="true">{chip.glyph}</span> {chip.label}
      </span>
      <div className="bh-row-body">
        {editing && onEdit ? (
          <BehaviorEditor
            plan={plan}
            step={step}
            onCancel={() => setEditing(false)}
            onSave={async (fields) => {
              if (await onEdit(step, fields)) setEditing(false);
            }}
          />
        ) : (
          <>
            <div className="bh-text-line">
              <button
                type="button"
                className={"bh-text bh-text-toggle" + (isOpen ? "" : " is-clamped")}
                aria-expanded={isOpen}
                title={isOpen ? "Show less" : "Show the whole behavior and its answers"}
                onClick={() => setOpen(!isOpen)}
              >
                {step.text}
              </button>
              <button type="button" className="bh-caret" aria-label={isOpen ? "Collapse" : "Expand"} onClick={() => setOpen(!isOpen)}>
                {isOpen ? "▾" : "▸"}
              </button>
            </div>
            {isOpen && step.expect ? <div className="bh-expect">Expect: {step.expect}</div> : null}
            {isOpen && needs.length ? (
              <div className="bh-needs" aria-label="Relies on">
                {needs.map((c) => (
                  <span key={c.id} className="bh-need-chip" title={c.title}>
                    {c.label}
                  </span>
                ))}
              </div>
            ) : null}
          </>
        )}
        {walked ? (
          <span className="bh-walked" title="The agent answered this behavior of yours with its own tools — open it for its evidence; your own answer replaces it">
            {walked}
          </span>
        ) : null}
        {!isOpen ? null : entry?.note && !yourEntry ? (
          <div className={"bh-evidence" + (walked ? " is-walked" : "")} title={walked ? "What the agent observed, and how" : "What the agent recorded"}>
            <span className="bh-evidence-by">{walked ? "The agent walked it:" : "The agent:"}</span> {entry.note}
          </div>
        ) : entry?.note && yourEntry && !noteOpen ? (
          <div className="bh-evidence">
            <span className="bh-evidence-by">Your note:</span> {entry.note}
          </div>
        ) : null}
        {isOpen ? (
        <div className="bh-row-actions">
          <button
            type="button"
            className={"bh-who" + (who.you ? " is-you" : "")}
            title={who.title}
            aria-label={"Who checks behavior " + (index + 1) + ": " + (who.you ? "you" : "the agent")}
            aria-pressed={who.you}
            disabled={!onEdit || busy}
            onClick={() => void toggleWho()}
          >
            {who.label}
          </button>
          {answers ? (
            <span className="bh-answers" role="group" aria-label={"Answer behavior " + (index + 1)}>
              {ANSWERS.map((a) => {
                const on = shown === a.value && (answered || pending === a.value);
                return (
                  <button
                    key={a.value}
                    type="button"
                    className={"test-btn bh-answer bh-answer-" + a.value}
                    data-on={on ? "1" : undefined}
                    aria-pressed={on}
                    disabled={locked}
                    title={on && yourEntry ? "Recorded — press again to take it back" : a.label + " (" + a.key + ")"}
                    onClick={() => void record(on && yourEntry ? "" : a.value)}
                  >
                    {pending === a.value ? "…" : a.label}
                  </button>
                );
              })}
            </span>
          ) : null}
          {entry?.result && !pending ? (
            <span className="bh-receipt">
              {byWords(entry.by)}
              {entry.at ? " · " + relTime(entry.at, { suffix: " old" }) : ""}
            </span>
          ) : null}
          {answered && !pending ? (
            <button type="button" className="tb-item bh-quiet" disabled={locked} title="Take this answer back (u)" onClick={() => void record("")}>
              Undo
            </button>
          ) : null}
          {(!entry || yourEntry || !answered) && !noteOpen ? (
            <button
              type="button"
              className="tb-item bh-quiet"
              title="Add a note (n)"
              onClick={() => {
                setNote(yourEntry ? entry?.note || "" : "");
                setNoteOpen(true);
              }}
            >
              + note
            </button>
          ) : null}
          {shown === "fail" && onFix ? (
            <button type="button" className="tb-item bh-quiet" title="Starts a new session with this failure attached" onClick={() => onFix(step)}>
              Fix
            </button>
          ) : null}
          {onEdit && !editing ? (
            <button type="button" className="tb-item bh-quiet" title="Change the wording — clears any answer on it" onClick={() => setEditing(true)}>
              Edit
            </button>
          ) : null}
          {step.manual && onRemove ? (
            confirmRemove ? (
              <span className="bh-confirm" role="alertdialog" aria-label="Remove this behavior?">
                Remove it?{" "}
                <button
                  type="button"
                  className="test-btn bh-danger"
                  disabled={busy}
                  onClick={async () => {
                    setBusy(true);
                    try {
                      await onRemove(step);
                    } finally {
                      setBusy(false);
                      setConfirmRemove(false);
                    }
                  }}
                >
                  Remove
                </button>{" "}
                <button type="button" className="test-btn" autoFocus onClick={() => setConfirmRemove(false)}>
                  Keep it
                </button>
              </span>
            ) : (
              <button type="button" className="tb-item bh-quiet" title="Delete this behavior you added" onClick={() => setConfirmRemove(true)}>
                Remove
              </button>
            )
          ) : null}
        </div>
        ) : null}
        {noteOpen ? (
          <div className="bh-note-row">
            <input
              type="text"
              className="fl-input bh-note"
              placeholder={shown === "pass" ? "anything worth recording?" : "what happened?"}
              aria-label={"Note for behavior " + (index + 1)}
              autoComplete="off"
              autoFocus
              value={note}
              disabled={busy}
              onChange={(e) => setNote(e.target.value)}
              onKeyDown={(e) => {
                e.stopPropagation();
                if (e.key === "Enter") {
                  e.preventDefault();
                  void saveNote();
                } else if (e.key === "Escape") {
                  e.preventDefault();
                  setNoteOpen(false);
                  ref.current?.focus();
                }
              }}
            />
            {answered && yourEntry ? (
              <button type="button" className="test-btn" disabled={busy} onClick={() => void saveNote()}>
                {busy ? "Saving…" : "Save"}
              </button>
            ) : (
              <span className="bh-hint">Pick Pass, Fail or Can't check to save it</span>
            )}
          </div>
        ) : null}
      </div>
    </li>
  );
}

/** A behavior corrected in place: what to do + what should happen. */
function BehaviorEditor({ plan, step, onCancel, onSave }: { plan: TestPlan; step: TestStep; onCancel(): void; onSave(fields: Partial<TestStep>): Promise<void> }) {
  const [text, setText] = useState(step.text);
  const [expect, setExpect] = useState(step.expect || "");
  const earlier = earlierSteps(plan, step.id);
  // Only ids that are still earlier behaviors (the server drops the rest too).
  const [needs, setNeeds] = useState<string[]>(() => (step.needs || []).filter((id) => earlier.some((e) => e.step.id === id)));
  const [busy, setBusy] = useState(false);
  const save = async () => {
    if (!text.trim()) return;
    setBusy(true);
    try {
      // `needs` only when it changed: the ordinary edit's body stays as it was.
      const was = (step.needs || []).join("\u0000");
      await onSave({ text: text.trim(), expect: expect.trim(), ...(needs.join("\u0000") !== was ? { needs } : {}) });
    } finally {
      setBusy(false);
    }
  };
  const keys = (e: ReactKeyboardEvent) => {
    e.stopPropagation();
    if (e.key === "Escape") {
      e.preventDefault();
      onCancel();
    }
  };
  return (
    <div className="bh-edit">
      <textarea className="fl-input" rows={2} aria-label="What the work should do" autoFocus value={text} disabled={busy} onChange={(e) => setText(e.target.value)} onKeyDown={keys} />
      <input
        type="text"
        className="fl-input"
        aria-label="What should happen"
        placeholder="what should happen"
        autoComplete="off"
        value={expect}
        disabled={busy}
        onChange={(e) => setExpect(e.target.value)}
        onKeyDown={(e) => {
          keys(e);
          if (e.key === "Enter") {
            e.preventDefault();
            void save();
          }
        }}
      />
      <div className="bh-edit-actions">
        <NeedsPicker earlier={earlier} value={needs} disabled={busy} onChange={setNeeds} />
        <button type="button" className="btn-primary" disabled={busy || !text.trim()} onClick={() => void save()}>
          {busy ? "Saving…" : "Save"}
        </button>
        <button type="button" className="test-btn" disabled={busy} onClick={onCancel}>
          Cancel
        </button>
      </div>
    </div>
  );
}
