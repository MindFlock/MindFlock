/** The Hub panel (lib/hub.ts; docs/web-ui.md "Hub"): a drawer on the right
 * that lists what is in the hub — newest first, a thumbnail for a picture, the
 * first lines of a text, an icon and size for any other file — and is the one
 * place items are added (drop, Add files…, New snippet, or Ctrl/⌘+V while it
 * is open) and used (Copy reference, Send to session…, and the ⋯ menu).
 *
 * Not a modal: the sessions stay usable beside it, so an item can be DRAGGED
 * onto a session's row, its composer or its terminal. Escape closes it while
 * focus is inside it. Electron has no window.prompt/confirm: rename, note and
 * "add it anyway" are in-page. */

import { useEffect, useMemo, useRef, useState, type DragEvent, type KeyboardEvent } from "react";
import { useConfig } from "../../state/queries";
import { relTime } from "../../lib/format";
import {
  HUB_MIME,
  addFiles,
  addText,
  addedToast,
  canCopyImage,
  copyImage,
  copyItemText,
  copyPath,
  copyRef,
  download,
  expiryLabel,
  humanSize,
  kindLabel,
  rawUrl,
  removeItem,
  updateItem,
  usageLine,
  useHub,
  useHubUi,
  whoLabel,
  type HubItem,
} from "../../lib/hub";
import { HubSendDialog } from "./HubSendDialog";
import "./HubPanel.css";

/** Whether a key/paste target is a place the person types (then the panel
 * leaves the event alone). */
function editable(t: EventTarget | null): boolean {
  const el = t as HTMLElement | null;
  if (!el || !el.tagName) return false;
  return el.isContentEditable || el.tagName === "TEXTAREA" || (el.tagName === "INPUT" && (el as HTMLInputElement).type !== "button");
}

export function HubPanel() {
  const open = useHubUi((s) => s.open);
  const guest = !!useConfig().data?.guest;
  // A #hub link (and the preview's `--view hub`) opens it.
  useEffect(() => {
    if (location.hash === "#hub") useHubUi.getState().setOpen(true);
  }, []);
  if (!open || guest) return <HubSendDialog />;
  return (
    <>
      <HubDrawer />
      <HubSendDialog />
    </>
  );
}

function HubDrawer() {
  const setOpen = useHubUi((s) => s.setOpen);
  const q = useHub(true);
  const list = q.data;
  const items = list?.items || [];
  const ref = useRef<HTMLElement>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const [dragOver, setDragOver] = useState(false);
  const [busy, setBusy] = useState("");
  const [snippet, setSnippet] = useState<string | null>(null);
  const [secrets, setSecrets] = useState<File[]>([]);
  const [filter, setFilter] = useState("");

  // Take focus on open (so Ctrl/⌘+V lands here at once), give it back on close.
  useEffect(() => {
    const prev = document.activeElement as HTMLElement | null;
    ref.current?.focus({ preventScroll: true });
    return () => {
      if (prev && prev.isConnected && prev !== document.body) prev.focus({ preventScroll: true });
    };
  }, []);

  const add = async (files: File[], secretOk = false) => {
    if (!files.length) return;
    setBusy(files.length === 1 ? "Adding " + (files[0].name || "the file") + "…" : "Adding " + files.length + " files…");
    const r = await addFiles(files, { secretOk });
    setBusy("");
    addedToast(r);
    const s = r.refused.filter((x) => x.secret).map((x) => x.file);
    if (s.length) setSecrets((prev) => [...prev, ...s]);
  };

  // Paste anywhere while the panel is open — unless the person is typing in
  // a box (the composer, a terminal, the snippet editor), which keeps it.
  useEffect(() => {
    const onPaste = (e: ClipboardEvent) => {
      const inside = !!ref.current?.contains(e.target as Node);
      if (editable(e.target) || (!inside && document.activeElement && document.activeElement !== document.body && !ref.current?.contains(document.activeElement)))
        return;
      const cd = e.clipboardData;
      if (!cd) return;
      const files = Array.from(cd.files || []);
      const text = cd.getData("text/plain");
      if (!files.length && !text.trim()) return;
      e.preventDefault();
      if (files.length) void add(files);
      else {
        setBusy("Adding the text…");
        addText(text)
          .then((it) => addedToast({ added: [it], refused: [] }))
          .catch((err) => addedToast({ added: [], refused: [{ file: new File([], "the text"), error: String(err?.message || err), secret: false }] }))
          .finally(() => setBusy(""));
      }
    };
    document.addEventListener("paste", onPaste);
    return () => document.removeEventListener("paste", onPaste);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const onKeyDown = (e: KeyboardEvent) => {
    if (e.key === "Escape" && !e.defaultPrevented) {
      e.preventDefault();
      setOpen(false);
    }
  };

  const hasFiles = (e: DragEvent) => Array.from(e.dataTransfer.types || []).includes("Files");
  const shown = useMemo(() => {
    const f = filter.trim().toLowerCase();
    if (!f) return items;
    return items.filter(
      (i) => i.name.toLowerCase().includes(f) || i.ref.includes(f) || (i.note || "").toLowerCase().includes(f) || whoLabel(i).toLowerCase().includes(f)
    );
  }, [items, filter]);

  return (
    <aside
      id="hub-panel"
      ref={ref}
      tabIndex={-1}
      role="complementary"
      aria-label="Hub — files, pictures and snippets every session can use"
      className={dragOver ? "hub-drop" : undefined}
      onKeyDown={onKeyDown}
      onDragOver={(e) => {
        if (!hasFiles(e)) return;
        e.preventDefault();
        e.dataTransfer.dropEffect = "copy";
        setDragOver(true);
      }}
      onDragLeave={(e) => {
        if (!e.currentTarget.contains(e.relatedTarget as Node | null)) setDragOver(false);
      }}
      onDrop={(e) => {
        if (!hasFiles(e)) return;
        e.preventDefault();
        e.stopPropagation();
        setDragOver(false);
        void add(Array.from(e.dataTransfer.files || []));
      }}
    >
      <header className="hub-head">
        <div className="hub-title">
          <h2>Hub</h2>
          <p className="hub-sub">
            Files, pictures and snippets every session can use. Each item has a reference like <code>hub:k7m2</code> —
            put it in any prompt and the agent gets the file.
          </p>
        </div>
        <button type="button" className="hub-x" aria-label="Close the hub" title="Close (Esc)" onClick={() => setOpen(false)}>
          ✕
        </button>
      </header>

      <div className="hub-add">
        <button type="button" className="hub-btn primary" onClick={() => fileInput.current?.click()}>
          Add files…
        </button>
        <button type="button" className="hub-btn" onClick={() => setSnippet(snippet === null ? "" : null)} aria-expanded={snippet !== null}>
          New snippet
        </button>
        <span className="hub-add-hint">or drop here · paste with {navigator.platform?.startsWith("Mac") ? "⌘" : "Ctrl+"}V</span>
        <input
          ref={fileInput}
          type="file"
          multiple
          hidden
          aria-hidden="true"
          tabIndex={-1}
          onChange={(e) => {
            const fs = Array.from(e.target.files || []);
            e.target.value = "";
            void add(fs);
          }}
        />
      </div>

      {snippet !== null && (
        <SnippetEditor
          value={snippet}
          onChange={setSnippet}
          onDone={() => setSnippet(null)}
        />
      )}

      {busy && (
        <p className="hub-busy" role="status">
          {busy}
        </p>
      )}

      {secrets.map((f, i) => (
        <div key={f.name + i} className="hub-confirm" role="alertdialog" aria-label={"Add " + f.name + " anyway?"}>
          <p>
            <strong>{f.name} looks like a secret</strong> (an env file, key or credential). Every session can read the hub.
          </p>
          <div className="hub-confirm-actions">
            <button type="button" className="hub-btn" autoFocus onClick={() => setSecrets((s) => s.filter((_, j) => j !== i))}>
              Don't add it
            </button>
            <button
              type="button"
              className="hub-btn danger"
              onClick={() => {
                setSecrets((s) => s.filter((_, j) => j !== i));
                void add([f], true);
              }}
            >
              Add it anyway
            </button>
          </div>
        </div>
      ))}

      {items.length > 6 && (
        <input
          className="hub-filter"
          type="search"
          placeholder="Find an item…"
          aria-label="Find an item in the hub"
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
        />
      )}

      <div className="hub-list-wrap">
        {q.isLoading ? (
          <p className="hub-empty muted">Loading the hub…</p>
        ) : q.isError && !list ? (
          <p className="hub-empty" role="alert">
            Couldn't load the hub.{" "}
            <button type="button" className="hub-link" onClick={() => void q.refetch()}>
              Try again
            </button>
          </p>
        ) : !items.length ? (
          <div className="hub-empty-big">
            <div className="hub-empty-art" aria-hidden="true">
              ⇪
            </div>
            <p>
              <strong>Nothing in the hub yet.</strong>
            </p>
            <p>
              Drop a file here, paste a screenshot ({navigator.platform?.startsWith("Mac") ? "⌘" : "Ctrl+"}V), or add a snippet.
              Each item gets a reference like <code>hub:k7m2</code> — put it in any session's prompt, or use{" "}
              <em>Send to session…</em>
            </p>
            <p className="muted">Agents can add files here too (the MindFlock tool "hub").</p>
          </div>
        ) : !shown.length ? (
          <p className="hub-empty muted">Nothing matches "{filter}".</p>
        ) : (
          <ul className="hub-list" aria-label="Hub items, newest first">
            {shown.map((it) => (
              <HubCard key={it.id} item={it} />
            ))}
          </ul>
        )}
      </div>

      {list && (
        <footer className="hub-foot">
          <span>{usageLine(list)}</span>
          <span className="muted" title={"Settings: hub.keep_days (" + list.keep_days + "). Pin an item to keep it."}>
            Unpinned items go {list.keep_days} days after their last use
          </span>
        </footer>
      )}
    </aside>
  );
}

function SnippetEditor(props: { value: string; onChange(v: string): void; onDone(): void }) {
  const [saving, setSaving] = useState(false);
  const save = async () => {
    if (!props.value.trim() || saving) return;
    setSaving(true);
    try {
      const it = await addText(props.value);
      addedToast({ added: [it], refused: [] });
      props.onDone();
    } catch (err) {
      addedToast({ added: [], refused: [{ file: new File([], "the snippet"), error: String((err as Error)?.message || err), secret: false }] });
    } finally {
      setSaving(false);
    }
  };
  return (
    <div className="hub-snippet">
      <textarea
        autoFocus
        rows={5}
        aria-label="The snippet's text"
        placeholder="Paste or type text to share — a log line, a command, a note for an agent…"
        value={props.value}
        onChange={(e) => props.onChange(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
            e.preventDefault();
            void save();
          } else if (e.key === "Escape") {
            e.preventDefault();
            e.stopPropagation();
            props.onDone();
          }
        }}
      />
      <div className="hub-snippet-actions">
        <button type="button" className="hub-btn" onClick={props.onDone}>
          Cancel
        </button>
        <button type="button" className="hub-btn primary" disabled={!props.value.trim() || saving} onClick={() => void save()} title="Add it to the hub (Ctrl+Enter)">
          Add snippet
        </button>
      </div>
    </div>
  );
}

function KindIcon({ item }: { item: HubItem }) {
  if (item.kind === "image") {
    return <img className="hub-thumb" src={rawUrl(item)} alt="" loading="lazy" draggable={false} />;
  }
  const ext = (item.name.match(/\.([A-Za-z0-9]{1,5})$/) || [])[1] || (item.kind === "text" ? "txt" : "");
  return (
    <span className={"hub-icon" + (item.kind === "text" ? " is-text" : "")} aria-hidden="true">
      <svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinejoin="round">
        <path d="M6 2.8h8l4.5 4.5v13.9H6z" />
        <path d="M14 2.8v4.5h4.5" />
        {item.kind === "text" && <path d="M8.6 12h7M8.6 15h7M8.6 18h4.5" />}
      </svg>
      {ext && <span className="hub-ext">{ext.slice(0, 4)}</span>}
    </span>
  );
}

function HubCard({ item }: { item: HubItem }) {
  const openSend = useHubUi((s) => s.openSend);
  const [menu, setMenu] = useState(false);
  const [editing, setEditing] = useState<"" | "name" | "note">("");
  const [val, setVal] = useState("");
  const menuRef = useRef<HTMLDivElement>(null);
  const menuBtn = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (!menu) return;
    menuRef.current?.querySelector<HTMLElement>('[role="menuitem"]')?.focus();
    const onDown = (e: MouseEvent) => {
      if (!menuRef.current?.contains(e.target as Node) && e.target !== menuBtn.current) setMenu(false);
    };
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [menu]);

  const startEdit = (what: "name" | "note") => {
    setMenu(false);
    setVal(what === "name" ? item.name : item.note || "");
    setEditing(what);
  };
  const saveEdit = async () => {
    if (editing === "name" && !val.trim()) return;
    const patch = editing === "name" ? { name: val } : { note: val };
    setEditing("");
    await updateItem(item, patch);
  };

  const menuKey = (e: KeyboardEvent) => {
    if (e.key === "Escape") {
      e.preventDefault();
      e.stopPropagation();
      setMenu(false);
      menuBtn.current?.focus();
      return;
    }
    if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
    const rows = Array.from(menuRef.current?.querySelectorAll<HTMLElement>('[role="menuitem"]') || []);
    if (!rows.length) return;
    e.preventDefault();
    const at = rows.indexOf(document.activeElement as HTMLElement);
    rows[(at + (e.key === "ArrowDown" ? 1 : -1) + rows.length) % rows.length].focus();
  };

  const act = (fn: () => void) => () => {
    setMenu(false);
    fn();
  };
  const textual = item.kind === "text" || item.textual;
  const meta = [kindLabel(item), humanSize(item.size), whoLabel(item), relTime(item.added)].join(" · ");

  return (
    <li
      className={"hub-card" + (item.pinned ? " is-pinned" : "")}
      draggable={!editing}
      onDragStart={(e) => {
        e.dataTransfer.setData(HUB_MIME, JSON.stringify({ id: item.id, ref: item.ref, name: item.name }));
        e.dataTransfer.setData("text/plain", item.ref);
        e.dataTransfer.effectAllowed = "copy";
      }}
      title={"Drag onto a session, its message box or its terminal to use " + item.ref}
    >
      <div className="hub-card-top">
        <KindIcon item={item} />
        <div className="hub-card-main">
          {editing === "name" ? (
            <input
              className="hub-edit"
              autoFocus
              aria-label="New name"
              value={val}
              onChange={(e) => setVal(e.target.value)}
              onBlur={() => void saveEdit()}
              onKeyDown={(e) => {
                if (e.key === "Enter") void saveEdit();
                if (e.key === "Escape") {
                  e.preventDefault();
                  e.stopPropagation();
                  setEditing("");
                }
              }}
            />
          ) : (
            <span className="hub-name" title={item.name}>
              {item.pinned && (
                <span className="hub-pin" title="Pinned — kept past the expiry" aria-label="Pinned">
                  📌
                </span>
              )}
              {item.name}
            </span>
          )}
          <span className="hub-meta">{meta}</span>
          <div className="hub-row2">
            <button
              type="button"
              className="hub-ref"
              title={"Copy the reference — paste it in any prompt or terminal. " + expiryLabel(item)}
              aria-label={"Copy reference " + item.ref}
              onClick={() => void copyRef(item)}
            >
              <span className="hub-ref-code">{item.ref}</span>
              <span className="hub-ref-copy" aria-hidden="true">
                Copy
              </span>
            </button>
            <div className="hub-actions">
              <button type="button" className="hub-btn" onClick={() => openSend(item)} title="Put it in a session's prompt, or copy the file into its folder">
                Send to session…
              </button>
              <div className="hub-more">
                <button
                  ref={menuBtn}
                  type="button"
                  className="hub-btn hub-more-btn"
                  aria-label={"More for " + item.name}
                  aria-haspopup="menu"
                  aria-expanded={menu}
                  onClick={() => setMenu((v) => !v)}
                >
                  ⋯
                </button>
                {menu && (
                  <div className="hub-menu" role="menu" ref={menuRef} onKeyDown={menuKey} aria-label={"More for " + item.name}>
                    {textual && (
                      <button type="button" role="menuitem" onClick={act(() => void copyItemText(item))}>
                        Copy text
                      </button>
                    )}
                    {item.kind === "image" && canCopyImage() && (
                      <button type="button" role="menuitem" onClick={act(() => void copyImage(item))}>
                        Copy image
                      </button>
                    )}
                    <button type="button" role="menuitem" onClick={act(() => void copyPath(item))}>
                      Copy path
                    </button>
                    <button type="button" role="menuitem" onClick={act(() => download(item))}>
                      Download
                    </button>
                    <button type="button" role="menuitem" onClick={() => startEdit("name")}>
                      Rename
                    </button>
                    <button type="button" role="menuitem" onClick={() => startEdit("note")}>
                      {item.note ? "Edit note" : "Add a note"}
                    </button>
                    <button type="button" role="menuitem" onClick={act(() => void updateItem(item, { pinned: !item.pinned }))}>
                      {item.pinned ? "Unpin" : "Pin — keep it"}
                    </button>
                    <button type="button" role="menuitem" className="danger" onClick={act(() => void removeItem(item))}>
                      Remove
                    </button>
                  </div>
                )}
              </div>
            </div>
          </div>
        </div>
      </div>
      {textual && item.preview && <pre className="hub-preview">{item.preview}</pre>}
      {editing === "note" ? (
        <textarea
          className="hub-edit hub-note-edit"
          autoFocus
          rows={2}
          aria-label="Note"
          placeholder="A note for you (agents see it too)"
          value={val}
          onChange={(e) => setVal(e.target.value)}
          onBlur={() => void saveEdit()}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              void saveEdit();
            }
            if (e.key === "Escape") {
              e.preventDefault();
              e.stopPropagation();
              setEditing("");
            }
          }}
        />
      ) : (
        item.note && <p className="hub-note">{item.note}</p>
      )}
    </li>
  );
}
