/** Settings → Git issues: the issue-handling twin of PrReview.tsx. Its own
 * repo list (github.issue_repos, independent of PR review's github.repos) and
 * opt-in toggle (github.issues_enabled — absent = OFF, unlike PR review);
 * open-issues panel with skip-reason chips + force start. */

import { useEffect, useState } from "react";
import { api } from "../../../api/client";
import { toast } from "../../../lib/toast";
import { refreshInstances, usePanelQuery } from "../../../state/queries";
import { SettingField, useSettings } from "../useSettings";
import type { ScreenProps } from "../SettingsDialog";

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

function issueAgeText(iso?: string): string {
  const t = Date.parse(iso || "");
  if (!isFinite(t)) return "";
  const mins = Math.max(0, Math.round((Date.now() - t) / 60000));
  if (mins < 60) return mins + "m old";
  const h = Math.round(mins / 60);
  if (h < 48) return h + "h old";
  return Math.round(h / 24) + "d old";
}

export function GitIssues({ gotoScreen }: ScreenProps) {
  const s = useSettings();
  const gh = (s.settings.github || {}) as {
    issues_enabled?: boolean;
    issue_repos?: string[];
    issue_skip_authors?: string[] | string;
  };
  // Opt-in: absent => off (only an explicit true switches issue handling on).
  const enabled = gh.issues_enabled === true;
  const repos = Array.isArray(gh.issue_repos) ? gh.issue_repos : [];
  const skipAuthors = Array.isArray(gh.issue_skip_authors)
    ? gh.issue_skip_authors.join(", ")
    : gh.issue_skip_authors || "";

  const [repoNew, setRepoNew] = useState("");
  const [skipDraft, setSkipDraft] = useState(String(skipAuthors));
  useEffect(() => setSkipDraft(String(skipAuthors)), [skipAuthors]);
  // Cached in the query client so reopening the screen keeps the last list.
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
  const issuesNote =
    issuesError || !issuesQuery.isFetching ? "" : issues ? "Refreshing…" : "Loading…";

  const [ghTest, setGhTest] = useState<{ testing: boolean; ok?: boolean; msg?: string }>({
    testing: false,
  });

  const saveGithub = (patch: Record<string, unknown>, okMsg: string) =>
    s.saveGroup("github", patch, okMsg);

  const saveRepos = (list: string[], msg: string) =>
    saveGithub({ issue_repos: list }, msg);

  const addRepo = () => {
    const val = repoNew.trim();
    if (!val) return;
    if (!/^[^\s/]+\/[^\s/]+$/.test(val)) {
      toast("Use owner/name, e.g. MindFlock/MindFlock");
      return;
    }
    if (repos.some((r) => r.toLowerCase() === val.toLowerCase())) {
      setRepoNew("");
      toast(val + " is already in the list");
      return;
    }
    setRepoNew("");
    saveRepos([...repos, val], "Added " + val);
  };

  const n = repos.length;
  const statusText = !n
    ? "○ Add a repository below, then turn Automated handling on"
    : enabled
      ? `● Active — handling new issues in ${n} ${n === 1 ? "repository" : "repositories"}`
      : `‖ Off — ${n} ${n === 1 ? "repository" : "repositories"} kept; turn Automated handling on to start`;

  return (
    <>
      <h3 className="set-section-title">Automated issue handling</h3>
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
          <button type="button" className="linklike" data-goto-screen="ticketing" onClick={() => gotoScreen("ticketing")}>
            Connect one in Settings → Ticketing
          </button>
        </p>
      </div>
      <p className="set-hint set-block-hint">
        MindFlock watches for <em>newly opened issues</em> on the repositories below, grabs
        each issue and all its comments, and automatically spins up a coding session that
        starts work on a fresh branch for it. Separate from PR review — the repository lists
        are independent.
      </p>

      <div
        className="set-row set-switch-row"
        id="gh-issues-toggle-row"
        title="Turn automated issue handling on or off — your repositories are kept either way"
      >
        <span className="set-label">Automated handling</span>
        {/* label wraps only the switch, so clicking the row text no longer flips it */}
        <label className="ca-switch">
          <input
            type="checkbox"
            id="gh-issues-enabled"
            checked={enabled}
            onChange={(e) =>
              saveGithub(
                { issues_enabled: e.target.checked },
                e.target.checked ? "Automated issue handling on" : "Automated issue handling off"
              )
            }
          />
          <span className="ca-slider" />
        </label>
      </div>
      <div
        id="gh-issues-status"
        className={"pr-status" + (n > 0 && enabled ? " on" : n > 0 ? " paused" : "")}
      >
        {statusText}
      </div>

      <div className="set-row">
        <span className="set-label">Repositories to watch</span>
        <div id="gh-issue-repos-list" className="repo-list">
          {!repos.length ? (
            <div className="repo-empty">
              No repositories yet — add one below to start handling new issues.
            </div>
          ) : (
            repos.map((repo) => (
              <span className="repo-chip" key={repo}>
                <span className="repo-chip-name">{repo}</span>
                <button
                  type="button"
                  className="repo-chip-x"
                  title={"Remove " + repo}
                  aria-label={"Remove " + repo}
                  onClick={() =>
                    saveRepos(
                      repos.filter((r) => r !== repo),
                      "Removed " + repo
                    )
                  }
                >
                  ✕
                </button>
              </span>
            ))
          )}
        </div>
        <div className="repo-add-row">
          <input
            type="text"
            id="gh-issue-repo-new"
            placeholder="owner/name — e.g. mindflockai/MindFlock"
            autoComplete="off"
            spellCheck={false}
            value={repoNew}
            onChange={(e) => setRepoNew(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                addRepo();
              }
            }}
          />
          <button type="button" id="gh-issue-repo-add-btn" className="btn-primary" onClick={addRepo}>
            + Add
          </button>
        </div>
        <span className="set-hint">
          Type a repo as <code>owner/name</code>, then press Enter or click Add. This list
          is separate from PR review's — a repo can be on either, or both.
        </span>
      </div>

      <div className="set-row" id="gh-open-issues-row">
        <span className="set-label">Open issues</span>
        <div className="pr-open-toolbar">
          <button type="button" id="gh-issues-refresh" className="test-btn" onClick={loadOpenIssues}>
            Refresh
          </button>
          <span id="gh-issues-note" className="pr-open-note">{issuesNote}</span>
        </div>
        <div id="gh-issues-list" className="pr-open-list">
          {issuesError ? (
            <div className="repo-empty">{issuesError}</div>
          ) : issues === null ? null : !issuesRepos.length ? (
            <div className="repo-empty">Add a repository above to see its open issues.</div>
          ) : !issues.length ? (
            <div className="repo-empty">No open issues on the watched repositories.</div>
          ) : (
            issues.map((i) => (
              <OpenIssueRow key={(i.repo || "") + i.number} i={i} onStarted={relistIssues} />
            ))
          )}
        </div>
        <span className="set-hint">
          Every open issue on the repositories above, with why auto handling has or hasn't
          picked it up. <strong>Start work</strong> spins up a session for that issue right
          now, bypassing the age / already-handled filters.
        </span>
      </div>

      <details className="pr-advanced">
        <summary>Advanced options</summary>
        <div className="pr-advanced-body">
          <label className="set-row">
            <span className="set-label">Min issue age (minutes)</span>
            <SettingField group="github" field="issue_min_age_minutes" type="number" placeholder="15" />
            <span className="set-hint">
              Grace period after an issue opens before work starts, so you can finish
              writing it. Default 15. Independent of PR review's setting.
            </span>
          </label>
          <label className="set-row">
            <span className="set-label">Poll every (seconds)</span>
            <SettingField group="github" field="issue_poll_interval_seconds" type="number" placeholder="60" />
            <span className="set-hint">
              How often to check GitHub for new issues. Default 60. Independent of PR
              review's setting.
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
              review's list.
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
                  try {
                    const r = await api<Record<string, unknown>>("/api/settings/test/github", {
                      method: "POST",
                    });
                    const bits = ["token: " + (r?.token_source || "none")];
                    if (r?.gh_installed)
                      bits.push(r.gh_authenticated ? "gh authenticated" : "gh not authenticated");
                    else bits.push("gh not installed");
                    if (r?.detail) bits.push(String(r.detail));
                    setGhTest({ testing: false, ok: !!r?.ok, msg: bits.join(" · ") });
                  } catch (e) {
                    setGhTest({ testing: false, ok: false, msg: (e as Error).message });
                  }
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
              account. Set or change it under{" "}
              <button type="button" className="linklike" onClick={() => gotoScreen("repo")}>
                Settings → PR review
              </button>
              ; the button above checks the current connection.
            </span>
          </div>
        </div>
      </details>
    </>
  );
}

function OpenIssueRow({ i, onStarted }: { i: OpenIssue; onStarted(): void }) {
  const [state, setState] = useState<"idle" | "starting" | "started">("idle");
  return (
    <div
      className="pr-open-item"
      title={(i.repo || "") + "#" + i.number + " — " + (i.title || "") + "\nby " + (i.author || "?")}
    >
      <div className="pr-open-main">
        <a
          href={i.url || "#"}
          target="_blank"
          rel="noopener noreferrer"
          className="pr-open-ref"
          title={"Open " + (i.repo || "") + "#" + i.number + " on GitHub"}
        >
          {(i.repo || "") + "#" + i.number}
        </a>
        <span className="pr-open-title">{i.title || ""}</span>
      </div>
      <div className="pr-open-meta">
        <span>
          by {i.author || "?"} · {issueAgeText(i.created_at)}
        </span>
        {i.has_session ? (
          <span className="pr-open-chip on">session open</span>
        ) : i.eligible ? (
          <span className="pr-open-chip ok">queued for auto handling</span>
        ) : (
          (i.reasons || []).map((reason) => (
            <span className="pr-open-chip" key={reason}>
              {reason}
            </span>
          ))
        )}
      </div>
      {i.has_session ? (
        <button type="button" className="btn-primary pr-review-btn" disabled>
          Session open
        </button>
      ) : (
        <button
          type="button"
          className="btn-primary pr-review-btn"
          disabled={state !== "idle"}
          onClick={async () => {
            setState("starting");
            try {
              const r = await api<{ title?: string }>("/api/github/issues/start", {
                json: { repo: i.repo, number: i.number },
              });
              toast("Issue session " + (r?.title || "") + " — provisioning, see the sidebar");
              setState("started");
              // The server already has a provisioning row for it: pull it now
              // instead of leaving the sidebar blank until the next poll.
              refreshInstances();
              setTimeout(onStarted, 5000);
            } catch (err) {
              toast("Start work failed: " + ((err as Error).message || "error"));
              setState("idle");
            }
          }}
        >
          {state === "starting" ? "Starting…" : state === "started" ? "Started" : "Start work"}
        </button>
      )}
    </div>
  );
}
