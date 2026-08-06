/** Settings → Workspace (partial 108): checkout dir + base branches. */

import { SettingField } from "../useSettings";
import type { ScreenProps } from "../SettingsDialog";

export function Workspace({ gotoScreen }: ScreenProps) {
  return (
    <>
      <h3 className="set-section-title">Workspace</h3>
      <div className="caps-gate" data-caps-gate="git">
        <p>
          Install <strong>Git</strong> to get access to these features — isolated worktree
          checkouts per session, base branches, diffs, commits and pull requests. Until then,
          sessions simply run in the folder you pick.
        </p>
        <p>
          Install it (e.g. <code>sudo apt install git</code> / <code>brew install git</code>),
          then reload this page.{" "}
          <button type="button" className="linklike" data-goto-screen="doctor" onClick={() => gotoScreen("doctor")}>
            Check in Doctor
          </button>
        </p>
      </div>
      <p className="set-hint set-block-hint">
        Where MindFlock puts the working checkouts your <em>coding sessions</em> run in. Which
        repo a session clones comes from its <strong>Ticketing source</strong> (each source
        names its own repo) or the repo you pick when starting a manual session — so there's
        nothing to set here per repo.
      </p>
      <label className="set-row">
        <span className="set-label">Workspace dir</span>
        <SettingField group="repository" field="workspace_dir" placeholder="./workspaces" />
        <span className="set-hint">Folder on disk where session checkouts live.</span>
      </label>
      <label className="set-row">
        <span className="set-label">Default base branch</span>
        <SettingField group="repository" field="base_branch" placeholder="main" />
        <span className="set-hint">
          Branch new session branches are cut from when the source doesn't specify one.
        </span>
      </label>
      <label className="set-row">
        <span className="set-label">Default PR base branch</span>
        <SettingField group="repository" field="pr_base_branch" placeholder="use the session's own base" />
        <span className="set-hint">
          Branch the <strong>Make PR</strong> button targets. Set it to e.g. <code>staging</code>{" "}
          to always PR there. Blank = PR into whatever branch the session was created from.
        </span>
      </label>
      <label className="set-row">
        <span className="set-label">Fast-track goes as far as</span>
        <SettingField
          group="repository"
          field="fasttrack_depth"
          options={[
            { value: "", label: "Open PR (default)" },
            { value: "commit", label: "Commit only" },
            { value: "push", label: "…then push" },
            { value: "pr", label: "…then open PR" },
            { value: "merge", label: "…then merge" },
          ]}
        />
        <span className="set-hint">
          Where the <strong>⏩</strong> button stops. It waits for the agent to finish, then
          commits, pushes and carries on to this rung. Merging is irreversible, so it is never
          the default and an intake <em>source</em> can't default to it — only an individual item.
        </span>
      </label>
      <label className="set-row">
        <span className="set-label">Retryable pre-commit hooks</span>
        <SettingField
          group="repository"
          field="precommit_retry_hooks"
          placeholder="gitnexus-index"
        />
        <span className="set-hint">
          Comma-separated pre-commit hook <strong>IDs</strong> (not display names — pre-commit's{" "}
          <code>name:</code> is free text, so <code>Black format</code> is the hook{" "}
          <code>black</code>). When one of these fails <em>without changing any files</em>,
          fast-track retries the commit once and then re-runs it with{" "}
          <code>SKIP=&lt;id&gt;</code> so the commit can land, and says which hook it skipped.
          Test and secret-scanning hooks are refused here — a failing test always stops the run.
        </span>
      </label>
    </>
  );
}
