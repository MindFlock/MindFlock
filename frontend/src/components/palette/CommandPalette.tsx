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
  runChordAction,
  sessionAction,
  type SessionActionId,
} from "../../lib/sessionActionTable";
import { orderedInstances } from "../sidebar/ordering";
import { sessionLabel } from "../../lib/sessionLabel";
import { openDevicesFor, routePastedCode } from "../../lib/deviceActions";
import { fuzzyScore, isKeyHint, paletteCode, rankActions, type PaletteAction } from "./paletteRank";

export { fuzzyScore };
import { openSettings } from "../../lib/openSettings";
import { TEMPLATES_CHANGED, ensureTemplates, runProblem, templatesNow } from "../../lib/templates";
import { runTemplateNow } from "../../lib/templateRun";
import { settingsActions } from "./settingsSearch";
import { VERIFY_BLURB } from "../../lib/vocab";
import { TOURS, TOUR_IDS } from "../../lib/tours";
import { replayTour } from "../onboarding/replayTour";
import { replayPeopleTour } from "../settings/screens/peer/replayPeopleTour";

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
  "branch",
  "hide",
  "resume",
  "pause",
  "close",
  "delete",
  "map",
  "merge",
];

/** The verbs that act outside this screen — git, a remote, ending a session:
 * listed, but never the Enter default (palette/paletteRank.ts). */
const OUTWARD = new Set<SessionActionId>(["commit", "push", "pr", "merge", "close", "delete", "pause"]);

/** The empty-query menu's order (`top`): what a person reaches for. */
const TOP = {
  new: 0,
  recent: 1, // + 0.1 per place in the MRU (the last three looked at)
  intake: 2,
  verify: 3,
  schedule: 4,
  setup: 5,
  settings: 6,
  tour: 7,
  shortcuts: 8,
};

export function CommandPalette({ host }: { host: KeymapHost }) {
  const open = useUi((s) => s.openDialog === "palette");
  const closeDialog = useUi((s) => s.closeDialog);
  const { data: config } = useConfig();
  // Same cache the sidebar bars and Settings → Extensions read; no fetch of
  // its own.
  const { data: extensions } = useExtensions();
  const [query, setQuery] = useState("");
  const [sel, setSel] = useState(0);
  // The empty query is a short menu; "All commands…" opens the full index.
  const [showAll, setShowAll] = useState(false);
  // Bumped when the Templates list lands or changes, so "Run template: …"
  // rows follow it (the first paint uses the cached/mirrored list).
  const [tplVersion, setTplVersion] = useState(0);
  const inputRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    if (open) {
      setQuery("");
      setSel(0);
      setShowAll(false);
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
    acts.push({ label: "New session…", hint: "Alt+N", top: TOP.new, run: () => ui.openDialogFor("new-session") });

    // --- Dialog openers ----------------------------------------------------
    // Each Intake tab gets its own entry: the queue you want is the thing you
    // have in mind, and typing "issues" should land on it directly rather than
    // on a dialog you then have to navigate.
    acts.push({ label: "Open Intake", hint: "Alt+I", top: TOP.intake, run: () => ui.openDialogFor("intake") });
    acts.push({ label: "Intake: Tickets", run: () => ui.openDialogFor("intake", "tickets") });
    acts.push({ label: "Intake: GitHub PRs", run: () => ui.openDialogFor("intake", "prs") });
    acts.push({ label: "Intake: GitHub issues", run: () => ui.openDialogFor("intake", "issues") });
    acts.push({
      label: "Intake: Auto-start",
      hint: "what starts on its own",
      run: () => ui.openDialogFor("intake", "autostart"),
    });
    // The other end of the same arc, so it sits with the Intake entries rather
    // than down among the settings screens.
    acts.push({
      label: "Verify — " + VERIFY_BLURB,
      hint: "Alt+V",
      top: TOP.verify,
      run: () => ui.openDialogFor("verify"),
    });
    acts.push({
      label: "Schedule…",
      hint: "Alt+S",
      top: TOP.schedule,
      keywords: "templates cron recurring nightly timer daily weekly",
      run: () => ui.openDialogFor("schedule"),
    });
    // Customize and its tabs: the extras you opt into.
    acts.push({ label: "Customize…", hint: "choose sidebar bars", run: () => ui.openDialogFor("customize") });
    acts.push({
      label: "Select sessions…",
      hint: "close, hide or delete several at once · Ctrl+click a row",
      run: () => ui.setBulkMode(true),
    });
    acts.push({ label: "Prompts…", hint: "manage saved prompts", run: () => ui.openDialogFor("prompts") });
    // Schedule is the top bar's; "Templates…" is the name it had, kept as a
    // row so the word people learned still finds it.
    acts.push({ label: "Templates…", hint: "your templates — in Schedule", run: () => ui.openDialogFor("schedule") });
    // A NEW template's form with its schedule on — it says template, because
    // that is what it makes ("Schedule a session…" still finds it).
    acts.push({
      label: "New scheduled template…",
      hint: "daily, weekdays or cron",
      keywords: "schedule a session recurring cron every day nightly weekly",
      run: () => ui.openDialogFor("schedule", "schedule"),
    });
    // Run now: a fresh session from a template, its task already sent —
    // one row per template that can start one on its own (it has a folder).
    for (const tpl of templatesNow()) {
      if (runProblem(tpl)) continue;
      acts.push({
        label: "Run template: " + tpl.name,
        hint: "fresh session, task sent",
        run: () => void runTemplateNow(tpl.name),
      });
    }
    acts.push({ label: "Recently closed…", hint: "reopen or delete closed sessions", run: () => ui.openDialogFor("recent") });
    acts.push({ label: "Assistant instructions (agent file)…", run: () => ui.openDialogFor("assistant-agent") });
    if (caps.git)
      acts.push({
        label: "Zones…",
        hint: "keep-out paths per repo, and derived outputs",
        run: () => ui.openDialogFor("red-zones"),
      });
    acts.push({ label: "Open Settings", top: TOP.settings, run: () => openSettings() });
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
    acts.push({ label: "Open Setup checklist", top: TOP.setup, run: () => ui.openDialogFor("setup") });
    // One "Replay <X> tour" per tour (lib/tours); the Welcome one is on the
    // short menu. Found by "tour", "help", "how" or the surface's name.
    for (const id of TOUR_IDS) {
      const t = TOURS[id];
      acts.push({
        label: `Replay ${id === "welcome" ? "welcome" : t.title} tour`,
        hint: id === "welcome" ? "the three steps to your first session" : `how ${t.title} works`,
        keywords: "tour help how guide tutorial walkthrough show me around " + t.title.toLowerCase(),
        top: id === "welcome" ? TOP.tour : undefined,
        // People is a Settings screen, not a top-bar dialog: its own replay.
        run: () => (id === "people" ? replayPeopleTour() : replayTour(id)),
      });
    }
    acts.push({ label: "Toggle sidebar", hint: "Ctrl+B", run: () => ui.toggleSidebar() });
    acts.push({ label: "Keyboard shortcuts", hint: "?", top: TOP.shortcuts, run: () => host.toggleShortcuts() });

    // --- The focused session's verbs ---------------------------------------
    // Labels, shortcuts and enable rules come from the session action table
    // (lib/sessionActionTable) — the same rows the sidebar › menu and the
    // Ctrl+K chords read. Only what applies right now is listed: an entry
    // that only explains why not is noise in a launcher — except where the
    // table says the reason IS worth saying (`why`, e.g. Branch outside git):
    // listed with the reason as its hint, never the Enter default, and
    // running it raises that reason as an error card.
    const { rows } = orderedInstances(instances(), ui.order);
    const t = ui.focused;
    if (t) {
      const c = actionCtx(t, { inst: rows.find((r) => r.title === t), caps, ideName });
      for (const id of PALETTE_ACTIONS) {
        const why = sessionAction(id).enabled(c) ? "" : sessionAction(id).why?.(c) || "";
        if (why) {
          acts.push({
            label: `${actionLabel(id, c)} — ${t}`,
            hint: actionHint(id, c),
            outward: true,
            run: () => runChordAction(id, t),
          });
          continue;
        }
        if (!sessionAction(id).enabled(c)) continue;
        acts.push({
          label: `${actionLabel(id, c)} — ${t}`,
          hint: actionHint(id, c),
          outward: OUTWARD.has(id),
          run: () => sessionAction(id).run(t),
        });
      }
    }

    // --- Focus rows: every session under the name its rail row shows -------
    for (const inst of rows) {
      // `display_title` is a proxied row's own name (its title carries the device).
      const own = (inst as unknown as { display_title?: string }).display_title || inst.title;
      const name = ui.aliases[inst.title] || sessionLabel(own, inst.branch || "").text;
      const recent = ui.mru.indexOf(inst.title);
      acts.push({
        label: "Focus: " + name,
        top: recent >= 0 && recent < 3 ? TOP.recent + recent / 10 : undefined,
        run: () => selectSession(inst.title),
      });
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
    const out = rankActions(actions, query, {
      showAll,
      allRow: (n) => ({
        label: "All commands…",
        hint: n + " — or type to search",
        keepOpen: true,
        run: () => setShowAll(true),
      }),
    });
    // Settings rows ("Settings → GitHub") go right after the commands whose
    // own names contain what was typed, before looser matches.
    if (query.trim()) {
      const direct = out.filter((a) => {
        const s = fuzzyScore(query, a.label);
        return s >= 0 && s < 1000;
      }).length;
      out.splice(direct, 0, ...settingsActions(query));
    }
    // The query IS a code (pasted straight into the palette): joining with
    // it is the first thing offered, routed by its format.
    // Second, not first, when a command's own name contains the query: "run
    // template" reads as a device name + an 8-letter code too, and Enter must
    // run the command that was typed, not try to join.
    const code = paletteCode(query);
    const named = out.some((a) => fuzzyScore(query, a.label) >= 0 && fuzzyScore(query, a.label) < 1000);
    if (code.kind)
      out[named ? "push" : "unshift"]({
        label: "Join with " + (code.kind === "peer" ? "invite " : "code ") + code.code,
        hint: code.kind === "peer" ? "another person · People" : "your devices",
        run: () => void routePastedCode(query),
      });
    return out;
  }, [actions, query, showAll]);

  useEffect(() => {
    if (sel >= filtered.length) setSel(Math.max(0, filtered.length - 1));
  }, [filtered.length, sel]);

  if (!open) return null;

  const run = (a: PaletteAction | undefined) => {
    if (!a) return;
    if (a.keepOpen) {
      a.run();
      setSel(0);
      return;
    }
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
              {a.sub ? (
                <span className="palette-text">
                  <span className="palette-label">{a.label}</span>
                  <span className="palette-sub">{a.sub}</span>
                </span>
              ) : (
                <span className="palette-label">{a.label}</span>
              )}
              {a.hint && <span className={"palette-hint" + (isKeyHint(a.hint) ? "" : " is-desc")}>{a.hint}</span>}
            </li>
          ))}
          {!filtered.length && <li className="palette-empty muted">No matching commands</li>}
        </ul>
      </div>
    </Modal>
  );
}
