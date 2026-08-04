/** Intake → Issues: the issue-handling twin of the Pull requests tab. Its own
 * repo list (github.issue_repos, independent of review's github.repos) and its
 * own opt-in toggle (github.issues_enabled — absent = OFF, unlike review).
 *
 * Same anatomy as the other two tabs (see ./kit.tsx). The GitHub credential is
 * shared with review and lives on that tab; this one links there. */

import { useEffect, useState } from "react";
import { api } from "../../api/client";
import { refreshInstances, usePanelQuery } from "../../state/queries";
import { SettingField, useSettings } from "../settings/useSettings";
import { AgentPicker, useAgentChoices } from "../settings/screens/AgentPicker";
import { runGithubTest } from "../dialogs/SetupDialog";
import {
  AutomationSwitch,
  WorkGroup,
  WorkItemRow,
  WorkListPanel,
  ageText,
  panelNote,
  useToggleSet,
} from "./kit";
import { RepoSourceList, type RepoOverrides } from "./RepoSources";
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
}

/** Per-device: which repo groups are collapsed (membership = collapsed). */
const ISSUE_GROUPS_KEY = "mf_intake_issue_groups";

export function IssuesTab({ gotoTab }: TabProps) {
  const s = useSettings();
  const agentChoices = useAgentChoices();
  const groups = useToggleSet(ISSUE_GROUPS_KEY, true);
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

  const [ghTest, setGhTest] = useState<{ testing: boolean; ok?: boolean; msg?: string }>({
    testing: false,
  });

  const saveGithub = (patch: Record<string, unknown>, okMsg: string) =>
    s.saveGroup("github", patch, okMsg);

  const n = repos.length;
  const byRepo = new Map<string, OpenIssue[]>();
  for (const i of issues || []) {
    const key = i.repo || "unknown";
    if (!byRepo.has(key)) byRepo.set(key, []);
    byRepo.get(key)!.push(i);
  }
  const groupOrder = [
    ...repos.filter((r) => issuesRepos.includes(r) || byRepo.has(r)),
    ...[...byRepo.keys()].filter((r) => !repos.includes(r)),
  ];

  return (
    <>
      <div className="caps-gate" data-caps-gate="git">
        <p>
          Install <strong>Git</strong> to get access to these features — automated issue
          handling clones your repositories and works on fresh branches, which needs git.
          Install it (e.g. <code>sudo apt install git</code> / <code>brew install git</code>
          ), then reload this page.
        </p>
      </div>
      <div className="caps-gate" data-caps-gate="ticketing">
        <p>
          Connect a <strong>ticketing tool</strong> to get access to these features —
          automated issue handling runs alongside ticket ingestion, which needs a connected
          ticketing source (Jira, Linear, GitHub Issues, Shortcut or Asana).
        </p>
        <p>
          <button type="button" className="linklike" data-goto-tab="tickets" onClick={() => gotoTab("tickets")}>
            Connect one on the Tickets tab
          </button>
        </p>
      </div>
      <p className="set-hint set-block-hint">
        MindFlock watches for <em>newly opened issues</em> on the repositories below, grabs
        each one with its comments, and starts a session on a fresh branch. Its repository
        list is independent of PR review's.
      </p>

      <AutomationSwitch
        label="Automated handling"
        title="Turn automated issue handling on or off — your repositories are kept either way"
        rowId="gh-issues-toggle-row"
        inputId="gh-issues-enabled"
        statusId="gh-issues-status"
        checked={enabled}
        onChange={(next) =>
          saveGithub(
            { issues_enabled: next },
            next ? "Automated issue handling on" : "Automated issue handling off"
          )
        }
        tone={n > 0 && enabled ? "on" : n > 0 ? "paused" : ""}
        status={
          !n
            ? "○ Add a repository below, then turn Automated handling on"
            : enabled
              ? `● Active — handling new issues in ${n} ${n === 1 ? "repository" : "repositories"}`
              : `‖ Off — ${n} ${n === 1 ? "repository" : "repositories"} kept; turn Automated handling on to start`
        }
      />

      <RepoSourceList
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
        }}
        listId="gh-issue-repos-list"
        addId="gh-issue-repo-add-btn"
        addLabel="+ Add repository"
        emptyText="No repositories yet — add one below to start handling new issues."
        hint={
          <>
            Each card is one repository, with its own agent CLI and filters. Blank fields
            inherit the tab defaults under <strong>Advanced options</strong>. This list is
            separate from PR review's — a repo can be on either, or both.
          </>
        }
      />

      <WorkListPanel
        label="Open issues"
        onRefresh={loadOpenIssues}
        note={issuesNote}
        rowId="gh-open-issues-row"
        refreshId="gh-issues-refresh"
        noteId="gh-issues-note"
        listId="gh-issues-list"
        hint={
          <>
            Every open issue on the repositories above
, grouped by repository, with why auto handling has or hasn't
            picked it up. <strong>Start work</strong> spins up a session for that issue right
            now, bypassing the age / already-handled filters.
          </>
        }
      >
        {issuesError ? (
          <div className="repo-empty">{issuesError}</div>
        ) : issues === null ? null : !issuesRepos.length ? (
          <div className="repo-empty">Add a repository above to see its open issues.</div>
        ) : !issues.length ? (
          <div className="repo-empty">No open issues on the watched repositories.</div>
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
                      eligibleLabel="queued for auto handling"
                      reasons={i.reasons}
                      actionLabel="Start work"
                      failPrefix="Start work failed"
                      agents={agentChoices.names}
                      configuredAgent={
                        overrides[repo]?.agent ||
                        String(gh.issue_agent || agentChoices.fallback || "")
                      }
                      onStart={async (agent) => {
                        const r = await api<{ title?: string }>("/api/github/issues/start", {
                          json: {
                            repo: i.repo,
                            number: i.number,
                            ...(agent ? { agent } : {}),
                          },
                        });
                        // The server already has a provisioning row for it: pull it now
                        // instead of leaving the sidebar blank until the next poll.
                        refreshInstances();
                        setTimeout(relistIssues, 5000);
                        return "Issue session " + (r?.title || "");
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

      <details className="pr-advanced">
        <summary>Advanced options</summary>
        <div className="pr-advanced-body">
          <p className="set-hint set-block-hint">
            The defaults every repository card inherits, plus the settings that are
            genuinely one-per-app.
          </p>
          <AgentPicker
            label="Agent CLI"
            value={String(gh.issue_agent || "")}
            choices={agentChoices}
            onChange={(v) => s.saveField("github", "issue_agent", v)}
            hint={
              <>
                Which coding CLI runs issue-handling sessions by default. Independent of PR
                review's — setting one does not change the other. A repository card can
                override it.
              </>
            }
          />
          <label className="set-row">
            <span className="set-label">Min issue age (minutes)</span>
            <SettingField group="github" field="issue_min_age_minutes" type="number" placeholder="15" />
            <span className="set-hint">
              Grace period after an issue opens before work starts, so you can finish
              writing it. Default 15. Independent of PR review's setting; a repository card
              can override it.
            </span>
          </label>
          <label className="set-row">
            <span className="set-label">Poll every (seconds)</span>
            <SettingField group="github" field="issue_poll_interval_seconds" type="number" placeholder="60" />
            <span className="set-hint">
              How often to check GitHub for new issues. Default 60. One poll loop covers
              every repository, so this one is not per-card.
            </span>
          </label>
          <label className="set-row">
            <span className="set-label">Skip authors</span>
            <input
              type="text"
              id="gh-issue-skip-authors"
              placeholder="dependabot, renovate"
              autoComplete="off"
              value={skipDraft}
              onChange={(e) => setSkipDraft(e.target.value)}
              onBlur={() => {
                const list = skipDraft.split(",").map((x) => x.trim()).filter(Boolean);
                saveGithub({ issue_skip_authors: list }, "Saved skip authors");
              }}
            />
            <span className="set-hint">
              Comma-separated GitHub logins whose issues are ignored. Independent of PR
              review's list; a repository card can override it.
            </span>
          </label>
          <div className="set-row">
            <span className="set-label">GitHub token</span>
            <span className="test-row">
              <button
                type="button"
                id="gh-issue-test-btn"
                className="test-btn"
                onClick={async () => {
                  setGhTest({ testing: true });
                  setGhTest(await runGithubTest());
                }}
              >
                Test GitHub
              </button>
              <span
                id="gh-issue-test-result"
                className={"test-result" + (ghTest.msg ? (ghTest.ok ? " ok" : " bad") : "")}
              >
                {ghTest.testing ? "testing…" : ghTest.msg ? (ghTest.ok ? "✓ " : "✗ ") + ghTest.msg : ""}
              </span>
            </span>
            <span className="set-hint">
              The GitHub credential is shared with PR review — it authenticates the same
              account. Set or change it on the{" "}
              <button type="button" className="linklike" onClick={() => gotoTab("prs")}>
                Pull requests tab
              </button>
              ; the button above checks the current connection.
            </span>
          </div>
        </div>
      </details>
    </>
  );
}
