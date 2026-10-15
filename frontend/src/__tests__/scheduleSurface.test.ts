/** Schedule on the top bar: the item (between Verify and Settings, Alt+S, a
 * red count only for scheduled runs that FAILED), the old "templates" name
 * still landing on it, and the surface's sections — what runs next, the
 * scheduled templates with their strip (Change schedule / Skip next / Pause),
 * then the ones not scheduled — plus the pure helpers behind them. */

/// <reference types="vite/client" />
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import TOPBAR from "../components/TopBar.tsx?raw";
import PALETTE from "../components/palette/CommandPalette.tsx?raw";
import TOUR from "../lib/tours/schedule.ts?raw";
import INDEX_CSS from "../styles/index.css?raw";

vi.mock("../api/client", () => ({ api: vi.fn() }));
vi.mock("../lib/toast", () => ({ toast: vi.fn() }));

const { useUi } = await import("../state/store");
const { KEYMAP } = await import("../lib/keymap");
const { failingSchedules, inLabel, nextUp, withScheduleFlags } = await import("../lib/templates");
const { TEMPLATES_UI_INIT, TemplatesDialogView, formFromTemplate, scheduledOrder, templateBody } = await import(
  "../components/dialogs/TemplatesDialog"
);

const NOW = new Date(2026, 9, 14, 9, 0);
const at = (h: number, m = 0, dayOffset = 0) => new Date(2026, 9, 14 + dayOffset, h, m).getTime() / 1000;

const T = [
  { name: "loose", repo_path: "/r", prompt: "just do it" },
  { name: "nightly", repo_path: "/r", prompt: "deps", schedule: { at: "21:00", days: "daily" as const } },
  { name: "standup", repo_path: "/r", prompt: "notes", schedule: { at: "11:00", days: "weekdays" as const } },
  { name: "held", repo_path: "/r", prompt: "x", schedule: { at: "08:00", days: "daily" as const, paused: true } },
];
const RUNS = {
  runs: {
    nightly: { next_at: at(21), last_error: "repo moved" },
    standup: { next_at: at(11), skipping_at: at(11) },
    held: { paused: true },
  },
  runs_here: true,
  runs_on: "",
};

function view(over: Partial<Parameters<typeof TemplatesDialogView>[0]> = {}) {
  return renderToStaticMarkup(
    createElement(TemplatesDialogView, {
      templates: T,
      runs: RUNS,
      ui: TEMPLATES_UI_INIT,
      form: formFromTemplate(null),
      busy: false,
      loaded: true,
      onForm: () => {},
      dispatch: () => {},
      onSave: () => {},
      onDelete: () => {},
      onClose: () => {},
      now: NOW,
      ...over,
    })
  );
}

describe("the top-bar item", () => {
  it("sits between Verify and Settings, like Intake and Verify", () => {
    const v = TOPBAR.indexOf('id="verify-btn"');
    const sc = TOPBAR.indexOf('id="schedule-btn"');
    const se = TOPBAR.indexOf('id="settings-btn"');
    expect(v).toBeGreaterThan(0);
    expect(v < sc && sc < se).toBe(true);
    const btn = TOPBAR.slice(sc, se);
    expect(btn).toContain('className="tb-item"');
    expect(btn).toContain('keys("schedule")');
    expect(btn).toContain('ui.openDialogFor("schedule")');
    // A count only for failures, in the failure red, nothing at zero.
    expect(btn).toContain('{schedFailing > 0 && <span className="tb-count is-failing">{schedFailing}</span>}');
    expect(TOPBAR).toContain("failingSchedules(sched.templates, sched.status).length");
  });

  it("opens on Alt+S, guarded like Alt+I / Alt+V, and nothing else takes Alt+S", () => {
    const e = KEYMAP.find((k) => k.id === "schedule");
    expect(e).toMatchObject({ key: "s", alt: true });
    expect(e?.help?.[1]).toBe("Alt+S");
    expect(typeof e?.when).toBe("function");
    expect(KEYMAP.filter((k) => k.alt === true && !k.mod && String(k.key).toLowerCase() === "s").length).toBe(1);
    useUi.getState().closeDialog();
    e?.run({} as KeyboardEvent);
    expect(useUi.getState().openDialog).toBe("schedule");
    useUi.getState().closeDialog();
  });
});

describe("the old name still lands", () => {
  it('openDialogFor("templates") opens Schedule, keeping the target', () => {
    useUi.getState().openDialogFor("templates", "new");
    expect(useUi.getState().openDialog).toBe("schedule");
    expect(useUi.getState().dialogTarget).toBe("new");
    useUi.getState().closeDialog();
  });

  it("the palette has Schedule… (on the short menu) and keeps Templates… as a way in", () => {
    expect(PALETTE).toContain('label: "Schedule…"');
    expect(PALETTE).toContain("top: TOP.schedule");
    expect(PALETTE).toMatch(/label: "Templates…", hint: "your templates — in Schedule", run: \(\) => ui\.openDialogFor\("schedule"\)/);
    expect(PALETTE).not.toContain('openDialogFor("templates"');
  });

  it("has its own tour, and its stylesheet is wired", () => {
    expect(TOUR).toContain('title: "Schedule"');
    expect(INDEX_CSS).toContain('@import "../components/dialogs/ScheduleDialog.css" layer(components);');
  });
});

describe("the surface", () => {
  it("is titled Schedule: next up, then Scheduled, then Not scheduled", () => {
    const html = view();
    expect(html).toContain('<h2 id="templates-title">Schedule</h2>');
    const next = html.indexOf('id="schedule-next"');
    const sched = html.indexOf('id="schedule-scheduled"');
    const saved = html.indexOf('id="schedule-saved"');
    expect(next > 0 && next < sched && sched < saved).toBe(true);
    // Next: the soonest run of a template that isn't paused.
    expect(html.slice(next, sched)).toContain("<strong>standup</strong>");
    expect(html.slice(next, sched)).toContain("in 2h");
    // Scheduled: soonest first, paused last; the unscheduled one is below.
    const order = ["standup", "nightly", "held", "loose"].map((n) => html.indexOf('data-name="' + n + '"'));
    expect(order.every((x, i) => x > 0 && (i === 0 || x > order[i - 1]))).toBe(true);
    expect(html.slice(saved)).toContain("Not scheduled");
    expect(html.slice(saved)).toContain("run now or add a schedule");
    expect(html).not.toMatch(/Saved setups|on a setup/);
  });

  it("a scheduled card has its strip; paused says Resume, a skip says Undo skip", () => {
    const html = view();
    const card = (n: string) => {
      const i = html.indexOf('data-name="' + n + '"');
      const j = html.indexOf('data-name="', i + 1);
      return html.slice(i, j < 0 ? undefined : j);
    };
    expect(card("nightly")).toContain(">Skip next<");
    expect(card("nightly")).toContain(">Pause<");
    expect(card("nightly")).toContain("Last run failed: repo moved");
    expect(html).toContain('class="tpl-card is-scheduled is-failing" data-name="nightly"');
    expect(html).toContain('class="tpl-card is-scheduled is-paused" data-name="held"');
    expect(card("standup")).toContain(">Undo skip<");
    // The skip rides THE schedule line: "… · next … (today skipped)"
    expect(card("standup")).toMatch(/class="sch-cadence">Weekdays at 11:00[^<]* · next [^<]*\(today skipped\)</);
    expect(card("held")).toContain(">Resume<");
    expect(card("held")).toContain(">Paused<");
    expect(card("held")).not.toContain("Skip next");
    for (const n of ["nightly", "standup", "held", "loose"]) {
      expect(card(n)).toContain("▶ Run now");
      expect(card(n)).toContain(">Edit<");
      expect(card(n)).toContain(">Delete<");
    }
  });

  it("says when another device fires the schedules", () => {
    const html = view({ runs: { ...RUNS, runs_here: false, runs_on: "mac-mini" } });
    expect(html).toContain("Schedules start on <strong>mac-mini</strong>");
  });

  it("with only unscheduled templates, Scheduled says how to get one", () => {
    const html = view({ templates: [T[0]], runs: { runs: {}, runs_here: true, runs_on: "" } });
    expect(html).toContain("Nothing runs by itself yet");
    expect(html).not.toContain('id="schedule-next"');
  });
});

describe("helpers", () => {
  it("failingSchedules counts scheduled, unpaused templates whose last run failed", () => {
    const runs = { runs: { nightly: { last_error: "x" }, loose: { last_error: "y" }, held: { last_error: "z" } } };
    expect(failingSchedules(T, runs)).toEqual(["nightly"]);
    expect(failingSchedules(T, { runs: {} })).toEqual([]);
  });

  it("nextUp skips paused ones and picks the soonest", () => {
    expect(nextUp(T, RUNS)).toEqual({ name: "standup", at: at(11) });
    expect(nextUp([T[0]], RUNS)).toBeNull();
  });

  it("inLabel says how far off, briefly", () => {
    expect(inLabel(at(9, 5), NOW)).toBe("in 5 min");
    expect(inLabel(at(11), NOW)).toBe("in 2h");
    expect(inLabel(at(11, 30), NOW)).toBe("in 2h 30m");
    expect(inLabel(at(21), NOW)).toBe("in 12h");
    expect(inLabel(at(9, 0, 3), NOW)).toBe("in 3 days");
    expect(inLabel(at(8), NOW)).toBe("now");
  });

  it("withScheduleFlags flips one switch and drops what's off or past", () => {
    const now = at(9);
    expect(withScheduleFlags(T[1], { paused: true }, now).schedule).toEqual({ at: "21:00", days: "daily", paused: true });
    expect(withScheduleFlags(T[3], { paused: false }, now).schedule).toEqual({ at: "08:00", days: "daily" });
    expect(withScheduleFlags(T[1], { skip_until: at(21) }, now).schedule?.skip_until).toBe(at(21));
    expect(withScheduleFlags(T[1], { skip_until: at(8) }, now).schedule).toEqual({ at: "21:00", days: "daily" });
  });

  it("an edit keeps Pause, and keeps Skip next only for the same cadence", () => {
    const later = Date.now() / 1000 + 3600;
    const t = { ...T[1], schedule: { at: "21:00", days: "daily" as const, paused: true, skip_until: later } };
    const f = formFromTemplate(t);
    const same = templateBody(f);
    expect("body" in same && same.body.schedule).toEqual({ at: "21:00", days: "daily", paused: true, skip_until: later });
    const moved = templateBody({ ...f, at: "22:00" });
    expect("body" in moved && moved.body.schedule).toEqual({ at: "22:00", days: "daily", paused: true });
  });

  it("scheduledOrder: soonest first, no next run after, paused last", () => {
    const list = scheduledOrder(T, { runs: { nightly: { next_at: at(21) }, standup: {} } });
    expect(list.map((t) => t.name)).toEqual(["nightly", "standup", "held"]);
  });
});
