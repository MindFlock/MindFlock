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
const {
  TEMPLATES_UI_INIT,
  TemplatesDialogView,
  cronPreviewLine,
  formFromTemplate,
  cadenceLine,
  historyLine,
  lastLine,
  nextLine,
  templateBody,
  templateMeta,
  templatesReducer,
} = await import(
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

  it("a refusal is the server's own sentence, never \"Started…\", and nothing is focused", async () => {
    // r5-schedule finding 1: the 409's detail is the toast
    api.mockRejectedValueOnce(
      Object.assign(new Error("codex isn't installed"), {
        body: { error: "codex isn't installed", detail: "Couldn't start pr-bot: codex isn't installed", reason: "agent_missing" },
      })
    );
    api.mockResolvedValue({ templates: [] });
    expect(await runTemplateNow("pr-bot")).toBe("");
    expect(selectSession).not.toHaveBeenCalled();
    expect(toast.mock.calls.map((c) => String(c[0]))).toEqual(["Couldn't start pr-bot: codex isn't installed"]);
    // the card re-reads its runs: the refusal is a recorded failed run
    expect(api.mock.calls.some((c) => c[0] === "/api/templates")).toBe(true);
    toast.mockReset();
    api.mockReset();
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
  it("every card has Run now; a folder-less one is disabled with the reason ON the card", () => {
    const html = view();
    expect(html.match(/tpl-btn tpl-run"/g)?.length).toBe(2);
    expect(html).toMatch(/disabled="" title="Run now needs a folder"[^>]*>▶ Run now/);
    // Not only a hover title: a line on the card, with the way to fix it.
    expect(html.match(/class="tpl-needs"/g)?.length).toBe(1);
    expect(html).toContain("Run now and schedules need a folder.");
    expect(html).toContain(">Add one<");
  });
  it("every card reaches its schedule — Schedule… or, on a scheduled one's strip, Change schedule", () => {
    const html = view();
    expect(html.match(/tpl-sched"/g)?.length).toBe(2);
    const sched = html.slice(html.indexOf('data-name="nightly"'), html.indexOf('data-name="loose"'));
    expect(sched).toContain('class="sch-strip"');
    expect(sched).toContain(">Change schedule<");
    expect(sched).not.toContain(">Schedule…<");
    const loose = html.slice(html.indexOf('data-name="loose"'));
    expect(loose).toContain('class="tpl-btn tpl-sched"');
    expect(loose).toContain(">Schedule…<");
    expect(loose).not.toContain("sch-strip");
  });
  it("Schedule… opens the editor with the schedule on", () => {
    // a card's Schedule… is the compact schedule-only row, not the whole form
    expect(templatesReducer(TEMPLATES_UI_INIT, { t: "schedule", name: "x" })).toEqual({
      editing: "x",
      confirming: null,
      error: "",
      scheduleOnly: true,
    });
    // a NEW template's (the palette's) is the whole form: it has no name yet
    expect(templatesReducer(TEMPLATES_UI_INIT, { t: "schedule", name: "" }).scheduleOnly).toBeUndefined();
  });
  it("names the time zone, and whether the last run was by hand or scheduled", () => {
    const now = new Date(2026, 9, 13, 12, 0);
    const next = new Date(2026, 9, 13, 21, 0).getTime() / 1000;
    const last = new Date(2026, 9, 12, 21, 0).getTime() / 1000;
    const st = { runs_here: true, runs_on: "" };
    const info = { next_at: next, last_at: last, last_trigger: "schedule", tz: "EDT" } as never;
    expect(cadenceLine(T[0], info)).toBe("Weekdays at 21:00 EDT");
    // the schedule line says when; the chip beside it says how far
    expect(nextLine(T[0], info, st, now)).toBe("in 9h");
    expect(lastLine(info, now)).toBe("Last run yesterday 21:00 · scheduled");
    expect(lastLine({ last_at: last, last_trigger: "manual" }, now)).toBe("Last run yesterday 21:00 · by hand");
  });
  it("a template can carry an autopilot depth, stored only when on", () => {
    const base = { ...formFromTemplate(null), name: "n", repo_path: "/r", prompt: "p" };
    const on = templateBody({ ...base, autopilot: "pr" });
    expect("body" in on && on.body).toMatchObject({ autopilot: "pr" });
    const off = templateBody(base);
    expect("body" in off && "autopilot" in off.body).toBe(false);
    expect(formFromTemplate({ name: "x", autopilot: "push" } as never).autopilot).toBe("push");
  });
  it("shows the schedule, next and last run", () => {
    const now = new Date(2026, 9, 13, 12, 0);
    const next = new Date(2026, 9, 13, 21, 0).getTime() / 1000;
    const last = new Date(2026, 9, 12, 21, 0).getTime() / 1000;
    const st = { runs_here: true, runs_on: "" };
    expect(cadenceLine(T[0], {})).toBe("Weekdays at 21:00");
    expect(nextLine(T[0], { next_at: next }, st, now)).toBe("in 9h");
    expect(lastLine({ last_at: last, last_session: "nightly-oct12" }, now)).toBe(
      "Last run yesterday 21:00 → nightly-oct12"
    );
    // fired by another device in the group: say which, not a next time
    expect(nextLine(T[0], { next_at: next }, { runs_here: false, runs_on: "mac-mini" }, now)).toBe(
      "Starts on mac-mini"
    );
    // paused says so, whatever the server's next time
    expect(nextLine({ ...T[0], schedule: { ...T[0].schedule, paused: true } }, { next_at: next }, st, now)).toBe(
      "Paused"
    );
    expect(lastLine({}, now)).toBe("");
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

describe("cron schedules, autopilot and history", () => {
  const base = { ...formFromTemplate(null), name: "n", repo_path: "/r", prompt: "p" };
  const CRON = { name: "watch", repo_path: "/r", prompt: "check the deploy", schedule: { cron: "*/30 * * * *" }, autopilot: "pr" as const };

  it("round-trips a cron schedule and the autopilot depth", () => {
    const f = formFromTemplate(CRON);
    expect(f).toMatchObject({ scheduled: true, days: "cron", cron: "*/30 * * * *", autopilot: "pr" });
    const r = templateBody(f);
    expect("body" in r && r.body.schedule).toEqual({ cron: "*/30 * * * *" });
    expect("body" in r && r.body.autopilot).toBe("pr");
  });
  it("squeezes the expression's spaces, needs one, and sends no autopilot key when off", () => {
    const r = templateBody({ ...base, scheduled: true, days: "cron", cron: "  0  21 * * 1-5 " });
    expect("body" in r && r.body.schedule).toEqual({ cron: "0 21 * * 1-5" });
    expect("body" in r && "autopilot" in r.body).toBe(false);
    expect(templateBody({ ...base, scheduled: true, days: "cron", cron: " " })).toHaveProperty("error");
  });
  it("the editor swaps the time for a cron field and shows the server's reading", () => {
    const editing = { ui: { ...TEMPLATES_UI_INIT, editing: "" } };
    const cron = view({ ...editing, form: { ...base, scheduled: true, days: "cron", cron: "*/30 * * * *" } });
    expect(cron).toContain('id="tpl-cron"');
    expect(cron).not.toContain('id="tpl-at"');
    expect(cron).toContain("minute hour day-of-month month weekday");
    const read = view({
      ...editing,
      form: { ...base, scheduled: true, days: "cron", cron: "*/30 * * * *" },
      cronPreview: { ok: true, label: "Every 30 minutes", next: [] },
    });
    expect(read).toContain("Every 30 minutes");
    const bad = view({
      ...editing,
      form: { ...base, scheduled: true, days: "cron", cron: "61 * * * *" },
      cronPreview: { ok: false, error: "minute: 61 is outside 0-59" },
    });
    expect(bad).toMatch(/tpl-cron-preview error[^>]*>minute: 61 is outside 0-59/);
    expect(view({ ...editing, form: base })).toContain('id="tpl-autopilot"');
  });
  it("cronPreviewLine lists the next runs", () => {
    const now = new Date(2026, 9, 14, 10, 0);
    const a = new Date(2026, 9, 14, 10, 30).getTime() / 1000;
    const b = new Date(2026, 9, 14, 11, 0).getTime() / 1000;
    expect(cronPreviewLine({ ok: true, label: "Every 30 minutes", next: [a, b] }, now)).toBe(
      "Every 30 minutes — next today 10:30, today 11:00"
    );
    expect(cronPreviewLine(null, now)).toBe("");
  });
  it("the card uses the server's words for a cron schedule and says what autopilot does", () => {
    const now = new Date(2026, 9, 14, 10, 0);
    expect(cadenceLine(CRON, { schedule_label: "Every 30 minutes" })).toBe("Every 30 minutes");
    expect(cadenceLine(CRON, {})).toBe("Cron */30 * * * *");
    // a cron schedule has no single "at", so no zone tacked on
    expect(cadenceLine(CRON, { schedule_label: "Every 30 minutes", tz: "EDT" } as never)).toBe("Every 30 minutes");
    expect(nextLine(CRON, { next_at: now.getTime() / 1000 + 25 * 60 }, { runs_here: true, runs_on: "" }, now)).toBe(
      "in 25 min"
    );
    expect(scheduleLabel({ cron: "0 9 * * 1" })).toBe("Cron 0 9 * * 1");
    expect(templateMeta(CRON)).toContain("Fast-track: Open PR");
  });
  it("a skip and the recent runs are on the card", () => {
    const at = new Date(2026, 9, 14, 9, 30).getTime() / 1000;
    const html = view({
      templates: [CRON],
      runs: {
        runs: {
          watch: {
            last_skip: "the previous run is still working",
            last_skip_at: at,
            history: [
              { at, trigger: "schedule", skipped: "the previous run is still working", session: "watch-oct14-0900" },
              { at, trigger: "schedule", session: "watch-oct14-0900", autopilot: "pr" },
              { at, trigger: "manual", error: "repo moved" },
            ],
          },
        },
        runs_here: true,
        runs_on: "",
      },
    });
    expect(html).toContain("Skipped ");
    expect(html).toContain("Recent runs (3)");
    expect(html).toContain("→ watch-oct14-0900 (Fast-track: Open PR)");
    expect(html).toContain("by hand — failed: repo moved");
  });
  it("historyLine", () => {
    const now = new Date(2026, 9, 14, 12, 0);
    const at = new Date(2026, 9, 14, 9, 30).getTime() / 1000;
    expect(historyLine({ at, trigger: "schedule", session: "s" }, now)).toBe("today 09:30 · scheduled → s");
    expect(historyLine({ at, trigger: "schedule", skipped: "busy" }, now)).toBe("today 09:30 · scheduled — skipped: busy");
  });
});
