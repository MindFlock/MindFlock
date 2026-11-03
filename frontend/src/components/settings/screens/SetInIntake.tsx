/** "Set in Intake ›": where a Settings row used to hold a ticket setting that
 * Intake now owns.
 *
 * Intake took ticketing over from Settings, but a few ticket rows stayed
 * behind — and one (`engine.max_sessions`, "Ticket sessions at once") was
 * drawn on both, the same field twice under two names. Each of those rows now
 * lives once, in Intake › Auto-start (the switches, the limit, and the folded
 * "Ticket sessions" options under them), and the Settings screen it left keeps
 * a one-line pointer with the old name, so someone who remembers it here (or
 * searches for it — palette/settingsSearch.ts lands on this row) is one click
 * from the real control.
 *
 * The link opens Intake on the tab and, given a selector, opens the fold
 * around that control and scrolls to it (as revealInSettings does in
 * Settings). */

import type { IntakeTabKey } from "../../intake/IntakeDialog";
import { useUi } from "../../../state/store";

/** Open Intake on `tab`; then bring `selector` (inside the Intake panel) on
 * screen, opening any fold around it. Gives up quietly after ~1 s. */
export function openIntakeAt(tab: IntakeTabKey, selector?: string): void {
  useUi.getState().openDialogFor("intake", tab);
  if (!selector || typeof document === "undefined") return;
  let tries = 0;
  const look = () => {
    const el = document.querySelector<HTMLElement>("#intake-panel " + selector);
    if (!el) {
      if (++tries < 30) setTimeout(look, 35);
      return;
    }
    for (let d = el.closest("details"); d; d = d.parentElement?.closest("details") ?? null) d.open = true;
    el.scrollIntoView?.({ block: "center" });
  };
  setTimeout(look, 0);
}

/** One row: the setting's old name, a line on what it does, and the link. */
export function SetInIntake({
  id,
  label,
  hint,
  tab = "autostart",
  selector,
}: {
  id: string;
  label: string;
  hint: string;
  tab?: IntakeTabKey;
  selector?: string;
}) {
  return (
    <div className="set-row set-in-intake" id={id} data-moved-to="intake">
      <span className="set-label">{label}</span>
      <span className="set-hint">
        {hint}{" "}
        <button
          type="button"
          className="linklike"
          data-open-intake={tab}
          title="Ticket settings live in Intake, next to the auto-start switches"
          onClick={() => openIntakeAt(tab, selector)}
        >
          Set in Intake ›
        </button>
      </span>
    </div>
  );
}
