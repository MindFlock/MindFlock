/** ONE table of the per-session actions. The sidebar row's › menu, the
 * command palette and the Ctrl+K chords (lib/keymap CHORDS) all read it, so
 * an action's label, shortcut and enable rule live here once; each surface
 * keeps only its own ORDER (a list of ids) and its own layout.
 *
 *   label    what the item says (the palette appends " — <session>")
 *   short    a tighter word for the row menu's end group ("Close", "Delete…")
 *   keys     the shortcut drawn beside it ("Ctrl+K C")
 *   chord    the Ctrl+K second key, when there is one (the user can rebind it;
 *            the letter is also the chord's stable id)
 *   note     one line on what it does: the palette hint after the keys, and
 *            the row menu's sub-line
 *   external ends in the browser (GitHub's own page) — the row adds ↗
 *   enabled  whether the action applies to this session right now. The palette
 *            and the row menu leave out an action that doesn't; a chord
 *            refuses with a toast. (Pause is the palette's only: the menus
 *            offer just Resume, on a paused session.)
 *   why      for a disabled action whose reason is worth SAYING rather than
 *            hiding the entry (Branch on a folder outside git): the row menu
 *            draws it greyed with this as its note, the palette lists it with
 *            this as its hint, and the chord raises it as an error card.
 *
 * Every rule reads the same context (`actionCtx`): the session row, the
 * server caps, whether it is a shared (peer, sandboxed) folder — where the
 * server refuses commit/push/PR/rename/copies/IDE, so they aren't offered. */

import type { Caps, Config, Instance } from "../api/types";
import { queryClient } from "../state/queries";
import { displayName, useUi } from "../state/store";
import {
  branchSession,
  closeSession,
  commitSession,
  copySession,
  deleteSession,
  hasPrSupport,
  hideSession,
  ideSession,
  instances,
  makePrSession,
  mergeSession,
  pauseSession,
  pushSession,
  resumeSession,
} from "./sessionActions";
import { LANE_LABEL, laneChoice, openFastTrackMenu, splitBlockReason, splitSession } from "./laneActions";
import { fastTrackStep } from "./stage";
import { focusQueueInput, isRemote } from "./playbooks";
import { errMsg } from "./format";
import { toast } from "./toast";
import { errorPop } from "./errorPop";
import {
  ASK_WORKERS_KEYS,
  FLOCK_KEYS,
  flockActionLabel,
  flockAvailable,
  insertWorkersRequest,
  toggleFlock,
} from "./workersRequest";

export interface ActionCtx {
  title: string;
  inst: Instance | undefined;
  caps: Caps;
  /** A shared (peer) folder: runs sandboxed, the server refuses most verbs. */
  shared: boolean;
  /** gh or a token can file/merge PRs; false ends in GitHub's own page. */
  prSupport: boolean;
  ideName: string;
  /** The pane is hidden (client-side). */
  hidden: boolean;
}

const DEFAULT_CAPS: Caps = { git: true, tailscale: true, ticketing: true, github: true };

/** The context every rule reads. `inst`/`caps`/`ideName` default to the live
 * caches; the row passes what it already rendered from. */
export function actionCtx(
  title: string,
  opts: { inst?: Instance; caps?: Caps; ideName?: string; hidden?: boolean } = {}
): ActionCtx {
  const config = queryClient.getQueryData<Config>(["config"]);
  const inst = opts.inst ?? instances().find((r) => r.title === title);
  const caps = opts.caps ?? config?.caps ?? DEFAULT_CAPS;
  return {
    title,
    inst,
    caps,
    shared: !!inst?.peer_share,
    prSupport: hasPrSupport(caps),
    ideName: opts.ideName ?? (config?.ide_name || "Cursor"),
    hidden: opts.hidden ?? useUi.getState().hidden.has(title),
  };
}

export interface SessionAction {
  label: string | ((c: ActionCtx) => string);
  short?: string;
  keys?: string;
  chord?: string;
  note?: string | ((c: ActionCtx) => string);
  /** Ends in the browser (no gh/token): the row marks it ↗. */
  external?(c: ActionCtx): boolean;
  enabled(c: ActionCtx): boolean;
  /** Why a disabled action doesn't apply, when that is worth saying ("" =
   * nothing to say — the entry is simply left out). Only asked of an action
   * whose `enabled` is false. */
  why?(c: ActionCtx): string;
  run(title: string): void;
}

const cfg = () => queryClient.getQueryData<Config>(["config"]);
const git = (c: ActionCtx) => c.caps.git !== false;
const gitHere = (c: ActionCtx) => git(c) && !c.shared;
const settled = (c: ActionCtx) => !!c.inst && !c.inst.pending;
// PR entries are never withheld: without gh/a token they still work, they
// just end in the browser — the note says so instead of the label.
const prNote = (c: ActionCtx) => (c.prSupport ? "" : "opens GitHub");
// Branch's say-why cases: the session is otherwise branchable, but its folder
// can't be. (A shared folder, another device's row or a session still starting
// just leaves the entry out.)
const branchWhy = (c: ActionCtx): string => {
  if (!c.inst || c.shared || isRemote(c.inst)) return "";
  if (c.caps.git === false) return "git is not installed — install git to branch sessions";
  if (c.inst.git_repo === false)
    return `Branch needs a git repository — ${c.inst.folder || c.inst.path || "this folder"} isn’t one`;
  return "";
};

export const SESSION_ACTIONS = {
  commit: { label: "Commit…", keys: "Ctrl+K C", chord: "c", enabled: gitHere, run: (t) => commitSession(t) },
  // Plain `git push` over the user's own remote — SSH or HTTPS, whatever they
  // configured. Never gated on the GitHub CLI.
  push: { label: "Push", keys: "Ctrl+K P", chord: "p", enabled: gitHere, run: (t) => pushSession(t) },
  // Stays enabled whether or not gh/a token is present: makePrSession degrades
  // to GitHub's prefilled compare page, so it never dead-ends.
  pr: {
    label: "Open PR",
    keys: "Ctrl+K R",
    chord: "r",
    note: prNote,
    external: (c) => !c.prSupport,
    enabled: gitHere,
    run: (t) => makePrSession(t),
  },
  // Deliberately unbound (the most consequential action) — and offered only
  // once there IS a PR. mergeSession() asks first and owns the
  // can't-merge-from-here fallback.
  merge: {
    label: "Merge PR",
    note: prNote,
    external: (c) => !c.prSupport,
    enabled: (c) => gitHere(c) && c.inst?.stage === "pr",
    run: (t) => void mergeSession(t),
  },
  viewPr: {
    label: "View PR ↗",
    enabled: (c) => gitHere(c) && !!c.inst?.pr_url,
    run: (t) => {
      const url = instances().find((r) => r.title === t)?.pr_url;
      if (url) window.open(url, "_blank");
    },
  },
  rename: {
    label: "Rename…",
    enabled: (c) => !c.shared,
    run: (t) => useUi.getState().openDialogFor("rename", t),
  },
  // Opens the Thread composer addressed to the session — what you type there
  // goes in as YOU, never through the agents' mailbox.
  message: {
    label: "Message…",
    keys: "Ctrl+K S",
    chord: "s",
    enabled: () => true,
    run: (t) => useUi.getState().threadOpen(t, { composeTo: t }),
  },
  queue: { label: "Queue prompt…", note: "runs when idle", enabled: () => true, run: (t) => focusQueueInput(t) },
  // Opens the pane's ⏩ picker — the ONE fast-track control, never a second
  // copy of its choices; each item acts at once, nothing is pasted.
  fastTrack: {
    label: "Fast-track…",
    keys: "Ctrl+K F",
    chord: "f",
    note: (c) => (c.inst ? LANE_LABEL[laneChoice(c.inst).lane] : ""),
    enabled: (c) => settled(c) && !c.shared && !!fastTrackStep(c.inst!),
    run: (t) => openFastTrackMenu(t),
  },
  // A split acts at once through the server. Local rows only: another
  // device's group and split routes aren't forwarded.
  split: {
    label: "Split into parallel pieces…",
    note: "starts a split",
    enabled: (c) =>
      settled(c) && !c.shared && !c.inst!.device && !c.title.includes("::") && !splitBlockReason(c.caps, c.inst!),
    run: (t) => {
      const inst = instances().find((r) => r.title === t);
      if (!inst) return;
      const name = displayName(t);
      splitSession(inst, name)
        .then((said) => toast(said, { duration: 5000 }))
        .catch((err) => toast(`${name}: ${errMsg(err)}`, { duration: 6000 }));
    },
  },
  // MindFlock workers without typing the ask (lib/workersRequest): once, typed
  // into the terminal for you to finish; or Flock mode, where the server adds
  // it to every prompt. This machine's own settled sessions only, never a
  // guest's — the same rows Pause/Resume act on.
  askWorkers: {
    label: "Ask for workers",
    keys: ASK_WORKERS_KEYS,
    note: "types the workers request into its terminal",
    enabled: (c) => flockAvailable(c.inst, cfg()) && c.inst!.status !== "paused",
    run: (t) => void insertWorkersRequest(t),
  },
  flock: {
    label: (c) => flockActionLabel(!!c.inst?.flock),
    keys: FLOCK_KEYS,
    note: "adds the workers request to everything you send here",
    enabled: (c) => flockAvailable(c.inst, cfg()),
    run: (t) => void toggleFlock(t),
  },
  thread: {
    label: "Thread",
    keys: "Ctrl+K T",
    chord: "t",
    note: "workers and messages",
    enabled: () => true,
    run: (t) => useUi.getState().threadOpen(t),
  },
  ide: {
    // "Cursor" alone read as the mouse cursor: the note says it's an editor.
    label: (c) => "Open in " + c.ideName,
    note: "your code editor",
    keys: "Ctrl+K O",
    chord: "o",
    enabled: (c) => !c.shared,
    run: (t) => void ideSession(t),
  },
  duplicate: {
    label: "Duplicate session",
    keys: "Ctrl+K D",
    chord: "d",
    enabled: (c) => !c.shared,
    run: (t) => void copySession(t),
  },
  // Duplicate's sibling for work that must not collide: its OWN worktree, on a
  // new branch cut from this one's HEAD, with a copy of its uncommitted
  // changes. Git only — the server refuses anything else, and so does `why`.
  branch: {
    label: "Branch session",
    keys: "Ctrl+K B",
    chord: "b",
    note: (c) => branchWhy(c) || "new worktree on a new branch from this one",
    enabled: (c) =>
      settled(c) && c.inst!.status !== "loading" && !c.shared && !isRemote(c.inst!) && !branchWhy(c),
    why: branchWhy,
    run: (t) => void branchSession(t),
  },
  hide: {
    label: (c) => (c.hidden ? "Show window" : "Hide window"),
    keys: "Ctrl+K H",
    chord: "h",
    enabled: () => true,
    run: (t) => hideSession(t),
  },
  // The Map tab: red zones, the plan, the blast radius. A plain open (no
  // file, no section) through store.openMap, which every "Open map" shares.
  map: {
    label: "Code map",
    keys: "Ctrl+K M",
    chord: "m",
    enabled: git,
    run: (t) => useUi.getState().openMap(t),
  },
  // Pause and Close both stop the agent; WHERE the session goes is the
  // difference, so that is what each one says. A shared folder can't be
  // paused (its sandbox would be torn down), only resumed.
  pause: {
    label: "Pause session",
    note: "stops the agent; keeps it in the sidebar to resume",
    enabled: (c) => settled(c) && c.inst!.status !== "paused" && !c.shared,
    run: (t) => void pauseSession(t),
  },
  resume: {
    label: "Resume session",
    note: "starts the agent again in the same worktree",
    // A peer's session on my rail (remote_peer) is theirs to resume: the
    // server answers 409, so the verb is not offered. My own sandboxed share
    // (peer_share, not remote) is still resumable.
    enabled: (c) =>
      settled(c) && c.inst!.status === "paused" && !(c.inst as { remote_peer?: boolean }).remote_peer,
    run: (t) => void resumeSession(t),
  },
  // The two ways a session ends, and only these two.
  close: {
    label: "Close session",
    short: "Close",
    keys: "Ctrl+W",
    note: "stops the agent and moves it to Recently closed",
    enabled: () => true,
    run: (t) => void closeSession(t),
  },
  delete: {
    label: "Delete session…",
    short: "Delete…",
    note: "removes the worktree and branch",
    enabled: gitHere,
    run: (t) => void deleteSession(t),
  },
} satisfies Record<string, SessionAction>;

export type SessionActionId = keyof typeof SESSION_ACTIONS;

/** An action by id, typed as the general shape (optional fields readable). */
export function sessionAction(id: SessionActionId): SessionAction {
  return SESSION_ACTIONS[id];
}

export function actionLabel(id: SessionActionId, c: ActionCtx): string {
  const l = sessionAction(id).label;
  return typeof l === "function" ? l(c) : l;
}

export function actionNote(id: SessionActionId, c: ActionCtx): string {
  const n = sessionAction(id).note;
  return (typeof n === "function" ? n(c) : n) || "";
}

/** The palette's hint column: the shortcut, then the note. */
export function actionHint(id: SessionActionId, c: ActionCtx): string | undefined {
  return [sessionAction(id).keys, actionNote(id, c)].filter(Boolean).join(" · ") || undefined;
}

/** The actions on a Ctrl+K chord, in the order the "?" sheet lists them. */
export const CHORD_ACTIONS: SessionActionId[] = [
  "commit",
  "push",
  "pr",
  "ide",
  "duplicate",
  "branch",
  "hide",
  "map",
  "message",
  "fastTrack",
  "thread",
];

/** The text the "?" sheet shows for a chord: the label (the IDE generic, the
 * window verb both ways) plus the note where it names the destination. */
export function chordDesc(id: SessionActionId): string {
  if (id === "ide") return "Open in your code editor";
  if (id === "hide") return "Hide / show window";
  if (id === "thread") return "Thread — " + actionNote(id, {} as ActionCtx);
  if (id === "flock") return "Workers on every prompt";
  const l = sessionAction(id).label;
  return typeof l === "string" ? l : id;
}

/** Run a chord's action on the focused session — or say why it doesn't apply
 * (a shared folder's Commit, Code map without git) instead of firing a call
 * the server would refuse. A title no row knows yet runs as before. */
export function runChordAction(id: SessionActionId, title: string) {
  const c = actionCtx(title);
  if (c.inst && !sessionAction(id).enabled(c)) {
    const why = sessionAction(id).why?.(c);
    if (why) errorPop(chordDesc(id) + " isn’t available for " + displayName(title), why);
    else toast(chordDesc(id) + " isn’t available for " + displayName(title));
    return;
  }
  sessionAction(id).run(title);
}
