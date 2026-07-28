/** Pair-with-device dialog (port of showDeviceConnect/submitDeviceConnect,
 * section 8): collects the REMOTE device's access token; the server
 * validates it against that device before persisting. */

import { useEffect, useRef, useState } from "react";
import { api } from "../../api/client";
import { queryClient, refreshInstances } from "../../state/queries";
import { useUi } from "../../state/store";
import { toast } from "../../lib/toast";

export function DeviceDialog() {
  const open = useUi((s) => s.openDialog === "device");
  const device = useUi((s) => s.dialogTarget);
  const closeDialog = useUi((s) => s.closeDialog);
  const [token, setToken] = useState("");
  const [err, setErr] = useState("");
  const inputRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    if (open) {
      setToken("");
      setErr("");
      setTimeout(() => inputRef.current?.focus(), 0);
    }
  }, [open]);

  if (!open || !device) return null;

  const submit = async () => {
    setErr("");
    try {
      const devices = await api(`/api/devices/${encodeURIComponent(device)}/connect`, {
        json: { token: token.trim() },
      });
      queryClient.setQueryData(["devices"], devices);
      closeDialog();
      toast("Connected to " + device);
      refreshInstances();
    } catch (e) {
      setErr((e as Error).message || "could not connect");
    }
  };

  return (
    <div
      id="device-dialog"
      className="modal"
      onClick={(e) => {
        if (e.target === e.currentTarget) closeDialog();
      }}
      onKeyDown={(e) => {
        if (e.key === "Escape") closeDialog();
      }}
    >
      <form
        id="device-form"
        onSubmit={(e) => {
          e.preventDefault();
          submit();
        }}
      >
        <h2>Connect to device</h2>
        <label>
          Access token for <span id="device-dialog-name" className="muted">{device}</span>
          <input
            type="password"
            id="device-token-input"
            ref={inputRef}
            autoComplete="off"
            spellCheck={false}
            placeholder="Token from that device's startup banner"
            value={token}
            onChange={(e) => setToken(e.target.value)}
          />
        </label>
        <p className="muted device-dialog-hint">
          The other device must have “Allow remote control” turned on in its Security settings.
        </p>
        <div className="modal-actions">
          <span id="device-dialog-err" className="dialog-err">
            {err}
          </span>
          <button type="button" id="device-cancel" onClick={closeDialog}>
            Cancel
          </button>
          <button type="submit">Connect</button>
        </div>
      </form>
    </div>
  );
}
