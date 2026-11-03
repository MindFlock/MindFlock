/** Flights' form controls — the app's own, never a native <select> or a bare
 * input. Enum choices are the New dialog's segmented control (`Seg`, the
 * `.rt-seg` look) or, for an open-ended list (the flyways), a wrapping row of
 * pill chips with one highlighted; text is a field with the New dialog's
 * radius, border, background and focus ring; a prompt is its prompt box; a
 * number is a field with its unit beside it ("10 min"); a yes/no is a switch
 * chip. Every control lives under `.fl-form` (FlywayCanvas.css), which mirrors
 * the New dialog's tokens. */

import { useId, useState, type ReactNode } from "react";
import { Seg } from "../dialogs/FastTrackChoice";

export { Seg };

/** One labelled row of the form: the label above, the control under it, a
 * hint below. */
export function FlField({ label, hint, children, wide }: { label: string; hint?: ReactNode; children: ReactNode; wide?: boolean }) {
  return (
    <div className={"fl-fld" + (wide ? " fl-fld-wide" : "")}>
      <span className="fl-fld-label">{label}</span>
      {children}
      {hint && <span className="fl-fld-hint">{hint}</span>}
    </div>
  );
}

/** A one-line text field. */
export function TextField({
  value,
  onChange,
  placeholder,
  label,
  mono,
  id,
  onEnter,
}: {
  value: string;
  onChange(v: string): void;
  placeholder?: string;
  /** Its accessible name (the FlField label above is not a <label>). */
  label: string;
  mono?: boolean;
  id?: string;
  onEnter?(): void;
}) {
  return (
    <input
      id={id}
      type="text"
      className={"fl-input" + (mono ? " fl-mono" : "")}
      aria-label={label}
      value={value}
      placeholder={placeholder}
      spellCheck={!mono}
      onChange={(e) => onChange(e.target.value)}
      onKeyDown={(e) => {
        if (e.key === "Enter" && onEnter) {
          e.preventDefault();
          onEnter();
        }
      }}
    />
  );
}

/** The New dialog's prompt box: a roomy multi-line field. Ctrl/⌘+Enter
 * submits when `onSubmit` is given. */
export function PromptBox({
  value,
  onChange,
  placeholder,
  label,
  rows = 3,
  mono,
  onSubmit,
  id,
}: {
  value: string;
  onChange(v: string): void;
  placeholder?: string;
  label: string;
  rows?: number;
  mono?: boolean;
  onSubmit?(): void;
  id?: string;
}) {
  return (
    <textarea
      id={id}
      className={"fl-prompt" + (mono ? " fl-mono" : "")}
      aria-label={label}
      rows={rows}
      value={value}
      placeholder={placeholder}
      spellCheck={!mono}
      onChange={(e) => onChange(e.target.value)}
      onKeyDown={(e) => {
        if (onSubmit && e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
          e.preventDefault();
          onSubmit();
        }
      }}
    />
  );
}

/** A number with its unit: [ 10 ] min. Empty stays empty (the server's
 * default then applies). */
export function NumberField({
  value,
  onChange,
  unit,
  label,
  min = 0,
}: {
  value: unknown;
  onChange(v: number | ""): void;
  unit: string;
  label: string;
  min?: number;
}) {
  const shown = value === undefined || value === null || value === "" ? "" : String(value);
  return (
    <span className="fl-num">
      <input
        type="text"
        inputMode="numeric"
        className="fl-input fl-num-input"
        aria-label={label + (unit ? " (" + unit + ")" : "")}
        value={shown}
        onChange={(e) => {
          const raw = e.target.value.replace(/[^\d]/g, "");
          onChange(raw === "" ? "" : Math.max(min, Math.trunc(Number(raw))));
        }}
      />
      {unit && <span className="fl-num-unit">{unit}</span>}
    </span>
  );
}

/** A yes/no as a switch chip: pressed = on. */
export function Toggle({ on, onChange, label, title }: { on: boolean; onChange(v: boolean): void; label: string; title?: string }) {
  return (
    <button
      type="button"
      className={"fl-toggle" + (on ? " on" : "")}
      aria-pressed={on}
      title={title}
      onClick={() => onChange(!on)}
    >
      <span className="fl-toggle-track" aria-hidden="true">
        <span className="fl-toggle-knob" />
      </span>
      <span className="fl-toggle-label">{label}</span>
    </button>
  );
}

/** A wrapping row of pill chips, one highlighted — for an open-ended list
 * (the flyways) where a joined segmented control would run off the edge. */
export function ChipRow<T extends string>({
  value,
  options,
  onChange,
  label,
  className,
  field,
}: {
  value: T;
  options: ReadonlyArray<{ value: T; label: string; title?: string; builtin?: boolean }>;
  onChange(v: T): void;
  label: string;
  className?: string;
  /** Extra attributes for the group (a card's `data-tk-field="plan"` hook). */
  field?: Record<string, string>;
}) {
  const id = useId();
  return (
    <div className={"fl-chiprow" + (className ? " " + className : "")} role="radiogroup" aria-label={label} id={id} {...field}>
      {options.map((o) => (
        <button
          key={o.value || "default"}
          type="button"
          role="radio"
          aria-checked={o.value === value}
          className={"fl-pick" + (o.value === value ? " on" : "") + (o.value === "" ? " fl-pick-default" : "")}
          title={o.title}
          data-value={o.value}
          onClick={() => onChange(o.value)}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

/** A duration in SECONDS shown as an amount and a unit (seconds / minutes /
 * hours) — the owner wants waits and loop paces in seconds or hours, not only
 * minutes. The unit follows the value (the largest that divides it evenly)
 * until one is picked. */
export function DurationField({ value, onChange, label }: { value: unknown; onChange(seconds: number | ""): void; label: string }) {
  const secs = value === undefined || value === null || value === "" ? null : Math.max(0, Math.trunc(Number(value) || 0));
  const auto: DurationUnit = secs && secs % 3600 === 0 ? "h" : secs && secs % 60 === 0 ? "min" : secs ? "s" : "min";
  const [picked, setPicked] = useState<DurationUnit | null>(null);
  const unit = picked ?? auto;
  const per = DURATION_UNITS[unit];
  const amount = secs === null ? "" : secs % per === 0 ? secs / per : Math.round((secs / per) * 100) / 100;
  return (
    <span className="fl-num fl-duration">
      <input
        type="text"
        inputMode="decimal"
        className="fl-input fl-num-input"
        aria-label={label + " (" + unit + ")"}
        value={String(amount)}
        onChange={(e) => {
          const raw = e.target.value.replace(/[^\d.]/g, "");
          onChange(raw === "" ? "" : Math.max(0, Math.round(Number(raw) * per)));
        }}
      />
      <Seg
        label={label + " unit"}
        value={unit}
        options={[
          { v: "s", label: "sec" },
          { v: "min", label: "min" },
          { v: "h", label: "hours" },
        ]}
        onChange={(x) => {
          const next = x as DurationUnit;
          setPicked(next);
          // The amount stays as typed; the seconds follow the new unit.
          if (secs !== null) onChange(Math.max(0, Math.round(Number(amount) * DURATION_UNITS[next])));
        }}
      />
    </span>
  );
}

export type DurationUnit = "s" | "min" | "h";
export const DURATION_UNITS: Record<DurationUnit, number> = { s: 1, min: 60, h: 3600 };
