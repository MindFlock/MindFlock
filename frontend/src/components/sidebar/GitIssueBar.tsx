/** Automated issue-handling bar: sits under the PR-review bar. The switch is
 * the github.issues_enabled setting — the settings addon emits
 * addon.settings.github_toggled on a real change and the ingestion addon
 * reconciles the pipeline process (start / stop / bounce), so flipping it
 * here takes effect on its own, independent of the other toggles. The dot is
 * gold while starting or idle-waiting for a new issue, green while one is
 * actually being worked on. Hidden until repositories are added (Intake →
 * Issues, its own list independent of PR review's) since there is nothing
 * to watch with an empty repo list. Unlike PR review (absent = on), issue
 * handling is opt-in: absent = off. */

import { useUi } from "../../state/store";
import { useGithubToggleBar } from "./useGithubToggleBar";

export function GitIssueBar() {
  const openDialogFor = useUi((s) => s.openDialogFor);
  // Opt-in: absent => off (only an explicit true switches issue handling on).
  const { visible, repos, on, active, starting, busy, toggle } = useGithubToggleBar({
    settingKey: "issues_enabled",
    reposKey: "issue_repos",
    defaultOn: false,
    activeFlag: "issues_active",
    toggleLabel: "Issue handling",
  });
  if (!visible) return null;

  return (
    <div
      id="git-issue-bar"
      title={
        `Automated issue handling — watches newly opened issues on ${repos.length} ` +
        `${repos.length === 1 ? "repository" : "repositories"}, grabs each issue and ` +
        "its comments, and starts work on a fresh branch. Runs on its own — " +
        "ticket ingestion and PR review can stay off."
      }
    >
      <span
        id="git-issue-dot"
        // `active` outranks the switch: an issue forced from Intake is
        // genuinely in flight even with automated handling switched off.
        className={"dc-dot " + (active ? "on" : !on ? "off" : "idle")}
        title={
          active
            ? "An issue is being brought in right now (automated or a forced start)"
            : on
              ? starting
                ? "Switched on — the pipeline is starting"
                : "Waiting for a newly opened issue — turns green while one is being handled"
              : undefined
        }
      />
      <span className="dc-label">Issue Handling</span>
      <span className="dc-actions">
        <button
          id="git-issue-repos-btn"
          className="dc-toggle"
          title="Repositories, open issues and options (Intake → Issues)"
          onClick={() => openDialogFor("intake", "issues")}
        >
          Issues
        </button>
        <label
          className="dc-switch"
          title="Flip to turn automated issue handling on/off — your repositories are kept either way"
        >
          <input
            type="checkbox"
            id="git-issue-toggle"
            checked={on}
            disabled={busy}
            onChange={(e) => toggle(e.target.checked)}
          />
          <span className="dc-slider" />
        </label>
      </span>
    </div>
  );
}
