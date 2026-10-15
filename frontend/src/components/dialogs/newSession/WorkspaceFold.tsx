/** Where a New session's work happens. Two parts:
 *
 * - WorksIn, ON the form (never folded): "Separate worktree" (recommended,
 *   the default for a git repo) or "This folder directly", the branch the
 *   worktree will be cut on, and a warning when another session already
 *   edits the folder directly.
 * - GitOptions, under More options: provisioning (setup + warm caches on
 *   top of the separate worktree), the Base branch picker, and git init for
 *   a folder that has no repo yet. */

import { useEffect, type Dispatch, type ReactNode, type SetStateAction } from "react";
import type { RepoBranches } from "../../../api/types";
import type { deviceApi } from "../../../lib/devices";
import type { RunDraft } from "../NewList";
import {
  CHECK_DEBOUNCE_MS,
  looksLikePath,
  otherSessionsLine,
  remoteBranchName,
  sharedCheckoutWarning,
  type PlannedName,
} from "../newSession";

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
        // An empty answer rather than null, which reads as "still loading".
        if (live) setBranches({ path, is_git: false, current: "", default: "", branches: [] });
      }
    }, CHECK_DEBOUNCE_MS);
    return () => {
      live = false;
      window.clearTimeout(h);
    };
  }, [open, page, repoPath, dapi]);
}

/* The words for where a session works, in ONE place: the New form and the
   Templates editor both draw these, so a template can never describe the
   same choice in other words (or with another kind of control). */

/** One answer to "Works in", in the column right of its radio: the title
 * (and any badge) on the first line, one short muted line under it. Laid out
 * as two lines on purpose — the title and its explanation used to run on as
 * one sentence, and a long one wrapped back under the radio mid-phrase. */
export function ModeText({ title, desc }: { title: ReactNode; desc: ReactNode }) {
  return (
    <span className="nf-opt-text">
      <span className="nf-opt-title">{title}</span>
      <span className="nf-opt-desc">{desc}</span>
    </span>
  );
}

/** "Separate worktree"'s plain line. */
export const WORKTREE_HINT = "Its own branch, so several sessions can run at once";

/** "Separate worktree": the recommended answer for a git repo. */
export function WorktreeText() {
  return (
    <ModeText
      title={
        <>
          Separate worktree <span className="nf-rec">recommended</span>
        </>
      }
      desc={WORKTREE_HINT}
    />
  );
}

/** A checkbox laid out like a Works-in answer: the box in its own column,
 * the title on line 1 and one short muted line under it, both hung on one
 * edge — for a form whose control column is too narrow for the one-line
 * "title — explanation" to fit (a template's editor), where the run-on
 * sentence wrapped back under the box mid-phrase. */
function StackedCheck({
  id,
  rowId,
  checked,
  onChange,
  title,
  desc,
}: {
  id: string;
  rowId?: string;
  checked: boolean;
  onChange(on: boolean): void;
  title: ReactNode;
  desc: ReactNode;
}) {
  return (
    <label id={rowId} className="check nf-opt nf-opt-check">
      <input type="checkbox" id={id} checked={checked} onChange={(e) => onChange(e.target.checked)} />
      <ModeText title={title} desc={desc} />
    </label>
  );
}

/** "Provision the worktree": the separate worktree plus the repo's setup. */
export function ProvisionCheck({
  id,
  rowId,
  checked,
  onChange,
  stacked,
}: {
  id: string;
  rowId?: string;
  checked: boolean;
  onChange(on: boolean): void;
  /** Title over a muted line (StackedCheck) instead of one run-on line. */
  stacked?: boolean;
}) {
  if (stacked)
    return (
      <StackedCheck
        id={id}
        rowId={rowId}
        checked={checked}
        onChange={onChange}
        title="Provision the worktree"
        desc="Also runs the repo's setup and warms test caches"
      />
    );
  return (
    <label id={rowId} className="check">
      <input type="checkbox" id={id} checked={checked} onChange={(e) => onChange(e.target.checked)} />
      Provision the worktree{" "}
      <span className="muted">— also run the repo's setup and warm test caches</span>
    </label>
  );
}

/** A provisioned worktree's strategy: a shared base clone, or a full clone. */
export function StrategyPicker({
  id,
  value,
  onChange,
}: {
  id: string;
  value: string;
  onChange(v: string): void;
}) {
  return (
    <label>
      Workspace strategy
      <select id={id} value={value} onChange={(e) => onChange(e.target.value)}>
        <option value="worktree">shared base clone (worktree) — fast, default</option>
        <option value="clone">full clone — standalone</option>
      </select>
    </label>
  );
}

/** git init for a folder with no repo yet. */
export function InitRepoCheck({
  id,
  checked,
  onChange,
  stacked,
}: {
  id: string;
  checked: boolean;
  onChange(on: boolean): void;
  /** Title over a muted line (StackedCheck) instead of one run-on line. */
  stacked?: boolean;
}) {
  if (stacked)
    return (
      <StackedCheck
        id={id}
        checked={checked}
        onChange={onChange}
        title="Create a git repo in this folder"
        desc="git init + first commit — turns on diff, commit and PR"
      />
    );
  return (
    <label className="check">
      <input type="checkbox" id={id} checked={checked} onChange={(e) => onChange(e.target.checked)} />
      Create a git repo in this folder{" "}
      <span className="muted">(git init + first commit — turns on diff, commit and PR)</span>
    </label>
  );
}

/** "This folder directly"'s plain line (the New form swaps it for a warning
 * when picking it would turn fast-track or auto-split off). */
export const IN_PLACE_HINT = "Edits your checkout as it is, on its current branch";

/** The line under "Separate worktree" naming the branch Create will cut: ONE
 * line, ellipsised when long (the full text is its title) — a branch name
 * wrapping mid-segment onto a second line read as a layout fault. `parts`
 * alternate prose and code: [prose, code, prose, code…]. */
function BranchLine({ parts, muted }: { parts: string[]; muted?: string }) {
  const full = parts.join("") + (muted ? " · " + muted : "");
  return (
    <p className="nf-opt-sub nf-branch-line" id="new-branch-preview" title={full}>
      {parts.map((t, i) => (!t ? null : i % 2 ? <code key={i}>{t}</code> : t))}
      {muted ? <span className="muted"> · {muted}</span> : null}
    </p>
  );
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
  /** What Create will name the session and the branch it cuts (null = not
   * known yet: no repo, or the folder not checked). */
  name: PlannedName | null;
  /** The branch prefix, for the "New branch me/… from your task" line. */
  prefix: string;
  /** What it is cut from ("" = the server's pick). */
  baseLabel: string;
  /** Works ON a pushed branch ("Work on x itself"). */
  continueBranch: string;
  /** Sessions already editing this folder directly. */
  sharers: string[];
  /** Sessions already working in this repo, any way. */
  others: string[];
  /** The folder is real but has no git repo: the question becomes "make it
   * a repo first, or just run in the folder?" (see PlainWorksIn). */
  plainFolder?: boolean;
  initRepo?: boolean;
  setInitRepo?: Set<boolean>;
}

/** "Works in" for a folder with no git repo: the one question that folder
 * raises, as two radios on the form — never a pointer to a checkbox folded
 * away elsewhere, and "recommended" only on the answer that can happen. */
export function PlainWorksIn({
  initRepo,
  setInitRepo,
  setInPlace,
  laneNeedsWorktree,
  draft,
}: {
  initRepo: boolean;
  setInitRepo: Set<boolean>;
  setInPlace: Set<boolean>;
  laneNeedsWorktree: boolean;
  draft: RunDraft;
}) {
  return (
    <div id="new-works-in" className="rt-row rt-row-top nf-works-in" data-caps="git">
      <span className="rt-label" id="new-mode-label">
        Works in
      </span>
      <div className="rt-ctl nf-mode" role="radiogroup" aria-labelledby="new-mode-label">
        <label className="check nf-opt">
          <input
            type="radio"
            name="new-workspace-mode"
            id="new-git-first"
            checked={initRepo}
            onChange={() => {
              setInitRepo(true);
              setInPlace(false);
            }}
          />
          <ModeText
            title={
              <>
                Make it a git repo first <span className="nf-rec">recommended</span>
              </>
            }
            desc="Turns on diff, commit and PR — git init and a first commit here"
          />
        </label>
        <label className="check nf-opt">
          <input
            type="radio"
            name="new-workspace-mode"
            id="new-in-place"
            checked={!initRepo}
            onChange={() => {
              setInitRepo(false);
              setInPlace(true);
              // Fast-track commits, and a folder with no repo has nothing to
              // commit to.
              if (laneNeedsWorktree) draft.setLane("leave");
            }}
          />
          <ModeText
            title="Just run in the folder"
            desc={
              laneNeedsWorktree
                ? "Turns Fast-track off — there is no repo to commit to"
                : "The agent edits the files as they are; no diff, commit or PR"
            }
          />
        </label>
      </div>
    </div>
  );
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
  name,
  prefix,
  baseLabel,
  continueBranch,
  sharers,
  others,
  plainFolder,
  initRepo,
  setInitRepo,
}: WorksInProps) {
  if (plainFolder && setInitRepo)
    return (
      <PlainWorksIn
        initRepo={!!initRepo}
        setInitRepo={setInitRepo}
        setInPlace={setInPlace}
        laneNeedsWorktree={laneNeedsWorktree}
        draft={draft}
      />
    );
  const worktree = !inPlace || laneNeedsWorktree || provisionOn;
  const warn = sharedCheckoutWarning(sharers);
  // In place, the shared-checkout warning below says it louder.
  const othersLine = warn && !worktree ? "" : otherSessionsLine(others, worktree);
  return (
    <WorksInGroup
      scope="new"
      worktree={worktree}
      onWorktree={() => setInPlace(false)}
      onInPlace={() => {
        // Fast-track commits for THIS session, so it never drives a
        // checkout others share: choosing the shared folder turns it
        // off (said on the line, not hidden).
        if (laneNeedsWorktree) draft.setLane("leave");
        setSplit(false);
        setInPlace(true);
        setProvision(false);
      }}
      inPlaceDesc={
        splitOn
          ? "Turns auto-split off — a split works in its own worktree"
          : laneNeedsWorktree
          ? "Turns fast-track off (it commits for this session, so it needs its own worktree)"
          : IN_PLACE_HINT
      }
      underWorktree={
        <>
          {worktree && isGit && !worktreeClamped && (name || continueBranch) && (
            continueBranch ? (
              <BranchLine parts={["Works on ", continueBranch, " itself"]} muted="shared with whoever pushed it" />
            ) : name && name.from === "none" ? (
              // Nothing typed yet: the name comes from the task, so say that
              // rather than preview an "untitled" nobody means to make.
              <BranchLine
                parts={["New branch ", prefix + "…", " from your task" + (baseLabel ? " · cut from " : ""), baseLabel]}
              />
            ) : name ? (
              <BranchLine
                parts={["New branch ", name.branch, baseLabel ? " · cut from " : "", baseLabel]}
                muted={name.renumbered ? "“" + name.renumbered + "” is taken, so this one is numbered" : ""}
              />
            ) : null
          )}
          {name?.clash && (
            <p className="nf-opt-sub nf-git-nudge" id="new-name-clash" role="status">
              A session called “{name.title}” already exists — give this one another name under More
              options, or clear it to name it from the task.
            </p>
          )}
          {othersLine && (
            <p className="nf-opt-sub nf-branch-line nf-others" id="new-others" role="status">
              {othersLine}
            </p>
          )}
          {worktree && worktreeClamped && <p className="nf-opt-sub nf-git-nudge">{worktreeClamped}</p>}
        </>
      }
      underInPlace={
        warn && !worktree ? (
          <p className="nf-opt-sub nf-git-nudge nf-share-warn" id="new-share-warn" role="status">
            {warn}
          </p>
        ) : null
      }
    />
  );
}

export interface WorksInGroupProps {
  /** Id prefix: "new" (the New form) or "tpl" (a template's editor). */
  scope: string;
  /** "Separate worktree" is the picked answer. */
  worktree: boolean;
  onWorktree(): void;
  onInPlace(): void;
  /** "This folder directly"'s muted line: IN_PLACE_HINT, or what picking it
   * turns off. */
  inPlaceDesc: ReactNode;
  /** Lines hung under "Separate worktree" (.nf-opt-sub): the branch it cuts,
   * the provisioning box. */
  underWorktree?: ReactNode;
  /** Lines hung under "This folder directly" (a shared-checkout warning). */
  underInPlace?: ReactNode;
  /** What follows both answers and is not a third one (git init). */
  after?: ReactNode;
}

/** The "Works in" row itself — its label in the form's label column, the two
 * structured answers beside it — drawn by the New form (WorksIn) and a
 * template's editor alike, so the two can never describe the choice in other
 * words or lay it out another way. What hangs under each answer is the
 * caller's. */
export function WorksInGroup({
  scope,
  worktree,
  onWorktree,
  onInPlace,
  inPlaceDesc,
  underWorktree,
  underInPlace,
  after,
}: WorksInGroupProps) {
  return (
    <div id={scope + "-works-in"} className="rt-row rt-row-top nf-works-in" data-caps="git">
      <span className="rt-label" id={scope + "-mode-label"}>
        Works in
      </span>
      <div className="rt-ctl nf-mode" role="radiogroup" aria-labelledby={scope + "-mode-label"}>
        <label className="check nf-opt">
          <input
            type="radio"
            name={scope + "-workspace-mode"}
            id={scope + "-worktree"}
            checked={worktree}
            onChange={onWorktree}
          />
          <WorktreeText />
        </label>
        {underWorktree}
        <label className="check nf-opt">
          <input
            type="radio"
            name={scope + "-workspace-mode"}
            id={scope + "-in-place"}
            checked={!worktree}
            onChange={onInPlace}
          />
          <ModeText title="This folder directly" desc={inPlaceDesc} />
        </label>
        {underInPlace}
        {after}
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
  /** Works in already asks "Make it a git repo first?" for this folder. */
  plainFolder?: boolean;
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
  plainFolder = false,
}: GitOptionsProps) {
  return (
    <div className="nf-git-opts" data-caps="git">
      {offerProvision && (
        /* Provisioning is the separate worktree PLUS the repo's setup — a
           modifier of that answer, so a box under it rather than a third
           answer to "works in". Ticking it picks the worktree. */
        <ProvisionCheck
          id="new-provision"
          rowId="new-provision-row"
          checked={provisionOn}
          onChange={(on) => {
            setProvision(on);
            if (on) setInPlace(false);
          }}
        />
      )}
      {provisionBlocked && (
        <p className="nf-git-nudge nf-provision-warn">
          {provisionBlocked}{" "}
          <button
            type="button"
            className="linklike"
            title="Picks “Make it a git repo first”"
            onClick={armInitRepo}
          >
            Create one here
          </button>
        </p>
      )}
      {offerProvision && provision && (
        <div id="provision-opts">
          <StrategyPicker id="new-workspace-strategy" value={strategy} onChange={setStrategy} />
          <p className="muted provision-hint">
            Tip: paste a full branch in <b>Name</b> (e.g.{" "}
            <code>feature/sc-17436/grafana-dashboard-…</code>) to use it as the branch
            verbatim — the session name becomes its last segment.
          </p>
        </div>
      )}

      {worktreeMode && isGit && branches === null && (
        /* The menu is still on its way: the row is drawn now, so More
           options shows what its summary promises from the first open. */
        <label className="nf-base-branch">
          Base branch{" "}
          <span className="muted">— the worktree is cut from it and diffed against it</span>
          <select id="new-base-branch" disabled value="">
            <option value="">Loading branches…</option>
          </select>
        </label>
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
      {!plainFolder && (!isGit || initRepo) && (
        <InitRepoCheck id="new-init-repo" checked={initRepo} onChange={setInitRepo} />
      )}
    </div>
  );
}
