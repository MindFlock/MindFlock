/** A flyway made and edited ON the Sky: a card floating at (x, y) over the
 * plane — its name, its stops on the editor canvas (drag to reorder, + for
 * the stop palette, the selected stop's inspector under it), **Describe a
 * stop…**, **Takes off** (its trigger: by hand, from intake, or on a
 * schedule — FlywayInspector), and Save / Save & fly on… / Cancel. Its ⋯
 * holds **Reset to default** (a built-in) and **Delete** (yours, asked
 * inline). The shelf is where flyways live — each a row of its stops,
 * built by dragging and saved at once (FlywayRow); this card is its details
 * editor (Edit details… in a row's ⋯): the trigger, the session, Describe,
 * and each stop's settings in the inspector. The Flights dialog's Flyways
 * tab is retired.
 *
 * `initial` seeds it — a flyway to edit (with an `id`: Save updates it), or a
 * live line's stops through `planFromSteps` ("Save as flyway"). **Save**
 * keeps the card open on the saved flyway (a second Save updates it); **Save &
 * fly on…** saves, then hands the flyway to `onSaved` and the host offers the
 * line picker. Unsaved changes are guarded inline (the desktop app has no
 * confirm()). */

import { useMemo, useState, type JSX } from "react";
import { OverflowMenu, type MenuItem } from "../OverflowMenu";
import { InlineConfirm } from "../InlineConfirm";
import { ApiError } from "../../api/client";
import { useFlightKinds } from "../../state/queries";
import { errMsg } from "../../lib/format";
import { toast } from "../../lib/toast";
import { TRIGGER_LABEL, blankPlan, duplicateStep, moveStep, newStep, planDirty, type FlightPlan, type FlightStep, type StepKindDef } from "../../lib/flights";
import { flywayDiagram, type DiagramNode } from "../../lib/flightDiagram";
import { deletePlan, resetPlan, savePlan } from "../../lib/flightsApi";
import { freshFlywayName } from "../../lib/flywayEdit";
import { Bird } from "./Bird";
import { FlywayCanvas } from "./FlywayCanvas";
import { DescribeField, StopInspector, StopPalette } from "./StopParts";
import { TextField } from "./controls";
import { FlywayTrigger } from "./FlywayInspector";
import "./Flights.css";
import "./FlywayCard.css";

const clone = <T,>(v: T): T => JSON.parse(JSON.stringify(v)) as T;

export { freshFlywayName };

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
  initialStop,
  onSaved,
  onCancel,
}: {
  x: number;
  y: number;
  plans: FlightPlan[];
  initial?: Partial<FlightPlan>;
  /** The stop to open on (✎ on a stop in a shelf row); the first one else. */
  initialStop?: number;
  onSaved(plan: FlightPlan): void;
  /** Closed: Cancel / ✕ / Escape, or the flyway was deleted. */
  onCancel(): void;
}): JSX.Element {
  const kinds = useFlightKinds().data || [];
  const [draft, setDraft] = useState<FlightPlan>(() => cardDraft(plans, initial));
  const [seed, setSeed] = useState<FlightPlan>(() => clone(draft));
  const [stop, setStop] = useState<number | null>(() => (draft.steps.length ? Math.max(0, Math.min(draft.steps.length - 1, initialStop ?? 0)) : null));
  const [paletteAt, setPaletteAt] = useState<number | null>(null);
  const [described, setDescribed] = useState<Set<string>>(new Set());
  const [error, setError] = useState("");
  const [busy, setBusy] = useState<"" | "save" | "fly" | "reset" | "delete">("");
  const [askDiscard, setAskDiscard] = useState(false);
  const [askDelete, setAskDelete] = useState(false);
  // Fixed at open: toggling the trigger inside must not fold its own section.
  const [triggerOpen] = useState(() => draft.trigger.kind === "schedule");

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
  const removeStop = (i: number) => {
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

  /** A built-in back the way MindFlock ships it (the card reloads on it). */
  const reset = async () => {
    if (busy || !draft.id) return;
    setBusy("reset");
    setError("");
    try {
      const p = await resetPlan(draft.id);
      setDraft(clone(p));
      setSeed(clone(p));
      setStop(p.steps.length ? 0 : null);
      setDescribed(new Set());
      toast("“" + p.name + "” is back to its default");
    } catch (err) {
      setError(errMsg(err));
    } finally {
      setBusy("");
    }
  };

  const remove = async () => {
    if (busy || !draft.id) return;
    setBusy("delete");
    try {
      await deletePlan(draft.id);
      toast("Deleted “" + draft.name + "”");
      onCancel();
    } catch (err) {
      setError(errMsg(err));
      setAskDelete(false);
    } finally {
      setBusy("");
    }
  };

  // The card's ⋯: Reset a built-in, Delete one of yours (asked inline — the
  // desktop app has no confirm()).
  const menu: MenuItem[] = draft.id
    ? draft.builtin
      ? [{ key: "reset", label: "Reset to default", title: "Put this built-in flyway back the way MindFlock ships it", onSelect: () => void reset() }]
      : [{ key: "delete", label: "Delete flyway…", danger: true, onSelect: () => setAskDelete(true) }]
    : [];

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
        {menu.length > 0 && <OverflowMenu label={"More for “" + draft.name + "”"} items={menu} />}
        <button type="button" className="fl-icon-btn" aria-label="Close" title="Close" onClick={cancel}>
          ✕
        </button>
      </header>
      {askDelete && (
        <InlineConfirm
          id="sky-flyway-delete"
          title={"Delete “" + draft.name + "”?"}
          body="Flights already in the air keep their own copy of its stops."
          confirmLabel="Delete"
          busy={busy === "delete"}
          onCancel={() => setAskDelete(false)}
          onConfirm={() => void remove()}
        />
      )}

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
        onRemove={(n) => removeStop(nodeIndex(n))}
        onDuplicate={(n) => setSteps(duplicateStep(draft.steps, nodeIndex(n)))}
        onDropStop={(kind, at) => {
          const def = kindOf.get(kind);
          if (def) insert(at ?? draft.steps.length, def);
        }}
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
              <button type="button" className="test-btn fl-btn" onClick={() => setSteps(duplicateStep(draft.steps, stop))}>
                Duplicate
              </button>
              <button type="button" className="test-btn fl-btn danger" onClick={() => removeStop(stop)}>
                Remove
              </button>
            </>
          }
        />
      ) : (
        <p className="fl-note muted">{draft.steps.length ? "Pick a stop to edit it." : "Press + to add the first stop, or describe it above."}</p>
      )}

      {/* Where its flights come from: open on a scheduled flyway (Schedule's
          "Edit in the Sky" lands here for its schedule). */}
      <details className="sky-flyway-card-trigger" open={triggerOpen || undefined}>
        <summary>
          Takes off: <strong>{TRIGGER_LABEL[draft.trigger.kind] || "By hand"}</strong>
        </summary>
        <FlywayTrigger plan={draft} onChange={(p) => setDraft((d) => ({ ...d, ...p }))} />
      </details>

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
