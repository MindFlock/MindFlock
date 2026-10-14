/** Where a New session's work happens. Two parts:
 *
 * - WorksIn, ON the form (never folded): "Separate worktree" (recommended,
 *   the default for a git repo) or "This folder directly", the branch the
 *   worktree will be cut on, and a warning when another session already
 *   edits the folder directly.
 * - GitOptions, under More options: provisioning (setup + warm caches on
 *   top of the separate worktree), the Base branch picker, and git init for
 *   a folder that has no repo yet. */

import { useEffect, type Dispatch, type SetStateAction } from "react";
import type { RepoBranches } from "../../../api/types";
import type { deviceApi } from "../../../lib/devices";
import type { RunDraft } from "../NewList";
import { CHECK_DEBOUNCE_MS, looksLikePath, remoteBranchName, sharedCheckoutWarning } from "../newSession";

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

export interface WorksInProps {
  inPlace: boolean;
  setInPlace: Set<boolean>;
  setProvision: Set<boolean>;
  provisionOn: boolean;
  laneNeedsWorktree: boolean;
  draft: RunDraft;
  splitOn: boolean;
  setSplit: Set<boolean>;
  worktreeClamped: string;
  /** A git repo is in the folder (the branch line is only true then). */
  isGit: boolean;
  /** The branch the worktree will be cut on ("" = not known yet). */
  branch: string;
  /** What it is cut from ("" = the server's pick). */
  baseLabel: string;
  /** Works ON a pushed branch ("Work on x itself"). */
  continueBranch: string;
  /** Sessions already editing this folder directly. */
  sharers: string[];
}

/** "Works in": the one workspace question every session answers, as two
 * radios on the form itself. The recommended one is the default for a repo:
 * a session on its own branch cannot overwrite another's edits, which is the
 * promise "run several at once" rests on. */
export function WorksIn({
  inPlace,
  setInPlace,
  setProvision,
  provisionOn,
  laneNeedsWorktree,
  draft,
  splitOn,
  setSplit,
  worktreeClamped,
  isGit,
  branch,
  baseLabel,
  continueBranch,
  sharers,
}: WorksInProps) {
  const worktree = !inPlace || laneNeedsWorktree || provisionOn;
  const warn = sharedCheckoutWarning(sharers);
  return (
    <div id="new-works-in" className="rt-row rt-row-top nf-works-in" data-caps="git">
      <span className="rt-label" id="new-mode-label">
        Works in
      </span>
      <div className="rt-ctl nf-mode" role="radiogroup" aria-labelledby="new-mode-label">
        <label className="check">
          <input
            type="radio"
            name="new-workspace-mode"
            id="new-worktree"
            checked={worktree}
            onChange={() => setInPlace(false)}
          />
          Separate worktree <span className="nf-rec">recommended</span>{" "}
          <span className="muted">— its own branch, so several sessions can run at once</span>
        </label>
        {worktree && isGit && !worktreeClamped && (branch || continueBranch) && (
          <p className="nf-branch-line" id="new-branch-preview">
            {continueBranch ? (
              <>
                Works on <code>{continueBranch}</code> itself (shared with whoever pushed it)
              </>
            ) : (
              <>
                New branch <code>{branch}</code>
                {baseLabel ? <> from {baseLabel}</> : null}
              </>
            )}
          </p>
        )}
        {worktree && worktreeClamped && <p className="nf-git-nudge">{worktreeClamped}</p>}
        <label className="check">
          <input
            type="radio"
            name="new-workspace-mode"
            id="new-in-place"
            checked={!worktree}
            onChange={() => {
              // Fast-track commits for THIS session, so it never drives a
              // checkout others share: choosing the shared folder turns it
              // off (said on the line, not hidden).
              if (laneNeedsWorktree) draft.setLane("leave");
              setSplit(false);
              setInPlace(true);
              setProvision(false);
            }}
          />
          This folder directly{" "}
          <span className="muted">
            {splitOn
              ? "— turns auto-split off (a split works in its own worktree)"
              : laneNeedsWorktree
              ? "— turns fast-track off (it commits for this session, so it needs its own worktree)"
              : "— edits your checkout as it is, on its current branch"}
          </span>
        </label>
        {warn && !worktree && (
          <p className="nf-git-nudge nf-share-warn" id="new-share-warn" role="status">
            {warn}
          </p>
        )}
      </div>
    </div>
  );
}

export interface GitOptionsProps {
  provision: boolean;
  setProvision: Set<boolean>;
  setInPlace: Set<boolean>;
  provisionOn: boolean;
  offerProvision: boolean;
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
  /** The folder is already a git repo: "Create a git repo" is not offered. */
  isGit: boolean;
}

/** More options → the git extras. */
export function GitOptions({
  provision,
  setProvision,
  setInPlace,
  provisionOn,
  offerProvision,
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
  isGit,
}: GitOptionsProps) {
  return (
    <div className="nf-git-opts" data-caps="git">
      {offerProvision && (
        /* Provisioning is the separate worktree PLUS the repo's setup — a
           modifier of that answer, so a box under it rather than a third
           answer to "works in". Ticking it picks the worktree. */
        <label id="new-provision-row" className="check">
          <input
            type="checkbox"
            id="new-provision"
            checked={provisionOn}
            onChange={(e) => {
              setProvision(e.target.checked);
              if (e.target.checked) setInPlace(false);
            }}
          />
          Provision the worktree{" "}
          <span className="muted">— also run the repo's setup and warm test caches</span>
        </label>
      )}
      {provisionBlocked && (
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

      {worktreeMode && branches?.is_git && branches.branches.length > 0 && (
        /* Only for a NEW WORKTREE: an in-place session runs on whatever the
           folder has checked out, and the server refuses a base for a
           provisioned one. The first row is the server's own choice, so
           leaving it sends nothing. */
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
            {branches.truncated && <span className="muted">Showing the first 200 branches.</span>}
          </label>
          {canContinue && remoteBranchName(baseBranch) && (
            /* A remote-tracking pick: cut a new branch from it (the default),
               or work ON it — the same branch whoever pushed it works on
               (continue_branch). */
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

      {/* Not offered for a folder that already IS a repo — there is nothing
          to create. It combines with either "works in" answer: git init here
          and then work here is the ordinary reading of both. */}
      {(!isGit || initRepo) && (
        <label className="check">
          <input
            type="checkbox"
            id="new-init-repo"
            checked={initRepo}
            onChange={(e) => setInitRepo(e.target.checked)}
          />
          Create a git repo in this folder{" "}
          <span className="muted">(git init + first commit — turns on diff, commit and PR)</span>
        </label>
      )}
    </div>
  );
}
