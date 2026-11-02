/** A box's right-click menu on the flyway canvas (lib/stopEditing
 * `menuItemsFor` says what is in it): drawn at the pointer inside the line's
 * stage, so it pans and zooms with the line — counter-scaled by the Sky's
 * `--sky-z` to stay screen-sized. ↑ / ↓ move between items, Enter presses
 * one, Esc or a press anywhere else closes it. Never a native menu: the
 * desktop app has none worth showing here. */

import { useEffect, useRef, type KeyboardEvent } from "react";
import type { StopMenuItem, StopMenuKey } from "../../lib/stopEditing";
import "./StopMenu.css";

/** The menu's keys: lib/stopEditing's, plus the canvas's own `rewind` (round
 * 15: a flown checkpoint's "Rewind to here", confirmed inline on the stage). */
export type SkyMenuKey = StopMenuKey | "rewind";

/** One line of the menu: lib/stopEditing's item, or the canvas's own. */
export type SkyMenuItem = Omit<StopMenuItem, "key"> & { key: SkyMenuKey };

export interface StopMenuProps {
  /** Where it opens, in the stage's own (unzoomed) pixels. */
  x: number;
  y: number;
  /** Its accessible name ("“Push” — what you can do"). */
  label: string;
  items: readonly SkyMenuItem[];
  onPick(key: SkyMenuKey): void;
  /** Esc (`returnFocus`: put the focus back on the box) or a press outside. */
  onClose(returnFocus: boolean): void;
}

export function StopMenu(p: StopMenuProps) {
  const ref = useRef<HTMLDivElement>(null);
  const live = () => [...(ref.current?.querySelectorAll<HTMLButtonElement>("button[role=menuitem]:not(:disabled)") || [])];

  useEffect(() => {
    live()[0]?.focus();
    const onDown = (e: Event) => {
      if (ref.current && !ref.current.contains(e.target as Node)) p.onClose(false);
    };
    // Capture + stopPropagation: the Sky's (and a dialog's) own Escape must
    // not also close the open box under the menu.
    const onKey = (e: globalThis.KeyboardEvent) => {
      if (e.key !== "Escape") return;
      e.stopPropagation();
      e.preventDefault();
      p.onClose(true);
    };
    document.addEventListener("pointerdown", onDown, true);
    document.addEventListener("keydown", onKey, true);
    return () => {
      document.removeEventListener("pointerdown", onDown, true);
      document.removeEventListener("keydown", onKey, true);
    };
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const onKey = (e: KeyboardEvent<HTMLDivElement>) => {
    const all = live();
    if (!all.length) return;
    const i = all.indexOf(document.activeElement as HTMLButtonElement);
    const go = (j: number) => {
      e.preventDefault();
      all[(j + all.length) % all.length].focus();
    };
    if (e.key === "ArrowDown") go(i + 1);
    else if (e.key === "ArrowUp") go(i < 0 ? -1 : i - 1);
    else if (e.key === "Home") go(0);
    else if (e.key === "End") go(-1);
    else if (e.key === "Tab") {
      e.preventDefault();
      p.onClose(true);
    }
  };

  return (
    <div
      ref={ref}
      className="fl-stopmenu"
      role="menu"
      aria-label={p.label}
      style={{ left: p.x, top: p.y }}
      data-no-drag
      onKeyDown={onKey}
      onPointerDown={(e) => e.stopPropagation()}
      onClick={(e) => e.stopPropagation()}
      onContextMenu={(e) => e.preventDefault()}
    >
      {p.items.map((it, i) =>
        it.note ? (
          <div key={"note" + i} className="fl-stopmenu-note" role="menuitem" aria-disabled="true" title={it.title}>
            {it.label}
          </div>
        ) : (
          <button
            key={it.key}
            type="button"
            role="menuitem"
            className={"fl-stopmenu-item" + (it.danger ? " danger" : "")}
            disabled={it.disabled}
            title={it.title}
            onClick={() => p.onPick(it.key)}
          >
            {it.label}
          </button>
        )
      )}
    </div>
  );
}
