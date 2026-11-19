/** The React shell's side of the To-do extension: the to-do list left the
 * Assistant (its Todo button and TodoDialog are gone) and every door that
 * opened it now runs the extension's `todo.open` command; the generic bar
 * badge (ExtensionSpec.badge) and host API level 2 (ui.focusSession /
 * ui.focusedSession). Pure seams plus source-text pins for the JSX wiring the
 * node environment can't render. */

import { afterEach, describe, expect, it } from "vitest";
import { BADGE_POLL_MS, badgePath, badgeRefreshOn, badgeView } from "../extensions/badge";
import {
  HOST_API_VERSION,
  extensionEnabled,
  extensionOffText,
  focusSessionByTitle,
  focusedSessionTitle,
  runAppExtCommand,
  syncExtensions,
} from "../extensions/host";
import type { ExtensionInfo } from "../extensions/types";
import { barNote } from "../components/customize/SidebarBarsPicker";
import { TODO_EXT_ID, TODO_OPEN_COMMAND } from "../lib/todoExt";
import { useUi } from "../state/store";
import { ALL_CODE, SOURCES, code, css, cssRule } from "./sourceText";

function todoExt(enabled = true): ExtensionInfo {
  return {
    id: "todo",
    label: "To-do",
    enabled,
    origin: "builtin",
    extension: {
      module: "/extensions/todo/index.js",
      bar_label: "To-do",
      buttons: [],
      commands: [],
      surfaces: [],
      stylesheet: true,
      api_version: 1,
      badge: "/api/todo/badge",
    },
  };
}

afterEach(() => {
  syncExtensions([]);
  useUi.setState({ focused: null } as never);
});

describe("the bar badge (extensions/badge.ts)", () => {
  it("shows a positive count with its title", () => {
    expect(badgeView({ count: 3, title: "2 need you · 3 open" })).toEqual({
      count: 3,
      text: "3",
      title: "2 need you · 3 open",
    });
  });
  it("falls back to the count as the tooltip", () => {
    expect(badgeView({ count: 1 })).toEqual({ count: 1, text: "1", title: "1" });
    expect(badgeView({ count: 2, title: "  " })?.title).toBe("2");
  });
  it("caps the pill text so it keeps its width", () => {
    expect(badgeView({ count: 99 })?.text).toBe("99");
    expect(badgeView({ count: 1234 })).toMatchObject({ count: 1234, text: "99+" });
  });
  it("draws nothing for zero, negatives or anything malformed", () => {
    for (const r of [
      { count: 0 },
      { count: -2 },
      { count: "3" },
      { count: NaN },
      { count: Infinity },
      {},
      null,
      undefined,
      "3",
      [3],
    ])
      expect(badgeView(r), JSON.stringify(r)).toBeNull();
  });
  it("refetches on the extension's own addon events only", () => {
    expect(badgeRefreshOn("todo", "addon.todo.changed")).toBe(true);
    expect(badgeRefreshOn("todo", "addon.todo.anything.else")).toBe(true);
    expect(badgeRefreshOn("todo", "addon.todos.changed")).toBe(false);
    expect(badgeRefreshOn("todo", "todo.due")).toBe(false);
    expect(badgeRefreshOn("todo", "session.status_changed")).toBe(false);
    expect(badgeRefreshOn("todo", undefined)).toBe(false);
    expect(badgeRefreshOn("", "addon..x")).toBe(false);
  });
  it("only fetches a same-origin API path", () => {
    expect(badgePath("/api/todo/badge")).toBe("/api/todo/badge");
    for (const b of [null, undefined, "", "api/todo", "//evil.example/x", "https://x/y"]) expect(badgePath(b)).toBeNull();
  });
  it("polls about once a minute", () => {
    expect(BADGE_POLL_MS).toBe(60_000);
  });
  it("is wired into the host-rendered bar, after the label, inside its flex box", () => {
    const bar = code("extensions/ExtensionBar.tsx");
    expect(bar).toContain('<span className="ext-label-text">');
    expect(bar).toContain("{badge && <ExtBadge extId={ext.id} path={badge} />}");
    expect(bar).toContain('ev.subscribe("*"');
    expect(bar).toContain("refetchInterval: BADGE_POLL_MS");
    // Silent on failure.
    expect(bar).toContain("q.isError ? null : badgeView(q.data)");
    const rule = cssRule(css("extensions/extensions.css"), ".ext-bar .ext-badge");
    expect(rule).toContain("flex: none");
    expect(rule).toContain("var(--accent-rgb)");
    // The label box keeps flex: 1, so the buttons never move.
    expect(cssRule(css("extensions/extensions.css"), ".ext-bar .ext-label")).toContain("flex: 1");
  });
});

describe("host API level 2", () => {
  it("is level 2", () => {
    expect(HOST_API_VERSION).toBe(2);
  });
  it("focusSession selects an existing session and says so", () => {
    const picked: string[] = [];
    const deps = { titles: () => ["fix-login", "docs"], select: (t: string) => void picked.push(t) };
    expect(focusSessionByTitle("docs", deps)).toBe(true);
    expect(picked).toEqual(["docs"]);
  });
  it("focusSession is false, and changes nothing, for an unknown or bad title", () => {
    const picked: string[] = [];
    const deps = { titles: () => ["fix-login"], select: (t: string) => void picked.push(t) };
    for (const t of ["ghost", "", null, 42, undefined]) expect(focusSessionByTitle(t, deps)).toBe(false);
    expect(picked).toEqual([]);
  });
  it("focusedSession is the focused title, or null", () => {
    expect(focusedSessionTitle()).toBeNull();
    useUi.setState({ focused: "fix-login" } as never);
    expect(focusedSessionTitle()).toBe("fix-login");
  });
  it("hands both to every extension's ui object, and types them as optional additions", () => {
    const host = code("extensions/host.ts");
    expect(host).toContain("focusSession: (title) => focusSessionByTitle(title)");
    expect(host).toContain("focusedSession: () => focusedSessionTitle()");
    const types = code("extensions/types.ts");
    expect(types).toContain("focusSession?(title: string): boolean;");
    expect(types).toContain("focusedSession?(): string | null;");
    expect(types).toContain("badge?: string | null;");
  });
});

describe("the doors into the To-do extension", () => {
  it("know when the extension is on", () => {
    expect(extensionEnabled("todo")).toBe(false);
    syncExtensions([todoExt()]);
    expect(extensionEnabled("todo")).toBe(true);
    syncExtensions([todoExt(false)]);
    expect(extensionEnabled("todo")).toBe(false);
  });
  it("do nothing harmful when it is off, and say where to turn it on", () => {
    expect(extensionOffText("To-do")).toBe("The To-do extension is turned off (Settings → Extensions)");
    const said: string[] = [];
    expect(runAppExtCommand("todo", "todo.open", "To-do", (m) => void said.push(m))).toBe(false);
    expect(said).toEqual(["The To-do extension is turned off (Settings → Extensions)"]);
  });
  it("run the extension's open command", () => {
    expect([TODO_EXT_ID, TODO_OPEN_COMMAND]).toEqual(["todo", "todo.open"]);
    expect(code("lib/todoExt.ts")).toContain("runAppExtCommand(TODO_EXT_ID, TODO_OPEN_COMMAND, TODO_LABEL)");
  });
  it("include the due-reminder's open button", () => {
    const rem = code("components/dialogs/TodoReminder.tsx");
    expect(rem).toContain("openTodoList();");
    expect(rem).not.toContain('openDialogFor("todo"');
  });
});

describe("the to-do list left the Assistant", () => {
  it("the Assistant bar is Chat · Instructions", () => {
    const bars = code("components/sidebar/SidebarBars.tsx");
    expect(bars).toContain('id="assistant-chat-btn"');
    expect(bars).toContain('id="assistant-agent-btn"');
    expect(bars).not.toContain("assistant-todo-btn");
    expect(bars).not.toMatch(/todo list/i);
  });
  it("TodoDialog and its plumbing are gone everywhere", () => {
    expect(Object.keys(SOURCES).some((p) => /TodoDialog/.test(p))).toBe(false);
    for (const gone of ["TodoDialog", "onOpenTodo", "todo-dialog", "todo-add-input", '"todo-panel"'])
      expect(ALL_CODE, gone).not.toContain(gone);
    expect(css("components/dialogs/todoPrompts.css")).not.toContain(".todo-row");
    // The prompts panel that shared the file stays.
    expect(css("components/dialogs/todoPrompts.css")).toContain("#prompts-panel");
  });
  it("Customize describes the Assistant without a todo list, and the To-do bar on its own", () => {
    expect(barNote("assistant").text).toBe("Chat, and its editable agent file");
    expect(barNote("ext:todo").text).toBe(
      "Things you need to do by hand — manual test steps, anything waiting on you"
    );
    expect(barNote("ext:todo").link).toEqual({ label: "Settings → Extensions", dialog: "settings", target: "extensions" });
    expect(barNote("ext:someone-elses").text).toBe("From an extension");
  });
});
