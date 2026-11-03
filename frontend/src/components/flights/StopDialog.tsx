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
 *    what it can still do: Remove, and on the current one Done / Retry / Skip
 *    and its halt point (set at once).
 *  - A GROUP (a stop with sub-steps): its name (renamed with Save), its
 *    sub-steps — the kind's icon, the title, the state, ✎ opens that
 *    sub-step's own dialog, 🗑 removes it — and Ungroup, Save to the shelf,
 *    Remove (the whole group).
 *
 * Esc and a press on the backdrop close it (Modal). No window.confirm /
 * prompt / alert anywhere: the desktop app has none. */

import { useState, type ReactNode } from "react";
import { LOOP_AGENT, loopsToAgent, type DiagramNode } from "../../lib/flightDiagram";
import type { FlightStep, StepKindDef } from "../../lib/flights";
import { Modal } from "../Modal";
import { StopIcon } from "./Bird";
import { FlField, PromptBox, TextField, Toggle } from "./controls";
import { StopForm, kindName } from "./StopParts";
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
  /** A stop that can change: Save. Without it the stop is read-only. */
  onSave?(patch: StopPatch): void | Promise<unknown>;
  onDuplicate?(): void;
  /** The one remove path (a stop, or the whole group). */
  onRemove?(): void;
  /** A read-only stop's halt point, set at once (the current stop's). */
  onToggleHalt?(): void;
  /** The current stop's own: Done (a perch), Retry (halted), Skip. */
  onDone?(): void;
  /** Done's own words when it does something else (a Verify stop: "Behaviors"
   * opens its card — it moves on when they are answered, not on Done). */
  doneLabel?: string;
  doneTitle?: string;
  onRetry?(): void;
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

function Shell({ p, title, children }: { p: StopDialogProps; title: string; children: ReactNode }) {
  return (
    <Modal id="stop-dialog" className="stop-dialog-modal" onClose={p.onClose} portal={p.portal !== false} role="dialog" aria-modal="true" aria-label={title}>
      <div className="stop-dialog" onKeyDown={(e) => e.key === "Escape" || e.stopPropagation()}>
        {children}
      </div>
    </Modal>
  );
}

function StepDialog(p: StopDialogProps) {
  const n = p.node;
  const [draft, setDraft] = useState<FlightStep | null>(() => (n.step ? copyStep(n.step) : null));
  const editable = !!p.onSave && !!draft;
  const dirty = !!draft && !!n.step && JSON.stringify(draft) !== JSON.stringify(n.step);
  const perch = (p.def?.actor || (n.step?.kind === "you" ? "you" : "robot")) === "you";
  const kind = n.step?.kind || n.icon;
  const title = (editable ? "Edit “" : "“") + (n.step?.title || n.title) + "”";
  const state = STOP_STATE_WORD[n.state];
  const save = async () => {
    if (!draft || !p.onSave) return;
    const patch: StopPatch = { title: draft.title, config: draft.config, on_fail: draft.on_fail };
    if (!!draft.halt !== !!n.step?.halt) patch.halt = !!draft.halt;
    // "Only when" (round 15): sent whenever it differs; "" clears it.
    if ((draft.when || "") !== (n.step?.when || "")) patch.when = draft.when || "";
    await p.onSave(patch);
  };
  return (
    <Shell p={p} title={title}>
      <form
        className="stop-dialog-form"
        onSubmit={(e) => {
          e.preventDefault();
          // Save with nothing changed keeps the stop as it is: a stop just
          // added opens here with its defaults, and those are a fine answer.
          if (!editable || p.busy) return;
          if (dirty) void save();
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
        {!editable && (
          <p className="stop-dialog-state" role="status">
            {state && <strong className={"stop-dialog-chip fl-st-" + n.state}>{state}</strong>}
            {p.note && <span>{p.note}</span>}
          </p>
        )}
        {draft && loopsToAgent(draft) && (
          <>
            <FlField label="Back to" wide hint="After this stop the agent is prompted again; once its turn is done the route runs again from its first stop.">
              <div className="fl-chiprow stop-dialog-loop-agent" data-loop-agent="">
                <strong>the agent</strong>
                {editable && (p.lineSteps?.length || 0) > 1 && (
                  <button type="button" className="test-btn" onClick={() => setDraft((d) => (d ? { ...d, config: { ...d.config, back_to: "" } } : d))}>
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
                  value={String(draft.config?.prompt ?? "")}
                  placeholder="The prompt that started the agent's turn"
                  onChange={(t) => setDraft((d) => (d ? { ...d, config: { ...d.config, prompt: t } } : d))}
                />
              ) : (
                <span className="muted">{String(draft.config?.prompt ?? "").trim() || "the same prompt again"}</span>
              )}
            </FlField>
          </>
        )}
        {draft && editable && p.loopAgent && draft.kind === "loop" && !loopsToAgent(draft) && (
          <div className="fl-chiprow">
            <button type="button" className="test-btn" title="The agent is prompted again after this stop, then the route runs again" onClick={() => setDraft((d) => (d ? { ...d, config: { ...d.config, back_to: LOOP_AGENT } } : d))}>
              ↺ Loop back to the agent instead
            </button>
          </div>
        )}
        {draft ? (
          <StopForm step={draft} def={loopFormDef(p.def, draft)} lineSteps={p.lineSteps} readOnly={!editable} onChange={(patch) => setDraft((d) => (d ? { ...d, ...patch } : d))} />
        ) : (
          <p className="fl-note muted">This stop's details haven't loaded yet.</p>
        )}
        {draft && (editable || p.onToggleHalt) && (
          <div className="stop-dialog-halt">
            <Toggle
              on={!!(editable ? draft.halt : n.step?.halt)}
              label="Halt point — the route stops here, before doing this stop"
              title="Like a breakpoint: Carry on goes on through it"
              onChange={(v) => (editable ? setDraft((d) => (d ? { ...d, halt: v || undefined } : d)) : p.onToggleHalt?.())}
            />
          </div>
        )}
        {p.extra}
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
          {p.onDone && (
            <button type="button" className="test-btn" disabled={p.busy} title={p.doneTitle} onClick={p.onDone}>
              {p.doneLabel || "Done"}
            </button>
          )}
          {p.onRetry && (
            <button type="button" className="test-btn" disabled={p.busy} onClick={p.onRetry}>
              Retry
            </button>
          )}
          {p.onFix && (
            <button type="button" className="test-btn" disabled={p.busy} onClick={p.onFix}>
              Let the agent fix it
            </button>
          )}
          {p.onSkip && (
            <button type="button" className="test-btn" disabled={p.busy} onClick={p.onSkip}>
              Skip
            </button>
          )}
          <button type="button" className="test-btn" onClick={p.onClose}>
            {editable ? "Cancel" : "Close"}
          </button>
          {editable && (
            <button type="submit" className="btn-primary" disabled={p.busy} title={dirty ? "Save the changes" : "Keep it as it is"}>
              Save
            </button>
          )}
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
