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
  /** One plain line on what is behind the result, drawn small under the
   * label in both result lists ("Your phone" vs "Phone alerts" read the
   * same until this says which is which). Searched too, after the label and
   * the terms. */
  sub: string;
  terms: readonly string[];
  /** A CSS selector inside the screen to scroll to (a folded <details>
   * around it is opened). */
  anchor?: string;
}

export const SETTINGS_SEARCH: readonly SettingsSearchEntry[] = [
  {
    key: "general",
    label: "General",
    sub: "Tours, hints and terminal scroll speed",
    terms: ["general", "hints", "tips", "scroll"],
  },
  {
    key: "general",
    label: "General → Tours",
    sub: "Replay the welcome tour and the menu tours",
    terms: ["tour", "tours", "replay", "welcome", "walkthrough", "tutorial", "getting started", "help"],
    anchor: "#general-tours",
  },
  {
    key: "general",
    label: "General → Budgets, resume and sleep",
    sub: "Cost alerts, carrying on after a usage reset, idle agents",
    terms: ["budget", "cost", "spend", "usage resets", "resume", "revive", "restart sessions", "sleep", "idle", "memory", "scroll speed"],
    anchor: "#general-advanced",
  },
  {
    key: "connections",
    label: "Connections",
    sub: "Everything MindFlock talks to: your agent, GitHub, ticket trackers",
    terms: ["connections", "connect", "integrations", "github", "token", "tracker", "tickets", "shortcut", "jira", "linear", "asana", "sign in", "login", "signed in"],
  },
  {
    key: "connections",
    label: "Connections → GitHub",
    sub: "Connect GitHub so agents can open pull requests",
    terms: ["github", "gh", "pull request", "pr", "token", "personal access token", "repo access"],
    anchor: '[data-conn="github"]',
  },
  {
    key: "notifications",
    label: "Notifications",
    sub: "Which events alert you, and where: bell, desktop, phone",
    terms: ["notifications", "notification", "notify", "alerts", "alert", "bell", "desktop", "mute", "ping", "sound"],
  },
  {
    key: "notifications",
    label: "Notifications → Phone alerts",
    sub: "Push notifications to your phone (ntfy)",
    terms: ["phone", "mobile", "push", "ntfy", "alerts on my phone", "notifications on phone"],
    anchor: "#notif-advanced",
  },
  {
    key: "coding",
    label: "Agents",
    sub: "Your default agent, its sign-in and extra launch flags",
    terms: ["agents", "agent", "default agent", "claude", "codex", "aider", "cli", "launch flags", "flags", "permissions", "skip permissions", "sign in", "model"],
  },
  {
    key: "coding",
    label: "Agents → Sessions running at once",
    sub: "How many ticket sessions run at the same time",
    terms: ["parallel", "concurrency", "concurrent", "at once", "simultaneous", "max sessions", "how many", "session limit", "limit", "queue"],
    anchor: "#coding-sessions-at-once",
  },
  {
    key: "providers",
    label: "Custom agents",
    sub: "Point an agent at its own build, or add one MindFlock doesn't ship",
    terms: ["custom agent", "add agent", "provider", "providers", "install agent", "binary", "path", "toml"],
  },
  {
    key: "accounts",
    label: "Accounts",
    sub: "Run sessions as different sign-ins or API keys",
    terms: ["accounts", "account", "api key", "openrouter", "profile", "second account", "switch account"],
  },
  {
    key: "localmodel",
    label: "Local model",
    sub: "Use a model running on this computer (Ollama and others)",
    terms: ["local model", "ollama", "llm", "offline model"],
  },
  {
    key: "orchestration",
    label: "Agent orchestration",
    sub: "Let agents start and steer sessions of their own, and how far",
    terms: ["orchestration", "mcp", "spawn", "sub-sessions", "fan out", "nesting", "parallel", "concurrency", "limits"],
  },
  {
    key: "workspace",
    label: "Workspace",
    sub: "Where sessions' files live and how ticket sessions copy a repo",
    terms: ["workspace", "worktree", "worktrees", "folder", "directory", "dir", "checkout", "branch", "base branch", "pr base"],
  },
  {
    key: "workspace",
    label: "Workspace → Where worktrees live",
    sub: "The folders new sessions' worktrees are made in",
    terms: ["where are worktrees", "worktree folder", "workspace folder", "path", "checkout location", "where are my files"],
    anchor: "#workspace-paths",
  },
  {
    key: "workspace",
    label: "Workspace → Worktree or clone",
    sub: "A worktree shares your repo's history; a clone is fully separate",
    terms: ["worktree mode", "clone", "full clone", "copy the repo", "ticket sessions"],
    anchor: "#workspace-copy-mode",
  },
  {
    key: "ide",
    label: "IDE",
    sub: "Which editor “Open in editor” uses",
    terms: ["ide", "editor", "cursor", "vscode", "vs code", "open in editor"],
  },
  {
    key: "security",
    label: "Security",
    sub: "Require an access token to open MindFlock from a browser",
    terms: ["security", "access token", "token", "password", "lock", "auth", "require token"],
  },
  {
    key: "appearance",
    label: "Appearance",
    sub: "Light or dark, colours, motion and break reminders",
    terms: ["appearance", "theme", "color", "colour", "accent", "reduce motion", "motion", "take a break", "break", "idle flock", "birds"],
  },
  {
    key: "appearance",
    label: "Appearance → Mode",
    sub: "Light or dark (the moon in the top bar switches too)",
    terms: ["dark", "light", "dark mode", "light mode", "night mode", "mode"],
    anchor: "#mode-swatches",
  },
  {
    key: "devices",
    label: "Your devices",
    sub: "Pair your other computers and share settings between them",
    terms: ["devices", "device", "computer", "pair", "pairing", "sync", "settings sync", "tailscale", "reachable", "remote"],
  },
  {
    key: "mobile",
    label: "Your phone",
    sub: "Use MindFlock itself from your phone, over Tailscale",
    terms: ["phone", "mobile", "qr", "qr code", "use on phone", "tailscale"],
  },
  {
    key: "peer",
    label: "People",
    sub: "Invite someone and share one folder with their agent",
    terms: ["people", "peer", "share", "invite", "collaborate", "teammate"],
  },
  {
    key: "extensions",
    label: "Extensions",
    sub: "Add-ons that bring their own sidebar bar and commands",
    terms: ["extensions", "extension", "addon", "plugin", "database"],
  },
  {
    key: "doctor",
    label: "Doctor",
    sub: "Check git, tmux and your agent are installed, and fix what's missing",
    terms: ["doctor", "install", "dependencies", "missing", "diagnose", "health", "troubleshoot", "fix"],
  },
  {
    key: "logs",
    label: "System logs",
    sub: "MindFlock's own logs, to paste into a bug report",
    terms: ["logs", "log", "errors", "debug"],
  },
  {
    key: "advanced",
    label: "Engine & updates",
    sub: "Version and updates, restart, uninstall",
    terms: ["engine", "update", "updates", "upgrade", "version", "restart server", "uninstall"],
  },
];

/** How well `entry` matches `q` (already lower-cased, trimmed): lower is
 * better, -1 is no match. The title (label) first, then the hidden terms,
 * then the subtitle: a whole label part → exact term → a label word starts
 * with it → a term starts with it → in the label → in a term → a subtitle
 * word starts with it → a whole term inside a longer query ("how many
 * sessions"). */
export function settingsMatchScore(entry: SettingsSearchEntry, q: string): number {
  if (q.length < 2) return -1;
  const label = entry.label.toLowerCase();
  const terms = entry.terms.map((t) => t.toLowerCase());
  const wordStart = (text: string) => (" " + text.replace(/[^a-z0-9]+/g, " ")).includes(" " + q);
  if (label === q || label.split(" → ").includes(q)) return 0;
  if (terms.includes(q)) return 1;
  if (wordStart(label)) return 2;
  if (terms.some((t) => t.startsWith(q))) return 3;
  if (label.includes(q)) return 4;
  if (q.length >= 3 && terms.some((t) => t.includes(q))) return 5;
  if (q.length >= 3 && wordStart(entry.sub.toLowerCase())) return 6;
  if (terms.some((t) => t.length >= 3 && q.includes(t))) return 7;
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

/** Palette rows for `query`: "Settings → …" with the entry's subtitle under
 * it, each opening its screen. */
export function settingsActions(query: string): Array<{ label: string; sub: string; hint?: string; run(): void }> {
  return searchSettings(query).map((e) => ({
    label: "Settings → " + e.label,
    sub: e.sub,
    hint: "settings",
    run: () => {
      openSettings(e.key);
      if (e.anchor) revealInSettings(e.anchor);
    },
  }));
}
