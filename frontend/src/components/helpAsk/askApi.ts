/** The pop-up's request and pointers (components/helpAsk/AskHelp; the
 * open/closed state is lib/helpAsk): POST /api/help/ask with the place
 * registry, the reply made safe to show, the starter questions, the spacing
 * between questions, and `reachPlace` / `goToPlace` — the only thing an
 * answer can make the UI do: open what a registry place needs, then wait for
 * its element. */

import { api } from "../../api/client";
import { useUi } from "../../state/store";
import { openSettings } from "../../lib/openSettings";
import { setView } from "../../lib/viewMode";
import { useHubUi } from "../../lib/hub";
import { readAdvancedMode, writeAdvancedMode } from "../settings/settingsMode";
import { revealTarget } from "../onboarding/CoachTour";
import { placesForModel, pointersFor, type HelpPlace } from "../../lib/helpPlaces";

export interface HelpSource {
  doc: string;
  heading: string;
  path: string;
  excerpt: string;
}

/** What the server answers (backend core/help_ask.answer). */
export interface HelpReply {
  text: string;
  found: boolean;
  source: HelpSource | null;
  model: boolean;
  pointers: string[];
  limited: boolean;
}

/** An answer as the pop-up shows it: the reply, and its pointers resolved
 * against the registry (anything else was dropped). */
export interface HelpAnswer extends HelpReply {
  question: string;
  places: HelpPlace[];
}

/** Starter questions, shown while the box is empty: ones the docs answer
 * well (each pinned by a backend test against the real docs). */
export const STARTERS: readonly string[] = [
  "How do I schedule a template?",
  "Where do I change the theme?",
  "What does Goes as far as do?",
  "How do I connect GitHub?",
];

/** Shortest gap between two questions sent (ms). A question asked sooner
 * waits out the rest of the gap (it is not dropped); one already waiting or
 * in flight, or the same question again, sends nothing — so holding Enter
 * sends one. */
export const ASK_GAP_MS = 1200;
let _lastSent = -Infinity;

/** Reserve the next send slot: how long to wait first (0 = now). */
export function takeAskSlot(now = Date.now()): number {
  const at = Math.max(now, _lastSent + ASK_GAP_MS);
  _lastSent = at;
  return at - now;
}

/** Ask the server; the answer with its pointers resolved. `signal` cancels. */
export async function askHelp(question: string, signal?: AbortSignal): Promise<HelpAnswer> {
  const q = question.trim();
  const reply = await api<HelpReply>("/api/help/ask", {
    method: "POST",
    json: { question: q, places: placesForModel() },
    signal,
  });
  return toAnswer(q, reply);
}

/** A server reply made safe to show: strings and booleans only, pointers
 * through the registry (model ids first, else the words' own matches). */
export function toAnswer(question: string, raw: unknown): HelpAnswer {
  const r = (raw && typeof raw === "object" ? raw : {}) as Partial<HelpReply>;
  const src = r.source && typeof r.source === "object" ? r.source : null;
  const str = (v: unknown) => (typeof v === "string" ? v : "");
  const reply: HelpReply = {
    text: str(r.text),
    found: r.found === true,
    source: src ? { doc: str(src.doc), heading: str(src.heading), path: str(src.path), excerpt: str(src.excerpt) } : null,
    model: r.model === true,
    pointers: Array.isArray(r.pointers) ? r.pointers.filter((x): x is string => typeof x === "string") : [],
    limited: r.limited === true,
  };
  return { ...reply, question, places: pointersFor(question, reply.pointers) };
}

/** Open what `place` needs before it can be seen: the sidebar, a Settings
 * screen (advanced settings on first when its control lives there), a
 * dialog on a tab, a view, the Shared files drawer. */
export function reachPlace(place: HelpPlace): void {
  const ui = useUi.getState();
  const r = place.reach;
  if (r.kind === "sidebar") {
    if (ui.sidebarHidden) ui.toggleSidebar();
  } else if (r.kind === "settings") {
    if (r.advanced && !readAdvancedMode()) writeAdvancedMode(true);
    openSettings(r.screen);
  } else if (r.kind === "dialog") {
    ui.openDialogFor(r.dialog, r.tab ?? null);
  } else if (r.kind === "view") {
    if (ui.view !== r.view) setView(r.view);
  } else if (r.kind === "hub") {
    useHubUi.getState().setOpen(true);
  }
}

/** Reach `place`, then look for its element every 100 ms for up to
 * `waitMs` (a dialog's rows land after it opens). True once it is on
 * screen (brought into view: folds opened, scrolled), false if it never
 * shows — then the pointer says in words where it is instead. */
export function goToPlace(place: HelpPlace, waitMs = 3000): Promise<boolean> {
  reachPlace(place);
  return new Promise((resolve) => {
    const t0 = Date.now();
    const look = () => {
      if (place.target && typeof document !== "undefined" && revealTarget(document, place.target)) return resolve(true);
      if (Date.now() - t0 >= waitMs) return resolve(false);
      setTimeout(look, 100);
    };
    // A frame first: whatever reachPlace opened renders.
    setTimeout(look, 30);
  });
}
