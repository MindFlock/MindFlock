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
import { create } from "zustand";
import { api } from "../../api/client";
import type { InstallStep } from "../../api/types";
import { toast } from "../../lib/toast";
import { copyText } from "../../lib/clipboard";
import { useWsTerm } from "../../lib/wsTerm";
import { Modal } from "../Modal";
import { notifyConnectionsChanged } from "../settings/screens/Connections";
import { agentName } from "../../lib/agentNames";
import { SignInWindow } from "./AgentSignIn";
import "./ConnectGitHub.css";

export type { InstallStep };

/** Whether the install window (or the sign-in window it hands over to) is
 * up. The Setup checklist keeps step ①'s body mounted while it is: a
 * successful install ticks the step done, and unmounting the body used to
 * take the window — and its "✓ installed" moment — with it. */
export const useInstallWindow = create<{ open: boolean; set(open: boolean): void }>((set) => ({
  open: false,
  set: (open) => set({ open }),
}));

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

/** The agent an install plan installs ("agent CLI (claude)" → "claude"),
 * or "" when it installs none. */
export function installedAgent(steps: InstallStep[]): string {
  const agent = steps.find((s) => s.id === "agent-cli");
  const m = agent ? /\(([^)]+)\)\s*$/.exec(agent.label) : null;
  return m ? m[1] : "";
}

/** What a finished install says: what landed, and the next step when an
 * agent was part of it. */
export function installedMessage(steps: InstallStep[]): string {
  const agent = installedAgent(steps);
  return agent ? "✓ " + agentName(agent) + " installed — Next: Sign in" : "✓ Everything is installed";
}

/** The summary of what one click installs, by the names a person knows
 * ("tmux, Claude Code" — never "agent CLI (claude)"). */
export function installSummary(steps: InstallStep[]): string {
  return stepNames(steps).join(", ");
}

/** The short names of what the plan installs: package names, an agent CLI by
 * its product name ("agent CLI (claude)" → "Claude Code"), "Homebrew"… */
function stepNames(steps: InstallStep[]): string[] {
  const out: string[] = [];
  for (const s of steps) {
    if (s.id === "packages") {
      out.push(...s.label.replace(/^system packages: /, "").split(", "));
    } else if (s.id === "homebrew") {
      out.push("Homebrew");
    } else {
      const m = /\(([^)]+)\)\s*$/.exec(s.label);
      out.push(m && /-cli$/.test(s.id) ? agentName(m[1]) : s.label);
    }
  }
  return out.filter(Boolean);
}

/** The window's line while the run goes: what it is fetching, by name, and
 * how long it has been at it ("Downloading Claude Code… 0:12"). The script's
 * own output can be silent for a minute (curl), so this line is the progress. */
export function installProgress(steps: InstallStep[], secs: number): string {
  const agent = installedAgent(steps);
  const others = stepNames(steps).filter((n) => !agent || n !== agentName(agent));
  const what = agent && !others.length ? "Downloading " + agentName(agent) : "Installing " + stepNames(steps).join(", ");
  const tail = asksForPassword(steps) ? " — type your password if asked" : " — can take a few minutes";
  return (steps.length ? what : "Installing") + "… " + fmtElapsed(secs) + tail;
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
  onSignIn,
}: {
  steps: InstallStep[] | undefined;
  onClose(): void;
  onDone(): void;
  /** "Sign in" after an agent landed: hands over to its sign-in window. */
  onSignIn(provider: string): void;
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
  const [planned] = useState(() => steps || []);
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
          // The window stays open on its result ("✓ Claude Code installed —
          // Next: Sign in"); the checklist behind it re-probes meanwhile.
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
  const agent = installedAgent(planned);
  const status =
    exit === 0
      ? installedMessage(planned)
      : exit !== null
        ? "✗ some steps failed — scroll up to see which"
        : state === "streaming"
          ? installProgress(planned, secs)
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
          <span className={exit === 0 ? "signin-ok" : "muted"} id="install-status" role="status">
            {status}
          </span>
          <button type="button" id="install-close" onClick={() => void close()}>
            Close
          </button>
        </div>
        {exit === 0 && (
          <div className="install-next" id="install-next">
            {agent ? (
              <button type="button" className="setup-primary" id="install-signin" onClick={() => onSignIn(agent)}>
                Sign in to {agentName(agent)}
              </button>
            ) : (
              <button type="button" className="setup-primary" id="install-done" onClick={() => void close()}>
                Done
              </button>
            )}
          </div>
        )}
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
        <div className="prov-login-term login-term-dark" ref={hostRef} />
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
  tourNext = false,
}: {
  steps: InstallStep[] | undefined;
  onDone(): void;
  /** The button is the setup checklist's next action (`data-tour="setup-next"`). */
  tourNext?: boolean;
}) {
  const [open, setOpen] = useState(false);
  /** The agent whose sign-in window took over from the install window. */
  const [signIn, setSignIn] = useState("");
  const setWindowOpen = useInstallWindow((st) => st.set);
  useEffect(() => {
    setWindowOpen(open || !!signIn);
  }, [open, signIn, setWindowOpen]);
  useEffect(() => () => setWindowOpen(false), [setWindowOpen]);
  // Not while the window is open: it follows the run itself.
  const runningFor = useRunningInstall(!open && !!steps?.length, onDone);
  // The window outlives the button: a successful install empties `steps` (the
  // re-probe finds nothing missing), and that is exactly the moment the result
  // has to stay on screen.
  const window_ = (open || signIn) && (
    <>
      {open && (
        <InstallWindow
          steps={steps}
          onClose={() => setOpen(false)}
          onDone={onDone}
          onSignIn={(p) => {
            setOpen(false);
            setSignIn(p);
          }}
        />
      )}
      {signIn && (
        <SignInWindow
          provider={signIn}
          onClose={() => {
            setSignIn("");
            notifyConnectionsChanged();
            onDone();
          }}
        />
      )}
    </>
  );
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
      <button
        type="button"
        className={"test-btn" + (tourNext ? " setup-primary" : "")}
        id="doctor-install-btn"
        data-tour={tourNext ? "setup-next" : undefined}
        onClick={() => setOpen(true)}
      >
        {installButtonLabel(steps)}
      </button>
      {/* The button already names what it installs; the hint adds only what
          it doesn't say (the full list when the label had to give up, the
          password). */}
      {(stepNames(steps).length > 3 || asksForPassword(steps)) && (
        <span className="set-hint">
          {" "}
          {stepNames(steps).length > 3 ? installSummary(steps) : ""}
          {asksForPassword(steps) ? (stepNames(steps).length > 3 ? " — " : "") + "asks for your password once" : ""}
        </span>
      )}
      <details className="install-manual-fold">
        <summary>Or run it yourself</summary>
        <ManualFallback cmds={manualCommands(steps)} lead="Paste this into a terminal on this computer:" />
      </details>
      {window_}
    </div>
  );
}
