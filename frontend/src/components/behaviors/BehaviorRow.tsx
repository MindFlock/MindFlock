/** One step on the Verify card: its result chip, its text (+ what should
 * happen), who does it (🤖 agent / 🙋 you) and what was recorded.
 * READ-ONLY — the steps are the agent's own (owner request 2026-11-10): a
 * person's part is the card's one Confirm verified, never a per-step answer
 * or edit. Open, a finished step shows its result, WHO answered it and the
 * evidence in full (and what the agent had said when a person overrode it); a
 * blocked step says why even folded. A failed step offers "Let its agent fix
 * it". */

import { useEffect, useState, type JSX } from "react";
import type { TestPlan, TestStep, TestStepResultEntry } from "../../api/types";
import { stepResult } from "../dialogs/verify";
import { relTime } from "../../lib/format";
import { AGENT_FIX, byWords, rowChip, whoOf } from "./cardModel";

/** A result entry as the server may send it: `agent_note` is the agent's
 * evidence kept when a PERSON overrode its answer (optional — an older
 * server never sends it; api/types.ts is not this module's to widen). */
type Entry = TestStepResultEntry & { agent_note?: string };

/** What a row shows about its recorded answer, kept pure for the tests:
 * who answered, the evidence (in full), the agent's own note when a person
 * overrode it, and — for a blocked step — the why, shown even folded. */
export function rowEvidence(entry: Entry | null | undefined): { by: string; note: string; agentNote: string; why: string } {
  if (!entry || !entry.result) return { by: "", note: String(entry?.note || "").trim(), agentNote: "", why: "" };
  const note = String(entry.note || "").trim();
  const agentNote = entry.by === "human" ? String(entry.agent_note || "").trim() : "";
  return {
    by: byWords(entry.by),
    note,
    agentNote,
    why: entry.result === "blocked" ? note || "No reason was given." : "",
  };
}

export interface BehaviorRowProps {
  plan: TestPlan;
  step: TestStep;
  index: number;
  /** Hand this failed step to the session's own agent (the card's fix). */
  onFix?(step: TestStep): void;
  /** The card's Expand all / Collapse all: every change of `n` sets this
   * row open or closed (rows start collapsed — one short line each). */
  allOpen?: { open: boolean; n: number };
}

export function BehaviorRow({ plan, step, index, onFix, allOpen }: BehaviorRowProps): JSX.Element {
  // Collapsed by default: a step is one short line (its chip and the start
  // of its text) until you open it — long, precise steps were a wall of text
  // in the Sky. Open shows the whole text, what to expect and the evidence.
  const [open, setOpen] = useState(!!allOpen?.open);
  useEffect(() => {
    if (allOpen && allOpen.n) setOpen(allOpen.open);
  }, [allOpen?.n]); // eslint-disable-line react-hooks/exhaustive-deps

  const entry = stepResult(plan, step.id) as Entry | null;
  const chip = rowChip(plan, step.id);
  const who = whoOf(plan, step);
  const ev = rowEvidence(entry);
  return (
    <li
      className={"bh-row is-" + chip.state + (who.you ? " is-yours" : "")}
      data-step-id={step.id}
      aria-label={"Step " + (index + 1) + ": " + step.text + " — " + chip.label + (ev.by ? ", by " + ev.by : "")}
    >
      <span className="bh-chip" data-check={chip.state} title={chip.label + (ev.by ? " — by " + ev.by : "")}>
        <span aria-hidden="true">{chip.glyph}</span> {chip.label}
      </span>
      <div className="bh-row-body">
        <div className="bh-text-line">
          <button
            type="button"
            className={"bh-text bh-text-toggle" + (open ? "" : " is-clamped")}
            aria-expanded={open}
            title={open ? "Show less" : "Show the whole step and its result"}
            onClick={() => setOpen(!open)}
          >
            {step.text}
          </button>
          <span className={"bh-who" + (who.you ? " is-you" : "")} title={who.title}>
            {who.label}
          </span>
          <button type="button" className="bh-caret" aria-label={open ? "Collapse" : "Expand"} onClick={() => setOpen(!open)}>
            {open ? "▾" : "▸"}
          </button>
        </div>
        {ev.why && !open ? (
          <div className="bh-evidence bh-why is-clamped" title={ev.why}>
            <span className="bh-evidence-by">Why:</span> {ev.why}
          </div>
        ) : null}
        {open && step.expect ? <div className="bh-expect">Expect: {step.expect}</div> : null}
        {open && (ev.why || ev.note) ? (
          <div className="bh-evidence" title={entry?.by === "human" ? "What you recorded" : "What the agent recorded"}>
            <span className="bh-evidence-by">{ev.why ? "Why it's blocked:" : entry?.by === "human" ? "Your note:" : "The agent:"}</span> {ev.why || ev.note}
          </div>
        ) : null}
        {open && ev.agentNote ? (
          <div className="bh-evidence bh-agent-note" title="What the agent recorded before you answered">
            <span className="bh-evidence-by">The agent had said:</span> {ev.agentNote}
          </div>
        ) : null}
        {open && (entry?.result || (chip.state === "fail" && onFix)) ? (
          <div className="bh-row-actions">
            {entry?.result ? (
              <span className="bh-receipt">
                {chip.label} · by {ev.by}
                {entry.at ? " · " + relTime(entry.at, { suffix: " old" }) : ""}
              </span>
            ) : null}
            {chip.state === "fail" && onFix ? (
              <button type="button" className="tb-item bh-quiet" title={AGENT_FIX.title} onClick={() => onFix(step)}>
                {AGENT_FIX.label}
              </button>
            ) : null}
          </div>
        ) : null}
      </div>
    </li>
  );
}
