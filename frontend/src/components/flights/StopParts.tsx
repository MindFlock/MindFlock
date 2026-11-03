/** The parts around the canvas: the stop PALETTE (what + opens — "The flock
 * does" robot stops and "You do" perches, each with its summary), the stop
 * FORM (`StopForm`: a stop's title, its kind's fields, its "Only when"
 * condition, on_fail — what the
 * INSPECTOR beside the canvas and a session line's StopDialog both draw), and
 * DESCRIBE (a sentence → new stops, through the server's
 * one-shot model). Shared by the Flyways editor and the session diagram. */

import { useEffect, useId, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { ApiError } from "../../api/client";
import { errMsg } from "../../lib/format";
import { LANE_LABEL, normalizeLane } from "../../lib/autopilot";
import { ON_FAIL_LABEL, defaultOnFail, fieldOptions, stopsBefore, type Flight, type FlightPlan, type FlightStep, type OnFail, type StepKindDef, type StepKindField, type StepRunState, type StopChoice } from "../../lib/flights";
import type { Instance } from "../../api/types";
import { queryClient } from "../../state/queries";
import { groundedFlights } from "../../lib/groundedLines";
import { windowName } from "../../lib/windowName";
import { stopSummary } from "../../lib/flightDiagram";
import { catalogMatch } from "../../lib/stopCatalog";
import { describeSteps } from "../../lib/flightsApi";
import { WHEN_COMBINE, WHEN_GRAMMAR, WHEN_MAX, checkWhen, skippedNote, whenChecker, whenError, type WhenCheck } from "../../lib/whenHelp";
import { StopIcon } from "./Bird";
import { ChipRow, FlField, NumberField, PromptBox, Seg, TextField, Toggle, DurationField } from "./controls";
import "./Flights.css";
import "./StopParts.css";

/** A kind's name as the palette and the inspector say it: a `you` stop is a
 * perch (an older server still calls it "You"), a `someone` stop waits on
 * Someone (round 15). */
export function kindName(def: Pick<StepKindDef, "kind" | "label"> | undefined, kind = ""): string {
  const k = def?.kind || kind;
  if (k === "you") return "Wait for me";
  if (k === "someone") return "Someone";
  return def?.label || kind;
}

// --- The palette -------------------------------------------------------------------

/** A saved chain the palette offers under the two groups ("A saved chain —
 * all of its stops"): its name on the chip, `title` its stops as a hover
 * line ("a → b → c"), which the filter reads too. */
export interface PaletteSaved {
  id: string;
  name: string;
  title?: string;
  disabled?: boolean;
  onPick(): void;
}

/** One row the palette shows, in the order it draws them: the "MindFlock
 * does" kinds, the "You do" kinds, then the saved chains. `rank`: how well
 * it matches the filter (0 = the label starts with it … 3 = only found
 * somewhere in it). */
export type PaletteRow<K = StepKindDef, S = PaletteSaved> =
  | { group: "robot" | "you"; kind: K; rank: number }
  | { group: "saved"; saved: S; rank: number };

const escapeRe = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

/** How well `query` matches: -1 none (the floating palette's own rule,
 * `catalogMatch`: every word found, case-blind), 0 `primary` starts with
 * it, 1 a word of `primary` does, 2 a word of another text does, 3 it is
 * only inside one. An empty query is 0 for everything. */
export function paletteRank(query: string, primary: string, ...rest: Array<string | undefined>): number {
  const q = query.trim().toLowerCase();
  if (!q) return 0;
  if (!catalogMatch(q, primary, ...rest)) return -1;
  const atWord = new RegExp("(^|[^\\p{L}\\p{N}])" + escapeRe(q), "u");
  const p = primary.toLowerCase();
  if (p.startsWith(q)) return 0;
  if (atWord.test(p)) return 1;
  if (rest.some((t) => !!t && atWord.test(t.toLowerCase()))) return 2;
  return 3;
}

/** What the palette shows for `query`: the kinds (label as `kindName` says
 * it, kind id, summary) and the saved chains (name, stops) that match, each
 * group best match first (ties keep the server's order). */
export function paletteRows<K extends Pick<StepKindDef, "kind" | "label" | "summary" | "actor">, S extends Pick<PaletteSaved, "name" | "title">>(
  kinds: readonly K[],
  saved: readonly S[],
  query: string
): PaletteRow<K, S>[] {
  const ranked = <T,>(list: readonly T[], rank: (t: T) => number) =>
    list
      .map((t, i) => ({ t, i, r: rank(t) }))
      .filter((x) => x.r >= 0)
      .sort((a, b) => a.r - b.r || a.i - b.i);
  const kindRank = (k: K) => paletteRank(query, kindName(k), k.label, k.kind, k.summary);
  return [
    ...ranked(kinds.filter((k) => k.actor !== "you"), kindRank).map((x): PaletteRow<K, S> => ({ group: "robot", kind: x.t, rank: x.r })),
    ...ranked(kinds.filter((k) => k.actor === "you"), kindRank).map((x): PaletteRow<K, S> => ({ group: "you", kind: x.t, rank: x.r })),
    ...ranked(saved, (p) => paletteRank(query, p.name, p.title)).map((x): PaletteRow<K, S> => ({ group: "saved", saved: x.t, rank: x.r })),
  ];
}

/** The row the highlight starts on: the best match, the first drawn among
 * equals (with no query, the first row) — -1 when nothing matches. */
export function bestRow(rows: readonly Pick<PaletteRow, "rank">[]): number {
  let best = -1;
  rows.forEach((r, i) => {
    if (best < 0 || r.rank < rows[best].rank) best = i;
  });
  return best;
}

/** What a key in the palette's filter does: ↓ / ↑ move the highlight
 * through the rows (round the ends), Enter picks the highlighted row, Esc
 * clears the text, or with none closes. null: the key is the field's own
 * (it types). */
export function paletteKey(key: string, query: string, hi: number, count: number): { hi?: number; query?: string; pick?: number; close?: boolean } | null {
  if (key === "ArrowDown") return count ? { hi: hi < 0 ? 0 : (hi + 1) % count } : {};
  if (key === "ArrowUp") return count ? { hi: hi <= 0 ? count - 1 : hi - 1 } : {};
  if (key === "Enter") return hi >= 0 && hi < count ? { pick: hi } : {};
  if (key === "Escape") return query ? { query: "" } : { close: true };
  return null;
}

export function StopPalette({
  kinds,
  onPick,
  onClose,
  title = "Add a stop",
  saved = [],
  savedHead = "A saved chain — all of its stops",
  extra,
  initialQuery = "",
}: {
  kinds: readonly StepKindDef[];
  onPick(k: StepKindDef): void;
  onClose(): void;
  title?: string;
  /** The saved chains, as chips under the two groups; the filter narrows
   * them with the kinds. */
  saved?: readonly PaletteSaved[];
  /** The saved chips' heading. */
  savedHead?: string;
  /** More under the groups, drawn as given (not filtered) — prefer `saved`. */
  extra?: ReactNode;
  /** The filter's text when the palette opens (empty: everything). */
  initialQuery?: string;
}) {
  const [query, setQuery] = useState(initialQuery);
  const [hiPick, setHi] = useState<number | null>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const rows = paletteRows(kinds, saved, query);
  // Typing puts the highlight back on the best match.
  const hi = hiPick !== null && hiPick < rows.length ? hiPick : bestRow(rows);
  useEffect(() => {
    listRef.current?.querySelector(".is-hi")?.scrollIntoView?.({ block: "nearest" });
  }, [hi, query]);
  const pick = (r: PaletteRow) => {
    if (r.group !== "saved") onPick(r.kind);
    else if (!r.saved.disabled) r.saved.onPick();
  };
  const typed = query.trim();
  const uid = useId();
  const listId = uid + "-list";
  const rowId = (n: number) => uid + "-row-" + n;

  const onFilterKey = (e: KeyboardEvent<HTMLInputElement>) => {
    // The Sky's own keys (T tidies, arrows pan, Delete removes…) never see
    // what is typed here.
    e.stopPropagation();
    if (e.nativeEvent.isComposing) return;
    const out = paletteKey(e.key, query, hi, rows.length);
    if (!out) return;
    e.preventDefault();
    if (out.hi !== undefined) setHi(out.hi);
    if (out.query !== undefined) {
      setQuery(out.query);
      setHi(null);
    }
    if (out.pick !== undefined && rows[out.pick]) pick(rows[out.pick]);
    if (out.close) onClose();
  };

  const groups: Array<[PaletteRow["group"], string]> = [
    ["robot", "MindFlock does"],
    ["you", "You do"],
  ];
  const numbered = rows.map((r, n) => ({ r, n }));
  const chips = numbered.filter((x) => x.r.group === "saved");
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
      {kinds.length === 0 && saved.length === 0 ? (
        <p className="fl-note muted">The stop list didn't load — this server may not know chains yet.</p>
      ) : (
        <input
          className="fl-input fl-palette-filter"
          type="text"
          placeholder="Type to filter…"
          aria-label="Filter stops"
          role="combobox"
          aria-expanded="true"
          aria-autocomplete="list"
          aria-controls={listId}
          aria-activedescendant={hi >= 0 ? rowId(hi) : undefined}
          autoComplete="off"
          spellCheck={false}
          autoFocus
          value={query}
          onChange={(e) => {
            setQuery(e.target.value);
            setHi(null);
          }}
          onKeyDown={onFilterKey}
        />
      )}
      <div className="fl-palette-list" id={listId} role="listbox" aria-label="Stops" ref={listRef}>
        <div className="fl-palette-groups">
          {groups.map(([id, head]) => {
            const list = numbered.filter((x) => x.r.group === id);
            return list.length ? (
              <div className={"fl-palette-group fl-palette-" + id} key={id}>
                <div className="fl-palette-head">{head}</div>
                {list.map(({ r, n }) =>
                  r.group === "saved" ? null : (
                    <button
                      key={r.kind.kind}
                      id={rowId(n)}
                      type="button"
                      role="option"
                      aria-selected={n === hi}
                      className={"fl-palette-item fl-kind-" + r.kind.actor + (n === hi ? " is-hi" : "")}
                      onClick={() => onPick(r.kind)}
                    >
                      <span className="fl-palette-icon">
                        <StopIcon kind={r.kind.actor === "you" ? "perch" : r.kind.kind} />
                      </span>
                      <span className="fl-palette-words">
                        <span className="fl-palette-label">{kindName(r.kind)}</span>
                        <span className="fl-palette-summary">{r.kind.summary}</span>
                      </span>
                    </button>
                  )
                )}
              </div>
            ) : null;
          })}
        </div>
        {chips.length > 0 && (
          <div className="fl-palette-flyways">
            <div className="fl-palette-head">{savedHead}</div>
            <div className="fl-chiprow">
              {chips.map(({ r, n }) =>
                r.group === "saved" ? (
                  <button
                    key={r.saved.id}
                    id={rowId(n)}
                    type="button"
                    role="option"
                    aria-selected={n === hi}
                    className={"fl-pick" + (n === hi ? " is-hi" : "")}
                    disabled={r.saved.disabled}
                    title={r.saved.title}
                    onClick={() => r.saved.onPick()}
                  >
                    {r.saved.name}
                  </button>
                ) : null
              )}
            </div>
          </div>
        )}
        {typed && rows.length === 0 && <p className="fl-note muted fl-palette-none">No stop matches “{typed}”</p>}
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

/** Round 18: a select the UI fills from the board (the Handoff's fields,
 * docs/flights.md "One chain triggers another"): `"chains"` — the board's
 * stops-only chains ("flight:<id>") then the saved stops ("plan:<id>");
 * `"sessions"` — "a new session" ("new") then every session's title. The
 * kinds' type still says only "stops_before", so it is read as a string. */
export type BoardDynamic = "chains" | "sessions";
export const boardDynamic = (f: Pick<StepKindField, "dynamic">): BoardDynamic | "" => {
  const d = String(f.dynamic || "");
  return d === "chains" || d === "sessions" ? d : "";
};

/** A Handoff's "Start which chain" choices: the stops-only chains on the
 * board (by their name), then the saved stops (each start makes a fresh
 * copy). */
export function chainChoices(flights: readonly Flight[] | null | undefined, plans: readonly FlightPlan[] | null | undefined): StopChoice[] {
  const out: StopChoice[] = [];
  for (const f of groundedFlights(flights)) out.push({ value: "flight:" + f.id, label: f.plan_name || "Chain", group: "On the board" });
  for (const p of plans || []) out.push({ value: "plan:" + p.id, label: p.name, group: "Saved chains — a fresh copy each time" });
  return out;
}

/** A Handoff's "Run it on" choices: a new session first, then every open
 * session (a pending one too). */
export function sessionChoices(rows: readonly Pick<Instance, "title">[] | null | undefined, nameOf: (t: string) => string = (t) => t): StopChoice[] {
  return [{ value: "new", label: "a new session" }, ...(rows || []).map((r) => ({ value: r.title, label: nameOf(r.title) }))];
}

/** The board's choices for a `dynamic` select, from what the app has loaded. */
export function boardChoices(d: BoardDynamic): StopChoice[] {
  if (d === "chains") return chainChoices(queryClient.getQueryData<Flight[] | null>(["flights"]), queryClient.getQueryData<FlightPlan[] | null>(["flight-plans"]));
  return sessionChoices(queryClient.getQueryData<Instance[]>(["instances"]), windowName);
}

/** One field, drawn with the app's controls. `choices`: a `dynamic`
 * select's options, filled from the line (`stopsBefore`) or the board. */
export function StopField({
  f,
  value,
  onChange,
  readOnly,
  choices,
}: {
  f: StepKindField;
  value: unknown;
  onChange(v: unknown): void;
  readOnly?: boolean;
  choices?: readonly StopChoice[];
}) {
  const v = value === undefined ? f.default : value;
  const label = cleanLabel(f);
  if (f.type === "select" && f.dynamic === "stops_before")
    return <StopsBeforeField label={label} value={String(v ?? "")} choices={choices || stopsBefore([], { id: "" })} readOnly={readOnly} onChange={onChange} />;
  const board = f.type === "select" ? boardDynamic(f) : "";
  if (board) return <BoardChoiceField label={label} kind={board} value={String(v ?? "")} choices={choices || boardChoices(board)} readOnly={readOnly} onChange={onChange} />;
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
  if (f.type === "duration")
    return (
      <FlField label={label}>
        <DurationField label={label} value={v} onChange={(x) => !readOnly && onChange(x)} />
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

/** A `dynamic: "stops_before"` select (a Loop's "Repeat from"): a chip per
 * stop before this one, in flight order — "the first stop" first, a group's
 * sub-steps in a row under the group's name. A value that is no longer
 * before it (moved, removed) shows as its own chip so the choice stays
 * visible until it is changed. */
export function StopsBeforeField({
  label,
  value,
  choices,
  onChange,
  readOnly,
}: {
  label: string;
  value: string;
  choices: readonly StopChoice[];
  onChange(v: string): void;
  readOnly?: boolean;
}) {
  const runs: Array<{ group: string; items: StopChoice[] }> = [];
  for (const c of choices) {
    const g = c.group || "";
    const last = runs[runs.length - 1];
    if (last && last.group === g) last.items.push(c);
    else runs.push({ group: g, items: [c] });
  }
  if (value && !choices.some((c) => c.value === value)) runs.push({ group: "", items: [{ value, label: "a stop no longer before this one" }] });
  const pick = (x: string) => !readOnly && onChange(x);
  return (
    <FlField label={label} wide hint="The chain goes back to this stop for each new run.">
      <div className="fl-stops-before" data-dynamic="stops_before">
        {runs.map((r, i) => (
          <div key={i} className={"fl-stops-before-run" + (r.group ? " fl-stops-before-group" : "")}>
            {r.group && <span className="fl-stops-before-name">{r.group}</span>}
            <ChipRow
              label={r.group ? label + " — " + r.group : label}
              value={value}
              options={r.items.map((c) => ({ value: c.value, label: c.label, title: c.value ? undefined : "Where the chain (or this group) starts" }))}
              onChange={pick}
            />
          </div>
        ))}
      </div>
    </FlField>
  );
}

/** A `dynamic: "chains" | "sessions"` select (round 18's Handoff): a chip per
 * choice, grouped as `StopsBeforeField` groups — a value no longer on the
 * board shows as its own chip so it stays visible until changed. */
export function BoardChoiceField({
  label,
  kind,
  value,
  choices,
  onChange,
  readOnly,
}: {
  label: string;
  kind: BoardDynamic;
  value: string;
  choices: readonly StopChoice[];
  onChange(v: string): void;
  readOnly?: boolean;
}) {
  const runs: Array<{ group: string; items: StopChoice[] }> = [];
  for (const c of choices) {
    const g = c.group || "";
    const last = runs[runs.length - 1];
    if (last && last.group === g) last.items.push(c);
    else runs.push({ group: g, items: [c] });
  }
  if (value && !choices.some((c) => c.value === value)) runs.push({ group: "", items: [{ value, label: kind === "chains" ? "a chain no longer on the board" : value }] });
  const pick = (x: string) => !readOnly && onChange(x);
  const hint = kind === "chains" ? "A chain on the board takes off (it leaves the board); a saved chain starts a fresh copy each time." : "Where the chain runs: a new session forks from this one.";
  return (
    <FlField label={label} wide hint={hint}>
      <div className="fl-stops-before fl-board-choice" data-dynamic={kind}>
        {!choices.length && <p className="fl-note muted">{kind === "chains" ? "No chain on the board yet — drop some stops on empty sky first." : "No session yet."}</p>}
        {runs.map((r, i) => (
          <div key={i} className={"fl-stops-before-run" + (r.group ? " fl-stops-before-group" : "")}>
            {r.group && <span className="fl-stops-before-name">{r.group}</span>}
            <ChipRow label={r.group ? label + " — " + r.group : label} value={value} options={r.items.map((c) => ({ value: c.value, label: c.label }))} onChange={pick} />
          </div>
        ))}
      </div>
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
  run,
  lineSteps,
}: {
  step: FlightStep;
  def: StepKindDef | undefined;
  onChange(patch: Partial<FlightStep>): void;
  readOnly?: boolean;
  /** The line's steps in flight order: a `dynamic` field's options. */
  lineSteps?: readonly FlightStep[] | null;
  /** The row of buttons at its foot (Move, Duplicate, Remove / Save). */
  actions?: ReactNode;
  note?: ReactNode;
  /** What the flight did with it: a skipped stop reads its note ("skipped:
   * <expr> — <the fact>"). */
  run?: Pick<StepRunState, "state" | "note">;
}) {
  const perch = (def?.actor || (step.kind === "you" ? "you" : "robot")) === "you";
  const skipped = skippedNote(run);
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
      {skipped && <p className="fl-note muted fl-skipped-note">{skipped}</p>}
      <StopForm step={step} def={def} onChange={onChange} readOnly={readOnly} lineSteps={lineSteps} />
      {actions && <div className="fl-inspector-acts">{actions}</div>}
    </section>
  );
}

/** A stop's form: its title, its kind's own fields, the condition it flies
 * on, what happens if it fails (the inspector's and the stop dialog's) —
 * the kind's `default_on_fail` pressed when the stop says nothing.
 * `readOnly` ignores edits. */
export function StopForm({
  step,
  def,
  onChange,
  readOnly,
  lineSteps,
}: {
  step: FlightStep;
  def: StepKindDef | undefined;
  onChange(patch: Partial<FlightStep>): void;
  readOnly?: boolean;
  /** The line's steps in flight order (a session's line, a flyway being
   * drawn): what a `dynamic: "stops_before"` field lists. */
  lineSteps?: readonly FlightStep[] | null;
}) {
  const fallback = defaultOnFail(def);
  const setField = (name: string, v: unknown) => onChange({ config: { ...step.config, [name]: v } });
  return (
    <div className="fl-form">
      <FlField label="Title" wide={false}>
        <TextField label="Stop title" value={step.title} onChange={(t) => !readOnly && onChange({ title: t })} />
      </FlField>
      {(def?.fields || []).map((f) =>
        step.kind === "loop" && f.name === "until" && f.type !== "select" && f.type !== "bool" && f.type !== "int" ? (
          // A Loop's "until" is a `when` expression (round 15's grammar):
          // the same editor, checked by the server as it is typed.
          <WhenField
            key={f.name}
            label={cleanLabel(f)}
            placeholder={f.placeholder}
            hint="Empty: only the count stops it. ⓘ lists the conditions."
            value={String(step.config?.[f.name] ?? "")}
            readOnly={readOnly}
            onChange={(w) => setField(f.name, w)}
          />
        ) : (
          <StopField
            key={f.name}
            f={f}
            value={step.config?.[f.name]}
            readOnly={readOnly}
            choices={f.dynamic === "stops_before" ? stopsBefore(lineSteps, step) : undefined}
            onChange={(v) => setField(f.name, v)}
          />
        )
      )}
      {step.kind === "agent" && (!!step.config?.program || !!step.config?.model) && !step.config?.fresh && (
        // Switch pilot (docs/flights.md "Round 15"): another CLI or model
        // only takes the controls in a new session.
        <p className="fl-form-note muted" role="note">
          The CLI and model apply only with “Start a new session for this stop”.
        </p>
      )}
      <WhenField value={step.when || ""} readOnly={readOnly} onChange={(w) => onChange({ when: w || undefined })} />
      <FlField label="If it fails" wide hint={def?.default_on_fail ? "Default for " + kindName(def) + ": " + ON_FAIL_LABEL[fallback] : undefined}>
        <Seg
          label="If it fails"
          value={step.on_fail || fallback}
          options={(Object.keys(ON_FAIL_LABEL) as OnFail[]).map((k) => ({
            v: k,
            label: ON_FAIL_LABEL[k],
            title: k === fallback ? "The default for this kind of stop" : undefined,
            disabled: readOnly,
          }))}
          onChange={(k) => !readOnly && onChange({ on_fail: k })}
        />
      </FlField>
    </div>
  );
}

/** "Only when": the stop's condition, one line, empty for none. ⓘ opens the
 * grammar beside it (inline, never a native alert); the server checks what
 * is typed 300 ms after the last keystroke — its error under the field — and
 * on blur its canonical form replaces the text. An older server without the
 * validator just keeps the text. */
export function WhenField({
  value,
  onChange,
  readOnly,
  label = "Only when",
  placeholder = "e.g. diff touches frontend/** and weekday",
  hint = "Empty: always. ⓘ lists the conditions.",
}: {
  value: string;
  onChange(v: string): void;
  readOnly?: boolean;
  /** Round 16: the same editor for a Loop's "until". */
  label?: string;
  placeholder?: string;
  hint?: string;
}) {
  const [help, setHelp] = useState(false);
  const [answer, setAnswer] = useState<{ text: string; res: WhenCheck | null } | null>(null);
  const change = useRef(onChange);
  change.current = onChange;
  const checker = useRef<ReturnType<typeof whenChecker> | null>(null);
  if (!checker.current)
    checker.current = whenChecker({
      check: checkWhen,
      onResult: (text, res) => setAnswer({ text, res }),
      onCanonical: (c) => change.current(c),
    });
  useEffect(() => () => checker.current?.cancel(), []);
  const err = whenError(value, answer?.text === value ? answer.res : null);
  return (
    <FlField label={label} wide hint={err ? undefined : hint}>
      <span className="fl-when-row">
        <input
          type="text"
          className={"fl-input fl-mono fl-when-input" + (err ? " fl-when-bad" : "")}
          aria-label={label}
          aria-invalid={err ? true : undefined}
          value={value}
          maxLength={WHEN_MAX}
          placeholder={placeholder || "e.g. diff touches frontend/** and weekday"}
          spellCheck={false}
          readOnly={readOnly}
          onChange={(e) => {
            if (readOnly) return;
            onChange(e.target.value);
            checker.current?.input(e.target.value);
          }}
          onFocus={() => checker.current?.focus()}
          onBlur={(e) => !readOnly && checker.current?.blur(e.target.value)}
        />
        <button
          type="button"
          className="fl-icon-btn fl-when-help-btn"
          aria-label="What can go here"
          title="What can go here"
          aria-expanded={help}
          onClick={() => setHelp((h) => !h)}
        >
          ⓘ
        </button>
      </span>
      {err && (
        <span className="fl-when-error" role="alert">
          {err}
        </span>
      )}
      {help && <WhenHelp onClose={() => setHelp(false)} />}
    </FlField>
  );
}

/** The ⓘ popover: the grammar, one row per condition. */
function WhenHelp({ onClose }: { onClose(): void }) {
  return (
    <div
      className="fl-when-help"
      role="dialog"
      aria-label="Only when — the conditions"
      onKeyDown={(e) => {
        if (e.key === "Escape") {
          e.stopPropagation();
          e.preventDefault();
          onClose();
        }
      }}
    >
      <div className="fl-when-help-top">
        <strong>The stop runs only when…</strong>
        <button type="button" className="fl-icon-btn" aria-label="Close" title="Close" autoFocus onClick={onClose}>
          ✕
        </button>
      </div>
      <dl className="fl-when-grammar">
        {WHEN_GRAMMAR.map((r) => (
          <div key={r.condition} className="fl-when-gram-row">
            <dt>
              <code>{r.condition}</code>
            </dt>
            <dd className="muted">{r.meaning}</dd>
          </div>
        ))}
      </dl>
      <p className="fl-note muted">{WHEN_COMBINE} A stop whose condition is false is skipped, never run.</p>
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
