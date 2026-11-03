/** Round 18: the icon a chain's tile draws (SkyChain) and the family colour it
 * wears. The stroke icons themselves are Bird.tsx's `StopIcon` (the palette,
 * the classic boxes, the inspector draw the same); this adds the glyphs only
 * a tile needs — the Handoff's ↗ ("Start a chain") and a group's stacked
 * layers — and the kind → family map for a host that has no `kinds` list. */

import type { StepKindDef } from "../../lib/flights";
import type { DiagramNode } from "../../lib/flightDiagram";
import { StopIcon } from "./Bird";

/** The palette's families (round 15) plus `flow` (one chain driving
 * another: the Handoff). A tile's icon is painted in its family's colour
 * (SkyChain.css `.sky-fam-*`). */
export type StopFamily = "work" | "ship" | "outside" | "people" | "flow";

/** The server's own families (backend/web/core/flights.py `_FAMILY` plus
 * each plug-in kind's `spec["family"]` in core/flight_kinds/, as
 * `GET /api/flights/kinds` sends them), for when no `kinds` list is at hand.
 * Keep it the server's word for word: Loop is work (it repeats earlier
 * stops), Checkpoint is ship. `perch` is the tile's name for `you`. Unknown
 * kinds are "outside", as the server says. */
const FAMILY: Record<string, StopFamily> = {
  agent: "work",
  split: "work",
  join: "work",
  command: "work",
  test: "work",
  judge: "work",
  review: "work",
  loop: "work",
  ship: "ship",
  checks: "ship",
  reviews: "ship",
  land: "ship",
  sync: "ship",
  checkpoint: "ship",
  wait: "outside",
  notify: "outside",
  ticket: "outside",
  budget: "outside",
  call: "outside",
  listen: "outside",
  preview: "outside",
  screenshot: "outside",
  you: "people",
  perch: "people",
  verify: "people",
  someone: "people",
  handoff: "flow",
};

const FAMILIES: ReadonlySet<string> = new Set(["work", "ship", "outside", "people", "flow"]);

/** A kind's family: the server's word when `kinds` names it, else ours. */
export function kindFamily(kind: string, kinds?: readonly Pick<StepKindDef, "kind" | "family">[] | null): StopFamily {
  const f = kinds?.find((k) => k.kind === kind)?.family;
  if (f && FAMILIES.has(f)) return f as StopFamily;
  return FAMILY[kind] || "outside";
}

/** A node's family: a perch is people, a group takes its first sub-step's,
 * a session / branch tile is work. */
export function nodeFamily(n: Pick<DiagramNode, "kind" | "icon" | "perch" | "group">, kinds?: readonly Pick<StepKindDef, "kind" | "family">[] | null): StopFamily {
  if (n.perch) return "people";
  if (n.kind === "group") return n.group?.steps[0] ? kindFamily(n.group.steps[0].kind, kinds) : "work";
  if (n.kind === "session" || n.kind === "child" || n.kind === "branch" || n.kind === "parent") return "work";
  return kindFamily(n.icon, kinds);
}

/** The icon a tile shows for `kind` (`n.perch ? "perch" : n.icon`). */
export function TileIcon({ kind, size = 28 }: { kind: string; size?: number }) {
  if (kind === "handoff")
    return (
      <svg className="fl-stop-icon" width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" focusable="false">
        <path d="M13.5 4.5H19.5V10.5" />
        <path d="M19.5 4.5L10 14" />
        <path d="M17 13.5V18a1.5 1.5 0 0 1-1.5 1.5H6A1.5 1.5 0 0 1 4.5 18V8.5A1.5 1.5 0 0 1 6 7h4.5" />
      </svg>
    );
  if (kind === "group")
    return (
      <svg className="fl-stop-icon" width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" focusable="false">
        <path d="M12 3.5l8.5 4.25L12 12 3.5 7.75z" />
        <path d="M3.5 12L12 16.25 20.5 12" />
        <path d="M3.5 16.25L12 20.5l8.5-4.25" />
      </svg>
    );
  return <StopIcon kind={kind} size={size} />;
}
