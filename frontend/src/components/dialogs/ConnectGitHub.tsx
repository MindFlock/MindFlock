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
 * Test and the push check stay disabled until a credential is saved — before
 * that, all either could say is "no token".
 * **Test** is real: POST /api/settings/test/github does a GET /user with the
 * token the one chain resolves plus one repo-access check against the first
 * configured repository. Then git's name/email (pre-filled from the account,
 * written to the global git config only on "Use these") and a push check that
 * asks the remote. No native prompt/confirm anywhere (the desktop app has
 * neither). */

import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../../api/client";
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

interface PushCheck {
  ok: boolean | null;
  id?: string;
  message?: string;
  fix?: string;
  remote?: string;
}

function errText(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

/** `gh auth login --web` (then `gh auth setup-git`) in a real terminal. */
function GhSignInWindow({ onClose }: { onClose(): void }) {
  const hostRef = useRef<HTMLDivElement>(null);
  const state = useWsTerm(hostRef, "/api/github/gh-login-terminal", true);
  const closeRef = useRef(onClose);
  closeRef.current = onClose;
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
          <span className="muted">
            {state === "streaming"
              ? "copy the code it shows, press Enter, sign in in the browser — then close this window"
              : state}
          </span>
          <button type="button" id="gh-signin-close" onClick={() => void close()}>
            Close
          </button>
        </div>
        <div className="prov-login-term" ref={hostRef} />
      </div>
    </Modal>
  );
}

export function ConnectGitHub({ onChange }: { onChange?(): void }) {
  const [st, setSt] = useState<GithubStatus | null>(null);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState("");
  const [flow, setFlow] = useState<GithubFlow | null>(null);
  const [ghOpen, setGhOpen] = useState(false);
  const [token, setToken] = useState("");
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [push, setPush] = useState<PushCheck | null>(null);
  const [test, setTest] = useState<TestLine | null>(null);
  const changeRef = useRef(onChange);
  changeRef.current = onChange;

  const load = useCallback(async () => {
    try {
      const s = await api<GithubStatus>("/api/github/status");
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

  const connected = useCallback(
    async (login: string) => {
      toast(login ? "GitHub connected — @" + login : "GitHub connected");
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

  const method = primaryGithubMethod(st);
  const hasDevice = method === "device";
  /** gh can sign in here (installed, or offered by the server). */
  const hasGh = (st.methods || []).includes("gh") || !!st.gh?.installed;
  const pasteBox = (
    <div className="setup-acct-row gh-token-row">
      <a href={st.token_url} target="_blank" rel="noopener noreferrer" className="gh-token-link">
        Make a token on GitHub
      </a>
      <input
        type="password"
        className="gh-token-input"
        aria-label="GitHub token"
        placeholder="…then paste it here"
        autoComplete="off"
        value={token}
        onChange={(e) => setToken(e.target.value)}
        onClick={(e) => e.stopPropagation()}
      />
      <button
        type="button"
        className="gh-token-save"
        disabled={!!busy || !token.trim()}
        onClick={(e) => {
          e.stopPropagation();
          void run("token", async () => {
            const r = await api<{ ok: boolean; login: string; error: string }>("/api/github/token", {
              json: { token: token.trim() },
            });
            setToken("");
            if (r.error) toast(r.error);
            await connected(r.login);
          });
        }}
      >
        {busy === "token" ? "Checking…" : "Save"}
      </button>
    </div>
  );

  return (
    <div className="connect-github" id="connect-github">
      {st.connected ? (
        <p className="gh-connected" id="gh-connected">
          ✓ Connected{st.login ? " as @" + st.login : ""}
          <span className="muted"> · {st.source === "gh-cli" ? "via gh" : st.source}</span>
          {st.user_error && <span className="error"> — {st.user_error}</span>}
        </p>
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
              <p className="set-hint gh-token-scopes" id="gh-token-scopes">
                Paste a GitHub token with the <code>repo</code> and <code>read:org</code> scopes.
                The link opens GitHub's token page with both ticked.
              </p>
              {pasteBox}
            </div>
          )}
          {!hasDevice && hasGh && (
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
                Or sign in with the GitHub CLI
              </button>
              <span className="set-hint">opens GitHub in your browser</span>
            </div>
          )}
          {hasDevice && (
            <details className="gh-token-fold" onClick={(e) => e.stopPropagation()}>
              <summary>Or paste a token</summary>
              <p className="set-hint gh-token-scopes">
                Needs the <code>repo</code> and <code>read:org</code> scopes.
              </p>
              {pasteBox}
            </details>
          )}
          <p className="muted setup-hint">
            Lets agents open and review pull requests. Pushing works without it.
          </p>
        </>
      )}

      {needsIdentity(st) && (
        <div className="gh-identity" id="gh-identity">
          <span className="set-label">Your name and email for commits</span>
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
            Saved in your global git config (~/.gitconfig) when you click — the @users.noreply address
            keeps your real email private.
          </span>
        </div>
      )}

      <div className="setup-acct-row gh-test-row">
        <button
          type="button"
          id="gh-connection-test"
          disabled={!!busy || !st.connected}
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
        {test && (
          <span className={"test-result " + (test.ok ? "ok" : "bad")} id="gh-connection-test-result">
            {(test.ok ? "✓ " : "✗ ") + test.msg}
          </span>
        )}
        {!test && (
          <span className="set-hint">
            {st.connected
              ? "Asks GitHub who you are, and whether it reaches your first repository."
              : "Save a token first."}
          </span>
        )}
      </div>

      <div className="setup-acct-row gh-push-row">
        <button
          type="button"
          id="gh-push-check"
          disabled={!!busy || !st.connected}
          onClick={(e) => {
            e.stopPropagation();
            void run("push", async () => setPush(await api<PushCheck>("/api/github/push-check", { method: "POST" })));
          }}
        >
          {busy === "push" ? "Checking…" : "Check I can push"}
        </button>
        {push && (
          <span className={"test-result " + (push.ok ? "ok" : push.ok === false ? "bad" : "")}>
            {(push.ok ? "✓ " : push.ok === false ? "✗ " : "") + (push.message || "")}
            {push.ok === false && push.fix ? " — " + push.fix : ""}
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
  const [open, setOpen] = useState(false);
  useEffect(() => {
    const on = () => setOpen(true);
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
          <span className="muted">so agents can open and review pull requests</span>
          <button type="button" id="connect-github-close" onClick={() => setOpen(false)}>
            Close
          </button>
        </div>
        <ConnectGitHub />
      </div>
    </Modal>
  );
}
