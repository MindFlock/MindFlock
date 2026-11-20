/** UI sweep round 2, package Q5 (the web UI half).
 *
 * - New → Ticket: Retry after a failed filing sends the shown draft back, so
 *   the server files it without another model turn; a failed source list has
 *   a Retry beside its message.
 * - Settings → Advanced: a failed update check has a "Check now" button that
 *   says "Checking…" while it runs (the Updates block replaced the shell's
 *   own "Check again", 2026-11 updates overhaul). */

import { describe, expect, it } from "vitest";
import { composeBody } from "../components/dialogs/NewTicketPane";
import { code, squash } from "./sourceText";

const PANE = squash(code("components/dialogs/NewTicketPane.tsx"));
const ADVANCED = squash(code("components/settings/screens/Advanced.tsx"));

describe("New → Ticket's Retry", () => {
  const draft = { name: "T", description: "It hangs.", criteria: ["c"] };

  it("sends the shown draft back with the sentence", () => {
    expect(composeBody("rec", "  fix it  ", draft)).toEqual({
      source: "rec",
      text: "fix it",
      draft,
    });
  });

  it("asks for a draft when there is none on screen", () => {
    expect(composeBody("rec", "fix it", null)).toEqual({ source: "rec", text: "fix it" });
  });

  it("is what the button files with, and says Retry while a draft is shown", () => {
    expect(PANE).toContain("json: composeBody(source, brief, draft)");
    expect(PANE).toContain(': draft ? "Retry" : "Create ticket"');
  });

  it("drops the draft once the sentence changes, so an edit drafts anew", () => {
    expect(PANE).toContain("if (draft) setDraft(null);");
  });
});

describe("New → Ticket's source list", () => {
  it("has a Retry beside a load failure that fetches the list again", () => {
    expect(PANE).toMatch(/\{sourcesError\}\{" "\} <button [^>]*onClick=\{\(\) => setSourcesTry\(\(n\) => n \+ 1\)\} > Retry <\/button>/);
    expect(PANE).toContain("}, [sourcesTry]);");
  });
});

describe("Settings → Advanced, a failed update check", () => {
  it("says so, and Check now says Checking… while it runs", () => {
    expect(ADVANCED).toContain("Couldn’t check for a newer version just now.");
    expect(ADVANCED).toContain('{checking ? "Checking…" : "Check now"}');
    expect(ADVANCED).toContain("onClick={checkNow}");
  });

  it("re-asks the shell too, which re-runs a failed check before answering", () => {
    expect(ADVANCED).toMatch(/const check = useCallback\( async \(refresh: boolean\) => \{ const shellAsk = mfengine\?\.updateInfo \? mfengine \.updateInfo\(\) \.then\(setShellInfo\)/);
    expect(ADVANCED).toMatch(/const checkNow = \(\) => \{[^}]*void check\(true\)/);
    // No dead-end "try again in a minute" left behind the button.
    expect(ADVANCED).not.toContain("Try again in a minute");
  });
});
