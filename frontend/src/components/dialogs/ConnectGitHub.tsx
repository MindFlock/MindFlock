/** Connect GitHub: THE one place the GitHub credential is set — Setup's step
 * and, through `ConnectGitHubDialog` (opened by `openConnectGitHub()` from
 * Intake's tabs, Settings → Connections' Configure, or anything else), a
 * standalone dialog.
 *
 * The token used to have four doors (Setup, a raw field under Intake → Pull
 * requests → Advanced, a per-source Token on GitHub Issues cards, env/gh) and
 * two resolvers that disagreed. Now this step (all `/api/github/*`,
 * backend/web/core/github_auth.py) offers the best way this computer has:
 *  - GitHub's device flow when an OAuth App client id is configured (the
 *    build's constant, `github.oauth_client_id` or MINDFLOCK_GITHUB_CLIENT_ID):
 *    a code to type at github.com/login/device, nothing to copy;
 *  - else the token path LEADS — the exact scopes named, a link to GitHub's
 *    token page with them ticked, and the paste box — with `gh auth login
 *    --web` in a sign-in terminal as the second way when gh is installed.
 *    No build-internals sentence ("device flow is not configured") and no
 *    dead button: a person sees the way that works on this computer.
 * The token path is three numbered steps — make a token (the scopes named,
 * the link ticks them) → paste it → Save & test — and the answer lands right
 * under step 3: "✓ Connected as @handle" or why GitHub said no. The Test
 * button appears once connected (before that, step 3 is the test); the push
 * check needs no token (it asks the remote with git), so it is never
 * disabled for want of one.
 * **Test** is real: POST /api/settings/test/github does a GET /user with the
 * token the one chain resolves plus one repo-access check against the first
 * configured repository. Then git's name/email (pre-filled from the account,
 * written to the global git config only on "Use these") and a push check that
 * asks the remote. No native prompt/confirm anywhere (the desktop app has
 * neither). */

import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../../api/client";
import { refreshGithubStatus } from "../../state/queries";
import { copyText } from "../../lib/clipboard";
import {
  flowFinished,
  needsIdentity,
  pollDelay,
  primaryGithubMethod,
  type GithubFlow,
  type GithubStatus,
} from "../../lib/onboarding";
import {
  OPEN_CONNECT_GITHUB_EVENT,
  describeConnectionTest,
  type ConnectionTest,
  type TestLine,
} from "../../lib/githubConnect";
import { toast } from "../../lib/toast";
import { useWsTerm } from "../../lib/wsTerm";
import { notifyConnectionsChanged } from "../settings/screens/Connections";
import { Modal } from "../Modal";
import "../settings/connectGitHub.css";
import "./ConnectGitHub.css";

interface PushCheck {
  ok: boolean | null;
  id?: string;
  message?: string;
  fix?: string;
  remote?: string;
  repo?: string;
}

/** What "Check I can push" will ask, from GET /api/github/status
 * (`push_target`): the remembered repo, its folder name, its origin URL. */
interface PushTarget {
  repo: string;
  name: string;
  remote: string;
}

type StatusWithTarget = GithubStatus & { push_target?: PushTarget };

/** The line beside "Check I can push" before it is pressed: WHICH repo and
 * remote it asks, so a failure is never about a repo you didn't know it meant. */
export function pushTargetLine(t: PushTarget | null | undefined): string {
  if (!t?.name) return "Checks your project's repo once a session has opened one — no token needed.";
  return "Checks " + t.name + (t.remote ? " → " + t.remote : " (it has no remote called origin yet)");
}

/** A fix that is a command goes in a Copy box; prose stays prose. */
export function isCommand(fix: string | undefined): boolean {
  return /^(git|gh|ssh-keygen|ssh-add)\s/.test((fix || "").trim());
}

/** The dialog's one primary path, and what is folded under it:
 *  - the device flow configured → ONE "Sign in with GitHub" button; the token
 *    steps fold under "Or paste a token"; no gh route (the button is easier);
 *  - not configured → the numbered token steps ARE the path; the gh CLI route
 *    (only where gh is installed) folds under "Other ways to connect". */
export function connectLayout(st: GithubStatus | null | undefined): {
  primary: "device" | "token";
  /** The token steps sit in a fold ("Or paste a token"). */
  tokenFolded: boolean;
  /** The gh CLI route, folded under "Other ways to connect". */
  ghFolded: boolean;
} {
  const device = primaryGithubMethod(st) === "device";
  const gh = (st?.methods || []).includes("gh") || !!st?.gh?.installed;
  return { primary: device ? "device" : "token", tokenFolded: device, ghFolded: !device && gh };
}

/** The folder name of a repo path (the API's `repo` is absolute). */
function repoName(path: string | undefined): string {
  const p = (path || "").replace(/[\\/]+$/, "");
  return p.split(/[\\/]/).pop() || p;
}

function errText(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

/** What the gh terminal asks, in the order it asks it. The server runs
 * `gh auth login --hostname github.com --git-protocol https --web` (then
 * `gh auth setup-git`), which still asks one question before the code. */
export const GH_CLI_STEPS = [
  "It asks “Authenticate Git with your GitHub credentials?” — press Enter (yes).",
  "It shows a one-time code — copy it, then press Enter to open GitHub.",
  "Paste the code on GitHub and approve. This window says when you're connected.",
];

/** How often the gh window re-asks whether GitHub is connected yet. */
export const GH_SIGNIN_POLL_MS = 3000;

/** `gh auth login --web` (then `gh auth setup-git`) in a real terminal. */
function GhSignInWindow({ onClose }: { onClose(): void }) {
  const hostRef = useRef<HTMLDivElement>(null);
  const state = useWsTerm(hostRef, "/api/github/gh-login-terminal", true);
  const closeRef = useRef(onClose);
  closeRef.current = onClose;
  // Watch for the sign-in landing, so the window can say so (and offer Done)
  // instead of leaving the person to guess when to close it.
  const [login, setLogin] = useState<string | null>(null);
  useEffect(() => {
    let live = true;
    const t = window.setInterval(async () => {
      try {
        const st = await api<GithubStatus>("/api/github/status");
        if (live && st.connected) {
          setLogin(st.login || "");
          window.clearInterval(t);
        }
      } catch {
        /* the conn banner owns an unreachable server */
      }
    }, GH_SIGNIN_POLL_MS);
    return () => {
      live = false;
      window.clearInterval(t);
    };
  }, []);
  const close = async () => {
    try {
      await api("/api/github/gh-login-close", { method: "POST" });
    } catch {
      /* closing is what the user asked for either way */
    }
    closeRef.current();
  };
  // Escape in the capture phase: it closes this window before the terminal
  // inside it sees the key, and (topmost wins) never the dialog under it.
  return (
    <Modal
      id="gh-signin-dialog"
      role="dialog"
      aria-modal="true"
      portal
      escapeCapture
      onClose={() => void close()}
    >
      <div className="prov-login-panel">
        <div className="ws-head">
          <h2>Sign in to GitHub</h2>
          <span className={login !== null ? "signin-ok" : "muted"} id="gh-signin-status" role="status">
            {login !== null
              ? "✓ Connected" + (login ? " as @" + login : "") + ". You can close this window."
              : state === "streaming"
                ? ""
                : state}
          </span>
          <button
            type="button"
            id="gh-signin-close"
            className={login !== null ? "signin-done" : ""}
            onClick={() => void close()}
          >
            {login !== null ? "Done" : "Close"}
          </button>
        </div>
        {login === null && (
          <ol className="gh-cli-steps muted" id="gh-cli-steps">
            {GH_CLI_STEPS.map((t) => (
              <li key={t}>{t}</li>
            ))}
          </ol>
        )}
        <div className="prov-login-term login-term-dark" ref={hostRef} />
      </div>
    </Modal>
  );
}

export function ConnectGitHub({
  onChange,
  onDone,
}: {
  onChange?(): void;
  /** Shown as "Done" beside "✓ Connected as @…": the dialog's own close, so
   * the person lands back on Setup with ④ ticked. */
  onDone?(): void;
}) {
  const [st, setSt] = useState<StatusWithTarget | null>(null);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState("");
  const [flow, setFlow] = useState<GithubFlow | null>(null);
  const [ghOpen, setGhOpen] = useState(false);
  const [token, setToken] = useState("");
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [push, setPush] = useState<PushCheck | null>(null);
  const [test, setTest] = useState<TestLine | null>(null);
  /** Step 3's answer: "Connected as @handle" or why GitHub said no. */
  const [tokenResult, setTokenResult] = useState<TestLine | null>(null);
  const changeRef = useRef(onChange);
  changeRef.current = onChange;

  const load = useCallback(async () => {
    try {
      const s = await api<StatusWithTarget>("/api/github/status");
      notifyConnectionsChanged();
      setSt(s);
      setErr("");
      if (s.flow && s.flow.state === "pending") setFlow(s.flow);
      // Pre-fill from the account; never overwrite what the person typed.
      setName((v) => v || s.identity?.name || s.identity_suggested?.name || "");
      setEmail((v) => v || s.identity?.email || s.identity_suggested?.email || "");
    } catch (e) {
      setErr(errText(e));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  // No toast: the dialog itself flips to "✓ Connected as @handle".
  const connected = useCallback(
    async (_login: string) => {
      await load();
      changeRef.current?.();
    },
    [load]
  );

  // The device flow: poll at the interval GitHub set until it ends.
  useEffect(() => {
    if (!flow || flowFinished(flow)) return;
    const t = setTimeout(async () => {
      try {
        const f = await api<GithubFlow>("/api/github/device/poll", { method: "POST" });
        setFlow(f);
        if (f.state === "done") void connected(f.login);
      } catch (e) {
        setFlow({ ...flow, state: "error", error: errText(e) });
      }
    }, pollDelay(flow) * 1000);
    return () => clearTimeout(t);
  }, [flow, connected]);

  const run = async (what: string, fn: () => Promise<void>) => {
    setBusy(what);
    try {
      await fn();
    } catch (e) {
      toast(errText(e), { duration: 8000 });
    } finally {
      setBusy("");
    }
  };

  if (err) return <p className="error">GitHub status failed: {err}</p>;
  if (!st) return <p className="muted">Checking GitHub…</p>;

  const layout = connectLayout(st);
  const method = layout.primary;
  const hasDevice = method === "device";
  /** Save the pasted token (GitHub checks it first), then run the Test, all
   * answered inline under step 3 — never a toast at the far edge. */
  const saveToken = () =>
    run("token", async () => {
      setTokenResult(null);
      setTest(null);
      // A rejected token is a 400 carrying the reason: said here, inline.
      let r: { ok: boolean; login: string; error: string };
      try {
        r = await api<{ ok: boolean; login: string; error: string }>("/api/github/token", {
          json: { token: token.trim() },
        });
      } catch (e) {
        setTokenResult({ ok: false, msg: errText(e) || "GitHub rejected this token." });
        return;
      }
      if (!r.ok) {
        setTokenResult({ ok: false, msg: r.error || "GitHub rejected this token." });
        return;
      }
      setToken("");
      setTokenResult({
        ok: true,
        msg: (r.login ? "Connected as @" + r.login : "Connected") + (r.error ? " (" + r.error + ")" : ""),
      });
      await connected(r.login);
      try {
        setTest(describeConnectionTest(await api<ConnectionTest>("/api/settings/test/github", { method: "POST" })));
      } catch {
        /* the save worked; Test stays one click away */
      }
    });

  const tokenSteps = (
    <ol className="gh-steps" id="gh-token-steps">
      <li className="gh-step">
        <span className="gh-step-n" aria-hidden="true">1</span>
        <div className="gh-step-body">
          <span className="gh-step-title">Make a token on GitHub</span>
          <span className="set-hint gh-token-scopes" id="gh-token-scopes">
            It needs the <code>repo</code> and <code>read:org</code> scopes. This link opens
            GitHub's token page with both ticked.
          </span>
          <a
            href={st.token_url}
            target="_blank"
            rel="noopener noreferrer"
            className="test-btn gh-token-link"
            onClick={(e) => e.stopPropagation()}
          >
            Open GitHub ↗
          </a>
        </div>
      </li>
      <li className="gh-step">
        <span className="gh-step-n" aria-hidden="true">2</span>
        <label className="gh-step-body">
          <span className="gh-step-title">Paste it here</span>
          <input
            type="password"
            className="gh-token-input"
            aria-label="GitHub token"
            placeholder="ghp_… or github_pat_…"
            autoComplete="off"
            value={token}
            onChange={(e) => {
              setToken(e.target.value);
              setTokenResult(null);
            }}
            onKeyDown={(e) => {
              if (e.key === "Enter" && token.trim() && !busy) void saveToken();
            }}
            onClick={(e) => e.stopPropagation()}
          />
        </label>
      </li>
      <li className="gh-step">
        <span className="gh-step-n" aria-hidden="true">3</span>
        <div className="gh-step-body">
          <span className="gh-step-row">
            <button
              type="button"
              className="gh-token-save"
              disabled={!!busy || !token.trim()}
              onClick={(e) => {
                e.stopPropagation();
                void saveToken();
              }}
            >
              {busy === "token" ? "Checking with GitHub…" : "Save & test"}
            </button>
            {!token.trim() && !tokenResult && <span className="set-hint">Paste a token first.</span>}
          </span>
          {tokenResult && (
            <span
              id="gh-token-result"
              role="status"
              className={"test-result " + (tokenResult.ok ? "ok" : "bad")}
            >
              {(tokenResult.ok ? "✓ " : "✗ ") + tokenResult.msg}
            </span>
          )}
        </div>
      </li>
    </ol>
  );

  return (
    <div className="connect-github" id="connect-github">
      {st.connected ? (
        <>
          <div className="gh-connected-row">
            <p className="gh-connected" id="gh-connected" role="status">
              ✓ Connected{st.login ? " as @" + st.login : ""}
              <span className="muted"> · {st.source === "gh-cli" ? "via gh" : st.source}</span>
              {st.user_error && <span className="error"> — {st.user_error}</span>}
            </p>
            {onDone && (
              <button
                type="button"
                className="setup-primary"
                id="gh-done"
                onClick={(e) => {
                  e.stopPropagation();
                  onDone();
                }}
              >
                Done
              </button>
            )}
          </div>
          <details className="gh-token-fold" onClick={(e) => e.stopPropagation()}>
            <summary>Use a different token</summary>
            {tokenSteps}
          </details>
        </>
      ) : (
        <>
          {method === "device" &&
            (flow && flow.state === "pending" ? (
              <div className="gh-device" id="gh-device">
                <span>Enter this code at GitHub:</span>
                <code className="gh-user-code">{flow.user_code}</code>
                <button
                  type="button"
                  className="test-btn"
                  onClick={(e) => {
                    e.stopPropagation();
                    void copyText(flow.user_code).then((ok) => toast(ok ? "Code copied" : "Copy failed"));
                  }}
                >
                  Copy
                </button>
                <a href={flow.verification_uri} target="_blank" rel="noopener noreferrer">
                  Open {flow.verification_uri.replace(/^https?:\/\//, "")}
                </a>
                <span className="muted">waiting for you to approve…</span>
              </div>
            ) : (
              <div className="setup-actions">
                <button
                  type="button"
                  id="gh-device-start"
                  disabled={!!busy}
                  onClick={(e) => {
                    e.stopPropagation();
                    void run("device", async () => {
                      const f = await api<GithubFlow & { ok: boolean }>("/api/github/device/start", {
                        method: "POST",
                      });
                      setFlow(f);
                    });
                  }}
                >
                  Sign in with GitHub
                </button>
                {flow && flow.error && <span className="error">{flow.error}</span>}
              </div>
            ))}
          {!hasDevice && (
            <div className="gh-token-lead" id="gh-token-lead">
              {tokenSteps}
            </div>
          )}
          {/* Without the device flow the token path above is THE path; the
              gh CLI route is folded, so step 1 is the only thing in view. */}
          {layout.ghFolded && (
            <details className="gh-other-ways" id="gh-other-ways" onClick={(e) => e.stopPropagation()}>
              <summary>Other ways to connect</summary>
              <div className="setup-actions gh-cli-alt">
                <button
                  type="button"
                  id="gh-cli-signin"
                  className="test-btn"
                  disabled={!!busy}
                  onClick={(e) => {
                    e.stopPropagation();
                    setGhOpen(true);
                  }}
                >
                  Sign in with the GitHub CLI
                </button>
                <span className="set-hint">a terminal asks one question, then opens GitHub in your browser</span>
              </div>
            </details>
          )}
          {layout.tokenFolded && (
            <details className="gh-token-fold" onClick={(e) => e.stopPropagation()}>
              <summary>Or paste a token</summary>
              {tokenSteps}
            </details>
          )}
          <p className="muted setup-hint">
            Needed to open and merge pull requests. Once connected, git uses it too: cloning and pushing GitHub repos asks for nothing else.
          </p>
        </>
      )}

      {needsIdentity(st) && (
        <div className="gh-identity" id="gh-identity">
          <span className="gh-step-title">Your name and email for commits</span>
          <div className="setup-acct-row">
            <input
              className="gh-ident-name"
              placeholder="Your Name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              onClick={(e) => e.stopPropagation()}
            />
            <input
              className="gh-ident-email"
              placeholder="you@example.com"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              onClick={(e) => e.stopPropagation()}
            />
            <button
              type="button"
              id="gh-ident-save"
              disabled={!!busy || !name.trim() || !email.trim()}
              title={!name.trim() || !email.trim() ? "Fill in both your name and email" : undefined}
              onClick={(e) => {
                e.stopPropagation();
                void run("identity", async () => {
                  await api("/api/github/identity", { json: { name: name.trim(), email: email.trim() } });
                  toast("git now knows who you are");
                  await load();
                  changeRef.current?.();
                });
              }}
            >
              Use these
            </button>
          </div>
          <span className="set-hint">
            {!name.trim() || !email.trim()
              ? "Fill in both, then click Use these. "
              : ""}
            Saved in your global git config (~/.gitconfig) — the @users.noreply address keeps your
            real email private.
          </span>
        </div>
      )}

      {/* Test only once there is something to test: before that, step 3
          above is the test (no button that is disabled for no stated reason). */}
      {st.connected && (
        <div className="setup-acct-row gh-test-row">
          <button
            type="button"
            id="gh-connection-test"
            disabled={!!busy}
            onClick={(e) => {
              e.stopPropagation();
              void run("test", async () => {
                const r = await api<ConnectionTest>("/api/settings/test/github", { method: "POST" });
                setTest(describeConnectionTest(r));
              });
            }}
          >
            {busy === "test" ? "Testing…" : "Test"}
          </button>
          {test ? (
            <span className={"test-result " + (test.ok ? "ok" : "bad")} id="gh-connection-test-result">
              {(test.ok ? "✓ " : "✗ ") + test.msg}
            </span>
          ) : (
            <span className="set-hint">Asks GitHub who you are, and whether it reaches your first repository.</span>
          )}
        </div>
      )}

      {/* No token needed: it asks your repository's remote with git itself. */}
      <div className="setup-acct-row gh-push-row">
        <button
          type="button"
          id="gh-push-check"
          disabled={!!busy}
          onClick={(e) => {
            e.stopPropagation();
            void run("push", async () => setPush(await api<PushCheck>("/api/github/push-check", { method: "POST" })));
          }}
        >
          {busy === "push" ? "Checking…" : "Check I can push"}
        </button>
        {push ? (
          <span
            className={"test-result " + (push.ok ? "ok" : push.ok === false ? "bad" : "")}
            id="gh-push-result"
            role="status"
          >
            {(push.ok ? "✓ " : push.ok === false ? "✗ " : "") +
              (push.ok === false && push.repo && push.id !== "no_origin" ? repoName(push.repo) + ": " : "") +
              (push.message || "")}
            {push.ok === false && push.remote ? <span className="muted"> ({push.remote})</span> : null}
            {push.fix && !isCommand(push.fix) ? " — " + push.fix : ""}
          </span>
        ) : (
          <span className="set-hint gh-push-target" id="gh-push-target">
            {pushTargetLine(st.push_target)}
          </span>
        )}
        {push?.fix && isCommand(push.fix) && (
          <span className="gh-push-fix" id="gh-push-fix">
            <span className="set-hint">
              {push.id === "no_origin" ? "Add one — your GitHub repository's address goes at the end:" : "Fix:"}
            </span>
            <code>{push.fix}</code>
            <button
              type="button"
              className="test-btn"
              onClick={(e) => {
                e.stopPropagation();
                void copyText(push.fix || "").then((ok) => toast(ok ? "Copied — paste it into a terminal" : "Copy failed"));
              }}
            >
              Copy
            </button>
          </span>
        )}
        {push?.ok === false && push.id === "https_auth" && st.connected && (
          <button
            type="button"
            id="gh-git-credential"
            disabled={!!busy}
            onClick={(e) => {
              e.stopPropagation();
              void run("cred", async () => {
                const r = await api<{ ok: boolean; helper: string; error: string }>(
                  "/api/github/git-credential",
                  { method: "POST" }
                );
                toast(r.helper === "gh" ? "git pushes through gh now" : "git pushes with your GitHub sign-in now");
                setPush(await api<PushCheck>("/api/github/push-check", { method: "POST" }));
              });
            }}
          >
            Let git push with this sign-in
          </button>
        )}
      </div>

      {ghOpen && (
        <GhSignInWindow
          onClose={() => {
            setGhOpen(false);
            void run("gh", async () => {
              try {
                const r = await api<{ ok: boolean; login: string }>("/api/github/import-gh", {
                  method: "POST",
                });
                await connected(r.login);
              } catch {
                await load(); // not signed in yet: nothing to import
              }
            });
          }}
        />
      )}
    </div>
  );
}

/** The standalone Connect GitHub dialog. Mounted once (App); opens on the
 * `mf:open-connect-github` window event (`openConnectGitHub()` in
 * lib/githubConnect.ts), so Intake's tabs, Settings → Connections and
 * extensions all reach the same step without props. */
export function ConnectGitHubDialog() {
  const [open, setOpenRaw] = useState(false);
  // Closing is when a sign-in made here becomes true everywhere else: the
  // GitHub status lines (Intake's GitHub tabs, the add flow) re-ask then.
  const setOpen = (next: boolean) => {
    setOpenRaw(next);
    if (!next) void refreshGithubStatus();
  };
  useEffect(() => {
    const on = () => setOpenRaw(true);
    window.addEventListener(OPEN_CONNECT_GITHUB_EVENT, on);
    return () => window.removeEventListener(OPEN_CONNECT_GITHUB_EVENT, on);
  }, []);
  if (!open) return null;
  return (
    <Modal
      id="connect-github-dialog"
      role="dialog"
      aria-modal="true"
      portal
      escapeCapture
      onClose={() => setOpen(false)}
    >
      <div className="connect-github-panel">
        <div className="ws-head">
          <h2>Connect GitHub</h2>
          <span className="muted">so agents can open and merge pull requests</span>
          <button type="button" id="connect-github-close" onClick={() => setOpen(false)}>
            Close
          </button>
        </div>
        <ConnectGitHub onDone={() => setOpen(false)} />
      </div>
    </Modal>
  );
}
