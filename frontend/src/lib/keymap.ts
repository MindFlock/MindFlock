/** Keyboard engine — port of app/110-keyboard.js (global keymap, Ctrl+K
 * chords, user overrides) plus the override-store helpers the "?" sheet
 * needs (_comboLabel/_comboProblem/_sameCombo/_defaultCombosFor from
 * app/240-palette-shortcuts.js).
 *
 * Global keyboard shortcuts — one table-driven keymap, VSCode-aligned.
 *
 * The desktop (Electron) shell freed the combos browsers reserve, so the
 * primary bindings mirror VSCode: Ctrl+W close, Ctrl+Shift+T reopen,
 * Ctrl+N new, Ctrl+Tab / Ctrl+1..9 to switch, Ctrl+K C commit,
 * Ctrl+P / Ctrl+Shift+P palette. The browser-safe aliases (Alt+N,
 * Alt+1..9, Delete) stay for the served web UI, where Chrome won't let a
 * page intercept Ctrl+W / Ctrl+N / Ctrl+Tab / Ctrl+1..9 / Ctrl+Shift+T.
 *
 * Matching rules: `mod` true = Ctrl (or ⌘ on Mac); "ctrl" = the Ctrl key
 * specifically (Ctrl+Tab must not claim ⌘+Tab, the macOS app switcher).
 * shift/alt must equal the entry's value ("any" opts out; default false —
 * so AltGr, which sets Ctrl+Alt together, never triggers a mod-only
 * entry). A `when` guard returning false lets the keystroke fall through
 * to the terminal/page instead of being swallowed. `help` rows feed the
 * "?" cheat-sheet, so the sheet can't drift from the real bindings; alias
 * entries leave help unset. */

import {
  commitSession,
  copySession,
  hideSession,
  ideSession,
  killSession,
  makePrSession,
  pushSession,
  selectRailKey,
  selectSession,
  undoLastClose,
} from "./sessionActions";
import { openFastTrackMenu } from "./laneActions";
import { toast } from "./toast";
import { notePrefWrite } from "./prefs";
import { useUi, type DialogName } from "../state/store";

/** One key combination as matched by the dispatcher and stored in the
 * "mf_keymap" overrides ({key, mod, shift, alt}). `mod` true = Ctrl/⌘;
 * "ctrl" = the physical Ctrl key only. shift/alt: "any" opts out of the
 * exact-match rule. */
export interface Combo {
  key: string;
  mod?: boolean | "ctrl";
  shift?: boolean | "any";
  alt?: boolean | "any";
}

/** A keymap table row. `id` marks an entry the "?" sheet lets the user
 * rebind (the id keys the localStorage override). `aliasOf` ties a
 * browser-safe duplicate to its primary — the alias retires once the
 * primary is customized. `pairOf` marks a Shift-inverse partner that
 * follows the primary's custom combo with Shift added. */
export interface KeymapEntry extends Combo {
  id?: string;
  aliasOf?: string;
  pairOf?: string;
  /** Fallback description for conflict messages on help-less entries. */
  label?: string;
  /** [group, keysLabel, description] — feeds the "?" cheat-sheet. */
  help?: [string, string, string];
  /** Guard: false lets the keystroke fall through to the terminal/page. */
  when?: () => boolean;
  run: (e: KeyboardEvent) => void;
}

/** Cross-cutting actions the keymap can't import cleanly (they live in
 * components the integrator owns: sidebar order, palette, sheet, settings
 * screens). Injected via installKeymap() and the palette's props. */
export interface KeymapHost {
  /** Ctrl+P / Ctrl+Shift+P — open/close the command palette. */
  togglePalette(): void;
  /** "?" — open/close the shortcuts cheat sheet. */
  toggleShortcuts(): void;
  /** "/" — focus the sidebar filter box (#session-filter). */
  focusFilter(): void;
  /** Ctrl+Tab / Ctrl+PgDn — next/previous rail row in STABLE sidebar order
   * (selecting never reorders it, so repeated presses tour the list —
   * sessions and windows alike, one list). */
  cycleWindow(dir: 1 | -1): void;
  /** Order key of the Nth rail row (0-based, sessions and windows in one
   * stable sidebar order — a session title or a window's sentinel), for
   * Ctrl+1..9 / Alt+1..9; null when there is no Nth row. */
  rowAt(index: number): string | null;
  /** Palette "Open Doctor" — jump to Settings → Doctor. */
  openDoctor(): void;
}

let _host: KeymapHost | null = null;

// --- Guards ----------------------------------------------------------------

export function isEditingTarget(el: Element | null): boolean {
  if (!el) return false;
  const tag = el.tagName;
  return (
    tag === "INPUT" ||
    tag === "TEXTAREA" ||
    tag === "SELECT" ||
    (el as HTMLElement).isContentEditable === true
  );
}

/** The keyboard is in the Thread tab's composer (grid/ThreadTab.tsx) — a
 * text box where you are writing to an agent. Ctrl+W there is a word-delete
 * habit, never "end this session": the composer exempts itself from the
 * close binding, the way isEditingTarget already exempts it from Delete. */
export function threadComposerFocused(): boolean {
  const el = document.activeElement as HTMLElement | null;
  return !!el && isEditingTarget(el) && !!el.closest?.(".thread-compose");
}

/** Is the keyboard anywhere inside a Thread tab — its buttons (All/Reports,
 * Show older, Show all, a worker's Review diff) keep the focus after a
 * click, and Delete / Ctrl+W pressed next must not end the orchestrator
 * behind them (the Map tab's `.cm-root` rule). */
export function threadTabFocused(): boolean {
  const el = document.activeElement as HTMLElement | null;
  return !!el?.closest?.(".thread-root");
}

export function terminalFocused(): boolean {
  const el = document.activeElement;
  return !!(el && el.closest && el.closest(".xterm"));
}

/** Modal dialogs during which Ctrl+W / Delete must not kill the session
 * behind: the store's dialog slot, plus a DOM fallback for modals that
 * still live outside the store (vanilla partials / addons). */
const MODAL_DIALOG_NAMES: DialogName[] = [
  "new-session",
  "recent",
  "commit",
  "rename",
  "device",
  // The Intake reads like a page, not a popover, and its per-card Remove buttons make
  // a stray Delete genuinely dangerous behind it.
  "intake",
  // Customize's checkboxes are about the rail, and the Prompts dialog has text
  // fields where Ctrl+W is muscle memory — neither may end the focused session
  // behind the dialog. (The waiting rows' Retry / Skip / Commit live in the
  // bell: see "notif-pop" in MODAL_DOM_IDS.)
  "customize",
  "prompts",
  // Same shape as Intake: a full-page surface with per-plan Delete buttons, and
  // nothing about it suggests the session behind is still taking keystrokes.
  "verify",
  // An extension's dialog body is arbitrary UI (forms, editable grids) — a
  // stray Delete or Ctrl+W meant for it must never reach the session behind.
  "extension",
  // Red zones…: per-zone × Remove buttons (Delete is the natural "remove this"
  // key) and pattern inputs where Ctrl+W is muscle memory — both would end the
  // focused session's agent behind the dialog.
  "red-zones",
];
const MODAL_DOM_IDS = [
  "new-dialog",
  "recent-dialog",
  "commit-dialog",
  "rename-dialog",
  "device-dialog",
  "intake-dialog",
  "customize-dialog",
  "prompts-dialog",
  "verify-dialog",
  "red-zones-dialog",
  // The take-a-break screen owns the whole window and holds the keyboard on
  // its own buttons; a Delete meant for the card must not reach the session
  // running behind it.
  "break-screen",
  // The ⏩ fast-track picker holds the keyboard for its arrows and letters; a
  // Delete or Ctrl+W pressed at it must not end the session it hangs from.
  "fast-track-menu",
  // The Prompts bar's menu holds the keyboard for its arrows the same way.
  "prompts-menu",
  // The bell holds the waiting rows (Retry / Skip / Commit / Raise budget /
  // Start the workers) about OTHER sessions; a stray Delete or Ctrl+W on one
  // of its buttons must never end the focused session behind it. The popover
  // only exists while the bell is open.
  "notif-pop",
];
export function modalOpen(): boolean {
  const open = useUi.getState().openDialog;
  if (open && MODAL_DIALOG_NAMES.includes(open)) return true;
  return MODAL_DOM_IDS.some((id) => {
    const el = document.getElementById(id);
    return !!el && !el.classList.contains("hidden");
  });
}

// --- User-customized bindings ------------------------------------------------
// Rebinding lives in the "?" sheet (click a row, press the new keys) and is
// cached in localStorage — and, since "Your devices", it follows the person:
// every save is mirrored to the server's prefs.keymap (lib/prefs.ts), which
// settings sync carries to your other computers. To keep one machine's
// keyboard different (a laptop without the keys your desk has), pin
// prefs.keymap in Settings → Devices → "Kept different on this device". Shape: { keys: {id: [{key,mod,shift,alt}, …]},
// chords: {defaultLetter: newSecondKey} }. An action can hold several
// combos (the sheet's "+" appends one). Anything missing or unparseable
// means "the defaults below"; a bare object (the pre-array format) is
// normalized to a one-combo array.

interface KeyOverrides {
  keys: Record<string, Combo[]>;
  chords: Record<string, string>;
}

/** Action ids that are gone. A rebind saved for one is dropped on load: it
 * would bind nothing, yet still count as a customisation ("Reset all") and
 * leave its old combo looking taken. "outbox" was Alt+O, for a Customize tab
 * that no longer exists — what it listed is in the bell and the group headers. */
const RETIRED_KEY_IDS = ["outbox"];

let _keyOv: KeyOverrides = { keys: {}, chords: {} };

/** Read the overrides from localStorage (startup, and again when an adopted
 * pref replaced them — reloadKeymap). Drops retired ids and normalizes the
 * pre-array format, writing the cleaned copy back. */
function _loadKeyOv() {
  _keyOv = { keys: {}, chords: {} };
  try {
    const v = JSON.parse(localStorage.getItem("mf_keymap") || "{}") || {};
    if (v.keys && typeof v.keys === "object") _keyOv.keys = v.keys;
    if (v.chords && typeof v.chords === "object") _keyOv.chords = v.chords;
    let migrated = false;
    for (const id of RETIRED_KEY_IDS) {
      if (id in _keyOv.keys) {
        delete _keyOv.keys[id];
        migrated = true;
      }
    }
    Object.keys(_keyOv.keys).forEach((k) => {
      if (!Array.isArray(_keyOv.keys[k])) {
        _keyOv.keys[k] = [_keyOv.keys[k] as unknown as Combo];
        migrated = true;
      }
    });
    if (migrated) localStorage.setItem("mf_keymap", JSON.stringify(_keyOv));
  } catch {
    /* defaults */
  }
}
_loadKeyOv();

function _saveKeyOv() {
  try {
    localStorage.setItem("mf_keymap", JSON.stringify(_keyOv));
  } catch {
    /* storage unavailable */
  }
  notePrefWrite("mf_keymap");
}

// Change notification so the "?" sheet's labels live-update (works with
// useSyncExternalStore: subscribeKeymap + keymapVersion).
let _version = 0;
const _subs = new Set<() => void>();
export function subscribeKeymap(cb: () => void): () => void {
  _subs.add(cb);
  return () => {
    _subs.delete(cb);
  };
}
export function keymapVersion(): number {
  return _version;
}
function _notify() {
  _version++;
  _subs.forEach((cb) => cb());
}

/** Re-read the overrides after localStorage was replaced underneath us (a
 * keymap adopted from another of your devices) and repaint every listener. */
export function reloadKeymap() {
  _loadKeyOv();
  _notify();
}

/** The user's custom combos for an action id, or undefined for defaults. */
export function getKeyOverride(id: string): Combo[] | undefined {
  return _keyOv.keys[id];
}
/** Replace an action's combos (the sheet's "set"/"add" both end here). */
export function setKeyCombos(id: string, combos: Combo[]) {
  _keyOv.keys[id] = combos;
  _saveKeyOv();
  _notify();
}
export function resetKeyOverride(id: string) {
  delete _keyOv.keys[id];
  _saveKeyOv();
  _notify();
}
/** Set a chord's second key; the default letter restores the default. */
export function setChordKey(id: string, key: string) {
  if (key === id) delete _keyOv.chords[id];
  else _keyOv.chords[id] = key;
  _saveKeyOv();
  _notify();
}
export function resetChordOverride(id: string) {
  delete _keyOv.chords[id];
  _saveKeyOv();
  _notify();
}
export function resetAllOverrides() {
  _keyOv = { keys: {}, chords: {} };
  _saveKeyOv();
  _notify();
}
export function hasOverrides(): boolean {
  return !!Object.keys(_keyOv.keys).length || !!Object.keys(_keyOv.chords).length;
}

// Non-null-ish while the "?" sheet is recording a new combo — the global
// dispatcher below must stay out of the way so recording a combo can never
// trigger the action it names.
let _rebindCapturing = false;
export function setRebindCapturing(on: boolean) {
  _rebindCapturing = on;
}

// --- Ctrl+K chords -----------------------------------------------------------
// VSCode's chord prefix; git verbs need two deliberate keystrokes so no
// single mistyped combo can push a branch or open a PR. Like VSCode, the
// prefix claims Ctrl+K even while a terminal is focused — readline's
// kill-line is the accepted cost (Ctrl+U still kills the whole line, and
// Esc cancels a pending chord).

export interface ChordEntry {
  desc: string;
  run: (title: string) => void;
}

export const CHORDS: Record<string, ChordEntry> = {
  c: { desc: "Commit…", run: (t) => commitSession(t) },
  // Plain `git push` over the user's own remote — SSH or HTTPS, whatever they
  // configured. Never gated on the GitHub CLI.
  p: { desc: "Push", run: (t) => pushSession(t) },
  // Stays bound whether or not gh/a token is present: makePrSession degrades to
  // GitHub's prefilled compare page, so the chord never dead-ends. Gating it on
  // a capability would just make the shortcut silently stop working.
  r: { desc: "Make PR", run: (t) => makePrSession(t) },
  o: { desc: "Open / focus IDE", run: (t) => ideSession(t) },
  d: { desc: "Duplicate session", run: (t) => copySession(t) },
  h: { desc: "Hide / show window", run: (t) => hideSession(t) },
  // The Map tab: red zones, the plan, the blast radius. Switching tabs from
  // outside the pane goes through lastTab, which the pane follows.
  m: {
    desc: "Code map",
    run: (t) => {
      selectSession(t, { noKeyboard: true });
      useUi.getState().setLastTab(t, "map");
    },
  },
  // Message… opens the Thread composer addressed to the focused session —
  // what you type there goes in as YOU, never through the agents' mailbox.
  // Ctrl+K F opens the pane's ⏩ fast-track picker (how far MindFlock carries
  // the session; each item acts at once, nothing is pasted), Ctrl+K T the
  // Thread itself.
  s: { desc: "Message…", run: (t) => useUi.getState().threadOpen(t, { composeTo: t }) },
  f: { desc: "Fast-track…", run: (t) => openFastTrackMenu(t) },
  t: { desc: "Thread — workers and messages", run: (t) => useUi.getState().threadOpen(t) },
};

/** Effective second key for a chord action: the user's override or the
 * action's default letter (which doubles as its stable id). */
export function chordKeyFor(id: string): string {
  return String(_keyOv.chords[id] || id).toLowerCase();
}

/** The chord whose user-chosen key took `id`'s DEFAULT letter, or null.
 * A chord added later (S, F, T) can find its letter already given to
 * another action by a rebinding made before it existed: the user's own
 * choice wins (the "?" sheet refuses to create such a clash, so this only
 * comes from history), and the sheet shows the newer chord as taken so it
 * can be given a free key — never silently unreachable. */
export function chordShadowedBy(id: string): string | null {
  if (_keyOv.chords[id]) return null;
  const key = chordKeyFor(id);
  return Object.keys(CHORDS).find((c) => c !== id && !!_keyOv.chords[c] && chordKeyFor(c) === key) ?? null;
}

/** The chord a second key runs: a user's explicit binding first, then the
 * defaults — whatever order the table lists them in. */
export function chordForKey(pressed: string): string | undefined {
  const ids = Object.keys(CHORDS);
  return (
    ids.find((k) => !!_keyOv.chords[k] && chordKeyFor(k) === pressed) ??
    ids.find((k) => !_keyOv.chords[k] && chordKeyFor(k) === pressed)
  );
}

let _chordPending = false;
let _chordTimer: ReturnType<typeof setTimeout> | undefined;
function _enterChord() {
  _chordPending = true;
  toast("Ctrl+K — waiting for the second key… (Esc cancels)", { duration: 3000 });
  clearTimeout(_chordTimer);
  _chordTimer = setTimeout(() => {
    _chordPending = false;
  }, 3000);
}
function _handleChordKey(e: KeyboardEvent) {
  const key = e.key || "";
  // Releasing/holding the prefix modifiers isn't the second key, and
  // neither is the prefix's own key-repeat while Ctrl+K is still held.
  if (key === "Control" || key === "Meta" || key === "Shift" || key === "Alt") return;
  if (e.repeat && key.toLowerCase() === "k") {
    e.preventDefault();
    return;
  }
  _chordPending = false;
  clearTimeout(_chordTimer);
  e.preventDefault();
  e.stopPropagation();
  if (key === "Escape") return;
  const pressed = key.toLowerCase();
  const cid = chordForKey(pressed);
  const chord = cid ? CHORDS[cid] : undefined;
  if (!chord) {
    toast("Ctrl+K " + key.toUpperCase() + " isn’t bound — press ? for shortcuts");
    return;
  }
  const focused = useUi.getState().focused;
  if (!focused) {
    toast("No focused session");
    return;
  }
  chord.run(focused);
}

// --- The keymap table --------------------------------------------------------

export const KEYMAP: KeymapEntry[] = [
  // -- Navigation ---------------------------------------------------------
  {
    key: "p",
    mod: true,
    shift: "any",
    id: "palette",
    help: ["Navigation", "Ctrl+P / Ctrl+Shift+P", "Command palette"],
    run: () => _host?.togglePalette(),
  },
  {
    key: "b",
    mod: true,
    id: "sidebar",
    help: ["Navigation", "Ctrl+B", "Toggle sidebar"],
    run: () => useUi.getState().toggleSidebar(),
  },
  {
    key: "n",
    mod: true,
    id: "new",
    help: ["Navigation", "Ctrl+N / Alt+N", "New session"],
    run: () => useUi.getState().openDialogFor("new-session"),
  },
  // browser-safe alias
  { key: "n", alt: true, aliasOf: "new", run: () => useUi.getState().openDialogFor("new-session") },
  {
    // Alt rather than Ctrl: Ctrl+I *is* Tab at the terminal, and every other
    // free Ctrl+letter either belongs to the shell or to the browser. Alt+I is
    // free and spells the thing.
    key: "i",
    alt: true,
    id: "intake",
    help: ["Navigation", "Alt+I", "Intake — tickets, PRs and issues"],
    // Guarded unlike Alt+N: on macOS Option+I types a dead-key accent, and a
    // surface you open a few times an hour is not worth eating a keystroke
    // someone meant for a text field or a terminal.
    when: () => !isEditingTarget(document.activeElement),
    run: () => useUi.getState().openDialogFor("intake"),
  },
  {
    // Alt for the same reasons as Alt+I next door — Ctrl+V is paste and always
    // will be — and guarded the same way: Option+V on macOS types √, and a
    // surface you visit a few times a day must not eat a keystroke aimed at a
    // text field or a terminal.
    key: "v",
    alt: true,
    id: "verify",
    help: ["Navigation", "Alt+V", "Verify — shipped changes nobody has checked"],
    when: () => !isEditingTarget(document.activeElement),
    run: () => useUi.getState().openDialogFor("verify"),
  },
  {
    key: "Tab",
    mod: "ctrl",
    id: "cycle",
    help: ["Navigation", "Ctrl+Tab / Ctrl+Shift+Tab", "Next / previous window"],
    run: () => _host?.cycleWindow(1),
  },
  { key: "Tab", mod: "ctrl", shift: true, pairOf: "cycle", run: () => _host?.cycleWindow(-1) },
  {
    key: "PageDown",
    mod: "ctrl",
    help: ["Navigation", "Ctrl+PgDn / Ctrl+PgUp", "Next / previous window (also)"],
    run: () => _host?.cycleWindow(1),
  },
  { key: "PageUp", mod: "ctrl", run: () => _host?.cycleWindow(-1) },
  {
    key: "/",
    id: "filter",
    help: ["Navigation", "/", "Filter sessions (when the list is long)"],
    when: () => {
      if (isEditingTarget(document.activeElement)) return false;
      // Inside a Map tab, `/` is that map's find box (CodeMapTab handles it).
      if ((document.activeElement as HTMLElement | null)?.closest?.(".cm-root")) return false;
      const box = document.getElementById("sidebar-search");
      return !!box && !box.classList.contains("hidden");
    },
    run: () => _host?.focusFilter(),
  },
  {
    key: "?",
    shift: "any",
    id: "sheet",
    help: ["Navigation", "?", "This shortcut sheet"],
    when: () => !isEditingTarget(document.activeElement),
    run: () => _host?.toggleShortcuts(),
  },
  // -- View ----------------------------------------------------------------
  // Ctrl+R reloads ONLY while no terminal is focused: with a terminal
  // focused the key falls through to the shell as reverse-i-search (\x12),
  // matching VSCode's "the terminal owns the keys" rule. The desktop
  // shell's Ctrl+Shift+R reload works from anywhere.
  {
    key: "r",
    mod: true,
    id: "reload",
    help: ["View", "Ctrl+R", "Reload the app (when a terminal isn’t focused)"],
    when: () => !terminalFocused(),
    run: () => location.reload(),
  },
  // -- Focused session -----------------------------------------------------
  // Commit lives on the Ctrl+K C chord (with the other git verbs) — see
  // CHORDS above.
  { key: "k", mod: true, label: "the Ctrl+K chord prefix", run: () => _enterChord() }, // chord prefix
  {
    key: "w",
    mod: true,
    id: "close",
    // "Session", not "window": the Alt+1..9 rows above use "window" for any
    // rail row (the assistant, a log tail), and this one only ever ends the
    // focused SESSION — a selected window never takes keyboard focus.
    help: ["Focused session", "Ctrl+W / Delete", "End the focused session (undo: Ctrl+Shift+T)"],
    when: () =>
      !!useUi.getState().focused && !modalOpen() && !threadComposerFocused() && !threadTabFocused(),
    run: () => {
      const f = useUi.getState().focused;
      if (f) killSession(f);
    },
  },
  {
    key: "Delete",
    shift: "any",
    aliasOf: "close", // browser-safe alias
    // Not from inside a Map tab: its cards and per-zone × Remove buttons are
    // focusable, a click there focuses this session, and Delete is the natural
    // "remove this" key — it must never end the agent (the Red zones dialog is
    // guarded the same way, via modalOpen). Ctrl+W still works there.
    when: () =>
      !!useUi.getState().focused &&
      !modalOpen() &&
      !isEditingTarget(document.activeElement) &&
      !(document.activeElement as HTMLElement | null)?.closest?.(".cm-root") &&
      !threadTabFocused(),
    run: () => {
      const f = useUi.getState().focused;
      if (f) killSession(f);
    },
  },
  {
    key: "t",
    mod: true,
    shift: true,
    id: "reopen",
    help: ["Focused session", "Ctrl+Shift+T / Ctrl+Z", "Reopen the last-closed session"],
    run: () => undoLastClose(),
  },
  {
    // Ctrl+Z (⌘Z) also reopens — but only outside a terminal/text field, so it
    // never steals the terminal's suspend (SIGTSTP) or an input's native undo.
    // xterm's helper textarea counts as an editing target, so terminals are
    // excluded automatically.
    key: "z",
    mod: true,
    aliasOf: "reopen",
    when: () => !isEditingTarget(document.activeElement),
    run: () => undoLastClose(),
  },
];
// Ctrl+1..9 (VSCode focus-group style) and Alt+1..9 (browser-safe; matches
// the sidebar number badges) focus the Nth rail row in stable sidebar order —
// a session or a window (the assistant, a log tail, a database table), which
// is why the dispatch goes through selectRailKey rather than selectSession.
"123456789".split("").forEach((d, i) => {
  const when = () => !!(_host && _host.rowAt(i));
  const run = () => {
    const t = _host?.rowAt(i);
    if (t) selectRailKey(t);
  };
  const help: [string, string, string] | undefined =
    i === 0 ? ["Navigation", "Ctrl+1 … 9 / Alt+1 … 9", "Focus the Nth window"] : undefined;
  KEYMAP.push({ key: d, mod: true, when, run, help, label: "Focus the Nth window" });
  KEYMAP.push({ key: d, alt: true, when, run, label: "Focus the Nth window" });
});

/** Resolve an entry's *effective* triggers under the user's overrides (see
 * the id/aliasOf/pairOf notes on KEYMAP) — an array, since an action can
 * hold several custom combos. Defaults return [b] itself, so "is this a
 * custom combo" stays a cheap t !== b check. Empty means the entry is
 * retired by a customization. */
export function effBindings(b: KeymapEntry): Combo[] {
  if (b.id && _keyOv.keys[b.id]) return _keyOv.keys[b.id];
  if (b.aliasOf && _keyOv.keys[b.aliasOf]) return [];
  if (b.pairOf && _keyOv.keys[b.pairOf]) {
    return _keyOv.keys[b.pairOf].map((o) => ({
      key: o.key,
      mod: o.mod,
      shift: true,
      alt: o.alt,
    }));
  }
  return [b];
}

// --- Display / rebinding helpers ----------------------------------------------

// Human label for a stored combo ({key,mod,shift,alt} → "Ctrl+Shift+X").
// `mod` renders as Ctrl to match the rest of the sheet (it means ⌘ too).
const KEY_DISP: Record<string, string> = {
  PageDown: "PgDn",
  PageUp: "PgUp",
  Escape: "Esc",
  " ": "Space",
  ArrowUp: "↑",
  ArrowDown: "↓",
  ArrowLeft: "←",
  ArrowRight: "→",
};
export function comboLabel(c: Combo): string {
  const k = KEY_DISP[c.key] || (c.key.length === 1 ? c.key.toUpperCase() : c.key);
  return (c.mod ? "Ctrl+" : "") + (c.shift === true ? "Shift+" : "") + (c.alt ? "Alt+" : "") + k;
}

/** Why a combo can't be used, or null if it's fine. Bare characters would
 * swallow normal typing; Shift is reserved on paired actions for the
 * reverse direction; and anything colliding with an active binding
 * (defaults, other customs, the Ctrl+K prefix) would be dead on arrival. */
export function comboProblem(c: Combo, selfId: string): string | null {
  if (!c.mod && !c.alt && c.key.length === 1)
    return "Include Ctrl or Alt — a bare key would fire while you type";
  if (c.shift && KEYMAP.some((x) => x.pairOf === selfId))
    return "Shift is reserved here for the reverse direction — pick a combo without Shift";
  for (const b of KEYMAP) {
    if (b.id === selfId || b.aliasOf === selfId || b.pairOf === selfId) continue;
    for (const t of effBindings(b)) {
      if (t.key !== c.key) continue;
      if (!!t.mod !== !!c.mod) continue;
      if (t.shift !== "any" && !!t.shift !== !!c.shift) continue;
      if (t.alt !== "any" && !!t.alt !== !!c.alt) continue;
      const src = b.help
        ? b
        : KEYMAP.find((x) => x.help && x.id && x.id === (b.aliasOf || b.pairOf));
      const what = (src && src.help && src.help[2]) || b.label || "another shortcut";
      return comboLabel(c) + " is already " + what;
    }
  }
  return null;
}

export function sameCombo(a: Combo, b: Combo): boolean {
  // Normalize shift so a default's `undefined` equals a capture's `false`
  // ("any" only equals "any" — it matches more keystrokes than false does).
  const s = (x: boolean | "any" | undefined) => (x === "any" ? "any" : !!x);
  return a.key === b.key && !!a.mod === !!b.mod && s(a.shift) === s(b.shift) && !!a.alt === !!b.alt;
}

/** The default combos for an action — its primary entry plus any
 * browser-safe aliases — used to seed "+ add" so adding an extra combo
 * keeps the defaults working instead of silently replacing them. */
export function defaultCombosFor(id: string): Combo[] {
  return KEYMAP.filter((x) => x.id === id || x.aliasOf === id).map((x) => ({
    key: x.key,
    mod: x.mod,
    shift: x.shift,
    alt: x.alt,
  }));
}

// --- The dispatcher -----------------------------------------------------------

/** The take-a-break screen (Settings → General) is a full-window overlay with
 * exactly two answers on it. Every global shortcut has to go quiet while it is
 * up: the surfaces they open are plain `.modal`s with no z-index, so they land
 * UNDER its opaque scrim — Ctrl+P opened an invisible palette that then took
 * every keystroke, and Enter in it could push or delete the focused session.
 *
 * Excludes the leaving state: the flock is flying home by then, the scrim has
 * faded and the app is clickable again, so it must be typeable again too. */
function breakScreenUp(): boolean {
  const el = document.getElementById("break-screen");
  return !!el && !el.classList.contains("break-leaving");
}

function _dispatch(e: KeyboardEvent) {
  if (_rebindCapturing) return; // the "?" sheet is recording a new combo
  if (breakScreenUp()) return; // nothing behind the break card is reachable
  if (_chordPending) {
    _handleChordKey(e);
    return;
  }
  const key = e.key || "";
  const norm = key.length === 1 ? key.toLowerCase() : key;
  const mod = e.ctrlKey || e.metaKey;
  for (const b of KEYMAP) {
    for (const t of effBindings(b)) {
      if (t.key !== norm) continue;
      if (t.mod === "ctrl" ? !e.ctrlKey : t.mod ? !mod : mod) continue;
      if (t.shift !== "any" && !!t.shift !== e.shiftKey) continue;
      if (t.alt !== "any" && !!t.alt !== e.altKey) continue;
      // A CUSTOM bare-key combo (no Ctrl/Alt — an F-key, Insert, …) must
      // never fire while typing: the default bare keys ("/", "?", Delete)
      // carry their own editing guards, but an override can't inherit them.
      // xterm's hidden helper textarea makes terminals count as editing too.
      if (t !== b && !t.mod && !t.alt && isEditingTarget(document.activeElement)) continue;
      if (b.when && !b.when()) return; // bound but guarded — let the key through
      e.preventDefault();
      e.stopPropagation();
      b.run(e);
      return;
    }
  }
}

/** Install the capture-phase document dispatcher (fires even while an xterm
 * pane holds keyboard focus). Returns a cleanup function. */
export function installKeymap(host: KeymapHost): () => void {
  _host = host;
  document.addEventListener("keydown", _dispatch, true); // capture
  return () => {
    document.removeEventListener("keydown", _dispatch, true);
    if (_host === host) _host = null;
    _chordPending = false;
    clearTimeout(_chordTimer);
  };
}
