/** A circled-i button that shows a control's longer explanation in a bubble
 * while you hover or focus it — the app's one "more about this" affordance.
 *
 * It replaced the "Learn more" folds (a <details> under a hint): a fold that
 * opens pushes every row below it down the screen, and a second click to put
 * it away is a chore for what is only ever a sentence or three.
 *
 * Behaviour:
 *  - Mouse: hover the icon opens it (after a short delay, so a pointer passing
 *    over does not flash it); leaving closes it after a 100ms grace, so the
 *    pointer can cross the gap onto the bubble — which stays open while
 *    hovered, so a link in it is reachable.
 *  - Keyboard: focus opens it (not a focus that came from a press — Chrome
 *    focuses a clicked button, and a mouse user's tip must close on leave);
 *    Enter/Space toggles it; Escape closes it and nothing else (the window
 *    capture listener stops the key before a dialog's own Escape sees it);
 *    blur closes it.
 *  - Touch: a tap toggles it, a tap anywhere else closes it.
 *
 * The bubble renders into document.body with fixed positioning, so a
 * scrolling Settings screen or a card's overflow never clips it; it flips
 * above the icon when there is no room below and shifts sideways to stay in
 * the window, the arrow still pointing at the icon (placeTip). The text also
 * stays in place, visually hidden, as the button's description: a screen
 * reader hears it without opening anything, and it is in the DOM wherever
 * the control is.
 *
 * DOM only — no window.prompt/alert/confirm, nothing Electron can't draw.
 * Styles: components/InfoTip.css. */

import {
  useCallback,
  useEffect,
  useId,
  useLayoutEffect,
  useReducer,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { createPortal } from "react-dom";

/** Why the tip is open; it is open while any of these holds. */
export interface TipState {
  hover: boolean;
  focus: boolean;
  /** Tapped open (touch) or opened from the keyboard: stays until dismissed. */
  pinned: boolean;
}

export type TipEvent =
  | "enter" // the mouse is over the icon or the bubble (after the open delay)
  | "leave" // the mouse left both, and the grace ran out
  | "focus" // keyboard focus reached the icon
  | "blur" // focus left the icon (and not into the bubble)
  | "tap" // a touch tap on the icon
  | "activate" // Enter / Space on the icon
  | "escape"
  | "outside"; // a press somewhere that is neither the icon nor the bubble

export const TIP_CLOSED: TipState = { hover: false, focus: false, pinned: false };

export const tipOpen = (s: TipState): boolean => s.hover || s.focus || s.pinned;

/** The tip's whole state machine, pure so the tests can drive it without a
 * DOM. A toggle that closes clears every reason at once: a tap that shuts a
 * tip the mouse opened must not leave it "still hovered" and open. */
export function tipReduce(s: TipState, e: TipEvent): TipState {
  switch (e) {
    case "enter":
      return s.hover ? s : { ...s, hover: true };
    case "leave":
      return s.hover ? { ...s, hover: false } : s;
    case "focus":
      return s.focus ? s : { ...s, focus: true };
    case "blur":
      return s.focus || s.pinned ? { ...s, focus: false, pinned: false } : s;
    case "tap":
    case "activate":
      return tipOpen(s) ? TIP_CLOSED : { ...s, pinned: true };
    case "escape":
    case "outside":
      return tipOpen(s) ? TIP_CLOSED : s;
  }
}

/** How long a hover waits before opening, and how long a leave waits before
 * closing (the grace that lets the pointer cross onto the bubble). */
export const TIP_OPEN_MS = 90;
export const TIP_CLOSE_MS = 100;

export interface Rect {
  top: number;
  bottom: number;
  left: number;
  right: number;
}

export interface TipPlace {
  top: number;
  left: number;
  side: "below" | "above";
  /** The arrow's centre, from the bubble's left edge. */
  arrowX: number;
}

/** Where the bubble goes: below the icon when it fits, above when only that
 * fits, else whichever side has more room (clamped into the window). Shifted
 * sideways to stay `margin` inside the window; the arrow keeps pointing at
 * the icon, but never closer than 12px to a rounded corner. */
export function placeTip(
  anchor: Rect,
  size: { w: number; h: number },
  view: { w: number; h: number },
  gap = 8,
  margin = 8
): TipPlace {
  const below = anchor.bottom + gap;
  const above = anchor.top - gap - size.h;
  const fitsBelow = below + size.h <= view.h - margin;
  const fitsAbove = above >= margin;
  let side: TipPlace["side"];
  if (fitsBelow) side = "below";
  else if (fitsAbove) side = "above";
  else side = anchor.top > view.h - anchor.bottom ? "above" : "below";
  const maxTop = Math.max(margin, view.h - margin - size.h);
  const top = Math.min(maxTop, Math.max(margin, side === "below" ? below : above));
  const cx = (anchor.left + anchor.right) / 2;
  const maxLeft = Math.max(margin, view.w - margin - size.w);
  const left = Math.min(maxLeft, Math.max(margin, cx - size.w / 2));
  const arrowX = Math.min(Math.max(12, cx - left), Math.max(12, size.w - 12));
  return { top: Math.round(top), left: Math.round(left), side, arrowX: Math.round(arrowX) };
}

export interface InfoTipProps {
  /** The explanation. Plain inline content: text, <strong>, <code>, a link. */
  children: ReactNode;
  /** The button's accessible name. */
  label?: string;
  /** An id for the button (the visually hidden description takes `id + "-tip"`). */
  id?: string;
  className?: string;
}

export function InfoTip({ children, label = "More info", id, className }: InfoTipProps) {
  const autoId = useId();
  const tipId = (id || "infotip" + autoId.replace(/[^a-zA-Z0-9_-]/g, "")) + "-tip";
  const [state, dispatch] = useReducer(tipReduce, TIP_CLOSED);
  const open = tipOpen(state);
  const btnRef = useRef<HTMLButtonElement | null>(null);
  const bubbleRef = useRef<HTMLDivElement | null>(null);
  const timer = useRef<number | undefined>(undefined);
  // A press is about to focus the button: that focus is the mouse's (or a
  // finger's), not the keyboard's, so it must not open the tip by itself.
  const pressed = useRef(false);
  const lastPointer = useRef<string>("");
  const [place, setPlace] = useState<TipPlace | null>(null);

  const later = useCallback((e: TipEvent, ms: number) => {
    window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => dispatch(e), ms);
  }, []);
  const now = useCallback((e: TipEvent) => {
    window.clearTimeout(timer.current);
    dispatch(e);
  }, []);
  useEffect(() => () => window.clearTimeout(timer.current), []);

  // Mouse in/out of either the icon or the bubble. Touch sends pointerenter
  // too (Chrome, before the tap's click) — that is the tap's job, not hover's.
  const enter = (e: React.PointerEvent) => {
    if (e.pointerType === "touch") return;
    if (state.hover) window.clearTimeout(timer.current);
    else later("enter", TIP_OPEN_MS);
  };
  const leave = (e: React.PointerEvent) => {
    if (e.pointerType === "touch") return;
    later("leave", TIP_CLOSE_MS);
  };

  // Position once the bubble has a size, and again whenever the page under
  // it moves (a Settings screen scrolls, the window resizes).
  useLayoutEffect(() => {
    if (!open) {
      setPlace(null);
      return;
    }
    const measure = () => {
      const a = btnRef.current;
      const b = bubbleRef.current;
      if (!a || !b) return;
      const r = a.getBoundingClientRect();
      setPlace(
        placeTip(r, { w: b.offsetWidth, h: b.offsetHeight }, { w: window.innerWidth, h: window.innerHeight })
      );
    };
    measure();
    let raf = 0;
    const again = () => {
      cancelAnimationFrame(raf);
      raf = requestAnimationFrame(measure);
    };
    window.addEventListener("scroll", again, true);
    window.addEventListener("resize", again);
    return () => {
      cancelAnimationFrame(raf);
      window.removeEventListener("scroll", again, true);
      window.removeEventListener("resize", again);
    };
  }, [open]);

  // While open: Escape closes the tip (first, and only the tip), and a press
  // outside both the icon and the bubble closes it.
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      e.preventDefault();
      e.stopPropagation();
      now("escape");
    };
    const onDown = (e: PointerEvent) => {
      const t = e.target as Node | null;
      if (t && (btnRef.current?.contains(t) || bubbleRef.current?.contains(t))) return;
      now("outside");
    };
    window.addEventListener("keydown", onKey, true);
    document.addEventListener("pointerdown", onDown, true);
    return () => {
      window.removeEventListener("keydown", onKey, true);
      document.removeEventListener("pointerdown", onDown, true);
    };
  }, [open, now]);

  return (
    <>
      <button
        type="button"
        ref={btnRef}
        id={id}
        className={"infotip-btn" + (open ? " open" : "") + (className ? " " + className : "")}
        aria-label={label}
        aria-describedby={tipId}
        aria-expanded={open}
        onPointerEnter={enter}
        onPointerLeave={leave}
        onPointerDown={(e) => {
          pressed.current = true;
          lastPointer.current = e.pointerType;
        }}
        onFocus={() => {
          if (pressed.current) pressed.current = false;
          else now("focus");
        }}
        onBlur={(e) => {
          pressed.current = false;
          if (bubbleRef.current && e.relatedTarget && bubbleRef.current.contains(e.relatedTarget as Node)) return;
          now("blur");
        }}
        onClick={(e) => {
          // Inside a <label> or a <summary>, a click would also flip the
          // control it belongs to.
          e.preventDefault();
          e.stopPropagation();
          if (e.detail === 0) now("activate");
          else if (lastPointer.current === "touch") now("tap");
          else if (!state.hover) now("enter");
          lastPointer.current = "";
        }}
      >
        <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true" focusable="false">
          <circle cx="8" cy="8" r="6.75" fill="none" stroke="currentColor" strokeWidth="1.4" />
          <circle cx="8" cy="5" r="0.95" fill="currentColor" />
          <path d="M8 7.4v4.2" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
        </svg>
      </button>
      <span id={tipId} role="tooltip" className="infotip-sr">
        {children}
      </span>
      {open &&
        typeof document !== "undefined" &&
        createPortal(
          <div
            ref={bubbleRef}
            className="infotip"
            data-side={place?.side || "below"}
            // Hidden from the accessibility tree: the in-place copy above is
            // the description, and reading both would say it twice.
            aria-hidden="true"
            style={{
              top: place ? place.top : -9999,
              left: place ? place.left : -9999,
              visibility: place ? undefined : "hidden",
              ["--infotip-arrow-x" as string]: (place?.arrowX ?? 16) + "px",
            }}
            onPointerEnter={enter}
            onPointerLeave={leave}
          >
            {children}
          </div>,
          document.body
        )}
    </>
  );
}
