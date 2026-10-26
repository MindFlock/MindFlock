/** Settings revamp: dedicated Notifications / Connections / IDE / Security
 * screens, browser-notification opt-in, the budget-lock overlay, System logs,
 * the grouped settings nav, and the removals (sidebar checkbox, foreign-repo
 * chip, pane maximize, provider login modal).
 *
 * Moved from tests/unit/test_frontend_settings_revamp.py, which grepped the
 * built app.js / style.css. Here each assertion is pinned to the module that
 * owns it (or exercised directly where it is a function), so a bundler bump
 * cannot break it and a stray copy elsewhere cannot satisfy it. The Settings
 * panels' query-cache half lives in intakePanels.test.ts; the route contract
 * (the paths the bundle calls exist on the server) stays in Python. */

import { afterEach, describe, expect, it } from "vitest";
import { ALL_CODE, SRC, dropComments, squash, src } from "./sourceText";
import { KEYMAP } from "../lib/keymap";
import { attachCopyOnSelect } from "../lib/terminals";
import { notifFromEvent } from "../components/NotificationsBell";
import { showSetupCard } from "../components/setup/setupModel";
import type { EventEnvelope } from "../state/queries";

const count = (hay: string, needle: string) => hay.split(needle).length - 1;
/** The declaration block of `selector {` in a stylesheet. */
const cssBlock = (css: string, selector: string) => {
  const at = css.indexOf(selector + " {");
  expect(at, selector).toBeGreaterThanOrEqual(0);
  return css.slice(at, css.indexOf("}", at));
};

const DIALOG = src("components/settings/SettingsDialog.tsx");
/** SCREENS, in order: key, label, nav group ("" for the ungrouped head). */
const SCREENS = [
  ...squash(DIALOG.split("const SCREENS = [", 2)[1].split("] as const", 1)[0]).matchAll(
    /\{ key: "([^"]+)", label: "([^"]+)",(?: group: "([^"]+)",)?/g
  ),
].map((m) => ({ key: m[1], label: m[2], group: m[3] || "" }));
const KEYS = SCREENS.map((s) => s.key);
const screen = (key: string) => SCREENS.find((s) => s.key === key);

const SYSLOGS = src("components/settings/screens/SystemLogs.tsx");

describe("settings screens", () => {
  it("every screen leads with a section-title heading", () => {
    // 14 since ticketing / PR review / git issues became Intake tabs, where the
    // tab strip is the heading.
    expect(count(ALL_CODE, "set-section-title")).toBeGreaterThanOrEqual(14);
  });

  it("a switch flips only on the switch, not the whole row", () => {
    // The row is a <div> and the .ca-switch is its own <label> (which still gives
    // the slider its click-to-toggle) — the inverse of the original layout, where
    // the whole row was one <label>.
    expect(ALL_CODE).toContain("set-switch-row");
    expect(ALL_CODE).toMatch(/<label\s+className="ca-switch"/);
    // …and none renders as a bare span/div (which wouldn't toggle at all).
    expect(ALL_CODE).not.toMatch(/<(?:span|div)\b[^>]*className=\{?"ca-switch[" ]/);
    // No switch row is a <label>: a row-wide label flips the toggle from
    // anywhere on the row, which is exactly the behaviour being removed.
    expect(ALL_CODE).not.toMatch(/<label\b[^>]*set-switch-row/);
  });

  it("has the new screens: connections, notifications, ide, security", () => {
    for (const k of ["connections", "notifications", "ide", "security"]) expect(KEYS).toContain(k);
    // Auth-mode picker bound to the generic settings wiring.
    expect(src("components/settings/screens/Security.tsx")).toContain('"auth_mode"');
    // Browser (Chrome) notification opt-in.
    expect(src("components/settings/screens/Notifications.tsx")).toContain('id="notif-browser-toggle"');
  });

  it("renders Connections inline — no separate manager modal", () => {
    const conn = src("components/settings/screens/Connections.tsx");
    expect(conn).toContain('id="settings-connections-list"');
    expect(KEYS).toContain("connections");
    expect(ALL_CODE).not.toContain("settings-open-connections"); // old "open manager" button gone
    expect(conn).toContain("conn-configure");
    expect(conn).toContain('"/api/connections'); // reads the status endpoint
  });

  it("relabels coding + providers to Agents / Custom agents (keys unchanged)", () => {
    // GLOSSARY (round 2): "Agents" and "Custom agents", never "Agent CLI".
    expect(screen("coding")?.label).toBe("Agents");
    expect(screen("providers")?.label).toBe("Custom agents");
    expect(ALL_CODE).not.toContain('label: "Agent CLI"');
    expect(ALL_CODE).not.toContain('label: "Agent providers"');
    expect(ALL_CODE).not.toContain('label: "Coding CLI"');
    expect(ALL_CODE).not.toContain('label: "Providers"');
  });

  it("puts Appearance + Mobile after Security, near the end", () => {
    for (const k of ["security", "appearance", "mobile", "doctor"]) expect(KEYS).toContain(k);
    const i = (k: string) => KEYS.indexOf(k);
    expect(i("security")).toBeLessThan(i("appearance"));
    expect(i("appearance")).toBeLessThan(i("mobile"));
    expect(i("mobile")).toBeLessThan(i("doctor"));
    // Appearance/Mobile trail General rather than following it immediately.
    expect(i("appearance")).toBeGreaterThan(i("general") + 1);
  });

  it("groups the nav under headings without touching keys or labels", () => {
    const group = (k: string) => screen(k)?.group;
    for (const k of ["general", "connections", "notifications"]) expect(group(k)).toBe("");
    const expectGroups: Record<string, string[]> = {
      Agents: ["coding", "providers", "accounts", "localmodel"],
      Code: ["workspace", "ide"],
      "This device": ["security", "appearance"],
      "Devices and people": ["devices", "mobile", "voice", "peer"],
      Extensions: ["extensions"],
      Troubleshooting: ["doctor", "logs", "advanced", "traffic"],
    };
    for (const [g, keys] of Object.entries(expectGroups)) for (const k of keys) expect(group(k), k).toBe(g);
    // The group follows the label, so the label pin above stays contiguous.
    expect(squash(DIALOG)).toContain('{ key: "coding", label: "Agents", group: "Agents"');
    // A non-clickable heading before each group's first screen.
    expect(DIALOG).toContain('className="set-nav-group"');
    // (Advanced mode: settingsMode.tsx's navHeading; basic mode lists the
    // few screens flat — see settingsMode.test.ts.)
    expect(DIALOG).toContain("navHeading(shown, i, advanced)");
    expect(src("components/settings/settingsMode.tsx")).toContain("s.group !== shown[i - 1]?.group");
    expect(src("components/settings/SettingsDialog.css")).toContain(".set-nav-group {");
  });

  it("scrolls the nav column so every item stays reachable", () => {
    const nav = cssBlock(src("components/settings/screens/screens.css"), "#settings-nav");
    expect(nav).toContain("overflow-y: auto");
    expect(nav).toContain("min-height: 0");
  });
});

describe("System logs", () => {
  it("is a nav item with its markup, loader and endpoint", () => {
    expect(screen("logs")?.label).toBe("System logs");
    expect(SYSLOGS).toContain('className="logs-toolbar"');
    for (const id of ["logs-view", "logs-source", "logs-refresh", "logs-follow"]) {
      expect(SYSLOGS).toContain(`id="${id}"`);
    }
    expect(SYSLOGS).toContain('"/api/logs?name="');
    expect(SYSLOGS).toContain("Open as pane");
  });

  it("makes the log path clickable — reveal in the desktop shell, copy otherwise", () => {
    expect(SYSLOGS).toContain('id="logs-path"');
    expect(SYSLOGS).toContain("showItem"); // the desktop reveal bridge
    expect(src("components/settings/screens/mobileLogs.css")).toContain(".logs-path {");
  });

  it("has a Copy-log button", () => {
    expect(SYSLOGS).toContain('id="logs-copy"');
  });

  it("opens as a pane from Settings only, never an up-front sidebar button", () => {
    expect(SYSLOGS).toContain('id="logs-open-pane"');
    expect(ALL_CODE).not.toContain("syslogs-btn");
    const pane = src("components/grid/SpecialPane.tsx");
    for (const sym of ["syslogs-pane", "syslogs-view", "/api/logs?name="]) expect(pane).toContain(sym);
    expect(src("components/grid/TerminalGrid.tsx")).toContain('"grid-row"'); // a grid pane like the others
    expect(src("components/settings/screens/mobileLogs.css")).toContain(".syslogs-view {");
  });
});

describe("keyboard", () => {
  const reload = KEYMAP.find((e) => e.id === "reload")!;
  const g = globalThis as Record<string, unknown>;
  const saved = { document: g.document, location: g.location };
  afterEach(() => {
    g.document = saved.document;
    g.location = saved.location;
  });

  it("Ctrl/Cmd+R reloads the app — unless a terminal is focused", () => {
    expect(reload).toMatchObject({ key: "r", mod: true });
    // With a terminal focused the key falls through to the shell as
    // reverse-i-search instead of reloading.
    const focus = (inXterm: boolean) => {
      g.document = { activeElement: { closest: (sel: string) => (sel === ".xterm" && inXterm ? {} : null) } };
    };
    focus(true);
    expect(reload.when!()).toBe(false);
    focus(false);
    expect(reload.when!()).toBe(true);
    let reloaded = 0;
    g.location = { reload: () => reloaded++ };
    reload.run({} as KeyboardEvent);
    expect(reloaded).toBe(1);
    expect(reload.help?.[2]).toBe("Reload the app (when a terminal isn’t focused)");
  });

  it("the terminal hands Ctrl/Cmd +/-/0 to the browser's native zoom", () => {
    let handler: ((ev: Partial<KeyboardEvent>) => boolean) | undefined;
    const term = {
      options: { disableStdin: true },
      loadAddon: () => {},
      onSelectionChange: () => {},
      getSelection: () => "",
      getSelectionPosition: () => undefined,
      attachCustomKeyEventHandler: (fn: typeof handler) => {
        handler = fn;
      },
    };
    const host = { title: "", addEventListener: () => {} };
    attachCopyOnSelect(host as never, term as never, {});
    const key = (code: string, mods: Partial<KeyboardEvent> = { ctrlKey: true }) =>
      handler!({ type: "keydown", key: "", code, shiftKey: false, altKey: false, ...mods });
    // Matched on .code, so zoom-in via Ctrl+Shift+= works too.
    for (const code of ["Equal", "Minus", "Digit0", "NumpadAdd"]) {
      expect(key(code), code).toBe(false);
      expect(key(code, { metaKey: true }), "Cmd " + code).toBe(false);
    }
    expect(key("Equal", { ctrlKey: true, shiftKey: true })).toBe(false);
    // Without the modifier they are just keys for the shell.
    expect(key("Equal", {})).toBe(true);
    // The rejected CSS-zoom approach (it shifted the layout off-center) stays out.
    expect(ALL_CODE).not.toContain("documentElement.style.zoom");
  });

  it("the shortcuts sheet documents zoom and reload", () => {
    const sheet = src("components/palette/ShortcutsSheet.tsx");
    for (const s of ["Zoom in (desktop app)", "Zoom out (desktop app)", "Reset zoom (desktop app)", "Reload the desktop shell"]) {
      expect(sheet).toContain(`"${s}"`);
    }
  });

  it("New session handles Ctrl/Cmd+Enter at the dialog level: submits the form, or reads the sentence on page 1", () => {
    // There is more than one Ctrl/Cmd+Enter in the app — the New → Ticket pane
    // has its own, and it files rather than submits — so the dialog-level one is
    // identified by what only it sits next to (the folder browser's Escape).
    const marker = 'e.key === "Enter" && (e.ctrlKey || e.metaKey)';
    const windows: string[] = [];
    for (const [path, raw] of Object.entries(SRC)) {
      const text = dropComments(raw);
      let at = text.indexOf(marker);
      while (at >= 0) {
        windows.push(path + "\n" + text.slice(Math.max(0, at - 400), at + 400));
        at = text.indexOf(marker, at + 1);
      }
    }
    const handlers = windows.filter((w) => w.includes("browserOpen"));
    expect(handlers).toHaveLength(1); // the dialog-level keydown is unambiguous
    const handler = handlers[0];
    expect(handler.startsWith("components/dialogs/NewSessionDialog.tsx\n")).toBe(true);
    expect(handler).toContain("submit();");
    // Page 1 shows no form, so the shortcut reads the sentence there instead
    // of creating an unseen "untitled" session (ctrlEnterAction).
    expect(handler).toContain("ctrlEnterAction({ page, runMode })");
    // …within the same keydown that closes the browser overlay first, then the dialog.
    expect(handler).toContain('e.key === "Escape"');
    expect(handler).toContain("browserOpen");
    expect(handler).toContain("closeDialog()");
    // The prompt textarea carries no onKeyDown, so a plain Enter still newlines.
    const fold = src("components/dialogs/newSession/PromptFold.tsx");
    const prompt = fold.slice(fold.indexOf('id="new-prompt"'));
    expect(prompt.slice(0, prompt.indexOf("/>"))).not.toContain("onKeyDown");
    // Deliberately not advertised in the dialog any more.
    expect(ALL_CODE).not.toContain("Ctrl+Enter creates");
  });
});

describe("notifications + first run", () => {
  const env = (e: Partial<EventEnvelope>): EventEnvelope => ({
    event: "",
    session: "s",
    seq: 1,
    ts: 0,
    old: null,
    new: null,
    data: {},
    ...e,
  });

  it("surfaces a forced start that died in provisioning", () => {
    // It only emits session.create_failed; without a case the user saw an
    // optimistic "starting…" toast and then nothing.
    expect(notifFromEvent(env({ event: "session.create_failed", data: { error: "clone failed" } }))).toEqual({
      text: "couldn't start — clone failed",
      cls: "n-warn",
    });
    expect(notifFromEvent(env({ event: "session.create_failed" }))?.text).toBe("couldn't start — failed");
  });

  it("gates first run on the server's onboarded flag, never a per-browser one", () => {
    // `onboarded` is the whole gate until /api/setup answers; the checklist is it after.
    expect(showSetupCard({ sessions: 0, status: null, onboarded: false })).toBe(true);
    expect(showSetupCard({ sessions: 0, status: null, onboarded: true })).toBe(false);
    expect(src("components/setup/useSetup.ts")).toContain('"/api/setup"');
    expect(ALL_CODE).not.toContain("shouldAutoShowSetup"); // the Setup modal never opens itself
    // Per-browser flags let a cleared profile or a second device replay first
    // run at a veteran; asserting they stay gone keeps one from creeping back.
    for (const retired of ["mf_setup_done", "mf_ever_created", "mf_tour_done"]) {
      expect(ALL_CODE, retired).not.toContain(retired);
    }
  });

  it("wires the budget-lock overlay", () => {
    const pane = src("components/grid/Pane.tsx");
    expect(pane).toContain('className="budget-lock"');
    expect(pane).toContain('"/budget/raise"');
    expect(pane).toContain("Budget reached");
    const css = src("components/grid/budgetNotifyTemplates.css");
    for (const sel of [".budget-lock {", ".budget-lock .bl-card {", ".budget-lock .bl-dur {"]) expect(css).toContain(sel);
  });
});

describe("agent sign-in", () => {
  it("removed the old provider login modal and makeTerm's absolutePath branch", () => {
    for (const gone of ["makeLoginTerm", "ProviderLoginModal", "loginFor"]) expect(ALL_CODE, gone).not.toContain(gone);
    // Signing in lives in exactly one owner of the login terminal.
    const owners = Object.entries(SRC)
      .filter(([, t]) => t.includes("/login-terminal"))
      .map(([p]) => p);
    expect(owners).toEqual(["components/dialogs/AgentSignIn.tsx"]);
    // makeTerm's connect() builds the instance path unconditionally.
    expect(ALL_CODE).not.toContain('absolutePath ?? "/api/instances/"');
    expect(src("lib/terminals.ts")).toContain('"/api/instances/" + encodeURIComponent(title) + wsPath');
  });

  it("Agent CLI reads /api/providers/status and persists a corrected default", () => {
    const cli = src("components/settings/screens/CodingCli.tsx");
    expect(cli).toContain('"/api/providers/status"'); // not /manage, so `installed` is present
    expect(cli).toContain("filter((p) => p.installed)"); // default candidates = installed CLIs
    // A missing stored default falls back to the first installed CLI AND persists it.
    expect(cli).toContain('saveField("coding_cli", "default_provider"');
    expect(cli).toContain("installed[0]");
  });

  it("Agent providers signs in through the shared window", () => {
    const providers = src("components/settings/screens/Providers.tsx");
    expect(providers).toContain("Agent providers");
    const rows = src("lib/providerRows.ts");
    expect(rows).toContain("install_hint");
    expect(rows).toContain("not installed");
    expect(ALL_CODE).not.toContain("ProviderLoginModal");
    expect(providers).not.toContain("login-terminal"); // the route lives in AgentSignIn only
    // Its sign-in is the shared agent card's (which renders AgentSignIn).
    expect(providers).toContain("<AgentCard");
    expect(src("components/settings/screens/Connections.tsx")).toContain("<AgentSignIn");
    expect(providers).toContain("/binary-path");
    // Row shapes are the unit-tested pure module, not inline JSX conditions.
    expect(providers).toContain("providerRows(");
    expect(rows).toContain("no sign-in needed");
    expect(rows).toContain('kind: "error"');
  });
});
