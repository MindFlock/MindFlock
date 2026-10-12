/** Rename dialog (port of renameSession/submitRename, section 8) — Electron
 * has no window.prompt(), hence a real modal. The new name is the session's
 * name everywhere (rail, phone, pushes), so it must be one no other session
 * has: refused inline here, and by the server (409) for what this can't see. */

import { useEffect, useRef, useState } from "react";
import { useUi } from "../../state/store";
import { Modal } from "../Modal";
import { toast } from "../../lib/toast";
import { nameTakenBy, windowName } from "../../lib/windowName";

export function RenameDialog() {
  const open = useUi((s) => s.openDialog === "rename");
  const target = useUi((s) => s.dialogTarget);
  const closeDialog = useUi((s) => s.closeDialog);
  const setAlias = useUi((s) => s.setAlias);
  const [value, setValue] = useState("");
  const [shown, setShown] = useState("");
  const inputRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    if (open && target) {
      const current = windowName(target);
      setShown(current);
      setValue(current);
      setTimeout(() => {
        inputRef.current?.focus();
        inputRef.current?.select();
      }, 0);
    }
  }, [open, target]);

  if (!open || !target) return null;

  const next = value.trim();
  const taken = next && next !== shown ? nameTakenBy(target, next) : "";

  const submit = () => {
    if (taken) return;
    if (!next || next === shown) {
      closeDialog();
      return;
    }
    setAlias(target, next);
    closeDialog();
    toast(`Renamed to “${next}”`);
  };

  return (
    <Modal id="rename-dialog" onClose={closeDialog}>
      <form
        id="rename-form"
        onSubmit={(e) => {
          e.preventDefault();
          submit();
        }}
      >
        <h2>Rename session</h2>
        <label>
          New name
          <input
            type="text"
            id="rename-input"
            ref={inputRef}
            autoComplete="off"
            spellCheck={false}
            aria-invalid={!!taken}
            value={value}
            onChange={(e) => setValue(e.target.value)}
          />
        </label>
        {taken && (
          <p className="rename-taken" role="alert">
            Another session is already named “{next}”.
          </p>
        )}
        <div className="modal-actions">
          <button type="button" id="rename-cancel" onClick={closeDialog}>
            Cancel
          </button>
          <button type="submit" disabled={!!taken || !next}>
            Rename
          </button>
        </div>
      </form>
    </Modal>
  );
}
