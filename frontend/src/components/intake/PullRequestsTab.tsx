/** Intake → Pull requests: MindFlock watches your own open PRs and, when a
 * reviewer comments, starts a session that addresses the comments. It does not
 * review other people's PRs — every word on the tab says "address comments",
 * never "review", because "Automated review" read as the opposite feature.
 *
 * Off until switched on, like Issues. Saved settings from before that default
 * keep their meaning (unset with repos = on — see pause.ts `prsOn`); a fresh
 * setup's first repository is saved with an explicit off.
 *
 * Same anatomy as the other two tabs (see ./kit.tsx): master switch, one card
 * per watched repository, then every open PR grouped by the repository it came
 * from with a chip saying why auto review has or hasn't taken it.
 *
 * The GitHub credential is NOT here any more: it has one home, the Connect
 * GitHub dialog (Settings → Connections → GitHub → Connect), and this tab only
 * offers the button that opens it. */

import { useEffect, useState } from "react";
import { api } from "../../api/client";
import { refreshInstances, usePanelQuery, useTicketingSources } from "../../state/queries";
import { SettingField, useSettings } from "../settings/useSettings";
import { AgentPicker, useAgentChoices } from "../settings/screens/AgentPicker";
import { openConnectGitHub } from "../../lib/githubConnect";
import {
  AutomationSwitch,
  useListFilter,
  WorkGroup,
  WorkItemRow,
  WorkListPanel,
  ageText,
  panelNote,
  reopenIntakeItem,
  useToggleSet,
  type ItemWorkspace,
} from "./kit";
import { RepoSourceList, type RepoOverrides } from "./RepoSources";
import { prsOn, useIntakePause } from "./pause";
import { prMatches } from "./search";
import type { TabProps } from "./IntakeDialog";

interface OpenPr {
  repo?: string;
  number: number;
  title?: string;
  url?: string;
  author?: string;
  head_ref?: string;
  base_ref?: string;
  created_at?: string;
  has_session?: boolean;
  eligible?: boolean;
  reasons?: string[];
  /** Present when an earlier review of this PR still has its clone here. */
  workspace?: ItemWorkspace;
}

/** Per-device: which repo groups are collapsed (membership = collapsed, so a
 * fresh install writes nothing and every group starts open). */
const PR_GROUPS_KEY = "mf_intake_pr_groups";

export function PullRequestsTab({ gotoTab }: TabProps) {
  const s = useSettings();
  const agentChoices = useAgentChoices();
  const groups = useToggleSet(PR_GROUPS_KEY, true);
  const filter = useListFilter(
    "gh-prs-filter",
    "Filter by repo, number, title, author, branch, or why…  ( Ctrl+F )",
  );
  const gh = (s.settings.github || {}) as {
    enabled?: boolean;
    agent?: string;
    repos?: string[];
    repo_settings?: RepoOverrides;
    base_branch?: string;
    min_age_minutes?: number | string;
    skip_authors?: string[] | string;
  };
  const repos = Array.isArray(gh.repos) ? gh.repos : [];
  // Explicit true, or unset with repos already watched (settings from before
  // the off-by-default change); explicit false => off.
  const enabled = prsOn(gh);
  // Off because Pause intake turned it off (it will come back on Resume).
  const pause = useIntakePause((useTicketingSources(true).data || []).length);
  const pausedOff = !enabled && repos.length > 0 && pause.pausedKind("prs");
  const overrides = (gh.repo_settings || {}) as RepoOverrides;
  const skipAuthors = Array.isArray(gh.skip_authors)
    ? gh.skip_authors.join(", ")
    : gh.skip_authors || "";

  const [skipDraft, setSkipDraft] = useState(String(skipAuthors));
  useEffect(() => setSkipDraft(String(skipAuthors)), [skipAuthors]);

  // Cached in the query client, not in this component: the tab unmounts
  // whenever the dialog closes or you switch tabs.
  const prsQuery = usePanelQuery<{
    prs?: OpenPr[];
    repos?: string[];
    login?: string;
    login_error?: string;
    stale?: boolean;
  }>("github-prs");
  const loadOpenPrs = prsQuery.refresh; // Refresh button: force a sweep
  const relistPrs = prsQuery.refetch; // after a force start: cache is fine,
  // has_session is annotated live on every response, even a cached one
  const prs = prsQuery.data ? prsQuery.data.prs || [] : null;
  const prsRepos = prsQuery.data?.repos || [];
  const prsError = prsQuery.error
    ? "Could not list PRs: " + (prsQuery.error.message || "error")
    : "";
  const prsNote =
    panelNote({ error: prsError, fetching: prsQuery.isFetching, loaded: !!prs }) ||
    (prsQuery.data?.login
      ? "GitHub: " + prsQuery.data.login
      : prsQuery.data?.login_error
        ? "GitHub login unknown — force review may still work"
        : "");

  const saveGithub = (patch: Record<string, unknown>, okMsg: string) =>
    s.saveGroup("github", patch, okMsg);

  const n = repos.length;
  // Group by the repo the PR came from. Card order is the user's own ordering
  // of the list above, so the groups read in the same order as the cards; a
  // repo that answered with rows but is no longer watched still gets a group
  // rather than silently dropping its PRs.
  // FILTERED BEFORE GROUPED, so a heading's count describes what is under it,
  // and a repo with no match drops out instead of rendering an empty group.
  const shownPrs = (prs || []).filter((p) => prMatches(p, filter.tokens));
  const byRepo = new Map<string, OpenPr[]>();
  for (const p of shownPrs) {
    const key = p.repo || "unknown";
    if (!byRepo.has(key)) byRepo.set(key, []);
    byRepo.get(key)!.push(p);
  }
  const groupOrder = filter.active
    ? [...byRepo.keys()]
    : [
        ...repos.filter((r) => prsRepos.includes(r) || byRepo.has(r)),
        ...[...byRepo.keys()].filter((r) => !repos.includes(r)),
      ];

  const sourceList = (
    <RepoSourceList
      key="sources"
      surface="pr"
      label="Repositories"
      repos={repos}
      overrides={overrides}
      onSave={(list, next, msg) =>
        saveGithub(
          {
            repos: list,
            repo_settings: next,
            // The first repository on a setup that never chose: saved OFF, so
            // adding one never starts acting on comments by surprise.
            ...(gh.enabled == null && !repos.length && list.length ? { enabled: false } : {}),
          },
          msg
        )
      }
      defaults={{
        agent: String(gh.agent || agentChoices.fallback || ""),
        baseBranch: String(gh.base_branch || ""),
        minAge: gh.min_age_minutes == null ? "" : String(gh.min_age_minutes),
        skipAuthors: String(skipAuthors),
        // Verify's field. Blank here for the same reason Issues passes a blank
        // base branch: the bag is one shape for all three surfaces, and a
        // surface that never renders a field has nothing to seed it with.
        liveBranch: "",
      }}
      listId="gh-repos-list"
      addId="gh-repo-add-btn"
      addLabel="+ Add repository"
      emptyText="No repositories yet — add one you open PRs on."
      hint={
        <>
          Each card is one repository, with its own agent and filters. Blank fields use the
          defaults under <strong>Advanced options</strong>.
        </>
      }
    />
  );

  const workList = (
    <WorkListPanel
      key="work"
      label="Your open pull requests"
      onRefresh={loadOpenPrs}
      note={prsNote}
      rowId="gh-open-prs-row"
      refreshId="gh-prs-refresh"
      noteId="gh-prs-note"
      listId="gh-prs-list"
      toolbarExtra={prs && prs.length ? filter.control : undefined}
      hint={
        <>
          Each row says whether auto-start will take it. <strong>Start</strong> addresses a
          PR's comments now, whatever the filters say.
        </>
      }
    >
      {prsError ? (
        <div className="repo-empty">{prsError}</div>
      ) : prs === null ? null : !prsRepos.length ? (
        <div className="repo-empty">Add a repository to see its open PRs.</div>
      ) : !prs.length ? (
        <div className="repo-empty">No open pull requests on the watched repositories.</div>
      ) : !groupOrder.length ? (
        <div className="repo-empty">No open pull request matches “{filter.query}”.</div>
      ) : (
        groupOrder.map((repo) => {
          const rows = byRepo.get(repo) || [];
          const body = !rows.length ? (
            <div className="repo-empty">No open pull requests in this repository.</div>
          ) : (
                rows.map((p) => (
                  <WorkItemRow
                    key={(p.repo || "") + p.number}
                    reference={"#" + p.number}
                    url={p.url}
                    title={p.title}
                    tooltip={
                      (p.repo || "") +
                      "#" +
                      p.number +
                      " — " +
                      (p.title || "") +
                      "\nby " +
                      (p.author || "?") +
                      " · " +
                      (p.head_ref || "?") +
                      " → " +
                      (p.base_ref || "?")
                    }
                    meta={`by ${p.author || "?"} · ${ageText(p.created_at)} · into ${p.base_ref || "?"}`}
                    hasSession={p.has_session}
                    eligible={p.eligible}
                    eligibleLabel={
                      enabled ? "will auto-start" : pausedOff ? "auto-start paused" : "auto-start off"
                    }
                    eligibleOn={enabled}
                    reasons={p.reasons}
                    actionLabel="Start"
                    failPrefix="Start failed"
                    workspace={p.workspace}
                    onReopen={async () => {
                      const title = await reopenIntakeItem({
                        kind: "prs",
                        repo: p.repo,
                        number: p.number,
                      });
                      setTimeout(relistPrs, 5000);
                      return title;
                    }}
                    agents={agentChoices.names}
                    configuredAgent={
                      overrides[repo]?.agent ||
                      String(gh.agent || agentChoices.fallback || "")
                    }
                    configuredDepth={overrides[repo]?.depth || ""}
                    onStart={async ({ agent, depth, effort }) => {
                      const r = await api<{ title?: string }>("/api/github/prs/review", {
                        json: {
                          repo: p.repo,
                          number: p.number,
                          ...(agent ? { agent } : {}),
                          ...(depth ? { depth } : {}),
                          ...(effort ? { effort } : {}),
                        },
                      });
                      // The server already has a provisioning row for it: pull it now
                      // instead of leaving the sidebar blank through the PR clone.
                      refreshInstances();
                      setTimeout(relistPrs, 5000);
                      return { message: "Session " + (r?.title || ""), title: r?.title };
                    }}
                  />
                ))
          );
          return (
            <WorkGroup
              key={repo}
              heading
              name={repo}
              count={rows.length}
              open={groups.isOpen(repo)}
              onToggle={() => groups.toggle(repo)}
            >
              {body}
            </WorkGroup>
          );
        })
      )}
    </WorkListPanel>
  );

  return (
    <>
      <div className="caps-gate" data-caps-gate="git">
        <p>
          Install <strong>Git</strong> to use this — a session checks out your PR's branch.
          Install it (e.g. <code>sudo apt install git</code> / <code>brew install git</code>),
          then reload this page.
        </p>
      </div>
      <p className="set-hint set-block-hint">
        When a reviewer comments on one of <em>your own</em> open PRs, MindFlock starts a
        session that addresses the comments. It doesn't review other people's PRs.
      </p>

      <AutomationSwitch
        label="Auto-address review comments"
        title="Start a session on its own when one of your PRs gets review comments — your repositories are kept either way"
        rowId="gh-pr-toggle-row"
        inputId="gh-pr-enabled"
        statusId="gh-pr-status"
        checked={enabled}
        paused={pausedOff}
        disabled={!n}
        onChange={(next) =>
          saveGithub(
            { enabled: next },
            next ? "Auto-address review comments on" : "Auto-address review comments off"
          )
        }
        note={
          !n
            ? "Add a repository below first"
            : pausedOff
              ? "Intake is paused — Resume intake at the top turns it back on"
              : undefined
        }
      />

      {/* WORK FIRST once a repository is watched: the PRs are the reason you
          came, the cards are setup you revisit. With none, the empty card list
          and its + Add come first — there is no work to show yet. */}
      {/* A KEYED pair, not two fragments: the order flips the moment the
          first repository is saved, and position-matched children would
          remount both — collapsing the card you are still configuring and
          dropping its focus mid-edit. Keys let React move them instead. */}
      {n ? [workList, sourceList] : [sourceList, workList]}

      <details className="pr-advanced">
        <summary>Advanced options</summary>
        <div className="pr-advanced-body">
          <p className="set-hint set-block-hint">
            Defaults for every repository card. A card can override each one.
          </p>
          <AgentPicker
            label="Agent"
            value={String(gh.agent || "")}
            choices={agentChoices}
            onChange={(v) => s.saveField("github", "agent", v)}
            hint={<>Which agent addresses the comments.</>}
          />
          <label className="set-row">
            <span className="set-label">Base branch</span>
            <SettingField group="github" field="base_branch" placeholder="e.g. main" />
            <span className="set-hint">
              Only PRs into this branch auto-start. Blank = the repo's default branch (else
              main).
            </span>
          </label>
          <label className="set-row">
            <span className="set-label">Min PR age (minutes)</span>
            <SettingField group="github" field="min_age_minutes" type="number" placeholder="15" />
            <span className="set-hint">
              Wait this long after a PR opens, so you can finish pushing. Default 15.
            </span>
          </label>
          <label className="set-row">
            <span className="set-label">Poll every (seconds)</span>
            <SettingField group="github" field="poll_interval_seconds" type="number" placeholder="60" />
            <span className="set-hint">
              How often to check GitHub. Default 60. Applies to every repository.
            </span>
          </label>
          <label className="set-row">
            <span className="set-label">Ignore comments from</span>
            <input
              type="text"
              id="gh-skip-authors"
              placeholder="dependabot, renovate"
              autoComplete="off"
              value={skipDraft}
              onChange={(e) => setSkipDraft(e.target.value)}
              onBlur={() => {
                const list = skipDraft.split(",").map((x) => x.trim()).filter(Boolean);
                saveGithub({ skip_authors: list }, "Saved");
              }}
            />
            <span className="set-hint">
              GitHub logins, comma-separated — e.g. bots whose comments you don't want acted on.
            </span>
          </label>
          <div className="set-row">
            <span className="set-label">GitHub connection</span>
            <span className="test-row">
              <button
                type="button"
                id="gh-connect-btn"
                className="test-btn"
                onClick={() => openConnectGitHub()}
              >
                Connect GitHub
              </button>
            </span>
            <span className="set-hint">
              One GitHub sign-in for PRs, issues and Open PR, shared with your devices.
            </span>
          </div>
        </div>
      </details>
    </>
  );
}
