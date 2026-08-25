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
import { dueCount, verdictOf } from "../dialogs/verify";

interface RepositorySettings {
  verify_enabled?: boolean;
  verify_repos?: string[];
}

export function VerifyBar() {
  const openDialogFor = useUi((s) => s.openDialogFor);
  const openDialog = useUi((s) => s.openDialog);
  const qc = useQueryClient();

  const { data: repo, refetch } = useQuery({
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
  // SOMETHING SHIPPED AND IS BROKEN — the most valuable thing this feature
  // produces, and the only state of it that never left the dialog. `dueCount`
  // deliberately excludes a recorded failure (a fail IS an answer, so the plan
  // is not "not checked yet"), and the loud red group lives inside a modal
  // nobody has open. So the bar said "on, nothing outstanding" over a checklist
  // that had found a real defect.
  const broken = plans.filter((p) => verdictOf(p) === "fail").length;

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
        "Verify — writes a checklist when a session branch ships, then hands you " +
        "the steps an agent cannot honestly check. " +
        (repos.length
          ? `Tracking ${repos.length} ${repos.length === 1 ? "repository" : "repositories"}.`
          : "No repositories tracked; the checklists here were asked for by hand or by a repo's own .mindflock.toml.")
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
              ? "Verify: " + broken + " shipped " + (broken === 1 ? "change" : "changes") +
                " failed its checklist"
              : !on
                ? "Verify: switched off"
                : "Verify: on, " + (due ? due + " not checked yet" : "nothing outstanding")
        }
        title={
          running
            ? "An agent is working through a checklist right now"
            : broken
              ? broken + (broken === 1 ? " shipped change" : " shipped changes") +
                " did not do what its checklist expected — open Checklists to see" +
                " which step, and what was observed"
              : !on
                ? // Deliberately not the word a removed status line used: a blunt
                  // bundle-wide guard in test_pr_review_settings.py watches for it
                  // coming back, and this tooltip is not what it is guarding.
                  "Switched off — nothing is written when a branch ships, and nothing new turns up to check"
                : due
                  ? due + (due === 1 ? " shipped change has" : " shipped changes have") +
                    " not been checked"
                  : "On, and nothing is outstanding — a checklist appears here when a branch ships"
        }
      />
      <span className="dc-label">Verify</span>
      <span className="dc-actions">
        <button
          id="verify-plans-btn"
          className="dc-toggle"
          title="Checklists for what shipped, tracked repositories and what counts as live (Alt+V)"
          onClick={() => openDialogFor("verify")}
        >
          Checklists
          {/* NO "how many to check" pill here. It was the same number, from the
              same rule, as the one on the top bar's Verify button — two badges
              a few pixels apart saying one thing, which reads as two things
              until you check. The top bar's is the one that survives: it is on
              the surface you look at when you are not thinking about verifying,
              which is the whole reason the count exists.
              The failure pill below is NOT that number and stays: "3 to check"
              and "1 of them is broken" are different questions with different
              urgencies, and nothing else on screen says the second one. */}
          {broken > 0 ? (
            <span
              className="dc-count dc-count-bad"
              title={
                broken + (broken === 1 ? " checklist has" : " checklists have") +
                " a step that failed"
              }
            >
              {"\u2717" + broken}
            </span>
          ) : null}
        </button>
        <label
          className="dc-switch"
          title="Flip to pause automatic checking — your repositories, checklists and answers are kept either way, and writing one by hand, running and answering all still work"
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
