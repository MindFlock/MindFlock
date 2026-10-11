/** The one door to the GitHub credential: open the Connect GitHub dialog from
 * anywhere (Intake's Pull requests / Issues tabs, Settings → Connections'
 * Configure, an extension) without threading props through the tree.
 *
 * `openConnectGitHub()` dispatches a window event; `ConnectGitHubDialog`
 * (components/dialogs/ConnectGitHub.tsx, mounted once in App) listens and
 * shows the same step Setup uses. Pure helpers live here so they can be unit
 * tested without a DOM.
 *
 * Why an event and not a store dialog name: another worker is rebuilding the
 * Connections screen and only needs a function to call — this keeps the
 * contract to one exported name plus one event name. */

export const OPEN_CONNECT_GITHUB_EVENT = "mf:open-connect-github";

/** Open the Connect GitHub dialog (no-op before App has mounted it). */
export function openConnectGitHub(): void {
  if (typeof window === "undefined") return;
  window.dispatchEvent(new CustomEvent(OPEN_CONNECT_GITHUB_EVENT));
}

/** Shown instead of a dead "Sign in with GitHub" button while the build ships
 * no OAuth App client id (backend DEFAULT_GITHUB_OAUTH_CLIENT_ID is empty and
 * neither github.oauth_client_id nor MINDFLOCK_GITHUB_CLIENT_ID is set). */
export const DEVICE_FLOW_UNCONFIGURED =
  "Device flow is not configured in this build; paste a token or use gh";

/** The POST /api/settings/test/github payload: a real GET /user plus one
 * repo-access check against the first configured repository. */
export interface ConnectionTest {
  ok?: boolean;
  token_source?: string;
  login?: string;
  user_error?: string;
  repo?: string;
  repo_ok?: boolean | null;
  repo_error?: string;
  can_push?: boolean;
  detail?: string;
  gh_installed?: boolean;
  gh_authenticated?: boolean;
}

export interface TestLine {
  ok: boolean;
  msg: string;
}

/** One line for the dialog's Test result. The verdict is the server's (token
 * accepted by GitHub AND the first repo reachable); gh is mentioned only as
 * the optional extra it is, never as a reason for a red ✗. */
export function describeConnectionTest(r: ConnectionTest | null | undefined): TestLine {
  if (!r) return { ok: false, msg: "no answer from the server" };
  const source = String(r.token_source || "none");
  if (source === "none" || !r.login) {
    return { ok: false, msg: r.user_error || r.detail || "no GitHub token available" };
  }
  const bits = ["@" + r.login + " via " + source];
  if (r.repo) {
    if (r.repo_ok) bits.push(r.repo + " reachable (" + (r.can_push ? "can push" : "read-only") + ")");
    else bits.push(r.repo + ": " + (r.repo_error || "not reachable"));
  } else {
    bits.push("no repository configured yet to check");
  }
  if (r.gh_installed) bits.push(r.gh_authenticated ? "gh signed in" : "gh installed, not signed in");
  return { ok: !!r.ok, msg: bits.join(" · ") };
}
