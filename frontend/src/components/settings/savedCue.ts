/** The small inline "✓ Saved" next to the control you just changed.
 *
 * A save used to say "Saved" in a toast at the bottom of the window — for
 * text fields only; toggles said nothing, or a different sentence. Now every
 * save in Settings marks the changed control's own label for a moment
 * (`data-saved` → a "✓ Saved" after the label, settingsMode.css), so the
 * feedback sits where the eye already is, the same way for every control.
 *
 * Which control? A save through `useSettings().saveField` doesn't know its
 * element, so SettingsDialog notes the last control touched (change or blur,
 * captured on the screens pane) and the save marks that one. A screen that
 * saves through its own API call passes its element straight in. No element
 * (or one no longer on screen) → `false`, and the caller falls back to a
 * toast. */

const ROW = ".set-row, .notif-channel, .ntfy-field-row, .set-switch-row";
const SHOW_MS = 1600;
/** A control touched longer ago than this is not what a save is about. */
const FRESH_MS = 5000;

let touched: { el: Element; at: number } | null = null;
const timers = new WeakMap<Element, ReturnType<typeof setTimeout>>();

/** Remember `target` as the control the next save is about. */
export function noteTouched(target: EventTarget | null): void {
  if (target && typeof (target as Element).closest === "function") {
    touched = { el: target as Element, at: Date.now() };
  }
}

/** The label to mark for `el`: its row's `.set-label`, else the row. */
function cueTarget(el: Element): Element | null {
  const row = el.closest(ROW);
  if (!row) return null;
  return row.querySelector(".set-label") || row;
}

/** Show "✓ Saved" by `el` (default: the last touched control). */
export function flashSaved(el?: Element | null): boolean {
  const source = el ?? (touched && Date.now() - touched.at < FRESH_MS ? touched.el : null);
  if (!source || !source.isConnected) return false;
  const target = cueTarget(source);
  if (!target) return false;
  target.setAttribute("data-saved", "");
  const prev = timers.get(target);
  if (prev) clearTimeout(prev);
  timers.set(
    target,
    setTimeout(() => target.removeAttribute("data-saved"), SHOW_MS)
  );
  return true;
}
