/** One session row (ports app.js section 9's _createSidebarRow /
 * _updateSidebarRow / _instActionsHtml / _rowAction). */

import {
  memo,
  useCallback,
  useEffect,
  useReducer,
  useRef,
  useState,
  useSyncExternalStore,
  type MouseEvent,
  type ReactElement,
} from "react";
import { rowDndProps } from "./rowDnd";
import type { NestInfo } from "./ordering";
import type { Instance } from "../../api/types";
import { instApi } from "../../api/client";
import { queryClient, refreshInstances, useConfig } from "../../state/queries";
import { useUi } from "../../state/store";
import { copyText } from "../../lib/clipboard";
import { errorPop } from "../../lib/errorPop";
import { errMsg } from "../../lib/format";
import { chipState, checkChip, effectiveActivity } from "../../lib/stage";
import { sessionLabel } from "../../lib/sessionLabel";
import {
  PR_FALLBACK_HINT,
  cleanupMissing,
  commitSession,
  copySession,
  hasPrSupport,
  hideSession,
  ideSession,
  killSession,
  makePrSession,
  mergeSession,
  pauseSession,
  pushSession,
  resumeSession,
  selectSession,
} from "../../lib/sessionActions";
import { toast } from "../../lib/toast";
import { peekTerm, subscribeTermStates } from "../../lib/terminals";
import { codemapSeenAt, redZoneChip, subscribeCodemapSeen } from "../../lib/codemapSeen";
import {
  inFamily,
  lineageMark,
  parentChip,
  rollup,
  shipLine,
  workerLine,
  type ShipTask,
} from "../../lib/agentMessages";
import { openThread, pasteWrapup } from "../../lib/flockActions";
import { forkBlockReason } from "../../lib/playbooks";
import { isEditingTarget } from "../../lib/keymap";
import { AnswerStrip, type AnswerStripHandle } from "../AnswerStrip";
import { leadChip, leadLine, railExtraChips } from "../../lib/splitRun";
import { RUN_DONE_STATES, type RunInfo } from "../../lib/runs";
import type { RunDTO } from "../../api/types";
import { SessionRowItems } from "./SessionRowItems";

/** How long a click on the selected row waits for a second click before it
 * turns into an inline rename. Under the browser's ~500ms dblclick ceiling,
 * but long enough that an unhurried double-click still opens the IDE. */
const DBLCLICK_MS = 300;

function displayTitle(inst: Instance): string {
  return (inst as unknown as { display_title?: string }).display_title || inst.title || "";
}

/** Family nesting geometry (px, in the row's own box). The status dot of a
 * flat row is centred at x≈32 (12px padding + the 11px number + a 5px gap);
 * each level moves the dot onwards right by NEST_STEP, and a level's
 * connector runs down the centre of its PARENT's dot. The number column never
 * moves: numbering is railOrder's, nesting is paint. */
const NEST_STEP = 14;
const NEST_X0 = 32;
const nestX = (depth: number) => NEST_X0 + NEST_STEP * (depth - 1);

/** The connector lines of one nested row. `part` "row" draws inside the
 * `.inst-row` (so the elbow can meet the dot at the row's vertical middle);
 * "tail" draws through whatever hangs under the row — the answer strip, the
 * actions menu — and on across the gap to the next row, so a family's guide
 * never breaks around a blocked worker's buttons. */
function NestLines({ nest, part }: { nest: NestInfo; part: "row" | "tail" }) {
  const lines: ReactElement[] = [];
  for (let k = 1; k < nest.depth; k++)
    if (nest.guides[k])
      lines.push(<span key={"g" + k} className="nl nl-v" style={{ left: nestX(k) }} />);
  if (part === "row") {
    if (nest.depth) {
      lines.push(
        <span key="elbow" className="nl nl-elbow" style={{ left: nestX(nest.depth), width: NEST_STEP - 5 }} />
      );
      if (nest.more) lines.push(<span key="more" className="nl nl-down" style={{ left: nestX(nest.depth) }} />);
    }
    if (nest.stem) lines.push(<span key="stem" className="nl nl-stem" style={{ left: nestX(nest.depth + 1) }} />);
  } else {
    if (nest.depth && nest.more)
      lines.push(<span key="more" className="nl nl-v" style={{ left: nestX(nest.depth) }} />);
    if (nest.stem) lines.push(<span key="stem" className="nl nl-v" style={{ left: nestX(nest.depth + 1) }} />);
  }
  return lines.length ? <>{lines}</> : null;
}

const FLAT: NestInfo = { depth: 0, more: false, guides: [], stem: false };
const NO_KIDS: Instance[] = [];

interface Props {
  inst: Instance;
  idx: number;
  onScreen: boolean;
  dropCue: "above" | "below" | null;
  onDragState(title: string | null): void;
  onDropCue(title: string, cue: "above" | "below" | null): void;
  onDropRow(dragTitle: string, targetTitle: string, before: boolean): void;
  /** Visual family nesting (`railNesting`); absent = flat. */
  nest?: NestInfo;
  /** The live sessions whose `parent` is this one (its workers), rail order. */
  kids?: Instance[];
  /** This row's `parent` is a live session on this rail. */
  parentLive?: boolean;
  /** Its group's view of this session (ship lanes): escalations, approvals. */
  runTask?: ShipTask | null;
  /** The group this session LEADS (a split, or one-for-all), when it does. */
  leadRun?: RunInfo | null;
}

export const SidebarRow = memo(function SidebarRow({
  inst,
  idx,
  onScreen,
  dropCue,
  onDragState,
  onDropCue,
  onDropRow,
  nest = FLAT,
  kids = NO_KIDS,
  parentLive = false,
  runTask = null,
  leadRun = null,
}: Props) {
  const [expanded, setExpanded] = useState(false);
  // "Delete + wipe worktree" asks in place (the desktop app has no confirm()):
  // the first click arms it, the second does it. Closing the menu disarms.
  const [wipeArmed, setWipeArmed] = useState(false);
  const strip = useRef<AnswerStripHandle | null>(null);
  const [editing, setEditing] = useState(false);
  // Escape must abandon the edit; unmounting the focused input can still run
  // the blur handler, so the cancel is flagged rather than inferred.
  const cancelled = useRef(false);
  const renameTimer = useRef<number | null>(null);
  // When a click ends an edit, that same click also reaches the row — without
  // this the dismiss would immediately re-arm the rename.
  const editEndedAt = useRef(0);
  const { data: config } = useConfig();
  const focused = useUi((s) => s.focused);
  const hidden = useUi((s) => s.hidden.has(inst.title));
  // All aliases, not just this row's: the lineage, the worker line, the
  // roll-up and its tooltip name OTHER sessions, and memo would otherwise hold
  // a renamed parent or worker until the next poll.
  const aliases = useUi((s) => s.aliases);
  const alias = aliases[inst.title];
  const openDialogFor = useUi((s) => s.openDialogFor);
  const setAlias = useUi((s) => s.setAlias);

  const title = inst.title;
  const missing = !!inst.workspace_missing;
  const paused = inst.status === "paused";
  // A force-start the server has accepted but not yet turned into a session:
  // it exists only as this row, so nothing on it is actionable yet.
  const pending = !!inst.pending;
  const caps = config?.caps ?? { git: true, tailscale: true, ticketing: true, github: true };
  // gh/token absent: PR + Merge stay in the menu (they fall back to GitHub in
  // the browser), but say so on hover instead of failing after the click.
  const prSupport = hasPrSupport(caps);
  const ideName = config?.ide_name || "Cursor";
  // A one-for-all / split piece that MindFlock merged back into its lead's
  // branch: that is its stage now, whatever its own branch says ("committed").
  const chip0 = chipState(inst);
  const chip =
    runTask?.state === "integrated"
      ? runTask.sameFolder
        ? { ...chip0, label: "committed", cls: "s-committed", title: "MindFlock committed its paths on its lead's branch" }
        : { ...chip0, label: "merged", cls: "s-committed", title: "Merged back into its lead's branch" }
      : chip0;
  const check = checkChip(inst);
  const num = idx < 9 ? String(idx + 1) : "";
  // Ticket/PR/issue sessions read as "(tix) add-dark-mode/sc-12345" instead of
  // the bare slug; a hand-made session's title is passed through unchanged.
  const label = sessionLabel(displayTitle(inst), inst.branch || "");
  const shown = alias || label.text;
  // Another session, named the way ITS row reads (alias, else the ticket/PR
  // label) so the two can be matched by eye.
  const nameOf = (t: string) => {
    if (aliases[t]) return aliases[t];
    const p = queryClient.getQueryData<Instance[]>(["instances"])?.find((x) => x.title === t);
    return p ? sessionLabel(displayTitle(p), p.branch || "").text : t;
  };
  // Lineage (MindFlock MCP): a muted "↳ <parent>" sub-line under the name of a
  // session another session's agent spawned or adopted.
  const lineage = lineageMark(inst.parent, inst.spawned, nameOf);
  // A family (an orchestrator and the workers it spawned or adopted) says how
  // the split is going right on the rail, with no fetch: a worker swaps the
  // "↳ parent" line for its status ("✓ reported", "? needs your answer" — still
  // naming the parent when it isn't drawn under it), the orchestrator gets a
  // roll-up of its workers, and while it sits idle over them its chip reads
  // "waiting" or, once all have reported, a clickable "wrap up".
  const activity = effectiveActivity(inst);
  const isWorker = !!inst.parent && parentLive && !pending;
  const nested = nest.depth > 0;
  const wline = isWorker
    ? workerLine(inst, { nested, parentName: nameOf(inst.parent!), act: activity })
    : null;
  // A group's LEAD (a split, or one for all): MindFlock merges its workers back
  // itself, so the row says how the group is going ("3 of 3 merged back") and
  // its chip asks for the one click that is yours (`→ PR?`, `plan?`) — never
  // the paste-the-wrap-up chip of a hand-run family.
  const isLead = inst.run?.role === "lead" && !pending;
  const lchip = isLead ? leadChip(leadRun) : null;
  const lline = isLead ? leadLine(leadRun as RunDTO | null) : null;
  const roll = kids.length && !pending && !isLead ? rollup(kids, nameOf, effectiveActivity) : null;
  // Fold this session's sub-sessions out of the rail (lib/familyFold). A
  // worded toggle beside the roll-up, never another chevron: the row's ›
  // already means "actions". The roll-up stays, so a folded family still
  // says what needs you.
  const folded = useUi((s) => s.collapsedFamilies.has(title));
  const foldBtn =
    kids.length && !pending ? (
      <button
        type="button"
        className="fold-kids"
        aria-expanded={!folded}
        title={
          folded
            ? `Show its ${kids.length} sub-session${kids.length === 1 ? "" : "s"} in the sidebar`
            : `Hide its ${kids.length} sub-session${kids.length === 1 ? "" : "s"} from the sidebar — they keep running`
        }
        onMouseDown={(e) => e.stopPropagation()}
        onClick={(e) => act(() => useUi.getState().toggleFamilyCollapsed(title), e)}
        onDoubleClick={(e) => e.stopPropagation()}
      >
        {folded ? "show " + kids.length : "hide"}
      </button>
    ) : null;
  // "wrap up" is a one-click paste into this session: never offered as one
  // when it can't take a paste (no tools this launch) — the fork button's rule.
  const pchip =
    kids.length && !pending && !missing && !isLead
      ? parentChip(inst, kids, nameOf, effectiveActivity, forkBlockReason(inst))
      : null;
  // Ship lanes: how far MindFlock carries this session ("→ PR · working 12m",
  // "⇡ opening PR", "✓ PR #318 · checks ✓"). It takes the status line's slot —
  // the worker line / lineage it replaces rides in its tooltip — so a session
  // in a group reads the same whether or not it is also in a family.
  const ship = pending || (isLead && lline) ? null : shipLine(inst, { act: activity, task: runTask });
  // A session MindFlock is carrying (a group member, or one with a lane) is
  // one you have handed off: its prompt gets the strip like a family's does.
  const shipLane = !!inst.run || !!ship;
  // A line that is waiting on YOU (a stuck group line, an ask-first approval)
  // is a door to where you act on it: the bell's "Needs attention" row. Only
  // where the bell HAS that row: a halted fast-track ("! fast-track stopped")
  // is also "escalated", but the bell holds nothing for it — its reason and
  // retry are on the pane's red ⏩ — so that line stays plain text.
  const runTaskState = String(runTask?.state || "");
  const shipOpensBell =
    !!ship &&
    (ship.state === "approve" ||
      (ship.state === "escalated" && (runTaskState === "needs_you" || runTaskState === "failed")));
  const openBell = () => {
    document.dispatchEvent(new CustomEvent("mf-open-bell", { detail: { title } }));
  };
  // The answer strip: a family member stuck on a dialog gets that dialog's own
  // buttons under its row (the orchestrator's spawn_session permission
  // prompts land here too — from its very first one, via its playbook). Keys
  // 1–9 press them while the ROW has focus.
  const answering =
    (inFamily(inst, isWorker, kids.length) || shipLane) && activity === "clarify" && !missing && !paused;
  const subline = !editing && (ship || wline || roll || lline || lineage);
  // "working · 6m" counts up by itself: a busy worker's row data can sit
  // unchanged between polls, and memo would freeze the minutes.
  const [, tick] = useReducer((n: number) => n + 1, 0);
  const counting = wline?.state === "working" || ship?.state === "working";
  useEffect(() => {
    if (!counting) return;
    const t = window.setInterval(tick, 30_000);
    return () => clearInterval(t);
  }, [counting]);
  const folder = inst.folder || inst.path || "";
  // Subscribed (not a render-time snapshot): the row must clear its red dot
  // the moment the agent socket connects, not on the next instances poll.
  const agentWs = useSyncExternalStore(
    subscribeTermStates,
    useCallback(() => peekTerm(title, "agent")?.state, [title])
  );
  const disconnected = inst.status === "running" && onScreen && agentWs === "disconnected";
  // Red-zone chip: breaches / an unseen block (red), a guard that isn't holding
  // (amber), or a shield while it is. "Unseen" is against when this browser last
  // had the Map open for the session — subscribed, so opening the Map clears it
  // at once rather than on the next poll.
  const mapSeen = useSyncExternalStore(
    subscribeCodemapSeen,
    useCallback(() => codemapSeenAt(title), [title])
  );
  // A merged-back piece / a lead asking for its release keeps ONE chip.
  const extra = railExtraChips(check, caps.git ? redZoneChip(inst.redzone, mapSeen) : null, {
    integrated: runTask?.state === "integrated",
    // A finished group's lead says how it ended on its line; its chips go quiet.
    leadAsks: !!lchip || (isLead && !!leadRun && RUN_DONE_STATES.has(leadRun.state)),
  });
  const rz = extra.rz;

  const act = async (fn: () => void | Promise<void>, e?: MouseEvent) => {
    e?.stopPropagation();
    await fn();
  };

  const clearRenameTimer = () => {
    if (renameTimer.current !== null) {
      clearTimeout(renameTimer.current);
      renameTimer.current = null;
    }
  };
  useEffect(() => clearRenameTimer, []);
  useEffect(() => {
    if (!expanded) setWipeArmed(false);
  }, [expanded]);

  /** Click on the row that's ALREADY selected → edit the name in place. The
   * second click of a double-click also lands here, so the edit is held for
   * the double-click window and cancelled by onDoubleClick (open in IDE). */
  const armRename = () => {
    clearRenameTimer();
    renameTimer.current = window.setTimeout(() => {
      renameTimer.current = null;
      cancelled.current = false;
      setEditing(true);
    }, DBLCLICK_MS);
  };

  const commitRename = (raw: string) => {
    setEditing(false);
    editEndedAt.current = Date.now();
    if (cancelled.current) {
      cancelled.current = false;
      return;
    }
    const next = raw.trim();
    // Blank, or typed back to what the row shows by itself, means "drop the
    // alias" — that's the default label as well as the raw title.
    const nextAlias =
      !next || next === label.text || next === displayTitle(inst) ? "" : next;
    if (nextAlias === (alias || "")) return;
    setAlias(title, nextAlias);
    toast(nextAlias ? `Renamed to “${nextAlias}”` : "Reset to real title");
  };

  const rowCls =
    "inst" +
    (nested ? " nest-" + nest.depth : "") +
    (nest.stem ? " has-stem" : "") +
    (focused === title ? " active" : "") +
    (hidden ? " is-hidden" : "") +
    (missing ? " ws-missing" : "") +
    (pending ? " is-pending" : "") +
    (dropCue ? ` drop-${dropCue}` : "");

  return (
    <li
      className={rowCls}
      data-title={title}
      // Focusable only while it carries an answer strip, so Tab reaches a
      // blocked worker and 1–9 answer it — never a bare digit typed elsewhere.
      tabIndex={answering ? 0 : undefined}
      onKeyDown={(e) => {
        if (!answering || e.ctrlKey || e.metaKey || e.altKey || !/^[1-9]$/.test(e.key)) return;
        if (isEditingTarget(e.target as Element)) return;
        if (strip.current?.answerKey(e.key)) {
          e.preventDefault();
          e.stopPropagation();
        }
      }}
      {...rowDndProps(
        title,
        { onDragState, onDropCue, onDropRow },
        // Row drag would hijack text selection inside the rename input.
        !editing
      )}
    >
      <div
        className="inst-row"
        onClick={() => {
          if (editing || Date.now() - editEndedAt.current < DBLCLICK_MS + 100) return;
          if (focused !== title) {
            selectSession(title);
            return;
          }
          if (!pending) armRename();
        }}
        onDoubleClick={() => {
          clearRenameTimer();
          if (!missing && !pending) ideSession(title, true);
        }}
      >
        {(nested || nest.stem) && <NestLines nest={nest} part="row" />}
        <span className="grip" title="Drag to reorder">⠿</span>
        <span className="idx" title={num ? `Ctrl+${num} / Alt+${num} to focus` : ""}>{num}</span>
        <span className={"dot " + inst.status + (disconnected ? " disconnected" : "")} />
        {!pending && (
          <button
            className={"chevron" + (expanded ? " open" : "")}
            title="Actions"
            onClick={(e) => act(() => setExpanded((v) => !v), e)}
          >
            ›
          </button>
        )}
        <span className={"meta" + (subline ? " has-lineage" : "")}>
          {editing ? (
            <input
              className="title title-edit"
              type="text"
              defaultValue={shown}
              autoFocus
              autoComplete="off"
              spellCheck={false}
              onFocus={(e) => e.currentTarget.select()}
              onMouseDown={(e) => e.stopPropagation()}
              onClick={(e) => e.stopPropagation()}
              onDoubleClick={(e) => e.stopPropagation()}
              onBlur={(e) => commitRename(e.currentTarget.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  e.preventDefault();
                  commitRename(e.currentTarget.value);
                } else if (e.key === "Escape") {
                  e.preventDefault();
                  cancelled.current = true;
                  editEndedAt.current = Date.now();
                  setEditing(false);
                }
              }}
            />
          ) : (
            <span
              className="title"
              title={[
                alias ? `${alias}  ·  ${label.text}` : label.text,
                // The real title is the identity behind a reformatted label —
                // it's what every API path and `tmux attach` is keyed by.
                label.kind ? `session: ${displayTitle(inst)}` : "",
                inst.branch ? `branch: ${inst.branch}` : "",
                wline ? wline.title : lineage ? lineage.title : "",
                focused === title ? "Click again to rename" : "",
              ]
                .filter(Boolean)
                .join("\n")}
            >
              {shown}
            </span>
          )}
          {!editing && ship && (
            <span
              className={"lineage ship-line " + ship.cls + (shipOpensBell ? " opens-bell" : "")}
              title={[
                ship.title,
                wline ? wline.title : lineage ? lineage.title : "",
                shipOpensBell ? "Click to open it in the bell" : "",
              ]
                .filter(Boolean)
                .join("\n")}
              {...(shipOpensBell
                ? {
                    role: "button",
                    tabIndex: 0,
                    // Like the lead line: the click is the line's, never the
                    // row's select / rename, and a double-click never opens
                    // the IDE. mousedown stays put so it can't start a drag.
                    onMouseDown: (e: MouseEvent) => e.stopPropagation(),
                    onClick: (e: MouseEvent) => act(openBell, e),
                    onDoubleClick: (e: MouseEvent) => e.stopPropagation(),
                    onKeyDown: (e: React.KeyboardEvent) => {
                      if (e.key !== "Enter" && e.key !== " ") return;
                      e.preventDefault();
                      e.stopPropagation();
                      openBell();
                    },
                  }
                : {})}
            >
              <span className="sl-lead">{ship.lead}</span>
              {ship.rest && (
                <span className={"sl-rest" + (ship.restCls ? " " + ship.restCls : "")}>{ship.rest}</span>
              )}
            </span>
          )}
          {!editing && !ship && lline && (
            <span className="roll-line">
              <span
                className={"lineage workers lead-line"}
                title={"Its group: " + (leadRun?.name || "") + "\nClick to open the Thread"}
                role="button"
                tabIndex={0}
                onClick={(e) => act(() => openThread(title), e)}
                onDoubleClick={(e) => e.stopPropagation()}
                onKeyDown={(e) => {
                  if (e.key !== "Enter" && e.key !== " ") return;
                  e.preventDefault();
                  e.stopPropagation();
                  openThread(title);
                }}
              >
                <span className={lline.cls || undefined}>{lline.text}</span>
                {lline.url && (
                  <a
                    className="lead-link"
                    href={lline.url}
                    target="_blank"
                    rel="noreferrer"
                    title={lline.url}
                    onClick={(e) => e.stopPropagation()}
                  >
                    {" ↗"}
                  </a>
                )}
              </span>
              {foldBtn}
            </span>
          )}
          {!editing && !ship && !lline && wline && (
            <span className={"lineage " + wline.cls} title={wline.title}>
              {wline.text}
            </span>
          )}
          {!editing && !ship && !lline && !wline && lineage && (
            <span
              className={"lineage" + (lineage.spawned ? " spawned" : "")}
              title={lineage.title}
            >
              {lineage.text}
            </span>
          )}
          {!editing && roll && (
            <span className="roll-line">
              <span
                className="lineage workers"
                title={roll.title}
                // Keyboard-reachable like a button (Tab, then Enter/Space):
                // it opens the family's Thread.
                role="button"
                tabIndex={0}
                aria-label={roll.parts.map((p) => p.text).join(" · ") + " — open the Thread"}
                onClick={(e) => act(() => openThread(title), e)}
                onDoubleClick={(e) => e.stopPropagation()}
                onKeyDown={(e) => {
                  if (e.key !== "Enter" && e.key !== " ") return;
                  e.preventDefault();
                  e.stopPropagation();
                  openThread(title);
                }}
              >
                {roll.parts.map((p, i) => (
                  <span key={i}>
                    {i > 0 && " · "}
                    <span className={p.cls || undefined}>{p.text}</span>
                  </span>
                ))}
              </span>
              {foldBtn}
            </span>
          )}
        </span>
        {lchip ? (
          <button
            type="button"
            className="stagechip wrapchip leadchip"
            title={lchip.title}
            aria-label={lchip.title}
            onClick={(e) => act(() => openThread(title), e)}
            onDoubleClick={(e) => e.stopPropagation()}
          >
            {lchip.label}
          </button>
        ) : pchip?.kind === "wrap" ? (
          <button
            type="button"
            className={"stagechip " + pchip.cls}
            title={pchip.title}
            aria-label={pchip.title}
            onClick={(e) => act(() => void pasteWrapup(title), e)}
            onDoubleClick={(e) => e.stopPropagation()}
          >
            {pchip.label}
          </button>
        ) : pchip ? (
          <span className={"stagechip " + pchip.cls} title={pchip.title}>
            {pchip.label}
          </span>
        ) : (
          <span className={"stagechip " + chip.cls} title={chip.title}>{chip.label}</span>
        )}
        {extra.check && (
          <span className={"stagechip checkchip " + extra.check.cls} title={extra.check.title}>
            {extra.check.label}
          </span>
        )}
        {rz && !pending && (
          <button
            type="button"
            className={"stagechip rzchip " + rz.cls}
            title={rz.title}
            aria-label={rz.title}
            onClick={(e) =>
              act(() => {
                selectSession(title);
                useUi.getState().setLastTab(title, "map");
              }, e)
            }
          >
            {rz.label}
          </button>
        )}
        {!pending && (
          <button
            className={"kill" + (missing ? " cleanup" : "")}
            title={
              missing
                ? "Clean up — workspace is gone; remove this session"
                : "End session — keeps worktree (Ctrl+W / Del; undo with Ctrl+Shift+T)"
            }
            onClick={(e) => act(() => (missing ? cleanupMissing(title) : killSession(title)), e)}
          >
            {missing ? "Clean up" : "✕"}
          </button>
        )}
      </div>
      {/* Everything that hangs under the row. flow-root so the strip's margins
          stay inside, where the family connector is drawn through them. */}
      <div className="inst-tail">
        {(nested || nest.stem) && <NestLines nest={nest} part="tail" />}
        {answering && (
          <AnswerStrip
            ref={strip}
            title={title}
            activity={activity}
            variant="rail"
            onOpen={() => selectSession(title)}
            // "No…": the agent now wants to hear what to do instead — say it in
            // the family's Thread, addressed to this session.
            onRedirect={() => openThread(isWorker ? inst.parent! : title, title)}
          />
        )}
        {expanded && !pending && (
          <div className="inst-actions">
            <div className="folder-row">
              <span className="folder-path" title={folder}>{inst.folder_label || folder || "—"}</span>
              <button
                className="folder-copy"
                title="Copy full folder path"
                onClick={(e) =>
                  act(async () => {
                    if (!folder) return;
                    if (await copyText(folder)) toast("Copied path");
                  }, e)
                }
              >
                Copy path
              </button>
            </div>
            <div className="menu-sep" />
            {missing ? (
              <button className="danger" onClick={() => cleanupMissing(title)}>
                Clean up — remove session
              </button>
            ) : (
              <>
                {caps.git && (
                  <>
                    <button onClick={() => commitSession(title)}>
                      Commit…<span className="kbd">Ctrl+K C</span>
                    </button>
                    <button onClick={() => pushSession(title)}>
                      Push<span className="kbd">Ctrl+K P</span>
                    </button>
                    <button
                      onClick={() => makePrSession(title)}
                      title={prSupport ? undefined : PR_FALLBACK_HINT}
                    >
                      Make PR{prSupport ? "" : " ↗"}
                      <span className="kbd">Ctrl+K R</span>
                    </button>
                    {inst.stage === "pr" && (
                      // Shares mergeSession() with the pill, the palette and the
                      // pane header so the confirm text and the
                      // can't-merge-from-here fallback exist in exactly one place.
                      <button
                        onClick={() => act(() => mergeSession(title))}
                        title={prSupport ? undefined : PR_FALLBACK_HINT}
                      >
                        Merge PR{prSupport ? "" : " ↗"}
                      </button>
                    )}
                    {inst.pr_url && (
                      <button onClick={() => window.open(inst.pr_url!, "_blank")}>Open PR ↗</button>
                    )}
                    <div className="menu-sep" />
                  </>
                )}
                {/* Hand-offs: Fast-track… (the ⏩ picker), Split into parallel
                    pieces…, Move out of a group, Message… — with a separator
                    of its own. */}
                <SessionRowItems inst={inst} />
                {inst.setup?.state === "failed" && (
                  <button
                    onClick={(e) =>
                      act(async () => {
                        try {
                          await instApi(title, "/setup/rerun", { method: "POST" });
                          toast("Worktree setup re-running — watch the setup chip");
                        } catch (err) {
                          toast("Setup re-run failed: " + errMsg(err), { duration: 6000 });
                        }
                        await refreshInstances();
                      }, e)
                    }
                  >
                    Re-run worktree setup
                  </button>
                )}
                {inst.check && inst.check.state !== "running" && (
                  <button
                    onClick={(e) =>
                      act(async () => {
                        try {
                          await instApi(title, "/check", { method: "POST" });
                          toast("Checks running…");
                        } catch (err) {
                          toast("Check run failed: " + errMsg(err), { duration: 6000 });
                        }
                        await refreshInstances();
                      }, e)
                    }
                  >
                    Run checks now
                  </button>
                )}
                <button onClick={() => openDialogFor("rename", title)}>Rename…</button>
                <button onClick={() => copySession(title)}>
                  Duplicate session<span className="kbd">Ctrl+K D</span>
                </button>
                <button onClick={() => ideSession(title)}>
                  Open / focus {ideName}<span className="kbd">Ctrl+K O</span>
                </button>
                <button onClick={() => hideSession(title)}>
                  {hidden ? "Show window" : "Hide window"}
                  {!hidden && <span className="kbd">Ctrl+K H</span>}
                </button>
                {inst.ports?.base ? (
                  <button
                    onClick={(e) =>
                      act(
                        () =>
                          void window.open(
                            `http://${location.hostname}:${inst.ports!.base}/`,
                            "_blank"
                          ),
                        e
                      )
                    }
                  >
                    Open preview ↗<span className="kbd">:{inst.ports.base}</span>
                  </button>
                ) : null}
                <button onClick={() => (paused ? resumeSession(title) : pauseSession(title))}>
                  {paused ? "Resume session" : "Pause session"}
                </button>
                {caps.git &&
                  (wipeArmed ? (
                    <div
                      className="wipe-confirm"
                      role="group"
                      title={`Permanently removes the worktree directory and closes its ${ideName} window. This cannot be undone.`}
                    >
                      <span className="wipe-q">
                        Delete <b>{shown}</b> and its folder?
                      </span>
                      <span className="wipe-acts">
                        <button
                          type="button"
                          className="danger"
                          onClick={() =>
                            act(async () => {
                              setWipeArmed(false);
                              try {
                                await instApi(title, "/cleanup", { method: "POST" });
                              } catch (err) {
                                errorPop("Delete failed", errMsg(err));
                              }
                              useUi.getState().setHidden(title, false);
                              await refreshInstances();
                            })
                          }
                        >
                          Delete + wipe
                        </button>
                        {/* Focus lands on the harmless answer: a second
                            Enter on the item never wipes. */}
                        <button type="button" autoFocus onClick={() => setWipeArmed(false)}>
                          Keep
                        </button>
                      </span>
                    </div>
                  ) : (
                    <button className="danger" onClick={() => setWipeArmed(true)}>
                      Delete + wipe worktree
                    </button>
                  ))}
              </>
            )}
          </div>
        )}
      </div>
    </li>
  );
});
