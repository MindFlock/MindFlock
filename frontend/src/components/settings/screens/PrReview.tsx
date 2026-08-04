/** Settings → PR review (partial 109 + section 21's github wiring): the repo
 * list IS the switch; open-PR panel with skip-reason chips + force review.
 *
 * The screen's shape — switch, status, watched list, agent, open-work panel,
 * Advanced — comes from ./automation so this and Git issues and Ticketing stay
 * the same screen with different nouns. This one additionally owns the GitHub
 * token, which issue handling shares and links back to. */

import { useEffect, useState } from "react";
import { api } from "../../../api/client";
import { refreshInstances, usePanelQuery } from "../../../state/queries";
import { SettingField, useSettings } from "../useSettings";
import { AgentPicker, useAgentChoices } from "./AgentPicker";
import { AutomationSwitch, RepoListField, WorkItemRow, WorkListPanel, ageText } from "./automation";
import { runGithubTest } from "../../dialogs/SetupDialog";
import type { ScreenProps } from "../SettingsDialog";

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

export function PrReview({ gotoScreen }: ScreenProps) {
  const s = useSettings();
  const agentChoices = useAgentChoices();
  const gh = (s.settings.github || {}) as {
    enabled?: boolean;
    agent?: string;
    issue_agent?: string;
    repos?: string[];
    skip_authors?: string[] | string;
  };
  // Absent => on (the default once repos exist); explicit false => paused.
  const enabled = gh.enabled !== false;
  const repos = Array.isArray(gh.repos) ? gh.repos : [];
  const skipAuthors = Array.isArray(gh.skip_authors)
    ? gh.skip_authors.join(", ")
    : gh.skip_authors || "";

  const [skipDraft, setSkipDraft] = useState(String(skipAuthors));
  useEffect(() => setSkipDraft(String(skipAuthors)), [skipAuthors]);

  // Cached in the query client, not in this component: the screen unmounts
  // whenever the dialog closes or you switch screens.
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
  const prsNote = prsError
    ? ""
    : prsQuery.data?.login
      ? "GitHub: " + prsQuery.data.login
      : prsQuery.data?.login_error
        ? "GitHub login unknown — force review may still work"
        : prsQuery.isFetching
          ? prs
            ? "Refreshing…"
            : "Loading…"
          : "";

  const [ghTest, setGhTest] = useState<{ testing: boolean; ok?: boolean; msg?: string }>({
    testing: false,
  });

  const saveGithub = (patch: Record<string, unknown>, okMsg: string) =>
    s.saveGroup("github", patch, okMsg);

  const n = repos.length;

  return (
    <>
      <h3 className="set-section-title">Automated PR review</h3>
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
          <button type="button" className="linklike" data-goto-screen="ticketing" onClick={() => gotoScreen("ticketing")}>
            Connect one in Settings → Ticketing
          </button>
        </p>
      </div>
      <p className="set-hint set-block-hint">
        MindFlock watches <em>your own</em> open pull requests on the repositories below and
        automatically spins up a coding session to address review comments. It runs while
        ingestion is active.
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
        tone={n > 0 && enabled ? "on" : n > 0 ? "paused" : ""}
        status={
          !n
            ? "○ Add a repository below to start reviewing your PRs"
            : enabled
              ? `● Active — reviewing PRs in ${n} ${n === 1 ? "repository" : "repositories"}`
              : `‖ Paused — ${n} ${n === 1 ? "repository" : "repositories"} kept; turn Automated review on to resume`
        }
      />

      <RepoListField
        label="Repositories to review"
        repos={repos}
        onSave={(list, msg) => saveGithub({ repos: list }, msg)}
        emptyText="No repositories yet — add one below to start reviewing your PRs."
        listId="gh-repos-list"
        inputId="gh-repo-new"
        addId="gh-repo-add-btn"
        hint={
          <>
            Type a repo as <code>owner/name</code>, then press Enter or click Add. Adding one
            turns review on; remove them all to turn it off.
          </>
        }
      />

      <AgentPicker
        label="Agent CLI"
        value={String(gh.agent || "")}
        choices={agentChoices}
        onChange={(v) => s.saveField("github", "agent", v)}
        hint={
          <>
            Which coding CLI runs PR-review sessions. Independent of issue handling's —
            pick a provider whose Connections row is green, or leave it on the app
            default.
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
            Every non-draft open PR on the repositories above, with why auto review has or hasn't
            picked it up. <strong>Begin review</strong> starts a review session for that PR right
            now, bypassing the author / age / already-reviewed filters.
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
          prs.map((p) => (
            <WorkItemRow
              key={(p.repo || "") + p.number}
              reference={(p.repo || "") + "#" + p.number}
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
              onStart={async () => {
                const r = await api<{ title?: string }>("/api/github/prs/review", {
                  json: { repo: p.repo, number: p.number },
                });
                // The server already has a provisioning row for it: pull it now
                // instead of leaving the sidebar blank through the PR clone.
                refreshInstances();
                setTimeout(relistPrs, 5000);
                return "Review session " + (r?.title || "");
              }}
            />
          ))
        )}
      </WorkListPanel>

      <details className="pr-advanced">
        <summary>Advanced options</summary>
        <div className="pr-advanced-body">
          <label className="set-row">
            <span className="set-label">Base branch</span>
            <SettingField group="github" field="base_branch" placeholder="any branch" />
            <span className="set-hint">
              Only PRs targeting this branch are reviewed. Blank = all branches.
            </span>
          </label>
          <label className="set-row">
            <span className="set-label">Min PR age (minutes)</span>
            <SettingField group="github" field="min_age_minutes" type="number" placeholder="15" />
            <span className="set-hint">
              Grace period after a PR opens before review starts, so you can finish pushing.
              Default 15.
            </span>
          </label>
          <label className="set-row">
            <span className="set-label">Poll every (seconds)</span>
            <SettingField group="github" field="poll_interval_seconds" type="number" placeholder="60" />
            <span className="set-hint">How often to check GitHub for new PRs. Default 60.</span>
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
            <span className="set-hint">Comma-separated GitHub logins whose PRs are ignored.</span>
          </label>
          <label className="set-row">
            <span className="set-label">Token</span>
            <SettingField group="github" field="token" type="password" placeholder="optional — else $GH_TOKEN / gh auth" />
            <span className="set-hint">
              Also what lets MindFlock open and merge PRs for you without the gh CLI.
              Falls back to $GH_TOKEN / $GITHUB_TOKEN / `gh auth token`. Pushing never
              needs it — that is plain git over your own remote.
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
              reported too, but it is optional.
            </span>
          </div>
        </div>
      </details>
    </>
  );
}
