/** Shared files (lib/hub.ts; docs/web-ui.md "Shared files"): a drawer on
 * the right that lists the shared files — newest first, under day headings,
 * filtered by kind (All · Pictures · Text · From agents) — and is the one
 * place items are added (drop, Add files…, New snippet, or Ctrl/⌘+V while it
 * is open) and used (Send to session…, the code chip, Pin, and the ⋯ menu).
 * Opened from the top bar's ⋯ menu, Alt+H or the command palette.
 *
 * Several at once: each card has a checkbox (shown on hover or focus, and on
 * every card once one is ticked; Shift+click ticks a range; "Select all"
 * ticks what the filter shows, Ctrl/⌘+A too). The selection bar then sends
 * them all in one message, puts them in the focused session's prompt,
 * copies their codes, pins / unpins or removes them — one request answered
 * per item (POST /api/hub/batch); what could not be done is said in the
 * drawer, item by item.
 *
 * Send to session… is a STEP of the drawer (HubSendStep: the content swaps,
 * with Back), never a dialog the drawer could cover.
 *
 * Not a modal: the sessions stay usable beside it, so an item — or the
 * whole selection — can be DRAGGED (by a card's grip) onto a session's row,
 * its message box or its terminal. Escape goes Back from the send step,
 * then clears the selection, then closes. Electron has no
 * window.prompt/confirm: rename, note, "add it anyway", "remove N?" and the
 * picture viewer are in-page. What it says goes to the top of the window
 * (HubNoticeBar), never over a session's prompt line. */

import { useEffect, useMemo, useRef, useState, type DragEvent, type KeyboardEvent, type MouseEvent as ReactMouseEvent } from "react";
import { api } from "../../api/client";
import { useConfig, useInstances } from "../../state/queries";
import { displayName, useUi } from "../../state/store";
import { errMsg, relTime } from "../../lib/format";
import {
  addFiles,
  addText,
  addedToast,
  canCopyImage,
  copyImage,
  copyItemText,
  copyPath,
  copyRef,
  copyRefs,
  download,
  dragSet,
  dropProblem,
  expiryLabel,
  humanSize,
  hush,
  kindLabel,
  pinItems,
  putInPrompt,
  raiseItemLimit,
  rawUrl,
  rebuildHub,
  refreshHub,
  removeItem,
  removeItems,
  say,
  startDrag,
  updateItem,
  usageLine,
  useHub,
  useHubUi,
  whoLabel,
  type HubItem,
} from "../../lib/hub";
import type { Instance } from "../../api/types";
import { Modal } from "../Modal";
import { HubSendStep, hubTargets } from "./HubSendStep";
import "./HubPanel.css";

/** Whether a key/paste target is a place the person types (then the panel
 * leaves the event alone). */
function editable(t: EventTarget | null): boolean {
  const el = t as HTMLElement | null;
  if (!el || !el.tagName) return false;
  return el.isContentEditable || el.tagName === "TEXTAREA" || (el.tagName === "INPUT" && (el as HTMLInputElement).type !== "button");
}

const PASTE_KEY = typeof navigator !== "undefined" && navigator.platform?.startsWith("Mac") ? "⌘V" : "Ctrl+V";

export type HubFilter = "all" | "pictures" | "text" | "agents";

export const FILTERS: { key: HubFilter; label: string }[] = [
  { key: "all", label: "All" },
  { key: "pictures", label: "Pictures" },
  { key: "text", label: "Text" },
  { key: "agents", label: "From agents" },
];

export function filterItems(items: HubItem[], f: HubFilter, find: string): HubItem[] {
  const q = find.trim().toLowerCase();
  return items.filter((i) => {
    if (f === "pictures" && i.kind !== "image") return false;
    if (f === "text" && !(i.kind === "text" || i.textual)) return false;
    if (f === "agents" && i.by !== "session") return false;
    if (!q) return true;
    return (
      i.name.toLowerCase().includes(q) ||
      i.ref.includes(q) ||
      (i.note || "").toLowerCase().includes(q) ||
      whoLabel(i).toLowerCase().includes(q) ||
      (i.preview || "").toLowerCase().includes(q)
    );
  });
}

/** A checkbox click with Shift held: every item between the last one
 * clicked (`anchor`) and this one, in the order shown, takes this one's new
 * state. Without Shift (or with no anchor still shown): just this one. */
export function rangeSelect(selected: string[], shownIds: string[], anchor: string | null, id: string, on: boolean, shift: boolean): string[] {
  let ids = [id];
  const a = anchor ? shownIds.indexOf(anchor) : -1;
  const b = shownIds.indexOf(id);
  if (shift && a >= 0 && b >= 0) ids = shownIds.slice(Math.min(a, b), Math.max(a, b) + 1);
  const set = new Set(selected);
  for (const x of ids) {
    if (on) set.add(x);
    else set.delete(x);
  }
  // In list order, so the bar and a send carry them as shown.
  return [...shownIds.filter((x) => set.has(x)), ...selected.filter((x) => set.has(x) && !shownIds.includes(x))];
}

/** Which of the selected items Pin and Unpin act on — the bar says both
 * counts when the selection is mixed, so a skip is never silent. */
export function pinSplit(items: Pick<HubItem, "pinned">[]): { toPin: number; toUnpin: number } {
  const pinned = items.filter((i) => i.pinned).length;
  return { toPin: items.length - pinned, toUnpin: pinned };
}

/** "Today" / "Yesterday" / "Mon 17 Nov" for an item's day. */
export function dayLabel(ts: number, now = Date.now() / 1000): string {
  const d = new Date(ts * 1000);
  const n = new Date(now * 1000);
  const day = (x: Date) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const diff = Math.round((day(n) - day(d)) / 86400000);
  if (diff <= 0) return "Today";
  if (diff === 1) return "Yesterday";
  return d.toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short" });
}

/** A snippet's first line: what its row shows instead of its made-up name. */
export function firstLine(it: Pick<HubItem, "preview" | "name">): string {
  const l = (it.preview || "").split("\n").find((x) => x.trim()) || "";
  return l.trim() || it.name;
}

export function HubPanel() {
  const open = useHubUi((s) => s.open);
  const guest = !!useConfig().data?.guest;
  // A #hub link (and the preview's `--view hub`) opens it.
  useEffect(() => {
    if (location.hash === "#hub") useHubUi.getState().setOpen(true);
  }, []);
  if (guest) return null;
  return (
    <>
      {open && <HubDrawer />}
      <HubNoticeBar />
    </>
  );
}

/** What Shared files says, at the top centre of the window. */
function HubNoticeBar() {
  const n = useHubUi((s) => s.notice);
  const drawer = useHubUi((s) => s.open);
  useEffect(() => {
    if (!n) return;
    const t = setTimeout(() => {
      if (useHubUi.getState().notice?.n === n.n) hush();
    }, n.ms);
    return () => clearTimeout(t);
  }, [n]);
  if (!n) return null;
  return (
    <div
      id="hub-notice"
      className={(n.error ? "is-error" : "") + (drawer ? " beside-drawer" : "")} role={n.error ? "alert" : "status"} aria-live={n.error ? "assertive" : "polite"}>
      <span className="hub-notice-text">{n.text}</span>
      {(n.actions || []).map((a) => (
        <button
          key={a.label}
          type="button"
          className="hub-notice-act"
          onClick={() => {
            hush();
            a.run();
          }}
        >
          {a.label}
        </button>
      ))}
      <button type="button" className="hub-notice-x" aria-label="Dismiss" onClick={hush}>
        ✕
      </button>
    </div>
  );
}

function HubDrawer() {
  const setOpen = useHubUi((s) => s.setOpen);
  const problems = useHubUi((s) => s.problems);
  const sending = useHubUi((s) => s.sending);
  const selected = useHubUi((s) => s.selected);
  const setSelected = useHubUi((s) => s.setSelected);
  const q = useHub(true);
  const list = q.data;
  const items = useMemo(() => list?.items || [], [list]);
  const ref = useRef<HTMLElement>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const findRef = useRef<HTMLInputElement>(null);
  const allBox = useRef<HTMLInputElement>(null);
  const anchor = useRef<string | null>(null);
  const [dragOver, setDragOver] = useState(false);
  const [busy, setBusy] = useState("");
  const [snippet, setSnippet] = useState<string | null>(null);
  const [secrets, setSecrets] = useState<File[]>([]);
  const [find, setFind] = useState("");
  const [filter, setFilter] = useState<HubFilter>("all");
  const [viewing, setViewing] = useState<HubItem | null>(null);
  const [clearing, setClearing] = useState(false);
  const [removing, setRemoving] = useState(false);
  const brokenErr =
    q.isError && (q.error as { body?: { code?: string } } | null)?.body?.code === "hub_broken" ? errMsg(q.error) : "";

  // The focused session, when Shared files can go there: the selection
  // bar's "Put in ‹it›'s prompt".
  const focused = useUi((s) => s.focused);
  const targets = hubTargets(useInstances().data as Instance[] | undefined);
  const here = focused && targets.some((t) => t.title === focused) ? focused : "";

  // Opening it always reads the list afresh (the ⋯ menu's copy may be old).
  useEffect(() => {
    void q.refetch();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

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

  // Paste anywhere while the list is open — unless the person is typing in
  // a box (the composer, a terminal, the snippet editor), which keeps it,
  // or the drawer is on its send step.
  useEffect(() => {
    const onPaste = (e: ClipboardEvent) => {
      const inside = !!ref.current?.contains(e.target as Node);
      if (
        editable(e.target) ||
        useHubUi.getState().sending ||
        (!inside && document.activeElement && document.activeElement !== document.body && !ref.current?.contains(document.activeElement))
      )
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
          .catch((err) => addedToast({ added: [], refused: [{ file: new File([], "the text"), error: errMsg(err), secret: false }] }))
          .finally(() => setBusy(""));
      }
    };
    document.addEventListener("paste", onPaste);
    return () => document.removeEventListener("paste", onPaste);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const hasFiles = (e: DragEvent) => Array.from(e.dataTransfer.types || []).includes("Files");
  const shown = useMemo(() => filterItems(items, filter, find), [items, filter, find]);
  const shownIds = useMemo(() => shown.map((i) => i.id), [shown]);
  const days = useMemo(() => {
    const out: { label: string; items: HubItem[] }[] = [];
    for (const it of shown) {
      const label = dayLabel(it.added);
      const last = out[out.length - 1];
      if (last && last.label === label) last.items.push(it);
      else out.push({ label, items: [it] });
    }
    return out;
  }, [shown]);
  const unpinned = items.filter((i) => !i.pinned).length;

  // The selection, as items still in the list (one removed elsewhere drops out).
  const selItems = useMemo(() => items.filter((i) => selected.includes(i.id)), [items, selected]);
  const hiddenSel = selItems.filter((i) => !shownIds.includes(i.id)).length;
  const shownSel = selItems.length - hiddenSel;
  const allOn = shown.length > 0 && shownSel === shown.length;
  useEffect(() => {
    if (allBox.current) allBox.current.indeterminate = shownSel > 0 && !allOn;
  }, [shownSel, allOn]);
  useEffect(() => {
    if (selItems.length !== selected.length) setSelected(selItems.map((i) => i.id));
    if (!selItems.length) setRemoving(false);
  }, [selItems, selected, setSelected]);
  const tick = (id: string, on: boolean, shift: boolean) => {
    setSelected(rangeSelect(useHubUi.getState().selected, shownIds, anchor.current, id, on, shift));
    anchor.current = id;
  };
  const selectAll = (on: boolean) => {
    const cur = useHubUi.getState().selected;
    setSelected(on ? [...new Set([...cur, ...shownIds])] : cur.filter((x) => !shownIds.includes(x)));
    anchor.current = null;
  };
  const clearSel = () => {
    setSelected([]);
    setRemoving(false);
    anchor.current = null;
  };

  const onKeyDown = (e: KeyboardEvent) => {
    if (e.defaultPrevented) return;
    if (e.key === "Escape") {
      e.preventDefault();
      if (useHubUi.getState().selected.length) clearSel();
      else setOpen(false);
      return;
    }
    if (sending) return;
    // "/" or Ctrl/⌘+F: the find box.
    if ((e.key === "/" && !editable(e.target)) || (e.key === "f" && (e.ctrlKey || e.metaKey))) {
      e.preventDefault();
      findRef.current?.focus();
      return;
    }
    // Ctrl/⌘+A out of a text field: tick everything the filter shows.
    if (e.key === "a" && (e.ctrlKey || e.metaKey) && !editable(e.target) && shown.length) {
      e.preventDefault();
      selectAll(true);
    }
  };

  const clearUnpinned = async () => {
    try {
      const r = await api<{ removed: number; freed: number }>("/api/hub/clear", { method: "POST" });
      say("Removed " + r.removed + (r.removed === 1 ? " item" : " items") + " — " + humanSize(r.freed) + " freed");
      refreshHub();
    } catch (err) {
      say("Couldn't remove them: " + errMsg(err), { error: true });
    } finally {
      setClearing(false);
    }
  };

  const { toPin, toUnpin } = pinSplit(selItems);
  const n = selItems.length;

  return (
    <aside
      id="hub-panel"
      ref={ref}
      tabIndex={-1}
      role="complementary"
      aria-label="Shared files — files and text every session can use"
      className={(dragOver ? "hub-drop" : "") + (n ? " has-selection" : "") + (sending ? " is-sending" : "")}
      onKeyDown={onKeyDown}
      onDragOver={(e) => {
        if (!hasFiles(e) || sending) return;
        e.preventDefault();
        e.dataTransfer.dropEffect = "copy";
        setDragOver(true);
      }}
      onDragLeave={(e) => {
        if (!e.currentTarget.contains(e.relatedTarget as Node | null)) setDragOver(false);
      }}
      onDrop={(e) => {
        if (!hasFiles(e) || sending) return;
        e.preventDefault();
        e.stopPropagation();
        setDragOver(false);
        void add(Array.from(e.dataTransfer.files || []));
      }}
    >
      <header className="hub-head">
        <div className="hub-title">
          <h2>Shared files</h2>
          {!sending && (
            <p className="hub-sub">
              Files and text every session can use. Each item has a code like <code>file:k7m2</code> — put it in any prompt
              and the agent gets the file.
            </p>
          )}
        </div>
        <button type="button" className="hub-x" aria-label="Close Shared files" title="Close (Esc)" onClick={() => setOpen(false)}>
          ✕
        </button>
      </header>

      {sending ? (
        <HubSendStep
          key={sending.map((i) => i.id).join(",")}
          items={sending}
          onBack={() => useHubUi.getState().openSend(null)}
          onDone={() => {
            useHubUi.getState().openSend(null);
            clearSel();
          }}
        />
      ) : (
        <>
          <div className="hub-add">
            <button type="button" className="hub-btn primary" onClick={() => fileInput.current?.click()}>
              Add files…
            </button>
            <button type="button" className="hub-btn" onClick={() => setSnippet(snippet === null ? "" : null)} aria-expanded={snippet !== null}>
              New snippet
            </button>
            <span className="hub-add-hint">or drop here · paste with {PASTE_KEY}</span>
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

          {snippet !== null && <SnippetEditor value={snippet} onChange={setSnippet} onDone={() => setSnippet(null)} />}

          {busy && (
            <p className="hub-busy" role="status">
              {busy}
            </p>
          )}

          {problems.map((p) => (
            <div key={p.id} className="hub-problem" role="alert">
              <p>{p.text}</p>
              <div className="hub-confirm-actions">
                {p.raiseTo && (
                  <button
                    type="button"
                    className="hub-btn primary"
                    onClick={() => {
                      dropProblem(p.id);
                      void raiseItemLimit(p.raiseTo!);
                    }}
                  >
                    Raise the limit to {p.raiseTo} MB
                  </button>
                )}
                <button type="button" className="hub-btn" onClick={() => dropProblem(p.id)}>
                  Dismiss
                </button>
              </div>
            </div>
          ))}

          {secrets.map((f, i) => (
            <div key={f.name + i} className="hub-confirm" role="alertdialog" aria-label={"Add " + f.name + " anyway?"}>
              <p>
                <strong>{f.name} looks like a secret</strong> (an env file, key or credential). Every session can read Shared files.
              </p>
              <div className="hub-confirm-actions">
                <button
                  type="button"
                  className="hub-btn"
                  onClick={() => {
                    setSecrets((s) => s.filter((_, j) => j !== i));
                    void add([f], true);
                  }}
                >
                  Add it anyway
                </button>
                <button type="button" className="hub-btn primary" autoFocus onClick={() => setSecrets((s) => s.filter((_, j) => j !== i))}>
                  Don't add it
                </button>
              </div>
            </div>
          ))}

          {items.length > 0 && (
            <div className="hub-tools">
              <label className="hub-all" title="Select everything shown (Ctrl+A)">
                <input
                  ref={allBox}
                  type="checkbox"
                  className="hub-check"
                  checked={allOn}
                  disabled={!shown.length}
                  aria-label={allOn ? "Clear the selection of what is shown" : "Select all " + shown.length + " shown"}
                  onChange={(e) => selectAll(e.target.checked)}
                />
              </label>
              <div className="hub-chips" role="group" aria-label="Show">
                {FILTERS.map((f) => (
                  <button
                    key={f.key}
                    type="button"
                    className={"hub-chip" + (filter === f.key ? " on" : "")}
                    aria-pressed={filter === f.key}
                    onClick={() => setFilter(f.key)}
                  >
                    {f.label}
                  </button>
                ))}
              </div>
              <input
                ref={findRef}
                className="hub-filter"
                type="search"
                placeholder="Find…  /"
                aria-label="Find a shared file (/ or Ctrl+F)"
                value={find}
                onChange={(e) => setFind(e.target.value)}
              />
            </div>
          )}

          <div className="hub-list-wrap">
            {brokenErr ? (
              <div className="hub-problem" role="alert">
                <p>{brokenErr}</p>
                <div className="hub-confirm-actions">
                  <button type="button" className="hub-btn primary" onClick={() => void rebuildHub()}>
                    Rebuild the list
                  </button>
                </div>
              </div>
            ) : q.isLoading ? (
              <p className="hub-empty muted">Loading shared files…</p>
            ) : q.isError && !list ? (
              <p className="hub-empty" role="alert">
                Couldn't load shared files ({errMsg(q.error)}).{" "}
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
                  <strong>Nothing in Shared files yet.</strong>
                </p>
                <p>
                  Drop a file here, paste a screenshot ({PASTE_KEY}), or add a snippet. Each item gets a code like{" "}
                  <code>file:k7m2</code> — put it in any session's prompt, use <em>Send to session…</em>, or drag the item onto a
                  session.
                </p>
                <p className="muted">Agents can add files here too.</p>
              </div>
            ) : !shown.length ? (
              <p className="hub-empty muted">Nothing here matches{find ? ' "' + find + '"' : ""}.</p>
            ) : (
              days.map((d) => (
                <section key={d.label} className="hub-day" aria-label={d.label}>
                  <h3 className="hub-day-h">{d.label}</h3>
                  <ul className="hub-list">
                    {d.items.map((it) => (
                      <HubCard
                        key={it.id}
                        item={it}
                        onView={setViewing}
                        checked={selected.includes(it.id)}
                        onTick={(on, shift) => tick(it.id, on, shift)}
                        dragItems={() => dragSet(it, items, useHubUi.getState().selected)}
                      />
                    ))}
                  </ul>
                </section>
              ))
            )}
          </div>

          {n > 0 ? (
            <div className="hub-selbar" role="toolbar" aria-label={n + " selected"}>
              <div className="hub-selbar-top">
                <strong>{n} selected</strong>
                {hiddenSel > 0 && (
                  <span className="muted" title="Ticked under another filter or search — the actions include them">
                    {" "}
                    · {hiddenSel} not shown by the filter
                  </span>
                )}
                <button type="button" className="hub-link hub-selbar-clear" onClick={clearSel} title="Clear the selection (Esc)">
                  Clear
                </button>
              </div>
              {removing ? (
                <div className="hub-selbar-confirm" role="alertdialog" aria-label={"Remove " + n + (n === 1 ? " item" : " items") + "?"}>
                  <p>
                    Remove {n === 1 ? selItems[0].name : n + " items"}? {n === 1 ? "Its code stops" : "Their codes stop"} working in every
                    session (Undo right after).
                  </p>
                  <div className="hub-confirm-actions">
                    <button
                      type="button"
                      className="hub-btn danger"
                      onClick={() => {
                        const these = selItems;
                        clearSel();
                        void removeItems(these);
                      }}
                    >
                      Remove {n === 1 ? "it" : n}
                    </button>
                    <button type="button" className="hub-btn primary" autoFocus onClick={() => setRemoving(false)}>
                      Keep
                    </button>
                  </div>
                </div>
              ) : (
                <div className="hub-selbar-acts">
                  <button type="button" className="hub-btn primary" onClick={() => useHubUi.getState().openSend(selItems)}>
                    Send to session…
                  </button>
                  {here && (
                    <button
                      type="button"
                      className="hub-btn"
                      title={"Type " + (n === 1 ? "its code" : "their " + n + " codes") + " into " + displayName(here) + "'s prompt"}
                      onClick={() => {
                        const these = selItems;
                        void putInPrompt(here, these, "", { focus: true });
                      }}
                    >
                      Put in {displayName(here)}'s prompt
                    </button>
                  )}
                  <button type="button" className="hub-btn" onClick={() => void copyRefs(selItems)}>
                    {n === 1 ? "Copy code" : "Copy codes"}
                  </button>
                  {toPin > 0 && (
                    <button
                      type="button"
                      className="hub-btn"
                      title={toUnpin ? toUnpin + " already pinned — this pins the other " + toPin : "Keep them past the automatic clean-up"}
                      onClick={() => void pinItems(selItems.filter((i) => !i.pinned), true)}
                    >
                      {toUnpin ? "Pin " + toPin : "Pin"}
                    </button>
                  )}
                  {toUnpin > 0 && (
                    <button
                      type="button"
                      className="hub-btn"
                      title={toPin ? toPin + " not pinned — this unpins the " + toUnpin + " that are" : "Let them go with the automatic clean-up"}
                      onClick={() => void pinItems(selItems.filter((i) => i.pinned), false)}
                    >
                      {toPin ? "Unpin " + toUnpin : "Unpin"}
                    </button>
                  )}
                  <button type="button" className="hub-btn danger" onClick={() => setRemoving(true)}>
                    Remove…
                  </button>
                </div>
              )}
            </div>
          ) : (
            list && (
              <footer className="hub-foot">
                <span>{usageLine(list)}</span>
                <span className="muted" title={"Settings: hub.keep_days (" + list.keep_days + ")"}>
                  Deleted automatically {list.keep_days} days after last use — Pin to keep
                </span>
                {unpinned > 0 &&
                  (clearing ? (
                    <span className="hub-foot-confirm" role="alertdialog" aria-label="Remove every unpinned item?">
                      Remove {unpinned} unpinned?{" "}
                      <button type="button" className="hub-link" onClick={() => void clearUnpinned()}>
                        Remove
                      </button>{" "}
                      <button type="button" className="hub-link" autoFocus onClick={() => setClearing(false)}>
                        Keep
                      </button>
                    </span>
                  ) : (
                    <button type="button" className="hub-link" onClick={() => setClearing(true)}>
                      Remove unpinned items
                    </button>
                  ))}
              </footer>
            )
          )}
        </>
      )}
      {viewing && <PictureViewer item={viewing} onClose={() => setViewing(null)} />}
    </aside>
  );
}

/** A picture, large, in-page (images only, served the same safe way). */
function PictureViewer({ item, onClose }: { item: HubItem; onClose(): void }) {
  return (
    <Modal onClose={onClose} className="hub-viewer" role="dialog" aria-modal="true" aria-label={item.name} portal>
      <figure className="hub-viewer-card">
        <img src={rawUrl(item)} alt={item.name} />
        <figcaption>
          <span>{item.name}</span>
          <button type="button" className="hub-btn" autoFocus onClick={onClose}>
            Close
          </button>
        </figcaption>
      </figure>
    </Modal>
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
      addedToast({ added: [], refused: [{ file: new File([], "the snippet"), error: errMsg(err), secret: false }] });
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
        <button type="button" className="hub-btn primary" disabled={!props.value.trim() || saving} onClick={() => void save()} title="Add it to Shared files (Ctrl+Enter)">
          Add snippet
        </button>
      </div>
    </div>
  );
}

function KindIcon({ item, onView }: { item: HubItem; onView?(it: HubItem): void }) {
  if (item.kind === "image") {
    return (
      <button type="button" className="hub-thumb-btn" aria-label={"View " + item.name + " larger"} title="View larger" onClick={() => onView?.(item)}>
        <img className="hub-thumb" src={rawUrl(item)} alt="" loading="lazy" draggable={false} />
      </button>
    );
  }
  const ext = (item.name.match(/\.([A-Za-z0-9]{1,5})$/) || [])[1] || (item.kind === "text" ? "txt" : "");
  return (
    <span className={"hub-icon" + (item.kind === "text" ? " is-text" : "")} aria-hidden="true">
      <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinejoin="round">
        <path d="M6 2.8h8l4.5 4.5v13.9H6z" />
        <path d="M14 2.8v4.5h4.5" />
        {item.kind === "text" && <path d="M8.6 12h7M8.6 15h7M8.6 18h4.5" />}
      </svg>
      {ext && <span className="hub-ext">{ext.slice(0, 4)}</span>}
    </span>
  );
}

/** A drag of several items shows one small label, not the first card. */
function dragLabel(dt: DataTransfer, n: number): void {
  if (n < 2 || typeof document === "undefined" || !dt.setDragImage) return;
  const el = document.createElement("div");
  el.className = "hub-drag-label";
  el.textContent = n + " shared files";
  document.body.appendChild(el);
  dt.setDragImage(el, 12, 12);
  setTimeout(() => el.remove(), 0);
}

function HubCard(props: {
  item: HubItem;
  onView(it: HubItem): void;
  checked: boolean;
  onTick(on: boolean, shift: boolean): void;
  /** What a drag of this card carries: the selection when it is ticked. */
  dragItems(): HubItem[];
}) {
  const { item, onView, checked } = props;
  const openSend = useHubUi((s) => s.openSend);
  const [menu, setMenu] = useState(false);
  const [editing, setEditing] = useState<"" | "name" | "note">("");
  const [val, setVal] = useState("");
  const textual = item.kind === "text" || item.textual;
  // Text items are one compact line until opened.
  const [openText, setOpenText] = useState(false);
  const compact = textual && !openText;
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
    setOpenText(true);
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
  const meta = [kindLabel(item), humanSize(item.size), whoLabel(item), relTime(item.added)].join(" · ");
  // A snippet's name is made from its first words: its row shows the text.
  const title = item.kind === "text" ? firstLine(item) : item.name;

  return (
    <li
      className={"hub-card" + (item.pinned ? " is-pinned" : "") + (compact ? " is-compact" : "") + (checked ? " is-selected" : "")}
      draggable={!editing}
      onDragStart={(e) => {
        const these = props.dragItems();
        startDrag(e.dataTransfer, these);
        dragLabel(e.dataTransfer, these.length);
      }}
    >
      <div className="hub-card-top">
        <input
          type="checkbox"
          className="hub-check"
          checked={checked}
          aria-label={"Select " + item.name}
          title="Select (Shift+click: a range)"
          onClick={(e: ReactMouseEvent<HTMLInputElement>) => props.onTick(e.currentTarget.checked, e.shiftKey)}
          onChange={() => {
            /* onClick carries Shift */
          }}
        />
        <span
          className="hub-grip"
          aria-hidden="true"
          title={
            checked
              ? "Drag onto a session, its message box or its terminal to use every selected item"
              : "Drag onto a session, its message box or its terminal to use " + item.ref
          }
        >
          ⠿
        </span>
        <KindIcon item={item} onView={onView} />
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
          ) : textual ? (
            <button
              type="button"
              className="hub-name hub-name-btn"
              aria-expanded={openText}
              title={openText ? "Show less" : "Show the text"}
              onClick={() => setOpenText((v) => !v)}
            >
              {title}
            </button>
          ) : (
            <span className="hub-name" title={item.name}>
              {title}
            </span>
          )}
          <span className="hub-meta">{meta}</span>
          <div className="hub-row2">
            <button
              type="button"
              className="hub-ref"
              title="Copy the code — paste it in any prompt or terminal"
              aria-label={"Copy code " + item.ref}
              onClick={() => void copyRef(item)}
            >
              <span className="hub-ref-code">{item.ref}</span>
              <span className="hub-ref-copy" aria-hidden="true">
                Copy code
              </span>
            </button>
            <div className="hub-actions">
              <button
                type="button"
                className={"hub-pin-btn" + (item.pinned ? " on" : "")}
                aria-pressed={item.pinned}
                aria-label={item.pinned ? "Unpin " + item.name : "Pin " + item.name + " — keep it"}
                title={expiryLabel(item)}
                onClick={() => void updateItem(item, { pinned: !item.pinned })}
              >
                <svg viewBox="0 0 24 24" width="14" height="14" fill={item.pinned ? "currentColor" : "none"} stroke="currentColor" strokeWidth="1.8" strokeLinejoin="round" aria-hidden="true">
                  <path d="M9 3h6l-1 6 4 4H6l4-4z" />
                  <path d="M12 13v8" />
                </svg>
              </button>
              <button
                type="button"
                className="hub-btn"
                aria-label={"Send " + item.name + " to a session…"}
                onClick={() => openSend(item)}
                title="Point a session's agent at it, or save a copy in its project folder"
              >
                {compact ? "Send…" : "Send to session…"}
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
      {textual && openText && item.preview && <pre className="hub-preview">{item.preview}</pre>}
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
        item.note && !compact && <p className="hub-note">{item.note}</p>
      )}
    </li>
  );
}
