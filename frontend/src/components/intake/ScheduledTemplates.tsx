/** Intake → Auto-start's last section: templates that start a session on
 * their own (a schedule). The rest of the tab is what tickets, PRs and issues
 * will start; a nightly template is the same kind of answer to "what starts
 * by itself?", and it was only visible inside the Templates dialog.
 *
 * Each row: name · when · next run, with Run now (the same call as the
 * Templates card and the palette's "Run template: …"). */

import { useEffect, useState } from "react";
import {
  ensureTemplates,
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
  /** "Every day at 21:00" / "Weekdays at 21:00". */
  when: string;
  /** "next today 21:00", "starts on mac-mini", or "". */
  next: string;
  /** Why Run now can't work ("" when it can). */
  problem: string;
}

/** The scheduled templates, in the list's order. Pure, for tests. */
export function scheduledRows(
  templates: Template[],
  status: TemplateRunsStatus,
  now: Date = new Date()
): ScheduledRow[] {
  return templates
    .filter((t) => !!t.schedule?.at)
    .map((t) => {
      const info = status.runs[t.name.trim().toLowerCase()] || {};
      const next =
        !status.runs_here && status.runs_on
          ? "starts on " + status.runs_on
          : info.next_at
            ? "next " + whenLabel(info.next_at, now)
            : "";
      return { name: t.name, when: scheduleLabel(t.schedule), next, problem: runProblem(t) };
    });
}

export function ScheduledTemplates() {
  const [templates, setTemplates] = useState<Template[]>(() => templatesNow());
  const [status, setStatus] = useState<TemplateRunsStatus>(() => templateRunsNow());
  const [running, setRunning] = useState("");

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
          onClick={() => useUi.getState().openDialogFor("templates")}
          title="Open Templates to change or stop a schedule"
        >
          Open Templates ›
        </button>
      </div>
      {rows.map((r) => (
        <div className="pr-open-item" key={r.name} title={r.name + " — " + r.when}>
          <div className="pr-open-main">
            <span className="pr-open-title">{r.name}</span>
          </div>
          <div className="pr-open-meta">
            <span className="ik-scheduled-when">{[r.when, r.next].filter(Boolean).join(" · ")}</span>
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
