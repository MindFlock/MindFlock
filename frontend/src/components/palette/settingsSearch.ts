/** Settings search for the command palette: typing "github", "notification",
 * "worktree" or "parallel" offers "Settings → …" rows that open Settings on
 * the right screen (and, for a few, scroll to the right control).
 *
 * One entry per Settings screen — `key` is the screen key (typed against
 * SettingsDialog's registry, so a renamed key fails tsc), `label` is the
 * screen's nav label (a test pins the two against SettingsDialog.tsx) — plus
 * a few entries for a control people look for by name. `terms` are the words
 * people type for it, synonyms included; matching is plain substring/prefix,
 * no fuzz, so "parallel" never matches by scattered letters.
 *
 * Pure apart from `run`: the palette merges `settingsActions(query)` into
 * its own results. An empty query offers nothing (the palette's empty view
 * is its menu). */

import { openSettings, type SettingsScreen } from "../../lib/openSettings";

export interface SettingsSearchEntry {
  key: SettingsScreen;
  /** The screen's nav label, or "Screen → Control" for a control entry. */
  label: string;
  terms: readonly string[];
  /** A CSS selector inside the screen to scroll to (a folded <details>
   * around it is opened). */
  anchor?: string;
}

export const SETTINGS_SEARCH: readonly SettingsSearchEntry[] = [
  {
    key: "general",
    label: "General",
    terms: ["general", "hints", "tips", "tour", "tours", "replay", "help"],
  },
  {
    key: "general",
    label: "General → Budgets, resume and sleep",
    terms: ["budget", "cost", "spend", "usage resets", "resume", "revive", "restart sessions", "sleep", "idle", "memory", "scroll speed"],
    anchor: "#general-advanced",
  },
  {
    key: "connections",
    label: "Connections",
    terms: ["connections", "connect", "integrations", "github", "token", "tracker", "tickets", "shortcut", "jira", "linear", "asana", "sign in", "login", "signed in"],
  },
  {
    key: "connections",
    label: "Connections → GitHub",
    terms: ["github", "gh", "pull request", "pr", "token", "personal access token", "repo access"],
    anchor: '[data-conn="github"]',
  },
  {
    key: "notifications",
    label: "Notifications",
    terms: ["notifications", "notification", "notify", "alerts", "alert", "bell", "desktop", "mute", "ping", "sound"],
  },
  {
    key: "notifications",
    label: "Notifications → Phone alerts",
    terms: ["phone", "mobile", "push", "ntfy", "alerts on my phone", "notifications on phone"],
    anchor: "#notif-advanced",
  },
  {
    key: "coding",
    label: "Agents",
    terms: ["agents", "agent", "default agent", "claude", "codex", "aider", "cli", "launch flags", "flags", "permissions", "skip permissions", "sign in", "model"],
  },
  {
    key: "coding",
    label: "Agents → Sessions running at once",
    terms: ["parallel", "concurrency", "concurrent", "at once", "simultaneous", "max sessions", "how many", "session limit", "limit", "queue"],
    anchor: "#coding-sessions-at-once",
  },
  {
    key: "providers",
    label: "Custom agents",
    terms: ["custom agent", "add agent", "provider", "providers", "install agent", "binary", "path", "toml"],
  },
  {
    key: "accounts",
    label: "Accounts",
    terms: ["accounts", "account", "api key", "openrouter", "profile", "second account", "switch account"],
  },
  { key: "localmodel", label: "Local model", terms: ["local model", "ollama", "llm", "offline model"] },
  {
    key: "orchestration",
    label: "Agent orchestration",
    terms: ["orchestration", "mcp", "spawn", "sub-sessions", "fan out", "nesting", "parallel", "concurrency", "limits"],
  },
  {
    key: "workspace",
    label: "Workspace",
    terms: ["workspace", "worktree", "worktrees", "folder", "directory", "dir", "checkout", "branch", "base branch", "pr base"],
  },
  {
    key: "workspace",
    label: "Workspace → Where worktrees live",
    terms: ["where are worktrees", "worktree folder", "workspace folder", "path", "checkout location", "where are my files"],
    anchor: "#workspace-paths",
  },
  {
    key: "workspace",
    label: "Workspace → Worktree or clone",
    terms: ["worktree mode", "clone", "full clone", "copy the repo", "ticket sessions"],
    anchor: "#workspace-copy-mode",
  },
  { key: "ide", label: "IDE", terms: ["ide", "editor", "cursor", "vscode", "vs code", "open in editor"] },
  {
    key: "security",
    label: "Security",
    terms: ["security", "access token", "token", "password", "lock", "auth", "require token"],
  },
  {
    key: "appearance",
    label: "Appearance",
    terms: ["appearance", "theme", "dark", "light", "dark mode", "color", "colour", "accent", "reduce motion", "motion", "take a break", "break", "idle flock", "birds"],
  },
  {
    key: "devices",
    label: "Your devices",
    terms: ["devices", "device", "computer", "pair", "pairing", "sync", "settings sync", "tailscale", "reachable", "remote"],
  },
  { key: "mobile", label: "Your phone", terms: ["phone", "mobile", "qr", "qr code", "use on phone", "tailscale"] },
  { key: "peer", label: "People", terms: ["people", "peer", "share", "invite", "collaborate", "teammate"] },
  { key: "extensions", label: "Extensions", terms: ["extensions", "extension", "addon", "plugin", "database"] },
  {
    key: "doctor",
    label: "Doctor",
    terms: ["doctor", "install", "dependencies", "missing", "diagnose", "health", "troubleshoot", "fix"],
  },
  { key: "logs", label: "System logs", terms: ["logs", "log", "errors", "debug"] },
  {
    key: "advanced",
    label: "Engine & updates",
    terms: ["engine", "update", "updates", "upgrade", "version", "restart server"],
  },
];

/** How well `entry` matches `q` (already lower-cased, trimmed): lower is
 * better, -1 is no match. Exact term → prefix of a term → in the label → in
 * a term → a whole term inside a longer query ("how many sessions"). */
export function settingsMatchScore(entry: SettingsSearchEntry, q: string): number {
  if (q.length < 2) return -1;
  const label = entry.label.toLowerCase();
  const terms = entry.terms.map((t) => t.toLowerCase());
  if (terms.includes(q) || label === q) return 0;
  if (terms.some((t) => t.startsWith(q)) || label.startsWith(q)) return 1;
  if (label.includes(q)) return 2;
  if (q.length >= 3 && terms.some((t) => t.includes(q))) return 3;
  if (terms.some((t) => t.length >= 3 && q.includes(t))) return 4;
  return -1;
}

/** The matching entries, best first (ties keep list order), at most `limit`. */
export function searchSettings(query: string, limit = 5): SettingsSearchEntry[] {
  const q = String(query || "").trim().toLowerCase();
  return SETTINGS_SEARCH.map((e, i) => ({ e, i, s: settingsMatchScore(e, q) }))
    .filter((x) => x.s >= 0)
    .sort((a, b) => a.s - b.s || a.i - b.i)
    .slice(0, limit)
    .map((x) => x.e);
}

/** Scroll Settings to `selector` once the screen has rendered, opening any
 * fold around it. Gives up quietly after ~1 s (the screen may not have it). */
export function revealInSettings(selector: string): void {
  if (typeof document === "undefined") return;
  let tries = 0;
  const look = () => {
    const el = document.querySelector<HTMLElement>("#settings-screens " + selector);
    if (!el) {
      if (++tries < 30) setTimeout(look, 35);
      return;
    }
    for (let d = el.closest("details"); d; d = d.parentElement?.closest("details") ?? null) d.open = true;
    if (el instanceof HTMLDetailsElement) el.open = true;
    el.scrollIntoView?.({ block: "start" });
  };
  setTimeout(look, 0);
}

/** Palette rows for `query`: "Settings → …", each opening its screen. */
export function settingsActions(query: string): Array<{ label: string; hint?: string; run(): void }> {
  return searchSettings(query).map((e) => ({
    label: "Settings → " + e.label,
    hint: "settings",
    run: () => {
      openSettings(e.key);
      if (e.anchor) revealInSettings(e.anchor);
    },
  }));
}
