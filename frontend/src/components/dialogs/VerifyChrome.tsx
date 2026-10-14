/** The Verify dialog's furniture around the checklists: the grouped list, the
 * master switch, the Sources it watches, the by-hand writer and the empty
 * state — Intake's anatomy, in Intake's order (see VerifyDialog.tsx).
 *
 * Split out of VerifyDialog.tsx; a checklist row itself is VerifyPlan.tsx. */

import { useEffect, useMemo, useRef, useState } from "react";
import type { TestPlan } from "../../api/types";
import { copyText } from "../../lib/clipboard";
import { toast } from "../../lib/toast";
import {
  fetchVerifyRepos,
  type VerifyRepo,
  writePlanFor,
  writePlanForBranch,
} from "../../lib/verifyActions";
import { AutomationSwitch, WorkGroup, useToggleSet } from "../intake/kit";
import { RepoSourceList, type RepoOverrides } from "../intake/RepoSources";
import { useSettings } from "../settings/useSettings";
import type { RowSelection } from "./rowSelect";
import {
  type ChecklistTarget,
  GROUP_KEYS,
  groupLabel,
  groupPlans,
  planName,
} from "./verify";
import { PlanRow } from "./VerifyPlan";

/** Per-device UI state, same reasoning as Intake's group toggles (kit.tsx):
 * "I collapsed Checked on this laptop" is not a property of the flock.
 *
 * Both sets store the EXCEPTIONS, so a fresh install writes nothing at all.
 * Groups default OPEN and plans default CLOSED, and each set holds whatever was
 * toggled away from that — one polarity per set, unlike the arrangement this
 * replaced, where a plan's saved gesture was XOR'd against whether its group was
 * urgent, so a plan you deliberately collapsed re-opened itself the moment it
 * changed group. The keys and the stored format are unchanged, so nobody's saved
 * state resets; a legacy entry simply reads as "I opened this". */
const GROUPS_KEY = "mf_verify_groups";
const PLANS_KEY = "mf_verify_plans";

/** How long "moved to Checked" stays where the checklist was. */
const MOVED_NOTICE_MS = 8000;

/** A request to bring one checklist into view — from a deep link
 * (`openDialogFor("verify", <branch>)`), from "Write steps yourself", or from a
 * "moved to …" notice. `nonce` changes per request. */
export interface PlanReveal {
  planId: string;
  nonce: number;
  /** Also open its Add a step form and put the caret in it. */
  addStep?: boolean;
}

interface MovedNotice {
  id: string;
  name: string;
  /** The group it LEFT — the notice is drawn where the reader was looking. */
  from: string;
  to: string;
}

/** The grouped list.
 *
 * Its own component so the two toggle sets are one hook pair rather than one
 * per plan, and so the dialog shell above stays about the shell. */
export function PlanList({
  plans,
  liveBranch,
  selection,
  reveal,
}: {
  plans: TestPlan[];
  liveBranch: string;
  /** Absent when nothing can be selected (there is no such caller today, but a
   * row that cannot be picked must not render a box that does nothing). */
  selection?: RowSelection;
  reveal?: PlanReveal | null;
}) {
  const groups = useToggleSet(GROUPS_KEY, true);
  // Plans default CLOSED and the set holds the ones you opened — one polarity,
  // unlike the XOR this replaced, where membership meant "toggled away from
  // whatever my group implies" and a plan you had deliberately collapsed
  // re-opened itself when it changed group.
  const opened = useToggleSet(PLANS_KEY);
  const grouped = useMemo(() => groupPlans(plans), [plans]);
  const [localReveal, setLocalReveal] = useState<PlanReveal | null>(null);
  const shownReveal =
    localReveal && (!reveal || localReveal.nonce > reveal.nonce) ? localReveal : reveal || null;

  // A REVEAL OPENS WHAT IT POINTS AT: the checklist itself, and the group it
  // sits in, so "Checklist" on a session row lands on the steps, not on a
  // collapsed heading.
  useEffect(() => {
    if (!shownReveal) return;
    const g = grouped.find((x) => x.plans.some((p) => p.id === shownReveal.planId));
    if (g && !groups.isOpen(g.key)) groups.toggle(g.key);
    if (!opened.isOpen(shownReveal.planId)) opened.toggle(shownReveal.planId);
    // Only on a NEW request — toggling is not idempotent.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [shownReveal?.nonce]);

  // A FINISHED CHECKLIST DOESN'T JUST VANISH. Answering the last step re-files
  // the plan under another heading — usually far down the list — and the row you
  // were working in disappeared from under the pointer. A notice stays where it
  // was, says where it went, and offers to follow it.
  const lastGroup = useRef(new Map<string, string>());
  const [moved, setMoved] = useState<MovedNotice[]>([]);
  useEffect(() => {
    const next = new Map<string, string>();
    const arrivals: MovedNotice[] = [];
    for (const g of grouped) {
      for (const p of g.plans) {
        next.set(p.id, g.key);
        const was = lastGroup.current.get(p.id);
        if (was && was !== g.key && opened.isOpen(p.id))
          arrivals.push({ id: p.id, name: planName(p), from: was, to: g.key });
      }
    }
    lastGroup.current = next;
    if (arrivals.length)
      setMoved((prev) => [...prev.filter((m) => !arrivals.some((a) => a.id === m.id)), ...arrivals]);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [grouped]);
  useEffect(() => {
    if (!moved.length) return;
    const id = window.setTimeout(() => setMoved((prev) => prev.slice(1)), MOVED_NOTICE_MS);
    return () => window.clearTimeout(id);
  }, [moved]);

  const present = new Set(grouped.map((g) => g.key));
  const keys = GROUP_KEYS.filter((k) => present.has(k) || moved.some((m) => m.from === k));

  return (
    <>
      {keys.map((key) => {
        const g = grouped.find((x) => x.key === key);
        const here = moved.filter((m) => m.from === key);
        return (
          <WorkGroup
            key={key}
            heading
            name={g?.label || groupLabel(key)}
            count={g?.plans.length || 0}
            detail={g?.detail}
            open={groups.isOpen(key)}
            onToggle={() => groups.toggle(key)}
          >
            {here.map((m) => (
              <div key={"moved:" + m.id} className="vf-moved" role="status" data-to={m.to}>
                <span>
                  {m.to === "done" ? "\u2713 " : m.to === "fail" ? "\u2717 " : ""}
                  <strong>{m.name}</strong> moved to {groupLabel(m.to)}.
                </span>
                <button
                  type="button"
                  className="test-btn"
                  onClick={() => {
                    setMoved((prev) => prev.filter((x) => x.id !== m.id));
                    setLocalReveal({ planId: m.id, nonce: Date.now() });
                  }}
                >
                  Show
                </button>
                <button
                  type="button"
                  className="vf-moved-x"
                  aria-label="Dismiss"
                  onClick={() => setMoved((prev) => prev.filter((x) => x.id !== m.id))}
                >
                  ×
                </button>
              </div>
            ))}
            {(g?.plans || []).map((plan) => (
              <PlanRow
                key={plan.id}
                plan={plan}
                liveBranch={liveBranch}
                selection={selection}
                expanded={opened.isOpen(plan.id)}
                onToggle={() => opened.toggle(plan.id)}
                onExpand={() => {
                  if (!opened.isOpen(plan.id)) opened.toggle(plan.id);
                }}
                reveal={
                  shownReveal && shownReveal.planId === plan.id
                    ? { nonce: shownReveal.nonce, addStep: shownReveal.addStep }
                    : undefined
                }
              />
            ))}
          </WorkGroup>
        );
      })}
    </>
  );
}

/** "Ask the agent for a checklist" and "Write steps yourself" — the two ways a
 * checklist comes to exist when nobody configured a repository.
 *
 * It used to be one fold called "Write a checklist by hand" whose body asked the
 * MODEL to write one, while the real by-hand control ("Add a step") was inside
 * each checklist. Now the fold says both, one row each, and each does what its
 * label says.
 *
 * The picker lists EVERY session with a branch, open or closed. One that can't
 * be picked stays listed, greyed, saying why ("has a checklist", "on main —
 * nothing to compare"): the old picker dropped them silently under help text
 * promising closed sessions were offered.
 *
 * Its last entry, **Another branch…**, is for work no session of yours has
 * been on (a teammate's branch): pick one of the repos you've worked in and
 * type the branch. */
export const ANOTHER_BRANCH = "__another_branch__";

export function VerifyNewChecklist({
  targets,
  reason,
  plans,
  preselect,
  open,
  onToggle,
  onReveal,
}: {
  targets: ChecklistTarget[];
  /** Why there is nothing to pick at all, from `noTargetsReason`; "" when there is. */
  reason: string;
  plans: TestPlan[];
  /** A session title to select (a deep link for a branch with no checklist). */
  preselect?: string;
  open: boolean;
  onToggle(open: boolean): void;
  onReveal(planId: string, addStep: boolean): void;
}) {
  const [busy, setBusy] = useState(false);
  const [picked, setPicked] = useState("");
  const [pickedPlan, setPickedPlan] = useState("");
  const pickable = targets.filter((t) => !t.reason);
  // With no session to pick, Another branch… is the only way in.
  const other = picked === ANOTHER_BRANCH || !pickable.length;
  const [repos, setRepos] = useState<VerifyRepo[] | null>(null);
  const [repoRoot, setRepoRoot] = useState("");
  const [branch, setBranch] = useState("");
  const repo = repos?.find((r) => r.root === repoRoot) || repos?.[0];

  // Fetched only once Another branch… is chosen: most people never open it.
  useEffect(() => {
    if (!other || repos !== null) return;
    let live = true;
    fetchVerifyRepos().then(
      (list) => live && setRepos(list),
      () => live && setRepos([])
    );
    return () => {
      live = false;
    };
  }, [other, repos]);

  useEffect(() => {
    if (preselect) setPicked(preselect);
  }, [preselect]);

  // A pick that stopped being pickable (it just got its checklist) falls back
  // to the first one that still can be — never to a greyed entry.
  const target = pickable.some((t) => t.title === picked)
    ? picked
    : pickable[0]?.title || "";
  const planTarget = plans.some((p) => p.id === pickedPlan) ? pickedPlan : plans[0]?.id || "";

  async function ask() {
    if (other ? !repo || !branch.trim() : !target) return;
    setBusy(true);
    try {
      if (!other) await writePlanFor(target);
      else if (await writePlanForBranch(repo!, branch.trim())) setBranch("");
    } finally {
      setBusy(false);
    }
  }

  return (
    <details
      className="pr-advanced vf-new"
      open={open}
      onToggle={(e) => onToggle((e.currentTarget as HTMLDetailsElement).open)}
    >
      <summary>New checklist</summary>
      <div className="pr-advanced-body">
        <div className="vf-newplan" id="vf-ask">
          <label htmlFor="vf-newplan-pick">Ask the agent for a checklist</label>
          <select
            id="vf-newplan-pick"
            value={other ? ANOTHER_BRANCH : target}
            disabled={busy}
            onChange={(e) => setPicked(e.target.value)}
          >
            {targets.map((t) => (
              <option key={t.title} value={t.title} disabled={!!t.reason}>
                {t.branch}
                {t.title !== t.branch ? " — " + t.title : ""}
                {t.closed ? " (closed)" : ""}
                {t.reason ? " · " + t.reason : ""}
              </option>
            ))}
            <option value={ANOTHER_BRANCH}>Another branch…</option>
          </select>
          {other && (
            <div className="vf-newplan-branch">
              <select
                id="vf-newplan-repo"
                aria-label="Repo"
                value={repo?.root || ""}
                disabled={busy || !repos?.length}
                onChange={(e) => setRepoRoot(e.target.value)}
              >
                {repos === null ? (
                  <option value="">Loading repos…</option>
                ) : !repos.length ? (
                  <option value="">No repos yet</option>
                ) : (
                  repos.map((r) => (
                    <option key={r.root} value={r.root} title={r.root}>
                      {r.name}
                    </option>
                  ))
                )}
              </select>
              <input
                id="vf-newplan-branch"
                type="text"
                aria-label="Branch"
                placeholder="branch, e.g. feature/login"
                value={branch}
                disabled={busy}
                onChange={(e) => setBranch(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") void ask();
                }}
              />
            </div>
          )}
          <button
            type="button"
            id="vf-newplan-go"
            className="test-btn"
            disabled={busy || (other ? !repo || !branch.trim() : !target)}
            onClick={() => void ask()}
          >
            {busy ? (
              <>
                <span className="btn-spin" aria-hidden="true" /> Asking…
              </>
            ) : (
              "Ask the agent"
            )}
          </button>
          <span className="set-hint">
            {other
              ? repos && !repos.length
                ? reason || "No repos yet — open a session in one first."
                : "For a branch no session has been on — the agent reads its changes in that repo."
              : reason ||
                "The agent reads the branch's changes and writes the steps — about three minutes. Closed sessions are listed too."}
          </span>
        </div>
        <div className="vf-newplan" id="vf-write">
          <label htmlFor="vf-write-pick">Write steps yourself</label>
          <select
            id="vf-write-pick"
            value={planTarget}
            disabled={!plans.length}
            onChange={(e) => setPickedPlan(e.target.value)}
          >
            {plans.map((p) => (
              <option key={p.id} value={p.id}>
                {p.branch ? p.branch + " — " : ""}
                {planName(p)}
              </option>
            ))}
          </select>
          <button
            type="button"
            id="vf-write-go"
            className="test-btn"
            disabled={!planTarget}
            onClick={() => onReveal(planTarget, true)}
          >
            Add a step
          </button>
          <span className="set-hint">
            {plans.length
              ? "Opens the checklist with its Add a step form."
              : "Steps are added to a checklist — ask the agent for one first."}
          </span>
        </div>
      </div>
    </details>
  );
}

/** The repositories Verify tracks, and what each of them calls live.
 *
 * The other half of the bargain the bar above makes. Writing a plan costs a real
 * model call, so nothing happens in a repo nobody named; adding one here is how
 * you say this repo has earned it — and where you say what "live" even means for
 * it — without leaving the dialog and without learning a config file exists.
 *
 * Membership IS the opt-in. There is no "automatic" checkbox any more: a repo on
 * this list gets a plan on every push of a session branch, exactly as a repo in
 * `github.repos` is watched by virtue of being there. A checkbox next to a list
 * you had to add the repo to anyway was two decisions for one intent, and the
 * off state was indistinguishable from having removed it.
 *
 * This is the SAME component the Intake tabs use (intake/RepoSources.tsx), on a
 * third `surface`, and that is the point. A repo is named the way GitHub names
 * it — you type `owner/name` — because that is how every other repo list in this
 * app is spelled, because it is stable across machines and clones, and because
 * the alternative this replaced (discovering local checkouts from whichever
 * sessions happened to be open) meant the list changed under you when you closed
 * a session, and keyed a machine-local absolute path that meant nothing to
 * anybody else in the flock.
 *
 * The live branch is per repo rather than one flock-wide setting because "what
 * counts as shipped" is a per-repo fact: `main` in one repo, `staging` in the
 * next, `release` in the third. One global answer was wrong the moment you
 * worked in two repos, and a plan measured against the wrong branch is worse
 * than no plan — it goes due when nothing shipped, or never goes due at all. A
 * blank field inherits, and its placeholder shows what it inherits, so "empty"
 * reads as "inherits staging" rather than as "nothing set".
 *
 * A checkout with no GitHub origin cannot be named here at all — there is no
 * slug to type. Its opt-in is the repo's own committed `.mindflock.toml`
 * (`[workspace] verify_on_push = true`), which is path-based, travels with the
 * code, and is OR'd with this list; neither can switch the other off. There is
 * no setting the dialog could write for it, so the fold under the list hands
 * over the snippet to commit — copyable, with one sentence saying what it does —
 * instead of a config key in the middle of a hint.
 */
export function VerifySources({
  liveBranch,
  deployDelay,
}: {
  liveBranch: string;
  /** The flock-wide deploy wait, in minutes — the cards' placeholder. */
  deployDelay: string;
}) {
  const s = useSettings();
  const repo = (s.settings.repository || {}) as {
    verify_repos?: string[];
    verify_repo_settings?: RepoOverrides;
  };
  const repos = Array.isArray(repo.verify_repos) ? repo.verify_repos : [];
  const overrides = (repo.verify_repo_settings || {}) as RepoOverrides;

  return (
    <>
    <RepoSourceList
      surface="verify"
      // "Sources", the word the two Intake tabs' equivalent block is called in
      // the code and the word this list actually is: where checklists come
      // from. It renders through `#verify-body > .set-row > .set-label`, which
      // is a re-anchored copy of Intake's own rule, so the heading above these
      // cards is the same uppercase rule as CHECKLISTS below them.
      label="Sources"
      repos={repos}
      overrides={overrides}
      // ONE save for both halves, as the Intake tabs do it: the list and the
      // per-repo blocks are one edit as far as the user is concerned, and
      // two POSTs would let a rename land without its settings — which is a
      // repo that silently came back with its live branch reset.
      onSave={(list, next, msg) =>
        s.saveGroup("repository", { verify_repos: list, verify_repo_settings: next }, msg)
      }
      // Only `liveBranch` means anything on this surface; the rest are the
      // Intake fields a verify card does not render (see `surface` there).
      defaults={{
        agent: "",
        baseBranch: "",
        minAge: "",
        skipAuthors: "",
        liveBranch,
        deployDelay,
      }}
      listId="vf-repos-list"
      addId="vf-repo-add-btn"
      addLabel="+ Add repository"
      emptyText="No repositories yet."
      hint="Each new branch pushed in these GitHub repositories gets a checklist."
    />
    <NoRemoteOptIn />
    </>
  );
}

/** The opt-in for a repo with no GitHub remote: a file to commit. */
const OPT_IN_SNIPPET = "[workspace]\nverify_on_push = true\n";

function NoRemoteOptIn() {
  return (
    <details className="pr-advanced vf-optin">
      <summary>Repository without a GitHub remote?</summary>
      <div className="pr-advanced-body">
        <p className="set-hint">
          Commit this as <code>.mindflock.toml</code> at the repository's root — each new
          branch pushed there then gets a checklist.
        </p>
        <div className="vf-snippet">
          <pre>{OPT_IN_SNIPPET}</pre>
          <button
            type="button"
            className="test-btn"
            onClick={() =>
              void copyText(OPT_IN_SNIPPET).then((ok) =>
                toast(ok ? "Copied" : "Couldn't copy — select the text instead"),
              )
            }
          >
            Copy
          </button>
        </div>
      </div>
    </details>
  );
}

/** The master switch — the one piece of configuration that stays above the work.
 *
 * "Is this thing on?" must be answerable without opening anything: it is the
 * first question asked of a paused feature and the answer to "why is nothing
 * appearing?", so it costs its own band at the top. Everything else that is
 * configuration is :func:`VerifySources` below it and :func:`VerifyByHand`
 * under the work, which is where Intake keeps the same two things. */
export function VerifySwitch({ hasPlans }: { hasPlans: boolean }) {
  const s = useSettings();
  const repo = (s.settings.repository || {}) as {
    verify_repos?: string[];
    verify_enabled?: boolean;
  };
  const n = Array.isArray(repo.verify_repos) ? repo.verify_repos.length : 0;
  // Absent => on, matching the backend default: the real gate is membership, so
  // an older settings file with no key behaves exactly as it did.
  const on = repo.verify_enabled !== false;
  // NOTHING TO SWITCH YET. A fresh install showed this ON above an empty list,
  // and nobody could tell what it governed. Until there is a repository or a
  // checklist it governs nothing, and the empty state already says what to do.
  if (!n && !hasPlans) return null;

  return (
    <AutomationSwitch
      label="Check what ships, automatically"
      title="Pause or resume new checklists on push — nothing is deleted"
      rowId="vf-toggle-row"
      inputId="vf-enabled"
      statusId="vf-status"
      checked={on}
      onChange={(next) =>
        s.saveGroup(
          "repository",
          { verify_enabled: next },
          next ? "Automatic checking on" : "Automatic checking paused"
        )
      }
      // No "On — no repository to watch yet": with no repository listed this
      // only shows because checklists exist, and those came from somewhere (a
      // repo's own .mindflock.toml), so the note contradicted the list below.
    />
  );
}

/** The empty list: what to do to get a checklist. The two moments (before /
 * after merge) are in the intro right above it, so they are not repeated. */
export function VerifyEmpty() {
  return (
    <div className="repo-empty vf-empty">
      <p>
        <strong>No checklists yet.</strong>
      </p>
      <p>
        Add a repository under <strong>Sources</strong>, or ask for one under{" "}
        <strong>New checklist</strong> below.
      </p>
    </div>
  );
}

/** The two moments, one plain line each — the whole model of the surface. */
export function VerifyMoments({
  liveBranch,
  overridden = false,
}: {
  liveBranch: string;
  /** Some repo ships from another branch (see `liveBranchOverridden`). */
  overridden?: boolean;
}) {
  return (
    <ul className="vf-moments">
      <li>
        <strong>Before merge</strong> — press <strong>Check now</strong> to check the branch.
        Every step passed reads <strong>Verified on branch</strong>.
      </li>
      <li>
        <strong>After merge</strong> — it comes back here to be checked again on{" "}
        <code>{liveBranch || "the branch you ship from"}</code>
        {overridden ? " (some repos ship from another branch — each checklist says which)" : ""}.
      </li>
    </ul>
  );
}
