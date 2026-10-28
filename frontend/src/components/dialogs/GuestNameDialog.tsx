/** "What should we call you?" — a guest link's first screen (docs/people.md).
 * The owner does not name the link; the guest names themselves, once, when
 * they open it (`POST /api/guest/name`), and the name must be free on this
 * rail: another guest's, or the owner's own, is a 409 the form shows. Until a
 * name is set the dialog stays (no Cancel, Escape does nothing): the pill on
 * the owner's rail needs a name before the guest is shown anywhere, and
 * App.tsx reports focus only once `config.guest.name` is set. */

import { useEffect, useRef, useState } from "react";
import { api } from "../../api/client";
import { errMsg } from "../../lib/format";
import { refreshConfig, useConfig } from "../../state/queries";
import { Modal } from "../Modal";

export function GuestNameDialog() {
  const { data: config } = useConfig();
  const guest = config?.guest;
  const open = !!guest && !guest.name;
  const [value, setValue] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const inputRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    if (open) setTimeout(() => inputRef.current?.focus(), 0);
  }, [open]);

  if (!open) return null;
  const next = value.trim();

  const submit = async () => {
    if (!next || busy) return;
    setBusy(true);
    setErr("");
    try {
      await api<{ name: string }>("/api/guest/name", { json: { name: next } });
      await refreshConfig();
    } catch (e) {
      setErr(errMsg(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal id="guest-name-dialog" role="dialog" aria-modal="true" portal closeOnBackdrop={false} closeOnEscape={false} onClose={() => {}}>
      <form
        id="guest-name-form"
        onSubmit={(e) => {
          e.preventDefault();
          void submit();
        }}
      >
        <h2>What should we call you?</h2>
        <p className="set-hint">
          You've been let into {guest.sessions.length === 1 ? "one session" : guest.sessions.length + " sessions"}.
          The owner sees your name on the session you're looking at.
        </p>
        <label>
          Your name
          <input
            type="text"
            id="guest-name-input"
            ref={inputRef}
            autoComplete="off"
            spellCheck={false}
            maxLength={40}
            aria-invalid={!!err}
            value={value}
            onChange={(e) => {
              setValue(e.target.value);
              setErr("");
            }}
          />
        </label>
        {err && (
          <p className="rename-taken" role="alert">
            {err}
          </p>
        )}
        <div className="modal-actions">
          <button type="submit" disabled={!next || busy}>
            {busy ? "Joining…" : "Join"}
          </button>
        </div>
      </form>
    </Modal>
  );
}
