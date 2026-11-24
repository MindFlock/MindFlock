import { describe, expect, it } from "vitest";
import { destinationLine, pickDraft, sentLine, type FeedbackDraft } from "../lib/feedback";
import TOPBAR from "../components/TopBar.tsx?raw";
import HELP from "../components/sidebar/HelpMenu.tsx?raw";
import SIDEBAR from "../components/sidebar/Sidebar.tsx?raw";
import SIDEBAR_CSS from "../components/sidebar/Sidebar.css?raw";
import PALETTE from "../components/palette/CommandPalette.tsx?raw";
import LIBFEEDBACK from "../lib/feedback.ts?raw";
import KEYMAP from "../lib/keymap.ts?raw";

const draft = (id: string, created: number): FeedbackDraft => ({
  id,
  title: "t-" + id,
  body: "b",
  kind: "bug",
  session: "",
  created,
  removed: 0,
});

describe("feedback", () => {
  it("says where a report goes and as whom before Send", () => {
    expect(destinationLine({ repo: "MindFlock/MindFlock", signed_in: true, login: "octo" })).toBe(
      "Posted publicly on GitHub (MindFlock/MindFlock) as octo."
    );
    expect(destinationLine({ repo: "MindFlock/MindFlock", signed_in: true, login: "" })).toBe(
      "Posted publicly on GitHub (MindFlock/MindFlock)."
    );
    expect(destinationLine({ repo: "MindFlock/MindFlock", signed_in: false, login: "" })).toContain("pre-filled GitHub page");
    expect(destinationLine(null)).toBe("");
  });

  it("every destination line says the report is public", () => {
    for (const signed_in of [true, false]) {
      expect(destinationLine({ repo: "r", signed_in, login: "" }).toLowerCase()).toContain("public");
    }
  });

  it("says what happened after Send", () => {
    expect(sentLine({ ok: true, via: "github", url: "u", number: 12 })).toBe("Thanks — filed as issue #12.");
    expect(sentLine({ ok: true, via: "browser", url: "u" })).toContain("press Submit there");
  });

  it("opens on the draft asked for, else the newest, else blank", () => {
    const drafts = [draft("new", 2), draft("old", 1)];
    expect(pickDraft(drafts, "old")?.id).toBe("old");
    expect(pickDraft(drafts, "gone")?.id).toBe("new");
    expect(pickDraft(drafts, null)?.id).toBe("new");
    expect(pickDraft([], "x")).toBeNull();
  });
});

/** Where the box is opened from: the sidebar foot's "? Help" menu (Linear,
 * Notion and Figma keep Feedback in a Help menu in a bottom corner), never a
 * button beside the version on the top bar. */
describe("? Help", () => {
  it("the top bar has no Feedback button any more", () => {
    expect(TOPBAR).not.toContain("tb-feedback");
    expect(TOPBAR).not.toContain("openFeedback");
    // The version still opens What's new.
    expect(TOPBAR).toContain('title="What\'s new" onClick={openWhatsNew}');
  });
  it("sits in the foot's tools row, in place of the Shortcuts link", () => {
    expect(SIDEBAR).toContain("<HelpMenu />");
    expect(SIDEBAR).not.toContain('id="shortcuts-btn"');
    expect(SIDEBAR.indexOf("<FooterCustomize />")).toBeLessThan(SIDEBAR.indexOf("<HelpMenu />"));
  });
  it("holds Send feedback…, Keyboard shortcuts and What's new, in that order — and no made-up Docs link", () => {
    const at = (label: string) => HELP.indexOf('label: "' + label + '"');
    expect(at("Send feedback…")).toBeGreaterThan(0);
    expect(at("Send feedback…")).toBeLessThan(at("Keyboard shortcuts"));
    expect(at("Keyboard shortcuts")).toBeLessThan(at("What's new"));
    expect(HELP).not.toMatch(/label: "Docs"/);
    expect(HELP).not.toMatch(/https?:\/\//);
    expect(HELP).toContain("run: () => openFeedback(),");
    expect(HELP).toContain('run: () => useUi.getState().openDialogFor("shortcuts"),');
    expect(HELP).toContain("run: () => openWhatsNew(),");
    // A guest has no feedback row (the server refuses a guest).
    expect(HELP).toContain("!guest && {");
  });
  it("a drafted report waiting is a calm mark on ? Help and on its row", () => {
    expect(HELP).toContain("const drafts = useFeedbackDrafts(!guest);");
    expect(HELP).toContain('className="help-draft-dot"');
    expect(HELP).toContain('text: drafts === 1 ? "1 draft" : drafts + " drafts", draft: true');
    expect(SIDEBAR_CSS).toMatch(/#help-btn \.help-draft-dot \{[^}]*background: var\(--accent\)/);
    expect(SIDEBAR_CSS).not.toMatch(/#help-(btn|menu)[^{]*\{[^}]*var\(--red/);
    // Re-read when an agent drafts one and when the box closes; never polled.
    expect(LIBFEEDBACK).toContain('ev.subscribe("feedback.draft", () => void queryClient.invalidateQueries({ queryKey: FEEDBACK_KEY }))');
    expect(LIBFEEDBACK).toContain("staleTime: Infinity,");
    expect(LIBFEEDBACK).not.toContain("refetchInterval");
  });
  it("behaves like the app's menus: keyboard, Esc, upward and unclipped, no window.prompt", () => {
    expect(HELP).toContain('aria-haspopup="menu"');
    expect(HELP).toContain('role="menuitem"');
    expect(HELP).toContain('if (e.key === "Escape") {');
    expect(HELP).toContain('e.key === "ArrowDown"');
    expect(HELP).toContain("createPortal(");
    expect(HELP).toContain("r.top - 6 - h");
    expect(HELP).not.toMatch(/window\.(prompt|confirm|alert)\(/);
    expect(SIDEBAR_CSS).toMatch(/#help-menu \{[^}]*position: fixed;/);
    // A Delete or Ctrl+W pressed in it is about the menu, never the session.
    expect(KEYMAP).toMatch(/POPOVER_IDS = \[[^\]]*"help-menu"/);
  });
  it("the palette entry stays", () => {
    expect(PALETTE).toContain('label: "Send feedback or report a problem…"');
    expect(PALETTE).toContain("run: () => openFeedback(),");
  });
});
