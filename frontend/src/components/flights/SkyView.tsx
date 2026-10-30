/** The sky — the main area's second view, beside the grid (docs/web-ui.md
 * "Two views: the grid and the sky", docs/flights.md "The Sky"). The same
 * sessions as the grid, drawn as lines on one open canvas: the rail, the top
 * bar, the bell and every dialog stay; only the main area changes
 * (App renders this in the grid's place while `useUi.view === "sky"`).
 *
 * One plane, panned and zoomed as a whole — one `translate(x, y) scale(z)` on
 * one container, no library. The wheel or a trackpad pans it, so do the middle
 * button, a touch drag and Space+drag (from anywhere); Ctrl/⌘+wheel and a pinch
 * zoom it (40%–300%). The keyboard: arrows pan, + / − zoom, 0 fits every line,
 * 1 fits the focused one, T tidies, Enter opens the focused line's box, Esc
 * closes it. A faint dot grid scales with it, the flock (SkyFlock) drifts
 * behind it.
 *
 * A left drag on empty sky is a MARQUEE (lib/skySelect): what it touches is
 * selected — whole lines, or the stop boxes of one line — Shift+drag adds,
 * a click on empty sky or Esc clears, Ctrl/⌘+A takes every line shown. The
 * selection bar (SkySelection) acts on it; dragging any selected line's bar
 * moves them all; Delete / Backspace removes (asking first where it must).
 *
 * Every session is a LINE (SkyLine): its title bar, its session diagram and
 * its session box, which opens into the session's pane right there. The
 * focused pane is the focused line (`useUi.focused`, one selection for both
 * views): a press on a line selects it, selecting a session anywhere brings
 * its line into view. Where a line sits, its size and whether its box is open
 * are the layout store's (lib/skyLayout, `mf_sky_layout`, per device).
 *
 * The sky is STILL: nothing moves unless the owner moves it. A line gets a
 * spot once, when it first appears (measured, at the first free place —
 * `assignSpots`), and keeps it however it grows; a dragged line is pinned
 * where it was dropped; Tidy forgets both and is the only time lines glide.
 * The camera fits once (a first show, a new view) and then only on demand
 * (0, 1, Fit). The camera, the drawer and the shelf survive a trip to the
 * grid and back.
 *
 * Built from the space (docs/flights.md "Round 11"): a double-click on empty
 * sky, the shelf's Session chip (pressed, or dragged onto the sky) or
 * Ctrl/Alt+N opens a NewSessionBlock there — a block the owner can drag by
 * its bar before it flies; its session's line shows at once (the optimistic
 * `pending_create` row), pinned where the block sat. The shelf at the foot
 * (StopShelf, one row: Session, the stop kinds, the saved stops) is where
 * stops come from: a chip dropped on a line is tacked on there, dropped on
 * empty sky it becomes stops with no session yet — a GROUNDED line
 * (GroundedLine, laid out under `flight:<id>`, after the session lines,
 * newest first) that does nothing until ⋯ Fly on a session… or a Session
 * chip dropped on it gives it one. `useUi.skyShelf` asks for the shelf from
 * outside — the palette, Schedule's Edit in the Sky, an old
 * `#flights/flyways/<id>` link: the shelf unfolds, and a named saved stop
 * opens its Takes off dialog. The intake lane (IntakeLane) sits at the
 * plane's left edge; the drawer at the right (SkyDrawer) holds what is
 * perched on you. A phone gets the lines as a plain list — no pan, no zoom —
 * with the shelf under it. */

import { useCallback, useEffect, useMemo, useRef, useState, type DragEvent as ReactDragEvent, type MouseEvent as ReactMouseEvent, type PointerEvent as ReactPointerEvent } from "react";
import { useFlightKinds, useFlightPlans, useFlights, useInstances } from "../../state/queries";
import { effectiveViewMode, useUi } from "../../state/store";
import { selectSession } from "../../lib/sessionActions";
import { overlayOpen } from "../../lib/keymap";
import { layoutDiagram, sessionDiagram, type Diagram } from "../../lib/flightDiagram";
import { STOP_MIME, isLive, newStep, type Flight, type FlightPlan, type FlightRow, type StepKindDef } from "../../lib/flights";
import { attachGrounded, startFlightOn, startGrounded, tackOn, tackOnNew, tackOnPlan } from "../../lib/flightsApi";
import { defaultStepFor, groundedDiagram, groundedFlights, groundedIdOf, groundedKey, isGroundedKey } from "../../lib/groundedLines";
import { windowName } from "../../lib/windowName";
import { reducedMotion, registerSkyHost, type Box, type SkyHost } from "../../lib/viewMode";
import {
  FLYWAY_MIME,
  INTAKE_W,
  LINES_LEFT,
  NEW_SESSION_MIME,
  SKY_DRAWER_KEY,
  SKY_MARGIN,
  SKY_MODES,
  ZOOM_MAX,
  ZOOM_MIN,
  assignSpots,
  boundsOf,
  centerOn,
  estimateLine,
  firstFit,
  fitCamera,
  inSky,
  lineLayout,
  lodFor,
  loadSkyLayout,
  patchSkyLine,
  pinnedPositions,
  skyDrawerOpen,
  skyLineTitles,
  tidySkyLayout,
  toPlane,
  zoomAt,
  type PlacedLine,
  type Pos,
  type Rect,
  type SkyCamera,
  type SkyLineLayout,
  type SkyMode,
} from "../../lib/skyLayout";
import {
  MARQUEE_MIN,
  NO_SELECTION,
  addSelection,
  isStopNode,
  planeToScreen,
  pruneSelection,
  rectFrom,
  screenToPlane,
  select,
  selectLines,
  selectionEmpty,
  stopSelection,
  toggleLine,
  type SelectLine,
  type SelectStop,
  type SkySelection,
} from "../../lib/skySelect";
import { ChipRow, TextField } from "./controls";
import { liveBoxParts } from "./FlightStrip";
import { FlywayCanvas } from "./FlywayCanvas";
import { IntakeLane } from "./IntakeLane";
import { NewSessionBlock } from "./NewSessionBlock";
import { SkyDrawer } from "./SkyDrawer";
import { SkyFlock } from "./SkyFlock";
import { SkyLine } from "./SkyLine";
import { SkySelectionBar, type SelectionAsk } from "./SkySelection";
import { GroundedLine, groundedName } from "./GroundedLine";
import { StopShelf } from "./StopShelf";
import { TakeoffDialog } from "./TakeoffDialog";
import { Bird } from "./Bird";
import { TourButton } from "../onboarding/TourButton";
import { useAutoTour } from "../onboarding/useAutoTour";
import "./Sky.css";

/** A line's chrome around its canvas: the title bar and the card's padding. */
const BAR_H = 34;
const LINE_PAD = 10;
const LINE_MIN_W = 280;
/** The dot grid's spacing at 100%. */
const GRID = 24;
/** One arrow press pans this far (screen px); Shift: four times. */
const PAN_STEP = 80;
/** The Stops shelf folded to its handle ("1"); shown unless folded. */
const SHELF_FOLD_KEY = "mf_sky_stops_folded";
/** How long Tidy's glide lasts (the slots' transition is on only then). */
const TIDY_MS = 350;

const readPref = (key: string): string | null => {
  try {
    return typeof localStorage === "undefined" ? null : localStorage.getItem(key);
  } catch {
    return null;
  }
};
const writePref = (key: string, v: string) => {
  try {
    localStorage.setItem(key, v);
  } catch {
    /* a private window: the choice lasts this visit */
  }
};

/** Typing in a field (or a contenteditable, or a terminal) owns the keyboard. */
const typing = (el: Element | null) =>
  !!el && (el.tagName === "INPUT" || el.tagName === "TEXTAREA" || (el as HTMLElement).isContentEditable === true);

/** The sky's own keys apply: nothing that takes keys itself has the focus —
 * a field, a button (Enter / Space press it), a chip row or a menu (arrows),
 * a flyway canvas (← / → move its stop), an opened box's pane. */
export function skyKeysOwn(el: Element | null): boolean {
  if (!el || el === document.body) return true;
  if (typing(el)) return false;
  return !el.closest("button, a, summary, select, [role=menu], [role=menuitem], [role=radiogroup], [role=listbox], [role=tablist], .fl-canvas, .pane");
}

/** A phone-narrow window: the lines as a list, no pan or zoom. */
function useNarrowWindow(): boolean {
  const [narrow, setNarrow] = useState(() => typeof window !== "undefined" && !!window.matchMedia?.("(max-width: 600px)").matches);
  useEffect(() => {
    const mq = typeof window !== "undefined" ? window.matchMedia?.("(max-width: 600px)") : undefined;
    if (!mq) return;
    const on = () => setNarrow(mq.matches);
    mq.addEventListener?.("change", on);
    return () => mq.removeEventListener?.("change", on);
  }, []);
  return narrow;
}

/** The size a line is drawn at (plane px) before SkyLine has measured it: its
 * canvas — perches opened wide, the bird's headroom — plus the title bar and
 * padding. */
export function lineSize(d: Diagram, flight: Flight | null): { w: number; h: number } {
  const lay = layoutDiagram(d, { sizeOf: liveBoxParts(flight, true, () => {}).sizeOf });
  // + 12: the canvas's own padding; + 4: the card's border and a hair.
  return { w: Math.max(LINE_MIN_W, lay.width + 12 + LINE_PAD * 2 + 4), h: lay.height + 32 + BAR_H + LINE_PAD * 2 };
}

/** A closed session's line (or removed stops) on its way out: the last
 * picture of it, fading. */
function GhostLine({ name, pos, diagram }: { name: string; pos: Pos; diagram: Diagram }) {
  return (
    <div className="sky-line sky-line-leaving" style={{ transform: `translate(${pos.x}px, ${pos.y}px)` }} aria-hidden="true">
      <div className="sky-line-bar">
        <Bird size={13} />
        <span className="sky-line-name">{name}</span>
      </div>
      <FlywayCanvas diagram={diagram} label={name} vertical={false} className="sky-canvas" />
    </div>
  );
}

interface Ghost {
  id: string;
  name: string;
  pos: Pos;
  diagram: Diagram;
}

/** A grounded line's chrome beyond a session line's: the dashed head box
 * where its session will sit. */
const GROUNDED_HEAD_W = 182;

/** Where the new-session block sits, and what it is for: `attach` = the
 * grounded flight it makes a session for; `flying` = the guessed title of
 * the session it is making (the block hides while its line shows). */
interface NewBlockAt extends Pos {
  attach?: { id: string; name: string };
  flying?: string;
}

/** The camera across a trip to the grid and back: where it was and which
 * view it was fitted for (the same view back: no refit). */
let memo: { cam: SkyCamera; key: string } | null = null;

export function SkyView() {
  const instQ = useInstances();
  const rows = useMemo(() => instQ.data || [], [instQ.data]);
  const flightsQ = useFlights();
  const flights = flightsQ.data;
  const kinds = useFlightKinds().data || [];
  const plansQ = useFlightPlans();
  const plans = plansQ.data || [];
  const focused = useUi((s) => s.focused);
  const mode = useUi((s) => s.skyMode);
  const setSkyMode = useUi((s) => s.setSkyMode);
  const gridOne = useUi((s) => (effectiveViewMode(s) as string) === "1");
  const motionOff = useUi((s) => s.reduceMotion) || reducedMotion();
  const narrow = useNarrowWindow();

  const [query, setQuery] = useState("");
  const [layout, setLayout] = useState(() => loadSkyLayout());
  const [measured, setMeasured] = useState<Record<string, { w: number; h: number }>>({});
  // The spots lines were given when they first appeared (`assignSpots`): kept
  // however a line grows, freed when its session goes, cleared by Tidy.
  const [autoPos, setAutoPos] = useState<Record<string, Pos>>({});
  const [tidying, setTidying] = useState(false);
  const [newOver, setNewOver] = useState(false);
  // A line being dragged by its bar — with every other selected line when it
  // is one of them: where each started, and how far they have come.
  const [drag, setDrag] = useState<{ from: Record<string, Pos>; dx: number; dy: number } | null>(null);
  const [cam, setCam] = useState<SkyCamera>(() => memo?.cam || { x: 0, y: 0, z: 1 });
  const camRef = useRef(cam);
  camRef.current = cam;
  const [vp, setVp] = useState({ w: 0, h: 0 });
  const vpRef = useRef<HTMLDivElement>(null);
  const [newAt, setNewAt] = useState<NewBlockAt | null>(null);
  const [drawerPref, setDrawerPref] = useState(() => readPref(SKY_DRAWER_KEY));
  // The Stops shelf is one slim row, shown unless the owner folds it (then
  // remembered per device).
  const [shelfFolded, setShelfFolded] = useState(() => readPref(SHELF_FOLD_KEY) === "1");
  const shelfAsk = useUi((s) => s.skyShelf);
  // A saved stop's Takes off dialog, asked for from outside (Schedule).
  const [takeoff, setTakeoff] = useState<FlightPlan | null>(null);
  // The grounded line last pressed (a session line's focus is useUi.focused).
  const [groundFocus, setGroundFocus] = useState("");
  // A grounded flight's ⋯ Fly on a session…: which line.
  const [attachPick, setAttachPick] = useState<Flight | null>(null);
  // A Session chip dragged over a grounded line: that line's key.
  const [sessionOver, setSessionOver] = useState("");
  // The Sky tour opens by itself the first time the sky is shown (once).
  useAutoTour("sky", true);
  const [flyPick, setFlyPick] = useState<FlightPlan | null>(null);
  // A stop chip on the shelf pressed with no line focused: which line.
  const [tackPick, setTackPick] = useState<StepKindDef | null>(null);
  const [drop, setDrop] = useState<{ title: string; plan: FlightPlan } | null>(null);
  // What a shelf chip being dragged carries (read at its dragstart).
  const dragged = useRef<{ plan: FlightPlan } | { kind: string } | null>(null);
  const [flash, setFlash] = useState("");
  const [spaceHeld, setSpaceHeld] = useState(false);
  const space = useRef(false);
  // The marquee's selection (lib/skySelect), what its bar is asking, and —
  // for stops — where they were drawn, relative to their line (the bar sits
  // over them, and follows the line if it moves).
  const [selRaw, setSel] = useState<SkySelection>(NO_SELECTION);
  const [selAsk, setSelAsk] = useState<SelectionAsk>("");
  const [selStops, setSelStops] = useState<Rect | null>(null);
  // The marquee being drawn (viewport px).
  const [marq, setMarq] = useState<Rect | null>(null);
  const clearSel = useCallback(() => {
    setSel(NO_SELECTION);
    setSelAsk("");
  }, []);

  // Every session's line (a `pending_create` row too: a session the sky is
  // making shows at once, a loading box under its name), then the grounded
  // flights' — stops with no session yet — newest first. Not in This session
  // or Perched on you; Find matches their name.
  const titles = useMemo(() => skyLineTitles(rows, mode, { query, focus: focused || "" }), [rows, mode, query, focused]);
  const grounded = useMemo(() => {
    if (mode === "one" || mode === "perched") return [];
    const q = query.trim().toLowerCase();
    return groundedFlights(flights).filter((f) => !q || groundedName(f).toLowerCase().includes(q));
  }, [flights, mode, query]);
  const groundedIds = useMemo(() => new Set(groundedFlights(flights).map((f) => f.id)), [flights]);
  const lines = useMemo(
    () => [
      ...titles.map((t) => {
        const fl = rows.find((r) => r.title === t)?.flight as FlightRow | null | undefined;
        const full = fl && isLive(fl) ? flights?.find((f) => f.id === fl.id) || null : null;
        const diagram = sessionDiagram(rows, t, { flights, kinds, family: mode === "mine" || mode === "one" });
        return { id: t, name: windowName(t), flight: null as Flight | null, diagram, ...(measured[t] || estimateLine(lineSize(diagram, full), layout[t])) };
      }),
      ...grounded.map((f) => {
        const id = groundedKey(f.id);
        const diagram = groundedDiagram(f, kinds);
        const base = lineSize(diagram, null);
        return { id, name: groundedName(f), flight: f, diagram, ...(measured[id] || estimateLine({ w: base.w + GROUNDED_HEAD_W, h: base.h }, layout[id])) };
      }),
    ],
    [titles, grounded, rows, flights, kinds, mode, measured, layout]
  );
  // The selection holds only lines still shown (a closed session drops out).
  const shownIds = useMemo(() => new Set(lines.map((l) => l.id)), [lines]);
  const sel = useMemo(() => pruneSelection(selRaw, shownIds), [selRaw, shownIds]);
  /** A line still on the board, shown or filtered out: its session is open
   * (not a hidden helper), or its grounded flight is still grounded. */
  const lineAlive = useCallback(
    (id: string) => (isGroundedKey(id) ? groundedIds.has(groundedIdOf(id)) : rows.some((r) => r.title === id && inSky(r))),
    [rows, groundedIds]
  );
  // Every line's spot: a pinned or already-given one stays put; a newcomer
  // gets the first free place clear of the placed lines (and of the lines a
  // filter hides, at their last measured size). `fresh` is what the effect
  // below keeps once the newcomer has been measured.
  const { place, fresh } = useMemo(() => {
    if (mode === "one") return { place: Object.fromEntries(lines.map((l) => [l.id, { x: SKY_MARGIN, y: SKY_MARGIN }])), fresh: {} as Record<string, Pos> };
    const saved: Record<string, Pos> = { ...autoPos, ...pinnedPositions(layout) };
    const shown = new Set(lines.map((l) => l.id));
    const existing: PlacedLine[] = lines.filter((l) => saved[l.id]).map((l) => ({ id: l.id, ...saved[l.id], w: l.w, h: l.h }));
    for (const [id, at] of Object.entries(saved))
      if (!shown.has(id) && measured[id] && lineAlive(id)) existing.push({ id, ...at, ...measured[id] });
    const fresh = assignSpots(existing, lines.filter((l) => !saved[l.id]), { width: Math.max(vp.w, LINES_LEFT + 900), left: LINES_LEFT, top: SKY_MARGIN });
    const place: Record<string, Pos> = { ...fresh };
    for (const e of existing) if (shown.has(e.id)) place[e.id] = { x: e.x, y: e.y };
    return { place, fresh };
  }, [lines, layout, autoPos, measured, lineAlive, vp.w, mode]);
  // Keep a newcomer's spot once it is known where it fits: the window
  // measured, the sessions and the flights loaded, the line drawn and
  // measured. Free the spots of sessions that are gone and of lines the
  // owner pinned (their spot is the pin now).
  const flightsSettled = flightsQ.isFetched || flightsQ.data !== undefined;
  useEffect(() => {
    if (mode === "one" || !vp.w || instQ.data === undefined) return;
    setAutoPos((cur) => {
      let next = cur;
      const edit = () => (next === cur ? (next = { ...cur }) : next);
      for (const id of Object.keys(cur))
        if (layout[id]?.pinned || !lineAlive(id)) delete edit()[id];
      if (flightsSettled) for (const [id, at] of Object.entries(fresh)) if (measured[id]) edit()[id] = at;
      return next;
    });
  }, [fresh, measured, layout, lineAlive, mode, vp.w, instQ.data, flightsSettled]);
  const posOf = useCallback((id: string): Pos => {
    const f = drag?.from[id];
    return f ? { x: f.x + drag!.dx, y: f.y + drag!.dy } : place[id] || { x: LINES_LEFT, y: SKY_MARGIN };
  }, [drag, place]);
  const rectOf = useCallback((id: string): Rect | null => {
    const l = lines.find((x) => x.id === id);
    return l ? { ...posOf(id), w: l.w, h: l.h } : null;
  }, [lines, posOf]);
  // Folded to its tab (the perch count on it) until opened, then remembered
  // per device: a first visit keeps the room for the sky.
  const drawerOpen = skyDrawerOpen(drawerPref);
  // The line a selection lands on: the focused session's, else its parent's
  // (a worker folded into its lead's line in Mine).
  const focusLine = useMemo(() => {
    if (!focused) return "";
    if (titles.includes(focused)) return focused;
    const parent = rows.find((r) => r.title === focused)?.parent || "";
    return titles.includes(parent) ? parent : "";
  }, [focused, titles, rows]);

  // The window's size, for fitting and for where the auto-layout wraps.
  useEffect(() => {
    const el = vpRef.current;
    if (!el) return;
    const measure = () => setVp({ w: el.clientWidth, h: el.clientHeight });
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, [narrow]);

  /** Every line and the intake lane — legibly (`firstFit`: the width at no
   * less than mid, top-left, when everything would need far), or, `whole`
   * (the toolbar's Fit), all of it however small. */
  const fitAll = useCallback((whole = false) => {
    const rects = lines.map((l) => ({ ...posOf(l.id), w: l.w, h: l.h }));
    if (mode !== "one") rects.push({ x: SKY_MARGIN, y: SKY_MARGIN, w: INTAKE_W, h: 1 });
    setCam((whole ? fitCamera : firstFit)(boundsOf(rects), vp.w, vp.h, mode === "one" ? 1.5 : 1));
  }, [lines, posOf, mode, vp.w, vp.h]);
  /** The grid's "1": the focused line filling the window. */
  const fitFocused = useCallback(() => {
    const r = focusLine ? rectOf(focusLine) : null;
    if (!r) return fitAll();
    setCam(fitCamera(r, vp.w, vp.h, 1.5));
  }, [focusLine, rectOf, fitAll, vp.w, vp.h]);

  // Fit ONCE: on the first show of the sky, once the lines have their spots
  // (drawn and measured, or after a short wait), and again only when the view
  // changes (All / Mine / Perched / This session). Never because a line grew,
  // a session came or went, or the window resized: the owner pans, or presses
  // 0, 1 or Fit. The first fit follows the grid's view: "1" fits the focused
  // line, the rest fit all.
  const fitted = useRef(memo?.key || "");
  const fitKey = mode + "\u0000" + (mode === "one" ? focused : "");
  const [waited, setWaited] = useState(false);
  useEffect(() => {
    if (instQ.data === undefined || waited) return;
    const t = setTimeout(() => setWaited(true), 900);
    return () => clearTimeout(t);
  }, [instQ.data, waited]);
  const settled = waited || (flightsSettled && lines.every((l) => measured[l.id] && (mode === "one" || autoPos[l.id] || layout[l.id]?.pinned)));
  useEffect(() => {
    if (!vp.w || !vp.h || instQ.data === undefined || !settled) return;
    if (fitted.current === fitKey) return;
    const first = !fitted.current;
    fitted.current = fitKey;
    if (first && gridOne && mode !== "one") fitFocused();
    else fitAll();
  }, [vp.w, vp.h, instQ.data, fitKey, settled]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(
    () => () => {
      memo = { cam: camRef.current, key: fitted.current };
    },
    []
  );

  // Selecting a session anywhere (the rail, the bell, a shortcut) brings its
  // line into view: centred if it is off screen, flashed either way.
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);
  // A first visit fits (the effect above), so the first line focused then is
  // already in the picture; back from the grid, the focused session may have
  // changed there, so it is checked.
  const lastFocus = useRef(memo ? "" : null as string | null);
  useEffect(() => {
    if (!vp.w || !focusLine) return;
    if (lastFocus.current === null || focusLine === lastFocus.current) {
      lastFocus.current = focusLine;
      return;
    }
    lastFocus.current = focusLine;
    const r = rectOf(focusLine);
    if (!r) return;
    // Pressed on the sky itself (its bar, a box): it is under the pointer
    // already — the camera stays put, the line only flashes. A wide line is
    // never "whole in view", so moving it would jolt every click.
    const pressed = pressedLine.current === focusLine;
    pressedLine.current = "";
    const c = camRef.current;
    const sx = c.x + r.x * c.z;
    const sy = c.y + r.y * c.z;
    const inView = sx >= 0 && sy >= 0 && sx + r.w * c.z <= vp.w && sy + Math.min(r.h, 240) * c.z <= vp.h;
    if (!inView && !pressed) {
      setCam(centerOn(r, vp.w, vp.h, Math.max(c.z, 0.85)));
    }
    setFlash(focusLine);
    setTimeout(() => alive.current && setFlash((f) => (f === focusLine ? "" : f)), 1400);
  }, [focusLine, rectOf, vp.w, vp.h]);

  // A closed (or deleted) session's line leaves: the last picture fades out.
  const last = useRef(new Map<string, Ghost>());
  const [ghosts, setGhosts] = useState<Ghost[]>([]);
  useEffect(() => {
    const now = new Map<string, Ghost>(lines.map((l) => [l.id, { id: l.id, name: l.name, pos: place[l.id], diagram: l.diagram }]));
    const gone = [...last.current.values()].filter((g) => !now.has(g.id) && !lineAlive(g.id));
    last.current = now;
    if (!gone.length || narrow) return;
    const ids = new Set(gone.map((g) => g.id));
    setGhosts((cur) => [...cur.filter((g) => !ids.has(g.id)), ...gone]);
    setTimeout(() => alive.current && setGhosts((cur) => cur.filter((g) => !ids.has(g.id))), 450);
  }, [lines, place, lineAlive, narrow]);

  /** Select a line's session — the same selection a pane click makes. A
   * press on the line itself is remembered, so the selection effect above
   * leaves the camera alone for it. */
  const pressedLine = useRef("");
  const focusOn = useCallback((id: string, pressed = false) => {
    pressedLine.current = pressed ? id : "";
    setGroundFocus("");
    selectSession(id, { noKeyboard: true });
  }, []);

  const setMode = (m: SkyMode) => {
    if (m === "one" && !focused) {
      if (!titles[0]) return;
      focusOn(titles[0]);
    }
    setSkyMode(m);
  };

  const zoomBy = (k: number) => {
    setCam((c) => zoomAt(c, c.z * k, vp.w / 2, vp.h / 2));
  };
  const panBy = (dx: number, dy: number) => {
    setCam((c) => ({ ...c, x: c.x + dx, y: c.y + dy }));
  };
  /** A point in the middle of what is on screen (a card's top-left). */
  const middle = (): Pos => {
    const c = toPlane(camRef.current, vp.w / 2, vp.h / 3);
    return { x: Math.round(c.x - 170), y: Math.round(c.y) };
  };

  const foldShelf = (folded: boolean) => {
    setShelfFolded(folded);
    writePref(SHELF_FOLD_KEY, folded ? "1" : "0");
  };
  // Asked for the shelf (useUi.showFlyways): unfold the Stops shelf, and open
  // the named saved stop's Takes off dialog once the saved stops have loaded
  // ("new": the shelf alone — a saved stop is made on the board now).
  const plansLoaded = plansQ.data !== undefined;
  useEffect(() => {
    if (!shelfAsk) return;
    if (shelfFolded) foldShelf(false);
    if (shelfAsk.plan && shelfAsk.plan !== "new" && !plansLoaded) return;
    useUi.getState().takeSkyShelf();
    const plan = shelfAsk.plan && shelfAsk.plan !== "new" ? plans.find((p) => p.id === shelfAsk.plan) : null;
    if (plan) setTakeoff(plan);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [shelfAsk, plansLoaded]);
  const newHere = () => setNewAt(middle());

  // --- The layout store -----------------------------------------------------------

  const onLayout = useCallback((id: string, patch: Partial<SkyLineLayout>) => {
    // `{ pinned: false }` is "Tidy this line": its spot is forgotten.
    setLayout((l) => (patch.pinned === false ? tidySkyLayout(l, id) : patchSkyLine(l, id, patch)));
  }, []);
  /** Every line back to its organized spot: the pins and the given spots
   * are forgotten, every line is placed again, and they glide there — the
   * only glide on the sky (`data-tidying` turns the slots' transition on for
   * a moment). The camera stays where it is. */
  const tidyTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const tidy = () => {
    setAutoPos({});
    setLayout((l) => tidySkyLayout(l));
    if (motionOff) return;
    setTidying(true);
    clearTimeout(tidyTimer.current);
    tidyTimer.current = setTimeout(() => alive.current && setTidying(false), TIDY_MS);
  };
  /** Tidy these (the selection bar): only their spots are forgotten. */
  const tidyThese = (ids: string[]) => {
    setAutoPos((cur) => Object.fromEntries(Object.entries(cur).filter(([id]) => !ids.includes(id))));
    setLayout((l) => ids.reduce((acc, id) => tidySkyLayout(acc, id), l));
    clearSel();
    if (motionOff) return;
    setTidying(true);
    clearTimeout(tidyTimer.current);
    tidyTimer.current = setTimeout(() => alive.current && setTidying(false), TIDY_MS);
  };
  const onMeasure = useCallback((id: string, w: number, h: number) => {
    setMeasured((m) => {
      const o = m[id];
      if (o && Math.abs(o.w - w) < 2 && Math.abs(o.h - h) < 2) return m;
      return { ...m, [id]: { w: Math.round(w), h: Math.round(h) } };
    });
  }, []);

  // --- Dragging a line by its bar ----------------------------------------------------

  // A selected line drags every selected line with it (each pinned where it
  // lands); a click on a bar outside the selection clears it, Shift+click
  // takes the line in or out.
  const onDragStart = (id: string, e: ReactPointerEvent) => {
    if (e.button !== 0) return;
    e.stopPropagation();
    const ids = sel.lines.has(id) ? [...sel.lines] : [id];
    const from: Record<string, Pos> = Object.fromEntries(ids.map((x) => [x, posOf(x)]));
    const g = { pid: e.pointerId, sx: e.clientX, sy: e.clientY, moved: false, shift: e.shiftKey };
    const delta = (ev: PointerEvent) => ({ dx: (ev.clientX - g.sx) / camRef.current.z, dy: (ev.clientY - g.sy) / camRef.current.z });
    const move = (ev: PointerEvent) => {
      if (ev.pointerId !== g.pid) return;
      if (!g.moved && Math.hypot(ev.clientX - g.sx, ev.clientY - g.sy) < 4) return;
      g.moved = true;
      setDrag({ from, ...delta(ev) });
    };
    const up = (ev: PointerEvent) => {
      if (ev.pointerId !== g.pid) return;
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
      window.removeEventListener("pointercancel", up);
      setDrag(null);
      if (g.moved) {
        const { dx, dy } = delta(ev);
        for (const [id, f] of Object.entries(from)) {
          const p = { x: f.x + dx, y: f.y + dy };
          onLayout(id, { x: p.x, y: p.y, pinned: true });
        }
        return;
      }
      if (g.shift) {
        setSel((s) => toggleLine(s, id));
        return;
      }
      if (!sel.lines.has(id)) clearSel();
      if (isGroundedKey(id)) setGroundFocus(id);
      else focusOn(id, true);
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
    window.addEventListener("pointercancel", up);
  };

  // --- Stops onto lines ----------------------------------------------------------------

  /** The live flight on a session's line, if any. */
  const liveFlightOf = (title: string): Flight | null => {
    const fl = rows.find((r) => r.title === title)?.flight as FlightRow | null | undefined;
    return fl && isLive(fl) ? flights?.find((f) => f.id === fl.id) || null : null;
  };
  /** Add a saved stop to `title` — or the focused line; with neither, a row
   * of the lines to pick from. A line with a flight in the air takes it as
   * one more stop (one group) at the end; one without starts flying it. */
  const onFly = (plan: FlightPlan, title?: string) => {
    const t = title || focusLine;
    if (!t) {
      setFlyPick(plan);
      return;
    }
    setFlyPick(null);
    const live = liveFlightOf(t);
    if (live) void tackOnPlan(live.id, plan.id);
    else void startFlightOn(t, plan.id);
  };
  /** A grounded flight takes off on `title` (its ⋯ Fly on a session…). */
  const onAttach = (f: Flight, title: string) => {
    setAttachPick(null);
    void attachGrounded(f.id, { title });
  };
  /** Tack one stop of `kind` (a shelf chip pressed) onto `title` — or the
   * focused line; with neither, a row of the lines to pick from. A line
   * with no flight yet starts an ad-hoc one (the fast-track's rungs become
   * its stops), as the line's own + does. */
  const onTack = (kind: string, title?: string) => {
    const def = kinds.find((k) => k.kind === kind);
    if (!def) return;
    const t = title || focusLine;
    if (!t) {
      setTackPick(def);
      return;
    }
    setTackPick(null);
    const full = liveFlightOf(t);
    if (full) void tackOn(full.id, newStep(def, full.steps.map((st) => st.id)));
    else void tackOnNew(t, [newStep(def, [])]);
  };
  /** The line under a screen point (the shelf chip being dragged over it). */
  const lineAt = (clientX: number, clientY: number): string => {
    const r = vpRef.current?.getBoundingClientRect();
    if (!r) return "";
    const p = toPlane(camRef.current, clientX - r.left, clientY - r.top);
    for (const l of [...lines].reverse()) {
      const q = posOf(l.id);
      if (p.x >= q.x && p.x <= q.x + l.w && p.y >= q.y && p.y <= q.y + l.h) return l.id;
    }
    return "";
  };
  /** A screen point on the plane. */
  const planeAt = (clientX: number, clientY: number): Pos => {
    const r = vpRef.current!.getBoundingClientRect();
    const at = toPlane(camRef.current, clientX - r.left, clientY - r.top);
    return { x: Math.round(at.x), y: Math.round(at.y) };
  };
  // A shelf chip's drag: what it carries is read once, at its dragstart (a
  // drag's data can't be read while it is over something).
  const onShelfDragStart = (e: ReactDragEvent) => {
    let id = "";
    let kind = "";
    try {
      id = e.dataTransfer?.getData(FLYWAY_MIME) || "";
      kind = e.dataTransfer?.getData(STOP_MIME) || "";
    } catch {
      id = "";
    }
    const plan = id ? plans.find((p) => p.id === id) || null : null;
    dragged.current = plan ? { plan } : kind ? { kind } : null;
  };
  const onSkyDragOver = (e: ReactDragEvent) => {
    const d = dragged.current;
    if (!d) return;
    const t = lineAt(e.clientX, e.clientY);
    // Over empty sky (stops with no session yet) or a line (tacked on).
    if (!t && !ground(e.target)) return;
    e.preventDefault();
    if (e.dataTransfer) e.dataTransfer.dropEffect = "copy";
    if ("plan" in d) setDrop((cur) => (t && !isGroundedKey(t) ? (cur?.title === t ? cur : { title: t, plan: d.plan }) : null));
  };
  // A chip dropped where its line's canvas didn't take it: on empty sky it
  // becomes a grounded line pinned at the drop; on a line, the end of it.
  const onSkyDrop = (e: ReactDragEvent) => {
    const d = dragged.current;
    if (!d) return;
    const t = lineAt(e.clientX, e.clientY);
    const onGround = !t && ground(e.target);
    dragged.current = null;
    setDrop(null);
    if (!t && !onGround) return;
    e.preventDefault();
    if (onGround) {
      const at = planeAt(e.clientX, e.clientY);
      void dropGrounded(d, at);
      return;
    }
    const gid = groundedIdOf(t);
    if ("plan" in d) {
      if (gid) void tackOnPlan(gid, d.plan.id);
      else onFly(d.plan, t);
    } else if (gid) {
      const def = kinds.find((k) => k.kind === d.kind);
      const f = flights?.find((x) => x.id === gid);
      if (def && f) void tackOn(gid, newStep(def, f.steps.map((st) => st.id)));
    } else onTack(d.kind, t);
  };
  /** A chip on empty sky: stops with no session yet, pinned where it fell. */
  const dropGrounded = async (d: { plan: FlightPlan } | { kind: string }, at: Pos) => {
    let f: Flight | null = null;
    if ("plan" in d) f = await startGrounded({ plan_id: d.plan.id });
    else {
      const def = kinds.find((k) => k.kind === d.kind);
      if (!def) return;
      f = await startGrounded({ steps: [defaultStepFor(def)] });
    }
    if (!f || !alive.current) return;
    const id = groundedKey(f.id);
    setLayout((l) => patchSkyLine(l, id, { x: at.x, y: at.y, pinned: true }));
    setGroundFocus(id);
  };
  const onShelfDragEnd = () => {
    dragged.current = null;
    setDrop(null);
    setSessionOver("");
    setNewOver(false);
  };

  // --- What the view switch reads -----------------------------------------------------

  const hostRef = useRef<SkyHost>({ newHere, ready: () => false, lineRects: () => new Map<string, Box>() });
  hostRef.current.newHere = newHere;
  hostRef.current.ready = () => !narrow && vp.w > 0 && fitted.current !== "";
  hostRef.current.lineRects = () => {
    const out = new Map<string, Box>();
    const r = vpRef.current?.getBoundingClientRect();
    if (!r) return out;
    const c = camRef.current;
    for (const l of lines) {
      const p = posOf(l.id);
      out.set(l.id, { x: r.left + c.x + p.x * c.z, y: r.top + c.y + p.y * c.z, w: l.w * c.z, h: Math.min(l.h, 260) * c.z });
    }
    return out;
  };
  useEffect(
    () =>
      registerSkyHost({
        newHere: () => hostRef.current.newHere(),
        ready: () => hostRef.current.ready(),
        lineRects: () => hostRef.current.lineRects(),
      }),
    []
  );

  // --- Pan, pinch, wheel, keys ---------------------------------------------------

  const pointers = useRef(new Map<number, Pos>());
  const pan = useRef<{ id: number; x: number; y: number; cx: number; cy: number } | null>(null);
  const pinch = useRef<{ d: number; z: number } | null>(null);
  const ground = (t: EventTarget | null) =>
    t instanceof Element && (t === vpRef.current || t.classList.contains("sky-plane") || t.classList.contains("sky-ground"));

  const startPan = (e: ReactPointerEvent<HTMLDivElement>) => {
    pointers.current.set(e.pointerId, { x: e.clientX, y: e.clientY });
    try {
      e.currentTarget.setPointerCapture(e.pointerId);
    } catch {
      /* a synthetic event has no capture */
    }
    if (pointers.current.size >= 2) {
      const [a, b] = [...pointers.current.values()];
      pinch.current = { d: Math.hypot(a.x - b.x, a.y - b.y) || 1, z: camRef.current.z };
      pan.current = null;
    } else pan.current = { id: e.pointerId, x: e.clientX, y: e.clientY, cx: camRef.current.x, cy: camRef.current.y };
  };
  const onDownCapture = (e: ReactPointerEvent<HTMLDivElement>) => {
    // Space held: a drag anywhere pans, even over a line.
    if (!space.current || (e.pointerType === "mouse" && e.button !== 0)) return;
    e.stopPropagation();
    e.preventDefault();
    startPan(e);
  };
  // The marquee: a left drag (mouse or pen) on empty sky. A touch drag still
  // pans, as the middle button does.
  const marquee = useRef<{ id: number; sx: number; sy: number; add: boolean; moved: boolean } | null>(null);
  const onDown = (e: ReactPointerEvent<HTMLDivElement>) => {
    if (!ground(e.target) || (e.pointerType === "mouse" && e.button !== 0 && e.button !== 1)) return;
    e.preventDefault();
    // The keyboard comes to the sky (arrows, + / −, T), out of a terminal.
    vpRef.current?.focus({ preventScroll: true });
    if (e.pointerType !== "touch" && e.button === 0) startMarquee(e);
    else startPan(e);
  };
  const startMarquee = (e: ReactPointerEvent<HTMLDivElement>) => {
    try {
      e.currentTarget.setPointerCapture(e.pointerId);
    } catch {
      /* a synthetic event has no capture */
    }
    marquee.current = { id: e.pointerId, sx: e.clientX, sy: e.clientY, add: e.shiftKey, moved: false };
  };
  /** The viewport's corner on screen. */
  const vpCorner = (): Pos => {
    const r = vpRef.current?.getBoundingClientRect();
    return { x: r?.left || 0, y: r?.top || 0 };
  };
  /** What the marquee can take, measured now (plane px): every shown line's
   * rect and title bar, and every stop box on it — never one inside an open
   * card's pane. */
  const selectables = (): { lines: SelectLine[]; stops: SelectStop[] } => {
    const out: { lines: SelectLine[]; stops: SelectStop[] } = { lines: [], stops: [] };
    const root = vpRef.current;
    if (!root) return out;
    const o = vpCorner();
    const c = camRef.current;
    const onPlane = (el: Element) => {
      const b = el.getBoundingClientRect();
      return screenToPlane(c, { x: b.left - o.x, y: b.top - o.y, w: b.width, h: b.height });
    };
    const slots = new Map<string, Element>();
    root.querySelectorAll(".sky-line-slot[data-slot]").forEach((el) => slots.set(el.getAttribute("data-slot") || "", el));
    for (const l of lines) {
      const slot = slots.get(l.id);
      const heads: Rect[] = [];
      slot?.querySelectorAll(".skl-bar").forEach((el) => {
        if (!el.closest(".skl-card")) heads.push(onPlane(el));
      });
      out.lines.push({ id: l.id, rect: { ...posOf(l.id), w: l.w, h: l.h }, heads });
      slot?.querySelectorAll("[data-node]").forEach((el) => {
        const node = el.getAttribute("data-node") || "";
        if (isStopNode(node) && !el.closest(".skl-card")) out.stops.push({ line: l.id, node, rect: onPlane(el) });
      });
    }
    return out;
  };
  const moveMarquee = (e: ReactPointerEvent<HTMLDivElement>) => {
    const m = marquee.current!;
    if (!m.moved && Math.hypot(e.clientX - m.sx, e.clientY - m.sy) < MARQUEE_MIN) return;
    m.moved = true;
    const o = vpCorner();
    setMarq(rectFrom({ x: m.sx - o.x, y: m.sy - o.y }, { x: e.clientX - o.x, y: e.clientY - o.y }));
  };
  const endMarquee = (e: ReactPointerEvent<HTMLDivElement>) => {
    const m = marquee.current!;
    marquee.current = null;
    setMarq(null);
    if (e.type === "pointercancel") return;
    if (!m.moved) {
      // A click on empty sky clears the selection.
      if (!m.add) clearSel();
      return;
    }
    const o = vpCorner();
    const area = screenToPlane(camRef.current, rectFrom({ x: m.sx - o.x, y: m.sy - o.y }, { x: e.clientX - o.x, y: e.clientY - o.y }));
    const items = selectables();
    const got = select(area, items);
    const next = m.add ? addSelection(sel, got) : got;
    setSel(next);
    setSelAsk("");
    // The stops' own boxes, relative to their line: the bar sits over them.
    const one = stopSelection(next);
    const boxes = one ? items.stops.filter((x) => x.line === one.line && one.nodes.includes(x.node)).map((x) => x.rect) : [];
    const b = boundsOf(boxes);
    const at = one ? posOf(one.line) : null;
    setSelStops(b && at ? { ...b, x: b.x - at.x, y: b.y - at.y } : null);
  };
  const onMove = (e: ReactPointerEvent<HTMLDivElement>) => {
    if (marquee.current?.id === e.pointerId) return moveMarquee(e);
    if (!pointers.current.has(e.pointerId)) return;
    pointers.current.set(e.pointerId, { x: e.clientX, y: e.clientY });
    const pn = pinch.current;
    if (pn && pointers.current.size >= 2) {
      const [a, b] = [...pointers.current.values()];
      const r = vpRef.current?.getBoundingClientRect();
      const d = Math.hypot(a.x - b.x, a.y - b.y) || 1;
      setCam(zoomAt(camRef.current, (pn.z * d) / pn.d, (a.x + b.x) / 2 - (r?.left || 0), (a.y + b.y) / 2 - (r?.top || 0)));
      return;
    }
    const pa = pan.current;
    if (pa && pa.id === e.pointerId) setCam({ ...camRef.current, x: pa.cx + (e.clientX - pa.x), y: pa.cy + (e.clientY - pa.y) });
  };
  const onUp = (e: ReactPointerEvent<HTMLDivElement>) => {
    if (marquee.current?.id === e.pointerId) return endMarquee(e);
    pointers.current.delete(e.pointerId);
    if (pointers.current.size < 2) pinch.current = null;
    if (pan.current?.id === e.pointerId) pan.current = null;
  };
  // The viewport is overflow: hidden, but a browser still scrolls it to show
  // a box that just took the focus (a click, Tab). The plane would slide out
  // from under the camera: turn that scroll into a pan and zero it.
  const onViewportScroll = () => {
    const el = vpRef.current;
    if (!el || (!el.scrollLeft && !el.scrollTop)) return;
    const dx = el.scrollLeft;
    const dy = el.scrollTop;
    el.scrollLeft = 0;
    el.scrollTop = 0;
    setCam((c) => ({ ...c, x: c.x - dx, y: c.y - dy }));
  };
  /** The new-session block at a screen point (a double-click, a drop). */
  const newAtScreen = (clientX: number, clientY: number) => {
    const r = vpRef.current!.getBoundingClientRect();
    const at = toPlane(camRef.current, clientX - r.left, clientY - r.top);
    setNewAt({ x: Math.round(at.x), y: Math.round(at.y) });
  };
  /** The block for a grounded line: a session made for its stops, which then
   * take off on it. It opens just above the line. */
  const newForGrounded = (key: string) => {
    const f = grounded.find((x) => groundedKey(x.id) === key);
    if (!f) return;
    const p = posOf(key);
    setNewAt({ x: p.x, y: Math.max(SKY_MARGIN, p.y - 40), attach: { id: f.id, name: groundedName(f) } });
  };
  const onDouble = (e: ReactMouseEvent<HTMLDivElement>) => {
    if (!ground(e.target)) return;
    newAtScreen(e.clientX, e.clientY);
  };
  // The shelf's Session chip dragged in: over empty sky the sky lights
  // softly and takes the drop; the block opens where it lands, as a
  // double-click there would. Over a grounded line the line lights and the
  // block makes a session for its stops. Over a session's line it is refused.
  const carriesNew = (e: ReactDragEvent) => Array.from(e.dataTransfer?.types || []).includes(NEW_SESSION_MIME);
  const onNewDragOver = (e: ReactDragEvent<HTMLDivElement>) => {
    if (!carriesNew(e)) return;
    const t = lineAt(e.clientX, e.clientY);
    const into = isGroundedKey(t) ? t : "";
    const ok = !t && ground(e.target);
    setNewOver(ok);
    setSessionOver((cur) => (cur === into ? cur : into));
    if (!ok && !into) return;
    e.preventDefault();
    if (e.dataTransfer) e.dataTransfer.dropEffect = "copy";
  };
  const onNewDragLeave = (e: ReactDragEvent<HTMLDivElement>) => {
    if (!(e.relatedTarget instanceof Node && e.currentTarget.contains(e.relatedTarget))) {
      setNewOver(false);
      setSessionOver("");
    }
  };
  const onNewDrop = (e: ReactDragEvent<HTMLDivElement>) => {
    if (!carriesNew(e)) return;
    setNewOver(false);
    setSessionOver("");
    const t = lineAt(e.clientX, e.clientY);
    if (isGroundedKey(t)) {
      e.preventDefault();
      e.stopPropagation();
      newForGrounded(t);
      return;
    }
    if (t || !ground(e.target)) return;
    e.preventDefault();
    e.stopPropagation();
    vpRef.current?.focus({ preventScroll: true });
    newAtScreen(e.clientX, e.clientY);
  };

  // The wheel: a native listener, since React's is passive and can't keep the
  // page from scrolling.
  useEffect(() => {
    const el = vpRef.current;
    if (!el) return;
    const onWheel = (e: WheelEvent) => {
      if ((e.target as Element | null)?.closest?.(".sky-own-scroll")) return;
      e.preventDefault();
      const unit = e.deltaMode === 1 ? 16 : e.deltaMode === 2 ? el.clientHeight : 1;
      const dx = e.deltaX * unit;
      const dy = e.deltaY * unit;
      if (e.ctrlKey || e.metaKey) {
        const r = el.getBoundingClientRect();
        setCam((c) => zoomAt(c, c.z * Math.exp(-dy * 0.0022), e.clientX - r.left, e.clientY - r.top));
      } else if (e.shiftKey && !dx) setCam((c) => ({ ...c, x: c.x - dy }));
      else setCam((c) => ({ ...c, x: c.x - dx, y: c.y - dy }));
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, [narrow]);

  // The sky's keys. Esc folds an open card first, then closes the focused
  // line's box; the rest only while nothing that takes keys has the focus
  // and no dialog is over the sky.
  const keys = useRef<(e: KeyboardEvent) => void>(() => {});
  keys.current = (e: KeyboardEvent) => {
    const el = document.activeElement;
    // The sky's own cards are dialogs too (role="dialog"): Esc folds them
    // unless a real dialog is over the whole sky.
    if (e.key === "Escape" && ((newAt && !newAt.flying) || flyPick || tackPick || attachPick) && !useUi.getState().openDialog) {
      e.preventDefault();
      if (!newAt?.flying) setNewAt(null);
      setFlyPick(null);
      setTackPick(null);
      setAttachPick(null);
      return;
    }
    if (overlayOpen()) return;
    // The selection: Esc clears it (or the question its bar is asking, from
    // inside the bar too); Delete / Backspace removes what it holds.
    const inBar = !!el?.closest(".sky-selbar");
    if (e.key === "Escape" && !selectionEmpty(sel) && (inBar || skyKeysOwn(el))) {
      e.preventDefault();
      if (selAsk) setSelAsk("");
      else clearSel();
      return;
    }
    // Ctrl/⌘+A: every line the sky shows.
    if ((e.ctrlKey || e.metaKey) && !e.altKey && !e.shiftKey && (e.key === "a" || e.key === "A") && skyKeysOwn(el) && lines.length) {
      e.preventDefault();
      setSel(selectLines(lines.map((l) => l.id)));
      setSelAsk("");
      return;
    }
    if (!skyKeysOwn(el) || e.ctrlKey || e.metaKey || e.altKey) return;
    const k = e.key;
    const handled = () => e.preventDefault();
    if (k === " " && !e.repeat) {
      handled();
      space.current = true;
      setSpaceHeld(true);
    } else if (k === "0") {
      handled();
      fitAll();
    } else if (k === "1") {
      handled();
      fitFocused();
    } else if (k === "+" || k === "=") {
      handled();
      zoomBy(1.2);
    } else if (k === "-" || k === "_") {
      handled();
      zoomBy(1 / 1.2);
    } else if (k === "t" || k === "T") {
      handled();
      tidy();
    } else if (k.startsWith("Arrow")) {
      handled();
      const d = PAN_STEP * (e.shiftKey ? 4 : 1);
      panBy(k === "ArrowLeft" ? d : k === "ArrowRight" ? -d : 0, k === "ArrowUp" ? d : k === "ArrowDown" ? -d : 0);
    } else if (k === "Enter" && focusLine) {
      handled();
      onLayout(focusLine, { open: true });
    } else if (k === "Escape" && focusLine && layout[focusLine]?.open) {
      handled();
      onLayout(focusLine, { open: false });
    }
  };
  // Delete / Backspace with a selection removes what it holds. Taken in the
  // window's capture phase, before the app's keymap (document, capture) sees
  // it: there Delete closes the focused SESSION, never what is selected.
  const removeKeys = useRef<(e: KeyboardEvent) => void>(() => {});
  removeKeys.current = (e: KeyboardEvent) => {
    if ((e.key !== "Delete" && e.key !== "Backspace") || selectionEmpty(sel) || e.ctrlKey || e.metaKey || e.altKey) return;
    if (overlayOpen() || !skyKeysOwn(document.activeElement)) return;
    e.preventDefault();
    e.stopPropagation();
    setSelAsk("remove");
  };
  useEffect(() => {
    if (narrow) return;
    const del = (e: KeyboardEvent) => removeKeys.current(e);
    window.addEventListener("keydown", del, true);
    return () => window.removeEventListener("keydown", del, true);
  }, [narrow]);
  useEffect(() => {
    if (narrow) return;
    const down = (e: KeyboardEvent) => keys.current(e);
    const up = (e: KeyboardEvent) => {
      if (e.key !== " ") return;
      space.current = false;
      setSpaceHeld(false);
    };
    window.addEventListener("keydown", down);
    window.addEventListener("keyup", up, true);
    return () => {
      window.removeEventListener("keydown", down);
      window.removeEventListener("keyup", up, true);
    };
  }, [narrow]);

  // --- Drawing ----------------------------------------------------------------------

  const empty =
    lines.length > 0
      ? ""
      : mode === "one"
        ? focused
          ? "“" + windowName(focused) + "” isn't in the sky right now."
          : "Pick a line first."
        : query
          ? "No line matches “" + query + "”."
          : mode === "perched"
            ? "Nothing is perched on you."
            : instQ.data === undefined
              ? ""
              : "No sessions in the sky yet — double-click anywhere, or drag the Session chip from the shelf below, to start one.";

  const toolbar = (
    <div className="sky-toolbar" role="toolbar" aria-label="The sky">
      <ChipRow<SkyMode>
        className="sky-modes"
        label="Which lines"
        value={mode}
        options={SKY_MODES.map((m) => ({ value: m.id, label: m.label, title: m.title }))}
        onChange={setMode}
      />
      <span className="sky-search">
        <TextField value={query} onChange={setQuery} label="Find a line" placeholder="Find a line…" onEnter={() => titles[0] && focusOn(titles[0])} />
      </span>
      {!narrow && (
        <button type="button" className="fl-btn sky-tidy" title="Put every line back in its organized spot (T)" onClick={tidy}>
          Tidy
        </button>
      )}
      {!narrow && (
        <span className="sky-zoom" role="group" aria-label="Zoom">
          <button type="button" className="sky-zoom-btn" aria-label="Zoom out" title="Zoom out (−)" disabled={cam.z <= ZOOM_MIN + 0.001} onClick={() => zoomBy(1 / 1.2)}>
            −
          </button>
          <span className="sky-zoom-read" aria-live="polite">
            {Math.round(cam.z * 100)}%
          </span>
          <button type="button" className="sky-zoom-btn" aria-label="Zoom in" title="Zoom in (+)" disabled={cam.z >= ZOOM_MAX - 0.001} onClick={() => zoomBy(1.2)}>
            +
          </button>
          <button type="button" className="sky-zoom-btn sky-fit-one" title="Fit the focused line — the grid's 1 (1)" disabled={!focusLine} onClick={fitFocused}>
            1
          </button>
          <button
            type="button"
            className="sky-zoom-btn sky-fit"
            title="See everything: every line in the window, however small — 0 fits them legibly (titles readable, pan down for the rest)"
            onClick={() => fitAll(true)}
          >
            Fit
          </button>
          <button type="button" className="sky-zoom-btn sky-zoom-100" title="Back to 100%" onClick={() => zoomBy(1 / cam.z)}>
            100%
          </button>
        </span>
      )}
      <TourButton tour="sky" />
    </div>
  );

  // Which line: a saved stop (Add) or a stop chip (Tack on) with no line
  // focused, or a grounded line's stops (Fly) looking for their session.
  const asking = attachPick
    ? { word: "Fly", name: groundedName(attachPick), on: (t: string) => onAttach(attachPick, t) }
    : flyPick
      ? { word: "Add", name: flyPick.name, on: (t: string) => onFly(flyPick, t) }
      : tackPick
        ? { word: "Tack", name: tackPick.label, on: (t: string) => onTack(tackPick.kind, t) }
        : null;
  // A grounded flight flies on a session with no flight in the air.
  const pickable = attachPick ? titles.filter((t) => !liveFlightOf(t)) : titles;
  const pick = asking && (
    <div className="sky-flypick" role="group" aria-label={asking.word + " " + asking.name + " on which line?"}>
      <span className="muted">
        {asking.word} {asking.name} on
      </span>
      {pickable.slice(0, 12).map((t) => (
        <button key={t} type="button" className="fl-chip" title={t} onClick={() => asking.on(t)}>
          {windowName(t)}
        </button>
      ))}
      {attachPick && pickable.length === 0 && <span className="muted">no session without a flight — drop a Session chip on the stops instead</span>}
      <button
        type="button"
        className="linklike"
        onClick={() => {
          setFlyPick(null);
          setTackPick(null);
          setAttachPick(null);
        }}
      >
        Cancel
      </button>
    </div>
  );

  const drawer = (
    <SkyDrawer
      open={drawerOpen}
      onToggle={() => {
        const v = drawerOpen ? "0" : "1";
        setDrawerPref(v);
        writePref(SKY_DRAWER_KEY, v);
      }}
      onFocus={focusOn}
    />
  );

  // The Stops shelf: the chips' drags are read here as they start (the sky
  // owns their drops). `display: contents`, so the shelf sits in the stage.
  const shelf = (
    <div className="sky-shelf-host" onDragStart={onShelfDragStart} onDragEnd={onShelfDragEnd}>
      <StopShelf onTack={onTack} onTackPlan={onFly} onSession={newHere} folded={shelfFolded} onFold={foldShelf} />
      {takeoff && <TakeoffDialog plan={takeoff} onClose={() => setTakeoff(null)} />}
    </div>
  );

  // The block, while its session is being made, hides: the session's own
  // line (the optimistic row) already sits where it was, pinned there.
  const newBlock = newAt && (
    <NewSessionBlock
      x={newAt.x}
      y={newAt.y}
      plans={plans}
      attach={newAt.attach}
      hidden={!!newAt.flying}
      zoom={narrow ? undefined : cam.z}
      onMove={narrow ? undefined : (at) => setNewAt({ ...newAt, ...at })}
      onCancel={() => setNewAt(null)}
      onPending={(guess) => {
        setLayout((l) => patchSkyLine(l, guess, { x: newAt.x, y: newAt.y, pinned: true }));
        setNewAt({ ...newAt, flying: guess });
      }}
      onFailed={() => {
        const guess = newAt.flying;
        if (guess) setLayout((l) => tidySkyLayout(l, guess));
        setNewAt((cur) => (cur ? { ...cur, flying: undefined } : cur));
      }}
      onCreated={(title) => {
        const guess = newAt.flying;
        setLayout((l) => patchSkyLine(l, title, { x: newAt.x, y: newAt.y, pinned: true }));
        if (guess && guess !== title) setLayout((l) => tidySkyLayout(l, guess));
        setNewAt(null);
        focusOn(title);
      }}
    />
  );

  const lineFor = (id: string, at?: Pos) => {
    const f = isGroundedKey(id) ? lines.find((l) => l.id === id)?.flight : null;
    if (f)
      return (
        <GroundedLine
          key={id}
          flight={f}
          kinds={kinds}
          plans={plans}
          layout={lineLayout(layout[id], at || { x: 0, y: 0 })}
          focused={groundFocus === id}
          onFocus={() => setGroundFocus(id)}
          zoom={cam.z}
          onDragStart={at ? (e) => onDragStart(id, e) : undefined}
          onMeasure={at ? (w, h) => onMeasure(id, w, h) : undefined}
          onFlyOn={() => {
            setFlyPick(null);
            setTackPick(null);
            setAttachPick(f);
          }}
          sessionOver={sessionOver === id}
          selected={at ? sel.lines.has(id) : undefined}
          highlighted={at ? sel.stops.get(id) : undefined}
        />
      );
    return lineForSession(id, at);
  };
  const lineForSession = (id: string, at?: Pos) => (
    <SkyLine
      key={id}
      title={id}
      rows={rows}
      plans={plans}
      layout={lineLayout(layout[id], at || { x: 0, y: 0 })}
      onLayout={(patch) => onLayout(id, patch)}
      focused={focused === id}
      onFocus={() => focusOn(id, true)}
      zoom={cam.z}
      onDragStart={at ? (e) => onDragStart(id, e) : undefined}
      onMeasure={at ? (w, h) => onMeasure(id, w, h) : undefined}
      dropFlyway={drop?.title === id ? drop.plan : null}
      selected={at ? sel.lines.has(id) : undefined}
      highlighted={at ? sel.stops.get(id) : undefined}
    />
  );

  // The selection's bar, screen-sized, kept inside the window: just above
  // the selected lines (below them at the window's top); just under the
  // selected stops (above them at the window's foot), off their line's bar.
  const selOne = stopSelection(sel);
  const selBox = (() => {
    const one = stopSelection(sel);
    if (one) {
      const at = posOf(one.line);
      return selStops ? { ...selStops, x: selStops.x + at.x, y: selStops.y + at.y } : rectOf(one.line);
    }
    return boundsOf([...sel.lines].map(rectOf).filter((r): r is Rect => !!r));
  })();
  const selScreen = selBox && planeToScreen(cam, selBox);
  const selY = (r: Rect) => {
    const above = r.y - 52;
    const below = r.y + Math.min(r.h, 240) + 8;
    const y = selOne ? (below <= vp.h - 64 ? below : above) : above >= 8 ? above : below;
    return Math.max(8, Math.min(y, vp.h - 64));
  };
  const selBar = selScreen && !drag && (
    <SkySelectionBar
      selection={sel}
      x={Math.max(8, Math.min(selScreen.x, vp.w - 460))}
      y={selY(selScreen)}
      ask={selAsk}
      setAsk={setSelAsk}
      nameOf={(id) => lines.find((l) => l.id === id)?.name || windowName(id)}
      flightOf={(id) => lines.find((l) => l.id === id)?.flight || liveFlightOf(id)}
      kinds={kinds}
      onTidy={tidyThese}
      onClear={clearSel}
    />
  );

  if (narrow)
    return (
      <main id="sky-view" className="sky sky-narrow" data-tour="flights-sky">
        {toolbar}
        {pick}
        {drawer}
        {newBlock}
        <div className="sky-list">
          {empty && <p className="fl-empty muted">{empty}</p>}
          {lines.map((l) => lineFor(l.id))}
        </div>
        {shelf}
      </main>
    );

  return (
    <main id="sky-view" className="sky" onDragOver={onSkyDragOver} onDrop={onSkyDrop}>
      {toolbar}
      {pick}
      <div className="sky-main">
        <div className="sky-stage">
          <div
            ref={vpRef}
            className={"sky-viewport" + (spaceHeld ? " is-grab" : "") + (newOver ? " is-new-over" : "")}
            data-tour="flights-sky"
            tabIndex={-1}
            role="application"
            aria-roledescription="canvas"
            aria-label="The sky: every session as a line — drag empty space or use the arrows to pan, Ctrl+wheel or + / − to zoom, double-click to start a session"
            style={{ backgroundSize: `${GRID * cam.z}px ${GRID * cam.z}px`, backgroundPosition: `${cam.x}px ${cam.y}px` }}
            onPointerDownCapture={onDownCapture}
            onPointerDown={onDown}
            onPointerMove={onMove}
            onPointerUp={onUp}
            onPointerCancel={onUp}
            onDoubleClick={onDouble}
            onDragOver={onNewDragOver}
            onDragLeave={onNewDragLeave}
            onDrop={onNewDrop}
            onScroll={onViewportScroll}
          >
            <SkyFlock width={vp.w} height={vp.h} zoom={cam.z} paused={motionOff || !!drag} />
            <div
              className="sky-plane"
              data-dragging={drag ? "" : undefined}
              data-tidying={tidying ? "" : undefined}
              style={{ transform: `translate(${cam.x}px, ${cam.y}px) scale(${cam.z})` }}
            >
              {mode !== "one" && (
                <div
                  className="sky-intake"
                  data-lod={lodFor(cam.z)}
                  style={{ transform: `translate(${SKY_MARGIN}px, ${SKY_MARGIN}px)`, width: INTAKE_W, "--sky-z": cam.z } as React.CSSProperties}
                >
                  <IntakeLane rows={rows} plans={plans} onFly={focusOn} width={INTAKE_W} />
                </div>
              )}
              {lines.map((l) => {
                // The slot sits the line at its spot (the one it was given, or
                // where it was dropped); only Tidy glides it to a new one.
                const at = posOf(l.id);
                return (
                  <div
                    key={l.id}
                    className={"sky-line-slot" + (flash === l.id ? " is-flash" : "") + (drag?.from[l.id] ? " is-dragging" : "") + (l.flight ? " is-grounded" : "")}
                    data-slot={l.id}
                    data-tour={l.flight ? undefined : "sky-line"}
                    style={{ transform: `translate(${at.x}px, ${at.y}px)` }}
                  >
                    {lineFor(l.id, at)}
                  </div>
                );
              })}
              {ghosts.map((g) => (
                <GhostLine key={"ghost:" + g.id} {...g} />
              ))}
              {newBlock}
            </div>
            {marq && <div className="sky-marquee" aria-hidden="true" style={{ transform: `translate(${marq.x}px, ${marq.y}px)`, width: marq.w, height: marq.h }} />}
            {selBar}
            {empty && (
              <p className="sky-empty muted" role="status">
                {empty}
              </p>
            )}
          </div>
          {shelf}
        </div>
        {drawer}
      </div>
    </main>
  );
}
