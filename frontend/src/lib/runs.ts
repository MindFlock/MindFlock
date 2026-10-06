/** Ship lanes on the rail: sessions started together sit under a group header.
 *
 * Pure (no DOM, no store) so the grouping and the header arithmetic are
 * unit-tested in node. Sidebar hands in the rail entries it already built —
 * saved order, filter and windows applied — and renders exactly what comes
 * back, publishing the SAME keys as `railOrder`.
 *
 * The invariants (the rail-unification contract):
 *  - a group header and a queued line are NOT rail keys: they never enter the
 *    saved order or `railOrder`, carry no number and are no drop target;
 *  - grouping only re-sequences rows that are already on the rail, and the
 *    published `railOrder` is the rendered sequence, so Alt+N still lands on the
 *    row whose badge says N;
 *  - folding a group drops its rows from the published keys — the same path a
 *    folded device section takes — so numbering skips what you can't see.
 *
 * A header appears only for a group of 2 or more (counting lines still
 * queued); a one-off session never gets one, and neither does a split or a
 * one-for-all group — that is a family under its lead. "On their own" appears
 * only under at least one header. */

import type { Instance, RunDTO, RunSummary, RunTask } from "../api/types";
import { escalationText, LANE_HEAD, shipLine } from "./agentMessages";

export const RUN_TERMINAL_TASK = new Set(["shipped", "integrated", "failed", "cancelled", "skipped"]);
export const RUN_DONE_STATES = new Set(["done", "done_with_failures", "cancelled"]);

/** How long a FINISHED one-for-all / split group keeps its details on the rail. */
export const TOGETHER_DETAIL_S = 7 * 86400;

/** Whether the rail needs a run's full record (tasks, release), not just its
 * summary: every group still moving, and a one-for-all / split group that
 * finished in the last week — its lead and pieces stay on the rail as a
 * family, and their lines ("✓ merged back", "✓ PR #N", "⇡ pushed — open the
 * PR") are read from its tasks and its release. */
export function needsRunDetail(r: Pick<RunSummary, "state" | "policy" | "created_at">, nowS: number): boolean {
  if (!RUN_DONE_STATES.has(r.state)) return true;
  return r.policy?.grouping === "together" && nowS - (Number(r.created_at) || 0) <= TOGETHER_DETAIL_S;
}

/** A rail entry as Sidebar builds it: a session row or a window row. */
export interface RailEntry {
  key: string;
  inst?: Instance;
}

/** One run as the rail reads it: the summary, plus its tasks when fetched. */
export type RunInfo = RunSummary & Partial<Omit<RunDTO, keyof RunSummary>> & { tasks?: RunTask[] };

export interface RunGroup<E extends RailEntry = RailEntry> {
  id: string;
  name: string;
  /** The group's lane head ("→ PR"), "" when unknown. */
  lane: string;
  state: string;
  paused: boolean;
  pauseReason: string;
  /** Finished (done, done with failures, cancelled). */
  done: boolean;
  /** Finished because you cancelled it — never drawn with a ✓. */
  cancelled: boolean;
  /** A member hit the usage limit, or the server paused the group for it. */
  waitingUsage: boolean;
  collapsed: boolean;
  entries: E[];
  /** Lines still waiting for a slot, in the order they will start. */
  queued: RunTask[];
  needs: number;
  shipped: number;
  failed: number;
  total: number;
  /** The run's task for a member title (escalations, approvals). */
  taskOf: Map<string, RunTask>;
}

export interface RailSplit<E extends RailEntry = RailEntry> {
  groups: RunGroup<E>[];
  /** One-for-all / split FAMILIES (a lead and its workers), in rail order:
   * no header of their own, and never under "On their own" — they are a group. */
  families: E[];
  /** Everything else, in rail order. */
  own: E[];
}

/** Split the rail into run groups and the rest.
 *
 * `runs` may be empty (an older server, or the first fetch still in flight): a
 * row's own `run` field then still groups it, named from the row. `act` is the
 * live activity (`effectiveActivity` in the app). */
export function splitRail<E extends RailEntry>(
  entries: E[],
  runs: RunInfo[],
  opts: { collapsed?: Set<string>; act?: (inst: Instance) => string; filtering?: boolean } = {}
): RailSplit<E> {
  const act = opts.act || ((i: Instance) => String(i.activity || "idle"));
  const byId = new Map(runs.map((r) => [r.id, r]));
  const members = new Map<string, E[]>();
  const firstAt = new Map<string, number>();
  const named = new Map<string, string>();
  entries.forEach((e, i) => {
    const run = e.inst && !e.inst.device ? e.inst.run : null;
    if (!run || !run.id) return;
    if (!members.has(run.id)) members.set(run.id, []);
    members.get(run.id)!.push(e);
    if (!firstAt.has(run.id)) firstAt.set(run.id, i);
    if (run.name && !named.has(run.id)) named.set(run.id, run.name);
  });
  const ids = [...new Set([...members.keys(), ...runs.map((r) => r.id)])];
  const groups: RunGroup<E>[] = [];
  for (const id of ids) {
    const info = byId.get(id) || null;
    const mine = members.get(id) || [];
    // A split / one-for-all group is a FAMILY on the rail (its lead with its
    // workers nested under it, SPEC §7.C.4 "a split is still a family"): its
    // lead's row says how it is going, so it never gets a header too.
    if (
      info?.policy?.grouping === "together" ||
      info?.split ||
      mine.some((e) => e.inst?.run?.role === "lead" || e.inst?.run?.grouping === "together")
    )
      continue;
    const tasks = info?.tasks || [];
    const done = !!info && RUN_DONE_STATES.has(info.state);
    const queued = done ? [] : tasks.filter((t) => t.state === "queued");
    if (!mine.length && !queued.length) continue;
    // Narrowed by the filter: a group with nothing left on screen goes too.
    if (opts.filtering && !mine.length) continue;
    const total = Math.max(info?.counts?.total || 0, tasks.length, mine.length + queued.length);
    if (total < 2) continue;
    const taskOf = new Map<string, RunTask>();
    for (const t of tasks) if (t.title) taskOf.set(t.title, t);
    const needsTitles = new Set<string>();
    let limited = false;
    for (const e of mine) {
      const a = act(e.inst!);
      if (a === "clarify") needsTitles.add(e.key);
      if (a === "limit") limited = true;
    }
    for (const t of tasks) if (t.state === "needs_you") needsTitles.add(t.title || t.id);
    // Shipped: the server's count when it has one, else what the rows say.
    const localShipped = mine.filter(
      (e) => shipLine(e.inst!, { act: act(e.inst!), task: taskOf.get(e.key) })?.state === "shipped"
    ).length;
    const shipped = info?.counts ? Math.max(info.counts.shipped || 0, 0) : localShipped;
    const failed = info?.counts?.failed || tasks.filter((t) => t.state === "failed").length;
    const lane = info?.policy?.lane ? LANE_HEAD[info.policy.lane] || "→ " + info.policy.lane : "";
    groups.push({
      id,
      name: info?.name || named.get(id) || "Group",
      lane,
      state: info?.state || "running",
      paused: !!info?.paused,
      pauseReason: info?.pause_reason || "",
      done,
      cancelled: info?.state === "cancelled",
      waitingUsage: limited || info?.pause_reason === "limit" || !!info?.waiting_for_usage,
      collapsed: !!opts.collapsed?.has(id),
      entries: mine,
      queued,
      needs: needsTitles.size,
      shipped,
      failed,
      total,
      taskOf,
    });
  }
  // Groups sit where their first member sits on the rail; a group whose lines
  // are all still queued follows, oldest first.
  const at = (g: RunGroup<E>) => firstAt.get(g.id) ?? Number.MAX_SAFE_INTEGER;
  const created = (g: RunGroup<E>) => byId.get(g.id)?.created_at || 0;
  groups.sort((a, b) => at(a) - at(b) || created(a) - created(b));
  const grouped = new Set(groups.flatMap((g) => g.entries.map((e) => e.key)));
  const rest = entries.filter((e) => !grouped.has(e.key));
  const family = (e: E) => {
    const run = e.inst && !e.inst.device ? e.inst.run : null;
    return !!run && (run.role === "lead" || run.grouping === "together");
  };
  return { groups, families: rest.filter(family), own: rest.filter((e) => !family(e)) };
}

/** The keys the rail shows, in the order it shows them — what Sidebar
 * publishes as `railOrder`. A folded group contributes nothing; headers and
 * queued lines are never keys. */
export function splitKeys(split: RailSplit): string[] {
  const out: string[] = [];
  for (const g of split.groups) if (!g.collapsed) for (const e of g.entries) out.push(e.key);
  for (const e of split.families || []) out.push(e.key);
  for (const e of split.own) out.push(e.key);
  return out;
}

/** "queued · next free slot", "queued · 2nd", "queued · 3rd"… */
export function queuedLine(i: number): string {
  if (i <= 0) return "queued · next free slot";
  const n = i + 1;
  const suf = n % 10 === 2 && n % 100 !== 12 ? "nd" : n % 10 === 3 && n % 100 !== 13 ? "rd" : "th";
  return "queued · " + n + suf;
}

/** A queued line's title: its ticket ref, then its text ("PAY-421 Upgrade to
 * Stripe…"); a typed task is just its text. */
export function queuedTitle(t: Pick<RunTask, "ticket_id" | "text" | "title">): string {
  const ref = String(t.ticket_id || "").trim();
  const text = String(t.text || "").trim();
  if (ref && text) return ref + " " + text;
  return ref || text || t.title || "queued line";
}

/** The header's "N/M shipped" badge text (and "· K failed" once finished). */
export function shippedBadge(g: Pick<RunGroup, "shipped" | "total">): string {
  return g.shipped + "/" + g.total + " shipped";
}

/** The header's tooltip: the whole state in one sentence per fact. */
export function groupTitle(g: RunGroup): string {
  const bits = [
    g.name + (g.lane ? " — each line goes " + g.lane.replace(/^→\s*/, "to ") : ""),
    shippedBadge(g) + (g.failed ? ", " + g.failed + " failed" : ""),
  ];
  if (g.needs) bits.push(g.needs + (g.needs === 1 ? " needs" : " need") + " you");
  if (g.queued.length) bits.push(g.queued.length + " queued — they start as slots free");
  if (g.cancelled) bits.push("cancelled — its sessions and branches were kept");
  else if (g.paused)
    bits.push(
      g.pauseReason === "budget"
        ? "paused: the budget is used up"
        : g.pauseReason === "limit"
          ? "paused: waiting for usage to come back"
          : "paused — nothing new starts and nothing ships"
    );
  else if (g.waitingUsage) bits.push("waiting for usage to come back");
  bits.push("Click to fold");
  return bits.join("\n");
}

// --- Run events → the bell and the toasts (SPEC §8) ---------------------------
//
// The server emits each of these ONCE (per run/task/reason/incarnation) from
// one emitter; the client's only jobs are to phrase them, keep the bell's
// third channel behind the same rule switches, and not double a row a replay
// brings back under a new seq (`dedupe`).

export interface RunNote {
  text: string;
  cls: string;
  /** The run the row is about — a click reveals its group header on the rail. */
  run: string;
  /** Same fact = same key: a replayed or re-sent event never adds a row. */
  dedupe: string;
  /** The notify rule that gates it ("" = bell-only, always shown). */
  rule: string;
  /** A split / one-for-all group's lead, when the row is about ITS click (the
   * plan to approve, the PR to open): the row opens the lead's Thread. */
  lead?: string;
}

/** What a group-level `run.needs_you` says when the server sent no sentence. */
const LEAD_ASKS: Record<string, string> = {
  plan: "the lead proposed the pieces — approve them",
  release: "one PR is ready to open",
  check_failed: "the check failed on the merged branch",
  lead_gone: "its lead is gone — nothing can merge or ship",
  stray: "changes no piece owns in the lead's folder — commit or discard them yourself",
};

const num = (v: unknown) => Number(v) || 0;
const str = (v: unknown) => (v == null ? "" : String(v));

/** A `run.*` event as one bell / toast line, or null to stay silent.
 * `run.needs_you` with reason "prompt" is never announced here: the session's
 * own "needs your input" (activity → clarify) already said it. */
export function runNote(
  event: string,
  data: Record<string, unknown> | null | undefined,
  info: { name?: (id: string) => string; lane?: (id: string) => string } = {}
): RunNote | null {
  const d = data || {};
  const run = str(d.run);
  const name = str(d.name) || info.name?.(run) || "A group";
  const who = str(d.ref) || str(d.title) || str(d.task) || "a line";
  switch (event) {
    case "run.needs_you": {
      const reason = str(d.reason);
      if (reason === "prompt") return null;
      if (reason in LEAD_ASKS) {
        const lead = str(d.title) || str(d.session);
        return {
          text: name + ": " + (str(d.text) || LEAD_ASKS[reason]),
          cls: reason === "check_failed" || reason === "lead_gone" || reason === "stray" ? "n-warn" : "n-info",
          run,
          // The server's own announce key names the ROUND (plan round 2, a
          // new release head, a later check failure): each is a new ask.
          dedupe: d.key ? "needs:" + str(d.key) : ["needs", run, reason, str(d.round) || str(d.incarnation)].join(":"),
          rule: "run_needs_you",
          lead: reason === "check_failed" || reason === "lead_gone" ? undefined : lead || undefined,
        };
      }
      return {
        text: name + ": " + who + " " + (str(d.text) || escalationText(reason)),
        cls: "n-warn",
        run,
        dedupe: d.key ? "needs:" + str(d.key) : ["needs", run, str(d.task), reason, str(d.incarnation)].join(":"),
        rule: "run_needs_you",
      };
    }
    case "run.finished": {
      const shipped = num(d.shipped);
      const failed = num(d.failed);
      const lane = info.lane?.(run) || "";
      const word = lane === "pr" || lane === "merge" ? (shipped === 1 ? " PR" : " PRs") : " shipped";
      // The server says what the group did (a one-for-all group ships ONE
      // branch: "one PR opened", "its branch was pushed …"); an older server
      // sends no outcome and gets the per-line count.
      const what = str(d.outcome) || shipped + word;
      return {
        text: name + " finished — " + what + (failed ? ", " + failed + " failed" : ""),
        cls: failed ? "n-warn" : "n-done",
        run,
        dedupe: "finished:" + run,
        rule: "run_finished",
      };
    }
    case "run.task_shipped": {
      const m = str(d.pr_url).match(/\/pull\/(\d+)/);
      return {
        text: name + ": " + who + " shipped" + (m ? " — PR #" + m[1] : ""),
        cls: "n-done",
        run,
        dedupe: ["shipped", run, str(d.task)].join(":"),
        rule: "",
      };
    }
    default:
      return null;
  }
}

/** Where a group event lands when the group has no header on the rail.
 *
 * A split or one-for-all group never gets one (it is a family under its lead),
 * nor does a group of one, nor a finished group whose rows were all closed. Its
 * finish toast / bell row then goes to the lead's Thread tab (`lead`) when the
 * lead is still on the rail — that is where its pieces, its release and its
 * "Copy summary" live — else to any member row still open (`row`); null when
 * nothing of the group is left to show. */
export function groupLanding(
  runId: string | null | undefined,
  runs: readonly RunInfo[] | null | undefined,
  rows: readonly Instance[] | null | undefined
): { lead: string } | { row: string } | null {
  if (!runId) return null;
  const live = (rows || []).filter((r) => !r.device);
  const has = (t: string) => !!t && live.some((r) => r.title === t);
  const lead = (runs || []).find((r) => r.id === runId)?.lead?.title || "";
  if (has(lead)) return { lead };
  const mine = live.filter((r) => r.run?.id === runId);
  const byRole = mine.find((r) => r.run?.role === "lead");
  if (byRole) return { lead: byRole.title };
  return mine.length ? { row: mine[0].title } : null;
}
