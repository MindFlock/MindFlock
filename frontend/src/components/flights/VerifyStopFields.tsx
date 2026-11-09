/** The Verify stop's editor (docs/flights.md "Verify stops"): a three-line
 * explainer, then the stop's four plain choices — Where (this branch's code
 * or one of the repos' environments, "Other…" typed), Ready when (+ its
 * minutes), Who checks (three choices, each with what it means), Notes — and
 * Post the results to the PR. `StopForm` draws it in place of the generic
 * fields when the kind serves `where`; the logic is lib/verifyStop.ts.
 *
 * `VerifyStopBehaviors` shows the stop's checklist, when it has one, each
 * behavior marked agent or you (the Behaviors card's own rules: cardModel's
 * `whoOf` / `rowChip`) — so the person sees which ones will need them. It
 * answers nothing: the card does (the dialog's "Behaviors" button). */

import { useState, type ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../../api/client";
import type { TestPlansResponse } from "../../api/types";
import { optionLabel, type StepKindDef, type StepKindField } from "../../lib/flights";
import { queryClient, useVerifyEnvironments } from "../../state/queries";
import {
  READY_MINUTES_HINT,
  VERIFY_NOTES_MAX,
  WHERE_BRANCH,
  WHO_CHOICES,
  envTitle,
  readyChoices,
  showsReadyMinutes,
  verifyConfig,
  whereOptions,
  withVerifyChoice,
  type VerifyConfig,
  type VerifyEnvsResponse,
} from "../../lib/verifyStop";
import { rowChip, whoOf } from "../behaviors/cardModel";
import { ChipRow, FlField, NumberField, PromptBox, Seg, TextField, Toggle } from "./controls";

/** The fields this editor draws itself; any other field the kind serves is
 * left to the generic `StopField`. */
export const VERIFY_OWN_FIELDS: readonly string[] = ["where", "ready", "ready_minutes", "who", "notes", "post_to_pr", "wait_live"];

/** Whether a kind is the new Verify (it serves `where`); an older server's
 * Verify (who + wait_live) keeps the generic form. */
export const isVerifyEditor = (kind: string, def: Pick<StepKindDef, "fields"> | undefined): boolean =>
  kind === "verify" && !!def?.fields?.some((f) => f.name === "where");

const OTHER = "\u0000other";

/** The explainer's three lines, in words for `where` and `who`. */
export function verifySteps(v: Pick<VerifyConfig, "where" | "who">): [string, string, string] {
  const on = v.where === WHERE_BRANCH ? "this branch's code" : v.where;
  const second = v.who === "you" ? "you check every one on " + on : "the agent checks its ones on " + on;
  const third = v.who === "agent" ? "yours stay open on the checklist — the route goes on" : v.who === "you" ? "they park the route here until you answer" : "yours park the route here (a perch)";
  return ["behaviors are written from the branch's changes", second, third];
}

export function VerifyExplainer({ v }: { v: Pick<VerifyConfig, "where" | "who"> }) {
  const lines = verifySteps(v);
  return (
    <ol className="vf-explain fl-fld-wide" aria-label="What this stop does">
      {lines.map((t, i) => (
        <li key={i}>
          <span className="vf-explain-n" aria-hidden="true">
            {"①②③"[i]}
          </span>
          <span>{t}</span>
        </li>
      ))}
    </ol>
  );
}

export function VerifyStopFields({
  config,
  def,
  readOnly,
  onChange,
  renderField,
}: {
  config: Record<string, unknown> | undefined;
  def: StepKindDef | undefined;
  readOnly?: boolean;
  onChange(config: Record<string, unknown>): void;
  /** The generic renderer, for a field this editor doesn't know. */
  renderField(f: StepKindField): ReactNode;
}) {
  const v = verifyConfig(config);
  const envs = useVerifyEnvironments(!readOnly);
  const set = (patch: Partial<VerifyConfig>) => !readOnly && onChange(withVerifyChoice(config, patch));
  const fieldOf = (name: string) => def?.fields.find((f) => f.name === name);
  const label = (name: string, d: string) => fieldOf(name)?.label || d;
  const extra = (def?.fields || []).filter((f) => !VERIFY_OWN_FIELDS.includes(f.name));
  const minutes = showsReadyMinutes(v.ready);
  return (
    <>
      <VerifyExplainer v={v} />
      <WherePicker label={label("where", "Where")} value={v.where} envs={envs.isError ? null : envs.data || null} failed={envs.isError} readOnly={readOnly} onChange={(where) => set({ where })} />
      <FlField label={label("ready", "Ready when")}>
        <Seg
          label={label("ready", "Ready when")}
          value={v.ready}
          options={readyChoices(v.where).map((o) => ({ v: o.value, label: optionLabel(fieldOf("ready"), o.value, o.label), disabled: readOnly }))}
          onChange={(ready) => set({ ready })}
        />
      </FlField>
      {minutes && (
        <FlField label={label("ready_minutes", "Minutes")} hint={v.where === WHERE_BRANCH ? "0 = right after it" : READY_MINUTES_HINT}>
          <NumberField label={label("ready_minutes", "Minutes")} unit="min" value={v.ready_minutes} onChange={(x) => set({ ready_minutes: x === "" ? 0 : Math.min(1440, x) })} />
        </FlField>
      )}
      <FlField label={label("who", "Who checks")} wide>
        <div className="vf-who" role="radiogroup" aria-label={label("who", "Who checks")}>
          {WHO_CHOICES.map((c) => (
            <button key={c.value} type="button" role="radio" aria-checked={v.who === c.value} className={"vf-who-opt" + (v.who === c.value ? " on" : "")} data-value={c.value} onClick={() => set({ who: c.value })}>
              <strong>{optionLabel(fieldOf("who"), c.value, c.label)}</strong>
              <span className="muted">{c.explain}</span>
            </button>
          ))}
        </div>
      </FlField>
      <FlField label={label("notes", "Notes for this check")} wide hint="For this stop only — on top of the repo's standing notes.">
        <PromptBox
          label={label("notes", "Notes for this check")}
          rows={2}
          value={v.notes}
          placeholder="e.g. Log in as the QA user; check the export button too."
          onChange={(t) => set({ notes: t.slice(0, VERIFY_NOTES_MAX) })}
        />
      </FlField>
      <div className="fl-fld">
        <Toggle on={v.post_to_pr} label={label("post_to_pr", "Post the results to the PR")} title="When the stop finishes, its results go on the session's PR as a comment" onChange={(x) => set({ post_to_pr: x })} />
      </div>
      {extra.map((f) => renderField(f))}
    </>
  );
}

/** Where: "This branch's code", each environment name, "Other…" (typed). An
 * older server without the environments list: a plain text field. */
function WherePicker({
  label,
  value,
  envs,
  failed,
  readOnly,
  onChange,
}: {
  label: string;
  value: string;
  envs: VerifyEnvsResponse | null;
  failed: boolean;
  readOnly?: boolean;
  onChange(v: string): void;
}) {
  const [typing, setTyping] = useState(false);
  if (failed)
    return (
      <FlField label={label} wide hint="“branch” = this branch's own code, or an environment's name">
        <TextField label={label} value={value} placeholder="branch or an environment name" onChange={(x) => !readOnly && onChange(x.trim())} />
      </FlField>
    );
  const opts = whereOptions(envs, value);
  const shown = typing ? OTHER : value;
  const hint =
    value === WHERE_BRANCH
      ? "A fresh checkout of this branch — nothing deployed."
      : envTitle(value, envs) || (envs ? "No watched repo has this environment yet — add it on the repo's Verify card (Intake)." : "");
  return (
    <FlField label={label} wide hint={hint ? <span className="vf-where-hint">{hint}</span> : undefined}>
      <ChipRow
        label={label}
        value={shown}
        options={[...opts.map((o) => ({ value: o.value, label: o.label, title: o.title })), { value: OTHER, label: "Other…", title: "Type an environment's name" }]}
        onChange={(x) => {
          if (readOnly) return;
          if (x === OTHER) return setTyping(true);
          setTyping(false);
          onChange(x);
        }}
      />
      {typing && (
        <TextField label={label + " — another environment"} value={value === WHERE_BRANCH ? "" : value} placeholder="an environment's name" onChange={(x) => !readOnly && onChange(x.trim() || WHERE_BRANCH)} />
      )}
    </FlField>
  );
}

/** The stop's checklist, when it has one: each behavior with who checks it
 * and where it stands. Read from the Behaviors list the top bar keeps warm. */
export function VerifyStopBehaviors({ planId }: { planId: string }) {
  const q = useQuery({ queryKey: ["test-plans"], queryFn: () => api<TestPlansResponse>("/api/test-plans"), enabled: false }, queryClient);
  const plan = planId ? (q.data?.plans || []).find((p) => p.id === planId) : undefined;
  if (!plan || !(plan.steps || []).length) return null;
  const steps = plan.steps;
  const yours = steps.filter((s) => whoOf(plan, s).you).length;
  return (
    <section className="vf-behaviors" aria-label="This stop's behaviors">
      <span className="fl-fld-label">
        Behaviors — {steps.length} · {yours ? yours + " yours" : "none yours"}
      </span>
      <ul>
        {steps.map((s) => {
          const who = whoOf(plan, s);
          const chip = rowChip(plan, s.id);
          return (
            <li key={s.id} className={"vf-behavior" + (who.you ? " vf-yours" : "")} data-check={chip.state}>
              <span className="vf-behavior-mark" title={chip.label} aria-label={chip.label}>
                {chip.glyph}
              </span>
              <span className="vf-behavior-text">{s.text}</span>
              <span className="vf-behavior-who" title={who.title}>
                {who.label}
              </span>
            </li>
          );
        })}
      </ul>
    </section>
  );
}
