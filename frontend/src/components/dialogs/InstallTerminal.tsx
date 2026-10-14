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
import { api } from "../../api/client";
import type { InstallStep } from "../../api/types";
import { toast } from "../../lib/toast";
import { copyText } from "../../lib/clipboard";
import { useWsTerm } from "../../lib/wsTerm";
import { Modal } from "../Modal";

export type { InstallStep };

interface InstallState {
  running: boolean;
  exit_code: number | null;
  /** When the current run started (epoch seconds); absent on an older server. */
  started_at?: number | null;
}

/** After this long without finishing, the window offers the commands to run
 * by hand — a silent `curl` on a slow or blocked network looks exactly like a
 * hang, and the person must never be left watching a cursor. The run itself
 * keeps going (sudo may simply be waiting for a password). */
export const INSTALL_SLOW_S = 90;

/** "1:05" for a span in seconds. */
export function fmtElapsed(secs: number): string {
  const s = Math.max(0, Math.floor(secs));
  return Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0");
}

/** The commands the one-click install runs, one per line — what to paste into
 * your own terminal when the window can't finish it. */
export function manualCommands(steps: InstallStep[] | undefined): string {
  return (steps || [])
    .map((s) => String(s.cmd || "").trim())
    .filter(Boolean)
    .join("\n");
}

/** Seconds since the run started: the server's start when it says, else
 * since this page first saw it running. */
function elapsedSince(st: InstallState | null, seenAt: number, nowMs: number): number {
  const from = st?.started_at ? st.started_at * 1000 : seenAt;
  return (nowMs - from) / 1000;
}

/** The "do it yourself" box: the commands, and Copy. */
function ManualFallback({ cmds, lead }: { cmds: string; lead: string }) {
  if (!cmds) return null;
  return (
    <div className="install-manual" id="install-manual">
      <span>{lead}</span>
      <pre className="install-cmd">
        <code>{cmds}</code>
      </pre>
      <button
        type="button"
        className="test-btn"
        onClick={async () => {
          if (await copyText(cmds)) toast("Copied — paste it into a terminal");
        }}
      >
        Copy
      </button>
    </div>
  );
}

/** The summary line under the button: what one click installs. */
export function installSummary(steps: InstallStep[]): string {
  return steps.map((s) => s.label.replace(/^system packages: /, "")).join(", ");
}

/** The short names of what the plan installs: package names, the agent CLI's
 * own name ("agent CLI (claude)" → "claude"), "Homebrew"… */
function stepNames(steps: InstallStep[]): string[] {
  const out: string[] = [];
  for (const s of steps) {
    if (s.id === "packages") {
      out.push(...s.label.replace(/^system packages: /, "").split(", "));
    } else if (s.id === "homebrew") {
      out.push("Homebrew");
    } else {
      const m = /\(([^)]+)\)\s*$/.exec(s.label);
      out.push(m && /-cli$/.test(s.id) ? m[1] : s.label);
    }
  }
  return out.filter(Boolean);
}

/** The button's text: names what one click installs ("Install tmux + claude")
 * when that fits, rather than a generic "everything missing". */
export function installButtonLabel(steps: InstallStep[]): string {
  const names = stepNames(steps);
  if (!names.length || names.length > 3) return "Install everything missing";
  return "Install " + names.join(" + ");
}

/** Whether the run will ask for a password (a package-manager or Homebrew step
 * — sudo, once, in the terminal), so the button can say so up front. */
export function asksForPassword(steps: InstallStep[]): boolean {
  return steps.some(
    (s) => s.id === "packages" || s.id === "homebrew" || /(^|[\s;&|(])sudo\s/.test(s.cmd || "")
  );
}

function InstallWindow({
  steps,
  onClose,
  onDone,
}: {
  steps: InstallStep[] | undefined;
  onClose(): void;
  onDone(): void;
}) {
  const hostRef = useRef<HTMLDivElement>(null);
  const state = useWsTerm(hostRef, "/api/doctor/install-terminal", true);
  const [exit, setExit] = useState<number | null>(null);
  const [run, setRun] = useState<InstallState | null>(null);
  const [now, setNow] = useState(() => Date.now());
  const seenAt = useRef(Date.now());
  // The commands are captured once: a successful run empties `steps` (the
  // re-probe finds nothing missing), and a failed one must still show them.
  const [cmds] = useState(() => manualCommands(steps));
  const doneRef = useRef(onDone);
  doneRef.current = onDone;

  useEffect(() => {
    let live = true;
    let fired = false;
    let sawRunning = false;
    const poll = async () => {
      setNow(Date.now());
      try {
        const s = await api<InstallState>("/api/doctor/install-state");
        if (!live) return;
        // Only an exit status that follows a run we watched counts — never a
        // previous run's file read before this one started.
        if (s.running) {
          sawRunning = true;
          setRun(s);
        }
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

  const secs = exit === null ? elapsedSince(run, seenAt.current, now) : 0;
  const slow = exit === null && secs >= INSTALL_SLOW_S;
  const what = installSummary(steps || []);
  const status =
    exit === 0
      ? "✓ everything installed"
      : exit !== null
        ? "✗ some steps failed — scroll up to see which"
        : state === "streaming"
          ? "Installing" + (what ? " " + what : "") + " · " + fmtElapsed(secs) +
            (asksForPassword(steps || []) ? " — type your password if asked" : " — downloads can take a few minutes")
          : state;

  // Escape is caught in the capture phase: it must close this window before
  // the terminal inside it (or Settings under it) sees the key.
  return (
    <Modal
      id="install-dialog"
      role="dialog"
      aria-modal="true"
      portal
      escapeCapture
      onClose={() => void close()}
    >
      <div className="prov-login-panel">
        <div className="ws-head">
          <h2>Install missing tools</h2>
          <span className="muted" id="install-status">
            {status}
          </span>
          <button type="button" id="install-close" onClick={() => void close()}>
            Close
          </button>
        </div>
        {(slow || (exit !== null && exit !== 0)) && (
          <ManualFallback
            cmds={cmds}
            lead={
              slow
                ? "Taking a while — it may be stuck on the network. It keeps trying; or run this in your own terminal:"
                : "Didn't finish. Run this in your own terminal instead:"
            }
          />
        )}
        <div className="prov-login-term" ref={hostRef} />
      </div>
    </Modal>
  );
}

/** A run still going, from a fresh page (a reload, another tab): the button's
 * place says so and reopens the window — the run never stopped. Polls only
 * while there is something to follow. */
function useRunningInstall(active: boolean, onFinished: () => void) {
  const [run, setRun] = useState<InstallState | null>(null);
  const [now, setNow] = useState(() => Date.now());
  const seenAt = useRef(Date.now());
  const fin = useRef(onFinished);
  fin.current = onFinished;
  useEffect(() => {
    if (!active) return;
    let live = true;
    let t: ReturnType<typeof setTimeout> | null = null;
    let wasRunning = false;
    const poll = async () => {
      try {
        const s = await api<InstallState>("/api/doctor/install-state");
        if (!live) return;
        setNow(Date.now());
        if (s.running) {
          wasRunning = true;
          setRun(s);
          t = setTimeout(poll, 2000);
        } else {
          setRun(null);
          if (wasRunning) fin.current();
        }
      } catch {
        /* an older server or a gated one: just the button, as before */
      }
    };
    void poll();
    return () => {
      live = false;
      if (t) clearTimeout(t);
    };
  }, [active]);
  return run ? elapsedSince(run, seenAt.current, now) : null;
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
  // Not while the window is open: it follows the run itself.
  const runningFor = useRunningInstall(!open && !!steps?.length, onDone);
  // The window outlives the button: a successful install empties `steps` (the
  // re-probe finds nothing missing), and that is exactly the moment the result
  // has to stay on screen.
  const window_ = open && <InstallWindow steps={steps} onClose={() => setOpen(false)} onDone={onDone} />;
  if (!steps || !steps.length) return window_ || null;
  if (runningFor !== null)
    return (
      <div className="doctor-install" id="doctor-install">
        <span className="install-running">Installing… {fmtElapsed(runningFor)}</span>{" "}
        <button type="button" className="test-btn" id="doctor-install-show" onClick={() => setOpen(true)}>
          Show
        </button>
        {runningFor >= INSTALL_SLOW_S && (
          <ManualFallback cmds={manualCommands(steps)} lead="Taking a while? Run this in your own terminal instead:" />
        )}
        {window_}
      </div>
    );
  return (
    <div className="doctor-install" id="doctor-install">
      <button type="button" className="test-btn" id="doctor-install-btn" onClick={() => setOpen(true)}>
        {installButtonLabel(steps)}
      </button>
      <span className="set-hint">
        {" "}
        {installSummary(steps)}
        {asksForPassword(steps) ? " — asks for your password once" : ""}
      </span>
      <details className="install-manual-fold">
        <summary>Or run it yourself</summary>
        <ManualFallback cmds={manualCommands(steps)} lead="Paste this into a terminal on this computer:" />
      </details>
      {window_}
    </div>
  );
}
