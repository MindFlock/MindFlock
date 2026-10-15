/** Confirmation toast (port of app.js toast()). Kept as a plain DOM singleton
 * rather than React state: it is published on the public extension API
 * (window.mindflock.toast — addons feature-detect it) and must work from any
 * context, including non-React callbacks. */

let toastTimer: ReturnType<typeof setTimeout> | undefined;

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
}

/** How long an action toast stays up after the pointer or focus leaves it. */
const ACTION_LINGER_MS = 2000;

export function toast(msg: string, opts?: ToastOpts) {
  const o = opts || {};
  let el = document.getElementById("cs-toast");
  if (!el) {
    el = document.createElement("div");
    el.id = "cs-toast";
    document.body.appendChild(el);
  }
  const box = el as HTMLElement;
  const hide = () => {
    clearTimeout(toastTimer);
    box.classList.remove("show", "clickable", "has-action");
    box.onclick = null;
  };
  // An action toast holds still while the pointer is on it or its button has
  // focus, so reaching for the button never races the timer.
  const linger = () => {
    clearTimeout(toastTimer);
    toastTimer = setTimeout(hide, ACTION_LINGER_MS);
  };
  const hold = () => clearTimeout(toastTimer);
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
  box.onclick = o.onClick
    ? () => {
        hide();
        o.onClick!();
      }
    : null;
  box.onmouseenter = o.action ? hold : null;
  box.onmouseleave = o.action ? linger : null;
  box.classList.add("show");
  clearTimeout(toastTimer);
  const dur = o.duration || (o.onClick || o.action ? 6000 : 1400);
  toastTimer = setTimeout(() => box.classList.remove("show", "has-action"), dur);
}

/** F3: publish on the extension API (docs/extensions.md; slots.js
 * feature-detects mf.toast). events.js creates window.mindflock first. */
export function publishToast() {
  const w = window as unknown as { mindflock?: Record<string, unknown> };
  w.mindflock = w.mindflock || {};
  w.mindflock.toast = toast;
}
