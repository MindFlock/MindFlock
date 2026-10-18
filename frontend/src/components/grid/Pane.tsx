/** One session pane (port of app.js makePane, section 14): header with grip /
 * title / the person's choice of tabs and buttons (lib/paneHeader — Customize
 * → Pane header) / ⋯ always last, the terminal hosts, Diff + Map + Queue
 * tabs, and the budget-lock overlay (Map is git-gated exactly like Diff). The
 * header draws words while they fit and icons once they don't (measured —
 * `fitDecision`); there is no Pause button — only ▶ Resume, on a paused
 * session. Terminals are adopted from lib/terminals' registry so they never
 * remount with the pane. */

import { useCallback, useEffect, useLayoutEffect, useRef, useState, useSyncExternalStore, type ReactNode } from "react";
import type { Instance } from "../../api/types";
import { instApi } from "../../api/client";
import { refreshInstances, useConfig, useInstances } from "../../state/queries";
import { useUi } from "../../state/store";
import { fmtUsd, displayBranch, firstLine } from "../../lib/format";
import { chipState, fastTrackStep, liveStep, nextStep, resetStep, withVerifyVerdict } from "../../lib/stage";
import { cleanupMissing, selectSession } from "../../lib/sessionActions";
import { sessionLabel } from "../../lib/sessionLabel";
import {
  focusTerm,
  freezeTerm,
  getTerm,
  peekTerm,
  releaseTerm,
  type DragScreenCtx,
  type TermHandle,
} from "../../lib/terminals";
import { toast } from "../../lib/toast";
import { copyPaneHistory } from "../../lib/paneHistory";
import { dropSideFor, type DropSide } from "./layout";
import type { DragCtx } from "./TerminalGrid";
import { DiffTab } from "./DiffTab";
import { HistoryOverlay } from "./HistoryOverlay";
import { PaneFindBar, type FindMode } from "./PaneFindBar";
import { PausedBody } from "./PausedBody";
import { QueueTab } from "./QueueTab";
import { ThreadTab } from "./ThreadTab";
import { CodeMapTab, prefetchCodeMap } from "./CodeMapTab";
import { SessionUsageChip } from "../usage/SessionUsageChip";
import { AccountChip } from "./AccountChip";
import { DriveButton } from "../presence/Presence";
import { useMe } from "../../lib/presence";
import { FastTrackMenu } from "./FastTrackMenu";
import { PaneMoreMenu } from "./PaneMoreMenu";
import { SessionMenu } from "../sidebar/SessionMenu";
import { MidName } from "../sidebar/MidName";
import { hasUncommitted, sessionMenu } from "../../lib/sessionMenu";
import { actionCtx, sessionAction } from "../../lib/sessionActionTable";
import { folderInUseText } from "../../lib/sessionActions";
import { errorPop } from "../../lib/errorPop";
import { useCalm } from "../../lib/useCalm";
import { agentBlocked } from "../../lib/sessionState";
import { AgentBlockedBanner } from "../setup/AgentBlocked";
import { useSetup } from "../setup/useSetup";
import { VerdictChip } from "../dialogs/VerdictChip";
import { JointVerdictChip } from "../settings/screens/peer/JointCheck";
import { familyOf, newestReportTs, threadBadge, threadTabShown } from "../../lib/thread";
import { effectiveActivity } from "../../lib/stage";
import {
  TITLE_ROOM_PX,
  fitDecision,
  headerItems,
  type FitState,
  type PaneButtonId,
  type PaneTabId,
} from "../../lib/paneHeader";
import { usageHeadline } from "../usage/usageModel";
import { codemapSeenAt, redZoneChip, subscribeCodemapSeen } from "../../lib/codemapSeen";
import { mapHasState, mapTabBadge } from "../../lib/mapEntry";
import { TabIcon } from "./TabIcon";

/** "thread" is the Thread tab (the session's workers and what was said
 * between them), opened by its own button or store.threadOpen. */
type Tab = "agent" | "shell" | "diff" | "queue" | "map" | "thread";

/** Tabs that need git (the Map reads the worktree's files, diff and imports). */
const GIT_TABS: ReadonlySet<string> = new Set(["diff", "map"]);
/** Idle before the focused pane warms its Map (a click-through of several panes must not start several). */
const MAP_PREFETCH_MS = 1500;

/** Tabs this pane can draw a body for. A saved or requested tab outside it
 * shows the Agent tab instead of an empty pane. */
const BODY_TABS: ReadonlySet<string> = new Set(["agent", "shell", "diff", "queue", "map", "thread"]);

/** The tab a pane actually shows for a saved/requested one: a git tab without
 * git, or a tab with no body here, falls back to Agent. */
function paneTab(t: string, git: boolean): Tab {
  if (!BODY_TABS.has(t)) return "agent";
  if (GIT_TABS.has(t) && !git) return "agent";
  return t as Tab;
}

/** The pane's − button. Hiding is not ending: ✕ means end/close everywhere
 * else (the rail row, a special window's head), so this one wears a minus. */
const HIDE_TITLE = "Hide window — the session keeps running (show it again from its row)";

function queueRelTime(ms: number): string {
  const m = Math.ceil(ms / 60000);
  if (m < 60) return m + "m";
  const h = Math.floor(m / 60);
  return h + "h " + (m - h * 60) + "m";
}

export function Pane({
  inst,
  drag,
  dragging,
}: {
  inst: Instance;
  drag: DragCtx;
  dragging: boolean;
}) {
  const title = inst.title;
  const { data: config } = useConfig();
  const focused = useUi((s) => s.focused === title);
  const alias = useUi((s) => s.aliases[title]);
  const lastTab = useUi((s) => s.lastTab[title]);
  const setLastTab = useUi((s) => s.setLastTab);
  const reduceMotion = useUi((s) => s.reduceMotion);
  // The Thread tab: shown for a session in a family (a parent or workers), or
  // one opened on purpose (Ctrl+K T / S, the palette — threadOpen addressed
  // its composer here). Its badge counts workers that need YOUR answer plus
  // reports newer than the last time you looked.
  const { data: allRows } = useInstances();
  const family = familyOf(title, allRows ?? []);
  const threadOpened = useUi((s) => s.threadComposeTarget?.title === title);
  const threadSeen = useUi((s) => s.threadLastSeen[title] || 0);
  // A group's lead always has one: its plan and its one PR live there, from
  // before its first worker exists.
  const threadShown = threadTabShown(
    !!family.parent || family.children.length > 0 || inst.run?.role === "lead",
    threadOpened || lastTab === "thread"
  );
  const badge = threadBadge(family.children, threadSeen, effectiveActivity);
  const newestReport = newestReportTs(family.children);
  // The Map tab's badge: the rail row's zone chip (a breach, an unseen block,
  // the guard off…), else a quiet count of changed files (lib/mapEntry).
  const mapSeen = useSyncExternalStore(
    subscribeCodemapSeen,
    useCallback(() => codemapSeenAt(title), [title])
  );
  const rzChip = redZoneChip(inst.redzone, mapSeen);
  const mapBadge = mapTabBadge(rzChip, inst.diff_stat?.files || 0);
  // What the Diff tab reloads on: the session's diff stats moving.
  const stat = inst.diff_stat;
  const un = stat?.uncommitted;
  const diffStamp = stat
    ? [stat.files, stat.additions, stat.deletions, un?.files ?? "", un?.additions ?? "", un?.deletions ?? ""].join(",")
    : "";
  // This pane's ⏩ fast-track picker, when it is the one open (Ctrl+K F, the
  // row › menu and the palette open it from anywhere, so it lives in the
  // store rather than here).
  const ftMenu = useUi((s) => (s.fastTrackMenu?.title === title ? s.fastTrackMenu : null));
  // State, not a ref: the picker measures itself against the ⏩ button, and a
  // picker opened in the same frame the pane mounts must re-render once it
  // exists.
  const [ftEl, setFtEl] = useState<HTMLButtonElement | null>(null);
  // Same "assume capable until the server says otherwise" fallback as the other
  // caps consumers (SidebarRow, CommandPalette, lib/sessionActions) — keep the
  // four literals identical so a PR affordance added here can't silently start
  // out disabled while the same button stays live everywhere else.
  const caps = config?.caps ?? {
    git: true,
    tailscale: true,
    ticketing: true,
    github: true,
  };

  const missing = !!inst.workspace_missing;
  const loading = inst.status === "loading";
  // Paused: the agent was stopped on purpose. The Agent tab is a placeholder
  // (PausedBody) and NO agent socket is opened — the server refuses one with
  // 4423 rather than relaunch the agent; Resume brings the terminal back.
  const paused = inst.status === "paused";

  // A shared-folder (peer) session has no companion shell (it would run
  // outside the sandbox), so a saved "shell" tab opens on the agent instead.
  const savedTab = (inst.peer_share && lastTab === "shell" ? "agent" : lastTab) || "agent";
  const [tab, setTab] = useState<Tab>(paneTab(savedTab, caps.git));
  const [booted, setBooted] = useState(false);
  const [wsState, setWsState] = useState("connecting");
  const [shellStarted, setShellStarted] = useState(savedTab === "shell");
  // Which pane's full-history overlay is open (null = closed). Opened by the
  // header history button or Ctrl+↑/↓ (dragSel null), or by drag-selecting
  // past a terminal edge — dragSel then carries the terminal's in-progress
  // selection (and edge which end of it) so the overlay continues the same
  // drag seamlessly. pos picks where the view opens (Ctrl+↑ starts at the
  // very top).
  const [histPane, setHistPane] = useState<{
    kind: "agent" | "shell";
    dragSel: string | null;
    edge?: "top" | "bottom";
    pos?: "top" | "bottom";
    ctx?: DragScreenCtx;
    /** The TUI's pinned-prompt row at gesture time — the anchor of last
     * resort when the visible rows (tool output) aren't in the transcript. */
    ghostSel?: string | null;
    /** Set by Ctrl+F: open in find mode, prefilled with this. */
    find?: string;
  } | null>(null);
  // The in-place find bar (Ctrl+F on a live terminal), which terminal it
  // searches and how (see PaneFindBar); null = closed.
  const [paneFind, setPaneFind] = useState<{
    kind: "agent" | "shell";
    query: string;
    mode: FindMode;
  } | null>(null);
  // The server indexes an idle agent pane for Ctrl+F ahead of time (a
  // briefly tall window, a sweep through its scrollback — see backend
  // core/pane_scroll_find.background_index). It announces it first, and the
  // pane shows a still snapshot until it's done, so none of it is seen.
  useEffect(() => {
    const ev = window.mindflock?.events;
    if (!ev) return;
    let thaw: (() => void) | null = null;
    const off = ev.subscribe("pane.find_index", (env) => {
      if (env.session !== title || ev.isReplay(env)) return;
      if (env.data?.active) {
        if (!thaw) thaw = freezeTerm(title, "agent");
      } else if (thaw) {
        thaw();
        thaw = null;
      }
    });
    return () => {
      off();
      thaw?.();
    };
  }, [title]);
  const bodyRef = useRef<HTMLDivElement | null>(null);
  const fitTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);

  // The same name the rail row shows (alias, else the ticket/PR label, else
  // the title), so a pane can be matched to its row by eye. The tooltip keeps
  // the raw title and branch.
  const label = sessionLabel(
    (inst as unknown as { display_title?: string }).display_title || title,
    inst.branch || ""
  );
  const displayName = alias || label.text;
  // A session on another tailnet device says which one. Local sessions say
  // nothing: a row only carries `device` once a second device is in play.
  const deviceName = inst.device ? inst.device_label || inst.device : "";
  const nameTip =
    (displayName !== title ? title + "  ·  " : "") +
    (inst.branch || title) +
    (deviceName ? "  ·  on " + deviceName : "");
  // The kind tag ("(tix) ") rides in its own span: a narrow head drops it
  // (Pane.css) so the few characters that fit are the name, not the tag.
  const kindTag = !alias && label.kind ? "(" + label.kind + ") " : "";
  const titleText =
    kindTag && displayName.startsWith(kindTag) ? (
      <>
        <span className="title-kind">{kindTag}</span>
        <MidName text={displayName.slice(kindTag.length)} />
      </>
    ) : (
      // Middle-truncated: a "-2" suffix is what tells two panes apart.
      <MidName text={displayName} />
    );

  // Adopt a registry terminal into a host div; re-runs safely on remount.
  const adopt = useCallback(
    (kind: "agent" | "shell") => (el: HTMLDivElement | null) => {
      if (!el || missing || loading || (kind === "agent" && paused)) return;
      const handle = getTerm(title, kind);
      if (!el.contains(handle.container)) el.appendChild(handle.container);
      handle.onHistoryDrag = (sel, edge, ctx) =>
        setHistPane({ kind, dragSel: sel, edge, ctx, ghostSel: ghostRef.current });
      if (kind === "agent") {
        // The agent TUI pins its own contextual prompt row ("❯ …") at the top
        // while scrolled back. Lift its text into the prompt bar (so the bar
        // tracks what you're LOOKING at, not just the latest prompt) and let
        // the render mask the duplicate row on desktop.
        handle.onTopRow = (t) => {
          const m = /^\s*[❯>]\s+(.+)$/.exec(t);
          const g = m && m[1].trim() ? m[1].trim() : null;
          ghostRef.current = g; // adopt()'s closures are stale — ref, not state
          setGhost(g);
        };
      }
      if (kind === "agent") {
        handle.onState = (s) => setWsState(s);
        handle.onBoot = () => setBooted(true);
        setWsState(handle.state);
        // Re-adopting an already-booted terminal (pane remounted by a grid
        // move) never fires onBoot again — sync the cover off its flag.
        if (handle.booted) setBooted(true);
      }
      requestAnimationFrame(() => handle.doFit());
    },
    [title, missing, loading, paused]
  );

  // Pausing with the pane open: drop the agent terminal (its socket would
  // only be refused now) so Resume starts a fresh one; the shell stays.
  useEffect(() => {
    if (!paused) return;
    releaseTerm(title, "agent");
    setBooted(false);
    setWsState("paused");
  }, [paused, title]);

  const fitSoon = useCallback(() => {
    clearTimeout(fitTimer.current);
    fitTimer.current = setTimeout(() => {
      const t: TermHandle | undefined = peekTerm(title, tab === "shell" ? "shell" : "agent");
      t?.doFit();
    }, 120);
  }, [title, tab]);

  // Debounced refits on pane-body resize (drag/reorder reflows fire many).
  useEffect(() => {
    if (!bodyRef.current || missing || loading) return;
    const obs = new ResizeObserver(fitSoon);
    obs.observe(bodyRef.current);
    return () => obs.disconnect();
  }, [fitSoon, missing, loading]);

  // Whether the pinned prompt shows its whole body. Collapsed to one line by
  // default; ONLY the arrow toggles it — no hover magic.
  const [pinOpen, setPinOpen] = useState(false);
  // The agent TUI's own pinned-prompt row text, when its top row shows one
  // (scrolled back in Claude Code). Non-null → the bar mirrors it and the
  // duplicate row is masked. The ref mirrors the state for adopt()'s stale
  // closures (the gesture handler reads it at fire time).
  const [ghost, setGhost] = useState<string | null>(null);
  const ghostRef = useRef<string | null>(null);

  // What the bar shows: the TUI's contextual prompt while scrolled back (it
  // tracks the section you're reading), the session's latest prompt at live.
  const ghostCore = (ghost || "").replace(/[…]+\s*$/, "").replace(/\s+/g, " ").trim();
  const fullNorm = (inst.last_prompt_full || "").replace(/\s+/g, " ");
  const ghostIsLatest = !ghost || (!!fullNorm && fullNorm.includes(ghostCore.slice(0, 80)));
  // Before the transcript names one (or for an agent without a transcript
  // reader), the task it was created with (Instance.Task, first line).
  const createTask = firstLine(inst.task);
  const task = inst.last_prompt || createTask || null;
  const pinLine = ghost ?? task;

  // Expanding an OLDER prompt (the ghost isn't the latest) needs its full
  // body looked up server-side — only the latest prompt's body rides the
  // snapshot. The ghost's own width-truncated text shows while it loads.
  const [lookup, setLookup] = useState<{ q: string; text: string } | null>(null);
  useEffect(() => {
    if (!pinOpen || ghostIsLatest || ghostCore.length < 8) return;
    if (lookup?.q === ghostCore) return;
    let dead = false;
    instApi<{ text?: string | null } | null>(title, "/prompt?q=" + encodeURIComponent(ghostCore))
      .then((j) => {
        if (!dead && j?.text) setLookup({ q: ghostCore, text: j.text });
      })
      .catch(() => {});
    return () => {
      dead = true;
    };
  }, [pinOpen, ghostIsLatest, ghostCore, title, lookup]);

  const pinBody = ghostIsLatest
    ? inst.last_prompt
      ? inst.last_prompt_full || inst.last_prompt
      : inst.task || task
    : lookup?.q === ghostCore
      ? lookup.text
      : ghost;

  // Only the pin's APPEARING or disappearing changes the terminal's height, and
  // it does so without touching .pane-body, so the ResizeObserver above never
  // fires for it. Expanding is not in this list on purpose: the body is an
  // overlay, so opening a prompt costs no refit and no tmux resize (which the
  // backend's activity classifier would have to rebaseline anyway). The bar's
  // own text is ellipsized to one line, so changing it can't change the height
  // either.
  const hasPin = !!pinLine;
  useEffect(() => {
    fitSoon();
  }, [hasPin, fitSoon]);

  // Ctrl+↑ / Ctrl+↓ on the focused pane opens the full-history view (at the
  // top / bottom respectively). Once it's open its own handler owns these
  // keys for in-view jumps, so this only fires from normal mode. Capture
  // phase so it wins over xterm; real text fields keep their arrows (the
  // terminal's hidden textarea is exempt — that IS the normal-mode case).
  useEffect(() => {
    if (!focused || missing || loading || histPane) return;
    const onKey = (e: KeyboardEvent) => {
      if (!(e.ctrlKey || e.metaKey) || e.altKey) return;
      if (e.key !== "ArrowUp" && e.key !== "ArrowDown") return;
      const a = document.activeElement;
      const editing =
        a &&
        (a.tagName === "INPUT" ||
          a.tagName === "TEXTAREA" ||
          a.tagName === "SELECT" ||
          (a as HTMLElement).isContentEditable === true);
      if (editing && !a.closest(".xterm")) return;
      e.preventDefault();
      e.stopPropagation();
      setHistPane({
        kind: tab === "shell" ? "shell" : "agent",
        dragSel: null,
        pos: e.key === "ArrowUp" ? "top" : "bottom",
      });
    };
    document.addEventListener("keydown", onKey, true);
    return () => document.removeEventListener("keydown", onKey, true);
  }, [focused, missing, loading, histPane, tab]);

  // Ctrl+F on a focused terminal: find across the pane's WHOLE scrollback,
  // the way a browser finds in a page (prefilled with a one-line terminal
  // selection, as browsers do) — the live pane itself scrolls to each hit
  // (PaneFindBar). The server says how, per pane, by what its mouse wheel
  // scrolls (GET …/find): tmux's history, or the app's own; only a pane that
  // scrolls from neither (an alternate screen with no mouse — a pager) gets
  // the full-history overlay's find instead. Only when the keyboard is in
  // this pane: a dialog's own Ctrl+F filter must win over a pane that merely
  // stays "focused" underneath it.
  useEffect(() => {
    if (!focused || missing || loading || histPane || paneFind) return;
    if (tab !== "agent" && tab !== "shell") return;
    const onKey = (e: KeyboardEvent) => {
      if (!(e.ctrlKey || e.metaKey) || e.altKey || e.shiftKey) return;
      if (e.key !== "f" && e.key !== "F") return;
      const a = document.activeElement;
      if (a && a !== document.body && !paneRef.current?.contains(a)) return;
      // A real text field in the pane keeps its own Ctrl+F; the terminal's
      // hidden textarea is exempt — that IS the case this is for.
      const editing =
        a &&
        (a.tagName === "INPUT" ||
          a.tagName === "TEXTAREA" ||
          a.tagName === "SELECT" ||
          (a as HTMLElement).isContentEditable === true);
      if (editing && !a.closest(".xterm")) return;
      e.preventDefault();
      e.stopPropagation();
      const kind = tab === "shell" ? "shell" : "agent";
      const sel = peekTerm(title, kind)?.term.getSelection().trim() ?? "";
      const query = sel && !sel.includes("\n") && sel.length <= 200 ? sel : "";
      instApi<{ mode: FindMode | "overlay" }>(title, `/find?pane=${kind}`)
        .then((r) => r.mode)
        .catch(() => "overlay" as const)
        .then((mode) => {
          if (mode === "overlay") setHistPane({ kind, dragSel: null, pos: "bottom", find: query });
          else setPaneFind({ kind, query, mode });
        });
    };
    document.addEventListener("keydown", onKey, true);
    return () => document.removeEventListener("keydown", onKey, true);
  }, [focused, missing, loading, histPane, paneFind, tab, title]);

  // The Map is warmed for the pane you are looking at (focused, git, not
  // already on the Map tab) after a short idle, so clicking Map paints at
  // once — nine panes may exist, so never for all of them at once.
  useEffect(() => {
    if (!focused || !caps.git || missing || loading || tab === "map") return;
    const t = setTimeout(() => void prefetchCodeMap(title), MAP_PREFETCH_MS);
    return () => clearTimeout(t);
  }, [focused, caps.git, missing, loading, tab, title]);

  const showTab = (t: Tab) => {
    // A tab click while the full-history overlay covers the body is
    // navigation, not a request to switch under glass: without this the
    // clicked tab highlighted while the overlay kept covering its content,
    // which reads as the tab being dead. Clicking the ACTIVE tab closes the
    // overlay too — "show me the tab" is what the press means either way.
    // The find bar searches one terminal, so it goes with the tab.
    setHistPane(null);
    setPaneFind(null);
    setTab(t);
    setLastTab(title, t);
    if (t === "shell") setShellStarted(true);
    setTimeout(() => peekTerm(title, t === "shell" ? "shell" : "agent")?.doFit(), 0);
  };

  // React to programmatic tab switches (e.g. the commit dialog switching to
  // the shell to watch pre-commit hooks). The pane's own tab buttons go
  // through showTab, which already updates local state, so this only fires
  // when lastTab is changed from outside the pane.
  useEffect(() => {
    if (!lastTab) return;
    const t = paneTab(lastTab, caps.git);
    if (t === tab) return;
    // Same rule as showTab: a switch done FOR the user (the commit dialog
    // jumping to the shell to watch pre-commit hooks) must be visible, so the
    // history overlay closes rather than covering the tab it switched to.
    setHistPane(null);
    setPaneFind(null);
    setTab(t);
    if (t === "shell") setShellStarted(true);
    setTimeout(() => peekTerm(title, t === "shell" ? "shell" : "agent")?.doFit(), 0);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [lastTab]);

  // Looking at the Thread is what "seen" means: on open, and again whenever a
  // new report lands while it is open, so the badge only ever counts reports
  // you have not had in front of you.
  useEffect(() => {
    if (tab === "thread") useUi.getState().setThreadLastSeen(title);
  }, [tab, title, newestReport]);

  // Header drag wiring (shared semantics with special panes).
  const paneRef = useRef<HTMLElement | null>(null);
  const headDrag = {
    draggable: true,
    onDragStart: (ev: React.DragEvent) => {
      const t = ev.target as HTMLElement;
      if (t.closest("select, input, textarea")) {
        ev.preventDefault();
        return;
      }
      ev.dataTransfer.setData("text/plain", title);
      ev.dataTransfer.effectAllowed = "move";
      drag.start(title);
    },
    onDragEnd: () => drag.end(),
  };
  const paneDrag = {
    onDragOver: (ev: React.DragEvent) => {
      if (!drag.dragTitle) return;
      ev.preventDefault();
      ev.dataTransfer.dropEffect = "move";
      if (title === drag.dragTitle) return;
      const rect = paneRef.current!.getBoundingClientRect();
      drag.hover(title, dropSideFor(rect, ev.clientX, ev.clientY) as DropSide);
    },
    onDrop: (ev: React.DragEvent) => {
      ev.preventDefault();
      drag.commit();
    },
  };

  // A picker whose ⏩ button has gone (the pane lost its workspace, is still
  // loading, or has no git) closes rather than waiting to reappear later.
  const ftGone = missing || loading || !fastTrackStep(inst);
  useEffect(() => {
    if (ftMenu && ftGone) useUi.getState().setFastTrackMenu(null);
  }, [ftMenu, ftGone]);
  const me = useMe(); // before the early returns: hooks run every render
  // The calm first hour (lib/calm.ts) folds Queue and ⏩ from a header that
  // is still the default; a customized one is left as chosen (headerItems).
  const calm = useCalm();
  const paneHeader = useUi((s) => s.paneHeader);
  // Words while they fit, icons once they don't: measured on the header
  // itself before paint (fitDecision), re-measured when it resizes and when
  // what it holds changes (its data-fit-key).
  const headRef = useRef<HTMLDivElement | null>(null);
  const [fit, setFit] = useState<FitState>({ icons: false, textNeed: null });
  const fitRef = useRef(fit);
  fitRef.current = fit;
  const fitKey = useRef("");
  const measureFit = useCallback(() => {
    const el = headRef.current;
    if (!el) return;
    let cur = fitRef.current;
    // A render still on its way: the DOM isn't the state being judged yet.
    if (el.classList.contains("head-icons") !== cur.icons) return;
    const key = el.dataset.fitKey || "";
    if (key !== fitKey.current) {
      fitKey.current = key;
      cur = { ...cur, textNeed: null };
    }
    const next = fitDecision(cur, el.clientWidth, headNeed(el));
    if (next.icons !== fitRef.current.icons || next.textNeed !== fitRef.current.textNeed) {
      fitRef.current = next;
      setFit(next);
    }
  }, []);
  useLayoutEffect(measureFit);
  useEffect(() => {
    const el = headRef.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(() => measureFit());
    ro.observe(el);
    return () => ro.disconnect();
  }, [missing, loading, measureFit]);
  const icons = fit.icons;
  // An agent that isn't installed (the terminal says `claude: not found`) or
  // isn't signed in (it sits at its first-run questions): the pane says what
  // that means and where the fix is.
  const { data: setupStatus } = useSetup();
  const noAgent = agentBlocked(inst, setupStatus?.logins);

  if (missing) {
    return (
      <section
        ref={paneRef as React.RefObject<HTMLElement>}
        className={"pane missing-pane" + (focused ? " focused" : "")}
        data-title={title}
        {...paneDrag}
      >
        <div className="pane-head" {...headDrag}>
          <span className="grip" title="Drag to move this window">⠿</span>
          <span className="title" title={nameTip}>{titleText}</span>
          <span className="stagechip s-missing" title="Workspace directory no longer exists">
            missing workspace
          </span>
          <span className="state">workspace gone</span>
          <div className="head-tail">
            <button
              className="act pane-close"
              type="button"
              aria-label="Hide window"
              title={HIDE_TITLE}
              onClick={(e) => {
                e.stopPropagation();
                useUi.getState().setHidden(title, true);
              }}
            >
              −
            </button>
          </div>
        </div>
        <div className="pane-body">
          <div className="missing-body">
            <p>Workspace directory no longer exists.</p>
            <p className="muted missing-path">{inst.folder || inst.path || ""}</p>
            <button
              type="button"
              className="missing-cleanup danger"
              onClick={(e) => {
                e.stopPropagation();
                cleanupMissing(title);
              }}
            >
              Delete
            </button>
          </div>
        </div>
      </section>
    );
  }

  if (loading) {
    // Draggable while it boots. Provisioning is the LONGEST a window is ever in
    // one state — a cold clone runs for minutes — and it was the one state you
    // could not move or drop onto, so you had to wait it out before arranging your
    // grid. Nothing about the drag touches the session; it is pure layout.
    return (
      <section
        ref={paneRef as React.RefObject<HTMLElement>}
        className={"pane loading-pane" + (dragging ? " dragging" : "")}
        data-title={title}
        {...paneDrag}
      >
        <div className="pane-head" {...headDrag}>
          <span className="grip" title="Drag to move this window">⠿</span>
          <span className="title" title={nameTip}>{titleText}</span>
          <span className="branch">{inst.branch ? "(" + displayBranch(inst) + ")" : ""}</span>
          <span className="state">provisioning…</span>
          <div className="head-tail">
            <button
              className="act pane-close"
              type="button"
              aria-label="Hide window"
              title={HIDE_TITLE}
              onClick={(e) => {
                e.stopPropagation();
                useUi.getState().setHidden(title, true);
              }}
            >
              −
            </button>
          </div>
        </div>
        <div className="pane-body">
          <div className="provisioning">
            <div className="spinner" />
            {/* Only a provisioned session clones anything; a plain worktree is
                a quick `git worktree add`, and saying "clones the base repo"
                there had people waiting for a download that never happens. */}
            {inst.provisioned ? (
              <>
                <p>Provisioning workspace…</p>
                <p className="muted">
                  The first provisioned run clones the base repo — this can take a while.
                </p>
              </>
            ) : inst.in_place ? (
              <p>Starting the session…</p>
            ) : (
              <p>Creating worktree{inst.branch ? " on " + inst.branch : ""}…</p>
            )}
          </div>
        </div>
      </section>
    );
  }

  const chip = chipState(inst);
  // A shared-folder (peer) session runs sandboxed: nextStep/fastTrackStep
  // offer nothing for it, and its companion shell is refused (no tab).
  const sharedFolder = !!inst.peer_share;
  const ns = nextStep(inst);
  const ft = fastTrackStep(inst);
  const rs = resetStep(inst);
  const step = liveStep(inst);
  const q = inst.queue;
  const pending = q?.pending || 0;
  const limitedMs = q?.limited_until ? q.limited_until * 1000 - Date.now() : 0;
  const holding = limitedMs > 0;
  const budget = inst.budget;
  const ds = inst.workspace_missing ? null : inst.diff_stat;
  const hasDiffStat = !!(ds && ((ds.files || 0) + (ds.additions || 0) + (ds.deletions || 0) > 0));
  // The guided button only wears the accent when pressing it is the thing to
  // do now: not while the agent is busy (working, asking, at its limit).
  // Quiet, it is still the same button, outlined. Hint and blocked steps keep
  // their own look. (A "Commit…" with nothing to commit isn't drawn at all —
  // shownStep below.)
  const act = effectiveActivity(inst);
  const quiet =
    !!ns && !ns.hint && !ns.disabled && (act === "working" || act === "clarify" || act === "limit");

  // What the header draws, in order: the person's choice (Customize → Pane
  // header, lib/paneHeader), less what this session can't have — Terminal
  // for a shared folder (its shell would run outside the sandbox), Diff and
  // Map without git, Thread without workers — plus the tab you are ON, shown
  // or not. The calm first hour folds Queue, Map and ⏩ from a default
  // header, except a Queue with something in it, a Map with changes or zone
  // state, or a fast-track someone set: those are state, not options.
  const items = headerItems(paneHeader, {
    active: tab,
    sharedFolder,
    git: caps.git,
    thread: threadShown,
    calm,
    queueBusy: pending > 0 || holding,
    mapBusy: mapHasState(inst, rzChip),
    ftBusy: !!ft && (ft.lane !== "leave" || ft.halted || ft.active || !!ftMenu),
  });
  const shows = (id: PaneButtonId) => items.buttons.includes(id);
  const ftShown = !!ft && shows("fasttrack");
  // The ⋯ menu: the session's one menu (lib/sessionMenu), less the tabs this
  // header already shows, plus what only a pane has — and everything the
  // header leaves out (History, the next step) is in it.
  const menuCtx = actionCtx(title, { inst, caps });
  const visibleTabs: string[] = items.tabs;
  // The terminal whose history the copy button takes: the tab you are on, and
  // only a terminal tab (Diff, Map, Queue and Thread have no scrollback).
  const termTab: "agent" | "shell" | null = tab === "agent" || tab === "shell" ? tab : null;
  const copyHistory = () => {
    if (termTab) void copyPaneHistory(title, termTab);
  };
  // "Commit…" only once there is something to commit; until then the header
  // has no lead button (the agent's state chip says what it is doing).
  const shownStep = ns && ns.label === "Commit…" && !hasUncommitted(inst) ? null : ns;
  const openHistory = () => setHistPane({ kind: tab === "shell" ? "shell" : "agent", dragSel: null });
  // SHIP carries Verify's verdict beside Open PR, and asks first on a
  // failed checklist (lib/stage.ts withVerifyVerdict).
  const menu = () =>
    withVerifyVerdict(
      sessionMenu(inst, menuCtx, {
        visibleTabs,
        onHistory: shows("history") ? undefined : openHistory,
        onCopyHistory: termTab ? copyHistory : undefined,
        onNextStep: shows("next") ? undefined : shownStep ?? undefined,
      }),
      inst,
    );
  // The read-outs the header leaves out (the cost chip, the diff-stat), at
  // the top of ⋯ so they are still one click away.
  const menuSummary = [
    !shows("cost") && usageHeadline(inst),
    !shows("diffstat") && hasDiffStat && diffStatText(inst),
  ].filter(Boolean) as string[];
  // No Pause button: pausing lives in the palette, the CLI and the API. A
  // session that IS paused (from there, or by an older build) shows ▶ Resume
  // here, next to ⋯, so the way back is never a menu away.
  const canResume = sessionAction("resume").enabled(menuCtx);
  // What makes the header's width change: the words↔icons fit re-measures
  // when this does (the cost ticking up doesn't count — words re-measure on
  // every render anyway).
  const fitKeyNow = [
    items.tabs.join(","),
    items.buttons.join(","),
    displayName,
    shownStep?.label ?? "",
    ft?.label ?? "",
    pending,
    holding ? 1 : 0,
    badge.count,
    mapBadge?.label ?? "",
    canResume ? 1 : 0,
    deviceName,
  ].join("|");

  const tabEls: Record<PaneTabId, ReactNode> = {
    agent: (
      <button
        key="agent"
        data-tab="agent"
        role="tab"
        aria-selected={tab === "agent"}
        className={tab === "agent" ? "active" : ""}
        title="Agent — the session's agent CLI"
        onClick={(e) => { e.stopPropagation(); showTab("agent"); }}
      >
        <TabIcon name="agent" />
        <span className="tab-label">Agent</span>
      </button>
    ),
    shell: (
      <button
        key="shell"
        data-tab="shell"
        role="tab"
        aria-selected={tab === "shell"}
        className={tab === "shell" ? "active" : ""}
        title="Terminal — a shell in this session's folder"
        onClick={(e) => { e.stopPropagation(); showTab("shell"); }}
      >
        <TabIcon name="shell" />
        <span className="tab-label">Terminal</span>
      </button>
    ),
    diff: (
      <button
        key="diff"
        data-tab="diff"
        role="tab"
        aria-selected={tab === "diff"}
        className={tab === "diff" ? "active" : ""}
        title="Diff — what this session changed"
        onClick={(e) => { e.stopPropagation(); showTab("diff"); }}
      >
        <TabIcon name="diff" />
        <span className="tab-label">Diff</span>
      </button>
    ),
    map: (
      <button
        key="map"
        data-tab="map"
        role="tab"
        aria-selected={tab === "map"}
        className={"map-tab" + (tab === "map" ? " active" : "")}
        title={"Code map — the worktree as a tree: agents, zones, plan, blast radius" + (mapBadge ? "\n" + mapBadge.title : "")}
        aria-label={mapBadge ? "Map, " + mapBadge.title : undefined}
        onClick={(e) => { e.stopPropagation(); showTab("map"); }}
      >
        <TabIcon name="map" />
        <span className="tab-label">Map</span>
        {mapBadge && (
          <span className={"queue-tab-badge map-badge " + mapBadge.cls} aria-hidden="true">
            {mapBadge.label}
          </span>
        )}
      </button>
    ),
    queue: (
      <button
        key="queue"
        data-tab="queue"
        role="tab"
        aria-selected={tab === "queue"}
        className={
          "queue-tab" +
          (tab === "queue" ? " active" : "") +
          (pending > 0 && q?.enabled === false ? " q-paused" : "") +
          (q?.loop ? " q-loop" : "") +
          (holding ? " q-limited" : "")
        }
        title={
          holding
            ? q?.wait_for_limit !== false
              ? `Usage limit reached · auto-resumes in ${queueRelTime(limitedMs)} (${pending} queued)`
              : `Usage limit reached · queue stopped (${pending} queued)`
            : "Send a message now or queue prompts to auto-run — keeps the session going across usage limits"
        }
        onClick={(e) => { e.stopPropagation(); showTab("queue"); }}
      >
        <TabIcon name="queue" />
        <span className="tab-label">Queue</span>
        {(pending > 0 || holding) && (
          <span className="queue-tab-badge">
            {holding ? "⏳" : pending > 99 ? "99+" : String(pending)}
          </span>
        )}
      </button>
    ),
    thread: (
      <button
        key="thread"
        data-tab="thread"
        role="tab"
        aria-selected={tab === "thread"}
        className={"thread-tab" + (tab === "thread" ? " active" : "")}
        title={
          "Thread — this session's workers, what they reported and what passed between them; write to any of them as you (Ctrl+K T)" +
          (badge.needs ? `\n${badge.needs} need${badge.needs === 1 ? "s" : ""} you` : "") +
          (badge.fresh ? `\n${badge.fresh} new report${badge.fresh === 1 ? "" : "s"}` : "")
        }
        onClick={(e) => { e.stopPropagation(); showTab("thread"); }}
      >
        <TabIcon name="thread" />
        <span className="tab-label">Thread</span>
        {badge.count > 0 && (
          <span className={"queue-tab-badge thread-badge" + (badge.needs ? " needs" : "")}>
            {badge.count > 99 ? "99+" : String(badge.count)}
          </span>
        )}
      </button>
    ),
  };

  const buttonEls: Record<PaneButtonId, ReactNode> = {
    next: shownStep ? (
      <button
        key="next"
        className={
          "nextstep" +
          (shownStep.hint ? " nextstep-hint" : "") +
          (shownStep.disabled ? " nextstep-blocked" : "") +
          (quiet ? " nextstep-quiet" : "")
        }
        type="button"
        disabled={!!shownStep.disabled}
        title={shownStep.title || "The next step for this session: " + shownStep.label.replace(/…$/, "")}
        onClick={(ev) => {
          ev.stopPropagation();
          if (!shownStep.disabled) shownStep.run();
        }}
      >
        {shownStep.label}
      </button>
    ) : null,
    fasttrack: ft ? (
      // THE per-session fast-track control: it names the target and opens
      // the picker. A control, not a status chip.
      <button
        key="fasttrack"
        ref={setFtEl}
        className={
          "nextstep nextstep-fast" +
          (ft.active ? " is-on" : "") +
          (ft.halted ? " nextstep-fast-halted" : "") +
          (ft.lane !== "leave" ? " is-set" : "") +
          (ftMenu ? " open" : "")
        }
        type="button"
        aria-haspopup="menu"
        aria-expanded={!!ftMenu}
        aria-label={
          "Fast-track: " +
          (ft.lane === "leave" ? "off" : ft.label.replace(/^⏩ /, "")) +
          (ft.askFirst ? ", asks first" : "")
        }
        title={ft.title}
        data-ft-lane={ft.lane}
        onClick={(ev) => {
          ev.stopPropagation();
          useUi.getState().setFastTrackMenu(ftMenu ? null : { title });
        }}
      >
        {/* Off is just the glyph: a word there read as a second
            next step. The aria-label still says "Fast-track: off". */}
        {ft.lane === "leave" && !ft.halted ? "⏩" : ft.label}
        {ft.askFirst && (
          <span className="ft-ask" aria-hidden="true">
            ?
          </span>
        )}
      </button>
    ) : null,
    history: (
      <button
        key="history"
        className="act pane-hist"
        type="button"
        aria-label="Browse the full history"
        title="Browse the full history — scroll & select like a page (or drag a selection past the top of the terminal)"
        onClick={(e) => {
          e.stopPropagation();
          openHistory();
        }}
      >
        <svg
          className="btn-icon"
          width="12"
          height="12"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
          aria-hidden="true"
        >
          <path d="M12 8v4l2 2" />
          <path d="M3.05 11a9 9 0 1 1 .5 4" />
          <path d="M3 22v-6h6" />
        </svg>
        <span className="btn-label" aria-hidden="true">History</span>
      </button>
    ),
    // The active terminal's whole history to the clipboard, one click.
    // Disabled, not hidden, on Diff / Map / Queue / Thread, so the row never
    // shifts as you change tabs.
    copy: (
      <button
        key="copy"
        className="act copy-history"
        type="button"
        disabled={!termTab}
        aria-label="Copy this pane's whole history"
        title={
          termTab
            ? `Copy this pane's whole history — the ${termTab === "shell" ? "Terminal" : "Agent"} tab's full scrollback`
            : "Copy this pane's whole history — open the Agent or Terminal tab first"
        }
        onClick={(e) => {
          e.stopPropagation();
          copyHistory();
        }}
      >
        <svg
          className="btn-icon"
          width="12"
          height="12"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
          aria-hidden="true"
        >
          <path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2" />
          <rect x="8" y="2" width="8" height="4" rx="1" />
        </svg>
      </button>
    ),
    cost: <SessionUsageChip key="cost" inst={inst} />,
    diffstat: hasDiffStat ? <CtxLine key="diffstat" inst={inst} /> : null,
    hide: (
      <button
        key="hide"
        className="act pane-close"
        type="button"
        aria-label="Hide window"
        title={HIDE_TITLE}
        onClick={(e) => {
          e.stopPropagation();
          useUi.getState().setHidden(title, true);
        }}
      >
        −
      </button>
    ),
  };

  return (
    <section
      ref={paneRef as React.RefObject<HTMLElement>}
      className={"pane" + (focused ? " focused" : "") + (dragging ? " dragging" : "")}
      data-title={title}
      onMouseDown={() => selectSession(title, { noKeyboard: true })}
      {...paneDrag}
    >
      <div
        ref={headRef}
        className={"pane-head pane-head-fit" + (icons ? " head-icons" : "")}
        data-fit-key={fitKeyNow}
        {...headDrag}
      >
        <span className="grip" title="Drag to move this window">⠿</span>
        <span className="title" title={nameTip}>
          {titleText}
        </span>
        {deviceName && (
          <span className="title-device" title={"Runs on " + deviceName}>
            {deviceName}
          </span>
        )}
        {(inst.behind || 0) > 0 && (
          // Not a .stagechip: those render 0x0 inside a pane head.
          <span className="pane-behind" title={`${inst.behind} commit(s) on the remote branch that this session doesn't have — Push offers to rebase`}>
            ↓{inst.behind} behind
          </span>
        )}
        <div className="tabs" data-tour="pane-tabs" role="tablist" aria-label="Session views" onKeyDown={tabArrowKey}>
          {items.tabs.map((id) => tabEls[id])}
        </div>
        {/* Free room sits here, so the buttons and ⋯ hold the right edge. */}
        <span className="head-spacer" aria-hidden="true" />
        <div className="actions">
          {step && (
            <span
              className={"stepnow is-" + step.tone + (step.href ? " is-link" : "")}
              title={step.title}
              onClick={
                step.href
                  ? (ev) => {
                      ev.stopPropagation();
                      window.open(step.href!, "_blank");
                    }
                  : undefined
              }
            >
              <span className="stepnow-dot" aria-hidden="true" />
              <span className="stepnow-text">{step.label}</span>
            </span>
          )}
          {rs && (
            <button
              className="nextstep nextstep-reset"
              type="button"
              title={rs.title}
              aria-label="Back to idle"
              onClick={(ev) => {
                ev.stopPropagation();
                rs.run();
              }}
            >
              {rs.label}
            </button>
          )}
          {/* Verify's verdict, next to Open PR; opens its checklist. */}
          <VerdictChip inst={inst} where="pane" />
          {/* A shared session: the joint check's verdict, when its link has one. */}
          {inst.peer_share && (
            <JointVerdictChip linkId={inst.peer_with?.link_id} peerName={inst.peer_with?.name} where="pane" />
          )}
          <span
            className={"stagechip " + chip.cls}
            title={inst.pr_url ? "Open PR" : chip.title}
            style={inst.pr_url ? { cursor: "pointer" } : undefined}
            onClick={
              inst.pr_url
                ? (ev) => {
                    ev.stopPropagation();
                    window.open(inst.pr_url!, "_blank");
                  }
                : undefined
            }
          >
            {chip.label}
          </span>
          <AccountChip inst={inst} />
          {/* The keyboard is one person's at a time (lib/presence): "Driving"
              / "Watching — take control", only once someone else is here. */}
          <DriveButton inst={inst} me={me} />
          {/* The person's buttons, in their order (Customize → Pane header). */}
          {items.buttons.map((id) => buttonEls[id])}
        </div>
        <span className={"state" + (wsState !== "connected" ? " state-bad" : "")}>{wsState}</span>
        {/* Pinned right even when the header scrolls: ▶ Resume and ⋯ — always
            the last thing in the row — stay reachable without scrolling. */}
        <div className="head-tail">
        {canResume && (
          <button
            className="act pane-resume"
            type="button"
            aria-label="Resume session"
            title="Resume — starts the agent again in the same worktree"
            onClick={(e) => {
              e.stopPropagation();
              sessionAction("resume").run(title);
            }}
          >
            {/* Glyph only where the header is short of room. */}
            {icons ? "▶" : "▶ Resume"}
          </button>
        )}
        {/* The New tour's last step points here (and at the tabs). */}
        <span className="pane-menu-hook" data-tour="pane-menu">
        <PaneMoreMenu btnRef={ft && !ftShown ? setFtEl : undefined}>
          {(close) => (
            <>
              {menuSummary.length > 0 && (
                <div className="pane-more-summary" role="note">
                  {menuSummary.map((t) => (
                    <span key={t}>{t}</span>
                  ))}
                </div>
              )}
              <SessionMenu
                model={menu()}
                onDone={close}
                onInUse={(t) => errorPop("Can't delete " + displayName, folderInUseText(t))}
              />
            </>
          )}
        </PaneMoreMenu>
        </span>
        </div>
      </div>
      <AgentBlockedBanner block={noAgent} title={title} />
      <div className="pane-body" ref={bodyRef}>
        {paused ? (
          <PausedBody key="paused" title={title} hidden={tab !== "agent"} />
        ) : (
        <div
          key="agent-term"
          className={"pane-term agent-term" + (tab !== "agent" ? " hidden" : "")}
          ref={adopt("agent")}
        >
          {/* Pinned prompt: what this session was last asked to do, always
              visible while the output scrolls beneath it (the behavior the
              mobile view's short screen gives for free). One line by default;
              clicking the arrow — and only the arrow, no hover — drops the
              whole prompt OVER the terminal, capped at a few lines with its own
              scrollbar. Over, not in flow: expanding must not shove the output
              you are reading. Rendered before the terminal container adopt()
              appends, so it sits above the terminal in the flex column. */}
          {pinLine ? (
            <div className={"prompt-pin" + (pinOpen ? " pin-open" : "")}>
              <button
                type="button"
                className="prompt-pin-mark"
                aria-expanded={pinOpen}
                title={pinOpen ? "Collapse to one line" : "Show the whole prompt"}
                onClick={(e) => {
                  e.stopPropagation();
                  setPinOpen((v) => !v);
                }}
              >
                ❯
              </button>
              {/* The one-line summary is always RENDERED, even when open — it
                  is what holds the bar at exactly one line, so expanding never
                  resizes the terminal. Open, the body is drawn OVER the bar and
                  Pane.css hides this copy with visibility while it keeps
                  occupying that line — so there is no duplicated first line and
                  no blank row, and the terminal still never moves. */}
              {/* "Task:" names what the line is; a scrolled-back older prompt
                  (the ghost) is just a prompt. */}
              {ghost == null && <span className="prompt-pin-label">Task:</span>}
              <span className="prompt-pin-text">{pinLine}</span>
              {pinOpen ? <div className="prompt-pin-body">{pinBody}</div> : null}
              {/* While the TUI's own pinned row is being mirrored above, hide
                  the duplicate: a strip of terminal background over the top
                  row (desktop only by construction — mobile has no bar). */}
              {ghost ? <div className="ghost-row-mask" aria-hidden="true" /> : null}
            </div>
          ) : null}
          {!booted && (
            <div className="term-loading">
              <div className="spinner" />
              <p>Loading session…</p>
            </div>
          )}
        </div>
        )}
        {shellStarted ? (
          <div className={"pane-term shell-term" + (tab !== "shell" ? " hidden" : "")} ref={adopt("shell")} />
        ) : (
          <div className={"pane-term shell-term" + (tab !== "shell" ? " hidden" : "")} />
        )}
        {caps.git && (
          <div className={"pane-diff" + (tab !== "diff" ? " hidden" : "")}>
            <DiffTab title={title} active={tab === "diff"} stamp={diffStamp} />
          </div>
        )}
        {caps.git && (
          <div className={"pane-map" + (tab !== "map" ? " hidden" : "")}>
            <CodeMapTab title={title} active={tab === "map"} />
          </div>
        )}
        <div className={"pane-queue" + (tab !== "queue" ? " hidden" : "")}>
          <QueueTab title={title} active={tab === "queue"} />
        </div>
        {threadShown && (
          <div className={"pane-thread" + (tab !== "thread" ? " hidden" : "")}>
            <ThreadTab title={title} active={tab === "thread"} />
          </div>
        )}
        {paneFind && (
          <PaneFindBar
            title={title}
            pane={paneFind.kind}
            mode={paneFind.mode}
            initialQuery={paneFind.query}
            onClose={() => {
              const kind = paneFind.kind;
              setPaneFind(null);
              setTimeout(() => focusTerm(title, kind), 0);
            }}
          />
        )}
        {histPane && (
          <HistoryOverlay
            title={title}
            pane={histPane.kind}
            dragSelection={histPane.dragSel}
            dragEdge={histPane.edge ?? "top"}
            dragCtx={histPane.ctx ?? null}
            dragGhost={histPane.ghostSel ?? null}
            initialPos={histPane.pos ?? "bottom"}
            initialFind={histPane.find}
            onClose={() => {
              const kind = histPane.kind;
              setHistPane(null);
              // Hand the keyboard straight back to the terminal the overlay
              // covered — closing should feel like returning, not leaving.
              selectSession(title, { noKeyboard: true });
              setTimeout(() => focusTerm(title, kind), 0);
            }}
          />
        )}
        {reduceMotion && chip.cls.split(" ").includes("s-running") && tab === "agent" && (
          <RunningCover paneRef={paneRef} />
        )}
        {budget?.locked && <BudgetLock title={title} budget={budget} />}
      </div>
      {ftMenu && ft && ftEl && (
        <FastTrackMenu
          title={title}
          anchor={ftEl}
          onClose={(refocus) => {
            const ui = useUi.getState();
            if (ui.fastTrackMenu?.title === title) ui.setFastTrackMenu(null);
            // Esc hands the keyboard back to the terminal the picker took it
            // from. Nothing else does: an outside click has put it wherever
            // the user clicked.
            if (refocus) setTimeout(() => focusTerm(title), 0);
          }}
        />
      )}
    </section>
  );
}

/** The width a pane header's items take as they are drawn now, with the
 * session's name at its reserved room (TITLE_ROOM_PX, or its own width when
 * shorter): every in-flow child's box and margins, the gaps between them and
 * the header's padding. The spacer is free room, so it counts as nothing but
 * its gaps; children that aren't drawn (display: none) count as nothing at
 * all. Read before paint by the words↔icons fit (fitDecision). */
function headNeed(head: HTMLElement): number {
  const cs = getComputedStyle(head);
  const px = (v: string) => parseFloat(v) || 0;
  let need = px(cs.paddingLeft) + px(cs.paddingRight);
  let drawn = 0;
  for (const el of Array.from(head.children) as HTMLElement[]) {
    if (!el.getClientRects().length) continue;
    const ecs = getComputedStyle(el);
    if (ecs.position === "absolute" || ecs.position === "fixed") continue;
    drawn++;
    if (el.classList.contains("head-spacer")) continue;
    need += px(ecs.marginLeft) + px(ecs.marginRight);
    if (el.classList.contains("title")) need += Math.min(textWidth(el.textContent || "", ecs.font) + 2, TITLE_ROOM_PX);
    else need += el.getBoundingClientRect().width;
  }
  return need + px(cs.columnGap) * Math.max(0, drawn - 1);
}

let measureCtx: CanvasRenderingContext2D | null | undefined;
/** A string's natural width in a font — the name's, which its own box can't
 * say once it has been squeezed. */
function textWidth(text: string, font: string): number {
  if (measureCtx === undefined) measureCtx = document.createElement("canvas").getContext("2d");
  if (!measureCtx) return text.length * 7;
  measureCtx.font = font;
  return measureCtx.measureText(text).width;
}

/** Reduce-motion cover (Settings → General): while the agent is running, an
 * opaque panel hides the live terminal so its constant scrolling isn't
 * fatiguing. Any keypress, wheel, or click reveals the terminal and it
 * re-covers after a short idle. Listeners are capture-phase on the pane so they
 * fire before xterm's own handlers can stop propagation; the cover itself is
 * pointer-events:none, so the same interaction also reaches the terminal. */
function RunningCover({ paneRef }: { paneRef: React.RefObject<HTMLElement | null> }) {
  const [revealed, setRevealed] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);

  useEffect(() => {
    const el = paneRef.current;
    if (!el) return;
    const wake = () => {
      setRevealed(true);
      clearTimeout(timer.current);
      timer.current = setTimeout(() => setRevealed(false), 10000);
    };
    const opts: AddEventListenerOptions = { capture: true, passive: true };
    el.addEventListener("keydown", wake, opts);
    el.addEventListener("wheel", wake, opts);
    el.addEventListener("pointerdown", wake, opts);
    return () => {
      clearTimeout(timer.current);
      el.removeEventListener("keydown", wake, opts);
      el.removeEventListener("wheel", wake, opts);
      el.removeEventListener("pointerdown", wake, opts);
    };
  }, [paneRef]);

  if (revealed) return null;
  return (
    <div className="running-cover" aria-hidden="true">
      <span className="rc-badge">
        <span className="rc-dot" />
        <span className="rc-label">running</span>
      </span>
      <span className="rc-hint">
        Clicking, scrolling, or typing anywhere in the window will make the content appear
      </span>
    </div>
  );
}

/** ←/→ (and Home/End) move between the header's tabs, as a tablist should;
 * Enter/Space on the focused tab opens it (it is a button). */
function tabArrowKey(e: React.KeyboardEvent<HTMLDivElement>) {
  if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(e.key)) return;
  const tabs = Array.from(e.currentTarget.querySelectorAll<HTMLElement>('[role="tab"]'));
  const i = tabs.indexOf(document.activeElement as HTMLElement);
  if (!tabs.length || i < 0) return;
  e.preventDefault();
  e.stopPropagation();
  const n = tabs.length;
  const j = e.key === "Home" ? 0 : e.key === "End" ? n - 1 : (i + (e.key === "ArrowRight" ? 1 : -1) + n) % n;
  tabs[j].focus();
}

function ctxNum(n: number): string {
  if (n < 1000) return String(n);
  const k = n / 1000;
  return (k < 10 ? k.toFixed(1) : String(Math.round(k))).replace(/\.0$/, "") + "k";
}

/** The diff-stat as one line ("+12 −3 · 2 files") — ⋯'s read-out when the
 * header leaves the diff-stat out. */
function diffStatText(inst: Instance): string {
  const ds = inst.diff_stat;
  const files = ds?.files || 0;
  return `+${ctxNum(ds?.additions || 0)} −${ctxNum(ds?.deletions || 0)} · ${files}${files === 1 ? " file" : " files"}`;
}

function CtxLine({ inst }: { inst: Instance }) {
  const ds = inst.diff_stat!;
  const files = ds.files || 0,
    additions = ds.additions || 0,
    deletions = ds.deletions || 0;
  const plural = files === 1 ? " file" : " files";
  let tip = `Total change vs base: +${additions} −${deletions} across ${files}${plural}`;
  const un = ds.uncommitted;
  if (un) tip += ` · uncommitted +${un.additions || 0} −${un.deletions || 0}`;
  return (
    <span className="ctxline" title={tip}>
      <span className="ctx-add">+{ctxNum(additions)}</span>
      <span className="ctx-del">−{ctxNum(deletions)}</span>
      <span className="ctx-files">· {files}{plural}</span>
    </span>
  );
}

/** Budget lock overlay: typing paused until the ceiling is raised. */
function BudgetLock({
  title,
  budget,
}: {
  title: string;
  budget: { cost?: number; limit?: number };
}) {
  const cost = budget.cost || 0;
  const limit = budget.limit || 0;
  const suggested = Math.max(Math.ceil((cost + 1) * 2) / 2, Math.ceil(limit + 1));
  const [value, setValue] = useState(String(suggested));
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const [hiddenOptimistic, setHiddenOptimistic] = useState(false);
  if (hiddenOptimistic) return null;

  const raise = async (hours: number) => {
    const lim = parseFloat(value);
    if (!(lim > 0)) {
      setErr("Enter a dollar amount above your current spend.");
      return;
    }
    setErr("");
    setBusy(true);
    try {
      await instApi(title, "/budget/raise", { json: { limit: lim, hours } });
      toast(hours > 0 ? `Budget raised to ${fmtUsd(lim)} for ${hours}h` : `Budget raised to ${fmtUsd(lim)}`);
      setHiddenOptimistic(true); // the next poll confirms
      refreshInstances();
    } catch (e) {
      setErr("Couldn't raise budget: " + ((e as Error).message || "error"));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="budget-lock">
      <div className="bl-card">
        <h3>💸 Budget reached</h3>
        <p className="bl-msg">
          This session has spent {fmtUsd(cost)} of its {fmtUsd(limit)} budget. Typing is paused
          until you raise it.
        </p>
        <label className="bl-limit-row">
          New budget (USD)
          <input
            className="bl-limit"
            type="number"
            min="0"
            step="0.5"
            inputMode="decimal"
            value={value}
            onChange={(e) => setValue(e.target.value)}
          />
        </label>
        <div className="bl-durations">
          {[1, 4, 24].map((h) => (
            <button key={h} type="button" className="bl-dur" disabled={busy} onClick={() => raise(h)}>
              {h} hour{h > 1 ? "s" : ""}
            </button>
          ))}
          <button type="button" className="bl-dur bl-forever" disabled={busy} onClick={() => raise(0)}>
            Forever
          </button>
        </div>
        <p className="bl-err error">{err}</p>
      </div>
    </div>
  );
}
