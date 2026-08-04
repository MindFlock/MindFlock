/** Commit dialog — ports the commit-dialog part of 260-commit-overlays.js and
 * commitSession/submitCommit from 100-session-actions.js (markup from
 * partials/230-dialog-commit.html). Multiline message; Ctrl/Cmd+Enter commits;
 * remembers the last message per session title in-memory; switches the user to
 * watching the terminal before submitting. */

import { useEffect, useRef, useState } from "react";
import { instApi } from "../../api/client";
import { refreshInstances } from "../../state/queries";
import { useUi } from "../../state/store";
import { errMsg } from "../../lib/format";
import { selectSession } from "../../lib/sessionActions";

/** Last commit message per session (pre-fills the prompt on retry).
 * In-memory only, like the vanilla Map. */
const lastCommitMsg = new Map<string, string>();

export function CommitDialog() {
  const open = useUi((s) => s.openDialog === "commit");
  const target = useUi((s) => s.dialogTarget);
  const closeDialog = useUi((s) => s.closeDialog);
  const [msg, setMsg] = useState("");
  const [error, setError] = useState("");
  const msgRef = useRef<HTMLTextAreaElement | null>(null);

  // On open: clear the error, pre-fill from the per-title memory, focus.
  useEffect(() => {
    if (!open) return;
    setError("");
    setMsg((target && lastCommitMsg.get(target)) || "");
    msgRef.current?.focus();
  }, [open, target]);

  // The Map above is per page load, so a reload — or the server restart that
  // prompts one — loses a message the hooks rejected. The worktree still holds
  // it (it is the file `git commit -F` read), so ask for it and adopt it when we
  // have nothing better. Late and unconditional-on-empty rather than awaited
  // before the first paint: the textarea stays focused and typeable throughout,
  // and anything already typed wins.
  useEffect(() => {
    if (!open || !target) return;
    if (lastCommitMsg.get(target)) return;
    let cancelled = false;
    void (async () => {
      try {
        const r = await instApi<{ message?: string }>(target, "/commit-message");
        const saved = (r?.message || "").trim();
        if (cancelled || !saved) return;
        // Only fill a still-empty box: the fetch may land after typing began.
        setMsg((cur) => (cur.trim() ? cur : saved));
      } catch {
        // Nothing pending, or the session has no worktree — an empty box is the
        // right answer either way, and this must never block committing.
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [open, target]);

  // Escape closes (vanilla: global Escape chain in 170-new-session.js).
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        closeDialog();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open, closeDialog]);

  if (!open) return null;

  async function submitCommit() {
    const title = target;
    const m = msg.trim();
    if (!m) {
      setError("Commit message required");
      return;
    }
    if (!title) return;
    lastCommitMsg.set(title, m);
    closeDialog();
    // Watch the pre-commit hooks run (vanilla switchToTerminal: focus the
    // pane and show its shell tab).
    useUi.getState().setLastTab(title, "shell");
    selectSession(title);
    try {
      await instApi(title, "/commit", { json: { message: m } });
    } catch (err) {
      alert("Commit failed: " + errMsg(err));
    }
    setTimeout(refreshInstances, 1000);
  }

  return (
    <div
      id="commit-dialog"
      className="modal"
      // Clicking the backdrop dismisses, like Escape — reaching for Cancel to
      // put away a dialog you're already looking away from is a wasted trip.
      // mousedown, not click: a click whose press began inside the form and
      // whose release landed out here is a text selection that overshot, and
      // closing on it would throw away the message being selected. Only the
      // backdrop itself counts, never a bubbled event from the form.
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) closeDialog();
      }}
    >
      <form
        id="commit-form"
        onSubmit={(e) => {
          e.preventDefault();
          void submitCommit();
        }}
      >
        <h2>Commit</h2>
        <label>
          Message{" "}
          <span className="muted">
            (multiline — first line is the summary; Ctrl+Enter to commit)
          </span>
          <textarea
            id="commit-msg"
            ref={msgRef}
            rows={8}
            autoComplete="off"
            spellCheck={false}
            placeholder={"Short summary line\n\nOptional longer description…"}
            value={msg}
            onChange={(e) => setMsg(e.target.value)}
            // Ctrl/Cmd+Enter commits without reaching for the mouse (Enter
            // alone inserts a newline, since the message is multiline).
            onKeyDown={(e) => {
              if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
                e.preventDefault();
                void submitCommit();
              }
            }}
          />
        </label>
        <div className="modal-actions">
          <button type="button" id="commit-cancel" onClick={closeDialog}>
            Cancel
          </button>
          <button type="submit">Commit</button>
        </div>
        <p id="commit-error" className="error">
          {error}
        </p>
      </form>
    </div>
  );
}
