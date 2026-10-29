/** Sky round 8 — flatten: the owner didn't love "sources (intake) schedule and
 * verify tabs within the flight thing" ("menus within menus within menus"),
 * so the top bar is flat again — New · Grid / Sky · Intake · Verify ·
 * Schedule · Settings — the Flights dialog is retired, its old addresses land
 * where they belong now, flyways live on the Sky's shelf, and the scheduled
 * ones also show in Schedule. */

/// <reference types="vite/client" />
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, describe, expect, it, vi } from "vitest";
import { code } from "./sourceText";
import type { FlightPlan } from "../lib/flights";

/** A Map-backed localStorage, installed BEFORE the store module loads. */
const storage = new Map<string, string>();
vi.stubGlobal("localStorage", {
  getItem: (k: string) => (storage.has(k) ? storage.get(k)! : null),
  setItem: (k: string, v: string) => void storage.set(k, String(v)),
  removeItem: (k: string) => void storage.delete(k),
  clear: () => storage.clear(),
});
vi.mock("../api/client", () => ({ api: vi.fn(), ApiError: class extends Error {} }));
vi.mock("../lib/toast", () => ({ toast: vi.fn() }));

const { useUi } = await import("../state/store");
const { KEYMAP } = await import("../lib/keymap");
const { installSurfaceHash, hashFor, parseHash } = await import("../lib/surfaceHash");
const { TEMPLATES_UI_INIT, TemplatesDialogView, formFromTemplate, scheduledFlyways, flywayNextLine } = await import(
  "../components/dialogs/TemplatesDialog"
);

const TOPBAR = code("components/TopBar.tsx");

afterEach(() => {
  useUi.getState().closeDialog();
  useUi.getState().takeSkyShelf();
  useUi.getState().setView("grid");
});

const open = () => [useUi.getState().openDialog, useUi.getState().dialogTarget];

describe("the top bar is flat again", () => {
  it("New · Grid / Sky · Intake · Verify · Schedule · Settings — and no Flights button", () => {
    const at = ['id="new-btn"', 'data-fold="view"', 'id="intake-btn"', 'id="verify-btn"', 'id="schedule-btn"', 'id="settings-btn"', 'id="palette-btn"'].map(
      (s) => TOPBAR.indexOf(s)
    );
    expect(at.every((x, i) => x > 0 && (i === 0 || x > at[i - 1])), at.join(",")).toBe(true);
    expect(TOPBAR).not.toContain('id="flights-btn"');
    expect(TOPBAR).not.toContain('openDialogFor("flights"');
    for (const name of ["intake", "verify", "schedule"]) expect(TOPBAR).toContain(`ui.openDialogFor("${name}")`);
  });

  it("each button wears its own badge and tooltip again", () => {
    expect(TOPBAR).toContain('{due > 0 && <span className={"tb-count" + (failing > 0 ? " is-failing" : "")}>{due}</span>}');
    expect(TOPBAR).toContain('{schedFailing > 0 && <span className="tb-count is-failing">{schedFailing}</span>}');
    expect(TOPBAR).toContain('"Intake — tickets, pull requests and issues; starts sessions when work arrives" + keys("intake")');
    expect(TOPBAR).toContain('"Verify — " + VERIFY_BLURB + keys("verify")');
  });

  it("the Sky half of the toggle counts the flights perched on you", () => {
    expect(TOPBAR).toContain("const perch = perchBadge(useFlights().data || []);");
    expect(TOPBAR).toMatch(/v === "sky" && perch\.count > 0 && \(\s*<span className=\{"tb-count" \+ \(perch\.failing \? " is-failing" : ""\)\}>\{perch\.count\}<\/span>/);
  });
});

describe("the keys", () => {
  const run = (id: string) => KEYMAP.find((k) => k.id === id)!.run({} as KeyboardEvent);

  it("Alt+I → Intake, Alt+V → Verify, Alt+S → Schedule, under their old ids (a rebind survives)", () => {
    for (const [id, key, help] of [
      ["intake", "i", "Intake — tickets, PRs and issues"],
      ["verify", "v", /^Verify — /],
      ["schedule", "s", "Schedule — templates that start sessions on their own"],
    ] as const) {
      const k = KEYMAP.find((e) => e.id === id)!;
      expect(k, id).toMatchObject({ key, alt: true });
      if (typeof help === "string") expect(k.help?.[2]).toBe(help);
      else expect(k.help?.[2]).toMatch(help);
      run(id);
      expect(open(), id).toEqual([id, null]);
    }
  });
});

describe("the palette", () => {
  const p = code("components/palette/CommandPalette.tsx");
  it("Intake… / Verify… / Schedule… open the dialogs; Switch to the Sky / grid; Flyways… is the shelf", () => {
    expect(p).toContain('run: () => ui.openDialogFor("intake"),');
    expect(p).toContain('label: "Verify…"');
    expect(p).toContain('label: "Schedule…"');
    expect(p).toContain('label: "Switch to the Sky", hint: "Alt+Y"');
    expect(p).toContain('label: "Switch to the grid", hint: "Alt+Y"');
    expect(p).toMatch(/label: "Flyways…",[\s\S]{0,200}run: \(\) => ui\.showFlyways\(\),/);
    expect(p).not.toContain('label: "Flights…"');
    expect(p).not.toContain('"flights"');
  });
});

describe("the Flights dialog is retired", () => {
  it("nothing mounts it, nothing opens it, and the store never holds it", () => {
    expect(code("App.tsx")).not.toContain("FlightsDialog");
    for (const gone of ["components/flights/FlightsDialog.tsx", "components/flights/PlanEditor.tsx", "components/flights/FlightsDialog.css"])
      expect(() => code(gone), gone).toThrow();
    useUi.getState().openDialogFor("flights", "sources/prs");
    expect(open()).toEqual(["intake", "prs"]);
    expect(hashFor("flights", null)).toBe("");
    expect(parseHash("#flights")).toBeNull();
  });

  it("the three dialogs open standalone, on their own deep links", () => {
    useUi.getState().openDialogFor("intake", "tickets:jira");
    expect(open()).toEqual(["intake", "tickets:jira"]);
    expect(hashFor("intake", "tickets:jira")).toBe("#intake/tickets%3Ajira");
    useUi.getState().openDialogFor("verify", "feature/x");
    expect(open()).toEqual(["verify", "feature/x"]);
    expect(hashFor("verify", "feature/x")).toBe("#verify/feature%2Fx");
    useUi.getState().openDialogFor("templates", "new");
    expect(open()).toEqual(["schedule", "new"]);
    expect(hashFor("schedule", "new")).toBe("#schedule");
    // Nothing embeds them any more.
    for (const f of ["App.tsx", "components/flights/SkyView.tsx", "components/flights/SkyDrawer.tsx"]) expect(code(f), f).not.toContain("embedded");
  });
});

describe("the old #flights links land where they belong now", () => {
  const fakeWin = (hash: string) => {
    const loc = { pathname: "/", search: "", hash };
    const calls: string[] = [];
    const win = {
      location: loc,
      history: {
        pushState: (_d: unknown, _u: string, url: string) => {
          calls.push("push " + url);
          loc.hash = url.includes("#") ? url.slice(url.indexOf("#")) : "";
        },
        replaceState: (_d: unknown, _u: string, url: string) => {
          calls.push("replace " + url);
          loc.hash = url.includes("#") ? url.slice(url.indexOf("#")) : "";
        },
        back: () => {},
      },
      addEventListener: () => {},
      removeEventListener: () => {},
    } as unknown as Window;
    return { win, loc, calls };
  };

  /** [old link, what is open, the view, the shelf request, the URL after] */
  const TABLE: Array<[string, [string | null, string | null], "grid" | "sky", string | null, string]> = [
    ["#flights", [null, null], "sky", null, "#sky"],
    ["#flights/sky", [null, null], "sky", null, "#sky"],
    ["#flights/sky/fix-login", [null, null], "sky", null, "#sky/fix-login"],
    ["#flights/board", [null, null], "sky", null, "#sky"],
    ["#flights/flyways", [null, null], "sky", "", "#sky"],
    ["#flights/flyways/p_1", [null, null], "sky", "p_1", "#sky"],
    ["#flights/plans/builtin%3Aticket", [null, null], "sky", "builtin:ticket", "#sky"],
    ["#flights/sources", ["intake", null], "grid", null, "#intake"],
    ["#flights/sources/tickets%3Ajira", ["intake", "tickets:jira"], "grid", null, "#intake/tickets%3Ajira"],
    ["#flights/checklists", ["verify", null], "grid", null, "#verify"],
    ["#flights/checklists/feature%2Fx", ["verify", "feature/x"], "grid", null, "#verify/feature%2Fx"],
    ["#flights/schedule", ["schedule", null], "grid", null, "#schedule"],
  ];

  it.each(TABLE)("%s", (hash, dialog, view, shelf, after) => {
    const w = fakeWin(hash);
    const off = installSurfaceHash(w.win);
    const s = useUi.getState();
    expect([s.openDialog, s.dialogTarget]).toEqual(dialog);
    expect(s.view).toBe(view);
    expect(s.skyShelf?.plan ?? null).toBe(shelf);
    // Rewritten in place, so Back from it closes rather than re-opens.
    expect(w.loc.hash).toBe(after);
    expect(w.calls.every((c) => c.startsWith("replace "))).toBe(true);
    off();
  });

  it("an old caller's openDialogFor('flights', …) is routed the same way", () => {
    useUi.getState().openDialogFor("flights", "sky/second");
    expect([useUi.getState().view, useUi.getState().skyMode, useUi.getState().focused, useUi.getState().openDialog]).toEqual(["sky", "one", "second", null]);
    useUi.getState().setView("grid");
    useUi.getState().openDialogFor("flights", "checklists/feature/x");
    expect(open()).toEqual(["verify", "feature/x"]);
  });
});

describe("flyways live in the Sky", () => {
  it("showFlyways opens the sky with a shelf request; the sky takes it once", () => {
    useUi.getState().openDialogFor("schedule");
    useUi.getState().showFlyways("p_1");
    expect(useUi.getState().openDialog).toBeNull();
    expect(useUi.getState().view).toBe("sky");
    expect(useUi.getState().skyShelf?.plan).toBe("p_1");
    useUi.getState().takeSkyShelf();
    expect(useUi.getState().skyShelf).toBeNull();
  });

  it("the sky opens its shelf and the flyway's card on the request; the drawer opens Verify / Intake directly", () => {
    const sky = code("components/flights/SkyView.tsx");
    expect(sky).toContain("const shelfAsk = useUi((s) => s.skyShelf);");
    expect(sky).toContain("useUi.getState().takeSkyShelf();");
    expect(sky).toContain('if (shelfAsk.plan === "new") setCard({ at: middle() });');
    expect(sky).not.toContain("onTab=");
    const drawer = code("components/flights/SkyDrawer.tsx");
    expect(drawer).toContain('onOpen={(b) => openDialogFor("verify", b)}');
    expect(drawer).toContain('onClick={() => openDialogFor("intake")}');
  });

  it("the card's ⋯ resets a built-in or deletes yours (asked inline), and carries the trigger", () => {
    const card = code("components/flights/FlywayCard.tsx");
    expect(card).toContain('label: "Reset to default"');
    expect(card).toContain('label: "Delete flyway…"');
    expect(card).toContain("<InlineConfirm");
    expect(card).toContain("<FlywayTrigger plan={draft}");
    expect(card).not.toMatch(/\bconfirm\(/);
  });
});

describe("Schedule lists the scheduled flyways", () => {
  const NOW = new Date(2026, 9, 14, 9, 0);
  const at = (h: number) => new Date(2026, 9, 14, h, 0).getTime() / 1000;
  const plan = (over: Partial<FlightPlan> & { runs?: Record<string, unknown> }): FlightPlan =>
    ({
      id: "p",
      name: "P",
      description: "",
      builtin: false,
      trigger: { kind: "schedule", schedule: { at: "21:00", days: "daily" }, paused: false },
      session: { repo_path: "/home/me/app", program: "claude", model: "", in_place: false },
      steps: [
        { id: "s1", kind: "agent", title: "Work", config: {}, on_fail: "halt" },
        { id: "s2", kind: "ship", title: "Push", config: {}, on_fail: "halt" },
      ],
      updated: 0,
      ...over,
    }) as FlightPlan;
  const FLYWAYS = [
    plan({ id: "p_manual", name: "By hand", trigger: { kind: "manual", schedule: null, paused: false } }),
    plan({ id: "p_late", name: "Nightly deps", runs: { next_at: at(21), schedule_label: "Every day at 21:00", last_at: at(8), last_session: "nightly-oct14" } }),
    plan({ id: "p_soon", name: "Standup notes", runs: { next_at: at(11), schedule_label: "Weekdays at 11:00", last_error: "repo moved", last_status: "failed" } }),
    plan({ id: "p_held", name: "Held", trigger: { kind: "schedule", schedule: { at: "08:00", days: "daily" }, paused: true }, runs: { paused: true, schedule_label: "Every day at 08:00" } }),
  ];
  const view = (flyways: FlightPlan[] | null) =>
    renderToStaticMarkup(
      createElement(TemplatesDialogView, {
        templates: [],
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
        flyways,
      })
    );

  it("only the flyways with a schedule trigger, soonest first, paused last", () => {
    expect(scheduledFlyways(FLYWAYS).map((p) => p.id)).toEqual(["p_soon", "p_late", "p_held"]);
    expect(flywayNextLine(FLYWAYS[3], NOW)).toBe("Paused");
    expect(flywayNextLine(FLYWAYS[1], NOW)).toBe("in 12h");
    expect(flywayNextLine(plan({ runs: { runs_here: false, runs_on: "mac-mini" } }), NOW)).toBe("Starts on mac-mini");
  });

  it("a section of rows with a template's anatomy: name, schedule line, next run, last result — Fly now, Edit in the Sky", () => {
    const html = view(FLYWAYS);
    const sec = html.slice(html.indexOf('id="schedule-flyways"'));
    expect(sec).toContain(">Scheduled flyways<");
    expect([...sec.matchAll(/data-plan="([^"]+)"/g)].map((m) => m[1])).toEqual(["p_soon", "p_late", "p_held"]);
    const late = sec.slice(sec.indexOf('data-plan="p_late"'), sec.indexOf('data-plan="p_held"'));
    expect(late).toContain("Nightly deps");
    expect(late).toContain("2 stops · app · claude");
    expect(late).toContain('<span class="sch-cadence">Every day at 21:00</span>');
    expect(late).toContain('<span class="sch-next">in 12h</span>');
    expect(late).toContain("Last run");
    expect(late).toContain("nightly-oct14");
    expect(late).toContain(">Fly now</button>");
    expect(late).toContain(">Edit in the Sky</button>");
    const soon = sec.slice(sec.indexOf('data-plan="p_soon"'), sec.indexOf('data-plan="p_late"'));
    expect(sec).toContain('class="tpl-card is-scheduled sch-flyway is-failing" data-plan="p_soon"');
    expect(soon).toContain("Last run failed: repo moved");
    const held = sec.slice(sec.indexOf('data-plan="p_held"'));
    expect(sec).toContain('class="tpl-card is-scheduled sch-flyway is-paused" data-plan="p_held"');
    expect(held).toContain("Paused");
  });

  it("nothing scheduled hides the section — no heading, no empty state", () => {
    expect(view([FLYWAYS[0]])).not.toContain("schedule-flyways");
    expect(view(null)).not.toContain("Scheduled flyways");
  });

  it("Fly now takes off on the flyway; Edit in the Sky closes Schedule and opens its card on the shelf", () => {
    const src = code("components/dialogs/TemplatesDialog.tsx");
    expect(src).toContain("void takeOff({ plan_id: f.id })");
    expect(src).toContain("useUi.getState().showFlyways(f.id);");
  });
});
