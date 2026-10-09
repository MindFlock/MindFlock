/** "Sign in to <agent>" — the agent CLI's own login flow in a real terminal.
 *
 * Sign-in was the last onboarding step that still sent people to a terminal:
 * Setup ② could only report "no sign of a login was found". The server already
 * hosts the CLI's login command in a throwaway terminal
 * (`/api/providers/<name>/login-terminal` — tmux, or a plain PTY on a host that
 * has no tmux yet); this opens it in the same window the installer uses. On
 * close the terminal is torn down (`login-close`) and `onDone` re-runs whatever
 * reported the missing login, so the ✗ clears without a manual re-check. */

import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { api } from "../../api/client";
import { useWsTerm } from "../../lib/wsTerm";

/** The doctor row shape this needs (a subset of DoctorCheckItem). */
export interface SignInCheck {
  id?: string;
  status?: string;
  cmd?: string;
  provider?: string;
}

/** Which provider a check offers a sign-in for, or `null`.
 *
 * Only an `agent-auth` row that found no login (`warn`) AND carries a runnable
 * `cmd` — the doctor sets one only for a login flow the provider DECLARED, so a
 * CLI with no login command (aider reads API keys) never gets a button that
 * would just drop the user into its REPL. */
export function signInTarget(c: SignInCheck | null | undefined): string | null {
  if (!c || c.id !== "agent-auth" || c.status !== "warn") return null;
  if (!c.cmd || !c.provider) return null;
  return c.provider;
}

/** "claude" → "Claude" for the button label. */
export function agentDisplayName(name: string): string {
  return name ? name.charAt(0).toUpperCase() + name.slice(1) : name;
}

function SignInWindow({ provider, onClose }: { provider: string; onClose(): void }) {
  const hostRef = useRef<HTMLDivElement>(null);
  const path = "/api/providers/" + encodeURIComponent(provider) + "/login-terminal";
  const state = useWsTerm(hostRef, path, true);
  const closeRef = useRef(onClose);
  closeRef.current = onClose;

  const close = async () => {
    try {
      await api("/api/providers/" + encodeURIComponent(provider) + "/login-close", {
        method: "POST",
      });
    } catch {
      /* closing the window is what the user asked for either way */
    }
    closeRef.current();
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
    state === "streaming"
      ? "sign in when it asks (a browser window may open) — then close this window"
      : state;

  return createPortal(
    <div
      className="modal"
      id="signin-dialog"
      role="dialog"
      aria-modal="true"
      onClick={(e) => {
        if (e.target === e.currentTarget) void close();
      }}
    >
      <div className="prov-login-panel">
        <div className="ws-head">
          <h2>Sign in to {agentDisplayName(provider)}</h2>
          <span className="muted" id="signin-status">
            {status}
          </span>
          <button type="button" id="signin-close" onClick={() => void close()}>
            Close
          </button>
        </div>
        <div className="prov-login-term" ref={hostRef} />
      </div>
    </div>,
    document.body
  );
}

/** The button + its window. `onDone` runs after the window closes. */
export function AgentSignIn({
  provider,
  onDone,
  className,
}: {
  provider: string;
  onDone(): void;
  className?: string;
}) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button
        type="button"
        className={"test-btn agent-signin-btn" + (className ? " " + className : "")}
        onClick={(e) => {
          e.stopPropagation();
          setOpen(true);
        }}
      >
        Sign in to {agentDisplayName(provider)}
      </button>
      {open && (
        <SignInWindow
          provider={provider}
          onClose={() => {
            setOpen(false);
            onDone();
          }}
        />
      )}
    </>
  );
}
