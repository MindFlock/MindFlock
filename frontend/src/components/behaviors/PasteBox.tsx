/** The exact text a person pastes for a Verify step (a chat command, a URL, a
 * value — the step's `paste`), in a monospace box with a one-click Copy.
 *
 * Copies through `copyText` (lib/clipboard), never navigator.clipboard
 * directly: that is blocked in the Electron renderer. Every press inside the
 * box stops at the box — the cards and panes it sits in drag, select and
 * toggle on mouse-down / click, and copying must do none of those. The
 * button reads "Copied" for a moment after a copy.
 *
 * Its look is in BehaviorsCard.css (`.vc-paste*`), which every caller already
 * loads: this module imports no stylesheet, so adding it moves no CSS. */

import { useEffect, useState, type JSX } from "react";
import { copyText } from "../../lib/clipboard";

/** How long the button reads "Copied" / "Copy failed". */
export const COPIED_MS = 1500;

export type PasteCopyState = "" | "copied" | "failed";

/** The button's words for each state (pure). */
export function pasteCopyLabel(state: PasteCopyState): string {
  return state === "copied" ? "Copied" : state === "failed" ? "Copy failed" : "Copy";
}

/** A Copy press: stop it at the box, copy `text`, say how it went. */
export async function copyPaste(
  e: { stopPropagation(): void; preventDefault?(): void },
  text: string,
  copy: (t: string) => Promise<boolean> = copyText,
): Promise<PasteCopyState> {
  e.stopPropagation();
  e.preventDefault?.();
  return (await copy(text)) ? "copied" : "failed";
}

const stop = (e: { stopPropagation(): void }) => e.stopPropagation();

export function PasteBox({ text }: { text?: string }): JSX.Element | null {
  const [state, setState] = useState<PasteCopyState>("");
  useEffect(() => {
    if (!state) return;
    const t = setTimeout(() => setState(""), COPIED_MS);
    return () => clearTimeout(t);
  }, [state]);
  const value = String(text || "").trim();
  if (!value) return null;
  return (
    <span className="vc-paste" data-no-drag onMouseDown={stop} onPointerDown={stop} onClick={stop} onDoubleClick={stop}>
      <code className="vc-paste-text">{value}</code>
      <button
        type="button"
        className={"test-btn vc-paste-copy" + (state ? " is-" + state : "")}
        title="Copy this, then paste it where the step says"
        aria-label={state ? pasteCopyLabel(state) : "Copy: " + value}
        aria-live="polite"
        onClick={(e) => void copyPaste(e, value).then(setState)}
      >
        {pasteCopyLabel(state)}
      </button>
    </span>
  );
}
