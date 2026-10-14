/** A session whose agent can't do its task — not installed here, or not
 * signed in — says so on its rail row and its pane, with the one action that
 * helps, instead of "Idle", "Question" or "claude exited 1".
 *
 * The reading is `agentBlocked` (lib/sessionState, from the Setup payload's
 * per-agent `logins`); SidebarRow and Pane only place these two pieces.
 *
 *   not installed        chip "Install" → the Setup checklist (step ① installs it)
 *   not signed in, live  chip "Sign in" → focuses the session: its own terminal
 *                        is already showing the agent's sign-in questions
 *   not signed in, gone  chip "Sign in" → the sign-in window, then Restart
 *
 * While a "not signed in" banner is up it re-reads the Setup payload, so the
 * row and pane clear by themselves once the sign-in lands. */

import { useEffect, useState, type MouseEvent } from "react";
import { useUi } from "../../state/store";
import { agentName } from "../../lib/agentNames";
import { reviveSession } from "../../lib/revive";
import { selectSession } from "../../lib/sessionActions";
import type { AgentBlock } from "../../lib/sessionState";
import { SignInWindow } from "../dialogs/AgentSignIn";
import { notifyConnectionsChanged } from "../settings/screens/Connections";
import { refreshSetup } from "./useSetup";

/** How often a signed-out session's banner re-checks the login. */
export const BLOCKED_POLL_MS = 8000;

/** The pane banner's sentence for a block. */
export function blockedSentence(b: AgentBlock): string {
  const who = agentName(b.name);
  if (b.why === "missing") return who + " isn't installed on this computer, so this session's agent couldn't start.";
  if (b.exited) return who + " isn't signed in, so this session's agent stopped. Sign in, and it restarts.";
  if (b.name === "claude")
    return (
      who +
      " isn't signed in yet. Answer its questions in the terminal below — a text style, then how to log in — and open the link it shows."
    );
  return who + " isn't signed in yet. Answer its sign-in prompts in the terminal below.";
}

/** The chip's action word. */
export function blockedAction(b: AgentBlock): string {
  return b.why === "missing" ? "Install" : "Sign in";
}

function useBlockedPoll(b: AgentBlock | null) {
  const signedOut = b?.why === "signed_out";
  useEffect(() => {
    if (!signedOut) return;
    // No re-probe: the login check is part of every /api/setup read.
    const t = window.setInterval(() => void refreshSetup(), BLOCKED_POLL_MS);
    return () => window.clearInterval(t);
  }, [signedOut]);
}

/** Sign in, then relaunch the session's agent (it quit at launch). */
function useSignInThenRestart(title: string, b: AgentBlock) {
  const [open, setOpen] = useState(false);
  const window_ = open ? (
    <SignInWindow
      provider={b.name}
      onClose={() => {
        setOpen(false);
        notifyConnectionsChanged();
        void refreshSetup(true).then(() => reviveSession(title));
      }}
    />
  ) : null;
  return { start: () => setOpen(true), window_ };
}

/** The rail row's chip: `className` is the row's stage-chip classes. */
export function AgentBlockedChip({
  block,
  title,
  className,
  hint,
}: {
  block: AgentBlock;
  title: string;
  className: string;
  hint?: string;
}) {
  const signIn = useSignInThenRestart(title, block);
  const act = blockedAction(block);
  const onClick = (e: MouseEvent) => {
    e.stopPropagation();
    if (block.why === "missing") useUi.getState().openDialogFor("setup");
    else if (block.exited) signIn.start();
    else selectSession(title);
  };
  return (
    <>
      <button
        type="button"
        className={className + " chip-noagent" + (block.why === "signed_out" ? " chip-signin" : "")}
        title={hint}
        aria-label={(block.why === "missing" ? "Not installed" : "Not signed in") + " — " + act.toLowerCase()}
        onClick={onClick}
        onDoubleClick={(e) => e.stopPropagation()}
      >
        <span className="chip-act" aria-hidden="true">
          {act}
        </span>
      </button>
      {signIn.window_}
    </>
  );
}

/** The pane's banner, above the terminal. */
export function AgentBlockedBanner({ block, title }: { block: AgentBlock | null; title: string }) {
  useBlockedPoll(block);
  if (!block) return null;
  return <Banner block={block} title={title} />;
}

function Banner({ block, title }: { block: AgentBlock; title: string }) {
  const signIn = useSignInThenRestart(title, block);
  return (
    <div className="pane-noagent" role="alert" data-why={block.why}>
      <span>{blockedSentence(block)}</span>
      {block.why === "missing" ? (
        <button
          type="button"
          onClick={(e) => {
            e.stopPropagation();
            useUi.getState().openDialogFor("setup");
          }}
        >
          Install {block.name}…
        </button>
      ) : block.exited ? (
        <button
          type="button"
          onClick={(e) => {
            e.stopPropagation();
            signIn.start();
          }}
        >
          Sign in…
        </button>
      ) : null}
      {signIn.window_}
    </div>
  );
}
