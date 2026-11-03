/** The Behaviors board's watched repositories (its ⋯ settings,
 * BehaviorsSettings.tsx): one quiet line — "Watching acme/api Live branch:
 * main · acme/web Live branch: staging" — and **Edit repositories** opens one
 * heading per repository with the editor Intake uses (intake/RepoSources.tsx
 * `RepoEditor` on the Verify use: the live branch, the wait after merge, where
 * it runs, notes for the behaviors writer). The live branch is per repo
 * because "what counts as shipped" is a per-repo fact; a blank field
 * inherits. Moved here from the retired Verify dialog (VerifyChrome.tsx), its
 * classes (`vf-sources`, `ik-watching…`) unchanged. */

import { useState } from "react";
import { HeadAction, WorkGroup } from "../intake/kit";
import { RepoEditor, type RepoOverrides } from "../intake/RepoSources";
import { useGithubConnected } from "../../state/queries";
import { openConnectGitHub } from "../../lib/githubConnect";
import { useSettings } from "../settings/useSettings";

export function WatchedRepositories({ liveBranch }: { liveBranch: string }) {
  const s = useSettings();
  const repo = (s.settings.repository || {}) as {
    verify_repos?: string[];
    verify_repo_settings?: RepoOverrides;
  };
  const repos = Array.isArray(repo.verify_repos) ? repo.verify_repos : [];
  const overrides = (repo.verify_repo_settings || {}) as RepoOverrides;
  const [open, setOpen] = useState(false);
  const [editing, setEditing] = useState<string | null>(null);
  const gh = useGithubConnected(repos.length > 0);
  if (!repos.length) return null;
  const liveOf = (r: string) => overrides[r]?.live_branch || liveBranch || "main";
  return (
    <div className="vf-sources" id="vf-repos-list">
      <div className="ik-watching">
        <span className="ik-watching-label">Watching</span>
        <span
          className="ik-watching-list"
          title={repos.map((r) => r + " — live branch: " + liveOf(r)).join("\n")}
        >
          {repos.map((r, i) => (
            <span key={r} className="ik-watching-item">
              {i ? <span className="ik-watching-sep"> · </span> : null}
              <b>{r}</b> <span className="ik-watching-live">Live branch: {liveOf(r)}</span>
            </span>
          ))}
        </span>
        <button
          type="button"
          id="vf-repos-edit"
          className="ik-quiet-btn"
          aria-expanded={open}
          onClick={() => {
            setOpen((v) => !v);
            setEditing(null);
          }}
        >
          {open ? "Close editor" : "Edit repositories"}
        </button>
      </div>
      {gh.data === false ? (
        <p className="set-hint ik-gh-needed" role="note">
          These repositories need GitHub connected —{" "}
          <button type="button" className="linklike" onClick={() => openConnectGitHub()}>
            Connect GitHub
          </button>
          .
        </p>
      ) : null}
      {open ? (
        <div className="ik-groups vf-repo-heads">
          {repos.map((r) => (
            <WorkGroup
              key={r}
              groupKey={r}
              heading
              name={r}
              detail={"Live branch: " + liveOf(r) + (overrides[r]?.target ? " · " + overrides[r]!.target : "")}
              onDetail={() => setEditing(r)}
              detailTitle={"Change how " + r + " is checked"}
              open={editing === r}
              onToggle={() => setEditing((cur) => (cur === r ? null : r))}
              action={
                <HeadAction
                  editing={editing === r}
                  name={r}
                  onClick={() => setEditing((cur) => (cur === r ? null : r))}
                />
              }
              editor={editing === r ? <RepoEditor use="verify" repo={r} liveBranch={liveBranch} /> : undefined}
            />
          ))}
        </div>
      ) : null}
    </div>
  );
}
