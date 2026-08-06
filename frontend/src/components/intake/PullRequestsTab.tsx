/** Intake → Pull requests: MindFlock watches your own open PRs and spins up a
 * session to address review comments.
 *
 * Same anatomy as the other two tabs (see ./kit.tsx): master switch, one card
 * per watched repository, then every open PR grouped by the repository it came
 * from with a chip saying why auto review has or hasn't taken it.
 *
 * This tab additionally owns the shared GitHub credential — the Issues tab
 * authenticates as the same account and links here for it. */

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
}

/** Per-device: which repo groups are collapsed (membership = collapsed, so a
 * fresh install writes nothing and every group starts open). */
const PR_GROUPS_KEY = "mf_intake_pr_groups";

export function PullRequestsTab({ gotoTab }: TabProps) {
  const s = useSettings();
  const agentChoices = useAgentChoices();
  const groups = useToggleSet(PR_GROUPS_KEY, true);
  const gh = (s.settings.github || {}) as {
    enabled?: boolean;
    agent?: string;
    repos?: string[];
    repo_settings?: RepoOverrides;
    base_branch?: string;
    min_age_minutes?: number | string;
    skip_authors?: string[] | string;
  };
  // Absent => on (the default once repos exist); explicit false => paused.
  const enabled = gh.enabled !== false;
  const repos = Array.isArray(gh.repos) ? gh.repos : [];
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

  const [ghTest, setGhTest] = useState<{ testing: boolean; ok?: boolean; msg?: string }>({
    testing: false,
  });

  const saveGithub = (patch: Record<string, unknown>, okMsg: string) =>
    s.saveGroup("github", patch, okMsg);

  const n = repos.length;
  // Group by the repo the PR came from. Card order is the user's own ordering
  // of the list above, so the groups read in the same order as the cards; a
  // repo that answered with rows but is no longer watched still gets a group
  // rather than silently dropping its PRs.
  const byRepo = new Map<string, OpenPr[]>();
  for (const p of prs || []) {
    const key = p.repo || "unknown";
    if (!byRepo.has(key)) byRepo.set(key, []);
    byRepo.get(key)!.push(p);
  }
  const groupOrder = [
    ...repos.filter((r) => prsRepos.includes(r) || byRepo.has(r)),
    ...[...byRepo.keys()].filter((r) => !repos.includes(r)),
  ];

  return (
    <>
      <div className="caps-gate" data-caps-gate="git">
        <p>
          Install <strong>Git</strong> to get access to these features — automated PR review
          checks out and works on your pull-request branches, which needs git. Install it (e.g.{" "}
          <code>sudo apt install git</code> / <code>brew install git</code>), then reload this
          page.
        </p>
      </div>
      <div className="caps-gate" data-caps-gate="ticketing">
        <p>
          Connect a <strong>ticketing tool</strong> to get access to these features — automated
          PR review runs alongside ticket ingestion, which needs a connected ticketing source
          (Jira, Linear, GitHub Issues, Shortcut or Asana).
        </p>
        <p>
          <button type="button" className="linklike" data-goto-tab="tickets" onClick={() => gotoTab("tickets")}>
            Connect one on the Tickets tab
          </button>
        </p>
      </div>
      <p className="set-hint set-block-hint">
        MindFlock watches <em>your own</em> open pull requests on the repositories below and
        spins up a session to address review comments. Runs while ingestion is active.
      </p>

      <AutomationSwitch
        label="Automated review"
        title="Turn automated PR review on or off — your repositories are kept either way"
        rowId="gh-pr-toggle-row"
        inputId="gh-pr-enabled"
        statusId="gh-pr-status"
        checked={enabled}
        onChange={(next) =>
          saveGithub(
            { enabled: next },
            next ? "Automated review on" : "Automated review paused"
          )
        }
        note={n ? undefined : "Add a repository below and this starts reviewing your PRs on it"}
      />

      <RepoSourceList
        surface="pr"
        label="Repositories"
        repos={repos}
        overrides={overrides}
        onSave={(list, next, msg) =>
          saveGithub({ repos: list, repo_settings: next }, msg)
        }
        defaults={{
          agent: String(gh.agent || agentChoices.fallback || ""),
          baseBranch: String(gh.base_branch || ""),
          minAge: gh.min_age_minutes == null ? "" : String(gh.min_age_minutes),
          skipAuthors: String(skipAuthors),
        }}
        listId="gh-repos-list"
        addId="gh-repo-add-btn"
        addLabel="+ Add repository"
        emptyText="No repositories yet — add one below to start reviewing your PRs."
        hint={
          <>
            Each card is one repository, with its own agent CLI and filters. Blank fields
            inherit the tab defaults under <strong>Advanced options</strong>. Adding a
            repository turns review on; remove them all to turn it off.
          </>
        }
      />

      <WorkListPanel
        label="Open pull requests"
        onRefresh={loadOpenPrs}
        note={prsNote}
        rowId="gh-open-prs-row"
        refreshId="gh-prs-refresh"
        noteId="gh-prs-note"
        listId="gh-prs-list"
        hint={
          <>
            Every non-draft open PR on the repositories above
, grouped by repository, with why auto review has or hasn't
            picked it up. <strong>Begin review</strong> starts a review session for that PR
            right now, bypassing the author / age / base-branch / already-reviewed filters.
          </>
        }
      >
        {prsError ? (
          <div className="repo-empty">{prsError}</div>
        ) : prs === null ? null : !prsRepos.length ? (
          <div className="repo-empty">Add a repository above to see its open PRs.</div>
        ) : !prs.length ? (
          <div className="repo-empty">No open pull requests on the watched repositories.</div>
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
                      eligibleLabel="queued for auto review"
                      reasons={p.reasons}
                      actionLabel="Begin review"
                      failPrefix="Begin review failed"
                      agents={agentChoices.names}
                      configuredAgent={
                        overrides[repo]?.agent ||
                        String(gh.agent || agentChoices.fallback || "")
                      }
                      configuredDepth={overrides[repo]?.depth || ""}
                      onStart={async ({ agent, depth }) => {
                        const r = await api<{ title?: string }>("/api/github/prs/review", {
                          json: {
                            repo: p.repo,
                            number: p.number,
                            ...(agent ? { agent } : {}),
                            ...(depth ? { depth } : {}),
                          },
                        });
                        // The server already has a provisioning row for it: pull it now
                        // instead of leaving the sidebar blank through the PR clone.
                        refreshInstances();
                        setTimeout(relistPrs, 5000);
                        return "Review session " + (r?.title || "");
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
            genuinely one-per-app (the poll loop and the GitHub credential).
          </p>
          <AgentPicker
            label="Agent CLI"
            value={String(gh.agent || "")}
            choices={agentChoices}
            onChange={(v) => s.saveField("github", "agent", v)}
            hint={
              <>
                Which coding CLI runs PR-review sessions by default. Independent of issue
                handling's — pick a provider whose Connections row is green, or leave it on
                the app default. A repository card can override it.
              </>
            }
          />
          <label className="set-row">
            <span className="set-label">Base branch</span>
            <SettingField group="github" field="base_branch" placeholder="e.g. main" />
            <span className="set-hint">
              Only PRs targeting this branch are auto-reviewed. Blank is <em>not</em> "every
              branch" — it falls through to <code>[repository].base_branch</code> and then to{" "}
              <code>main</code>, so set it explicitly if your repos disagree about their
              default branch (or override it per repository on its card). A PR into any other
              branch is still listed above with a chip saying so.
            </span>
          </label>
          <label className="set-row">
            <span className="set-label">Min PR age (minutes)</span>
            <SettingField group="github" field="min_age_minutes" type="number" placeholder="15" />
            <span className="set-hint">
              Grace period after a PR opens before review starts, so you can finish pushing.
              Default 15. A repository card can override it.
            </span>
          </label>
          <label className="set-row">
            <span className="set-label">Poll every (seconds)</span>
            <SettingField group="github" field="poll_interval_seconds" type="number" placeholder="60" />
            <span className="set-hint">
              How often to check GitHub for new PRs. Default 60. One poll loop covers every
              repository, so this one is not per-card.
            </span>
          </label>
          <label className="set-row">
            <span className="set-label">Skip authors</span>
            <input
              type="text"
              id="gh-skip-authors"
              placeholder="dependabot, renovate"
              autoComplete="off"
              value={skipDraft}
              onChange={(e) => setSkipDraft(e.target.value)}
              onBlur={() => {
                const list = skipDraft.split(",").map((x) => x.trim()).filter(Boolean);
                saveGithub({ skip_authors: list }, "Saved skip authors");
              }}
            />
            <span className="set-hint">
              Comma-separated GitHub logins whose <em>review comments</em> are ignored —
              review only ever takes your own PRs, so this is the bot feedback you don't
              want acted on, not a list of authors to skip. A repository card can override it.
            </span>
          </label>
          <label className="set-row">
            <span className="set-label">Token</span>
            <SettingField group="github" field="token" type="password" placeholder="optional — else $GH_TOKEN / gh auth" />
            <span className="set-hint">
              Shared with issue handling — it authenticates the same account. Also what
              lets MindFlock open and merge PRs for you without the gh CLI. Falls back to
              $GH_TOKEN / $GITHUB_TOKEN / `gh auth token`. Pushing never needs it — that is
              plain git over your own remote.
            </span>
          </label>
          <div className="set-row">
            <span className="test-row">
              <button
                type="button"
                id="gh-test-btn"
                className="test-btn"
                onClick={async () => {
                  setGhTest({ testing: true });
                  setGhTest(await runGithubTest());
                }}
              >
                Test GitHub
              </button>
              <span
                id="gh-test-result"
                className={"test-result" + (ghTest.msg ? (ghTest.ok ? " ok" : " bad") : "")}
              >
                {ghTest.testing ? "testing…" : ghTest.msg ? (ghTest.ok ? "✓ " : "✗ ") + ghTest.msg : ""}
              </span>
            </span>
            <span className="set-hint">
              Shows where a token would come from. A token is all this needs — gh is
              reported too, but it is optional. Use a card's <strong>Test access</strong>
              {" "}to check one repository.
            </span>
          </div>
        </div>
      </details>
    </>
  );
}
