/** The sky's undo stack (docs/flights.md "Round 14" → "Undo", "As built:
 * round 14 sky"): every removal in the sky — a stop or group off a line, a
 * grounded line deleted, a merge onto a session, a closed session, the
 * selection bar's Remove — pushes ONE entry. Ctrl+Z / ⌘Z in the sky pops the
 * newest; the removal's toast Undo takes ITS entry (`takeUndo`), so the two
 * never apply the same undo twice. No redo (Ctrl+Y is the view toggle).
 *
 * At most `UNDO_MAX` entries, each good for `UNDO_TTL_MS`. An entry may say
 * when it no longer applies (`valid`, e.g. the closed session is open again):
 * pops pass over it. Module state, one stack per page; `now` is a parameter
 * so tests need no clock. */

export const UNDO_MAX = 20;
export const UNDO_TTL_MS = 10 * 60 * 1000;

export interface UndoInput {
  /** What was done, as the "Undid: …" toast says it ("Remove “Review”"). */
  label: string;
  /** Puts it back. */
  undo(): Promise<unknown>;
  /** False once there is nothing left to undo (it was put back elsewhere). */
  valid?(): boolean;
}

export interface UndoEntry extends UndoInput {
  id: number;
  at: number;
}

let stack: UndoEntry[] = [];
let seq = 0;

const live = (e: UndoEntry, now: number) => now - e.at < UNDO_TTL_MS && (!e.valid || e.valid());

/** Drop what has expired or no longer applies. */
function prune(now: number) {
  stack = stack.filter((e) => live(e, now));
}

/** One more thing to undo; the oldest falls off past `UNDO_MAX`. */
export function pushUndo(input: UndoInput, now = Date.now()): UndoEntry {
  const e: UndoEntry = { ...input, id: ++seq, at: now };
  prune(now);
  stack.push(e);
  if (stack.length > UNDO_MAX) stack = stack.slice(stack.length - UNDO_MAX);
  return e;
}

/** The newest entry still good, taken off the stack (null: nothing to undo). */
export function popUndo(now = Date.now()): UndoEntry | null {
  prune(now);
  return stack.pop() || null;
}

/** The newest entry still good, left where it is. */
export function peekUndo(now = Date.now()): UndoEntry | null {
  prune(now);
  return stack[stack.length - 1] || null;
}

export function undoCount(now = Date.now()): number {
  prune(now);
  return stack.length;
}

/** A toast's Undo: take THIS entry off the stack. True = it was still there
 * (run it); false = Ctrl+Z already undid it (or it ran out). */
export function takeUndo(entry: Pick<UndoEntry, "id">, now = Date.now()): boolean {
  const i = stack.findIndex((e) => e.id === entry.id);
  if (i < 0) return false;
  const [e] = stack.splice(i, 1);
  return live(e, now);
}

/** The toast action for an entry: its Undo button runs it once, at most. */
export function undoAction(entry: UndoEntry, ariaLabel?: string): { label: string; ariaLabel?: string; onClick(): void } {
  return {
    label: "Undo",
    ...(ariaLabel ? { ariaLabel } : {}),
    onClick: () => {
      if (takeUndo(entry)) void entry.undo();
    },
  };
}

/** Empty the stack (tests). */
export function clearUndo() {
  stack = [];
}
