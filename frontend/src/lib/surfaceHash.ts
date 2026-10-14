/** Top-bar surfaces in the URL: New, Intake, Verify, Schedule and Settings
 * each get a hash (#new, #intake/tickets, #verify, #schedule,
 * #settings/accounts), so browser Back closes the open one instead of leaving the
 * app, and a link opens it. The store's openDialog stays the one truth; this
 * only mirrors it into history and back.
 *
 *   open a surface        push one history entry with its hash
 *   switch to another     replace that entry (Back still goes to the grid)
 *   close it in the app   step back over our entry (or strip the hash)
 *   Back / Forward        close / open whatever the hash now says
 *
 * Only these five: a commit dialog or the palette is a moment, not a place. */

import { useUi, type DialogName } from "../state/store";

const BY_HASH: Record<string, DialogName> = {
  new: "new-session",
  intake: "intake",
  verify: "verify",
  settings: "settings",
  schedule: "schedule",
  // Schedule's name before it moved to the top bar: an old link still lands.
  templates: "schedule",
};
const BY_DIALOG: Partial<Record<DialogName, string>> = {
  ...Object.fromEntries(Object.entries(BY_HASH).map(([h, d]) => [d, h])),
  schedule: "schedule",
  templates: "schedule",
};

export interface Surface {
  name: DialogName;
  target: string | null;
}

/** The hash for an open dialog, or "" when it isn't a surface. The target
 * (a Settings screen, an Intake tab) rides after a slash; Schedule's "new"
 * (reopen New on close) is internal and left out. */
export function hashFor(name: DialogName | null, target: string | null): string {
  const h = name ? BY_DIALOG[name] : undefined;
  if (!h) return "";
  const t = target && !(h === "schedule" && target === "new") ? "/" + encodeURIComponent(target) : "";
  return "#" + h + t;
}

/** What a hash asks for, or null for none / not ours. */
export function parseHash(hash: string): Surface | null {
  const m = /^#([a-z-]+)(?:\/(.*))?$/.exec(hash || "");
  if (!m || !BY_HASH[m[1]]) return null;
  let target: string | null = null;
  if (m[2]) {
    try {
      target = decodeURIComponent(m[2]);
    } catch {
      target = null;
    }
  }
  return { name: BY_HASH[m[1]], target };
}

/** Wire the store to the URL; returns the unhook. */
export function installSurfaceHash(win: Window = window): () => void {
  const hist = win.history;
  const loc = win.location;
  // Whether the current history entry is one WE pushed for an open surface
  // (then closing it in the app steps back over it).
  let pushed = false;
  // A Back we triggered ourselves: its popstate must not re-close anything.
  let stepping = false;
  // A close waits one microtask before stepping back: a dialog that closes and
  // opens the next surface in the same tick (Schedule handing back to New)
  // is a switch, and a back() racing a pushState would lose the new entry.
  let pendingClose = false;
  const urlWith = (hash: string) => loc.pathname + loc.search + hash;

  // A link (or a reload) that names a surface opens it.
  const first = parseHash(loc.hash);
  if (first) useUi.getState().openDialogFor(first.name, first.target);

  const unsub = useUi.subscribe((s, prev) => {
    if (s.openDialog === prev.openDialog && s.dialogTarget === prev.dialogTarget) return;
    const want = hashFor(s.openDialog, s.dialogTarget);
    const had = hashFor(prev.openDialog, prev.dialogTarget);
    if (want === loc.hash) return; // a popstate (or the first load) did it
    try {
      if (want && !had && !pendingClose) {
        hist.pushState(null, "", urlWith(want));
        pushed = true;
      } else if (want) {
        pendingClose = false;
        hist.replaceState(null, "", urlWith(want));
      } else if (pushed) {
        pendingClose = true;
        queueMicrotask(() => {
          if (!pendingClose) return;
          pendingClose = false;
          pushed = false;
          stepping = true;
          try {
            hist.back();
          } catch {
            stepping = false;
          }
        });
      } else if (loc.hash && parseHash(loc.hash)) {
        hist.replaceState(null, "", urlWith(""));
      }
    } catch {
      /* a sandboxed frame can refuse history writes: the app still works */
    }
  });

  const onPop = () => {
    if (stepping) {
      stepping = false;
      return;
    }
    const want = parseHash(loc.hash);
    const ui = useUi.getState();
    if (want) {
      // Forward onto a surface (or a link pasted into this tab).
      if (ui.openDialog !== want.name || ui.dialogTarget !== want.target) ui.openDialogFor(want.name, want.target);
      pushed = false;
    } else if (ui.openDialog && BY_DIALOG[ui.openDialog]) {
      // Back from a surface: close it, and the URL is already right.
      pushed = false;
      ui.closeDialog();
    }
  };
  win.addEventListener("popstate", onPop);
  return () => {
    unsub();
    win.removeEventListener("popstate", onPop);
  };
}
