/** The React half of the list dialogs' multi-select: one hook plus the two bits
 * of chrome (a row checkbox, a bulk bar) that Recently-closed and Workspaces
 * share. Kept together because the three only make sense as a set.
 *
 * Deliberately NOT named `bulk-cb` — tests/unit/test_frontend_bulk.py pins that
 * the SIDEBAR has no per-row checkbox, and a dialog class by that name would
 * make the guard lie. */

import { useCallback, useMemo, useRef, useState } from "react";
import {
  applyKeys,
  rangeBetween,
  selectAllState,
  selectedInOrder,
  type SelectAllState,
} from "../../lib/rowSelection";

export interface RowSelection {
  /** Selected, still-existing keys in list order. */
  keys: string[];
  has: (key: string) => boolean;
  /** Click handler for a row box. `shift` extends from the last box clicked. */
  toggle: (key: string, shift?: boolean) => void;
  /** The header box: applies to the rows currently on screen. */
  setAllVisible: (on: boolean) => void;
  allState: SelectAllState;
  clear: () => void;
  /** Selected rows the filter is currently hiding — surfaced in the bar so a
   * bulk delete can never quietly reach past what's on screen. */
  hiddenCount: number;
}

/**
 * @param allKeys      every selectable row's key, in list order
 * @param visibleKeys  the subset the filter is showing, in display order
 */
export function useRowSelection(allKeys: string[], visibleKeys: string[]): RowSelection {
  const [selected, setSelected] = useState<Set<string>>(() => new Set());
  const anchor = useRef<string | null>(null);
  // Read from the render's own arrays, but through refs so the callbacks below
  // stay stable for the whole dialog's life.
  const visibleRef = useRef(visibleKeys);
  visibleRef.current = visibleKeys;

  const keys = useMemo(() => selectedInOrder(selected, allKeys), [selected, allKeys]);
  const hiddenCount = useMemo(() => {
    const vis = new Set(visibleKeys);
    return keys.filter((k) => !vis.has(k)).length;
  }, [keys, visibleKeys]);

  const toggle = useCallback((key: string, shift?: boolean) => {
    setSelected((prev) => {
      if (shift && anchor.current) {
        // A shift-range always turns ON: "extend the selection" is what every
        // list does, and a range that sometimes clears is a trap.
        return applyKeys(prev, rangeBetween(visibleRef.current, anchor.current, key), true);
      }
      anchor.current = key;
      return applyKeys(prev, [key], !prev.has(key));
    });
  }, []);

  const setAllVisible = useCallback((on: boolean) => {
    anchor.current = null;
    setSelected((prev) => applyKeys(prev, visibleRef.current, on));
  }, []);

  const clear = useCallback(() => {
    anchor.current = null;
    setSelected((prev) => (prev.size ? new Set() : prev));
  }, []);

  return {
    keys,
    has: (k) => selected.has(k),
    toggle,
    setAllVisible,
    allState: selectAllState(selected, visibleKeys),
    clear,
    hiddenCount,
  };
}

/** A row's select box. Click is stopPropagation'd so a row that later becomes
 * clickable can't fight the checkbox. */
export function RowCheck({
  checked,
  disabled,
  title,
  onToggle,
}: {
  checked: boolean;
  disabled?: boolean;
  title: string;
  onToggle: (shift: boolean) => void;
}) {
  return (
    <input
      className="row-check"
      type="checkbox"
      checked={checked}
      disabled={disabled}
      title={title}
      aria-label={title}
      onChange={() => {
        /* driven by onClick — it carries the shift key */
      }}
      onClick={(e) => {
        e.stopPropagation();
        if (!disabled) onToggle(e.shiftKey);
      }}
    />
  );
}

/** The header "select all on screen" box, tri-state. */
export function SelectAllCheck({
  state,
  onChange,
  label,
}: {
  state: SelectAllState;
  onChange: (on: boolean) => void;
  label: string;
}) {
  return (
    <input
      className="row-check row-check-all"
      type="checkbox"
      checked={state === "all"}
      // "some" must render as a dash, not a tick: a ticked box over a partial
      // selection is how people delete rows they never chose.
      ref={(el) => {
        if (el) el.indeterminate = state === "some";
      }}
      title={label}
      aria-label={label}
      onChange={(e) => onChange(e.target.checked)}
    />
  );
}

/** Bulk action bar — only mounted when something is selected. Actions are
 * supplied by the dialog; Clear and the count live here. */
export function BulkRowBar({
  count,
  hiddenCount,
  noun,
  onClear,
  children,
}: {
  count: number;
  hiddenCount: number;
  noun: string;
  onClear: () => void;
  children: React.ReactNode;
}) {
  return (
    <div className="dlg-bulk">
      <span className="bulk-count">
        {count} {noun}
        {count === 1 ? "" : "s"} selected
        {hiddenCount ? ` · ${hiddenCount} hidden by the filter` : ""}
      </span>
      <div className="bulk-acts">
        {children}
        <button type="button" title="Clear selection" onClick={onClear}>
          Clear
        </button>
      </div>
    </div>
  );
}
