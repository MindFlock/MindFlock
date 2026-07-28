/** Confirmation toast (port of app.js toast()). Kept as a plain DOM singleton
 * rather than React state: it is published on the public extension API
 * (window.mindflock.toast — addons feature-detect it) and must work from any
 * context, including non-React callbacks. */

let toastTimer: ReturnType<typeof setTimeout> | undefined;

export interface ToastOpts {
  onClick?: () => void;
  duration?: number;
}

export function toast(msg: string, opts?: ToastOpts) {
  const o = opts || {};
  let el = document.getElementById("cs-toast");
  if (!el) {
    el = document.createElement("div");
    el.id = "cs-toast";
    document.body.appendChild(el);
  }
  el.textContent = msg;
  el.classList.toggle("clickable", !!o.onClick);
  (el as HTMLElement).onclick = o.onClick
    ? () => {
        el!.classList.remove("show", "clickable");
        (el as HTMLElement).onclick = null;
        o.onClick!();
      }
    : null;
  el.classList.add("show");
  clearTimeout(toastTimer);
  const dur = o.duration || (o.onClick ? 6000 : 1400);
  toastTimer = setTimeout(() => el!.classList.remove("show"), dur);
}

/** F3: publish on the extension API (docs/extensions.md; slots.js
 * feature-detects mf.toast). events.js creates window.mindflock first. */
export function publishToast() {
  const w = window as unknown as { mindflock?: Record<string, unknown> };
  w.mindflock = w.mindflock || {};
  w.mindflock.toast = toast;
}
