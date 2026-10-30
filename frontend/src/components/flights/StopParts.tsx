/** The parts around the canvas: the stop PALETTE (what + opens — "The flock
 * does" robot stops and "You do" perches, each with its summary), the stop
 * FORM (`StopForm`: a stop's title, its kind's fields, on_fail — what the
 * INSPECTOR beside the canvas and a session line's StopDialog both draw), and
 * DESCRIBE (a sentence → new stops, through the server's
 * one-shot model). Shared by the Flyways editor and the session diagram. */

import { useState, type ReactNode } from "react";
import { ApiError } from "../../api/client";
import { errMsg } from "../../lib/format";
import { LANE_LABEL, normalizeLane } from "../../lib/autopilot";
import { ON_FAIL_LABEL, fieldOptions, type FlightStep, type OnFail, type StepKindDef, type StepKindField } from "../../lib/flights";
import { stopSummary } from "../../lib/flightDiagram";
import { describeSteps } from "../../lib/flightsApi";
import { StopIcon } from "./Bird";
import { FlField, NumberField, PromptBox, Seg, TextField, Toggle } from "./controls";
import "./Flights.css";
import "./StopParts.css";

/** A kind's name as the palette and the inspector say it: a `you` stop is a
 * perch (an older server still calls it "You"). */
export function kindName(def: Pick<StepKindDef, "kind" | "label"> | undefined, kind = ""): string {
  if ((def?.kind || kind) === "you") return "Perch";
  return def?.label || kind;
}

// --- The palette -------------------------------------------------------------------

export function StopPalette({
  kinds,
  onPick,
  onClose,
  title = "Add a stop",
  extra,
}: {
  kinds: readonly StepKindDef[];
  onPick(k: StepKindDef): void;
  onClose(): void;
  title?: string;
  /** More rows under the two groups (the session diagram's "Fly a flyway"). */
  extra?: ReactNode;
}) {
  const groups: Array<[string, string, StepKindDef[]]> = [
    ["robot", "The flock does", kinds.filter((k) => k.actor !== "you")],
    ["you", "You do", kinds.filter((k) => k.actor === "you")],
  ];
  return (
    <div
      className="fl-palette"
      role="dialog"
      aria-label={title}
      onKeyDown={(e) => {
        if (e.key === "Escape") {
          e.stopPropagation();
          e.preventDefault();
          onClose();
        }
      }}
    >
      <div className="fl-palette-top">
        <strong>{title}</strong>
        <button type="button" className="fl-icon-btn" aria-label="Close" title="Close" onClick={onClose}>
          ✕
        </button>
      </div>
      {kinds.length === 0 && <p className="fl-note muted">The stop list didn't load — this server may not know Flights yet.</p>}
      <div className="fl-palette-groups">
        {groups.map(([id, head, list]) =>
          list.length ? (
            <div className={"fl-palette-group fl-palette-" + id} key={id}>
              <div className="fl-palette-head">{head}</div>
              {list.map((k, i) => (
                <button
                  key={k.kind}
                  type="button"
                  className={"fl-palette-item fl-kind-" + k.actor}
                  autoFocus={i === 0 && id === "robot"}
                  onClick={() => onPick(k)}
                >
                  <span className="fl-palette-icon">
                    <StopIcon kind={k.actor === "you" ? "perch" : k.kind} />
                  </span>
                  <span className="fl-palette-words">
                    <span className="fl-palette-label">{kindName(k)}</span>
                    <span className="fl-palette-summary">{k.summary}</span>
                  </span>
                </button>
              ))}
            </div>
          ) : null
        )}
      </div>
      {extra}
    </div>
  );
}

// --- The inspector -----------------------------------------------------------------

/** A select's choices in the words the app already uses for them. */
function choiceLabel(field: string, value: string, label: string): string {
  if (field === "lane") {
    const lane = normalizeLane(value);
    return lane ? LANE_LABEL[lane] : label;
  }
  if (field === "who") return value === "agent" ? "The agent" : value === "you" ? "You" : value === "both" ? "Both" : label;
  return label;
}

/** A number field's unit, from its name. */
export function unitOf(f: Pick<StepKindField, "name" | "label">): string {
  if (/minutes$/.test(f.name) || /minutes/i.test(f.label)) return "min";
  if (/rounds/.test(f.name)) return "rounds";
  return "";
}

/** A field's label without the unit its control already shows. */
function cleanLabel(f: StepKindField): string {
  return f.label.replace(/\s*\((minutes|one per line)\)\s*$/i, "");
}

/** One field, drawn with the app's controls. */
export function StopField({ f, value, onChange, readOnly }: { f: StepKindField; value: unknown; onChange(v: unknown): void; readOnly?: boolean }) {
  const v = value === undefined ? f.default : value;
  const label = cleanLabel(f);
  if (f.type === "bool")
    return (
      <div className="fl-fld">
        <Toggle on={!!v} onChange={(x) => !readOnly && onChange(x)} label={f.label} />
      </div>
    );
  if (f.type === "select")
    return (
      <FlField label={label}>
        <Seg
          label={label}
          value={String(v ?? "")}
          options={fieldOptions(f).map((o) => ({ v: o.value, label: choiceLabel(f.name, o.value, o.label), disabled: readOnly }))}
          onChange={(x) => onChange(x)}
        />
      </FlField>
    );
  if (f.type === "int")
    return (
      <FlField label={label}>
        <NumberField label={label} unit={unitOf(f)} value={v} onChange={(x) => !readOnly && onChange(x)} />
      </FlField>
    );
  if (f.type === "textarea") {
    const items = f.name === "items";
    const text = Array.isArray(v) ? v.map(String).join("\n") : String(v ?? "");
    return (
      <FlField label={label} wide hint={items ? "One item per line — each becomes a tick box." : undefined}>
        <PromptBox
          label={label}
          value={text}
          rows={f.name === "prompt" ? 4 : 3}
          mono={f.name === "command"}
          placeholder={f.placeholder}
          onChange={(x) => !readOnly && onChange(x)}
        />
      </FlField>
    );
  }
  return (
    <FlField label={label}>
      <TextField label={label} value={String(v ?? "")} placeholder={f.placeholder} onChange={(x) => !readOnly && onChange(x)} />
    </FlField>
  );
}

/** The selected stop's fields: its title, its kind's own fields, and what
 * happens if it fails. `readOnly` draws a done / current stop of a live
 * flight, which can't be edited. */
export function StopInspector({
  step,
  def,
  onChange,
  readOnly,
  actions,
  note,
}: {
  step: FlightStep;
  def: StepKindDef | undefined;
  onChange(patch: Partial<FlightStep>): void;
  readOnly?: boolean;
  /** The row of buttons at its foot (Move, Duplicate, Remove / Save). */
  actions?: ReactNode;
  note?: ReactNode;
}) {
  const perch = (def?.actor || (step.kind === "you" ? "you" : "robot")) === "you";
  return (
    <section className={"fl-inspector" + (perch ? " fl-inspector-perch" : "") + (readOnly ? " fl-readonly" : "")} aria-label={"Stop: " + step.title}>
      <header className="fl-inspector-head">
        <span className="fl-box-icon">
          <StopIcon kind={perch ? "perch" : step.kind} />
        </span>
        <span className="fl-inspector-kind">
          <strong>{kindName(def, step.kind)}</strong>
          <span className="muted">{def?.summary || stopSummary(step)}</span>
        </span>
      </header>
      {note}
      <StopForm step={step} def={def} onChange={onChange} readOnly={readOnly} />
      {actions && <div className="fl-inspector-acts">{actions}</div>}
    </section>
  );
}

/** A stop's form: its title, its kind's own fields, what happens if it
 * fails (the inspector's and the stop dialog's). `readOnly` ignores edits. */
export function StopForm({ step, def, onChange, readOnly }: { step: FlightStep; def: StepKindDef | undefined; onChange(patch: Partial<FlightStep>): void; readOnly?: boolean }) {
  return (
    <div className="fl-form">
      <FlField label="Title" wide={false}>
        <TextField label="Stop title" value={step.title} onChange={(t) => !readOnly && onChange({ title: t })} />
      </FlField>
      {(def?.fields || []).map((f) => (
        <StopField
          key={f.name}
          f={f}
          value={step.config?.[f.name]}
          readOnly={readOnly}
          onChange={(v) => onChange({ config: { ...step.config, [f.name]: v } })}
        />
      ))}
      <FlField label="If it fails" wide>
        <Seg
          label="If it fails"
          value={step.on_fail}
          options={(Object.keys(ON_FAIL_LABEL) as OnFail[]).map((k) => ({ v: k, label: ON_FAIL_LABEL[k], disabled: readOnly }))}
          onChange={(k) => !readOnly && onChange({ on_fail: k })}
        />
      </FlField>
    </div>
  );
}

// --- Describe ------------------------------------------------------------------------

/** "Describe a stop…": a sentence → stops (the server's one-shot model, the
 * result through the flyway validator). `onStops` gets them with fresh ids. */
export function DescribeField({
  want = "step",
  taken,
  onStops,
  placeholder = "Describe a stop… e.g. wait 5 minutes, then ask me to approve",
  button = "Describe",
  dataTour,
}: {
  want?: "step" | "plan";
  /** Ids already used (a described stop never collides with one). */
  taken: readonly string[];
  onStops(steps: FlightStep[]): void | Promise<void>;
  placeholder?: string;
  button?: string;
  dataTour?: string;
}) {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState("");
  const go = async () => {
    const t = text.trim();
    if (!t || busy) return;
    setBusy(true);
    setNote("");
    try {
      const got = await describeSteps(t, want);
      if (!got.length) {
        setNote("Nothing came back — try saying it another way.");
        return;
      }
      const ids = [...taken];
      const fresh = got.map((s) => {
        let n = ids.length + 1;
        while (ids.includes("s" + n)) n++;
        const id = ids.includes(s.id) || !s.id ? "s" + n : s.id;
        ids.push(id);
        return { ...s, id, config: s.config || {}, on_fail: s.on_fail || "halt" };
      });
      await onStops(fresh);
      setText("");
    } catch (err) {
      const st = err instanceof ApiError ? err.status : 0;
      const code = err instanceof ApiError ? (err.body as { error?: string } | null)?.error : "";
      setNote(
        code === "no_model" || st === 409
          ? "No agent CLI is set up to describe stops — add one in Settings, or pick stops with +."
          : code === "could_not_describe" || st === 422
            ? "Couldn't turn that into stops — try saying it another way."
            : errMsg(err)
      );
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="fl-describe" data-tour={dataTour}>
      <div className="fl-describe-row">
        <PromptBox label={button === "Describe" ? "Describe a stop" : button} value={text} rows={2} placeholder={placeholder} onChange={setText} onSubmit={() => void go()} />
        <button type="button" className="btn-primary fl-btn" disabled={!text.trim() || busy} onClick={() => void go()}>
          {busy ? "Describing…" : button}
        </button>
      </div>
      {note && (
        <p className="fl-note fl-describe-note" role="status">
          {note}
        </p>
      )}
    </div>
  );
}
