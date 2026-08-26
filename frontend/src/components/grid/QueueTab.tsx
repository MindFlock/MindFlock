/** Queue tab content (port of renderQueueInto/loadQueueInto, section 6):
 * the send/queue console — composer, auto-run flags, loop timer, usage-limit
 * hold status, and the reorderable item list with inline editing. */

import { Fragment, useCallback, useEffect, useRef, useState } from "react";
import { instApi } from "../../api/client";
import { fmtDurationShort } from "../../lib/format";
import { displayName } from "../../state/store";
import { toast } from "../../lib/toast";
import { dropIndex, hoverSlot, reorderItems } from "./queueDnd";
import { promptsFromFile } from "./queueImport";

interface QueueItem {
  id: string;
  text: string;
}

interface QueueStatePayload {
  items?: QueueItem[];
  enabled?: boolean;
  loop?: boolean;
  loop_interval?: number | string;
  last_sent?: number;
  wait_for_limit?: boolean;
  limited_until?: number;
}

const queueCache = new Map<string, QueueStatePayload>();

function qApi<T = QueueStatePayload>(title: string, path: string, opts?: RequestInit & { json?: unknown }) {
  return instApi<T>(title, path, opts);
}

export function QueueTab({ title, active }: { title: string; active: boolean }) {
  const [state, setState] = useState<QueueStatePayload | null>(queueCache.get(title) || null);
  const [draft, setDraft] = useState("");
  const [editing, setEditing] = useState<{ id: string; value: string } | null>(null);
  // Drag-and-drop reorder: the id in flight and the insertion slot the pointer
  // is over (0..items.length). A ref mirrors the drag so the poll can't yank
  // the list out from under the pointer mid-drag.
  const [dragId, setDragId] = useState<string | null>(null);
  const [dropSlot, setDropSlot] = useState<number | null>(null);
  const draggingRef = useRef(false);
  // Inline "add above/below" composer: the slot the new prompt goes into.
  const [inserting, setInserting] = useState<{ index: number; value: string } | null>(null);
  // A file (not a queue row) is being dragged over the tab.
  const [fileHover, setFileHover] = useState(false);

  const reload = useCallback(async () => {
    if (draggingRef.current) return; // don't reshuffle rows under a drag
    try {
      const st = await qApi(title, "/queue");
      queueCache.set(title, st);
      setState((prev) => (JSON.stringify(prev) === JSON.stringify(st) ? prev : st));
    } catch {
      /* keep the last render on error */
    }
  }, [title]);

  const mutate = useCallback(
    async (path: string, opts?: RequestInit & { json?: unknown }) => {
      try {
        const st = await qApi(title, path, opts);
        queueCache.set(title, st);
        setState(st);
      } catch (err) {
        toast("Queue update failed: " + ((err as Error).message || ""));
      }
    },
    [title]
  );

  const flags = (f: Record<string, unknown>) => mutate("/queue/flags", { json: f });

  // Revalidate on activation + refresh every 15s while active.
  useEffect(() => {
    if (!active) return;
    reload();
    const t = setInterval(() => {
      if (!document.hidden) reload();
    }, 15000);
    return () => clearInterval(t);
  }, [active, reload]);

  if (!state) return <div className="queue-loading muted">Loading…</div>;

  const items = state.items || [];
  const enabled = state.enabled !== false;
  const loop = !!state.loop;
  const loopInterval = Math.max(0, parseInt(String(state.loop_interval ?? 0), 10) || 0);
  const lastSent = state.last_sent ? state.last_sent * 1000 : 0;
  const waitLimit = state.wait_for_limit !== false;
  const limitedUntil = state.limited_until ? state.limited_until * 1000 : 0;
  const limitMs = limitedUntil - Date.now();

  let statusCls = "queue-status muted";
  let statusText: string;
  if (limitMs > 0 && waitLimit) {
    statusCls = "queue-status queue-limited";
    statusText = "⏳ Usage limit reached · auto-resumes in " + fmtDurationShort(limitMs);
  } else if (limitMs > 0) {
    statusCls = "queue-status queue-stopped";
    statusText =
      "⏸ Usage limit reached · queue stopped (turn on “Wait out usage limits” to auto-resume)";
  } else if (!items.length) {
    statusText = "Queue is empty — add prompts above and they auto-run when the agent is idle.";
  } else if (!enabled) {
    statusText = items.length + " queued · auto-run is OFF (turn it on above to send)";
  } else if (loop && loopInterval > 0) {
    const nextMs = lastSent ? lastSent + loopInterval * 60000 - Date.now() : 0;
    statusText =
      nextMs > 0
        ? `${items.length} queued · loops every ${loopInterval} min · next send in ${fmtDurationShort(nextMs)}`
        : `${items.length} queued · loops every ${loopInterval} min · sends when idle`;
  } else {
    statusText = loop
      ? items.length + " queued · next sends when idle, then re-queues (loop)"
      : items.length + " queued · next sends automatically when idle";
  }

  const send = async () => {
    const text = draft.trim();
    if (!text) return;
    try {
      await qApi(title, "/send", { json: { text } });
      setDraft("");
      toast("Sent to " + displayName(title));
      reload();
    } catch (err) {
      toast("Send failed: " + ((err as Error).message || ""));
    }
  };
  const add = async () => {
    const text = draft.trim();
    if (!text) return;
    try {
      const st = await qApi(title, "/queue", { json: { text } });
      queueCache.set(title, st);
      setDraft("");
      setState(st);
    } catch (err) {
      toast("Queue failed: " + ((err as Error).message || ""));
    }
  };

  const saveEdit = () => {
    if (!editing) return;
    const t = editing.value.trim();
    if (!t) return;
    setEditing(null);
    mutate("/queue/edit", { json: { id: editing.id, text: t } });
  };

  const saveInsert = () => {
    if (!inserting) return;
    const t = inserting.value.trim();
    if (!t) return;
    const at = inserting.index;
    setInserting(null);
    mutate("/queue", { json: { text: t, index: at } });
  };

  /** Dropped file(s) → one queued prompt per CSV row / text line, appended to
   * the end of the queue in one bulk call. */
  const importFiles = async (files: FileList) => {
    const texts: string[] = [];
    for (const f of Array.from(files)) {
      try {
        texts.push(...promptsFromFile(f.name, await f.text()));
      } catch {
        toast("Could not read " + f.name);
      }
    }
    if (!texts.length) {
      toast("No prompts found in the dropped file");
      return;
    }
    try {
      const st = await qApi<QueueStatePayload & { added?: number; skipped?: number }>(
        title,
        "/queue",
        { json: { texts } }
      );
      queueCache.set(title, st);
      setState(st);
      const added = st.added ?? texts.length;
      toast(
        "Queued " +
          added +
          (added === 1 ? " prompt" : " prompts") +
          (st.skipped ? " — " + st.skipped + " skipped (queue full)" : "")
      );
    } catch (err) {
      toast("Queue failed: " + ((err as Error).message || ""));
    }
  };

  const endDrag = () => {
    draggingRef.current = false;
    setDragId(null);
    setDropSlot(null);
  };

  const finishDrop = () => {
    const id = dragId;
    const slot = dropSlot;
    endDrag();
    if (!id || slot == null) return;
    const from = items.findIndex((x) => x.id === id);
    if (from < 0) return;
    const to = dropIndex(from, slot);
    if (to == null) return; // dropped back onto its own edges
    // Optimistic: settle the row where it was dropped, then let the server
    // response (via mutate) confirm the order.
    const optimistic = { ...state, items: reorderItems(items, from, to) };
    queueCache.set(title, optimistic);
    setState(optimistic);
    mutate("/queue/reorder", { json: { id, index: to } });
  };

  return (
    <div
      className={"queue-console" + (fileHover ? " file-hover" : "")}
      onDragOver={(e) => {
        // Files only — internal row drags carry dragId and are handled per row.
        if (dragId || !e.dataTransfer.types.includes("Files")) return;
        e.preventDefault();
        e.dataTransfer.dropEffect = "copy";
        setFileHover(true);
      }}
      onDragLeave={(e) => {
        if (!e.currentTarget.contains(e.relatedTarget as Node | null)) setFileHover(false);
      }}
      onDrop={(e) => {
        if (!fileHover) return;
        e.preventDefault();
        setFileHover(false);
        if (e.dataTransfer.files?.length) void importFiles(e.dataTransfer.files);
      }}
    >
      <div className="queue-pop-head">
        <span className="queue-pop-title">{title}</span>
        <span className="queue-pop-sub muted">
          Send a prompt now, or queue prompts to auto-run when the agent goes idle — the run keeps
          going unattended and resumes when usage limits reset. Drop a .csv or .txt file anywhere
          here to queue every row as its own prompt.
        </span>
      </div>
      <textarea
        className="queue-input"
        rows={3}
        placeholder="Type a prompt — Add to queue, or Send now"
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
      />
      <div className="queue-actions">
        <button className="queue-add primary" type="button" onClick={add}>
          Add to queue
        </button>
        <button className="queue-send" type="button" onClick={send}>
          Send now
        </button>
      </div>
      <div className="queue-flags">
        <label>
          <input
            type="checkbox"
            className="queue-enabled"
            checked={enabled}
            onChange={(e) => flags({ enabled: e.target.checked })}
          />{" "}
          Auto-run queue when idle
        </label>
        <label title="Re-queue each sent prompt so a single self-improving prompt cycles forever">
          <input
            type="checkbox"
            className="queue-loop"
            checked={loop}
            onChange={(e) => flags({ loop: e.target.checked })}
          />{" "}
          Loop (keep re-running)
        </label>
        <label
          className={"queue-loop-timer" + (loop ? "" : " disabled")}
          title="With Loop on, only re-send every N minutes (0 = re-send as soon as the agent is idle)"
        >
          ↳ every{" "}
          <input
            type="number"
            className="queue-loop-interval"
            min={0}
            max={1440}
            step={1}
            defaultValue={loopInterval}
            disabled={!loop}
            onChange={(e) => {
              let n = parseInt(e.target.value, 10);
              if (!(n >= 0)) n = 0;
              if (n > 1440) n = 1440;
              flags({ loop_interval: n });
            }}
          />{" "}
          min
        </label>
        <label title="When the agent hits its usage limit, wait out the window and auto-resume the moment it resets. Turn off to stop the queue when limited instead of waiting.">
          <input
            type="checkbox"
            className="queue-wait"
            checked={waitLimit}
            onChange={(e) => flags({ wait_for_limit: e.target.checked })}
          />{" "}
          Wait out usage limits (auto-resume on reset)
        </label>
      </div>
      <div className="queue-list">
        <div className={statusCls}>{statusText}</div>
        {items.map((it, i) => (
          <Fragment key={it.id}>
            {inserting?.index === i && (
              <InsertComposer
                value={inserting.value}
                onChange={(v) => setInserting({ index: i, value: v })}
                onSave={saveInsert}
                onCancel={() => setInserting(null)}
              />
            )}
          <div
            className={
              "queue-item" +
              (i === 0 ? " queue-next" : "") +
              (editing?.id === it.id ? " editing" : "") +
              (dragId === it.id ? " dragging" : "") +
              (dragId && dropSlot === i ? " drop-before" : "") +
              (dragId && i === items.length - 1 && dropSlot === items.length ? " drop-after" : "")
            }
            data-item-id={it.id}
            onDragOver={(e) => {
              if (!dragId) return;
              e.preventDefault();
              e.dataTransfer.dropEffect = "move";
              const rect = e.currentTarget.getBoundingClientRect();
              setDropSlot(hoverSlot(i, e.clientY - rect.top, rect.height));
            }}
            onDrop={(e) => {
              e.preventDefault();
              finishDrop();
            }}
          >
            <span
              className="qi-drag"
              title="Drag to reorder"
              draggable
              onDragStart={(e) => {
                const row = (e.currentTarget as HTMLElement).closest(".queue-item");
                if (row) e.dataTransfer.setDragImage(row, 16, 16);
                e.dataTransfer.effectAllowed = "move";
                e.dataTransfer.setData("text/plain", it.id);
                draggingRef.current = true;
                setDragId(it.id);
              }}
              onDragEnd={endDrag}
            >
              ⋮⋮
            </span>
            <span className="queue-item-pos" title={i === 0 ? "Sends next" : "Position " + (i + 1)}>
              {i === 0 ? "▶" : String(i + 1)}
            </span>
            {editing?.id === it.id ? (
              <div className="queue-item-edit">
                <textarea
                  autoFocus
                  rows={Math.min(10, Math.max(3, editing.value.split("\n").length + 1))}
                  value={editing.value}
                  onChange={(e) => setEditing({ id: it.id, value: e.target.value })}
                />
                <div className="queue-item-edit-btns">
                  <button type="button" className="qi-save" onClick={saveEdit}>
                    Save
                  </button>
                  <button type="button" className="qi-cancel" onClick={() => { setEditing(null); reload(); }}>
                    Cancel
                  </button>
                </div>
              </div>
            ) : (
              <>
                <span
                  className="queue-item-text"
                  title="Double-click to edit"
                  onDoubleClick={() => setEditing({ id: it.id, value: it.text })}
                >
                  {it.text}
                </span>
                <span className="queue-item-ctrls">
                  <button
                    className="qi-send"
                    title="Send this prompt now — skips the idle wait, cooldowns and any usage-limit hold"
                    onClick={async () => {
                      try {
                        const st = await qApi(title, "/queue/send_now", { json: { id: it.id } });
                        queueCache.set(title, st);
                        setState(st);
                        toast("Sent to " + displayName(title));
                      } catch (err) {
                        toast("Send failed: " + ((err as Error).message || ""));
                      }
                    }}
                  >
                    ▶
                  </button>
                  <button className="qi-edit" title="Edit" onClick={() => setEditing({ id: it.id, value: it.text })}>
                    ✎
                  </button>
                  <button
                    className="qi-add-above"
                    title="Add a prompt above this one"
                    onClick={() => setInserting({ index: i, value: "" })}
                  >
                    +↑
                  </button>
                  <button
                    className="qi-add-below"
                    title="Add a prompt below this one"
                    onClick={() => setInserting({ index: i + 1, value: "" })}
                  >
                    +↓
                  </button>
                  <button
                    className="qi-del"
                    title="Remove"
                    onClick={() => mutate("/queue?item=" + encodeURIComponent(it.id), { method: "DELETE" })}
                  >
                    ✕
                  </button>
                </span>
              </>
            )}
          </div>
          </Fragment>
        ))}
        {inserting && inserting.index >= items.length && (
          <InsertComposer
            value={inserting.value}
            onChange={(v) => setInserting({ index: inserting.index, value: v })}
            onSave={saveInsert}
            onCancel={() => setInserting(null)}
          />
        )}
        {items.length > 0 && (
          <button className="queue-clear" onClick={() => mutate("/queue", { method: "DELETE" })}>
            Clear all
          </button>
        )}
      </div>
    </div>
  );
}

/** Inline composer a +↑/+↓ button opens in place: a new prompt written at the
 * exact slot it will occupy, styled like the row editor so the list reads as
 * one column. */
function InsertComposer({
  value,
  onChange,
  onSave,
  onCancel,
}: {
  value: string;
  onChange(v: string): void;
  onSave(): void;
  onCancel(): void;
}) {
  return (
    <div className="queue-item queue-item-insert">
      <span className="queue-item-pos" title="New prompt goes here">
        +
      </span>
      <div className="queue-item-edit">
        <textarea
          autoFocus
          rows={Math.min(10, Math.max(3, value.split("\n").length + 1))}
          placeholder="New prompt for this spot…"
          value={value}
          onChange={(e) => onChange(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Escape") onCancel();
          }}
        />
        <div className="queue-item-edit-btns">
          <button type="button" className="qi-save" onClick={onSave}>
            Add here
          </button>
          <button type="button" className="qi-cancel" onClick={onCancel}>
            Cancel
          </button>
        </div>
      </div>
    </div>
  );
}
