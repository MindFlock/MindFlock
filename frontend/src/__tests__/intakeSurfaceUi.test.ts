/** The Intake dialog's UI (components/intake/*): the top-bar surface, its four
 * tabs and their badges, the source → workflow → state grouping, the
 * per-row overrides, the failure cards and the work-list filters.
 *
 * Moved from tests/unit/test_intake_surface.py (class TestIntakeDialogShell,
 * test_every_intake_work_list_ships_its_filter, test_the_bundle_ships_the_fourth_tab)
 * and tests/unit/test_intake_reopen.py (the bundle/stylesheet halves), which
 * grepped the built app.js / style.css. These pin the OWNING source module
 * (comments stripped, whitespace normalised) — or the behaviour, where the
 * thing is an exported function or table. The `/api/intake/reopen` call stays
 * a Python contract test next to the route. */

/// <reference types="vite/client" />
import { describe, expect, it } from "vitest";
import { ALL_CODE, SRC, dropComments, norm } from "./sourceText";
import TOPBAR_RAW from "../components/TopBar.tsx?raw";
import DIALOG_RAW from "../components/intake/IntakeDialog.tsx?raw";
import KIT_RAW from "../components/intake/kit.tsx?raw";
import TICKETS_RAW from "../components/intake/TicketsTab.tsx?raw";
import PRS_RAW from "../components/intake/PullRequestsTab.tsx?raw";
import ISSUES_RAW from "../components/intake/IssuesTab.tsx?raw";
import QUEUE_RAW from "../components/intake/QueueTab.tsx?raw";
import BUCKETS_RAW from "../components/intake/buckets.ts?raw";
import ERROR_POP_RAW from "../lib/errorPop.ts?raw";
import DIALOG_FILTER_RAW from "../components/dialogs/DialogFilter.tsx?raw";
import INTAKE_CSS_RAW from "../components/intake/IntakeDialog.css?raw";
import CAPS_CSS_RAW from "../components/capsGate.css?raw";
import CONN_CSS_RAW from "../components/ConnBanner.css?raw";
import SCREENS_CSS_RAW from "../components/settings/screens/screens.css?raw";

const { LEGACY_SCREEN_TABS, parseIntakeTarget } = await import("../components/intake/IntakeDialog");
const { visibleBuckets, countInBuckets } = await import("../components/intake/buckets");

/** Source minus comments, layout-blind (`norm`), and a method chain's line
 * break forgotten (`x\n  .y` reads `x.y`). Token order, quoting and spelling
 * still have to match. */
const code = (raw: string) => norm(dropComments(raw)).replace(/ \./g, ".");

/** Every app source module (tests excluded), comments stripped — for the
 * "nowhere in the app" pins the bundle used to answer. */
const ALL = Object.values(SRC).map(code).join("\n");

const TOPBAR = code(TOPBAR_RAW);
const DIALOG = code(DIALOG_RAW);
const KIT = code(KIT_RAW);
const TICKETS = code(TICKETS_RAW);

/** One top-level function's text, `function name(` to the brace at column 0. */
function fn(raw: string, name: string): string {
  const m = new RegExp("^(?:export )?(?:async )?function " + name + "\\(", "m").exec(raw);
  expect(m, `function ${name}`).toBeTruthy();
  return code(raw.slice(m!.index, raw.indexOf("\n}", m!.index) + 2));
}

/** CSS minus comments, whitespace collapsed. */
const css = (raw: string) => raw.replace(/\/\*[\s\S]*?\*\//g, "").replace(/\s+/g, " ");
const INTAKE_CSS = css(INTAKE_CSS_RAW);

/** The declarations of the rule that starts a line with exactly `selector {`
 * in `raw` — and there must be exactly one such rule. */
function rule(raw: string, selector: string): string {
  const flat = raw.replace(/\/\*[\s\S]*?\*\//g, "");
  const esc = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const hits = [...flat.matchAll(new RegExp("^" + esc + " \\{([^}]*)\\}", "gm"))];
  expect(hits.length, selector).toBe(1);
  return hits[0][1].replace(/\s+/g, " ");
}

describe("the Intake dialog shell", () => {
  it("is a top-bar surface of its own", () => {
    expect(TOPBAR).toContain('id="intake-btn"'); // the top-bar entry
    expect(TOPBAR).toMatch(/id="intake-btn"[^>]*onClick=\{\(\) => ui\.openDialogFor\("intake"\)\}/);
    expect(DIALOG).toContain('<div id="intake-panel">');
    // A blocking modal: its cards carry Remove buttons, so a stray Ctrl+W /
    // Delete must not kill the session behind it instead.
    expect(DIALOG).toContain('<Modal id="intake-dialog"');
  });

  it("the tab strip names the three queues (and the fourth) and counts them", () => {
    // The GitHub tabs say "GitHub" (round 3: an "Issues" tab beside a GitHub
    // Issues ticket provider read as two homes for one thing). Keys unchanged.
    for (const label of ['label: "Tickets"', 'label: "GitHub PRs"', 'label: "GitHub issues"', 'label: "Auto-start"'])
      expect(DIALOG, label).toContain(label);
    expect(DIALOG).toContain('{ key: "autostart", label: "Auto-start"');
    expect(DIALOG).toContain('className="ik-tab-count"');
    // A real tablist: tabs say which is selected and which panel they drive,
    // and arrow keys move along the strip.
    expect(DIALOG).toContain('role="tablist"');
    expect(DIALOG).toContain('role="tab"');
    expect(DIALOG).toContain("aria-selected={tab === t.key}");
    expect(DIALOG).toContain('role="tabpanel"');
    expect(DIALOG).toContain('e.key === "ArrowRight"');
  });

  it("the legacy Settings screen keys still route", () => {
    // Deep links recorded elsewhere — the sidebar bars, the welcome tour, and
    // the server's own `settings_screen` on a Connections card — must keep
    // working, so the retired keys map to the tab that replaced them.
    expect(LEGACY_SCREEN_TABS).toEqual({ ticketing: "tickets", repo: "prs", issues: "issues" });
    expect(DIALOG).toContain("LEGACY_SCREEN_TABS[head] || head");
    expect(parseIntakeTarget("ticketing")).toEqual({ tab: "tickets", focus: "" });
    expect(parseIntakeTarget("repo")).toEqual({ tab: "prs", focus: "" });
    expect(parseIntakeTarget("issues")).toEqual({ tab: "issues", focus: "" });
    // …and with a source after the colon (Connections' "Open in Intake").
    expect(parseIntakeTarget("ticketing:jira")).toEqual({ tab: "tickets", focus: "jira" });
    expect(parseIntakeTarget("prs:acme/api")).toEqual({ tab: "prs", focus: "acme/api" });
    expect(parseIntakeTarget("nonsense")).toEqual({ tab: "tickets", focus: "" });
    expect(parseIntakeTarget(null)).toEqual({ tab: "tickets", focus: "" });
  });

  it("the three screens left the Settings nav", () => {
    for (const gone of [
      '{ key: "ticketing", label: "Ticketing"',
      '{ key: "repo", label: "PR review"',
      '{ key: "issues", label: "Git issues"',
    ])
      expect(ALL, gone).not.toContain(gone);
  });

  it("the GitHub tabs keep their git gate — and no longer need a tracker", () => {
    // PR comments and issues run on their own switches and GitHub sign-in; the
    // pipeline boots for them with no ticket source, so they are not locked
    // behind "connect a ticketing tool" (round-2 judge: "why would PR handling
    // need Shortcut?").
    expect(DIALOG.split('caps: "git",').length - 1).toBe(2);
    expect(DIALOG).not.toContain('caps: "git ticketing"');
    expect(DIALOG).toContain("data-caps-need={t.caps}");
    expect(css(CAPS_CSS_RAW)).toContain('body.no-ticketing .ik-panel[data-caps-need~="ticketing"]');
  });

  it("the fourth tab (Auto-start) ships", () => {
    expect(DIALOG).toContain('"autostart"');
    expect(DIALOG).toContain("<QueueTab {...p} />");
  });
});

describe("the work lists group by source", () => {
  it("inbox lists group by source", () => {
    expect(KIT).toContain('" ik-source-group"');
    expect(KIT).toContain('className="ik-groups"');
    // Tickets nest workflow-state buckets INSIDE their source, keyed by the
    // pair — so opening one source's "In Progress" doesn't open another's.
    expect(TICKETS).toContain('src + "::" + b');
  });

  it("states nest under their workflow instead of repeating it", () => {
    // A Shortcut source with several workflows qualifies every state name, so
    // a flat list wrote the workflow onto all seven of its headings.
    expect(KIT).toContain('" ik-workflow-group"');
    expect(TICKETS).toContain("bucket_meta");
    // The bucket KEY stays qualified; only the heading inside a workflow group
    // drops the qualifier…
    expect(TICKETS).toContain("wf === null ? b : labelOf(b)");
    // …and the level only appears when there is more than one workflow.
    expect(TICKETS).toContain("workflows.filter(Boolean).length > 1");
  });

  it("the tab badge counts what the tab will show", () => {
    // It counted every ticket the provider ever assigned you — Completed and
    // Won't-do included — so it read `1221` over a list of 52.
    expect(BUCKETS_RAW).toMatch(/^export function countInBuckets\(/m);
    expect(BUCKETS_RAW).toMatch(/^export function visibleBuckets\(/m);
    const badge = fn(DIALOG_RAW, "TabCount");
    expect(badge).toContain("countInBuckets(");
    expect(badge).toContain("visibleBuckets(");
    expect(badge).toContain("loadShownBuckets()");
    expect(badge).toContain('className="ik-tab-count"');
    // …and counts what needs you: rows ready to start, not every row listed.
    expect(badge).toContain(".filter(isQueued)");
  });

  it("done buckets are what that filter excludes", () => {
    // The server flags done-type states; the client parks them behind the Add
    // menu by default, which is why the two counts diverged at all.
    const vis = fn(BUCKETS_RAW, "visibleBuckets");
    expect(vis).toContain("doneBuckets.includes(b)");
    expect(vis).toContain("shown === null");
    expect(visibleBuckets(["A", "Done", "B"], ["Done"], null)).toEqual(["A", "B"]);
    expect(visibleBuckets(["A", "Done", "B"], ["Done"], ["Done"])).toEqual(["Done"]);
    expect(countInBuckets([{ bucket: "A" }, { bucket: "Done" }], ["A"], false)).toBe(1);
  });

  it("every list states its source in a heading", () => {
    expect(KIT).toContain('" ik-source-group"');
    // Neither leftover from the intermediate designs: the per-row source
    // label, and the PR/issue tabs' conditional grouping.
    expect(ALL).not.toContain("ik-item-source");
    expect(ALL).not.toContain("groupOrder.length > 1");
    // `sourceOrder.length === 1` survives only to pick the hint's wording.
    expect(TICKETS).toContain("filterOf(sourceOrder[0])");
  });

  it("the hint names this flock's own states", () => {
    // "done states like Completed" was a guess about someone else's workflow.
    expect(TICKETS).toContain("doneBuckets.filter((b) => !visible.includes(b))");
    expect(TICKETS).toContain("parkedDone");
    expect(ALL).not.toContain("like Completed");
    // …and a plural/singular that matches however many there are.
    expect(TICKETS).toContain('parkedDone.length === 1 ? "is" : "are"');
  });

  it("ingest states are never merged across sources", () => {
    expect(ALL).not.toContain("Object.values(ingestStates).flat()");
    expect(TICKETS).toContain("Each source's heading says which tickets it auto-starts");
    // The single-source case still names that source's own states — read from
    // the SAVED filter, so an unreachable tracker can't make it "not set".
    expect(TICKETS).toContain("filterOf(sourceOrder[0])");
    expect(TICKETS).toContain("savedFilter(src)");
    expect(ALL).not.toContain("No ingest-state filter is set");
  });

  it("group headings look like something you can press", () => {
    // As plain text beside a small caret the top-level heading read as a stray
    // line of copy. Every level now has a hit area, a hover fill and a caret
    // that turns; the source additionally gets a filled band.
    expect(INTAKE_CSS).toContain(".ik-groups .tk-bucket-head:hover");
    expect(INTAKE_CSS).toContain(".ik-source-group > .tk-bucket-head {");
    // One glyph rotated by state, not two glyphs swapped.
    expect(INTAKE_CSS).toContain('.ik-groups [aria-expanded="true"] .tk-caret');
    expect(INTAKE_CSS).toContain("rotate(90deg)");
    expect(KIT).toContain('<span className="tk-caret">▸</span>');
    // Keyboard users get the same target.
    expect(INTAKE_CSS).toContain(".ik-groups .tk-bucket-toggle:focus-visible");
  });

  it("the tabs carry no redundant heading", () => {
    // The tab strip names the surface; an in-panel "TICKETING" title on top of
    // a tab called Tickets was pure repetition.
    const raw = ALL_CODE;
    expect(raw).not.toMatch(
      /className="set-section-title"[^>]*>\s*(Ticketing|Automated PR review|Automated issue handling)\s*</
    );
    expect(raw).not.toMatch(
      /"set-section-title",\s*children:\s*"(Ticketing|Automated PR review|Automated issue handling)"/
    );
  });
});

describe("each row's per-launch overrides", () => {
  const TABS = { TicketsTab: TICKETS_RAW, PullRequestsTab: PRS_RAW, IssuesTab: ISSUES_RAW, QueueTab: QUEUE_RAW };

  it("each row can be started on a different CLI", () => {
    expect(KIT).toContain('className="ik-item-agent"');
    // Sent only when picked, so an unpicked row keeps the old payload shape.
    for (const [name, src] of Object.entries(TABS)) expect(code(src), name).toContain("agent ? { agent } : {}");
  });

  it("each row can choose how far it goes", () => {
    expect(KIT).toContain('className="ik-item-depth"');
    for (const [name, src] of Object.entries(TABS)) expect(code(src), name).toContain("depth ? { depth } : {}");
    // The pickers share one line (see IntakeDialog.css).
    expect(KIT).toContain('<div className="ik-item-picks">');
    expect(INTAKE_CSS).toContain(".ik-item-depth");
    expect(INTAKE_CSS).toContain(".ik-item-picks");
  });

  it("each row can choose how hard the agent thinks", () => {
    expect(KIT).toContain('className="ik-item-effort"');
    for (const [name, src] of Object.entries(TABS)) expect(code(src), name).toContain("effort ? { effort } : {}");
    // A CLI with no effort setting gets a disabled control that says so.
    expect(KIT).toContain('"No effort (" + (effortProvider || "this CLI") + ")"');
    // Each picker carries its own small label.
    for (const label of ["Agent", "Fast-track", "Effort"])
      expect(KIT).toContain('<span className="ik-item-pick-label">' + label + "</span>");
    // Three pickers share the line, so it has to be allowed to fold…
    expect(rule(INTAKE_CSS_RAW, ".ik-item-start .ik-item-picks")).toContain("flex-wrap: wrap");
    expect(INTAKE_CSS).toContain(".ik-item-effort:disabled");
    // …and the control column is CAPPED, because its children are stretched
    // to the widest of them: without this the third picker widened `Begin
    // work` into a full-width banner.
    expect(rule(INTAKE_CSS_RAW, ".ik-item-start")).toMatch(/max-width: \d+px/);
  });

  it("the start button stays a button", () => {
    const btn = rule(INTAKE_CSS_RAW, ".ik-item-start .pr-review-btn");
    expect(btn).toContain("align-self: flex-end");
    expect(btn).toContain("min-width");
    expect(rule(INTAKE_CSS_RAW, ".ik-item-start .ik-start-again")).toContain("align-self: flex-end");
  });
});

describe("failures", () => {
  it("pop up where failures live", () => {
    // Bottom-right cards, the corner the connection-lost card already owns —
    // not the 1.4s bottom-centre confirmation strip.
    const stack = rule(CONN_CSS_RAW, "#cs-errors");
    expect(stack).toContain("position: fixed");
    expect(stack).toContain("right: 16px");
    expect(stack).toContain("bottom: 16px");
    // Never stacked ON the connection card.
    const conn = css(CONN_CSS_RAW);
    expect(conn).toContain("body.conn-lost #cs-errors");
    expect(conn).toContain("#cs-errors .cs-error-body");
    expect(ERROR_POP_RAW).toContain('document.getElementById("cs-errors")');
    expect(ERROR_POP_RAW).toContain('el.id = "cs-errors"');
    // Both intake failure paths use it, and the success confirmation does not
    // (a start that worked is still a toast).
    expect(KIT).toContain("errorPop(");
    expect(TICKETS).toContain("errorPop(");
    expect(KIT).toContain('toast(msg + " — starting, see the sidebar")');
  });

  it("a long failure reason cannot widen the row", () => {
    // The track may be narrower than its content, the meta line may be too,
    // and the chip breaks rather than setting a floor.
    expect(rule(SCREENS_CSS_RAW, ".pr-open-item")).toContain("minmax(0, 1fr) auto");
    expect(rule(SCREENS_CSS_RAW, ".pr-open-meta")).toContain("min-width: 0");
    const chip = rule(SCREENS_CSS_RAW, ".pr-open-chip");
    expect(chip).toContain("overflow-wrap: anywhere");
    expect(chip).toContain("white-space: normal");
    expect(chip).not.toContain("text-overflow");
  });

  it("a reason too long for a row opens instead of wrapping", () => {
    expect(KIT).toContain("shortReason(reason)");
    expect(KIT).toContain('"\\n\\nClick for the full message."');
    expect(KIT).toContain('errorPop(reference + " — why it was skipped", reason)');
    expect(css(SCREENS_CSS_RAW)).toContain(".pr-open-chip-more");
  });
});

describe("every Intake work list ships its filter", () => {
  it("one filter, four panels — Recently closed's box, not a second one", () => {
    // The four lists grow without bound; scrolling was the only way to a row.
    for (const [id, src] of [
      ["tk-tickets-filter", TICKETS_RAW],
      ["gh-prs-filter", PRS_RAW],
      ["gh-issues-filter", ISSUES_RAW],
      ["ik-queue-filter", QUEUE_RAW],
    ])
      expect(code(src), id).toContain('"' + id + '"');
    // The shared component is what makes Ctrl+F and "Escape clears, then
    // closes" the same everywhere.
    expect(DIALOG_FILTER_RAW).toContain("dlg-filter");
    expect(INTAKE_CSS.split(".pr-open-toolbar .dlg-filter").length - 1).toBeGreaterThanOrEqual(1);
  });
});

describe("Reopen window (intake reopen)", () => {
  it("the row ships the button, and the start demotes when a workspace exists", () => {
    expect(KIT).toContain('"Reopen session"');
    // The start button demotes when a workspace exists, so the row has one
    // primary action rather than two competing ones.
    expect(KIT).toMatch(/canReopen\s*\?\s*"test-btn ik-start-again"/);
    // ...and a row that will auto-start anyway gets the quiet Start too
    // (round 3: seven identical purple Starts; the chip carries the row).
    expect(KIT).toMatch(/willAutoStart\s*\?\s*"test-btn pr-review-btn ik-start-quiet"\s*:\s*"btn-primary pr-review-btn"/);
    expect(INTAKE_CSS).toContain(".ik-start-again");
  });
});
