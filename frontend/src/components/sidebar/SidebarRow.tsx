/** One session row (ports app.js section 9's _createSidebarRow /
 * _updateSidebarRow / _instActionsHtml / _rowAction). */

import {
  memo,
  useCallback,
  useEffect,
  useId,
  useRef,
  useState,
  useSyncExternalStore,
  type MouseEvent,
  type ReactElement,
} from "react";
import { rowDndProps } from "./rowDnd";
import type { NestInfo } from "./ordering";
import type { Instance } from "../../api/types";
import { queryClient, useConfig } from "../../state/queries";
import { useUi } from "../../state/store";
import { chipState, checkChip, effectiveActivity } from "../../lib/stage";
import { agentBlocked, agentBlockedLine, agentBlockedState, deriveSessionState } from "../../lib/sessionState";
import { AgentBlockedChip } from "../setup/AgentBlocked";
import { useSetup } from "../setup/useSetup";
import { reviveSession } from "../../lib/revive";
import { wakeSession } from "../../lib/wake";
import { backgroundChip } from "../../lib/backgroundJobs";
import { stopBackgroundJob } from "../../lib/stopBackgroundJob";
import { sessionLabel } from "../../lib/sessionLabel";
import { useNow } from "../../lib/useNow";
import {
  cleanupMissing,
  folderInUseText,
  ideSession,
  selectSession,
} from "../../lib/sessionActions";
import { toast } from "../../lib/toast";
import { nameTakenBy } from "../../lib/windowName";
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
import { SessionMenu } from "./SessionMenu";
import { RowMenu, type RowMenuOpen } from "./RowMenu";
import { MidName } from "./MidName";
import { TaskLine, taskLineOf } from "./TaskLine";
import { sessionMenu } from "../../lib/sessionMenu";
import { PresenceDots } from "../presence/Presence";
import { useMe } from "../../lib/presence";
import { PEER_SCREEN, peerChip } from "../../lib/peer";
import { actionCtx, sessionAction } from "../../lib/sessionActionTable";
import { openSettings } from "../../lib/openSettings";
import { VerdictChip } from "../dialogs/VerdictChip";

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
  /** Draw the Alt+N number (off in the calm first hour — lib/calm.ts). */
  numbered?: boolean;
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
  numbered = true,
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
  // The session menu, open: where it hangs (under the ⋮, or at the pointer of
  // a right-click) — null while closed.
  const [menuAt, setMenuAt] = useState<RowMenuOpen | null>(null);
  const expanded = !!menuAt;
  const kebab = useRef<HTMLButtonElement | null>(null);
  const menuId = useId();
  // The row's ✕ on a session with uncommitted changes asks first, on a strip
  // under the row (never confirm(): the desktop app has none).
  const [closeArmed, setCloseArmed] = useState(false);
  // The sessions holding this row's folder when a Delete 409ed ("Duplicate
  // session" runs in place). Drawn as a card under the row, outside the menu,
  // so it survives the menu closing; dismissed by its ✕ or by a later success.
  const [inUse, setInUse] = useState<string[] | null>(null);
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
  const setAlias = useUi((s) => s.setAlias);
  const picking = useUi((s) => s.bulkMode);
  const picked = useUi((s) => s.bulkSelected.has(inst.title));

  const title = inst.title;
  const missing = !!inst.workspace_missing;
  const paused = inst.status === "paused";
  // A force-start the server has accepted but not yet turned into a session:
  // it exists only as this row, so nothing on it is actionable yet.
  const pending = !!inst.pending;
  const caps = config?.caps ?? { git: true, tailscale: true, ticketing: true, github: true };
  const ideName = config?.ide_name || "Cursor";
  // Files with uncommitted changes (what the ✕ asks about before it closes).
  const uncommitted = Number(inst.diff_stat?.uncommitted?.files) || 0;
  // ONE session state for the row (lib/sessionState): the dot and the chip
  // below are painted from this same object, so they can never disagree.
  // …unless its agent isn't installed here or isn't signed in: then nothing
  // about it is Idle or Offline — it can't start, and Install / Sign in is
  // the one thing that helps (components/setup/AgentBlocked).
  const { data: setupStatus } = useSetup();
  const noAgent = agentBlocked(inst, setupStatus?.logins);
  const ss = noAgent ? agentBlockedState(noAgent) : deriveSessionState(inst);
  const me = useMe();
  // A one-for-all / split piece that MindFlock merged back into its lead's
  // branch: that is its stage now, whatever its own branch says ("committed").
  const chip0 = chipState(inst, ss);
  const chip =
    runTask?.state === "integrated"
      ? runTask.sameFolder
        ? { ...chip0, label: "committed", cls: "s-committed", title: "MindFlock committed its paths on its lead's branch" }
        : { ...chip0, label: "merged", cls: "s-committed", title: "Merged back into its lead's branch" }
      : chip0;
  const check = checkChip(inst);
  const num = numbered && idx < 9 ? String(idx + 1) : "";
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
  // worded toggle beside the roll-up, never a chevron: a chevron reads as
  // "expand", and the row's actions are its ⋮. The roll-up stays, so a folded family still
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
  // What the session was asked to do, under its name — its task headline, a
  // short verb phrase of the newest meaningful prompt (TaskLine) — when
  // nothing more pressing (a ship line, a family line) has the slot.
  // "untitled · Idle" said nothing about the work.
  const task = taskLineOf(inst).text;
  const taskLine = !noAgent && !ship && !lline && !wline && !lineage && !!task;
  const subline = !editing && (ship || wline || roll || lline || lineage || taskLine || !!noAgent);
  // "working · 6m" counts up by itself: a busy worker's row data can sit
  // unchanged between polls, and memo would freeze the minutes.
  useNow(wline?.state === "working" || ship?.state === "working" ? 30_000 : false);
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
  // A shared-folder (peer-link) session: its chip, and the actions the
  // server refuses for it left out of the menu below.
  const peer = peerChip(inst);
  // A job the agent backgrounded and left behind: waiting (will wake it) or
  // stuck (never will; the chip stops it). lib/backgroundJobs.
  const bg = pending ? null : backgroundChip(inst);

  const act = async (fn: () => void | Promise<void>, e?: MouseEvent) => {
    e?.stopPropagation();
    await fn();
  };

  // The ⋯ menu's verbs come from the session action table — the same rows
  // the palette and the Ctrl+K chords read — grouped by lib/sessionMenu.
  const ctx = actionCtx(title, { inst, caps, ideName, hidden });
  const clearRenameTimer = () => {
    if (renameTimer.current !== null) {
      clearTimeout(renameTimer.current);
      renameTimer.current = null;
    }
  };
  useEffect(() => clearRenameTimer, []);

  /** Hang the menu under the ⋮, right edges aligned (the pane header's ⋯
   * popover does the same). */
  const openAtKebab = (keyboard: boolean) => {
    const b = kebab.current?.getBoundingClientRect();
    if (!b) return;
    clearRenameTimer();
    setMenuAt({ x: b.right, y: b.bottom + 4, flipY: b.top - 4, align: "end", keyboard });
  };
  const closeMenu = (refocus = false) => {
    setMenuAt(null);
    if (refocus) kebab.current?.focus();
  };
  /** Right-click anywhere on the row: the same menu, at the pointer. The
   * rename input and links keep the browser's own menu (copy / paste). */
  const onRowContextMenu = (e: MouseEvent) => {
    if (pending || editing) return;
    const t = e.target as Element;
    if (isEditingTarget(t) || t.closest?.("a[href]")) return;
    e.preventDefault();
    e.stopPropagation();
    clearRenameTimer();
    // The Menu key / Shift+F10 also arrive as a contextmenu event, with no
    // pointer behind them: those hang the menu from the ⋮.
    if (e.button !== 2 && e.clientX === 0 && e.clientY === 0) return openAtKebab(true);
    setMenuAt({ x: e.clientX, y: e.clientY, flipY: e.clientY, align: "start", keyboard: false });
  };

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
    // Blank or unchanged keeps the name. A rename is the session's name
    // everywhere, so a name another session has is refused, not shared.
    if (!next || next === shown) return;
    const taken = nameTakenBy(title, next);
    if (taken) {
      toast(`Another session is already named “${next}”`);
      return;
    }
    // Typed back to what the row shows by itself: no rename left to keep.
    setAlias(title, next === label.text ? "" : next);
    toast(`Renamed to “${next}”`);
  };

  const rowCls =
    "inst" +
    (nested ? " nest-" + nest.depth : "") +
    (nest.stem ? " has-stem" : "") +
    (focused === title ? " active" : "") +
    (hidden ? " is-hidden" : "") +
    (missing ? " ws-missing" : "") +
    (pending ? " is-pending" : "") +
    (picked ? " bulk-picked" : "") +
    (menuAt ? " menu-open" : "") +
    (dropCue ? ` drop-${dropCue}` : "");

  return (
    <>
      <li
        className={rowCls}
        data-title={title}
        // Focusable only while it carries an answer strip, so Tab reaches a
        // blocked worker and 1–9 answer it — never a bare digit typed elsewhere.
        tabIndex={answering ? 0 : undefined}
        onKeyDown={(e) => {
          // Shift+F10 / the Menu key on anything focused in the row (its ⋮, a
          // chip, the answer strip): the row's menu, under the ⋮.
          if (
            !pending &&
            !editing &&
            (e.key === "ContextMenu" || (e.key === "F10" && e.shiftKey && !e.ctrlKey && !e.metaKey && !e.altKey)) &&
            !isEditingTarget(e.target as Element)
          ) {
            e.preventDefault();
            e.stopPropagation();
            openAtKebab(true);
            return;
          }
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
          onMouseDown={(e) => {
            // Shift-click picks a range; don't let it select text on the way.
            if (e.shiftKey) e.preventDefault();
          }}
          onClick={(e) => {
            if (editing || Date.now() - editEndedAt.current < DBLCLICK_MS + 100) return;
            // Picking rows for the bulk bar: Ctrl/⌘ toggles one, Shift takes the
            // range from the last pick, and in select mode a plain click toggles.
            if (!pending && (e.ctrlKey || e.metaKey || e.shiftKey || picking)) {
              clearRenameTimer();
              useUi.getState().pickRow(title, e.shiftKey);
              return;
            }
            if (focused !== title) {
              selectSession(title);
              return;
            }
            if (!pending) armRename();
          }}
          onDoubleClick={() => {
            clearRenameTimer();
            if (picking) return;
            if (!missing && !pending) ideSession(title, true);
          }}
          onContextMenu={onRowContextMenu}
        >
          {(nested || nest.stem) && <NestLines nest={nest} part="row" />}
          <span className="grip" title="Drag to reorder">⠿</span>
          <span className="idx" title={num ? `Ctrl+${num} / Alt+${num} to focus` : ""}>
            {picking ? (picked ? "✓" : "") : num}
          </span>
          {/* The chip already says the state: the dot draws only where it says
              something the chip doesn't — a dropped terminal socket, or a node
              of a family tree (whose connector lines meet it). */}
          <span
            className={
              "dot tone-" +
              ss.tone +
              " st-" +
              ss.state +
              (disconnected ? " disconnected" : "") +
              (disconnected || nested || nest.stem || lchip || pchip ? "" : " dot-quiet")
            }
            title={disconnected ? "Terminal socket lost — reconnecting" : ss.label}
          />
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
                  shown,
                  // A ticket/PR label's slug, until the session is renamed: then
                  // the new name is the only name it goes by.
                  label.kind && !alias ? `session: ${displayTitle(inst)}` : "",
                  inst.branch ? `branch: ${inst.branch}` : "",
                  wline ? wline.title : lineage ? lineage.title : "",
                  focused === title && !picking ? "Click again to rename" : "",
                  picking ? "" : "Ctrl+click or Shift+click to select several",
                ]
                  .filter(Boolean)
                  .join("\n")}
              >
                {/* Truncated at the END (round 5, m13: chips squeezed
                    "fix-typo-readme" to "fi…eadme"); middle-truncated only
                    when a "-2" / "(3)" suffix is what tells two rows apart. */}
                {/(-\d+|\(\d+\))$/.test(shown) ? <MidName text={shown} /> : shown}
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
                  title={"Its team run: " + (leadRun?.name || "") + "\nClick to open the Thread"}
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
            {!editing && noAgent && (
              <span className="lineage noagent-line" title={ss.hint}>
                {agentBlockedLine(noAgent)}
              </span>
            )}
            {!editing && taskLine && <TaskLine inst={inst} />}
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
          ) : noAgent ? (
            <AgentBlockedChip block={noAgent} title={title} className={"stagechip chip-restart " + chip.cls} hint={ss.hint} />
          ) : ss.state === "offline" && ss.restartable && !paused ? (
            // Offline carries its remedy: Restart relaunches the agent (the same
            // lazy revive a terminal attach or a sent prompt does).
            <button
              type="button"
              className={"stagechip chip-restart " + chip.cls}
              title={chip.title}
              aria-label={"Offline — restart the agent"}
              onClick={(e) => act(() => void reviveSession(title), e)}
              onDoubleClick={(e) => e.stopPropagation()}
            >
              {chip.label}
              <span className="chip-act" aria-hidden="true">
                ↻ Restart
              </span>
            </button>
          ) : inst.sleeping && !inst.device && !paused ? (
            // Asleep (backend core/sleep.py): a calm chip whose remedy shows on
            // hover. Clicking the row wakes it too (opening its terminal does).
            <button
              type="button"
              className={"stagechip chip-restart chip-wake " + chip.cls}
              title={chip.title}
              aria-label={"Sleeping — wake the agent"}
              onClick={(e) => act(() => void wakeSession(title), e)}
              onDoubleClick={(e) => e.stopPropagation()}
            >
              {chip.label}
              <span className="chip-act" aria-hidden="true">
                Wake
              </span>
            </button>
          ) : (
            <span className={"stagechip " + chip.cls} title={chip.title}>{chip.label}</span>
          )}
          {/* Who else is on this session (lib/presence): drawn only once
              someone besides you watches or drives it. */}
          {!pending && <PresenceDots inst={inst} me={me} />}
          {inst.shared_branch && (
            <span className="shared-branch-mark" title={"Shared branch " + (inst.branch || "") + " — others push to it too"} aria-label="shared branch">⑂</span>
          )}
          {extra.check && (
            <span className={"stagechip checkchip " + extra.check.cls} title={extra.check.title}>
              {extra.check.label}
            </span>
          )}
          {/* Verify's verdict for this branch; opens its checklist. */}
          {!pending && (
            <span className="vchip-hook" data-tour="verify-chip">
              <VerdictChip inst={inst} where="row" />
            </span>
          )}
          {bg &&
            (bg.stopPid ? (
              <button
                type="button"
                className={"stagechip " + bg.cls}
                title={bg.title}
                aria-label={bg.title}
                onClick={(e) => act(() => void stopBackgroundJob(title, bg.stopPid as number), e)}
                onDoubleClick={(e) => e.stopPropagation()}
              >
                {bg.label}
              </button>
            ) : (
              <span className={"stagechip " + bg.cls} title={bg.title}>
                {bg.label}
              </span>
            ))}
          {peer && (
            // Rank 2 of the peer-link UX scan: a shared-folder session says so
            // HERE, on the rail row (a pane-head chip renders 0x0 — see
            // grid/DiffTab.css). Click: People, where its link is.
            <button
              type="button"
              className={"stagechip peerchip" + (peer.connected === false ? " peer-offline" : "")}
              title={peer.title}
              aria-label={peer.title}
              onClick={(e) => act(() => openSettings(PEER_SCREEN), e)}
              onDoubleClick={(e) => e.stopPropagation()}
            >
              {peer.connected !== null && <span className="peerchip-dot" aria-hidden="true" />}
              {peer.label}
            </button>
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
            // The session's actions: a ⋮ at the row's right end, beside the ✕,
            // drawn on hover / keyboard focus / the selected row (SidebarRow.css)
            // and always holding its slot, so nothing reflows when it appears.
            // A right-click anywhere on the row opens the same menu.
            <button
              ref={kebab}
              type="button"
              className={"row-kebab" + (expanded ? " open" : "")}
              title="Actions for this session (or right-click the row)"
              aria-label="Actions for this session"
              aria-haspopup="menu"
              aria-expanded={expanded}
              aria-controls={expanded ? menuId : undefined}
              // A keyboard press (Enter / Space) has no click count: focus the
              // first item then, the way a menu button does.
              onClick={(e) => act(() => (expanded ? closeMenu() : openAtKebab(e.detail === 0)), e)}
              onDoubleClick={(e) => e.stopPropagation()}
            >
              <KebabIcon />
            </button>
          )}
          {!pending && (
            <button
              className={"kill" + (missing ? " cleanup" : "")}
              aria-label={missing ? "Delete session" : "Close session"}
              title={
                missing
                  ? "Delete — its workspace is gone; this removes the session"
                  : "Close session — stops the agent and moves it to Recently closed (Ctrl+W / Del; reopen with Ctrl+Shift+T)"
              }
              onClick={(e) =>
                act(() => {
                  if (missing) return cleanupMissing(title);
                  // Uncommitted work: say so before the agent stops.
                  if (uncommitted > 0 && !closeArmed) return setCloseArmed(true);
                  setCloseArmed(false);
                  sessionAction("close").run(title);
                }, e)
              }
            >
              {missing ? "Delete" : "✕"}
            </button>
          )}
        </div>
        {/* Everything that hangs under the row. flow-root so the strip's margins
            stay inside, where the family connector is drawn through them. */}
        <div className="inst-tail">
          {(nested || nest.stem) && <NestLines nest={nest} part="tail" />}
          {closeArmed && !missing && (
            <div className="close-confirm" role="group" aria-label={`Close ${shown}?`}>
              <span className="del-q">
                Close session? Its {uncommitted} uncommitted change{uncommitted === 1 ? "" : "s"} stay in its
                folder — reopen it from Recently closed.
              </span>
              <span className="del-acts">
                <button
                  type="button"
                  className="danger"
                  onClick={(e) =>
                    act(() => {
                      setCloseArmed(false);
                      sessionAction("close").run(title);
                    }, e)
                  }
                >
                  Close session
                </button>
                <button type="button" autoFocus onClick={(e) => act(() => setCloseArmed(false), e)}>
                  Cancel
                </button>
              </span>
            </div>
          )}
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
          {inUse && (
            <div className="folder-in-use" role="status">
              <span className="fiu-text">{folderInUseText(inUse)}</span>
              <button
                type="button"
                className="fiu-x"
                aria-label="Dismiss"
                title="Dismiss"
                onClick={(e) => act(() => setInUse(null), e)}
              >
                ✕
              </button>
            </div>
          )}
        </div>
      </li>
      {menuAt && !pending && (
        // THE per-session menu (lib/sessionMenu) — the pane header's ⋯ draws
        // the same model. A popover over the whole window (RowMenu), outside the
        // <li> even in React's tree so its keys and clicks never reach the row.
        <RowMenu
          id={menuId}
          at={menuAt}
          label={"Actions for " + shown}
          opener={kebab.current}
          onClose={closeMenu}
        >
          <SessionMenu
            model={sessionMenu(inst, ctx, {
              onSelect: () => {
                useUi.getState().pickRow(title, false);
                closeMenu();
              },
              picked,
            })}
            onInUse={setInUse}
            onDone={() => closeMenu()}
          />
        </RowMenu>
      )}
    </>
  );
});

/** ⋮ — three dots on the 24-unit grid the app's other line icons use, filled
 * at the weight of their 2-unit strokes. */
function KebabIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
      <circle cx="12" cy="5" r="2" />
      <circle cx="12" cy="12" r="2" />
      <circle cx="12" cy="19" r="2" />
    </svg>
  );
}
