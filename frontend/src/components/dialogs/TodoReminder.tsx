/** The reminder dialog for timed todos (the Assistant's todo list, `due`):
 * pops up by itself when a todo's time has come — at it or any time after —
 * over whatever is open, until you check it off, snooze it or dismiss it.
 *
 * The server owns the clock (backend/web/addons/assistant.py): `GET
 * /api/assistant/todos/due` lists what is due now, and its reminder loop
 * emits `todo.due` the moment one comes due (the same event pushes it to the
 * phone through the notify addon's "A todo comes due" rule). This polls every
 * 30 s and refetches on that event, so it shows within seconds while the app
 * is open, and at once when the app comes back after the time passed.
 *
 * Snooze and dismiss are saved on the todo, so every window (and the other
 * server sharing the file) agrees. Closing the dialog without choosing hides
 * it here for 10 minutes. */

import { useCallback, useEffect, useState } from "react";
import { api } from "../../api/client";
import { useUi } from "../../state/store";
import { Modal } from "../Modal";
import { formatDue, snoozeChoices } from "../../lib/todoTime";
import "./todoReminder.css";

interface DueTodo {
  id: string;
  text: string;
  done: boolean;
  due?: string;
}

const POLL_MS = 30_000;
/** Closing the dialog without a choice: hidden here this long. */
const CLOSE_HIDE_MS = 10 * 60_000;

/** Whether a desktop notification may be shown (Electron grants it). */
function canNotify(): boolean {
  return typeof Notification !== "undefined" && Notification.permission === "granted";
}

export function TodoReminder() {
  const [due, setDue] = useState<DueTodo[]>([]);
  const [hiddenUntil, setHiddenUntil] = useState(0);
  const [seen, setSeen] = useState<Set<string>>(() => new Set());
  const [picking, setPicking] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const r = await api<{ due?: DueTodo[] }>("/api/assistant/todos/due");
      setDue(Array.isArray(r.due) ? r.due : []);
    } catch {
      /* no assistant addon on this server, or offline: nothing to remind */
    }
  }, []);

  useEffect(() => {
    void load();
    const t = setInterval(() => void load(), POLL_MS);
    const ev = window.mindflock?.events;
    const off = ev?.subscribe?.("todo.due", () => void load());
    const onFocus = () => void load();
    window.addEventListener("focus", onFocus);
    return () => {
      clearInterval(t);
      window.removeEventListener("focus", onFocus);
      if (typeof off === "function") off();
    };
  }, [load]);

  // A todo that newly came due while you are elsewhere: a desktop
  // notification too (when the OS lets this app post them).
  useEffect(() => {
    const fresh = due.filter((t) => !seen.has(t.id + "@" + (t.due || "")));
    if (!fresh.length) return;
    setSeen((s) => {
      const n = new Set(s);
      for (const t of fresh) n.add(t.id + "@" + (t.due || ""));
      return n;
    });
    setHiddenUntil(0); // something new is due: show it even if closed earlier
    if ((document.hidden || !document.hasFocus()) && canNotify()) {
      for (const t of fresh.slice(0, 3)) {
        try {
          new Notification("Reminder", { body: t.text, tag: "todo-" + t.id });
        } catch {
          /* not allowed here */
        }
      }
    }
  }, [due, seen]);

  const act = async (id: string, path: string, json: Record<string, unknown> = {}) => {
    setBusy(true);
    try {
      await api("/api/assistant/todos/" + encodeURIComponent(id) + "/" + path, { method: "POST", json });
    } catch {
      /* the next poll shows the truth */
    } finally {
      setBusy(false);
      setPicking(null);
      void load();
    }
  };

  if (!due.length || Date.now() < hiddenUntil) return null;
  const choices = snoozeChoices();

  return (
    <Modal id="todo-reminder" role="dialog" aria-label="Reminder" onClose={() => setHiddenUntil(Date.now() + CLOSE_HIDE_MS)}>
      <div className="todo-reminder">
        <div className="tr-head">
          <h2>⏰ {due.length === 1 ? "Reminder" : due.length + " reminders"}</h2>
        </div>
        <ul className="tr-list">
          {due.map((t) => (
            <li key={t.id} className="tr-item">
              <div className="tr-text">{t.text}</div>
              <div className="tr-when muted">Due {formatDue(t.due)}</div>
              <div className="tr-acts">
                <button type="button" className="btn-primary" disabled={busy} onClick={() => void act(t.id, "done")}>
                  Done
                </button>
                {choices.map((c) => (
                  <button key={c.label} type="button" disabled={busy} title={"Remind me again " + (c.label === "Tomorrow 9 AM" ? c.label.toLowerCase() : "in " + c.label)} onClick={() => void act(t.id, "snooze", { until: c.until })}>
                    {c.label}
                  </button>
                ))}
                {picking === t.id ? (
                  <input
                    type="datetime-local"
                    autoFocus
                    className="tr-pick"
                    onKeyDown={(e) => {
                      if (e.key === "Enter" && e.currentTarget.value) void act(t.id, "snooze", { until: e.currentTarget.value });
                      else if (e.key === "Escape") {
                        e.stopPropagation();
                        setPicking(null);
                      }
                    }}
                    onBlur={(e) => {
                      if (e.currentTarget.value) void act(t.id, "snooze", { until: e.currentTarget.value });
                      else setPicking(null);
                    }}
                  />
                ) : (
                  <button type="button" disabled={busy} onClick={() => setPicking(t.id)}>
                    Pick a time…
                  </button>
                )}
                <button type="button" className="tr-dismiss" disabled={busy} title="Stop reminding me (the todo stays on the list)" onClick={() => void act(t.id, "dismiss")}>
                  Dismiss
                </button>
              </div>
            </li>
          ))}
        </ul>
        <div className="tr-foot">
          <button
            type="button"
            className="tr-open"
            onClick={() => {
              setHiddenUntil(Date.now() + CLOSE_HIDE_MS);
              useUi.getState().openDialogFor("todo", "");
            }}
          >
            Open the todo list
          </button>
          <button type="button" onClick={() => setHiddenUntil(Date.now() + CLOSE_HIDE_MS)}>
            Later
          </button>
        </div>
      </div>
    </Modal>
  );
}
