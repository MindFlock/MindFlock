/** New → Ticket, the centred one-height card, and the folder row that replaced
 * a dead window.prompt.
 *
 * Moved from tests/unit/test_frontend_new_ticket.py, which grepped the built
 * bundle. The behaviour of filing lives in tests/unit/test_ticket_compose.py;
 * these pin the dialog's tabs, the Ticket pane's controls and copy, and the
 * stylesheet rules the card's geometry depends on. */

import { describe, expect, it } from "vitest";
import { NEW_TABS } from "../components/dialogs/newSession";
import { ALL_CODE, ALL_CSS, attr, cssRule, src, squash, tagsWith, tagsWithId } from "./sourceText";

const CSS = src("components/dialogs/NewSessionDialog.css");
const PANE = src("components/dialogs/NewTicketPane.tsx");
const BROWSER = src("components/dialogs/newSession/FolderBrowser.tsx");

describe("the New dialog's two tabs", () => {
  it("are Session and Ticket", () => {
    expect(NEW_TABS).toEqual([
      { key: "session", label: "Session" },
      { key: "ticket", label: "Ticket" },
    ]);
  });

  it("are styled by the SPECIFIC selector, with an active underline", () => {
    // Not just ".nf-tab": `.ws-head button` paints every button in a dialog
    // header as a bordered pill and outranks a bare class, so the tabs once
    // shipped as two more buttons in a row of buttons with nothing marking
    // which one you were on.
    expect(cssRule(CSS, ".nf-head .nf-tabs .nf-tab")).not.toBe("");
    // Underline tabs, the same idiom Intake uses — the app has one tab style.
    expect(cssRule(CSS, ".nf-head .nf-tabs .nf-tab.active")).not.toBe("");
  });
});

describe("the card: landing panes share a top edge, the form is centred", () => {
  // v0.7's geometry for the two landing panes (Session page 1 and the Ticket
  // tab): each its own natural height, all starting at the same y, so a tab
  // click never moves the card. The top is centred on a LANDING pane's height,
  // so both read as centred. The full form (page 2) is centred at one fixed
  // height, so a fold never moves it.
  it("anchors the landing panes' top edge", () => {
    expect(squash(ALL_CODE)).toContain('className={"modal" + (shownTab === "ticket" || page === 1 ? " nf-landing" : "")}');
    const block = cssRule(CSS, "#new-dialog.nf-landing");
    expect(block).toContain("align-items: flex-start;");
    expect(block).toContain("padding-top: var(--nf-top);");
    expect(block).toContain("--nf-top: max(16px, calc((100vh - var(--nf-anchor-h)) / 2));");
    expect(block).toContain("--nf-anchor-h: min(86vh, 400px);");
    expect(block).toContain("--nf-max-h: calc(100vh - var(--nf-top) - 16px);");
    expect(squash(CSS)).toContain(
      "#new-dialog.nf-landing #new-form, #new-dialog.nf-landing #new-ticket-form { max-height: var(--nf-max-h); }"
    );
  });

  it("gives the landing panes their natural height, and only the form the fixed one", () => {
    expect(cssRule(CSS, "#new-form.nf-ask")).toBe("height: auto;");
    expect(squash(ALL_CODE)).toContain('className={"ta-drop" + (page === 1 ? " nf-ask" : "")}');
    expect(squash(CSS)).toContain(
      "#new-ticket-form { background: var(--panel); border: 1px solid var(--border); border-radius: 12px; " +
        "padding: 20px 22px; width: min(660px, 94vw); height: auto;"
    );
    // The form is centred: the base rule overrides nothing of .modal's.
    const base = cssRule(CSS, "#new-dialog");
    expect(base).not.toContain("align-items");
    expect(base).not.toContain("padding-top");
    expect(base).toContain("--nf-card-h: min(86vh, 620px);");
    // Each literal is written once, so the anchor and the cap cannot drift.
    for (const lit of ["min(86vh, 620px)", "min(86vh, 400px)"])
      expect(ALL_CSS.split(lit).length - 1).toBe(1);
  });
});

describe("the Ticket pane", () => {
  it("ships its box, its picker and its button", () => {
    expect(tagsWithId(PANE, "nt-brief")).toHaveLength(1);
    expect(tagsWithId(PANE, "nt-source")).toHaveLength(1);
    expect(PANE).toContain('"Create ticket"');
    // The placeholder is the whole tutorial: the only thing that shows the
    // shape of a brief worth filing.
    expect(attr(tagsWithId(PANE, "nt-brief")[0], "placeholder")).toContain("the login page hangs for SSO users");
  });

  it("says there is no ticket form on purpose", () => {
    // Without it the pane looks like a form somebody forgot to finish.
    expect(squash(PANE)).toContain("There is no form: the tracker already has one");
  });

  it("links the filed ticket, opened safely in a new tab", () => {
    // The link IS the deliverable — the whole reason the pane has a third state.
    const links = tagsWith(PANE, "className", "nt-filed-link");
    expect(links).toHaveLength(1);
    expect(links[0].name).toBe("a");
    expect(attr(links[0], "rel")).toBe("noopener noreferrer");
    expect(cssRule(CSS, ".nt-filed-link")).not.toBe("");
  });
});

describe("the folder browser creates folders without window.prompt", () => {
  // Electron does not implement prompt(): it returned null and + Folder did
  // nothing, silently, in the desktop app. The old call site is pinned ABSENT,
  // because a re-added prompt beside the inline row would pass a presence-only
  // check and still be dead in Electron.
  it("has no prompt call site, and the inline row is there and styled", () => {
    expect(ALL_CODE).not.toContain("New folder name (created in ");
    expect(tagsWith(BROWSER, "className", "rb-new")).toHaveLength(1);
    expect(tagsWith(BROWSER, "placeholder", "New folder name")).toHaveLength(1);
    expect(cssRule(CSS, ".rb-new")).not.toBe("");
  });
});
