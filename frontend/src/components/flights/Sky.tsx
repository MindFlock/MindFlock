/** Flights → Sky: the open expanse every flight is drawn in (docs/flights.md
 * "The Sky", docs/web-ui.md "## The Sky").
 *
 * One plane, panned and zoomed as a whole — one `translate(x, y) scale(z)` on
 * one container, no library. Drag on empty sky pans it (mouse and touch), so
 * does the wheel or a trackpad; Ctrl/⌘+wheel and a pinch zoom it (40%–200%);
 * Space+drag pans from anywhere; 0 fits everything in the window. A faint dot
 * grid scales with it.
 *
 * Every session is a LINE floating in it: its session diagram on the flyway
 * canvas (the session box, its workers, the fast-track's rungs or the
 * flight's stops, and the trailing + "Tack on a stop"), under a title bar —
 * its name, its state chip, ⋯ (Open session · Show only this line · Fly a
 * flyway… · Close session) — that drags the line anywhere. Where a line was
 * put is remembered per device (lib/skyLayout, `mf_sky_pos`); the rest flow
 * into rows, newest at the top. A new session appears on the next poll; a
 * closed one's line fades away. The bird stands on each line's active box.
 *
 * Built from the space: a double-click on empty sky (or + New session) opens
 * a NewSessionBlock there, and the session it makes appears where it was.
 * The intake lane (IntakeLane) sits at the plane's left edge; the drawer at
 * the right (SkyDrawer) holds what is perched on you. A phone gets the lines
 * as a plain list instead — no pan, no zoom. */

import { useCallback, useEffect, useMemo, useRef, useState, type MouseEvent as ReactMouseEvent, type PointerEvent as ReactPointerEvent } from "react";
import { useFlightKinds, useFlightPlans, useFlights, useInstances } from "../../state/queries";
import { useUi } from "../../state/store";
import { closeSession } from "../../lib/sessionActions";
import { deriveSessionState } from "../../lib/sessionState";
import { birdPerch, layoutDiagram, sessionDiagram, type Diagram } from "../../lib/flightDiagram";
import { isLive, type Flight, type FlightRow } from "../../lib/flights";
import {
  INTAKE_W,
  LINES_LEFT,
  SKY_MARGIN,
  SKY_MODES,
  ZOOM_MAX,
  ZOOM_MIN,
  boundsOf,
  centerOn,
  fitCamera,
  inSky,
  lineFamily,
  loadSkyPos,
  placeLines,
  rememberSkyPos,
  skyLineTitles,
  skyTarget,
  toPlane,
  zoomAt,
  type Pos,
  type SkyCamera,
  type SkyMode,
} from "../../lib/skyLayout";
import { OverflowMenu, type MenuItem } from "../OverflowMenu";
import { FlywayCanvas } from "./FlywayCanvas";
import { liveBoxParts, openFlightSession } from "./FlightStrip";
import { useSessionLine, type LineSource } from "./useSessionLine";
import { ChipRow, TextField } from "./controls";
import { IntakeLane } from "./IntakeLane";
import { NewSessionBlock } from "./NewSessionBlock";
import { SkyDrawer } from "./SkyDrawer";
import { Bird } from "./Bird";
import "./Sky.css";

/** A line's chrome around its canvas: the title bar and the card's padding. */
const BAR_H = 34;
const LINE_PAD = 10;
const LINE_MIN_W = 280;
/** The dot grid's spacing at 100%. */
const GRID = 24;
const MODE_KEY = "mf_sky_view";
const DRAWER_KEY = "mf_sky_drawer";

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

/** Typing in a field (or a contenteditable) owns the keyboard. */
const typing = (el: Element | null) =>
  !!el && (el.tagName === "INPUT" || el.tagName === "TEXTAREA" || (el as HTMLElement).isContentEditable === true);

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

/** The size a line is drawn at (plane px): its canvas — perches opened wide,
 * the bird's headroom — plus the title bar and padding. */
export function lineSize(d: Diagram, flight: Flight | null): { w: number; h: number } {
  const lay = layoutDiagram(d, { sizeOf: liveBoxParts(flight, true, () => {}).sizeOf });
  // + 12: the canvas's own padding; + 4: the card's border and a hair.
  return { w: Math.max(LINE_MIN_W, lay.width + 12 + LINE_PAD * 2 + 4), h: lay.height + 32 + BAR_H + LINE_PAD * 2 };
}

interface LineProps {
  id: string;
  src: LineSource;
  family: boolean;
  /** Placed in the plane (else: in the phone's list). */
  pos?: Pos;
  w?: number;
  zoom?: number;
  dragging?: boolean;
  flash?: boolean;
  chosen?: boolean;
  onPress(id: string): void;
  onOnly(id: string): void;
  onMove?(id: string, p: Pos): void;
  onDrop?(id: string, p: Pos): void;
}

/** One session's line: the title bar (drag it to move the line), then the
 * session diagram with the bird, then whatever its + or a stop opened. */
function SkyLine(p: LineProps) {
  const line = useSessionLine(p.id, p.src, { family: p.family, onSession: (t) => openFlightSession(t) });
  const grab = useRef<{ id: number; sx: number; sy: number; x: number; y: number; moved: boolean } | null>(null);
  const row = line.row;
  if (!row) return null;
  const ss = deriveSessionState(row);
  const placed = !!p.pos;
  const z = p.zoom || 1;
  const at = (e: ReactPointerEvent, g: NonNullable<typeof grab.current>): Pos => ({ x: g.x + (e.clientX - g.sx) / z, y: g.y + (e.clientY - g.sy) / z });

  const onBarDown = (e: ReactPointerEvent<HTMLDivElement>) => {
    if (!placed || e.button !== 0) return;
    if ((e.target as Element).closest("button, summary, a, input, [data-no-drag]")) return;
    e.stopPropagation();
    grab.current = { id: e.pointerId, sx: e.clientX, sy: e.clientY, x: p.pos!.x, y: p.pos!.y, moved: false };
    try {
      e.currentTarget.setPointerCapture(e.pointerId);
    } catch {
      /* a synthetic event has no capture */
    }
  };
  const onBarMove = (e: ReactPointerEvent<HTMLDivElement>) => {
    const g = grab.current;
    if (!g || g.id !== e.pointerId) return;
    if (!g.moved && Math.hypot(e.clientX - g.sx, e.clientY - g.sy) < 4) return;
    g.moved = true;
    p.onMove?.(p.id, at(e, g));
  };
  const onBarUp = (e: ReactPointerEvent<HTMLDivElement>) => {
    const g = grab.current;
    if (!g || g.id !== e.pointerId) return;
    grab.current = null;
    if (g.moved) p.onDrop?.(p.id, at(e, g));
    else p.onPress(p.id);
  };

  const items: MenuItem[] = [
    { key: "open", label: "Open session", title: "Close the sky and open this session", onSelect: () => openFlightSession(p.id) },
    { key: "only", label: "Show only this line", title: "This session: one line, filling the sky", onSelect: () => p.onOnly(p.id) },
    { key: "flyway", label: "Fly a flyway…", title: "Tack a stop, or a whole flyway, onto this line", onSelect: () => line.togglePalette(true) },
    { key: "close", label: "Close session", danger: true, title: "Stops the agent and moves it to Recently closed", onSelect: () => void closeSession(p.id) },
  ];

  return (
    <div
      className={
        "sky-line" +
        (placed ? "" : " sky-line-flow") +
        (line.editing ? " is-editing" : "") +
        (p.dragging ? " is-dragging" : "") +
        (p.flash ? " is-flash" : "") +
        (p.chosen ? " is-chosen" : "")
      }
      style={placed ? { transform: `translate(${p.pos!.x}px, ${p.pos!.y}px)`, width: p.w } : undefined}
      data-line={p.id}
      role="group"
      aria-label={p.id + "'s line"}
    >
      <div
        className="sky-line-bar"
        title={placed ? "Drag to move this line" : undefined}
        onPointerDown={onBarDown}
        onPointerMove={onBarMove}
        onPointerUp={onBarUp}
        onPointerCancel={onBarUp}
      >
        <Bird size={13} />
        <span className="sky-line-name">{p.id}</span>
        <span className={"fl-box-chip sky-line-chip tone-" + ss.tone} title={ss.hint}>
          {ss.label}
        </span>
        <span className="sky-line-menu" data-no-drag>
          <OverflowMenu label={"Actions for " + p.id} items={items} />
        </span>
      </div>
      {line.cancel.node}
      <FlywayCanvas
        diagram={line.diagram}
        label={"What " + p.id + " is doing and will do"}
        vertical={placed ? false : undefined}
        bird={birdPerch(line.diagram)}
        className="sky-canvas"
        {...line.canvas}
      />
      {line.editing && (
        <div className="sky-line-panels sky-own-scroll" data-no-drag>
          {line.palette}
          {line.inspector}
        </div>
      )}
    </div>
  );
}

/** A closed session's line on its way out: the last picture of it, fading. */
function GhostLine({ id, pos, w, diagram }: { id: string; pos: Pos; w: number; diagram: Diagram }) {
  return (
    <div className="sky-line sky-line-leaving" style={{ transform: `translate(${pos.x}px, ${pos.y}px)`, width: w }} aria-hidden="true">
      <div className="sky-line-bar">
        <Bird size={13} />
        <span className="sky-line-name">{id}</span>
      </div>
      <FlywayCanvas diagram={diagram} label={id} vertical={false} className="sky-canvas" />
    </div>
  );
}

interface Ghost {
  id: string;
  pos: Pos;
  w: number;
  diagram: Diagram;
}

export interface SkyProps {
  /** This session's title (`#flights/sky/<title>`), "" for the whole sky. */
  focus: string;
  onTab(tab: "plans" | "sources" | "checklists", rest?: string): void;
}

export function Sky({ focus, onTab }: SkyProps) {
  const instQ = useInstances();
  const rows = useMemo(() => instQ.data || [], [instQ.data]);
  const flights = useFlights().data;
  const kinds = useFlightKinds().data || [];
  const plans = useFlightPlans().data || [];
  const openDialogFor = useUi((s) => s.openDialogFor);
  const narrow = useNarrowWindow();

  const [pick, setPick] = useState<Exclude<SkyMode, "one">>(() => {
    const m = readPref(MODE_KEY);
    return m === "all" || m === "perched" ? m : "mine";
  });
  const mode: SkyMode = focus ? "one" : pick;
  const [query, setQuery] = useState("");
  const [saved, setSaved] = useState(() => loadSkyPos());
  const [drag, setDrag] = useState<{ id: string; pos: Pos } | null>(null);
  const [cam, setCam] = useState<SkyCamera>({ x: 0, y: 0, z: 1 });
  const camRef = useRef(cam);
  camRef.current = cam;
  const [vp, setVp] = useState({ w: 0, h: 0 });
  const vpRef = useRef<HTMLDivElement>(null);
  const [newAt, setNewAt] = useState<Pos | null>(null);
  const [drawerPref, setDrawerPref] = useState(() => readPref(DRAWER_KEY));
  const [chosen, setChosen] = useState("");
  const [flash, setFlash] = useState("");
  const [spaceHeld, setSpaceHeld] = useState(false);
  const space = useRef(false);

  const src: LineSource = useMemo(() => ({ rows, flights, kinds, plans }), [rows, flights, kinds, plans]);
  const family = lineFamily(mode);
  const titles = useMemo(() => skyLineTitles(rows, mode, { query, focus }), [rows, mode, query, focus]);
  const lines = useMemo(
    () =>
      titles.map((t) => {
        const fl = rows.find((r) => r.title === t)?.flight as FlightRow | null | undefined;
        const full = fl && isLive(fl) ? flights?.find((f) => f.id === fl.id) || null : null;
        const diagram = sessionDiagram(rows, t, { flights, kinds, family });
        return { id: t, diagram, ...lineSize(diagram, full) };
      }),
    [titles, rows, flights, kinds, family]
  );
  const place = useMemo(
    () =>
      mode === "one"
        ? Object.fromEntries(lines.map((l) => [l.id, { x: SKY_MARGIN, y: SKY_MARGIN }]))
        : placeLines(lines, saved, { width: Math.max(vp.w, LINES_LEFT + 900), left: LINES_LEFT, top: SKY_MARGIN }),
    [lines, saved, vp.w, mode]
  );
  const posOf = useCallback((id: string): Pos => (drag?.id === id ? drag.pos : place[id] || { x: LINES_LEFT, y: SKY_MARGIN }), [drag, place]);
  // Open until folded away (then remembered per device).
  const drawerOpen = drawerPref !== "0";

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

  // The owner has panned or zoomed since the last fit: leave the camera be.
  const touched = useRef(false);
  const fit = useCallback(() => {
    touched.current = false;
    const rects = lines.map((l) => ({ ...posOf(l.id), w: l.w, h: l.h }));
    if (mode !== "one") rects.push({ x: SKY_MARGIN, y: SKY_MARGIN, w: INTAKE_W, h: 1 });
    setCam(fitCamera(boundsOf(rects), vp.w, vp.h, mode === "one" ? 1.5 : 1));
  }, [lines, posOf, mode, vp.w, vp.h]);

  // Fit once the window and the sessions are known, again on a new view, and
  // whenever the lines change shape (the flights load and a perch opens wide,
  // a session comes or goes) until the owner moves the camera.
  const fitted = useRef("");
  const fitKey = mode + "\u0000" + focus;
  const shape = lines.map((l) => l.id + ":" + l.w + "x" + l.h).join("|");
  useEffect(() => {
    if (!vp.w || !vp.h || instQ.data === undefined) return;
    if (fitted.current === fitKey && touched.current) return;
    fitted.current = fitKey;
    fit();
  }, [vp.w, vp.h, instQ.data, fitKey, shape]); // eslint-disable-line react-hooks/exhaustive-deps

  // A closed (or deleted) session's line leaves: the last picture fades out.
  const last = useRef(new Map<string, Ghost>());
  const alive = useRef(true);
  const [ghosts, setGhosts] = useState<Ghost[]>([]);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);
  useEffect(() => {
    const now = new Map<string, Ghost>(lines.map((l) => [l.id, { id: l.id, pos: place[l.id], w: l.w, diagram: l.diagram }]));
    const gone = [...last.current.values()].filter((g) => !now.has(g.id) && !rows.some((r) => r.title === g.id && inSky(r)));
    last.current = now;
    if (!gone.length || narrow) return;
    const ids = new Set(gone.map((g) => g.id));
    setGhosts((cur) => [...cur.filter((g) => !ids.has(g.id)), ...gone]);
    setTimeout(() => alive.current && setGhosts((cur) => cur.filter((g) => !ids.has(g.id))), 450);
  }, [lines, place, rows, narrow]);

  const flyTo = useCallback(
    (id: string) => {
      const l = lines.find((x) => x.id === id);
      if (!l) {
        // Not in this view (filtered out, or a worker folded into its parent):
        // the single-session view shows it.
        if (rows.some((r) => r.title === id && inSky(r))) openDialogFor("flights", skyTarget(id));
        return;
      }
      touched.current = true;
      setCam(centerOn({ ...posOf(id), w: l.w, h: l.h }, vp.w, vp.h, Math.max(camRef.current.z, 0.85)));
      setChosen(id);
      setFlash(id);
      setTimeout(() => alive.current && setFlash((f) => (f === id ? "" : f)), 1400);
    },
    [lines, rows, posOf, vp.w, vp.h, openDialogFor]
  );

  const setMode = (m: SkyMode) => {
    if (m === "one") {
      const t = focus || chosen || titles[0];
      if (t) openDialogFor("flights", skyTarget(t));
      return;
    }
    setPick(m);
    writePref(MODE_KEY, m);
    if (focus) openDialogFor("flights", skyTarget());
  };

  const zoomBy = (k: number) => {
    touched.current = true;
    setCam((c) => zoomAt(c, c.z * k, vp.w / 2, vp.h / 2));
  };
  const newHere = () => {
    const c = toPlane(camRef.current, vp.w / 2, vp.h / 3);
    setNewAt({ x: Math.round(c.x - 170), y: Math.round(c.y) });
  };

  // --- Pan, pinch, wheel, keys ---------------------------------------------------

  const pointers = useRef(new Map<number, Pos>());
  const pan = useRef<{ id: number; x: number; y: number; cx: number; cy: number } | null>(null);
  const pinch = useRef<{ d: number; z: number } | null>(null);
  const ground = (t: EventTarget | null) =>
    t instanceof Element && (t === vpRef.current || t.classList.contains("sky-plane") || t.classList.contains("sky-ground"));

  const startPan = (e: ReactPointerEvent<HTMLDivElement>) => {
    touched.current = true;
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
  const onDown = (e: ReactPointerEvent<HTMLDivElement>) => {
    if (!ground(e.target) || (e.pointerType === "mouse" && e.button !== 0 && e.button !== 1)) return;
    e.preventDefault();
    startPan(e);
  };
  const onMove = (e: ReactPointerEvent<HTMLDivElement>) => {
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
    pointers.current.delete(e.pointerId);
    if (pointers.current.size < 2) pinch.current = null;
    if (pan.current?.id === e.pointerId) pan.current = null;
  };
  const onDouble = (e: ReactMouseEvent<HTMLDivElement>) => {
    if (!ground(e.target)) return;
    const r = vpRef.current!.getBoundingClientRect();
    const at = toPlane(camRef.current, e.clientX - r.left, e.clientY - r.top);
    setNewAt({ x: Math.round(at.x), y: Math.round(at.y) });
  };

  // The wheel: a native listener, since React's is passive and can't keep the
  // page (and the dialog) from scrolling.
  useEffect(() => {
    const el = vpRef.current;
    if (!el) return;
    const onWheel = (e: WheelEvent) => {
      if ((e.target as Element | null)?.closest?.(".sky-own-scroll")) return;
      e.preventDefault();
      touched.current = true;
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

  // Space+drag pans, 0 fits, Escape folds an open new-session card away.
  useEffect(() => {
    if (narrow) return;
    const down = (e: KeyboardEvent) => {
      const el = document.activeElement;
      if (e.key === "Escape" && newAt) {
        e.preventDefault();
        setNewAt(null);
        return;
      }
      if (typing(el) || e.ctrlKey || e.metaKey || e.altKey) return;
      if (e.key === " " && !e.repeat && !el?.closest?.("button, summary, a, [role=listitem]")) {
        e.preventDefault();
        space.current = true;
        setSpaceHeld(true);
      } else if (e.key === "0") {
        e.preventDefault();
        fit();
      }
    };
    const up = (e: KeyboardEvent) => {
      if (e.key !== " ") return;
      space.current = false;
      setSpaceHeld(false);
    };
    window.addEventListener("keydown", down, true);
    window.addEventListener("keyup", up, true);
    return () => {
      window.removeEventListener("keydown", down, true);
      window.removeEventListener("keyup", up, true);
    };
  }, [narrow, fit, newAt]);

  // --- Lines ------------------------------------------------------------------------

  const onLineMove = useCallback((id: string, p: Pos) => setDrag({ id, pos: p }), []);
  const onLineDrop = useCallback((id: string, p: Pos) => {
    setSaved((s) => rememberSkyPos(s, id, p));
    setDrag(null);
  }, []);
  const onOnly = useCallback((id: string) => openDialogFor("flights", skyTarget(id)), [openDialogFor]);

  const empty =
    lines.length > 0
      ? ""
      : mode === "one"
        ? focus
          ? "“" + focus + "” isn't in the sky right now."
          : "Pick a line first."
        : query
          ? "No line matches “" + query + "”."
          : mode === "perched"
            ? "Nothing is perched on you."
            : instQ.data === undefined
              ? ""
              : "No sessions in the sky yet — double-click anywhere, or + New session, to start one.";

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
        <TextField value={query} onChange={setQuery} label="Find a line" placeholder="Find a line…" onEnter={() => titles[0] && flyTo(titles[0])} />
      </span>
      <button type="button" className="btn-primary fl-btn sky-new" data-tour="flights-newblock" title="Start a session here (or double-click the sky)" onClick={newHere}>
        + New session
      </button>
      {!narrow && (
        <span className="sky-zoom" role="group" aria-label="Zoom">
          <button type="button" className="sky-zoom-btn" aria-label="Zoom out" disabled={cam.z <= ZOOM_MIN + 0.001} onClick={() => zoomBy(1 / 1.2)}>
            −
          </button>
          <span className="sky-zoom-read" aria-live="polite">
            {Math.round(cam.z * 100)}%
          </span>
          <button type="button" className="sky-zoom-btn" aria-label="Zoom in" disabled={cam.z >= ZOOM_MAX - 0.001} onClick={() => zoomBy(1.2)}>
            +
          </button>
          <button type="button" className="sky-zoom-btn sky-fit" title="Fit every line in the window (0)" onClick={fit}>
            Fit
          </button>
          <button type="button" className="sky-zoom-btn" title="Back to 100%" onClick={() => zoomBy(1 / cam.z)}>
            100%
          </button>
        </span>
      )}
    </div>
  );

  const drawer = (
    <SkyDrawer
      open={drawerOpen}
      onToggle={() => {
        const v = drawerOpen ? "0" : "1";
        setDrawerPref(v);
        writePref(DRAWER_KEY, v);
      }}
      onFocus={flyTo}
      onTab={onTab}
    />
  );

  const newBlock = newAt && (
    <NewSessionBlock
      x={newAt.x}
      y={newAt.y}
      plans={plans}
      onCancel={() => setNewAt(null)}
      onCreated={(title) => {
        setSaved((s) => rememberSkyPos(s, title, newAt));
        setNewAt(null);
        setChosen(title);
      }}
    />
  );

  if (narrow)
    return (
      <div className="sky sky-narrow" data-tour="flights-sky">
        {toolbar}
        {drawer}
        {newBlock}
        <div className="sky-list">
          {empty && <p className="fl-empty muted">{empty}</p>}
          {lines.map((l) => (
            <SkyLine key={l.id} id={l.id} src={src} family={family} onPress={setChosen} onOnly={onOnly} />
          ))}
        </div>
      </div>
    );

  return (
    <div className="sky">
      {toolbar}
      <div className="sky-main">
        <div
          ref={vpRef}
          className={"sky-viewport" + (spaceHeld ? " is-grab" : "")}
          data-tour="flights-sky"
          role="application"
          aria-roledescription="canvas"
          aria-label="The sky: every session as a line of blocks — drag empty space to pan, Ctrl+wheel to zoom, double-click to start a session"
          style={{ backgroundSize: `${GRID * cam.z}px ${GRID * cam.z}px`, backgroundPosition: `${cam.x}px ${cam.y}px` }}
          onPointerDownCapture={onDownCapture}
          onPointerDown={onDown}
          onPointerMove={onMove}
          onPointerUp={onUp}
          onPointerCancel={onUp}
          onDoubleClick={onDouble}
        >
          <div className="sky-plane" style={{ transform: `translate(${cam.x}px, ${cam.y}px) scale(${cam.z})` }}>
            {mode !== "one" && (
              <div className="sky-intake" style={{ transform: `translate(${SKY_MARGIN}px, ${SKY_MARGIN}px)`, width: INTAKE_W }}>
                <IntakeLane rows={rows} plans={plans} onFly={flyTo} width={INTAKE_W} />
              </div>
            )}
            {lines.map((l) => (
              <SkyLine
                key={l.id}
                id={l.id}
                src={src}
                family={family}
                pos={posOf(l.id)}
                w={l.w}
                zoom={cam.z}
                dragging={drag?.id === l.id}
                flash={flash === l.id}
                chosen={chosen === l.id}
                onPress={setChosen}
                onOnly={onOnly}
                onMove={onLineMove}
                onDrop={onLineDrop}
              />
            ))}
            {ghosts.map((g) => (
              <GhostLine key={"ghost:" + g.id} {...g} />
            ))}
            {newBlock}
          </div>
          {empty && (
            <p className="sky-empty muted" role="status">
              {empty}
            </p>
          )}
        </div>
        {drawer}
      </div>
    </div>
  );
}
