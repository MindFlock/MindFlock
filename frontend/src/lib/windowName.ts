/** What a session is CALLED, resolved the one way the sidebar resolves it.
 *
 * THE BUG THIS ENDS. A desktop notification named its session by the raw title
 * off the event envelope — `shortcut-21431`, `sitecheck-bot7-copy-copy` — while
 * the window it was about had been renamed, or was labelled by the pipeline as
 * `(tix) add-dark-mode/sc-12345`. A push telling you to go and look at a session
 * that does not appear to exist under that name is worse than no push: the one
 * thing it has to get right is which window it means.
 *
 * So the notification asks the app rather than re-deriving the answer. There is
 * exactly one rule, and this is it:
 *
 *   1. the rename, if there is one (`mf_aliases`, the same map `displayName`
 *      and every React surface read);
 *   2. otherwise the pipeline label the sidebar shows — `sessionLabel`, which
 *      needs the session's branch and therefore needs the instances snapshot;
 *   3. otherwise the raw title, which is what a hand-made session is called.
 *
 * Published on `window.mindflock` (the extension API) because the caller that
 * needs it most is not React: `static/addons/notify.js` is a plain script and
 * cannot import any of this. It feature-detects, so an older addon file, or a
 * page where the bridge has not been installed yet, keeps working.
 */

import { isVerifySession } from "../components/dialogs/verify";
import { matchesFilter, orderedInstances } from "../components/sidebar/ordering";
import type { Instance } from "../api/types";
import { sessionLabel } from "./sessionLabel";
import { useUi } from "../state/store";

/** A row in the `/api/instances` snapshot, as much of it as naming needs. */
interface Named {
  title?: string;
  /** Set only on a proxied row, where `title` is `<device>::<title>`: the
   * session's own name on the machine it lives on — and the name that machine's
   * events arrive under. Matched as well as `title` so a notification about a
   * remote session is named like its row rather than falling through to raw. */
  display_title?: string;
  branch?: string;
}

function snapshot(): Named[] {
  // `globalThis`, not `window`: in a browser they are the same object, and the
  // unit tests run in the node environment this file must not require.
  const w = globalThis as unknown as { mindflock?: { sessions?: () => unknown[] } };
  try {
    const list = w.mindflock?.sessions?.();
    return Array.isArray(list) ? (list as Named[]) : [];
  } catch {
    return [];
  }
}

/** The session's display name — its rename, else its sidebar label, else the
 * raw title. Never throws and never returns "": callers put this in front of
 * a user. */
export function windowName(title: string): string {
  const raw = String(title || "");
  if (!raw) return "";
  const alias = useUi.getState().aliases[raw];
  if (alias) return alias;
  const inst = snapshot().find(
    (i) => i && (i.title === raw || i.display_title === raw)
  );
  if (!inst) return raw;
  // `display_title` for a proxied row: `sessionLabel` is documented to take the
  // device-stripped title, and "<device>::sc-12345" matches none of its shapes.
  return sessionLabel(inst.display_title || inst.title || raw, inst.branch || "").text || raw;
}

/** The session's SLOT NUMBER in the sidebar — "1"…"9", or "" when the row
 * shows none (tenth row onward, or not currently listed).
 *
 * The rail's numbers are how people locate a window ("[3] finished" → glance
 * at row 3), so a notification carrying one must show THE number the rail
 * shows right now. That number is client state — the drag order plus the live
 * filter — so it is derived here, at render time, exactly the way the rail
 * derives its rows: verify sessions excluded, the saved order applied, the
 * filter applied. One deliberate divergence: on a tailnet with device groups
 * the rail numbers grouped rows in section order and skips collapsed groups;
 * this resolver lists local rows first and cannot see collapse state, so a
 * remote session's number can drift from its row there. Local sessions — the
 * ones this machine's events are about — always match.
 */
export function slotNumber(title: string): string {
  const raw = String(title || "");
  if (!raw) return "";
  try {
    const ui = useUi.getState();
    const listed = (snapshot() as Instance[]).filter(
      (i) => i && !isVerifySession(String(i.title || ""))
    );
    const { rows } = orderedInstances(listed, ui.order);
    const filtered = rows.filter((i) => matchesFilter(i, ui.filter, ui.aliases));
    const local = filtered.filter((i) => !(i as { device?: string }).device);
    const remote = filtered.filter((i) => (i as { device?: string }).device);
    const idx = [...local, ...remote].findIndex(
      (i) => i.title === raw || (i as { display_title?: string }).display_title === raw
    );
    return idx >= 0 && idx < 9 ? String(idx + 1) : "";
  } catch {
    return "";
  }
}

/** Publish on the extension API, next to `mf.toast`. `core/events.js` creates
 * `window.mindflock` before the bundle runs, so this only ever adds a key. */
export function publishWindowName() {
  const w = globalThis as unknown as { mindflock?: Record<string, unknown> };
  w.mindflock = w.mindflock || {};
  w.mindflock.displayName = windowName;
  w.mindflock.slotNumber = slotNumber;
}
