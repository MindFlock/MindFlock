/** Intake → Pull requests: MindFlock watches your own open PRs and, when a
 * reviewer comments, starts a session that addresses the comments. It does not
 * review other people's PRs — every word on the tab says "address comments",
 * never "review", because "Automated review" read as the opposite feature.
 *
 * Off until switched on, like Issues. Saved settings from before that default
 * keep their meaning (unset with repos = on — see switches.ts `prsOn`); a fresh
 * setup's first repository is saved with an explicit off.
 *
 * Same anatomy as the other two tabs (see ./kit.tsx): master switch, the
 * GitHub line, then every open PR grouped by the repository it came from, with
 * a chip saying why auto-start has or hasn't taken it. Every watched
 * repository has a heading, and that heading is where it is edited (Edit opens
 * its editor attached under it); "+ Add source" in the dialog head adds one.
 * There are no repository cards under the work any more.
 *
 * The GitHub credential has one home, the Connect GitHub dialog; this tab
 * shows its real state on one line ("connected as @you · Manage…"). */

import { api } from "../../api/client";
import {
  refreshInstances,
  usePanelQuery,
  useTicketingCatalog,
} from "../../state/queries";
import { SettingField, useSettings } from "../settings/useSettings";
import { AgentPicker, useAgentChoices } from "../settings/screens/AgentPicker";
import {
  GithubSetupCard,
  GithubStatusLine,
  HeadAction,
  LoginListInput,
  repoGroupOrder,
  useEditing,
  useListFilter,
  WorkGroup,
  WorkItemRow,
  WorkListPanel,
  ageText,
  panelNote,
  reopenIntakeItem,
  trackerKinds,
  useToggleSet,
  type ItemWorkspace,
} from "./kit";
import { AddRepoFlow, RepoEditor, type RepoOverrides } from "./RepoSources";
import { prsOn } from "./switches";
import { prMatches } from "./search";
import type { TabProps } from "./IntakeDialog";
import { AutoStartLink } from "./AutoStartSwitches";

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
  /** The session title this item starts (or started) as. */
  session?: string;
  eligible?: boolean;
  reasons?: string[];
  /** Present when an earlier review of this PR still has its clone here. */
  workspace?: ItemWorkspace;
}

/** Per-device: which repo groups are collapsed (membership = collapsed, so a
 * fresh install writes nothing and every group starts open). */
const PR_GROUPS_KEY = "mf_intake_pr_groups";

export function PullRequestsTab({ gotoTab, focus, adding, startAdd, endAdd }: TabProps) {
  const s = useSettings();
  const agentChoices = useAgentChoices();
  const catalog = useTicketingCatalog(true).data || [];
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
  const overrides = (gh.repo_settings || {}) as RepoOverrides;
  const skipAuthors = Array.isArray(gh.skip_authors)
    ? gh.skip_authors.join(", ")
    : gh.skip_authors || "";
  const edit = useEditing(focus);


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
  const prsError = prsQuery.error
    ? "Could not list PRs: " + (prsQuery.error.message || "error")
    : "";
  // Who GitHub says you are is the status line's job now; the note keeps only
  // the case it can't show — the listing couldn't read the login.
  const prsNote =
    panelNote({ error: prsError, fetching: prsQuery.isFetching, loaded: !!prs }) ||
    (prsQuery.data?.login_error ? "GitHub login unknown — force review may still work" : "");

  const saveGithub = (patch: Record<string, unknown>, okMsg: string) =>
    s.saveGroup("github", patch, okMsg);

  const n = repos.length;
  // FILTERED BEFORE GROUPED, so a heading's count describes what is under it.
  // Every WATCHED repository keeps its heading even when nothing in it matches
  // (it reads 0): the heading is where the repository is edited, so it never
  // drops out. Keyed lower-case — GitHub's owner/name is case-insensitive.
  const shownPrs = (prs || []).filter((p) => prMatches(p, filter.tokens));
  const byRepo = new Map<string, OpenPr[]>();
  for (const p of shownPrs) {
    const key = (p.repo || "unknown").toLowerCase();
    if (!byRepo.has(key)) byRepo.set(key, []);
    byRepo.get(key)!.push(p);
  }
  const extraNames = new Map(shownPrs.map((p) => [(p.repo || "unknown").toLowerCase(), p.repo || "unknown"]));
  const groupOrder = repoGroupOrder(
    repos.map((r) => r.toLowerCase()),
    byRepo
  ).map((k) => repos.find((r) => r.toLowerCase() === k) || extraNames.get(k) || k);
  const watched = new Set(repos.map((r) => r.toLowerCase()));
  const anyMatch = shownPrs.length > 0;

  // The wizard opens only when asked for ("+ Add source", or the setup card's
  // "Pick a repo…"): an empty tab shows the setup card instead.
  const showAdd = !!adding;
  const closeAdd = () => {
    endAdd();
  };
  const addFlow = showAdd ? (
    <AddRepoFlow
      key="add"
      uses={adding?.uses?.length ? adding.uses : ["pr"]}
      trackers={trackerKinds(catalog)}
      onPickTracker={(provider) => startAdd({ kind: "tracker", provider }, "tickets")}
      onCancel={closeAdd}
      onDone={(repo) => {
        closeAdd();
        if (!groups.isOpen(repo)) groups.toggle(repo);
      }}
    />
  ) : null;

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
          PR's comments now, whatever the filters say. <strong>Edit</strong> on a repository
          changes how it is watched.
        </>
      }
    >
      {prsError ? <div className="repo-empty">{prsError}</div> : null}
      {!n && !groupOrder.length ? (
        <div className="repo-empty ik-empty-add">
          <span>No repositories yet — add one you open PRs on.</span>
          {!showAdd ? (
            <button type="button" className="test-btn" onClick={() => startAdd({ kind: "repo", uses: ["pr"] })}>
              + Add a repository
            </button>
          ) : null}
        </div>
      ) : null}
      {filter.active && !anyMatch && groupOrder.length ? (
        <p className="ik-nomatch">No open pull request matches “{filter.query}”.</p>
      ) : null}
      {groupOrder.map((repo) => {
        const key = repo.toLowerCase();
        const rows = byRepo.get(key) || [];
        const isWatched = watched.has(key);
        const editing = isWatched && edit.isEditing(repo);
        const loading = prs === null;
        const body =
          loading || (filter.active && !rows.length) ? null : !rows.length ? (
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
                session={p.session}
                eligible={p.eligible}
                eligibleLabel={enabled ? "will auto-start" : "auto-start off"}
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
                  return { message: r?.title ? "Session " + r.title : "Session for #" + p.number, title: r?.title };
                }}
              />
            ))
          );
        return (
          <WorkGroup
            key={key}
            groupKey={repo}
            heading
            name={repo}
            count={loading ? undefined : rows.length}
            detail={
              filter.active && !rows.length
                ? "no match"
                : isWatched
                  ? // Which CLI this repository's sessions run — always said, so
                    // "on the default" never looks like "no agent set".
                    overrides[repo]?.agent ||
                    (gh.agent || agentChoices.fallback
                      ? (gh.agent || agentChoices.fallback) + " (default)"
                      : "app default")
                  : "no longer watched"
            }
            onDetail={isWatched ? () => edit.open(repo) : undefined}
            detailTitle={"Change how " + repo + " is watched"}
            open={groups.isOpen(repo)}
            onToggle={() => groups.toggle(repo)}
            action={
              isWatched ? (
                <HeadAction editing={editing} name={repo} onClick={() => edit.toggle(repo)} />
              ) : undefined
            }
            editor={editing ? <RepoEditor use="pr" repo={repo} /> : undefined}
          >
            {body}
          </WorkGroup>
        );
      })}
    </WorkListPanel>
  );

  return (
    <>
      {addFlow}
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

      {!n && !showAdd ? (
        <GithubSetupCard
          what="PR review comments"
          onPickRepo={() => startAdd({ kind: "repo", uses: ["pr"] })}
          onTurnOn={() => gotoTab("autostart")}
        />
      ) : (
        <>
          {/* The switch itself is on the Auto-start tab, beside the other two. */}
          <AutoStartLink kind="prs" gotoTab={gotoTab} />
          <GithubStatusLine buttonId="gh-connect-btn" needFor="your PRs can't be listed or started" />
        </>
      )}

      {n || groupOrder.length ? workList : null}

      {n ? (
      <details className="pr-advanced">
        <summary>Advanced options</summary>
        <div className="pr-advanced-body">
          <p className="set-hint set-block-hint">
            Defaults for every repository. A repository's own <strong>Edit</strong> can override
            each one.
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
            <LoginListInput
              id="gh-skip-authors"
              stored={String(skipAuthors)}
              placeholder="dependabot, renovate"
              onSave={(list) => void saveGithub({ skip_authors: list }, "Saved")}
            />
            <span className="set-hint">
              GitHub logins, comma-separated — e.g. bots whose comments you don't want acted on.
            </span>
          </label>
        </div>
      </details>
      ) : null}
    </>
  );
}
