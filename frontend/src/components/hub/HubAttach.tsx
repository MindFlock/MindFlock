/** The hub in a composer (the Thread and Queue tabs' message boxes):
 *
 *  - typing `hub:` offers a picker of hub items (↑/↓, Enter or Tab picks,
 *    Esc closes) — the reference goes in as `hub:k7m2`, expanded by the server
 *    when the message is sent;
 *  - the paperclip attaches an item from the hub, or uploads files straight
 *    into it, and puts their references in;
 *  - a picture or file PASTED into the box goes into the hub, its reference
 *    into the text; a hub item DRAGGED onto the box drops its reference.
 *
 * `useHubComposer` wires a controlled <textarea>; it never owns the text. */

import { useEffect, useMemo, useRef, useState, type ClipboardEvent, type DragEvent, type KeyboardEvent, type ReactNode, type RefObject } from "react";
import { spliceAtCaret } from "../../lib/fileDropTextarea";
import {
  HUB_MIME,
  addFiles,
  addedToast,
  humanSize,
  matchItems,
  refTokenAt,
  useHub,
  useHubUi,
  type HubItem,
} from "../../lib/hub";
import { useConfig } from "../../state/queries";
import { toast } from "../../lib/toast";

interface Opts {
  box: RefObject<HTMLTextAreaElement | null>;
  value: string;
  setValue(v: string): void;
  /** The session this composer belongs to: "Send to session…" and drops on
   * its row can put text into it (useHubUi.insert). */
  title?: string;
}

export interface HubComposer {
  /** Call from the textarea's onChange AFTER setting the value. */
  onInput(el: HTMLTextAreaElement): void;
  /** Call first from onKeyDown; true = the picker used the key. */
  onKeyDown(e: KeyboardEvent<HTMLTextAreaElement>): boolean;
  onPaste(e: ClipboardEvent<HTMLTextAreaElement>): void;
  onDragOver(e: DragEvent<HTMLElement>): void;
  onDragLeave(e: DragEvent<HTMLElement>): void;
  onDrop(e: DragEvent<HTMLElement>): void;
  /** Insert text at the caret. */
  insert(text: string): void;
  dropping: boolean;
  /** The picker popup (render inside a position:relative box). */
  popup: ReactNode;
  /** The paperclip (render in the composer's footer). */
  attach: ReactNode;
}

export function useHubComposer({ box, value, setValue, title }: Opts): HubComposer {
  const guest = !!useConfig().data?.guest;
  const [tok, setTok] = useState<{ start: number; query: string } | null>(null);
  const [sel, setSel] = useState(0);
  const [dropping, setDropping] = useState(false);
  const hub = useHub(!guest && !!tok);
  const matches = useMemo(() => (tok ? matchItems(hub.data?.items || [], tok.query) : []), [tok, hub.data]);
  const valueRef = useRef(value);
  valueRef.current = value;

  const place = (next: string, caret: number) => {
    setValue(next);
    setTimeout(() => {
      const el = box.current;
      if (!el) return;
      el.focus();
      el.setSelectionRange(caret, caret);
    }, 0);
  };

  const insert = (text: string) => {
    const el = box.current;
    const v = valueRef.current;
    const s = el ? el.selectionStart ?? v.length : v.length;
    const e = el ? el.selectionEnd ?? v.length : v.length;
    const r = spliceAtCaret(v, s, e, text.trim());
    place(r.value, r.caret);
  };

  // "Send to session…" (and a row drop on a busy session) hands text here.
  const pending = useHubUi((s) => (title && s.insert?.title === title ? s.insert : null));
  const seen = useRef(0);
  useEffect(() => {
    if (!pending || pending.n === seen.current) return;
    seen.current = pending.n;
    insert(pending.text);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pending?.n]);

  const choose = (it: HubItem) => {
    if (!tok) return;
    const v = valueRef.current;
    const caret = tok.start + 4 + tok.query.length;
    const ins = it.ref + " ";
    place(v.slice(0, tok.start) + ins + v.slice(caret).replace(/^ /, ""), tok.start + ins.length);
    setTok(null);
  };

  const upload = async (files: File[]) => {
    const r = await addFiles(files);
    addedToast({ added: [], refused: r.refused });
    if (r.added.length) {
      insert(r.added.map((a) => a.ref).join(" "));
      toast(
        (r.added.length === 1 ? r.added[0].name + " is" : r.added.length + " files are") +
          " in the hub — the reference is in your message"
      );
    }
    if (r.refused.some((x) => x.secret))
      useHubUi.getState().setOpen(true);
  };

  const isHub = (e: DragEvent) => Array.from(e.dataTransfer.types || []).includes(HUB_MIME);

  return {
    insert,
    dropping,
    onInput(el) {
      if (guest) return;
      const t = refTokenAt(el.value, el.selectionStart ?? el.value.length);
      setTok(t);
      setSel(0);
    },
    onKeyDown(e) {
      if (!tok || !matches.length) {
        if (tok && e.key === "Escape") {
          e.preventDefault();
          e.stopPropagation();
          setTok(null);
          return true;
        }
        return false;
      }
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        setSel((i) => (i + (e.key === "ArrowDown" ? 1 : -1) + matches.length) % matches.length);
        return true;
      }
      if ((e.key === "Enter" && !e.ctrlKey && !e.metaKey && !e.altKey && !e.shiftKey) || e.key === "Tab") {
        e.preventDefault();
        choose(matches[Math.min(sel, matches.length - 1)]);
        return true;
      }
      if (e.key === "Escape") {
        e.preventDefault();
        e.stopPropagation();
        setTok(null);
        return true;
      }
      return false;
    },
    onPaste(e) {
      if (guest) return;
      const files = Array.from(e.clipboardData?.files || []);
      if (!files.length) return;
      e.preventDefault();
      void upload(files);
    },
    onDragOver(e) {
      if (guest || !isHub(e)) return;
      e.preventDefault();
      e.stopPropagation();
      e.dataTransfer.dropEffect = "copy";
      setDropping(true);
    },
    onDragLeave(e) {
      if (!e.currentTarget.contains(e.relatedTarget as Node | null)) setDropping(false);
    },
    onDrop(e) {
      if (guest || !isHub(e)) return;
      e.preventDefault();
      e.stopPropagation();
      setDropping(false);
      try {
        const it = JSON.parse(e.dataTransfer.getData(HUB_MIME)) as { ref: string };
        if (it?.ref) insert(it.ref);
      } catch {
        /* not ours */
      }
    },
    popup:
      tok && matches.length ? (
        <ul className="hub-pick" role="listbox" aria-label="Hub items — Enter or Tab puts the reference in">
          <li className="hub-pick-head" role="presentation" aria-hidden="true">
            Hub items · Enter puts the reference in
          </li>
          {matches.map((it, i) => (
            <li
              key={it.id}
              role="option"
              aria-selected={i === sel}
              onMouseDown={(e) => {
                e.preventDefault();
                choose(it);
              }}
            >
              <span className="hub-pick-ref">{it.ref}</span>
              <span className="hub-pick-name">{it.name}</span>
              <span className="hub-pick-ref">{humanSize(it.size)}</span>
            </li>
          ))}
        </ul>
      ) : tok && hub.data && !hub.data.items.length ? (
        <ul className="hub-pick" role="listbox" aria-label="Hub items">
          <li className="hub-pick-head" role="presentation">
            The hub is empty — paste a file here, or use the paperclip
          </li>
        </ul>
      ) : null,
    attach: guest ? null : <HubAttachButton onInsert={insert} onUpload={upload} />,
  };
}

/** The paperclip: attach an item from the hub, or upload into it. */
export function HubAttachButton(props: { onInsert(text: string): void; onUpload(files: File[]): Promise<void> }) {
  const [open, setOpen] = useState(false);
  const hub = useHub(open);
  const wrap = useRef<HTMLDivElement>(null);
  const file = useRef<HTMLInputElement>(null);
  const items = (hub.data?.items || []).slice(0, 8);

  useEffect(() => {
    if (!open) return;
    wrap.current?.querySelector<HTMLElement>("[data-first]")?.focus();
    const onDown = (e: MouseEvent) => {
      if (!wrap.current?.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [open]);

  const onKey = (e: KeyboardEvent<HTMLDivElement>) => {
    if (e.key === "Escape") {
      e.preventDefault();
      e.stopPropagation();
      setOpen(false);
      return;
    }
    if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
    const rows = Array.from(wrap.current?.querySelectorAll<HTMLElement>(".hub-pick button") || []);
    if (!rows.length) return;
    e.preventDefault();
    const at = rows.indexOf(document.activeElement as HTMLElement);
    rows[(at + (e.key === "ArrowDown" ? 1 : -1) + rows.length) % rows.length].focus();
  };

  return (
    <div className="hub-attach" ref={wrap} onKeyDown={onKey}>
      <button
        type="button"
        className="hub-attach-btn"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label="Attach from the hub"
        title="Attach from the hub — or paste a picture or file straight into the box"
        onClick={() => setOpen((v) => !v)}
      >
        <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <path d="M20.5 11.5l-8.2 8.2a5 5 0 0 1-7.1-7.1l8.6-8.6a3.4 3.4 0 0 1 4.8 4.8l-8.6 8.6a1.7 1.7 0 0 1-2.4-2.4l7.9-7.9" />
        </svg>
        Hub
      </button>
      <input
        ref={file}
        type="file"
        multiple
        hidden
        aria-hidden="true"
        tabIndex={-1}
        onChange={(e) => {
          const fs = Array.from(e.target.files || []);
          e.target.value = "";
          setOpen(false);
          void props.onUpload(fs);
        }}
      />
      {open && (
        <ul className="hub-pick" role="menu" aria-label="Attach from the hub">
          <li role="none">
            <button type="button" role="menuitem" data-first="" onClick={() => file.current?.click()}>
              Upload files to the hub…
            </button>
          </li>
          {items.length > 0 && (
            <li className="hub-pick-head" role="presentation">
              Recent in the hub
            </li>
          )}
          {items.map((it) => (
            <li key={it.id} role="none">
              <button
                type="button"
                role="menuitem"
                onClick={() => {
                  setOpen(false);
                  props.onInsert(it.ref);
                }}
              >
                <span className="hub-pick-ref">{it.ref}</span> <span className="hub-pick-name">{it.name}</span>
              </button>
            </li>
          ))}
          {hub.data && !items.length && (
            <li className="hub-pick-head" role="presentation">
              The hub is empty
            </li>
          )}
          <li role="none">
            <button
              type="button"
              role="menuitem"
              onClick={() => {
                setOpen(false);
                useHubUi.getState().setOpen(true);
              }}
            >
              Open the hub
            </button>
          </li>
        </ul>
      )}
    </div>
  );
}
