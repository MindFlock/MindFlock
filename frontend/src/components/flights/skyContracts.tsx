/** PLACEHOLDERS for the sky's parts that other workers build (Sky round 7):
 *
 *   SkyLine      → components/flights/SkyLine.tsx
 *   SkyFlock     → components/flights/SkyFlock.tsx
 *   FlywayShelf  → components/flights/FlywayShelf.tsx
 *   FlywayCard   → components/flights/FlywayCard.tsx
 *
 * Those paths are theirs, so the sky view (SkyView.tsx) imports the parts
 * from HERE, and each placeholder carries the exact contract its real file
 * exports. At merge this whole file becomes four re-exports:
 *
 *   export * from "./SkyLine";
 *   export * from "./SkyFlock";
 *   export * from "./FlywayShelf";
 *   export * from "./FlywayCard";
 *
 * Until then: SkyLine is the line the Sky drew inline before (title bar, the
 * session diagram, its + / palette / inspector) plus the opened box as the
 * session's real Pane; the flock and the shelf draw nothing; the card only
 * says it is coming. Conventions the host relies on (docs/web-ui.md "Two
 * views"): `onLayout({ pinned: false })` is "Tidy this line"; a shelf chip
 * drags with `FLYWAY_MIME` (lib/skyLayout) carrying the flyway's id. */

import { useEffect, useRef, type JSX, type PointerEvent as ReactPointerEvent } from "react";
import type { Instance } from "../../api/types";
import { useFlightKinds, useFlights } from "../../state/queries";
import { useUi } from "../../state/store";
import { closeSession } from "../../lib/sessionActions";
import { deriveSessionState } from "../../lib/sessionState";
import { birdPerch } from "../../lib/flightDiagram";
import type { FlightPlan } from "../../lib/flights";
import { cycleSize, lineFamily, PANE_H, PANE_W, type SkyLineLayout } from "../../lib/skyLayout";
import { setView, showInSky } from "../../lib/viewMode";
import { OverflowMenu, type MenuItem } from "../OverflowMenu";
import { Pane } from "../grid/Pane";
import type { DragCtx } from "../grid/TerminalGrid";
import { FlywayCanvas } from "./FlywayCanvas";
import { useSessionLine } from "./useSessionLine";
import { Bird } from "./Bird";

export type { SkyLineLayout };

export interface SkyLineProps {
  title: string;
  rows: Instance[];
  plans: FlightPlan[];
  layout: SkyLineLayout;
  onLayout(patch: Partial<SkyLineLayout>): void;
  focused: boolean;
  onFocus(): void;
  zoom: number;
  onDragStart?(e: React.PointerEvent): void;
  onMeasure?(w: number, h: number): void;
  dropFlyway?: FlightPlan | null;
}

/** A pane in the sky is never dragged between grid slots. */
const NO_DRAG: DragCtx = { dragTitle: null, start() {}, hover() {}, commit() {}, end() {} };

const SIZE_LABEL: Record<SkyLineLayout["size"], string> = { compact: "Compact", normal: "Normal", wide: "Wide" };

/** One session's line: the title bar (drag it to move the line), the session
 * diagram with the bird, whatever its + or a stop opened, and — opened — the
 * session's pane. */
export function SkyLine(p: SkyLineProps): JSX.Element {
  const flights = useFlights().data;
  const kinds = useFlightKinds().data || [];
  const family = lineFamily(useUi((s) => s.skyMode));
  const line = useSessionLine(p.title, { rows: p.rows, flights, kinds, plans: p.plans }, { family, onSession: () => p.onLayout({ open: !p.layout.open }) });
  const root = useRef<HTMLDivElement>(null);
  const onMeasure = useRef(p.onMeasure);
  onMeasure.current = p.onMeasure;
  useEffect(() => {
    const el = root.current;
    if (!el) return;
    const tell = () => onMeasure.current?.(el.offsetWidth, el.offsetHeight);
    tell();
    if (typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(tell);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  const row = line.row;
  if (!row) return null as unknown as JSX.Element;
  const ss = deriveSessionState(row);
  const { layout } = p;

  const onBarDown = (e: ReactPointerEvent<HTMLDivElement>) => {
    if (e.button !== 0 || (e.target as Element).closest("button, summary, a, input, [data-no-drag]")) return;
    p.onDragStart?.(e);
  };

  const items: MenuItem[] = [
    { key: "open", label: layout.open ? "Close the box" : "Open the box", title: "The session's pane, right here in the sky (Enter / Esc)", onSelect: () => p.onLayout({ open: !layout.open }) },
    {
      key: "grid",
      label: "Open in the grid",
      title: "Switch to the grid with this session focused",
      onSelect: () => {
        p.onFocus();
        setView("grid");
      },
    },
    { key: "only", label: "Show only this line", title: "This session: one line, filling the sky", onSelect: () => showInSky(p.title) },
    { key: "size", label: "Size: " + SIZE_LABEL[cycleSize(layout.size)], title: "Compact → Normal → Wide", onSelect: () => p.onLayout({ size: cycleSize(layout.size) }) },
    ...(layout.pinned ? [{ key: "tidy", label: "Tidy this line", title: "Forget where you put it — back to its organized spot", onSelect: () => p.onLayout({ pinned: false }) }] : []),
    { key: "flyway", label: "Fly a flyway…", title: "Tack a stop, or a whole flyway, onto this line", onSelect: () => line.togglePalette(true) },
    { key: "close", label: "Close session", danger: true, title: "Stops the agent and moves it to Recently closed", onSelect: () => void closeSession(p.title) },
  ];

  return (
    <div
      ref={root}
      className={
        "sky-line sky-size-" +
        layout.size +
        (line.editing ? " is-editing" : "") +
        (p.focused ? " is-chosen" : "") +
        (layout.open ? " is-open" : "") +
        (p.dropFlyway ? " is-drop-target" : "")
      }
      style={{ transform: `translate(${layout.x}px, ${layout.y}px)` }}
      data-line={p.title}
      role="group"
      aria-label={p.title + "'s line"}
      onPointerDown={(e) => {
        if (!(e.target as Element).closest("button, summary, a, input, textarea")) p.onFocus();
      }}
    >
      <div className="sky-line-bar" title="Drag to move this line" onPointerDown={onBarDown}>
        <Bird size={13} />
        <span className="sky-line-name">{p.title}</span>
        <span className={"fl-box-chip sky-line-chip tone-" + ss.tone} title={ss.hint}>
          {ss.label}
        </span>
        {layout.pinned && (
          <span className="sky-line-pin" title="Put here by hand — Tidy puts it back">
            📌
          </span>
        )}
        <span className="sky-line-menu" data-no-drag>
          <OverflowMenu label={"Actions for " + p.title} items={items} />
        </span>
      </div>
      {p.dropFlyway && <div className="sky-line-drop">Drop to fly {p.dropFlyway.name}</div>}
      {line.cancel.node}
      <FlywayCanvas diagram={line.diagram} label={"What " + p.title + " is doing and will do"} vertical={false} bird={birdPerch(line.diagram)} className="sky-canvas" {...line.canvas} />
      {line.editing && (
        <div className="sky-line-panels sky-own-scroll" data-no-drag>
          {line.palette}
          {line.inspector}
        </div>
      )}
      {layout.open && (
        <div className="sky-line-pane sky-own-scroll" data-no-drag style={{ width: layout.paneW || PANE_W, height: layout.paneH || PANE_H }}>
          <Pane inst={row} drag={NO_DRAG} dragging={false} />
        </div>
      )}
    </div>
  );
}

/** The ambient flock over the empty sky. */
export function SkyFlock(_props: { width: number; height: number; zoom: number; paused?: boolean; density?: number }): JSX.Element {
  return null as unknown as JSX.Element;
}

/** The folding strip of flyways at the sky's foot. */
export function FlywayShelf(_props: { plans: FlightPlan[]; onNew(): void; onFly(plan: FlightPlan, title?: string): void; onEdit(plan: FlightPlan): void }): JSX.Element {
  return null as unknown as JSX.Element;
}

/** A flyway's card on the sky (new, or `initial` to edit). */
export function FlywayCard(props: { x: number; y: number; plans: FlightPlan[]; initial?: Partial<FlightPlan>; onSaved(plan: FlightPlan): void; onCancel(): void }): JSX.Element {
  return (
    <div className="sky-flyway-card" style={{ transform: `translate(${props.x}px, ${props.y}px)` }} role="dialog" aria-label="Flyway">
      <p className="muted">{props.initial?.name ? "Editing " + props.initial.name : "A new flyway"} — the card arrives with the flyway shelf.</p>
      <button type="button" className="fl-btn" onClick={props.onCancel}>
        Close
      </button>
    </div>
  );
}
