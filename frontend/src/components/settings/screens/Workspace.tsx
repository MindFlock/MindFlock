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
    </>
  );
}
