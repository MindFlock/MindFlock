/** The sidebar's Pull requests bar — Intake → Pull requests' door and quick
 * switch: its label opens that tab, its switch is the tab's "Automated review"
 * switch. Sits under the Tickets bar. The switch is the github.enabled setting
 * — the settings addon emits addon.settings.github_toggled on a real change
 * and the ingestion addon reconciles the pipeline process (start / stop /
 * bounce), so flipping it here takes effect on its own, independent of the
 * ticket toggle. The dot is gold while starting or idle-waiting for a
 * reviewable PR, green while one is actually being handled. Hidden until PR
 * review is set up (a repository added in Intake → Pull requests) since review
 * can't run with an empty repo list. */

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
    toggleLabel: "Automated review",
  });
  if (!visible) return null;

  return (
    <div
      id="pr-review-bar"
      title={
        `Pull requests — automated review watches your open PRs on ${repos.length} ` +
        `${repos.length === 1 ? "repository" : "repositories"} and starts a review ` +
        "session for each."
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
      <button
        id="pr-review-prs-btn"
        type="button"
        className="dc-label dc-open"
        title="Open Intake → Pull requests"
        onClick={() => openDialogFor("intake", "prs")}
      >
        Pull requests
      </button>
      <span className="dc-actions">
        <label
          className="dc-switch"
          title="Automated review — the same switch as Intake → Pull requests (your repositories are kept either way)"
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
