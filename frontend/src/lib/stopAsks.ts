/** What a stop parked on the person asks, beyond a plain Wait for me — the
 * ONE place every surface (a stop's tile and dialog, the pane strip, the
 * Sky's Needs you panel, the bell, the stop menu) reads it from, so their
 * buttons and words can't drift apart:
 *
 *  - `unresolved` — a Review stop out of fix rounds with findings still open
 *    (backend flight_kinds/review.py, `detail.ask` "unresolved",
 *    `detail.findings` the text). Primary "Give it another round" (the step
 *    retry: the agent gets another set of rounds, a follow-up review first);
 *    secondary "Carry on" (Done: the route goes on with the findings kept on
 *    record).
 *  - `owed` — the one perch that asks for every check a Verify stop with
 *    "Carry on, ask me at the end" left for the end (backend flights.py
 *    `_owed_perch`; `detail.ask` "owed", `detail.owed` the groups). One
 *    primary "Done — they all work" (Done confirms them all); per group "This
 *    one didn't work" (POST /api/test-plans/{plan}/confirm, verified false —
 *    the route goes back to that Verify stop).
 *
 * And, while a route still flies with checks owed (`flight.owed`,
 * `owed_note`; the session row's `flight.owed`, `owed_note`): the quiet
 * "2 checks owed to you at the end" line and the items grouped by stop for
 * its tooltip — never worded as if the route waits on the person yet. */

import type { Flight, FlightRow, FlightStep, StepRunState } from "./flights";

/** One Verify stop's checks owed to the person: its stop id and title, the
 * verify plan, and what to check. */
export interface OwedGroup {
  step: string;
  title: string;
  plan: string;
  items: string[];
}

/** A flight's `owed` entry: a group plus when it was owed, the run it was
 * owed for, and the stop asking for it now ("" = none yet). */
export interface OwedEntry extends OwedGroup {
  since?: number;
  run_at?: number;
  perch?: string;
}

/** The ask of a stop parked on the person that this module words ("" = a
 * plain perch, or one another surface words — a Verify's blocked / memory /
 * confirm). */
export type StopAsk = "" | "unresolved" | "owed";

/** `detail.ask` of a stop parked on the person: "unresolved" only on a Review
 * stop, "owed" only on a Wait for me stop; anything else is "". */
export function stopAskOf(step: Pick<FlightStep, "kind"> | null | undefined, rec: Pick<StepRunState, "state" | "detail"> | null | undefined): StopAsk {
  if (!step || !rec || rec.state !== "waiting_you") return "";
  const d = rec.detail || {};
  if (d.failed_reason) return "";
  const ask = String(d.ask ?? "");
  if (ask === "unresolved" && step.kind === "review") return "unresolved";
  if (ask === "owed" && step.kind === "you") return "owed";
  return "";
}

/** The ask of the stop a flight is parked at ("" when it isn't parked on the
 * person, or the stop's ask is a plain one). */
export function flightAsk(flight: Pick<Flight, "steps" | "cursor" | "state" | "step_states"> | null | undefined): StopAsk {
  if (!flight || flight.state !== "waiting_you") return "";
  const step = flight.steps[flight.cursor];
  return step ? stopAskOf(step, flight.step_states?.[step.id]) : "";
}

/** A Review stop's open findings (`detail.findings`), trimmed; "" for none. */
export function findingsOf(detail: Record<string, unknown> | null | undefined): string {
  return String(detail?.findings ?? "").trim();
}

/** Owed groups, read defensively: an entry needs a stop id; a missing title
 * reads "Verify"; items are non-empty strings. */
export function owedGroups(raw: unknown): OwedGroup[] {
  if (!Array.isArray(raw)) return [];
  const out: OwedGroup[] = [];
  for (const e of raw) {
    if (!e || typeof e !== "object") continue;
    const o = e as Record<string, unknown>;
    const step = String(o.step ?? "").trim();
    if (!step) continue;
    const items = Array.isArray(o.items) ? o.items.map((i) => String(i ?? "").trim()).filter(Boolean) : [];
    out.push({ step, title: String(o.title ?? "").trim() || "Verify", plan: String(o.plan ?? ""), items });
  }
  return out;
}

/** How many checks the groups hold: every item, a group with none counts one
 * (the server's `owed_count`). */
export function owedCount(groups: readonly OwedGroup[]): number {
  return groups.reduce((n, g) => n + (g.items.length || 1), 0);
}

const checks = (n: number) => n + (n === 1 ? " check" : " checks");

/** "2 checks owed to you at the end" — the server's `owed_line`, for a dto
 * from an older server that sent `owed` without `owed_note`. */
export function owedLine(n: number): string {
  return n > 0 ? checks(n) + " owed to you at the end" : "";
}

const FINAL = new Set(["done", "cancelled"]);

/** The quiet line next to a route still flying with checks owed: the
 * server's `owed_note`, else worked out from `owed`; "" when nothing is
 * owed, when the route has finished, or once the perch asking for them is
 * up (it says so itself — the line would only repeat it). */
export function owedNoteOf(f: (Pick<Flight, "state"> & { owed?: unknown; owed_note?: string }) | Pick<FlightRow, "state" | "owed" | "owed_note"> | null | undefined): string {
  if (!f || FINAL.has(f.state) || f.state === "waiting_you") return "";
  const groups = owedGroups(f.owed);
  if (!groups.length) return "";
  return String(f.owed_note || "").trim() || owedLine(owedCount(groups));
}

/** The tooltip under the quiet line: the items grouped by their Verify
 * stop's title, then when they are asked for. */
export function owedTip(groups: readonly OwedGroup[]): string {
  if (!groups.length) return "";
  return owedItemsText(groups) + "\n" + OWED_FLYING;
}

/** The route still flies: what the quiet line's tooltip ends with. */
export const OWED_FLYING = "The route carries on; it asks you once, before it finishes.";

/** The owed items grouped by their Verify stop's title, one per line — the
 * list alone, for the perch's own buttons (whose words say what they do). */
export function owedItemsText(groups: readonly OwedGroup[]): string {
  const lines: string[] = [];
  for (const g of groups) {
    lines.push(g.title + ":");
    for (const i of g.items.length ? g.items : ["Confirm it works"]) lines.push("  • " + i);
  }
  return lines.join("\n");
}

/** A perch's words for its two answers. */
export const UNRESOLVED_RETRY = "Give it another round";
export const UNRESOLVED_RETRY_TITLE = "The agent gets another set of fix rounds — the reviewer checks these findings first";
export const UNRESOLVED_DONE = "Carry on";
export const UNRESOLVED_DONE_TITLE = "The route goes on — the open findings stay on record";
export const OWED_DONE = "Done — they all work";
export const OWED_DONE_TITLE = "Confirms every check listed here as working — the route moves on";
export const OWED_FAILED = "This one didn't work";
export const OWED_FAILED_TITLE = "Say what you saw — the route goes back to this Verify stop";
/** The pane strip's way to the list (each check's "This one didn't work"). */
export const OWED_OPEN = "One didn't work…";
export const OWED_OPEN_TITLE = "Open the Route tab — the stop lists each check with “This one didn't work”";

/** The answers a parked stop offers, in order, the first primary. `retry` is
 * the step retry, `done` the step done; Skip stays the caller's (every
 * perch has it). Null for an ask this module doesn't word. */
export interface AskOffer {
  key: "retry" | "done";
  label: string;
  title: string;
  primary: boolean;
}
export function askOffers(ask: StopAsk): AskOffer[] | null {
  if (ask === "unresolved")
    return [
      { key: "retry", label: UNRESOLVED_RETRY, title: UNRESOLVED_RETRY_TITLE, primary: true },
      { key: "done", label: UNRESOLVED_DONE, title: UNRESOLVED_DONE_TITLE, primary: false },
    ];
  if (ask === "owed") return [{ key: "done", label: OWED_DONE, title: OWED_DONE_TITLE, primary: true }];
  return null;
}

/** The sentence a parked stop leads with: the server's note ("2 findings
 * still open after 3 rounds", "Before this route finishes: 3 checks for
 * you"), capitalised; `fallback` when it has none. */
export function askSentence(rec: Pick<StepRunState, "note"> | null | undefined, fallback = ""): string {
  const n = String(rec?.note ?? "").trim();
  return n ? n.charAt(0).toUpperCase() + n.slice(1) : fallback;
}

/** The room (px) a parked stop's ask takes under its buttons, on a tile or
 * a box: a Review's findings (their box scrolls past 160px), or the owed
 * checks by stop (a heading, the items, "This one didn't work"). */
export function askRoom(ask: StopAsk | undefined, detail: Record<string, unknown> | null | undefined): number {
  if (ask === "unresolved") return Math.min(160, findingsOf(detail).split("\n").length * 16 + 12) + 6;
  if (ask === "owed") return owedGroups(detail?.owed).reduce((h, g) => h + 46 + Math.max(1, g.items.length) * 22, 0);
  return 0;
}

/** A tile with an ask is this much wider, so findings and items read. */
export const ASK_TILE_W = 300;
