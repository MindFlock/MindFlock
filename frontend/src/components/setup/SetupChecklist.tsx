/** The first-run checklist — ONE component for every place setup shows:
 * the empty grid's card, the Setup modal (opened from the "Setup 3/5" chip,
 * the sidebar's doctor chip, the palette, or `?setup=1`).
 *
 * The steps, their names, order and groups are the server's (`GET /api/setup`,
 * built by backend/web/core/setup_steps.py — the same list `mindflock init`
 * prints). Three groups, so a new user sees the few things that matter first:
 *
 *   Get started            the three REQUIRED steps to a running agent
 *     ① Install the tools        "Install …" + only what is missing
 *     ② Sign in to your agent    the default agent's card (AgentCard — the same
 *                                card Settings shows: Install → Sign in → Test)
 *     ③ Start your first agent   opens New session; ticks only once an agent
 *                                has actually started on its task
 *   Connect your work      optional, counted in the chip's N/5
 *     ④ Connect GitHub           the Connect GitHub dialog · Skip
 *     ⑤ Connect your tracker     Intake → Tickets (add a source) · Skip
 *   Optional, later        folded; not counted, never "next"
 *     Use it from your phone     one line: set up in Settings → Your phone
 *
 * One obvious next action: the server's `summary.next` step is highlighted and
 * its button is the only accent-coloured one. A done step shrinks to its title.
 * Once a session exists, ③'s primary is "Show my session →" (the session is
 * the thing to watch), with "or start another" as a quiet link.
 *
 * First hour only: step ① lists only what blocks ①–③ (phone, Tailscale, MTU
 * and sudo fixes live in Settings → Your phone / Doctor), and the CLI report
 * `mindflock serve` printed at boot is in Settings → System logs, not here.
 *
 * Tour hooks (stable, pinned by tests): `data-tour="setup-step-<id>"` on each
 * counted step's root, `data-tour="setup-next"` on the next step's primary.
 *
 * Nothing here marks a step done: the server does, from the thing itself (the
 * doctor run, the first session, a GitHub credential, the first ticket source,
 * a QR fetch). The only writes are the Skips. */

import { useEffect, useState, type ReactNode } from "react";
import { useUi } from "../../state/store";
import { useInstances } from "../../state/queries";
import { selectSession } from "../../lib/sessionActions";
import { toast } from "../../lib/toast";
import { agentName } from "../../lib/agentNames";
import { CONNECTIONS_CHANGED_EVENT } from "../settings/screens/Connections";
import { AgentCard } from "./AgentCard";
import { requestApiKeyForm } from "./accountsIntent";
import { DoctorList } from "./DoctorList";
import { openConnectGitHub } from "../../lib/githubConnect";
import { useInstallWindow } from "../dialogs/InstallTerminal";
import { refreshSetup, skipSetupStep, useSetup } from "./useSetup";
import { STATUS_GLYPH, nextStepId, setupCount, orderedSteps, stepNumber } from "./setupModel";
import type { Instance, SetupStatus, SetupStep } from "../../api/types";
import "./setup.css";

/** The checklist's first line, in the chip's own count (setupCount): the
 * three required steps ("1 of 3 done · next: Sign in to your agent"), then
 * what optional is left ("3 of 3 done · 2 optional left · next: Connect
 * GitHub"). */
export function progressLine(status: SetupStatus | null | undefined): string {
  if (!status) return "Checking…";
  const s = status.summary;
  const next = status.steps.find((x) => x.id === s.next);
  if (s.complete) return "All set";
  const tail = next ? " · next: " + next.title : "";
  const c = setupCount(status);
  return c.done + " of " + c.total + " done" + (c.optional ? " · " + c.optional + " optional left" : "") + tail;
}

function StepHead({ step }: { step: SetupStep }) {
  const note = step.status === "done" ? "done" : step.status === "skipped" ? "skipped" : "";
  const num = stepNumber(step.id);
  return (
    <h3>
      {num && <span className="setup-num">{num}</span>} {step.title}
      {note && (
        <span className={"setup-state st-" + step.status} data-step-status={step.status}>
          {STATUS_GLYPH[step.status] + " " + note}
        </span>
      )}
    </h3>
  );
}

/** Skip / Show again for an optional step. */
function SkipRow({ step }: { step: SetupStep }) {
  const [busy, setBusy] = useState(false);
  if (step.done) return null;
  const run = async (undo: boolean) => {
    setBusy(true);
    try {
      await skipSetupStep(step.id, undo);
    } catch (e) {
      toast((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  return (
    <button
      type="button"
      className={"linklike setup-skip" + (step.skipped ? " setup-unskip" : "")}
      disabled={busy}
      onClick={(e) => {
        e.stopPropagation();
        void run(step.skipped);
      }}
    >
      {step.skipped ? "Show again" : "Skip"}
    </button>
  );
}

/** One step: its head always; its body only while it is to do (a done step
 * is one line; a skipped one keeps its "Show again"). `fold` is what a DONE
 * step may still show, folded. `keepBody` holds the body on screen after the
 * step ticks done — a window opened from it (the installer) lives there. */
function Step({
  step,
  next,
  children,
  fold,
  keepBody = false,
}: {
  step: SetupStep;
  next: string;
  children: ReactNode;
  fold?: ReactNode;
  keepBody?: boolean;
}) {
  const isNext = step.id === next;
  const body = step.status === "todo" || (keepBody && step.status === "done");
  return (
    <section
      className={"setup-step st-" + step.status + (isNext ? " is-next" : "")}
      data-step={step.id}
      data-status={step.status}
      data-tour={"setup-step-" + step.id}
    >
      <StepHead step={step} />
      {body && <div className="setup-step-body">{children}</div>}
      {step.status === "skipped" && (
        <div className="setup-actions">
          <SkipRow step={step} />
        </div>
      )}
      {step.status === "done" && !body && fold}
    </section>
  );
}

/** A primary button: accent-coloured only on the step to do next, so there is
 * one obvious thing to press — and that one carries the tour's
 * `data-tour="setup-next"`. */
function Action({
  next,
  className,
  onClick,
  children,
  title,
}: {
  next: boolean;
  className: string;
  onClick(): void;
  children: ReactNode;
  title?: string;
}) {
  return (
    <button
      type="button"
      className={className + (next ? " setup-primary" : "")}
      data-tour={next ? "setup-next" : undefined}
      title={title}
      onClick={(e) => {
        e.stopPropagation();
        onClick();
      }}
    >
      {children}
    </button>
  );
}

/** The session "Show my session" brings forward: the focused one when it is
 * a live session, else the first one (a first hour has one). */
export function sessionToShow(instances: readonly Pick<Instance, "title">[] | undefined, focused: string | null): string {
  const list = instances || [];
  if (focused && list.some((i) => i.title === focused)) return focused;
  return list[0]?.title || "";
}

/** GitHub's row under ④: whether to skip it, said where the decision is. */
export const GITHUB_STEP_HINT = "Optional — pushing works without it; needed to open and merge pull requests.";

export function SetupChecklist() {
  const { data: status, error } = useSetup();
  const { data: instances } = useInstances();
  const focused = useUi((st) => st.focused);
  const installOpen = useInstallWindow((st) => st.open);
  const [reprobeKey, setReprobeKey] = useState(0);
  const steps = orderedSteps(status);
  const by = new Map(steps.map((s) => [s.id, s]));
  const step = (id: string) => by.get(id) as SetupStep;
  const next = nextStepId(status);

  // Connecting GitHub (its own dialog) or signing an agent in announces
  // itself; re-read the checklist so the step ticks without a reload.
  useEffect(() => {
    const on = () => void refreshSetup();
    window.addEventListener(CONNECTIONS_CHANGED_EVENT, on);
    return () => window.removeEventListener(CONNECTIONS_CHANGED_EVENT, on);
  }, []);

  // Close the modal before jumping to another surface — the checklist itself
  // is the same component inside the modal and on the grid, and a dialog
  // opened over a dialog is the one thing the dialog store can't show.
  const closeSetup = () => {
    if (useUi.getState().openDialog === "setup") useUi.getState().closeDialog();
  };
  const go = (name: "new-session" | "settings" | "intake", target?: string) => {
    closeSetup();
    useUi.getState().openDialogFor(name, target);
  };

  const tools = step("tools");
  const agent = step("agent_login");
  const repo = step("repo");
  const github = step("github");
  const tracker = step("tracker");
  const phone = step("phone");
  const shown = sessionToShow(instances, focused);
  const showSession = () => {
    closeSetup();
    if (shown) selectSession(shown);
  };
  const defaultAgent = status?.default_provider || "";
  const defaultInstalled = !!(status?.logins || []).find((l) => l.name === defaultAgent && l.installed);
  // The first session exists but its agent hasn't started on anything yet.
  const repoStarted = !!(repo as SetupStep & { started?: boolean }).started;
  const startDone = [tools, agent, repo].every((s) => s.done);

  return (
    <div className="setup-checklist" data-setup-next={status?.summary.next || ""}>
      {error ? (
        <p className="error">setup status failed: {(error as Error).message}</p>
      ) : (
        <p className="muted setup-progress">{progressLine(status)}</p>
      )}

      <div className="setup-group" data-group="start">
        <h4 className="setup-group-title">
          Get started
          {startDone && <span className="setup-group-note st-done"> ✓ done</span>}
        </h4>

        {/* ① Install Claude Code (the server names the default agent) */}
        <Step
          step={tools}
          next={next}
          keepBody={installOpen}
          fold={
            <details className="setup-fold" onClick={(e) => e.stopPropagation()}>
              <summary>Show checks</summary>
              <div className="setup-doctor">
                <DoctorList essential reprobeKey={reprobeKey} onChange={() => void refreshSetup()} />
              </div>
            </details>
          }
        >
          <div className="setup-doctor">
            <DoctorList
              essential
              fallback={status?.doctor}
              tourNext={tools.id === next}
              reprobeKey={reprobeKey}
              onChange={() => void refreshSetup()}
            />
          </div>
          <div className="setup-actions">
            <button
              type="button"
              className="linklike setup-recheck"
              onClick={(e) => {
                e.stopPropagation();
                setReprobeKey((k) => k + 1);
              }}
            >
              Re-check
            </button>
          </div>
        </Step>

        {/* ② Sign in to your agent */}
        <Step step={agent} next={next}>
          {!status ? (
            <p className="muted setup-hint">Checking…</p>
          ) : !defaultInstalled ? (
            <p className="muted setup-hint">
              Install {agentName(defaultAgent || "claude")} first (①).
            </p>
          ) : (
            <>
              <AgentCard
                provider={defaultAgent}
                gotoScreen={(n) => go("settings", n)}
                onChanged={() => void refreshSetup(true)}
                tourNext={agent.id === next}
              />
              <p className="muted setup-hint setup-agent-change">
                Using {agentName(defaultAgent)} ·{" "}
                <button
                  type="button"
                  className="linklike setup-change-agent"
                  onClick={(e) => {
                    e.stopPropagation();
                    go("settings", "coding");
                  }}
                >
                  change
                </button>
              </p>
            </>
          )}
          <p className="muted setup-hint">
            Using an API key instead?{" "}
            <button
              type="button"
              className="linklike setup-open-accounts"
              onClick={(e) => {
                e.stopPropagation();
                // Land on Accounts' API-key form, not its "CLI account" default.
                requestApiKeyForm(defaultAgent);
                go("settings", "accounts");
              }}
            >
              Add an API key
            </button>
          </p>
        </Step>

        {/* ③ Start your first agent. Once a session exists the thing to do
            is watch it, so its primary is "Show my session" and a second
            session is a quiet link. */}
        <Step step={repo} next={next}>
          {repoStarted || shown ? (
            <>
              <p className="muted setup-hint setup-repo-started">
                {agent.done
                  ? "Your first session is open. This ticks once its agent starts on your task."
                  : "Your first session is open, but its agent can't start until you sign in (②)."}
              </p>
              <div className="setup-actions">
                {shown && (
                  <Action next={repo.id === next} className="setup-show-session" onClick={showSession}>
                    Show my session →
                  </Action>
                )}
                <button
                  type="button"
                  className="linklike setup-new setup-new-another"
                  onClick={(e) => {
                    e.stopPropagation();
                    go("new-session");
                  }}
                >
                  {shown ? "or start another" : "+ New session"}
                </button>
              </div>
            </>
          ) : (
            <>
              <p className="muted setup-hint">Pick a repo folder and say what to do.</p>
              <div className="setup-actions">
                <Action next={repo.id === next} className="setup-new" onClick={() => go("new-session")}>
                  + New session
                </Action>
              </div>
            </>
          )}
        </Step>
      </div>

      <div className="setup-group" data-group="connect">
        <h4 className="setup-group-title">
          Connect your work <span className="setup-group-note">optional</span>
        </h4>

        {/* ④ Connect GitHub */}
        <Step step={github} next={next}>
          <p className="muted setup-hint setup-github-hint">{GITHUB_STEP_HINT}</p>
          <div className="setup-actions">
            <Action
              next={github.id === next}
              className="setup-connect-github"
              title={
                "Paste a GitHub token (or sign in with the GitHub CLI if it's installed). That token is " +
                "all MindFlock needs to open and merge PRs — pushing is plain git push over your own remote."
              }
              onClick={() => openConnectGitHub()}
            >
              Connect GitHub
            </Action>
            <SkipRow step={github} />
          </div>
        </Step>

        {/* ⑤ Connect your tracker */}
        <Step step={tracker} next={next}>
          <p className="muted setup-hint">
            Turn tickets from Jira, Linear, Shortcut, Asana or GitHub Issues into sessions.
          </p>
          <div className="setup-actions">
            <Action
              next={tracker.id === next}
              className="setup-open-intake"
              onClick={() => go("intake", "tickets")}
            >
              Add a ticket source
            </Action>
            <SkipRow step={tracker} />
          </div>
        </Step>
      </div>

      {/* Optional, later — never part of getting started, so folded, and one
          line: everything about the phone (Tailscale, its fixes, the QR) is
          Settings → Your phone's. */}
      <details className="setup-later" data-group="later" onClick={(e) => e.stopPropagation()}>
        <summary>Optional, later</summary>
        <p
          className={"setup-later-row st-" + phone.status}
          data-step="phone"
          data-status={phone.status}
        >
          <span className="setup-later-title">
            {phone.title}
            {phone.done && <span className="setup-state st-done"> {STATUS_GLYPH.done} done</span>}
          </span>
          {" — "}
          <button
            type="button"
            className="linklike setup-open-mobile"
            onClick={(e) => {
              e.stopPropagation();
              go("settings", "mobile");
            }}
          >
            Set up in Settings → Your phone
          </button>
        </p>
      </details>
    </div>
  );
}
