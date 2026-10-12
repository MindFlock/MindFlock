/** The New form's "Git & workspace" fold: where the work happens (new
 * worktree, this folder, provisioned), the Base branch picker for a new
 * worktree, and git init. */

import { useEffect, type Dispatch, type SetStateAction } from "react";
import type { RepoBranches } from "../../../api/types";
import type { deviceApi } from "../../../lib/devices";
import type { RunDraft } from "../NewList";
import { CHECK_DEBOUNCE_MS, looksLikePath, remoteBranchName } from "../newSession";

type Set<T> = Dispatch<SetStateAction<T>>;

/** The Base branch menu's data, for the folder in the field. */
export function useBaseBranches({
  open,
  page,
  repoPath,
  dapi,
  setBaseBranch,
  setBranches,
}: {
  open: boolean;
  page: number;
  repoPath: string;
  dapi: ReturnType<typeof deviceApi>;
  setBaseBranch: Set<string>;
  setBranches: Set<RepoBranches | null>;
}) {
  // The Base branch menu for the folder in the field, re-asked when the
  // folder changes (debounced like the /api/repos/check probe) and only for
  // a path — a name being looked up is not a folder yet. The server memoises
  // ten seconds per folder, so the second ask costs nothing.
  useEffect(() => {
    if (!open || page !== 2) return;
    const path = repoPath.trim();
    setBaseBranch("");
    if (!path || !looksLikePath(path)) {
      setBranches(null);
      return;
    }
    let live = true;
    const h = window.setTimeout(async () => {
      try {
        const b = await dapi<RepoBranches>(
          "/api/repos/branches?path=" + encodeURIComponent(path)
        );
        if (live) setBranches(b);
      } catch {
        // No menu, no error: the server picks the base as it always did.
        if (live) setBranches(null);
      }
    }, CHECK_DEBOUNCE_MS);
    return () => {
      live = false;
      window.clearTimeout(h);
    };
  }, [open, page, repoPath, dapi]);
}

export interface WorkspaceFoldProps {
  advancedOpen: boolean;
  setAdvancedOpen: Set<boolean>;
  foldSummary: string;
  inPlace: boolean;
  setInPlace: Set<boolean>;
  provision: boolean;
  setProvision: Set<boolean>;
  provisionOn: boolean;
  offerProvision: boolean;
  laneNeedsWorktree: boolean;
  draft: RunDraft;
  splitOn: boolean;
  setSplit: Set<boolean>;
  worktreeClamped: string;
  provisionBlocked: string;
  armInitRepo(): void;
  strategy: string;
  setStrategy: Set<string>;
  worktreeMode: boolean;
  branches: RepoBranches | null;
  baseBranch: string;
  setBaseBranch: Set<string>;
  canContinue: boolean;
  continueOn: string;
  setContinueOn: Set<string>;
  initRepo: boolean;
  setInitRepo: Set<boolean>;
}

export function WorkspaceFold({
  advancedOpen,
  setAdvancedOpen,
  foldSummary,
  inPlace,
  setInPlace,
  provision,
  setProvision,
  provisionOn,
  offerProvision,
  laneNeedsWorktree,
  draft,
  splitOn,
  setSplit,
  worktreeClamped,
  provisionBlocked,
  armInitRepo,
  strategy,
  setStrategy,
  worktreeMode,
  branches,
  baseBranch,
  setBaseBranch,
  canContinue,
  continueOn,
  setContinueOn,
  initRepo,
  setInitRepo,
}: WorkspaceFoldProps) {
  return (
    <details
      id="new-advanced"
      className="nf-advanced"
      data-caps="git"
      open={advancedOpen}
      onToggle={(e) => setAdvancedOpen((e.target as HTMLDetailsElement).open)}
    >
      <summary>
        Git &amp; workspace
        <span className="nf-fold-sum" id="new-advanced-sum">
          — {foldSummary}
        </span>
      </summary>
      <div className="nf-advanced-body">
        {/* ONE question with three answers, not a radio pair plus a
            stray checkbox.

            "Isn't Provision workspace the same exact thing as New
            worktree?" — asked about the previous shape, and a fair
            reading of it: both do produce a separate checkout, they sat
            in the same fold, and nothing said how they were related. They
            are not the same (provisioning ALSO runs the repo's setup
            commands and seeds the warm caches, and can make a full clone
            instead of a worktree), but "worktree" being a radio while
            "provision" was a checkbox implied they answered DIFFERENT
            questions, when they answer the same one: where does this
            session's work happen.

            Three radios say the relationship out loud — provisioning is
            the worktree option plus setup — and cost nothing in
            behaviour: the three states were already mutually exclusive,
            each old setter clearing the other by hand. This is that
            exclusion written down instead of maintained. */}
        <div
          className="nf-mode"
          role="radiogroup"
          aria-labelledby="new-mode-label"
        >
          <div id="new-mode-label" className="nf-mode-label">
            Where the work happens
          </div>
          <label className="check">
            <input
              type="radio"
              name="new-workspace-mode"
              id="new-worktree"
              checked={(!inPlace || laneNeedsWorktree) && !provisionOn}
              onChange={() => {
                setInPlace(false);
                setProvision(false);
              }}
            />
            New worktree{" "}
            <span className="muted">
              (a separate checkout on its own branch — nothing is installed
              into it)
            </span>
          </label>
          <label className="check">
            <input
              type="radio"
              name="new-workspace-mode"
              id="new-in-place"
              checked={inPlace && !laneNeedsWorktree}
              onChange={() => {
                // Fast-track commits for THIS session, so it never
                // drives a checkout others share: choosing the shared
                // folder turns it off (said on the line, not hidden).
                if (laneNeedsWorktree) draft.setLane("leave");
                setSplit(false);
                setInPlace(true);
                setProvision(false);
              }}
            />
            Work directly in this folder{" "}
            <span className="muted">
              {splitOn
                ? "(turns auto-split off — a split works in its own worktree)"
                : laneNeedsWorktree
                ? "(turns fast-track off — it commits for this session, so it needs its own worktree)"
                : "(no worktree — edits the original; multiple sessions can share it)"}
            </span>
          </label>
          {offerProvision && (
            <label id="new-provision-row" className="check">
              <input
                type="radio"
                name="new-workspace-mode"
                id="new-provision"
                checked={provisionOn}
                onChange={() => {
                  setProvision(true);
                  setInPlace(false);
                }}
              />
              Provision workspace{" "}
              <span className="muted">
                — the same separate checkout, plus run repo setup &amp; warm
                test caches (or a full clone instead of a worktree)
              </span>
            </label>
          )}
          {worktreeClamped && (
            /* The server would silently do this anyway; saying so is the
               difference between a form that reports the session you are
               about to get and one that reports the session you asked
               for. Same register and same remedy as the git nudge under
               the Folder field. */
            <p className="nf-git-nudge">{worktreeClamped}</p>
          )}
          {provisionBlocked && (
            /* The git aside under the Folder field says the same thing in
               the register of a plain session, where a repo-less folder is
               merely a folder without diff/commit/PR. Here it is a hard
               stop, it is three screens further down, and the fix is the
               checkbox directly below — so it earns its own line. */
            <p className="nf-git-nudge nf-provision-warn">
              {provisionBlocked}{" "}
              <button
                type="button"
                className="linklike"
                title="Ticks “Create a git repo in this folder” below"
                onClick={armInitRepo}
              >
                Create one here
              </button>
            </p>
          )}
          {offerProvision && provision && (
            <div id="provision-opts">
              <label>
                Workspace strategy
                <select
                  id="new-workspace-strategy"
                  value={strategy}
                  onChange={(e) => setStrategy(e.target.value)}
                >
                  <option value="worktree">shared base clone (worktree) — fast, default</option>
                  <option value="clone">full clone — standalone</option>
                </select>
              </label>
              <p className="muted provision-hint">
                Tip: paste a full branch in <b>Name</b> (e.g.{" "}
                <code>feature/sc-17436/grafana-dashboard-…</code>) to use it as the branch
                verbatim — the session name becomes its last segment.
              </p>
            </div>
          )}
        </div>

        {worktreeMode && branches?.is_git && branches.branches.length > 0 && (
          /* Only for a NEW WORKTREE: an in-place session runs on
             whatever the folder has checked out, and the server
             refuses a base for a provisioned one. The first row is
             the server's own choice, so leaving it sends nothing. */
          <>
          <label className="nf-base-branch">
            Base branch{" "}
            <span className="muted">— the worktree is cut from it and diffed against it</span>
            <select
              id="new-base-branch"
              value={baseBranch}
              onChange={(e) => setBaseBranch(e.target.value)}
            >
              <option value="">
                {(branches.current || branches.default || "the checked-out branch") +
                  (branches.current ? " (checked out)" : "")}
              </option>
              {branches.branches.some((b) => !b.remote && !b.current) && (
                <optgroup label="Local">
                  {branches.branches
                    .filter((b) => !b.remote && !b.current)
                    .map((b) => (
                      <option key={b.name} value={b.name}>
                        {b.name}
                      </option>
                    ))}
                </optgroup>
              )}
              {branches.branches.some((b) => b.remote) && (
                <optgroup label="Remote">
                  {branches.branches
                    .filter((b) => b.remote)
                    .map((b) => (
                      <option key={b.name} value={b.name}>
                        {b.name}
                      </option>
                    ))}
                </optgroup>
              )}
            </select>
            {branches.truncated && (
              <span className="muted">Showing the first 200 branches.</span>
            )}
          </label>
          {canContinue && remoteBranchName(baseBranch) && (
            /* A remote-tracking pick: cut a new branch from it (the
               default), or work ON it — the same branch whoever
               pushed it works on (continue_branch). */
            <div className="nf-base-mode" role="radiogroup" aria-label="What to do with the remote branch">
              <label className="check">
                <input
                  type="radio"
                  name="new-base-mode"
                  id="new-base-mode-new"
                  checked={continueOn !== baseBranch}
                  onChange={() => setContinueOn("")}
                />
                Start a new branch from {baseBranch}
              </label>
              <label className="check">
                <input
                  type="radio"
                  name="new-base-mode"
                  id="new-base-mode-continue"
                  checked={continueOn === baseBranch}
                  onChange={() => setContinueOn(baseBranch)}
                />
                Work on {remoteBranchName(baseBranch)} itself{" "}
                <span className="muted">(shared with whoever pushed it)</span>
              </label>
            </div>
          )}
          </>
        )}

        {/* OUTSIDE the mode group, because it is not a fourth answer to
            "where does the work happen" — it is a thing done to the folder
            before any of the three run, and it combines with all of them.
            No longer exclusive with "work directly in this folder", and
            the pairing was always backwards: git-initialising a folder and
            then working in that same folder is the ordinary thing to want
            — arguably the most natural mode for a folder you just made,
            since a worktree cut from a brand-new repo is the awkward case.
            The server does exactly that combination: _prepare_plain_repo
            git-inits and makes the first commit, and the session comes up
            in place with diff/commit/PR on. */}
        <label className="check">
          <input
            type="checkbox"
            id="new-init-repo"
            checked={initRepo}
            onChange={(e) => setInitRepo(e.target.checked)}
          />
          Create a git repo in this folder{" "}
          <span className="muted">(git init + initial commit — enables diff/commit/PR)</span>
        </label>
      </div>
    </details>
  );
}
