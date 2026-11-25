/** One model ran out while the plan has room (backend core/model_downshift.py):
 * a session whose newest notice was "your Fable limit resets Nov 30" was
 * switched to the next model down (`/model opus`) and told to carry on. This
 * is the app's toast for it. */

import { displayName } from "../state/store";
import { toast } from "./toast";

/** The server's event for one switch. */
export const MODEL_DOWNSHIFT_EVENT = "session.model_downshifted";

/** That event's data. */
export interface ModelDownshiftData {
  from?: string;
  to?: string;
  reset_at?: number | null;
  detail?: string;
}

const cap = (s: string | undefined): string => (s ? s.charAt(0).toUpperCase() + s.slice(1) : "");

/** "Nov 30 at 11pm" for a reset epoch (seconds), "" when unknown. */
export function resetWhen(at: number | null | undefined): string {
  if (!at || !isFinite(at)) return "";
  const d = new Date(at * 1000);
  const month = d.toLocaleString("en-US", { month: "short" });
  const h = d.getHours() % 12 || 12;
  const m = d.getMinutes();
  return month + " " + d.getDate() + " at " + h + (m ? ":" + String(m).padStart(2, "0") : "") + (d.getHours() < 12 ? "am" : "pm");
}

/** "“api” switched from Fable to Opus — Fable is used up until Nov 30 at 11pm". */
export function modelDownshiftText(
  d: ModelDownshiftData,
  session: string,
  nameOf: (t: string) => string = displayName
): string {
  const back = resetWhen(d.reset_at);
  return (
    "“" + nameOf(session || "") + "” switched from " + cap(d.from) + " to " + cap(d.to) +
    ": " + cap(d.from) + " is used up" + (back ? " until " + back : "")
  );
}

/** Show the toast for one `session.model_downshifted`. */
export function modelDownshiftToast(d: ModelDownshiftData, session: string): void {
  toast(modelDownshiftText(d, session), {
    title:
      "Its model ran out while your plan still has room, so MindFlock switched it to the next model and told it to carry on. " +
      "Turn this off in Settings → Agent orchestration.",
    duration: 9000,
  });
}
