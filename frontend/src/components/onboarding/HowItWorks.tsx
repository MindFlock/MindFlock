/** How MindFlock works — the welcome picture, reopenable any time from
 * Settings → General → Help: the three-part sketch (session → route → Needs
 * you), the picture in one breath, what the Sky and the Grid are for, and the
 * test every stop passes. One card and a Close; nothing to set up, so a
 * veteran can reread it without re-running the first-run checklist (the
 * Welcome tour's Replay does that).
 *
 * Its open flag is a tiny store of its own (like the Sky guide's), so the
 * host only mounts `<HowItWorks />`; given `open` it is controlled (tests).
 * The Modal portals to <body>, so it paints over the Settings dialog. */

import { create } from "zustand";
import { GRID_LINE, SKY_LINE, STOP_TEST, VIEWS_LINE, VISION_BREATH } from "../../lib/onboarding";
import { Modal } from "../Modal";
import { VisionPicture } from "./VisionPicture";

export const HOW_IT_WORKS_TITLE = "How MindFlock works";

export const useHowItWorks = create<{ open: boolean }>(() => ({ open: false }));
export const openHowItWorks = () => useHowItWorks.setState({ open: true });
export const closeHowItWorks = () => useHowItWorks.setState({ open: false });

export function HowItWorks(p: { open?: boolean; onClose?(): void; portal?: boolean }) {
  const stored = useHowItWorks((s) => s.open);
  const open = p.open !== undefined ? p.open : stored;
  const onClose = p.onClose ?? closeHowItWorks;
  if (!open) return null;
  const [first, ...rest] = VISION_BREATH;
  return (
    <Modal id="how-it-works" role="dialog" aria-modal="true" aria-labelledby="hiw-title" onClose={onClose} portal={p.portal !== false}>
      <div className="hiw-card">
        <h2 id="hiw-title">{HOW_IT_WORKS_TITLE}</h2>
        <VisionPicture />
        <p className="hiw-breath">
          <b>{first}</b> {rest.join(" ")}
        </p>
        <ul className="hiw-views">
          <li>
            <b>Sky</b>: {SKY_LINE.charAt(0).toLowerCase() + SKY_LINE.slice(1)}
          </li>
          <li>
            <b>Grid</b>: {GRID_LINE.charAt(0).toLowerCase() + GRID_LINE.slice(1)}
          </li>
          <li>{VIEWS_LINE}</li>
          <li>{STOP_TEST}</li>
        </ul>
        <div className="hiw-foot">
          <button type="button" className="btn-primary" autoFocus onClick={onClose}>
            Got it
          </button>
        </div>
      </div>
    </Modal>
  );
}
