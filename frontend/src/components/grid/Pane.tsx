/** One session pane (port of app.js makePane, section 14): header with grip /
 * title / diff-stat context line / tabs / next-step / usage chip / state pill
 * / history + copy-all + hide (✕), the terminal hosts, Diff + Queue tabs, and
 * the budget-lock overlay. Terminals are adopted from lib/terminals' registry
 * so they never remount with the pane. */

import { useCallback, useEffect, useRef, useState } from "react";
import type { Instance } from "../../api/types";
import { instApi } from "../../api/client";
import { refreshInstances, useConfig } from "../../state/queries";
import { useUi } from "../../state/store";
import { copyText } from "../../lib/clipboard";
import { fmtUsd, displayBranch } from "../../lib/format";
import { chipState, fastTrackStep, liveStep, nextStep, resetStep } from "../../lib/stage";
import { cleanupMissing, selectSession } from "../../lib/sessionActions";
import {
  focusTerm,
  getTerm,
  peekTerm,
  type DragScreenCtx,
  type TermHandle,
} from "../../lib/terminals";
import { toast } from "../../lib/toast";
import { dropSideFor, type DropSide } from "./layout";
import type { DragCtx } from "./TerminalGrid";
import { DiffTab } from "./DiffTab";
import { HistoryOverlay } from "./HistoryOverlay";
import { QueueTab } from "./QueueTab";
import { SessionUsageChip } from "../usage/SessionUsageChip";
import { AccountChip } from "./AccountChip";

type Tab = "agent" | "shell" | "diff" | "queue";

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

  const savedTab = (lastTab as Tab) || "agent";
  const [tab, setTab] = useState<Tab>(
    savedTab === "diff" && !caps.git ? "agent" : savedTab
  );
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
  } | null>(null);
  const bodyRef = useRef<HTMLDivElement | null>(null);
  const fitTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);

  const displayName = alias || title;

  // Adopt a registry terminal into a host div; re-runs safely on remount.
  const adopt = useCallback(
    (kind: "agent" | "shell") => (el: HTMLDivElement | null) => {
      if (!el || missing || loading) return;
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
    [title, missing, loading]
  );

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
  const pinLine = ghost ?? inst.last_prompt;

  // Expanding an OLDER prompt (the ghost isn't the latest) needs its full
  // body looked up server-side — only the latest prompt's body rides the
  // snapshot. The ghost's own width-truncated text shows while it loads.
  const [lookup, setLookup] = useState<{ q: string; text: string } | null>(null);
  useEffect(() => {
    if (!pinOpen || ghostIsLatest || ghostCore.length < 8) return;
    if (lookup?.q === ghostCore) return;
    let dead = false;
    fetch(
      `/api/instances/${encodeURIComponent(title)}/prompt?q=${encodeURIComponent(ghostCore)}`
    )
      .then((r) => (r.ok ? r.json() : null))
      .then((j: { text?: string | null } | null) => {
        if (!dead && j?.text) setLookup({ q: ghostCore, text: j.text });
      })
      .catch(() => {});
    return () => {
      dead = true;
    };
  }, [pinOpen, ghostIsLatest, ghostCore, title, lookup]);

  const pinBody = ghostIsLatest
    ? inst.last_prompt_full || inst.last_prompt
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

  const showTab = (t: Tab) => {
    // A tab click while the full-history overlay covers the body is
    // navigation, not a request to switch under glass: without this the
    // clicked tab highlighted while the overlay kept covering its content,
    // which reads as the tab being dead. Clicking the ACTIVE tab closes the
    // overlay too — "show me the tab" is what the press means either way.
    setHistPane(null);
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
    let t = lastTab as Tab;
    if (t === "diff" && !caps.git) t = "agent";
    if (t === tab) return;
    // Same rule as showTab: a switch done FOR the user (the commit dialog
    // jumping to the shell to watch pre-commit hooks) must be visible, so the
    // history overlay closes rather than covering the tab it switched to.
    setHistPane(null);
    setTab(t);
    if (t === "shell") setShellStarted(true);
    setTimeout(() => peekTerm(title, t === "shell" ? "shell" : "agent")?.doFit(), 0);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [lastTab]);

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
          <span className="title">{displayName}</span>
          <span className="stagechip s-missing" title="Workspace directory no longer exists">
            missing workspace
          </span>
          <span className="state">workspace gone</span>
          <div className="head-tail">
            <button
              className="act pane-close"
              type="button"
              aria-label="Hide window"
              title="Hide this window (show it again from its sidebar row)"
              onClick={(e) => {
                e.stopPropagation();
                useUi.getState().setHidden(title, true);
              }}
            >
              ✕
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
              Clean up
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
          <span className="title">{displayName}</span>
          <span className="branch">{inst.branch ? "(" + displayBranch(inst) + ")" : ""}</span>
          <span className="state">provisioning…</span>
          <div className="head-tail">
            <button
              className="act pane-close"
              type="button"
              aria-label="Hide window"
              title="Hide this window — provisioning keeps going (show it again from its sidebar row)"
              onClick={(e) => {
                e.stopPropagation();
                useUi.getState().setHidden(title, true);
              }}
            >
              ✕
            </button>
          </div>
        </div>
        <div className="pane-body">
          <div className="provisioning">
            <div className="spinner" />
            <p>Provisioning workspace…</p>
            <p className="muted">
              The first provisioned run clones the base repo — this can take a while.
            </p>
          </div>
        </div>
      </section>
    );
  }

  const chip = chipState(inst);
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

  return (
    <section
      ref={paneRef as React.RefObject<HTMLElement>}
      className={"pane" + (focused ? " focused" : "") + (dragging ? " dragging" : "")}
      data-title={title}
      onMouseDown={() => selectSession(title, { noKeyboard: true })}
      {...paneDrag}
    >
      <div className="pane-head" {...headDrag}>
        <span className="grip" title="Drag to move this window">⠿</span>
        <span className="title" title={(alias ? title + "  ·  " : "") + (inst.branch || title)}>
          {displayName}
        </span>
        {hasDiffStat && <CtxLine inst={inst} />}
        <div className="tabs">
          <button data-tab="agent" className={tab === "agent" ? "active" : ""} onClick={(e) => { e.stopPropagation(); showTab("agent"); }}>
            Agent
          </button>
          <button data-tab="shell" className={tab === "shell" ? "active" : ""} onClick={(e) => { e.stopPropagation(); showTab("shell"); }}>
            Terminal
          </button>
          {caps.git && (
            <button data-tab="diff" className={tab === "diff" ? "active" : ""} onClick={(e) => { e.stopPropagation(); showTab("diff"); }}>
              Diff
            </button>
          )}
          <button
            data-tab="queue"
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
            Queue
            {(pending > 0 || holding) && (
              <span className="queue-tab-badge">
                {holding ? "⏳" : pending > 99 ? "99+" : String(pending)}
              </span>
            )}
          </button>
        </div>
        <div className="actions">
          {ns ? (
            <button
              className={
                "nextstep" +
                (ns.hint ? " nextstep-hint" : "") +
                (ns.disabled ? " nextstep-blocked" : "")
              }
              type="button"
              disabled={!!ns.disabled}
              title={ns.title || "Do the next step"}
              onClick={(ev) => {
                ev.stopPropagation();
                if (!ns.disabled) ns.run();
              }}
            >
              {ns.label}
            </button>
          ) : null}
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
              {step.target && <span className="stepnow-target">{step.target}</span>}
            </span>
          )}
          {ft && (
            <button
              className={
                "nextstep nextstep-fast" +
                (ft.active ? " is-on" : "") +
                (ft.hint ? " nextstep-fast-halted" : "")
              }
              type="button"
              aria-pressed={!!ft.active}
              title={ft.title}
              onClick={(ev) => {
                ev.stopPropagation();
                ft.run();
              }}
            >
              {ft.label}
            </button>
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
        </div>
        <AccountChip inst={inst} />
        <SessionUsageChip inst={inst} />
        <span className={"state" + (wsState !== "connected" ? " state-bad" : "")}>{wsState}</span>
        {/* Pinned right even when the header scrolls: history, copy-all, close
            stay reachable without scrolling to the end of a long header. */}
        <div className="head-tail">
        <button
          className="act copyhist"
          type="button"
          title="Browse the full history — scroll & select like a page (or drag a selection past the top of the terminal)"
          onClick={(e) => {
            e.stopPropagation();
            setHistPane({ kind: tab === "shell" ? "shell" : "agent", dragSel: null });
          }}
        >
          <svg
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
        </button>
        <button
          className="act copyhist"
          type="button"
          title="Copy this pane's whole history to the clipboard"
          onClick={(e) => {
            e.stopPropagation();
            const which = tab === "shell" ? "shell" : "agent";
            fetch(`/api/instances/${encodeURIComponent(title)}/history?pane=${which}`)
              .then((r) => {
                if (!r.ok)
                  return r.text().then((t) => {
                    throw new Error(t || "HTTP " + r.status);
                  });
                return r.text();
              })
              .then((text) => {
                if (!text.trim()) {
                  toast("No history to copy");
                  return;
                }
                copyText(text).then((ok) =>
                  toast(ok ? `Copied full ${which} history (${text.length} chars)` : "Copy failed")
                );
              })
              .catch((err) => toast("History copy failed: " + (err as Error).message));
          }}
        >
          <svg
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
        <button
          className="act pane-close"
          type="button"
          aria-label="Hide window"
          title="Hide this window — the session keeps running (show it again from its sidebar row)"
          onClick={(e) => {
            e.stopPropagation();
            useUi.getState().setHidden(title, true);
          }}
        >
          ✕
        </button>
        </div>
      </div>
      <div className="pane-body" ref={bodyRef}>
        <div className={"pane-term agent-term" + (tab !== "agent" ? " hidden" : "")} ref={adopt("agent")}>
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
        {shellStarted ? (
          <div className={"pane-term shell-term" + (tab !== "shell" ? " hidden" : "")} ref={adopt("shell")} />
        ) : (
          <div className={"pane-term shell-term" + (tab !== "shell" ? " hidden" : "")} />
        )}
        {caps.git && (
          <div className={"pane-diff" + (tab !== "diff" ? " hidden" : "")}>
            <DiffTab title={title} active={tab === "diff"} />
          </div>
        )}
        <div className={"pane-queue" + (tab !== "queue" ? " hidden" : "")}>
          <QueueTab title={title} active={tab === "queue"} />
        </div>
        {histPane && (
          <HistoryOverlay
            title={title}
            pane={histPane.kind}
            dragSelection={histPane.dragSel}
            dragEdge={histPane.edge ?? "top"}
            dragCtx={histPane.ctx ?? null}
            dragGhost={histPane.ghostSel ?? null}
            initialPos={histPane.pos ?? "bottom"}
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
        {reduceMotion && chip.cls === "s-running" && tab === "agent" && (
          <RunningCover paneRef={paneRef} />
        )}
        {budget?.locked && <BudgetLock title={title} budget={budget} />}
      </div>
    </section>
  );
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

function CtxLine({ inst }: { inst: Instance }) {
  const ds = inst.diff_stat!;
  const files = ds.files || 0,
    additions = ds.additions || 0,
    deletions = ds.deletions || 0;
  const plural = files === 1 ? " file" : " files";
  const ctxNum = (n: number) => {
    if (n < 1000) return String(n);
    const k = n / 1000;
    return (k < 10 ? k.toFixed(1) : String(Math.round(k))).replace(/\.0$/, "") + "k";
  };
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
