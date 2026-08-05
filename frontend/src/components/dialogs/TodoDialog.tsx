/** Todo dialog (port of the todo functions, section 18): the assistant's
 * todos.json — drag-orderable, backed by REST, polled while open so edits
 * the assistant makes show up live. */

import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../../api/client";
import { useUi } from "../../state/store";

interface Todo {
  id: string;
  text: string;
  done: boolean;
}

const rid = () => Math.random().toString(36).slice(2, 10);

export function TodoDialog() {
  const open = useUi((s) => s.openDialog === "todo");
  const closeDialog = useUi((s) => s.closeDialog);
  const [todos, setTodos] = useState<Todo[]>([]);
  const [state, setState] = useState("");
  const [draft, setDraft] = useState("");
  const dragId = useRef<string | null>(null);
  const [dragOver, setDragOver] = useState<string | null>(null);
  const dragging = useRef(false);
  const addRef = useRef<HTMLInputElement | null>(null);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editDraft, setEditDraft] = useState("");
  const editCancelled = useRef(false);
  // Mirrored in a ref so `load` can stay identity-stable: if it changed on every
  // edit, the polling effect below would re-run and re-focus the Add box, which
  // blurs (and so commits) the inline edit the instant it opens.
  const editingRef = useRef<string | null>(null);
  const setEditing = useCallback((id: string | null) => {
    editingRef.current = id;
    setEditingId(id);
  }, []);

  const save = useCallback(async (next: Todo[]) => {
    setTodos(next);
    setState("saving…");
    try {
      const r = await api<{ todos?: Todo[] }>("/api/assistant/todos", {
        method: "PUT",
        json: { todos: next },
      });
      setTodos(r.todos || []);
      setState("saved");
    } catch {
      setState("save failed");
    }
  }, []);

  const load = useCallback(async () => {
    // Don't clobber a drag in flight, text being typed into the Add box, or an
    // inline edit in progress.
    if (dragging.current || document.activeElement === addRef.current || editingRef.current) return;
    try {
      const r = await api<{ todos?: Todo[] }>("/api/assistant/todos");
      setTodos(r.todos || []);
      setState((s) => (s === "load failed" ? "" : s));
    } catch {
      setState("load failed");
    }
  }, []);

  // Focus the Add box once per opening — never on an unrelated re-render, or it
  // would yank focus out of an inline edit.
  useEffect(() => {
    if (!open) return;
    const t = setTimeout(() => addRef.current?.focus(), 0);
    return () => clearTimeout(t);
  }, [open]);

  useEffect(() => {
    if (!open) return;
    load();
    const poll = setInterval(() => {
      if (!document.hidden) load();
    }, 4000);
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") closeDialog();
    };
    document.addEventListener("keydown", onKey);
    return () => {
      clearInterval(poll);
      document.removeEventListener("keydown", onKey);
    };
  }, [open, load, closeDialog]);

  if (!open) return null;

  const add = () => {
    const text = draft.trim();
    if (!text) return;
    setDraft("");
    save([...todos, { id: rid(), text, done: false }]);
  };

  const startEdit = (t: Todo) => {
    setEditing(t.id);
    setEditDraft(t.text);
    editCancelled.current = false;
  };

  const commitEdit = (id: string) => {
    if (editCancelled.current) return;
    setEditing(null);
    const text = editDraft.trim();
    const cur = todos.find((x) => x.id === id);
    if (!cur || !text || text === cur.text) return;
    save(todos.map((x) => (x.id === id ? { ...x, text } : x)));
  };

  const cancelEdit = () => {
    editCancelled.current = true;
    setEditing(null);
  };

  return (
    <div
      id="todo-dialog"
      className="modal"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) closeDialog();
      }}
    >
      <div id="todo-panel">
        <div className="ws-head">
          <h2>Todo</h2>
          <span id="todo-state" className="muted">{state}</span>
          <button type="button" id="todo-close" onClick={closeDialog}>
            Close
          </button>
        </div>
        <div className="todo-add">
          <input
            type="text"
            id="todo-add-input"
            ref={addRef}
            autoComplete="off"
            placeholder="Add a todo…"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") add();
            }}
          />
          <button type="button" id="todo-add-btn" onClick={add}>
            Add
          </button>
        </div>
        <ul id="todo-list" className="todo-list">
          {!todos.length ? (
            <li className="todo-empty">No todos yet. Add one, or ask the assistant.</li>
          ) : (
            todos.map((t) => (
              <li
                key={t.id}
                className={
                  "todo-row" + (t.done ? " done" : "") + (dragOver === t.id ? " drag-over" : "")
                }
                draggable={editingId !== t.id}
                onDragStart={(e) => {
                  dragId.current = t.id;
                  dragging.current = true;
                  e.dataTransfer.effectAllowed = "move";
                }}
                onDragEnd={() => {
                  dragging.current = false;
                  setDragOver(null);
                }}
                onDragOver={(e) => {
                  e.preventDefault();
                  if (dragId.current && dragId.current !== t.id) setDragOver(t.id);
                }}
                onDragLeave={() => setDragOver((d) => (d === t.id ? null : d))}
                onDrop={(e) => {
                  e.preventDefault();
                  setDragOver(null);
                  const from = todos.findIndex((x) => x.id === dragId.current);
                  const to = todos.findIndex((x) => x.id === t.id);
                  dragId.current = null;
                  if (from < 0 || to < 0 || from === to) return;
                  const next = todos.slice();
                  const [moved] = next.splice(from, 1);
                  next.splice(to, 0, moved);
                  save(next);
                }}
              >
                <span className="drag-handle" title="Drag to reorder">⠿</span>
                <input
                  type="checkbox"
                  checked={t.done}
                  onChange={(e) =>
                    save(todos.map((x) => (x.id === t.id ? { ...x, done: e.target.checked } : x)))
                  }
                />
                {editingId === t.id ? (
                  <input
                    type="text"
                    className="todo-edit"
                    autoFocus
                    autoComplete="off"
                    value={editDraft}
                    onChange={(e) => setEditDraft(e.target.value)}
                    onBlur={() => commitEdit(t.id)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter") {
                        e.currentTarget.blur();
                      } else if (e.key === "Escape") {
                        e.stopPropagation();
                        cancelEdit();
                      }
                    }}
                  />
                ) : (
                  <span
                    className="todo-text"
                    title="Click to edit"
                    onClick={() => startEdit(t)}
                  >
                    {t.text}
                  </span>
                )}
                <button
                  className="todo-del"
                  title="Delete"
                  onClick={() => save(todos.filter((x) => x.id !== t.id))}
                >
                  ×
                </button>
              </li>
            ))
          )}
        </ul>
      </div>
    </div>
  );
}
