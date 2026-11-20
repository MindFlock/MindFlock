/* Opening What's new by hand, on its own so a caller that only needs the
 * opener (a toast, the bell) does not pull WhatsNew.tsx and its stylesheet
 * forward in the bundle: the built CSS follows import order. */

import { useUi } from "../../state/store";
import { reopenEntries } from "../../lib/whatsNew";
import { RELEASES } from "../../whatsNew/releases";
import { useWhatsNew } from "./seen";

/** Open What's new by hand: the latest release's entries. Closes whatever
 * dialog it was opened from (Settings → Help), so a "Show me" lands clean. */
export function openWhatsNew(): void {
  const entries = reopenEntries(RELEASES, useWhatsNew.getState().running);
  if (!entries.length) return;
  useUi.getState().closeDialog();
  useWhatsNew.getState().open(entries);
}
