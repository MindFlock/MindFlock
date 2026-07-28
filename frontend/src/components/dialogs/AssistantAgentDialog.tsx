/** Assistant "agent file" editor (port of initAssistantAgentFile, section
 * 18): the personal assistant's standing instructions (its CLAUDE.md). */

import { useEffect, useRef, useState } from "react";
import { api } from "../../api/client";
import { useUi } from "../../state/store";
import { toast } from "../../lib/toast";

export function AssistantAgentDialog() {
  const open = useUi((s) => s.openDialog === "assistant-agent");
  const closeDialog = useUi((s) => s.closeDialog);
  const [text, setText] = useState("");
  const [status, setStatus] = useState("");
  const taRef = useRef<HTMLTextAreaElement | null>(null);

  useEffect(() => {
    if (!open) return;
    setStatus("");
    (async () => {
      try {
        const r = await api<{ text?: string }>("/api/assistant/instructions");
        setText(r?.text || "");
      } catch {
        setText("");
      }
      setTimeout(() => taRef.current?.focus(), 0);
    })();
  }, [open]);

  if (!open) return null;

  const save = async (restart: boolean) => {
    setStatus("Saving…");
    try {
      await api("/api/assistant/instructions", { method: "PUT", json: { text } });
      if (restart) {
        try {
          await api("/api/assistant/restart", { method: "POST" });
        } catch {
          /* restart is best-effort */
        }
      }
      setStatus(
        restart
          ? "Saved — assistant restarted; reopen Chat to talk to it."
          : "Saved — applies the next time the assistant starts."
      );
      toast(restart ? "Assistant instructions saved & restarted" : "Assistant instructions saved");
    } catch (err) {
      setStatus("Save failed: " + ((err as Error).message || ""));
    }
  };

  return (
    <div
      id="assistant-agent-dialog"
      className="modal"
      onClick={(e) => {
        if (e.target === e.currentTarget) closeDialog();
      }}
    >
      <div id="assistant-agent-panel">
        <div className="ws-head">
          <h2>Assistant agent file</h2>
          <button type="button" id="assistant-agent-close" onClick={closeDialog}>
            Close
          </button>
        </div>
        <p className="set-hint">
          Your own instructions for the personal assistant — added on top of its built-in
          behavior. MindFlock always keeps the assistant's core rules (how it answers, how it
          manages your todo list), so nothing here can break those. Describe how it should
          behave, what it knows, tone, etc. Applies the next time it starts.
        </p>
        <textarea
          id="assistant-agent-text"
          ref={taRef}
          rows={16}
          spellCheck={false}
          placeholder="e.g. Always answer concisely. Prefer bullet points. I work in Pacific time. Call me Ethan."
          value={text}
          onChange={(e) => setText(e.target.value)}
        />
        <div className="modal-actions">
          <span id="assistant-agent-status" className="set-hint">{status}</span>
          <button type="button" id="assistant-agent-restart" className="test-btn" onClick={() => save(true)}>
            Save &amp; restart
          </button>
          <button type="button" id="assistant-agent-save" onClick={() => save(false)}>
            Save
          </button>
        </div>
      </div>
    </div>
  );
}
