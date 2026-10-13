/** Run now + schedules on templates: one click starts a FRESH session from a
 * template with its prompt already sent (POST /api/templates/{name}/run, then
 * focus it), from the command palette and from each Templates card; a
 * template may carry a schedule (every day / weekdays at a local time) whose
 * next and last run show on the card. */

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { beforeEach, describe, expect, it, vi } from "vitest";
import PALETTE from "../components/palette/CommandPalette.tsx?raw";

const api = vi.fn();
const selectSession = vi.fn();
const refreshInstances = vi.fn(async () => {});
const toast = vi.fn();
vi.mock("../api/client", () => ({ api: (...a: unknown[]) => api(...a) }));
vi.mock("../lib/toast", () => ({ toast: (...a: unknown[]) => toast(...a) }));
vi.mock("../lib/sessionActions", () => ({ selectSession: (...a: unknown[]) => selectSession(...a) }));
vi.mock("../state/queries", () => ({ refreshInstances: () => refreshInstances() }));

const { runTemplateNow } = await import("../lib/templateRun");
const { runProblem, scheduleLabel, whenLabel } = await import("../lib/templates");
const { TEMPLATES_UI_INIT, TemplatesDialogView, formFromTemplate, runLine, templateBody } = await import(
  "../components/dialogs/TemplatesDialog"
);

beforeEach(() => {
  api.mockReset();
  selectSession.mockReset();
  toast.mockReset();
});

describe("runTemplateNow", () => {
  it("POSTs the template's run route and focuses the new session", async () => {
    api.mockImplementation(async (path: string) =>
      path.endsWith("/run") ? { title: "rivian-update-oct13" } : { templates: [] }
    );
    const title = await runTemplateNow("Rivian update");
    expect(api.mock.calls[0][0]).toBe("/api/templates/Rivian%20update/run");
    expect(api.mock.calls[0][1]).toMatchObject({ method: "POST" });
    expect(title).toBe("rivian-update-oct13");
    expect(selectSession).toHaveBeenCalledWith("rivian-update-oct13");
  });

  it("a refusal is said, and nothing is focused", async () => {
    api.mockRejectedValue(new Error("this template has no folder"));
    expect(await runTemplateNow("loose")).toBe("");
    expect(selectSession).not.toHaveBeenCalled();
    expect(String(toast.mock.calls[0][0])).toContain("no folder");
  });
});

describe("the palette offers Run template rows", () => {
  it("one per runnable template, running runTemplateNow", () => {
    expect(PALETTE).toContain('"Run template: " + tpl.name');
    expect(PALETTE).toContain("runTemplateNow(tpl.name)");
    expect(PALETTE).toContain("if (runProblem(tpl)) continue;");
  });
  it("a template without a folder can't run on its own", () => {
    expect(runProblem({ name: "p", prompt: "x" })).not.toBe("");
    expect(runProblem({ name: "p", prompt: "x", repo_path: "/r" })).toBe("");
    expect(runProblem({ name: "p", provisioned: true })).toBe("");
  });
});

const T = [
  { name: "nightly", repo_path: "/r", prompt: "pull upstream", schedule: { at: "21:00", days: "weekdays" as const } },
  { name: "loose", prompt: "just a prompt" },
];

function view(over: Partial<Parameters<typeof TemplatesDialogView>[0]> = {}) {
  return renderToStaticMarkup(
    createElement(TemplatesDialogView, {
      templates: T,
      ui: TEMPLATES_UI_INIT,
      form: formFromTemplate(null),
      busy: false,
      loaded: true,
      onForm: () => {},
      dispatch: () => {},
      onSave: () => {},
      onDelete: () => {},
      onClose: () => {},
      ...over,
    })
  );
}

describe("Templates cards", () => {
  it("every card has Run now; a folder-less one is disabled with the reason", () => {
    const html = view();
    expect(html.match(/tpl-btn tpl-run"/g)?.length).toBe(2);
    expect(html).toMatch(/disabled="" title="Pick a folder[^"]*"[^>]*>Run now/);
  });
  it("shows the schedule, next and last run", () => {
    const now = new Date(2026, 9, 13, 12, 0);
    const next = new Date(2026, 9, 13, 21, 0).getTime() / 1000;
    const last = new Date(2026, 9, 12, 21, 0).getTime() / 1000;
    expect(
      runLine(T[0], { next_at: next, last_at: last, last_session: "nightly-oct12" }, { runs_here: true, runs_on: "" }, now)
    ).toBe("Weekdays at 21:00 · next today 21:00 · last yesterday 21:00 → nightly-oct12");
    // fired by another device in the group: say which, not a next time
    expect(runLine(T[0], { next_at: next }, { runs_here: false, runs_on: "mac-mini" }, now)).toBe(
      "Weekdays at 21:00 · starts on mac-mini"
    );
    expect(runLine(T[1], {}, { runs_here: true, runs_on: "" }, now)).toBe("");
  });
  it("Starting… while a run is in flight; every Run now waits", () => {
    const html = view({ running: "nightly" });
    expect(html).toContain("Starting…");
  });
  it("a last-run failure is on the card", () => {
    const html = view({ runs: { runs: { nightly: { last_error: "repo moved" } }, runs_here: true, runs_on: "" } });
    expect(html).toContain("Last run failed: repo moved");
  });
});

describe("schedule form", () => {
  const base = { ...formFromTemplate(null), name: "n", repo_path: "/r", prompt: "p" };
  it("round-trips a template's schedule", () => {
    const f = formFromTemplate(T[0]);
    expect(f).toMatchObject({ scheduled: true, at: "21:00", days: "weekdays" });
    const r = templateBody(f);
    expect("body" in r && r.body.schedule).toEqual({ at: "21:00", days: "weekdays" });
  });
  it("no schedule sends no schedule key", () => {
    const r = templateBody(base);
    expect("body" in r && "schedule" in r.body).toBe(false);
  });
  it("a scheduled template needs a time, a folder and a prompt", () => {
    expect(templateBody({ ...base, scheduled: true, at: "" })).toHaveProperty("error");
    expect(templateBody({ ...base, scheduled: true, repo_path: "" })).toHaveProperty("error");
    expect(templateBody({ ...base, scheduled: true, prompt: " " })).toHaveProperty("error");
    expect(templateBody({ ...base, scheduled: true, at: "09:30" })).toHaveProperty("body");
  });
  it("the editor has the schedule controls once ticked", () => {
    const html = view({ ui: { ...TEMPLATES_UI_INIT, editing: "" }, form: { ...base, scheduled: true } });
    expect(html).toContain('id="tpl-scheduled"');
    expect(html).toContain('id="tpl-at"');
    expect(html).toContain("Weekdays</option>");
  });
});

describe("labels", () => {
  it("scheduleLabel / whenLabel", () => {
    expect(scheduleLabel({ at: "21:00", days: "daily" })).toBe("Every day at 21:00");
    expect(scheduleLabel({ at: "07:15", days: "weekdays" })).toBe("Weekdays at 07:15");
    const now = new Date(2026, 9, 13, 12, 0);
    expect(whenLabel(new Date(2026, 9, 14, 21, 0).getTime() / 1000, now)).toBe("tomorrow 21:00");
    expect(whenLabel(null, now)).toBe("");
  });
});
