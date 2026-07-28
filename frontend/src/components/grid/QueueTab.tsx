/** Queue tab content (port of renderQueueInto/loadQueueInto, section 6):
 * the send/queue console — composer, auto-run flags, loop timer, usage-limit
 * hold status, and the reorderable item list with inline editing. */

import { useCallback, useEffect, useState } from "react";
import { instApi } from "../../api/client";
import { fmtDurationShort } from "../../lib/format";
import { toast } from "../../lib/toast";

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

  const reload = useCallback(async () => {
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
      toast("Sent to " + title);
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

  return (
    <div className="queue-console">
      <div className="queue-pop-head">
        <span className="queue-pop-title">{title}</span>
        <span className="queue-pop-sub muted">
          Send a prompt now, or queue prompts to auto-run when the agent goes idle — the run keeps
          going unattended and resumes when usage limits reset
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
          <div
            key={it.id}
            className={
              "queue-item" + (i === 0 ? " queue-next" : "") + (editing?.id === it.id ? " editing" : "")
            }
            data-item-id={it.id}
          >
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
                        toast("Sent to " + title);
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
                    className="qi-up"
                    title="Move up"
                    disabled={i === 0}
                    onClick={() => mutate("/queue/reorder", { json: { id: it.id, direction: "up" } })}
                  >
                    ↑
                  </button>
                  <button
                    className="qi-down"
                    title="Move down"
                    disabled={i === items.length - 1}
                    onClick={() => mutate("/queue/reorder", { json: { id: it.id, direction: "down" } })}
                  >
                    ↓
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
        ))}
        {items.length > 0 && (
          <button className="queue-clear" onClick={() => mutate("/queue", { method: "DELETE" })}>
            Clear all
          </button>
        )}
      </div>
    </div>
  );
}
