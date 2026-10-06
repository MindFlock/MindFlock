/** The order diagram on a Thread: in what order an orchestrator's workers
 * run (core.worker_order), and — for a split's lead — the stages its group
 * goes through. Pure: the shapes, the words and the tones; ThreadTab and
 * RunLeadPanel draw them (OrderDiagram.tsx).
 *
 * Read left to right: each step's cards run TOGETHER, and a step starts once
 * every card of the step before it is done. No lines between cards (the
 * steps' columns say the order); "why" a card waits is a line of text on it. */

import type { OrderCard, RunDTO, WorkerFence, WorkerOrder } from "../api/types";

const obj = (v: unknown): Record<string, unknown> => (v && typeof v === "object" ? (v as Record<string, unknown>) : {});
const str = (v: unknown): string => (typeof v === "string" ? v : "");
const strs = (v: unknown): string[] => (Array.isArray(v) ? v.filter((x): x is string => typeof x === "string") : []);
const num = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null);

export function normFence(v: unknown): WorkerFence | null {
  if (!v || typeof v !== "object") return null;
  const o = obj(v);
  const only = strs(o.only);
  const keep_out = strs(o.keep_out);
  if (!only.length && !keep_out.length) return null;
  return { only, keep_out, reason: str(o.reason), by: str(o.by) };
}

function normCard(v: unknown): OrderCard | null {
  const o = obj(v);
  const title = str(o.title);
  if (!title) return null;
  const why: Record<string, string> = {};
  for (const [k, w] of Object.entries(obj(o.why))) if (typeof w === "string") why[k] = w;
  return {
    title,
    state: str(o.state) || "held",
    word: str(o.word),
    detail: str(o.detail),
    after: strs(o.after),
    why,
    fence: normFence(o.fence),
    released_at: num(o.released_at),
    ended_at: num(o.ended_at),
    planned: o.planned === true,
  };
}

/** A thread's / `GET …/order`'s `order`, shape-checked; null for none. */
export function normOrder(v: unknown): WorkerOrder | null {
  if (!v || typeof v !== "object") return null;
  const o = obj(v);
  const steps = (Array.isArray(o.steps) ? o.steps : [])
    .map((s, i) => {
      const so = obj(s);
      const workers = (Array.isArray(so.workers) ? so.workers : []).map(normCard).filter((c): c is OrderCard => !!c);
      return { n: num(so.n) ?? i + 1, workers };
    })
    .filter((s) => s.workers.length > 0);
  if (!steps.length) return null;
  return {
    mode: str(o.mode) || "parallel",
    max_parallel: num(o.max_parallel) ?? 0,
    cap: num(o.cap) ?? 0,
    steps,
  };
}

/** The one-line rule the order follows: "One at a time", "Up to 2 at a
 * time", "In 3 steps", "All at once". */
export function modeLine(o: WorkerOrder): string {
  const parts: string[] = [];
  if (o.mode === "serial") parts.push("One at a time");
  else if (o.cap > 0) parts.push("Up to " + o.cap + " at a time");
  if (o.steps.length > 1 && o.mode !== "serial") parts.push("in " + o.steps.length + " steps");
  if (!parts.length) parts.push("All at once");
  const s = parts.join(", ");
  return s.charAt(0).toUpperCase() + s.slice(1);
}

/** The dot's tone, in the Thread's palette: ok (green), work (accent), bad
 * (red), idle (muted). */
export function cardTone(c: Pick<OrderCard, "state">): "ok" | "work" | "bad" | "idle" {
  if (c.state === "done") return "ok";
  if (c.state === "running") return "work";
  if (c.state === "stopped") return "bad";
  return "idle";
}

/** Why a card runs after the ones it waits on, for the reasons that are not
 * already obvious from the steps: "same files as w1 (src/api.py)". */
export function overlapLines(c: Pick<OrderCard, "why">, nameOf: (t: string) => string = (t) => t): string[] {
  const out: string[] = [];
  for (const [t, w] of Object.entries(c.why || {})) {
    if (w.startsWith("overlap: ")) out.push("same files as " + nameOf(t) + " (" + w.slice(9) + ")");
  }
  return out;
}

/** The fence as chips: "only src/api/**" (green) and "⛔ db/" (red). */
export function fenceChips(f: WorkerFence | null | undefined): Array<{ kind: "only" | "out"; text: string }> {
  if (!f) return [];
  return [
    ...f.only.map((p) => ({ kind: "only" as const, text: "only " + p })),
    ...f.keep_out.map((p) => ({ kind: "out" as const, text: "⛔ " + p })),
  ];
}

/** A step's caption: "Step 1" plus how its cards run. */
export function stepCaption(i: number, cards: number, o: WorkerOrder): string {
  const base = "Step " + (i + 1);
  if (cards < 2) return base;
  if (o.cap > 0 && cards > o.cap) return base + " · " + o.cap + " at a time";
  return base + " · together";
}

/** Whether the diagram says anything a plain worker list doesn't: more than
 * one step, a cap, a fence, or a card waiting its turn. */
export function orderWorthShowing(o: WorkerOrder | null | undefined): o is WorkerOrder {
  if (!o || !o.steps.length) return false;
  if (o.steps.length > 1 || o.cap > 0 || o.mode === "serial") return true;
  return o.steps.some((s) => s.workers.some((c) => !!c.fence || c.state === "held" || c.planned));
}

// --- A split's group: its stages ------------------------------------------------------

export type StageState = "done" | "now" | "next" | "bad";

export interface Stage {
  key: string;
  label: string;
  /** How its items run: "together", "one at a time" … */
  how: string;
  state: StageState;
  detail: string;
}

const TERMINAL_OK = new Set(["shipped", "integrated"]);
const TERMINAL = new Set(["shipped", "integrated", "failed", "cancelled", "skipped"]);

/** The stages a split / one-for-all group runs through, left to right:
 * Plan → Pieces (together, up to N) → Merge back / Commit each (one at a
 * time) → Check → One PR. */
export function runStages(
  run: Pick<RunDTO, "state" | "split" | "mode" | "concurrency" | "tasks" | "plan" | "check" | "release">
): Stage[] {
  const tasks = (run.tasks || []).filter((t) => t.state !== "cancelled" || !!t.title);
  const n = tasks.length;
  const sf = run.mode === "same_folder";
  const st = run.state;
  const planning = st === "planning" || st === "plan_ready";
  const out: Stage[] = [];
  if (run.split) {
    const pieces = run.plan?.pieces?.length || 0;
    out.push({
      key: "plan",
      label: "Plan",
      how: "the lead proposes, you approve",
      state: st === "planning" ? "now" : st === "plan_ready" ? "now" : "done",
      detail: st === "planning" ? "the lead is reading the code" : st === "plan_ready" ? pieces + " pieces — waiting for you" : pieces ? pieces + " pieces" : "",
    });
  }
  const working = tasks.filter((t) => !TERMINAL.has(t.state) && t.state !== "queued" && t.state !== "integrating").length;
  const queued = tasks.filter((t) => t.state === "queued").length;
  const finishedWork = tasks.filter((t) => TERMINAL.has(t.state) || t.state === "integrating").length;
  const failed = tasks.filter((t) => t.state === "failed").length;
  const cap = Number(run.concurrency) || 0;
  out.push({
    key: "pieces",
    label: run.split ? "Pieces" : "Lines",
    how: sf ? "together, in this folder" : cap && n > cap ? "together, " + cap + " at a time" : "together",
    state: planning ? "next" : failed ? "bad" : n && finishedWork === n ? "done" : n ? "now" : "next",
    detail: n
      ? [finishedWork + " of " + n + " done", working ? working + " working" : "", queued ? queued + " queued" : ""]
          .filter(Boolean)
          .join(" · ")
      : "",
  });
  const merged = tasks.filter((t) => TERMINAL_OK.has(t.state)).length;
  const merging = tasks.filter((t) => t.state === "integrating").length;
  out.push({
    key: "merge",
    label: sf ? "Commit each" : "Merge back",
    how: "one at a time",
    state: planning || !n ? "next" : merged === n ? "done" : merging || merged ? "now" : "next",
    detail: n && !planning ? merged + " of " + n + (sf ? " committed" : " merged") : "",
  });
  const check = run.check?.state || "none";
  out.push({
    key: "check",
    label: "Check",
    how: "the whole branch",
    state: check === "ok" || check === "skipped" ? "done" : check === "failed" ? "bad" : check === "running" || check === "pending" ? "now" : "next",
    detail: check === "ok" ? run.check?.summary || "passed" : check === "failed" ? run.check?.summary || "failed" : check === "skipped" ? "no check command" : "",
  });
  const rel = run.release?.state || "none";
  out.push({
    key: "release",
    label: "One PR",
    how: "you release it",
    state: rel === "done" || rel === "handoff" ? "done" : rel === "failed" ? "bad" : rel === "releasing" || st === "release_ready" ? "now" : "next",
    detail: rel === "done" ? (run.release?.pr_url ? "opened" : "released") : st === "release_ready" ? "waiting for you" : "",
  });
  return out;
}
