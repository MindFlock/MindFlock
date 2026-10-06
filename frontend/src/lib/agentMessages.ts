/** MindFlock MCP, UI side: how agent-to-agent traffic and session lineage
 * read in the app.
 *
 * Pure (no DOM, no store) so the wording is unit-tested in node; the callers
 * pass in the name resolver (`displayName`, which honours the user's aliases).
 *
 * Three surfaces, deliberately different in volume:
 *  - the toast (EventToasts) shows a `session.message` as one line —
 *    "✉ orch → w1: …" — and the caller throttles it per sender;
 *  - the bell feed (NotificationsBell) lists only `kind=result` — a worker
 *    reporting done / blocked / failed is news you want in "what happened while
 *    you were away"; an orchestrator chatting with its workers is not, and
 *    listing every message would bury the rows that are;
 *  - the rail row (SidebarRow) carries the lineage marker (`lineageMark`),
 *    and for a family (an orchestrator and the workers it spawned) the
 *    worker's status line (`workerLine`), the parent's roll-up (`rollup`) and
 *    the parent's waiting / wrap-up chip (`parentChip`).
 */

import type { Instance, LastReport } from "../api/types";

/** The fields of a `session.message` event's `data` the UI reads. `status` is
 * read when present (a worker's report_result carries it in the message's
 * data; an event that forwards it gets the richer line). */
export interface MessageEventData {
  id?: string;
  from?: string;
  kind?: string;
  text?: string;
  delivery?: string;
  status?: string;
}

/** Characters of the message body a toast / bell row shows. */
export const MESSAGE_SNIPPET = 80;

/** One line, at most `max` chars, "…" when cut. The server already sanitizes
 * the body (control chars stripped), but the event text can still hold
 * newlines — a toast is one line. */
export function snippet(text: unknown, max = MESSAGE_SNIPPET): string {
  const s = String(text ?? "").replace(/\s+/g, " ").trim();
  return s.length > max ? s.slice(0, max - 1).trimEnd() + "…" : s;
}

/** Who sent it, for display: a session's name, or "external" for the CLI /
 * an MCP client outside the flock (`from` is ""). */
function senderName(from: string, nameOf: (t: string) => string): string {
  return from ? nameOf(from) : "external";
}

/** A worker's report status, normalised ("" when the event carries none). */
function resultStatus(d: MessageEventData): string {
  return String(d.status || "").trim().toLowerCase();
}

/** ✓ for done (or unknown), ⚠ for blocked / failed. */
function resultMark(status: string): string {
  return status === "blocked" || status === "failed" ? "⚠" : "✓";
}

/** The toast line for a `session.message` event (`recipient` is the event's
 * session), e.g. "✉ orch → w1: rebase onto main first" or
 * "✓ worker w1 reported: done — tests pass". */
export function messageToastText(
  recipient: string,
  data: MessageEventData | null | undefined,
  nameOf: (t: string) => string
): string {
  const d = data || {};
  const from = senderName(String(d.from || ""), nameOf);
  const body = snippet(d.text);
  if (d.kind === "result") {
    const status = resultStatus(d);
    const head = resultMark(status) + " worker " + from + " reported";
    if (status) return head + ": " + status + (body ? " — " + body : "");
    return head + (body ? ": " + body : "");
  }
  return "✉ " + from + " → " + nameOf(recipient) + (body ? ": " + body : "");
}

/** The bell-feed row for a `session.message` event, or null to stay out of
 * the feed (plain messages — see the module comment). The row is filed under
 * the recipient (the parent), so the text names the worker. */
export function messageNotif(
  data: MessageEventData | null | undefined,
  nameOf: (t: string) => string
): { text: string; cls: string } | null {
  const d = data || {};
  if (d.kind !== "result") return null;
  const status = resultStatus(d);
  const from = senderName(String(d.from || ""), nameOf);
  const body = snippet(d.text);
  const warn = status === "blocked" || status === "failed";
  return {
    text: "worker " + from + " reported" + (status ? " " + status : "") + (body ? " — " + body : ""),
    cls: warn ? "n-warn" : "n-done",
  };
}

/** How the rail row marks a session's lineage, or null for a plain session.
 *
 * `parent` is "" when the session has none OR its stored parent is not a live
 * session any more (the server validates lazily), so a spawned row without a
 * parent is an orphan or an external client's spawn — still worth flagging as
 * agent-made, because that is what gates kill/delete in the MCP. */
export function lineageMark(
  parent: string | undefined,
  spawned: boolean | undefined,
  nameOf: (t: string) => string
): { text: string; title: string; spawned: boolean } | null {
  const p = String(parent || "");
  const s = !!spawned;
  if (!p && !s) return null;
  if (p) {
    const name = nameOf(p);
    return {
      text: "↳ " + name,
      title: s
        ? "Spawned by the agent in “" + name + "”"
        : "Child of “" + name + "” (adopted)",
      spawned: s,
    };
  }
  return {
    text: "↳ agent",
    title: "Spawned by an agent — its parent session is gone, or it was an external MCP client",
    spawned: true,
  };
}

// --- Families on the rail ------------------------------------------------------
//
// A family is an orchestrator and the live sessions whose `parent` names it.
// Everything below reads only row fields (`activity`, `activity_since`,
// `last_report`), so the rail tells you how a split is going without a fetch:
// zero clicks for "who's done, who's stuck".

/** The row fields the family wording reads. */
export type FamilyRow = Pick<Instance, "title"> &
  Partial<Pick<Instance, "activity" | "activity_since" | "status" | "parent" | "last_report" | "lane">>;

/** THE "is a worker of" rule, shared by every surface that counts workers —
 * the fork menu's "workers · N", the palette's worker entries, the rail's
 * roll-up and chip, the bell, and the Thread's rows and "all workers" — so
 * none of them disagree, and all agree with the server's `children_of`
 * (which decides `has_children`): a LOCAL row whose `parent` is the session.
 * A pending row (still cloning) isn't in the server's registry yet and can't
 * be messaged; a remote device's row names a parent on THAT device. */
export function isChildOf(
  row: Pick<Instance, "title"> & Partial<Pick<Instance, "parent" | "pending" | "device">>,
  title: string
): boolean {
  return !!title && row.title !== title && String(row.parent || "") === title && !row.pending && !row.device;
}

/** `title`'s workers by {@link isChildOf}, in the order given (rail order). */
export function childrenOf<T extends Pick<Instance, "title"> & Partial<Pick<Instance, "parent" | "pending" | "device">>>(
  title: string,
  rows: readonly T[]
): T[] {
  return rows.filter((r) => isChildOf(r, title));
}

/** Where one worker stands, most urgent first:
 *  - `ask`     waiting on YOUR answer (a dialog — clarify);
 *  - `blocked` / `failed`  its newest report says so;
 *  - `done`    reported done;
 *  - `limit`   hit the usage limit (its queue waits the window out by itself);
 *  - `working` busy with no report yet, or back at work after one;
 *  - `idle`    stopped without reporting. */
export type WorkerState = "ask" | "blocked" | "failed" | "done" | "limit" | "working" | "idle";

/** The live activity of a row — `effectiveActivity` in the app (it smooths
 * flicker), the raw field in tests. */
export type ActivityOf = (row: FamilyRow) => string;
const rawActivity: ActivityOf = (r) => String(r.activity || "idle");

/** The worker's report, or null when it hasn't sent one OR has gone back to
 * work since (its parent replied with more to do): a report is a claim about
 * the work as it stood then, and a busy worker has moved on from it. */
export function currentReport(row: FamilyRow, act: string): LastReport | null {
  const r = row.last_report;
  if (!r || !String(r.status || "").trim()) return null;
  const busy = act === "working" || act === "clarify" || act === "limit";
  if (busy && Number(row.activity_since) > Number(r.ts)) return null;
  return r;
}

/** Classify one worker (see `WorkerState`). A dialog outranks a report: a
 * worker that reported and then stopped on a prompt needs you first. */
export function workerState(row: FamilyRow, act: string): WorkerState {
  if (act === "clarify") return "ask";
  const r = currentReport(row, act);
  if (r) {
    const s = String(r.status).trim().toLowerCase();
    return s === "blocked" || s === "failed" ? s : "done";
  }
  if (act === "limit") return "limit";
  if (act === "working") return "working";
  return "idle";
}

const isReported = (s: WorkerState) => s === "done" || s === "blocked" || s === "failed";

/** "40s", "6m", "2h", "3d" — how long since `ts` (epoch seconds). */
function since(ts: number, now: number): string {
  const secs = Math.max(0, Math.floor(now - ts));
  if (secs < 60) return secs + "s";
  if (secs < 3600) return Math.floor(secs / 60) + "m";
  if (secs < 86400) return Math.floor(secs / 3600) + "h";
  return Math.floor(secs / 86400) + "d";
}

/** The status line under a worker's name on the rail.
 *
 * `nested` = the row sits directly under its parent (or a sibling) and is
 * indented with a connector, which already says whose worker it is. A worker
 * dragged away from its family loses the indent, so its line names the parent
 * instead: "↳ api · ✓ reported". `cls` picks the colour: rep-done (green),
 * rep-blocked (red), rep-ask (gold), rep-work / rep-idle (muted). */
export function workerLine(
  row: FamilyRow,
  opts: { nested: boolean; parentName: string; act?: string; now?: number }
): { text: string; cls: string; title: string; state: WorkerState } {
  const act = opts.act ?? rawActivity(row);
  const now = opts.now ?? Date.now() / 1000;
  const state = workerState(row, act);
  const r = currentReport(row, act);
  let text: string;
  let cls: string;
  if (state === "ask") {
    text = "? needs your answer";
    cls = "rep-ask";
  } else if (state === "blocked" || state === "failed") {
    text = "✗ " + state;
    cls = "rep-blocked";
  } else if (state === "done") {
    text = "✓ reported";
    cls = "rep-done";
  } else if (state === "limit") {
    text = "usage limit — waiting";
    cls = "rep-blocked";
  } else if (state === "working") {
    const t = Number(row.activity_since) || 0;
    text = t > 0 ? "working · " + since(t, now) : "working";
    cls = "rep-work";
  } else {
    text = "idle — no report";
    cls = "rep-idle";
  }
  // The lane leads (when the row has one) so a narrow rail cuts the detail,
  // never how far MindFlock carries the session. A worker waiting on you or
  // blocked says THAT first instead — it is the thing to act on.
  const lead = laneLead(row.lane);
  if (lead && (state === "working" || state === "idle" || state === "done" || state === "limit")) {
    text = lead + " · " + text;
  }
  const parent = opts.parentName;
  let detail: string;
  if (state === "ask") detail = "it is waiting on a prompt — answer it here or in its pane";
  else if (r)
    detail =
      "reported " + String(r.status).trim().toLowerCase() + " " + since(Number(r.ts), now) + " ago" +
      (r.summary ? ": " + snippet(r.summary, 140) : "");
  else if (state === "working") detail = "still working, no report yet";
  else if (state === "limit") detail = "hit the usage limit; its queue resumes when the window resets";
  else detail = "stopped without reporting back";
  return {
    text: opts.nested || !parent ? text : "↳ " + parent + " · " + text,
    cls,
    title: (parent ? "Worker of “" + parent + "” — " : "") + detail,
    state,
  };
}

/** One coloured part of the roll-up line. `cls`: needs (gold), bad (red),
 * ok (green), "" (plain). */
export interface RollupPart {
  text: string;
  cls: "needs" | "bad" | "ok" | "";
}

const plural = (n: number, word: string) => n + " " + word + (n === 1 ? "" : "s");

/** The orchestrator's roll-up line, most urgent first — "1 needs you ·
 * 3 workers", "2 of 3 reported", "all 3 reported", "3 working". The tooltip
 * names every worker with its own status, so hovering answers "which one?"
 * without opening anything. null for a session with no workers. */
export function rollup(
  children: FamilyRow[],
  nameOf: (t: string) => string,
  actOf: ActivityOf = rawActivity,
  now: number = Date.now() / 1000
): { parts: RollupPart[]; title: string } | null {
  const n = children.length;
  if (!n) return null;
  const states = children.map((c) => workerState(c, actOf(c)));
  const count = (s: WorkerState) => states.filter((x) => x === s).length;
  const ask = count("ask");
  const failed = count("failed");
  const blocked = count("blocked");
  const reported = states.filter(isReported).length;
  const working = count("working");
  const parts: RollupPart[] = [];
  if (ask) parts.push({ text: ask + " needs you", cls: "needs" });
  if (failed) parts.push({ text: failed + " failed", cls: "bad" });
  if (blocked) parts.push({ text: blocked + " blocked", cls: "bad" });
  if (ask) parts.push({ text: plural(n, "worker"), cls: "" });
  else if (reported === n) parts.push({ text: n === 1 ? "worker reported" : "all " + n + " reported", cls: "ok" });
  else if (reported) parts.push({ text: reported + " of " + n + " reported", cls: "" });
  else if (working === n) parts.push({ text: n + " working", cls: "" });
  else if (working) parts.push({ text: working + " of " + n + " working", cls: "" });
  else parts.push({ text: plural(n, "worker") + " · no reports", cls: "" });
  const lines = children.map(
    (c) => nameOf(c.title) + " — " + workerLine(c, { nested: true, parentName: "", act: actOf(c), now }).text
  );
  return { parts, title: lines.join("\n") + "\nClick to open the Thread" };
}

/** The orchestrator's chip, in place of its normal stage chip while it sits
 * idle over a family:
 *  - "wrap up" (filled, clickable): every worker has reported — one click
 *    pastes the Wrap up prompt, and you press Enter;
 *  - "waiting" (dashed): it is idle but some worker hasn't reported.
 * null otherwise (working, on a prompt, paused…): the normal chip then says
 * what the orchestrator itself is doing, which matters more. */
export function parentChip(
  parent: FamilyRow,
  children: FamilyRow[],
  nameOf: (t: string) => string,
  actOf: ActivityOf = rawActivity,
  /** Why the orchestrator can't take a paste now (playbooks.forkBlockReason —
   * e.g. this launch has no MindFlock tools): the chip still says "wrap up",
   * but as a label with the reason, never a one-click paste. */
  blocked = ""
): { kind: "wrap" | "waiting" | "blocked"; label: string; cls: string; title: string } | null {
  const n = children.length;
  if (!n || actOf(parent) !== "idle") return null;
  if (parent.status && parent.status !== "running") return null;
  const name = nameOf(parent.title);
  const reported = children.filter((c) => isReported(workerState(c, actOf(c)))).length;
  if (reported === n && blocked)
    return {
      kind: "blocked",
      label: "wrap up",
      cls: "s-waiting",
      title: (n === 1 ? "Its worker has reported" : "All " + n + " workers reported") + " — " + blocked,
    };
  if (reported === n)
    return {
      kind: "wrap",
      label: "wrap up",
      cls: "wrapchip",
      title:
        (n === 1 ? "Its worker has reported" : "All " + n + " workers reported") +
        " — paste the Wrap up prompt into " + name +
        " (you press Enter; it merges, runs the tests, and asks before deleting)",
    };
  return {
    kind: "waiting",
    label: "waiting",
    cls: "s-waiting",
    title: name + " is idle — " + (n - reported) + " of " + n + " workers haven't reported yet",
  };
}

/** The live children of each session ({@link isChildOf}), in the order
 * given (rail order). A remote device's rows are left out (their `parent`
 * names a title on THAT device), and so is a pending one. */
export function childrenByParent<T extends FamilyRow & { device?: string; pending?: boolean }>(
  rows: T[]
): Map<string, T[]> {
  const live = new Set(rows.filter((r) => !r.device).map((r) => r.title));
  const out = new Map<string, T[]>();
  for (const r of rows) {
    const p = String(r.parent || "");
    if (!live.has(p) || !isChildOf(r, p)) continue;
    if (!out.has(p)) out.set(p, []);
    out.get(p)!.push(r);
  }
  return out;
}

/** Whether a session's prompts get the one-click answer strip (the rail row
 * and the bell): a live worker, a session with live workers, or one CREATED
 * as an orchestrator (`playbook`, e.g. "split" from the New dialog's "Split
 * across workers") — whose first spawn_session prompt, and anything it asks
 * before that, comes before its first child exists. */
export function inFamily(row: { playbook?: string }, isWorker: boolean, kids: number): boolean {
  return isWorker || kids > 0 || !!row.playbook;
}

/** The bell's lineage suffix on a worker's attention item: "· worker of api". */
export function workerOf(parent: string | undefined, nameOf: (t: string) => string): string {
  return parent ? "· worker of " + nameOf(parent) : "";
}

// --- Ship lanes on the rail -----------------------------------------------------
//
// Every session can say how far MindFlock carries it once its agent stops — its
// LANE: leave it / commit / push / open a PR / merge. The rail's status line
// leads with that lane ("→ PR · working 12m"), so a narrow rail cuts the
// detail and never the destination. Pure, like the rest of this module: it
// reads only row fields (`lane`, `autopilot`, `stage`, `pr_url`, `merge_state`)
// plus, for a session started in a group, its run task's state.

/** The row fields the ship line reads. */
export type ShipRow = FamilyRow &
  Partial<Pick<Instance, "lane" | "autopilot" | "stage" | "pr_url" | "merge_state" | "run">>;

/** The task fields the ship line reads (a run's view of this session). */
export interface ShipTask {
  state: string;
  reason?: string;
  /** Set while a merge-back conflict is handed to the lead. */
  conflict?: { files: string[] } | null;
}

/** How the lane names itself at the head of the line. */
export const LANE_HEAD: Record<string, string> = {
  leave: "agent only",
  commit: "→ commit",
  push: "→ push",
  pr: "→ PR",
  merge: "→ merge",
};

/** What each lane promises, for the tooltip. */
const LANE_MEANS: Record<string, string> = {
  leave: "MindFlock commits nothing for it",
  commit: "MindFlock commits it with a message written from the diff once its agent stops and your hooks pass",
  push: "MindFlock commits and pushes it once its agent stops and your hooks pass",
  pr: "MindFlock commits, pushes and opens its PR once its agent stops and your hooks pass",
  merge: "MindFlock commits, pushes, opens its PR and merges it once checks pass",
};

const LANE_RANK: Record<string, number> = { leave: 0, commit: 1, push: 2, pr: 3, merge: 4 };

/** The session's lane: the server's `lane` field, else (a server that predates
 * it) what an armed fast-track record implies. null = no lane. */
export function laneOf(
  row: Partial<Pick<Instance, "lane" | "autopilot">>
): { target: string; ask_first: boolean; owner?: string } | null {
  const l = row.lane;
  if (l && l.target && l.target in LANE_RANK) return l;
  const ap = row.autopilot;
  if (ap && ap.depth) {
    const target = ap.depth === "agent" ? "leave" : ap.depth;
    if (target in LANE_RANK) return { target, ask_first: false };
  }
  return null;
}

/** The lane's head phrase for a line that leads with it ("→ PR"), or "" when
 * the row has no lane or its lane ships nothing — a worker's status line
 * prefixes this (THE lane phrase is LANE_HEAD; nothing else spells it). */
export function laneLead(lane: { target?: string } | null | undefined): string {
  const t = String(lane?.target || "");
  if (!t || t === "leave") return "";
  return LANE_HEAD[t] || "→ " + t;
}

const LANE_DEPTH_RANK: Record<string, number> = { agent: 0, commit: 1, push: 2, pr: 3, merge: 4 };

/** Whether an "asks first" lane is parked at its held rung waiting for your go
 * (server: lanes.awaiting_approval): its fast-track finished short of the
 * lane. A grouped line says the same through its task (`needs_you`/`approve`). */
export function awaitingApproval(row: ShipRow): boolean {
  const lane = laneOf(row);
  const ap = row.autopilot;
  if (!lane || !lane.ask_first || !ap || ap.state !== "done") return false;
  return (LANE_DEPTH_RANK[ap.depth] ?? 0) < (LANE_RANK[lane.target] ?? 0);
}

/** The PR number off the merge lookup, else off the URL. "" when unknown. */
export function prNumber(row: ShipRow): string {
  const n = row.merge_state?.number;
  if (n) return String(n);
  const m = String(row.pr_url || "").match(/\/pull\/(\d+)/);
  return m ? m[1] : "";
}

/** "checks ✓" / "checks ✗" / "checks…" for an open PR, "" when unknown. */
export function checksText(row: ShipRow): { text: string; bad: boolean } {
  const c = String(row.merge_state?.checks || "");
  if (c === "ok") return { text: "checks ✓", bad: false };
  if (c === "failed") return { text: "checks ✗", bad: true };
  if (c === "pending") return { text: "checks…", bad: false };
  return { text: "", bad: false };
}

/** Which lane the session's git stage already satisfies (0 = none). */
function stageRank(stage: string): number {
  return stage === "committed" ? 1 : stage === "pushed" ? 2 : stage === "pr" ? 3 : 0;
}

/** A run escalation, said in a few words (the Outbox has the full sentence). */
export function escalationText(reason: string): string {
  const r = String(reason || "").trim();
  switch (r) {
    case "stuck":
      return "stalled twice — no diff, no report";
    case "blocked":
      return "its agent reported blocked";
    case "ship_halted":
      return "hooks failed twice";
    case "conflict":
      return "merge conflict";
    case "budget":
      return "the group's budget is used up";
    case "restart":
      return "couldn't pick it back up after a restart";
    case "":
      return "needs you";
    default:
      return r;
  }
}

/** One rail status line in two parts: `lead` (never truncated — the lane, or
 * the state that replaces it) and `rest` (the detail a narrow rail cuts). */
export interface ShipLine {
  lead: string;
  rest: string;
  cls: string;
  /** Extra class for `rest` alone ("bad" for a red checks mark). */
  restCls: string;
  title: string;
  state: "ask" | "escalated" | "approve" | "shipped" | "shipping" | "working" | "limit" | "idle";
}

/** The rail's status line for a session with a lane (or in a group), or null
 * for a session MindFlock isn't carrying anywhere — that row keeps today's
 * line. Most urgent first:
 *  - `? needs your answer` (gold): its agent is on a prompt;
 *  - `! hooks failed twice — open the Outbox` (red): a run escalation, or a
 *    fast-track that stopped;
 *  - `✓ PR #318 · checks ✓` (green): the lane is reached;
 *  - `⇡ opening PR` (accent): MindFlock is carrying it now;
 *  - `→ commit, asks first · ready — see the Outbox`: waiting on your OK;
 *  - `→ PR · working 12m` / `· idle` / `· usage limit`: the agent's turn. */
export function shipLine(row: ShipRow, opts: { act?: string; now?: number; task?: ShipTask | null } = {}): ShipLine | null {
  const lane = laneOf(row);
  if (!lane && !row.run) return null;
  const target = lane?.target || "leave";
  if (target === "leave" && !row.run) return null;
  const act = opts.act ?? rawActivity(row);
  const now = opts.now ?? Date.now() / 1000;
  const task = opts.task || null;
  const ap = row.autopilot || null;
  const stage = String(row.stage || "");
  const head = (LANE_HEAD[target] || "→ " + target) + (lane?.ask_first ? ", asks first" : "");
  const copy = lane?.owner && lane.owner !== row.title ? "\nThis window shares its branch with “" + lane.owner + "”, which carries it." : "";
  const means = (LANE_MEANS[target] || "") + (lane?.ask_first ? ", and shows it to you in the Outbox before anything leaves this machine" : "");
  const base = { restCls: "", title: (means ? "Lane: " + means + "." : "") + copy };
  const line = (lead: string, rest: string, cls: string, state: ShipLine["state"], why = ""): ShipLine => ({
    ...base,
    lead,
    rest,
    cls,
    state,
    title: (why ? why + "\n" : "") + base.title,
  });

  if (act === "clarify") return line("? needs your answer", "", "rep-ask", "ask", "Its agent is waiting on a prompt — answer it here, in the Outbox, or in its pane.");
  const tstate = String(task?.state || "");
  const reason = String(task?.reason || "");
  if (tstate === "needs_you" && reason && reason !== "prompt" && reason !== "approve")
    return line("! " + escalationText(reason), " — open the Outbox", "rep-blocked", "escalated", "MindFlock stopped and needs you: " + escalationText(reason) + ".");
  if (tstate === "failed")
    return line("! failed", reason ? " — " + escalationText(reason) : " — open the Outbox", "rep-blocked", "escalated", "This line failed" + (reason ? ": " + reason : "") + ".");
  if (ap && ap.state === "halted")
    return line("! fast-track stopped", ap.reason ? " — " + ap.reason : "", "rep-blocked", "escalated", "Shipping stopped" + (ap.reason ? ": " + ap.reason : "") + ".");

  // A one-for-all / split line merges back into its lead's branch: that IS
  // its outcome (its own lane is only "commit"), so say so before the lane.
  if (tstate === "integrated") return line("✓ merged back", "", "rep-done", "shipped", "Merged back into its lead's branch.");
  if (tstate === "integrating" && (reason === "conflict" || !!task?.conflict))
    return line(
      "! conflict",
      " — open the Thread",
      "rep-ask",
      "shipping",
      "Merging it back conflicted — its lead is resolving it; the lead's Thread shows where it is."
    );
  if (tstate === "integrating")
    return line("⇄ merging", "", "rep-ship", "shipping", "MindFlock is merging it back into its lead's branch.");

  // The lane is reached: say what it produced.
  const rank = LANE_RANK[target] ?? 0;
  // A one-for-all / split member's own commit is not its outcome — merging
  // back is — so its git stage never says it is done; only its task does.
  const merges = row.run?.grouping === "together" && row.run?.role !== "lead";
  const reached =
    tstate === "shipped" ||
    tstate === "integrated" ||
    (rank > 0 && target !== "merge" && !merges && stageRank(stage) >= rank) ||
    (target === "merge" && !!ap && ap.state === "done" && ap.step === "merge");
  if (reached && rank > 0) {
    if (target === "pr" || target === "merge") {
      const n = prNumber(row);
      const pr = n ? "PR #" + n : "PR";
      if (target === "merge" && ap?.state === "done" && ap.step === "merge")
        return line("✓ merged", n ? " · " + pr : "", "rep-done", "shipped", "Merged.");
      const ck = checksText(row);
      return { ...line("✓ " + pr, ck.text ? " · " + ck.text : "", "rep-done", "shipped", "Its PR is open."), restCls: ck.bad ? "bad" : "" };
    }
    return line(target === "push" ? "✓ pushed" : "✓ committed", "", "rep-done", "shipped", "Done: its lane ends at " + target + ".");
  }

  // MindFlock is carrying it now: the next outward step, by what git says.
  const moving =
    tstate === "shipping" ||
    tstate === "integrating" ||
    (!!ap && ap.state === "running" && act !== "working" && !!ap.step && ap.step !== "agent");
  if (moving && rank > 0) {
    const s = stageRank(stage);
    const note = String(ap?.note || "");
    const verb =
      ap?.step === "check" || /\bcheck/i.test(note)
        ? "running checks"
        : tstate === "integrating"
          ? "merging back"
          : s === 0
            ? "committing"
            : s === 1
              ? "pushing"
              : s === 2
                ? "opening PR"
                : "merging";
    return line("⇡ " + verb, "", "rep-ship", "shipping", note ? "MindFlock: " + note : "MindFlock is shipping it.");
  }
  if ((tstate === "needs_you" && reason === "approve") || (!tstate && awaitingApproval(row)))
    return line(head, " · ready — see the Outbox", "rep-ask", "approve", "It stopped where you asked: the Outbox shows the commit message and PR before anything is pushed.");

  let rest: string;
  let state: ShipLine["state"] = "idle";
  if (act === "working") {
    const t = Number(row.activity_since) || 0;
    rest = t > 0 ? " · working " + since(t, now) : " · working";
    state = "working";
  } else if (act === "limit") {
    rest = " · usage limit — waiting";
    state = "limit";
  } else if (act === "offline") rest = " · offline";
  else rest = " · idle";
  return line(head, rest, "rep-lane", state);
}
