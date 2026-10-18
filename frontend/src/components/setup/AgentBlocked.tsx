/** A session whose agent can't do its task — not installed here, or not
 * signed in — says so on its rail row and its pane, with the one action that
 * helps, instead of "Idle", "Question" or "claude exited 1".
 *
 * The reading is `agentBlocked` (lib/sessionState, from the Setup payload's
 * per-agent `logins`); SidebarRow and Pane only place these two pieces.
 *
 *   not installed        chip "Install" → the Setup checklist (step ① installs it)
 *                        for the default agent; Settings → Agents on that
 *                        agent's card for any other (Setup checks only the
 *                        default)
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
import { openAgentSettings } from "../settings/screens/CodingCli";
import { refreshSetup, useSetup } from "./useSetup";

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

/** Where "Install" goes for agent `name`: Setup installs the default agent
 * (its step ①) and checks nothing else, so another agent opens Settings →
 * Agents on its own card. */
export function installTarget(name: string, defaultAgent: string): "setup" | "agents" {
  return !!name && name === defaultAgent ? "setup" : "agents";
}

function useOpenInstall(name: string): () => void {
  const defaultAgent = useSetup().data?.default_provider || "";
  return () => {
    if (installTarget(name, defaultAgent) === "setup") useUi.getState().openDialogFor("setup");
    else openAgentSettings(name);
  };
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
  const install = useOpenInstall(block.name);
  const act = blockedAction(block);
  const onClick = (e: MouseEvent) => {
    e.stopPropagation();
    if (block.why === "missing") install();
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
  const install = useOpenInstall(block.name);
  return (
    <div className="pane-noagent" role="alert" data-why={block.why}>
      <span>{blockedSentence(block)}</span>
      {block.why === "missing" ? (
        <button
          type="button"
          onClick={(e) => {
            e.stopPropagation();
            install();
          }}
        >
          Install {agentName(block.name)}…
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
