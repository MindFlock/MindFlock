/** The Verify dialog's furniture around the checklists: the grouped list, the
 * master switch, the Sources it watches, the by-hand writer and the empty
 * state — Intake's anatomy, in Intake's order (see VerifyDialog.tsx).
 *
 * Split out of VerifyDialog.tsx; a checklist row itself is VerifyPlan.tsx. */

import { useState } from "react";
import type { TestPlan } from "../../api/types";
import { writePlanFor } from "../../lib/verifyActions";
import { AutomationSwitch, WorkGroup, useToggleSet } from "../intake/kit";
import { RepoSourceList, type RepoOverrides } from "../intake/RepoSources";
import { useSettings } from "../settings/useSettings";
import type { RowSelection } from "./rowSelect";
import { groupPlans } from "./verify";
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

/** The grouped list.
 *
 * Its own component so the two toggle sets are one hook pair rather than one
 * per plan, and so the dialog shell above stays about the shell. */
export function PlanList({
  plans,
  liveBranch,
  selection,
}: {
  plans: TestPlan[];
  liveBranch: string;
  /** Absent when nothing can be selected (there is no such caller today, but a
   * row that cannot be picked must not render a box that does nothing). */
  selection?: RowSelection;
}) {
  const groups = useToggleSet(GROUPS_KEY, true);
  // Plans default CLOSED and the set holds the ones you opened — one polarity,
  // unlike the XOR this replaced, where membership meant "toggled away from
  // whatever my group implies" and a plan you had deliberately collapsed
  // re-opened itself when it changed group.
  const opened = useToggleSet(PLANS_KEY);

  return (
    <>
      {groupPlans(plans).map((g) => (
        <WorkGroup
          key={g.key}
          heading
          name={g.label}
          count={g.plans.length}
          detail={g.detail}
          open={groups.isOpen(g.key)}
          onToggle={() => groups.toggle(g.key)}
        >
          {g.plans.map((plan) => (
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
            />
          ))}
        </WorkGroup>
      ))}
    </>
  );
}

/** "Write a plan for…" — the button, and the normal way a plan comes to exist.
 *
 * Automatic generation is opt-in per repo (the Repositories list beside it, or
 * the repo's own committed `.mindflock.toml`) and off everywhere else, because
 * writing a plan costs a real model call and a Verify list that fills itself
 * destroys the one number this surface exists to show. That leaves a
 * chicken-and-egg problem this bar is the answer to: nobody opts a repo in to
 * something they have never watched work, so there has to be a way to ask for
 * exactly one plan, by name, with nothing configured.
 *
 * Only sessions with a branch and no plan yet are offered (`planTargets` picks
 * them). It renders even when that list is empty — disabled, saying why —
 * because a control that disappears makes the dialog a different shape on
 * different visits, and the empty state then has to carry a second variant
 * purely to avoid pointing at furniture that is not on the screen. */
function NewPlanBar({
  candidates,
  closed,
  reason,
}: {
  candidates: string[];
  /** Which candidates are closed sessions — labelled, because "this will not
   * open a window" is worth knowing before pressing the button. */
  closed: Set<string>;
  reason: string;
}) {
  const [busy, setBusy] = useState("");
  const [picked, setPicked] = useState("");

  // The picker holds a stale title once that session gets a plan (it leaves
  // `candidates` but stays in state), so resolve against the live list rather
  // than trusting it.
  const target = candidates.includes(picked) ? picked : candidates[0] || "";

  // "Writing…" while the request is out; what the press then says (written,
  // or already there under which name) is `writePlanFor`'s.
  async function write() {
    setBusy(target);
    try {
      await writePlanFor(target);
    } finally {
      setBusy("");
    }
  }

  return (
    <div className="vf-newplan">
      <label htmlFor="vf-newplan-pick">Write a checklist for</label>
      <select
        id="vf-newplan-pick"
        value={target}
        disabled={!!busy || !candidates.length}
        onChange={(e) => setPicked(e.target.value)}
      >
        {/* An empty select when there is nothing to offer, rather than an
            <option> carrying a sentence. A disabled dropdown whose only entry is
            a paragraph reads as a broken list of one; the reason belongs in the
            hint below, which is where this control already explains itself. */}
        {candidates.map((t) => (
          <option key={t} value={t}>
            {t}
            {closed.has(t) ? " (closed)" : ""}
          </option>
        ))}
      </select>
      <button
        type="button"
        id="vf-newplan-go"
        className="test-btn"
        disabled={!!busy || !candidates.length}
        onClick={() => void write()}
      >
        {busy ? (
          <>
            <span className="btn-spin" aria-hidden="true" /> Writing…
          </>
        ) : (
          "Write it"
        )}
      </button>
      {/* Why you cannot, when you cannot — and it is three different reasons.
          One sentence covered all of them and was simply false for the commonest
          one: telling somebody with five open sessions to start a session. */}
      <span className="set-hint">
        {reason || "Reads the branch's diff and writes a checklist of what to check."}
      </span>
    </div>
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
 * code, and is OR'd with this list; neither can switch the other off. The
 * footnote below says so, because a user whose repo cannot appear here needs to
 * be told where else to look rather than left clicking Add.
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
      emptyText="No repositories yet — add one and every session branch pushed in it gets a checklist."
      // Trimmed to the length of Intake's, which is the length a hint under an
      // always-visible section can be before it outweighs the cards above it.
      // The committed-opt-in story it used to carry in full is in web-ui.md and
      // configuration.md now; what survives is the pointer, because a user whose
      // repo has no GitHub slug has to be told this list is not their door.
      hint={
        <>
          Each card is one GitHub repository, matched to your checkouts by their{" "}
          <code>origin</code>, with its own live branch. Being on this list is the opt-in:
          the first push of a session branch in these repos gets a checklist. A repo with
          no GitHub remote opts in with <code>[workspace] verify_on_push = true</code> in
          its own <code>.mindflock.toml</code> instead.
        </>
      }
    />
  );
}

/** The master switch — the one piece of configuration that stays above the work.
 *
 * "Is this thing on?" must be answerable without opening anything: it is the
 * first question asked of a paused feature and the answer to "why is nothing
 * appearing?", so it costs its own band at the top. Everything else that is
 * configuration is :func:`VerifySources` below it and :func:`VerifyByHand`
 * under the work, which is where Intake keeps the same two things. */
export function VerifySwitch() {
  const s = useSettings();
  const repo = (s.settings.repository || {}) as {
    verify_repos?: string[];
    verify_enabled?: boolean;
  };
  const n = Array.isArray(repo.verify_repos) ? repo.verify_repos.length : 0;
  // Absent => on, matching the backend default: the real gate is membership, so
  // an older settings file with no key behaves exactly as it did.
  const on = repo.verify_enabled !== false;

  return (
    <AutomationSwitch
      label="Check what ships, automatically"
      title="Turn automatic checking on or off — your repositories, checklists and answers are kept either way"
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
      // The one thing a switch genuinely cannot say for itself: it is ON and it
      // is watching nothing, which is the state every new install starts in and
      // the reason the list below is empty. Never shown to narrate on/off.
      note={
        n
          ? undefined
          : "Add a repository below and every session branch pushed in it gets a checklist"
      }
    />
  );
}

/** The by-hand writer, in the fold Intake keeps its once-a-month settings in.
 *
 * `.pr-advanced` is REUSED rather than re-cut, because it is the same kind of
 * thing in the same place: a `<details>` after the work list holding what you
 * touch rarely (PullRequestsTab.tsx / IssuesTab.tsx put theirs at the bottom of
 * the tab, and the rules live in screens.css). Borrowing the class means the two
 * surfaces cannot drift apart by a padding value.
 *
 * The sources above are how a checklist normally comes to exist. This is the
 * escape hatch, and it earns its place for one reason: nobody opts a repository
 * in to something they have never watched work, so there has to be a way to ask
 * for exactly one checklist, by name, with nothing configured. That is a
 * once-per-lifetime need, which is precisely what a fold is for.
 */
export function VerifyByHand({
  candidates,
  closed,
  reason,
}: {
  candidates: string[];
  /** Which candidates are closed sessions, so the picker can say so. */
  closed: Set<string>;
  /** Why `candidates` is empty, from `noTargetsReason`; "" when it isn't. */
  reason: string;
}) {
  return (
    <details className="pr-advanced">
      <summary>Write a checklist by hand</summary>
      <div className="pr-advanced-body">
        <p className="set-hint set-block-hint">
          Reads a session branch's diff and writes a checklist for it now, without
          tracking its repository. Useful once, to see what the checklists look like
          before opting a repository in. Sessions you have already{" "}
          <strong>closed</strong> are offered too — asking for a checklist is
          something you do when the work is finished and the window has been put
          away, so refusing there made the button useless at the one moment you
          want it.
        </p>
        <NewPlanBar candidates={candidates} closed={closed} reason={reason} />
      </div>
    </details>
  );
}

/** The first thing anybody sees, and for a while the only thing.
 *
 * Not "nothing here": a surface nobody has fed yet looks broken, and the honest
 * answer is that nothing has shipped. What follows is the chain — which is
 * stated NOWHERE else in the product, in the UI or in the docs — because every
 * question a new user has about Verify is really "what has to happen before a
 * row appears here?", and it is three things in a fixed order.
 *
 * It points UP at Sources rather than carrying its own button. It used to have
 * one, because the repository list was behind a collapsed fold and a direction
 * to a fold is a direction to something the reader cannot see; now the list is
 * an always-visible section a few lines above, exactly as it is on the Intake
 * tabs, so naming it is enough and a second Add control would be two doors onto
 * one room. */
export function VerifyEmpty({ liveBranch }: { liveBranch: string }) {
  return (
    <div className="repo-empty vf-empty">
      <p>
        <strong>Nothing has shipped that needs checking yet.</strong>
      </p>
      <ol className="vf-howto">
        <li>
          Add a repository under <strong>Sources</strong> above — every session branch
          pushed in it gets a checklist.
        </li>
        <li>The checklist waits while the branch is reviewed and merged.</li>
        <li>
          When that work reaches <code>{liveBranch || "the live branch"}</code>, it turns up
          here for you to check.
        </li>
      </ol>
    </div>
  );
}
