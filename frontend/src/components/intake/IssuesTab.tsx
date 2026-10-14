/** Intake → Issues: the issue-handling twin of the Pull requests tab. Its own
 * repo list (github.issue_repos, independent of review's github.repos) and its
 * own opt-in toggle (github.issues_enabled — absent = OFF, unlike review).
 *
 * Same anatomy as the other two tabs (see ./kit.tsx). The GitHub credential is
 * shared with review and has one home, the Connect GitHub dialog (Settings →
 * Connections → GitHub → Connect); this tab only offers the button. */

import { useEffect, useState } from "react";
import { api } from "../../api/client";
import { refreshInstances, usePanelQuery } from "../../state/queries";
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
import { loadRemembered } from "./pause";
import { issueMatches } from "./search";
import type { TabProps } from "./IntakeDialog";

interface OpenIssue {
  repo?: string;
  number: number;
  title?: string;
  url?: string;
  author?: string;
  created_at?: string;
  has_session?: boolean;
  eligible?: boolean;
  reasons?: string[];
  /** Present when an earlier run of this issue still has its workspace here. */
  workspace?: ItemWorkspace;
}

/** Per-device: which repo groups are collapsed (membership = collapsed). */
const ISSUE_GROUPS_KEY = "mf_intake_issue_groups";

export function IssuesTab({ gotoTab }: TabProps) {
  const s = useSettings();
  const agentChoices = useAgentChoices();
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
  // Off because Pause intake turned it off (it will come back on Resume).
  const pausedOff = !enabled && (loadRemembered() || []).includes("issues");
  const overrides = (gh.issue_repo_settings || {}) as RepoOverrides;
  const skipAuthors = Array.isArray(gh.issue_skip_authors)
    ? gh.issue_skip_authors.join(", ")
    : gh.issue_skip_authors || "";

  const [skipDraft, setSkipDraft] = useState(String(skipAuthors));
  useEffect(() => setSkipDraft(String(skipAuthors)), [skipAuthors]);
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
  const issuesRepos = issuesQuery.data?.repos || [];
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
  // under them. A repo whose issues are all filtered out drops off entirely
  // rather than rendering an empty group with "no open issues in this
  // repository" — which, with a filter on, would be an answer to a question
  // nobody asked.
  const shown = (issues || []).filter((i) => issueMatches(i, filter.tokens));
  const byRepo = new Map<string, OpenIssue[]>();
  for (const i of shown) {
    const key = i.repo || "unknown";
    if (!byRepo.has(key)) byRepo.set(key, []);
    byRepo.get(key)!.push(i);
  }
  const groupOrder = filter.active
    ? [...byRepo.keys()]
    : [
        ...repos.filter((r) => issuesRepos.includes(r) || byRepo.has(r)),
        ...[...byRepo.keys()].filter((r) => !repos.includes(r)),
      ];

  const sourceList = (
    <RepoSourceList
      key="sources"
      surface="issue"
      label="Repositories"
      repos={repos}
      overrides={overrides}
      onSave={(list, next, msg) =>
        saveGithub({ issue_repos: list, issue_repo_settings: next }, msg)
      }
      defaults={{
        agent: String(gh.issue_agent || agentChoices.fallback || ""),
        baseBranch: "",
        minAge: gh.issue_min_age_minutes == null ? "" : String(gh.issue_min_age_minutes),
        skipAuthors: String(skipAuthors),
        // Verify's field, blank here for the same reason baseBranch is: this
        // surface does not render it, so there is nothing to seed.
        liveBranch: "",
      }}
      listId="gh-issue-repos-list"
      addId="gh-issue-repo-add-btn"
      addLabel="+ Add repository"
      emptyText="No repositories yet — add one whose new issues you want worked on."
      hint={
        <>
          Each card is one repository, with its own agent and filters. Blank fields use the
          defaults under <strong>Advanced options</strong>. Separate from the Pull requests
          tab's list.
        </>
      }
    />
  );

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
          session for an issue now, whatever the filters say.
        </>
      }
    >
      {issuesError ? (
        <div className="repo-empty">{issuesError}</div>
      ) : issues === null ? null : !issuesRepos.length ? (
        <div className="repo-empty">Add a repository to see its open issues.</div>
      ) : !issues.length ? (
        <div className="repo-empty">No open issues on the watched repositories.</div>
      ) : !groupOrder.length ? (
        <div className="repo-empty">No open issue matches “{filter.query}”.</div>
      ) : (
        groupOrder.map((repo) => {
          const rows = byRepo.get(repo) || [];
          const body = !rows.length ? (
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
                    eligible={i.eligible}
                    eligibleLabel={
                      enabled ? "will auto-start" : pausedOff ? "auto-start paused" : "auto-start off"
                    }
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
          Install <strong>Git</strong> to use this — each issue is worked on in a fresh
          branch. Install it (e.g. <code>sudo apt install git</code> /{" "}
          <code>brew install git</code>), then reload this page.
        </p>
      </div>
      <p className="set-hint set-block-hint">
        When a new issue opens on one of the repositories below, MindFlock starts a session
        for it, with its comments, on a fresh branch.
      </p>

      <AutomationSwitch
        label="Auto-start new issues"
        title="Start a session on its own for each newly opened issue — your repositories are kept either way"
        rowId="gh-issues-toggle-row"
        inputId="gh-issues-enabled"
        statusId="gh-issues-status"
        checked={enabled}
        disabled={!n}
        onChange={(next) =>
          saveGithub(
            { issues_enabled: next },
            next ? "Auto-start new issues on" : "Auto-start new issues off"
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

      {/* WORK FIRST once a repository is watched — the same anatomy as the
          Tickets and Pull requests tabs. */}
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
            value={String(gh.issue_agent || "")}
            choices={agentChoices}
            onChange={(v) => s.saveField("github", "issue_agent", v)}
            hint={<>Which agent works new issues. Separate from the Pull requests tab's.</>}
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
            <input
              type="text"
              id="gh-issue-skip-authors"
              placeholder="dependabot, renovate"
              autoComplete="off"
              value={skipDraft}
              onChange={(e) => setSkipDraft(e.target.value)}
              onBlur={() => {
                const list = skipDraft.split(",").map((x) => x.trim()).filter(Boolean);
                saveGithub({ issue_skip_authors: list }, "Saved");
              }}
            />
            <span className="set-hint">GitHub logins, comma-separated.</span>
          </label>
          <div className="set-row">
            <span className="set-label">GitHub connection</span>
            <span className="test-row">
              <button
                type="button"
                id="gh-issue-connect-btn"
                className="test-btn"
                onClick={() => openConnectGitHub()}
              >
                Connect GitHub
              </button>
            </span>
            <span className="set-hint">
              The same GitHub sign-in the Pull requests tab uses.
            </span>
          </div>
        </div>
      </details>
    </>
  );
}
