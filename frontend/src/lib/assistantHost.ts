/** The voice assistant's hands and eyes on this page: what it can see (the
 * {@link assistantSnapshot}: the view, the open dialog and Settings screen,
 * the focused session, the command palette's commands, the clickable
 * things on screen, the browser-only prefs) and what it can do (the
 * {@link execUiAction} executors for every UiAction of the contract) —
 * "anything I can do by hand".
 *
 * The DOM parts (the clickables scan, the click's flash, typing into the
 * focused field) are split into pure helpers over a duck-typed element so
 * vitest covers them in node: {@link clickableName}, {@link pickLabel},
 * {@link scanClickables}, {@link typeInto}. */

import type { Config } from "../api/types";
import { buildPaletteActions } from "../components/palette/CommandPalette";
import type { PaletteAction } from "../components/palette/paletteRank";
import type { SettingsScreen } from "../components/settings/SettingsDialog";
import { extensionsNow, queryClient } from "../state/queries";
import { displayName, useUi, type DialogName } from "../state/store";
import { prefSnapshot, setAssistantPref } from "./assistantPrefs";
import { keymapHost, type KeymapHost } from "./keymap";
import { openSettings } from "./openSettings";
import { lastSettingsScreen } from "./settingsLastScreen";
import { instances, selectSession } from "./sessionActions";
import { SESSION_ACTIONS, actionCtx, actionLabel, runChordAction, sessionAction, type SessionActionId } from "./sessionActionTable";
import { setView } from "./viewMode";
import {
  SNAPSHOT_CLICKABLES_MAX,
  SNAPSHOT_COMMANDS_MAX,
  type AssistantChip,
  type ExecResult,
  type Snapshot,
  type UiAction,
} from "./voiceAssistant";

// --- what it may open ------------------------------------------------------------ //

/** The dialogs `{do:"open"}` takes, with the name a chip says. The ones
 * that need a session (commit, rename, move…) are session actions instead. */
export const ASSISTANT_DIALOGS: Partial<Record<DialogName, string>> = {
  "new-session": "New session",
  settings: "Settings",
  intake: "Intake",
  customize: "Customize",
  verify: "Verify",
  recent: "Recently closed",
  prompts: "Prompts",
  setup: "the Setup checklist",
  todo: "To-do",
  "assistant-agent": "Assistant instructions",
  palette: "the command palette",
  shortcuts: "Keyboard shortcuts",
  "red-zones": "Zones",
  schedule: "Schedule",
};

/** Settings' screens (SettingsDialog SCREENS keys), with their labels. */
export const ASSISTANT_SETTINGS_SCREENS: Record<Exclude<SettingsScreen, "traffic">, string> = {
  general: "General",
  connections: "Connections",
  notifications: "Notifications",
  coding: "Agents",
  providers: "Custom agents",
  accounts: "Accounts",
  localmodel: "Local model",
  orchestration: "Agent orchestration",
  workspace: "Workspace",
  ide: "IDE",
  security: "Security",
  appearance: "Appearance",
  devices: "Your devices",
  mobile: "Your phone",
  voice: "Voice",
  peer: "People",
  extensions: "Extensions",
  doctor: "Doctor",
  logs: "System logs",
  advanced: "Engine & updates",
};

// --- names ----------------------------------------------------------------------- //

/** A label as compared: lower case, no ellipsis, punctuation or extra space. */
export function normLabel(s: string): string {
  return String(s || "")
    .toLowerCase()
    .replace(/[’`]/g, "'")
    .replace(/…|\.\.\./g, "")
    .replace(/[^\p{L}\p{N}' ]+/gu, " ")
    .replace(/\s+/g, " ")
    .trim();
}

/** The best of `names` for `label`: the exact one, else the same words
 * (case, punctuation, "…" aside), else the one that starts with them, else
 * the one that contains them; -1 when none. Ties go to the first (screen
 * order). */
export function pickLabel(names: readonly string[], label: string): number {
  const exact = names.indexOf(label);
  if (exact >= 0) return exact;
  const want = normLabel(label);
  if (!want) return -1;
  const ns = names.map(normLabel);
  const same = ns.indexOf(want);
  if (same >= 0) return same;
  const starts = ns.findIndex((n) => n.startsWith(want + " ") || (want.length >= 4 && n.startsWith(want)));
  if (starts >= 0) return starts;
  return want.length >= 3 ? ns.findIndex((n) => n.includes(want)) : -1;
}

/** A session by what the assistant called it: its title, or its rail name. */
export function resolveSession(name: string, titles: readonly string[], nameOf: (t: string) => string = displayName): string | null {
  if (titles.includes(name)) return name;
  const want = normLabel(name);
  const hit = titles.find((t) => normLabel(t) === want) || titles.find((t) => normLabel(nameOf(t)) === want);
  return hit || null;
}

// --- the commands ---------------------------------------------------------------- //

const NO_HOST: KeymapHost = {
  togglePalette: () => useUi.getState().openDialogFor("palette"),
  toggleShortcuts: () => useUi.getState().openDialogFor("shortcuts"),
  focusFilter: () => {},
  cycleWindow: () => {},
  rowAt: () => null,
  openDoctor: () => openSettings("doctor"),
};

/** The palette's commands right now, built as the palette builds them —
 * with it closed. Focus rows are left out (focus is its own action), and
 * so is "All commands…" (the palette's own). */
export function paletteCommands(): PaletteAction[] {
  try {
    const acts = buildPaletteActions({
      config: queryClient.getQueryData<Config>(["config"]),
      host: keymapHost() || NO_HOST,
      extensions: extensionsNow(),
    });
    const seen = new Set<string>();
    return acts.filter((a) => {
      if (a.label.startsWith("Focus: ") || seen.has(a.label)) return false;
      seen.add(a.label);
      return true;
    });
  } catch {
    return [];
  }
}

// --- the clickables -------------------------------------------------------------- //

/** What the scan reads of an element (a DOM Element has all of it). */
export interface ElLike {
  tagName: string;
  textContent: string | null;
  getAttribute(name: string): string | null;
  closest?(sel: string): ElLike | null;
  getClientRects?(): { length: number };
  labels?: ArrayLike<{ textContent: string | null }> | null;
  disabled?: boolean;
  type?: string;
  value?: string;
  id?: string;
  ownerDocument?: { getElementById(id: string): { textContent: string | null } | null } | null;
}

export const CLICKABLE_SELECTOR = [
  "button",
  "a[href]",
  "summary",
  'input[type="checkbox"]',
  'input[type="radio"]',
  'input[type="button"]',
  'input[type="submit"]',
  '[role="button"]',
  '[role="tab"]',
  '[role="link"]',
  '[role="menuitem"]',
  '[role="menuitemradio"]',
  '[role="menuitemcheckbox"]',
  '[role="checkbox"]',
  '[role="switch"]',
  '[role="radio"]',
  '[role="option"]',
].join(",");

const squash = (s: string | null | undefined) => String(s || "").replace(/\s+/g, " ").trim();
const NAME_MAX = 80;

/** An element's accessible name, as a person would say it: aria-label,
 * aria-labelledby, a form control's label, its text, its title. "" when it
 * has none worth saying (no letter or digit: "×", "›"). */
export function clickableName(el: ElLike): string {
  let name = squash(el.getAttribute("aria-label"));
  if (!name) {
    const by = squash(el.getAttribute("aria-labelledby"));
    if (by && el.ownerDocument)
      name = squash(
        by
          .split(" ")
          .map((id) => el.ownerDocument?.getElementById(id)?.textContent || "")
          .join(" ")
      );
  }
  const tag = String(el.tagName || "").toLowerCase();
  if (!name && tag === "input") {
    if (el.type === "button" || el.type === "submit") name = squash(el.value);
    else {
      const lab = el.labels && el.labels.length ? el.labels[0].textContent : el.closest?.("label")?.textContent;
      name = squash(lab);
    }
  }
  if (!name) name = squash(el.textContent);
  if (!name) name = squash(el.getAttribute("title"));
  if (!/[\p{L}\p{N}]/u.test(name)) return "";
  return name.length > NAME_MAX ? name.slice(0, NAME_MAX - 1).trimEnd() + "…" : name;
}

/** Shown, enabled, not hidden from assistive tech, not the assistant's own
 * strip. */
export function clickableUsable(el: ElLike): boolean {
  if (el.disabled || el.getAttribute("aria-disabled") === "true") return false;
  if (el.closest?.('[aria-hidden="true"], [inert], #assist-strip')) return false;
  if (el.getClientRects && el.getClientRects().length === 0) return false;
  return true;
}

/** The root a scan looks in: the topmost open modal, else the page. */
export function scanRoot(doc: Document = document): ParentNode {
  const modals = Array.from(doc.querySelectorAll<HTMLElement>(".modal"));
  for (let i = modals.length - 1; i >= 0; i--) if (modals[i].getClientRects().length) return modals[i];
  return doc.body || doc;
}

/** The clickables under `root`: each named once (the first in screen order
 * wins), at most `max`. */
export function scanClickables(
  root: { querySelectorAll(sel: string): ArrayLike<ElLike> },
  max = SNAPSHOT_CLICKABLES_MAX
): Array<{ name: string; el: ElLike }> {
  const out: Array<{ name: string; el: ElLike }> = [];
  const seen = new Set<string>();
  const all = root.querySelectorAll(CLICKABLE_SELECTOR);
  for (let i = 0; i < all.length && out.length < max; i++) {
    const el = all[i];
    if (!clickableUsable(el)) continue;
    const name = clickableName(el);
    if (!name || seen.has(name)) continue;
    seen.add(name);
    out.push({ name, el });
  }
  return out;
}

/** How long a clicked thing is outlined (so the owner sees what was pressed). */
export const FLASH_MS = 900;

/** Click the visible thing called `label`: scrolled into view, flashed,
 * clicked. */
export function clickLabel(label: string, doc: Document = document): ExecResult {
  const found = scanClickables(scanRoot(doc) as unknown as { querySelectorAll(sel: string): ArrayLike<ElLike> });
  const i = pickLabel(
    found.map((f) => f.name),
    label
  );
  if (i < 0) return { ok: false, text: "Nothing on screen called “" + label + "”" };
  const el = found[i].el as unknown as HTMLElement;
  try {
    el.scrollIntoView?.({ block: "nearest", inline: "nearest" });
  } catch {
    /* old engine */
  }
  el.classList?.add("assist-flash");
  setTimeout(() => el.classList?.remove("assist-flash"), FLASH_MS);
  el.click();
  return { ok: true, text: "Pressed " + found[i].name };
}

// --- typing ---------------------------------------------------------------------- //

/** What {@link typeInto} needs of a text field. */
export interface FieldLike {
  value: string;
  selectionStart: number | null;
  selectionEnd: number | null;
  dispatchEvent(e: Event): boolean;
}

/** Insert `text` at the cursor of `el` the way React sees it: the value
 * goes through the prototype's setter (a controlled input's own setter would
 * swallow the change), then an `input` event bubbles. */
export function typeInto(el: FieldLike, text: string, makeEvent: () => Event = () => new Event("input", { bubbles: true })): void {
  const v = el.value || "";
  const start = el.selectionStart ?? v.length;
  const end = el.selectionEnd ?? v.length;
  const next = v.slice(0, start) + text + v.slice(end);
  const proto = Object.getPrototypeOf(el);
  const setter = proto ? Object.getOwnPropertyDescriptor(proto, "value")?.set : undefined;
  if (setter) setter.call(el, next);
  else el.value = next;
  try {
    el.selectionStart = el.selectionEnd = start + text.length;
  } catch {
    /* not a text input */
  }
  el.dispatchEvent(makeEvent());
}

/** The focused element when it takes text (never the terminal's hidden xterm
 * textarea: words there go to the session, which is the `send` action). */
function focusedField(): (HTMLInputElement | HTMLTextAreaElement) | null {
  const el = document.activeElement;
  if (!el || el.classList.contains("xterm-helper-textarea")) return null;
  if (el instanceof HTMLTextAreaElement) return el.readOnly || el.disabled ? null : el;
  if (el instanceof HTMLInputElement) {
    const ok = ["text", "search", "url", "email", ""].includes(el.type);
    return ok && !el.readOnly && !el.disabled ? el : null;
  }
  return null;
}

// --- the snapshot ---------------------------------------------------------------- //

/** What is on screen now (every field the contract has). */
export function assistantSnapshot(): Snapshot {
  const ui = useUi.getState();
  const dialog = ui.openDialog || null;
  let clickables: string[] = [];
  try {
    clickables = scanClickables(scanRoot() as unknown as { querySelectorAll(sel: string): ArrayLike<ElLike> }).map((c) => c.name);
  } catch {
    clickables = [];
  }
  return {
    view: ui.view,
    dialog,
    settings_screen: dialog === "settings" ? lastSettingsScreen() || ui.dialogTarget || null : null,
    focused: ui.focused || null,
    commands: paletteCommands()
      .map((a) => a.label)
      .slice(0, SNAPSHOT_COMMANDS_MAX),
    clickables,
    prefs: prefSnapshot(),
  };
}

// --- the executors --------------------------------------------------------------- //

/** Where an executor reaches the page (the defaults are the real ones;
 * tests pass fakes). */
export interface ExecEnv {
  openDialog(name: string, target?: string | null): void;
  openSettings(screen: string): void;
  closeDialog(): void;
  openDialogName(): string | null;
  setView(mode: "grid" | "sky"): void;
  titles(): string[];
  name(title: string): string;
  focus(title: string): void;
  commands(): PaletteAction[];
  sessionAction(id: string, title: string): ExecResult;
  click(label: string): ExecResult;
  pref(key: string, value: unknown): { ok: boolean; text: string };
  type(text: string): ExecResult;
}

export const realEnv: ExecEnv = {
  openDialog: (name, target = null) => useUi.getState().openDialogFor(name as DialogName, target),
  openSettings: (screen) => openSettings(screen as SettingsScreen),
  closeDialog: () => useUi.getState().closeDialog(),
  openDialogName: () => useUi.getState().openDialog,
  setView: (mode) => setView(mode),
  titles: () => instances().map((i) => i.title),
  name: displayName,
  focus: (title) => selectSession(title, { noKeyboard: true, voice: true }),
  commands: paletteCommands,
  sessionAction: (id, title) => {
    const sid = id as SessionActionId;
    const c = actionCtx(title);
    const label = actionLabel(sid, c).replace(/…$/, "");
    if (c.inst && !sessionAction(sid).enabled(c)) {
      runChordAction(sid, title); // says why, on screen
      return { ok: false, text: label + " isn’t available for " + displayName(title) };
    }
    runChordAction(sid, title);
    return { ok: true, text: label + " — " + displayName(title), session: title };
  },
  click: (label) => clickLabel(label),
  pref: (key, value) => setAssistantPref(key, value),
  type: (text) => {
    const el = focusedField();
    if (!el) return { ok: false, text: "No text box has the cursor" };
    typeInto(el, text);
    return { ok: true, text: "Typed “" + (text.length > 40 ? text.slice(0, 39) + "…" : text) + "”" };
  },
};

const SCREEN_PREFIX = "settings:";

/** A chip of the strip, clicked: back to what it did — the session, the
 * dialog, the Settings screen. */
export function chipJump(c: AssistantChip): void {
  if (c.session) selectSession(c.session, { voice: true });
  else if (c.open?.startsWith(SCREEN_PREFIX)) openSettings(c.open.slice(SCREEN_PREFIX.length) as SettingsScreen);
  else if (c.open) useUi.getState().openDialogFor(c.open as DialogName);
}

/** Perform one UiAction: what it did (a chip), or why not. Never throws. */
export function execUiAction(a: UiAction, env: ExecEnv = realEnv): ExecResult {
  try {
    switch (a.do) {
      case "open": {
        if (a.target.startsWith(SCREEN_PREFIX)) {
          const key = a.target.slice(SCREEN_PREFIX.length) as keyof typeof ASSISTANT_SETTINGS_SCREENS;
          const label = ASSISTANT_SETTINGS_SCREENS[key];
          if (!label) return { ok: false, text: "No Settings screen called " + key };
          env.openSettings(key);
          return { ok: true, text: "Opened Settings → " + label, open: a.target };
        }
        const label = ASSISTANT_DIALOGS[a.target as DialogName];
        if (!label) return { ok: false, text: "Can't open " + a.target };
        env.openDialog(a.target);
        return { ok: true, text: "Opened " + label, open: a.target };
      }
      case "close": {
        const was = env.openDialogName();
        if (!was) return { ok: true, text: "Nothing was open" };
        env.closeDialog();
        const label = was === "settings" ? "Settings" : ASSISTANT_DIALOGS[was as DialogName] || was;
        return { ok: true, text: "Closed " + label };
      }
      case "view":
        env.setView(a.mode);
        return { ok: true, text: a.mode === "sky" ? "Showing the Sky" : "Showing the grid" };
      case "focus": {
        const title = resolveSession(a.session, env.titles(), env.name);
        if (!title) return { ok: false, text: "No session called " + a.session };
        env.focus(title);
        return { ok: true, text: "Focused " + env.name(title), session: title };
      }
      case "command": {
        const acts = env.commands();
        const i = pickLabel(
          acts.map((x) => x.label),
          a.label
        );
        if (i < 0) return { ok: false, text: "No command called “" + a.label + "”" };
        acts[i].run();
        return { ok: true, text: acts[i].label.replace(/…$/, "") };
      }
      case "session_action": {
        if (!(a.action in SESSION_ACTIONS)) return { ok: false, text: "No session action called " + a.action };
        const title = resolveSession(a.session, env.titles(), env.name);
        if (!title) return { ok: false, text: "No session called " + a.session };
        return env.sessionAction(a.action, title);
      }
      case "click":
        return env.click(a.label);
      case "pref":
        return env.pref(a.key, a.value);
      case "type":
        return env.type(a.text);
    }
  } catch (err) {
    return { ok: false, text: "Couldn't do that: " + ((err as Error)?.message || "error") };
  }
  return { ok: false, text: "Can't do that" };
}
