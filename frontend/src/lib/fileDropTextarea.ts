/** Drop a file onto a <textarea> and get its uploaded PATH spliced in at the
 * caret — the textarea twin of `attachFileDrop`, which does the same thing for
 * a PTY.
 *
 * WHY A PATH AND NOT THE CONTENTS. Same reason as the terminal version: the
 * agent runs on this machine and cannot see the browser, so "look at this"
 * has to become something it can open. Uploading and inserting the path works
 * for a 40MB PDF as well as a three-line log, and it keeps the two gestures
 * telling the user the same story — drop a file anywhere in this app and what
 * lands is a path.
 *
 * WHY IT IS NOT `attachFileDrop`. That helper ends in `term.paste()`, and a
 * textarea here is a CONTROLLED React input: writing `host.value` directly is
 * discarded on the next render, so the new text has to go back through the
 * component's own setState. Hence a separate wiring that hands the caller a
 * finished string and lets it own the value.
 */

import { useCallback, useRef } from "react";
import { dtHasFiles, uploadFilesAsPathText } from "./clipboard";

/** Marks a textarea as the live drop target. Styled in dialogs/fileDropTextarea.css. */
export const TEXTAREA_DROP_CLASS = "ta-file-drop";

/** Where the upload should land. */
export interface TextareaDropOptions {
  /** Session whose workspace receives the file; omitted → ~/.mindflock/pastes. */
  session?: string;
  /** Fallback for a card with NO textarea mounted yet.
   *
   * Page 1 of the new-session form is one question and three buttons — the
   * prompt fold is on page 2 — so there is genuinely no box to write into, and
   * a drop there would otherwise be accepted and then vanish. The card hands us
   * this instead: it takes the path text and puts it wherever it belongs. */
  onInsert?: (text: string) => void;
}

/** Write `next` into a CONTROLLED textarea so React notices.
 *
 * Assigning `ta.value` is not enough: React keeps a private value tracker on the
 * node and swallows the resulting event when the tracked value still matches, so
 * the component's own onChange never runs and the next render puts the old text
 * straight back. Going through the prototype's setter updates the node behind
 * the tracker's back; the dispatched `input` event is then the ordinary one
 * React already listens for, and state updates through the component's existing
 * onChange rather than through a setter this module would have to be handed.
 *
 * That is what lets ONE zone serve both tabs of the new-session dialog: the
 * prompt and the ticket brief live in different components with different state,
 * and neither has to know this exists.
 */
export function setControlledValue(ta: HTMLTextAreaElement, next: string): void {
  const desc = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(ta), "value");
  if (desc?.set) desc.set.call(ta, next);
  else ta.value = next;
  ta.dispatchEvent(new Event("input", { bubbles: true }));
}

/** Open any collapsed <details> the textarea is buried in.
 *
 * The prompt box lives inside the dialog's "Prompt" fold, which is CLOSED by
 * default — a closed <details> keeps its children in the DOM but gives them no
 * layout box. Dropping a file and watching nothing happen is the whole bug this
 * guards: the path did land, in a field nobody could see. Setting `.open` fires
 * the fold's own onToggle, so React's state follows rather than fighting it.
 */
function revealTextarea(ta: HTMLElement): void {
  for (let el = ta.parentElement; el; el = el.parentElement) {
    if (el.tagName === "DETAILS" && !(el as HTMLDetailsElement).open) {
      (el as HTMLDetailsElement).open = true;
    }
  }
}

/** Splice `insert` into `value` between `start` and `end`, padding so the path
 * never fuses onto a neighbouring word.
 *
 * Exported for its own sake: the padding is the part with opinions in it, and
 * pinning it in a test is cheaper than reconstructing a drag to find out that
 * two files dropped in a row came back as `/a/b.png/a/c.png`.
 */
export function spliceAtCaret(
  value: string,
  start: number,
  end: number,
  insert: string
): { value: string; caret: number } {
  const s = Math.max(0, Math.min(start, value.length));
  const e = Math.max(s, Math.min(end, value.length));
  const before = value.slice(0, s);
  const after = value.slice(e);
  // Pad on each side only where there is not already whitespace: a drop into an
  // empty box must not start the text with a space, and dropping onto a
  // selection in the middle of a sentence must not leave a double space behind
  // where the selection used to be. A trailing space when nothing follows is
  // deliberate and matches the PTY paste — the next thing typed is a new word,
  // and the next file dropped is a separate path rather than a concatenation.
  const lead = before && !/\s$/.test(before) ? " " : "";
  const tail = after && /^\s/.test(after) ? "" : " ";
  const body = lead + insert + tail;
  return { value: before + body + after, caret: before.length + body.length };
}

/** Wire a drop ZONE — the dialog CARD, not the textarea inside it. Returns a
 * disposer that removes every listener.
 *
 * The zone is the whole card for three reasons, each of which was a bug first:
 *
 *  - The prompt box is inside a fold that is CLOSED by default, so targeting the
 *    textarea meant the session tab had no droppable area at all — a closed
 *    <details> gives its children no layout box to drag onto.
 *  - A two-row textarea is a small thing to ask someone to hit while holding a
 *    file. The card is not.
 *  - A textarea is a replaced element and generates no ::before/::after, so a
 *    "drop here" caption cannot be drawn on one at all.
 *
 * The textarea is FOUND inside the zone rather than passed in, which is what
 * lets a single zone serve both tabs: whichever card is mounted contains
 * exactly one textarea — the prompt, or the ticket brief.
 */
export function attachFileDropZone(
  zone: HTMLElement,
  opts: () => TextareaDropOptions
): () => void {
  const box = (): HTMLTextAreaElement | null => zone.querySelector("textarea");
  // readOnly is how the new-ticket brief says "filing, hands off" (a disabled
  // textarea would lose focus and move the caret), so honour it here too rather
  // than accepting a drop the component is not ready to hold.
  const shut = () => {
    const ta = box();
    // No box yet is fine IF the card gave us somewhere else to put the text;
    // without that there is nothing this drop could do, so do not claim it.
    if (!ta) return !opts().onInsert;
    return ta.readOnly || ta.disabled;
  };

  const onDragOver = (ev: DragEvent) => {
    if (shut() || !dtHasFiles(ev.dataTransfer)) return;
    // Prevented AND stopped: the dialog sits inside the grid, whose panes carry
    // their own drop handler for rearranging windows. A file dropped on a text
    // box is not a pane being moved.
    ev.preventDefault();
    ev.stopPropagation();
    ev.dataTransfer!.dropEffect = "copy";
    zone.classList.add(TEXTAREA_DROP_CLASS);
  };

  const onDragLeave = (ev: DragEvent) => {
    if (!zone.contains(ev.relatedTarget as Node)) zone.classList.remove(TEXTAREA_DROP_CLASS);
  };

  const onDrop = (ev: DragEvent) => {
    if (shut() || !dtHasFiles(ev.dataTransfer)) return;
    ev.preventDefault();
    ev.stopPropagation();
    zone.classList.remove(TEXTAREA_DROP_CLASS);

    // Read the caret NOW, before the upload's await. A drop does not focus the
    // box, and an unfocused textarea reports selectionStart 0 — which would
    // quietly insert the path at the very START of everything the user wrote.
    // So: caret when this box actually holds it, end of the text otherwise.
    const host = box();
    if (!host) {
      // Page 1: no box on screen. Upload anyway and hand the path to the card,
      // which carries it to the field the user has not reached yet.
      const { session: sess, onInsert } = opts();
      void uploadFilesAsPathText(ev.dataTransfer!.files, sess).then((text) => {
        if (text) onInsert?.(text);
      });
      return;
    }
    const focused = typeof document !== "undefined" && document.activeElement === host;
    const start = focused ? (host.selectionStart ?? host.value.length) : host.value.length;
    const end = focused ? (host.selectionEnd ?? start) : host.value.length;

    const files = ev.dataTransfer!.files;
    const { session } = opts();
    void uploadFilesAsPathText(files, session).then((text) => {
      if (!text) return;
      // Re-read the value rather than trusting the one captured above: an upload
      // takes as long as it takes, and the user can type through it. The offsets
      // are clamped by spliceAtCaret, so worst case the path lands late rather
      // than overwriting what they just typed.
      const next = spliceAtCaret(host.value, start, end, text);
      setControlledValue(host, next.value);
      revealTextarea(host);
      // After a controlled re-render the DOM caret is wherever React left it, so
      // put it back on the far side of what we inserted — the user's next
      // keystroke belongs after the path, not before it.
      const restore = () => {
        try {
          host.focus();
          host.setSelectionRange(next.caret, next.caret);
        } catch {
          /* detached or not yet re-rendered; the value is what matters */
        }
      };
      if (typeof requestAnimationFrame === "function") requestAnimationFrame(restore);
      else restore();
    });
  };

  zone.addEventListener("dragover", onDragOver);
  zone.addEventListener("dragleave", onDragLeave);
  zone.addEventListener("drop", onDrop);
  return () => {
    zone.removeEventListener("dragover", onDragOver);
    zone.removeEventListener("dragleave", onDragLeave);
    zone.removeEventListener("drop", onDrop);
    zone.classList.remove(TEXTAREA_DROP_CLASS);
  };
}

/** React wiring. Returns a CALLBACK REF for the WRAPPER around the textarea —
 * spread it as `ref={...}` on that wrapper, not on the box itself.
 *
 * A callback ref rather than a `useRef` object plus a `useEffect`, because these
 * zones are NOT mounted for the life of the component that wires them. The
 * new-session dialog renders its prompt form and <NewTicketPane> on mutually
 * exclusive tabs, so an effect that binds once would run while the zone it
 * wants does not exist, bind nothing, and never get a second chance — the
 * feature would be silently dead for anyone who opened the dialog on the other
 * tab. A callback ref fires exactly when the node arrives and when it leaves.
 */
export function useFileDropTextarea(
  opts: TextareaDropOptions
): (el: HTMLElement | null) => void {
  // Options through a ref so an inline object literal at the call site does not
  // re-create the callback on every render — a new callback ref makes React
  // detach and re-attach, which mid-drag drops the drag.
  const latest = useRef(opts);
  latest.current = opts;
  const detach = useRef<(() => void) | null>(null);

  return useCallback((el: HTMLElement | null) => {
    detach.current?.();
    detach.current = null;
    if (el) detach.current = attachFileDropZone(el, () => latest.current);
  }, []);
}
