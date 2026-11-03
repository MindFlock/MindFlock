/** The Behaviors board (docs/behaviors.md §3; notes in docs/behaviors/board.md):
 * a calm Sky object (layout key "behaviors", lib/skyObjects `BEHAVIORS`) in
 * the right column, for the behaviors with NO session frame in the Sky — a
 * closed session, another branch, a watched repo's push. Rows grouped by tone
 * (Needs you · Failed · Checking · Not checked · Verified today); a row opens
 * into the one Behaviors card (`BehaviorsCard compact`). Its ⋯ holds what the
 * old Verify dialog's header held (`BehaviorsSettings`, in a portal popover);
 * its × hides it.
 *
 * Shown when a row needs you or failed, when turned on from the Sky's ⋯
 * ("Show the Behaviors board", per device, `BEHAVIORS_PREF`), or while a
 * request asked for it (`useBehaviorsFocus().boardRequested`). Hidden, it is
 * never placed and keeps no room.
 *
 * Also here, so SkyView carries only a few lines: `useBehaviorsBoard` (the
 * board's rows, visibility and state) and `useBehaviorsRequest` (an
 * `openBehaviors` request: the camera flies to the session's frame and the
 * tile opens its card, else the board shows with that row open). */

import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type PointerEvent as ReactPointerEvent } from "react";
import { createPortal } from "react-dom";
import type { Instance, TestPlan } from "../../api/types";
import { behaviorSummary, nextNeedingYou, orphanPlans, planForSession, type BehaviorTone } from "../../lib/behaviors";
import { findPlan, useBehaviorsFocus, type BehaviorsRequest, type BehaviorsTarget } from "../../lib/behaviorsFocus";
import { useTestPlans } from "../../state/queries";
import { BehaviorsCard } from "../behaviors/BehaviorsCard";
import { BehaviorsSettings } from "../behaviors/BehaviorsSettings";
import { planName } from "../dialogs/verify";
import { TOUR_REVEAL } from "../onboarding/CoachTour";
import "./BehaviorsBoard.css";

/** The board's layout key. */
export const BEHAVIORS_KEY = "behaviors";
/** Turned on from the Sky's ⋯ ("1"), per device. */
export const BEHAVIORS_PREF = "mf_sky_behaviors";
export const BEHAVIORS_W = 300;
export const BEHAVIORS_H = 200;
export const BEHAVIORS_MIN = { w: 240, h: 110 };
export const BEHAVIORS_MAX = { w: 900, h: 1600 };
/** The line under its title: what the board is for. */
export const BEHAVIORS_EXPLAIN = "Behaviors with no session in the Sky — a closed session, another branch, a watched repo's push";
/** "Verified today": verified in the last day. */
export const VERIFIED_TODAY_S = 24 * 3600;
/** After the camera's glide (~220 ms), the open card is brought whole into view. */
const REVEAL_AFTER_MS = 320;
/** A fly that finds no frame for this long gives up. */
const FLY_GIVE_UP_MS = 3000;

/** The groups, in order: one per tone. */
export const BOARD_GROUPS: readonly { tone: BehaviorTone; label: string }[] = [
  { tone: "you", label: "Needs you" },
  { tone: "bad", label: "Failed" },
  { tone: "busy", label: "Checking" },
  { tone: "quiet", label: "Not checked" },
  { tone: "ok", label: "Verified today" },
];

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

/** When the behaviors were last checked (their newest run), unix seconds; 0 = never. */
export function lastCheckedAt(plan: TestPlan): number {
  return (plan.runs || []).reduce((t, r) => Math.max(t, r.at || 0), 0);
}

/** The behaviors that need you or failed: they bring the board out by themselves. */
export function isUrgent(plan: TestPlan): boolean {
  const t = behaviorSummary(plan).tone;
  return t === "you" || t === "bad";
}

export interface BoardGroup {
  tone: BehaviorTone;
  label: string;
  plans: TestPlan[];
}

/** The board's rows by group, empty groups left out. Verified behaviors stay
 * only for a day after their last check (the board is not an archive). */
export function boardGroups(plans: readonly TestPlan[], now = Date.now() / 1000): BoardGroup[] {
  const by = new Map<BehaviorTone, TestPlan[]>();
  for (const p of plans) {
    const tone = behaviorSummary(p).tone;
    if (tone === "ok" && now - lastCheckedAt(p) > VERIFIED_TODAY_S) continue;
    by.set(tone, [...(by.get(tone) || []), p]);
  }
  return BOARD_GROUPS.filter((g) => by.has(g.tone)).map((g) => ({ ...g, plans: by.get(g.tone)! }));
}

/** Whether the board is on the Sky: never on a phone or in This session;
 * else turned on, asked for, or holding a row that needs you or failed which
 * its × has not already put away. */
export function boardShown(s: { allowed: boolean; pref: boolean; requested: boolean; urgent: readonly string[]; dismissed: readonly string[] }): boolean {
  if (!s.allowed) return false;
  return s.pref || s.requested || s.urgent.some((id) => !s.dismissed.includes(id));
}

/** Where a request goes: a session frame in the Sky (and the plan whose card
 * its tile opens), or the board (with that row open, "" for none). */
export type BehaviorsAim = { to: "line"; line: string; plan: TestPlan | null } | { to: "board"; expand: string };

/** Resolve an `openBehaviors` target: `board` → the board; `next` → the next
 * behaviors that need you (else failed), else the board; a plan id / title /
 * branch → its session's frame when the Sky draws one, else its board row.
 * A title with a frame but no behaviors yet still flies to the frame. */
export function aimFor(t: BehaviorsTarget, plans: readonly TestPlan[], lineForPlan: (p: TestPlan) => string, lineOfTitle: (title: string) => string): BehaviorsAim {
  if (t.board) return { to: "board", expand: "" };
  const plan = t.next ? nextNeedingYou(plans) : findPlan(plans, t);
  if (!plan) {
    const line = !t.next && t.title ? lineOfTitle(t.title) : "";
    return line ? { to: "line", line, plan: null } : { to: "board", expand: "" };
  }
  const line = lineForPlan(plan);
  return line ? { to: "line", line, plan } : { to: "board", expand: plan.id };
}

/** An attribute value inside a selector's quotes. */
const attrSel = (v: string) => v.replace(/["\\]/g, "\\$&");

export interface BehaviorsBoardState {
  /** The board is on the Sky. */
  on: boolean;
  /** Every plan, and whether they have loaded (or failed to). */
  plans: TestPlan[];
  loaded: boolean;
  /** The board's rows: the behaviors with no frame in the Sky. */
  rows: TestPlan[];
  /** The row open into its card ("" = none). */
  expanded: string;
  setExpanded(id: string): void;
  /** The ⋯ toggle: shown → hidden (as ×), hidden → turned on. */
  toggle(): void;
  /** The ×: hidden, the request cleared, its urgent rows put away. */
  hide(): void;
  /** A request's: shown (and that row open). */
  show(expand?: string): void;
  /** The line (layout id) that draws a plan's session, or "". */
  lineForPlan(plan: TestPlan): string;
  /** The line that draws a session (its own, or its lead's), or "". */
  lineOfTitle(title: string): string;
}

/** The board's data and state, for SkyView. `titles` are the session lines
 * the Sky draws; a worker is drawn inside its lead's line. `allowed`: the
 * view places objects (not a phone, not This session). */
export function useBehaviorsBoard({ rows, titles, allowed }: { rows: readonly Instance[]; titles: readonly string[]; allowed: boolean }): BehaviorsBoardState {
  const q = useTestPlans();
  const plans = useMemo(() => q.data?.plans || [], [q.data]);
  const loaded = q.data !== undefined || q.isError;
  const requested = useBehaviorsFocus((s) => s.boardRequested);
  const [pref, setPrefState] = useState(() => readPref(BEHAVIORS_PREF) === "1");
  const setPref = useCallback((v: boolean) => {
    setPrefState(v);
    writePref(BEHAVIORS_PREF, v ? "1" : "0");
  }, []);
  const [dismissed, setDismissed] = useState<string[]>([]);
  const [expanded, setExpanded] = useState("");
  const lineOfTitle = useMemo(() => {
    const shown = new Set(titles);
    const parent = new Map(rows.map((r) => [r.title, r.parent || ""]));
    return (title: string) => (shown.has(title) ? title : shown.has(parent.get(title) || "\u0000") ? parent.get(title)! : "");
  }, [rows, titles]);
  const framed = useMemo(() => rows.filter((r) => lineOfTitle(r.title)), [rows, lineOfTitle]);
  const lineForPlan = useCallback(
    (plan: TestPlan) => {
      const r = framed.find((x) => planForSession(plans, x)?.id === plan.id);
      return r ? lineOfTitle(r.title) : "";
    },
    [framed, plans, lineOfTitle]
  );
  const orphans = useMemo(() => orphanPlans(plans, framed), [plans, framed]);
  const urgent = useMemo(() => orphans.filter(isUrgent).map((p) => p.id), [orphans]);
  const on = boardShown({ allowed, pref, requested, urgent, dismissed });
  const hide = useCallback(() => {
    setPref(false);
    setDismissed(urgent);
    setExpanded("");
    useBehaviorsFocus.getState().setBoardRequested(false);
  }, [setPref, urgent]);
  const show = useCallback((expand = "") => {
    useBehaviorsFocus.getState().setBoardRequested(true);
    if (expand) setExpanded(expand);
  }, []);
  const toggle = useCallback(() => (on ? hide() : setPref(true)), [on, hide, setPref]);
  return { on, plans, loaded, rows: orphans, expanded, setExpanded, toggle, hide, show, lineForPlan, lineOfTitle };
}

/** Where the camera goes for a request: a line id or the board's key, and
 * the selector of what to bring whole into view after the glide ("" = none). */
export interface BehaviorsFly {
  id: string;
  reveal: string;
}

/** Take one request: resolve it, open the card (`setOpenPlan`: the session's
 * tile draws it) or show the board with that row open, `take()` it, and say
 * where the camera goes. */
export function applyRequest(request: BehaviorsRequest, board: Pick<BehaviorsBoardState, "plans" | "lineForPlan" | "lineOfTitle" | "show">): BehaviorsFly {
  const f = useBehaviorsFocus.getState();
  const a = aimFor(request, board.plans, board.lineForPlan, board.lineOfTitle);
  f.take();
  if (a.to === "line") {
    if (a.plan) f.setOpenPlan(a.plan.id);
    return { id: a.line, reveal: a.plan ? `.sky-line-slot[data-slot="${attrSel(a.line)}"] [data-plan="${attrSel(a.plan.id)}"]` : "" };
  }
  board.show(a.expand);
  return { id: BEHAVIORS_KEY, reveal: a.expand ? `.sky-bh [data-bh-row="${attrSel(a.expand)}"]` : "" };
}

/** An `openBehaviors` request, taken by the Sky (docs/behaviors.md §4): once
 * the plans are in and the Sky is `ready` (measured and placed — a request
 * made before SkyView mounted waits in the store for this), resolve it
 * (`aimFor`), open the card (`setOpenPlan`, the tile draws it) or the board's
 * row, fly the camera there (`fly(id)`: false while the target has no rect
 * yet — tried again next render, for a few seconds), then bring the open card
 * whole into view, and `take()` the request. */
export function useBehaviorsRequest(board: BehaviorsBoardState, { ready, fly }: { ready: boolean; fly(id: string): boolean }): void {
  const request = useBehaviorsFocus((s) => s.request);
  const [aim, setAim] = useState<{ id: string; reveal: string; at: number } | null>(null);
  const boardRef = useRef(board);
  boardRef.current = board;
  useEffect(() => {
    if (!request || !board.loaded || !ready) return;
    setAim({ ...applyRequest(request, boardRef.current), at: Date.now() });
  }, [request, board.loaded, ready]);
  const revealT = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  useEffect(() => () => clearTimeout(revealT.current), []);
  useEffect(() => {
    if (!aim) return;
    if (!fly(aim.id)) {
      if (Date.now() - aim.at > FLY_GIVE_UP_MS) setAim(null);
      return;
    }
    setAim(null);
    if (!aim.reveal || typeof document === "undefined") return;
    clearTimeout(revealT.current);
    revealT.current = setTimeout(() => {
      document.querySelector(aim.reveal)?.dispatchEvent(new Event(TOUR_REVEAL, { bubbles: true, cancelable: true }));
    }, REVEAL_AFTER_MS);
  }, [aim, fly]);
}

export interface BehaviorsBoardProps {
  /** The board's rows (the behaviors with no frame in the Sky). */
  plans: readonly TestPlan[];
  /** The row open into its card ("" = none). */
  expanded: string;
  onExpand(id: string): void;
  /** Its ×. */
  onHide?(): void;
  /** The frame the host keeps (its layout entry's w / h), else its own size. */
  width?: number;
  height?: number;
  zoom?: number;
  onHeadDown?(e: ReactPointerEvent): void;
  draggable?: boolean;
  onResize?(w: number, h: number): void;
  onMeasure?(w: number, h: number): void;
  /** For "Verified today": unix seconds, else now. */
  now?: number;
}

/** A press on the head that is the head's own (not one of its buttons). */
const headPress = (e: ReactPointerEvent) => e.button === 0 && !(e.target as Element).closest("button, a, input, summary, [data-no-drag]");

export function BehaviorsBoard({ plans, expanded, onExpand, onHide, width, height, zoom = 1, onHeadDown, draggable = true, onResize, onMeasure, now }: BehaviorsBoardProps) {
  const rootRef = useRef<HTMLElement>(null);
  const [live, setLive] = useState<{ w: number; h: number } | null>(null);
  const grab = useRef<{ id: number; sx: number; sy: number; w: number; h: number } | null>(null);
  const frameNow = live || { w: width ?? 0, h: height ?? 0 };
  const resizeDown = (e: ReactPointerEvent<HTMLSpanElement>) => {
    if (e.button !== 0 || !onResize) return;
    e.preventDefault();
    e.stopPropagation();
    const el = rootRef.current;
    grab.current = { id: e.pointerId, sx: e.clientX, sy: e.clientY, w: width ?? (el ? el.offsetWidth : BEHAVIORS_W), h: height ?? (el ? el.offsetHeight : BEHAVIORS_H) };
    try {
      e.currentTarget.setPointerCapture(e.pointerId);
    } catch {
      /* a synthetic event has no capture */
    }
  };
  const resizeMove = (e: ReactPointerEvent<HTMLSpanElement>) => {
    const g = grab.current;
    if (!g || g.id !== e.pointerId) return;
    const z = zoom > 0 ? zoom : 1;
    setLive({ w: Math.max(BEHAVIORS_MIN.w, Math.round(g.w + (e.clientX - g.sx) / z)), h: Math.max(BEHAVIORS_MIN.h, Math.round(g.h + (e.clientY - g.sy) / z)) });
  };
  const resizeUp = (e: ReactPointerEvent<HTMLSpanElement>) => {
    const g = grab.current;
    if (!g || g.id !== e.pointerId) return;
    grab.current = null;
    if (live && onResize) onResize(live.w, live.h);
    setLive(null);
  };
  // The host lays the board out from what it measures (plane px), as a line's.
  const measureRef = useRef(onMeasure);
  measureRef.current = onMeasure;
  useEffect(() => {
    const el = rootRef.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const read = () => {
      const w = el.offsetWidth;
      const h = el.offsetHeight;
      if (w > 0 && h > 0) measureRef.current?.(w, h);
    };
    read();
    const ro = new ResizeObserver(read);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  const headDown = (e: ReactPointerEvent) => {
    if (draggable && onHeadDown && headPress(e)) onHeadDown(e);
  };
  const groups = boardGroups(plans, now);
  const urgent = plans.filter(isUrgent).length;

  return (
    <section
      ref={rootRef}
      className="sky-bh"
      data-tour="behaviors-board"
      aria-label="Behaviors"
      style={{ width: frameNow.w > 0 ? frameNow.w : undefined, height: frameNow.h > 0 ? frameNow.h : undefined }}
    >
      <div className="sky-bh-bar" data-sky-head title={draggable && onHeadDown ? "Drag to move the Behaviors board" : undefined} onPointerDown={headDown}>
        <span className="sky-bh-title">Behaviors</span>
        {urgent > 0 && (
          <span className="sky-bh-count" title={urgent + (urgent === 1 ? " needs" : " need") + " you or failed"}>
            {urgent}
          </span>
        )}
        <span className="sky-bh-tools">
          <BoardSettingsButton />
          {onHide && (
            <button type="button" className="fl-icon-btn sky-bh-hide" aria-label="Hide the Behaviors board" title="Hide the Behaviors board — ⋯ › Show the Behaviors board brings it back" onClick={onHide}>
              ×
            </button>
          )}
        </span>
      </div>
      <p className="sky-bh-explain">{BEHAVIORS_EXPLAIN}</p>
      <div className="sky-bh-rows sky-own-scroll">
        {!groups.length ? (
          <p className="sky-bh-empty">Nothing here: every session's behaviors are on its frame in the Sky.</p>
        ) : (
          groups.map((g) => (
            <section key={g.tone} className={"sky-bh-group is-" + g.tone} aria-label={g.label}>
              <h4 className="sky-bh-group-head">
                {g.label}
                <span className="sky-bh-group-n">{g.plans.length}</span>
              </h4>
              <ul className="sky-bh-list">
                {g.plans.map((p) => (
                  <BoardRow key={p.id} plan={p} open={expanded === p.id} onToggle={() => onExpand(expanded === p.id ? "" : p.id)} />
                ))}
              </ul>
            </section>
          ))
        )}
      </div>
      {onResize && (
        <span
          className="sky-bh-resize skl-resize"
          role="separator"
          tabIndex={0}
          data-no-drag
          aria-label="Resize the Behaviors board"
          title="Drag to resize the Behaviors board"
          onPointerDown={resizeDown}
          onPointerMove={resizeMove}
          onPointerUp={resizeUp}
          onPointerCancel={resizeUp}
        />
      )}
    </section>
  );
}

function BoardRow({ plan, open, onToggle }: { plan: TestPlan; open: boolean; onToggle(): void }) {
  const s = behaviorSummary(plan);
  return (
    <li className={"sky-bh-item is-" + s.tone} data-bh-row={plan.id}>
      <button type="button" className="fl-check-row sky-bh-row" aria-expanded={open} title={open ? "Fold these behaviors" : "Open these behaviors"} onClick={onToggle}>
        <span className={"sky-bh-dot is-" + s.tone} aria-hidden="true" />
        <span className="sky-bh-name">{planName(plan)}</span>
        <span className="sky-bh-short">{s.short}</span>
      </button>
      {plan.branch && <code className="sky-bh-branch">{plan.branch}</code>}
      {open && (
        <div className="sky-bh-card" data-no-drag>
          <BehaviorsCard plan={plan} compact onClose={onToggle} />
        </div>
      )}
    </li>
  );
}

/** The board's ⋯: the settings the old Verify dialog's header held, in a
 * popover on the page (a portal: the plane's zoom never shrinks it). */
function BoardSettingsButton() {
  const [open, setOpen] = useState(false);
  const [at, setAt] = useState<{ top?: number; bottom?: number; right: number } | null>(null);
  const btn = useRef<HTMLButtonElement>(null);
  const pop = useRef<HTMLDivElement>(null);
  const close = useCallback((refocus: boolean) => {
    setOpen(false);
    setAt(null);
    if (refocus) btn.current?.focus();
  }, []);
  // Under the button, right edges aligned; above it when the window is short.
  useLayoutEffect(() => {
    if (!open || !btn.current) return;
    const r = btn.current.getBoundingClientRect();
    const right = Math.max(8, window.innerWidth - r.right);
    setAt(r.bottom + 360 > window.innerHeight && r.top > 360 ? { bottom: window.innerHeight - r.top + 4, right } : { top: r.bottom + 4, right });
  }, [open]);
  useEffect(() => {
    if (!open) return;
    const onDown = (e: Event) => {
      const t = e.target as Node;
      if (pop.current?.contains(t) || btn.current?.contains(t)) return;
      close(false);
    };
    // Capture + stop: the sky's own Escape must not also clear its selection.
    const onEsc = (e: globalThis.KeyboardEvent) => {
      if (e.key !== "Escape") return;
      e.preventDefault();
      e.stopPropagation();
      close(true);
    };
    document.addEventListener("pointerdown", onDown, true);
    document.addEventListener("keydown", onEsc, true);
    return () => {
      document.removeEventListener("pointerdown", onDown, true);
      document.removeEventListener("keydown", onEsc, true);
    };
  }, [open, close]);
  return (
    <>
      <button
        ref={btn}
        type="button"
        className={"fl-icon-btn sky-bh-more" + (open ? " is-open" : "")}
        aria-haspopup="dialog"
        aria-expanded={open}
        aria-label="Behaviors settings"
        title="Behaviors settings: write them on every push, watched repositories, check another branch"
        onClick={() => (open ? close(false) : setOpen(true))}
      >
        ⋯
      </button>
      {open &&
        at &&
        typeof document !== "undefined" &&
        createPortal(
          <div
            ref={pop}
            className="sky-bh-pop"
            role="dialog"
            aria-label="Behaviors settings"
            style={{ top: at.top, bottom: at.bottom, right: at.right }}
            data-no-drag
            onPointerDown={(e) => e.stopPropagation()}
          >
            <BehaviorsSettings onClose={() => close(true)} />
          </div>,
          document.body
        )}
    </>
  );
}
