/** The task a session was started with, remembered by this browser from the
 * New dialog's create — what the pane's "Task:" line shows until the agent's
 * own transcript names its latest prompt (`last_prompt`, served by the
 * provider: an agent without a transcript reader never sends one). A display
 * fallback only: capped, best-effort, and the server's answer always wins. */

const KEY = "mf_session_tasks";
const CAP = 60;

function load(): Record<string, string> {
  try {
    const v = JSON.parse(localStorage.getItem(KEY) || "{}");
    return v && typeof v === "object" ? (v as Record<string, string>) : {};
  } catch {
    return {};
  }
}

/** Remember `task` (first line, ≤200 chars) as what `title` was asked to do. */
export function rememberTask(title: string, task: string): void {
  const line = String(task || "").trim().split("\n")[0].trim().slice(0, 200);
  if (!title || !line) return;
  try {
    const m = load();
    delete m[title];
    m[title] = line;
    const keys = Object.keys(m);
    for (const k of keys.slice(0, Math.max(0, keys.length - CAP))) delete m[k];
    localStorage.setItem(KEY, JSON.stringify(m));
  } catch {
    /* storage off: the line just waits for the transcript */
  }
}

export function rememberedTask(title: string): string {
  return load()[title] || "";
}
