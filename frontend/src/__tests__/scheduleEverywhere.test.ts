/** "Schedule a session…" from the palette, and scheduled templates listed in
 * Intake → Auto-start (what starts on its own). */

/// <reference types="vite/client" />
import { describe, expect, it } from "vitest";
import { scheduledRows } from "../components/intake/ScheduledTemplates";
import PALETTE from "../components/palette/CommandPalette.tsx?raw";
import TEMPLATES_DIALOG from "../components/dialogs/TemplatesDialog.tsx?raw";
import QUEUE from "../components/intake/QueueTab.tsx?raw";
import SCHEDULED from "../components/intake/ScheduledTemplates.tsx?raw";

const NOW = new Date(2026, 9, 14, 9, 0);
const at = (h: number, m = 0, dayOffset = 0) =>
  new Date(2026, 9, 14 + dayOffset, h, m).getTime() / 1000;

describe("scheduledRows", () => {
  const list = [
    { name: "Nightly deps", repo_path: "/r", schedule: { at: "21:00", days: "daily" as const } },
    { name: "One-off", repo_path: "/r", prompt: "x" },
    { name: "Standup notes", schedule: { at: "08:30", days: "weekdays" as const } },
  ];

  it("lists only templates with a schedule: name · when · next run", () => {
    const rows = scheduledRows(
      list,
      { runs: { "nightly deps": { next_at: at(21) }, "standup notes": { next_at: at(8, 30, 1) } }, runs_here: true, runs_on: "" },
      NOW
    );
    expect(rows).toEqual([
      { name: "Nightly deps", when: "Every day at 21:00", next: "next today 21:00", problem: "" },
      {
        name: "Standup notes",
        when: "Weekdays at 08:30",
        next: "next tomorrow 08:30",
        problem: "Pick a folder for this template first",
      },
    ]);
  });

  it("lists a cron schedule too, in the server's words, and says paused", () => {
    const rows = scheduledRows(
      [
        { name: "Watch", repo_path: "/r", prompt: "p", schedule: { cron: "*/30 * * * *" } },
        { name: "Held", repo_path: "/r", prompt: "p", schedule: { at: "09:00", days: "daily", paused: true } },
      ],
      { runs: { watch: { schedule_label: "Every 30 minutes", next_at: at(9, 30) } }, runs_here: true, runs_on: "" },
      NOW
    );
    expect(rows.map((r) => [r.name, r.when, r.next])).toEqual([
      ["Watch", "Every 30 minutes", "next today 09:30"],
      ["Held", "Every day at 09:00", "paused"],
    ]);
  });

  it("says which device fires them when it isn't this one", () => {
    const rows = scheduledRows(list, { runs: {}, runs_here: false, runs_on: "mac-mini" }, NOW);
    expect(rows[0].next).toBe("starts on mac-mini");
  });
});

describe("wiring", () => {
  it("Auto-start shows the section, with Run now", () => {
    expect(QUEUE).toContain("{!filter.active && <ScheduledTemplates />}");
    expect(SCHEDULED).toContain('<span className="ik-queue-kind">Scheduled templates</span>');
    expect(SCHEDULED).toContain("await runTemplateNow(r.name);");
    expect(SCHEDULED).toContain('"Run now"');
    // Its link goes to Schedule, the surface's name on the top bar.
    expect(SCHEDULED).toContain('openDialogFor("schedule")');
    expect(SCHEDULED).toContain("Open Schedule ›");
  });

  it("the palette's New scheduled template… opens a new template with its schedule on", () => {
    expect(PALETTE).toContain('label: "New scheduled template…"');
    expect(PALETTE).toContain('hint: "daily, weekdays or cron"');
    expect(PALETTE).toContain('ui.openDialogFor("schedule", "schedule")');
    // "schedule" = a new template; New's ⏰ on a chip names one (lib/templates'
    // readTemplatesTarget, pinned in newSessionDraft.test.ts).
    expect(TEMPLATES_DIALOG).toContain("const want = readTemplatesTarget(useUi.getState().dialogTarget).schedule;");
    expect(TEMPLATES_DIALOG).toMatch(/setScheduleFocus\(true\);\s*dispatch\(want \? \{ t: "schedule", name: want \} : \{ t: "new" \}\);/);
  });
});
