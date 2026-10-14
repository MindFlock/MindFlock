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
 *     ② Sign in to your agent    one sign-in button per INSTALLED agent CLI
 *     ③ Start your first agent   opens New session (with the folder browser)
 *   Connect your work      optional, counted in the chip's N/5
 *     ④ Connect GitHub           the Connect GitHub dialog · Skip
 *     ⑤ Connect your tracker     Intake → Tickets (add a source) · Skip
 *   Optional, later        folded; not counted, never "next"
 *     Use it from your phone     Install Tailscale, or the QR
 *
 * One obvious next action: the server's `summary.next` step is highlighted and
 * its button is the only accent-coloured one. A done step shrinks to its title.
 *
 * Nothing here marks a step done: the server does, from the thing itself (the
 * doctor run, the first session, a GitHub credential, the first ticket source,
 * a QR fetch). The only writes are the Skips. The first-run report `mindflock
 * serve` printed at boot is folded at the bottom — the desktop app's server
 * writes it to a log nobody reads. */

import { useCallback, useEffect, useState, type ReactNode } from "react";
import { api } from "../../api/client";
import { useUi } from "../../state/store";
import { toast } from "../../lib/toast";
import { AgentSignIn, agentDisplayName } from "../dialogs/AgentSignIn";
import { CONNECTIONS_CHANGED_EVENT } from "../settings/screens/Connections";
import { DoctorList } from "./DoctorList";
import { openConnectGitHub } from "../../lib/githubConnect";
import { refreshSetup, skipSetupStep, useSetup } from "./useSetup";
import {
  STATUS_GLYPH,
  installedLogins,
  loginLine,
  nextStepId,
  setupCount,
  orderedSteps,
  stepNumber,
  tailscaleMissing,
} from "./setupModel";
import type { SetupStatus, SetupStep } from "../../api/types";
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
 * step may still show, folded. */
function Step({
  step,
  next,
  children,
  fold,
}: {
  step: SetupStep;
  next: string;
  children: ReactNode;
  fold?: ReactNode;
}) {
  const isNext = step.id === next;
  return (
    <section
      className={"setup-step st-" + step.status + (isNext ? " is-next" : "")}
      data-step={step.id}
      data-status={step.status}
    >
      <StepHead step={step} />
      {step.status === "todo" && <div className="setup-step-body">{children}</div>}
      {step.status === "skipped" && (
        <div className="setup-actions">
          <SkipRow step={step} />
        </div>
      )}
      {step.status === "done" && fold}
    </section>
  );
}

/** A primary button: accent-coloured only on the step to do next, so there is
 * one obvious thing to press. */
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

export function SetupChecklist() {
  const { data: status, error } = useSetup();
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
  const logins = installedLogins(status?.logins);
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

        {/* ① Install the tools */}
        <Step
          step={tools}
          next={next}
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
            <DoctorList essential reprobeKey={reprobeKey} onChange={() => void refreshSetup()} />
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
          ) : logins.length === 0 ? (
            <p className="muted setup-hint">
              Install the tools first — that installs {status.default_provider || "your agent"}.
            </p>
          ) : (
            <ul className="setup-logins">
              {logins.map((l) => {
                const line = loginLine(l);
                return (
                  <li
                    key={l.name}
                    className={"setup-login st-" + (line.ok === true ? "ok" : line.ok === false ? "todo" : "unknown")}
                    data-provider={l.name}
                    data-logged-in={String(l.logged_in)}
                  >
                    <span className="setup-login-name">
                      {agentDisplayName(l.name)}
                      {l.default && <span className="muted"> (default)</span>}
                    </span>
                    <span className="setup-login-state">
                      {(line.ok === true ? "✓ " : line.ok === false ? "• " : "") + line.text}
                    </span>
                    {l.login_cmd && line.ok !== true && (
                      <AgentSignIn provider={l.name} onDone={() => void refreshSetup(true)} />
                    )}
                  </li>
                );
              })}
            </ul>
          )}
          <p className="muted setup-hint">
            Using an API key?{" "}
            <button
              type="button"
              className="linklike setup-open-accounts"
              onClick={(e) => {
                e.stopPropagation();
                go("settings", "accounts");
              }}
            >
              Settings → Accounts
            </button>
          </p>
        </Step>

        {/* ③ Start your first agent */}
        <Step step={repo} next={next}>
          <p className="muted setup-hint">Pick a repo folder and say what to do.</p>
          <div className="setup-actions">
            <Action next={repo.id === next} className="setup-new" onClick={() => go("new-session")}>
              + New session
            </Action>
          </div>
        </Step>
      </div>

      <div className="setup-group" data-group="connect">
        <h4 className="setup-group-title">
          Connect your work <span className="setup-group-note">optional</span>
        </h4>

        {/* ④ Connect GitHub */}
        <Step step={github} next={next}>
          <p className="muted setup-hint">Lets agents open and merge pull requests.</p>
          <div className="setup-actions">
            <Action
              next={github.id === next}
              className="setup-connect-github"
              title={
                "Sign in with GitHub, use gh, or paste a token. That token is the whole setup for " +
                "opening and merging PRs — pushing is plain git push over your own remote."
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

      {/* Optional, later — never part of getting started, so folded. */}
      <details className="setup-later" data-group="later" onClick={(e) => e.stopPropagation()}>
        <summary>Optional, later</summary>
        <section className={"setup-step st-" + phone.status} data-step="phone" data-status={phone.status}>
          <StepHead step={phone} />
          <PhoneStep status={status} done={phone.done} />
          <div className="setup-actions">
            <button
              type="button"
              className="linklike setup-open-mobile"
              onClick={(e) => {
                e.stopPropagation();
                go("settings", "mobile");
              }}
            >
              Settings → Your phone
            </button>
          </div>
        </section>
        <p className="muted setup-hint setup-later-more">
          Other computers, sharing with people, extensions and IDEs are all in{" "}
          <button
            type="button"
            className="linklike setup-open-settings"
            onClick={(e) => {
              e.stopPropagation();
              go("settings");
            }}
          >
            Settings
          </button>
          .
        </p>
      </details>

      {status?.report && (
        <details className="setup-report" onClick={(e) => e.stopPropagation()}>
          <summary>What the server found at first start</summary>
          <pre>{status.report}</pre>
        </details>
      )}
    </div>
  );
}

/** The phone extra's body: "Install Tailscale" with the doctor's hint when it
 * is missing, else a Show QR button (the fetch is deliberate — the server
 * marks the step done when the owner fetches the QR). */
function PhoneStep({ status, done }: { status: SetupStatus | null | undefined; done: boolean }) {
  const [qr, setQr] = useState<{ svg?: string; urls?: Array<{ label?: string; url?: string }> } | null>(null);
  const [busy, setBusy] = useState(false);
  const showQr = useCallback(async () => {
    setBusy(true);
    try {
      const d = await api<{ qr_svg?: string; urls?: Array<{ label?: string; url?: string }> }>("/api/mobile?setup=1");
      setQr({ svg: d.qr_svg || "", urls: d.urls || [] });
      void refreshSetup();
    } catch (e) {
      toast((e as Error).message);
    } finally {
      setBusy(false);
    }
  }, []);

  if (status && tailscaleMissing(status)) {
    const ts = status.tailscale!;
    return (
      <div className="setup-phone setup-phone-tailscale" data-phone="install-tailscale">
        <p className="muted setup-hint">Your phone reaches MindFlock over Tailscale, which isn't on this computer yet.</p>
        {ts.fix && (
          <p className="setup-hint">
            Install Tailscale: <code>{ts.fix}</code>
          </p>
        )}
        {ts.docs && (
          <p className="setup-hint">
            <a href={ts.docs} target="_blank" rel="noopener noreferrer">
              {ts.docs}
            </a>
          </p>
        )}
      </div>
    );
  }
  return (
    <div className="setup-phone" data-phone="qr">
      <p className="muted setup-hint">
        {done
          ? "Your phone has been here. Scan again any time from Settings → Your phone."
          : "Install Tailscale on your phone, sign in to the same account, then scan this QR."}
      </p>
      {qr?.svg ? (
        <div className="setup-qr" dangerouslySetInnerHTML={{ __html: qr.svg }} />
      ) : (
        <div className="setup-actions">
          <button
            type="button"
            className="setup-show-qr"
            disabled={busy}
            onClick={(e) => {
              e.stopPropagation();
              void showQr();
            }}
          >
            {busy ? "Loading…" : "Show QR"}
          </button>
        </div>
      )}
      {qr && !qr.svg && (
        <p className="muted setup-hint">No QR yet — turn on Make reachable in Settings → Your devices first.</p>
      )}
    </div>
  );
}
