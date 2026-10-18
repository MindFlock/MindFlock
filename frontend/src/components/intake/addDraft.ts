/** Intake → Tickets: the "+ Add source" tracker draft, and the save outcome
 * every ticket-source card reads — the rules that are not React.
 *
 * Pure, and split out of TicketsTab for the same reason `merge.ts` and
 * `queue.ts` are: the component is hundreds of lines of provider plumbing,
 * and these rules are the ones that lost input when they were wrong.
 *
 *  - A draft survives ‹ Back (it goes back a step, keeping what was typed —
 *    a pasted token included) and a tab switch (the dialog keeps a stash the
 *    tab restores when it mounts again). Only Cancel, or picking a different
 *    tracker, drops it.
 *  - Saves are whole-list PUTs that can overlap. Each card remembers the
 *    sequence number of its newest save, so an older answer arriving late
 *    neither leaves a card on "Saving…" nor marks it failed after a newer
 *    save of the same list landed.
 */

import type { TicketingSource } from "../../api/types";
import type { FieldErrors } from "./sourceForm";

export type AddStep = 1 | 2 | 3;

/** The draft as the flow tracks it: its id in the source list, and the step. */
export interface TrackerDraft {
  id: string;
  step: AddStep;
}

/** What the dialog keeps for the Tickets tab while it is not mounted. */
export interface DraftStash {
  source: TicketingSource;
  step: AddStep;
}

/** ‹ Back: step 3 → 2, step 2 → 1 (the tiles, with this draft's still picked). */
export function backStep(step: AddStep): AddStep {
  return step === 3 ? 2 : 1;
}

/** Step 1's Continue with tile `key`, over the draft (if any) the person went
 * back from: the same tracker resumes it as typed; Repository hands over to
 * the repository flow; another tracker starts a fresh draft in its place. */
export function continueWith(
  draft: TrackerDraft | null,
  draftProvider: string | undefined,
  key: string
): "resume" | "repo" | "replace" {
  if (key === "repo") return "repo";
  if (draft && draftProvider === key) return "resume";
  return "replace";
}

/** The draft to keep while the tab is away: only one the server doesn't hold
 * (a stored source is in the list already). null = nothing to keep. */
export function stashOf(
  list: TicketingSource[] | null,
  draft: TrackerDraft | null,
  stored: ReadonlySet<string>
): DraftStash | null {
  if (!draft || stored.has(draft.id)) return null;
  const source = (list || []).find((s) => s.id === draft.id);
  return source ? { source: { ...source }, step: draft.step } : null;
}

/** The list and draft with a stash put back. A stash whose id is taken by now
 * (the source was saved some other way) is dropped rather than doubled. */
export function restoreStash(
  list: TicketingSource[],
  stash: DraftStash | null
): { list: TicketingSource[]; draft: TrackerDraft | null } {
  if (!stash || list.some((s) => s.id === stash.source.id)) return { list, draft: null };
  return { list: [...list, stash.source], draft: { id: stash.source.id, step: stash.step } };
}

/** What a save resolved to — `ok`, or the per-source field errors the server
 * refused it with (empty for a network or server failure: nothing to mark). */
export interface PersistResult {
  ok: boolean;
  fieldErrors: Record<string, FieldErrors>;
  /** Why, when it failed without field errors. */
  message?: string;
}

/** The footer line after Finish's save was refused: the marked fields when
 * there are some, else the failure itself (it used to say "Fix the marked
 * fields first." with nothing marked). */
export function finishRefusal(r: PersistResult, draftId: string, fixFields: string): string {
  if (Object.keys(r.fieldErrors[draftId] || {}).length) return fixFields;
  if (Object.keys(r.fieldErrors).length) return fixFields;
  return "Not saved — " + (r.message || "the server didn't answer") + ". Try again.";
}

/** Per card, the sequence number of the newest save that carried its change;
 * and the newest save that succeeded. */
export interface SaveSeqs {
  card: Record<string, number>;
  ok: number;
}

/** A save is starting: these cards now belong to `seq`. */
export function beginSave(seqs: SaveSeqs, seq: number, ids: string[]): SaveSeqs {
  const card = { ...seqs.card };
  for (const id of ids) card[id] = seq;
  return { card, ok: seqs.ok };
}

/** Save `seq` answered (`ok` or not) for the cards it carried: which of them
 * to mark "Saved ✓" and which "Not saved". A card a NEWER save still carries
 * is left to that save. A late failure after a newer save of the whole list
 * succeeded is not a failure — that save stored these cards too. */
export function settleSave(
  seqs: SaveSeqs,
  seq: number,
  ids: string[],
  ok: boolean
): { seqs: SaveSeqs; saved: string[]; failed: string[] } {
  const mine = ids.filter((id) => (seqs.card[id] ?? 0) <= seq);
  if (ok) return { seqs: { card: seqs.card, ok: Math.max(seqs.ok, seq) }, saved: mine, failed: [] };
  if (seqs.ok > seq) return { seqs, saved: mine, failed: [] };
  return { seqs, saved: [], failed: mine };
}
