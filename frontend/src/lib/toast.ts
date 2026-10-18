/** Confirmation toast (port of app.js toast()). Kept as a plain DOM singleton
 * rather than React state: it is published on the public extension API
 * (window.mindflock.toast — addons feature-detect it) and must work from any
 * context, including non-React callbacks.
 *
 * Two slots at most. The bottom one (#cs-toast) is the newest message. An
 * action toast still on screen — the Close toast's Undo — is never replaced by
 * a plain message: it steps up into the held slot (#cs-toast-held) and keeps
 * its button and its own timer, and the message takes the bottom slot. A new
 * action toast does replace an older one (one Undo at a time). */

/** Each slot's hide timer, so the held toast keeps counting down on its own. */
const timers = new WeakMap<HTMLElement, ReturnType<typeof setTimeout>>();

const MAIN_ID = "cs-toast";
const HELD_ID = "cs-toast-held";

/** A real button inside the toast — the thing to press, rather than the whole
 * toast being the click target. */
export interface ToastAction {
  label: string;
  onClick(): void;
  /** What a screen reader announces for the button; defaults to the label
   * followed by the toast's message, so "Undo" says what it undoes. */
  ariaLabel?: string;
}

export interface ToastOpts {
  /** The whole toast is the click target (the older shape; kept for callers
   * that use it). */
  onClick?: () => void;
  /** A button inside the toast; pressing it closes the toast. */
  action?: ToastAction;
  /** Tooltip for the toast (e.g. the action's keyboard shortcut). */
  title?: string;
  duration?: number;
  /** Something went wrong: announced as an alert, and up a little longer.
   * Defaults to what the words say (`looksLikeError`). */
  error?: boolean;
}

/** How long an action toast stays up after the pointer or focus leaves it. */
const ACTION_LINGER_MS = 2000;

/** Whether a message reads as a failure ("Send failed: …", "Couldn't copy") —
 * the toasts that say so are plain strings, so the words decide. */
export function looksLikeError(msg: string): boolean {
  return /^(couldn[’']t|could not|can[’']t|cannot|error\b)|\bfailed\b/i.test(String(msg || "").trim());
}

/** How long a toast stays up: long enough to read (~22 characters a second,
 * plus a beat to look), never under 1.4s — 2.5s for an error, 6s for one with
 * something to press — and never over 10s. An explicit `duration` wins. */
export function toastDuration(
  msg: string,
  o: Pick<ToastOpts, "duration" | "action" | "onClick" | "error"> = {}
): number {
  if (o.duration) return o.duration;
  const err = o.error ?? looksLikeError(msg);
  const floor = o.action || o.onClick ? 6000 : err ? 2500 : 1400;
  const read = 600 + 45 * String(msg || "").length;
  return Math.min(10000, Math.max(floor, read));
}

/** Take a toast down; a held one leaves the page once it has faded. */
function retire(box: HTMLElement) {
  clearTimeout(timers.get(box));
  box.classList.remove("show", "clickable", "has-action");
  box.onclick = null;
  if (box.id === HELD_ID) setTimeout(() => box.remove?.(), 200);
}

function setTimer(box: HTMLElement, ms: number) {
  clearTimeout(timers.get(box));
  timers.set(
    box,
    setTimeout(() => retire(box), ms)
  );
}

function dropHeld() {
  const held = document.getElementById(HELD_ID);
  if (!held) return;
  clearTimeout(timers.get(held as HTMLElement));
  held.remove?.();
}

export function toast(msg: string, opts?: ToastOpts) {
  const o = opts || {};
  let el = document.getElementById(MAIN_ID);
  if (el && o.action) {
    // A new Undo replaces the old one, held or not.
    dropHeld();
  } else if (el && el.classList.contains("has-action") && el.classList.contains("show")) {
    // The action toast still up keeps its button: it steps up a slot.
    dropHeld();
    el.id = HELD_ID;
    el.classList.add("held");
    el = null;
  }
  if (!el) {
    el = document.createElement("div");
    el.id = MAIN_ID;
    document.body.appendChild(el);
  }
  const box = el as HTMLElement;
  const err = o.error ?? looksLikeError(msg);
  // A live region: read out when it changes — politely, or at once for an error.
  box.setAttribute?.("role", err ? "alert" : "status");
  box.setAttribute?.("aria-live", err ? "assertive" : "polite");
  const hide = () => retire(box);
  // An action toast holds still while the pointer is on it or its button has
  // focus, so reaching for the button never races the timer.
  const linger = () => setTimer(box, ACTION_LINGER_MS);
  const hold = () => clearTimeout(timers.get(box));
  if (o.action) {
    const act = o.action;
    const text = document.createElement("span");
    text.className = "cs-toast-msg";
    text.textContent = msg;
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "cs-toast-act";
    btn.textContent = act.label;
    btn.setAttribute("aria-label", act.ariaLabel || act.label + ": " + msg);
    if (o.title) btn.title = o.title;
    btn.onfocus = hold;
    btn.onblur = linger;
    btn.onclick = (e) => {
      e.stopPropagation();
      hide();
      act.onClick();
    };
    box.replaceChildren(text, btn);
  } else {
    box.textContent = msg;
  }
  box.title = o.title || "";
  box.classList.toggle("clickable", !!o.onClick);
  box.classList.toggle("has-action", !!o.action);
  box.classList.toggle("is-error", err);
  box.onclick = o.onClick
    ? () => {
        hide();
        o.onClick!();
      }
    : null;
  box.onmouseenter = o.action ? hold : null;
  box.onmouseleave = o.action ? linger : null;
  box.classList.add("show");
  setTimer(box, toastDuration(msg, o));
}

/** F3: publish on the extension API (docs/extensions.md; slots.js
 * feature-detects mf.toast). events.js creates window.mindflock first. */
export function publishToast() {
  const w = window as unknown as { mindflock?: Record<string, unknown> };
  w.mindflock = w.mindflock || {};
  w.mindflock.toast = toast;
}
