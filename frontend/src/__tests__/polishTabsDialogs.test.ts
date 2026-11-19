/** Touch-up round on a session's tabs, a few dialogs and Settings: the edge
 * cases that dead-ended or double-fired. Pure helpers are driven directly;
 * the wiring of components that need a DOM is pinned at their source (vitest
 * runs in node, see vitest.config.ts). */

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, describe, expect, it, vi } from "vitest";
import { code, squash } from "./sourceText";

const storage = new Map<string, string>();
vi.stubGlobal("localStorage", {
  getItem: (k: string) => (storage.has(k) ? storage.get(k)! : null),
  setItem: (k: string, v: string) => void storage.set(k, String(v)),
  removeItem: (k: string) => void storage.delete(k),
  clear: () => storage.clear(),
});

const { diffFailure } = await import("../components/grid/DiffTab");
const { settingsStatus } = await import("../components/settings/useSettings");
const { SettingsDialog } = await import("../components/settings/SettingsDialog");
const { resetLayout } = await import("../components/customize/CustomizeDialog");
const { modalEscape, pushModal } = await import("../components/Modal");
const { togglePaneItem } = await import("../lib/paneHeader");
const { useUi } = await import("../state/store");
const { QueryClient, QueryClientProvider } = await import("@tanstack/react-query");

describe("Diff tab: a failed load keeps its toolbar", () => {
  it("diffFailure: git's error, or a fetch error with no diff to show", () => {
    expect(diffFailure({ error: "fatal: bad revision 'main'" }, "")).toBe("fatal: bad revision 'main'");
    expect(diffFailure(null, "HTTP 500")).toBe("HTTP 500");
    // A failed refresh keeps the last good diff on screen.
    expect(diffFailure({ content: "diff --git a b" }, "HTTP 500")).toBe("");
    expect(diffFailure(null, "")).toBe("");
  });

  it("says Couldn't load the diff with the git text as detail, under ↻ / bases / All files", () => {
    const tab = code("components/grid/DiffTab.tsx");
    expect(tab).not.toContain("Error: {");
    const branch = tab.split("if (failure || !payload) {", 2)[1].split("if (file) {", 1)[0];
    expect(branch).toContain("Couldn't load the diff");
    expect(branch).toContain("{failure}");
    expect(branch).toContain("{reloadButton}");
    expect(branch).toContain("{modeButtons}");
    expect(branch).toContain("← All files");
    // The toolbar pieces exist before the failure branch uses them.
    expect(tab.indexOf("const reloadButton =")).toBeLessThan(tab.indexOf("if (failure || !payload) {"));
    expect(tab.indexOf("const modeButtons =")).toBeLessThan(tab.indexOf("if (failure || !payload) {"));
  });
});

describe("Queue tab", () => {
  const q = code("components/grid/QueueTab.tsx");

  it("Send now / Add to queue: one in flight at a time, buttons disabled meanwhile", () => {
    expect(q).toContain("if (sendingRef.current) return;");
    expect(q).toContain("const send = () => once(sendText);");
    expect(q).toContain("const add = () => once(addText);");
    expect(q).toContain('title="Add to queue (Alt+Enter)" disabled={sending} onClick={add}');
    expect(q).toContain('title="Send now (Ctrl+Enter)" disabled={sending} onClick={send}');
    // The keys go through the same guarded send/add.
    expect(q).toContain('void (k === "send" ? send() : add());');
    // Released even when the send throws.
    expect(squash(q)).toContain("} finally { sendingRef.current = false; setSending(false); }");
  });

  it("a failed first load says so with a Retry instead of Loading… forever", () => {
    expect(q).toContain("setLoadFailed(true);");
    expect(q).toContain("Couldn't load the queue.");
    expect(q).toContain('onClick={() => void reload()}');
  });
});

describe("Commit dialog: no commit while ✨ is writing", () => {
  const c = code("components/dialogs/CommitDialog.tsx");
  it("disables submit and guards the handler (Ctrl+Enter reaches it too)", () => {
    const handler = c.split("async function submitCommit() {", 2)[1];
    expect(handler.indexOf("if (writing) return;")).toBeGreaterThanOrEqual(0);
    expect(handler.indexOf("if (writing) return;")).toBeLessThan(handler.indexOf("const m = msg.trim();"));
    expect(squash(c)).toContain('<button type="submit" disabled={writing}');
  });
});

describe("Settings", () => {
  it("settingsStatus: any document is ready; none is loading, or an error once fetching stops", () => {
    expect(settingsStatus({ data: {} })).toBe("ready");
    expect(settingsStatus({ data: { a: 1 }, isError: true })).toBe("ready");
    expect(settingsStatus({})).toBe("loading");
    expect(settingsStatus({ isError: true })).toBe("error");
    // Retry in flight: back to "Loading settings…".
    expect(settingsStatus({ isError: true, isFetching: true })).toBe("loading");
  });

  afterEach(() => {
    useUi.setState({ openDialog: null, dialogTarget: null } as never);
    Object.assign(useUi.getInitialState(), useUi.getState());
  });

  function render(seed?: object) {
    useUi.setState({ openDialog: "settings", dialogTarget: null } as never);
    Object.assign(useUi.getInitialState(), useUi.getState());
    const client = new QueryClient();
    if (seed) client.setQueryData(["settings"], seed);
    return renderToStaticMarkup(
      createElement(QueryClientProvider, { client }, createElement(SettingsDialog, {}))
    );
  }

  it("shows Loading settings… and no controls until the document has loaded", () => {
    const html = render();
    expect(html).toContain("Loading settings…");
    expect(html).not.toContain('class="set-screen');
    // The nav is still there to move around.
    expect(html).toContain('id="settings-nav"');
  });

  it("shows the screens once it has", () => {
    const html = render({});
    expect(html).not.toContain("Loading settings…");
    expect(html).toContain('class="set-screen active"');
  });

  it("a failed read says Couldn't read settings, with a Retry", () => {
    const d = code("components/settings/SettingsDialog.tsx");
    expect(d).toContain("Couldn't read settings.");
    expect(d).toContain("onClick={() => void model.reload()}");
  });

  it("a secret field clears only after its save succeeds", () => {
    const u = squash(code("components/settings/useSettings.tsx"));
    expect(u).not.toContain("if (isSecret) setValue(\"\");");
    expect(u).toContain(
      'void s.saveField(props.group, props.field, value).then((ok) => { if (ok && isSecret) setValue((cur) => (cur === value ? "" : cur)); });'
    );
  });
});

describe("Make PR: the branch list failing is said, with a Retry", () => {
  it("names the failure, keeps typing possible and retries", () => {
    const m = code("components/dialogs/MakePrDialog.tsx");
    expect(m).toContain("setListFailed(true);");
    expect(m).toContain("Couldn't load the branch list. You can still type a branch name.");
    expect(m).toContain("onClick={() => loadBranches(target)}");
    // Retry never wipes what was typed: the reset lives in the open effect.
    const load = m.split("function loadBranches(", 2)[1].split("useEffect(", 1)[0];
    expect(load).not.toContain("setValue(repo");
  });
});

describe("Customize: Reset to default has an Undo", () => {
  it("the pane header comes back as it was", () => {
    const p = togglePaneItem(null, "map");
    useUi.getState().setPaneHeader(p);
    const undo = resetLayout("pane");
    expect(useUi.getState().paneHeader).toBeNull();
    undo();
    expect(useUi.getState().paneHeader).toEqual(p);
    useUi.getState().setPaneHeader(null);
  });

  it("the sidebar's bars, order and filter box come back as they were", () => {
    const ui = useUi.getState();
    ui.resetSidebarBars();
    ui.toggleBarHidden("verify");
    ui.setBarOrder(["prompts", "usage"]);
    ui.setShowFilter(false);
    const hidden = [...useUi.getState().hiddenBars].sort();
    const undo = resetLayout("sidebar");
    expect(useUi.getState().barOrder).toEqual([]);
    expect(useUi.getState().showFilter).toBe(true);
    undo();
    const s = useUi.getState();
    expect(s.barOrder).toEqual(["prompts", "usage"]);
    expect(s.showFilter).toBe(false);
    expect([...s.hiddenBars].sort()).toEqual(hidden);
    expect(s.barsExplicit).toBe(true);
    s.resetSidebarBars();
  });

  it("the dialog offers it in the app's undo toast", () => {
    const d = code("components/customize/CustomizeDialog.tsx");
    expect(d).toContain('action: { label: "Undo", ariaLabel: "Undo the reset", onClick: undo }');
  });
});

describe("Modal keepFocus: the To-do reminder doesn't take the keys", () => {
  const esc = () =>
    ({ key: "Escape", defaultPrevented: false, preventDefault: vi.fn(), stopPropagation: vi.fn() }) as unknown as KeyboardEvent;

  it("a passive modal on top lets Escape reach the one under it", () => {
    const under = {};
    let inside = false;
    const reminder = { passive: () => !inside };
    const popUnder = pushModal(under);
    const popReminder = pushModal(reminder);
    const closeUnder = vi.fn();
    const closeReminder = vi.fn();
    try {
      // Focus is elsewhere (typing to an agent): the reminder doesn't answer.
      expect(modalEscape(reminder, esc(), { onClose: closeReminder })).toBe(false);
      expect(modalEscape(under, esc(), { onClose: closeUnder })).toBe(true);
      expect(closeUnder).toHaveBeenCalledTimes(1);
      // Clicked into it: it is the top again, and Escape closes it.
      inside = true;
      expect(modalEscape(under, esc(), { onClose: closeUnder })).toBe(false);
      expect(modalEscape(reminder, esc(), { onClose: closeReminder })).toBe(true);
      expect(closeReminder).toHaveBeenCalledTimes(1);
    } finally {
      popReminder();
      popUnder();
    }
  });

  it("is opt-in: the default still takes focus, and only the reminder opts in", () => {
    const m = code("components/Modal.tsx");
    expect(m).toContain("keepFocus = false,");
    expect(m).toContain("if (node && !keepFocus && !node.contains(prev)) node.focus({ preventScroll: true });");
    expect(code("components/dialogs/TodoReminder.tsx")).toMatch(/<Modal\s+id="todo-reminder"[^>]*\bkeepFocus\b/);
  });
});
