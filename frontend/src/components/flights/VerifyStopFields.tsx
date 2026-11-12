/** The Verify stop's editor (docs/flights.md "Verify stops"): a three-line
 * explainer of the current design (its agent writes and runs its own steps;
 * where and when; what makes the route wait on you), then the stop's plain
 * choices — Where (this branch's code or one of the repos' environments: a
 * pick from the list, never free text while the list is known), Ready when
 * (+ its minutes), Who checks (three choices, each with its effect), Notes —
 * and Post the results to the PR. `StopForm` draws it in place of the
 * generic fields when the kind serves `where`, with the stop's sentence
 * (`verifySentence`) above its title; the logic is lib/verifyStop.ts.
 *
 * `VerifyStopLive` shows the stop's live Verify, when it has one: its state,
 * how far the agent got, and Steps to the session's Verify. */

import type { ReactNode } from "react";
import type { SessionVerify } from "../../api/types";
import { openBehaviors } from "../../lib/behaviorsFocus";
import { optionLabel, type StepKindDef, type StepKindField } from "../../lib/flights";
import { verifyGlyph, verifyProgress, verifyWord } from "../../lib/sessionState";
import { useVerifyEnvironments } from "../../state/queries";
import {
  READY_MINUTES_HINT,
  READY_MINUTES_MAX,
  VERIFY_NOTES_MAX,
  WHERE_BRANCH,
  WHO_CHOICES,
  envTitle,
  readyChoices,
  showsReadyMinutes,
  verifyConfig,
  verifyWhenWords,
  whereOptions,
  withVerifyChoice,
  type VerifyConfig,
  type VerifyEnvsResponse,
} from "../../lib/verifyStop";
import { ChipRow, FlField, NumberField, PromptBox, Seg, TextField, Toggle } from "./controls";

/** The fields this editor draws itself; any other field the kind serves is
 * left to the generic `StopField`. */
export const VERIFY_OWN_FIELDS: readonly string[] = ["where", "ready", "ready_minutes", "who", "notes", "post_to_pr", "wait_live"];

/** Whether a kind is the new Verify (it serves `where`); an older server's
 * Verify (who + wait_live) keeps the generic form. */
export const isVerifyEditor = (kind: string, def: Pick<StepKindDef, "fields"> | undefined): boolean =>
  kind === "verify" && !!def?.fields?.some((f) => f.name === "where");

/** The explainer's three lines: ① what its agent does, ② where it runs and
 * when it starts, ③ what makes the route wait on you and what lets it go on. */
export function verifySteps(v: Pick<VerifyConfig, "where" | "who"> & Partial<Pick<VerifyConfig, "ready" | "ready_minutes">>, envs?: VerifyEnvsResponse | null): [string, string, string] {
  const when = verifyWhenWords({ where: v.where, ready: v.ready || "now", ready_minutes: v.ready_minutes || 0 }, envs);
  const second =
    v.where === WHERE_BRANCH
      ? "It runs on this branch's code — a fresh checkout, nothing deployed — " + when + "."
      : "It runs against " + v.where + " — the deployed environment of that name in this repo's Verify settings — " + when + ".";
  const third =
    v.who === "agent"
      ? "The route goes on by itself when every step passes; it waits on you only if something ends blocked or failed."
      : v.who === "you"
        ? "The route always waits for you to confirm, even when every step passes."
        : "The route waits on you for the steps only a person can do; when every step passes it goes on.";
  return ["Its agent comes up with its own steps from what it built, runs them, and shows evidence for each.", second, third];
}

export function VerifyExplainer({ v, envs }: { v: Pick<VerifyConfig, "where" | "who"> & Partial<Pick<VerifyConfig, "ready" | "ready_minutes">>; envs?: VerifyEnvsResponse | null }) {
  const lines = verifySteps(v, envs);
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
  // "When it needs you" (`ask`: Wait here / Carry on, ask me at the end)
  // sits under Who checks — the server's field, drawn the generic way.
  const askField = fieldOf("ask");
  const extra = (def?.fields || []).filter((f) => !VERIFY_OWN_FIELDS.includes(f.name) && f !== askField);
  const minutes = showsReadyMinutes(v.ready);
  const envList = envs.isError ? null : envs.data || null;
  return (
    <>
      <VerifyExplainer v={v} envs={envList} />
      <WherePicker label={label("where", "Where")} value={v.where} envs={envList} failed={envs.isError} readOnly={readOnly} onChange={(where) => set({ where })} />
      <FlField label={label("ready", "Ready when")}>
        <Seg
          label={label("ready", "Ready when")}
          value={v.ready}
          options={readyChoices(v.where).map((o) => ({ v: o.value, label: optionLabel(fieldOf("ready"), o.value, o.label), disabled: readOnly }))}
          onChange={(ready) => set({ ready })}
        />
      </FlField>
      {minutes && (
        <FlField label={label("ready_minutes", "Minutes")} hint={(v.where === WHERE_BRANCH ? "0 = right after it" : READY_MINUTES_HINT) + " · 0 to " + READY_MINUTES_MAX + " min (24 h)"}>
          <NumberField label={label("ready_minutes", "Minutes")} unit="min" value={v.ready_minutes} onChange={(x) => set({ ready_minutes: x === "" ? 0 : Math.min(READY_MINUTES_MAX, x) })} />
        </FlField>
      )}
      <FlField label={label("who", "Who checks")} wide>
        <div className="vf-who" role="radiogroup" aria-label={label("who", "Who checks")}>
          {WHO_CHOICES.map((c) => (
            <button key={c.value} type="button" role="radio" aria-checked={v.who === c.value} className={"vf-who-opt" + (v.who === c.value ? " on" : "")} data-value={c.value} onClick={() => set({ who: c.value })}>
              <strong>{c.label}</strong>
              <span className="muted">{c.explain}</span>
            </button>
          ))}
        </div>
      </FlField>
      {askField && renderField(askField)}
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
        <span className="fl-fld-hint">Off: the results stay in MindFlock. On: when the stop finishes they also go on the session's PR as a comment (needs a PR).</span>
      </div>
      {extra.map((f) => renderField(f))}
    </>
  );
}

/** Where: "This branch's code", then each environment the repo's Verify
 * settings list — a pick, never free text, so an unknown name can't be
 * chosen. A saved name the list no longer has stays visible, flagged, until
 * it is changed. Without the list (an older server, a failed fetch): a plain
 * text field that says the name must match. */
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
  if (failed)
    return (
      <FlField label={label} wide hint={WHERE_TYPED_HINT}>
        <TextField label={label} value={value} placeholder="branch or an environment name" onChange={(x) => !readOnly && onChange(x.trim())} />
      </FlField>
    );
  const opts = whereOptions(envs, value);
  const unknown = !!envs && opts.some((o) => o.custom && o.value === value);
  const hint = whereHint(value, envs, unknown);
  return (
    <FlField label={label} wide hint={hint ? <span className={"vf-where-hint" + (unknown ? " vf-where-unknown" : "")}>{hint}</span> : undefined}>
      <ChipRow label={label} value={value} options={opts.map((o) => ({ value: o.value, label: o.custom ? o.label + " (unknown)" : o.label, title: o.title }))} onChange={(x) => !readOnly && onChange(x)} />
    </FlField>
  );
}

/** The free-text Where's hint (no environments list to pick from). */
export const WHERE_TYPED_HINT = "“branch” = this branch's own code; any other name must match an environment in this repo's Verify settings";

/** What the picked Where means. */
export function whereHint(value: string, envs: VerifyEnvsResponse | null, unknown: boolean): string {
  if (value === WHERE_BRANCH) return "A fresh checkout of this branch — nothing deployed. Pick an environment to check a deployed copy instead.";
  if (unknown) return "“" + value + "” is not an environment of any watched repo — pick one above, or add it on the repo's Verify card (Intake). The stop fails while it is unknown.";
  const t = envTitle(value, envs);
  return t ? "The deployed copy: " + t : "";
}

/** The session's live Verify, when it is this stop's: the plan the stop
 * recorded (its step detail's `plan`) is the session's, or — nothing
 * recorded yet — the stop is the one the route is at. Null otherwise (a
 * Verify stop still ahead, or one whose run was another). */
export function stopVerifyOf(v: SessionVerify | null | undefined, stopPlan: string, here: boolean): SessionVerify | null {
  if (!v || typeof v.state !== "string") return null;
  if (stopPlan) return v.plan === stopPlan ? v : null;
  return here ? v : null;
}

/** A Verify stop's live Verify inside its dialog (self-verify has no
 * pre-written list): the state word, how far its agent got ("3 of 5 steps
 * done"), and Steps — the session's Verify, its steps and their evidence.
 * Nothing when the stop has no Verify yet. */
export function VerifyStopLive({ verify, session }: { verify: SessionVerify | null | undefined; session: string }) {
  if (!verify) return null;
  const word = verifyWord(verify.state);
  if (!word) return null;
  return (
    <section className="vf-live" aria-label="This stop's Verify" data-verify={verify.state}>
      <span className="fl-fld-label">Verify</span>
      <p className="vf-live-row">
        <strong className={"vf-live-word is-" + verify.state}>
          <span aria-hidden="true">{verifyGlyph(verify.state)}</span> {word}
        </strong>
        <span className="muted">{verifyProgress(verify)}</span>
        <button type="button" className="test-btn" title="The steps and their evidence" onClick={() => openBehaviors({ planId: verify.plan, title: session })}>
          Steps
        </button>
      </p>
    </section>
  );
}
