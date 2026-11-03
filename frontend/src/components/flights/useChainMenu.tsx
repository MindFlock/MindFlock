/** Round 18: a chain tile's right-click menu (SkyChain `onMenu`) — the classic
 * canvas's items (lib/stopEditing `menuItemsFor`): Edit / Details, Duplicate,
 * Halt here, Remove, and on the stop the flight is at Halt now / Carry on,
 * Done, Skip, Retry. Order is the wires here, so no Earlier / Later. Drawn in
 * a portal on the page (the Sky's plane is zoomed, and a fixed box inside a
 * transformed one is not fixed), at the pointer. */
import { useEffect, useRef, useState, type PointerEvent as ReactPointerEvent, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { api } from "../../api/client";
import type { Instance } from "../../api/types";
import { loopChoices, type DiagramNode, type LoopChoice } from "../../lib/flightDiagram";
import { errMsg } from "../../lib/format";
import { pauseSession, resumeSession } from "../../lib/sessionActions";
import { toast } from "../../lib/toast";
import { windowName } from "../../lib/windowName";
import { loopable, type ChainGraph } from "../../lib/skyGraph";
import { menuItemsFor } from "../../lib/stopEditing";
import { controlFlight, joinParallel, leaveParallel, stepDone, stepRetry, stepSkip } from "../../lib/flightsApi";
import { isParallelSafe, type FlightStep, type StepKindDef } from "../../lib/flights";
import { StopMenu, type SkyMenuItem } from "./StopMenu";

export interface ChainMenuHandlers {
  onEdit?(n: DiagramNode): void;
  onSelect?(n: DiagramNode): void;
  onRemove?(n: DiagramNode): void;
  onDuplicate?(n: DiagramNode): void;
  onToggleHalt?(n: DiagramNode): void;
  onOpen?(n: DiagramNode): void;
  /** A Loop right after `from` back to `to` (a stop, or the session box:
   * back to the agent) — the line's own, the wire's twin. */
  onLoopFrom?(from: DiagramNode, to: DiagramNode): void;
  /** The "Loop back to…" choices for a stop (lib/flightDiagram
   * `loopChoices`) — a session's line lists its agent first. Without it the
   * menu lists the earlier stops from the flight's steps (`stepLoopChoices`). */
  loopChoicesOf?(n: DiagramNode): LoopChoice[];
}

/** A stop's "Loop back to…" choices from the flight's steps alone (a chain
 * whose line does not list them: a stops-only chain — no agent there): only
 * for a stop that can still change. */
export function stepLoopChoices(n: DiagramNode, steps: readonly FlightStep[]): LoopChoice[] {
  if (!n.editable) return [];
  const nodes: DiagramNode[] = steps.map((st, i) => ({
    id: "stop:" + st.id,
    kind: "stop",
    row: "main",
    title: st.title,
    sub: "",
    state: "pending",
    icon: st.kind,
    step: st,
    index: i,
    ...(st.parallel ? { parallel: { id: st.parallel.id, index: 0, size: 0 } } : {}),
  }));
  const me = n.step ? nodes.find((m) => m.step!.id === n.step!.id) : undefined;
  if (!me) return [];
  return loopChoices({ ...me, title: n.title }, nodes);
}

/** The menu's "Loop back to…" line (it opens the list of where to). */
export const LOOP_BACK_KEY = "loop-back";

/** Round 21 (docs/flights-loop-agent-sky.md): the menu with "Loop back to…"
 * put in above its danger line / notes, when there is somewhere to go. */
export function withLoopItem(items: readonly SkyMenuItem[], choices: readonly LoopChoice[]): SkyMenuItem[] {
  const out = [...items];
  if (!choices.length) return out;
  const agent = choices.some((c) => c.agent);
  const item: SkyMenuItem = {
    key: LOOP_BACK_KEY as SkyMenuItem["key"],
    label: "Loop back to…",
    title: agent ? "Repeat: after this stop, run the agent again — or go back to an earlier stop" : "Repeat: after this stop, go back to an earlier stop",
  };
  const cut = out.findIndex((i) => i.danger || i.note);
  out.splice(cut < 0 ? out.length : cut, 0, item);
  return out;
}

/** The "Loop back to…" submenu: a label line, one item per choice (the
 * agent first), and ← Back. */
export function loopMenuItems(choices: readonly LoopChoice[]): SkyMenuItem[] {
  return [
    { key: "loop-note" as SkyMenuItem["key"], label: "Loop back to…", title: "Where the route goes back to after this stop", note: true },
    ...choices.map((c) => ({ key: c.key as SkyMenuItem["key"], label: c.label, title: c.title })),
    { key: "loop-cancel" as SkyMenuItem["key"], label: "← Back", title: "Back to the stop's menu" },
  ];
}

const HERE: ReadonlySet<string> = new Set(["current", "perched", "halted", "paused"]);

export function useChainMenu(
  h: ChainMenuHandlers,
  flightId: string,
  /** Round 20: the flight's stops and the kinds, for "Run in parallel with
   * the next stop" / "Run on its own". */
  ctx?: { steps?: readonly FlightStep[] | null; kinds?: readonly StepKindDef[] | null }
): { onMenu(n: DiagramNode, at: { clientX: number; clientY: number }): void; menu: ReactNode } {
  const [open, setOpen] = useState<{ n: DiagramNode; x: number; y: number; sub?: "loop" } | null>(null);
  const onMenu = (n: DiagramNode, at: { clientX: number; clientY: number }) => setOpen({ n, x: at.clientX, y: at.clientY });
  if (!open || typeof document === "undefined") return { onMenu, menu: null };
  const n = open.n;
  const live = !!flightId && (!!n.step || n.kind === "group");
  const items = menuItemsFor(n, {
    can: { edit: !!(h.onEdit || h.onSelect), duplicate: !!h.onDuplicate, remove: !!h.onRemove, open: !!h.onOpen, halt: !!h.onToggleHalt },
    live,
    here: live && HERE.has(n.state),
  }) as SkyMenuItem[];
  // Round 20: side by side with the next stop, or out of its group again —
  // only for a stop still ahead whose kind may run in parallel.
  const st = n.step;
  const steps = ctx?.steps || [];
  const at = st ? steps.findIndex((x) => x.id === st.id) : -1;
  const next = at >= 0 ? steps.slice(at + 1).find((x) => !st?.parallel || x.parallel?.id !== st.parallel.id) : undefined;
  const par: SkyMenuItem[] = [];
  if (st && n.editable && flightId && isParallelSafe(st.kind, ctx?.kinds)) {
    if (st.parallel) par.push({ key: "solo" as SkyMenuItem["key"], label: "Run on its own", title: "Take it out of its parallel group — it runs after the group" });
    else if (next && isParallelSafe(next.kind, ctx?.kinds))
      par.push({ key: "parallel" as SkyMenuItem["key"], label: "Run in parallel with the next stop", title: "“" + st.title + "” and “" + next.title + "” run side by side" });
  }
  if (par.length) {
    const cut = items.findIndex((i) => i.danger || i.note);
    items.splice(cut < 0 ? items.length : cut, 0, ...par);
  }
  // Round 21: "Loop back to…" — the agent and every earlier stop, the
  // same Loop a wire dropped there draws.
  const choices = h.onLoopFrom && (n.kind === "stop" || n.kind === "group") ? (h.loopChoicesOf ? h.loopChoicesOf(n) : stepLoopChoices(n, steps)) : [];
  const shown = open.sub === "loop" ? loopMenuItems(choices) : withLoopItem(items, choices);
  if (!shown.length) return { onMenu, menu: null };
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
    else if (key === "solo") void leaveParallel(flightId, step);
    else if (key === "parallel" && next) void joinParallel(flightId, step, next.id);
    else {
      const c = choices.find((x) => x.key === key);
      if (c) h.onLoopFrom?.(c.from, c.to);
    }
  };
  const menu = createPortal(
    <div className="sky-chain-menu" style={{ position: "fixed", left: 0, top: 0, width: 0, height: 0, zIndex: 80, ["--sky-z" as string]: 1 }}>
      <StopMenu
        x={open.x}
        y={open.y}
        key={open.sub || "menu"}
        label={open.sub === "loop" ? "Loop “" + n.title + "” back to…" : "“" + n.title + "” — what you can do"}
        items={shown}
        onPick={(key) => {
          // The submenu opens in the menu's place (and ← Back leaves it).
          const k: string = key;
          if (k === LOOP_BACK_KEY || k === "loop-cancel") return setOpen({ ...open, sub: k === LOOP_BACK_KEY ? "loop" : undefined });
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
 * where its wire starts that can repeat (`loopable`), or (round 21) the
 * chain's agent tile — `onLoopTo(via, <the session node>)`. The stop under the
 * pointer lights (`.sky-aim-over`); a press that never moves stays the pill's
 * click (its dialog); Esc cancels. */
export function pillAimer(graph: ChainGraph, onLoopTo?: (loop: DiagramNode, to: DiagramNode) => void) {
  return (via: DiagramNode, e: ReactPointerEvent<HTMLElement>) => {
    if (!onLoopTo || !via.editable || e.button !== 0) return;
    const wire = graph.wires.find((w) => w.via?.id === via.id);
    const end = graph.nodes.find((n) => n.id === wire?.from);
    if (!end || end.node.index === undefined) return;
    // Round 21: the AGENT tile is a target too (a session's chain): the
    // Loop goes back to the agent (`back_to: "agent"`).
    const agent = graph.head.kind === "session" ? graph.headNode : null;
    const sx = e.clientX;
    const sy = e.clientY;
    let moved = false;
    let lit: Element | null = null;
    const targetAt = (x: number, y: number) => {
      const el = document.elementFromPoint(x, y)?.closest("[data-node]");
      if (!el || el.closest("[data-chain]")?.getAttribute("data-chain") !== graph.key) return null;
      if (agent && el.getAttribute("data-node") === agent.id) return wire?.to === agent.id ? null : { el, n: agent };
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

// --- Round 20: the agent tile's menu ----------------------------------------------

/** One line of an agent tile's menu: what it says and what it does. A
 * `note` is a label line (the session's part of the menu starts with one). */
export interface AgentMenuItem {
  key: string;
  label: string;
  title: string;
  danger?: boolean;
  disabled?: boolean;
  note?: boolean;
  onSelect?(): void;
}

/** The agent's own items (docs/flights.md "Round 20" A): Open terminal, Send a
 * prompt…, and Stop (Pause: the agent stops, the session stays — Resume
 * while it is paused). `openTerminal` is the host's (the card in the tile's
 * place, or the pane's Agent tab); `compose` opens the composer. */
export function agentMenuItems(row: Pick<Instance, "title" | "status"> | null | undefined, opts: { openTerminal(): void; compose(): void; opened?: boolean }): AgentMenuItem[] {
  if (!row) return [];
  const paused = row.status === "paused";
  return [
    { key: "agent-open", label: opts.opened ? "Fold the terminal" : "Open terminal", title: opts.opened ? "Back to the agent's stop" : "Its real terminal, here in the stop's place", onSelect: opts.openTerminal },
    { key: "agent-prompt", label: "Send a prompt…", title: "Type a prompt for this agent and send it", disabled: paused, onSelect: opts.compose },
    paused
      ? { key: "agent-resume", label: "Resume", title: "Start the agent again in the same worktree", onSelect: () => void resumeSession(row.title) }
      : { key: "agent-stop", label: "Stop", title: "Stop the agent — the session stays (Resume starts it again)", danger: true, onSelect: () => void pauseSession(row.title) },
  ];
}

/** Send `text` to a session's agent (POST /api/instances/{title}/send — typed
 * and submitted as if at its terminal). True when it went. */
export async function sendPrompt(title: string, text: string): Promise<boolean> {
  const t = text.trim();
  if (!t) return false;
  try {
    await api("/api/instances/" + encodeURIComponent(title) + "/send", { method: "POST", json: { text: t, dialog_safe: true } });
    toast("Sent to " + windowName(title));
    return true;
  } catch (err) {
    toast("Couldn't send: " + errMsg(err));
    return false;
  }
}

/** A small composer at the pointer: a prompt for one agent (Enter sends,
 * Shift+Enter is a new line, Esc or a press outside closes it). */
function PromptComposer({ title, x, y, onClose }: { title: string; x: number; y: number; onClose(): void }) {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const ref = useRef<HTMLFormElement>(null);
  useEffect(() => {
    const onDown = (e: Event) => {
      if (ref.current && !ref.current.contains(e.target as Node)) onClose();
    };
    document.addEventListener("pointerdown", onDown, true);
    return () => document.removeEventListener("pointerdown", onDown, true);
  }, [onClose]);
  const send = async () => {
    if (busy || !text.trim()) return;
    setBusy(true);
    const ok = await sendPrompt(title, text);
    setBusy(false);
    if (ok) onClose();
  };
  return (
    <form
      ref={ref}
      className="sky-agent-prompt"
      style={{ left: x, top: y }}
      role="dialog"
      aria-label={"Send a prompt to " + windowName(title)}
      data-no-drag
      onPointerDown={(e) => e.stopPropagation()}
      onSubmit={(e) => {
        e.preventDefault();
        void send();
      }}
    >
      <label className="sky-agent-prompt-head" htmlFor="sky-agent-prompt-text">
        Prompt for {windowName(title)}
      </label>
      <textarea
        id="sky-agent-prompt-text"
        className="fl-input"
        rows={3}
        autoFocus
        value={text}
        placeholder="What should the agent do?"
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => {
          e.stopPropagation();
          if (e.key === "Escape") {
            e.preventDefault();
            onClose();
          } else if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            void send();
          }
        }}
      />
      <div className="sky-agent-prompt-row">
        <span className="muted">Enter sends · Shift+Enter new line</span>
        <button type="button" className="linklike" onClick={onClose}>
          Cancel
        </button>
        <button type="submit" className="fl-btn btn-primary" disabled={busy || !text.trim()}>
          Send
        </button>
      </div>
    </form>
  );
}

/** The agent tile's right-click menu (and its Send a prompt… composer), in a
 * portal on the page at the pointer, as the stops' menu is. */
export function useAgentMenu(): {
  open(title: string, at: { clientX: number; clientY: number }, items: readonly AgentMenuItem[]): void;
  compose(title: string, at: { clientX: number; clientY: number }): void;
  menu: ReactNode;
} {
  const [state, setState] = useState<{ title: string; x: number; y: number; items: readonly AgentMenuItem[] } | null>(null);
  const [composer, setComposer] = useState<{ title: string; x: number; y: number } | null>(null);
  const open = (title: string, at: { clientX: number; clientY: number }, items: readonly AgentMenuItem[]) => setState({ title, x: at.clientX, y: at.clientY, items });
  // The composer stays on screen (it is ~330 × 170 px).
  const compose = (title: string, at: { clientX: number; clientY: number }) =>
    setComposer({
      title,
      x: Math.max(8, Math.min(at.clientX, (typeof window === "undefined" ? 1e4 : window.innerWidth) - 340)),
      y: Math.max(8, Math.min(at.clientY, (typeof window === "undefined" ? 1e4 : window.innerHeight) - 180)),
    });
  if (typeof document === "undefined" || (!state && !composer)) return { open, compose, menu: null };
  const menu = createPortal(
    <div className="sky-chain-menu" style={{ position: "fixed", left: 0, top: 0, width: 0, height: 0, zIndex: 80, ["--sky-z" as string]: 1 }}>
      {state && (
        <StopMenu
          x={state.x}
          y={state.y}
          label={"The agent of “" + windowName(state.title) + "” — what you can do"}
          items={state.items.map((it) => ({ key: it.key as SkyMenuItem["key"], label: it.label, title: it.title, danger: it.danger, disabled: it.disabled, note: it.note }))}
          onPick={(key) => {
            const it = state.items.find((i) => i.key === key);
            setState(null);
            it?.onSelect?.();
          }}
          onClose={() => setState(null)}
        />
      )}
      {composer && <PromptComposer title={composer.title} x={composer.x} y={composer.y} onClose={() => setComposer(null)} />}
    </div>,
    document.body
  );
  return { open, compose, menu };
}
