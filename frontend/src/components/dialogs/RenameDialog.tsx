/** Rename dialog (port of renameSession/submitRename, section 8): sets a
 * display alias — Electron has no window.prompt(), hence a real modal. */

import { useEffect, useRef, useState } from "react";
import { useUi } from "../../state/store";
import { toast } from "../../lib/toast";

export function RenameDialog() {
  const open = useUi((s) => s.openDialog === "rename");
  const target = useUi((s) => s.dialogTarget);
  const closeDialog = useUi((s) => s.closeDialog);
  const setAlias = useUi((s) => s.setAlias);
  const [value, setValue] = useState("");
  const inputRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    if (open && target) {
      setValue(useUi.getState().aliases[target] || "");
      setTimeout(() => {
        inputRef.current?.focus();
        inputRef.current?.select();
      }, 0);
    }
  }, [open, target]);

  if (!open || !target) return null;

  const submit = () => {
    const alias = value.trim();
    setAlias(target, alias);
    closeDialog();
    toast(alias ? `Renamed to “${alias}”` : "Reset to real title");
  };

  return (
    <div
      id="rename-dialog"
      className="modal"
      onClick={(e) => {
        if (e.target === e.currentTarget) closeDialog();
      }}
      onKeyDown={(e) => {
        if (e.key === "Escape") closeDialog();
      }}
    >
      <form
        id="rename-form"
        onSubmit={(e) => {
          e.preventDefault();
          submit();
        }}
      >
        <h2>Rename session</h2>
        <label>
          Display name for <span id="rename-real" className="muted">“{target}”</span>
          <input
            type="text"
            id="rename-input"
            ref={inputRef}
            autoComplete="off"
            spellCheck={false}
            placeholder="Leave blank to reset to the real title"
            value={value}
            onChange={(e) => setValue(e.target.value)}
          />
        </label>
        <div className="modal-actions">
          <button type="button" id="rename-cancel" onClick={closeDialog}>
            Cancel
          </button>
          <button type="submit">Rename</button>
        </div>
      </form>
    </div>
  );
}
