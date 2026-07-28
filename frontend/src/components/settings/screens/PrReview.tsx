/** Settings → PR review (partial 109 + section 21's github wiring): the repo
 * list IS the switch; open-PR panel with skip-reason chips + force review. */

import { useEffect, useState } from "react";
import { api } from "../../../api/client";
import { toast } from "../../../lib/toast";
import { usePanelQuery } from "../../../state/queries";
import { SettingField, useSettings } from "../useSettings";
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

function prAgeText(iso?: string): string {
  const t = Date.parse(iso || "");
  if (!isFinite(t)) return "";
  const mins = Math.max(0, Math.round((Date.now() - t) / 60000));
  if (mins < 60) return mins + "m old";
  const h = Math.round(mins / 60);
  if (h < 48) return h + "h old";
  return Math.round(h / 24) + "d old";
}

export function PrReview({ gotoScreen }: ScreenProps) {
  const s = useSettings();
  const gh = (s.settings.github || {}) as {
    enabled?: boolean;
    repos?: string[];
    skip_authors?: string[] | string;
  };
  // Absent => on (the default once repos exist); explicit false => paused.
  const enabled = gh.enabled !== false;
  const repos = Array.isArray(gh.repos) ? gh.repos : [];
  const skipAuthors = Array.isArray(gh.skip_authors)
    ? gh.skip_authors.join(", ")
    : gh.skip_authors || "";

  const [repoNew, setRepoNew] = useState("");
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

  const saveRepos = (list: string[], msg: string) =>
    saveGithub({ repos: list }, msg);

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
    ? "○ Add a repository below to start reviewing your PRs"
    : enabled
      ? `● Active — reviewing PRs in ${n} ${n === 1 ? "repository" : "repositories"}`
      : `‖ Paused — ${n} ${n === 1 ? "repository" : "repositories"} kept; turn Automated review on to resume`;

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
          (Shortcut, Jira, Linear, GitHub Issues or Asana).
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

      <div
        className="set-row set-switch-row"
        id="gh-pr-toggle-row"
        title="Turn automated PR review on or off — your repositories are kept either way"
      >
        <span className="set-label">Automated review</span>
        {/* label wraps only the switch, so clicking the row text no longer flips it */}
        <label className="ca-switch">
          <input
            type="checkbox"
            id="gh-pr-enabled"
            checked={enabled}
            onChange={(e) =>
              saveGithub(
                { enabled: e.target.checked },
                e.target.checked ? "Automated review on" : "Automated review paused"
              )
            }
          />
          <span className="ca-slider" />
        </label>
      </div>
      <div
        id="gh-pr-status"
        className={"pr-status" + (n > 0 && enabled ? " on" : n > 0 ? " paused" : "")}
      >
        {statusText}
      </div>

      <div className="set-row">
        <span className="set-label">Repositories to review</span>
        <div id="gh-repos-list" className="repo-list">
          {!repos.length ? (
            <div className="repo-empty">
              No repositories yet — add one below to start reviewing your PRs.
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
            id="gh-repo-new"
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
          <button type="button" id="gh-repo-add-btn" className="btn-primary" onClick={addRepo}>
            + Add
          </button>
        </div>
        <span className="set-hint">
          Type a repo as <code>owner/name</code>, then press Enter or click Add. Adding one
          turns review on; remove them all to turn it off.
        </span>
      </div>

      <div className="set-row" id="gh-open-prs-row">
        <span className="set-label">Open pull requests</span>
        <div className="pr-open-toolbar">
          <button type="button" id="gh-prs-refresh" className="test-btn" onClick={loadOpenPrs}>
            Refresh
          </button>
          <span id="gh-prs-note" className="pr-open-note">{prsNote}</span>
        </div>
        <div id="gh-prs-list" className="pr-open-list">
          {prsError ? (
            <div className="repo-empty">{prsError}</div>
          ) : prs === null ? null : !prsRepos.length ? (
            <div className="repo-empty">Add a repository above to see its open PRs.</div>
          ) : !prs.length ? (
            <div className="repo-empty">No open pull requests on the watched repositories.</div>
          ) : (
            prs.map((p) => <OpenPrRow key={(p.repo || "") + p.number} p={p} onStarted={relistPrs} />)
          )}
        </div>
        <span className="set-hint">
          Every non-draft open PR on the repositories above, with why auto review has or hasn't
          picked it up. <strong>Begin review</strong> starts a review session for that PR right
          now, bypassing the author / age / already-reviewed filters.
        </span>
      </div>

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
            <span className="set-hint">Falls back to $GH_TOKEN / $GITHUB_TOKEN / `gh auth token`.</span>
          </label>
          <div className="set-row">
            <span className="test-row">
              <button
                type="button"
                id="gh-test-btn"
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
                id="gh-test-result"
                className={"test-result" + (ghTest.msg ? (ghTest.ok ? " ok" : " bad") : "")}
              >
                {ghTest.testing ? "testing…" : ghTest.msg ? (ghTest.ok ? "✓ " : "✗ ") + ghTest.msg : ""}
              </span>
            </span>
            <span className="set-hint">
              Shows where a token would come from and whether gh is authenticated.
            </span>
          </div>
        </div>
      </details>
    </>
  );
}

function OpenPrRow({ p, onStarted }: { p: OpenPr; onStarted(): void }) {
  const [state, setState] = useState<"idle" | "starting" | "started">("idle");
  return (
    <div
      className="pr-open-item"
      title={
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
    >
      <div className="pr-open-main">
        <a
          href={p.url || "#"}
          target="_blank"
          rel="noopener noreferrer"
          className="pr-open-ref"
          title={"Open " + (p.repo || "") + "#" + p.number + " on GitHub"}
        >
          {(p.repo || "") + "#" + p.number}
        </a>
        <span className="pr-open-title">{p.title || ""}</span>
      </div>
      <div className="pr-open-meta">
        <span>
          by {p.author || "?"} · {prAgeText(p.created_at)} · into {p.base_ref || "?"}
        </span>
        {p.has_session ? (
          <span className="pr-open-chip on">session open</span>
        ) : p.eligible ? (
          <span className="pr-open-chip ok">queued for auto review</span>
        ) : (
          (p.reasons || []).map((reason) => (
            <span className="pr-open-chip" key={reason}>
              {reason}
            </span>
          ))
        )}
      </div>
      {p.has_session ? (
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
              const r = await api<{ title?: string }>("/api/github/prs/review", {
                json: { repo: p.repo, number: p.number },
              });
              toast(
                "Review session " + (r?.title || "") + " starting — it will appear in the sidebar shortly"
              );
              setState("started");
              setTimeout(onStarted, 5000);
            } catch (err) {
              toast("Begin review failed: " + ((err as Error).message || "error"));
              setState("idle");
            }
          }}
        >
          {state === "starting" ? "Starting…" : state === "started" ? "Started" : "Begin review"}
        </button>
      )}
    </div>
  );
}
