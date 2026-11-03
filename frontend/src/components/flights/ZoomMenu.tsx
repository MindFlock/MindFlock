/** The zoom readout as a menu (docs/flights.md "Round 17": task
 * `zoom-shortcuts`, Figma's habit). The readout ("87 %") is a button in the
 * toolbar's zoom group; a press opens a small inline menu under it — never a
 * native select: Fit all (Shift+1 / 0), Fit selection (Shift+2, greyed with
 * nothing selected), Fit the focused line (1, greyed with none), a divider,
 * then 50 % · 100 % (Shift+0) · 150 % · 200 %, the current one ticked (the
 * nearest preset within 2 points). Each item shows its key caps on the right.
 * ↑ / ↓ / Home / End walk the items, Enter or a click picks one (and closes),
 * Esc puts the focus back on the button, a press anywhere else closes.
 *
 * The button wears the zoom group's secondary tier (`sky-zoom-btn`), the rows
 * the menu-row tier (`fl-stopmenu-item`, StopMenu.css) — flightsButtons.test
 * pins every flights button to a tier; ZoomMenu.css only adds the grid, the
 * caps and the tick. The menu is portalled to <body> at fixed coordinates: the zoom group clips
 * (`overflow: hidden`, Sky.css) and the toolbar must not grow under it.
 * The pure parts (`zoomMenuItems`, `presetAt`, `zoomMenuKey`) are exported
 * for the tests (the node test environment has no DOM). */

import { useEffect, useLayoutEffect, useRef, useState, type KeyboardEvent, type ReactElement } from "react";
import { createPortal } from "react-dom";
import "./ZoomMenu.css";

export interface ZoomMenuProps {
  zoom: number;
  min: number;
  max: number;
  onZoomTo(z: number): void;
  onFitAll(): void;
  onFitSelection(): void;
  onFitFocused(): void;
  canFitSelection: boolean;
  canFitFocused: boolean;
}

/** The menu's preset zooms, in order. */
export const ZOOM_PRESETS = [0.5, 1, 1.5, 2] as const;

/** One row of the menu. `keys` holds each way to press it (["Shift", "1"]
 * or ["0"]), drawn as caps on the right; `gapBefore` draws the divider. */
export interface ZoomMenuItem {
  id: string;
  label: string;
  keys: string[][];
  disabled: boolean;
  checked: boolean;
  gapBefore: boolean;
  run(): void;
}

/** "87 %" — the readout, a thin space before the sign like the contract's. */
export const zoomLabel = (z: number) => Math.round(z * 100) + " %";

/** The preset the zoom sits on: the nearest within 2 percentage points, or
 * null between them. */
export function presetAt(zoom: number): number | null {
  let best: number | null = null;
  for (const p of ZOOM_PRESETS) {
    const d = Math.abs(zoom - p) * 100;
    if (d <= 2 + 1e-9 && (best === null || d < Math.abs(zoom - best) * 100)) best = p;
  }
  return best;
}

/** The menu's rows for these props (the order, keys, greying and tick). */
export function zoomMenuItems(p: ZoomMenuProps): ZoomMenuItem[] {
  const on = presetAt(p.zoom);
  const items: ZoomMenuItem[] = [
    { id: "fit-all", label: "Fit all", keys: [["Shift", "1"], ["0"]], disabled: false, checked: false, gapBefore: false, run: () => p.onFitAll() },
    {
      id: "fit-selection",
      label: "Fit selection",
      keys: [["Shift", "2"]],
      disabled: !p.canFitSelection,
      checked: false,
      gapBefore: false,
      run: () => p.onFitSelection(),
    },
    {
      id: "fit-focused",
      label: "Fit the focused chain",
      keys: [["1"]],
      disabled: !p.canFitFocused,
      checked: false,
      gapBefore: false,
      run: () => p.onFitFocused(),
    },
  ];
  ZOOM_PRESETS.forEach((z, i) =>
    items.push({
      id: "zoom-" + Math.round(z * 100),
      label: zoomLabel(z),
      keys: z === 1 ? [["Shift", "0"]] : [],
      disabled: z < p.min - 1e-9 || z > p.max + 1e-9,
      checked: on === z,
      gapBefore: i === 0,
      run: () => p.onZoomTo(z),
    })
  );
  return items;
}

/** What a key does inside the open menu, given the focused row `at` (-1 =
 * none) among the `live` (enabled) rows' count: `{ to }` moves the focus,
 * "close" folds the menu back to its button, null leaves the key alone. */
export function zoomMenuKey(key: string, at: number, live: number): { to: number } | "close" | null {
  if (key === "Escape" || key === "Tab") return "close";
  if (!live) return null;
  const wrap = (j: number) => ({ to: (j + live) % live });
  if (key === "ArrowDown") return wrap(at + 1);
  if (key === "ArrowUp") return wrap(at < 0 ? -1 : at - 1);
  if (key === "Home") return { to: 0 };
  if (key === "End") return { to: live - 1 };
  return null;
}

/** The caps of one row: each way to press it, " / " between them. */
function Caps({ keys }: { keys: string[][] }) {
  if (!keys.length) return null;
  return (
    <span className="sky-zoom-menu-keys" aria-hidden="true">
      {keys.map((combo, i) => (
        <span key={i} className="sky-zoom-menu-combo">
          {i > 0 && <span className="sky-zoom-menu-or">/</span>}
          {combo.map((k) => (
            <kbd key={k}>{k}</kbd>
          ))}
        </span>
      ))}
    </span>
  );
}

/** The open menu's rows (no positioning, no listeners): what the portal
 * draws, and what the tests render. */
export function ZoomMenuList(p: ZoomMenuProps & { onPicked?(): void; onKeyDown?(e: KeyboardEvent<HTMLDivElement>): void }): ReactElement {
  return (
    <div className="sky-zoom-menu" role="menu" aria-label="Zoom" onKeyDown={p.onKeyDown}>
      {zoomMenuItems(p).map((it) => [
        it.gapBefore ? <div key={it.id + ":sep"} className="sky-zoom-menu-sep" role="separator" /> : null,
        <button
          key={it.id}
          type="button"
          role="menuitem"
          aria-current={it.checked || undefined}
          className={"fl-stopmenu-item sky-zoom-menu-item" + (it.checked ? " is-current" : "")}
          data-item={it.id}
          disabled={it.disabled}
          aria-keyshortcuts={it.keys.map((c) => c.join("+")).join(" ") || undefined}
          onClick={() => {
            it.run();
            p.onPicked?.();
          }}
        >
          <span className="sky-zoom-menu-tick" aria-hidden="true">
            {it.checked ? "✓" : ""}
          </span>
          <span className="sky-zoom-menu-label">{it.label}</span>
          <Caps keys={it.keys} />
        </button>,
      ])}
    </div>
  );
}

export function ZoomMenu(p: ZoomMenuProps): ReactElement {
  const [open, setOpen] = useState(false);
  const [at, setAt] = useState<{ top?: number; bottom?: number; right: number } | null>(null);
  const btn = useRef<HTMLButtonElement>(null);
  const menu = useRef<HTMLDivElement>(null);
  const live = () => [...(menu.current?.querySelectorAll<HTMLButtonElement>("button[role=menuitem]:not(:disabled)") || [])];

  const close = (refocus: boolean) => {
    setOpen(false);
    setAt(null);
    if (refocus) btn.current?.focus();
  };

  // Under the button, right edges aligned; above it when the window is short.
  useLayoutEffect(() => {
    if (!open || !btn.current) return;
    const r = btn.current.getBoundingClientRect();
    const right = Math.max(8, window.innerWidth - r.right);
    setAt(r.bottom + 280 > window.innerHeight && r.top > 280 ? { bottom: window.innerHeight - r.top + 4, right } : { top: r.bottom + 4, right });
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const first = live().find((b) => b.classList.contains("is-current")) || live()[0];
    first?.focus();
    const onDown = (e: Event) => {
      const t = e.target as Node;
      if (menu.current?.contains(t) || btn.current?.contains(t)) return;
      close(false);
    };
    // Capture + stop: the sky's own Escape must not also clear its selection.
    const onEsc = (e: globalThis.KeyboardEvent) => {
      if (e.key !== "Escape") return;
      e.preventDefault();
      e.stopPropagation();
      close(true);
    };
    const onAway = () => close(false);
    document.addEventListener("pointerdown", onDown, true);
    document.addEventListener("keydown", onEsc, true);
    window.addEventListener("resize", onAway);
    window.addEventListener("blur", onAway);
    return () => {
      document.removeEventListener("pointerdown", onDown, true);
      document.removeEventListener("keydown", onEsc, true);
      window.removeEventListener("resize", onAway);
      window.removeEventListener("blur", onAway);
    };
  }, [open, at]); // eslint-disable-line react-hooks/exhaustive-deps

  const onKey = (e: KeyboardEvent<HTMLDivElement>) => {
    const all = live();
    const r = zoomMenuKey(e.key, all.indexOf(document.activeElement as HTMLButtonElement), all.length);
    if (!r) return;
    e.preventDefault();
    e.stopPropagation();
    if (r === "close") close(true);
    else all[r.to]?.focus();
  };

  const onBtnKey = (e: KeyboardEvent<HTMLButtonElement>) => {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      setOpen(true);
    }
  };

  const label = zoomLabel(p.zoom);
  return (
    <span className="sky-zoom-menu-wrap">
      <button
        ref={btn}
        type="button"
        className={"sky-zoom-btn sky-zoom-read sky-zoom-menu-btn" + (open ? " is-open" : "")}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={"Zoom " + label + " — fit and presets"}
        title="Zoom: fit and presets"
        onClick={() => (open ? close(false) : setOpen(true))}
        onKeyDown={onBtnKey}
      >
        {label}
        <span className="sky-zoom-menu-caret" aria-hidden="true">
          ▾
        </span>
      </button>
      {open &&
        at &&
        createPortal(
          <div ref={menu} className="sky-zoom-menu-pop" style={{ top: at.top, bottom: at.bottom, right: at.right }} data-no-drag onPointerDown={(e) => e.stopPropagation()}>
            <ZoomMenuList {...p} onPicked={() => close(true)} onKeyDown={onKey} />
          </div>,
          document.body
        )}
    </span>
  );
}
