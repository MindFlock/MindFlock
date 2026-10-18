/** Intake → Issues: the issue-handling twin of the Pull requests tab. Its own
 * repo list (github.issue_repos, independent of review's github.repos) and its
 * own opt-in toggle (github.issues_enabled — absent = OFF, unlike review).
 *
 * Same anatomy as the other two tabs (see ./kit.tsx): every watched repository
 * has a heading, edited where it shows; "+ Add source" adds one, with "New
 * issues" ticked. The GitHub credential is shared with review and has one home,
 * the Connect GitHub dialog; this tab shows its state on one line. */

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
import { issueMatches } from "./search";
import type { TabProps } from "./IntakeDialog";
import { AutoStartLink } from "./AutoStartSwitches";

interface OpenIssue {
  repo?: string;
  number: number;
  title?: string;
  url?: string;
  author?: string;
  created_at?: string;
  has_session?: boolean;
  /** The session title this item starts (or started) as. */
  session?: string;
  eligible?: boolean;
  reasons?: string[];
  /** Present when an earlier run of this issue still has its workspace here. */
  workspace?: ItemWorkspace;
}

/** Per-device: which repo groups are collapsed (membership = collapsed). */
const ISSUE_GROUPS_KEY = "mf_intake_issue_groups";

export function IssuesTab({ gotoTab, focus, adding, startAdd, endAdd }: TabProps) {
  const s = useSettings();
  const agentChoices = useAgentChoices();
  const catalog = useTicketingCatalog(true).data || [];
  const groups = useToggleSet(ISSUE_GROUPS_KEY, true);
  const filter = useListFilter(
    "gh-issues-filter",
    "Filter by repo, number, title, author, or why…  ( Ctrl+F )",
  );
  const gh = (s.settings.github || {}) as {
    issues_enabled?: boolean;
    issue_agent?: string;
    issue_repos?: string[];
    issue_repo_settings?: RepoOverrides;
    issue_min_age_minutes?: number | string;
    issue_skip_authors?: string[] | string;
  };
  // Opt-in: absent => off (only an explicit true switches issue handling on).
  const enabled = gh.issues_enabled === true;
  const repos = Array.isArray(gh.issue_repos) ? gh.issue_repos : [];
  const overrides = (gh.issue_repo_settings || {}) as RepoOverrides;
  const skipAuthors = Array.isArray(gh.issue_skip_authors)
    ? gh.issue_skip_authors.join(", ")
    : gh.issue_skip_authors || "";
  const edit = useEditing(focus);

  // Cached in the query client so reopening the tab keeps the last list.
  const issuesQuery = usePanelQuery<{
    issues?: OpenIssue[];
    repos?: string[];
    stale?: boolean;
  }>("github-issues");
  const loadOpenIssues = issuesQuery.refresh; // Refresh button: force a sweep
  const relistIssues = issuesQuery.refetch; // after a force start (has_session
  // is annotated live even on a cache hit, so no sweep is needed)
  const issues = issuesQuery.data ? issuesQuery.data.issues || [] : null;
  const issuesError = issuesQuery.error
    ? "Could not list issues: " + (issuesQuery.error.message || "error")
    : "";
  const issuesNote = panelNote({
    error: issuesError,
    fetching: issuesQuery.isFetching,
    loaded: !!issues,
  });

  const saveGithub = (patch: Record<string, unknown>, okMsg: string) =>
    s.saveGroup("github", patch, okMsg);

  const n = repos.length;
  // FILTERED BEFORE GROUPED, so the counts on the headings describe what is
  // under them — but every WATCHED repository keeps its heading (reading 0),
  // because the heading is where it is edited.
  const shown = (issues || []).filter((i) => issueMatches(i, filter.tokens));
  const byRepo = new Map<string, OpenIssue[]>();
  for (const i of shown) {
    const key = (i.repo || "unknown").toLowerCase();
    if (!byRepo.has(key)) byRepo.set(key, []);
    byRepo.get(key)!.push(i);
  }
  const extraNames = new Map(shown.map((i) => [(i.repo || "unknown").toLowerCase(), i.repo || "unknown"]));
  const groupOrder = repoGroupOrder(
    repos.map((r) => r.toLowerCase()),
    byRepo
  ).map((k) => repos.find((r) => r.toLowerCase() === k) || extraNames.get(k) || k);
  const watched = new Set(repos.map((r) => r.toLowerCase()));

  // The wizard opens only when asked for ("+ Add source", or the setup card's
  // "Pick a repo…"): an empty tab shows the setup card instead.
  const showAdd = !!adding;
  const closeAdd = () => {
    endAdd();
  };
  const addFlow = showAdd ? (
    <AddRepoFlow
      key="add"
      uses={adding?.uses?.length ? adding.uses : ["issue"]}
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
      label="Open issues"
      onRefresh={loadOpenIssues}
      note={issuesNote}
      rowId="gh-open-issues-row"
      refreshId="gh-issues-refresh"
      noteId="gh-issues-note"
      listId="gh-issues-list"
      toolbarExtra={issues && issues.length ? filter.control : undefined}
      hint={
        <>
          Each row says whether auto-start will take it. <strong>Start</strong> starts a
          session for an issue now, whatever the filters say. <strong>Edit</strong> on a
          repository changes how it is watched.
        </>
      }
    >
      {issuesError ? <div className="repo-empty">{issuesError}</div> : null}
      {!n && !groupOrder.length ? (
        <div className="repo-empty ik-empty-add">
          <span>No repositories yet — add one whose new issues you want worked on.</span>
          {!showAdd ? (
            <button type="button" className="test-btn" onClick={() => startAdd({ kind: "repo", uses: ["issue"] })}>
              + Add a repository
            </button>
          ) : null}
        </div>
      ) : null}
      {filter.active && !shown.length && groupOrder.length ? (
        <p className="ik-nomatch">No open issue matches “{filter.query}”.</p>
      ) : null}
      {groupOrder.map((repo) => {
        const key = repo.toLowerCase();
        const rows = byRepo.get(key) || [];
        const isWatched = watched.has(key);
        const editing = isWatched && edit.isEditing(repo);
        const loading = issues === null;
        const body =
          loading || (filter.active && !rows.length) ? null : !rows.length ? (
            <div className="repo-empty">No open issues in this repository.</div>
          ) : (
            rows.map((i) => (
              <WorkItemRow
                key={(i.repo || "") + i.number}
                reference={"#" + i.number}
                url={i.url}
                title={i.title}
                tooltip={
                  (i.repo || "") + "#" + i.number + " — " + (i.title || "") + "\nby " + (i.author || "?")
                }
                meta={`by ${i.author || "?"} · ${ageText(i.created_at)}`}
                hasSession={i.has_session}
                session={i.session}
                eligible={i.eligible}
                eligibleLabel={enabled ? "will auto-start" : "auto-start off"}
                eligibleOn={enabled}
                reasons={i.reasons}
                actionLabel="Start"
                failPrefix="Start failed"
                workspace={i.workspace}
                onReopen={async () => {
                  const title = await reopenIntakeItem({
                    kind: "issues",
                    repo: i.repo,
                    number: i.number,
                  });
                  setTimeout(relistIssues, 5000);
                  return title;
                }}
                agents={agentChoices.names}
                configuredAgent={
                  overrides[repo]?.agent ||
                  String(gh.issue_agent || agentChoices.fallback || "")
                }
                configuredDepth={overrides[repo]?.depth || ""}
                onStart={async ({ agent, depth, effort }) => {
                  const r = await api<{ title?: string }>("/api/github/issues/start", {
                    json: {
                      repo: i.repo,
                      number: i.number,
                      ...(agent ? { agent } : {}),
                      ...(depth ? { depth } : {}),
                      ...(effort ? { effort } : {}),
                    },
                  });
                  // The server already has a provisioning row for it: pull it now
                  // instead of leaving the sidebar blank until the next poll.
                  refreshInstances();
                  setTimeout(relistIssues, 5000);
                  return { message: r?.title ? "Session " + r.title : "Session for #" + i.number, title: r?.title };
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
                  ? overrides[repo]?.agent ||
                    (gh.issue_agent || agentChoices.fallback
                      ? (gh.issue_agent || agentChoices.fallback) + " (default)"
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
            editor={editing ? <RepoEditor use="issue" repo={repo} /> : undefined}
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
          Install <strong>Git</strong> to use this — each issue is worked on in a fresh
          branch. Install it (e.g. <code>sudo apt install git</code> /{" "}
          <code>brew install git</code>), then reload this page.
        </p>
      </div>
      <p className="set-hint set-block-hint">
        When a new GitHub issue opens on one of the repositories below, MindFlock starts a
        session for it, with its comments, on a fresh branch.
      </p>

      {!n && !showAdd ? (
        <GithubSetupCard
          what="New issues"
          onPickRepo={() => startAdd({ kind: "repo", uses: ["issue"] })}
          onTurnOn={() => gotoTab("autostart")}
        />
      ) : (
        <>
          {/* The switch itself is on the Auto-start tab, beside the other two. */}
          <AutoStartLink kind="issues" gotoTab={gotoTab} />
          <GithubStatusLine buttonId="gh-issue-connect-btn" needFor="issues can't be listed or started" />
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
            value={String(gh.issue_agent || "")}
            choices={agentChoices}
            onChange={(v) => s.saveField("github", "issue_agent", v)}
            hint={<>Which agent works new issues. Separate from the GitHub PRs tab's.</>}
          />
          <label className="set-row">
            <span className="set-label">Min issue age (minutes)</span>
            <SettingField group="github" field="issue_min_age_minutes" type="number" placeholder="15" />
            <span className="set-hint">
              Wait this long after an issue opens, so its author can finish writing. Default 15.
            </span>
          </label>
          <label className="set-row">
            <span className="set-label">Poll every (seconds)</span>
            <SettingField group="github" field="issue_poll_interval_seconds" type="number" placeholder="60" />
            <span className="set-hint">
              How often to check GitHub. Default 60. Applies to every repository.
            </span>
          </label>
          <label className="set-row">
            <span className="set-label">Ignore issues from</span>
            <LoginListInput
              id="gh-issue-skip-authors"
              stored={String(skipAuthors)}
              placeholder="dependabot, renovate"
              onSave={(list) => void saveGithub({ issue_skip_authors: list }, "Saved")}
            />
            <span className="set-hint">GitHub logins, comma-separated.</span>
          </label>
        </div>
      </details>
      ) : null}
    </>
  );
}
