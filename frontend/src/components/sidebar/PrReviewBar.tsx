/** The sidebar's Pull requests bar — Intake → Pull requests' door and quick
 * switch: its label opens that tab, its switch is the tab's "Auto-address
 * review comments" switch. Sits under the Tickets bar. The switch is the github.enabled setting
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
  // Absent => on (the default once repos exist); explicit false => off.
  const { visible, repos, on, active, starting, busy, toggle } = useGithubToggleBar({
    settingKey: "enabled",
    reposKey: "repos",
    defaultOn: true,
    activeFlag: "pr_active",
    toggleLabel: "Auto-address review comments",
  });
  if (!visible) return null;

  return (
    <div
      id="pr-review-bar"
      title={
        `GitHub PRs — addresses review comments on your open PRs in ${repos.length} ` +
        `${repos.length === 1 ? "repository" : "repositories"}.`
      }
    >
      <span
        id="pr-review-dot"
        // `active` outranks the switch: a review forced from Intake is
        // genuinely in flight even with automated review switched off.
        className={"dc-dot " + (active ? "on" : !on ? "off" : "idle")}
        title={
          active
            ? "A session is addressing a PR's comments right now"
            : on
              ? starting
                ? "Switched on — starting"
                : "Waiting for review comments on your PRs — turns green while one is being handled"
              : undefined
        }
      />
      <button
        id="pr-review-prs-btn"
        type="button"
        className="dc-label dc-open"
        title="Open Intake → GitHub PRs"
        onClick={() => openDialogFor("intake", "prs")}
      >
        GitHub PRs
      </button>
      <span className="dc-actions">
        <label
          className="dc-switch"
          title="Auto-address review comments — the same switch as Intake → GitHub PRs (your repositories are kept either way)"
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
