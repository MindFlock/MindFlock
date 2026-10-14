/** Settings → Workspace (partial 108): checkout dir + base branches. */

import { SettingField } from "../useSettings";
import type { ScreenProps } from "../SettingsDialog";
import { AdvancedFold, LearnMore } from "../settingsMode";

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
        Where sessions keep their copy of your repo, and which branches they start from.
      </p>
      <label className="set-row">
        <span className="set-label">Workspace folder</span>
        <SettingField group="repository" field="workspace_dir" placeholder="./workspaces" />
        <span className="set-hint">
          Where ticket sessions get their separate worktree. A full path (like
          ~/mindflock/workspaces) is clearest.
        </span>
      </label>
      <label className="set-row">
        <span className="set-label">Default base branch</span>
        <SettingField group="repository" field="base_branch" placeholder="main" />
        <span className="set-hint">New session branches start from this, unless told otherwise.</span>
      </label>
      <label className="set-row">
        <span className="set-label">Default PR base branch</span>
        <SettingField group="repository" field="pr_base_branch" placeholder="use the session's own base" />
        <span className="set-hint">
          Where <strong>Open PR</strong> points. Blank = the branch the session started from.
        </span>
      </label>
      {/* What "shipped" means, fast-track's default reach and its hook
          retries: tuning for a flow that already works — folded. */}
      <AdvancedFold id="workspace-advanced">
        <label className="set-row">
          <span className="set-label">Live branch</span>
          <SettingField group="repository" field="live_branch" placeholder="main" />
          <span className="set-hint">
            The branch that means <em>shipped</em>. Verify asks you to check a change once it
            lands here. Blank = the PR base branch, then the default base branch.
          </span>
        </label>
        <label className="set-row">
          <span className="set-label">Fast-track goes as far as</span>
          <SettingField
            group="repository"
            field="fasttrack_depth"
            options={[
              { value: "", label: "Off (default)" },
              { value: "commit", label: "Commit" },
              { value: "push", label: "Push" },
              { value: "pr", label: "Open PR" },
              { value: "merge", label: "Merge when green" },
            ]}
          />
          <span className="set-hint">
            <strong>New sessions start Off; batches and ticket runs start at this default.</strong>{" "}
            Change one session any time with its <strong>⏩</strong> button.
          </span>
          <LearnMore>
            A batch is a list in the New dialog or Intake's <em>Start together</em>. Fast-track
            waits for the agent to finish, then commits, pushes and carries on to this step. A
            ticket source keeps its own <em>Fast-track tickets to</em> (Intake), which never
            merges.
          </LearnMore>
        </label>
        <label className="set-row">
          <span className="set-label">Retryable pre-commit hooks</span>
          <SettingField
            group="repository"
            field="precommit_retry_hooks"
            placeholder="gitnexus-index"
          />
          <span className="set-hint">
            Hooks fast-track may skip when they fail without changing any files.
          </span>
          <LearnMore>
            Comma-separated pre-commit hook <strong>IDs</strong> (not display names:{" "}
            <code>Black format</code> is the hook <code>black</code>). Fast-track retries the
            commit once, then re-runs it with <code>SKIP=&lt;id&gt;</code> and says which hook it
            skipped. Test and secret-scanning hooks are refused here.
          </LearnMore>
        </label>
      </AdvancedFold>
    </>
  );
}
