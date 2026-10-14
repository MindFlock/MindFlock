/** J2 — command palette (port of app.js section 22): one fuzzy-filtered
 * entry point for every UI action. Arrow keys + Enter run, Esc closes,
 * typed text filters (substring beats subsequence). */

import { useEffect, useMemo, useRef, useState } from "react";
import { useConfig, useExtensions } from "../../state/queries";
import { useUi } from "../../state/store";
import { Modal } from "../Modal";
import { runCommand } from "../../extensions/host";
import type { KeymapHost } from "../../lib/keymap";
import { instances, selectSession } from "../../lib/sessionActions";
import {
  actionCtx,
  actionHint,
  actionLabel,
  sessionAction,
  type SessionActionId,
} from "../../lib/sessionActionTable";
import { orderedInstances } from "../sidebar/ordering";
import { sessionLabel } from "../../lib/sessionLabel";
import { openDevicesFor, routePastedCode } from "../../lib/deviceActions";
import { routeCode } from "../../lib/fleet";
import { openSettings } from "../../lib/openSettings";
import { TEMPLATES_CHANGED, ensureTemplates, runProblem, templatesNow } from "../../lib/templates";
import { runTemplateNow } from "../../lib/templateRun";
import { settingsActions } from "./settingsSearch";

interface PaletteAction {
  label: string;
  hint?: string;
  run(): void;
}

/** The focused session's entries, in palette order (the table holds what
 * each one says and when it applies). Pause and Resume never both apply;
 * Merge is last — the most consequential, and deliberately unbound. */
const PALETTE_ACTIONS: SessionActionId[] = [
  "rename",
  "message",
  "queue",
  "fastTrack",
  "split",
  "thread",
  "commit",
  "push",
  "pr",
  "ide",
  "duplicate",
  "hide",
  "resume",
  "pause",
  "close",
  "delete",
  "map",
  "merge",
];

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
  // Bumped when the Templates list lands or changes, so "Run template: …"
  // rows follow it (the first paint uses the cached/mirrored list).
  const [tplVersion, setTplVersion] = useState(0);
  const inputRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    if (open) {
      setQuery("");
      setSel(0);
      setTimeout(() => inputRef.current?.focus(), 0);
    }
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const bump = () => setTplVersion((v) => v + 1);
    document.addEventListener(TEMPLATES_CHANGED, bump);
    void ensureTemplates().catch(() => {});
    return () => document.removeEventListener(TEMPLATES_CHANGED, bump);
  }, [open]);

  const actions = useMemo<PaletteAction[]>(() => {
    if (!open) return [];
    const ui = useUi.getState();
    const caps = config?.caps ?? { git: true, tailscale: true, ticketing: true, github: true };
    const ideName = config?.ide_name || "Cursor";
    const acts: PaletteAction[] = [];
    // Order: New, then the places you open, then what acts on the focused
    // session, then Focus rows, then extensions — commands before sessions, so
    // an empty query reads as a menu rather than as the session list again.
    acts.push({ label: "New session…", hint: "Ctrl+N", run: () => ui.openDialogFor("new-session") });

    // --- Dialog openers ----------------------------------------------------
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
    // The other end of the same arc, so it sits with the Intake entries rather
    // than down among the settings screens.
    acts.push({
      label: "Verify — check what was pushed",
      hint: "Alt+V",
      run: () => ui.openDialogFor("verify"),
    });
    // Customize and its tabs: the extras you opt into.
    acts.push({ label: "Customize…", hint: "choose sidebar bars", run: () => ui.openDialogFor("customize") });
    acts.push({
      label: "Select sessions…",
      hint: "close, hide or delete several at once · Ctrl+click a row",
      run: () => ui.setBulkMode(true),
    });
    acts.push({ label: "Prompts…", hint: "manage saved prompts", run: () => ui.openDialogFor("prompts") });
    acts.push({ label: "Templates…", hint: "manage saved New-session setups", run: () => ui.openDialogFor("templates") });
    // Run now: a fresh session from a template, its prompt already sent —
    // one row per template that can start one on its own (it has a folder).
    for (const tpl of templatesNow()) {
      if (runProblem(tpl)) continue;
      acts.push({
        label: "Run template: " + tpl.name,
        hint: "fresh session, prompt sent",
        run: () => void runTemplateNow(tpl.name),
      });
    }
    acts.push({ label: "Recently closed…", hint: "reopen or delete closed sessions", run: () => ui.openDialogFor("recent") });
    acts.push({ label: "Assistant agent file…", run: () => ui.openDialogFor("assistant-agent") });
    if (caps.git)
      acts.push({
        label: "Zones…",
        hint: "keep-out paths per repo, and derived outputs",
        run: () => ui.openDialogFor("red-zones"),
      });
    acts.push({ label: "Open Settings", run: () => openSettings() });
    acts.push({ label: "Open Doctor", run: () => host.openDoctor() });
    acts.push({
      label: "Your devices",
      hint: "add a computer you own, or join one; settings sync",
      run: () => openSettings("devices"),
    });
    acts.push({
      label: "Your phone",
      hint: "the QR code and /m link",
      run: () => openSettings("mobile"),
    });
    acts.push({
      label: "People…",
      hint: "peer links: invite someone, or join a peer with their code",
      run: () => openSettings("peer"),
    });
    // ONE place for every code (lib/deviceActions.ts): Your devices'
    // paste box, which sends one of your devices' codes to Your devices and
    // someone's mfp1:/mfp2: invite to People. (A code typed
    // straight into this box gets its own entry at the top, below.)
    acts.push({
      label: "Paste a code…",
      hint: "join with a code from your other computer, or someone's invite",
      run: () => openDevicesFor({ focusPaste: true }),
    });
    acts.push({ label: "Open Setup checklist", run: () => ui.openDialogFor("setup") });
    acts.push({ label: "Tour", hint: "what MindFlock is, in five slides", run: () => ui.openTour() });
    acts.push({ label: "Toggle sidebar", hint: "Ctrl+B", run: () => ui.toggleSidebar() });
    acts.push({ label: "Keyboard shortcuts", hint: "?", run: () => host.toggleShortcuts() });

    // --- The focused session's verbs ---------------------------------------
    // Labels, shortcuts and enable rules come from the session action table
    // (lib/sessionActionTable) — the same rows the sidebar › menu and the
    // Ctrl+K chords read. Only what applies right now is listed: an entry
    // that only explains why not is noise in a launcher.
    const { rows } = orderedInstances(instances(), ui.order);
    const t = ui.focused;
    if (t) {
      const c = actionCtx(t, { inst: rows.find((r) => r.title === t), caps, ideName });
      for (const id of PALETTE_ACTIONS) {
        if (!sessionAction(id).enabled(c)) continue;
        acts.push({
          label: `${actionLabel(id, c)} — ${t}`,
          hint: actionHint(id, c),
          run: () => sessionAction(id).run(t),
        });
      }
    }

    // --- Focus rows: every session under the name its rail row shows -------
    for (const inst of rows) {
      // `display_title` is a proxied row's own name (its title carries the device).
      const own = (inst as unknown as { display_title?: string }).display_title || inst.title;
      const name = ui.aliases[inst.title] || sessionLabel(own, inst.branch || "").text;
      acts.push({ label: "Focus: " + name, run: () => selectSession(inst.title) });
    }
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
  }, [open, config, host, extensions, tplVersion]);

  const filtered = useMemo(() => {
    const scored = actions
      .map((a, i) => ({ a, i, s: fuzzyScore(query, a.label) }))
      .filter((x) => x.s >= 0);
    scored.sort((x, y) => x.s - y.s || x.i - y.i);
    const out = scored.map((x) => x.a);
    out.splice(scored.filter((x) => x.s < 1000).length, 0, ...settingsActions(query));
    // The query IS a code (pasted straight into the palette): joining with
    // it is the first thing offered, routed by its format.
    // Second, not first, when a command's own name contains the query: "run
    // template" reads as a device name + an 8-letter code too, and Enter must
    // run the command that was typed, not try to join.
    const code = routeCode(query);
    const named = scored.some((x) => x.s < 1000);
    if (code.kind)
      out[named ? "push" : "unshift"]({
        label: "Join with " + (code.kind === "peer" ? "invite " : "code ") + code.code,
        hint: code.kind === "peer" ? "another person · People" : "your devices",
        run: () => void routePastedCode(query),
      });
    return out;
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
    <Modal id="palette" onClose={closeDialog}>
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
    </Modal>
  );
}
