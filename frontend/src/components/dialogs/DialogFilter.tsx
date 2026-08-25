/** The Ctrl+F filter box for the list dialogs (Recently closed, Workspaces on
 * disk). Both lists grow without bound — dozens of closed sessions, every
 * worktree ever cut — so scrolling to find one row was the only way through.
 *
 * Ctrl+F (Cmd+F on a Mac) focuses and selects this input instead of opening the
 * browser's own find bar: the native one searches the rendered page, which in a
 * virtualised, ellipsised list finds the wrong thing or nothing at all. The
 * handler is registered only while the dialog is mounted, and each dialog mounts
 * exactly one of these, so the shortcut always belongs to the dialog on screen. */

import { useEffect, useRef } from "react";

interface Props {
  value: string;
  onChange: (v: string) => void;
  placeholder: string;
  /** aria-label / id stem, e.g. "recent-filter". */
  id: string;
  /** Called on Escape when the box is already empty — the dialogs use it to
   * close, so Escape is "clear, then leave" rather than a dead key. */
  onEscape?: () => void;
}

export function DialogFilter({ value, onChange, placeholder, id, onEscape }: Props) {
  const ref = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.key === "f" || e.key === "F") && (e.ctrlKey || e.metaKey) && !e.altKey) {
        e.preventDefault();
        e.stopPropagation();
        ref.current?.focus();
        ref.current?.select();
      }
    };
    // Capture, so it beats both the app's own document dispatcher and the
    // browser's find bar.
    document.addEventListener("keydown", onKey, true);
    return () => document.removeEventListener("keydown", onKey, true);
  }, []);

  return (
    <div className="dlg-filter">
      <input
        ref={ref}
        id={id}
        type="text"
        autoComplete="off"
        spellCheck={false}
        placeholder={placeholder}
        aria-label={placeholder}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={(e) => {
          if (e.key !== "Escape") return;
          // Don't let the dialog's own Escape handler run past a non-empty box:
          // the first Escape clears the search, the second closes.
          e.stopPropagation();
          if (value) onChange("");
          else onEscape?.();
        }}
      />
      {value && (
        <button
          type="button"
          className="dlg-filter-clear"
          title="Clear filter"
          aria-label="Clear filter"
          onClick={() => {
            onChange("");
            ref.current?.focus();
          }}
        >
          ✕
        </button>
      )}
    </div>
  );
}
