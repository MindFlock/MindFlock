/** A sticky note (docs/flights.md "Round 15" → `objects`): a sky object
 * (layout key `note:<id>`) the owner writes on. ⋯ → "Add a note", or a
 * right-click on empty sky, puts one at the pointer; a click on its body
 * edits it (a textarea; blur saves), its head drags it (`onHeadDown`), its
 * corner sizes it (`onResize`, the delta over the zoom), × or Delete /
 * Backspace with the note focused removes it (the host's Undo toast). A
 * note dropped on a line is attached to it (`attached`: a pin glyph) and
 * moves with the line. Stored server-side (api/skyApi, `/api/sky/notes`). */

import { useEffect, useRef, useState, type KeyboardEvent as ReactKeyboardEvent, type PointerEvent as ReactPointerEvent } from "react";
import type { SkyNote } from "../../api/skyApi";
import { NOTE_MIN } from "../../lib/skyLayout";
import { noteSize } from "../../lib/skyObjects";
import "./StickyNote.css";

export interface StickyNoteProps {
  note: SkyNote;
  zoom?: number;
  editing?: boolean;
  onEdit?(on: boolean): void;
  onChange?(text: string): void;
  onResize?(w: number, h: number): void;
  onRemove?(): void;
  onHeadDown?(e: ReactPointerEvent): void;
  draggable?: boolean;
  onMeasure?(w: number, h: number): void;
  attached?: boolean;
}

/** The most a note holds. */
export const NOTE_MAX_CHARS = 2000;

const headPress = (e: ReactPointerEvent) => e.button === 0 && !(e.target as Element).closest("button, a, input, textarea, [data-no-drag]");

export function StickyNote({ note, zoom = 1, editing = false, onEdit, onChange, onResize, onRemove, onHeadDown, draggable = true, onMeasure, attached = false }: StickyNoteProps) {
  const rootRef = useRef<HTMLDivElement>(null);
  const areaRef = useRef<HTMLTextAreaElement>(null);
  const [draft, setDraft] = useState(note.text);
  const [live, setLive] = useState<{ w: number; h: number } | null>(null);
  const grab = useRef<{ id: number; sx: number; sy: number; w: number; h: number } | null>(null);
  const size = live || noteSize(note);
  useEffect(() => {
    if (!editing) setDraft(note.text);
  }, [note.text, editing]);
  useEffect(() => {
    if (editing) areaRef.current?.focus({ preventScroll: true });
  }, [editing]);
  const measureRef = useRef(onMeasure);
  measureRef.current = onMeasure;
  useEffect(() => {
    const el = rootRef.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const read = () => {
      if (el.offsetWidth > 0 && el.offsetHeight > 0) measureRef.current?.(el.offsetWidth, el.offsetHeight);
    };
    read();
    const ro = new ResizeObserver(read);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  const save = () => {
    const text = draft.slice(0, NOTE_MAX_CHARS);
    if (text !== note.text) onChange?.(text);
    onEdit?.(false);
  };
  const resizeDown = (e: ReactPointerEvent<HTMLSpanElement>) => {
    if (e.button !== 0 || !onResize) return;
    e.preventDefault();
    e.stopPropagation();
    grab.current = { id: e.pointerId, sx: e.clientX, sy: e.clientY, w: size.w, h: size.h };
    try {
      e.currentTarget.setPointerCapture(e.pointerId);
    } catch {
      /* a synthetic event has no capture */
    }
  };
  const resizeMove = (e: ReactPointerEvent<HTMLSpanElement>) => {
    const g = grab.current;
    if (!g || g.id !== e.pointerId) return;
    const z = zoom > 0 ? zoom : 1;
    setLive({ w: Math.max(NOTE_MIN.w, Math.round(g.w + (e.clientX - g.sx) / z)), h: Math.max(NOTE_MIN.h, Math.round(g.h + (e.clientY - g.sy) / z)) });
  };
  const resizeUp = (e: ReactPointerEvent<HTMLSpanElement>) => {
    const g = grab.current;
    if (!g || g.id !== e.pointerId) return;
    grab.current = null;
    if (live && onResize) onResize(live.w, live.h);
    setLive(null);
  };
  const headDown = (e: ReactPointerEvent) => {
    // The note takes the keyboard (Delete removes it), then the host moves it.
    rootRef.current?.focus({ preventScroll: true });
    if (draggable && onHeadDown && headPress(e)) onHeadDown(e);
  };
  const onKey = (e: ReactKeyboardEvent<HTMLDivElement>) => {
    if (e.target !== e.currentTarget) return;
    if ((e.key === "Delete" || e.key === "Backspace") && onRemove) {
      e.preventDefault();
      e.stopPropagation();
      onRemove();
    } else if (e.key === "Enter" && onEdit) {
      e.preventDefault();
      onEdit(true);
    }
  };
  return (
    <div
      ref={rootRef}
      className={"sky-note" + (editing ? " is-editing" : "") + (attached ? " is-attached" : "")}
      data-note={note.id}
      data-sky-box
      tabIndex={-1}
      role="note"
      aria-label={"Note" + (note.text ? ": " + note.text.slice(0, 60) : "")}
      style={{ width: size.w, height: size.h }}
      onKeyDown={onKey}
    >
      <div className="sky-note-head" data-sky-head title={draggable && onHeadDown ? "Drag to move the note" : undefined} onPointerDown={headDown}>
        <span className="sky-note-pin" aria-hidden="true">
          {attached ? "📌" : "▪"}
        </span>
        {onRemove && (
          <button type="button" className="fl-icon-btn sky-note-x" aria-label="Remove the note" title="Remove (Delete)" data-no-drag onClick={onRemove}>
            ×
          </button>
        )}
      </div>
      {editing ? (
        <textarea
          ref={areaRef}
          className="sky-note-area"
          value={draft}
          maxLength={NOTE_MAX_CHARS}
          placeholder="Write a note…"
          onChange={(e) => setDraft(e.target.value)}
          onBlur={save}
          onKeyDown={(e) => {
            if (e.key === "Escape") {
              e.preventDefault();
              e.stopPropagation();
              setDraft(note.text);
              onEdit?.(false);
            }
          }}
        />
      ) : (
        <div className={"sky-note-text" + (note.text ? "" : " is-empty")} onClick={() => onEdit?.(true)} title="Click to edit">
          {note.text || "Write a note…"}
        </div>
      )}
      {onResize && (
        <span
          className="sky-note-resize skl-resize"
          role="separator"
          tabIndex={0}
          data-no-drag
          aria-label="Resize the note"
          title="Drag to resize the note"
          onPointerDown={resizeDown}
          onPointerMove={resizeMove}
          onPointerUp={resizeUp}
          onPointerCancel={resizeUp}
        />
      )}
    </div>
  );
}
