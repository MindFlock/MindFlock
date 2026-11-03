/** Intake → Auto-start's last line: how many things start by themselves on a
 * timer, and the way to them — "2 scheduled — Open Schedule ›". The rest of
 * the tab is what tickets, PRs and issues will start; a nightly template or
 * a scheduled saved chain is the same kind of answer to "what starts by
 * itself?", but its home is Schedule (the top bar): it used to be a second,
 * read-only list here with its own Run now, which is one list too many.
 *
 * The helpers (scheduledRows, nextScheduledText, useScheduledTemplates) stay:
 * the Intake banner and the Auto-start count say what runs next. */

import { useEffect, useState } from "react";
import {
  ensureTemplates,
  hasSchedule,
  runProblem,
  scheduleLabel,
  templateScheduleLine,
  TEMPLATES_CHANGED,
  templateRunsNow,
  templatesNow,
  whenLabel,
  type Template,
  type TemplateRunsStatus,
} from "../../lib/templates";
import { useUi } from "../../state/store";
import { useFlightPlans } from "../../state/queries";
import type { FlightPlan } from "../../lib/flights";

export interface ScheduledRow {
  name: string;
  /** "Every day at 21:00" / "Weekdays at 21:00" (plus the zone when the
   * server names it: "Every day at 07:30 EDT"). */
  when: string;
  /** "next today 21:00", "starts on mac-mini", or "". */
  next: string;
  /** Why Run now can't work ("" when it can). */
  problem: string;
  /** "tomorrow's run skipped" — set only while Skip next holds a slot. */
  skipped?: string;
  /** THE schedule line (lib/templates' templateScheduleLine), word for word
   * what the Schedule card and New's ⏰ chip say. */
  line: string;
}

/** "tomorrow's run skipped" / "Fri's run skipped" for a skipped slot. */
export function skippedText(epochSec: number | null | undefined, now: Date = new Date()): string {
  const label = whenLabel(epochSec, now);
  if (!label) return "";
  return label.split(" ")[0] + "'s run skipped";
}

/** The one-line summary of a scheduled template, as the Schedule dialog
 * says it — "Every day at 07:30 EDT · next Fri 07:30 (tomorrow skipped)".
 * It is `templateScheduleLine`, the one line every surface shares; Intake
 * used to build a near-copy of its own. */
export function scheduleLine(row: ScheduledRow): string {
  return row.line;
}

/** The scheduled templates, in the list's order. Pure, for tests. */
export function scheduledRows(
  templates: Template[],
  status: TemplateRunsStatus,
  now: Date = new Date()
): ScheduledRow[] {
  return templates
    // A cron schedule is a schedule too (`.at` alone left every one out).
    .filter(hasSchedule)
    .map((t) => {
      const info = status.runs[t.name.trim().toLowerCase()] || {};
      const next = t.schedule?.paused
        ? "paused"
        : !status.runs_here && status.runs_on
          ? "starts on " + status.runs_on
          : info.next_at
            ? "next " + whenLabel(info.next_at, now)
            : "";
      const tz = (info as { tz?: string }).tz;
      const skipping = !t.schedule?.paused && !info.paused ? info.skipping_at : 0;
      const row: ScheduledRow = {
        name: t.name,
        when: (info.schedule_label || scheduleLabel(t.schedule, now)) + (tz && (t.schedule?.at || t.schedule?.once) ? " " + tz : ""),
        next,
        problem: runProblem(t),
        line: templateScheduleLine(t, status, now),
      };
      if (skipping) row.skipped = skippedText(skipping, now);
      return row;
    });
}

/** The scheduled template that starts on its own next, on this device, for
 * the Intake banner and the Auto-start count: "nightly tests, Fri 07:30 —
 * tomorrow's run skipped". "" when none is due here. */
export function nextScheduledText(
  templates: Template[],
  status: TemplateRunsStatus,
  now: Date = new Date()
): string {
  if (!status.runs_here && status.runs_on) return "";
  let best: { t: Template; at: number; skip: number } | null = null;
  for (const t of templates.filter(hasSchedule)) {
    const info = status.runs[t.name.trim().toLowerCase()] || {};
    if (t.schedule?.paused || info.paused || !info.next_at) continue;
    if (!best || info.next_at < best.at) best = { t, at: info.next_at, skip: info.skipping_at || 0 };
  }
  if (!best) return "";
  return (
    best.t.name + ", " + whenLabel(best.at, now) + (best.skip ? " — " + skippedText(best.skip, now) : "")
  );
}

/** The templates and their run status, kept current — shared by this list,
 * the Intake banner and the Auto-start count. */
export function useScheduledTemplates(): { templates: Template[]; status: TemplateRunsStatus } {
  const [templates, setTemplates] = useState<Template[]>(() => templatesNow());
  const [status, setStatus] = useState<TemplateRunsStatus>(() => templateRunsNow());
  useEffect(() => {
    const reload = () => {
      setTemplates(templatesNow());
      setStatus(templateRunsNow());
    };
    document.addEventListener(TEMPLATES_CHANGED, reload);
    void ensureTemplates()
      .then(reload)
      .catch(() => {});
    return () => document.removeEventListener(TEMPLATES_CHANGED, reload);
  }, []);
  return { templates, status };
}

/** How many things Schedule lists as starting on a timer: the templates
 * with a schedule, and the saved chains whose trigger is a schedule that has
 * its time set. Pure, for tests. */
export function scheduledCount(templates: Template[], plans: readonly FlightPlan[] | null | undefined): number {
  const stops = (plans || []).filter((p) => p.trigger?.kind === "schedule" && !!p.trigger.schedule).length;
  return templates.filter(hasSchedule).length + stops;
}

/** "2 scheduled" / "Nothing on a timer yet" — the line's words. */
export function scheduledLineText(n: number): string {
  return n > 0 ? n + " scheduled" : "Nothing on a timer yet";
}

export function ScheduledTemplates() {
  const { templates } = useScheduledTemplates();
  const plans = useFlightPlans().data;
  const n = scheduledCount(templates, plans);
  return (
    <div className="ik-queue-section ik-queue-scheduled-line" id="ik-queue-scheduled">
      <div className="ik-queue-head">
        <span className="ik-queue-kind">{scheduledLineText(n)}</span>
        <button
          type="button"
          className="test-btn ik-queue-goto"
          onClick={() => useUi.getState().openDialogFor("schedule")}
          title="Schedule — templates and saved routes that start by themselves: run, pause, skip or change them there"
        >
          Open Schedule ›
        </button>
      </div>
    </div>
  );
}
