/** UI sweep round 3, the New dialog and browser notifications: a refused
 * create keeps how far it goes, page 1 says when the agent isn't installed,
 * the folder picker hands focus back, Escape in page 1's Browse puts the old
 * folder back, the GitHub hint under "Goes as far as" is a link, and a
 * browser notification's click opens its session. */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { code } from "./sourceText";
import { closeFolderPicker, openFolderPicker, pickFolder } from "../lib/folderPicker";

vi.mock("../lib/sessionActions", () => ({ selectSession: vi.fn() }));
import { selectSession } from "../lib/sessionActions";
import { openSession, publishWindowName } from "../lib/windowName";

const DIALOG = code("components/dialogs/NewSessionDialog.tsx");
const LIST = code("components/dialogs/NewList.tsx");
const DESCRIBE = code("components/dialogs/newSession/DescribeSection.tsx");

/** The body of `name`'s arrow/function, from its declaration to the first
 * line that closes it at the declaration's indent. */
function body(src: string, decl: string): string {
  const at = src.indexOf(decl);
  expect(at, decl).toBeGreaterThanOrEqual(0);
  const lineStart = src.lastIndexOf("\n", at) + 1;
  const indent = src.slice(lineStart, at).match(/^\s*/)?.[0] ?? "";
  const end = src.indexOf("\n" + indent + "}", at);
  return src.slice(at, end);
}

describe("a refused create keeps how far it goes (#4)", () => {
  it("useRunDraft no longer resets on every open", () => {
    const hook = LIST.slice(LIST.indexOf("export function useRunDraft("), LIST.indexOf("export function RunItems("));
    expect(hook).not.toMatch(/useEffect\(\(\) => \{\s*if \(!open\) return;\s*setPreview\(null\)/);
    expect(hook).toMatch(/const reset = useCallback\(\(\) => \{[^}]*setLaneChoice\(null\);[^}]*setAskFirst\(false\);/);
    expect(hook).toMatch(/\breset,\s*\};/);
  });

  it("only New's resetForm puts them back, never a kept opening", () => {
    const reset = body(DIALOG, "const resetForm = (prefill: string) =>");
    expect(reset).toContain("draft.reset();");
    expect(DIALOG.split("draft.reset()").length - 1).toBe(1);
  });
});

describe("page 1 says when the agent isn't installed (#10)", () => {
  it("page 1's note is the not-installed note first", () => {
    expect(DIALOG).toContain("agentNote: agentMissing ? missingNote : agentSignedOut ? signedOutNote : null");
  });

  it("Start is off on both pages while the agent is missing", () => {
    const blocked = DIALOG.slice(DIALOG.indexOf("const startBlocked = runMode"), DIALOG.indexOf("const createFolder"));
    expect(blocked).toMatch(/: agentMissing\s*\?\s*"Can't start yet — pick an agent that's installed\."/);
  });
});

describe("the folder picker hands focus back (#11)", () => {
  type El = { isConnected: boolean; focus: ReturnType<typeof vi.fn> };
  const el = (connected = true): El => ({ isConnected: connected, focus: vi.fn() });
  let doc: { body: object; activeElement: unknown };

  beforeEach(() => {
    vi.useFakeTimers();
    doc = { body: {}, activeElement: null };
    vi.stubGlobal("document", doc);
  });
  afterEach(() => {
    closeFolderPicker(undefined, false);
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  const open = (anchor?: El) =>
    openFolderPicker({ start: "/a", anchor: anchor as unknown as HTMLElement, onPick: () => {} });

  it("Use this folder refocuses what had focus at open", () => {
    const field = el();
    doc.activeElement = field;
    open();
    doc.activeElement = doc.body; // the window took focus, then went away
    pickFolder("/a/b");
    expect(field.focus).not.toHaveBeenCalled();
    vi.runAllTimers();
    expect(field.focus).toHaveBeenCalledTimes(1);
  });

  it("✕ / Escape do too; the anchor stands in for a field that is gone", () => {
    const gone = el(false);
    const anchor = el();
    doc.activeElement = gone;
    open(anchor);
    doc.activeElement = doc.body;
    closeFolderPicker();
    vi.runAllTimers();
    expect(gone.focus).not.toHaveBeenCalled();
    expect(anchor.focus).toHaveBeenCalledTimes(1);
  });

  it("an owner closing its own browse, or one that focused elsewhere, keeps focus where it is", () => {
    const field = el();
    doc.activeElement = field;
    const id = open();
    doc.activeElement = doc.body;
    closeFolderPicker(id, false);
    vi.runAllTimers();
    expect(field.focus).not.toHaveBeenCalled();

    open();
    const other = el();
    doc.activeElement = other;
    pickFolder("/x");
    vi.runAllTimers();
    expect(field.focus).not.toHaveBeenCalled();
  });
});

describe("Escape in Runs in → Browse… puts the old folder back (#29)", () => {
  it("remembers the folder at open and restores it on cancel", () => {
    expect(DESCRIBE).toMatch(/browseFrom\.current = runBrowse \? repoPath : null;/);
    const cancel = DESCRIBE.slice(DESCRIBE.indexOf("onCancel: () => {", DESCRIBE.indexOf("useFolderPickerWhile(runBrowse")));
    expect(cancel).toMatch(
      /const from = browseFrom\.current;[\s\S]*if \(from !== null && from !== repoPath\) folderDo\(\{ t: "user-set", path: from \}\);[\s\S]*setRunBrowse\(false\);/
    );
  });
});

describe("the GitHub hint under Goes as far as is a link (#30)", () => {
  it("opens Settings → Connections", () => {
    const hint = DIALOG.slice(DIALOG.indexOf('id="new-ft-github"'), DIALOG.indexOf('id="new-ft-github"') + 900);
    expect(hint).toContain('id="new-ft-github-connect"');
    expect(hint).toMatch(/closeDialog\(\);\s*openSettings\("connections"\);/);
  });
});

describe("a browser notification's click opens its session (#28)", () => {
  afterEach(() => {
    delete (globalThis as unknown as { mindflock?: unknown }).mindflock;
  });

  it("is published next to displayName and selects through the app", () => {
    publishWindowName();
    const mf = (globalThis as unknown as { mindflock: Record<string, unknown> }).mindflock;
    expect(mf.openSession).toBe(openSession);
    openSession("alpha");
    expect(selectSession).toHaveBeenCalledWith("alpha", { noKeyboard: true });
    openSession("");
    expect(selectSession).toHaveBeenCalledTimes(1);
  });
});
