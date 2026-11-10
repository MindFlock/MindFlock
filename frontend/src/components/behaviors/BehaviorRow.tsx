/** One step on the Behaviors card: its result chip, its text (+ what should
 * happen), who does it (🤖 agent / 🙋 you) and what the agent recorded.
 * READ-ONLY — the steps are the agent's own (owner request 2026-11-10): a
 * person's part is the card's one Confirm verified, never a per-step answer
 * or edit. A failed step offers Fix (a new session with the failure). */

import { useEffect, useState, type JSX } from "react";
import type { TestPlan, TestStep } from "../../api/types";
import { stepResult } from "../dialogs/verify";
import { relTime } from "../../lib/format";
import { byWords, rowChip, whoOf } from "./cardModel";

export interface BehaviorRowProps {
  plan: TestPlan;
  step: TestStep;
  index: number;
  /** Open a session to fix this failed step. */
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

  const entry = stepResult(plan, step.id);
  const chip = rowChip(plan, step.id);
  const who = whoOf(plan, step);
  return (
    <li
      className={"bh-row is-" + chip.state + (who.you ? " is-yours" : "")}
      data-step-id={step.id}
      aria-label={"Step " + (index + 1) + ": " + step.text + " — " + chip.label}
    >
      <span className="bh-chip" data-check={chip.state} title={chip.label}>
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
        {open && step.expect ? <div className="bh-expect">Expect: {step.expect}</div> : null}
        {open && entry?.note ? (
          <div className="bh-evidence" title={entry.by === "human" ? "What a person recorded" : "What the agent recorded"}>
            <span className="bh-evidence-by">{entry.by === "human" ? "Your note:" : "The agent:"}</span> {entry.note}
          </div>
        ) : null}
        {open && (entry?.result || (chip.state === "fail" && onFix)) ? (
          <div className="bh-row-actions">
            {entry?.result ? (
              <span className="bh-receipt">
                {byWords(entry.by)}
                {entry.at ? " · " + relTime(entry.at, { suffix: " old" }) : ""}
              </span>
            ) : null}
            {chip.state === "fail" && onFix ? (
              <button type="button" className="tb-item bh-quiet" title="Starts a new session with this failure attached" onClick={() => onFix(step)}>
                Fix
              </button>
            ) : null}
          </div>
        ) : null}
      </div>
    </li>
  );
}
