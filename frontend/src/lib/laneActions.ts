/** Ship lanes, the action half: how far MindFlock carries a session once its
 * agent is done ("Leave it" / "Commit" / "Open a PR" / "Merge when green"),
 * and the direct calls that set it, ship it now, or split it.
 *
 * Every entry point — the pane's Ship & split menu, the row › menu, the
 * palette, Ctrl+K L, the New dialog — goes through here, and every one ACTS:
 * `POST /api/instances/{t}/lane` arms the autopilot server-side, nothing is
 * pasted into the agent. (The rail's lane LINES are phrased elsewhere; this
 * file only holds the vocabulary the controls need.)
 *
 * The pure half (normalising, reading a row's lane, the menu's model) is
 * unit-tested in node; the rest is a thin layer over SPEC §5. */

import { api, ApiError, instApi } from "../api/client";
import type { Caps, Instance } from "../api/types";
import { useUi } from "../state/store";
import { errMsg } from "./format";
import { isRemote } from "./playbooks";
import { instances, selectSession } from "./sessionActions";
import { laneOf } from "./agentMessages";
import { taskPath } from "./runsApi";
import { toast } from "./toast";

export type Lane = "leave" | "commit" | "push" | "pr" | "merge";

/** The lanes in ladder order. `push` is a real rung (the server takes it, and
 * a fast-track default can be it) but no control OFFERS it: "pushed, no PR"
 * is a state worth reaching only on purpose, from the ⏩ default. */
export const LANE_ORDER: readonly Lane[] = ["leave", "commit", "push", "pr", "merge"];

/** The four the segmented control and the menu offer. */
export const LANE_CHOICES: readonly Lane[] = ["leave", "commit", "pr", "merge"];

/** The New dialog's segment labels. */
export const LANE_LABEL: Record<Lane, string> = {
  leave: "Leave it",
  commit: "Commit",
  push: "Push",
  pr: "Open a PR",
  merge: "Merge when green",
};

/** The Ship & split menu's rows: label, key, and what it means in a line. */
export const LANE_MENU: ReadonlyArray<{
  lane: Lane;
  label: string;
  key: string;
  desc: string;
}> = [
  {
    lane: "leave",
    label: "Leave it",
    key: "L",
    desc: "MindFlock doesn't commit anything",
  },
  {
    lane: "commit",
    label: "Commit",
    key: "C",
    desc: "Commit with a message from the diff once the agent stops and hooks pass",
  },
  {
    lane: "pr",
    label: "Open a PR",
    key: "P",
    desc: "Commit, push, open the PR",
  },
  {
    lane: "merge",
    label: "Merge when checks pass",
    key: "M",
    desc: "…and merge it once CI is green",
  },
];

/** A lane from anything the server or a setting might hand us. The autopilot
 * ladder's own words map across: `agent` (stop when the agent stops) and
 * `off` are both "Leave it". Unknown → "". */
export function normalizeLane(v: string | null | undefined): Lane | "" {
  const s = String(v || "")
    .trim()
    .toLowerCase();
  if (s === "agent" || s === "off" || s === "none") return "leave";
  return (LANE_ORDER as readonly string[]).includes(s) ? (s as Lane) : "";
}

/** The lane a NEW batch starts on: the fast-track default from Settings →
 * Workspace ("Fast-track goes as far as"), falling back to a PR — the
 * server's own default for that setting. */
export function laneDefault(fasttrackDepth: string | null | undefined): Lane {
  return normalizeLane(fasttrackDepth) || "pr";
}

/** A session's lane as the menu reads it: agentMessages.laneOf (THE rule —
 * `row.lane` is authoritative, else an older server's autopilot depth), in the
 * menu's shape, so the menu's ✓ and the rail's line never disagree. No lane
 * reads as "leave". */
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
export function teamRunCaps(caps: Partial<Caps> | undefined): { split: boolean; together: boolean } {
  const t = caps?.team_runs;
  return { split: t?.split === true, together: t?.together === true };
}

/** The reason shown on a control an OLDER server can't take (its caps don't
 * say it does — it would refuse the create). */
export const SERVER_NO_SPLIT = "this MindFlock server can't split a line into pieces — update it";
export const SERVER_NO_TOGETHER = "this MindFlock server can't make one PR for a group — update it";

/** "Ask me before it ships" means nothing on a lane that never ships. */
export function askFirstApplies(lane: Lane): boolean {
  return lane !== "leave";
}

// --- What the menu can do for a session ---------------------------------------

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
    if (!m.enabled) return "MindFlock tools are switched off for new sessions — Settings → General";
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

// --- Talking to the server ----------------------------------------------------

export interface LaneResult {
  ok: boolean;
  lane?: { target?: string; ask_first?: boolean };
}

/** Arm (or, for "leave", disarm) this session's lane. Throws the server's
 * sentence on refusal. */
export async function setLane(title: string, lane: Lane, askFirst: boolean): Promise<LaneResult> {
  return instApi<LaneResult>(title, "/lane", {
    json: { lane, ask_first: askFirstApplies(lane) && askFirst },
  });
}

/** Take what's there now through the lane — no waiting for the agent's idle
 * dwell. `lane` is the lane the row SHOWED (what the user is approving); the
 * server never falls back to the Settings default for a row with no lane of
 * its own. It refuses (409) while the agent is mid-turn. */
export async function shipNow(title: string, lane?: Lane): Promise<void> {
  await instApi(title, "/ship-now", { json: lane && lane !== "leave" ? { lane } : {} });
}

/** Why this row can't set a lane or ship ITSELF, or "": a group's lead ships
 * once, through the group's release; a one-for-all member's work merges into
 * the group's one PR; a copy window shows the lane of the window that drives
 * its branch (`lane.owner`) and is never armed. The server refuses all three
 * too — this only says so before the click. */
export function laneLockReason(
  inst: Partial<Pick<Instance, "lane" | "run" | "title">>
): string {
  const run = inst.run;
  if (run && run.id) {
    const group = run.name || "its group";
    if (run.role === "lead") return `It leads ${group} — the group ships it once, through the group's release`;
    if (run.grouping === "together")
      return `Its work merges into ${group}'s one PR — move it out of the group to give it a lane of its own`;
  }
  const owner = inst.lane?.owner;
  if (owner && inst.title && owner !== inst.title) return `${owner} drives this branch — set its lane from that window`;
  return "";
}

/** Detach a session from its group: the group stops driving it, and its
 * session and lane stay exactly as they are (SPEC §5 `skip` on a running
 * task). */
export async function moveOutOfGroup(runId: string, taskId: string): Promise<void> {
  await api(taskPath(runId, taskId, "skip"), { json: {} });
}

/** Make `title` the lead of a split run: it proposes the pieces, the user
 * approves them in its Thread tab, MindFlock starts and merges them back.
 * `lead` names the existing session, which becomes the lead (an older server
 * without `caps.team_runs.split` refuses splits, and the menu item that calls
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

/** The lane a new single session was given in the New dialog, applied once the
 * session can take it. A fresh worktree may not exist for a few seconds (the
 * create returns while it provisions, and arming answers 409 "workspace not
 * ready" until it does), so a 409 is retried on a short ladder; anything else
 * is the server's sentence in a toast. Never blocks the dialog. */
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
        toast(`Couldn't set “${LANE_LABEL[lane]}” on ${title}: ${errMsg(err)}`, {
          duration: 7000,
        });
        return false;
      }
      await new Promise((r) => setTimeout(r, delays[i]));
    }
  }
}

// --- The Ship & split menu's shape --------------------------------------------

export type ShipEntry =
  | {
      kind: "lane";
      lane: Lane;
      label: string;
      key: string;
      desc: string;
      current: boolean;
      why: string;
    }
  | { kind: "ask"; key: string; on: boolean; why: string }
  | { kind: "split"; key: string; why: string }
  | { kind: "shipnow"; key: string; why: string }
  | { kind: "detach"; key: string }
  | { kind: "message" };

export interface ShipMenuModel {
  current: { lane: Lane; askFirst: boolean };
  lanes: ShipEntry[];
  split: ShipEntry[];
  /** The group section: its name and live size, or null for a session on its
   * own (whose section then has no heading). */
  group: { name: string; count: number } | null;
  tail: ShipEntry[];
}

/** What the menu offers `inst`, with each item's reason when it can't. */
export function shipMenuModel(
  inst: Pick<Instance, "provider" | "program" | "mcp_attached" | "activity"> &
    Partial<Pick<Instance, "lane" | "autopilot" | "run" | "title" | "device">>,
  rows: readonly Partial<Pick<Instance, "run">>[],
  caps: Partial<Caps> | undefined
): ShipMenuModel {
  const cur = laneChoice(inst);
  const lock = laneLockReason(inst);
  const lanes: ShipEntry[] = LANE_MENU.map((m) => ({
    kind: "lane" as const,
    ...m,
    current: m.lane === cur.lane,
    why: lock,
  }));
  lanes.push({
    kind: "ask",
    key: "A",
    on: cur.askFirst && askFirstApplies(cur.lane),
    why: lock || (askFirstApplies(cur.lane) ? "" : "Nothing ships while it's “Leave it” — pick a lane first"),
  });
  const split: ShipEntry[] = [{ kind: "split", key: "S", why: splitBlockReason(caps, inst) }];
  const member = inst.run && inst.run.id ? inst.run : null;
  const tail: ShipEntry[] = [
    {
      kind: "shipnow",
      key: "N",
      why: lock || (cur.lane === "leave" ? "Pick how far it goes first — “Leave it” ships nothing" : ""),
    },
  ];
  // A group's LEAD has no task of its own to move out (it is the group's
  // branch): only a member line can be detached.
  if (member && member.task && member.role !== "lead") tail.push({ kind: "detach", key: "O" });
  tail.push({ kind: "message" });
  return {
    current: { lane: cur.lane, askFirst: cur.askFirst },
    lanes,
    split,
    group: member
      ? {
          name: member.name || "group",
          count: Math.max(1, groupSize(inst, rows)),
        }
      : null,
    tail,
  };
}

/** Every entry in the order the menu draws (and the arrows walk) them. */
export function shipEntries(m: ShipMenuModel): ShipEntry[] {
  return [...m.lanes, ...m.split, ...m.tail];
}

/** The letter an entry answers to ("" for Message…, which is a chord). */
export function entryKey(e: ShipEntry): string {
  return e.kind === "message" ? "" : e.key;
}

/** Why an entry can't run right now, or "". */
export function entryWhy(e: ShipEntry): string {
  return e.kind === "lane" || e.kind === "ask" || e.kind === "split" || e.kind === "shipnow" ? e.why : "";
}

/** Run one Ship & split entry for `inst` — the pane menu and the row › menu
 * both come here, so the two can never disagree about what an item does.
 * Resolves to the sentence to toast; throws the server's refusal. Message…
 * is not here: it opens the Thread composer, which is the caller's UI. */
export async function runShipEntry(
  e: ShipEntry,
  inst: Pick<Instance, "title" | "program" | "repo"> & Partial<Instance>,
  name: string,
  current: { lane: Lane; askFirst: boolean }
): Promise<string> {
  const title = inst.title;
  switch (e.kind) {
    case "lane":
      await setLane(title, e.lane, current.askFirst);
      return e.lane === "leave"
        ? `${name}: MindFlock won't commit anything`
        : `${name} → ${LANE_LABEL[e.lane]}${current.askFirst ? ", asks first" : ""}`;
    case "ask":
      await setLane(title, current.lane, !e.on);
      return e.on
        ? `${name} ships without asking`
        : `${name} stops before it ships and asks in the Outbox`;
    case "split":
      // The release lane of the split: the session's own, or a PR when it
      // had none — a split that ends in "nothing" would merge pieces into a
      // branch nobody is told about.
      await startSplitOf(
        inst,
        name,
        current.lane === "leave" ? "pr" : current.lane,
        current.askFirst
      );
      useUi.getState().threadOpen(title);
      return `${name} is proposing the pieces — approve the plan in its Thread tab`;
    case "shipnow":
      await shipNow(title, current.lane);
      return `Shipping ${name} now → ${LANE_LABEL[current.lane]}`;
    case "detach": {
      const run = inst.run;
      if (!run) return "";
      await moveOutOfGroup(run.id, run.task);
      return `${name} is out of ${run.name} — its session and lane stay as they are`;
    }
    case "message":
      return "";
  }
}

/** Open a session's Ship & split menu (Ctrl+K F, Ctrl+K L, the palette). It
 * hangs off the pane's own fork button, so the pane comes forward first. */
export function openShipMenu(title: string, sub: "lane" | null = null): void {
  const inst = instances().find((r) => r.title === title);
  if (!inst || inst.pending) {
    toast("That session isn't ready yet");
    return;
  }
  if (isRemote(inst)) {
    toast("Ship & split works on this machine's sessions — open it on its own device");
    return;
  }
  selectSession(title, { noKeyboard: true });
  // A frame later: the pane may have just been un-hidden, and its button is
  // the anchor the menu measures itself against.
  requestAnimationFrame(() => useUi.getState().setPlaybookMenu({ title, sub }));
}
