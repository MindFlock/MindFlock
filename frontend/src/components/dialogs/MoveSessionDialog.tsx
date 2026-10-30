/** Move to a folder… (docs/flights.md "Moving a session"): the session ⋯
 * menu's item opens this on `dialogTarget` = the title. The field is the NEW
 * folder's path, prefilled with the current folder's parent + "/" so typing a
 * name (Tab completes through /api/browse) lands it beside where it is now.
 * Move asks the server (lib/sessionActions moveSession); a 202 closes this and
 * the row says "moving…" until `session.moved` / `session.move_failed`. A 409
 * `working` without the box ticked is answered here, under the box that
 * remedies it; every other failure goes on the error card. No
 * window.confirm/prompt (Electron has none). */

import { useEffect, useState } from "react";
import type { Instance } from "../../api/types";
import { useUi } from "../../state/store";
import { useInstances } from "../../state/queries";
import { Modal } from "../Modal";
import { PathField } from "../PathField";
import { MOVE_MID_TURN, moveSession } from "../../lib/sessionActions";
import { windowName } from "../../lib/windowName";
import "./MoveSessionDialog.css";

/** The folder a session lives in: its workspace path. */
export function sessionFolder(inst: Partial<Instance> | undefined): string {
  return inst?.path || inst?.folder || "";
}

/** The field's first value: the current folder's parent + "/" ("" when the
 * folder is unknown or is a root). */
export function moveDestSeed(folder: string): string {
  const f = folder.replace(/[\\/]+$/, "");
  const cut = Math.max(f.lastIndexOf("/"), f.lastIndexOf("\\"));
  return cut > 0 ? f.slice(0, cut + 1) : "";
}

/** Why Move is greyed ("" = it can go): nothing typed, the same folder, or
 * the parent alone (the seed, before a name is typed). */
export function moveBlockReason(folder: string, dest: string): string {
  const d = dest.trim();
  if (!d) return "Type the new folder's path";
  const strip = (p: string) => p.replace(/[\\/]+$/, "");
  if (strip(d) === strip(folder)) return "That is where it is now";
  if (/[\\/]$/.test(d) && strip(d) === strip(moveDestSeed(folder))) return "Add the new folder's name";
  return "";
}

export function MoveSessionDialog() {
  const open = useUi((s) => s.openDialog === "move");
  const target = useUi((s) => s.dialogTarget);
  const closeDialog = useUi((s) => s.closeDialog);
  const { data } = useInstances();
  if (!open || !target) return null;
  const inst = (data || []).find((i) => i.title === target);
  // Keyed on the title: every opening starts from the seed, and a poll never
  // overwrites what is being typed.
  return <MoveForm key={target} title={target} folder={sessionFolder(inst)} onClose={closeDialog} />;
}

function MoveForm({ title, folder, onClose }: { title: string; folder: string; onClose(): void }) {
  const [dest, setDest] = useState(() => moveDestSeed(folder));
  const [force, setForce] = useState(false);
  const [midTurn, setMidTurn] = useState(false);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    setTimeout(() => {
      // Caret at the end: the seed is a parent folder, the name comes next.
      const el = document.getElementById("move-dest") as HTMLInputElement | null;
      if (!el) return;
      el.focus();
      el.setSelectionRange(el.value.length, el.value.length);
    }, 0);
  }, []);

  const name = windowName(title);
  const why = moveBlockReason(folder, dest);

  const submit = async () => {
    if (why || busy) return;
    setBusy(true);
    setMidTurn(false);
    const r = await moveSession(title, dest.trim(), force);
    setBusy(false);
    if (r.ok) onClose();
    else if (r.refusal === "working" && !force) setMidTurn(true);
  };

  return (
    <Modal id="move-dialog" onClose={onClose}>
      <form
        id="move-form"
        onSubmit={(e) => {
          e.preventDefault();
          void submit();
        }}
      >
        <h2>Move “{name}” to a folder</h2>
        <div className="move-now">
          <span className="move-now-label">Now in</span>
          <code className="move-now-path" title={folder}>
            {folder || "(folder unknown)"}
          </code>
        </div>
        <label>
          New folder
          <PathField
            id="move-dest"
            value={dest}
            onChange={(v) => {
              setDest(v);
              setMidTurn(false);
            }}
            pickerTitle="Pick where the folder goes"
            placeholder="/path/to/new-folder"
            aria-invalid={midTurn}
          />
        </label>
        <p className="move-says">
          MindFlock pauses the agent, moves the folder, and resumes it there. Linked worktrees are repaired.
        </p>
        <label className="move-force">
          <input
            type="checkbox"
            id="move-force"
            checked={force}
            onChange={(e) => {
              setForce(e.target.checked);
              setMidTurn(false);
            }}
          />
          Even if it is mid-turn
        </label>
        {midTurn && (
          <p className="move-midturn" role="alert">
            {MOVE_MID_TURN}
          </p>
        )}
        <div className="modal-actions">
          <button type="button" id="move-cancel" onClick={onClose}>
            Cancel
          </button>
          <button type="submit" id="move-submit" disabled={!!why || busy} title={why || undefined}>
            {busy ? "Moving…" : "Move"}
          </button>
        </div>
      </form>
    </Modal>
  );
}
