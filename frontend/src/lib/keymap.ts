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

import { selectRailKey, undoLastClose } from "./sessionActions";
import { CHORD_ACTIONS, chordDesc, runChordAction, sessionAction, type SessionActionId } from "./sessionActionTable";
import { ASK_WORKERS_KEYS, FLOCK_KEYS } from "./workersRequest";
import { toast } from "./toast";
import { notePrefWrite } from "./prefs";
import { useUi } from "../state/store";
import { VERIFY_BLURB } from "./vocab";
import { jumpToNeedsYou } from "./needsYou";

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
  /** Push-to-talk style: run(e, "down") once per physical press (key-repeat
   * ignored), then run(e, "up") when that key or one of its modifiers is
   * released — or run(null, "up") when the window loses focus mid-hold. */
  holdable?: boolean;
  run: (e: KeyboardEvent | null, phase?: HoldPhase) => void;
}

export type HoldPhase = "down" | "up";

/** The talk hotkey's contract with the top-bar mic (TopBarMic): a window
 * event whose detail.phase is "down" or "up". The mic reads it through its
 * own push-to-talk mode — hold: down listens, up ends the phrase; toggle:
 * each down flips, up is ignored. */
export const VOICE_TALK_EVENT = "mindflock:voice-talk";

function voiceTalk(phase: HoldPhase) {
  if (typeof window === "undefined") return;
  window.dispatchEvent(new CustomEvent(VOICE_TALK_EVENT, { detail: { phase } }));
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

/** Ctrl+W / Delete must never end the session behind an open dialog or
 * overlay. This used to be an allow-list of dialog names and element ids, and
 * every surface added since (the palette, Settings, Make PR, Setup, To-do,
 * the shortcut sheet, the templates modal, an extension's dialog…) was a hole
 * in it until someone lost a session through it. So the check is now generic:
 *
 *   1. ANY dialog in the store's slot counts (every DialogName is an overlay;
 *      none of them wants the session behind it ended).
 *   2. ANY visible element that reads as a dialog in the DOM counts:
 *      `[role="dialog"]`, `.modal`, `.dialog`, `.palette`, or a `data-modal`
 *      attribute — which also catches vanilla partials and addon dialogs the
 *      store never hears about.
 *   3. A short list of popovers that hold the keyboard without being dialogs
 *      (the bell, the ⏩ picker, the Prompts menu, the break screen) — they are
 *      not modal in markup, but a Delete pressed at one of their buttons is
 *      about THEM, never about the focused session.
 *
 * Visible = present, no `hidden` attribute, no `.hidden` class on it or an
 * ancestor, and not `display:none` where computed style is available. */
export const OVERLAY_SELECTOR = '[role="dialog"], .modal, .dialog, .palette, [data-modal]';
const POPOVER_IDS = ["break-screen", "fast-track-menu", "prompts-menu", "notif-pop"];

function elementVisible(el: Element): boolean {
  const h = el as HTMLElement;
  if (h.hidden === true || h.hasAttribute?.("hidden")) return false;
  if (h.classList?.contains("hidden")) return false;
  if (h.closest?.(".hidden, [hidden]")) return false;
  const win = typeof window !== "undefined" ? window : null;
  if (win && typeof win.getComputedStyle === "function") {
    try {
      const cs = win.getComputedStyle(h);
      if (cs.display === "none" || cs.visibility === "hidden") return false;
    } catch {
      /* a detached or foreign node: count it */
    }
  }
  return true;
}

/** A dialog, overlay or keyboard-holding popover is on screen. */
export function overlayOpen(): boolean {
  if (useUi.getState().openDialog) return true;
  const doc = typeof document !== "undefined" ? document : null;
  if (!doc) return false;
  if (typeof doc.querySelectorAll === "function") {
    const nodes = doc.querySelectorAll(OVERLAY_SELECTOR);
    for (let i = 0; i < nodes.length; i++) if (elementVisible(nodes[i])) return true;
  }
  if (typeof doc.getElementById === "function") {
    for (const id of POPOVER_IDS) {
      const el = doc.getElementById(id);
      if (el && elementVisible(el)) return true;
    }
  }
  return false;
}

/** Kept under its old name: callers (and tests) ask "is a modal open". */
export function modalOpen(): boolean {
  return overlayOpen();
}

/** The keyboard is in a place where a key means TEXT, not a command: an input,
 * textarea, select or contenteditable (isEditingTarget), or anywhere inside a
 * terminal — xterm's helper textarea is one of those, but a click on the
 * terminal's canvas can leave focus on a wrapper, and Ctrl+W typed at a shell
 * is delete-word, not "end this session". */
export function typingTarget(el: Element | null): boolean {
  if (!el) return false;
  if (isEditingTarget(el)) return true;
  return !!el.closest?.(".xterm, .xterm-helper-textarea");
}

/** The one guard both Ctrl+W and Delete consult: a focused session, no dialog
 * or overlay open, and the keyboard not in a text field or a terminal. Exported
 * so the test can drive it with a fake document. */
export function endShortcutAllowed(): boolean {
  if (!useUi.getState().focused) return false;
  if (overlayOpen()) return false;
  const active = typeof document !== "undefined" ? (document.activeElement as Element | null) : null;
  if (typingTarget(active)) return false;
  // Not from inside a Map tab: its cards and per-zone × Remove buttons are
  // focusable, a click there focuses this session, and Delete is the natural
  // "remove this" key — it must never end the agent.
  if ((active as HTMLElement | null)?.closest?.(".cm-root")) return false;
  if (threadTabFocused()) return false;
  return true;
}

// --- User-customized bindings ------------------------------------------------
// Rebinding lives in the "?" sheet (click a row, press the new keys) and is
// cached in localStorage — and, since "Your devices", it follows the person:
// every save is mirrored to the server's prefs.keymap (lib/prefs.ts), which
// settings sync carries to your other computers. To keep one machine's
// keyboard different (a laptop without the keys your desk has), pin
// prefs.keymap in Settings → Your devices → "Kept different on this device". Shape: { keys: {id: [{key,mod,shift,alt}, …]},
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
  /** Runs on the focused session — or, for a `global` chord, with the focused
   * session (or null) as where to start from. */
  run: (title: string | null) => void;
  /** Needs no focused session (Ctrl+K N). */
  global?: boolean;
  /** The "?" sheet's group; default "Focused session". */
  group?: string;
}

/** Built from the session action table (lib/sessionActionTable), which names
 * each action's chord letter, label and enable rule — the row menu, the
 * palette and these chords read the same row. `desc` is a getter so the
 * table is read when the "?" sheet draws, not at import. Then the one chord
 * that is not a session verb: N, the next session that needs you. */
export const CHORDS: Record<string, ChordEntry> = {
  ...Object.fromEntries(
    CHORD_ACTIONS.map((id: SessionActionId) => [
      sessionAction(id).chord!,
      {
        get desc() {
          return chordDesc(id);
        },
        run: (t: string | null) => {
          if (t) runChordAction(id, t);
        },
      },
    ])
  ),
  n: {
    desc: "Next session that needs you — in the bell's order",
    global: true,
    group: "Navigation",
    run: (t) => jumpToNeedsYou(t),
  },
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
  if (chord.global) {
    chord.run(focused);
    return;
  }
  if (!focused) {
    toast("No focused session");
    return;
  }
  chord.run(focused);
}

// --- The keymap table --------------------------------------------------------

/** Alt+G / Alt+Shift+G: a focused session, no dialog over it, and the keyboard
 * in its terminal or nowhere in particular — never a text field's keystroke. */
export function workersKeyAllowed(): boolean {
  if (!useUi.getState().focused) return false;
  if (overlayOpen()) return false;
  return terminalFocused() || !isEditingTarget(document.activeElement);
}

/** Ctrl+W / Delete: the table's Close, on the focused session. */
function closeFocused() {
  const f = useUi.getState().focused;
  if (f) sessionAction("close").run(f);
}

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
    help: ["Navigation", "Alt+V", "Verify — " + VERIFY_BLURB],
    when: () => !isEditingTarget(document.activeElement),
    run: () => useUi.getState().openDialogFor("verify"),
  },
  {
    // The top bar's next word, on the same terms as Alt+I / Alt+V: Ctrl+S is
    // "save page" in a browser tab, Alt+S is free in the app, the desktop shell
    // and the pane find bar (Alt+C / W / R), and guarded the same way — Option+S
    // types ß on macOS.
    key: "s",
    alt: true,
    id: "schedule",
    help: ["Navigation", "Alt+S", "Schedule — templates that start sessions on their own"],
    when: () => !isEditingTarget(document.activeElement),
    run: () => useUi.getState().openDialogFor("schedule"),
  },
  {
    // Talk to the focused session through the top-bar mic. Unlike Alt+I / V /
    // S it has NO editing guard: the point is to focus a terminal and talk,
    // and xterm's helper textarea is an editing target. The dispatcher
    // prevents the default, so neither a terminal (where Ctrl+T is readline's
    // transpose-chars) nor a text field ever sees the keystroke — the mic
    // types the words into a focused field itself.
    key: "t",
    mod: true,
    id: "talk",
    holdable: true,
    help: ["Navigation", "Ctrl+T", "Talk to the focused session — hold or press, per the mic's push-to-talk mode"],
    run: (_e, phase) => voiceTalk(phase || "down"),
  },
  {
    // Browser-safe alias: Chrome won't let a page intercept Ctrl+T (new tab).
    // Option+Space would type a non-breaking space on macOS, but the default
    // is prevented here too. Retires, like every alias, once Talk is rebound.
    key: " ",
    alt: true,
    aliasOf: "talk",
    holdable: true,
    run: (_e, phase) => voiceTalk(phase || "down"),
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
    // rail row (the assistant, a log tail), and this one only ever closes the
    // focused SESSION — a selected window never takes keyboard focus.
    // Close moves it to Recently closed (Ctrl+Shift+T brings it back).
    help: [
      "Focused session",
      "Ctrl+W / Delete",
      "Close the focused session — moves it to Recently closed (not while a dialog is open or you are typing; undo: Ctrl+Shift+T)",
    ],
    when: () => endShortcutAllowed() && !threadComposerFocused(),
    run: () => closeFocused(),
  },
  {
    key: "Delete",
    shift: "any",
    aliasOf: "close", // browser-safe alias
    when: () => endShortcutAllowed(),
    run: () => closeFocused(),
  },
  {
    // MindFlock workers without typing the ask (lib/workersRequest). Alt, like
    // Alt+I / V / S: Ctrl+G is the pane find bar's "next match" and Ctrl+W is
    // Close; Alt+W is already that find bar's "whole word" while it is open,
    // and Alt+F opens Chrome's menu on Windows. Alt+G is free in Chrome,
    // Electron and the pane find bar on Windows, macOS and Linux (Option+G
    // types © on a Mac, and bash's rarely used M-g glob-complete — both
    // prevented here). Unlike Alt+I it fires FROM a terminal — that is where
    // you are when you want workers — but a text field outside one keeps the
    // keystroke, and nothing happens behind a dialog.
    key: "g",
    alt: true,
    id: "workers",
    help: ["Focused session", ASK_WORKERS_KEYS, "Ask for workers — types the workers request into the focused session (finish it, or press Enter)"],
    when: () => workersKeyAllowed(),
    run: () => {
      const f = useUi.getState().focused;
      if (f) runChordAction("askWorkers", f);
    },
  },
  {
    // Its sticky twin: Flock mode adds the request to every prompt sent there.
    // Its own id (not a pairOf) so the sheet lists and rebinds it on its own.
    key: "g",
    alt: true,
    shift: true,
    id: "flock",
    help: ["Focused session", FLOCK_KEYS, "Workers on every prompt — turn the focused session's Flock on or off"],
    when: () => workersKeyAllowed(),
    run: () => {
      const f = useUi.getState().focused;
      if (f) runChordAction("flock", f);
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

/** Every live combo for an action (its own entry and its browser-safe
 * aliases, under the user's rebinds), as labels — what a tooltip should
 * advertise. Pick with `usable` to drop the ones this context can't press. */
export function shortcutLabels(id: string, usable: (label: string) => boolean = () => true): string[] {
  const out: string[] = [];
  for (const b of KEYMAP) {
    if (b.id !== id && b.aliasOf !== id) continue;
    for (const c of effBindings(b)) {
      const l = comboLabel(c);
      if (usable(l) && !out.includes(l)) out.push(l);
    }
  }
  return out;
}

// Human label for a stored combo ({key,mod,shift,alt} → "Ctrl+Shift+X").
// `mod` renders as Ctrl to match the rest of the sheet (it means ⌘ too).
const KEY_DISP: Record<string, string> = {
  PageDown: "PgDn",
  PageUp: "PgUp",
  Escape: "Esc",
  " ": "Space",
  "\u00a0": "Space",
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
      if (normKey(t.key) !== normKey(c.key)) continue;
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
  return (
    normKey(a.key) === normKey(b.key) && !!a.mod === !!b.mod && s(a.shift) === s(b.shift) && !!a.alt === !!b.alt
  );
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

/** A key as the table stores it: letters lower-cased, and macOS's Option+Space
 * (which reports the non-breaking space it would type) read as Space — on
 * both sides, since the sheet's recorder may have stored either. */
function normKey(key: string): string {
  if (key === "\u00a0") return " ";
  return key.length === 1 ? key.toLowerCase() : key;
}

// Holdable actions currently down, with the combo that pressed them — so the
// release is matched against the binding that actually fired (rebinds
// included), and an "up" is only ever sent after a "down".
const _held = new Map<KeymapEntry, Combo>();

function _release(b: KeymapEntry, e: KeyboardEvent | null) {
  if (!_held.delete(b)) return;
  b.run(e, "up");
}

/** Window blur mid-hold: the keyup will land in another window, so let go of
 * everything now rather than leave the mic listening. */
function _releaseAll() {
  for (const b of Array.from(_held.keys())) _release(b, null);
}

const MOD_RELEASED: Record<string, (t: Combo) => boolean> = {
  Alt: (t) => t.alt === true,
  Shift: (t) => t.shift === true,
  Control: (t) => !!t.mod,
  Meta: (t) => t.mod === true,
};

function _dispatchUp(e: KeyboardEvent) {
  // Never gated by the sheet's recorder or the break screen: a press that
  // began before either appeared must still end.
  if (!_held.size) return;
  const key = e.key || "";
  const norm = normKey(key);
  for (const [b, t] of Array.from(_held)) {
    if (normKey(t.key) === norm) {
      e.preventDefault();
      e.stopPropagation();
      _release(b, e);
    } else if (MOD_RELEASED[key]?.(t)) {
      // Letting go of the modifier first ends the hold too (and on macOS the
      // main key's own keyup never arrives while ⌘ is down). The modifier's
      // keyup is left alone for everyone else.
      _release(b, e);
    }
  }
}

function _dispatch(e: KeyboardEvent) {
  if (_rebindCapturing) return; // the "?" sheet is recording a new combo
  if (breakScreenUp()) return; // nothing behind the break card is reachable
  if (_chordPending) {
    _handleChordKey(e);
    return;
  }
  const norm = normKey(e.key || "");
  const mod = e.ctrlKey || e.metaKey;
  for (const b of KEYMAP) {
    for (const t of effBindings(b)) {
      if (normKey(t.key) !== norm) continue;
      if (t.mod === "ctrl" ? !e.ctrlKey : t.mod ? !mod : mod) continue;
      if (t.shift !== "any" && !!t.shift !== e.shiftKey) continue;
      if (t.alt !== "any" && !!t.alt !== e.altKey) continue;
      // A CUSTOM bare-key combo (no Ctrl/Alt — an F-key, Insert, …) must
      // never fire while typing: the default bare keys ("/", "?", Delete)
      // carry their own editing guards, but an override can't inherit them.
      // xterm's hidden helper textarea makes terminals count as editing too —
      // except for a holdable action (talk), which is meant to be pressed
      // from a terminal; the sheet already refuses bare printable keys.
      if (t !== b && !t.mod && !t.alt && isEditingTarget(document.activeElement) && !(b.holdable && terminalFocused()))
        continue;
      if (b.when && !b.when()) return; // bound but guarded — let the key through
      e.preventDefault();
      e.stopPropagation();
      if (b.holdable) {
        if (e.repeat) return; // auto-repeat while held: still one press
        _release(b, e); // a fresh press whose release we never saw ends that one first
        _held.set(b, t);
        b.run(e, "down");
        return;
      }
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
  document.addEventListener("keyup", _dispatchUp, true);
  const win = typeof window !== "undefined" ? window : null;
  win?.addEventListener("blur", _releaseAll);
  return () => {
    document.removeEventListener("keydown", _dispatch, true);
    document.removeEventListener("keyup", _dispatchUp, true);
    win?.removeEventListener("blur", _releaseAll);
    _releaseAll();
    if (_host === host) _host = null;
    _chordPending = false;
    clearTimeout(_chordTimer);
  };
}
