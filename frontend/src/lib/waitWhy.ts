/** Why an automatic start waits in the launch gate (backend
 * core/launch_gate.py `decide`), in ONE vocabulary every surface reads: the
 * rail's bar (MemoryWaitBar), a waiting row's chip, line and tooltip
 * (lib/sessionState, SidebarRow), the Memory gauge (usage/memoryModel) and a
 * team run's badge (RunGroupHeader).
 *
 * The rail used to say "Memory low — new agents wait" whenever anything was
 * queued, while the gauge beside it said "OK — room to spare": the queue also
 * holds starts back for the live-agent cap, the spacing between automatic
 * starts and agents still warming up. Each `why` now has its own words, and
 * only `memory` and `warming` may say "memory".
 *
 *   why      chip                                        what clears it
 *   memory   Waiting for memory                          room frees up
 *   warming  Waiting — other agents are still starting   they settle
 *   spacing  Starting shortly                            a few seconds
 *   cap      Waiting for a free agent slot               an agent finishes
 *   queue    Queued — N ahead                            the ones ahead start
 *   restart  Queued                                      its turn after a restart
 *
 * An older server sends no `why`: it is read from the `reason` text (the
 * regexes `holdingLine` used to apply). Pure. */

import { relTime } from "./format";

export type WaitWhy = "memory" | "warming" | "cap" | "spacing" | "queue" | "restart";

export const WAIT_WHYS: readonly WaitWhy[] = ["memory", "warming", "cap", "spacing", "queue", "restart"];

/** The value as a WaitWhy, or null for anything else. */
export function asWaitWhy(v: unknown): WaitWhy | null {
  return typeof v === "string" && (WAIT_WHYS as readonly string[]).includes(v) ? (v as WaitWhy) : null;
}

/** An older server's `reason` text → its why. Memory when nothing else fits:
 * that is what the gate held starts back for before it learned the rest. */
export function whyFromReason(reason: string | null | undefined): WaitWhy {
  const r = String(reason || "");
  if (/automatic starts stop at/i.test(r)) return "cap";
  if (/automatic starts are \d+\s*s apart|the last agent started/i.test(r)) return "spacing";
  if (/before the restart/i.test(r)) return "restart";
  if (/still starting/i.test(r)) return "warming";
  return "memory";
}

/** A queue item's (or a row's `waiting_for_memory`'s) why. */
export function waitWhyOf(w: { why?: unknown; reason?: string | null } | null | undefined): WaitWhy {
  return asWaitWhy(w?.why) ?? whyFromReason(w?.reason);
}

/** The gate's why while it holds anything, else null: its own `why`, else
 * the head of the queue's, else its `reason` text (the head's reason, on an
 * older server). */
export function gateWhy(
  g: { holding?: boolean; queued?: { why?: unknown; reason?: string }[]; reason?: string; why?: unknown } | null | undefined
): WaitWhy | null {
  if (!g || !g.holding || !g.queued?.length) return null;
  const head = g.queued[0];
  return asWaitWhy(g.why) ?? asWaitWhy(head?.why) ?? whyFromReason(g.reason || head?.reason);
}

/** Whether a why may use the word "memory". */
export function aboutMemory(why: WaitWhy): boolean {
  return why === "memory" || why === "warming";
}

/** The waiting row's chip: "Waiting for memory", "Queued — 2 ahead". */
export function waitChip(why: WaitWhy, position = 1): string {
  switch (why) {
    case "memory":
      return "Waiting for memory";
    case "warming":
      return "Waiting — other agents are still starting";
    case "spacing":
      return "Starting shortly";
    case "cap":
      return "Waiting for a free agent slot";
    case "queue": {
      const ahead = Math.max(0, (Number(position) || 1) - 1);
      return ahead ? "Queued — " + ahead + " ahead" : "Queued";
    }
    case "restart":
      return "Queued";
  }
}

/** What ends the wait, said plainly (the tooltips). */
export function waitClears(why: WaitWhy): string {
  switch (why) {
    case "memory":
      return "It starts by itself once there is room";
    case "warming":
      return "It starts by itself once the agents starting now have settled";
    case "spacing":
      return "Automatic starts are spaced out, so it starts in a few seconds";
    case "cap":
      return "It starts by itself when a running agent finishes or goes to sleep";
    case "queue":
      return "It starts by itself after the ones ahead of it";
    case "restart":
      return "It was waiting before MindFlock restarted and starts by itself when its turn comes";
  }
}

/** What the waiting start is: "New worker", "Restart", "Wake". */
export function waitKindWord(kind: string | null | undefined, creates?: boolean): string {
  switch (String(kind || "")) {
    case "spawn":
      return "New worker";
    case "ticket":
      return "New ticket session";
    case "queued prompt":
      return "Restart for a queued prompt";
    case "wake":
      return "Wake";
    case "revive":
      return "Restart";
    default:
      return creates ? "New session" : "Start";
  }
}

/** 1 → "1st", 2 → "2nd", 11 → "11th", 22 → "22nd". */
export function ordinal(n: number): string {
  const k = Math.max(0, Math.floor(Number(n) || 0));
  const teen = k % 100 >= 11 && k % 100 <= 13;
  const suf = teen ? "th" : k % 10 === 1 ? "st" : k % 10 === 2 ? "nd" : k % 10 === 3 ? "rd" : "th";
  return k + suf;
}

/** "waiting 3m" since `sinceS` (epoch seconds); "" when unknown. */
export function waitedFor(sinceS: number | null | undefined, nowMs: number): string {
  const since = Number(sinceS) || 0;
  if (since <= 0) return "";
  return "waiting " + relTime(since, { suffix: "", now: nowMs / 1000 });
}

/** "next in line" / "2nd in line"; "" when the position is unknown. */
export function inLine(position: number | null | undefined): string {
  const p = Math.floor(Number(position) || 0);
  if (p < 1) return "";
  return p === 1 ? "next in line" : ordinal(p) + " in line";
}

/** The fields of a queue item these words read (all optional: an older
 * server sends fewer). */
export interface WaitLike {
  why?: unknown;
  reason?: string | null;
  kind?: string | null;
  since?: number | null;
  position?: number | null;
  creates?: boolean | null;
}

/** The waiting row's second line: "New worker · waiting 3m · 2nd in line". */
export function waitLine(w: WaitLike, nowMs: number): string {
  return [waitKindWord(w.kind, !!w.creates), waitedFor(w.since, nowMs), inLine(w.position)].filter(Boolean).join(" · ");
}

/** The waiting row's tooltip: the chip, the server's reason, the line, what
 * ends it and its two buttons. */
export function waitHint(w: WaitLike, nowMs: number): string {
  const why = waitWhyOf(w);
  const reason = String(w.reason || "").trim();
  return (
    waitChip(why, Number(w.position) || 1) +
    (reason ? " — " + reason : "") +
    "\n" +
    waitLine(w, nowMs) +
    "\n" +
    waitClears(why) +
    ". Start anyway starts it now; Stop waiting cancels it."
  );
}

/** The rail bar's head while the gate holds anything. */
export function holdHead(why: WaitWhy): string {
  switch (why) {
    case "memory":
      return "Memory low — new agents wait";
    case "warming":
      return "Agents still starting — new agents wait";
    case "cap":
      return "Agent limit reached — new agents wait";
    case "spacing":
      return "Starting agents one at a time";
    case "queue":
    case "restart":
      return "New agents queued";
  }
}

/** A team run's badge while its queued lines wait on the gate. */
export function runWaitBadge(why: WaitWhy): { label: string; title: string } {
  switch (why) {
    case "memory":
      return { label: "memory", title: "Memory is low — queued tasks start once it recovers" };
    case "warming":
      return { label: "starting", title: "Other agents are still starting and need memory — queued tasks start once they settle" };
    case "cap":
      return { label: "agent limit", title: "The live-agent limit is reached — queued tasks start as agents finish" };
    case "spacing":
      return { label: "starting", title: "Automatic starts are spaced out — queued tasks start in a moment" };
    case "queue":
    case "restart":
      return { label: "queued", title: "Queued tasks start in turn" };
  }
}

/** The run's hover line (lib/runs groupTitle) for the same wait. */
export function runWaitLine(why: WaitWhy): string {
  switch (why) {
    case "memory":
      return "waiting for memory — queued lines start once it recovers";
    case "warming":
      return "waiting — other agents are still starting; queued lines start once they settle";
    case "cap":
      return "waiting for a free agent slot — queued lines start as agents finish";
    case "spacing":
      return "starting queued lines one at a time";
    case "queue":
    case "restart":
      return "queued lines start in turn";
  }
}

/** The gauge's pressure words while the level reads OK but the gate holds
 * starts back for something (lib usage/memoryModel levelWords). */
export function gaugeOkWords(why: WaitWhy): string {
  switch (why) {
    case "memory":
      return "OK, but no room for one more agent — new agents wait";
    case "warming":
      return "OK — new agents wait while others start";
    case "cap":
      return "OK — agent limit reached, new agents wait";
    case "spacing":
      return "OK — starting agents one at a time";
    case "queue":
    case "restart":
      return "OK — new agents queued";
  }
}

/** The toast after Stop waiting. */
export function stopWaitingToast(name: string, creates?: boolean): string {
  return creates ? "Cancelled " + name + " — it won't be started" : name + " stopped waiting — start it yourself when you need it";
}
