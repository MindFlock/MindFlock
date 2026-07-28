/** Footer "Customize" popover: choose which bars show in the sidebar. Sits
 * between the session count and the shortcuts link. Availability gating still
 * applies on top — a checked bar can stay absent (e.g. Ticket Ingestion
 * without a connected ticketing source). Persisted via useUi.hiddenBars. */

import { useEffect, useRef, useState } from "react";
import { useUi } from "../../state/store";
import { orderedBars } from "./barDefs";

export function FooterCustomize() {
  const hiddenBars = useUi((s) => s.hiddenBars);
  const toggleBarHidden = useUi((s) => s.toggleBarHidden);
  const barOrder = useUi((s) => s.barOrder);
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  return (
    <div id="foot-customize" ref={ref}>
      <button
        id="foot-customize-btn"
        type="button"
        className={"foot-link" + (open ? " open" : "")}
        title="Choose which bars show in the sidebar"
        aria-haspopup="true"
        aria-expanded={open}
        onClick={() => setOpen(!open)}
      >
        ⚙ Customize
      </button>
      {open && (
        <div id="foot-customize-menu" role="menu">
          <div className="fc-title">Show in sidebar</div>
          {orderedBars(barOrder).map((b) => (
            <label key={b.key} className="fc-item">
              <input
                type="checkbox"
                checked={!hiddenBars.has(b.key)}
                onChange={() => toggleBarHidden(b.key)}
              />
              {b.label}
            </label>
          ))}
        </div>
      )}
    </div>
  );
}
