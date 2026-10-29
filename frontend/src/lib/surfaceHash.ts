/** Top-bar surfaces in the URL: New, Flights and Settings each get a hash
 * (#new, #flights, #flights/plans, #settings/accounts), so browser Back
 * closes the open one instead of leaving the app, and a link opens it. The
 * old surfaces' links still land — #intake/tickets, #verify, #schedule,
 * #templates open Flights on the matching tab (the store maps the names; see
 * openDialogFor). The store's openDialog stays the one truth; this only
 * mirrors it into history and back.
 *
 *   open a surface        push one history entry with its hash
 *   switch to another     replace that entry (Back still goes to the grid)
 *   close it in the app   step back over our entry (or strip the hash)
 *   Back / Forward        close / open whatever the hash now says
 *
 * Only these: a commit dialog or the palette is a moment, not a place.
 *
 * The main area's VIEW rides the hash too, under any surface: `#sky` is the
 * sky, `#sky/<title>` the sky on that session alone, no hash the grid. A view
 * switch replaces the entry rather than pushing one (Back is for leaving a
 * surface, not for undoing a toggle); closing a surface lands back on the
 * view's hash. Old links that named the Sky as a Flights tab
 * (`#flights/sky/<title>`, `#flights/board`) open the view (the store's
 * openDialogFor routes them) and are rewritten to `#sky…`. */

import { useUi, type DialogName, type MainView } from "../state/store";
import type { SkyMode } from "./skyLayout";

const BY_HASH: Record<string, DialogName> = {
  new: "new-session",
  flights: "flights",
  // Flights' tabs by their old names: opened as "flights" by the store.
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
  if (h === "flights") {
    // "<tab>/<deep link>": the tab and its first slash stay readable
    // (#flights/checklists/feature%2Fx); Schedule's internal "new" is left
    // out, as it always was.
    const t = String(target || "").replace(/^schedule\/new$/, "schedule");
    // Flyways is the landing tab.
    if (!t || t === "plans") return "#flights";
    const cut = t.indexOf("/");
    return "#flights/" + (cut < 0 ? encodeURIComponent(t) : encodeURIComponent(t.slice(0, cut)) + "/" + encodeURIComponent(t.slice(cut + 1)));
  }
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

/** The view's own hash: "" for the grid, `#sky`, or `#sky/<title>` for the
 * sky on the focused session alone. */
export function viewHash(s: { view: MainView; skyMode: SkyMode; focused: string | null }): string {
  if (s.view !== "sky") return "";
  return s.skyMode === "one" && s.focused ? "#sky/" + encodeURIComponent(s.focused) : "#sky";
}

/** A view hash: `#sky` / `#sky/<title>` → the sky on `focus` ("" = all). */
export function parseViewHash(hash: string): { view: "sky"; focus: string } | null {
  const m = /^#sky(?:\/(.*))?$/.exec(hash || "");
  if (!m) return null;
  let focus = "";
  try {
    focus = m[1] ? decodeURIComponent(m[1]) : "";
  } catch {
    focus = "";
  }
  return { view: "sky", focus };
}

/** Everything the URL says right now: the open surface's hash, else the
 * view's. */
const urlHash = (s: Parameters<typeof viewHash>[0] & { openDialog: DialogName | null; dialogTarget: string | null }) =>
  hashFor(s.openDialog, s.dialogTarget) || viewHash(s);

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

  const replace = (hash: string) => {
    try {
      hist.replaceState(null, "", urlWith(hash));
    } catch {
      /* a sandboxed frame: the app still works */
    }
  };

  // A link (or a reload) that names a surface opens it — and an old one
  // (#intake/tickets) is rewritten in place to where it landed
  // (#flights/sources/tickets), so Back from it closes rather than re-opens.
  // A view hash sets the view; no hash keeps the one this device remembers.
  const first = parseHash(loc.hash);
  const firstView = parseViewHash(loc.hash);
  if (firstView) useUi.getState().showSky(firstView.focus);
  if (first || firstView || !loc.hash) {
    if (first) useUi.getState().openDialogFor(first.name, first.target);
    const now = urlHash(useUi.getState());
    if (now !== loc.hash && (now || loc.hash)) {
      try {
        hist.replaceState(null, "", urlWith(now));
      } catch {
        /* a sandboxed frame: the app still works */
      }
    }
  }

  const unsub = useUi.subscribe((s, prev) => {
    if (s.openDialog === prev.openDialog && s.dialogTarget === prev.dialogTarget) {
      // Only the view moved: it shows in the URL unless a surface owns it
      // (then closing that surface lands on the view's hash).
      const v = viewHash(s);
      if (v === viewHash(prev) || hashFor(s.openDialog, s.dialogTarget) || pendingClose || stepping) return;
      if (v !== loc.hash) replace(v);
      return;
    }
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
        hist.replaceState(null, "", urlWith(viewHash(s)));
      }
    } catch {
      /* a sandboxed frame can refuse history writes: the app still works */
    }
  });

  const onPop = () => {
    const ui = useUi.getState();
    if (stepping) {
      stepping = false;
      // Back over our surface's entry lands on the view it was opened from:
      // say the view as it is now.
      const v = viewHash(ui);
      if (v !== loc.hash) replace(v);
      return;
    }
    const want = parseHash(loc.hash);
    if (want) {
      // Forward onto a surface (or a link pasted into this tab).
      if (ui.openDialog !== want.name || ui.dialogTarget !== want.target) ui.openDialogFor(want.name, want.target);
      pushed = false;
    } else if (ui.openDialog && BY_DIALOG[ui.openDialog]) {
      // Back from a surface: close it, and keep the view you are in.
      pushed = false;
      ui.closeDialog();
      const v = viewHash(useUi.getState());
      if (v !== loc.hash) replace(v);
    } else {
      // Back / Forward between views (or a #sky link pasted into this tab).
      const view = parseViewHash(loc.hash);
      if (view) ui.showSky(view.focus);
      else if (!loc.hash) ui.setView("grid");
    }
  };
  win.addEventListener("popstate", onPop);
  return () => {
    unsub();
    win.removeEventListener("popstate", onPop);
  };
}
