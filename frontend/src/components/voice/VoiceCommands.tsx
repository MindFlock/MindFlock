/** The voice cheat sheet: what you can say, under the top bar's mic.
 *
 * Opened by the router's `commands` answer ("what can I say", "voice
 * commands": its `items`, its `say` read out by the mic) or, without voice,
 * by "Voice commands…" in the mic's options (right-click). Rows are grouped
 * when they carry a `group` (in the order the groups first appear), else one
 * flat list. Dismissed by Esc, a press anywhere outside it, its ×, or the
 * next phrase the mic hears (TopBarMic closes it). Esc here never reaches the
 * mic's own Esc (which would throw the dictation away): it is caught on the
 * window first. */

import { useEffect, useRef } from "react";
import { createPortal } from "react-dom";
import { placeUnder } from "../../lib/menuPlace";
import type { VoiceCommandItem } from "../../lib/voiceDesktop";
import "./VoiceCommands.css";

/** The sheet's width (px), as the mic's options. */
export const SHEET_W = 340;

/** `items` in groups, in the order each group first appears; a list with no
 * groups at all is one untitled group. Rows without words are dropped. */
export function groupCommands(items: readonly VoiceCommandItem[]): Array<{ group: string; items: VoiceCommandItem[] }> {
  const out: Array<{ group: string; items: VoiceCommandItem[] }> = [];
  for (const it of items) {
    if (!it || typeof it.say !== "string" || !it.say.trim()) continue;
    const g = String(it.group || "").trim();
    let at = out.find((o) => o.group === g);
    if (!at) out.push((at = { group: g, items: [] }));
    at.items.push(it);
  }
  return out;
}

export function VoiceCommands({
  items,
  anchor,
  onClose,
}: {
  items: readonly VoiceCommandItem[];
  /** The mic button: the sheet hangs under it. */
  anchor: HTMLElement | null;
  onClose(): void;
}) {
  const ref = useRef<HTMLDivElement | null>(null);
  const close = useRef(onClose);
  close.current = onClose;

  useEffect(() => {
    // Window, capture: before the mic's document-level Esc (which would
    // cancel a dictation) — this Esc only closes the sheet.
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      e.stopImmediatePropagation();
      e.preventDefault();
      close.current();
    };
    const onDown = (e: PointerEvent) => {
      const t = e.target as Node | null;
      if (t && ref.current?.contains(t)) return;
      close.current();
    };
    window.addEventListener("keydown", onKey, true);
    document.addEventListener("pointerdown", onDown, true);
    return () => {
      window.removeEventListener("keydown", onKey, true);
      document.removeEventListener("pointerdown", onDown, true);
    };
  }, []);

  const groups = groupCommands(items);
  const rect = anchor?.getBoundingClientRect();
  const w = Math.min(SHEET_W, window.innerWidth - 32);
  const place = rect ? placeUnder(rect, w, window.innerWidth, window.innerHeight) : null;
  const style = place
    ? { top: place.top + "px", left: place.left + "px", width: w + "px", maxHeight: place.maxHeight + "px" }
    : { top: "48px", right: "16px", width: w + "px" };

  return createPortal(
    <div className="voice-cmds" id="voice-commands" ref={ref} style={style} role="dialog" aria-label="Voice commands">
      <div className="voice-cmds-head">
        <span className="voice-cmds-title">What you can say</span>
        <button type="button" className="voice-cmds-close" aria-label="Close" title="Close (Esc)" onClick={() => close.current()}>
          ×
        </button>
      </div>
      {groups.length ? (
        groups.map((g) => (
          <section className="voice-cmds-group" key={g.group || "_"}>
            {g.group && <h4 className="voice-cmds-group-name">{g.group}</h4>}
            <dl className="voice-cmds-list">
              {g.items.map((it, i) => (
                <div className="voice-cmds-row" key={i + ":" + it.say}>
                  <dt className="voice-cmds-say">“{it.say}”</dt>
                  <dd className="voice-cmds-does">{it.does}</dd>
                </div>
              ))}
            </dl>
          </section>
        ))
      ) : (
        <p className="voice-cmds-empty">No commands to show.</p>
      )}
    </div>,
    document.body
  );
}
