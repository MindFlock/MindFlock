/** Assistant agent file editor (port of initAssistantAgentFile, section
 * 18): the personal assistant's standing instructions (its CLAUDE.md). */

import { useEffect, useRef, useState } from "react";
import { api } from "../../api/client";
import { useUi } from "../../state/store";
import { toast } from "../../lib/toast";
import { errMsg } from "../../lib/format";
import { Modal } from "../Modal";
import { AgentPicker, useAgentChoices } from "../settings/screens/AgentPicker";

/** Where the instructions load stands. Save stays off until "ok": a box that
 * failed to load is empty, and saving it would write "" over the real file. */
export type InstructionsLoad = "loading" | "ok" | "failed";

/** What a finished save says. A restart that failed is reported, never
 * covered by "assistant restarted". */
export function assistantSaveStatus(restart: boolean, restartError: string | null): string {
  if (!restart) return "Saved. Applies the next time the assistant starts.";
  if (restartError !== null) return "Saved, but the assistant didn't restart: " + restartError;
  return "Saved and the assistant restarted. Reopen Chat to talk to it.";
}

export function AssistantAgentDialog() {
  const open = useUi((s) => s.openDialog === "assistant-agent");
  const closeDialog = useUi((s) => s.closeDialog);
  const [text, setText] = useState("");
  // The file as last loaded or saved: the box differs from it = unsaved edits.
  const [saved, setSaved] = useState("");
  const [load, setLoad] = useState<InstructionsLoad>("loading");
  const [loadError, setLoadError] = useState("");
  const [saving, setSaving] = useState(false);
  const [status, setStatus] = useState("");
  const [agent, setAgent] = useState("");
  const agentChoices = useAgentChoices();
  const taRef = useRef<HTMLTextAreaElement | null>(null);
  // Typed into the box before the load landed: the load must not overwrite it.
  const typed = useRef(false);
  // Bumped per load, so a slow answer for an earlier open or Retry is dropped.
  const loadSeq = useRef(0);

  /** Load the file (on open, and from Retry). Never replaces typed text. */
  const loadInstructions = async () => {
    const seq = ++loadSeq.current;
    setLoad("loading");
    setLoadError("");
    try {
      const r = await api<{ text?: string }>("/api/assistant/instructions");
      if (seq !== loadSeq.current) return;
      const file = r?.text || "";
      setSaved(file);
      if (!typed.current) setText(file);
      setLoad("ok");
    } catch (err) {
      if (seq !== loadSeq.current) return;
      setLoadError(errMsg(err));
      setLoad("failed");
    }
  };

  useEffect(() => {
    if (!open) return;
    setStatus("");
    setSaving(false);
    typed.current = false;
    setText("");
    setSaved("");
    void (async () => {
      await loadInstructions();
      try {
        const st = await api<{
          settings?: { coding_cli?: { assistant_provider?: string } };
        }>("/api/settings");
        setAgent(String(st?.settings?.coding_cli?.assistant_provider || ""));
      } catch {
        setAgent("");
      }
      setTimeout(() => taRef.current?.focus(), 0);
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  if (!open) return null;

  const dirty = text !== saved;
  const canSave = load === "ok" && !saving;

  const save = async (restart: boolean) => {
    if (!canSave) return;
    setSaving(true);
    setStatus("Saving…");
    try {
      await api("/api/assistant/instructions", { method: "PUT", json: { text } });
      setSaved(text);
      let restartError: string | null = null;
      if (restart) {
        try {
          await api("/api/assistant/restart", { method: "POST" });
        } catch (err) {
          restartError = errMsg(err);
        }
      }
      setStatus(assistantSaveStatus(restart, restartError));
      if (restartError === null)
        toast(restart ? "Instructions saved & assistant restarted" : "Instructions saved");
    } catch (err) {
      setStatus("Save failed: " + errMsg(err));
    } finally {
      setSaving(false);
    }
  };

  return (
    // A backdrop click never throws away unsaved instructions.
    <Modal id="assistant-agent-dialog" onClose={closeDialog} closeOnBackdrop={!dirty}>
      <div id="assistant-agent-panel">
        <div className="ws-head">
          {/* "Instructions", like both of its doors: "Agent" is the coding
              agent's word alone. Its file is still the agent file. */}
          <h2>Assistant instructions</h2>
          <button type="button" id="assistant-agent-close" onClick={closeDialog}>
            Close
          </button>
        </div>
        <p className="set-hint">
          Your own instructions for the personal assistant — added on top of its built-in
          behavior. MindFlock always keeps the assistant's core rules (how it answers, how it
          manages your To-do list), so nothing here can break those. Describe how it should
          behave, what it knows, tone, etc. Applies the next time it starts.
        </p>
        <AgentPicker
          label="Agent"
          value={agent}
          choices={agentChoices}
          onChange={async (v) => {
            setAgent(v);
            try {
              await api("/api/settings", {
                method: "PUT",
                json: { coding_cli: { assistant_provider: v } },
              });
              setStatus("Agent saved — Save & restart to switch the running assistant.");
            } catch (err) {
              setStatus("Could not save agent: " + ((err as Error).message || ""));
            }
          }}
          hint={
            <>
              Which agent runs the assistant. It doesn't have to be the one your sessions
              use. Takes effect the next time the assistant starts — <b>Save &amp; restart</b>
              below does that now.
            </>
          }
        />
        <textarea
          id="assistant-agent-text"
          ref={taRef}
          rows={16}
          spellCheck={false}
          placeholder="e.g. Always answer concisely. Prefer bullet points. I work in Pacific time. Call me Ethan."
          value={text}
          onChange={(e) => {
            typed.current = true;
            setText(e.target.value);
          }}
        />
        {load === "failed" && (
          <p className="set-hint" id="assistant-agent-load-failed" role="alert">
            Couldn't load the instructions. {loadError}{" "}
            <button type="button" className="linklike" onClick={() => void loadInstructions()}>
              Retry
            </button>
          </p>
        )}
        <div className="modal-actions">
          <span id="assistant-agent-status" className="set-hint">{status}</span>
          <button
            type="button"
            id="assistant-agent-restart"
            className="test-btn"
            disabled={!canSave}
            onClick={() => void save(true)}
          >
            Save &amp; restart
          </button>
          <button type="button" id="assistant-agent-save" disabled={!canSave} onClick={() => void save(false)}>
            Save
          </button>
        </div>
      </div>
    </Modal>
  );
}
