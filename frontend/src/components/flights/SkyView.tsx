/** The sky — the main area's second view, beside the grid (docs/web-ui.md
 * "Two views: the grid and the sky", docs/flights.md "The Sky"). The same
 * sessions as the grid, drawn as lines on one open canvas: the rail, the top
 * bar, the bell and every dialog stay; only the main area changes
 * (App renders this in the grid's place while `useUi.view === "sky"`).
 *
 * One plane, panned and zoomed as a whole — one `translate(x, y) scale(z)` on
 * one container, no library. The wheel or a trackpad pans it, so do the middle
 * button, a touch drag and Space+drag (from anywhere); Ctrl/⌘+wheel and a pinch
 * zoom it (40%–300%). The keyboard: arrows pan, + / − zoom, 0 fits every line (as Fit),
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
 * with the shelf under it.
 *
 * Round 12 (docs/flights.md "As built: round 12 sky"): the intake lane is a
 * sky object like a line — layout key "intake", placed first by
 * `assignSpots`, dragged by its head on the line drag's own path, pinned where
 * dropped, forgotten by Tidy, expanded (`open`) from its head. Two TOOLS
 * (`mf_sky_tool`, the toolbar's Select (V) · Pan (H), Space held = pan):
 * `gestureFor` (lib/skySelect) decides what a press becomes. Delete / Backspace
 * goes where the keyboard is (`deleteFor`): the selection, a box (a stop
 * leaves its line, a session's box asks to close it), a line (its close or
 * remove question), or — on nothing of the sky's — the app's keymap.
 *
 * Round 13 (docs/flights.md "As built: round 13 sky"), a diagram tool's
 * manners: a palette click PLACES its block on the board (`placeGrounded`: a
 * grounded line at a free spot above the shelf or right of the floating
 * palette — `spotNear` — unselected, unfocused); only a drop on a line, the
 * line's own +, or a grounded line dragged onto a session's line
 * (`mergeTargetAt`, `.is-merge-target`) adds to a line. NOTHING FOCUSED is a
 * state: the sky never picks a line itself; a click on empty sky or Esc
 * selects nothing (`selectSession(null)`). The blocks float as a sky object
 * too (`SkyPalette`, layout key "palette", `mf_sky_palette`), placed, dragged,
 * sized and tidied like the intake lane.
 *
 * Round 14 (docs/flights.md "As built: round 14 sky"): every removal pushes
 * one entry on the sky's undo stack (lib/skyUndo) and Ctrl+Z / ⌘Z pops it
 * ("Undid: …"); a removal asks only when something runs (lib/skySelect
 * `removalNeedsAsk`); "All blocks…" floats the palette LARGE on the board
 * (`catalogFrame`) instead of a window-sized dialog — Esc or its × docks it.
 *
 * Round 15 (docs/flights.md "As built: round 15 objects"): everything on the
 * plane that is not a line is a SKY OBJECT from one registry
 * (lib/skyObjects `SKY_OBJECTS` → `skyObjects(data)`): the intake lane and
 * the palette (left, placed first), the Finished strip and the people
 * blocks (a column right of every line, `rightColumn`), the sticky notes
 * (free, pinned where they were put — server-side, `/api/sky/notes`). One
 * loop places, draws, drags, sizes and measures them (`hostFor`). The
 * Finished strip is hidden by default — ⋯ › Show the Finished strip
 * (`FINISHED_KEY`, per device); hidden, it takes no room. Devices are not
 * board objects: while a session's chain is dragged alone, the other devices
 * show as drop targets along the view's top edge (DeviceBlocks `DeviceDock`,
 * `sessionDrag`). A session's line dragged alone onto a person block or a
 * device target asks it to share or move it (`dropped`); a note dragged onto a line attaches to
 * it and moves with it; ⋯ → Add a note, or a right-click on empty sky, puts
 * a note at the pointer; Delete on a focused note removes it with Undo.
 *
 * Round 17 (docs/flights.md "As built: round 17 sky-core"), the manners of a
 * canvas tool: a dragged box SNAPS its edges and centres to the others'
 * within 8 screen px (lib/skySnap, `.sky-guide` lines while it lasts, Alt =
 * no snap); Shift+1 fits all, Shift+2 the selection, Shift+0 is 100 %; every
 * fit, focus and jump EASES the camera (~220 ms ease-out, `goTo`; none under
 * reduced motion); Ctrl+Shift+Z / ⌘⇧Z REDOES what Ctrl+Z undid ("Redid: …",
 * lib/skyUndo `popRedo`); Tab floats the palette with its filter focused and
 * a second Tab docks it (a double-click on empty sky is the new-session
 * block, as the guide says); F with a selection is FOCUS MODE (`.sky-focus` on the plane,
 * `data-dim` on the rest — a click or Esc lifts it); `?` opens the shortcuts
 * sheet; the minimap and the off-screen hints sit bottom-right, the zoom
 * readout is a menu (Minimap, OffscreenHints, ZoomMenu, SkyShortcuts).
 *
 * Round 18 (docs/flights.md "Round 18", docs/flights-r18/sky.md): every line
 * is a CHAIN of free-floating tiles (SkyLine / GroundedLine over SkyChain).
 * Each chain hands this host its graph (`useChainRegistry`); a port pressed
 * starts the host's wire drag (useSkyWiring: the rubber band, the targets lit
 * on every chain, Esc cancels), and the drop runs what lib/skyGraph
 * `wireIntent` says (`runWireIntent`: move · loop · merge · attach · handoff
 * · palette at the drop · split off). The wires BETWEEN chains — a Handoff
 * stop's dashed wire to the chain it starts — are drawn on the plane
 * (SkyWires); a stops-only chain something hands off to wears a trigger head. */

import { NEW_ROUTE_STARTS_ON } from "./useSessionLine";
import { optimisticDelete } from "../../lib/flightOptimism";
import { cloneElement, Fragment, isValidElement, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type DragEvent as ReactDragEvent, type MouseEvent as ReactMouseEvent, type PointerEvent as ReactPointerEvent, type ReactElement } from "react";
import { useDevices, useFlightKinds, useFlightPlans, useFlights, useInstances, usePeerLinks, useTestPlans } from "../../state/queries";
import { saveSkyNotes, type SkyNote } from "../../api/skyApi";
import type { Instance } from "../../api/types";
import { errMsg } from "../../lib/format";
import {
  hitAt,
  deviceKey,
  isObjectKey,
  newNoteId,
  noteDropped,
  noteIdOf,
  notePos,
  noteSize,
  objectClass,
  objectKind,
  skyObjects,
  useLandingData,
  useSkyNotesQuery,
  type SkyObjectData,
  type SkyObjectHost,
  type SkyObjectItem,
  type SkyObjectView,
} from "../../lib/skyObjects";
import { DeviceDock } from "./DeviceBlocks";
import type { LandingStripProps } from "./LandingStrip";
import { effectiveViewMode, useUi } from "../../state/store";
import { useOutbox } from "../../state/runs";
import { needsYouTitles } from "../../lib/needsYou";
import { MINIMAP_H, MINIMAP_W } from "../../lib/minimap";
import { selectSession } from "../../lib/sessionActions";
import { overlayOpen } from "../../lib/keymap";
import { layoutDiagram, sessionDiagram, type Diagram } from "../../lib/flightDiagram";
import { STOP_MIME, isShown, newStep, type Flight, type FlightPlan, type FlightRow, type FlightStep, type IntakeSourceRef, type StepKindDef } from "../../lib/flights";
import { attachGrounded, DEFAULT_SESSION, deleteFlight, renameFlight, startFlightOn, startGrounded, tackOn, tackOnNew, tackOnPlan, tackOnSteps } from "../../lib/flightsApi";
import { NODE_H, PITCH_X, type WireEnd, type WireIntent } from "../../lib/skyGraph";
import type { CatalogDrop } from "../../lib/stopCatalog";
import { toast } from "../../lib/toast";
import { onSessionRenamed } from "../../lib/renameSession";
import { popRedo, popUndo, pushUndo, undoAction } from "../../lib/skyUndo";
import { snapBox, snapThreshold, type SnapGuide } from "../../lib/skySnap";
import { deriveSessionState } from "../../lib/sessionState";
import { defaultStepFor, groundedDiagram, groundedFlights, groundedIdOf, groundedKey, groundedShown, handoffLinks, isGroundedKey, triggerHeadFor } from "../../lib/groundedLines";
import { windowName } from "../../lib/windowName";
import { reducedMotion, registerSkyHost, type Box, type SkyHost } from "../../lib/viewMode";
import {
  FLYWAY_MIME,
  FULL_DETAIL_LODS,
  INTAKE_KEY,
  INTAKE_OPEN_W,
  INTAKE_W,
  LINES_LEFT,
  NEW_SESSION_MIME,
  NOTE_H,
  NOTE_W,
  PALETTE_H,
  PALETTE_W,
  PLACE_GAP,
  SKY_DRAWER_KEY,
  SKY_GAP,
  SKY_MARGIN,
  SKY_MODES,
  ZOOM_MAX,
  ZOOM_MIN,
  CAM_EASE_MS,
  assignSpots,
  fillWidth,
  boundsOf,
  catalogFrame,
  centerOn,
  easeOut,
  estimateLine,
  firstFit,
  fitCamera,
  fitSelection,
  grownOver,
  lerpCamera,
  sameCamera,
  inSky,
  intakeSize,
  lineLayout,
  lodFor,
  loadSkyLayout,
  paletteFrame,
  patchSkyLine,
  renameSkyLine,
  pinnedPositions,
  rightColumn,
  skyDrawerOpen,
  skyLineTitles,
  pickThisSession,
  fitRoute,
  ROUTE_FIT_MIN,
  screenBoxCovers,
  linePerched,
  spotNear,
  tidySkyLayout,
  closeSkyWindows,
  toPlane,
  zoomAt,
  type PlacedLine,
  type Pos,
  type Rect,
  type SkyCamera,
  type SkyLineLayout,
  type SkyMode,
  intakeFrame,
  withLiveFlights,
  clearRightColumn,
  groundedLanding,
  GROUNDED_HIDDEN_LINE,
} from "../../lib/skyLayout";
import {
  MARQUEE_MIN,
  NO_SELECTION,
  SKY_TOOLS,
  addSelection,
  deleteFor,
  gestureFor,
  isStopNode,
  loadSkyTool,
  mergeTargetAt,
  planeToScreen,
  pruneSelection,
  rectFrom,
  saveSkyTool,
  screenToPlane,
  select,
  selectLines,
  selectionEmpty,
  stopSelection,
  toggleLine,
  type SelectLine,
  type SelectStop,
  type SkySelection,
  type SkyTarget,
  type SkyTool, toggleStop } from "../../lib/skySelect";
import { ChipRow, TextField } from "./controls";
import { OverflowMenu } from "../OverflowMenu";
import { liveBoxParts } from "./FlightStrip";
import { IntakeLane, openDefaultEditor } from "./IntakeLane";
import { NewSessionBlock } from "./NewSessionBlock";
import { SkyDrawer } from "./SkyDrawer";
import { useBehaviorsBoard, useBehaviorsRequest } from "./BehaviorsBoard";
import { SkyFlock, WeatherWord } from "./SkyFlock";
import { SkyLine, type LineDelete } from "./SkyLine";
import { SkySelectionBar, type SelectionAsk } from "./SkySelection";
import { GroundedLine, groundedName, markRouteEdit } from "./GroundedLine";
import { SkyWires, handoffWires } from "./SkyWires";
import { runWireIntent, useChainRegistry, useSkyWiring, type PlacedChain } from "./useSkyWiring";
import { StopShelf } from "./StopShelf";
import { PALETTE_KEY, PALETTE_MODE_KEY, SkyPalette, type PaletteMode } from "./SkyPalette";
import { SkyGuide } from "./SkyGuide";
import { Minimap, OffscreenHints, type MinimapRect } from "./Minimap";
import { ZoomMenu } from "./ZoomMenu";
import { SkyShortcuts } from "./SkyShortcuts";
import { SKY_SHORTCUT_GROUPS } from "../../lib/skyShortcuts";
import { TakeoffDialog } from "./TakeoffDialog";
import { TourButton } from "../onboarding/TourButton";
import { QuickAdd } from "./QuickAdd";
import { PlacePicker } from "./PlacePicker";
import { TOUR_REVEAL } from "../onboarding/CoachTour";
import { revealCamera } from "../../lib/skyReveal";
import { cardCamera, phoneRouteZoom, scrollToShow } from "../../lib/skyCamera";
import { useAutoTour, useTourOnFirstSight } from "../onboarding/useAutoTour";
import { behaviorsOnARoute } from "../../lib/behaviors";
import { SkyEmpty } from "../onboarding/SkyEmpty";
import "./Sky.css";
import "./SkyObjects.css";

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
/** The minimap open (default) or folded to its glyph, per device. */
const MINIMAP_KEY = "mf_sky_minimap";
/** The Finished strip shown ("1"); hidden unless turned on, per device. */
const FINISHED_KEY = "mf_sky_finished";
/** The toolbar's two tools, in words — named for what a drag does, with the
 * key that picks each (V / H, SkyView's keydown). */
export const TOOL_CHIPS: Record<SkyTool, { label: string; title: string }> = {
  drag: { label: "Select (V)", title: "Select (V): a drag on empty sky selects, a route moves by its bar — hold Space to pan" },
  click: { label: "Pan (H)", title: "Pan (H): a drag anywhere pans, a click focuses a route or selects a stop — Shift+drag selects" },
};
/** What on the sky takes a press itself, whatever the tool: a control, a
 * field, an open card, a line's editor, a menu, the selection's bar, the
 * new-session block. */
const SKY_CONTROLS =
  "button, a, input, textarea, select, summary, label, [contenteditable=true], [role=menu], [role=menuitem], [data-no-drag], .skl-card, .skl-panels, .sky-selbar, .sky-own-scroll, .sky-new, .sky-palette-slot";

/** Round 15's controls: a block's inline question, the right-click menu. */
const OBJECT_CONTROLS = SKY_CONTROLS + ", .sky-block-ask, .sky-ground-menu";

/** What a press landed on (lib/skySelect `gestureFor`'s target). */
export function skyTargetOf(el: Element | null, ground: (el: Element) => boolean): SkyTarget {
  if (!el || ground(el)) return "ground";
  if (el.closest(OBJECT_CONTROLS)) return "control";
  if (el.closest("[data-node]")) return "box";
  if (el.closest(".skl-bar, .sky-lane-head, .sky-lane-rail, [data-sky-head]")) return "line";
  return "box";
}

/** The sky object an element is on: its line's slot id, "intake", a round
 * 15 object's key (`data-object`), or "". */
export function skyObjectOf(el: Element | null): string {
  if (!el) return "";
  if (el.closest(".sky-intake")) return INTAKE_KEY;
  const obj = el.closest(".sky-object[data-object]");
  if (obj) return obj.getAttribute("data-object") || "";
  return el.closest(".sky-line-slot[data-slot]")?.getAttribute("data-slot") || "";
}

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

/** The intake lane's height at FULL detail, whatever the zoom draws now
 * (lib/skyLayout `FULL_DETAIL_LODS`): its slot copied — the drawn lane as it
 * is, same width, same sources, items and folds — set to each full level of
 * detail beside the real one, hidden, measured and removed in the same task
 * (never painted, never observed). The lane's level of detail is CSS alone
 * (IntakeLane.css), so the copy is exact however its blocks wrap. 0 when
 * there is nothing to measure (no slot, no layout — a test's DOM). */
export function laneFullHeight(slot: HTMLElement | null): number {
  const parent = slot?.parentElement;
  if (!slot || !parent || !slot.querySelector(":scope > .sky-lane")) return 0;
  let tallest = 0;
  for (const { lod, z } of FULL_DETAIL_LODS) {
    const ghost = slot.cloneNode(true) as HTMLElement;
    ghost.removeAttribute("data-tour");
    ghost.setAttribute("aria-hidden", "true");
    ghost.setAttribute("data-lod", lod);
    ghost.style.setProperty("--sky-z", String(z));
    ghost.style.visibility = "hidden";
    ghost.style.pointerEvents = "none";
    ghost.style.transition = "none";
    parent.appendChild(ghost);
    tallest = Math.max(tallest, ghost.querySelector<HTMLElement>(":scope > .sky-lane")?.offsetHeight || 0);
    ghost.remove();
  }
  return tallest;
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

/** Round 18: a chain's label above its tiles (plane px, with its gap). */
export const CHAIN_LABEL_H = 34;

/** Round 18: the size a chain is drawn at before it has measured: its tiles
 * left→right at the pitch (the head one more), its label above. */
export function chainSize(d: Diagram, head: boolean): { w: number; h: number } {
  const n = d.nodes.filter((x) => x.row === "main" && x.kind !== "session" && x.kind !== "add" && x.kind !== "parent").length + (head ? 1 : 0);
  return { w: Math.max(LINE_MIN_W, Math.max(1, n) * PITCH_X + 60), h: NODE_H + CHAIN_LABEL_H + 24 };
}

/** A closed session's line (or removed stops) on its way out: the last
 * picture of it, fading. */
/** Where the new-session block sits, and what it is for: `attach` = the
 * grounded flight it makes a session for; `flying` = the guessed title of
 * the session it is making (the block hides while its line shows). */
interface NewBlockAt extends Pos {
  attach?: { id: string; name: string };
  flying?: string;
}

/** The empty lists a query stands for before it has loaded (one object each). */
/** Where a block placed on the board lands beside: the shelf, the floating
 * palette, or the intake lane (a source's default stops). */
type PlaceNear = "shelf" | "palette" | "lane";

const NO_KINDS: StepKindDef[] = [];
const NO_ROWS: Instance[] = [];
const NO_PLANS: FlightPlan[] = [];

/** The camera across a trip to the grid and back: where it was and which
 * view it was fitted for (the same view back: no refit). */
let memo: { cam: SkyCamera; key: string; one: string; auto: boolean } | null = null;

export function SkyView() {
  const instQ = useInstances();
  const flightsQ = useFlights();
  const flights = flightsQ.data;
  // Stable while loading: a new [] each render would remake every chain.
  const kinds = useFlightKinds().data || NO_KINDS;
  // A chain just built on a session draws from the flights list until the
  // row's own `flight` catches up (the instances snapshot lags a tick).
  const rows = useMemo(() => withLiveFlights(instQ.data || NO_ROWS, flights, kinds), [instQ.data, flights, kinds]);
  const plansQ = useFlightPlans();
  const plans = plansQ.data || NO_PLANS;
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
  // A renamed session keeps its line where it was: lib/renameSession moved
  // the stored layout, this moves the copies in hand.
  useEffect(
    () =>
      onSessionRenamed((from, to) => {
        setLayout((l) => renameSkyLine(l, from, to));
        setAutoPos((a) => {
          if (!(from in a)) return a;
          const { [from]: at, ...rest } = a;
          return to in rest ? rest : { ...rest, [to]: at };
        });
      }),
    []
  );
  const [tidying, setTidying] = useState(false);
  const [newOver, setNewOver] = useState(false);
  // A line being dragged by its bar — with every other selected line when it
  // is one of them: where each started, and how far they have come.
  const [drag, setDrag] = useState<{ from: Record<string, Pos>; dx: number; dy: number } | null>(null);
  // Round 17: the alignment guides of the drag under way (lib/skySnap), the
  // focus mode (F), the shortcuts sheet (?), the minimap folded or open, and
  // whether the palette's filter should take the focus when it floats (Tab).
  const [guides, setGuides] = useState<SnapGuide[]>([]);
  const [focusMode, setFocusMode] = useState(false);
  const [shortcutsOpen, setShortcutsOpen] = useState(false);
  const [minimapOpen, setMinimapOpen] = useState(() => readPref(MINIMAP_KEY) !== "0");
  const [finishedOn, setFinishedOnState] = useState(() => readPref(FINISHED_KEY) === "1");
  const setFinishedOn = useCallback((v: boolean) => {
    setFinishedOnState(v);
    writePref(FINISHED_KEY, v ? "1" : "0");
  }, []);
  const [filterFocus, setFilterFocus] = useState(false);
  const [cam, setCamState] = useState<SkyCamera>(() => memo?.cam || { x: 0, y: 0, z: 1 });
  const camRef = useRef(cam);
  camRef.current = cam;
  // The eased camera (round 17): a fit, a focus or a jump glides to its
  // target over CAM_EASE_MS (ease-out, requestAnimationFrame) and lands
  // EXACTLY on it; any direct move of the camera (the wheel, a pan, a pinch,
  // + / −, the arrows) stops the glide where it is.
  const ease = useRef<number>(0);
  const stopEase = useCallback(() => {
    if (!ease.current) return;
    if (typeof cancelAnimationFrame === "function") cancelAnimationFrame(ease.current);
    ease.current = 0;
  }, []);
  const setCam: typeof setCamState = useCallback(
    (next) => {
      stopEase();
      setCamState(next);
    },
    [stopEase]
  );
  // This session's fit follows the route as it grows (round 21) until the
  // owner moves the camera themselves (the wheel, a drag, a pinch, the zoom
  // keys, the minimap); a new switch arms it again.
  const autoFit = useRef(memo ? memo.auto : true);
  const userCam: typeof setCamState = useCallback(
    (next) => {
      autoFit.current = false;
      setCam(next);
    },
    [setCam]
  );
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
  // The blocks: the bottom shelf, or floating on the board (a sky object,
  // layout key "palette"), remembered per device. A phone keeps the shelf.
  const [paletteMode, setPaletteModeState] = useState<PaletteMode>(() => (readPref(PALETTE_MODE_KEY) === "float" ? "float" : "shelf"));
  // Opened by the shelf's "All blocks…": the large frame it was given for
  // this opening (when the owner never sized it), and Esc docks it again.
  const [catalogOpen, setCatalogOpen] = useState<{ w: number; h: number } | null>(null);
  const setPaletteMode = (m: PaletteMode) => {
    setPaletteModeState(m);
    writePref(PALETTE_MODE_KEY, m);
    if (m === "shelf") {
      setCatalogOpen(null);
      setFilterFocus(false);
    }
  };
  const paletteOn = paletteMode === "float" && !narrow;
  // A grounded line dragged by its bar: the session line under the pointer,
  // which takes its stops on release.
  const [mergeOver, setMergeOver] = useState("");
  // Settings → Appearance → Birds in the Sky: the background flock (off by
  // default). SkyFlock always mounts, so the weather shows either way.
  const skyBirds = useUi((s) => s.skyBirds);
  const [drop, setDrop] = useState<{ title: string; plan: FlightPlan } | null>(null);
  // What a shelf chip being dragged carries (read at its dragstart).
  const dragged = useRef<{ plan: FlightPlan } | { kind: string } | null>(null);
  const [flash, setFlash] = useState("");
  // Round 15: the sky objects' data — the Finished strip's (a minute's
  // cache on the server too; fetched only while it is shown), the peer links
  // (people blocks), the other devices (drop targets while a session's chain
  // is dragged) and the sticky notes (server-side, saved a
  // moment after every change) — and what the objects are asking: a line
  // dropped on a block (`dropped`, key → title), the note being edited, the
  // right-click menu on empty sky.
  const landingQ = useLandingData(!narrow && mode !== "one" && finishedOn);
  const linksQ = usePeerLinks();
  const devicesQ = useDevices();
  const notesQ = useSkyNotesQuery();
  const [notes, setNotes] = useState<SkyNote[]>([]);
  const notesRef = useRef(notes);
  notesRef.current = notes;
  const notesSeeded = useRef(false);
  useEffect(() => {
    if (!notesQ.data || notesSeeded.current) return;
    notesSeeded.current = true;
    setNotes(notesQ.data);
  }, [notesQ.data]);
  const noteSave = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const putNotes = useCallback((next: SkyNote[]) => {
    notesRef.current = next;
    setNotes(next);
    clearTimeout(noteSave.current);
    noteSave.current = setTimeout(() => void saveSkyNotes(next).catch((e) => toast("Saving the notes failed: " + errMsg(e))), 400);
  }, []);
  const [editingNote, setEditingNote] = useState("");
  const [dropped, setDropped] = useState<Record<string, string>>({});
  // A session's chain in the hand (alone): the device targets show.
  const [sessionDrag, setSessionDrag] = useState(false);
  const deviceEls = useRef(new Map<string, HTMLElement>());
  const [groundMenu, setGroundMenu] = useState<{ x: number; y: number; at: Pos } | null>(null);
  const [spaceHeld, setSpaceHeld] = useState(false);
  const space = useRef(false);
  // The tool (drag / click), remembered per device; Space held is click mode.
  const [tool, setToolState] = useState<SkyTool>(() => loadSkyTool());
  const setTool = useCallback((t: SkyTool) => {
    setToolState(t);
    saveSkyTool(t);
  }, []);
  const liveTool: SkyTool = spaceHeld ? "click" : tool;
  const [panning, setPanning] = useState(false);
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
    setFocusMode(false);
  }, []);

  // Every session's line (a `pending_create` row too: a session the sky is
  // making shows at once, a loading box under its name), then the grounded
  // flights' — stops with no session yet — newest first. Not in This session
  // or Perched on you; Find matches their name.
  // This session (the default) with nothing focused still shows a route —
  // shown, never selected (round 21, lib/skyLayout `pickThisSession`): the
  // first that needs you (the bell's order), else the latest working one,
  // else the MRU head, else the rail's #1. The route already shown stays
  // while it is still on the sky and either needs you or nothing else does,
  // so a poll never swaps the picture under you for a busier session.
  const mru = useUi((s) => s.mru);
  const railOrder = useUi((s) => s.railOrder);
  const outboxQ = useOutbox();
  const needsYou = useMemo(() => needsYouTitles(rows, outboxQ.data?.groups?.waiting), [rows, outboxQ.data]);
  const lastShown = useRef("");
  const oneFocus = useMemo(() => {
    if (focused || mode !== "one") return focused || "";
    const best = pickThisSession(rows, { needsYou, mru, railOrder });
    const cur = rows.find((r) => r.title === lastShown.current && inSky(r));
    if (!cur || cur.title === best) return best;
    const needs = (r: Instance | undefined) => !!r && (needsYou.includes(r.title) || linePerched(r));
    return needs(cur) || !needs(rows.find((r) => r.title === best)) ? cur.title : best;
  }, [focused, mode, rows, mru, railOrder, needsYou]);
  useEffect(() => {
    if (mode === "one" && oneFocus) lastShown.current = oneFocus;
  }, [mode, oneFocus]);
  const titles = useMemo(() => skyLineTitles(rows, mode, { query, focus: oneFocus }), [rows, mode, query, oneFocus]);
  // The Verify tour opens by itself once: the first time a Behaviors stop
  // shows on a route here (docs/behaviors.md §4).
  const testPlans = useTestPlans().data?.plans;
  useTourOnFirstSight("verify", useMemo(() => behaviorsOnARoute(testPlans, rows, titles), [testPlans, rows, titles]));
  // The Behaviors board (docs/behaviors.md §3): the behaviors with no frame here.
  const behaviors = useBehaviorsBoard({ rows, titles, allowed: !narrow && mode !== "one" });
  // Routes with no session: none in This session; in Needs you the ones
  // running on their own that wait on you (halted, waiting, at a halt point).
  const grounded = useMemo(() => groundedShown(flights, mode, query, groundedName), [flights, mode, query]);
  const groundedIds = useMemo(() => new Set(groundedFlights(flights).map((f) => f.id)), [flights]);
  const lines = useMemo(
    () => [
      ...titles.map((t) => {
        // Its family too: a line draws its workers inside it in every view.
        const diagram = sessionDiagram(rows, t, { flights, kinds, family: true });
        return { id: t, name: windowName(t), flight: null as Flight | null, diagram, ...(measured[t] || estimateLine(chainSize(diagram, true), layout[t])) };
      }),
      ...grounded.map((f) => {
        const id = groundedKey(f.id);
        const diagram = groundedDiagram(f, kinds);
        return { id, name: groundedName(f), flight: f, diagram, ...(measured[id] || estimateLine(chainSize(diagram, false), layout[id])) };
      }),
    ],
    [titles, grounded, rows, flights, kinds, mode, measured, layout]
  );
  // The intake lane: a sky object placed like a line (not in This session).
  // Its room is what it measured — never under the folded lane's width, so
  // unfolding the rail never runs into the lines beside it.
  const intakeOn = mode !== "one";
  const intakeBox = useMemo(() => {
    const b = intakeSize(layout[INTAKE_KEY], measured[INTAKE_KEY]);
    return { w: Math.max(b.w, INTAKE_W), h: b.h };
  }, [layout, measured]);
  // The floating palette's room: the frame its corner set, else its measure.
  const paletteBox = useMemo(
    () => paletteFrame(layout[PALETTE_KEY]) || (catalogOpen?.w ? catalogOpen : null) || measured[PALETTE_KEY] || { w: PALETTE_W, h: PALETTE_H },
    [layout, measured, catalogOpen]
  );
  // Round 15: every sky object on the board, from the one registry
  // (lib/skyObjects), in placement order; its room is its frame, else its
  // measure, else its kind's guess (a note's is its own numbers).
  const links = useMemo(() => linksQ.data?.links || [], [linksQ.data]);
  const devices = useMemo(() => (devicesQ.data?.devices || []).filter((d) => d.device && d.device !== devicesQ.data?.self?.device), [devicesQ.data]);
  const behaviorsOn = behaviors.on;
  const objData: SkyObjectData = useMemo(
    () => ({ mode, narrow, intakeOn, paletteOn, links, devices, notes, behaviorsOn }),
    [mode, narrow, intakeOn, paletteOn, links, devices, notes, behaviorsOn]
  );
  // Devices are drop targets shown during a session drag (the dock), never
  // board objects; the Finished strip is on the board only when turned on —
  // hidden, it is not placed and reserves no room in the right column.
  const objects = useMemo(() => skyObjects(objData).filter((o) => o.def.kind !== "device" && (o.def.kind !== "landing" || finishedOn)), [objData, finishedOn]);
  const objectById = useMemo(() => new Map(objects.map((o) => [o.key, o])), [objects]);
  const objectBox = useCallback(
    (o: SkyObjectItem): { w: number; h: number } => (o.def.kind === "note" ? noteSize(o.data as SkyNote) : o.def.size(layout[o.key], measured[o.key])),
    [layout, measured]
  );
  // The selection holds only lines still shown (a closed session drops out).
  const shownIds = useMemo(() => new Set([...lines.map((l) => l.id), ...(intakeOn ? [INTAKE_KEY] : [])]), [lines, intakeOn]);
  const sel = useMemo(() => pruneSelection(selRaw, shownIds), [selRaw, shownIds]);
  /** A line still on the board, shown or filtered out: its session is open
   * (not a hidden helper), or its grounded flight is still grounded. */
  const lineAlive = useCallback(
    (id: string) =>
      objectById.has(id) ? true : id === INTAKE_KEY ? intakeOn : id === PALETTE_KEY ? paletteOn : isGroundedKey(id) ? groundedIds.has(groundedIdOf(id)) : rows.some((r) => r.title === id && inSky(r)),
    [rows, groundedIds, intakeOn, paletteOn, objectById]
  );
  // Every line's spot: a pinned or already-given one stays put; a newcomer
  // gets the first free place clear of the placed lines (and of the lines a
  // filter hides, at their last measured size). `fresh` is what the effect
  // below keeps once the newcomer has been measured. The intake lane is the
  // first of them: unpinned, it takes the plane's corner and the lines flow
  // to its right and under it. The floating palette comes next.
  const { place, fresh } = useMemo(() => {
    if (mode === "one") {
      const one: Record<string, Pos> = Object.fromEntries(lines.map((l) => [l.id, { x: SKY_MARGIN, y: SKY_MARGIN }]));
      // The palette under the one line, unless it was put somewhere.
      if (paletteOn) one[PALETTE_KEY] = pinnedPositions(layout)[PALETTE_KEY] || { x: SKY_MARGIN, y: SKY_MARGIN + (lines[0]?.h || 0) + SKY_GAP };
      return { place: one, fresh: {} as Record<string, Pos> };
    }
    const saved: Record<string, Pos> = { ...autoPos, ...pinnedPositions(layout) };
    const items = [{ id: INTAKE_KEY, ...intakeBox }, ...(paletteOn ? [{ id: PALETTE_KEY, ...paletteBox }] : []), ...lines];
    // Round 15: the objects placed right of every line (the strip, the
    // devices, the people) — with a spot, they are in the way of the lines.
    const rightItems = objects.filter((o) => o.def.place === "right").map((o) => ({ id: o.key, ...objectBox(o) }));
    const shown = new Set([...items, ...rightItems].map((l) => l.id));
    const existing: PlacedLine[] = [...items, ...rightItems].filter((l) => saved[l.id]).map((l) => ({ id: l.id, ...saved[l.id], w: l.w, h: l.h }));
    for (const [id, at] of Object.entries(saved))
      if (!shown.has(id) && measured[id] && lineAlive(id)) existing.push({ id, ...at, ...measured[id] });
    // The wrap width that fills the window best (a landscape screen gets wider
    // rows than a portrait one; a few lines are placed as at the minimum).
    const newcomers = items.filter((l) => !saved[l.id]);
    const area = { width: Math.max(vp.w, LINES_LEFT + 900), left: SKY_MARGIN, top: SKY_MARGIN };
    area.width = fillWidth(existing, newcomers, area, vp.w, vp.h, { flat: [INTAKE_KEY, PALETTE_KEY], right: rightItems });
    const fresh = assignSpots(existing, newcomers, area);
    const place: Record<string, Pos> = { ...fresh };
    for (const e of existing) if (shown.has(e.id)) place[e.id] = { x: e.x, y: e.y };
    // The right column: a newcomer goes just right of everything placed,
    // under the ones before it; its spot is kept like a line's.
    // A line that grew into an auto-placed one (the landing strip under a
    // chain's new stops) moves the column right of everything; one the owner
    // dragged stays put.
    const column: PlacedLine[] = rightItems.filter((r) => autoPos[r.id] && !layout[r.id]?.pinned && place[r.id]).map((r) => ({ id: r.id, ...place[r.id], w: r.w, h: r.h }));
    const lineRects: Rect[] = items.filter((l) => place[l.id]).map((l) => ({ ...place[l.id], w: l.w, h: l.h }));
    for (const [id, at] of Object.entries(clearRightColumn(column, lineRects))) {
      fresh[id] = at;
      place[id] = at;
    }
    const placedRects: Rect[] = [...items, ...rightItems].filter((l) => place[l.id]).map((l) => ({ ...place[l.id], w: l.w, h: l.h }));
    for (const [id, at] of Object.entries(rightColumn(placedRects, rightItems.filter((r) => !saved[r.id])))) {
      fresh[id] = at;
      place[id] = at;
    }
    // A note sits where it was put: free at its own numbers, attached
    // relative to its line's spot (so it moves with the line).
    for (const o of objects) if (o.def.kind === "note") place[o.key] = notePos(o.data as SkyNote, (id) => place[id] || null);
    return { place, fresh };
  }, [lines, intakeBox, paletteBox, paletteOn, layout, autoPos, measured, lineAlive, vp.w, vp.h, mode, objects, objectBox]);
  // Keep a newcomer's spot once it is known where it fits: the window
  // measured, the sessions and the flights loaded, the line drawn and
  // measured. Free the spots of sessions that are gone and of lines the
  // owner pinned (their spot is the pin now). The intake lane keeps its spot
  // first — no line is kept before it has one, or 0.9 s have passed — so the
  // lines are placed round the lane at its real size.
  const flightsSettled = flightsQ.isFetched || flightsQ.data !== undefined;
  const [waited, setWaited] = useState(false);
  const intakeReady = !!autoPos[INTAKE_KEY] || !!layout[INTAKE_KEY]?.pinned || !!measured[INTAKE_KEY] || waited;
  useEffect(() => {
    if (mode === "one" || !vp.w || instQ.data === undefined) return;
    setAutoPos((cur) => {
      let next = cur;
      const edit = () => (next === cur ? (next = { ...cur }) : next);
      for (const id of Object.keys(cur))
        if (layout[id]?.pinned || !lineAlive(id)) delete edit()[id];
      if (flightsSettled && intakeReady) for (const [id, at] of Object.entries(fresh)) if (measured[id] || id === INTAKE_KEY || id === PALETTE_KEY) edit()[id] = at;
      return next;
    });
  }, [fresh, measured, layout, lineAlive, mode, vp.w, instQ.data, flightsSettled, intakeReady]);
  const posOf = useCallback((id: string): Pos => {
    const f = drag?.from[id];
    return f ? { x: f.x + drag!.dx, y: f.y + drag!.dy } : place[id] || (id === INTAKE_KEY || id === PALETTE_KEY ? { x: SKY_MARGIN, y: SKY_MARGIN } : { x: LINES_LEFT, y: SKY_MARGIN });
  }, [drag, place]);
  const rectOf = useCallback((id: string): Rect | null => {
    if (id === INTAKE_KEY) return intakeOn ? { ...posOf(id), ...intakeBox } : null;
    if (id === PALETTE_KEY) return paletteOn ? { ...posOf(id), ...paletteBox } : null;
    const o = objectById.get(id);
    if (o) return { ...posOf(id), ...objectBox(o) };
    const l = lines.find((x) => x.id === id);
    return l ? { ...posOf(id), w: l.w, h: l.h } : null;
  }, [lines, posOf, intakeOn, intakeBox, paletteOn, paletteBox, objectById, objectBox]);
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

  // The coach tour asks for a target it can't see (onboarding/CoachTour
  // `TOUR_REVEAL`): the camera glides to it — a scroll can't move the plane.
  useEffect(() => {
    const el = vpRef.current;
    if (!el) return;
    const onReveal = (e: Event) => {
      const t = e.target instanceof Element ? e.target : null;
      if (!t || !el.contains(t) || t === el) return;
      e.preventDefault();
      const v = el.getBoundingClientRect();
      const r = t.getBoundingClientRect();
      goToRef.current(revealCamera(camRef.current, { x: r.left - v.left, y: r.top - v.top, w: r.width, h: r.height }, v.width, v.height));
    };
    el.addEventListener(TOUR_REVEAL, onReveal);
    return () => el.removeEventListener(TOUR_REVEAL, onReveal);
  }, []);

  /** The camera glides to `target` (round 17): ~220 ms ease-out on
   * requestAnimationFrame, the last frame exactly the target. Instant under
   * reduced motion, `instant`, with no frame scheduler, or when it is already
   * there. The glide moves the plane only — no FLIP ghosts. */
  const goTo = useCallback(
    (target: SkyCamera, instant = false) => {
      stopEase();
      const from = camRef.current;
      if (instant || motionOff || sameCamera(from, target) || typeof requestAnimationFrame !== "function" || typeof performance === "undefined") {
        setCamState(target);
        return;
      }
      const t0 = performance.now();
      const step = (now: number) => {
        const t = (now - t0) / CAM_EASE_MS;
        if (t >= 1) {
          ease.current = 0;
          setCamState(target);
          return;
        }
        setCamState(lerpCamera(from, target, easeOut(t)));
        ease.current = requestAnimationFrame(step);
      };
      ease.current = requestAnimationFrame(step);
    },
    [motionOff, stopEase]
  );  const goToRef = useRef(goTo);
  goToRef.current = goTo;

  useEffect(() => stopEase, [stopEase]);
  /** What a fit of everything holds: every line, the intake lane's width
   * and top (its height is its own scroll's), the right column's width and
   * top, and the sticky notes whole (round 21: a fit never slices a note). */
  const everyRect = useCallback((): Rect[] => {
    const rects: Rect[] = lines.map((l) => ({ ...posOf(l.id), w: l.w, h: l.h }));
    if (intakeOn) rects.push({ ...posOf(INTAKE_KEY), w: intakeBox.w, h: 1 });
    for (const o of objects) if (o.def.place === "right") rects.push({ ...posOf(o.key), w: objectBox(o).w, h: 1 });
    for (const o of objects) if (o.def.kind === "note") rects.push({ ...posOf(o.key), ...objectBox(o) });
    return rects;
  }, [lines, posOf, intakeOn, intakeBox, objects, objectBox]);
  /** Fit all (round 21b): ONE fit for `0`, Shift+1, the zoom menu's Fit all,
   * the toolbar's Fit and the fit after a Tidy — everything on screen, notes
   * included, however small (`fitCamera`: no legibility floor). Eased,
   * unless `instant`. */
  const fitAll = useCallback((instant = false) => {
    goTo(fitCamera(boundsOf(everyRect()), vp.w, vp.h, mode === "one" ? 1.5 : 1), instant);
  }, [everyRect, mode, vp.w, vp.h, goTo]);
  /** The first picture (a first visit, a new view) — legibly (`firstFit`:
   * the width at no less than mid, top-left, when everything would need far).
   * Only this automatic fit keeps the floor; Fit all never does. */
  const firstPicture = useCallback(() => {
    goTo(firstFit(boundsOf(everyRect()), vp.w, vp.h, mode === "one" ? 1.5 : 1), true);
  }, [everyRect, mode, vp.w, vp.h, goTo]);
  /** The line This session shows (focused or picked), else "". */
  const oneLine = mode === "one" ? titles[0] || "" : "";
  /** The grid's "1": the focused line filling the window. This session's
   * route (focused or only shown) gets the route fit (`fitRoute`: 48 px a
   * side, 60–150 %), and the re-fit as it grows is armed again. */
  const fitFocused = useCallback((instant = false) => {
    if (oneLine) {
      const r = rectOf(oneLine);
      if (!r) return;
      autoFit.current = true;
      goTo(fitRoute(r, vp.w, vp.h), instant);
      return;
    }
    const r = focusLine ? rectOf(focusLine) : null;
    if (!r) return fitAll(instant);
    goTo(fitCamera(r, vp.w, vp.h, 1.5), instant);
  }, [oneLine, focusLine, rectOf, fitAll, vp.w, vp.h, goTo]);

  // Fit ONCE: on the first show of the sky, once the lines have their spots
  // (drawn and measured, or after a short wait), and again only when the view
  // changes (All / Mine / Perched / This session). Never because a line grew,
  // a session came or went, or the window resized: the owner pans, or presses
  // 0, 1 or Fit. The first fit follows the grid's view: "1" fits the focused
  // line, the rest fit all.
  const fitted = useRef(memo?.key || "");
  const fitKey = mode + "\u0000" + (mode === "one" ? oneFocus : "");
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
    // The first picture is not a glide: there is nothing to glide from.
    // This session fits by its own effect below (it follows the route).
    if (first && gridOne && mode !== "one") fitFocused(true);
    else if (mode !== "one") firstPicture();
  }, [vp.w, vp.h, instQ.data, fitKey, settled]); // eslint-disable-line react-hooks/exhaustive-deps
  // This session (round 21): every switch and the first load fit the route
  // to the window (`fitRoute`), and fit it AGAIN whenever its box changes —
  // the flights arriving after the first paint, a stop added, the box opened
  // — until the owner moves the camera (`userCam`); a new route re-arms it.
  // The first picture is instant, a switch or a growing route glides.
  const oneFitted = useRef(memo ? memo.one : "");
  const oneRect = oneLine ? rectOf(oneLine) : null;
  const oneKey = oneRect ? [oneRect.x, oneRect.y, oneRect.w, oneRect.h].map(Math.round).join(",") : "";
  useEffect(() => {
    if (mode !== "one") {
      oneFitted.current = "";
      return;
    }
    if (!oneLine || !oneRect || !vp.w || !vp.h || instQ.data === undefined) return;
    const first = !oneFitted.current;
    if (oneFitted.current !== oneLine) autoFit.current = true;
    oneFitted.current = oneLine;
    if (!autoFit.current) return;
    goTo(fitRoute(oneRect, vp.w, vp.h), first);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- keyed on the box
  }, [mode, oneLine, oneKey, vp.w, vp.h, instQ.data === undefined]);
  useEffect(
    () => () => {
      memo = { cam: camRef.current, key: fitted.current, one: oneFitted.current, auto: autoFit.current };
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
    // Nothing focused: the next line selected is a change again.
    if (!focusLine && lastFocus.current) lastFocus.current = "";
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
    // Switching session never opens its box on the terminal (the owner,
    // 2026-11-03: "when we switch session in sky I don't want the text window
    // to pop up"): a box stays as it was left; a click on its agent opens it.
    const c = camRef.current;
    const sx = c.x + r.x * c.z;
    const sy = c.y + r.y * c.z;
    const inView = sx >= 0 && sy >= 0 && sx + r.w * c.z <= vp.w && sy + Math.min(r.h, 240) * c.z <= vp.h;
    if (!inView && !pressed && mode !== "one") {
      goTo(centerOn(r, vp.w, vp.h, Math.max(c.z, 0.85)));
    }
    setFlash(focusLine);
    setTimeout(() => alive.current && setFlash((f) => (f === focusLine ? "" : f)), 1400);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- read once per pick
  }, [focusLine, rectOf, vp.w, vp.h]);

  /** Enter opened a line's box on its terminal (round 21): once the card is
   * drawn, the camera glides to it, centred, at no less than 100 % (round
   * 21b: at the route's own fit its tabs were 8 px) — closer only if it was
   * closer already. The route fit stands down for it: the owner asked to
   * see the box. The camera it left is kept: Esc closing the box goes back
   * to it (This session re-fits the route instead). */
  const beforeEnter = useRef<{ id: string; cam: SkyCamera } | null>(null);
  const revealOpened = (id: string) => {
    autoFit.current = false;
    beforeEnter.current = { id, cam: camRef.current };
    let tries = 0;
    const look = () => {
      if (!alive.current) return;
      const root = vpRef.current;
      let slot: Element | null = null;
      root?.querySelectorAll(".sky-line-slot[data-slot]").forEach((el) => {
        if (el.getAttribute("data-slot") === id) slot = el;
      });
      const card = (slot as Element | null)?.querySelector(".skl-card");
      if (!root || !slot || !card) {
        if (++tries < 12) setTimeout(look, 50);
        return;
      }
      const v = root.getBoundingClientRect();
      const c = card.getBoundingClientRect();
      if (!c.width || !c.height) return;
      goToRef.current(cardCamera(camRef.current, { x: c.left - v.left, y: c.top - v.top, w: c.width, h: c.height }, v.width, v.height));
    };
    setTimeout(look, 30);
  };
  /** Esc closed the box Enter opened: back to the view before it — This
   * session's route fit, else the camera Enter left. */
  const restoreAfterEnter = (id: string) => {
    const was = beforeEnter.current;
    beforeEnter.current = null;
    if (!was || was.id !== id) return;
    // This session: the route fit re-arms — the box closing changes the
    // route's size, and the fit effect glides to the closed route.
    if (mode === "one") autoFit.current = true;
    else goTo(was.cam);
  };

  // A line that leaves is gone at once (the owner: deletes must disappear
  // right away); round 18 dropped the fade, which drew the old row format.

  /** Select a line's session — the same selection a pane click makes. A
   * press on the line itself is remembered, so the selection effect above
   * leaves the camera alone for it. */
  const pressedLine = useRef("");
  const focusOn = useCallback((id: string, pressed = false) => {
    pressedLine.current = pressed ? id : "";
    setGroundFocus("");
    selectSession(id, { noKeyboard: true });
  }, []);
  /** Nothing focused (a click on empty sky, Esc): no line, no session — in
   * the grid too. The sky never picks a line by itself. */
  const unfocus = useCallback(() => {
    setGroundFocus("");
    if (useUi.getState().focused) selectSession(null);
  }, []);
  /** A line that just appeared because of something done elsewhere (an
   * intake item flown): flashed, never focused. */
  const flashLine = useCallback((id: string) => {
    setFlash(id);
    setTimeout(() => alive.current && setFlash((f) => (f === id ? "" : f)), 1400);
  }, []);
  // An openBehaviors request (docs/behaviors.md §4, BehaviorsBoard
  // `useBehaviorsRequest`): the camera glides to the session's frame (its
  // tile opens the card) or to the Behaviors board (that row open).
  const flyToBehaviors = (id: string) => {
    if (narrow) return true;
    const r = rectOf(id);
    if (!r || !vp.w) return false;
    goTo(centerOn(r, vp.w, vp.h, Math.max(camRef.current.z, 0.85)));
    flashLine(id);
    return true;
  };
  useBehaviorsRequest(behaviors, { ready: narrow || (!!vp.w && !!vp.h && instQ.data !== undefined && settled), fly: flyToBehaviors });

  // A view switch never picks a line: This session with nothing focused
  // SHOWS one (`pickThisSession`), never selects it.
  const setMode = (m: SkyMode) => setSkyMode(m);

  const zoomBy = (k: number) => {
    userCam((c) => zoomAt(c, c.z * k, vp.w / 2, vp.h / 2));
  };
  const panBy = (dx: number, dy: number) => {
    userCam((c) => ({ ...c, x: c.x + dx, y: c.y + dy }));
  };
  /** The zoom menu's presets and Shift+0: to `z` about the window's middle, eased. */
  const zoomTo = (z: number) => {
    autoFit.current = false;
    goTo(zoomAt(camRef.current, z, vp.w / 2, vp.h / 2));
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
    setLayout((l) => (patch.pinned === false ? closeSkyWindows(tidySkyLayout(l, id), [id]) : patchSkyLine(l, id, patch)));
  }, []);
  /** Every line back to its organized spot: the pins and the given spots
   * are forgotten, every line is placed again, and they glide there — the
   * only glide on the sky (`data-tidying` turns the slots' transition on for
   * a moment), and every open session window is minimised back to its box.
   * The rows are as wide as fills the window best (`fillWidth`), and once
   * every line has its new spot the camera eases to the whole fit, so a Tidy
   * ends with everything on screen (the effect below `tidyFit`). Tidy these
   * and the lane making way leave the camera where it is. */
  const tidyTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const tidyFit = useRef(false);
  const tidyFitTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const tidy = () => {
    setAutoPos({});
    setLayout((l) => closeSkyWindows(tidySkyLayout(l)));
    // Arm the fit (the effect below); disarmed after 2 s regardless, so a
    // line that never gets its spot (never measured) can't jump the camera
    // long after the Tidy.
    tidyFit.current = true;
    clearTimeout(tidyFitTimer.current);
    tidyFitTimer.current = setTimeout(() => (tidyFit.current = false), 2000);
    if (motionOff) return;
    setTidying(true);
    clearTimeout(tidyTimer.current);
    tidyTimer.current = setTimeout(() => alive.current && setTidying(false), TIDY_MS);
  };
  // After a Tidy, once every shown line (and the lane) has its new spot —
  // given by the refill above or pinned — ease to the whole fit, once.
  useEffect(() => {
    if (!tidyFit.current) return;
    const spotted = (id: string) => mode === "one" || !!autoPos[id] || !!layout[id]?.pinned;
    if (!lines.every((l) => spotted(l.id)) || (intakeOn && !spotted(INTAKE_KEY))) return;
    tidyFit.current = false;
    fitAll();
  }, [place, autoPos, layout, lines, intakeOn, mode, fitAll]);
  /** Tidy these (the selection bar): only their spots are forgotten, only
   * their windows minimised. */
  const tidyThese = (ids: string[]) => {
    setAutoPos((cur) => Object.fromEntries(Object.entries(cur).filter(([id]) => !ids.includes(id))));
    setLayout((l) => closeSkyWindows(ids.reduce((acc, id) => tidySkyLayout(acc, id), l), ids));
    clearSel();
    if (motionOff) return;
    setTidying(true);
    clearTimeout(tidyTimer.current);
    tidyTimer.current = setTimeout(() => alive.current && setTidying(false), TIDY_MS);
  };
  // The intake lane's slot (`.sky-intake`): what its full-detail measure is
  // read from (`laneFullHeight`).
  const intakeEl = useRef<HTMLDivElement | null>(null);
  const onMeasure = useCallback((id: string, w: number, h: number) => {
    // The lane's room is its FULL-detail height, not what this zoom draws
    // (lib/skyLayout `FULL_DETAIL_LODS`): lines placed beside a compact Fit
    // lane must not end up under it at 100 %.
    if (id === INTAKE_KEY) h = Math.max(h, laneFullHeight(intakeEl.current));
    setMeasured((m) => {
      const o = m[id];
      if (o && Math.abs(o.w - w) < 2 && Math.abs(o.h - h) < 2) return m;
      return { ...m, [id]: { w: Math.round(w), h: Math.round(h) } };
    });
  }, []);
  // What the lane holds can change with no change to what this zoom draws
  // (items arriving at far, where they are hidden): watched, it is measured
  // again at full detail (once a frame at most).
  const intakeWatch = useRef<(() => void) | null>(null);
  const intakeSlot = useCallback(
    (el: HTMLDivElement | null) => {
      intakeWatch.current?.();
      intakeWatch.current = null;
      intakeEl.current = el;
      if (!el || typeof MutationObserver === "undefined") return;
      let frame = 0;
      const mo = new MutationObserver(() => {
        if (frame) return;
        frame = requestAnimationFrame(() => {
          frame = 0;
          const lane = el.querySelector<HTMLElement>(":scope > .sky-lane");
          if (lane?.offsetWidth && lane.offsetHeight) onMeasure(INTAKE_KEY, lane.offsetWidth, lane.offsetHeight);
        });
      });
      mo.observe(el, { childList: true, subtree: true, characterData: true, attributes: true, attributeFilter: ["open", "data-open", "data-collapsed"] });
      intakeWatch.current = () => {
        mo.disconnect();
        cancelAnimationFrame(frame);
      };
    },
    [onMeasure]
  );
  // The lane GREW where it sits (a source or items arrived, ▸ / ▾ expanded
  // it): the auto-placed lines it now covers make way — their spots are
  // freed, so `assignSpots` places them again clear of it (gliding, as a
  // Tidy). A line the owner pinned stays where it was put; a lane dragged
  // onto lines shoves nothing (lib/skyLayout `grownOver`).
  const laneWas = useRef<Rect | null>(null);
  useEffect(() => {
    const at = place[INTAKE_KEY];
    const now = mode !== "one" && intakeOn && !drag && at ? { ...at, ...intakeBox } : null;
    const was = laneWas.current;
    laneWas.current = now;
    const movable: PlacedLine[] = [];
    for (const id of Object.keys(autoPos)) {
      const r = id !== INTAKE_KEY && !layout[id]?.pinned ? rectOf(id) : null;
      if (r) movable.push({ id, ...r });
    }
    const out = grownOver(was, now, movable);
    if (!out.length) return;
    setAutoPos((cur) => Object.fromEntries(Object.entries(cur).filter(([id]) => !out.includes(id))));
    if (motionOff) return;
    setTidying(true);
    clearTimeout(tidyTimer.current);
    tidyTimer.current = setTimeout(() => alive.current && setTidying(false), TIDY_MS);
  }, [place, intakeBox, intakeOn, mode, drag, autoPos, layout, rectOf, motionOff]);

  // --- Dragging a sky object by its bar ------------------------------------------------

  // A sky object with a layout entry — a line by its bar, the intake lane by
  // its head. A selected one drags every selected one with it (each pinned
  // where it lands); a click on a bar outside the selection clears it,
  // Shift+click takes the object in or out. In click mode nothing moves (the
  // press never gets here: the sky pans it in its capture phase).
  const onDragStart = (id: string, e: ReactPointerEvent) => {
    if (e.button !== 0 || gestureFor(liveTool, "line", { space: space.current }) !== "move-line") return;
    e.stopPropagation();
    const ids = sel.lines.has(id) ? [...sel.lines] : [id];
    const from: Record<string, Pos> = Object.fromEntries(ids.map((x) => [x, posOf(x)]));
    const g = { pid: e.pointerId, sx: e.clientX, sy: e.clientY, moved: false, shift: e.shiftKey, dx: 0, dy: 0 };
    // Round 17: the pressed box snaps its edges and centres to every box that
    // stays put (lib/skySnap, 8 screen px) — the others in the selection
    // follow by the same snapped delta. Alt held: the raw delta.
    const pressedRect = rectOf(id);
    const still = pressedRect
      ? [...lines.map((l) => l.id), ...objects.map((o) => o.key), INTAKE_KEY, PALETTE_KEY]
          .filter((x, i, arr) => !ids.includes(x) && arr.indexOf(x) === i)
          .map((x) => ({ id: x, rect: rectOf(x) }))
          .filter((x): x is { id: string; rect: Rect } => !!x.rect)
          .map((x) => ({ id: x.id, ...x.rect }))
      : [];
    const delta = (ev: PointerEvent) => {
      const raw = { dx: (ev.clientX - g.sx) / camRef.current.z, dy: (ev.clientY - g.sy) / camRef.current.z };
      if (!pressedRect || ev.altKey || !still.length) return { ...raw, guides: [] as SnapGuide[] };
      const r = snapBox({ id, x: from[id].x + raw.dx, y: from[id].y + raw.dy, w: pressedRect.w, h: pressedRect.h }, still, snapThreshold(camRef.current.z));
      return { dx: r.x - from[id].x, dy: r.y - from[id].y, guides: r.guides };
    };
    // A grounded line dragged alone may land on a session's line: the line
    // under the pointer (the others sit still while it moves).
    const merging = isGroundedKey(id) && ids.length === 1;
    const others = merging ? lines.filter((l) => l.id !== id).map((l) => ({ id: l.id, rect: { ...posOf(l.id), w: l.w, h: l.h } })) : [];
    // Round 15: a session's line dragged alone may land on a person block or
    // a device target (it then asks); a note dragged alone on any line (it
    // attaches). The object under the pointer lights as the target. The
    // device targets are the dock's (screen px, shown once the drag starts).
    const kind = objectKind(id);
    const toBlock = !kind && !merging && !isGroundedKey(id) && ids.length === 1;
    const toLine = kind === "note" && ids.length === 1;
    const blocks = toBlock ? objects.filter((o) => o.def.accepts === "session").map((o) => ({ id: o.key, rect: rectOf(o.key)! })) : [];
    const lineRects = toLine ? lines.map((l) => ({ id: l.id, rect: { ...posOf(l.id), w: l.w, h: l.h } })) : [];
    const toDevice = toBlock && devices.length > 0 && mode !== "one";
    const deviceAt = (ev: PointerEvent) => {
      if (!toDevice) return "";
      for (const [key, el] of deviceEls.current) {
        const r = el.getBoundingClientRect();
        if (ev.clientX >= r.left && ev.clientX <= r.right && ev.clientY >= r.top && ev.clientY <= r.bottom) return key;
      }
      return "";
    };
    const mergeAt = (ev: PointerEvent) =>
      merging ? mergeTargetAt(planeAt(ev.clientX, ev.clientY), others, id) : toBlock ? deviceAt(ev) || hitAt(planeAt(ev.clientX, ev.clientY), blocks) : toLine ? hitAt(planeAt(ev.clientX, ev.clientY), lineRects) : "";
    const move = (ev: PointerEvent) => {
      if (ev.pointerId !== g.pid) return;
      if (!g.moved && Math.hypot(ev.clientX - g.sx, ev.clientY - g.sy) < 4) return;
      if (!g.moved && toDevice) setSessionDrag(true);
      g.moved = true;
      const d = delta(ev);
      g.dx = d.dx;
      g.dy = d.dy;
      setDrag({ from, dx: d.dx, dy: d.dy });
      setGuides(d.guides);
      if (merging || toBlock || toLine) {
        const t = mergeAt(ev);
        setMergeOver((cur) => (cur === t ? cur : t));
      }
    };
    const up = (ev: PointerEvent) => {
      if (ev.pointerId !== g.pid) return;
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
      window.removeEventListener("pointercancel", up);
      setDrag(null);
      setGuides([]);
      setMergeOver("");
      setSessionDrag(false);
      const landed = g.moved && ev.type !== "pointercancel" ? mergeAt(ev) : "";
      const into = merging ? landed : "";
      if (into) {
        void mergeGrounded(id, into, from[id]);
        return;
      }
      if (landed && toBlock) {
        // The line goes back where it was; the block asks what to do.
        setDropped((d) => ({ ...d, [landed]: id }));
        return;
      }
      if (toLine && g.moved) {
        // Where the last move put it (snapped, unless Alt was held).
        const { dx, dy } = g;
        dropNote(noteIdOf(id), { x: from[id].x + dx, y: from[id].y + dy }, landed);
        return;
      }
      if (g.moved) {
        const { dx, dy } = g;
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
      if (id === INTAKE_KEY || id === PALETTE_KEY) return;
      if (isObjectKey(id)) return;
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
    return fl && isShown(fl) ? flights?.find((f) => f.id === fl.id) || null : null;
  };
  /** A saved stop dropped on `title`'s line. A line with a flight in the air
   * takes it as one more stop (one group) at the end; one without starts
   * flying it. Never the focused line by itself: a palette click places
   * (`placeGrounded`). */
  const onFly = (plan: FlightPlan, title: string) => {
    const live = liveFlightOf(title);
    if (live) void tackOnPlan(live.id, plan.id);
    else void startFlightOn(title, plan.id, NEW_ROUTE_STARTS_ON);
  };
  /** A grounded flight takes off on `title` (its ⋯ Fly on a session…). */
  const onAttach = (f: Flight, title: string) => {
    setAttachPick(null);
    void attachGrounded(f.id, { title });
  };
  /** One stop of `kind` dropped on `title`'s line. A line with no flight yet
   * starts an ad-hoc one (the fast-track's rungs become its stops), as the
   * line's own + does. */
  const onTack = (kind: string, title: string) => {
    const def = kinds.find((k) => k.kind === kind);
    if (!def) return;
    const full = liveFlightOf(title);
    if (full) void tackOn(full.id, newStep(def, full.steps.map((st) => st.id)));
    else void tackOnNew(title, [newStep(def, [])]);
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
  /** Stops with no session yet: a grounded flight of a saved stop or of one
   * stop of a kind. */
  const makeGrounded = async (d: { plan: FlightPlan } | { kind: string }): Promise<Flight | null> => {
    if ("plan" in d) return startGrounded({ plan_id: d.plan.id });
    const def = kinds.find((k) => k.kind === d.kind);
    return def ? startGrounded({ steps: [defaultStepFor(def)] }) : null;
  };
  /** A chip on empty sky: stops with no session yet, pinned where it fell —
   * unfocused, like everything the board is given. */
  const dropGrounded = async (d: { plan: FlightPlan } | { kind: string }, at: Pos) => {
    const f = await makeGrounded(d);
    if (!f || !alive.current) return;
    setLayout((l) => patchSkyLine(l, groundedKey(f.id), { x: at.x, y: at.y, pinned: true }));
    groundedLanded(f);
  };
  /** A route with no session just made or asked for: the view that hides
   * such routes (This session, Needs you) moves to All when it has nothing
   * else to show, else says where it went with a Show all button (lib/skyLayout
   * `groundedLanding`) — a right-click or a drop never vanishes silently. */
  const groundedLanded = (f: Flight) => {
    const how = groundedLanding(useUi.getState().skyMode, instQ.data?.length === 0);
    if (how === "switch") setSkyMode("all");
    else if (how === "say")
      toast("“" + groundedName(f) + "” is on the Sky — All or Mine shows it", { action: { label: "Show all", onClick: () => setSkyMode("all") } });
  };
  /** Where a palette click puts its block (lib/skyLayout `spotNear`): the free
   * spot nearest just above the shelf's middle, or right of the floating
   * palette, inside the view, clear of every line, the lane and the palette.
   * Read at the moment it lands (after the flight is made), so a ref. */
  const spotRef = useRef<(f: Flight, near: PlaceNear) => Pos>(() => ({ x: 0, y: 0 }));
  spotRef.current = (f, near) => {
    const key = groundedKey(f.id);
    const size = measured[key] || chainSize(groundedDiagram(f, kinds), false);
    const c = camRef.current;
    const pad = 16 / c.z;
    const view: Rect = { x: -c.x / c.z + pad, y: -c.y / c.z + pad, w: vp.w / c.z - 2 * pad, h: vp.h / c.z - 2 * pad };
    const taken = [...lines.map((l) => l.id).filter((id) => id !== key), INTAKE_KEY, PALETTE_KEY].map(rectOf).concat(objects.map((o) => rectOf(o.key))).filter((r): r is Rect => !!r);
    // Right of the intake lane (a source's default stops, from its stops
    // chip) or of the floating palette, else just above the shelf.
    const pal = near === "palette" ? rectOf(PALETTE_KEY) : near === "lane" ? rectOf(INTAKE_KEY) : null;
    const want = pal ? { x: pal.x + pal.w + PLACE_GAP, y: pal.y } : { x: view.x + view.w / 2 - size.w / 2, y: view.y + view.h - size.h };
    return spotNear(want, taken, size, view);
  };
  // A placed line lands at its estimated size; once drawn and measured it is
  // placed once more at its real size (as `assignSpots` keeps a spot only once
  // a line is measured), so it never spills out of the view or onto a line.
  const placing = useRef(new Map<string, PlaceNear>());
  useEffect(() => {
    for (const [key, near] of placing.current) {
      if (!measured[key]) continue;
      placing.current.delete(key);
      const f = flights?.find((x) => groundedKey(x.id) === key);
      if (!f) continue;
      const at = spotRef.current(f, near);
      setLayout((l) => patchSkyLine(l, key, { x: at.x, y: at.y, pinned: true }));
    }
  }, [measured, flights]);
  /** A palette click (a chip, the catalog's Add): the block goes ON THE
   * BOARD — a grounded line near the palette, pinned there, nothing
   * selected, nothing focused (docs/flights.md "Round 13": place, don't
   * tack). Only a drop on a line, or its own +, adds to a line. */
  const placeGrounded = async (d: CatalogDrop, near: "shelf" | "palette") => {
    const plan = "planId" in d ? plans.find((p) => p.id === d.planId) : null;
    if ("planId" in d && !plan) return;
    const f = await makeGrounded(plan ? { plan } : { kind: (d as { kind: string }).kind });
    if (!f || !alive.current) return;
    const at = spotRef.current(f, near);
    setLayout((l) => patchSkyLine(l, groundedKey(f.id), { x: at.x, y: at.y, pinned: true }));
    placing.current.set(groundedKey(f.id), near);
    // This session and Needs you draw no grounded line: say where it went.
    groundedLanded(f);
  };
  /** A saved route's "Edit its stops": an editable copy of its stops on the
   * board (placed as a shelf click places, its name the route's), marked so
   * its Save writes back to that saved route (GroundedLine `markRouteEdit`). */
  const editPlan = async (plan: FlightPlan) => {
    // Its stops one by one (no name on the start: that would fold them into
    // one group box), then the route's name.
    const f = await startGrounded({ steps: plan.steps.map((st) => ({ ...st, group: undefined })) });
    if (!f) return;
    markRouteEdit(f.id, plan.id);
    void renameFlight(f.id, plan.name);
    if (!alive.current) return;
    const at = spotRef.current(f, "shelf");
    setLayout((l) => patchSkyLine(l, groundedKey(f.id), { x: at.x, y: at.y, pinned: true }));
    placing.current.set(groundedKey(f.id), "shelf");
    setGroundFocus(groundedKey(f.id));
    groundedLanded(f);
    toast("Editing “" + plan.name + "” — change its stops, then Save on its bar");
  };
  /** The drawer's press on a route with no session: the camera flies to it
   * (switching to All first when this view doesn't draw it). */
  const pendingFly = useRef("");
  const focusFlight = (id: string) => {
    const key = groundedKey(id);
    if (rectOf(key)) return jumpTo(key);
    pendingFly.current = key;
    setSkyMode("all");
  };
  useEffect(() => {
    const key = pendingFly.current;
    if (!key || !rectOf(key)) return;
    pendingFly.current = "";
    jumpTo(key);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- once the line is drawn
  }, [rectOf]);
  /** The intake lane's stops chip: that source's default stops as an editor
   * chain on the board (IntakeLane `openDefaultEditor`) — placed right of the
   * lane and focused; the one already there (one per source) is jumped to
   * instead of making a second. */
  const editDefault = (ref: IntakeSourceRef) =>
    void openDefaultEditor(ref, flights, {
      existing: (f) => {
        const key = groundedKey(f.id);
        if (rectOf(key)) jumpTo(key);
        else groundedLanded(f);
      },
      made: (f) => {
        if (!alive.current) return;
        const key = groundedKey(f.id);
        const at = spotRef.current(f, "lane");
        setLayout((l) => patchSkyLine(l, key, { x: at.x, y: at.y, pinned: true }));
        placing.current.set(key, "lane");
        setGroundFocus(key);
        groundedLanded(f);
      },
    });
  /** Pin a line at `at` — through the layout store even when the sky has
   * gone (an Undo pressed from the grid). */
  const pinAt = (key: string, at: Pos) => {
    if (alive.current) setLayout((l) => patchSkyLine(l, key, { x: at.x, y: at.y, pinned: true }));
    else patchSkyLine(loadSkyLayout(), key, { x: at.x, y: at.y, pinned: true });
  };
  /** A grounded line made again from its stops and name, pinned where the
   * old one sat (every Undo of a grounded line's removal or merge). */
  const reground = async (steps: FlightStep[], name: string, was: Pos | undefined, editsDefault?: IntakeSourceRef) => {
    // An editor of a source's default comes back an editor (never runnable).
    const g = await startGrounded({ steps, ...(name ? { name } : {}), ...(editsDefault ? { edits_default: editsDefault } : {}) });
    if (g && was) pinAt(groundedKey(g.id), was);
    return g;
  };
  /** Remove a grounded line (its ⋯ Remove, Delete): at once — it never runs —
   * with Undo in the toast and on Ctrl+Z (one entry, lib/skyUndo). */
  const removeGrounded = async (key: string) => {
    const f = flights?.find((x) => x.id === groundedIdOf(key));
    if (!f) return;
    const was = posOf(key);
    // Gone from the board at once; a refusal puts it back (lib/flightOptimism).
    if (!(await optimisticDelete(f.id, () => deleteFlight(f.id)))) return;
    const steps = f.steps;
    const name = f.plan_name || "";
    const shown = groundedName(f);
    const ed = f.edits_default;
    // Undo makes the line again (a new flight id); Redo removes THAT one.
    let cur = f.id;
    const entry = pushUndo({
      label: "Remove “" + shown + "”",
      undo: async () => {
        const g = await reground(steps, name, was, ed);
        if (g) cur = g.id;
        return g;
      },
      redo: () => deleteFlight(cur),
    });
    toast("Removed “" + shown + "”", { action: undoAction(entry, "Undo: put “" + shown + "” back on the Sky") });
  };
  /** A grounded line dropped on a session's line: its stops go onto that
   * session — `attachGrounded` when it has no flight, else tacked on as one
   * group named after the grounded line, and the grounded flight goes. Undo
   * makes the grounded line again from the same stops, where it was. */
  const mergeGrounded = async (key: string, title: string, was: Pos | undefined, at?: number) => {
    const f = flights?.find((x) => x.id === groundedIdOf(key));
    if (!f) return;
    const steps = f.steps;
    // Joined on as ONE named group only when it was really named (by you, or
    // a saved stop) and holds more than one stop; otherwise the stops land as
    // themselves (a lone Commit must stay "Commit", not a "Stops" box).
    const given = (f.plan_name || "").trim();
    const name = steps.length > 1 && given && given !== "Stops" && !/ \(from .+\)$/.test(given) ? given : "";
    // Round 18: onto another stops-only chain too (a wire from its tile).
    const into = isGroundedKey(title) ? flights?.find((x) => x.id === groundedIdOf(title)) || null : null;
    if (isGroundedKey(title) && !into) return;
    const live = into || liveFlightOf(title);
    // Their spots were their own chain's: they are placed afresh here.
    const bare = steps.map(({ pos: _pos, ...st }) => st as FlightStep);
    let ok = false;
    // Round 19: wired / dropped onto a session it waits there, ready.
    if (!live) ok = !!(await attachGrounded(f.id, { title, start_on: NEW_ROUTE_STARTS_ON }));
    else if (await tackOnSteps(live.id, bare, at !== undefined && at < live.steps.length ? at : undefined, name || undefined)) ok = await deleteFlight(f.id);
    if (!ok) return;
    const n = steps.length;
    const msg = "Moved " + n + (n === 1 ? " stop" : " stops") + " onto " + (into ? groundedName(into) : windowName(title));
    // The grounded line back where it was (the stops stay on the session too:
    // Undo makes the line again, it does not take them off); Redo takes the
    // remade line off the board again.
    let cur = "";
    const entry = pushUndo({
      label: msg,
      undo: async () => {
        const g = await reground(steps, name, was);
        cur = g ? g.id : "";
        return g;
      },
      redo: async () => (cur ? deleteFlight(cur) : false),
    });
    toast(msg, { action: undoAction(entry, "Undo: put “" + (name || "the stops") + "” back on the Sky") });
  };
  const onShelfDragEnd = () => {
    dragged.current = null;
    setDrop(null);
    setSessionOver("");
    setNewOver(false);
  };

  // --- Sticky notes ------------------------------------------------------------------

  /** A note's text or frame changed: saved a moment later. */
  const changeNote = useCallback(
    (id: string, patch: Partial<SkyNote>) => putNotes(notesRef.current.map((n) => (n.id === id ? { ...n, ...patch, updated: Date.now() / 1000 } : n))),
    [putNotes]
  );
  /** × or Delete on a note: gone, with an Undo toast that puts it back. */
  const removeNote = useCallback(
    (id: string) => {
      const n = notesRef.current.find((x) => x.id === id);
      if (!n) return;
      putNotes(notesRef.current.filter((x) => x.id !== id));
      setEditingNote((e) => (e === id ? "" : e));
      vpRef.current?.focus({ preventScroll: true });
      // One entry for the toast's Undo and Ctrl+Z; Ctrl+Shift+Z removes it again.
      const entry = pushUndo({
        label: "Remove the note",
        undo: async () => putNotes([...notesRef.current.filter((x) => x.id !== id), n]),
        redo: async () => putNotes(notesRef.current.filter((x) => x.id !== id)),
      });
      toast("Note removed", { action: undoAction(entry, "Undo: put the note back") });
    },
    [putNotes]
  );
  /** A new note at `at` (plane px): empty, in edit at once. */
  const addNote = (at: Pos) => {
    const n: SkyNote = { id: newNoteId(), text: "", x: Math.round(at.x), y: Math.round(at.y), w: NOTE_W, h: NOTE_H, line: "", updated: Date.now() / 1000 };
    putNotes([...notesRef.current, n]);
    setEditingNote(n.id);
  };
  /** A note dropped at `at`: on a line it attaches (its offset from the
   * line's spot — it moves with the line), on empty sky it sits free there. */
  const dropNote = (id: string, at: Pos, line: string) => {
    const lineAt = line ? place[line] || null : null;
    putNotes(notesRef.current.map((n) => (n.id === id ? noteDropped(n, { x: Math.round(at.x), y: Math.round(at.y) }, line, lineAt) : n)));
  };
  /** The right-click menu on empty sky: Add a note at the pointer. */
  const onGroundMenu = (e: ReactMouseEvent<HTMLDivElement>) => {
    if (!ground(e.target)) return;
    e.preventDefault();
    const r = vpRef.current!.getBoundingClientRect();
    setGroundMenu({ x: e.clientX - r.left, y: e.clientY - r.top, at: planeAt(e.clientX, e.clientY) });
  };
  useEffect(() => {
    if (!groundMenu) return;
    const close = (e: PointerEvent) => {
      if (!(e.target instanceof Element && e.target.closest(".sky-ground-menu"))) setGroundMenu(null);
    };
    window.addEventListener("pointerdown", close, true);
    return () => window.removeEventListener("pointerdown", close, true);
  }, [groundMenu]);

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
    stopEase();
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
  // Click mode (or Space held): the sky takes every press but a control's
  // in its capture phase, so no line, bar or box starts a move of its own. A
  // press that moves pans (the pointer is captured only then); one that never
  // moves is a click on what it pressed (`clickOn`): its own click still
  // lands on it, so a box is selected and a double-click opens it, as ever.
  // Shift+drag draws the marquee.
  const press = useRef<{ id: number; sx: number; sy: number; el: Element | null } | null>(null);
  // --- Round 18: wires between ports (docs/flights.md "Round 18" → "Wiring") ---
  // Every chain hands the host its graph (`registerChain`); a port pressed
  // starts the host's wire drag (useSkyWiring), which can reach any chain;
  // the drop's `wireIntent` runs here (`runIntent`).
  const chainReg = useChainRegistry();
  const placedChains = (): Map<string, PlacedChain> => {
    const m = new Map<string, PlacedChain>();
    const shown = new Set(lines.map((l) => l.id));
    for (const [key, e] of chainReg.entries.current) if (shown.has(key)) m.set(key, { ...e, key, origin: posOf(key) });
    return m;
  };
  /** The flight a chain is: a stops-only one's, or a session's live one. */
  const flightOfKey = (key: string): Flight | null => (isGroundedKey(key) ? flights?.find((f) => f.id === groundedIdOf(key)) || null : liveFlightOf(key));
  const runIntent = (intent: WireIntent, ctx: { src: WireEnd; at: Pos }) =>
    runWireIntent(intent, ctx, {
      flightOf: flightOfKey,
      flights: flights || [],
      kinds,
      chain: (key) => placedChains().get(key) || null,
      merge: (from, into, at) => mergeGrounded(from, into, posOf(from), at),
      pinAt: (key, at) => alive.current && pinAt(key, at),
      labelH: CHAIN_LABEL_H,
    });
  const wiring = useSkyWiring({
    chains: () => placedChains(),
    planeAt: (x, y) => planeAt(x, y),
    ground: (el) => ground(el),
    onIntent: (i, ctx) => void runIntent(i, ctx),
    // Round 20: unsafe kinds never light as parallel targets.
    kinds: () => kinds,
  });
  /** A chain's port pressed: the wire drag, in drag mode or click mode alike. */
  const portDownFor = (key: string) => (node: Parameters<typeof wiring.startWire>[1], side: "in" | "out", e: ReactPointerEvent<HTMLElement>) => wiring.startWire(key, node, side, e);
  // The handoff wires between chains (dashed, the source session's colour):
  // redrawn when a chain changes or moves.
  const handoffs = useMemo(() => handoffLinks(flights), [flights]);
  const xwires = useMemo(
    () => handoffWires(handoffs, placedChains().values()),
    // eslint-disable-next-line react-hooks/exhaustive-deps -- the registry is a ref; its version moves with it
    [handoffs, chainReg.version, place, drag, lines]
  );
  // A stops-only chain another chain hands off to starts with a trigger tile.
  const triggerHeads = useMemo(() => new Map(grounded.map((f) => [f.id, triggerHeadFor(f, flights, windowName)])), [grounded, flights]);

  const onDownCapture = (e: ReactPointerEvent<HTMLDivElement>) => {
    // Round 17: any press on the sky lifts the focus mode.
    if (focusMode) setFocusMode(false);
    if (e.pointerType === "mouse" && e.button !== 0) return;
    const el = e.target instanceof Element ? e.target : null;
    // A loop being drawn or re-aimed is the line's own (FlywayCanvas).
    if (el?.closest(".fl-loop-handle, .fl-loop-pill[role='button']")) return;
    // Round 18: a chain's port (or its +) is a wire, whatever the tool.
    if (el?.closest(".sky-chain [data-port]")) return;
    const t = space.current ? "click" : tool;
    if (t === "drag") return;
    const g = gestureFor(t, skyTargetOf(e.target as Element, ground), { shift: e.shiftKey, space: space.current, touch: e.pointerType === "touch" });
    if (g === "none") return;
    e.stopPropagation();
    e.preventDefault();
    if (g === "marquee") {
      vpRef.current?.focus({ preventScroll: true });
      startMarquee(e);
      return;
    }
    pointers.current.set(e.pointerId, { x: e.clientX, y: e.clientY });
    if (pointers.current.size >= 2) {
      press.current = null;
      return startPan(e);
    }
    press.current = { id: e.pointerId, sx: e.clientX, sy: e.clientY, el: e.target instanceof Element ? e.target : null };
  };
  /** A click-mode press that never moved: focus the line it was on, select
   * the box (its own click, which still lands on it, does the selecting —
   * the box gets the keyboard here so Delete reaches it), clear the
   * selection on empty sky. Shift+click is the marquee's (`endMarquee`). */
  const clickOn = (el: Element | null) => {
    const id = skyObjectOf(el);
    if (!sel.lines.has(id)) clearSel();
    if (!id || id === INTAKE_KEY || isObjectKey(id)) {
      // Empty sky: nothing selected, nothing focused.
      if (!id) {
        vpRef.current?.focus({ preventScroll: true });
        unfocus();
      }
      return;
    }
    if (isGroundedKey(id)) setGroundFocus(id);
    else focusOn(id, true);
    const box = el?.closest("[data-node]");
    const line = el?.closest(".sky-line-slot")?.querySelector(".skl");
    ((box || line) as HTMLElement | null | undefined)?.focus({ preventScroll: true });
  };
  // The marquee: a left drag (mouse or pen) on empty sky. A touch drag still
  // pans, as the middle button does.
  const marquee = useRef<{ id: number; sx: number; sy: number; add: boolean; moved: boolean; el: Element | null } | null>(null);
  const onDown = (e: ReactPointerEvent<HTMLDivElement>) => {
    if (!ground(e.target) || (e.pointerType === "mouse" && e.button !== 0 && e.button !== 1)) return;
    e.preventDefault();
    // The keyboard comes to the sky (arrows, + / −, T), out of a terminal.
    vpRef.current?.focus({ preventScroll: true });
    const g = e.button === 0 ? gestureFor(liveTool, "ground", { shift: e.shiftKey, touch: e.pointerType === "touch" }) : "pan";
    if (g === "marquee") startMarquee(e);
    else startPan(e);
  };
  const startMarquee = (e: ReactPointerEvent<HTMLDivElement>) => {
    try {
      e.currentTarget.setPointerCapture(e.pointerId);
    } catch {
      /* a synthetic event has no capture */
    }
    marquee.current = { id: e.pointerId, sx: e.clientX, sy: e.clientY, add: e.shiftKey, moved: false, el: e.target instanceof Element ? e.target : null };
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
      // The tiles themselves (round 18: `.sky-node`), not their ports.
      slot?.querySelectorAll(":is(.sky-node, .fl-box)[data-node]").forEach((el) => {
        const node = el.getAttribute("data-node") || "";
        if (isStopNode(node) && !el.closest(".skl-card")) out.stops.push({ line: l.id, node, rect: onPlane(el) });
      });
    }
    // The intake lane is taken as a line (its head is its bar): it moves and
    // tidies with the selection, and Remove leaves it be.
    const lane = rectOf(INTAKE_KEY);
    if (lane) {
      const heads: Rect[] = [];
      root.querySelectorAll(".sky-intake :is(.sky-lane-head, .sky-lane-rail)").forEach((el) => heads.push(onPlane(el)));
      out.lines.push({ id: INTAKE_KEY, rect: lane, heads });
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
      // A click on empty sky clears the selection; Shift+click on a line (click
      // mode's marquee starts anywhere) takes it in or out.
      const id = skyObjectOf(m.el);
      if (m.add && id) setSel((s) => toggleLine(s, id));
      else if (!m.add) {
        clearSel();
        unfocus();
      }
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
    const pr = press.current;
    if (pr && pr.id === e.pointerId) {
      // A click-mode press becomes a pan once it moves.
      if (Math.hypot(e.clientX - pr.sx, e.clientY - pr.sy) < MARQUEE_MIN) return;
      press.current = null;
      try {
        vpRef.current?.setPointerCapture(e.pointerId);
      } catch {
        /* a synthetic event has no capture */
      }
      pan.current = { id: e.pointerId, x: pr.sx, y: pr.sy, cx: camRef.current.x, cy: camRef.current.y };
      setPanning(true);
    }
    if (!pointers.current.has(e.pointerId)) return;
    pointers.current.set(e.pointerId, { x: e.clientX, y: e.clientY });
    const pn = pinch.current;
    if (pn && pointers.current.size >= 2) {
      const [a, b] = [...pointers.current.values()];
      const r = vpRef.current?.getBoundingClientRect();
      const d = Math.hypot(a.x - b.x, a.y - b.y) || 1;
      userCam(zoomAt(camRef.current, (pn.z * d) / pn.d, (a.x + b.x) / 2 - (r?.left || 0), (a.y + b.y) / 2 - (r?.top || 0)));
      return;
    }
    const pa = pan.current;
    if (pa && pa.id === e.pointerId) userCam({ ...camRef.current, x: pa.cx + (e.clientX - pa.x), y: pa.cy + (e.clientY - pa.y) });
  };
  const onUp = (e: ReactPointerEvent<HTMLDivElement>) => {
    if (marquee.current?.id === e.pointerId) return endMarquee(e);
    const pr = press.current;
    if (pr && pr.id === e.pointerId) {
      press.current = null;
      if (e.type !== "pointercancel") clickOn(pr.el);
    }
    pointers.current.delete(e.pointerId);
    if (pointers.current.size < 2) pinch.current = null;
    if (pan.current?.id === e.pointerId) {
      pan.current = null;
      setPanning(false);
    }
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
  /** Round 17: the palette floats at a plane point with its filter focused
   * (Tab) — placing then lands beside it. */
  const floatPaletteAt = (at: Pos) => {
    setFilterFocus(true);
    onLayout(PALETTE_KEY, { x: Math.round(at.x), y: Math.round(at.y), pinned: true });
    setPaletteMode("float");
  };
  /** Where Tab floats the palette: where it was last pinned, else the view's
   * left third, a little down from the toolbar. */
  const paletteSpot = (): Pos => {
    const stored = layout[PALETTE_KEY];
    if (stored?.pinned && typeof stored.x === "number" && typeof stored.y === "number") return { x: stored.x, y: stored.y };
    const c = toPlane(camRef.current, Math.max(24, vp.w / 2 - PALETTE_W * camRef.current.z / 2), 48);
    return { x: Math.round(c.x), y: Math.round(c.y) };
  };
  // A double-click on empty sky opens the new-session block at the pointer
  // — what the guide and the shelf's Session chip say it does (Tab floats
  // the palette). A double-click on a line keeps its meaning — it never gets
  // here.
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
    // A pan under way keeps panning: a wheel event within PAN_GAP_MS of the
    // last one the sky took is the same gesture, even if the pointer is now
    // over an open session card (the owner: passing over one mustn't slow the
    // sky down). The card (or any own-scroll panel) takes the wheel only when
    // a NEW gesture starts with the pointer resting on it.
    const PAN_GAP_MS = 600;
    let lastPan = 0;
    const onWheel = (e: WheelEvent) => {
      const now = e.timeStamp || performance.now();
      const inOwn = !!(e.target as Element | null)?.closest?.(".sky-own-scroll");
      if (inOwn && now - lastPan > PAN_GAP_MS) return;
      // Captured: the card's terminal never sees a wheel that pans the sky.
      e.preventDefault();
      e.stopPropagation();
      lastPan = now;
      const unit = e.deltaMode === 1 ? 16 : e.deltaMode === 2 ? el.clientHeight : 1;
      const dx = e.deltaX * unit;
      const dy = e.deltaY * unit;
      if (e.ctrlKey || e.metaKey) {
        const r = el.getBoundingClientRect();
        userCam((c) => zoomAt(c, c.z * Math.exp(-dy * 0.0022), e.clientX - r.left, e.clientY - r.top));
      } else if (e.shiftKey && !dx) userCam((c) => ({ ...c, x: c.x - dy }));
      else userCam((c) => ({ ...c, x: c.x - dx, y: c.y - dy }));
    };
    el.addEventListener("wheel", onWheel, { passive: false, capture: true });
    return () => el.removeEventListener("wheel", onWheel, { capture: true });
  }, [narrow]);

  // What is selected, as one plane rectangle: the selected stops' boxes
  // (relative to their line, so they follow it), else the selected lines'
  // and objects' bounds. The selection bar sits by it; Shift+2 fits it.
  const selOne = stopSelection(sel);
  const selBox = (() => {
    if (selOne) {
      const at = posOf(selOne.line);
      return selStops ? { ...selStops, x: selStops.x + at.x, y: selStops.y + at.y } : rectOf(selOne.line);
    }
    return boundsOf([...sel.lines].map(rectOf).filter((r): r is Rect => !!r));
  })();
  /** Shift+2 / the zoom menu's Fit selection: the selected boxes fill the
   * window (eased). Nothing selected: the camera stays. */
  const fitSel = () => {
    const target = selBox ? fitSelection([selBox], vp.w, vp.h, selOne ? 1.5 : 1.25) : null;
    if (target) goTo(target);
  };
  /** The minimap's and the hints' jump: the line centred (eased) and
   * focused as a press on it would — no box opens, the camera is ours. */
  const jumpTo = (id: string) => {
    const r = rectOf(id);
    if (!r) return;
    goTo(centerOn(r, vp.w, vp.h, Math.max(camRef.current.z, 0.85)));
    if (isGroundedKey(id)) setGroundFocus(id);
    else if (lines.some((l) => l.id === id)) focusOn(id, true);
  };
  // Round 17: every line and object on the plane for the minimap and the
  // off-screen hints — its rect, what it is, its state word (a session's
  // state, a grounded flight's), its name, whether it is selected or focused.
  const mapRects: MinimapRect[] = [
    ...lines.map((l): MinimapRect => {
      const r = rectOf(l.id)!;
      const row = l.flight ? undefined : rows.find((x) => x.title === l.id);
      return {
        id: l.id,
        ...r,
        kind: l.flight ? "grounded" : "line",
        tone: l.flight ? l.flight.state : row ? deriveSessionState(row).state : "",
        label: l.name,
        selected: sel.lines.has(l.id),
        focused: l.flight ? groundFocus === l.id : focusLine === l.id,
        // Round 21: a route that needs you — its edge chip says so when a
        // fit clips it, or it lies off screen.
        // Not in This session: the one route shown is the one you are on.
        needsYou: mode === "one" ? false : l.flight ? l.flight.state === "waiting_you" || l.flight.state === "halted" : !!row && (needsYou.includes(l.id) || linePerched(row)),
      };
    }),
    ...objects.flatMap((o): MinimapRect[] => {
      const r = rectOf(o.key);
      return r ? [{ id: o.key, ...r, kind: o.def.kind as MinimapRect["kind"], tone: "", label: o.key === INTAKE_KEY ? "Intake" : o.key, selected: sel.lines.has(o.key) }] : [];
    }),
  ];
  // Round 21: the open minimap folds to its glyph while it would sit over a
  // route's box (its pref stays open; it opens again once the route is clear
  // of it). Its glyph pressed then shows it anyway, until the view changes.
  // Its box is measured where it last drew open (viewport px), else guessed.
  // Round 21b: an open drawer drawn OVER the sky (not beside it) hides the
  // viewport's right side; the edge chips keep inside what is visible.
  const [drawerCover, setDrawerCover] = useState(0);
  useLayoutEffect(() => {
    const root = vpRef.current;
    const d = drawerOpen && root ? root.closest("#sky-view")?.querySelector(".sky-drawer.open") : null;
    let cover = 0;
    if (root && d) {
      const v = root.getBoundingClientRect();
      const r = d.getBoundingClientRect();
      if (r.width && r.left < v.right && r.right > v.left && r.top < v.bottom && r.bottom > v.top) cover = Math.round(v.right - Math.max(r.left, v.left));
    }
    setDrawerCover(cover);
  }, [drawerOpen, vp.w, vp.h]);
  const mmHost = useRef<HTMLDivElement>(null);
  const [mmBox, setMmBox] = useState<Rect | null>(null);
  const [mmPeek, setMmPeek] = useState(false);
  useEffect(() => setMmPeek(false), [mode, oneLine]);
  const mmAt: Rect = mmBox || { x: vp.w - 24 - (MINIMAP_W + 2), y: vp.h - 24 - (MINIMAP_H + 30), w: MINIMAP_W + 2, h: MINIMAP_H + 30 };
  const mmCovers = minimapOpen && !mmPeek && screenBoxCovers(mmAt, mapRects.filter((r) => r.kind === "line" || r.kind === "grounded"), cam);
  const mmShown = minimapOpen && !mmCovers;
  useLayoutEffect(() => {
    if (!mmShown) return;
    const el = mmHost.current?.querySelector(".sky-minimap");
    const root = vpRef.current;
    if (!el || !root) return;
    const v = root.getBoundingClientRect();
    const r = el.getBoundingClientRect();
    if (!r.width || !r.height) return;
    const box = { x: r.left - v.left, y: r.top - v.top, w: r.width, h: r.height };
    setMmBox((b) => (b && Math.abs(b.x - box.x) < 1 && Math.abs(b.y - box.y) < 1 && Math.abs(b.w - box.w) < 1 && Math.abs(b.h - box.h) < 1 ? b : box));
  });
  /** Focus mode: what is not selected is dimmed (`data-dim`). */
  const dimmed = (id: string) => focusMode && !sel.lines.has(id) && selOne?.line !== id;

  // The sky's keys. Esc folds an open card first, then closes the focused
  // line's box; the rest only while nothing that takes keys has the focus
  // and no dialog is over the sky.
  const keys = useRef<(e: KeyboardEvent) => void>(() => {});
  keys.current = (e: KeyboardEvent) => {
    const el = document.activeElement;
    // Round 17: the shortcuts sheet — Esc closes it (its own filter too).
    if (e.key === "Escape" && shortcutsOpen) {
      e.preventDefault();
      setShortcutsOpen(false);
      return;
    }
    // Focus mode lifts on Esc before anything else Esc does.
    if (e.key === "Escape" && focusMode && !useUi.getState().openDialog) {
      e.preventDefault();
      setFocusMode(false);
      return;
    }
    // Tab inside the floating palette's filter (where the first Tab put the
    // keyboard): the second Tab docks it.
    if (e.key === "Tab" && !e.shiftKey && !e.ctrlKey && !e.metaKey && !e.altKey && paletteOn && typing(el) && el?.closest(".sky-palette-slot")) {
      e.preventDefault();
      setPaletteMode("shelf");
      vpRef.current?.focus({ preventScroll: true });
      return;
    }
    // The sky's own cards are dialogs too (role="dialog"): Esc folds them
    // unless a real dialog is over the whole sky.
    if (e.key === "Escape" && (groundMenu || Object.keys(dropped).length) && !useUi.getState().openDialog) {
      e.preventDefault();
      setGroundMenu(null);
      setDropped({});
      return;
    }
    if (e.key === "Escape" && ((newAt && !newAt.flying) || attachPick) && !useUi.getState().openDialog) {
      e.preventDefault();
      if (!newAt?.flying) setNewAt(null);
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
    // Esc inside the palette All blocks… opened (not in its filter): dock it.
    if (e.key === "Escape" && catalogOpen && paletteOn && !typing(el) && el?.closest(".sky-palette-slot")) {
      e.preventDefault();
      setPaletteMode("shelf");
      return;
    }
    if (!skyKeysOwn(el) || e.ctrlKey || e.metaKey || e.altKey) return;
    const k = e.key;
    const handled = () => e.preventDefault();
    // Round 17: Shift+1 fits all, Shift+2 the selection, Shift+0 is 100 %
    // (by the key's position, so a layout's ! / @ / ) count too); Tab floats
    // the palette with its filter focused, a second Tab docks it; F with a
    // selection is focus mode; ? the shortcuts sheet.
    const digit = (n: string) => e.code === "Digit" + n || e.code === "Numpad" + n;
    if (k === " " && !e.repeat) {
      handled();
      space.current = true;
      setSpaceHeld(true);
    } else if (e.shiftKey && (digit("1") || k === "!")) {
      handled();
      fitAll();
    } else if (e.shiftKey && (digit("2") || k === "@")) {
      handled();
      fitSel();
    } else if (e.shiftKey && (digit("0") || k === ")")) {
      handled();
      zoomTo(1);
    } else if (k === "Tab" && !e.shiftKey && !narrow) {
      handled();
      if (paletteOn) setPaletteMode("shelf");
      else floatPaletteAt(paletteSpot());
    } else if ((k === "f" || k === "F") && !selectionEmpty(sel)) {
      handled();
      setFocusMode(true);
    } else if (k === "?") {
      handled();
      setShortcutsOpen(true);
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
    } else if (k === "v" || k === "V") {
      handled();
      setTool("drag");
    } else if (k === "h" || k === "H") {
      handled();
      setTool("click");
    } else if (k.startsWith("Arrow")) {
      handled();
      const d = PAN_STEP * (e.shiftKey ? 4 : 1);
      panBy(k === "ArrowLeft" ? d : k === "ArrowRight" ? -d : 0, k === "ArrowUp" ? d : k === "ArrowDown" ? -d : 0);
    } else if (k === "Enter" && (focusLine || oneLine)) {
      handled();
      const id = focusLine || oneLine;
      onLayout(id, { open: true });
      revealOpened(id);
    } else if (k === "Escape" && (focusLine || oneLine) && layout[focusLine || oneLine]?.open) {
      handled();
      const id = focusLine || oneLine;
      onLayout(id, { open: false });
      restoreAfterEnter(id);
    } else if (k === "Escape" && catalogOpen && paletteOn) {
      // Opened by "All blocks…", nothing else to close: back to the shelf.
      handled();
      setPaletteMode("shelf");
    } else if (k === "Escape" && (focused || groundFocus) && !marquee.current) {
      // Nothing selected, no dialog, no marquee: Esc leaves nothing focused.
      handled();
      unfocus();
    }
  };
  // Delete / Backspace on the sky (lib/skySelect `deleteFor`): the selection
  // asks its bar to remove; a box or a line asks ITS line (the handle each
  // line registers: a stop through the canvas's Remove, a session's box or
  // line asks to close it, a grounded line asks to be removed). Taken in the
  // window's capture phase, before the app's keymap (document, capture) sees
  // it: there Delete closes the focused SESSION, never what is selected.
  // With nothing of the sky's under the keyboard, the keymap's Delete stands.
  const deleters = useRef(new Map<string, LineDelete>());
  const registers = useRef(new Map<string, (fn: LineDelete | null) => void>());
  /** A line's `registerDelete`, one per line id (stable across renders). */
  const registerFor = (id: string) => {
    let r = registers.current.get(id);
    if (!r) {
      r = (fn) => {
        if (fn) deleters.current.set(id, fn);
        else deleters.current.delete(id);
      };
      registers.current.set(id, r);
    }
    return r;
  };
  /** Where the keyboard is, for Delete: "sky" (its empty space, or nothing
   * focused), a sky object and the box on it ("" = the object itself), or
   * null — a field, an open card's pane, a menu, a question, or outside the
   * sky: none of the sky's business. */
  const deleteFocus = (el: Element | null): "sky" | { line: string; node: string } | null => {
    const root = vpRef.current;
    if (!el || el === document.body || el === root) return "sky";
    if (!root?.contains(el)) return null;
    if (typing(el) || el.closest(".skl-card, .pane, .skl-panels, .sky-selbar, .sky-palette-slot, [role=menu], [role=dialog], [role=alertdialog]")) return null;
    const line = skyObjectOf(el);
    if (!line) return "sky";
    return { line, node: el.closest("[data-node]")?.getAttribute("data-node") || "" };
  };
  /** The box the focused line has selected (its canvas's own selection). */
  const selectedBox = (): { line: string; node: string } | null => {
    const id = groundFocus || focusLine;
    if (!id) return null;
    let slot: Element | null = null;
    vpRef.current?.querySelectorAll(".sky-line-slot[data-slot]").forEach((el) => {
      if (el.getAttribute("data-slot") === id) slot = el;
    });
    const box = [...((slot as Element | null)?.querySelectorAll(".sky-node.is-selected[data-node], .fl-box.selected[data-node]") || [])].find((b) => !b.closest(".skl-card"));
    return box ? { line: id, node: box.getAttribute("data-node") || "" } : null;
  };
  const removeKeys = useRef<(e: KeyboardEvent) => void>(() => {});
  removeKeys.current = (e: KeyboardEvent) => {
    if (redoKey(e)) return undoNow(e, true);
    if (undoKey(e)) return undoNow(e);
    if ((e.key !== "Delete" && e.key !== "Backspace") || e.ctrlKey || e.metaKey || e.altKey) return;
    if (overlayOpen()) return;
    const el = document.activeElement;
    // A focused note (not its textarea): the note goes, with Undo.
    const noteEl = el?.closest(".sky-note[data-note]");
    if (noteEl && !typing(el)) {
      e.preventDefault();
      e.stopPropagation();
      removeNote(noteEl.getAttribute("data-note") || "");
      return;
    }
    const at = deleteFocus(el);
    if (!at) return;
    // The selection's, unless the keyboard is on a box (that box goes).
    const d = deleteFor(skyKeysOwn(el) ? sel : NO_SELECTION, at === "sky" ? null : at, at === "sky" ? selectedBox() : null);
    if (d.kind === "keymap") return;
    e.preventDefault();
    e.stopPropagation();
    if (d.kind === "selection") setSelAsk("remove");
    else if (d.kind === "box" || d.kind === "line") deleters.current.get(d.line)?.(d.kind === "box" ? d.node : "");
  };
  /** Ctrl+Z / ⌘Z (no Shift, no Alt): undo. Ctrl+Y stays the view switch. */
  const undoKey = (e: KeyboardEvent) => (e.ctrlKey || e.metaKey) && !e.altKey && !e.shiftKey && (e.key === "z" || e.key === "Z");
  /** Ctrl+Shift+Z / ⌘⇧Z: redo (round 17). */
  const redoKey = (e: KeyboardEvent) => (e.ctrlKey || e.metaKey) && !e.altKey && e.shiftKey && (e.key === "z" || e.key === "Z");
  /** Ctrl+Z on the sky: the newest removal back (lib/skyUndo); `redo`:
   * Ctrl+Shift+Z, the newest undone removal again ("Redid: …"). Not in a
   * field (its own undo), an open card's pane (the terminal's), a menu or a
   * dialog, nor while a drag, a pan or a marquee runs. */
  const undoNow = (e: KeyboardEvent, redo = false) => {
    const el = document.activeElement;
    if (typing(el) || overlayOpen()) return;
    if (el && el !== document.body && el.closest(".pane, .skl-card, [role=menu], [role=dialog], [role=alertdialog]")) return;
    e.preventDefault();
    e.stopPropagation();
    if (drag || marquee.current || pan.current) return;
    if (redo) {
      const again = popRedo();
      if (!again?.redo) {
        toast("Nothing to redo");
        return;
      }
      toast("Redid: " + again.label);
      void again.redo();
      return;
    }
    const entry = popUndo();
    if (!entry) {
      // The sky takes Ctrl+Z from the app's keymap (where it reopens the last
      // closed session): say where that road went.
      toast("Nothing to undo", { title: "Ctrl+Shift+T reopens the last closed session" });
      return;
    }
    toast("Undid: " + entry.label);
    void entry.undo();
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

  // This session with no session at all says what All says (and SkyEmpty's
  // calm card shows instead); with sessions but none it can show, it points
  // at where one is picked (round 21: never a dead end).
  const noSessions = instQ.data !== undefined && instQ.data.length === 0;
  // Routes with no session yet, hidden by This session: the sky is not
  // empty, they are under All (polish pass 5).
  const groundedHidden = mode === "one" && groundedIds.size > 0;
  const empty =
    lines.length > 0
      ? ""
      : groundedHidden && noSessions && !focused && !query
        ? GROUNDED_HIDDEN_LINE
      : mode === "one" && focused
        ? "“" + windowName(focused) + "” isn't in the sky right now."
        : mode === "one" && !noSessions
          ? instQ.data === undefined
            ? ""
            : narrow
              ? "Pick a session in the list to show its route here."
              : "Pick a session on the left to show its route here."
          : query
            ? "No route matches “" + query + "”."
            : mode === "perched"
              ? "Nothing needs you."
              : instQ.data === undefined
                ? ""
                : "No sessions in the sky yet — a route is the stops a session's work goes through, from the agent to the PR. Double-click anywhere, or press the Session chip in the shelf below, to start one.";

  // Nothing at all on an unfiltered sky: SkyEmpty's calm card instead of the line.
  const blank = lines.length === 0 && !query && mode !== "perched" && noSessions && !(mode === "one" && focused) && !groundedHidden;

  const toolbar = (
    <div className="sky-toolbar" role="toolbar" aria-label="The sky">
      <ChipRow<SkyMode>
        className="sky-modes"
        label="Which routes"
        value={mode}
        options={SKY_MODES.map((m) => ({ value: m.id, label: m.label, title: m.title }))}
        onChange={setMode}
      />
      {!narrow && (
        // The tool: Select (a line moves by its bar) · Pan (a drag pans).
        <ChipRow<SkyTool>
          className="sky-modes sky-tools"
          field={{ "data-tour": "sky-tool" }}
          label="Tool"
          value={tool}
          options={SKY_TOOLS.map((t) => ({ value: t, ...TOOL_CHIPS[t] }))}
          onChange={setTool}
        />
      )}
      <span className="sky-search">
        <TextField value={query} onChange={setQuery} label="Find a route" placeholder="Find a route…" onEnter={() => titles[0] && focusOn(titles[0])} />
      </span>
      {!narrow && (
        <button type="button" className="fl-btn sky-tidy" title="Put every route back in its organized spot (T)" onClick={tidy}>
          Tidy
        </button>
      )}
      {/* The weather word (docs/flights.md "As built: round 15 weather"):
          "☁ usage 87 %" / "☾ quiet hours", nothing in clear weather. */}
      {!narrow && <WeatherWord />}
      {!narrow && (
        <span className="sky-zoom" role="group" aria-label="Zoom" data-tour="sky-zoom">
          <button type="button" className="sky-zoom-btn" aria-label="Zoom out" title="Zoom out (−)" disabled={cam.z <= ZOOM_MIN + 0.001} onClick={() => zoomBy(1 / 1.2)}>
            −
          </button>
          <ZoomMenu
            zoom={cam.z}
            min={ZOOM_MIN}
            max={ZOOM_MAX}
            onZoomTo={zoomTo}
            onFitAll={() => fitAll()}
            onFitSelection={fitSel}
            onFitFocused={() => fitFocused()}
            canFitSelection={!selectionEmpty(sel)}
            canFitFocused={!!(focusLine || oneLine)}
          />
          <button type="button" className="sky-zoom-btn" aria-label="Zoom in" title="Zoom in (+)" disabled={cam.z >= ZOOM_MAX - 0.001} onClick={() => zoomBy(1.2)}>
            +
          </button>
          <button type="button" className="sky-zoom-btn sky-fit-one" title="Fit the focused route — the grid's 1 (1)" disabled={!focusLine && !oneLine} onClick={() => fitFocused()}>
            1
          </button>
          <button
            type="button"
            className="sky-zoom-btn sky-fit"
            title="Fit all: every route and note in the window, however small (0, Shift+1)"
            onClick={() => fitAll()}
          >
            Fit
          </button>
          <button type="button" className="sky-zoom-btn sky-zoom-100" title="Back to 100% (Shift+0)" onClick={() => zoomTo(1)}>
            100%
          </button>
        </span>
      )}
      {!narrow && (
        <OverflowMenu
          label="More"
          items={[
            { key: "note", label: "Add a note", title: "A sticky note in the middle of the view", onSelect: () => addNote(middle()) },
            { key: "keys", label: "Keyboard shortcuts…", title: "Every key the sky knows (?)", onSelect: () => setShortcutsOpen(true) },
            {
              key: "finished",
              label: (finishedOn ? "✓ " : "") + "Show the Finished strip",
              title: "Each repo's main branch — its CI and its last merge — and the routes merged today, in a strip right of the routes",
              className: "sky-finished-toggle",
              onSelect: () => setFinishedOn(!finishedOn),
            },
            {
              key: "behaviors",
              label: (behaviors.on ? "✓ " : "") + "Show the Behaviors board",
              title: "Behaviors with no session in the Sky — a closed session, another branch, a watched repo's push — in a board right of the routes",
              className: "sky-behaviors-toggle",
              onSelect: behaviors.toggle,
            },
          ]}
        />
      )}
      <TourButton tour="sky" />
    </div>
  );

  // A grounded flight flies on a session with no flight in the air.
  const pickable = attachPick ? titles.filter((t) => !liveFlightOf(t)) : titles;
  // Where a stops-only chain runs: a popup with a search (PlacePicker) — the
  // default folder, a session without a flight, or another folder.
  const pick = attachPick && (
    <PlacePicker
      name={groundedName(attachPick)}
      sessions={pickable.map((t) => ({ title: t, folder: String(rows.find((r) => r.title === t)?.folder || "") }))}
      onClose={() => setAttachPick(null)}
      onPick={(place) => {
        const f = attachPick;
        setAttachPick(null);
        if (place.kind === "default") onAttach(f, DEFAULT_SESSION);
        else if (place.kind === "session") onAttach(f, place.title);
        else void attachGrounded(f.id, { title: "", folder: place.path }).then((x) => x && toast("Running “" + groundedName(f) + "” in " + (x.session ? "a new session, " + x.session : place.path)));
      }}
    />
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
      onFocusFlight={focusFlight}
    />
  );

  /** "All blocks…": the palette floats over the board, LARGE when the owner
   * never sized it (`catalogFrame`: ≈ 60vw × 70vh at this zoom, right of the
   * view's centre, inside the view) and pinned there — a sky object, not a
   * modal: the board stays around it, so a drag out of it is the obvious
   * move. Its × (or Esc) docks it back at the bottom. */
  const openCatalog = () => {
    const c = camRef.current;
    const view: Rect = { x: -c.x / c.z, y: -c.y / c.z, w: vp.w / c.z, h: vp.h / c.z };
    const screen = { w: typeof window !== "undefined" ? window.innerWidth : vp.w, h: typeof window !== "undefined" ? window.innerHeight : vp.h };
    const stored = paletteFrame(layout[PALETTE_KEY]);
    const f = catalogFrame(view, screen, c.z, stored);
    // {0, 0}: opened by All blocks… at the owner's own frame.
    setCatalogOpen(stored ? { w: 0, h: 0 } : { w: f.w, h: f.h });
    onLayout(PALETTE_KEY, { x: f.x, y: f.y, pinned: true });
    setPaletteMode("float");
  };

  // The Stops shelf: the chips' drags are read here as they start (the sky
  // owns their drops). `display: contents`, so the shelf sits in the stage.
  // A click on a chip PLACES its block above the shelf; floating, the shelf
  // gives way to the palette on the board.
  const shelf = (
    <div className="sky-shelf-host" onDragStart={onShelfDragStart} onDragEnd={onShelfDragEnd}>
      {!paletteOn && (
        <StopShelf
          onPlace={(d) => void placeGrounded(d, "shelf")}
          onFloat={narrow ? undefined : () => setPaletteMode("float")}
          onCatalog={narrow ? undefined : openCatalog}
          onSession={newHere}
          onEditPlan={(plan) => void editPlan(plan)}
          folded={shelfFolded}
          onFold={foldShelf}
        />
      )}
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
        // Its line shows where the block sat; it is not focused for you.
        setNewAt(null);
      }}
    />
  );

  // A phone's This session (round 21b): each route fits the width (no
  // closer than 100 %, no further than the route fit's floor), and the stop
  // that needs you — a perched or halted stop, else an asking agent tile —
  // is scrolled whole into view once per stop (the owner's own scroll stays).
  const phoneList = useRef<HTMLDivElement>(null);
  const [phoneFit, setPhoneFit] = useState<Record<string, number>>({});
  const phoneFitRef = useRef(phoneFit);
  phoneFitRef.current = phoneFit;
  const phoneShown = useRef<Record<string, string>>({});
  const phoneOne = narrow && mode === "one";
  useLayoutEffect(() => {
    const list = phoneList.current;
    if (!phoneOne || !list) return;
    const next: Record<string, number> = {};
    list.querySelectorAll<HTMLElement>(".sky-phone-fit[data-fit]").forEach((outer) => {
      const id = outer.dataset.fit || "";
      const inner = outer.firstElementChild as HTMLElement | null;
      if (!id || !inner) return;
      const z0 = phoneFitRef.current[id] || 1;
      next[id] = phoneRouteZoom(inner.getBoundingClientRect().width / z0, outer.clientWidth, ROUTE_FIT_MIN);
      if (Math.abs(next[id] - z0) > 0.005) return; // the zoom moves first; the scroll follows on the next pass
      const stop =
        inner.querySelector(".sky-node-stop.st-perched, .sky-node-stop.st-halted, .sky-node-stop.st-failed, .sky-node-stop.st-waiting_you") ||
        inner.querySelector(".sky-node.sky-head.tone-attention");
      const key = stop ? (stop.getAttribute("data-node") || stop.className) + "@" + next[id].toFixed(3) : "";
      if (!stop || phoneShown.current[id] === key) return;
      phoneShown.current[id] = key;
      const o = outer.getBoundingClientRect();
      const r = stop.getBoundingClientRect();
      const left = r.left - o.left + outer.scrollLeft;
      outer.scrollLeft = scrollToShow(outer.scrollLeft, outer.clientWidth, left, left + r.width);
    });
    const cur = phoneFitRef.current;
    if (Object.keys(next).length !== Object.keys(cur).length || Object.entries(next).some(([k, z]) => Math.abs((cur[k] || 0) - z) > 0.005)) setPhoneFit(next);
  });
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
          draggable={liveTool === "drag"}
          registerDelete={at ? registerFor(id) : undefined}
          onFlyOn={() => setAttachPick(f)}
          onRemove={() => removeGrounded(id)}
          sessionOver={sessionOver === id}
          merging={!!mergeOver && !!drag?.from[id]}
          selected={at ? sel.lines.has(id) : undefined}
          highlighted={at ? sel.stops.get(id) : undefined}
          onShiftSelect={at ? (n) => setSel((s) => toggleStop(s, id, n.id)) : undefined}
          head={triggerHeads.get(f.id) || null}
          registerChain={at ? chainReg.registerFor(id) : undefined}
          onPortDown={at ? portDownFor(id) : undefined}
          targets={wiring.targetsOf(id)}
          over={wiring.overOf(id)}
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
      draggable={liveTool === "drag"}
      registerDelete={at ? registerFor(id) : undefined}
      dropFlyway={drop?.title === id ? drop.plan : null}
      mergeTarget={mergeOver === id}
      selected={at ? sel.lines.has(id) : undefined}
      highlighted={at ? sel.stops.get(id) : undefined}
      onShiftSelect={at ? (n) => setSel((s) => toggleStop(s, id, n.id)) : undefined}
      registerChain={at ? chainReg.registerFor(id) : undefined}
      onPortDown={at ? portDownFor(id) : undefined}
      targets={wiring.targetsOf(id)}
      over={wiring.overOf(id)}
    />
  );

  // The selection's bar, screen-sized, kept inside the window: just above
  // the selected lines (below them at the window's top); just under the
  // selected stops (above them at the window's foot), off their line's bar.
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
      nameOf={(id) => (id === INTAKE_KEY ? "Intake" : lines.find((l) => l.id === id)?.name || windowName(id))}
      flightOf={(id) => lines.find((l) => l.id === id)?.flight || liveFlightOf(id)}
      kinds={kinds}
      onTidy={tidyThese}
      onClear={clearSel}
      rowOf={(id) => rows.find((r) => r.title === id)}
      posOf={(id) => (lines.some((l) => l.id === id) ? posOf(id) : undefined)}
      restoreGrounded={reground}
    />
  );

  const intakeOpen = !!layout[INTAKE_KEY]?.open;
  const intakeFrameNow = intakeFrame(layout[INTAKE_KEY]);
  const intakeW = intakeFrameNow?.w ?? (intakeOpen ? INTAKE_OPEN_W : INTAKE_W);
  const paletteFrameNow = paletteFrame(layout[PALETTE_KEY]);

  // Round 15: what every sky object gets (lib/skyObjects `SkyObjectHost`).
  // The intake lane and the palette keep their JSX here (the view's own
  // closures); the registry draws the rest.
  const renderIntake = (host: SkyObjectHost) => {
    // The intake lane: a sky object at its spot (layout key
    // "intake"), dragged by its head, expanded from it.
    const intakeAt = host.pos;
    return (
      <div
        ref={intakeSlot}
        data-tour="sky-intake"
        className={"sky-intake" + (drag?.from[INTAKE_KEY] ? " is-dragging" : "") + (sel.lines.has(INTAKE_KEY) ? " is-selected" : "")}
        data-lod={lodFor(cam.z)}
        data-open={intakeOpen ? "" : undefined}
        data-dim={dimmed(INTAKE_KEY) ? "" : undefined}
        style={{ transform: `translate(${intakeAt.x}px, ${intakeAt.y}px)`, width: intakeW, "--sky-z": cam.z } as React.CSSProperties}
      >
        <IntakeLane
          rows={rows}
          plans={plans}
          onFly={flashLine}
          width={intakeW}
          height={intakeFrameNow?.h}
          zoom={cam.z}
          onResize={(w, h) => onLayout(INTAKE_KEY, { w, h })}
          open={intakeOpen}
          onToggleOpen={() => onLayout(INTAKE_KEY, { open: !intakeOpen })}
          onHeadDown={(e) => onDragStart(INTAKE_KEY, e)}
          draggable={liveTool === "drag"}
          onMeasure={(w, h) => onMeasure(INTAKE_KEY, w, h)}
          onEditDefault={editDefault}
        />
      </div>
    );
  };
  const renderPalette = (host: SkyObjectHost) => (
    // The floating palette: a sky object at its spot (layout key
    // "palette"), dragged by its head, sized by its corner; its
    // blocks drag out exactly as the shelf's chips.
    <div
      className={"sky-palette-slot" + (drag?.from[PALETTE_KEY] ? " is-dragging" : "")}
      data-lod={lodFor(cam.z)}
      data-dim={dimmed(PALETTE_KEY) ? "" : undefined}
      style={{ transform: `translate(${host.pos.x}px, ${host.pos.y}px)`, "--sky-z": cam.z } as React.CSSProperties}
      onDragStart={onShelfDragStart}
      onDragEnd={onShelfDragEnd}
    >
      <SkyPalette
        width={paletteFrameNow?.w ?? (catalogOpen?.w || undefined)}
        height={paletteFrameNow?.h ?? (catalogOpen?.h || undefined)}
        zoom={cam.z}
        onHeadDown={(e) => onDragStart(PALETTE_KEY, e)}
        draggable={liveTool === "drag"}
        onResize={(w, h) => onLayout(PALETTE_KEY, { w, h })}
        onMeasure={(w, h) => onMeasure(PALETTE_KEY, w, h)}
        onPlace={(d) => void placeGrounded(d, "palette")}
        onDock={() => setPaletteMode("shelf")}
        onClose={() => setPaletteMode("shelf")}
        focusFilter={!!catalogOpen || filterFocus}
        onSession={newHere}
      />
    </div>
  );
  const objView: SkyObjectView = {
    ...objData,
    rows,
    plans,
    renderIntake,
    renderPalette,
    flashLine,
    placeGrounded: (d, near) => void placeGrounded(d, near),
    newHere,
    onShelfDragStart,
    onShelfDragEnd,
    landing: landingQ.data || null,
    landingError: landingQ.error ? errMsg(landingQ.error) : "",
    dropped,
    answerDrop: (key) =>
      setDropped((d) => {
        const { [key]: _gone, ...rest } = d;
        return rest;
      }),
    editingNote,
    setEditingNote,
    changeNote,
    removeNote,
    behaviors: { rows: behaviors.rows, expanded: behaviors.expanded, setExpanded: behaviors.setExpanded, hide: behaviors.hide },
  };
  const hostFor = (o: SkyObjectItem): SkyObjectHost => ({
    key: o.key,
    zoom: cam.z,
    lod: lodFor(cam.z),
    layoutEntry: layout[o.key],
    pos: posOf(o.key),
    draggable: liveTool === "drag",
    selected: sel.lines.has(o.key),
    dragging: !!drag?.from[o.key],
    target: mergeOver === o.key,
    onHeadDown: (e) => onDragStart(o.key, e),
    onResize: (w, h) => onLayout(o.key, { w, h }),
    onMeasure: (w, h) => onMeasure(o.key, w, h),
    onLayout: (patch) => onLayout(o.key, patch),
    view: objView,
    data: o.data,
  });
  /** One object on the plane: the lane's and the palette's own wrappers,
   * or the registry's (`.sky-object`, placed like a slot). */
  const objectFor = (o: SkyObjectItem) => {
    const host = hostFor(o);
    if (o.def.kind === "intake" || o.def.kind === "palette") return <Fragment key={o.key}>{o.def.render(host)}</Fragment>;
    // The Finished strip's × hides it (the ⋯ toggle brings it back).
    const drawn = o.def.render(host);
    const body = o.def.kind === "landing" && isValidElement(drawn) ? cloneElement(drawn as ReactElement<LandingStripProps>, { onHide: () => setFinishedOn(false) }) : drawn;
    return (
      <div
        key={o.key}
        className={objectClass(o.def, { dragging: host.dragging, selected: host.selected, target: host.target })}
        data-object={o.key}
        data-lod={host.lod}
        data-dim={dimmed(o.key) ? "" : undefined}
        style={{ transform: `translate(${host.pos.x}px, ${host.pos.y}px)`, "--sky-z": cam.z } as React.CSSProperties}
      >
        {body}
      </div>
    );
  };

  if (narrow)
    return (
      <main id="sky-view" className="sky sky-narrow" data-tour="flights-sky">
        {toolbar}
        <SkyGuide />
        {pick}
        {drawer}
        {newBlock}
        <div className="sky-list" ref={phoneList}>
          <SkyEmpty blank={blank} newSession={noSessions && !lines.length} className="fl-empty muted">{empty}</SkyEmpty>
          {phoneOne
            ? lines.map((l) => (
                <div key={l.id} className="sky-phone-fit" data-fit={l.id}>
                  <div className="sky-phone-fit-in" style={phoneFit[l.id] && phoneFit[l.id] !== 1 ? { zoom: phoneFit[l.id] } : undefined}>
                    {lineFor(l.id)}
                  </div>
                </div>
              ))
            : lines.map((l) => lineFor(l.id))}
        </div>
        {shelf}
      </main>
    );

  return (
    <main id="sky-view" className="sky" data-tool={liveTool} onDragOver={onSkyDragOver} onDrop={onSkyDrop}>
      {toolbar}
      <SkyGuide />
      {pick}
      <div className="sky-main">
        <div className="sky-stage">
          <div
            ref={vpRef}
            className={"sky-viewport" + (spaceHeld ? " is-grab" : "") + (panning ? " is-panning" : "") + (newOver ? " is-new-over" : "")}
            // The selection holds something: the keymap's Delete yields to the
            // sky's (lib/keymap `skyBoxFocused`).
            data-sky-box={selectionEmpty(sel) ? undefined : ""}
            data-tour="flights-sky"
            tabIndex={-1}
            role="application"
            aria-roledescription="sky"
            aria-label={
              "The sky: every session as a route — " +
              (liveTool === "click" ? "drag anywhere to pan, Shift+drag to select" : "drag empty space to select, the wheel or the arrows to pan") +
              ", Ctrl+wheel or + / − to zoom, double-click for a new session, Tab for the stops, ? for every key"
            }
            style={{ backgroundSize: `${GRID * cam.z}px ${GRID * cam.z}px`, backgroundPosition: `${cam.x}px ${cam.y}px` }}
            onPointerDownCapture={onDownCapture}
            onPointerDown={onDown}
            onPointerMove={onMove}
            onPointerUp={onUp}
            onPointerCancel={onUp}
            onDoubleClick={onDouble}
            onContextMenu={onGroundMenu}
            onDragOver={onNewDragOver}
            onDragLeave={onNewDragLeave}
            onDrop={onNewDrop}
            onScroll={onViewportScroll}
          >
            <SkyFlock width={vp.w} height={vp.h} zoom={cam.z} paused={motionOff || !!drag} birds={skyBirds} />
            <div
              className={"sky-plane" + (focusMode ? " sky-focus" : "")}
              data-dragging={drag ? "" : undefined}
              data-wiring={wiring.dragging ? "" : undefined}
              data-tidying={tidying ? "" : undefined}
              data-snapping={guides.length ? "" : undefined}
              style={{ transform: `translate(${cam.x}px, ${cam.y}px) scale(${cam.z})` }}
            >
              {objects.map(objectFor)}
              {lines.map((l) => {
                // The slot sits the line at its spot (the one it was given, or
                // where it was dropped); only Tidy glides it to a new one.
                const at = posOf(l.id);
                return (
                  <div
                    key={l.id}
                    className={
                      "sky-line-slot" +
                      (flash === l.id ? " is-flash" : "") +
                      (drag?.from[l.id] ? " is-dragging" : "") +
                      (drag?.from[l.id] && guides.length ? " is-snapping" : "") +
                      (l.flight ? " is-grounded" : "") +
                      (mergeOver && !l.flight && drag?.from[l.id] ? " is-merging" : "")
                    }
                    data-slot={l.id}
                    data-tour={l.flight ? undefined : "sky-line"}
                    data-dim={dimmed(l.id) ? "" : undefined}
                    style={{ transform: `translate(${at.x}px, ${at.y}px)` }}
                  >
                    {lineFor(l.id, at)}
                  </div>
                );
              })}
              {/* Round 18: the wires between chains and the one being drawn. */}
              <SkyWires band={wiring.band} wires={xwires} />
              {newBlock}
              {/* Round 17: the alignment guides of the drag under way — 1 screen
                  px of accent, from the moving box to the one it aligned with. */}
              {guides.map((g, i) => (
                <div
                  key={g.axis + i}
                  className="sky-guide"
                  data-axis={g.axis}
                  aria-hidden="true"
                  style={
                    g.axis === "x"
                      ? ({ left: g.at, top: g.from, height: g.to - g.from, "--sky-z": cam.z } as React.CSSProperties)
                      : ({ top: g.at, left: g.from, width: g.to - g.from, "--sky-z": cam.z } as React.CSSProperties)
                  }
                />
              ))}
            </div>
            {marq && <div className="sky-marquee" aria-hidden="true" style={{ transform: `translate(${marq.x}px, ${marq.y}px)`, width: marq.w, height: marq.h }} />}
            {groundMenu && (
              // Right-click on empty sky: add a block by search, right there.
              <QuickAdd
                x={groundMenu.x}
                y={groundMenu.y}
                kinds={kinds}
                plans={plans}
                onPick={(pick) => {
                  void dropGrounded(pick, groundMenu.at);
                  setGroundMenu(null);
                }}
                onNote={() => {
                  addNote(groundMenu.at);
                  setGroundMenu(null);
                }}
                onClose={() => setGroundMenu(null)}
              />
            )}
            {selBar}
            {/* The other devices as drop targets: shown while a session's
                chain is dragged alone, or while a drop's question is open. */}
            {mode !== "one" && (
              <DeviceDock
                devices={devices}
                dragging={sessionDrag}
                over={mergeOver}
                dropped={dropped}
                keyOf={deviceKey}
                onDone={objView.answerDrop}
                targetRef={(key, el) => {
                  if (el) deviceEls.current.set(key, el);
                  else deviceEls.current.delete(key);
                }}
              />
            )}
            {/* Round 17: the minimap (folded to its glyph or open, remembered)
                and the off-screen hints, bottom-right above the shelf; the
                shortcuts sheet over it all. The three are seams the
                `minimap` and `zoom-shortcuts` tasks fill in. */}
            <OffscreenHints rects={mapRects} cam={cam} viewport={vp} inset={drawerCover ? { right: drawerCover } : undefined} onJump={jumpTo} />
            {/* Nothing on the sky (no session yet): no minimap — the empty
                card is the one thing to look at. */}
            {mapRects.length > 0 && (
              <div className="sky-minimap-host" ref={mmHost} data-covering={mmCovers ? "" : undefined}>
                <Minimap
                  rects={mapRects}
                  cam={cam}
                  viewport={vp}
                  onCamera={(c) => userCam(c)}
                  onJump={jumpTo}
                  open={mmShown}
                  onToggle={() => {
                    // Folded only because it was over a route: show it anyway.
                    if (mmCovers) return setMmPeek(true);
                    if (mmPeek) return setMmPeek(false);
                    const v = !minimapOpen;
                    setMinimapOpen(v);
                    writePref(MINIMAP_KEY, v ? "1" : "0");
                  }}
                />
              </div>
            )}
            <SkyShortcuts open={shortcutsOpen} onClose={() => setShortcutsOpen(false)} groups={SKY_SHORTCUT_GROUPS} />
            {/* Not under the floating palette (or All stops, which floats it):
                the card draws above the plane, so it covered the catalog. */}
            {!(paletteOn && blank) && <SkyEmpty blank={blank} newSession={noSessions && !lines.length} className="sky-empty muted">{empty}</SkyEmpty>}
          </div>
          {shelf}
        </div>
        {drawer}
      </div>
    </main>
  );
}
