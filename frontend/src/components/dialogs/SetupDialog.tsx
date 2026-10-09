/** C2 — first-run setup checklist + shared doctor renderers (port of app.js
 * section 15). Exports:
 *   SetupDialog     — the reopenable Setup modal
 *   SetupChecklist  — the ①②③ checklist (also used by the grid empty state)
 *   DoctorList      — the check list (also used by Settings → Doctor)
 *   useDoctorWarn   — F8 warn-chip state (sidebar chip reads it)
 *   useDoctorAutoShow — headless: load + 5-min doctor probe, auto-open rules
 *   shouldAutoShowSetup — the auto-open rule itself (pure, so it is tested) */

import { useCallback, useEffect, useState } from "react";
import { create } from "zustand";
import { api } from "../../api/client";
import { useConfig, useInstances } from "../../state/queries";
import { useUi } from "../../state/store";
import { toast } from "../../lib/toast";
import { InstallMissing, type InstallStep } from "./InstallTerminal";
import { AgentSignIn, signInTarget } from "./AgentSignIn";
import { ConnectGitHub } from "./ConnectGitHub";
import {
  STATUS_GLYPH,
  stepNumber,
  visibleSteps,
  type OnboardingPlan,
  type OnboardingStep,
  type StepId,
} from "../../lib/onboarding";

// --- Doctor model -------------------------------------------------------------

export interface DoctorCheckItem {
  id?: string;
  label?: string;
  status?: "ok" | "info" | "warn" | "fail" | string;
  detail?: string;
  fix?: string;
  /** The runnable fix (an agent-auth row's declared login command). */
  cmd?: string;
  /** The agent provider an agent row is about (drives "Sign in to …"). */
  provider?: string;
}

export interface DoctorPayload {
  ok: boolean;
  checks?: DoctorCheckItem[];
  /** Everything missing that this machine needs, as one install (see
   * `backend.doctor.install_plan`). */
  install?: { steps?: InstallStep[]; packages?: string[] };
}

const DOCTOR_ICON: Record<string, string> = { ok: "✓", info: "ℹ", warn: "!", fail: "✗" };

/** Cached GET /api/doctor payload (5-min background probe keeps it warm). */
let lastDoctor: DoctorPayload | null = null;

interface DoctorWarnState {
  failing: boolean;
  dismissed: boolean;
  dismiss(): void;
  _setFailing(f: boolean): void;
}

const useDoctorWarnStore = create<DoctorWarnState>((set) => ({
  failing: false,
  dismissed: false, // per page load; ✕ hides until reload
  dismiss: () => set({ dismissed: true }),
  _setFailing: (failing) => set({ failing }),
}));

/** F8 doctor-warn chip state for the sidebar. */
export function useDoctorWarn() {
  return useDoctorWarnStore();
}

let setupAutoShown = false; // auto-open the setup dialog at most once per load

/** The desktop app's first-run hand-off: after installing the engine it opens
 * the app with `?setup=install`, asking for the Setup dialog on its
 * Dependencies step (tmux and the agent CLI are still missing — the engine
 * install can't do them without a terminal). Returns whether it was asked for,
 * and strips the parameter so a reload doesn't ask again. */
export function consumeSetupIntent(
  loc: { search: string; pathname: string; hash: string } = window.location,
  hist: { replaceState(data: unknown, unused: string, url?: string): void } = window.history
): boolean {
  const params = new URLSearchParams(loc.search);
  if (params.get("setup") !== "install") return false;
  params.delete("setup");
  const q = params.toString();
  try {
    hist.replaceState(null, "", loc.pathname + (q ? "?" + q : "") + loc.hash);
  } catch {
    /* a URL we can't rewrite still opened the dialog */
  }
  return true;
}

/** Should a failing doctor probe pop the first-run checklist at this user?
 *
 * Only at one nothing knows to be past first-run. The checklist is a first-run
 * surface, and a veteran was getting ambushed by it on load because some
 * optional check went to warn — an agent CLI that declares no credential
 * locations, say. For her the sidebar's doctor chip is the right amount of noise,
 * and it is untouched by this.
 *
 * The server's flag is the entire rule. `onboarded` is undefined until
 * /api/config lands, and an unknown flag opens nothing: the honest reading is
 * "ask again in a moment", which is why the caller re-evaluates instead of
 * latching on the first probe. There is deliberately no per-browser "already saw
 * it" override — the two localStorage keys that used to sit here (mf_setup_done,
 * mf_ever_created) had no writer left in this app, and the only thing a working
 * one could have bought is a user with a missing tmux never being shown the
 * checklist again. It should keep opening every load until either the tools are
 * installed or a session exists.
 *
 * And never over an empty grid. With no session at all the grid's first-run
 * card already shows this very checklist, so popping the modal on top of it put
 * the same three steps on screen twice, right under the welcome tour. The
 * caller also spends the once-per-load latch on that case (see
 * useDoctorAutoShow), so creating the first session does not pop the modal
 * over it while the cached config still says "not onboarded". */
export function shouldAutoShowSetup(opts: {
  failing: boolean;
  onboarded: boolean | undefined;
  sessions: number;
}): boolean {
  return opts.failing && opts.onboarded === false && opts.sessions > 0;
}

/** Headless doctor probe: on load + every 5 minutes (never on the 4s poll).
 * Failing required tools auto-open the setup checklist once per load, for a
 * first-run user only — see shouldAutoShowSetup.
 *
 * The onboarded flag comes from the shared config query rather than an argument
 * because this hook is called from the app shell's very first render, before
 * /api/config has resolved: a value handed in there would be `undefined` for the
 * life of the probe, and so would a getQueryData() read inside the mount-once
 * effect below. Subscribing costs no extra request (same query key, 60s
 * staleTime) and makes the decision reactive, which is what the race needs. */
export function useDoctorAutoShow() {
  const { data: config } = useConfig();
  const { data: instances } = useInstances();
  // -1 while the first snapshot is in flight: unknown, so neither rule fires.
  const sessions = instances ? instances.length : -1;
  const failing = useDoctorWarnStore((s) => s.failing);

  useEffect(() => {
    // Asked for by the desktop app's first run: open now, whatever the probe
    // says, and spend the once-per-load latch on it.
    if (consumeSetupIntent()) {
      setupAutoShown = true;
      useUi.getState().openDialogFor("setup");
    }
    const check = async () => {
      try {
        const d = await api<DoctorPayload>("/api/doctor");
        lastDoctor = d;
        useDoctorWarnStore.getState()._setFailing(!(d && d.ok));
      } catch {
        /* unreachable backend is the conn-banner's job */
      }
    };
    check();
    const t = setInterval(() => {
      if (!document.hidden) check();
    }, 300000);
    return () => clearInterval(t);
  }, []);

  // Whichever of the probe and the config query lands second decides. Doing it
  // here rather than inside check() is what stops a genuinely new user from
  // waiting five minutes for the next probe just because doctor answered before
  // the onboarded flag did.
  useEffect(() => {
    if (setupAutoShown) return;
    // The grid's own card is showing the checklist this load: that IS the
    // auto-show. Latch, so the session it leads to does not get the modal
    // dropped on it a poll later (config is cached, and still says false).
    if (failing && config?.onboarded === false && sessions === 0) {
      setupAutoShown = true;
      return;
    }
    const show = shouldAutoShowSetup({ failing, onboarded: config?.onboarded, sessions });
    if (!show) return;
    setupAutoShown = true;
    useUi.getState().openDialogFor("setup");
  }, [failing, config?.onboarded, sessions]);
}

/** The doctor check list (setup panel + Settings → Doctor). */
export function DoctorList({ reprobeKey }: { reprobeKey?: number }) {
  const [doctor, setDoctor] = useState<DoctorPayload | null>(lastDoctor);
  const [error, setError] = useState("");
  // Bumped when the install window finishes, so the list re-probes itself.
  const [installed, setInstalled] = useState(0);

  useEffect(() => {
    let live = true;
    (async () => {
      const reprobe = (reprobeKey || 0) > 0 || installed > 0;
      try {
        const d = await api<DoctorPayload>("/api/doctor" + (reprobe ? "?refresh=1" : ""));
        if (!live) return;
        lastDoctor = d;
        setDoctor(d);
        setError("");
      } catch (e) {
        if (live) setError((e as Error).message);
      }
    })();
    return () => {
      live = false;
    };
  }, [reprobeKey, installed]);

  if (error) return <p className="error">doctor failed: {error}</p>;
  if (!doctor) return <p className="muted">Checking dependencies…</p>;
  if (!doctor.checks || !doctor.checks.length) return <p className="muted">doctor unavailable</p>;
  return (
    <>
      <InstallMissing steps={doctor.install?.steps} onDone={() => setInstalled((n) => n + 1)} />
      <ul className="doctor-list">
        {doctor.checks.map((c, i) => (
          <li key={c.id || i} className={"doctor-check st-" + (c.status || "info")}>
            <span className="doctor-ico">{DOCTOR_ICON[c.status || ""] || "•"}</span>
            <span className="doctor-label">{c.label || c.id || ""}</span>
            <span className="doctor-detail">
              {c.detail || ""}
              {c.fix && c.status !== "ok" && <span className="doctor-fix"> fix: {c.fix}</span>}
              {signInTarget(c) && (
                <AgentSignIn
                  provider={signInTarget(c) as string}
                  className="doctor-signin"
                  onDone={() => setInstalled((n) => n + 1)}
                />
              )}
            </span>
          </li>
        ))}
      </ul>
    </>
  );
}

// --- Account tests -------------------------------------------------------------

function TestResult({ state }: { state: { testing: boolean; ok?: boolean; msg?: string } }) {
  if (state.testing) return <span className="test-result">testing…</span>;
  if (state.msg === undefined) return <span className="test-result" />;
  return (
    <span className={"test-result " + (state.ok ? "ok" : "bad")}>
      {(state.ok ? "✓ " : "✗ ") + state.msg}
    </span>
  );
}

export type TestState = { testing: boolean; ok?: boolean; msg?: string };
const idleTest: TestState = { testing: false };

// --- The one GitHub credential test ------------------------------------------
// The setup checklist and both GitHub Intake tabs all show
// POST /api/settings/test/github. They used to each build their own summary
// line, which is how "gh not installed" ended up reading like a failure in
// three places at once. One helper now, so the wording cannot drift again.

/** Render the /settings/test/github payload.
 *
 * The ✓/✗ verdict is driven purely by whether a TOKEN resolves. gh is reported
 * because it is genuinely useful — but it is optional, so a contributor who
 * pushes over SSH and has a token in Settings is fully configured and must not
 * be shown a red ✗ for a CLI she does not need. */
export function describeGithubTest(r: Record<string, unknown> | null): TestState {
  const source = String(r?.token_source || "none");
  // Trust the server's own verdict (it is already token-derived) but re-derive
  // it defensively so an older/leaner payload still can't blame gh.
  const haveToken = !!r?.ok || (source !== "" && source !== "none");
  const bits = ["token: " + source];
  if (r?.gh_installed) bits.push(r.gh_authenticated ? "gh authenticated" : "gh not authenticated");
  else bits.push("gh not installed (optional)");
  if (r?.detail) bits.push(String(r.detail));
  return { testing: false, ok: haveToken, msg: bits.join(" · ") };
}

/** Run the test and return a ready-to-render TestState. Never throws. */
export async function runGithubTest(): Promise<TestState> {
  try {
    const r = await api<Record<string, unknown>>("/api/settings/test/github", { method: "POST" });
    return describeGithubTest(r);
  } catch (e) {
    return { testing: false, ok: false, msg: (e as Error).message };
  }
}

/** One plan step's status line: its glyph and the server's reason. */
function StepReason({ step }: { step?: OnboardingStep }) {
  if (!step || !step.reason) return null;
  return (
    <p className={"setup-reason st-" + step.status} data-step-status={step.status}>
      {(STATUS_GLYPH[step.status] || "•") + " " + step.reason}
    </p>
  );
}

/** The first-run plan, step by step (empty-state card + the Setup dialog).
 *
 * The order and every step's status come from the server's plan (GET
 * /api/onboarding — the same one `mindflock init` prints): Dependencies →
 * "First computer, or join one you already have?" → agent sign-in →
 * Tailscale (only when going multi-device) → Connect GitHub → first session.
 * The devices question comes before the agent and GitHub steps because
 * joining brings the default agent, the GitHub token and ticket sources —
 * never the agent's sign-in, which stays on each computer.
 *
 * `standalone` is the grid's first-run card identifying itself, and nothing
 * renders differently for it: it used to switch on a self-dismissal that could
 * never fire, since the card exists only for a user the server calls not
 * onboarded and the dismissal asked for the opposite. The prop is still accepted
 * because TerminalGrid passes it, and an unknown prop there is a type error that
 * would take the whole "Three steps to a running agent" card down. */
export function SetupChecklist(_props: { standalone?: boolean }) {
  const [reprobeKey, setReprobeKey] = useState(0);
  const [gh, setGh] = useState<TestState>(idleTest);
  const [sc, setSc] = useState<TestState>(idleTest);
  const [agent, setAgent] = useState<TestState>(idleTest);
  // The default agent's provider when it has no login yet (from the doctor's
  // agent-auth row, then from each agent test) — offers "Sign in to …".
  const [signIn, setSignIn] = useState<string | null>(null);
  const [scToken, setScToken] = useState("");
  const [plan, setPlan] = useState<OnboardingPlan | null>(null);
  const [choosing, setChoosing] = useState(false);

  const loadPlan = useCallback(async (refresh = false) => {
    try {
      setPlan(await api<OnboardingPlan>("/api/onboarding" + (refresh ? "?refresh=1" : "")));
    } catch {
      /* the steps still work from their own probes */
    }
  }, []);

  useEffect(() => {
    let live = true;
    void loadPlan();
    api<DoctorPayload>("/api/doctor")
      .then((d) => {
        if (live) setSignIn(signInTarget((d.checks || []).find((c) => c.id === "agent-auth")));
      })
      .catch(() => {
        /* the checklist above reports an unreachable doctor */
      });
    return () => {
      live = false;
    };
  }, [loadPlan]);

  const step = (id: StepId) => plan?.steps.find((x) => x.id === id);
  const shown = visibleSteps(plan);
  const num = (id: StepId) => stepNumber(id, shown);

  const closeSetup = () => {
    if (useUi.getState().openDialog === "setup") useUi.getState().closeDialog();
  };
  const openDevices = () => {
    closeSetup();
    useUi.getState().openDialogFor("settings", "devices");
  };

  const choose = async (choice: "first" | "join" | "") => {
    setChoosing(true);
    try {
      setPlan(await api<OnboardingPlan>("/api/onboarding/choice", { json: { choice } }));
    } catch (e) {
      toast((e as Error).message);
    } finally {
      setChoosing(false);
    }
  };

  const testGithub = useCallback(async () => {
    setGh({ testing: true });
    setGh(await runGithubTest());
  }, []);

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

  const testAgent = useCallback(async () => {
    setAgent({ testing: true });
    try {
      const r = await api<{ ok?: boolean; cli?: { detail?: string }; auth?: DoctorCheckItem }>(
        "/api/settings/test/agent",
        { method: "POST" }
      );
      setSignIn(signInTarget(r?.auth));
      const bits: string[] = [];
      if (r?.cli?.detail) bits.push(r.cli.detail);
      if (r?.auth?.detail) bits.push(r.auth.detail);
      setAgent({
        testing: false,
        ok: !!r?.ok,
        msg: bits.join(" · ") || (r?.ok ? "agent CLI ready" : "agent CLI not ready"),
      });
    } catch (e) {
      setAgent({ testing: false, ok: false, msg: (e as Error).message });
    }
    void loadPlan(true);
  }, [loadPlan]);

  const devices = step("devices");
  const agentStep = step("agent");
  const tsStep = step("tailscale");
  const ghStep = step("github");

  return (
    <>
      <div className="setup-step" data-step="deps">
        <h3>
          <span className="setup-num">{num("deps")}</span> Dependencies
        </h3>
        <div className="setup-doctor">
          <DoctorList reprobeKey={reprobeKey} />
        </div>
        <div className="setup-actions">
          <button
            type="button"
            className="setup-recheck"
            onClick={(e) => {
              e.stopPropagation();
              setReprobeKey((k) => k + 1);
              void loadPlan(true);
            }}
          >
            Re-check
          </button>
        </div>
      </div>

      <div className="setup-step setup-devices" data-step="devices">
        <h3>
          <span className="setup-num">{num("devices")}</span> First computer, or join one you already
          have?
        </h3>
        {!plan ? (
          <p className="muted setup-hint">Checking…</p>
        ) : devices?.ask ? (
          <>
            <p className="muted setup-hint">
              Already use MindFlock on another computer? Join it first: your settings, GitHub token
              and ticket sources come along. (Agent sign-ins stay on each computer, so you sign in
              here after.)
            </p>
            <div className="setup-actions">
              <button
                type="button"
                className="setup-first-computer"
                disabled={choosing}
                onClick={(e) => {
                  e.stopPropagation();
                  void choose("first");
                }}
              >
                This is my first computer
              </button>
              <button
                type="button"
                className="setup-join-computer"
                disabled={choosing}
                onClick={(e) => {
                  e.stopPropagation();
                  void choose("join");
                }}
              >
                Join one I already have
              </button>
            </div>
          </>
        ) : (
          <>
            <StepReason step={devices} />
            <div className="setup-actions">
              {devices?.choice === "join" && devices.status !== "ok" && (
                <button
                  type="button"
                  className="setup-open-devices"
                  onClick={(e) => {
                    e.stopPropagation();
                    openDevices();
                  }}
                >
                  Join it in Settings → Devices
                </button>
              )}
              {devices?.status !== "ok" || devices?.choice === "first" ? (
                <button
                  type="button"
                  className="linklike setup-devices-change"
                  disabled={choosing}
                  onClick={(e) => {
                    e.stopPropagation();
                    void choose("");
                  }}
                >
                  Change
                </button>
              ) : null}
            </div>
            {devices?.choice === "join" && devices.status !== "ok" && (
              <p className="muted setup-hint">
                On your other computer: Settings → Devices → Add a device shows a code to paste
                here — and, for a computer with nothing installed yet, one line that installs
                MindFlock and joins in one go.
              </p>
            )}
          </>
        )}
      </div>

      <div className="setup-step" data-step="agent">
        <h3>
          <span className="setup-num">{num("agent")}</span> Sign in to your agent
        </h3>
        {agentStep?.status === "skip" ? (
          <StepReason step={agentStep} />
        ) : (
          <>
            <StepReason step={agentStep} />
            <div className="setup-acct-row">
              <button
                type="button"
                className="setup-test-agent"
                onClick={(e) => {
                  e.stopPropagation();
                  testAgent();
                }}
              >
                Test agent CLI
              </button>
              <TestResult state={agent} />
              {signIn && <AgentSignIn provider={signIn} onDone={() => void testAgent()} />}
            </div>
          </>
        )}
      </div>

      {shown.includes("tailscale") && (
        <div className="setup-step" data-step="tailscale">
          <h3>
            <span className="setup-num">{num("tailscale")}</span> Tailscale
          </h3>
          <StepReason step={tsStep} />
          {tsStep?.status === "todo" && tsStep.fix && (
            <p className="setup-hint">
              fix: <code>{tsStep.fix}</code>
            </p>
          )}
          {tsStep?.status === "todo" && (
            <div className="setup-actions">
              <button
                type="button"
                className="setup-open-tailscale"
                onClick={(e) => {
                  e.stopPropagation();
                  openDevices();
                }}
              >
                Sign in in Settings → Devices
              </button>
            </div>
          )}
        </div>
      )}

      <div className="setup-step" data-step="github">
        <h3>
          <span className="setup-num">{num("github")}</span> Connect GitHub
        </h3>
        {ghStep?.status === "skip" ? (
          <StepReason step={ghStep} />
        ) : (
          <ConnectGitHub onChange={() => void loadPlan()} />
        )}
        {/* The account tests and a Shortcut token: handy, never a step. */}
        <details className="setup-optional" onClick={(e) => e.stopPropagation()}>
          <summary>Optional: test GitHub or a Shortcut token</summary>
          <div className="setup-acct-row">
            <button
              type="button"
              className="setup-test-github"
              // What the welcome tour's old PR-review slide used to say: the
              // token is what onboarding asks for, never a gh login.
              title={
                "Checks that a GitHub token resolves (Connect GitHub above, $GH_TOKEN / $GITHUB_TOKEN, " +
                "or gh auth token). That token is the whole setup for opening and merging PRs — the gh CLI " +
                "is optional, and pushing is plain git push over your own remote."
              }
              onClick={(e) => {
                e.stopPropagation();
                testGithub();
              }}
            >
              Test GitHub
            </button>
            <TestResult state={gh} />
          </div>
          <div className="setup-acct-row setup-shortcut-row">
            <button
              type="button"
              className="setup-test-shortcut"
              onClick={(e) => {
                e.stopPropagation();
                testShortcut();
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
        <p className="muted setup-hint">
          Ticket sources are set up in{" "}
          <button
            type="button"
            className="setup-open-intake linklike"
            onClick={(e) => {
              e.stopPropagation();
              closeSetup();
              useUi.getState().openDialogFor("intake", "tickets");
            }}
          >
            Intake
          </button>
          {" · agent accounts in "}
          <button
            type="button"
            className="setup-open-settings linklike"
            onClick={(e) => {
              e.stopPropagation();
              closeSetup();
              useUi.getState().openDialogFor("settings", "accounts");
            }}
          >
            Settings → Accounts
          </button>
        </p>
      </div>

      <div className="setup-step" data-step="repo">
        <h3>
          <span className="setup-num">{num("repo")}</span> Create your first session
        </h3>
        <StepReason step={step("repo")} />
        <div className="setup-actions">
          <button
            type="button"
            className="setup-new"
            onClick={(e) => {
              e.stopPropagation();
              closeSetup();
              useUi.getState().openDialogFor("new-session");
            }}
          >
            + New session
          </button>
        </div>
      </div>
    </>
  );
}

export function SetupDialog() {
  const open = useUi((s) => s.openDialog === "setup");
  const closeDialog = useUi((s) => s.closeDialog);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        closeDialog();
        e.preventDefault();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open, closeDialog]);

  if (!open) return null;

  return (
    <div
      id="setup-dialog"
      className="modal"
      onClick={(e) => {
        if (e.target === e.currentTarget) closeDialog();
      }}
    >
      <div id="setup-panel-dlg">
        <div className="ws-head">
          <h2>Setup</h2>
          <button type="button" id="setup-close" onClick={closeDialog}>
            Close
          </button>
        </div>
        <div id="setup-dialog-body">
          <SetupChecklist />
        </div>
      </div>
    </div>
  );
}
