/** The floating folder picker: the New form's folder browser in a window you
 * can drag out of the way of the form it is filling.
 *
 * Non-modal on purpose — the field it fills, and the rest of the dialog, stay
 * usable while it is open — and portalled to <body> above every dialog but
 * under the toast. One window at a time (lib/folderPicker): a second field's
 * Browse… re-targets it.
 *
 * Keys: Escape closes it (no pick) BEFORE any dialog under it sees the key —
 * the listener is installed on window in the capture phase when this module
 * loads, which is before any Modal mounts, so it runs first; the one key it
 * leaves alone is Escape inside the "+ Folder" name box, which closes just that
 * row. Enter is "Use this folder" (on a row: that row), ↑/↓ walk the rows, and
 * → / ← open / leave a folder as they always did. */

import { useEffect, useLayoutEffect, useRef, useState, type KeyboardEvent, type PointerEvent } from "react";
import { createPortal } from "react-dom";
import { FolderBrowser } from "./dialogs/newSession/FolderBrowser";
import {
  PICKER_DEFAULT,
  clampRect,
  closeFolderPicker,
  defaultRect,
  dragRect,
  getFolderPicker,
  loadPickerRect,
  pickFolder,
  savePickerRect,
  useFolderPicker,
  type PickerRect,
} from "../lib/folderPicker";

/** Escape, for the window: true when it took the key. Exported for the test. */
export function pickerEscape(e: Pick<KeyboardEvent, "key"> & { target: EventTarget | null }): boolean {
  if (e.key !== "Escape" || !getFolderPicker()) return false;
  const t = e.target as Element | null;
  // The new-folder row owns its own Escape (it closes the row, not the window).
  if (t && typeof t.closest === "function" && t.closest(".fpw .rb-new")) return false;
  return true;
}

if (typeof window !== "undefined") {
  window.addEventListener(
    "keydown",
    (e) => {
      if (!pickerEscape(e)) return;
      e.preventDefault();
      e.stopPropagation();
      closeFolderPicker();
    },
    true
  );
}

function viewport() {
  return { vw: window.innerWidth, vh: window.innerHeight };
}

export function FolderPickerWindow() {
  const req = useFolderPicker();
  const [rect, setRect] = useState<PickerRect | null>(null);
  const [chosen, setChosen] = useState("");
  const rootRef = useRef<HTMLDivElement | null>(null);
  const drag = useRef<{ sx: number; sy: number; from: PickerRect } | null>(null);
  const rectRef = useRef<PickerRect | null>(null);
  rectRef.current = rect;

  // Each request starts from its field's value, and the window opens where it
  // was left (else beside the button that opened it).
  useLayoutEffect(() => {
    if (!req) return;
    setChosen((req.selected ?? req.start).trim());
    if (!rectRef.current) {
      const { vw, vh } = viewport();
      const saved = loadPickerRect();
      const a = req.anchor?.getBoundingClientRect?.() || null;
      setRect(saved ? clampRect(saved, vw, vh) : defaultRect(a, vw, vh, PICKER_DEFAULT));
    }
  }, [req?.id]);

  // Keys go to the window as it opens (it is drawn one render after the
  // request lands, once it knows where).
  const shown = !!req && !!rect;
  useEffect(() => {
    if (shown) rootRef.current?.focus({ preventScroll: true });
  }, [req?.id, shown]);

  // A smaller window (a phone turned, a desktop window resized) pulls it back on.
  useEffect(() => {
    if (!req) return;
    const onResize = () => {
      const r = rectRef.current;
      if (!r) return;
      const { vw, vh } = viewport();
      setRect(clampRect(r, vw, vh));
    };
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, [req]);

  // The corner handle resizes it (CSS resize): keep the size it was left at.
  useEffect(() => {
    const el = rootRef.current;
    if (!req || !el || typeof ResizeObserver === "undefined") return;
    let timer = 0;
    const ro = new ResizeObserver(() => {
      const r = rectRef.current;
      if (!r || drag.current) return;
      const w = el.offsetWidth;
      const h = el.offsetHeight;
      if (Math.abs(w - r.w) < 2 && Math.abs(h - r.h) < 2) return;
      window.clearTimeout(timer);
      timer = window.setTimeout(() => {
        const { vw, vh } = viewport();
        const next = clampRect({ ...r, w, h }, vw, vh);
        setRect(next);
        savePickerRect(next);
      }, 150);
    });
    ro.observe(el);
    return () => {
      ro.disconnect();
      window.clearTimeout(timer);
    };
  }, [req?.id, shown]);

  if (!req || !rect) return null;

  const onBarDown = (e: PointerEvent<HTMLDivElement>) => {
    if (e.button !== 0 || (e.target as Element).closest("button")) return;
    e.preventDefault();
    drag.current = { sx: e.clientX, sy: e.clientY, from: rect };
    e.currentTarget.setPointerCapture?.(e.pointerId);
  };
  const onBarMove = (e: PointerEvent<HTMLDivElement>) => {
    const d = drag.current;
    if (!d) return;
    const { vw, vh } = viewport();
    setRect(dragRect(d.from, d.sx, d.sy, e.clientX, e.clientY, vw, vh));
  };
  const onBarUp = (e: PointerEvent<HTMLDivElement>) => {
    if (!drag.current) return;
    drag.current = null;
    e.currentTarget.releasePointerCapture?.(e.pointerId);
    if (rectRef.current) savePickerRect(rectRef.current);
  };

  const rows = () =>
    Array.from(rootRef.current?.querySelectorAll<HTMLButtonElement>("#rb-list .rb-item") || []);

  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    const t = e.target as HTMLElement;
    if (t.closest(".rb-new")) return; // the name box handles its own keys
    if (e.key === "Enter") {
      // A child row: Enter takes THAT row. The "use this folder — …" row and
      // the window's own buttons keep their native click. Anything else (the
      // window itself, the path readout) uses the chosen folder.
      const row = t.closest<HTMLElement>(".rb-item:not(.rb-use)");
      if (row?.dataset.path) {
        e.preventDefault();
        e.stopPropagation();
        pickFolder(row.dataset.path);
      } else if (t.tagName !== "BUTTON" && chosen) {
        e.preventDefault();
        e.stopPropagation();
        pickFolder(chosen);
      }
    } else if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      const list = rows();
      if (!list.length) return;
      e.preventDefault();
      const at = list.indexOf(t as HTMLButtonElement);
      const step = e.key === "ArrowDown" ? 1 : -1;
      const next = at < 0 ? (step > 0 ? 0 : list.length - 1) : Math.max(0, Math.min(list.length - 1, at + step));
      list[next].focus();
    }
  };

  const win = (
    <div
      ref={rootRef}
      className="fpw"
      id="folder-picker"
      role="dialog"
      aria-modal="false"
      aria-labelledby="fpw-title"
      tabIndex={-1}
      style={{ left: rect.x, top: rect.y, width: rect.w, height: rect.h }}
      onKeyDown={onKeyDown}
    >
      <div
        className="fpw-bar"
        title="Drag to move"
        onPointerDown={onBarDown}
        onPointerMove={onBarMove}
        onPointerUp={onBarUp}
        onPointerCancel={onBarUp}
      >
        <span className="fpw-title" id="fpw-title">
          {req.title || "Pick a folder"}
        </span>
        <button
          type="button"
          className="fpw-close"
          id="fpw-close"
          aria-label="Close"
          title="Close without picking (Esc)"
          onClick={() => closeFolderPicker(req.id)}
        >
          ✕
        </button>
      </div>
      <div className="fpw-body">
        <FolderBrowser
          key={req.id}
          device={req.device || ""}
          initialPath={req.start.trim()}
          selected={chosen}
          fallback
          onSelect={(p) => {
            setChosen(p);
            req.onSelect?.(p);
          }}
          onPick={(p) => pickFolder(p)}
          onListing={(path, entries) =>
            // Walking somewhere the chosen folder is not on screen (← up past it,
            // a seed that did not exist) makes the folder you are in the choice:
            // "Use this folder" must never take a folder you cannot see.
            setChosen((c) => (c === path || entries.includes(c) ? c : path))
          }
        />
      </div>
      <div className="fpw-foot">
        <span className="fpw-chosen" title={chosen}>
          {/* An LTR run inside the RTL box: the box clips the HEAD of a long
              path, and the run keeps a path's leading "/" at its start (bare
              RTL text moves the punctuation to the far end). */}
          <span dir="ltr">{chosen}</span>
        </span>
        <button
          type="button"
          className="fpw-use"
          id="fpw-use"
          disabled={!chosen}
          title={chosen ? "Fill the field with " + chosen + " (Enter)" : undefined}
          onClick={() => pickFolder(chosen)}
        >
          Use this folder
        </button>
      </div>
    </div>
  );
  return createPortal(win, document.body);
}
