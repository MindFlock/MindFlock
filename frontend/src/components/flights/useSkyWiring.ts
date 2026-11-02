/** Round 18 — the Sky's wiring (docs/flights.md "Round 18" → "Wiring"): the
 * HOST (SkyView) runs every wire drag, so a wire pulled out of one chain's
 * port can land on another chain. Two halves:
 *
 * - Chain side (SkyLine, GroundedLine): `useChainEntry` hands the host the
 *   chain's graph, where the chain sits inside its slot (plane px) and its
 *   palette; `useNodeMoves` keeps a dragged tile where it was dropped until
 *   the flights query has its saved `pos` (`queueStopPos`, one `…/layout`
 *   call per flight); `useTourHooks` puts the coach tour's `data-tour`
 *   hooks on tiles SkyChain draws.
 * - Host side: `useChainRegistry` keeps every chain's entry by its key (a
 *   session title or `"flight:<id>"`); `useSkyWiring` runs the drag — window
 *   pointer listeners, the rubber band in plane px, the nodes a drop means
 *   something on (`targets`, lit on each chain) and the one under the
 *   pointer (`over`) — and on release asks `wireIntent` (lib/skyGraph) what
 *   the drop does and hands that to the host. Esc cancels. */

import { loadNodePos, saveNodePos } from "../../lib/skyNodePos";
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type PointerEvent as ReactPointerEvent, type RefObject } from "react";
import { firstStepOf, loopSlotAfter, loopStepTo, type Diagram, type DiagramNode } from "../../lib/flightDiagram";
import { joinParallel, moveStop, queueStopPos, saveStopPositions, splitOffChain, tackOn, tackOnNew } from "../../lib/flightsApi";
import type { Flight, StepKindDef } from "../../lib/flights";
import { HANDOFF_KIND, groundedKey, handoffStepFor, sessionWord } from "../../lib/groundedLines";
import { GRID_SNAP, NODE_W, TILE, inPortOf, outPortOf, wireIntent, type ChainGraph, type GraphNode, type WireEnd, type WireIntent } from "../../lib/skyGraph";
import type { Pos } from "../../lib/skyLayout";
import { toast } from "../../lib/toast";

/** The graph's head id (lib/skyGraph `HEAD_ID`). */
export const HEAD = "head";

/** What a chain tells the host. */
export interface ChainEntry {
  graph: ChainGraph;
  /** Where the chain's origin sits inside its slot (plane px). */
  dx: number;
  dy: number;
  /** Open the chain's palette for a stop right after `after` (null: the
   * first slot), the new stop drawn at `pos` (chain-local). */
  paletteAfter(after: DiagramNode | null, pos: Pos | null): void;
  /** Round 20 (optional — a chain that has it can take parallel stops): open
   * the chain's palette for a stop that flies side by side with `withNode`
   * — the picked stop is tacked on with `tackOnParallel(flight, step,
   * firstStepOf(withNode).id)` — the new tile drawn at `pos` (chain-local).
   * Without it a wire to empty sky past a stop that has a next opens the
   * plain palette after that stop (`paletteAfter`), as before. */
  paletteParallel?(withNode: DiagramNode, pos: Pos | null): void;
}

/** A chain on the plane: its entry and its slot's spot. */
export interface PlacedChain extends ChainEntry {
  key: string;
  origin: Pos;
}

/** Every node of a chain, the head first. */
export const chainNodes = (g: Pick<ChainGraph, "headNode" | "nodes">): GraphNode[] => (g.headNode ? [g.headNode, ...g.nodes] : g.nodes);

/** A node of a chain by its graph id. */
export const chainNode = (g: Pick<ChainGraph, "headNode" | "nodes">, id: string): GraphNode | null => (id === HEAD ? g.headNode : g.nodes.find((n) => n.id === id) || null);

/** A chain-local spot on the plane. */
export const onPlane = (c: Pick<PlacedChain, "origin" | "dx" | "dy">, p: Pos): Pos => ({ x: c.origin.x + c.dx + p.x, y: c.origin.y + c.dy + p.y });

/** A plane spot in a chain's own px. */
export const inChain = (c: Pick<PlacedChain, "origin" | "dx" | "dy">, p: Pos): Pos => ({ x: p.x - c.origin.x - c.dx, y: p.y - c.origin.y - c.dy });

/** A port's spot on the plane. */
export const portOnPlane = (c: Pick<PlacedChain, "origin" | "dx" | "dy">, n: GraphNode, side: "in" | "out"): Pos => onPlane(c, side === "in" ? inPortOf(n) : outPortOf(n));

/** Every node a wire from `src` could land on, by chain key (the contract's
 * Wiring table, asked of `wireIntent` node by node). */
export function wireTargetsOf(src: WireEnd, chains: Iterable<Pick<PlacedChain, "graph">>, shift = false, kinds?: readonly StepKindDef[] | null): Map<string, Set<string>> {
  const out = new Map<string, Set<string>>();
  for (const c of chains)
    for (const n of chainNodes(c.graph)) {
      if (wireIntent(src, { chain: c.graph, node: n, side: "in" }, { shift, kinds }).op === "none") continue;
      let s = out.get(c.graph.key);
      if (!s) out.set(c.graph.key, (s = new Set()));
      s.add(n.id);
    }
  return out;
}

/** The chain node under an element (a tile, a port, the socket, the +), or
 * null — never one inside an open card's pane. */
export function nodeUnder(el: Element | null, chains: ReadonlyMap<string, Pick<PlacedChain, "graph">>): { key: string; node: GraphNode } | null {
  const nodeEl = el?.closest("[data-node]");
  const chainEl = nodeEl?.closest(".sky-chain[data-chain]");
  if (!nodeEl || !chainEl || nodeEl.closest(".skl-card")) return null;
  const key = chainEl.getAttribute("data-chain") || "";
  const c = chains.get(key);
  const node = c ? chainNode(c.graph, nodeEl.getAttribute("data-node") || "") : null;
  return c && node ? { key, node } : null;
}

// --- Host side ---------------------------------------------------------------------

/** What the host draws of a chain: where it sits and its tiles' ids and spots. */
export function entrySig(e: Pick<ChainEntry, "graph" | "dx" | "dy">): string {
  const g = e.graph;
  return [g.key, g.flightId, g.head.kind, e.dx, e.dy, ...chainNodes(g).map((n) => n.id + "@" + n.x + "," + n.y)].join("|");
}

/** The host's registry of chains: one stable `register` per key (a line's
 * prop), the entries in a ref, and a counter that moves when one changes
 * (what the cross-chain wires redraw on). */
export function useChainRegistry() {
  const entries = useRef(new Map<string, ChainEntry>());
  const sigs = useRef(new Map<string, string>());
  const registers = useRef(new Map<string, (e: ChainEntry | null) => void>());
  const [version, setVersion] = useState(0);
  const registerFor = useCallback((key: string) => {
    let r = registers.current.get(key);
    if (!r) {
      r = (e) => {
        // The latest entry always (what a drop reads); a redraw only when
        // what the cross-chain wires draw moved — never for a graph that is
        // merely a new object (that would be a render loop).
        if (e) entries.current.set(key, e);
        else entries.current.delete(key);
        const sig = e ? entrySig(e) : "";
        if (sig === (sigs.current.get(key) || "")) return;
        if (sig) sigs.current.set(key, sig);
        else sigs.current.delete(key);
        setVersion((v) => v + 1);
      };
      registers.current.set(key, r);
    }
    return r;
  }, []);
  return { entries, registerFor, version };
}

/** The rubber band of a wire being drawn (plane px). */
export interface WireBand {
  from: Pos;
  to: Pos;
  side: "in" | "out";
  /** The drop under the pointer means something. */
  over: boolean;
}

export interface SkyWiringOpts {
  /** Every chain on the plane, now. */
  chains(): ReadonlyMap<string, PlacedChain>;
  planeAt(clientX: number, clientY: number): Pos;
  /** An element is empty sky. */
  ground(el: Element | null): boolean;
  /** The drop's intent, and where it was let go (plane px). */
  onIntent(intent: WireIntent, ctx: { src: WireEnd; at: Pos }): void;
  /** Round 20 (optional): the kinds list, so a drop knows which stops may
   * run in parallel (`parallel_safe`); without it lib/flights
   * `PARALLEL_UNSAFE` decides. */
  kinds?(): readonly StepKindDef[] | null | undefined;
}

/** The host's wire drag. `startWire` from a chain's `onPortDown`. */
export function useSkyWiring(opts: SkyWiringOpts) {
  const optsRef = useRef(opts);
  optsRef.current = opts;
  const [band, setBand] = useState<WireBand | null>(null);
  const [targets, setTargets] = useState<ReadonlyMap<string, ReadonlySet<string>>>(() => new Map());
  const [over, setOver] = useState<{ key: string; node: string } | null>(null);
  const stop = useRef<(() => void) | null>(null);
  useEffect(() => () => stop.current?.(), []);

  const startWire = useCallback((key: string, node: GraphNode, side: "in" | "out", e: ReactPointerEvent<HTMLElement> | PointerEvent) => {
    const o = optsRef.current;
    const chains = o.chains();
    const c = chains.get(key);
    if (!c) return;
    stop.current?.();
    const src: WireEnd = { chain: c.graph, node, side };
    const el = (e as { currentTarget?: EventTarget | null }).currentTarget;
    const r = el instanceof Element ? el.getBoundingClientRect() : null;
    const from = r && (r.width || r.height) ? o.planeAt(r.left + r.width / 2, r.top + r.height / 2) : portOnPlane(c, node, side);
    let shift = e.shiftKey;
    const kindsNow = () => optsRef.current.kinds?.() || null;
    setTargets(wireTargetsOf(src, chains.values(), shift, kindsNow()));
    setBand({ from, to: from, side, over: false });
    setOver(null);
    const pid = e.pointerId;
    const sx = e.clientX;
    const sy = e.clientY;
    let moved = false;
    const hit = (ev: { clientX: number; clientY: number }) => {
      const at = typeof document !== "undefined" && document.elementFromPoint ? document.elementFromPoint(ev.clientX, ev.clientY) : null;
      return { el: at, node: nodeUnder(at, optsRef.current.chains()) };
    };
    const move = (ev: PointerEvent) => {
      if (ev.pointerId !== pid) return;
      if (!moved && Math.hypot(ev.clientX - sx, ev.clientY - sy) >= 3) moved = true;
      const now = optsRef.current;
      const to = now.planeAt(ev.clientX, ev.clientY);
      if (ev.shiftKey !== shift) {
        shift = ev.shiftKey;
        setTargets(wireTargetsOf(src, now.chains().values(), shift, kindsNow()));
      }
      const h = hit(ev).node;
      const hc = h ? now.chains().get(h.key) : null;
      const ok = !!h && !!hc && wireIntent(src, { chain: hc.graph, node: h.node, side: "in" }, { shift, kinds: kindsNow() }).op !== "none";
      setOver((cur) => (ok ? (cur?.key === h!.key && cur.node === h!.node.id ? cur : { key: h!.key, node: h!.node.id }) : null));
      setBand({ from, to, side, over: ok });
    };
    const end = (commit: boolean, ev?: PointerEvent) => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
      window.removeEventListener("pointercancel", cancel);
      window.removeEventListener("keydown", esc, true);
      stop.current = null;
      setBand(null);
      setTargets(new Map());
      setOver(null);
      if (moved && typeof window !== "undefined") {
        // The click that follows the release is the drag's, not a tile's.
        const eat = (c: Event) => {
          c.stopPropagation();
          c.preventDefault();
        };
        window.addEventListener("click", eat, { capture: true, once: true });
        setTimeout(() => window.removeEventListener("click", eat, { capture: true }), 0);
      }
      if (!commit || !ev || !moved) return;
      const now = optsRef.current;
      const at = now.planeAt(ev.clientX, ev.clientY);
      const h = hit(ev);
      const placed = h.node ? now.chains().get(h.node.key) : null;
      const dst: WireEnd | null = h.node && placed ? { chain: placed.graph, node: h.node.node, side: "in" } : now.ground(h.el) ? { sky: at } : null;
      const intent: WireIntent = dst ? wireIntent(src, dst, { shift: ev.shiftKey, kinds: kindsNow() }) : { op: "none" };
      now.onIntent(intent, { src, at });
    };
    const up = (ev: PointerEvent) => {
      if (ev.pointerId === pid) end(true, ev);
    };
    const cancel = (ev: PointerEvent) => {
      if (ev.pointerId === pid) end(false);
    };
    const esc = (ev: KeyboardEvent) => {
      if (ev.key !== "Escape") return;
      ev.preventDefault();
      ev.stopPropagation();
      end(false);
    };
    stop.current = () => end(false);
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
    window.addEventListener("pointercancel", cancel);
    window.addEventListener("keydown", esc, true);
  }, []);

  /** Each chain's lit nodes and the one under the pointer. */
  const targetsOf = (key: string): ReadonlySet<string> | undefined => targets.get(key);
  const overOf = (key: string): string | null => (over?.key === key ? over.node : null);
  return { band, targets, over, startWire, targetsOf, overOf, dragging: !!band };
}

/** What running a dropped wire needs from the host. */
export interface WireRunDeps {
  /** The flight a chain is (a stops-only one's, a session's live one). */
  flightOf(key: string): Flight | null;
  flights: readonly Flight[];
  kinds: readonly StepKindDef[];
  chain(key: string): PlacedChain | null;
  /** A stops-only chain's stops onto another chain (`mergeGrounded`), at a
   * flight index (undefined: the end; a session with no flight: attach). */
  merge(fromKey: string, intoKey: string, at?: number): Promise<unknown> | void;
  /** Pin a chain's slot. */
  pinAt(key: string, at: Pos): unknown;
  /** A chain's label height (a new chain's slot sits that much above its tiles). */
  labelH: number;
}

/** A stops-only chain's name (GroundedLine's `groundedName`). */
const groundedName = (f: Pick<Flight, "plan_name">) => f.plan_name || "Stops";

/** A flight index as the routes take it: past the end = appended. */
const atOrEnd = (f: Pick<Flight, "steps">, at: number | undefined) => (at !== undefined && at < f.steps.length ? at : undefined);

/** Do what a dropped wire means (docs/flights.md "Round 18" → "Wiring"):
 * move a stop, draw a Loop, merge or attach a stops-only chain, put in a
 * Handoff, open a palette at the drop, or split the chain there. */
export async function runWireIntent(intent: WireIntent, ctx: { src: WireEnd; at: Pos }, d: WireRunDeps): Promise<void> {
  if (intent.op === "none") return;
  if (intent.op === "attach") {
    await d.merge(intent.chain, intent.session);
    return;
  }
  if (intent.op === "merge") {
    const f = d.flightOf(intent.chain);
    await d.merge(intent.from, intent.chain, f && intent.after ? loopSlotAfter(intent.after) : undefined);
    return;
  }
  if (intent.op === "palette") {
    const c = d.chain(intent.chain);
    if (!c) return;
    // The new tile's top-left: its square centred where the wire was let go.
    const p = inChain(c, intent.at);
    const snap = (v: number) => Math.round(v / GRID_SNAP) * GRID_SNAP;
    c.paletteAfter(intent.after, { x: snap(p.x - NODE_W / 2), y: snap(p.y - TILE / 2) });
    return;
  }
  if (intent.op === "parallel" && !intent.step) {
    // A NEW stop beside X's next: the chain's palette for it (its own hook),
    // else the plain palette after X (an older chain).
    const c = d.chain(intent.chain);
    if (!c) return;
    const p = inChain(c, intent.at ?? ctx.at);
    const snap = (v: number) => Math.round(v / GRID_SNAP) * GRID_SNAP;
    const pos = { x: snap(p.x - NODE_W / 2), y: snap(p.y - TILE / 2) };
    if (c.paletteParallel) c.paletteParallel(intent.with, pos);
    else c.paletteAfter(intent.after, pos);
    return;
  }
  const f = d.flightOf(intent.chain);
  if (intent.op === "parallel") {
    // Shift-drop: that stop joins X's next's group.
    const step = intent.step ? firstStepOf(intent.step) : undefined;
    const withStep = firstStepOf(intent.with);
    if (!f || !step || !withStep) return;
    if (await joinParallel(f.id, step.id, withStep.id)) toast("“" + step.title + "” runs beside “" + withStep.title + "”");
    return;
  }
  if (intent.op === "handoff") {
    const def = d.kinds.find((k) => k.kind === HANDOFF_KIND);
    if (!def) return void toast("This server has no “Start a chain” stop yet");
    const target = d.flights.find((x) => groundedKey(x.id) === intent.target);
    const step = handoffStepFor(def, f ? f.steps.map((st) => st.id) : [], intent.target);
    const ok = f ? await tackOn(f.id, step, atOrEnd(f, loopSlotAfter(intent.from))) : !!(await tackOnNew(intent.chain, [step]));
    if (ok) toast("“" + intent.from.title + "” starts “" + (target ? groundedName(target) : "that chain") + "” on " + sessionWord("new"));
    return;
  }
  if (!f) return;
  if (intent.op === "move") {
    const step = firstStepOf(intent.step);
    const to = intent.after ? loopSlotAfter(intent.after) : 0;
    if (step && to !== undefined) await moveStop(f.id, step.id, Math.min(to, f.steps.length - 1));
    return;
  }
  if (intent.op === "loop") {
    const def = d.kinds.find((k) => k.kind === "loop");
    const target = firstStepOf(intent.to);
    const at = loopSlotAfter(intent.from);
    if (!def || !target || at === undefined) return void toast("This server has no Loop stop");
    if (await tackOn(f.id, loopStepTo(def, f.steps.map((st) => st.id), target), atOrEnd(f, at))) toast("Loops back to “" + target.title + "”");
    return;
  }
  if (intent.op === "split") {
    const step = firstStepOf(intent.step);
    if (!step) return;
    const chain = await splitOffChain(f.id, step.id, { x: 0, y: 0 });
    if (!chain) return;
    // Its slot where the wire was let go (its first tile under the pointer),
    // its other tiles kept where they were, relative to it.
    const label = d.chain(intent.chain)?.dy ?? d.labelH;
    d.pinAt(groundedKey(chain.id), { x: Math.round(ctx.at.x - NODE_W / 2), y: Math.round(ctx.at.y - TILE / 2 - label) });
    const base = "sky" in ctx.src ? null : ctx.src.node;
    const moved: Record<string, { x: number; y: number }> = {};
    if (base) for (const st of chain.steps.slice(1)) if (st.pos) moved[st.id] = { x: st.pos.x - base.x, y: st.pos.y - base.y };
    if (Object.keys(moved).length) await saveStopPositions(chain.id, moved);
    toast("Split off “" + groundedName(chain) + "”");
  }
}

// --- Chain side --------------------------------------------------------------------

/** The step a node's spot is kept on: its own, a group's first sub-step. */
export const posStepOf = (n: Pick<DiagramNode, "step" | "group">) => firstStepOf(n);

/** A diagram with spots `local` (by step id) laid over its steps' saved
 * `pos` — a tile drawn where it was dropped before the server says so. */
export function withLocalPos(d: Diagram, local: Readonly<Record<string, Pos>>): Diagram {
  const ids = Object.keys(local);
  if (!ids.length) return d;
  const put = (n: DiagramNode): DiagramNode => {
    const st = posStepOf(n);
    const p = st ? local[st.id] : undefined;
    if (!st || !p) return n;
    if (n.step) return { ...n, step: { ...n.step, pos: p } };
    if (n.group && n.group.steps.length) return { ...n, group: { ...n.group, steps: [{ ...n.group.steps[0], pos: p }, ...n.group.steps.slice(1)] } };
    return n;
  };
  return { ...d, nodes: d.nodes.map(put) };
}

/** A chain's dragged tiles: drawn at once where they were dropped, saved a
 * moment later (`queueStopPos`); a spot the server now has (or refused) is
 * let go. `move` is undefined with no flight (nothing to keep it on). */
export function useNodeMoves(flightId: string | null | undefined, d: Diagram, chainKey?: string) {
  const [local, setLocal] = useState<Record<string, Pos>>({});
  // Sub-sessions and workers (no step to carry a `pos`): kept per device.
  const [mine, setMine] = useState<Record<string, Pos>>(() => (chainKey ? loadNodePos(chainKey) : {}));
  useEffect(() => setMine(chainKey ? loadNodePos(chainKey) : {}), [chainKey]);
  // Spots the flights query already has are the server's again.
  useEffect(() => {
    const ids = Object.keys(local);
    if (!ids.length) return;
    const saved = new Map<string, Pos | undefined>();
    for (const n of d.nodes) {
      const st = posStepOf(n);
      if (st) saved.set(st.id, st.pos);
    }
    const keep = ids.filter((id) => {
      const s = saved.get(id);
      return !(s && s.x === local[id].x && s.y === local[id].y);
    });
    if (keep.length !== ids.length) setLocal(Object.fromEntries(keep.map((id) => [id, local[id]])));
  }, [d, local]);
  const move =
    flightId || chainKey
      ? (node: GraphNode, pos: Pos) => {
          const p = { x: Math.round(pos.x), y: Math.round(pos.y) };
          const st = posStepOf(node.node);
          if (st && flightId) {
            setLocal((l) => ({ ...l, [st.id]: p }));
            queueStopPos(flightId, st.id, p, (ok) => {
              if (!ok) setLocal((l) => Object.fromEntries(Object.entries(l).filter(([id]) => id !== st.id)));
            });
            return;
          }
          if (!st && chainKey) {
            setMine((m) => {
              const next = { ...m, [node.id]: p };
              saveNodePos(chainKey, next);
              return next;
            });
          }
        }
      : undefined;
  // Memoized: the chain's graph (and so the host's registry) follows it.
  const diagram = useMemo(() => withNodePos(withLocalPos(d, local), mine), [d, local, mine]);
  return { diagram, move };
}

/** The diagram with the per-device spots of its non-step nodes (`node.pos`). */
export function withNodePos(d: Diagram, spots: Readonly<Record<string, Pos>>): Diagram {
  if (!Object.keys(spots).length) return d;
  return { ...d, nodes: d.nodes.map((n) => (!n.step && !n.group && spots[n.id] ? { ...n, pos: spots[n.id] } : n)) };
}

/** Hand the host this chain's entry whenever its graph, or where it sits in
 * its slot, changes (`wrap` = the element holding the SkyChain, a child of
 * the line's root, which sits at the slot's origin). */
export function useChainEntry(
  register: ((e: ChainEntry | null) => void) | undefined,
  graph: ChainGraph,
  wrap: RefObject<HTMLElement | null>,
  paletteAfter: ChainEntry["paletteAfter"],
  /** Round 20 (optional): the chain's palette for a stop in parallel. */
  paletteParallel?: ChainEntry["paletteParallel"]
) {
  const palRef = useRef(paletteAfter);
  palRef.current = paletteAfter;
  const pal = useCallback<ChainEntry["paletteAfter"]>((a, p) => palRef.current(a, p), []);
  const parRef = useRef(paletteParallel);
  parRef.current = paletteParallel;
  const hasPar = !!paletteParallel;
  const par = useCallback<NonNullable<ChainEntry["paletteParallel"]>>((w, p) => parRef.current?.(w, p), []);
  const last = useRef<{ graph: ChainGraph; dx: number; dy: number; par: boolean } | null>(null);
  useLayoutEffect(() => {
    if (!register) return;
    const el = wrap.current;
    const dx = el ? el.offsetLeft : 0;
    const dy = el ? el.offsetTop : 0;
    const l = last.current;
    if (l && l.graph === graph && l.dx === dx && l.dy === dy && l.par === hasPar) return;
    last.current = { graph, dx, dy, par: hasPar };
    register({ graph, dx, dy, paletteAfter: pal, ...(hasPar ? { paletteParallel: par } : {}) });
  });
  useEffect(() => {
    if (!register) return;
    return () => {
      last.current = null;
      register(null);
    };
  }, [register]);
}

/** The coach tour's hooks on tiles SkyChain draws (lib/tours/sky.ts): each
 * selector's first match inside `root` gets its `data-tour`, on the element
 * itself. */
export function useTourHooks(root: RefObject<HTMLElement | null>, hooks: Readonly<Record<string, string>>) {
  useEffect(() => {
    const el = root.current;
    if (!el) return;
    for (const [sel, name] of Object.entries(hooks)) {
      const t = el.querySelector(sel);
      if (t && t.getAttribute("data-tour") !== name) t.setAttribute("data-tour", name);
    }
  });
}
