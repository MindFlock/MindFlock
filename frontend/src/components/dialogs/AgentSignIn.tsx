/** "Sign in to <agent>" — the agent CLI's own login flow in a real terminal.
 *
 * Sign-in was the last onboarding step that still sent people to a terminal:
 * Setup ② could only report "no sign of a login was found". The server already
 * hosts the CLI's login command in a throwaway terminal
 * (`/api/providers/<name>/login-terminal` — tmux, or a plain PTY on a host that
 * has no tmux yet); this opens it in the same window the installer uses. On
 * close the terminal is torn down (`login-close`) and `onDone` re-runs whatever
 * reported the missing login, so the ✗ clears without a manual re-check.
 *
 * The window's words describe what the terminal really shows. Claude Code's
 * first run asks for a text style and a login method BEFORE any link appears,
 * so "copy the link below" pointed at nothing; the steps are spelled out per
 * agent (`signInSteps`), and the window watches the login probe so it can
 * say "✓ Signed in" the moment it lands (`/api/setup` → `logins`). */

import { useEffect, useRef, useState } from "react";
import { Modal } from "../Modal";
import { api } from "../../api/client";
import { useWsTerm } from "../../lib/wsTerm";
import { notifyConnectionsChanged } from "../settings/screens/Connections";
import { agentName } from "../../lib/agentNames";
import "./ConnectGitHub.css";

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

/** "claude" → "Claude Code": the product name, for the button and title. */
export function agentDisplayName(name: string): string {
  return name ? agentName(name) : name;
}

/** What the person will actually see in the terminal, in order. Claude Code
 * asks two first-run questions before it prints a link; the other agents go
 * straight to their own login prompts. */
export function signInSteps(provider: string): string[] {
  if (provider === "claude")
    return [
      "Pick a text style (any is fine) and press Enter.",
      "Choose how to log in — usually your Claude account.",
      "Open the link it shows, sign in, and paste the code back here if asked.",
    ];
  return [
    "Answer " + agentDisplayName(provider) + "'s prompts in the terminal below.",
    "If it shows a link, open it in your browser and sign in.",
  ];
}

/** How often the window re-checks the login while it is open. */
export const SIGNIN_POLL_MS = 3000;

export function SignInWindow({
  provider,
  profile,
  onClose,
}: {
  provider: string;
  /** An auth profile id: the login runs in that account's own config dir
   * (`?profile=`), so a second account signs in without touching the first. */
  profile?: string;
  onClose(): void;
}) {
  const hostRef = useRef<HTMLDivElement>(null);
  const query = profile ? "?profile=" + encodeURIComponent(profile) : "";
  const path = "/api/providers/" + encodeURIComponent(provider) + "/login-terminal" + query;
  const state = useWsTerm(hostRef, path, true);
  const closeRef = useRef(onClose);
  closeRef.current = onClose;
  // Watch the login probe (the ambient login only: an account profile's
  // login lives in its own dir, which this probe doesn't read).
  const [signedIn, setSignedIn] = useState(false);
  useEffect(() => {
    if (profile) return;
    let live = true;
    const t = window.setInterval(async () => {
      try {
        const d = await api<{ logins?: Array<{ name: string; logged_in: boolean | null }> }>("/api/setup");
        if (!live) return;
        if ((d?.logins || []).some((l) => l.name === provider && l.logged_in === true)) {
          setSignedIn(true);
          window.clearInterval(t);
        }
      } catch {
        /* the conn banner owns an unreachable server */
      }
    }, SIGNIN_POLL_MS);
    return () => {
      live = false;
      window.clearInterval(t);
    };
  }, [provider, profile]);

  const close = async () => {
    try {
      await api("/api/providers/" + encodeURIComponent(provider) + "/login-close" + query, {
        method: "POST",
      });
    } catch {
      /* closing the window is what the user asked for either way */
    }
    closeRef.current();
  };

  const status = signedIn ? "✓ Signed in. You can close this window." : state === "streaming" ? "" : state;

  // Escape is caught in the capture phase: it must close this window before
  // the terminal inside it (or the dialog under it) sees the key.
  return (
    <Modal
      id="signin-dialog"
      role="dialog"
      aria-modal="true"
      portal
      escapeCapture
      onClose={() => void close()}
    >
      <div className="prov-login-panel">
        <div className="ws-head">
          <h2>
            Sign in to {agentDisplayName(provider)}
            {profile ? " (" + profile + ")" : ""}
          </h2>
          <span className={signedIn ? "signin-ok" : "muted"} id="signin-status" role="status">
            {status}
          </span>
          <button type="button" id="signin-close" className={signedIn ? "signin-done" : ""} onClick={() => void close()}>
            {signedIn ? "Done" : "Close"}
          </button>
        </div>
        {!signedIn && (
          <ol className="signin-steps muted" id="signin-steps">
            {signInSteps(provider).map((t) => (
              <li key={t}>{t}</li>
            ))}
          </ol>
        )}
        <div className="prov-login-term login-term-dark" ref={hostRef} />
      </div>
    </Modal>
  );
}

/** The button + its window. `onDone` runs after the window closes. `profile`
 * signs an auth profile's own account in (Settings → Accounts); `label`
 * replaces the "Sign in to <Agent>" text (a row that already names the agent
 * just says "Sign in"). */
export function AgentSignIn({
  provider,
  profile,
  label,
  onDone,
  className,
  dataTour,
}: {
  provider: string;
  profile?: string;
  label?: string;
  onDone(): void;
  className?: string;
  /** A tour hook (`data-tour`), e.g. "setup-next" on the checklist's step ②. */
  dataTour?: string;
}) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button
        type="button"
        className={"test-btn agent-signin-btn" + (className ? " " + className : "")}
        data-tour={dataTour}
        onClick={(e) => {
          e.stopPropagation();
          setOpen(true);
        }}
      >
        {label || "Sign in to " + agentDisplayName(provider)}
      </button>
      {open && (
        <SignInWindow
          provider={provider}
          profile={profile}
          onClose={() => {
            setOpen(false);
            notifyConnectionsChanged();
            onDone();
          }}
        />
      )}
    </>
  );
}
