/** A stop's dialog — THE way to edit a box on a session's line (round 11:
 * it replaces the inline inspector). ✎ on the hover toolbar, Enter / Space
 * on a focused box, a double-click and the right-click menu's Edit open it
 * (`FlywayCanvas`'s `onEdit`; `useSessionLine` wires it).
 *
 *  - A STOP: its kind (icon and name, read-only), its title, its kind's
 *    fields (StopParts' `StopForm`, the inspector's own renderer), what
 *    happens if it fails and its halt point; Save (PATCH the stop), Cancel,
 *    Duplicate and Remove. A stop that can't change — it has flown, or the
 *    flight is at it — opens read-only with its state and note, and only
 *    what it can still do: Remove, and on the current one Done / Retry /
 *    Carry on / Skip and its halt point (set at once).
 *  - Flow polish: a stop that is HALTED, waits on you or where the route is
 *    paused leads with why (the chip and the reason, in plain words) and its
 *    remedies right under it — the one that usually works primary — and
 *    folds its settings into "Its settings". The current stop can change
 *    when it failed or the route waits at its halt point (`saveHint`).
 *    Save checks the cheap things first (`clientCheck`) and shows a refusal
 *    (`SaveResult`) under the field it names — never a card; Esc, the
 *    backdrop and Cancel ask before dropping changed settings; a stop the
 *    route reached mid-edit keeps the draft on screen (`SAVE_LOST`).
 *  - A GROUP (a stop with sub-steps): its name (renamed with Save), its
 *    sub-steps — the kind's icon, the title, the state, ✎ opens that
 *    sub-step's own dialog, 🗑 removes it — and Ungroup, Save to the shelf,
 *    Remove (the whole group).
 *
 * Esc and a press on the backdrop close it (Modal). No window.confirm /
 * prompt / alert anywhere: the desktop app has none. */

import { useState, type ReactNode } from "react";
import { isSaveResult } from "../../lib/flightsApi";
import { LOOP_AGENT, loopsToAgent, type DiagramNode } from "../../lib/flightDiagram";
import type { SessionVerify } from "../../api/types";
import type { FlightStep, StepKindDef } from "../../lib/flights";
import { InlineConfirm } from "../InlineConfirm";
import { Modal } from "../Modal";
import { StopIcon } from "./Bird";
import { FlField, PromptBox, TextField, Toggle } from "./controls";
import { StopForm, formFieldNames, kindName } from "./StopParts";
import { VerifyStopLive } from "./VerifyStopFields";
import "./StopDialog.css";

/** A box's state in words, for the dialog's read-only line and its rows. */
export const STOP_STATE_WORD: Partial<Record<DiagramNode["state"], string>> = {
  done: "Done",
  skipped: "Skipped",
  failed: "Failed",
  halted: "Halted",
  current: "Under way",
  perched: "Needs you",
  paused: "Paused here",
  pending: "Still to come",
  edit: "Not run yet",
};

/** The halt point's toggle (its words are the vocabulary's: a halt point
 * PAUSES the route; Carry on lets it through). */
export const HALT_LABEL = "Halt point — the route pauses before this stop until you press Carry on";

/** What a stop's Save sends: only what a stop IS. */
export type StopPatch = Partial<Pick<FlightStep, "title" | "config" | "on_fail" | "halt" | "when">>;

export interface StopDialogProps {
  /** The stop or the group box (the line's own node, flight indexes). */
  node: DiagramNode;
  /** A stop's kind (the inspector's `inspectorDef`). */
  def?: StepKindDef;
  /** Every kind, for a group's rows. */
  kindOf?: ReadonlyMap<string, StepKindDef>;
  busy?: boolean;
  /** Why it can't change, and what the flight noted on it (read-only). */
  note?: string;
  /** A Verify stop's live Verify (the session row's `verify`, when it is
   * this stop's — VerifyStopFields `stopVerifyOf`): its state, how far the
   * agent got, and Steps. Nothing shows without one. */
  verify?: SessionVerify | null;
  /** The session the Verify runs in (Steps opens its Verify). */
  verifySession?: string;
  /** More under the form (a Split's hint, its "+ Join"). */
  extra?: ReactNode;
  /** The line's steps in flight order: a `dynamic: "stops_before"` field
   * (a Loop's "Repeat from") lists the stops before this one. */
  lineSteps?: readonly FlightStep[] | null;
  /** Round 21: the line has a session — a Loop may go back to its AGENT
   * (`back_to: "agent"`); the dialog offers it. */
  loopAgent?: boolean;
  /** Render into document.body (the Sky zooms its lines with a transform,
   * which would carry a fixed backdrop with it). Default true. */
  portal?: boolean;
  onClose(): void;
  /** A stop that can change: Save. Without it the stop is read-only. It may
   * resolve to a `SaveResult`: a refusal stays open with the server's
   * sentence under the field it names; `ok` clears the edits (the dialog
   * then shows the saved stop). */
  onSave?(patch: StopPatch): void | Promise<unknown>;
  /** The current stop's settings can change right now (it failed, or the
   * route waits at its halt point): "Save, then Retry runs it…". */
  saveHint?: string;
  onDuplicate?(): void;
  /** The one remove path (a stop, or the whole group). */
  onRemove?(): void;
  /** A read-only stop's halt point, set at once (the current stop's). */
  onToggleHalt?(): void;
  /** The current stop's own: Done (a perch), Retry (halted), Skip. */
  onDone?(): void;
  /** Done's own words (a Verify stop waiting on your sign-off: "Confirm
   * verified"). */
  doneLabel?: string;
  doneTitle?: string;
  onRetry?(): void;
  /** Carry on: the route is paused (a person's pause or a halt point). */
  onResume?(): void;
  /** The remedy that usually works, drawn first as the primary button:
   * Retry for a failure, Carry on for a pause, Done for a sign-off. */
  primary?: "done" | "retry" | "resume";
  /** What the stop asks of the person (a Verify sign-off's steps to check,
   * a blocked check's way out), listed under its reason. */
  asks?: readonly string[];
  /** A halted Verify stop: Let the agent fix it. */
  onFix?(): void;
  onSkip?(): void;
  /** Group: rename it (Save), its rows' ✎ and 🗑, Ungroup, Save to the shelf. */
  onRename?(name: string): void | Promise<unknown>;
  onOpenSub?(n: DiagramNode): void;
  onRemoveSub?(n: DiagramNode): void;
  onUngroup?(): void;
  onSaveShelf?(name: string): void | Promise<unknown>;
}

const copyStep = (s: FlightStep): FlightStep => ({ ...s, config: { ...s.config } });

/** Round 21: the fields a Loop's form draws itself when it goes back to the
 * agent — "Back to: the agent" in place of the stops list, and its prompt
 * with its own hint (docs/flights-loop-agent-sky.md). */
export const LOOP_AGENT_OWN_FIELDS: readonly string[] = ["back_to", "prompt"];

/** The kind's fields for the form: a Loop back to the agent leaves its
 * `back_to` and `prompt` to the dialog (`LOOP_AGENT_OWN_FIELDS`). */
export function loopFormDef(def: StepKindDef | undefined, step: Pick<FlightStep, "kind" | "config">): StepKindDef | undefined {
  if (!def || !loopsToAgent(step)) return def;
  return { ...def, fields: def.fields.filter((f) => !LOOP_AGENT_OWN_FIELDS.includes(f.name)) };
}

/** What a Loop back to the agent sends it each run: its `prompt`, "" = the
 * prompt that started the agent's turn again. */
export const LOOP_PROMPT_HINT = "Empty = send the same prompt again";

export function StopDialog(p: StopDialogProps) {
  return p.node.kind === "group" ? <GroupDialog {...p} /> : <StepDialog {...p} />;
}

function Shell({ p, title, children, onClose, onEscape }: { p: StopDialogProps; title: string; children: ReactNode; onClose?(): void; onEscape?(e: KeyboardEvent): boolean | void }) {
  return (
    <Modal id="stop-dialog" className="stop-dialog-modal" onClose={onClose || p.onClose} onEscape={onEscape} portal={p.portal !== false} role="dialog" aria-modal="true" aria-label={title}>
      <div className="stop-dialog" onKeyDown={(e) => e.key === "Escape" || e.stopPropagation()}>
        {children}
      </div>
    </Modal>
  );
}

/** The current stop's remedies, in the order the dialog draws them: the one
 * that usually works (`primary`) first, then Retry · Carry on · Done · Let
 * the agent fix it · Skip — each only when its handler is given. */
export type RemedyKey = "done" | "retry" | "resume" | "fix" | "skip";
export interface Remedy {
  key: RemedyKey;
  label: string;
  title: string;
  primary: boolean;
}
const REMEDY_ORDER: readonly RemedyKey[] = ["retry", "resume", "done", "fix", "skip"];
export function remedies(p: Pick<StopDialogProps, "onDone" | "onRetry" | "onResume" | "onFix" | "onSkip" | "primary" | "doneLabel" | "doneTitle">): Remedy[] {
  const has: Record<RemedyKey, boolean> = { done: !!p.onDone, retry: !!p.onRetry, resume: !!p.onResume, fix: !!p.onFix, skip: !!p.onSkip };
  const words: Record<RemedyKey, [string, string]> = {
    retry: ["Retry", "Run this stop again"],
    resume: ["Carry on", "The route goes on from here"],
    done: [p.doneLabel || "Done", p.doneTitle || "You've done it — the route goes on"],
    fix: ["Let the agent fix it", "The agent gets the failure, then the stop runs again"],
    skip: ["Skip", "Skip this stop — the route goes on without it"],
  };
  const lead = p.primary && has[p.primary] ? p.primary : null;
  const keys = [...(lead ? [lead] : []), ...REMEDY_ORDER.filter((k) => k !== lead)].filter((k) => has[k]);
  return keys.map((k) => ({ key: k, label: words[k][0], title: words[k][1], primary: k === lead }));
}

/** The last name in a refusal's field path: `steps[2].config.timeout_minutes`
 * → `timeout_minutes`, `step.title` → `title`, `steps[1]` → "". */
export function fieldNameOf(path: string): string {
  const last = (path || "").split(".").pop() || "";
  return /^(steps?)(\[\d+\])?$/.test(last) ? "" : last.replace(/\[\d+\]$/, "");
}

/** The fields every stop's form draws itself (StopParts' `StopForm`). */
const FORM_LABELS: Record<string, string> = { title: "Title", on_fail: "If it fails", when: "Only when", halt: "Halt point" };

/** The label a person sees for a refusal's field (its kind's `def.fields`,
 * else the form's own), "" when the path names none we draw. */
export function fieldLabelOf(path: string, def: StepKindDef | undefined): string {
  const name = fieldNameOf(path);
  if (!name) return "";
  return def?.fields.find((f) => f.name === name)?.label || FORM_LABELS[name] || "";
}

/** What a refused (or not-yet-sendable) save says, and where. */
export interface FieldProblem {
  field: string;
  detail: string;
}

/** The cheap checks before Save — the server checks the rest and its
 * sentence shows the same way. */
export function clientCheck(step: Pick<FlightStep, "kind" | "config"> | null | undefined): FieldProblem | null {
  if (step?.kind === "command" && !String(step.config?.command ?? "").trim()) return { field: "command", detail: "Needs a command" };
  return null;
}

/** An Agent stop with no prompt is fine: it says what that does. */
export const AGENT_EMPTY_HINT = "Empty = the agent carries on with its task";
export function agentPromptEmpty(step: Pick<FlightStep, "kind" | "config"> | null | undefined): boolean {
  return step?.kind === "agent" && !String(step.config?.prompt ?? "").trim();
}

/** When the route reaches a stop while its dialog has unsaved edits. */
export const SAVE_LOST = "The route reached this stop — your changes can't be saved now";

function Problem({ problem, def }: { problem: FieldProblem; def: StepKindDef | undefined }) {
  const label = fieldLabelOf(problem.field, def);
  return (
    <p className="fl-note stop-dialog-error" role="alert" data-field={fieldNameOf(problem.field) || undefined}>
      {label && <strong>{label}: </strong>}
      {problem.detail}
    </p>
  );
}

function StepDialog(p: StopDialogProps) {
  const n = p.node;
  // The person's edits (null: none yet — the dialog shows the stop as the
  // server has it, so a refetch shows through until they type).
  const [draft, setDraft] = useState<FlightStep | null>(null);
  const [refused, setRefused] = useState<FieldProblem | null>(null);
  const [asking, setAsking] = useState(false);
  const [hadSave] = useState(() => !!p.onSave);
  const shown = draft || n.step || null;
  const editable = !!p.onSave && !!shown;
  const dirty = !!draft && !!n.step && JSON.stringify(draft) !== JSON.stringify(n.step);
  // The route reached the stop mid-edit: the draft stays (read-only, to copy).
  const lost = hadSave && !p.onSave && dirty;
  const change = (f: (d: FlightStep) => FlightStep) => {
    if (!editable) return;
    setRefused(null);
    setDraft((d) => (d || n.step ? f(d || copyStep(n.step!)) : d));
  };
  const perch = (p.def?.actor || (n.step?.kind === "you" ? "you" : "robot")) === "you";
  const kind = n.step?.kind || n.icon;
  const title = (editable ? "Edit “" : "“") + (n.step?.title || n.title) + "”";
  const state = STOP_STATE_WORD[n.state];
  const acts = remedies(p);
  // Halted, waiting on you, paused: why and what to do lead; the settings fold.
  const parked = acts.length > 0 && (n.state === "halted" || n.state === "perched" || n.state === "paused");
  const check = editable ? clientCheck(shown) : null;
  const problem = refused || check;
  // A refusal for a field the form draws goes under that field, in red; any
  // other (a whole-stop refusal, a field drawn elsewhere) above the form.
  const formDef = shown ? loopFormDef(p.def, shown) : p.def;
  const problemName = problem ? fieldNameOf(problem.field) : "";
  const inForm = !!problem && !!shown && !!problemName && formFieldNames(shown, formDef).has(problemName);
  /** Save; true when it went through (a caller that resolves to nothing
   * closes the dialog itself). */
  const save = async (): Promise<boolean> => {
    if (!draft || !p.onSave) return true;
    if (check) {
      setRefused(check);
      return false;
    }
    const patch: StopPatch = { title: draft.title, config: draft.config, on_fail: draft.on_fail };
    if (!!draft.halt !== !!n.step?.halt) patch.halt = !!draft.halt;
    // "Only when" (round 15): sent whenever it differs; "" clears it.
    if ((draft.when || "") !== (n.step?.when || "")) patch.when = draft.when || "";
    const r = await p.onSave(patch);
    if (isSaveResult(r)) {
      if (!r.ok) {
        setRefused({ field: r.field, detail: r.detail || "The server didn't take it" });
        return false;
      }
      setDraft(null);
      setRefused(null);
    }
    return true;
  };
  // Retry / Carry on / the agent's fix run the stop: unsaved edits go first.
  const run = (k: RemedyKey) => {
    const go = { done: p.onDone, retry: p.onRetry, resume: p.onResume, fix: p.onFix, skip: p.onSkip }[k];
    if (!go) return;
    if (editable && dirty && (k === "retry" || k === "resume" || k === "fix")) void save().then((ok) => ok && go());
    else go();
  };
  const close = () => (dirty ? setAsking(true) : p.onClose());
  const remedyButtons = acts.map((a) =>
    a.primary ? (
      <button key={a.key} type="button" className="btn-primary" data-remedy={a.key} disabled={p.busy} title={a.title} onClick={() => run(a.key)}>
        {a.label}
      </button>
    ) : (
      <button key={a.key} type="button" className="test-btn" data-remedy={a.key} disabled={p.busy} title={a.title} onClick={() => run(a.key)}>
        {a.label}
      </button>
    )
  );
  const settings = (
    <>
      {shown && loopsToAgent(shown) && (
        <>
          <FlField label="Back to" wide hint="After this stop the agent is prompted again; once its turn is done the route runs again from its first stop.">
            <div className="fl-chiprow stop-dialog-loop-agent" data-loop-agent="">
              <strong>the agent</strong>
              {editable && (p.lineSteps?.length || 0) > 1 && (
                <button type="button" className="test-btn" onClick={() => change((d) => ({ ...d, config: { ...d.config, back_to: "" } }))}>
                  Go back to a stop instead
                </button>
              )}
            </div>
          </FlField>
          <FlField label="Prompt" wide hint={LOOP_PROMPT_HINT}>
            {editable ? (
              <PromptBox
                label="Prompt for the agent each run"
                rows={3}
                value={String(shown.config?.prompt ?? "")}
                placeholder="The prompt that started the agent's turn"
                onChange={(t) => change((d) => ({ ...d, config: { ...d.config, prompt: t } }))}
              />
            ) : (
              <span className="muted">{String(shown.config?.prompt ?? "").trim() || "the same prompt again"}</span>
            )}
          </FlField>
        </>
      )}
      {shown && editable && p.loopAgent && shown.kind === "loop" && !loopsToAgent(shown) && (
        <div className="fl-chiprow">
          <button type="button" className="test-btn" title="The agent is prompted again after this stop, then the route runs again" onClick={() => change((d) => ({ ...d, config: { ...d.config, back_to: LOOP_AGENT } }))}>
            ↺ Loop back to the agent instead
          </button>
        </div>
      )}
      {shown ? (
        <StopForm
          step={shown}
          def={formDef}
          lineSteps={p.lineSteps}
          readOnly={!editable}
          errors={inForm ? { [problemName]: problem!.detail } : null}
          onChange={(patch) => change((d) => ({ ...d, ...patch }))}
        />
      ) : (
        <p className="fl-note muted">This stop's details haven't loaded yet.</p>
      )}
      {editable && agentPromptEmpty(shown) && (
        <p className="fl-form-note muted stop-dialog-hint" role="note">
          Prompt: {AGENT_EMPTY_HINT}
        </p>
      )}
      {kind === "verify" && <VerifyStopLive verify={p.verify} session={p.verifySession || ""} />}
      {shown && (editable || p.onToggleHalt) && (
        <div className="stop-dialog-halt">
          <Toggle
            on={!!(editable ? shown.halt : n.step?.halt)}
            label={HALT_LABEL}
            title="Like a breakpoint: Carry on goes on through it"
            onChange={(v) => (editable ? change((d) => ({ ...d, halt: v || undefined })) : p.onToggleHalt?.())}
          />
        </div>
      )}
    </>
  );
  return (
    <Shell p={p} title={title} onClose={close} onEscape={() => (asking ? (setAsking(false), true) : false)}>
      <form
        className="stop-dialog-form"
        onSubmit={(e) => {
          e.preventDefault();
          // Save with nothing changed keeps the stop as it is: a stop just
          // added opens here with its defaults, and those are a fine answer.
          if (!editable || p.busy) return;
          if (check) setRefused(check);
          else if (dirty) void save();
          else p.onClose();
        }}
      >
        <h2>{editable ? "Edit stop" : "Stop"}</h2>
        <div className={"stop-dialog-kind" + (perch ? " stop-dialog-perch" : "")}>
          <span className="fl-box-icon">
            <StopIcon kind={perch ? "perch" : kind} />
          </span>
          <span className="stop-dialog-kind-words">
            <strong>{kindName(p.def, kind)}</strong>
            {p.def?.summary && <span className="muted">{p.def.summary}</span>}
          </span>
        </div>
        {(!editable || parked) && (state || p.note) && (
          <p className="stop-dialog-state" role="status">
            {state && <strong className={"stop-dialog-chip fl-st-" + n.state}>{state}</strong>}
            {p.note && <span>{p.note}</span>}
          </p>
        )}
        {parked && !!p.asks?.length && (
          <ul className="stop-dialog-asks">
            {p.asks.map((t, i) => (
              <li key={i}>{t}</li>
            ))}
          </ul>
        )}
        {parked && <div className="stop-dialog-remedies fl-chiprow">{remedyButtons}</div>}
        {lost && (
          <p className="fl-note stop-dialog-lost" role="alert">
            {SAVE_LOST}
          </p>
        )}
        {problem && !inForm && <Problem problem={problem} def={p.def} />}
        {parked ? (
          <details className="stop-dialog-settings" open={dirty || !!refused || inForm || lost || undefined}>
            <summary>Its settings</summary>
            {editable && p.saveHint && <p className="fl-note muted stop-dialog-save-hint">{p.saveHint}</p>}
            {settings}
          </details>
        ) : (
          <>
            {editable && p.saveHint && <p className="fl-note muted stop-dialog-save-hint">{p.saveHint}</p>}
            {settings}
          </>
        )}
        {p.extra}
        {asking && (
          <InlineConfirm
            title="Discard your changes?"
            body="What you changed in this stop is not saved."
            confirmLabel="Discard"
            cancelLabel="Keep editing"
            onConfirm={p.onClose}
            onCancel={() => setAsking(false)}
          />
        )}
        <div className="modal-actions stop-dialog-actions">
          <span className="stop-dialog-side">
            {p.onRemove && (
              <button type="button" className="test-btn stop-dialog-danger" disabled={p.busy} onClick={p.onRemove}>
                Remove
              </button>
            )}
            {editable && p.onDuplicate && (
              <button type="button" className="test-btn" disabled={p.busy} onClick={p.onDuplicate}>
                Duplicate
              </button>
            )}
          </span>
          {!parked && remedyButtons}
          <button type="button" className="test-btn" onClick={close}>
            {editable ? "Cancel" : "Close"}
          </button>
          {editable &&
            (parked ? (
              // The remedy up top is the primary here; Save is the second tier.
              <button type="submit" className="test-btn" disabled={p.busy} title={dirty ? "Save the changes" : "Keep it as it is"}>
                Save
              </button>
            ) : (
              <button type="submit" className="btn-primary" disabled={p.busy} title={dirty ? "Save the changes" : "Keep it as it is"}>
                Save
              </button>
            ))}
        </div>
      </form>
    </Shell>
  );
}

function GroupDialog(p: StopDialogProps) {
  const n = p.node;
  const g = n.group;
  const [name, setName] = useState(g?.name || n.title);
  const next = name.trim();
  const renamed = !!next && next !== (g?.name || n.title);
  const title = "“" + n.title + "”";
  return (
    <Shell p={p} title={title}>
      <form
        className="stop-dialog-form"
        onSubmit={(e) => {
          e.preventDefault();
          if (p.onRename && renamed && !p.busy) void p.onRename(next);
        }}
      >
        <h2>{p.onRename ? "Edit group" : "Group"}</h2>
        <div className="stop-dialog-kind">
          <span className="fl-box-icon">
            <StopIcon kind="group" />
          </span>
          <span className="stop-dialog-kind-words">
            <strong>A group of {g?.total || 0} stops</strong>
            <span className="muted">the chain runs them one by one{g?.current ? " — " + g.done + " of " + g.total + " done" : ""}</span>
          </span>
        </div>
        {p.note && (
          <p className="stop-dialog-state" role="status">
            <span>{p.note}</span>
          </p>
        )}
        <FlField label="Name" wide={false}>
          <TextField label="Group name" value={name} onChange={(t) => p.onRename && setName(t)} />
        </FlField>
        <ol className="stop-dialog-subs" aria-label={"The stops in " + n.title}>
          {(g?.nodes || []).map((s) => {
            const def = s.step ? p.kindOf?.get(s.step.kind) : undefined;
            const perch = (def?.actor || (s.step?.kind === "you" ? "you" : "robot")) === "you";
            return (
              <li key={s.id} className={"stop-dialog-sub fl-st-" + s.state}>
                <span className="fl-box-icon">
                  <StopIcon kind={perch ? "perch" : s.icon} size={13} />
                </span>
                <span className="stop-dialog-sub-title">{s.title}</span>
                <span className="stop-dialog-sub-state">{STOP_STATE_WORD[s.state] || ""}</span>
                {p.onOpenSub && (
                  <button type="button" className="fl-icon-btn" aria-label={"Edit “" + s.title + "”"} title={s.editable ? "Edit this stop" : "Open this stop (read-only)"} onClick={() => p.onOpenSub!(s)}>
                    ✎
                  </button>
                )}
                {p.onRemoveSub && (
                  <button type="button" className="fl-icon-btn stop-dialog-danger" aria-label={"Remove “" + s.title + "”"} title="Remove this stop from the route" disabled={p.busy} onClick={() => p.onRemoveSub!(s)}>
                    🗑
                  </button>
                )}
              </li>
            );
          })}
        </ol>
        <div className="modal-actions stop-dialog-actions">
          <span className="stop-dialog-side">
            {p.onRemove && (
              <button type="button" className="test-btn stop-dialog-danger" disabled={p.busy} onClick={p.onRemove}>
                Remove
              </button>
            )}
            {p.onUngroup && (
              <button type="button" className="test-btn" disabled={p.busy} title="Its stops go back on the route one by one" onClick={p.onUngroup}>
                Ungroup
              </button>
            )}
            {p.onSaveShelf && (
              <button type="button" className="test-btn" disabled={p.busy || !next} title="Keep these stops on the shelf as a saved route, to drag onto any route" onClick={() => void p.onSaveShelf!(next)}>
                Save to the shelf
              </button>
            )}
          </span>
          <button type="button" className="test-btn" onClick={p.onClose}>
            {p.onRename ? "Cancel" : "Close"}
          </button>
          {p.onRename && (
            <button type="submit" className="btn-primary" disabled={!renamed || p.busy}>
              Save
            </button>
          )}
        </div>
      </form>
    </Shell>
  );
}
