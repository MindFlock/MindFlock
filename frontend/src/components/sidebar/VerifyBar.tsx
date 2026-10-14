/** Verify bar: the other end of the pipeline the ingestion bars start.
 *
 * Same anatomy as PrReviewBar and GitIssueBar — dot, label, a button into the
 * surface, a switch — because it is the same KIND of thing: an automation that
 * runs on its own and that a person needs to be able to see the state of, and
 * pause, without opening anything.
 *
 * The switch is `repository.verify_enabled`. It pauses the AUTOMATIC half only:
 * no plan is written when a branch lands, and the liveness loop stops moving
 * plans to `due` (backend/web/server.py). Nothing is deleted — the tracked
 * repos, the plans and every recorded answer survive, so flipping it back on
 * resumes rather than re-sets-up. Write plan, Run and answering a step all still
 * work while it is off, exactly as a forced PR review runs with automated review
 * switched off; a switch that disabled the buttons too would give "off" two
 * meanings, and the one people want is "stop doing things without me".
 *
 * VISIBILITY. Unlike the GitHub bars, an empty repo list is NOT enough to hide
 * this one: a plan can exist without any repo being tracked — written by hand
 * from the dialog, or by a repo whose own committed `.mindflock.toml` opted in,
 * which is the only opt-in available to a checkout with no GitHub slug. Hiding
 * on `repos.length === 0` would have hidden the bar from exactly those users,
 * along with the switch that governs them. So it shows when there is anything to
 * govern: a tracked repo, or a plan.
 */

import { useEffect } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../../api/client";
import { errMsg } from "../../lib/format";
import { useTestPlans } from "../../state/queries";
import { useUi } from "../../state/store";
import { toast } from "../../lib/toast";
import { dueCount, failingCount } from "../dialogs/verify";

interface RepositorySettings {
  verify_enabled?: boolean;
  verify_repos?: string[];
}

/** The repository half of the settings Verify runs on (`verify_enabled`,
 * `verify_repos`) — one query for this bar. */
export function useVerifySettings() {
  return useQuery({
    queryKey: ["verify-settings"],
    queryFn: async () => {
      const r = await api<{ settings?: { repository?: RepositorySettings } }>(
        "/api/settings"
      );
      return r?.settings?.repository || {};
    },
    refetchInterval: 30_000,
    retry: false,
  });
}

export function VerifyBar() {
  const openDialogFor = useUi((s) => s.openDialogFor);
  const openDialog = useUi((s) => s.openDialog);
  const qc = useQueryClient();

  const { data: repo, refetch } = useVerifySettings();

  // The same query the top-bar badge already keeps warm, so reading it here
  // costs no extra polling — and guarantees the bar and the badge can never
  // disagree about how much is outstanding.
  const { data: plansData } = useTestPlans();

  // The Verify dialog and the Settings screens both edit these without touching
  // this query — re-read whenever a dialog closes so the bar reflects it at once.
  useEffect(() => {
    if (openDialog === null) refetch();
  }, [openDialog, refetch]);

  const repos = Array.isArray(repo?.verify_repos) ? repo.verify_repos : [];
  const plans = plansData?.plans || [];
  // Absent => on, matching the backend default.
  const on = repo?.verify_enabled !== false;
  const due = dueCount(plans);
  const running = plans.some((p) => p.state === "running");
  // SOMETHING PUSHED AND IS BROKEN — the most valuable thing this feature
  // produces. The top bar's badge counts it among "needs you or failed"; this
  // red pill says how many of those are failures: failed steps AND checklists
  // that couldn't be written — the "Failed" group, by the same rule.
  const broken = failingCount(plans);

  if (!repo || (repos.length === 0 && plans.length === 0)) return null;

  const toggle = async (enable: boolean) => {
    try {
      await api("/api/settings", { json: { repository: { verify_enabled: enable } } });
      toast(enable ? "Automatic checking on" : "Automatic checking paused");
    } catch (err) {
      toast("Verify " + (enable ? "on" : "off") + " failed: " + errMsg(err));
    } finally {
      refetch();
      // The switch changes what the liveness loop will do on its next pass, and
      // the dialog reads the same settings — pull both rather than waiting out
      // the polls.
      qc.invalidateQueries({ queryKey: ["test-plans"] });
    }
  };

  return (
    <div
      id="verify-bar"
      title={
        "Verify — a checklist for each pushed branch; the agent checks what it can, the rest are yours. " +
        (repos.length
          ? `Watching ${repos.length} ${repos.length === 1 ? "repository" : "repositories"}.`
          : "No repositories watched.")
      }
    >
      <span
        id="verify-dot"
        // `running` outranks the switch, the same way a forced PR review does:
        // an agent checking a plan right now is genuinely in flight even with
        // automatic verification switched off.
        // Three states, matching PrReviewBar and GitIssueBar exactly: green =
        // something is happening, grey = switched off, amber = armed and
        // waiting. It used to go grey for BOTH "paused" and "on, nothing
        // outstanding" — the same colour for the two opposite answers to "is
        // this thing working?", and the opposite of the two bars directly above
        // it. How much is outstanding is the count's job, not the dot's.
        className={
          "dc-dot " + (running ? "on" : broken ? "dc-error" : !on ? "off" : "idle")
        }
        role="img"
        aria-label={
          running
            ? "Verify: an agent is checking a checklist"
            : broken
              ? "Verify: " + broken + (broken === 1 ? " checklist" : " checklists") + " failed"
              : !on
                ? "Verify: switched off"
                : "Verify: on, " + (due ? due + " need you" : "nothing outstanding")
        }
        title={
          running
            ? "An agent is working through a checklist right now"
            : broken
              ? broken + (broken === 1 ? " checklist" : " checklists") +
                " failed — open Verify to see which"
              : !on
                ? // Deliberately not the word a removed status line used: a blunt
                  // bundle-wide guard in test_pr_review_settings.py watches for it
                  // coming back, and this tooltip is not what it is guarding.
                  "Paused: no new checklists on push"
                : due
                  ? due + (due === 1 ? " checklist needs" : " checklists need") + " you"
                  : "On — nothing to check right now"
        }
      />
      {/* The bar's name IS its door (the pattern every automation bar shares):
          one control under one name, rather than a label beside a second
          button that opened the same dialog under another word. */}
      <button
        id="verify-plans-btn"
        type="button"
        className="dc-label dc-open"
        title="Open Verify"
        onClick={() => openDialogFor("verify")}
      >
        Verify
      </button>
      <span className="dc-actions">
        {/* NO "how many to check" pill here. It was the same number, from the
            same rule, as the one on the top bar's Verify button — two badges a
            few pixels apart saying one thing, which reads as two things until
            you check. The top bar's is the one that survives: it is on the
            surface you look at when you are not thinking about verifying,
            which is the whole reason the count exists.
            The failure pill below is NOT that number and stays: "3 to check"
            and "1 of them is broken" are different questions with different
            urgencies, and nothing else on screen says the second one. */}
        {broken > 0 ? (
          <span
            className="dc-count dc-count-bad"
            title={
              broken + (broken === 1 ? " checklist" : " checklists") +
              " failed — a step failed, or it couldn't be written"
            }
          >
            {"\u2717" + broken}
          </span>
        ) : null}
        <label
          className="dc-switch"
          title="Pause or resume new checklists on push — nothing is deleted"
        >
          <input
            type="checkbox"
            id="verify-toggle"
            checked={on}
            onChange={(e) => void toggle(e.target.checked)}
          />
          <span className="dc-slider" />
        </label>
      </span>
    </div>
  );
}
