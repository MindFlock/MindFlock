/** Save-as-you-type for the ticket source card's text fields.
 *
 * The fields used to save on blur only, so a URL typed and left in its box sat
 * under a footer that said "Saved ✓" — and was gone after a reload, an Escape
 * or a closed tab (round 3, M1). Now every keystroke (re)starts a short timer
 * and the value is sent when typing pauses, on blur, on Enter, and when the
 * field goes away (the dialog closing) — whichever comes first, once.
 *
 * `onPending` fires as soon as the field holds text that has not been sent,
 * which is what lets the card say "Saving…" instead of a stale "Saved ✓". */

import { useEffect, useRef } from "react";

/** How long typing must pause before the value is sent. Long enough not to
 * save (and auto-Test) a token per keystroke, short enough to beat a reload. */
export const AUTOSAVE_MS = 700;

export interface Autosave<T> {
  /** The field changed: send `value` once typing pauses. */
  schedule(value: T): void;
  /** Send whatever is pending now (blur, Enter, unmount). No-op when nothing is. */
  flush(): void;
  /** Whether typed text is waiting to be sent — a re-seed from the server
   * must not overwrite it. */
  isPending(): boolean;
}

/** The debounce itself, DOM- and React-free. `commit` / `onPending` are read
 * through getters at call time, so a timer armed several renders ago commits
 * with the caller's CURRENT callback (and so the card's current source). */
export function makeAutosave<T>(
  commit: () => (value: T) => void,
  onPending: () => (() => void) | undefined,
  delay: number = AUTOSAVE_MS
): Autosave<T> {
  let timer: ReturnType<typeof setTimeout> | null = null;
  let pending: { value: T } | null = null;
  const flush = () => {
    if (timer) clearTimeout(timer);
    timer = null;
    const p = pending;
    pending = null;
    if (p) commit()(p.value);
  };
  return {
    schedule(value: T) {
      pending = { value };
      onPending()?.();
      if (timer) clearTimeout(timer);
      timer = setTimeout(flush, delay);
    },
    flush,
    isPending: () => pending !== null,
  };
}

export function useAutosave<T>(
  commit: (value: T) => void,
  onPending?: () => void,
  delay: number = AUTOSAVE_MS
): Autosave<T> {
  const commitRef = useRef(commit);
  commitRef.current = commit;
  const pendingRef = useRef(onPending);
  pendingRef.current = onPending;
  const saver = useRef<Autosave<T> | null>(null);
  if (!saver.current)
    saver.current = makeAutosave<T>(
      () => commitRef.current,
      () => pendingRef.current,
      delay
    );
  const s = saver.current;

  // Closing the dialog (Close, Escape, a click outside) unmounts the field:
  // what was typed is sent rather than dropped.
  useEffect(() => () => s.flush(), [s]);

  return s;
}
