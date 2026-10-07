/** "Install everything missing" — one button, one terminal, one sudo prompt.
 *
 * The doctor already knows every dependency this machine needs and lacks
 * (`GET /api/doctor` → `install.steps`); this runs them as ONE script in a real
 * terminal (`/api/doctor/install-terminal`), because sudo has to ask for a
 * password and people want to watch apt work. The script is rebuilt by the
 * server from a fresh probe — nothing here sends a command.
 *
 * Closing mid-install does not kill it: the server keeps the run going and the
 * next open reattaches. The window watches `/api/doctor/install-state` and calls
 * `onDone` when the script finishes, so the checklist behind it re-probes on its
 * own instead of showing stale ✗s. */

import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { api } from "../../api/client";
import { toast } from "../../lib/toast";
import { useWsTerm } from "../../lib/wsTerm";

export interface InstallStep {
  id: string;
  label: string;
  cmd: string;
}

interface InstallState {
  running: boolean;
  exit_code: number | null;
}

/** The summary line under the button: what one click installs. */
export function installSummary(steps: InstallStep[]): string {
  return steps.map((s) => s.label.replace(/^system packages: /, "")).join(", ");
}

function InstallWindow({ onClose, onDone }: { onClose(): void; onDone(): void }) {
  const hostRef = useRef<HTMLDivElement>(null);
  const state = useWsTerm(hostRef, "/api/doctor/install-terminal", true);
  const [exit, setExit] = useState<number | null>(null);
  const doneRef = useRef(onDone);
  doneRef.current = onDone;

  useEffect(() => {
    let live = true;
    let fired = false;
    let sawRunning = false;
    const poll = async () => {
      try {
        const s = await api<InstallState>("/api/doctor/install-state");
        if (!live) return;
        // Only an exit status that follows a run we watched counts — never a
        // previous run's file read before this one started.
        if (s.running) sawRunning = true;
        if (!sawRunning || s.exit_code === null || s.exit_code === undefined) return;
        setExit(s.exit_code);
        if (!fired) {
          fired = true;
          doneRef.current();
        }
      } catch {
        /* the conn banner owns an unreachable server */
      }
    };
    const t = setInterval(poll, 1000);
    return () => {
      live = false;
      clearInterval(t);
    };
  }, []);

  const close = async () => {
    try {
      const r = await api<{ closed: boolean }>("/api/doctor/install-close", { method: "POST" });
      if (!r.closed) toast("Still installing — it keeps going in the background");
    } catch {
      /* closing the window is what the user asked for either way */
    }
    onClose();
  };

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape" || e.defaultPrevented) return;
      e.preventDefault();
      e.stopPropagation();
      void close();
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const status =
    exit === 0
      ? "✓ everything installed"
      : exit !== null
        ? "✗ some steps failed — scroll up to see which"
        : state === "streaming"
          ? "installing — type your password if sudo asks"
          : state;

  return createPortal(
    <div
      className="modal"
      id="install-dialog"
      role="dialog"
      aria-modal="true"
      onClick={(e) => {
        if (e.target === e.currentTarget) void close();
      }}
    >
      <div className="prov-login-panel">
        <div className="ws-head">
          <h2>Install missing dependencies</h2>
          <span className="muted" id="install-status">
            {status}
          </span>
          <button type="button" id="install-close" onClick={() => void close()}>
            Close
          </button>
        </div>
        <div className="prov-login-term" ref={hostRef} />
      </div>
    </div>,
    document.body
  );
}

/** The button + its window. Renders nothing when there is nothing to install. */
export function InstallMissing({
  steps,
  onDone,
}: {
  steps: InstallStep[] | undefined;
  onDone(): void;
}) {
  const [open, setOpen] = useState(false);
  // The window outlives the button: a successful install empties `steps` (the
  // re-probe finds nothing missing), and that is exactly the moment the result
  // has to stay on screen.
  const window_ = open && <InstallWindow onClose={() => setOpen(false)} onDone={onDone} />;
  if (!steps || !steps.length) return window_ || null;
  return (
    <div className="doctor-install" id="doctor-install">
      <button type="button" className="test-btn" id="doctor-install-btn" onClick={() => setOpen(true)}>
        Install everything missing
      </button>
      <span className="set-hint"> {installSummary(steps)}</span>
      {window_}
    </div>
  );
}
