/** Right-click on empty sky: add a block by search, placed where you clicked
 * (the owner: "right click and add a block by search so I can just place
 * stuff easier"). A field with the focus, every stop kind and saved stop
 * filtered as you type (label, summary, kind, family); ↑ ↓ pick, Enter or a
 * click places it, Esc closes; "Add a note" stays at its foot. */
import { useMemo, useState, type JSX, type KeyboardEvent } from "react";
import type { FlightPlan, StepKindDef } from "../../lib/flights";
import { savedChainLabel } from "../../lib/stopCatalog";
import { TileIcon } from "./StopIcons";
import { kindName } from "./StopParts";

export type QuickPick = { kind: string } | { plan: FlightPlan };

interface Row {
  key: string;
  label: string;
  hint: string;
  icon: string;
  pick: QuickPick;
}

export function quickRows(kinds: readonly StepKindDef[], plans: readonly FlightPlan[], q: string): Row[] {
  const all: Array<Row & { words: string }> = [
    ...kinds.map((k) => ({ key: "k:" + k.kind, label: kindName(k), hint: k.summary || "", icon: k.kind, pick: { kind: k.kind } as QuickPick, words: [kindName(k), k.label, k.summary, k.kind, k.family].join(" ") })),
    ...plans.map((p) => ({ key: "p:" + p.id, label: p.name, hint: savedChainLabel(p.steps.length), icon: "group", pick: { plan: p } as QuickPick, words: [p.name, "saved chain", p.steps.map((s) => s.title).join(" ")].join(" ") })),
  ];
  const terms = q.toLowerCase().split(/\s+/).filter(Boolean);
  const hit = terms.length ? all.filter((r) => terms.every((t) => r.words.toLowerCase().includes(t))) : all;
  return hit.map(({ words: _w, ...r }) => r);
}

export function QuickAdd(p: {
  x: number;
  y: number;
  kinds: readonly StepKindDef[];
  plans: readonly FlightPlan[];
  onPick(pick: QuickPick): void;
  onNote(): void;
  onClose(): void;
}): JSX.Element {
  const [q, setQ] = useState("");
  const [at, setAt] = useState(0);
  const rows = useMemo(() => quickRows(p.kinds, p.plans, q), [p.kinds, p.plans, q]);
  const sel = Math.min(at, Math.max(0, rows.length - 1));
  const onKey = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setAt((i) => Math.min(rows.length - 1, i + 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setAt((i) => Math.max(0, i - 1));
    } else if (e.key === "Enter") {
      e.preventDefault();
      if (rows[sel]) p.onPick(rows[sel].pick);
    } else if (e.key === "Escape") {
      e.preventDefault();
      e.stopPropagation();
      p.onClose();
    }
  };
  return (
    <div className="sky-ground-menu sky-quick-add" role="dialog" aria-label="Add a stop here" style={{ left: p.x, top: p.y }} onContextMenu={(e) => e.preventDefault()}>
      <input
        className="sky-quick-field"
        autoFocus
        placeholder="Add a stop…"
        aria-label="Search stops"
        value={q}
        onChange={(e) => {
          setQ(e.target.value);
          setAt(0);
        }}
        onKeyDown={onKey}
      />
      <div className="sky-quick-list" role="listbox" aria-label="Stops">
        {rows.slice(0, 40).map((r, i) => (
          <button
            key={r.key}
            type="button"
            role="option"
            aria-selected={i === sel}
            className={"fl-stopmenu-item sky-quick-row" + (i === sel ? " is-sel" : "")}
            onMouseEnter={() => setAt(i)}
            onClick={() => p.onPick(r.pick)}
          >
            <span className="sky-quick-icon" aria-hidden="true">
              <TileIcon kind={r.icon} size={16} />
            </span>
            <span className="sky-quick-text">
              <span className="sky-quick-label">{r.label}</span>
              {r.hint && <span className="sky-quick-hint">{r.hint}</span>}
            </span>
          </button>
        ))}
        {!rows.length && <div className="sky-quick-none">No stop matches “{q}”</div>}
      </div>
      <button type="button" className="fl-stopmenu-item sky-quick-note" onClick={p.onNote}>
        Add a note
      </button>
    </div>
  );
}
