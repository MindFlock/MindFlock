/** Saved prompts = the templates that are a prompt (lib/templates.ts): one
 * list, on the server, synced between devices. This module is the
 * synchronous view the New-session "Saved prompt…" picker, the sidebar
 * Prompts bar and the Prompts dialog read — every function keeps its old
 * signature, answering from the templates cache and pushing writes to
 * `/api/templates` behind the scenes. The per-browser list this used to keep
 * in localStorage is migrated to the server once (templates.ts). */

import { normalizePresets, presetKey } from "./prefs";
import {
  TEMPLATES_CHANGED,
  deleteTemplate,
  ensureTemplates,
  promptTemplates,
  isPromptOnly,
  saveTemplate,
  setTemplates,
  templatesLoaded,
  templatesNow,
  type Template,
} from "./templates";

export interface Preset {
  name: string;
  prompt: string;
}

/** The legacy per-browser key — only the migration reads it now. */
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

/** Fired on document after every change (same event as TEMPLATES_CHANGED),
 * so a surface that lists the saved prompts repaints when another edits
 * them — or when the server's list lands. */
export const PRESETS_CHANGED = TEMPLATES_CHANGED;

/** The saved prompts as this page knows them. The first call on a page
 * starts the fetch (the answer arrives as a PRESETS_CHANGED event); until
 * then the mirror from last time, or nothing. */
export function loadUserPresets(): Preset[] {
  if (!templatesLoaded()) void ensureTemplates().catch(() => undefined);
  return promptTemplates(templatesNow());
}

/** Write the saved list: whatever is missing from it is deleted on the
 * server, whatever is new or changed is saved. Names are trimmed and a name
 * repeated ignoring case keeps only its newest (later) entry — the server's
 * rule is "a preset IS its name". Returns the names that were dropped, so
 * the caller can say so. The cache is updated at once; the server calls
 * follow. */
export function saveUserPresets(list: Preset[]): string[] {
  const { list: clean, dropped } = normalizePresets(list);
  const current = promptTemplates(templatesNow());
  const keep = new Set(clean.map((p) => presetKey(p.name)));
  for (const p of current)
    if (!keep.has(presetKey(p.name))) void deleteTemplate(p.name).catch(() => undefined);
  for (const p of clean) {
    const old = current.find((q) => presetKey(q.name) === presetKey(p.name));
    if (!old || old.name !== p.name || old.prompt !== p.prompt)
      void saveTemplate(templateFor(p)).catch(() => undefined);
  }
  // Optimistic: the picker shows the list it was just handed, not last
  // fetch's, and the server answers with the same list a moment later.
  const others = templatesNow().filter((t) => !isPromptOnly(t));
  setTemplates([...others, ...clean.map(templateFor)]);
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
  const existing = templatesNow().find((t) => presetKey(t.name) === k);
  // A full template (program, folder…) that shares the name keeps its other
  // fields: this is "save this prompt under that name", not "forget the rest".
  void saveTemplate({ ...(existing || {}), name: n, prompt }).catch(() => undefined);
  setTemplates([
    ...templatesNow().filter((t) => presetKey(t.name) !== k),
    { ...(existing || {}), name: n, prompt },
  ]);
  return { list, replaced: hit && hit.name.trim() !== n ? hit.name : null };
}

/** The server keeps one template per name already, so there is nothing to
 * fold any more. Kept for the callers that ran it before a pull. */
export function tidyUserPresets(): string[] {
  return [];
}

function templateFor(p: Preset): Template {
  const existing = templatesNow().find((t) => presetKey(t.name) === presetKey(p.name));
  return { ...(existing || {}), name: p.name, prompt: p.prompt };
}

/** Option values are "b:<name>" / "u:<name>" so the two namespaces can share
 * a name without colliding. */
export function findPreset(value: string): Preset | null {
  const m = /^([bu]):([\s\S]*)$/.exec(value || "");
  if (!m) return null;
  const list = m[1] === "b" ? BUILTIN_PRESETS : loadUserPresets();
  return list.find((p) => p.name === m[2]) || null;
}
