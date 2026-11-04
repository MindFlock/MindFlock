/** The "Memory is low" confirm a person's own start gets (lib/memoryConfirm
 * holds the rules; this is the surface). Opens by itself when a New,
 * Duplicate, Start, Start all, Resume or Wake comes back 409
 * `memory_confirm`, and settles the waiting start:
 *   Start anyway · Put idle agents to sleep first · Cancel.
 * Electron has no window.confirm — this is the app's own dialog. */

import { useSyncExternalStore } from "react";
import { Modal } from "./Modal";
import { currentMemoryConfirm, subscribeMemoryConfirm } from "../lib/memoryConfirm";
import "./MemoryConfirm.css";

export function MemoryConfirm() {
  const p = useSyncExternalStore(subscribeMemoryConfirm, currentMemoryConfirm, currentMemoryConfirm);
  if (!p) return null;
  const critical = p.body.level === "critical";
  return (
    <Modal id="memory-confirm" role="alertdialog" aria-labelledby="memory-confirm-head" onClose={() => p.resolve("cancel")}>
      <div className={"mc-card" + (critical ? " critical" : "")}>
        <h2 id="memory-confirm-head">{critical ? "Memory is critically low" : "Memory is low"}</h2>
        <p className="mc-message">{p.body.message}</p>
        <p className="mc-what">
          {critical
            ? "MindFlock won't start " + p.what + " unless you say so. Putting idle agents to sleep frees their memory and keeps their conversations."
            : "Agents that start by themselves are waiting until memory recovers. Putting idle agents to sleep frees their memory and keeps their conversations."}
        </p>
        <div className="modal-actions">
          <button type="button" id="memory-confirm-cancel" onClick={() => p.resolve("cancel")}>
            Cancel
          </button>
          {p.canSleep && (
            <button type="button" id="memory-confirm-sleep" onClick={() => p.resolve("sleep")}>
              Put idle agents to sleep first
            </button>
          )}
          <button
            type="button"
            id="memory-confirm-start"
            className={critical ? "mc-danger" : "mc-start"}
            onClick={() => p.resolve("start")}
          >
            Start anyway
          </button>
        </div>
      </div>
    </Modal>
  );
}
