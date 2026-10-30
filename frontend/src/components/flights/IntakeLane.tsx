/** The Sky's intake lane: the ticket ingestion pipeline drawn as blocks,
 * pinned to the sky's left edge (docs/flights-intake-lane.md).
 *
 * One SOURCE BLOCK per thing the pipeline polls — each ticket source, each PR
 * repo monitor, each issue repo monitor — saying what it is (the kind badge
 * Waiting to fly uses), whether it starts work by itself and how often it
 * looks (the auto-start chip), the saved stop its items fly, the items that are
 * eligible and have no session yet (each with Fly), and a thin stub per
 * session it already started — `data-lane-target="<title>"` is where the
 * Sky's arrow to that session's line begins. ⋯ opens the source in Intake.
 * The lane folds to a rail of one icon per source with its count.
 *
 * On the Sky the lane is a sky object like a line (docs/flights.md "As built:
 * round 12 sky"): the host places it (layout key "intake"), drags it by its
 * head (`onHeadDown`, unless `draggable` is false — click mode), and gives it
 * room from `onMeasure` (a ResizeObserver, as SkyLine's). EXPANDED (`open`,
 * ▸ / ▾ on its head or a double-click there) every source lists all of its
 * waiting items with their headlines in full, at the width the host gives.
 *
 * The grouping, counts, default saved stop and started-session matching are
 * lib/skyIntake.ts `sourceBlocks`; the Fly request is the Board's own
 * (`flyRequest`, with the source's saved stop). */

import { useEffect, useRef, useState, type PointerEvent as ReactPointerEvent } from "react";
import { api } from "../../api/client";
import type { Instance } from "../../api/types";
import { refreshInstances, useMindflockStatus, usePanelQuery, useSettingsDoc, useTicketingCatalog } from "../../state/queries";
import { useUi } from "../../state/store";
import { errMsg } from "../../lib/format";
import { windowName } from "../../lib/windowName";
import { WAITING_KIND_BADGE, WAITING_KIND_HEAD, type FlightPlan } from "../../lib/flights";
import { flyRequest, refreshFlights, type WaitingTarget } from "../../lib/flightsApi";
import { laneCount, shownWaiting, sourceBlocks, type LanePanels, type LaneWaiting, type SourceBlock } from "../../lib/skyIntake";
import { useWaiting } from "./useWaiting";
import "./Flights.css";
import "./IntakeLane.css";

export interface IntakeLaneProps {
  rows: Instance[];
  plans: FlightPlan[];
  onFly(title: string): void;
  width?: number;
  /** Expanded: every waiting item of every source, headlines in full. */
  open?: boolean;
  /** ▸ / ▾ on the head, or a double-click on it: expand or fold. */
  onToggleOpen?(): void;
  /** A press on the head (or the rail): the host drags the lane by it. */
  onHeadDown?(e: ReactPointerEvent): void;
  /** False in the sky's click mode: the head never starts a move. */
  draggable?: boolean;
  /** The lane's drawn size (plane px), whenever it changes. */
  onMeasure?(w: number, h: number): void;
}

/** Per browser: whether the lane is folded to its rail. */
const COLLAPSED_LS_KEY = "mf_sky_lane_collapsed";

function loadCollapsed(): boolean {
  try {
    return localStorage.getItem(COLLAPSED_LS_KEY) === "1";
  } catch {
    return false;
  }
}

function saveCollapsed(v: boolean) {
  try {
    if (v) localStorage.setItem(COLLAPSED_LS_KEY, "1");
    else localStorage.removeItem(COLLAPSED_LS_KEY);
  } catch {
    /* storage unavailable */
  }
}

/** The rail's one-glyph name for each kind. */
const RAIL_GLYPH: Record<SourceBlock["kind"], string> = { tickets: "T", prs: "PR", issues: "I" };

/** Fly one waiting item the way the Board's row does: the Intake start route
 * with the source's saved stop. Resolves to the session title the start returned
 * (the ticket's reference when an older server answers without one). */
export async function flyLaneItem(w: LaneWaiting, block: Pick<SourceBlock, "flyway">): Promise<string> {
  const t = w.item.target as WaitingTarget;
  const req = flyRequest(t, block.flyway.own ? block.flyway.id : "");
  try {
    const r = await api<{ title?: string }>(req.path, { json: req.body });
    void refreshFlights();
    return r?.title || (t.kind === "tickets" ? w.item.reference : "");
  } finally {
    void refreshInstances();
  }
}

function WaitingChip({ w, block, onFly }: { w: LaneWaiting; block: SourceBlock; onFly(title: string): void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const fly = async () => {
    setBusy(true);
    setError("");
    try {
      const title = await flyLaneItem(w, block);
      if (title) onFly(title);
    } catch (err) {
      setError(errMsg(err));
    } finally {
      setBusy(false);
    }
  };
  return (
    <li className="sky-lane-item" data-waiting={w.item.key}>
      <div className="sky-lane-item-row">
        <span className="sky-lane-item-words" title={w.item.reference + " — " + w.item.title}>
          <span className="sky-lane-item-ref">{w.item.reference}</span>
          <span className="sky-lane-item-headline">{w.item.title}</span>
        </span>
        <button
          type="button"
          className="btn-primary fl-btn sky-lane-fly"
          disabled={busy}
          title={"Take off on " + w.item.reference + " · " + block.flyway.name}
          onClick={() => void fly()}
        >
          {busy ? "…" : "Fly"}
        </button>
      </div>
      {error && (
        <p className="error fl-error sky-lane-error" role="alert">
          {error}
        </p>
      )}
    </li>
  );
}

function Block({ block, onFly, open }: { block: SourceBlock; onFly(title: string): void; open?: boolean }) {
  const { shown, more } = shownWaiting(block, open ? Infinity : undefined);
  return (
    <section className={"sky-lane-block sky-lane-block-" + block.kind} data-lane-source={block.key} aria-label={block.label}>
      <header className="sky-lane-block-head">
        <span className={"fl-kind-badge fl-kind-badge-" + block.kind}>{WAITING_KIND_BADGE[block.kind]}</span>
        <span className="sky-lane-block-name" title={block.label}>
          {block.label}
        </span>
        <button
          type="button"
          className="sky-lane-more"
          aria-label={"Open " + block.label + " in Intake"}
          title="Open this source in Intake"
          onClick={() => useUi.getState().openDialogFor("intake", block.target)}
        >
          ⋯
        </button>
      </header>
      <div className="sky-lane-chips">
        <span className={"sky-lane-auto" + (block.auto ? " on" : "")}>{block.autoText}</span>
        <span className={"sky-lane-flyway" + (block.flyway.own ? " own" : "")} title={block.flyway.own ? "This source's saved stop" : "The built-in stops for " + WAITING_KIND_HEAD[block.kind].toLowerCase()}>
          {block.flyway.name}
        </span>
      </div>
      {block.waiting.length === 0 ? (
        <p className="sky-lane-empty muted">Nothing waiting</p>
      ) : (
        <ul className="sky-lane-items">
          {shown.map((w) => (
            <WaitingChip key={w.item.key} w={w} block={block} onFly={onFly} />
          ))}
          {more > 0 && <li className="sky-lane-n-more muted">{more} more</li>}
        </ul>
      )}
      {block.started.length > 0 && (
        <ul className="sky-lane-stubs" aria-label="Started sessions">
          {block.started.map((s) => (
            <li key={s.title} className="sky-lane-stub" data-lane-target={s.title} title={s.reference ? s.reference + " → " + windowName(s.title) : windowName(s.title)}>
              <span className="sky-lane-stub-line" aria-hidden="true" />
              <span className="sky-lane-stub-name">{windowName(s.title)}</span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

/** A press on the head that is the head's own (not one of its buttons). */
const headPress = (e: ReactPointerEvent) => e.button === 0 && !(e.target as Element).closest("button, a, input, summary, [data-no-drag]");

export function IntakeLane({ rows, plans, onFly, width = 260, open = false, onToggleOpen, onHeadDown, draggable = true, onMeasure }: IntakeLaneProps) {
  const settings = useSettingsDoc(true).data as Record<string, unknown> | undefined;
  const status = useMindflockStatus().data;
  const providers = useTicketingCatalog(true).data;
  const { waiting } = useWaiting(true);
  // The same cached panels useWaiting reads: their rows name the session each
  // item started as, which is how a started session finds its block.
  const tickets = usePanelQuery<NonNullable<LanePanels["tickets"]> & { stale?: boolean }>("tickets").data;
  const prs = usePanelQuery<NonNullable<LanePanels["prs"]> & { stale?: boolean }>("github-prs").data;
  const issues = usePanelQuery<NonNullable<LanePanels["issues"]> & { stale?: boolean }>("github-issues").data;
  const [collapsed, setCollapsed] = useState(loadCollapsed);
  // The host lays the lane out from what it measures (plane px), as a line's.
  const rootRef = useRef<HTMLElement>(null);
  const measureRef = useRef(onMeasure);
  measureRef.current = onMeasure;
  const shown = !!settings;
  useEffect(() => {
    const el = rootRef.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    let last = "";
    const read = () => {
      const w = el.offsetWidth;
      const h = el.offsetHeight;
      const k = w + "x" + h;
      if (!w || !h || k === last) return;
      last = k;
      measureRef.current?.(w, h);
    };
    const ro = new ResizeObserver(read);
    ro.observe(el);
    read();
    return () => ro.disconnect();
  }, [shown, collapsed]);

  if (!settings) return null;
  const blocks = sourceBlocks(settings, waiting, rows, {
    plans,
    status,
    providers,
    panels: { tickets: tickets ?? null, prs: prs ?? null, issues: issues ?? null },
  });
  const fold = (v: boolean) => {
    setCollapsed(v);
    saveCollapsed(v);
  };
  const total = blocks.reduce((n, b) => n + laneCount(b), 0);
  const headDown = (e: ReactPointerEvent) => {
    if (draggable && onHeadDown && headPress(e)) onHeadDown(e);
  };
  const moveHint = draggable && onHeadDown ? "Drag to move the intake lane" : "";

  if (collapsed) {
    return (
      <nav ref={rootRef} className="sky-lane sky-lane-rail" aria-label="Intake" data-collapsed="true" title={moveHint || undefined} onPointerDown={headDown}>
        <button type="button" className="sky-lane-toggle" aria-expanded={false} title="Open the intake lane" onClick={() => fold(false)}>
          ›
        </button>
        {blocks.map((b) => (
          <button
            type="button"
            key={b.key}
            className={"sky-lane-rail-icon fl-kind-badge fl-kind-badge-" + b.kind}
            data-lane-source={b.key}
            title={b.label + " — " + (b.waiting.length ? b.waiting.length + " waiting" : "nothing waiting")}
            onClick={() => fold(false)}
          >
            {RAIL_GLYPH[b.kind]}
            {laneCount(b) > 0 && <span className="sky-lane-rail-count">{laneCount(b)}</span>}
          </button>
        ))}
      </nav>
    );
  }

  return (
    <nav ref={rootRef} className="sky-lane" aria-label="Intake" data-open={open ? "" : undefined} style={{ width }}>
      <div
        className="sky-lane-head"
        data-sky-head
        title={[moveHint, onToggleOpen ? "double-click to " + (open ? "fold" : "expand") : ""].filter(Boolean).join(" · ") || undefined}
        onPointerDown={headDown}
        onDoubleClick={(e) => {
          if (!onToggleOpen || (e.target as Element).closest("button")) return;
          e.stopPropagation();
          onToggleOpen();
        }}
      >
        <span className="sky-lane-title">Intake</span>
        {total > 0 && <span className="fl-count">{total}</span>}
        {onToggleOpen && (
          <button
            type="button"
            className="sky-lane-toggle sky-lane-expand"
            aria-expanded={open}
            aria-label={open ? "Fold the intake lane's items" : "Expand the intake lane: every item, in full"}
            title={open ? "Fold: the first items of each source" : "Expand: every waiting item, in full"}
            onClick={onToggleOpen}
          >
            {open ? "▾" : "▸"}
          </button>
        )}
        <button type="button" className="sky-lane-toggle" aria-expanded={true} title="Fold the intake lane" onClick={() => fold(true)}>
          ‹
        </button>
      </div>
      {blocks.length === 0 ? (
        <p className="sky-lane-empty muted">
          No sources yet.{" "}
          <button type="button" className="linklike" onClick={() => useUi.getState().openDialogFor("intake")}>
            Intake ›
          </button>
        </p>
      ) : (
        blocks.map((b) => <Block key={b.key} block={b} onFly={onFly} open={open} />)
      )}
    </nav>
  );
}
