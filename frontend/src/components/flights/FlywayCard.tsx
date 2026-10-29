/** A flyway made ON the Sky: a card floating at (x, y) over the plane — its
 * name, its stops on the editor canvas (drag to reorder, + for the stop
 * palette, the selected stop's inspector under it), **Describe a stop…**, and
 * Save / Save & fly on… / Cancel. The Flyways tab's editor, cut to what a
 * card can hold: the trigger and session defaults stay in that tab.
 *
 * `initial` seeds it — a flyway to edit (with an `id`: Save updates it), or a
 * live line's stops through `planFromSteps` ("Save as flyway"). **Save**
 * keeps the card open on the saved flyway (a second Save updates it); **Save &
 * fly on…** saves, then hands the flyway to `onSaved` and the host offers the
 * line picker. Unsaved changes are guarded inline (the desktop app has no
 * confirm()). */

import { useMemo, useState, type JSX } from "react";
import { ApiError } from "../../api/client";
import { useFlightKinds } from "../../state/queries";
import { errMsg } from "../../lib/format";
import { toast } from "../../lib/toast";
import { blankPlan, moveStep, newStep, planDirty, type FlightPlan, type FlightStep, type StepKindDef } from "../../lib/flights";
import { flywayDiagram, type DiagramNode } from "../../lib/flightDiagram";
import { savePlan } from "../../lib/flightsApi";
import { Bird } from "./Bird";
import { FlywayCanvas } from "./FlywayCanvas";
import { DescribeField, StopInspector, StopPalette } from "./StopParts";
import { TextField } from "./controls";
import "./FlywayCard.css";

const clone = <T,>(v: T): T => JSON.parse(JSON.stringify(v)) as T;

/** "New flyway", or "New flyway 2", 3… — never a name already on the shelf. */
export function freshFlywayName(plans: readonly Pick<FlightPlan, "name">[], base = "New flyway"): string {
  const taken = new Set(plans.map((p) => p.name.trim().toLowerCase()));
  if (!taken.has(base.toLowerCase())) return base;
  let n = 2;
  while (taken.has((base + " " + n).toLowerCase())) n++;
  return base + " " + n;
}

/** The card's starting flyway: `initial` over a blank manual one; a new one
 * gets a fresh name. */
export function cardDraft(plans: readonly Pick<FlightPlan, "name">[], initial?: Partial<FlightPlan>): FlightPlan {
  const base = blankPlan();
  const seeded: FlightPlan = { ...base, ...clone(initial || {}) };
  if (!initial?.name) seeded.name = freshFlywayName(plans);
  return seeded;
}

/** What the server's 400 named (`{error, field}`) → the stop it is about. */
function stopOfField(field: string, steps: readonly FlightStep[]): number {
  const idx = /steps\[?\.?(\d+)/.exec(field);
  if (idx) return Number(idx[1]);
  return steps.findIndex((s) => field === s.id || field.startsWith(s.id + "."));
}

export interface CardSaveResult {
  plan: FlightPlan | null;
  error: string;
  /** The stop the refusal points at (-1: none). */
  stop: number;
}

/** Save (POST /api/flights/plans; an `id` = update) the card's flyway, its
 * name trimmed. `andFly` then hands the saved flyway to `onSaved` — Save &
 * fly on…; a plain Save keeps the card open. A refusal comes back as the
 * server's sentence and the stop it names. */
export async function saveCard(draft: FlightPlan, andFly: boolean, onSaved: (plan: FlightPlan) => void): Promise<CardSaveResult> {
  try {
    const plan = await savePlan({ ...draft, name: draft.name.trim() || "New flyway" });
    toast("Saved “" + plan.name + "”");
    if (andFly) onSaved(plan);
    return { plan, error: "", stop: -1 };
  } catch (err) {
    const body = err instanceof ApiError ? (err.body as { field?: string } | null) : null;
    return { plan: null, error: errMsg(err), stop: body?.field ? stopOfField(body.field, draft.steps) : -1 };
  }
}

export function FlywayCard({
  x,
  y,
  plans,
  initial,
  onSaved,
  onCancel,
}: {
  x: number;
  y: number;
  plans: FlightPlan[];
  initial?: Partial<FlightPlan>;
  onSaved(plan: FlightPlan): void;
  onCancel(): void;
}): JSX.Element {
  const kinds = useFlightKinds().data || [];
  const [draft, setDraft] = useState<FlightPlan>(() => cardDraft(plans, initial));
  const [seed, setSeed] = useState<FlightPlan>(() => clone(draft));
  const [stop, setStop] = useState<number | null>(() => (draft.steps.length ? 0 : null));
  const [paletteAt, setPaletteAt] = useState<number | null>(null);
  const [described, setDescribed] = useState<Set<string>>(new Set());
  const [error, setError] = useState("");
  const [busy, setBusy] = useState<"" | "save" | "fly">("");
  const [askDiscard, setAskDiscard] = useState(false);

  const kindOf = useMemo(() => new Map(kinds.map((k) => [k.kind, k])), [kinds]);
  const diagram = useMemo(() => flywayDiagram(draft.steps, kinds), [draft.steps, kinds]);
  const dirty = planDirty(draft, seed) || !draft.id;
  const editing = !!initial?.id;
  const sel = stop !== null ? draft.steps[stop] : null;

  const setSteps = (steps: FlightStep[]) => setDraft((d) => ({ ...d, steps }));
  const nodeIndex = (n: DiagramNode) => (n.index === undefined ? -1 : n.index);
  const move = (from: number, to: number) => {
    if (to < 0 || to >= draft.steps.length) return;
    setSteps(moveStep(draft.steps, from, to));
    setStop(to);
  };
  const insert = (at: number, def: StepKindDef) => {
    const steps = draft.steps.slice();
    steps.splice(at, 0, newStep(def, draft.steps.map((s) => s.id)));
    setSteps(steps);
    setStop(at);
    setPaletteAt(null);
  };
  const remove = (i: number) => {
    setSteps(draft.steps.filter((_, j) => j !== i));
    setStop(draft.steps.length > 1 ? Math.max(0, i - 1) : null);
  };

  const save = async (andFly: boolean) => {
    if (busy) return;
    setBusy(andFly ? "fly" : "save");
    setError("");
    const r = await saveCard(draft, andFly, onSaved);
    setBusy("");
    if (r.plan) {
      setDraft(clone(r.plan));
      setSeed(clone(r.plan));
      setDescribed(new Set());
      return;
    }
    setError(r.error);
    if (r.stop >= 0) setStop(r.stop);
  };

  const cancel = () => {
    if (dirty && draft.steps.length && !askDiscard) setAskDiscard(true);
    else onCancel();
  };

  return (
    <div
      className="sky-flyway-card"
      style={{ left: x, top: y }}
      role="dialog"
      aria-label={editing ? "Edit the flyway “" + draft.name + "”" : "New flyway"}
      data-tour="sky-flyway-card"
      onPointerDown={(e) => e.stopPropagation()}
      onWheel={(e) => e.stopPropagation()}
      onKeyDown={(e) => {
        if (e.key === "Escape" && paletteAt === null) {
          e.stopPropagation();
          cancel();
        }
      }}
    >
      <header className="sky-flyway-card-head">
        <Bird size={16} />
        <TextField label="Flyway name" value={draft.name} placeholder="Name this flyway" onChange={(name) => setDraft((d) => ({ ...d, name }))} />
        {draft.builtin && <span className="fl-builtin-tag">Built-in</span>}
        {draft.id && dirty && <span className="fl-unsaved">Unsaved</span>}
        <button type="button" className="fl-icon-btn" aria-label="Close" title="Close" onClick={cancel}>
          ✕
        </button>
      </header>

      <FlywayCanvas
        compact
        diagram={diagram}
        label="The new flyway's stops"
        selected={sel ? diagram.nodes.find((n) => n.index === stop)?.id : paletteAt !== null && paletteAt >= draft.steps.length ? "add" : null}
        highlight={new Set(diagram.nodes.filter((n) => n.step && described.has(n.step.id)).map((n) => n.id))}
        onSelect={(n) => {
          setStop(nodeIndex(n));
          setPaletteAt(null);
        }}
        onOpen={(n) => {
          if (n.kind === "add") setPaletteAt(paletteAt === draft.steps.length ? null : draft.steps.length);
        }}
        onMove={move}
        onRemove={(n) => remove(nodeIndex(n))}
        onInsert={(at) => setPaletteAt(paletteAt === at ? null : at)}
      />
      {paletteAt !== null && (
        <StopPalette
          kinds={kinds}
          title={paletteAt >= draft.steps.length ? "Add a stop at the end" : "Add a stop before “" + (draft.steps[paletteAt]?.title || "") + "”"}
          onPick={(k) => insert(paletteAt, k)}
          onClose={() => setPaletteAt(null)}
        />
      )}
      <DescribeField
        taken={draft.steps.map((s) => s.id)}
        onStops={(added) => {
          setSteps([...draft.steps, ...added]);
          setDescribed((d) => new Set([...d, ...added.map((s) => s.id)]));
          setStop(draft.steps.length);
          setPaletteAt(null);
        }}
      />
      {sel && stop !== null ? (
        <StopInspector
          step={sel}
          def={kindOf.get(sel.kind)}
          onChange={(p) => setSteps(draft.steps.map((s, j) => (j === stop ? { ...s, ...p } : s)))}
          note={described.has(sel.id) ? <p className="fl-note fl-described-note">Added by your description — check it, then Save.</p> : null}
          actions={
            <>
              <button type="button" className="test-btn fl-btn" disabled={stop === 0} onClick={() => move(stop, stop - 1)} aria-label="Move earlier">
                ← Earlier
              </button>
              <button type="button" className="test-btn fl-btn" disabled={stop === draft.steps.length - 1} onClick={() => move(stop, stop + 1)} aria-label="Move later">
                Later →
              </button>
              <button type="button" className="test-btn fl-btn danger" onClick={() => remove(stop)}>
                Remove
              </button>
            </>
          }
        />
      ) : (
        <p className="fl-note muted">{draft.steps.length ? "Pick a stop to edit it." : "Press + to add the first stop, or describe it above."}</p>
      )}

      {error && (
        <p className="fl-note sky-flyway-card-error" role="alert">
          {error}
        </p>
      )}
      {askDiscard ? (
        <div className="sky-flyway-card-acts" role="group" aria-label="Discard this flyway?">
          <span className="sky-flyway-card-ask">Discard the unsaved changes?</span>
          <button type="button" className="test-btn fl-btn danger" onClick={onCancel}>
            Discard
          </button>
          <button type="button" className="test-btn fl-btn" autoFocus onClick={() => setAskDiscard(false)}>
            Keep editing
          </button>
        </div>
      ) : (
        <div className="sky-flyway-card-acts">
          <button type="button" className="btn-primary fl-btn" disabled={!!busy || !draft.steps.length} onClick={() => void save(true)}>
            {busy === "fly" ? "Saving…" : "Save & fly on…"}
          </button>
          <button type="button" className="test-btn fl-btn" disabled={!!busy || !draft.steps.length || !dirty} onClick={() => void save(false)}>
            {busy === "save" ? "Saving…" : dirty ? "Save" : "Saved"}
          </button>
          <button type="button" className="test-btn fl-btn" onClick={cancel}>
            {dirty ? "Cancel" : "Close"}
          </button>
        </div>
      )}
    </div>
  );
}
