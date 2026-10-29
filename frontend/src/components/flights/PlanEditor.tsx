/** Flights → Flyways: the flyways on the left (built-ins first), the one you
 * picked on the right, drawn as what it is — stops on a CANVAS, joined by
 * drawn connectors, read left to right (FlywayCanvas). Drag a box to reorder
 * it (or ← → on the selected one), Delete removes it, + between boxes or at
 * the end opens the stop palette ("The flock does" / "You do"), and the
 * selected stop's fields sit in the inspector under the canvas — the app's
 * own chips and fields, never a native <select>. **Describe a stop…** under
 * the canvas hands a sentence to the server's one-shot model and appends the
 * stops it answers with, highlighted until the flyway is saved.
 *
 * A flyway is stored as a plan (`/api/flights/plans`, `FlightPlan`); only the
 * words changed. Unsaved changes are guarded inline (never confirm(): the
 * desktop app has none) — here when another flyway is picked, and by
 * FlightsDialog when another tab is. */

import { useEffect, useMemo, useState } from "react";
import { ApiError } from "../../api/client";
import { useFlightKinds, useFlightPlans, useInstances } from "../../state/queries";
import { useUi } from "../../state/store";
import { toast } from "../../lib/toast";
import { errMsg } from "../../lib/format";
import { cronOf, cronPreview, onceOf, type CronPreview } from "../../lib/templates";
import { InlineConfirm } from "../InlineConfirm";
import { PathField } from "../PathField";
import { useAgentChoices } from "../settings/screens/AgentPicker";
import { cronPreviewLine } from "../dialogs/TemplateEditor";
import {
  TRIGGER_LABEL,
  blankPlan,
  duplicateStep,
  moveStep,
  newStep,
  planDirty,
  scheduleOfWhen,
  whenFormOf,
  type FlightPlan,
  type FlightStep,
  type StepKindDef,
  type TriggerKind,
  type WhenForm,
} from "../../lib/flights";
import { flywayDiagram, type DiagramNode } from "../../lib/flightDiagram";
import { deletePlan, resetPlan, savePlan, takeOff } from "../../lib/flightsApi";
import { FlywayCanvas } from "./FlywayCanvas";
import { DescribeField, StopInspector, StopPalette } from "./StopParts";
import { ChipRow, FlField, Seg, TextField, Toggle } from "./controls";
import { Bird } from "./Bird";

const clonePlan = (p: FlightPlan): FlightPlan => JSON.parse(JSON.stringify(p)) as FlightPlan;

/** What the server's 400 named: `{error, field}` → the stop it is about. */
function stopOfField(field: string, steps: FlightStep[]): number {
  const idx = /steps\[?\.?(\d+)/.exec(field);
  if (idx) return Number(idx[1]);
  return steps.findIndex((s) => field === s.id || field.startsWith(s.id + "."));
}

const DAYS: ReadonlyArray<{ v: WhenForm["days"]; label: string }> = [
  { v: "daily", label: "Every day" },
  { v: "weekdays", label: "Weekdays" },
  { v: "once", label: "Once" },
  { v: "cron", label: "Custom" },
];

/** A scheduled flyway's when: days as chips, the time / date / cron as fields,
 * and the server's preview of the next run under them. */
function ScheduleChips({ when, onWhen, preview }: { when: WhenForm; onWhen(w: WhenForm): void; preview: CronPreview | null }) {
  const line = cronPreviewLine(preview, new Date());
  return (
    <div className="fl-schedule">
      <Seg label="Which days" value={when.days} options={DAYS} onChange={(days) => onWhen({ ...when, days })} />
      {when.days === "cron" ? (
        <TextField label="Cron expression" mono value={when.cron} placeholder="0 21 * * 1-5" onChange={(cron) => onWhen({ ...when, cron })} />
      ) : (
        <span className="fl-schedule-at">
          {when.days === "once" && (
            <input
              type="date"
              className="fl-input fl-date"
              aria-label="Date"
              value={when.date}
              onChange={(e) => onWhen({ ...when, date: e.target.value })}
            />
          )}
          <span className="muted">at</span>
          <input type="time" className="fl-input fl-time" aria-label="Time" value={when.at} onChange={(e) => onWhen({ ...when, at: e.target.value })} />
        </span>
      )}
      {line && <span className="fl-fld-hint">{line}</span>}
    </div>
  );
}

export function Plans({ initialPlan, onDirty }: { initialPlan?: string; onDirty(dirty: boolean): void }) {
  const plansQ = useFlightPlans();
  const kinds = useFlightKinds().data || [];
  const plans = plansQ.data || [];
  const instances = useInstances().data || [];
  const agents = useAgentChoices();
  const closeDialog = useUi((s) => s.closeDialog);

  // Seeded on the first render when the flyways are already cached (the
  // Board reads them too), so the editor opens on one rather than "Loading…".
  const first = plans.find((p) => p.id === initialPlan) || plans[0] || null;
  const [selected, setSelected] = useState<string | null>(() => (first ? first.id : null));
  const [draft, setDraft] = useState<FlightPlan | null>(() => (first ? clonePlan(first) : null));
  const [seed, setSeed] = useState<FlightPlan | null>(() => (first ? clonePlan(first) : null));
  const [stop, setStop] = useState<number | null>(() => (first?.steps.length ? 0 : null));
  const [paletteAt, setPaletteAt] = useState<number | null>(null);
  const [described, setDescribed] = useState<Set<string>>(new Set());
  const [error, setError] = useState("");
  const [pending, setPending] = useState<string | null>(null);
  const [busy, setBusy] = useState("");
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [askTakeOff, setAskTakeOff] = useState(false);
  const [takeOffRepo, setTakeOffRepo] = useState("");
  const [takeOffTitle, setTakeOffTitle] = useState("");
  const [preview, setPreview] = useState<CronPreview | null>(null);

  const dirty = planDirty(draft, seed);
  useEffect(() => onDirty(dirty), [dirty, onDirty]);

  const load = (p: FlightPlan | null) => {
    const copy = p ? clonePlan(p) : blankPlan();
    setSelected(p ? p.id : "");
    setDraft(copy);
    setSeed(clonePlan(copy));
    setStop(copy.steps.length ? 0 : null);
    setPaletteAt(copy.steps.length ? null : 0);
    setDescribed(new Set());
    setError("");
    setConfirmDelete(false);
    setAskTakeOff(false);
  };

  // First load: the flyway a deep link named (#flights/plans/<id>), else the first.
  useEffect(() => {
    if (selected !== null || !plans.length) return;
    load(plans.find((p) => p.id === initialPlan) || plans[0]);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [plans.length, initialPlan]);

  // The schedule trigger's preview, from the server, as it is typed.
  const when = whenFormOf(draft?.trigger.schedule);
  const cronText =
    draft?.trigger.kind === "schedule"
      ? (when.days === "cron" ? when.cron : when.days === "once" ? onceOf(when.date, when.at) : cronOf(when.at, when.days)).trim()
      : "";
  useEffect(() => {
    if (!cronText) {
      setPreview(null);
      return;
    }
    let live = true;
    const t = setTimeout(() => {
      cronPreview(cronText)
        .then((pv) => live && setPreview(pv))
        .catch(() => live && setPreview(null));
    }, 300);
    return () => {
      live = false;
      clearTimeout(t);
    };
  }, [cronText]);

  const kindOf = useMemo(() => new Map(kinds.map((k) => [k.kind, k])), [kinds]);
  const diagram = useMemo(() => flywayDiagram(draft?.steps || [], kinds), [draft?.steps, kinds]);

  if (plansQ.data === null)
    return <p className="fl-note muted">This server has no flyways yet — update MindFlock to edit them.</p>;
  if (!draft) return <p className="fl-note muted">{plansQ.error ? "Could not load the flyways: " + errMsg(plansQ.error) : "Loading flyways…"}</p>;

  const patch = (p: Partial<FlightPlan>) => setDraft((d) => (d ? { ...d, ...p } : d));
  const setSteps = (steps: FlightStep[]) => patch({ steps });
  const patchStep = (i: number, p: Partial<FlightStep>) => setSteps(draft.steps.map((s, j) => (j === i ? { ...s, ...p } : s)));
  const sel = stop !== null ? draft.steps[stop] : null;
  const builtinPlan = plans.find((p) => p.id === draft.id)?.builtin || draft.builtin;
  const needsSession = draft.trigger.kind === "manual" || draft.trigger.kind === "schedule";

  const pick = (id: string) => {
    if (id === selected) return;
    if (dirty) {
      setPending(id);
      return;
    }
    load(id ? plans.find((p) => p.id === id) || null : null);
  };

  const move = (from: number, to: number) => {
    if (to < 0 || to >= draft.steps.length) return;
    setSteps(moveStep(draft.steps, from, to));
    setStop(to);
  };

  const insert = (at: number, def: StepKindDef) => {
    const step = newStep(def, draft.steps.map((s) => s.id));
    const steps = draft.steps.slice();
    steps.splice(at, 0, step);
    setSteps(steps);
    setStop(at);
    setPaletteAt(null);
  };

  const remove = (i: number) => {
    setSteps(draft.steps.filter((_, j) => j !== i));
    setStop(draft.steps.length > 1 ? Math.max(0, i - 1) : null);
  };

  const save = async (): Promise<FlightPlan | null> => {
    setBusy("save");
    setError("");
    try {
      const saved = await savePlan(draft);
      load(saved);
      toast("Saved “" + saved.name + "”");
      return saved;
    } catch (err) {
      const body = err instanceof ApiError ? (err.body as { error?: string; field?: string } | null) : null;
      setError(errMsg(err));
      if (body?.field) {
        const at = stopOfField(body.field, draft.steps);
        if (at >= 0) setStop(at);
      }
      return null;
    } finally {
      setBusy("");
    }
  };

  const fly = async (opts: { repo_path?: string; title?: string }) => {
    let id = draft.id;
    if (dirty || !id) {
      const saved = await save();
      if (!saved) return;
      id = saved.id;
    }
    setBusy("takeoff");
    try {
      const f = await takeOff({ plan_id: id, ...(opts.title ? { title: opts.title } : {}), ...(opts.repo_path ? { repo_path: opts.repo_path } : {}) });
      // Every start ends in its session (takeOff selects it once its row exists).
      if (f) closeDialog();
    } finally {
      setBusy("");
    }
  };

  const onTakeOff = () => {
    if (needsSession && draft.session.repo_path) {
      void fly({});
      return;
    }
    setTakeOffRepo(draft.session.repo_path);
    setAskTakeOff(true);
  };

  const nodeIndex = (n: DiagramNode) => (n.index === undefined ? -1 : n.index);
  const openSessions = instances.filter((i) => !i.flight && !i.pending);

  return (
    <div className="fl-plans" data-tour="flights-plans">
      <aside className="fl-plan-list" aria-label="Flyways">
        {plans.map((p) => (
          <button
            key={p.id}
            type="button"
            className={"fl-plan-item" + (p.id === selected ? " active" : "")}
            aria-current={p.id === selected ? "true" : undefined}
            onClick={() => pick(p.id)}
          >
            <span className="fl-plan-name">{p.name}</span>
            <span className="fl-plan-meta muted">
              {p.builtin ? "Built-in · " : ""}
              {TRIGGER_LABEL[p.trigger?.kind] || "By hand"} · {p.steps.length} {p.steps.length === 1 ? "stop" : "stops"}
            </span>
          </button>
        ))}
        <button type="button" className={"fl-plan-item fl-plan-new" + (selected === "" ? " active" : "")} onClick={() => pick("")}>
          + New flyway
        </button>
      </aside>

      <div className="fl-editor">
        {pending !== null && (
          <InlineConfirm
            title={"Discard changes to “" + draft.name + "”?"}
            body="They haven't been saved."
            confirmLabel="Discard"
            cancelLabel="Keep editing"
            onCancel={() => setPending(null)}
            onConfirm={() => {
              const id = pending;
              setPending(null);
              load(id ? plans.find((p) => p.id === id) || null : null);
            }}
          />
        )}
        <div className="fl-editor-head">
          <span className="fl-editor-bird" aria-hidden="true">
            <Bird size={18} />
          </span>
          <input className="fl-input fl-plan-title" aria-label="Flyway name" value={draft.name} onChange={(e) => patch({ name: e.target.value })} />
          {builtinPlan && <span className="fl-builtin-tag">Built-in</span>}
          {dirty && <span className="fl-unsaved">Unsaved</span>}
        </div>

        {/* THE CANVAS: stops left to right, connectors between, + at the end. */}
        <FlywayCanvas
          diagram={diagram}
          label="The flyway's stops"
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
          dataTour="flights-describe"
          taken={draft.steps.map((s) => s.id)}
          onStops={(added) => {
            setSteps([...draft.steps, ...added]);
            setDescribed((d) => new Set([...d, ...added.map((s) => s.id)]));
            setStop(draft.steps.length);
            setPaletteAt(null);
          }}
        />

        {/* The selected stop's fields. */}
        {sel && stop !== null ? (
          <StopInspector
            step={sel}
            def={kindOf.get(sel.kind)}
            onChange={(p) => patchStep(stop, p)}
            note={described.has(sel.id) ? <p className="fl-note fl-described-note">Added by your description — check it, then Save.</p> : null}
            actions={
              <>
                <button type="button" className="test-btn fl-btn" disabled={stop === 0} onClick={() => move(stop, stop - 1)} aria-label="Move earlier">
                  ← Earlier
                </button>
                <button
                  type="button"
                  className="test-btn fl-btn"
                  disabled={stop === draft.steps.length - 1}
                  onClick={() => move(stop, stop + 1)}
                  aria-label="Move later"
                >
                  Later →
                </button>
                <button type="button" className="test-btn fl-btn" onClick={() => setSteps(duplicateStep(draft.steps, stop))}>
                  Duplicate
                </button>
                <button type="button" className="test-btn fl-btn danger" onClick={() => remove(stop)}>
                  Remove
                </button>
              </>
            }
          />
        ) : (
          <p className="fl-note muted">{draft.steps.length ? "Pick a stop to edit it." : "Press + to add the first stop."}</p>
        )}

        {/* Where this flyway's flights come from, and what they fly in. */}
        <section className="fl-section" aria-label="Where its flights come from">
          <div className="fl-form">
            <FlField label="Takes off" wide>
              <Seg
                label="Takes off"
                value={draft.trigger.kind}
                options={(Object.keys(TRIGGER_LABEL) as TriggerKind[]).map((k) => ({ v: k, label: TRIGGER_LABEL[k] }))}
                onChange={(kind) =>
                  patch({
                    trigger: {
                      ...draft.trigger,
                      kind,
                      schedule: kind === "schedule" ? draft.trigger.schedule || { at: "21:00", days: "daily" } : null,
                    },
                  })
                }
              />
            </FlField>
            {draft.trigger.kind === "schedule" && (
              <FlField label="When" wide>
                <ScheduleChips
                  when={when}
                  preview={preview}
                  onWhen={(w) => patch({ trigger: { ...draft.trigger, schedule: scheduleOfWhen(w) } })}
                />
                <Toggle
                  on={draft.trigger.paused}
                  onChange={(paused) => patch({ trigger: { ...draft.trigger, paused } })}
                  label="Paused"
                  title="A paused schedule takes off nothing"
                />
              </FlField>
            )}
            {needsSession && (
              <>
                <FlField label="Repository" wide>
                  <PathField
                    id="fl-plan-repo"
                    value={draft.session.repo_path}
                    placeholder="Ask when it takes off"
                    pickerTitle="The repository this flyway's sessions start in"
                    onChange={(v) => patch({ session: { ...draft.session, repo_path: v } })}
                  />
                </FlField>
                <FlField label="Agent" wide>
                  <ChipRow
                    label="Agent"
                    value={draft.session.program}
                    options={[
                      { value: "", label: agents.fallback ? "Default · " + agents.fallback : "Default" },
                      ...agents.names.map((n) => ({ value: n, label: n })),
                    ]}
                    onChange={(program) => patch({ session: { ...draft.session, program } })}
                  />
                </FlField>
                <FlField label="Model">
                  <TextField
                    label="Model"
                    value={draft.session.model}
                    placeholder="The agent's default"
                    onChange={(model) => patch({ session: { ...draft.session, model } })}
                  />
                </FlField>
              </>
            )}
          </div>
        </section>

        {error && (
          <p className="error fl-error" role="alert">
            {error}
          </p>
        )}
        {askTakeOff && (
          <div className="fl-takeoff-ask" role="group" aria-label="Fly this flyway">
            <div className="fl-form">
              <FlField label="In a new session, in this repository" wide>
                <PathField id="fl-takeoff-repo" value={takeOffRepo} onChange={setTakeOffRepo} placeholder="/path/to/repo" />
              </FlField>
              {openSessions.length > 0 && (
                <FlField label="Or on a session that is open" wide>
                  <ChipRow
                    label="Or on a session that is open"
                    value={takeOffTitle}
                    options={[{ value: "", label: "A new session" }, ...openSessions.map((i) => ({ value: i.title, label: i.title }))]}
                    onChange={setTakeOffTitle}
                  />
                </FlField>
              )}
            </div>
            <div className="fl-inspector-acts">
              <button
                type="button"
                className="btn-primary fl-btn"
                disabled={busy !== "" || (!takeOffRepo.trim() && !takeOffTitle)}
                onClick={() => void fly(takeOffTitle ? { title: takeOffTitle } : { repo_path: takeOffRepo.trim() })}
              >
                Take off
              </button>
              <button type="button" className="test-btn fl-btn" onClick={() => setAskTakeOff(false)}>
                Cancel
              </button>
            </div>
          </div>
        )}
        {confirmDelete && (
          <InlineConfirm
            title={"Delete “" + draft.name + "”?"}
            body="Flights already in the air keep their own copy of its stops."
            confirmLabel="Delete"
            busy={busy === "delete"}
            onCancel={() => setConfirmDelete(false)}
            onConfirm={async () => {
              setBusy("delete");
              try {
                await deletePlan(draft.id);
                toast("Deleted “" + draft.name + "”");
                setSelected(null);
                setDraft(null);
                setSeed(null);
              } catch (err) {
                setError(errMsg(err));
              } finally {
                setBusy("");
                setConfirmDelete(false);
              }
            }}
          />
        )}
        <div className="fl-editor-foot">
          <button type="button" className="btn-primary fl-btn" disabled={!dirty || busy !== ""} onClick={() => void save()}>
            {busy === "save" ? "Saving…" : "Save"}
          </button>
          <button
            type="button"
            className="test-btn fl-btn fl-takeoff"
            disabled={busy !== "" || !draft.steps.length}
            title={dirty ? "Saves the flyway, then a flight takes off on it" : "A flight takes off on this flyway now"}
            onClick={onTakeOff}
          >
            {busy === "takeoff" ? "Taking off…" : "Fly this flyway"}
          </button>
          <span className="fl-foot-spacer" />
          {builtinPlan ? (
            <button
              type="button"
              className="test-btn fl-btn"
              disabled={busy !== ""}
              title="Put this built-in flyway back the way MindFlock ships it"
              onClick={async () => {
                setBusy("reset");
                try {
                  const p = await resetPlan(draft.id);
                  load(p);
                  toast("“" + p.name + "” is back to its default");
                } catch (err) {
                  setError(errMsg(err));
                } finally {
                  setBusy("");
                }
              }}
            >
              Reset to default
            </button>
          ) : draft.id ? (
            <button type="button" className="test-btn fl-btn danger" onClick={() => setConfirmDelete(true)}>
              Delete
            </button>
          ) : null}
        </div>
      </div>
    </div>
  );
}
