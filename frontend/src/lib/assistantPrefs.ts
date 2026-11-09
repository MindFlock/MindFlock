/** The browser-only preferences the voice assistant can see and set — the
 * ones that live in this page (the store, localStorage, the mic's own
 * prefs), not in the server's settings (those are the server's `setting`
 * action). Each one says what it is called on screen, what it holds now and
 * how to set it, so the assistant can do "turn the birds off" or "show the
 * sidebar" without a click path.
 *
 * Kept small and curated: a pref the owner would say out loud, with the
 * label the screen uses (Settings → Appearance, the mic's options). */

import { useUi, type ViewMode } from "../state/store";
import { SKY_MODES, type SkyMode } from "./skyLayout";
import { isLightNow, setTheme } from "./theme";
import { loadVoicePrefs, saveVoicePrefs, type RepliesMode } from "./voiceDesktop";
import type { SnapshotPref } from "./voiceAssistant";

export interface AssistantPref {
  key: string;
  label: string;
  type: "bool" | "select" | "number";
  value(): unknown;
  options?: string[];
  /** Number prefs: the range. */
  min?: number;
  max?: number;
  set(value: never): void;
}

const bool = (key: string, label: string, get: () => boolean, set: (on: boolean) => void): AssistantPref => ({
  key,
  label,
  type: "bool",
  value: get,
  set: set as (v: never) => void,
});

const select = <T extends string>(key: string, label: string, options: readonly T[], get: () => T, set: (v: T) => void): AssistantPref => ({
  key,
  label,
  type: "select",
  options: [...options],
  value: get,
  set: set as (v: never) => void,
});

const num = (key: string, label: string, min: number, max: number, get: () => number, set: (n: number) => void): AssistantPref => ({
  key,
  label,
  type: "number",
  min,
  max,
  value: get,
  set: set as (v: never) => void,
});

const ui = () => useUi.getState();
const voice = (patch: Partial<ReturnType<typeof loadVoicePrefs>>) => saveVoicePrefs({ ...loadVoicePrefs(), ...patch });

/** The catalog, read fresh (values are live). */
export function assistantPrefs(): AssistantPref[] {
  return [
    bool("sky_birds", "Birds in the Sky", () => ui().skyBirds, (on) => ui().setSkyBirds(on)),
    bool("sidebar", "Sidebar shown", () => !ui().sidebarHidden, (on) => {
      if (on === ui().sidebarHidden) ui().toggleSidebar();
    }),
    select("view", "Main view (grid of panes or the Sky)", ["grid", "sky"] as const, () => ui().view, (v) => ui().setView(v)),
    select("sky_mode", "Sky shows (" + SKY_MODES.map((m) => m.id + " = " + m.label).join(", ") + ")", SKY_MODES.map((m) => m.id), () => ui().skyMode, (m: SkyMode) => ui().setSkyMode(m)),
    select("grid_panes", "Panes in the grid", ["auto", "2", "4", "9"] as const, () => ui().viewMode, (v: ViewMode) => ui().setViewMode(v)),
    select("theme", "Light or dark", ["dark", "light"] as const, () => (isLightNow() ? "light" : "dark"), (v) => setTheme(v === "light")),
    bool("reduce_motion", "Reduce motion", () => ui().reduceMotion, (on) => ui().setReduceMotion(on)),
    bool("break_reminder", "Take a break (reminders)", () => ui().breakReminder, (on) => ui().setBreakReminder(on)),
    num("break_every", "Minutes between break reminders", 5, 240, () => ui().breakEveryMin, (n) => ui().setBreakEveryMin(n)),
    bool("idle_flock", "Idle flock", () => ui().idleFlock, (on) => ui().setIdleFlock(on)),
    num("idle_after", "Minutes idle before the flock appears", 1, 120, () => ui().idleFlockAfterMin, (n) => ui().setIdleFlockAfterMin(n)),
    bool("hints", "Getting-started hints", () => ui().hintsEnabled, (on) => ui().setHintsEnabled(on)),
    select("read_replies", "Read replies aloud (off, mine, all = everything)", ["off", "mine", "all"] as const, () => loadVoicePrefs().replies, (v: RepliesMode) => voice({ replies: v })),
    bool("spoken_confirmations", "Spoken confirmations", () => loadVoicePrefs().confirm !== false, (on) => voice({ confirm: on })),
    bool("mic_sounds", "Mic sounds (chimes)", () => loadVoicePrefs().sounds !== false, (on) => voice({ sounds: on })),
    select("push_to_talk", "Push to talk (toggle = click, hold)", ["toggle", "hold"] as const, () => loadVoicePrefs().ptt, (v) => voice({ ptt: v })),
  ];
}

/** The snapshot's `prefs`: each pref's key, label, type, value, options. */
export function prefSnapshot(list: AssistantPref[] = assistantPrefs()): SnapshotPref[] {
  return list.map((p) => {
    let value: unknown = null;
    try {
      value = p.value();
    } catch {
      value = null;
    }
    return p.options ? { key: p.key, label: p.label, type: p.type, value, options: p.options } : { key: p.key, label: p.label, type: p.type, value };
  });
}

const ON = new Set(["true", "on", "yes", "1", "show", "shown", "enable", "enabled"]);
const OFF = new Set(["false", "off", "no", "0", "hide", "hidden", "disable", "disabled"]);

/** `value` as `p` takes it, or undefined when it can't be (a select's
 * option is matched case-insensitively, by id or its label word). */
export function coercePref(p: AssistantPref, value: unknown): unknown {
  if (p.type === "bool") {
    if (typeof value === "boolean") return value;
    const s = String(value ?? "").trim().toLowerCase();
    if (ON.has(s)) return true;
    if (OFF.has(s)) return false;
    return undefined;
  }
  if (p.type === "number") {
    const n = typeof value === "number" ? value : Number(String(value ?? "").trim());
    if (!Number.isFinite(n)) return undefined;
    return Math.min(p.max ?? n, Math.max(p.min ?? n, Math.round(n)));
  }
  const s = String(value ?? "").trim().toLowerCase();
  const opts = p.options || [];
  const hit = opts.find((o) => o.toLowerCase() === s);
  if (hit) return hit;
  // The words on screen: "This session" → one, "Needs you" → perched.
  if (p.key === "sky_mode") return SKY_MODES.find((m) => m.label.toLowerCase() === s)?.id;
  if (p.key === "read_replies" && s === "everything") return "all";
  if (p.key === "push_to_talk" && (s === "click" || s === "press")) return "toggle";
  return undefined;
}

const shown = (v: unknown) => (v === true ? "on" : v === false ? "off" : String(v));

/** Set pref `key` to `value`: a chip line ("Birds in the Sky: off"), or why
 * not. */
export function setAssistantPref(key: string, value: unknown, list: AssistantPref[] = assistantPrefs()): { ok: boolean; text: string } {
  const p = list.find((x) => x.key === key);
  if (!p) return { ok: false, text: "No preference called " + key };
  const v = coercePref(p, value);
  if (v === undefined) return { ok: false, text: p.label + " can't be " + shown(value) };
  try {
    p.set(v as never);
  } catch (err) {
    return { ok: false, text: "Couldn't set " + p.label + ": " + ((err as Error)?.message || "error") };
  }
  return { ok: true, text: p.label.replace(/\s*\(.*\)$/, "") + ": " + shown(v) };
}
