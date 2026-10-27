/** The ask pop-up and the mic, wired together without either importing the
 * other's component.
 *
 * While the pop-up (AskDialog) is open it registers what is being asked
 * (`setAskVoice`): the Talk hotkey, the top-bar mic and the pop-up's own mic
 * button then listen for THAT session's answer — a dialog's words go to the
 * voice router for that session (`answer` → /answer), a question's words are
 * typed into the pop-up's answer field and "submit" sends them. The mic
 * (TopBarMic, which owns the recognizer and the speech queue) registers its
 * side (`registerAskMic`): read the ask out and listen hands-free, cancel,
 * and whether it is listening — published back with its caption, so the
 * pop-up can show what it hears. */

import type { Dialog } from "../api/types";

/** What the pop-up is asking, for the mic. */
export interface AskVoice {
  title: string;
  kind: "dialog" | "question";
  /** The words the mic shows before a word is heard. */
  hint(): string;
  /** The answer field (a question; null for a dialog). */
  field(): HTMLTextAreaElement | null;
  /** Send what the field holds (a question): POST /send {text, enter:
   * true[, say]} — resolves to the caption line, `ok` when it went. */
  submit(say: boolean): Promise<{ ok: boolean; line: string }>;
}

/** What the mic does for the pop-up. */
export interface AskMic {
  /** Read the ask out and listen (12 s, one retry) — only with "Read replies
   * aloud" on; nothing is said otherwise. */
  handsFree(title: string, ask: { dialog?: Dialog | null; question?: string }): void;
  /** Stop listening / reading for `title` (the pop-up moved on). */
  stop(title: string): void;
  /** Take back what this dictation typed and stop (Escape while listening).
   * False when it wasn't listening. */
  cancel(): boolean;
  /** There is a recognizer on this page. */
  supported(): boolean;
  /** Ready the microphone ahead of a press (the pop-up's mic appeared) —
   * only when its permission is already given. */
  warm?(): void;
}

let ask: AskVoice | null = null;
let mic: AskMic | null = null;

export function setAskVoice(a: AskVoice | null): void {
  ask = a;
}

/** Clear only if `a` is still the registered one (a newer ask may have
 * replaced it before this one's cleanup ran). */
export function clearAskVoice(a: AskVoice): void {
  if (ask === a) ask = null;
}

export function askVoice(): AskVoice | null {
  return ask;
}

export function registerAskMic(m: AskMic): () => void {
  mic = m;
  return () => {
    if (mic === m) mic = null;
  };
}

export function askMic(): AskMic | null {
  return mic;
}

// --- The mic's state, for the pop-up's button ------------------------------- //

export interface MicState {
  listening: boolean;
  /** The title the dictation is for ("" = a text box / none). */
  title: string;
  caption: string;
  unsure: string;
}

let micState: MicState = { listening: false, title: "", caption: "", unsure: "" };
const listeners = new Set<() => void>();

export function publishMicState(patch: Partial<MicState>): void {
  const next = { ...micState, ...patch };
  if (
    next.listening === micState.listening &&
    next.title === micState.title &&
    next.caption === micState.caption &&
    next.unsure === micState.unsure
  )
    return;
  micState = next;
  listeners.forEach((l) => l());
}

export function getMicState(): MicState {
  return micState;
}

export function subscribeMicState(cb: () => void): () => void {
  listeners.add(cb);
  return () => {
    listeners.delete(cb);
  };
}
