/** Commit dialog — ports the commit-dialog part of 260-commit-overlays.js and
 * commitSession/submitCommit from 100-session-actions.js (markup from
 * partials/230-dialog-commit.html). Multiline message; Ctrl/Cmd+Enter commits;
 * remembers the last message per session title in-memory; switches the user to
 * watching the terminal before submitting. */

import { useEffect, useRef, useState } from "react";
import { instApi } from "../../api/client";
import { freshStage } from "../../lib/stageWatch";
import { useUi } from "../../state/store";
import { errMsg } from "../../lib/format";
import { selectSession, startFastTrack } from "../../lib/sessionActions";
import { DEPTH_STEP_LABELS, SESSION_DEPTHS, normalizeDepth } from "../../lib/autopilot";

/** Last commit message per session (pre-fills the prompt on retry).
 * In-memory only, like the vanilla Map. */
const lastCommitMsg = new Map<string, string>();

export function CommitDialog() {
  const open = useUi((s) => s.openDialog === "commit");
  const target = useUi((s) => s.dialogTarget);
  const closeDialog = useUi((s) => s.closeDialog);
  const [msg, setMsg] = useState("");
  const [error, setError] = useState("");
  // The ✨ button's in-flight latch. A generation is one CLI turn (~10s), long
  // enough that without a visible busy state the button reads as broken.
  const [writing, setWriting] = useState(false);
  // How far this commit should carry the session. "commit" keeps the historical
  // behaviour (commit and stop), and is what the plain path still does.
  const [depth, setDepth] = useState("commit");
  const msgRef = useRef<HTMLTextAreaElement | null>(null);

  // On open: clear the error, pre-fill from the per-title memory, focus.
  useEffect(() => {
    if (!open) return;
    setError("");
    setMsg((target && lastCommitMsg.get(target)) || "");
    setDepth("commit");
    setWriting(false);
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

  // The button says what the press will actually do, so the chosen rung is never
  // a hidden setting.
  const submitLabel =
    {
      commit: "Commit",
      push: "Commit & push",
      pr: "Commit & open PR",
      merge: "Commit & merge",
    }[depth] || "Commit";

  /** ✨ — have the session's coding CLI read the diff and write the message.
   *
   * Whatever is in the box travels along as a hint and is then replaced by the
   * answer: the press means "write it", and a suggestion that appended itself to
   * a half-finished sentence would be worse than either alone. The old text is
   * still one Ctrl+Z away in the textarea's own undo stack.
   *
   * A failure leaves the box untouched and says why. The reason matters — "aider
   * has no headless mode", "claude is not installed", "nothing to describe" all
   * need different things from the user, and a bare "failed" would send them
   * looking in the wrong place.
   */
  async function writeMessage() {
    const title = target;
    if (!title || writing) return;
    setWriting(true);
    setError("");
    try {
      const r = await instApi<{ message?: string }>(title, "/commit-message/suggest", {
        json: { hint: msg.trim().slice(0, 500) },
      });
      const written = (r?.message || "").trim();
      if (!written) throw new Error("no message came back");
      setMsg(written);
      lastCommitMsg.set(title, written);
      const el = msgRef.current;
      if (el) {
        el.focus();
        el.setSelectionRange(written.length, written.length);
      }
    } catch (err) {
      setError(errMsg(err));
    } finally {
      setWriting(false);
    }
  }

  async function submitCommit() {
    const title = target;
    const m = msg.trim();
    if (!m) {
      setError("Commit message required");
      return;
    }
    if (!title) return;
    lastCommitMsg.set(title, m);
    // The merge rung's confirm has to happen while the dialog is still up —
    // closeDialog() below is what makes "abort the whole submit" meaningful.
    const chosen = normalizeDepth(depth) || "commit";
    closeDialog();
    // Watch the pre-commit hooks run (vanilla switchToTerminal: focus the
    // pane and show its shell tab).
    useUi.getState().setLastTab(title, "shell");
    selectSession(title);
    // COMMIT NOW, then arm the rest. The earlier version only armed for a deeper
    // rung and never POSTed /commit — but arming is arm-and-wait, so pressing
    // "Commit & push" did nothing for 30s and then committed whatever the agent
    // had written in the meantime, under the message typed before all of it. You
    // pressed Commit; the commit has to happen.
    try {
      await instApi(title, "/commit", { json: { message: m } });
      if (chosen !== "commit") await startFastTrack(title, chosen, m);
    } catch (err) {
      alert("Commit failed: " + errMsg(err));
    }
    // One fresh read so the pill flips to "pre-commit" the moment the hooks start
    // (the old 1s invalidate provably observed nothing — GET /api/instances serves
    // the tick snapshot for up to 10s — and it reset the 4s poll timer on the way
    // out, pushing the first useful poll ~1s later). The server's commit watcher
    // republishes again when the hooks finish, so "Push" needs no further poll.
    void freshStage(title);
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
        {/* The label is deliberately NOT wrapping the textarea any more: the ✨
            button belongs on the field's own header row, and a <button> inside a
            <label> is invalid HTML (interactive content) — so the two are
            associated with htmlFor instead. */}
        <div id="commit-msg-field">
          <div className="cm-head">
            <label htmlFor="commit-msg">
              Message{" "}
              <span className="muted">
                (multiline — first line is the summary; Ctrl+Enter to commit)
              </span>
            </label>
            <button
              type="button"
              id="commit-write"
              onClick={() => void writeMessage()}
              disabled={writing}
              aria-busy={writing || undefined}
              title={
                "Read the diff and write the commit message, using this session's" +
                " coding CLI. Replaces what's in the box."
              }
            >
              {writing ? "Writing…" : "✨ Write it"}
            </button>
          </div>
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
        </div>
        <label id="commit-depth-row">
          Then keep going{" "}
          <span className="muted">(fast-track — stops at the rung you pick)</span>
          <select
            id="commit-depth"
            value={depth}
            // Deliberately NOT persisted: this pick applies to THIS commit. The
            // previous version wrote a shared localStorage key that outranked
            // Settings, so touching this dropdown once silently re-targeted every
            // ⏩ button on the machine with no way to see or undo it.
            onChange={(e) => setDepth(e.target.value)}
          >
            {SESSION_DEPTHS.map((d) => (
              <option key={d} value={d}>
                {DEPTH_STEP_LABELS[d]}
              </option>
            ))}
          </select>
        </label>
        <div className="modal-actions">
          <button type="button" id="commit-cancel" onClick={closeDialog}>
            Cancel
          </button>
          <button type="submit">{submitLabel}</button>
        </div>
        <p id="commit-error" className="error">
          {error}
        </p>
      </form>
    </div>
  );
}
