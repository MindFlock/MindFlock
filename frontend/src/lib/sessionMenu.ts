/** THE per-session menu: one model, drawn by the sidebar row's ⋯ (an inline
 * accordion) and the pane header's ⋯ (a popover) alike, so a session has one
 * set of actions wherever you open it. The verbs themselves (label, keys,
 * enable rule) still come from the session action table; this module only
 * decides the GROUPS and the ORDER:
 *
 *   Work     Message…, Queue prompt…
 *   Ship     Commit… (only once there is something to commit), Push, Open PR,
 *            Merge PR, View PR, Fast-track…, Verify checklist…, Run checks now
 *   Session  Rename…, Resume (only on a paused session: there is no Pause
 *            here — the palette, `mindflock pause` and the API still pause),
 *            Workers on every prompt (a ✓ switch: Flock mode)
 *   More ▸   the rare ones, folded: Terminal, Diff, Code map, Thread, Browse
 *            history, Copy entire history (pane only), Split into parallel
 *            pieces…, Duplicate, Open in the IDE,
 *            Open preview, Select, Hide window, Move out of a group, Re-run
 *            worktree setup, Copy folder path
 *   Danger   Close, Delete…
 *
 * A group with nothing in it is left out. A pane passes what its own header
 * already shows (a visible tab is not repeated) and what only a pane has
 * (Browse history, Copy entire history), and — when its header leaves the
 * guided button out (Customize → Pane header) — that button's step, first
 * under Ship unless Ship already offers it by name, so whatever the header
 * hides is still here. */

import type { Instance } from "../api/types";
import { instApi } from "../api/client";
import { refreshInstances } from "../state/queries";
import { displayName, useUi } from "../state/store";
import { PR_FALLBACK_HINT, cleanupMissing, deleteConfirmText, deleteSession, selectSession } from "./sessionActions";
import { api } from "../api/client";
import { CONNECT_GITHUB_FOR_PRS, NOTHING_TO_PUSH } from "./vocab";
import { actionLabel, actionNote, sessionAction, type ActionCtx, type SessionActionId } from "./sessionActionTable";
import { LANE_SHORT, detachableGroup, laneChoice, moveOutOfGroup, splitBlockReason, splitSession } from "./laneActions";
import { fastTrackStep } from "./stage";
import { isRemote } from "./playbooks";
import { openChecklist } from "./checklistLink";
import { openSettings } from "./openSettings";
import { PEER_SCREEN } from "./peer";
import { copyText } from "./clipboard";
import { flockTooltip } from "./workersRequest";
import { errorPop } from "./errorPop";
import { errMsg } from "./format";
import { toast } from "./toast";

export interface MenuItem {
  /** Stable id (data-menu): tests and the pane/row tell items apart by it. */
  key: string;
  label: string;
  keys?: string;
  /** A muted line under the label (where the session goes, what it opens). */
  note?: string;
  title?: string;
  /** Ends in the browser: drawn with ↗. */
  external?: boolean;
  /** A switch's state (Workers on every prompt): the label carries the ✓. */
  checked?: boolean;
  /** Drawn, greyed, and says why in its title. */
  disabled?: boolean;
  danger?: boolean;
  /** Asks on an inline strip first; the text is the question's one line. */
  confirm?: string;
  run(): void | Promise<unknown>;
}

export type MenuGroupId = "work" | "ship" | "session" | "danger";

export interface MenuGroup {
  id: MenuGroupId;
  /** The small heading drawn over the group ("" = none: Danger is its own). */
  label: string;
  items: MenuItem[];
}

export interface SessionMenuModel {
  groups: MenuGroup[];
  /** The folded "More" items (empty = no More fold). */
  more: MenuItem[];
}

export interface SessionMenuOpts {
  /** Tabs the pane header already shows: their openers are left out. */
  visibleTabs?: string[];
  /** Pane-only: open the history view. */
  onHistory?: () => void;
  /** Pane-only: copy the active terminal tab's whole history (absent on a
   * tab with no terminal). */
  onCopyHistory?: () => void;
  /** Pane-only: the guided next step its header doesn't show (Commit…,
   * Push, Open PR, Re-commit…), so it is one click away here instead. */
  onNextStep?: { label: string; title?: string; disabled?: boolean; run(): unknown };
  /** Row-only: pick the row for the bulk bar. */
  onSelect?: () => void;
  picked?: boolean;
}

/** Uncommitted work in the session's folder (files counts too: an empty or
 * binary new file changes no lines). Unknown reads as none. */
export function hasUncommitted(inst: Partial<Instance> | undefined): boolean {
  const u = inst?.diff_stat?.uncommitted;
  return (u?.files || 0) + (u?.additions || 0) + (u?.deletions || 0) > 0;
}

/** Nothing to ship: the server's diff-stat is in and reads zero, committed
 * or not. Unknown (no stat yet, an older server) is NOT empty — a greyed Push
 * on a session that does have work would be the worse mistake. */
export function nothingToShip(inst: Partial<Instance> | undefined): boolean {
  const d = inst?.diff_stat;
  if (!d) return false;
  return !hasUncommitted(inst) && (d.files || 0) + (d.additions || 0) + (d.deletions || 0) === 0;
}

/** Whether the configured editor ("Open in Cursor") is installed here:
 * true / false once GET /api/ides has answered, null before (or when it
 * can't say — a custom command), which shows the item as it always was. */
let ideKnown: boolean | null = null;
let ideAsked = false;
function ideInstalled(): boolean | null {
  if (!ideAsked) {
    ideAsked = true;
    void api<{ ides?: Array<{ command: string; installed?: boolean }>; current?: string }>("/api/ides")
      .then((d) => {
        const cur = (d?.ides || []).find((i) => i.command === d?.current);
        ideKnown = cur && typeof cur.installed === "boolean" ? cur.installed : null;
      })
      .catch(() => {});
  }
  return ideKnown;
}

/** Show a tab of the session's pane from outside it (lastTab is what the pane
 * follows). */
function openTab(title: string, tab: string) {
  selectSession(title, { noKeyboard: true });
  useUi.getState().setLastTab(title, tab);
}

/** Run a server call, toast what it said (or the error) and refresh. */
function call(name: string, fn: () => Promise<string>) {
  return fn()
    .then((said) => {
      if (said) toast(said, { duration: 5000 });
      refreshInstances();
    })
    .catch((err) => toast(`${name}: ${errMsg(err)}`, { duration: 6000 }));
}

export function sessionMenu(inst: Instance, ctx: ActionCtx, opts: SessionMenuOpts = {}): SessionMenuModel {
  const title = inst.title;
  const name = displayName(title);
  const peer = !!inst.peer_share;
  const remote = isRemote(inst);
  const missing = !!inst.workspace_missing;
  const tabs = new Set(opts.visibleTabs || []);
  const fromTable = (id: SessionActionId, extra: Partial<MenuItem> = {}): MenuItem | null => {
    const a = sessionAction(id);
    if (!a.enabled(ctx)) {
      // Shown greyed, saying why, when the table has a reason worth saying.
      const why = a.why?.(ctx);
      if (!why) return null;
      return { key: id, label: actionLabel(id, ctx), keys: a.keys, disabled: true, title: why, run: () => {}, ...extra, note: why };
    }
    const ext = !!a.external?.(ctx);
    return {
      key: id,
      label: actionLabel(id, ctx),
      keys: a.keys,
      external: ext,
      title: ext ? PR_FALLBACK_HINT : undefined,
      run: () => a.run(title),
      ...extra,
    };
  };
  const keep = (xs: Array<MenuItem | null | false | undefined>) => xs.filter(Boolean) as MenuItem[];

  // A session whose folder is gone has one thing left to do.
  if (missing)
    return {
      groups: [
        {
          id: "danger",
          label: "",
          items: [
            {
              key: "delete",
              label: "Delete — remove session",
              danger: true,
              run: () => cleanupMissing(title),
            },
          ],
        },
      ],
      more: [],
    };

  const work = keep([
    fromTable("message"),
    fromTable("queue", { label: "Queue prompt…", note: actionNote("queue", ctx) }),
  ]);

  const ft = fastTrackStep(inst);
  // Shipping with nothing to ship, or a PR with no GitHub to open it on: the
  // item stays where it is, greyed, saying why (the Ctrl+K chords and the
  // pane's guided button still answer — nothing is taken away).
  const empty = nothingToShip(inst);
  const greyed = (item: MenuItem | null, why: string): MenuItem | null =>
    item && why && !item.disabled ? { ...item, disabled: true, external: false, title: why, note: why, run: () => {} } : item;
  const noGithub = ctx.prSupport === false ? CONNECT_GITHUB_FOR_PRS : "";
  const ship = keep([
    // Nothing to commit = no Commit… (the Ctrl+K C chord still answers).
    hasUncommitted(inst) && fromTable("commit"),
    greyed(fromTable("push"), empty ? NOTHING_TO_PUSH : ""),
    greyed(fromTable("pr"), empty ? NOTHING_TO_PUSH : noGithub),
    greyed(fromTable("merge"), noGithub),
    fromTable("viewPr"),
    ft &&
      !peer && {
        key: "fast-track",
        label: "Fast-track…",
        note: LANE_SHORT[laneChoice(inst).lane],
        keys: sessionAction("fastTrack").keys,
        title: ft.title,
        run: () => sessionAction("fastTrack").run(title),
      },
    !!inst.branch &&
      !peer && {
        key: "checklist",
        label: "Verify checklist…",
        title: "Verify's checklist for " + inst.branch + " — what to check before or after it merges",
        run: () => openChecklist(inst.branch),
      },
    inst.check &&
      inst.check.state !== "running" &&
      !peer && {
        key: "run-checks",
        label: "Run checks now",
        run: () =>
          instApi(title, "/check", { method: "POST" })
            .then(() => toast("Checks running…"))
            .catch((err) => errorPop("Check run failed", errMsg(err)))
            .finally(() => refreshInstances()),
      },
    peer && {
      key: "bring-home",
      label: "Bring work home…",
      title: "Checkpoint the shared folder and bring it into your own repo as a peer/… branch",
      run: () => openSettings(PEER_SCREEN),
    },
  ]);
  const next = opts.onNextStep;
  const shipGroup =
    next && !ship.some((i) => i.label === next.label)
      ? [
          {
            key: "next-step",
            label: next.label,
            note: "next step",
            title: next.title,
            disabled: !!next.disabled,
            run: () => void next.run(),
          },
          ...ship,
        ]
      : ship;

  // No Pause here (the owner found the button pointless; the palette, the
  // CLI and the API still pause). A session that IS paused — from those, or
  // by an older build — must still come back from the menu: Resume.
  // Workers on every prompt (Flock mode) is a switch: the ✓ says it is on,
  // the note what it does. Same rows as Resume — this machine's own, settled.
  const flockOn = !!inst.flock;
  const session = keep([
    fromTable("rename"),
    fromTable("resume", { note: actionNote("resume", ctx) }),
    fromTable("flock", {
      label: (flockOn ? "✓ " : "") + "Workers on every prompt",
      checked: flockOn,
      note: flockOn ? "on — the workers request is added to everything you send" : actionNote("flock", ctx),
      title: flockTooltip(flockOn),
    }),
  ]);

  const splitWhy = remote ? "" : splitBlockReason(ctx.caps, inst);
  const group = remote ? null : detachableGroup(inst);
  const folder = inst.folder || inst.path || "";
  const more = keep([
    !peer && !tabs.has("shell") && { key: "terminal", label: "Terminal", title: "A shell in this session's folder", run: () => openTab(title, "shell") },
    ctx.caps.git !== false &&
      !tabs.has("diff") && { key: "diff", label: "Diff", title: "What this session changed", run: () => useUi.getState().openDiff(title) },
    ctx.caps.git !== false && !tabs.has("map") && fromTable("map"),
    fromTable("thread", { note: actionNote("thread", ctx) }),
    opts.onHistory && {
      key: "history",
      label: "Browse history",
      title: "Scroll and select the full history like a page",
      run: opts.onHistory,
    },
    opts.onCopyHistory && {
      key: "copy-history",
      label: "Copy entire history",
      title: "Copy the open terminal tab's whole history to the clipboard",
      run: opts.onCopyHistory,
    },
    !remote &&
      !peer &&
      !inst.pending && {
        key: "split",
        label: "Split into parallel pieces…",
        disabled: !!splitWhy,
        title:
          splitWhy ||
          "The agent proposes pieces with separate paths; you approve them and pick where they run — separate worktrees merged back, or this folder",
        run: () => call(name, () => splitSession(inst, name)),
      },
    group && {
      key: "detach",
      label: "Move out of " + group.name,
      title: "The team run stops driving it; the session and its fast-track stay",
      run: () =>
        call(name, async () => {
          await moveOutOfGroup(group.id, group.task);
          return `${name} is out of ${group.name} — its session and fast-track stay as they are`;
        }),
    },
    fromTable("duplicate"),
    fromTable("branch", { note: actionNote("branch", ctx) }),
    // Only when the editor is there (or nobody can tell): an "Open in
    // Cursor" for an editor this machine doesn't have is a dead end.
    (remote || ideInstalled() !== false) && fromTable("ide", { note: actionNote("ide", ctx) }),
    !!inst.ports?.base && {
      key: "preview",
      label: "Open preview",
      external: true,
      keys: ":" + inst.ports!.base,
      title: "Opens what this session serves on port " + inst.ports!.base + " (its dev server) in a new tab",
      run: () => void window.open(`http://${location.hostname}:${inst.ports!.base}/`, "_blank"),
    },
    opts.onSelect && {
      key: "select",
      label: opts.picked ? "Unselect" : "Select",
      keys: "Ctrl+click",
      title: "Pick several sessions to act on them together",
      run: opts.onSelect,
    },
    // Hidden, the session can't be focused, so its chord can't reach it: the
    // shortcut is drawn only for Hide.
    fromTable("hide", ctx.hidden ? { keys: undefined } : {}),
    inst.setup?.state === "failed" &&
      !peer && {
        key: "rerun-setup",
        label: "Re-run worktree setup",
        run: () =>
          instApi(title, "/setup/rerun", { method: "POST" })
            .then(() => toast("Worktree setup re-running — watch the setup chip"))
            .catch((err) => errorPop("Setup re-run failed", errMsg(err)))
            .finally(() => refreshInstances()),
      },
    !!folder && {
      key: "copy-path",
      label: "Copy folder path",
      title: folder,
      run: () =>
        copyText(folder).then((ok) => {
          if (ok) toast("Copied path");
        }),
    },
  ]);

  const danger = keep([
    {
      key: "close",
      label: "✕ " + sessionAction("close").short,
      keys: sessionAction("close").keys,
      note: actionNote("close", ctx),
      title: "Stops the agent and moves the session to Recently closed; reopen it from there (Ctrl+Shift+T)",
      run: () => sessionAction("close").run(title),
    },
    sessionAction("delete").enabled(ctx) && {
      key: "delete",
      label: sessionAction("delete").short!,
      danger: true,
      confirm: deleteConfirmText(inst),
      title: "Deletes its folder and branch, uncommitted changes included (asks first)",
      // Already asked (the strip): the answer may name the sessions holding
      // its folder, which the menu shows under the row.
      run: () => deleteSession(title, true),
    },
  ]);

  const groups: MenuGroup[] = [
    { id: "work" as const, label: "Work", items: work },
    { id: "ship" as const, label: "Ship", items: shipGroup },
    { id: "session" as const, label: "Session", items: session },
    { id: "danger" as const, label: "", items: danger },
  ].filter((g) => g.items.length);
  return { groups, more };
}
