/** Intake → Epics: the tab (right after Tickets), its "Assigned epics" panel
 * grouped by source then by state, and its badge — run through renderToString
 * (node env, no jsdom) with the epics query seeded in a QueryClient. */

import { createElement } from "react";
import { renderToString } from "react-dom/server";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, expect, it } from "vitest";
import { src } from "./sourceText";
import { epicMeta, epicSourceNote, epicsToStart, groupEpics, type EpicRow, type EpicsResponse } from "../components/intake/epics";

const { TAB_KEYS, parseIntakeTarget, EpicsCount } = await import("../components/intake/IntakeDialog");
const { EpicsTab } = await import("../components/intake/EpicsTab");

const epic = (over: Partial<EpicRow>): EpicRow => ({
  source: "sc",
  source_label: "Shortcut",
  id: "1",
  title: "epic-1",
  name: "Epic one",
  url: "https://app.shortcut.com/x/epic/1",
  state: "To Do",
  state_rank: 0,
  in_states: false,
  stories_total: 4,
  mine_open: 2,
  has_session: false,
  auto: false,
  labels: [],
  eligible: false,
  startable: true,
  reasons: [],
  ...over,
});

const DATA: EpicsResponse = {
  sources: [
    { source: "sc", source_label: "Shortcut", epic_states: ["In Progress"], auto: true },
    { source: "sc2", source_label: "Shortcut (ops)", epic_states: [], auto: false },
  ],
  epics: [
    epic({ id: "3", title: "epic-3", name: "Late one", state: "In Progress", state_rank: 2, in_states: true, eligible: true, auto: true }),
    epic({ id: "1", title: "epic-1", name: "Backlog epic", state: "To Do", state_rank: 0, reasons: ["not in an auto-start state"] }),
    epic({ id: "4", title: "epic-4", name: "Running epic", state: "In Progress", state_rank: 2, in_states: true, eligible: true, auto: true, has_session: true }),
    epic({ id: "2", title: "epic-2", name: "Labelled", state: "Ready", state_rank: 1, labels: ["mindflock"] }),
  ],
  errors: [],
};

function render(el: React.ReactElement, data: EpicsResponse | undefined) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  if (data) qc.setQueryData(["epics"], data);
  return renderToString(createElement(QueryClientProvider, { client: qc }, el));
}

const tabProps = { gotoTab: () => {}, adding: null, startAdd: () => {}, endAdd: () => {} };

describe("the Epics tab", () => {
  it("sits right after Tickets and parses as a target", () => {
    expect(TAB_KEYS.slice(0, 2)).toEqual(["tickets", "epics"]);
    expect(parseIntakeTarget("epics")).toEqual({ tab: "epics", focus: "" });
    expect(parseIntakeTarget("epics:sc")).toEqual({ tab: "epics", focus: "sc" });
    const dialog = src("components/intake/IntakeDialog.tsx");
    expect(dialog).toContain('{ key: "epics", label: "Epics", el: (p) => <EpicsTab {...p} /> }');
  });

  it("is no longer a section inside Tickets", () => {
    expect(src("components/intake/TicketsTab.tsx")).not.toContain("EpicsSection");
  });
});

describe("groupEpics", () => {
  it("groups by source in the server's order (every source, even empty), then by state_rank", () => {
    const g = groupEpics(DATA);
    expect(g.map((x) => [x.source, x.label, x.total])).toEqual([
      ["sc", "Shortcut", 4],
      ["sc2", "Shortcut (ops)", 0],
    ]);
    expect(g[0].states.map((s) => s.state)).toEqual(["To Do", "Ready", "In Progress"]);
    // Rows keep the server's order inside a state.
    expect(g[0].states[2].rows.map((e) => e.id)).toEqual(["3", "4"]);
  });

  it("adds a source only an epic or an error names, after the listed ones", () => {
    const g = groupEpics({
      sources: [{ source: "a", epic_states: [] }],
      epics: [epic({ source: "b", source_label: "B" })],
      errors: [{ source: "c", error: "401" }],
    });
    expect(g.map((x) => x.source)).toEqual(["a", "b", "c"]);
    expect(g[1].meta).toBeNull();
    expect(g[2].error).toBe("401");
  });

  it("puts unranked states last, by name (older servers send no rank)", () => {
    const g = groupEpics({
      epics: [epic({ state: "Zed", state_rank: undefined }), epic({ id: "9", state: "Alpha", state_rank: undefined }), epic({ id: "8", state: "Mid", state_rank: 5 })],
    });
    expect(g[0].states.map((s) => s.state)).toEqual(["Mid", "Alpha", "Zed"]);
  });
});

describe("source notes", () => {
  it("say what auto-starts, that it is off, or that no states are set", () => {
    expect(epicSourceNote({ source: "s", epic_states: ["A", "B"], auto: true })?.text).toBe("auto-starts epics in: A, B");
    expect(epicSourceNote({ source: "s", epic_states: ["A"], auto: false })?.text).toBe("auto-start off");
    expect(epicSourceNote({ source: "s", epic_states: [], auto: true })?.text).toBe(
      "no auto-start states — set them on the source card in Tickets"
    );
    expect(epicSourceNote(null)).toBeNull();
  });
});

describe("epicMeta", () => {
  it("says the state and your open stories of the total", () => {
    expect(epicMeta({ state: "On Deck", mine_open: 3, stories_total: 13 })).toBe("On Deck · 3 of yours to do (of 13)");
    expect(epicMeta({ state: "to do", mine_open: null, stories_total: 5 })).toBe("to do · 5 stories");
    expect(epicMeta({ state: "", mine_open: 0, stories_total: 0 })).toBe("0 of yours to do");
  });
});

describe("the badge", () => {
  it("counts eligible epics with no lead yet", () => {
    expect(epicsToStart(DATA.epics).map((e) => e.id)).toEqual(["3"]);
    expect(epicsToStart(undefined)).toEqual([]);
    const html = render(createElement(EpicsCount), DATA);
    expect(html).toContain('class="ik-tab-count"');
    expect(html).toContain(">1</span>");
    expect(html).toContain("1 ready to start — the ones auto-start would take (4 listed)");
  });

  it("shows nothing at zero or before the list loads", () => {
    expect(render(createElement(EpicsCount), undefined)).toBe("");
    expect(render(createElement(EpicsCount), { ...DATA, epics: [epic({})] })).toBe("");
  });
});

describe("the Assigned epics panel", () => {
  it("renders sources, states in rank order, markers, reasons and actions", () => {
    const html = render(createElement(EpicsTab, tabProps), DATA);
    expect(html).toContain("Assigned epics");
    const at = (s: string) => html.indexOf(s);
    expect(at("Shortcut")).toBeLessThan(at("Shortcut (ops)"));
    expect(at(">To Do<")).toBeLessThan(at(">Ready<"));
    expect(at(">Ready<")).toBeLessThan(at(">In Progress<"));
    expect(html).toContain("auto-starts epics in: In Progress");
    expect(html).toContain("no auto-start states — set them on the source card in Tickets");
    expect(html).toContain("No open epics of yours.");
    // Eligible + auto: marked; with a lead: Open lead; not eligible: its reasons.
    expect(html.match(/ik-epic-auto/g)?.length).toBe(1);
    expect(html).toContain("Open lead ›");
    expect(html).toContain("not in an auto-start state");
    expect(html).toContain("labels: mindflock");
    expect(html).toContain('href="https://app.shortcut.com/x/epic/1"');
  });

  it("points to Tickets when there is no epics-capable source", () => {
    const html = render(createElement(EpicsTab, tabProps), { epics: [], sources: [], errors: [] });
    expect(html).toContain("Epics come from a Shortcut source");
    expect(html).toContain('id="ep-goto-tickets"');
  });

  it("says a source's error in its own group", () => {
    const html = render(createElement(EpicsTab, tabProps), {
      sources: [{ source: "sc", source_label: "Shortcut", epic_states: ["X"], auto: true }],
      epics: [],
      errors: [{ source: "sc", error: "HTTP 401" }],
    });
    expect(html).toContain("can&#x27;t list epics");
    expect(html).toContain("Shortcut: HTTP 401");
    expect(html).not.toContain("No open epics of yours.");
  });
});
