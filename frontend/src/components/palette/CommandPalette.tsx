/** J2 — command palette (port of app.js section 22): one fuzzy-filtered
 * entry point for every UI action. Arrow keys + Enter run, Esc closes,
 * typed text filters (substring beats subsequence). */

import { useEffect, useMemo, useRef, useState } from "react";
import { instApi } from "../../api/client";
import { useConfig, useExtensions } from "../../state/queries";
import { displayName, useUi } from "../../state/store";
import { toast } from "../../lib/toast";
import { runCommand } from "../../extensions/host";
import type { KeymapHost } from "../../lib/keymap";
import {
  commitSession,
  copySession,
  hasPrSupport,
  hideSession,
  ideSession,
  instances,
  makePrSession,
  mergeSession,
  pushSession,
  selectSession,
} from "../../lib/sessionActions";
import { orderedInstances } from "../sidebar/ordering";

interface PaletteAction {
  label: string;
  hint?: string;
  run(): void;
}

async function sendMessagePrompt(title: string) {
  const text = window.prompt("Send a message to " + displayName(title) + ":");
  if (!text || !text.trim()) return;
  try {
    await instApi(title, "/send", { json: { text: text.trim() } });
    toast("Sent to " + displayName(title));
  } catch (err) {
    toast("Send failed: " + ((err as Error).message || ""));
  }
}

async function queuePromptPrompt(title: string) {
  const text = window.prompt("Queue a prompt for " + displayName(title) + " (auto-runs when idle):");
  if (!text || !text.trim()) return;
  try {
    await instApi(title, "/queue", { json: { text: text.trim() } });
    toast("Queued for " + displayName(title));
  } catch (err) {
    toast("Queue failed: " + ((err as Error).message || ""));
  }
}

/** Contiguous substring beats a spread-out subsequence; earlier/tighter
 * matches rank higher. -1 = no match. (Port of fuzzyScore.) */
export function fuzzyScore(query: string, text: string): number {
  query = String(query || "").toLowerCase();
  text = String(text || "").toLowerCase();
  if (!query) return 0;
  const sub = text.indexOf(query);
  if (sub >= 0) return sub;
  let ti = 0,
    score = 1000; // any subsequence ranks below every substring
  for (const ch of query) {
    const at = text.indexOf(ch, ti);
    if (at < 0) return -1;
    score += at - ti;
    ti = at + 1;
  }
  return score;
}

export function CommandPalette({ host }: { host: KeymapHost }) {
  const open = useUi((s) => s.openDialog === "palette");
  const closeDialog = useUi((s) => s.closeDialog);
  const { data: config } = useConfig();
  // Same cache the sidebar bars and Settings → Extensions read; no fetch of
  // its own.
  const { data: extensions } = useExtensions();
  const [query, setQuery] = useState("");
  const [sel, setSel] = useState(0);
  const inputRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    if (open) {
      setQuery("");
      setSel(0);
      setTimeout(() => inputRef.current?.focus(), 0);
    }
  }, [open]);

  const actions = useMemo<PaletteAction[]>(() => {
    if (!open) return [];
    const ui = useUi.getState();
    const caps = config?.caps ?? { git: true, tailscale: true, ticketing: true, github: true };
    // PR entries are never withheld: without gh/token they still work, they
    // just end in the browser. The hint column says so instead of the label.
    const prHint = hasPrSupport(caps) ? "" : " · opens GitHub";
    const ideName = config?.ide_name || "Cursor";
    const acts: PaletteAction[] = [];
    acts.push({ label: "New session…", hint: "Ctrl+N", run: () => ui.openDialogFor("new-session") });
    const { rows } = orderedInstances(instances(), ui.order);
    for (const inst of rows) {
      const name = ui.aliases[inst.title] || inst.title;
      acts.push({ label: "Focus: " + name, hint: "session", run: () => selectSession(inst.title) });
    }
    const t = ui.focused;
    if (t) {
      acts.push({ label: `Rename… — ${t}`, hint: "display", run: () => ui.openDialogFor("rename", t) });
      acts.push({ label: `Send message… — ${t}`, hint: "agent", run: () => sendMessagePrompt(t) });
      acts.push({ label: `Queue prompt… — ${t}`, hint: "auto-run", run: () => queuePromptPrompt(t) });
      if (caps.git) {
        acts.push({ label: `Commit… — ${t}`, hint: "Ctrl+K C", run: () => commitSession(t) });
        acts.push({ label: `Push — ${t}`, hint: "Ctrl+K P", run: () => pushSession(t) });
        acts.push({ label: `Create PR — ${t}`, hint: "Ctrl+K R" + prHint, run: () => makePrSession(t) });
      }
      acts.push({ label: `Open in ${ideName} — ${t}`, hint: "Ctrl+K O", run: () => ideSession(t) });
      acts.push({ label: `Duplicate session — ${t}`, hint: "Ctrl+K D", run: () => copySession(t) });
      acts.push({ label: `Hide window — ${t}`, hint: "Ctrl+K H", run: () => hideSession(t) });
      // Merge is deliberately unbound (most consequential action) — palette or
      // sidebar menu only, and mergeSession() itself confirms.
      if (caps.git)
        acts.push({
          label: `Merge PR to staging — ${t}`,
          hint: prHint ? prHint.replace(" · ", "") : undefined,
          run: () => mergeSession(t),
        });
    }
    acts.push({ label: "Keyboard shortcuts", hint: "?", run: () => host.toggleShortcuts() });
    // Each Intake tab gets its own entry: the queue you want is the thing you
    // have in mind, and typing "issues" should land on it directly rather than
    // on a dialog you then have to navigate.
    acts.push({ label: "Open Intake", hint: "Alt+I", run: () => ui.openDialogFor("intake") });
    acts.push({ label: "Intake: Tickets", run: () => ui.openDialogFor("intake", "tickets") });
    acts.push({ label: "Intake: Pull requests", run: () => ui.openDialogFor("intake", "prs") });
    acts.push({ label: "Intake: Issues", run: () => ui.openDialogFor("intake", "issues") });
    acts.push({
      label: "Intake: Auto-start",
      hint: "what starts on its own",
      run: () => ui.openDialogFor("intake", "autostart"),
    });
    // The other half of the same arc, so it sits with the Intake entries rather
    // than down among the settings screens.
    acts.push({
      label: "Verify — what's waiting on you",
      hint: "Alt+V",
      run: () => ui.openDialogFor("verify"),
    });
    acts.push({ label: "Open Settings", run: () => ui.openDialogFor("settings") });
    acts.push({ label: "Open Doctor", run: () => host.openDoctor() });
    acts.push({ label: "Open Setup checklist", run: () => ui.openDialogFor("setup") });
    acts.push({ label: "Toggle sidebar", hint: "Ctrl+B", run: () => ui.toggleSidebar() });
    acts.push({ label: "New from Recently closed…", run: () => ui.openDialogFor("recent") });
    // Extensions (Addon API v3): every command of every ENABLED extension,
    // under the manifest's palette title ("Database: Explorer" style). Listing
    // needs no extension code — a declarative command opens its surface from
    // the manifest alone, and the rest activate the module on first run.
    for (const ext of extensions || []) {
      if (!ext.enabled) continue;
      for (const cmd of ext.extension.commands) {
        acts.push({
          label: cmd.title || cmd.id,
          hint: ext.label,
          run: () => void runCommand(ext.id, cmd.id),
        });
      }
    }
    return acts;
  }, [open, config, host, extensions]);

  const filtered = useMemo(() => {
    const scored = actions
      .map((a, i) => ({ a, i, s: fuzzyScore(query, a.label) }))
      .filter((x) => x.s >= 0);
    scored.sort((x, y) => x.s - y.s || x.i - y.i);
    return scored.map((x) => x.a);
  }, [actions, query]);

  useEffect(() => {
    if (sel >= filtered.length) setSel(Math.max(0, filtered.length - 1));
  }, [filtered.length, sel]);

  if (!open) return null;

  const run = (a: PaletteAction | undefined) => {
    if (!a) return;
    closeDialog();
    a.run();
  };

  return (
    <div
      id="palette"
      className="modal"
      onClick={(e) => {
        if (e.target === e.currentTarget) closeDialog();
      }}
    >
      <div id="palette-panel">
        <input
          id="palette-input"
          ref={inputRef}
          type="text"
          placeholder="Type a command or session name…"
          autoComplete="off"
          spellCheck={false}
          value={query}
          onChange={(e) => {
            setQuery(e.target.value);
            setSel(0);
          }}
          onKeyDown={(e) => {
            if (e.key === "ArrowDown") {
              e.preventDefault();
              setSel((s) => Math.min(filtered.length - 1, s + 1));
            } else if (e.key === "ArrowUp") {
              e.preventDefault();
              setSel((s) => Math.max(0, s - 1));
            } else if (e.key === "Enter") {
              e.preventDefault();
              run(filtered[sel]);
            } else if (e.key === "Escape") {
              e.preventDefault();
              closeDialog();
            }
          }}
        />
        <ul id="palette-list">
          {filtered.map((a, i) => (
            <li
              key={a.label + i}
              className={"palette-item" + (i === sel ? " selected" : "")}
              onMouseMove={() => setSel(i)}
              onClick={() => run(a)}
            >
              <span className="palette-label">{a.label}</span>
              {a.hint && <span className="palette-hint">{a.hint}</span>}
            </li>
          ))}
          {!filtered.length && <li className="palette-empty muted">No matching commands</li>}
        </ul>
      </div>
    </div>
  );
}
