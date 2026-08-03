/** Human-readable sidebar labels for sessions that a ticket / PR / issue
 * created.
 *
 * Those sessions are titled by their machine slug — `sc-12345`, `pr-app-42`,
 * `issue-app-77` — which says nothing about what the work IS. The feature name
 * is already sitting in the branch (`feature/<slug>/<name>`, or the PR's head
 * ref), so the label pulls it forward:
 *
 *     sc-12345      feature/sc-12345/add-dark-mode   ->  (tix) add-dark-mode/sc-12345
 *     pr-app-42     fix/login-crash                  ->  (pr) login-crash/app-42
 *     issue-app-77  feature/issue-app-77/cant-open   ->  (iss) cant-open/app-77
 *
 * Titles that no pipeline created are returned untouched — a hand-made
 * "my-refactor" session has nothing to reformat.
 *
 * This is display only. `inst.title` remains the identity every API path,
 * tmux name and workspace dir is keyed by, so the real title always rides
 * along in the row's tooltip.
 */

export interface SessionLabel {
  /** "(tix) add-dark-mode/sc-12345", or the plain title when not a pipeline
   * session. Never truncated: how much fits is a question about the width of
   * the sidebar right now, which only CSS knows, so the row ellipsizes this
   * with `text-overflow` instead of a JS character budget guessing at it. */
  text: string;
  /** Kind tag without parens ("tix" | "pr" | "iss"), or "" for plain sessions. */
  kind: string;
  /** Feature name, un-clipped ("add-dark-mode"), or "" when none was derivable. */
  name: string;
  /** Identifying slug ("sc-12345", "app-42"). */
  slug: string;
}

function escapeRe(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/** The `<name>` of a `feature/<slug>/<name>` branch, else "". */
function featureBranchName(branch: string, slug: string): string {
  const m = new RegExp(`^feature/${escapeRe(slug)}/(.+)$`).exec(branch);
  return m ? m[1] : "";
}

/** Last path segment of a branch ref — a PR's `fix/login-crash` reads as
 * "login-crash". */
function branchTail(branch: string): string {
  const parts = branch.split("/").filter(Boolean);
  return parts.length ? parts[parts.length - 1] : "";
}

/** Split a session title into its kind tag and identifying slug. */
function splitKind(title: string): { kind: string; slug: string } | null {
  if (title.startsWith("pr-")) return { kind: "pr", slug: title.slice(3) };
  if (title.startsWith("issue-")) return { kind: "iss", slug: title.slice(6) };
  return null;
}

/**
 * @param title  The session's own title (already device-stripped — pass
 *               `display_title` for a proxied row, not "<device>::<title>").
 * @param branch The session's branch, which is where the feature name lives.
 */
export function sessionLabel(title: string, branch: string): SessionLabel {
  const plain: SessionLabel = { text: title, kind: "", name: "", slug: title };
  if (!title) return plain;

  const split = splitKind(title);
  // A ticket session is titled by its provider slug with no kind prefix of its
  // own; the branch it owns (feature/<title>/…) is what identifies it as one.
  const ticketName = split ? "" : featureBranchName(branch, title);
  if (!split && !ticketName) return plain;

  const kind = split ? split.kind : "tix";
  const slug = split ? split.slug : title;
  if (!slug) return plain;

  // PR/issue sessions: the feature name comes from the branch under their own
  // slug when the pipeline made one, else from the PR's head ref.
  const name = split ? featureBranchName(branch, title) || branchTail(branch) : ticketName;
  // A branch that just restates the slug adds nothing worth the row width.
  const useName = name && name !== slug && name !== title ? name : "";

  return {
    text: useName ? `(${kind}) ${useName}/${slug}` : `(${kind}) ${slug}`,
    kind,
    name: useName,
    slug,
  };
}
