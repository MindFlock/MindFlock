/** Flights → Plans: the plan list on the left (built-ins marked), the editor
 * on the right.
 *
 * The editor draws a plan as what it is — a HORIZONTAL ROW OF BLOCKS, read
 * left to right, no connection points to wire: drag a block (or ← → on a
 * focused one) to reorder it, **+** between blocks opens the palette (the
 * server's step registry, robot and *you* kinds in two groups), and the
 * selected block's form sits under the row, generated from its kind's fields.
 * **Describe a step…** at the end of the row hands a sentence to the server's
 * one-shot model and appends the blocks it answers with, highlighted until the
 * plan is saved.
 *
 * Unsaved changes are guarded inline (never confirm(): the desktop app has
 * none) — here when another plan is picked, and by FlightsDialog when another
 * tab is. */

import { useEffect, useMemo, useRef, useState, type DragEvent, type KeyboardEvent } from "react";
import { ApiError } from "../../api/client";
import { useFlightKinds, useFlightPlans, useInstances } from "../../state/queries";
import { useUi } from "../../state/store";
import { toast } from "../../lib/toast";
import { errMsg } from "../../lib/format";
import { cronOf, cronPreview, onceOf, type CronPreview } from "../../lib/templates";
import { InlineConfirm } from "../InlineConfirm";
import { PathField } from "../PathField";
import { useAgentChoices } from "../settings/screens/AgentPicker";
import { ScheduleWhen, formFromTemplate, type TemplateForm } from "../dialogs/TemplateEditor";
import {
  ON_FAIL_LABEL,
  TRIGGER_LABEL,
  blankPlan,
  duplicateStep,
  fieldOptions,
  moveStep,
  newStep,
  planDirty,
  scheduleOfWhen,
  stepActor,
  whenFormOf,
  type FlightPlan,
  type FlightStep,
  type OnFail,
  type StepKindDef,
  type StepKindField,
  type TriggerKind,
} from "../../lib/flights";
import { deletePlan, describeSteps, resetPlan, savePlan, takeOff } from "../../lib/flightsApi";

const clonePlan = (p: FlightPlan): FlightPlan => JSON.parse(JSON.stringify(p)) as FlightPlan;

/** What the server's 400 named: `{error, field}` → the block it is about. */
function blockOfField(field: string, steps: FlightStep[]): number {
  const idx = /steps\[?\.?(\d+)/.exec(field);
  if (idx) return Number(idx[1]);
  const byId = steps.findIndex((s) => field === s.id || field.startsWith(s.id + "."));
  return byId;
}

/** The palette: every kind, robot and *you* in two groups, each with its
 * summary. */
function Palette({ kinds, onPick, onClose }: { kinds: StepKindDef[]; onPick(k: StepKindDef): void; onClose(): void }) {
  const groups: Array<[string, StepKindDef[]]> = [
    ["Robot steps — MindFlock does them", kinds.filter((k) => k.actor === "robot")],
    ["You steps — the flight waits for you", kinds.filter((k) => k.actor === "you")],
  ];
  return (
    <div
      className="fl-palette"
      role="dialog"
      aria-label="Add a block"
      onKeyDown={(e) => {
        if (e.key === "Escape") {
          e.stopPropagation();
          e.preventDefault();
          onClose();
        }
      }}
    >
      {groups.map(([title, list]) =>
        list.length ? (
          <div className="fl-palette-group" key={title}>
            <div className="fl-palette-head">{title}</div>
            {list.map((k, i) => (
              <button
                key={k.kind}
                type="button"
                className={"fl-palette-item fl-kind-" + k.actor}
                autoFocus={i === 0 && title.startsWith("Robot")}
                onClick={() => onPick(k)}
              >
                <span className="fl-palette-label">{k.label}</span>
                <span className="fl-palette-summary">{k.summary}</span>
              </button>
            ))}
          </div>
        ) : null
      )}
      <button type="button" className="linklike fl-palette-close" onClick={onClose}>
        Close
      </button>
    </div>
  );
}

/** One field of a block's form, by the registry's type. */
function FieldInput({ f, value, onChange }: { f: StepKindField; value: unknown; onChange(v: unknown): void }) {
  const id = "fl-field-" + f.name;
  const v = value === undefined ? f.default : value;
  if (f.type === "bool")
    return (
      <label className="fl-field fl-field-bool" htmlFor={id}>
        <input id={id} type="checkbox" checked={!!v} onChange={(e) => onChange(e.target.checked)} />
        <span>{f.label}</span>
      </label>
    );
  return (
    <label className="fl-field" htmlFor={id}>
      <span className="fl-field-label">{f.label}</span>
      {f.type === "textarea" ? (
        <textarea id={id} rows={3} value={String(v ?? "")} placeholder={f.placeholder} onChange={(e) => onChange(e.target.value)} />
      ) : f.type === "select" ? (
        <select id={id} value={String(v ?? "")} onChange={(e) => onChange(e.target.value)}>
          {fieldOptions(f).map((o) => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
        </select>
      ) : f.type === "int" ? (
        <input
          id={id}
          type="number"
          inputMode="numeric"
          value={v === undefined || v === null || v === "" ? "" : String(v)}
          placeholder={f.placeholder}
          onChange={(e) => onChange(e.target.value === "" ? "" : Math.trunc(Number(e.target.value)))}
        />
      ) : (
        <input id={id} type="text" value={String(v ?? "")} placeholder={f.placeholder} onChange={(e) => onChange(e.target.value)} />
      )}
    </label>
  );
}

export function Plans({ initialPlan, onDirty }: { initialPlan?: string; onDirty(dirty: boolean): void }) {
  const plansQ = useFlightPlans();
  const kinds = useFlightKinds().data || [];
  const plans = plansQ.data || [];
  const instances = useInstances().data || [];
  const agents = useAgentChoices();
  const closeDialog = useUi((s) => s.closeDialog);

  // Seeded on the first render when the plans are already cached (the Board
  // reads them too), so the editor opens on a plan rather than "Loading…".
  const first = plans.find((p) => p.id === initialPlan) || plans[0] || null;
  const [selected, setSelected] = useState<string | null>(() => (first ? first.id : null));
  const [draft, setDraft] = useState<FlightPlan | null>(() => (first ? clonePlan(first) : null));
  const [seed, setSeed] = useState<FlightPlan | null>(() => (first ? clonePlan(first) : null));
  const [block, setBlock] = useState<number | null>(() => (first?.steps.length ? 0 : null));
  const [paletteAt, setPaletteAt] = useState<number | null>(null);
  const [described, setDescribed] = useState<Set<string>>(new Set());
  const [error, setError] = useState("");
  const [pending, setPending] = useState<string | null>(null);
  const [busy, setBusy] = useState("");
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [askTakeOff, setAskTakeOff] = useState(false);
  const [takeOffRepo, setTakeOffRepo] = useState("");
  const [takeOffTitle, setTakeOffTitle] = useState("");
  const [describeText, setDescribeText] = useState("");
  const [describeNote, setDescribeNote] = useState("");
  const [preview, setPreview] = useState<CronPreview | null>(null);
  const dragFrom = useRef<number | null>(null);
  const [dragOver, setDragOver] = useState<number | null>(null);
  const blockRefs = useRef<Array<HTMLDivElement | null>>([]);

  const dirty = planDirty(draft, seed);
  useEffect(() => onDirty(dirty), [dirty, onDirty]);

  const load = (p: FlightPlan | null) => {
    const copy = p ? clonePlan(p) : blankPlan();
    setSelected(p ? p.id : "");
    setDraft(copy);
    setSeed(clonePlan(copy));
    setBlock(copy.steps.length ? 0 : null);
    setPaletteAt(null);
    setDescribed(new Set());
    setError("");
    setConfirmDelete(false);
    setAskTakeOff(false);
    setDescribeNote("");
  };

  // First load: the plan a deep link named (#flights/plans/<id>), else the first.
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

  if (plansQ.data === null)
    return <p className="fl-note muted">This server has no flight plans yet — update MindFlock to edit them.</p>;
  if (!draft) return <p className="fl-note muted">{plansQ.error ? "Could not load the plans: " + errMsg(plansQ.error) : "Loading plans…"}</p>;

  const patch = (p: Partial<FlightPlan>) => setDraft((d) => (d ? { ...d, ...p } : d));
  const setSteps = (steps: FlightStep[]) => patch({ steps });
  const patchStep = (i: number, p: Partial<FlightStep>) =>
    setSteps(draft.steps.map((s, j) => (j === i ? { ...s, ...p } : s)));
  const sel = block !== null ? draft.steps[block] : null;
  const selKind = sel ? kindOf.get(sel.kind) : undefined;
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
    setBlock(to);
    requestAnimationFrame(() => blockRefs.current[to]?.focus());
  };

  const insert = (at: number, def: StepKindDef) => {
    const step = newStep(def, draft.steps.map((s) => s.id));
    const steps = draft.steps.slice();
    steps.splice(at, 0, step);
    setSteps(steps);
    setBlock(at);
    setPaletteAt(null);
  };

  const remove = (i: number) => {
    setSteps(draft.steps.filter((_, j) => j !== i));
    setBlock(draft.steps.length > 1 ? Math.max(0, i - 1) : null);
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
        const at = blockOfField(body.field, draft.steps);
        if (at >= 0) setBlock(at);
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

  const describe = async () => {
    const text = describeText.trim();
    if (!text) return;
    setBusy("describe");
    setDescribeNote("");
    try {
      const got = await describeSteps(text, "step");
      if (!got.length) {
        setDescribeNote("No block came back — try saying it another way.");
        return;
      }
      // Fresh local ids, so a described block never collides with one here.
      const taken = draft.steps.map((s) => s.id);
      const added = got.map((s) => {
        let n = taken.length + 1;
        while (taken.includes("s" + n)) n++;
        const id = taken.includes(s.id) || !s.id ? "s" + n : s.id;
        taken.push(id);
        return { ...s, id, config: s.config || {}, on_fail: s.on_fail || "halt" };
      });
      setSteps([...draft.steps, ...added]);
      setDescribed((d) => new Set([...d, ...added.map((s) => s.id)]));
      setBlock(draft.steps.length);
      setDescribeText("");
    } catch (err) {
      const st = err instanceof ApiError ? err.status : 0;
      const code = err instanceof ApiError ? (err.body as { error?: string } | null)?.error : "";
      setDescribeNote(
        code === "no_model" || st === 409
          ? "No agent CLI is set up to describe steps — add one in Settings, or pick blocks with +."
          : code === "could_not_describe" || st === 422
            ? "Couldn't turn that into blocks — try saying it another way."
            : errMsg(err)
      );
    } finally {
      setBusy("");
    }
  };

  const onBlockKey = (e: KeyboardEvent, i: number) => {
    if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
      e.preventDefault();
      move(i, i + (e.key === "ArrowLeft" ? -1 : 1));
    } else if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      setBlock(i);
    }
  };

  const onDrop = (e: DragEvent, to: number) => {
    e.preventDefault();
    const from = dragFrom.current;
    dragFrom.current = null;
    setDragOver(null);
    if (from === null) return;
    move(from, from < to ? to - 1 : to);
  };

  const addButton = (at: number) => (
    <button
      type="button"
      className={"fl-add" + (paletteAt === at ? " active" : "") + (dragOver === at ? " fl-drop" : "")}
      aria-label={at === draft.steps.length ? "Add a block at the end" : "Add a block here"}
      title="Add a block"
      onClick={() => setPaletteAt(paletteAt === at ? null : at)}
      onDragOver={(e) => {
        if (dragFrom.current === null) return;
        e.preventDefault();
        setDragOver(at);
      }}
      onDragLeave={() => setDragOver(null)}
      onDrop={(e) => onDrop(e, at)}
    >
      +
    </button>
  );

  return (
    <div className="fl-plans" data-tour="flights-plans">
      <aside className="fl-plan-list" aria-label="Plans">
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
              {TRIGGER_LABEL[p.trigger?.kind] || "Manual"} · {p.steps.length} {p.steps.length === 1 ? "block" : "blocks"}
            </span>
          </button>
        ))}
        <button type="button" className={"fl-plan-item fl-plan-new" + (selected === "" ? " active" : "")} onClick={() => pick("")}>
          + New plan
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
          <input
            className="fl-plan-title"
            aria-label="Plan name"
            value={draft.name}
            onChange={(e) => patch({ name: e.target.value })}
          />
          {builtinPlan && <span className="fl-builtin-tag">Built-in</span>}
          {dirty && <span className="fl-unsaved muted">Unsaved</span>}
        </div>

        {/* THE ROW: blocks left to right, + between them, Describe at the end. */}
        <div className="fl-row-scroll">
          <div className="fl-row" role="list" aria-label="Blocks — drag, or ← → on a focused block, to reorder">
            {addButton(0)}
            {draft.steps.map((s, i) => {
              const you = stepActor(s, kinds) === "you";
              return (
                <div key={s.id} className="fl-row-cell">
                  <div
                    ref={(el) => {
                      blockRefs.current[i] = el;
                    }}
                    role="listitem"
                    tabIndex={0}
                    draggable
                    className={
                      "fl-block" +
                      (you ? " fl-block-you" : " fl-block-robot") +
                      (block === i ? " selected" : "") +
                      (described.has(s.id) ? " fl-described" : "")
                    }
                    title={(kindOf.get(s.kind)?.summary || s.kind) + (described.has(s.id) ? " — added by description" : "")}
                    aria-label={s.title + (you ? " (you)" : "") + ", block " + (i + 1) + " of " + draft.steps.length}
                    onClick={() => setBlock(i)}
                    onKeyDown={(e) => onBlockKey(e, i)}
                    onDragStart={(e) => {
                      dragFrom.current = i;
                      e.dataTransfer.effectAllowed = "move";
                      e.dataTransfer.setData("text/plain", s.id);
                    }}
                    onDragEnd={() => {
                      dragFrom.current = null;
                      setDragOver(null);
                    }}
                    onDragOver={(e) => {
                      if (dragFrom.current === null) return;
                      e.preventDefault();
                      setDragOver(i);
                    }}
                    onDrop={(e) => onDrop(e, i)}
                  >
                    <span className="fl-block-kind">{you ? "You" : kindOf.get(s.kind)?.label || s.kind}</span>
                    <span className="fl-block-title">{s.title}</span>
                    {described.has(s.id) && <span className="fl-block-new">added by description</span>}
                  </div>
                  {addButton(i + 1)}
                </div>
              );
            })}
            <div className="fl-describe" data-tour="flights-describe">
              <textarea
                aria-label="Describe a step"
                placeholder="Describe a step… e.g. wait 5 minutes, then ask me to approve"
                rows={2}
                value={describeText}
                onChange={(e) => setDescribeText(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
                    e.preventDefault();
                    void describe();
                  }
                }}
              />
              <button
                type="button"
                className="test-btn fl-btn"
                disabled={!describeText.trim() || busy === "describe"}
                onClick={() => void describe()}
              >
                {busy === "describe" ? "Describing…" : "Describe a step"}
              </button>
            </div>
          </div>
        </div>
        {describeNote && (
          <p className="fl-note fl-describe-note" role="status">
            {describeNote}
          </p>
        )}
        {paletteAt !== null &&
          (kinds.length ? (
            <Palette kinds={kinds} onPick={(k) => insert(paletteAt, k)} onClose={() => setPaletteAt(null)} />
          ) : (
            <p className="fl-note muted">The block list didn't load — this server may not know Flights yet.</p>
          ))}

        {/* The selected block's form. */}
        {sel && block !== null ? (
          <div className="fl-block-form" aria-label={"Block: " + sel.title}>
            <div className="fl-block-form-head">
              <strong>{selKind?.label || sel.kind}</strong>
              <span className="muted">{selKind?.summary}</span>
            </div>
            <label className="fl-field">
              <span className="fl-field-label">Block title</span>
              <input type="text" value={sel.title} onChange={(e) => patchStep(block, { title: e.target.value })} />
            </label>
            {(selKind?.fields || []).map((f) => (
              <FieldInput
                key={f.name}
                f={f}
                value={sel.config?.[f.name]}
                onChange={(v) => patchStep(block, { config: { ...sel.config, [f.name]: v } })}
              />
            ))}
            <label className="fl-field">
              <span className="fl-field-label">If it fails</span>
              <select value={sel.on_fail} onChange={(e) => patchStep(block, { on_fail: e.target.value as OnFail })}>
                {(Object.keys(ON_FAIL_LABEL) as OnFail[]).map((k) => (
                  <option key={k} value={k}>
                    {ON_FAIL_LABEL[k]}
                  </option>
                ))}
              </select>
            </label>
            <div className="fl-block-acts">
              <button type="button" className="test-btn fl-btn" disabled={block === 0} onClick={() => move(block, block - 1)} aria-label="Move left">
                ←
              </button>
              <button
                type="button"
                className="test-btn fl-btn"
                disabled={block === draft.steps.length - 1}
                onClick={() => move(block, block + 1)}
                aria-label="Move right"
              >
                →
              </button>
              <button type="button" className="test-btn fl-btn" onClick={() => setSteps(duplicateStep(draft.steps, block))}>
                Duplicate
              </button>
              <button type="button" className="test-btn fl-btn danger" onClick={() => remove(block)}>
                Delete
              </button>
            </div>
          </div>
        ) : (
          <p className="fl-note muted">{draft.steps.length ? "Pick a block to edit it." : "Press + to add the first block."}</p>
        )}

        {/* Where this plan's flights come from. */}
        <div className="fl-section">
          <label className="fl-field fl-field-inline">
            <span className="fl-field-label">Starts from</span>
            <select
              aria-label="Trigger"
              value={draft.trigger.kind}
              onChange={(e) => {
                const kind = e.target.value as TriggerKind;
                patch({
                  trigger: {
                    ...draft.trigger,
                    kind,
                    schedule: kind === "schedule" ? draft.trigger.schedule || { at: "21:00", days: "daily" } : null,
                  },
                });
              }}
            >
              {(Object.keys(TRIGGER_LABEL) as TriggerKind[]).map((k) => (
                <option key={k} value={k}>
                  {TRIGGER_LABEL[k]}
                </option>
              ))}
            </select>
          </label>
          {draft.trigger.kind === "schedule" && (
            <div className="fl-when">
              <ScheduleWhen
                form={{ ...formFromTemplate(null), ...when } as TemplateForm}
                onForm={(p) => patch({ trigger: { ...draft.trigger, schedule: scheduleOfWhen({ ...when, ...(p as object) }) } })}
                cronPreview={preview}
              />
              <label className="fl-field fl-field-bool">
                <input
                  type="checkbox"
                  checked={draft.trigger.paused}
                  onChange={(e) => patch({ trigger: { ...draft.trigger, paused: e.target.checked } })}
                />
                <span>Paused</span>
              </label>
            </div>
          )}
          {needsSession && (
            <div className="fl-session-defaults">
              <label className="fl-field">
                <span className="fl-field-label">Repository</span>
                <PathField
                  id="fl-plan-repo"
                  value={draft.session.repo_path}
                  placeholder="Ask when it takes off"
                  pickerTitle="The repository this plan's sessions start in"
                  onChange={(v) => patch({ session: { ...draft.session, repo_path: v } })}
                />
              </label>
              <label className="fl-field">
                <span className="fl-field-label">Agent</span>
                <select value={draft.session.program} onChange={(e) => patch({ session: { ...draft.session, program: e.target.value } })}>
                  <option value="">{agents.fallback ? "Default (" + agents.fallback + ")" : "Default"}</option>
                  {agents.names.map((n) => (
                    <option key={n} value={n}>
                      {n}
                    </option>
                  ))}
                </select>
              </label>
              <label className="fl-field">
                <span className="fl-field-label">Model</span>
                <input
                  type="text"
                  value={draft.session.model}
                  placeholder="The agent's default"
                  onChange={(e) => patch({ session: { ...draft.session, model: e.target.value } })}
                />
              </label>
            </div>
          )}
        </div>

        {error && (
          <p className="error fl-error" role="alert">
            {error}
          </p>
        )}
        {askTakeOff && (
          <div className="fl-takeoff-ask" role="group" aria-label="Take off">
            <label className="fl-field">
              <span className="fl-field-label">In a new session, in this repository</span>
              <PathField id="fl-takeoff-repo" value={takeOffRepo} onChange={setTakeOffRepo} placeholder="/path/to/repo" />
            </label>
            <label className="fl-field">
              <span className="fl-field-label">Or on a session that is open</span>
              <select value={takeOffTitle} onChange={(e) => setTakeOffTitle(e.target.value)}>
                <option value="">—</option>
                {instances
                  .filter((i) => !i.flight)
                  .map((i) => (
                    <option key={i.title} value={i.title}>
                      {i.title}
                    </option>
                  ))}
              </select>
            </label>
            <div className="fl-block-acts">
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
            body="Flights already in the air keep their own copy of its blocks."
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
            title={dirty ? "Saves the plan, then starts a flight of it" : "Start a flight of this plan now"}
            onClick={onTakeOff}
          >
            {busy === "takeoff" ? "Taking off…" : "Take off"}
          </button>
          <span className="fl-foot-spacer" />
          {builtinPlan ? (
            <button
              type="button"
              className="test-btn fl-btn"
              disabled={busy !== ""}
              title="Put this built-in plan back the way MindFlock ships it"
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
