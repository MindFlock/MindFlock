/** The app's ⋯ overflow menu. Lives in components/ (not dialogs/) because it is
 * a primitive, not a Verify screen; its look is the `.vf-menu` rules in
 * dialogs/VerifyDialog.css, where it was first cut. */

import { useEffect, useRef, useState } from "react";

/** One entry in a ⋯ menu. */
export interface MenuItem {
  key: string;
  label: string;
  title?: string;
  /** Turns red on hover. The confirm() is the real guard; this is the warning. */
  danger?: boolean;
  /** Extra classes for an item some test pins by name (see "Cancel run"). */
  className?: string;
  onSelect(): void;
}

/** The ⋯ overflow: every action that applies but is not the one you came for.
 *
 * A native `<details>` rather than a hand-rolled popover, because the open state
 * then survives without a portal and the summary is focusable and announced for
 * free. What is NOT free is the three things a menu owes its user, all of which
 * are here: Escape closes it (in the capture phase, so the first Escape closes
 * the MENU and only the second closes the dialog behind it), a click anywhere
 * else closes it, and it flips upward near the bottom of the scroll container
 * (`boundaryId`, else the window) rather than opening into a clipped strip
 * nobody can read.
 *
 * Renders nothing at all when there is nothing applicable — a ⋯ that opens onto
 * an empty box is worse than no ⋯. */
export function OverflowMenu({
  label,
  items,
  boundaryId,
}: {
  label: string;
  items: MenuItem[];
  /** Id of the scroll container whose bottom edge the menu flips up against. */
  boundaryId?: string;
}) {
  const ref = useRef<HTMLDetailsElement>(null);
  const [open, setOpen] = useState(false);
  const [up, setUp] = useState(false);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      setOpen(false);
      // Capture phase + stopPropagation: the dialog's own Escape listener sits
      // on window in the bubble phase (the Modal shell), so without this one
      // keypress would close the menu and the whole dialog together.
      e.stopPropagation();
      e.preventDefault();
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey, true);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey, true);
    };
  }, [open]);

  if (!items.length) return null;

  return (
    <details className={"vf-menu" + (up ? " vf-menu-up" : "")} open={open} ref={ref}>
      <summary
        aria-label={label}
        title={label}
        onClick={(e) => {
          // The <details> would toggle itself; we drive it so the flip can be
          // measured on the way open, while the row is still where it was.
          e.preventDefault();
          const next = !open;
          if (next && ref.current) {
            const here = ref.current.getBoundingClientRect();
            const body = boundaryId ? document.getElementById(boundaryId) : null;
            const floor = body ? body.getBoundingClientRect().bottom : window.innerHeight;
            setUp(floor - here.bottom < 24 * items.length + 40);
          }
          setOpen(next);
        }}
      >
        ⋯
      </summary>
      <div className="vf-menu-list">
        {items.map((item) => (
          <button
            key={item.key}
            type="button"
            className={
              "vf-menu-item" +
              (item.danger ? " vf-menu-danger" : "") +
              (item.className ? " " + item.className : "")
            }
            title={item.title}
            onClick={() => {
              setOpen(false);
              item.onSelect();
            }}
          >
            {item.label}
          </button>
        ))}
      </div>
    </details>
  );
}
