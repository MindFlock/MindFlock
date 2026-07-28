/** J4 prompt-preset store (port of app.js section 23's store): built-ins +
 * user-saved presets in localStorage "mindflock.prompt_presets". Shared by
 * the New-session preset picker and the Prompts library dialog. */

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

export function saveUserPresets(list: Preset[]) {
  try {
    localStorage.setItem(PRESET_STORE_KEY, JSON.stringify(list));
  } catch {
    /* storage unavailable */
  }
}

/** Option values are "b:<name>" / "u:<name>" so the two namespaces can share
 * a name without colliding. */
export function findPreset(value: string): Preset | null {
  const m = /^([bu]):([\s\S]*)$/.exec(value || "");
  if (!m) return null;
  const list = m[1] === "b" ? BUILTIN_PRESETS : loadUserPresets();
  return list.find((p) => p.name === m[2]) || null;
}
