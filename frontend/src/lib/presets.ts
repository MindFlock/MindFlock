/** J4 prompt-preset store (port of app.js section 23's store): built-ins +
 * user-saved presets in localStorage "mindflock.prompt_presets". Shared by
 * the New-session preset picker and the Prompts library dialog. The saved
 * list follows the person: each save is mirrored to prefs.prompt_presets
 * (lib/prefs.ts) and arrives on your other devices through settings sync. */

import { normalizePresets, notePrefWrite, presetKey } from "./prefs";

export interface Preset {
  name: string;
  prompt: string;
}

export const PRESET_STORE_KEY = "mindflock.prompt_presets";

export const BUILTIN_PRESETS: Preset[] = [
  {
    name: "Fix failing tests",
    prompt:
      "Run the test suite, find the failing tests, and fix the underlying " +
      "causes. Do not weaken, skip, or delete tests just to make them pass.",
  },
  {
    name: "Address PR review comments",
    prompt:
      "Look up the open pull request for this branch, read every unresolved " +
      "review comment, and address each one with a code change (or explain why " +
      "no change is needed).",
  },
  {
    name: "Write tests for recent changes",
    prompt:
      "Inspect the most recent commits and the working tree, then write " +
      "focused tests covering the changed behavior. Run them and make them pass.",
  },
  {
    name: "Refactor for clarity — no behavior change",
    prompt:
      "Refactor the code you touch for clarity and simplicity WITHOUT " +
      "changing behavior. Keep the public API stable and keep all tests green.",
  },
];

export function loadUserPresets(): Preset[] {
  try {
    const arr = JSON.parse(localStorage.getItem(PRESET_STORE_KEY) || "[]");
    return Array.isArray(arr)
      ? arr.filter(
          (p) => p && typeof p.name === "string" && p.name && typeof p.prompt === "string"
        )
      : [];
  } catch {
    return [];
  }
}

/** Fired on document after every save, so a surface that lists the saved
 * prompts (the sidebar Prompts bar) repaints when another one edits them. */
export const PRESETS_CHANGED = "mf-presets-changed";

/** Write the saved list. Names are trimmed and a name repeated ignoring case
 * keeps only its newest (later) entry — the server's rule is "a preset IS its
 * name", and a list it would shorten silently comes back shortened on the
 * next pull. Returns the names that were dropped, so the caller can say so. */
export function saveUserPresets(list: Preset[]): string[] {
  const { list: clean, dropped } = normalizePresets(list);
  try {
    localStorage.setItem(PRESET_STORE_KEY, JSON.stringify(clean));
  } catch {
    /* storage unavailable */
  }
  notePrefWrite(PRESET_STORE_KEY);
  if (typeof document !== "undefined") document.dispatchEvent(new Event(PRESETS_CHANGED));
  return dropped;
}

/** Save one prompt under `name`, replacing any saved prompt with the same
 * name ignoring case ("deploy" replaces "Deploy"). `replaced` is the old
 * entry's name when its spelling differed — worth telling the person, since
 * the other one is gone. */
export function upsertUserPreset(
  name: string,
  prompt: string
): { list: Preset[]; replaced: string | null } {
  const n = name.trim();
  const k = presetKey(n);
  const old = loadUserPresets();
  const hit = old.find((p) => presetKey(p.name) === k);
  const list = old.filter((p) => presetKey(p.name) !== k);
  list.push({ name: n, prompt });
  saveUserPresets(list);
  return { list, replaced: hit && hit.name.trim() !== n ? hit.name : null };
}

/** A list saved before names were compared ignoring case may hold "Deploy"
 * and "deploy" both; the server keeps only one. Fold it here first (keeping
 * the newer) and report what went, instead of letting the next pull drop one
 * silently. Returns [] and writes nothing when the list is already clean. */
export function tidyUserPresets(): string[] {
  let raw: unknown;
  try {
    raw = JSON.parse(localStorage.getItem(PRESET_STORE_KEY) || "[]");
  } catch {
    return [];
  }
  if (!Array.isArray(raw)) return [];
  const { list, dropped } = normalizePresets(raw as Preset[]);
  if (!dropped.length) return [];
  saveUserPresets(list);
  return dropped;
}

/** Option values are "b:<name>" / "u:<name>" so the two namespaces can share
 * a name without colliding. */
export function findPreset(value: string): Preset | null {
  const m = /^([bu]):([\s\S]*)$/.exec(value || "");
  if (!m) return null;
  const list = m[1] === "b" ? BUILTIN_PRESETS : loadUserPresets();
  return list.find((p) => p.name === m[2]) || null;
}
