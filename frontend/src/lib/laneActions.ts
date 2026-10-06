/** Fast-track, the action half: how far MindFlock carries a session once its
 * agent is done — Off / Commit / Push / Open a PR / Merge when green, plus
 * "Ask me before it ships" — and the direct calls that set it or split a
 * session into parallel pieces.
 *
 * ONE name on every screen: Fast-track. The API spells a fast-track target
 * "lane" (`POST /api/instances/{t}/lane`, `row.lane`, the MCP's params), and
 * this file keeps that word for its internals; no label it hands a screen
 * ever says it.
 *
 * Every door goes to the same picker: the pane's ⏩ button (THE per-session
 * control — it shows the target and opens the picker), the row › menu's
 * "Fast-track…", the palette and Ctrl+K F. The New dialog, Intake's "Start
 * together" (which opens New) and the Commit dialog draw the same choices as
 * a segmented control. Every one ACTS: `POST /lane` arms the autopilot
 * server-side; nothing is pasted into the agent.
 *
 * The pure half (normalising, reading a row's target, the picker's model) is
 * unit-tested in node; the rest is a thin layer over the routes. */

import { api, ApiError, instApi } from "../api/client";
import type { AutopilotRun, Caps, Instance, LaneInfo } from "../api/types";
import { patchInstance } from "../state/queries";
import { useUi } from "../state/store";
import { errMsg } from "./format";
import { instances, selectSession } from "./sessionActions";
import { laneOf } from "./agentMessages";
import { taskPath } from "./runsApi";
import { toast } from "./toast";

/** A fast-track target as the API names it ("leave" = Off). */
export type Lane = "leave" | "commit" | "push" | "pr" | "merge";

/** The rungs in ladder order. Every fast-track control offers all five —
 * Push included: "pushed, no PR" is a real place to stop, and leaving it out
 * of one control was how the same choice read differently in two places. */
export const LANE_ORDER: readonly Lane[] = ["leave", "commit", "push", "pr", "merge"];

/** What every fast-track control offers, nearest first. */
export const LANE_CHOICES: readonly Lane[] = LANE_ORDER;

/** THE user-facing word for each rung. */
export const LANE_LABEL: Record<Lane, string> = {
  leave: "Off",
  commit: "Commit",
  push: "Push",
  pr: "Open a PR",
  merge: "Merge when green",
};

/** The ⏩ button's own text for each rung, where a pane head has no room. */
export const LANE_SHORT: Record<Lane, string> = {
  leave: "off",
  commit: "Commit",
  push: "Push",
  pr: "PR",
  merge: "Merge",
};

/** What each rung means, in one line: the picker's second line and the
 * segmented control's tooltips. */
export const LANE_DESC: Record<Lane, string> = {
  leave: "MindFlock doesn't commit anything — you take it from there",
  commit: "Commit with a message written from the diff once the agent stops and your hooks pass",
  push: "Commit and push once the agent stops and your hooks pass",
  pr: "Commit, push and open a PR once the agent stops and your hooks pass",
  merge: "Commit, push, open a PR and merge it once its checks pass",
};

/** The picker's letters: Off, Commit, pUsh, PR, Merge (A is "ask first"). */
export const LANE_KEY: Record<Lane, string> = {
  leave: "O",
  commit: "C",
  push: "U",
  pr: "P",
  merge: "M",
};

/** The toggle every fast-track control carries beside the rungs. */
export const ASK_FIRST_LABEL = "Ask me before it ships";
export const ASK_FIRST_DESC = "Stop one step short and wait for your OK in the bell";

/** A target from anything the server or a setting might hand us. The autopilot
 * ladder's own words map across: `agent` (stop when the agent stops) and
 * `off` are both Off. Unknown → "". */
export function normalizeLane(v: string | null | undefined): Lane | "" {
  const s = String(v || "")
    .trim()
    .toLowerCase();
  if (s === "agent" || s === "off" || s === "none") return "leave";
  return (LANE_ORDER as readonly string[]).includes(s) ? (s as Lane) : "";
}

/** THE default for batches and ticket runs: Settings → Workspace
 * "Fast-track goes as far as" (`/api/config` fasttrack_default). Out of the
 * box — unset, blank or unknown — it is Off: nothing is fast-tracked unless
 * someone asked. A single new session never reads it (runStart.defaultLaneFor
 * starts it Off). */
export function laneDefault(setting: string | null | undefined): Lane {
  return normalizeLane(setting) || "leave";
}

/** A session's target as the controls read it: agentMessages.laneOf (THE rule
 * — `row.lane` is authoritative, else an older server's autopilot depth), in
 * the controls' shape, so the ⏩ button, the picker's ✓ and the rail's line
 * never disagree. No target reads as Off. */
export function laneChoice(inst: Partial<Pick<Instance, "lane" | "autopilot">>): {
  lane: Lane;
  askFirst: boolean;
  owner: string;
} {
  const l = laneOf(inst);
  const lane = l ? normalizeLane(l.target) : "";
  if (!l || !lane) return { lane: "leave", askFirst: false, owner: "" };
  return { lane, askFirst: !!l.ask_first, owner: l.owner || "" };
}

/** What a group can be started as, from the server's `caps.team_runs` — THE
 * switch for the two shapes an older server refuses (one PR for the whole
 * group; splitting one line into pieces). A server that doesn't say is read
 * as "can't": its create would 400 them. Every control that offers either one
 * reads this. */
export function teamRunCaps(caps: Partial<Caps> | undefined): {
  split: boolean;
  together: boolean;
  /** The most pieces a split may have — 8 (the server's default) when an
   * older server doesn't say. */
  maxPieces: number;
} {
  const t = caps?.team_runs;
  const max = Number(t?.max_pieces);
  return {
    split: t?.split === true,
    together: t?.together === true,
    maxPieces: Number.isFinite(max) && max >= 2 ? Math.floor(max) : 8,
  };
}

/** The reason shown on a control an OLDER server can't take (its caps don't
 * say it does — it would refuse the create). */
export const SERVER_NO_SPLIT = "this MindFlock server can't split a line into pieces — update it";
export const SERVER_NO_TOGETHER = "this MindFlock server can't make one PR for a group — update it";

/** "Ask me before it ships" means nothing while fast-track is off. */
export function askFirstApplies(lane: Lane): boolean {
  return lane !== "leave";
}

// --- What a session can do ----------------------------------------------------

/** Why "Split into parallel pieces…" is off for this session, or "". A split
 * makes this session the LEAD, and the lead proposes the plan with its
 * MindFlock tools — so it needs a CLI that gets them, this launch to have
 * them, and an agent that isn't sitting on a prompt or a usage limit. */
export function splitBlockReason(
  caps: Partial<Caps> | undefined,
  inst: Pick<Instance, "provider" | "program" | "mcp_attached" | "activity"> &
    Partial<Pick<Instance, "title" | "device">>
): string {
  if (!teamRunCaps(caps).split) return SERVER_NO_SPLIT.charAt(0).toUpperCase() + SERVER_NO_SPLIT.slice(1);
  const m = caps?.agent_mcp;
  if (m) {
    if (!m.enabled) return "MindFlock tools are switched off for new sessions — Settings → Agent orchestration";
    const provider = inst.provider || inst.program || "";
    if (!(m.providers || []).includes(provider)) return "This CLI doesn't get the MindFlock tools";
  }
  if (inst.mcp_attached === false) return "Restart this agent to give it the MindFlock tools";
  if (inst.activity === "clarify") return "Answer its prompt first";
  if (inst.activity === "limit") return "It's at its usage limit — split once it's back";
  return "";
}

/** The task a split starts from: what the session was last asked, in full
 * when the server kept it. */
export function splitTaskOf(
  inst: Partial<Pick<Instance, "last_prompt_full" | "last_prompt" | "last_turn" | "title">>
): string {
  return String(inst.last_prompt_full || inst.last_prompt || inst.last_turn || "").trim();
}

/** How many live rows share this session's group (itself included). */
export function groupSize(
  inst: Partial<Pick<Instance, "run">>,
  rows: readonly Partial<Pick<Instance, "run">>[]
): number {
  const id = inst.run?.id;
  if (!id) return 0;
  return rows.filter((r) => r.run?.id === id).length;
}

/** The group a row can be moved out of: a MEMBER line of a group (a lead has
 * no task of its own — it is the group's branch), or null. */
export function detachableGroup(
  inst: Partial<Pick<Instance, "run">>
): { id: string; task: string; name: string } | null {
  const run = inst.run;
  if (!run || !run.id || !run.task || run.role === "lead") return null;
  return { id: run.id, task: run.task, name: run.name || "its group" };
}

/** Why this row can't set its own fast-track, or "": a group's lead ships
 * once, through the group's release; a one-for-all member's work merges into
 * the group's one PR; a copy window shows the target of the window that
 * drives its branch (`lane.owner`) and is never armed. The server refuses all
 * three too — this only says so before the click. */
export function laneLockReason(
  inst: Partial<Pick<Instance, "lane" | "run" | "title">>
): string {
  const run = inst.run;
  if (run && run.id) {
    const group = run.name || "its group";
    if (run.role === "lead") return `It leads ${group} — the group ships it once, through the group's release`;
    if (run.grouping === "together")
      return `Its work merges into ${group}'s one PR — move it out of the group to fast-track it on its own`;
  }
  const owner = inst.lane?.owner;
  if (owner && inst.title && owner !== inst.title) return `${owner} drives this branch — set its fast-track from that window`;
  return "";
}

// --- The ⏩ picker's shape -----------------------------------------------------

export interface FastTrackItem {
  lane: Lane;
  label: string;
  desc: string;
  key: string;
  current: boolean;
}

export interface FastTrackModel {
  current: { lane: Lane; askFirst: boolean };
  /** Why nothing in the picker can be chosen for this row, or "". */
  lock: string;
  items: FastTrackItem[];
  ask: { on: boolean; why: string };
}

/** What the ⏩ picker offers `inst`: every rung (the current one ticked),
 * and "Ask me before it ships", with the reason when it can't be used. */
export function fastTrackModel(
  inst: Partial<Pick<Instance, "lane" | "autopilot" | "run" | "title">>
): FastTrackModel {
  const cur = laneChoice(inst);
  const lock = laneLockReason(inst);
  return {
    current: { lane: cur.lane, askFirst: cur.askFirst },
    lock,
    items: LANE_CHOICES.map((lane) => ({
      lane,
      label: LANE_LABEL[lane],
      desc: LANE_DESC[lane],
      key: LANE_KEY[lane],
      current: lane === cur.lane,
    })),
    ask: {
      on: cur.askFirst && askFirstApplies(cur.lane),
      why: lock || (askFirstApplies(cur.lane) ? "" : "Fast-track is off — pick how far it goes first"),
    },
  };
}

/** The sentence a pick is confirmed with. */
export function fastTrackSaid(name: string, lane: Lane, askFirst: boolean): string {
  if (lane === "leave") return `${name}: fast-track off — MindFlock won't commit anything`;
  return `${name}: fast-track → ${LANE_LABEL[lane]}${askFirstApplies(lane) && askFirst ? ", asks first" : ""}`;
}

// --- Talking to the server ----------------------------------------------------

export interface LaneResult {
  ok: boolean;
  held?: boolean;
  lane?: LaneInfo | null;
  autopilot?: AutopilotRun | null;
}

/** Arm (or, for Off, disarm) this session's fast-track. `message` is the
 * commit message the Commit dialog just used. Throws the server's sentence on
 * refusal. */
export async function setLane(
  title: string,
  lane: Lane,
  askFirst: boolean,
  opts: { message?: string } = {}
): Promise<LaneResult> {
  return instApi<LaneResult>(title, "/lane", {
    json: {
      lane,
      ask_first: askFirstApplies(lane) && askFirst,
      ...(opts.message ? { message: opts.message } : {}),
    },
  });
}

/** Choose a session's fast-track from a control and say so. The row flips the
 * moment it is chosen (the ⏩ button and the rail line read the cached row, and
 * a round trip made a local, instant choice read as a laggy one); the
 * server's answer settles it, and a refusal rolls it back and says why.
 * Resolves to whether it took. */
export async function pickFastTrack(
  title: string,
  name: string,
  lane: Lane,
  askFirst: boolean,
  opts: { message?: string; quiet?: boolean } = {}
): Promise<boolean> {
  const row = instances().find((r) => r.title === title);
  const before = { lane: row?.lane ?? null, autopilot: row?.autopilot ?? null };
  patchInstance(title, {
    lane:
      lane === "leave"
        ? null
        : { target: lane, ask_first: askFirstApplies(lane) && askFirst, owner: title, by: "user" },
    ...(lane === "leave" ? { autopilot: null } : {}),
  });
  try {
    const r = await setLane(title, lane, askFirst, opts);
    patchInstance(title, {
      lane: r?.lane ?? null,
      ...(r && "autopilot" in r ? { autopilot: r.autopilot ?? null } : {}),
    });
    if (!opts.quiet)
      toast(
        r?.held
          ? `${name}: its group is paused — fast-track → ${LANE_LABEL[lane]} starts when it resumes`
          : fastTrackSaid(name, lane, askFirst),
        { duration: 4500 }
      );
    return true;
  } catch (err) {
    patchInstance(title, before);
    toast(`${name}: ${errMsg(err)}`, { duration: 6000 });
    return false;
  }
}

/** Detach a session from its group: the group stops driving it, and its
 * session and fast-track stay exactly as they are (the run's `skip` on a
 * running task). */
export async function moveOutOfGroup(runId: string, taskId: string): Promise<void> {
  await api(taskPath(runId, taskId, "skip"), { json: {} });
}

/** Make `title` the lead of a split run: it proposes the pieces, the user
 * approves them in its Thread tab, MindFlock starts and merges them back.
 * `lead` names the existing session, which becomes the lead (an older server
 * without `caps.team_runs.split` refuses splits, and every control that calls
 * this is disabled with SERVER_NO_SPLIT). */
export async function startSplitOf(
  inst: Pick<Instance, "title" | "program" | "repo"> & Partial<Instance>,
  name: string,
  lane: Lane,
  askFirst: boolean
): Promise<{ run?: { id?: string; name?: string } }> {
  return api("/api/runs", {
    json: {
      name,
      items: [{ kind: "task", text: splitTaskOf(inst) }],
      policy: {
        lane,
        ask_first: askFirstApplies(lane) && askFirst,
        grouping: "together",
        release: "ask",
      },
      concurrency: 3,
      program: inst.provider || inst.program,
      split: true,
      lead: inst.title,
    },
  });
}

/** "Split into parallel pieces…" — the row › menu and the palette. The
 * group's one PR goes as far as the session's own fast-track, or to a PR
 * when it had none: a split that ends in "nothing" would merge the pieces
 * into a branch nobody is told about. Resolves to the sentence to toast;
 * throws the server's refusal. */
export async function splitSession(
  inst: Pick<Instance, "title" | "program" | "repo"> & Partial<Instance>,
  name: string
): Promise<string> {
  const cur = laneChoice(inst);
  await startSplitOf(inst, name, cur.lane === "leave" ? "pr" : cur.lane, cur.askFirst);
  useUi.getState().threadOpen(inst.title);
  return `${name} is proposing the pieces — approve the plan in its Thread tab`;
}

/** The fast-track a new single session was given in the New dialog, applied
 * once the session can take it. A fresh worktree may not exist for a few
 * seconds (the create returns while it provisions, and arming answers 409
 * "workspace not ready" until it does), so a 409 is retried on a short
 * ladder; anything else is the server's sentence in a toast. Never blocks the
 * dialog. */
export async function setLaneWhenReady(
  title: string,
  lane: Lane,
  askFirst: boolean,
  delays: readonly number[] = [1500, 3000, 5000, 8000, 12000, 20000]
): Promise<boolean> {
  if (lane === "leave") return true;
  for (let i = 0; ; i++) {
    try {
      await setLane(title, lane, askFirst);
      return true;
    } catch (err) {
      const retry = err instanceof ApiError && err.status === 409 && i < delays.length;
      if (!retry) {
        toast(`Couldn't fast-track ${title} to “${LANE_LABEL[lane]}”: ${errMsg(err)}`, {
          duration: 7000,
        });
        return false;
      }
      await new Promise((r) => setTimeout(r, delays[i]));
    }
  }
}

/** Open a session's ⏩ picker (Ctrl+K F, the row › menu, the palette). It
 * hangs off the pane's own ⏩ button, so the pane comes forward first. */
export function openFastTrackMenu(title: string): void {
  const inst = instances().find((r) => r.title === title);
  if (!inst || inst.pending) {
    toast("That session isn't ready yet");
    return;
  }
  selectSession(title, { noKeyboard: true });
  // A frame later: the pane may have just been un-hidden, and its ⏩ button is
  // the anchor the picker measures itself against.
  requestAnimationFrame(() => useUi.getState().setFastTrackMenu({ title }));
}
