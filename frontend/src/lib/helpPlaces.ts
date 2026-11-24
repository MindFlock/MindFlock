/** The places "Ask how something works" can point at: ONE registry of the
 * app's pointable places — an id, the words people use for it, how to get
 * there (open Settings on a screen, open a dialog, show the sidebar, switch
 * view) and the element to spotlight, tour-style (onboarding/CoachTour).
 *
 * Seeded from what the app already knows, so it cannot drift on its own:
 *  - every Settings search entry (palette/settingsSearch SETTINGS_SEARCH)
 *    is a place — its screen, its anchor, its words — generated, not copied;
 *  - a place inside a toured surface takes its target from that tour's step
 *    (lib/tours: `tourTarget`), so a hook the tour renames moves here too;
 *  - the rest are the top bar's and the sidebar's own stable ids, the ones
 *    the palette and the tours already name.
 * A test (helpPlaces.test.ts) pins that every target's hook exists in a
 * component's source and that the words find the right place.
 *
 * Matching a question to places needs no model (`matchPlaces`: the same
 * idea as the docs search — words weighted by how rare they are among the
 * places, a label word counting more than a keyword, a keyword more than the
 * caption). With a model, the server may name ids from `placesForModel()`
 * instead; anything not in this registry is dropped (`placeById`).
 *
 * Pure data + pure functions: lib/helpAsk's `reachPlace` opens what a place
 * needs before it is spotlit. */

import { SETTINGS_SEARCH, type SettingsSearchEntry } from "../components/palette/settingsSearch";
import { TOURS, type TourId } from "./tours";
import type { SettingsScreen } from "./openSettings";
import type { DialogName } from "../state/store";

/** What has to happen before a place is on screen. */
export type PlaceReach =
  | { kind: "none" }
  | { kind: "sidebar" }
  | { kind: "settings"; screen: SettingsScreen; advanced?: boolean }
  | { kind: "dialog"; dialog: DialogName; tab?: string }
  | { kind: "view"; view: "grid" | "sky" }
  | { kind: "hub" };

export interface HelpPlace {
  /** Stable, lower-case: `[a-z0-9._:-]` (the server drops anything else). */
  id: string;
  /** What it is, as the UI names it ("Settings → Appearance → Mode"). */
  label: string;
  /** One plain sentence, shown beside the spotlight. */
  caption: string;
  /** The words people use for it, space-separated (a "quoted phrase" is
   * not needed: a keyword of several words matches as a phrase). */
  keywords: readonly string[];
  /** CSS selector list, first match wins (CoachTour's findTarget). */
  target: string;
  reach: PlaceReach;
  /** Said plainly when it is not on screen even after `reach`. */
  how: string;
}

/** A toured surface's target for its `data-tour` hook — the tour's own
 * selector list, so the registry follows the tour. "" when the tour has no
 * such step (the test fails on it). */
export function tourTarget(tour: TourId, hook: string): string {
  const attr = 'data-tour="' + hook + '"';
  const steps = TOURS[tour]?.steps || [];
  // The step that rings the hook FIRST (Schedule's opening step names the
  // top-bar button before "schedule-new"), else any step that names it.
  const step = steps.find((s) => s.target.split(",")[0].includes(attr)) || steps.find((s) => s.target.includes(attr));
  return step ? step.target : "";
}

const slug = (s: string) =>
  s
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");

/** A Settings search entry as a place: its screen (the nav button, else the
 * screen itself) or its anchor inside the screen. */
export function settingsPlace(e: SettingsSearchEntry): HelpPlace {
  const parts = e.label.split(" → ");
  const id = "settings." + e.key + (parts.length > 1 ? "." + slug(parts.slice(1).join(" ")) : "");
  return {
    id,
    label: "Settings → " + e.label,
    caption: e.sub.replace(/\.$/, "") + ".",
    keywords: ["settings", ...e.terms],
    target: e.anchor
      ? "#settings-screens " + e.anchor
      : '.set-nav-item[data-screen="' + e.key + '"], #settings-screens [data-screen="' + e.key + '"]',
    reach: { kind: "settings", screen: e.key, advanced: e.advanced },
    how: "Open Settings (the ⚙ in the top bar) and pick " + parts.join(" → ") + ".",
  };
}

/** The places that are not Settings entries: the top bar, the sidebar, the
 * views and the dialogs' first controls. */
const UI_PLACES: HelpPlace[] = [
  {
    id: "topbar.new",
    label: "New session (top bar)",
    caption: "Start a session: say what to do and pick a folder; the agent works on its own branch.",
    keywords: ["new", "new session", "start", "create", "begin", "launch", "agent", "task", "run an agent", "start a session"],
    target: "#new-btn",
    reach: { kind: "none" },
    how: "Click + New in the top bar, or press Alt+N.",
  },
  {
    id: "new.task",
    label: "New session → the task box",
    caption: "Describe the work in a sentence here; the folder and the start button sit just below.",
    keywords: ["describe", "task", "prompt", "what to do", "new session", "start a session", "first session"],
    target: tourTarget("new", "new-task"),
    reach: { kind: "dialog", dialog: "new-session" },
    how: "Click + New in the top bar and type the task in the first box.",
  },
  {
    id: "topbar.intake",
    label: "Intake (top bar)",
    caption: "Tickets, pull requests and issues waiting to become sessions, and what starts on its own.",
    keywords: ["intake", "tickets", "ticket", "jira", "linear", "shortcut", "asana", "issues", "pull requests", "prs", "queue", "sources", "auto-start"],
    target: "#intake-btn, #more-btn",
    reach: { kind: "none" },
    how: "Click Intake in the top bar (in a narrow window it is under ⋯), or press Alt+I.",
  },
  {
    id: "intake.sources",
    label: "Intake → add a source",
    caption: "Connect a ticket tracker or GitHub here, then pick the repo its work lands in.",
    keywords: ["add source", "connect", "tracker", "ticket source", "github issues", "jira", "linear", "shortcut", "asana", "intake"],
    target: tourTarget("intake", "intake-tiles"),
    reach: { kind: "dialog", dialog: "intake" },
    how: "Open Intake from the top bar; the sources are at the top.",
  },
  {
    id: "intake.autostart",
    label: "Intake → Auto-start",
    caption: "What starts sessions on its own: new tickets, review comments, new issues.",
    keywords: ["auto-start", "autostart", "automatic", "automatically", "on its own", "start on their own", "intake"],
    target: tourTarget("intake", "intake-autostart"),
    reach: { kind: "dialog", dialog: "intake", tab: "autostart" },
    how: "Open Intake from the top bar and pick the Auto-start tab.",
  },
  {
    id: "topbar.schedule",
    label: "Schedule (top bar)",
    caption: "Templates that start sessions on their own: daily, on weekdays, once or by cron.",
    keywords: ["schedule", "scheduled", "template", "templates", "cron", "recurring", "daily", "nightly", "weekly", "timer", "every day"],
    target: "#schedule-btn, #more-btn",
    reach: { kind: "none" },
    how: "Click Schedule in the top bar (in a narrow window it is under ⋯), or press Alt+S.",
  },
  {
    id: "schedule.new",
    label: "Schedule → new template",
    caption: "Make a template here: a name, a folder, the task, and when it runs.",
    keywords: ["new template", "make a template", "create template", "schedule a template", "template", "cron"],
    target: tourTarget("schedule", "schedule-new"),
    reach: { kind: "dialog", dialog: "schedule" },
    how: "Open Schedule from the top bar and click New template.",
  },
  {
    id: "topbar.settings",
    label: "Settings (top bar)",
    caption: "Every setting, with a search box at the top that finds any of them by a word.",
    keywords: ["settings", "preferences", "options", "configure", "config", "gear"],
    target: "#settings-btn, #more-btn",
    reach: { kind: "none" },
    how: "Click ⚙ Settings in the top bar, or press Ctrl+,.",
  },
  {
    id: "settings.search",
    label: "Settings → search",
    caption: "Type a word like dark, phone or parallel and jump straight to that setting.",
    keywords: ["search settings", "find a setting", "settings search", "setting"],
    target: "#settings-search",
    reach: { kind: "settings", screen: "connections" },
    how: "Open Settings; the search box is at the top of its list of screens.",
  },
  {
    id: "topbar.palette",
    label: "Command palette (top bar)",
    caption: "Every command and session by name: type a few letters and press Enter.",
    keywords: ["command palette", "palette", "commands", "command", "search", "find", "launcher", "quick open", "ctrl+p", "ctrl+k"],
    target: "#palette-btn",
    reach: { kind: "none" },
    how: "Press Ctrl+P (Cmd+P on a Mac), or click the search box at the top bar's right end.",
  },
  {
    id: "topbar.theme",
    label: "Light or dark (top bar)",
    caption: "Switches between light and dark; colours and themes are in Settings → Appearance.",
    keywords: ["theme", "dark", "light", "dark mode", "light mode", "night", "moon", "sun", "colour", "color", "appearance"],
    target: "#theme-btn",
    reach: { kind: "none" },
    how: "Click the moon or sun in the top bar, or open Settings → Appearance.",
  },
  {
    id: "topbar.sidebar",
    label: "Show or hide the sidebar (top bar)",
    caption: "Shows or hides the list of sessions on the left; Ctrl+B does the same.",
    keywords: ["sidebar", "hide sidebar", "show sidebar", "collapse", "expand", "rail", "left panel", "toggle sidebar"],
    target: "#sidebar-toggle",
    reach: { kind: "none" },
    how: "Click the sidebar button at the top bar's left end, or press Ctrl+B.",
  },
  {
    id: "topbar.bell",
    label: "Notifications (the bell)",
    caption: "What needs you: questions, finished work and failures, newest first.",
    keywords: ["bell", "notifications", "notification", "alerts", "needs you", "inbox", "what needs me", "waiting for me"],
    target: "#notif-btn",
    reach: { kind: "none" },
    how: "Click the bell in the top bar.",
  },
  {
    id: "topbar.mic",
    label: "Voice (the mic)",
    caption: "Talk to the focused session; its options hold the wake word and spoken replies.",
    keywords: ["voice", "mic", "microphone", "talk", "speak", "dictate", "speech", "wake word", "say", "spoken replies"],
    target: tourTarget("voice", "voice-mic"),
    reach: { kind: "none" },
    how: "Click the mic in the top bar.",
  },
  {
    id: "sidebar.sessions",
    label: "Your sessions (sidebar)",
    caption: "Every session, one row each: click a row to focus it, and its › menu holds commit, push and PR.",
    keywords: ["sessions", "session list", "rows", "row", "rail", "list", "my sessions", "row menu", "commit", "push", "pull request", "close a session"],
    // Not the voice tour's '[data-tour="session-row"]': no row renders it.
    target: "li.inst, #instance-list",
    reach: { kind: "sidebar" },
    how: "Show the sidebar (Ctrl+B); the sessions are the rows at its top.",
  },
  {
    id: "sidebar.filter",
    label: "Filter sessions (sidebar)",
    caption: "Type part of a name to narrow the list of sessions.",
    keywords: ["filter", "search sessions", "find a session", "narrow", "filter sessions"],
    target: "#session-filter",
    reach: { kind: "sidebar" },
    how: "Show the sidebar (Ctrl+B) and type in the filter above the sessions, or press /.",
  },
  {
    id: "sidebar.views",
    label: "Grid or Sky (sidebar foot)",
    caption: "Shows every session as panes (Grid) or as routes on one canvas (Sky); Ctrl+Y switches.",
    keywords: ["grid", "sky", "view", "views", "switch view", "panes", "canvas", "layout", "ctrl+y"],
    target: "#view-modes",
    reach: { kind: "sidebar" },
    how: "Show the sidebar (Ctrl+B); Grid and Sky are at its foot. Ctrl+Y switches too.",
  },
  {
    id: "sidebar.help",
    label: "? Help (sidebar foot)",
    caption: "Send feedback, every keyboard shortcut, what's new, and this question box.",
    keywords: ["help", "feedback", "send feedback", "report a bug", "bug", "problem", "idea", "what's new", "release notes", "shortcuts"],
    target: "#help-btn",
    reach: { kind: "sidebar" },
    how: "Show the sidebar (Ctrl+B); ? Help is at its bottom right.",
  },
  {
    id: "sidebar.customize",
    label: "Customize (sidebar foot)",
    caption: "Choose which bars the sidebar shows and what the pane header holds.",
    keywords: ["customize", "customise", "bars", "sidebar bars", "pane header", "prompts bar", "add a bar"],
    target: "#foot-customize-btn, #foot-customize",
    reach: { kind: "sidebar" },
    how: "Show the sidebar (Ctrl+B); ⚙ Customize is at its foot.",
  },
  {
    id: "sidebar.recent",
    label: "Recently closed (sidebar)",
    caption: "Reopen a session you closed, or clear what it left on disk.",
    keywords: ["recently closed", "reopen", "restore", "undo close", "closed sessions", "bring back", "deleted session"],
    target: "#recent-btn",
    reach: { kind: "sidebar" },
    how: "Press Ctrl+Shift+T to reopen the last one, or pick Recently closed… in the command palette.",
  },
  {
    id: "sidebar.setup",
    label: "Setup checklist (sidebar foot)",
    caption: "The steps to a first session: install your agent, sign in, pick a repo.",
    keywords: ["setup", "set up", "install", "getting started", "checklist", "sign in", "doctor", "first run"],
    target: "#setup-chip",
    reach: { kind: "sidebar" },
    how: "Pick Open Setup checklist in the command palette (Ctrl+P).",
  },
  {
    id: "pane.menu",
    label: "A pane's menu (Grid)",
    caption: "Each pane's tabs and ⋯ menu: the agent, a terminal, the diff, and what to do with the work.",
    keywords: ["pane", "pane menu", "tabs", "terminal", "diff", "changes", "files changed", "pane header"],
    target: tourTarget("new", "pane-tabs"),
    reach: { kind: "view", view: "grid" },
    how: "Switch to the Grid (Ctrl+Y) and look at the top of a session's pane.",
  },
  {
    id: "sky.shelf",
    label: "Saved routes (the Sky's shelf)",
    caption: "Routes you saved, ready to fly again; + adds a stop to a session's route.",
    keywords: ["route", "routes", "saved routes", "shelf", "stops", "stop", "chain", "flight", "flights", "fly"],
    target: tourTarget("sky", "sky-shelf"),
    reach: { kind: "view", view: "sky" },
    how: "Switch to the Sky (Ctrl+Y); the shelf is along its edge.",
  },
  {
    id: "sky.bird",
    label: "The bird (Sky)",
    caption: "Where a session is on its route: the stop it is flying now.",
    keywords: ["bird", "where is my session", "progress", "current stop", "flying"],
    target: tourTarget("sky", "flights-bird"),
    reach: { kind: "view", view: "sky" },
    how: "Switch to the Sky (Ctrl+Y) and look along a session's route.",
  },
  {
    id: "hub.panel",
    label: "Shared files",
    caption: "Files, pictures and snippets every session can use; drop one in and send it to a session.",
    keywords: ["shared files", "hub", "files", "share", "upload", "screenshot", "image", "picture", "snippet", "attach", "paste"],
    target: "#hub-panel",
    reach: { kind: "hub" },
    how: "Press Alt+H, or pick Shared files… in the command palette.",
  },
];

/** Every place: the UI's, then one per Settings search entry (first of
 * each id kept). */
export const HELP_PLACES: readonly HelpPlace[] = (() => {
  const out: HelpPlace[] = [];
  const seen = new Set<string>();
  for (const p of [...UI_PLACES, ...SETTINGS_SEARCH.map(settingsPlace)]) {
    if (seen.has(p.id)) continue;
    seen.add(p.id);
    out.push(p);
  }
  return out;
})();

const BY_ID = new Map(HELP_PLACES.map((p) => [p.id, p]));

/** The place with this id, or undefined — the ONLY way an id from the
 * server becomes something the UI acts on. */
export function placeById(id: unknown): HelpPlace | undefined {
  return typeof id === "string" ? BY_ID.get(id) : undefined;
}

/** What the model may choose from: `{id, label}`, the label carrying the
 * caption so it can tell look-alikes apart (cut to the server's 90). */
export function placesForModel(): Array<{ id: string; label: string }> {
  return HELP_PLACES.map((p) => ({ id: p.id, label: (p.label + " — " + p.caption).slice(0, 90) }));
}

// --- matching without a model ------------------------------------------------ //

const STOP = new Set(
  `a an the and or but if then so of to in on at by for with from into onto about as is are was were be been being
  do does did doing done have has had i me my we our you your it its this that these those there here what which who
  whom whose when where why how can could should would will shall may might must not no yes any some all each every
  one just also very more most than too only own same such get got make use using used way thing things mindflock
  tell explain describe please there's what's how's where's show find see look go open work works working`.split(/\s+/)
);

/** The light suffix strip voice help uses (backend core/voice_help _stem). */
export function stem(w: string): string {
  if (w.length > 4 && w.endsWith("ies")) return w.slice(0, -3) + "y";
  if (w.length > 5 && w.endsWith("ing")) return w.slice(0, -3);
  if (w.length > 4 && w.endsWith("ed")) return w.endsWith("ued") || w.endsWith("led") ? w.slice(0, -1) : w.slice(0, -2);
  if (w.length > 3 && w.endsWith("s") && !w.endsWith("ss")) return w.slice(0, -1);
  return w;
}

export function terms(text: string): string[] {
  return (String(text || "").toLowerCase().match(/[a-z0-9+]+/g) || []).filter((w) => !STOP.has(w)).map(stem);
}

interface Indexed {
  place: HelpPlace;
  label: Set<string>;
  keys: Set<string>;
  caption: Set<string>;
  phrases: string[];
}

let _index: { rows: Indexed[]; idf: Map<string, number> } | null = null;

function index() {
  if (_index) return _index;
  const rows: Indexed[] = HELP_PLACES.map((place) => ({
    place,
    label: new Set(terms(place.label)),
    keys: new Set(place.keywords.flatMap(terms)),
    caption: new Set(terms(place.caption)),
    phrases: place.keywords.filter((k) => k.includes(" ")).map((k) => k.toLowerCase()),
  }));
  const df = new Map<string, number>();
  for (const r of rows) for (const t of new Set([...r.label, ...r.keys, ...r.caption])) df.set(t, (df.get(t) || 0) + 1);
  const n = rows.length;
  const idf = new Map<string, number>();
  for (const [t, c] of df) idf.set(t, Math.log(1 + (n - c + 0.5) / (c + 0.5)));
  _index = { rows, idf };
  return _index;
}

/** A score under this is no match. */
export const MATCH_MIN = 2.2;
/** A match scoring under this share of the best is left out. */
export const MATCH_SHARE = 0.55;

/** Each place's score for `question` (0 = no match): every question word
 * counts its rarity among the places, ×3 in the label, ×2 in the keywords,
 * ×1 in the caption; a keyword phrase found whole in the question adds 3. */
export function scorePlaces(question: string): Array<{ place: HelpPlace; score: number }> {
  const { rows, idf } = index();
  const q = [...new Set(terms(question))];
  const text = " " + String(question || "").toLowerCase().replace(/[^a-z0-9+]+/g, " ") + " ";
  return rows.map((r) => {
    let s = 0;
    for (const t of q) {
      const w = idf.get(t) ?? 0;
      if (r.label.has(t)) s += 3 * w;
      else if (r.keys.has(t)) s += 2 * w;
      else if (r.caption.has(t)) s += w;
    }
    for (const ph of r.phrases) if (text.includes(" " + ph.replace(/[^a-z0-9+]+/g, " ").trim() + " ")) s += 3;
    return { place: r.place, score: s };
  });
}

/** The places `question` is about, best first, at most `n` — none scoring
 * under MATCH_MIN or under MATCH_SHARE of the best. No model. */
export function matchPlaces(question: string, n = 3): HelpPlace[] {
  const scored = scorePlaces(question)
    .filter((x) => x.score >= MATCH_MIN)
    .sort((a, b) => b.score - a.score);
  if (!scored.length) return [];
  const top = scored[0].score;
  return scored
    .filter((x) => x.score >= top * MATCH_SHARE)
    .slice(0, n)
    .map((x) => x.place);
}

/** A question that only asks where something is ("where is the bell",
 * "where do I change the theme", "show me Intake"): its first pointer is
 * shown at once. */
export function isWhereQuestion(question: string): boolean {
  return /^\s*(where('s| is| are| do i| can i| would i)?\b|show me\b|how do i (find|get to|open)\b|which button\b)/i.test(question);
}

/** The pointers for an answer: the model's ids (registry ones only) when it
 * named any, else the words' own matches. */
export function pointersFor(question: string, modelIds: readonly unknown[] | undefined | null, n = 3): HelpPlace[] {
  const fromModel: HelpPlace[] = [];
  for (const id of modelIds || []) {
    const p = placeById(id);
    if (p && !fromModel.includes(p)) fromModel.push(p);
  }
  return (fromModel.length ? fromModel : matchPlaces(question, n)).slice(0, n);
}

/** The hooks a selector list relies on — `#id`, `.class`, `[attr="value"]`
 * — for the test that pins each one to a component that renders it. */
export function selectorHooks(selector: string): Array<{ kind: "id" | "class" | "attr"; name: string; value?: string }> {
  const out: Array<{ kind: "id" | "class" | "attr"; name: string; value?: string }> = [];
  const re = /#([\w-]+)|\.([\w-]+)|\[([\w-]+)(?:="([^"]*)")?\]/g;
  for (let m = re.exec(selector); m; m = re.exec(selector)) {
    if (m[1]) out.push({ kind: "id", name: m[1] });
    else if (m[2]) out.push({ kind: "class", name: m[2] });
    else out.push({ kind: "attr", name: m[3], value: m[4] });
  }
  return out;
}
