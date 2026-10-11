/** The first-run checklist — ONE component for every place setup shows:
 * the empty grid's card, the Setup modal (opened from the "Setup 3/5" chip,
 * the sidebar's doctor chip, the palette, or `?setup=1`).
 *
 * Five steps, named and ordered by the server (`GET /api/setup`, built by
 * backend/web/core/setup_steps.py — the same list `mindflock init` prints):
 *
 *   ① Engine and tools         the doctor list + "Install everything missing"
 *   ② Sign in to your agent    one sign-in button per INSTALLED agent CLI
 *   ③ Pick a repo              opens New session (with the folder browser)
 *   ④ Connect a tracker and GitHub   Settings → Connections · Skip
 *   ⑤ Your phone               Install Tailscale, or the QR · Skip
 *
 * Nothing here marks a step done: the server does, from the thing itself (the
 * doctor run, the first session, the first ticket source, a QR fetch). The
 * only writes are the two Skips. The first-run report `mindflock serve`
 * printed at boot is shown at the bottom — the desktop app's server writes it
 * to a log nobody reads. */

import { useCallback, useState } from "react";
import { api } from "../../api/client";
import { useUi } from "../../state/store";
import { toast } from "../../lib/toast";
import { AgentSignIn, agentDisplayName } from "../dialogs/AgentSignIn";
import { DoctorList } from "./DoctorList";
import { TestResult, idleTest, type TestState } from "./githubTest";
import { openConnectGitHub } from "../../lib/githubConnect";
import { refreshSetup, skipSetupStep, useSetup } from "./useSetup";
import {
  STATUS_GLYPH,
  installedLogins,
  loginLine,
  orderedSteps,
  stepNumber,
  tailscaleMissing,
} from "./setupModel";
import type { SetupStatus, SetupStep } from "../../api/types";
import "./setup.css";

/** What the dialog's head says: "3 of 5 done · next: Pick a repo". */
export function progressLine(status: SetupStatus | null | undefined): string {
  if (!status) return "Checking…";
  const s = status.summary;
  const next = status.steps.find((x) => x.id === s.next);
  if (s.complete) return "All set";
  return s.done + " of " + s.total + " done" + (next ? " · next: " + next.title : "");
}

function StepHead({ step }: { step: SetupStep }) {
  const note =
    step.status === "done"
      ? "done"
      : step.status === "skipped"
        ? "skipped"
        : step.required
          ? ""
          : "optional";
  return (
    <h3>
      <span className="setup-num">{stepNumber(step.id)}</span> {step.title}
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

export function SetupChecklist() {
  const { data: status, error } = useSetup();
  const [reprobeKey, setReprobeKey] = useState(0);
  const steps = orderedSteps(status);
  const by = new Map(steps.map((s) => [s.id, s]));
  const step = (id: string) => by.get(id) as SetupStep;

  // Close the modal before jumping to another surface — the checklist itself
  // is the same component inside the modal and on the grid, and a dialog
  // opened over a dialog is the one thing the dialog store can't show.
  const closeSetup = () => {
    if (useUi.getState().openDialog === "setup") useUi.getState().closeDialog();
  };
  const go = (name: "new-session" | "settings", target?: string) => {
    closeSetup();
    useUi.getState().openDialogFor(name, target);
  };

  const tools = step("tools");
  const agent = step("agent_login");
  const repo = step("repo");
  const tracker = step("tracker");
  const phone = step("phone");
  const logins = installedLogins(status?.logins);

  return (
    <div className="setup-checklist" data-setup-next={status?.summary.next || ""}>
      {error ? (
        <p className="error">setup status failed: {(error as Error).message}</p>
      ) : (
        <p className="muted setup-progress">{progressLine(status)}</p>
      )}

      {/* ① Engine and tools */}
      <section className={"setup-step st-" + tools.status} data-step="tools" data-status={tools.status}>
        <StepHead step={tools} />
        <details className="setup-fold" open={!tools.done}>
          <summary onClick={(e) => e.stopPropagation()}>
            {tools.done ? "Everything required is installed — show the list" : "What this computer has"}
          </summary>
          <div className="setup-doctor">
            <DoctorList reprobeKey={reprobeKey} onChange={() => void refreshSetup()} />
          </div>
          <div className="setup-actions">
            <button
              type="button"
              className="setup-recheck"
              onClick={(e) => {
                e.stopPropagation();
                setReprobeKey((k) => k + 1);
              }}
            >
              Re-check
            </button>
          </div>
        </details>
      </section>

      {/* ② Sign in to your agent */}
      <section className={"setup-step st-" + agent.status} data-step="agent_login" data-status={agent.status}>
        <StepHead step={agent} />
        {!status ? (
          <p className="muted setup-hint">Checking…</p>
        ) : logins.length === 0 ? (
          <p className="muted setup-hint">
            No agent CLI is installed yet — step ① installs {status.default_provider || "one"}.
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
          API keys and extra accounts live in{" "}
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
          .
        </p>
      </section>

      {/* ③ Pick a repo */}
      <section className={"setup-step st-" + repo.status} data-step="repo" data-status={repo.status}>
        <StepHead step={repo} />
        {repo.done ? (
          <p className="muted setup-hint">Your first session was created. New (Ctrl+N) starts another.</p>
        ) : (
          <>
            <p className="muted setup-hint">
              A session is an isolated git worktree plus a terminal running your agent. Pick the
              folder to work in — the folder browser is in the dialog — and say what to do.
            </p>
            <div className="setup-actions">
              <button
                type="button"
                className="setup-new"
                onClick={(e) => {
                  e.stopPropagation();
                  go("new-session");
                }}
              >
                + New session
              </button>
            </div>
          </>
        )}
      </section>

      {/* ④ Connect a tracker and GitHub */}
      <section className={"setup-step st-" + tracker.status} data-step="tracker" data-status={tracker.status}>
        <StepHead step={tracker} />
        <p className="muted setup-hint">
          {tracker.done
            ? "A tracker is connected — Intake turns its tickets into sessions."
            : "Connect GitHub for PRs and issues; your tracker (Jira, Linear, Shortcut, Asana, GitHub Issues) is a ticket source in Settings → Connections. Done once the first ticket source is saved."}
        </p>
        <div className="setup-actions">
          <button
            type="button"
            className="setup-open-connections"
            onClick={(e) => {
              e.stopPropagation();
              go("settings", "connections");
            }}
          >
            Open Settings → Connections
          </button>
          <button
            type="button"
            className="setup-connect-github"
            title={
              "Sign in with GitHub, use gh, or paste a token, then Test it against your first repository. " +
              "That token is the whole setup for opening and merging PRs — the gh CLI is optional, and " +
              "pushing is plain git push over your own remote."
            }
            onClick={(e) => {
              e.stopPropagation();
              openConnectGitHub();
            }}
          >
            Connect GitHub
          </button>
          <SkipRow step={tracker} />
        </div>
        <ShortcutTest />
      </section>

      {/* ⑤ Your phone */}
      <section className={"setup-step st-" + phone.status} data-step="phone" data-status={phone.status}>
        <StepHead step={phone} />
        <PhoneStep status={status} done={phone.done} />
        <div className="setup-actions">
          <button
            type="button"
            className="setup-open-mobile"
            onClick={(e) => {
              e.stopPropagation();
              go("settings", "mobile");
            }}
          >
            Settings → Your phone
          </button>
          <SkipRow step={phone} />
        </div>
      </section>

      {status?.report && (
        <details className="setup-report" onClick={(e) => e.stopPropagation()}>
          <summary>What the server found at first start</summary>
          <pre>{status.report}</pre>
        </details>
      )}
    </div>
  );
}

/** Step ⑤'s body: "Install Tailscale" with the doctor's hint when it is
 * missing, else a Show QR button (the fetch is deliberate — the server marks
 * the step done when the owner fetches the QR). */
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
        <p className="muted setup-hint">
          Your phone reaches MindFlock over Tailscale, which isn't ready on this computer
          {ts.detail ? " — " + ts.detail : ""}.
        </p>
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
        <p className="muted setup-hint">
          No QR yet — turn on Make reachable in Settings → Your devices first.
        </p>
      )}
    </div>
  );
}

/** Step ④'s fold: the Shortcut token test. Handy, never a step. GitHub's
 * test is the Connect GitHub dialog's own Test (the button above). */
function ShortcutTest() {
  const [sc, setSc] = useState<TestState>(idleTest);
  const [scToken, setScToken] = useState("");

  const testShortcut = useCallback(async () => {
    setSc({ testing: true });
    const tok = scToken.trim();
    try {
      const r = await api<Record<string, unknown>>("/api/settings/test/shortcut", {
        json: { api_token: tok || "" },
      });
      if (r?.ok) {
        setSc({
          testing: false,
          ok: true,
          msg: "Shortcut OK — " + (r.name || r.mention_name || r.member_id),
        });
        toast("Shortcut token OK" + (r.name ? " — " + r.name : ""));
        if (tok) {
          // Persist through the normal settings path (never echoed back).
          try {
            await api("/api/settings", { json: { shortcut: { api_token: tok } } });
          } catch {
            /* the Settings screen remains the fallback */
          }
          setScToken("");
        }
        return;
      }
      setSc({ testing: false, ok: false, msg: String(r?.error || "test failed") });
    } catch (e) {
      setSc({ testing: false, ok: false, msg: (e as Error).message });
    }
  }, [scToken]);

  return (
    <details className="setup-optional" onClick={(e) => e.stopPropagation()}>
      <summary>Optional: test a Shortcut token</summary>
      <div className="setup-acct-row setup-shortcut-row">
        <button
          type="button"
          className="setup-test-shortcut"
          onClick={(e) => {
            e.stopPropagation();
            void testShortcut();
          }}
        >
          Test Shortcut
        </button>
        <input
          type="password"
          className="setup-shortcut-token"
          placeholder="Shortcut API token (optional)"
          autoComplete="off"
          title="Paste a Shortcut API token to test it — saved on success, never displayed. Leave empty to test the stored token."
          value={scToken}
          onChange={(e) => setScToken(e.target.value)}
          onClick={(e) => e.stopPropagation()}
        />
        <TestResult state={sc} />
      </div>
    </details>
  );
}
