/** Automated-PR-review bar: sits under the ticket-ingestion bar. The switch
 * is the github.enabled setting — the settings addon emits
 * addon.settings.github_toggled on a real change and the ingestion addon
 * reconciles the pipeline process (start / stop / bounce), so flipping it
 * here takes effect on its own, independent of the ticket toggle. The dot is
 * gold while starting or idle-waiting for a reviewable PR, green while one is
 * actually being handled. Hidden until PR review is set up (a repository
 * added in Intake → Pull requests) since review can't run with an empty repo
 * list. */

import { useUi } from "../../state/store";
import { useGithubToggleBar } from "./useGithubToggleBar";

export function PrReviewBar() {
  const openDialogFor = useUi((s) => s.openDialogFor);
  // Absent => on (the default once repos exist); explicit false => paused.
  const { visible, repos, on, active, starting, busy, toggle } = useGithubToggleBar({
    settingKey: "enabled",
    reposKey: "repos",
    defaultOn: true,
    activeFlag: "pr_active",
    toggleLabel: "PR review",
  });
  if (!visible) return null;

  return (
    <div
      id="pr-review-bar"
      title={
        `Automated PR review — watches your open pull requests on ${repos.length} ` +
        `${repos.length === 1 ? "repository" : "repositories"} and spins up review ` +
        "sessions. Runs on its own — ticket ingestion can stay off."
      }
    >
      <span
        id="pr-review-dot"
        // `active` outranks the switch: a review forced from Intake is
        // genuinely in flight even with automated review switched off.
        className={"dc-dot " + (active ? "on" : !on ? "off" : "idle")}
        title={
          active
            ? "A pull request is being brought in for review right now (automated or a forced start)"
            : on
              ? starting
                ? "Switched on — the review pipeline is starting"
                : "Waiting for an open PR with actionable review comments — turns green while one is being handled"
              : undefined
        }
      />
      <span className="dc-label">PR Review</span>
      <span className="dc-actions">
        <button
          id="pr-review-prs-btn"
          className="dc-toggle"
          title="Repositories, open PRs and review options (Intake → Pull requests)"
          onClick={() => openDialogFor("intake", "prs")}
        >
          PRs
        </button>
        <label
          className="dc-switch"
          title="Flip to turn automated PR review on/off — your repositories are kept either way"
        >
          <input
            type="checkbox"
            id="pr-review-toggle"
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
