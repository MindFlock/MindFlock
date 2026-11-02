/** Round 18: a chain tile's right-click menu (SkyChain `onMenu`) — the classic
 * canvas's items (lib/stopEditing `menuItemsFor`): Edit / Details, Duplicate,
 * Halt here, Remove, and on the stop the flight is at Halt now / Carry on,
 * Done, Skip, Retry. Order is the wires here, so no Earlier / Later. Drawn in
 * a portal on the page (the Sky's plane is zoomed, and a fixed box inside a
 * transformed one is not fixed), at the pointer. */
import { useState, type PointerEvent as ReactPointerEvent, type ReactNode } from "react";
import { createPortal } from "react-dom";
import type { DiagramNode } from "../../lib/flightDiagram";
import { loopable, type ChainGraph } from "../../lib/skyGraph";
import { menuItemsFor } from "../../lib/stopEditing";
import { controlFlight, stepDone, stepRetry, stepSkip } from "../../lib/flightsApi";
import { StopMenu, type SkyMenuItem } from "./StopMenu";

export interface ChainMenuHandlers {
  onEdit?(n: DiagramNode): void;
  onSelect?(n: DiagramNode): void;
  onRemove?(n: DiagramNode): void;
  onDuplicate?(n: DiagramNode): void;
  onToggleHalt?(n: DiagramNode): void;
  onOpen?(n: DiagramNode): void;
}

const HERE: ReadonlySet<string> = new Set(["current", "perched", "halted", "paused"]);

export function useChainMenu(h: ChainMenuHandlers, flightId: string): { onMenu(n: DiagramNode, at: { clientX: number; clientY: number }): void; menu: ReactNode } {
  const [open, setOpen] = useState<{ n: DiagramNode; x: number; y: number } | null>(null);
  const onMenu = (n: DiagramNode, at: { clientX: number; clientY: number }) => setOpen({ n, x: at.clientX, y: at.clientY });
  if (!open || typeof document === "undefined") return { onMenu, menu: null };
  const n = open.n;
  const live = !!flightId && (!!n.step || n.kind === "group");
  const items = menuItemsFor(n, {
    can: { edit: !!(h.onEdit || h.onSelect), duplicate: !!h.onDuplicate, remove: !!h.onRemove, open: !!h.onOpen, halt: !!h.onToggleHalt },
    live,
    here: live && HERE.has(n.state),
  }) as SkyMenuItem[];
  if (!items.length) return { onMenu, menu: null };
  const step = n.step?.id || "";
  const pick = (key: string) => {
    if (key === "edit") (h.onEdit || h.onSelect)?.(n);
    else if (key === "duplicate") h.onDuplicate?.(n);
    else if (key === "remove") h.onRemove?.(n);
    else if (key === "halt") h.onToggleHalt?.(n);
    else if (key === "done") void stepDone(flightId, step);
    else if (key === "skip") void stepSkip(flightId, step);
    else if (key === "retry") void stepRetry(flightId, step);
    else if (key === "pause" || key === "resume") void controlFlight(flightId, key);
    else if (key === "open" || key === "tackon") h.onOpen?.(n);
  };
  const menu = createPortal(
    <div className="sky-chain-menu" style={{ position: "fixed", left: 0, top: 0, width: 0, height: 0, zIndex: 80, ["--sky-z" as string]: 1 }}>
      <StopMenu
        x={open.x}
        y={open.y}
        label={"“" + n.title + "” — what you can do"}
        items={items}
        onPick={(key) => {
          pick(key);
          setOpen(null);
        }}
        onClose={() => setOpen(null)}
      />
    </div>,
    document.body
  );
  return { onMenu, menu };
}

/** Round 18: a Loop's pill dragged onto another stop of its chain re-aims it
 * (`onLoopTo`, the line's own handler in flight indexes): a stop at or before
 * where its wire starts that can repeat (`loopable`). The stop under the
 * pointer lights (`.sky-aim-over`); a press that never moves stays the pill's
 * click (its dialog); Esc cancels. */
export function pillAimer(graph: ChainGraph, onLoopTo?: (loop: DiagramNode, to: DiagramNode) => void) {
  return (via: DiagramNode, e: ReactPointerEvent<HTMLElement>) => {
    if (!onLoopTo || !via.editable || e.button !== 0) return;
    const wire = graph.wires.find((w) => w.via?.id === via.id);
    const end = graph.nodes.find((n) => n.id === wire?.from);
    if (!end || end.node.index === undefined) return;
    const sx = e.clientX;
    const sy = e.clientY;
    let moved = false;
    let lit: Element | null = null;
    const targetAt = (x: number, y: number) => {
      const el = document.elementFromPoint(x, y)?.closest("[data-node]");
      if (!el || el.closest("[data-chain]")?.getAttribute("data-chain") !== graph.key) return null;
      const n = graph.nodes.find((g) => g.id === el.getAttribute("data-node"));
      return n && loopable(n.node) && n.node.index! <= end.node.index! ? { el, n } : null;
    };
    const light = (el: Element | null) => {
      if (lit === el) return;
      lit?.classList.remove("sky-aim-over");
      el?.classList.add("sky-aim-over");
      lit = el;
    };
    const stop = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
      window.removeEventListener("pointercancel", stop);
      window.removeEventListener("keydown", key, true);
      light(null);
      if (moved) {
        const eat = (c: Event) => {
          c.stopPropagation();
          c.preventDefault();
        };
        window.addEventListener("click", eat, { capture: true, once: true });
        setTimeout(() => window.removeEventListener("click", eat, { capture: true }), 0);
      }
    };
    const move = (ev: PointerEvent) => {
      if (!moved && Math.hypot(ev.clientX - sx, ev.clientY - sy) < 4) return;
      moved = true;
      const t = targetAt(ev.clientX, ev.clientY);
      light(t ? (t.el.closest(".sky-node") ?? t.el) : null);
    };
    const up = (ev: PointerEvent) => {
      const t = moved ? targetAt(ev.clientX, ev.clientY) : null;
      stop();
      if (t) onLoopTo(via, t.n.node);
    };
    const key = (ev: KeyboardEvent) => {
      if (ev.key !== "Escape") return;
      ev.preventDefault();
      ev.stopPropagation();
      stop();
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
    window.addEventListener("pointercancel", stop);
    window.addEventListener("keydown", key, true);
  };
}
