/** "What needs me?" — THE one Needs-you list. The bell's badge and popover,
 * the Sky's Needs you strip and panel (flights/SkyDrawer), the top bar's Sky
 * badge, Ctrl+K N and the palette's empty query all read `needsYouRows`, so
 * no two of them can ever disagree. The phone page (static/mobile.js, which
 * can't import TS) mirrors the same rule — `NEEDS_YOU_KINDS` there is this
 * file's list, kept in step by tests/unit/test_mobile_needs_you.py.
 *
 * What is in it, in this order (`NEEDS_YOU_KINDS`):
 *  - asking      a session whose agent asks you something (a dialog, or a
 *                question in plain words)
 *  - approval    the server's waiting rows: an approval you asked for, an
 *                escalation (a group's plan, its one PR, a budget, a conflict…)
 *  - wait_for_me a route waiting on you at a Wait for me stop (or a Verify
 *                stop whose behaviors wait on you)
 *  - halted      a route halted at a stop until you Retry or Skip it
 *  - plan        a plan waiting for Go (the Map's Plan section)
 *  - broken      pre-commit / setup failed, offline, stuck
 *  - checks      checks failing
 *  - behaviors   behaviors that failed or wait on you
 *  - ready       pushed — ready for PR
 * One row per session (its most pressing reason); a row about no session (a
 * group's budget, a route with no session, behaviors) keeps its own key. */

import { useMemo } from "react";
import type { Instance, OutboxResponse, OutboxWaiting, TestPlan, TestPlansResponse } from "../api/types";
import { queryClient, useFlights, useInstances, useTestPlans } from "../state/queries";
import { useOutbox } from "../state/runs";
import { attentionItems } from "../components/sidebar/ordering";
import { needsAttention, withPlanReady, type NeedsRow } from "../components/outbox/outbox";
import { isFailing, isWaitingOnYou } from "../components/dialogs/verify";
import { selectSession } from "./sessionActions";
import { windowName } from "./windowName";
import { toast } from "./toast";
import { perchedOnYou, rowWaitsOnYou, type Flight } from "./flights";

/** The kinds of the one list, in its order. mobile.js `NEEDS_YOU_KINDS`
 * names the same kinds in the same order. */
export const NEEDS_YOU_KINDS = [
  "asking",
  "approval",
  "wait_for_me",
  "halted",
  "plan",
  "broken",
  "checks",
  "behaviors",
  "ready",
] as const;
export type NeedsYouKind = (typeof NEEDS_YOU_KINDS)[number];

/** A waiting row MindFlock makes itself for a route: which flight and which
 * stop, so its Done / Retry / Skip work without the session row (a route
 * with no session has none). */
export type FlightWaiting = OutboxWaiting & { flight?: { id: string; step_id: string; step_title: string } };

/** One row of the Needs-you list: the bell's row (`NeedsRow`) plus what it
 * is, the route behind it when the full flight is known (the Sky's panel
 * draws its stop block), and the behaviors it is about. */
export interface NeedsYouRow extends NeedsRow {
  kind: NeedsYouKind;
  flight?: Flight;
  behaviors?: TestPlan;
}

/** What the list is made from — the caches every surface already reads. */
export interface NeedsYouSources {
  instances: readonly Instance[];
  /** `GET /api/outbox` → `groups.waiting`. */
  waiting?: readonly OutboxWaiting[] | null;
  /** `GET /api/flights` (null: this server has no routes). */
  flights?: readonly Flight[] | null;
  /** `GET /api/test-plans` → `plans`. */
  testPlans?: readonly TestPlan[] | null;
}

/** The server's waiting rows plus a `flight_you` row for every session whose
 * flight is perched on you (a `you` stop) and that the outbox doesn't list yet (it
 * lists them once its notify hook has run; the session row says so first).
 * The bell draws these with Done / Skip (outbox/WaitingRow), so a you step can
 * be ticked off from wherever you are. */
export function withFlightYou(
  waiting: readonly OutboxWaiting[] | undefined | null,
  instances: readonly Instance[]
): OutboxWaiting[] {
  const out = (waiting || []).slice();
  const listed = new Set(out.filter((w) => w.kind === "flight_you").map((w) => w.title));
  for (const inst of instances) {
    const fl = inst.flight;
    if (!rowWaitsOnYou(fl) || listed.has(inst.title)) continue;
    out.push({
      key: "flight:" + fl!.id,
      title: inst.title,
      kind: "flight_you",
      reason: perchedOnYou(fl!.step.title),
      text: fl!.plan_name,
      actions: ["done", "skip", "open"],
    });
  }
  return out;
}

function flightRow(kind: "flight_you" | "flight_halted", title: string, f: {
  id: string;
  plan_name: string;
  reason?: string;
  step?: { id: string; title: string } | null;
}): FlightWaiting {
  const stepTitle = f.step?.title || "";
  return {
    key: "flight:" + f.id,
    title,
    kind,
    reason: kind === "flight_you" ? perchedOnYou(stepTitle) : f.reason || "Halted" + (stepTitle ? " at " + stepTitle : ""),
    text: f.plan_name,
    actions: kind === "flight_you" ? ["done", "skip", "open"] : ["retry", "skip", "open"],
    flight: { id: f.id, step_id: f.step?.id || "", step_title: stepTitle },
  };
}

/** `withFlightYou` plus every other route that waits on you: one at a Verify
 * stop whose behaviors wait on you, one halted until you Retry or Skip
 * (`flight_halted`), and — from the full flights — a route the session rows
 * don't show yet, or one with no session at all (keyed by the flight). */
export function withFlightRows(
  waiting: readonly OutboxWaiting[] | undefined | null,
  instances: readonly Instance[],
  flights?: readonly Flight[] | null
): FlightWaiting[] {
  const out: FlightWaiting[] = withFlightYou(waiting, instances);
  const live = new Set(instances.map((i) => i.title));
  const hasFlight = new Set(out.filter((w) => w.kind === "flight_you" || w.kind === "flight_halted").map((w) => w.title));
  const keys = new Set(out.map((w) => w.key));
  const full = new Map((flights || []).map((f) => [f.id, f]));
  const add = (w: FlightWaiting) => {
    if (keys.has(w.key) || (w.title && hasFlight.has(w.title))) return;
    keys.add(w.key);
    if (w.title) hasFlight.add(w.title);
    out.push(w);
  };
  for (const inst of instances) {
    const fl = inst.flight;
    if (!fl || !fl.id) continue;
    const f = full.get(fl.id);
    if (fl.state === "waiting_you") add(flightRow("flight_you", inst.title, { ...fl, reason: f?.reason }));
    else if (fl.state === "halted") add(flightRow("flight_halted", inst.title, { ...fl, reason: f?.reason }));
  }
  for (const f of flights || []) {
    if (f.state !== "waiting_you" && f.state !== "halted") continue;
    const step = f.steps[f.cursor];
    add(
      flightRow(f.state === "halted" ? "flight_halted" : "flight_you", f.session && live.has(f.session) ? f.session : "", {
        id: f.id,
        plan_name: f.plan_name,
        reason: f.reason,
        step: step ? { id: step.id, title: step.title } : null,
      })
    );
  }
  return out;
}

function kindOf(r: NeedsRow, byTitle: Map<string, Instance>): NeedsYouKind {
  if (r.plan) return "plan";
  if (r.waiting) {
    if (r.waiting.kind === "flight_you") return "wait_for_me";
    if (r.waiting.kind === "flight_halted") return "halted";
    return "approval";
  }
  const p = r.attn?.p ?? 1;
  if (p === 0) return "asking";
  if (p === 2) return "checks";
  if (p >= 3) return "ready";
  return rowWaitsOnYou(byTitle.get(r.title)?.flight) ? "wait_for_me" : "broken";
}

const ORDER = new Map<string, number>(NEEDS_YOU_KINDS.map((k, i) => [k, i]));

/** THE Needs-you list (see the file's comment for what is in it and in which
 * order). Pure, so every surface's count is unit-tested to be the same. */
export function needsYouRows(src: NeedsYouSources): NeedsYouRow[] {
  const instances = src.instances || [];
  const byTitle = new Map(instances.map((i) => [i.title, i]));
  const fullById = new Map((src.flights || []).map((f) => [f.id, f]));
  const merged = withPlanReady(
    needsAttention(attentionItems(instances as Instance[]), withFlightRows(src.waiting, instances, src.flights)),
    instances
  );
  const rows: NeedsYouRow[] = merged.map((r) => {
    const kind = kindOf(r, byTitle);
    // The route's id: on the rows made here, else (the server's own
    // flight_you row, withFlightYou's) the session row's flight.
    const fid =
      kind === "wait_for_me" || kind === "halted"
        ? (r.waiting as FlightWaiting | undefined)?.flight?.id || byTitle.get(r.title)?.flight?.id
        : undefined;
    const flight = fid ? fullById.get(fid) : undefined;
    return flight ? { ...r, kind, flight } : { ...r, kind };
  });
  for (const p of src.testPlans || []) {
    if (!isFailing(p) && !isWaitingOnYou(p)) continue;
    rows.push({ key: "behaviors:" + p.id, title: "", rank: 3, kind: "behaviors", behaviors: p });
  }
  // Stable: within a kind, the order the parts came in.
  return rows
    .map((r, i) => [r, i] as const)
    .sort((a, b) => ORDER.get(a[0].kind)! - ORDER.get(b[0].kind)! || a[1] - b[1])
    .map(([r]) => r);
}

/** How many need you and whether one is halted (the top bar's Sky badge turns
 * red then) — the count every badge shows. */
export function needsYouBadge(rows: readonly NeedsYouRow[]): { count: number; failing: boolean } {
  return { count: rows.length, failing: rows.some((r) => r.kind === "halted") };
}

/** The list from the live caches, for a component. Every surface calls this
 * one hook, so they read the same four queries and show the same rows. */
export function useNeedsYou(): NeedsYouRow[] {
  const { data: instances } = useInstances();
  const { data: outbox } = useOutbox();
  const { data: flights } = useFlights();
  const { data: plans } = useTestPlans();
  return useMemo(
    () =>
      needsYouRows({
        instances: instances || [],
        waiting: outbox?.groups?.waiting,
        flights,
        testPlans: plans?.plans,
      }),
    [instances, outbox, flights, plans]
  );
}

/** The sessions that need you, list order, one title each. A row with no
 * session (a budget pause, a ticket line, behaviors) has nowhere to jump to
 * and is left out. */
export function needsYouTitles(
  instances: Instance[],
  waiting: readonly OutboxWaiting[] | undefined | null,
  flights?: readonly Flight[] | null,
  testPlans?: readonly TestPlan[] | null
): string[] {
  const live = new Set(instances.map((i) => i.title));
  const out: string[] = [];
  for (const r of needsYouRows({ instances, waiting, flights, testPlans })) {
    if (r.title && live.has(r.title) && !out.includes(r.title)) out.push(r.title);
  }
  return out;
}

/** The one after `current` in `order`, wrapping; the first when `current`
 * isn't in it; null when nothing needs you. */
export function nextNeedsYou(order: string[], current: string | null | undefined): string | null {
  if (!order.length) return null;
  const at = current ? order.indexOf(current) : -1;
  return order[(at + 1) % order.length];
}

/** The list's sessions right now, from the caches the bell itself reads. */
export function needsYouNow(): string[] {
  const rows = queryClient.getQueryData<Instance[]>(["instances"]) || [];
  const outbox = queryClient.getQueryData<OutboxResponse | null>(["outbox"]);
  const flights = queryClient.getQueryData<Flight[] | null>(["flights"]);
  const plans = queryClient.getQueryData<TestPlansResponse>(["test-plans"]);
  return needsYouTitles(rows, outbox?.groups?.waiting, flights, plans?.plans);
}

/** Ctrl+K N: select the next session that needs you, and say which of how
 * many it is. */
export function jumpToNeedsYou(current: string | null | undefined) {
  const order = needsYouNow();
  const next = nextNeedsYou(order, current);
  if (!next) {
    toast("Nothing needs you right now");
    return;
  }
  selectSession(next);
  toast(windowName(next) + " — " + (order.indexOf(next) + 1) + " of " + order.length + " that need you");
}
