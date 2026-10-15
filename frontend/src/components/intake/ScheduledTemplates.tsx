/** Intake → Auto-start's last section: templates that start a session on
 * their own (a schedule). The rest of the tab is what tickets, PRs and issues
 * will start; a nightly template is the same kind of answer to "what starts
 * by itself?" — its home is Schedule (the top bar), linked from here.
 *
 * Each row: name · when · next run, with Run now (the same call as the
 * Templates card and the palette's "Run template: …"). */

import { useEffect, useState } from "react";
import {
  ensureTemplates,
  hasSchedule,
  runProblem,
  scheduleLabel,
  TEMPLATES_CHANGED,
  templateRunsNow,
  templatesNow,
  whenLabel,
  type Template,
  type TemplateRunsStatus,
} from "../../lib/templates";
import { runTemplateNow } from "../../lib/templateRun";
import { useUi } from "../../state/store";

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
}

/** "tomorrow's run skipped" / "Fri's run skipped" for a skipped slot. */
export function skippedText(epochSec: number | null | undefined, now: Date = new Date()): string {
  const label = whenLabel(epochSec, now);
  if (!label) return "";
  return label.split(" ")[0] + "'s run skipped";
}

/** The one-line summary of a scheduled template, as the Schedule dialog
 * says it: cadence, when next, and a skipped slot — "Every day at 07:30 EDT ·
 * next Fri 07:30 · tomorrow's run skipped".
 *
 * Written here until the Schedule surface exports one shared line
 * (`templateScheduleLine()` in lib/templates.ts, round 5); swap this for it
 * when it lands. */
export function scheduleLine(row: ScheduledRow): string {
  return [row.when, row.next, row.skipped].filter(Boolean).join(" · ");
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
        when: (info.schedule_label || scheduleLabel(t.schedule)) + (tz && t.schedule?.at ? " " + tz : ""),
        next,
        problem: runProblem(t),
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

export function ScheduledTemplates() {
  const { templates, status } = useScheduledTemplates();
  const [running, setRunning] = useState("");

  const rows = scheduledRows(templates, status);
  if (!rows.length) return null;
  return (
    <div className="ik-queue-section" id="ik-queue-scheduled">
      <div className="ik-queue-head">
        <span className="ik-queue-kind">Scheduled templates</span>
        <span className="ik-tab-count">{rows.length}</span>
        <button
          type="button"
          className="test-btn ik-queue-goto"
          onClick={() => useUi.getState().openDialogFor("schedule")}
          title="Open Schedule to change, pause or skip a schedule"
        >
          Open Schedule ›
        </button>
      </div>
      {rows.map((r) => (
        <div className="pr-open-item" key={r.name} title={r.name + " — " + scheduleLine(r)}>
          <div className="pr-open-main">
            <span className="pr-open-title">{r.name}</span>
          </div>
          <div className="pr-open-meta">
            <span className="ik-scheduled-when">{scheduleLine(r)}</span>
          </div>
          <div className="ik-item-start">
            <button
              type="button"
              className="btn-primary pr-review-btn"
              disabled={!!r.problem || running === r.name}
              title={r.problem || "Run now — starts a new session from this template"}
              onClick={async () => {
                setRunning(r.name);
                try {
                  await runTemplateNow(r.name);
                } finally {
                  setRunning("");
                }
              }}
            >
              {running === r.name ? "Starting…" : "Run now"}
            </button>
          </div>
        </div>
      ))}
    </div>
  );
}
