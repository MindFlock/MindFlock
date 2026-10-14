/** Settings, round 4 (ux-lab round3/settings/report.md):
 *  - basic mode renders NO power-user fold (one advanced mechanism, not two);
 *    only Notifications' optional folds opt back in with `basic`;
 *  - phone alerts: "Set up" saves a real topic, the placeholder is no longer
 *    a random name that changes every visit, "Send a test" says why it waits;
 *  - every save marks its control "✓ Saved" (savedCue.ts), number fields
 *    refuse -2 with a message instead of saving it as "no limit";
 *  - "Search settings…" at the top of the nav, on the palette's index;
 *  - a phone-width nav is one <select>, not a re-flowing wrap of tabs;
 *  - Workspace answers "where do my worktrees live?" for every session;
 *  - People no longer points a GUI user at a repo docs path. */

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { code, css, src } from "./sourceText";

const store: Record<string, string> = {};
vi.stubGlobal("localStorage", {
  getItem: (k: string) => (k in store ? store[k] : null),
  setItem: (k: string, v: string) => {
    store[k] = String(v);
  },
  removeItem: (k: string) => {
    delete store[k];
  },
});

const { AdvancedFold, SettingsModeCtx } = await import("../components/settings/settingsMode");
const { numberFieldError } = await import("../components/settings/useSettings");
const { flashSaved, noteTouched } = await import("../components/settings/savedCue");
const { ntfyTestBlocked } = await import("../components/settings/screens/Notifications");
const { SettingsSearch } = await import("../components/settings/SettingsSearch");
const { SettingsDialog } = await import("../components/settings/SettingsDialog");
const { useUi } = await import("../state/store");
const { QueryClient, QueryClientProvider } = await import("@tanstack/react-query");

function fold(advanced: boolean, basic = false): string {
  return renderToStaticMarkup(
    createElement(
      SettingsModeCtx.Provider,
      { value: advanced },
      createElement(AdvancedFold, { id: "x-fold", basic, children: createElement("span", null, "power row") })
    )
  );
}

describe("AdvancedFold", () => {
  it("renders nothing at all in basic mode", () => {
    expect(fold(false)).toBe("");
  });
  it("is an open fold in advanced mode", () => {
    const html = fold(true);
    expect(html).toMatch(/<details[^>]*id="x-fold"[^>]*open/);
    expect(html).toContain("power row");
  });
  it("`basic` keeps it on a basic screen, closed", () => {
    const html = fold(false, true);
    expect(html).toContain('id="x-fold"');
    expect(html).not.toMatch(/<details[^>]*open/);
  });
});

describe("number fields", () => {
  it("refuse a negative with the reason, naming what blank means", () => {
    expect(numberFieldError("-2", 0, "no limit")).toBe("Enter a number, 0 or more, or leave it blank for no limit.");
    expect(numberFieldError("-1", 0, "0")).toBe("Enter a number, 0 or more.");
  });
  it("take blank, zero and positives", () => {
    for (const v of ["", "  ", "0", "3", "2.5"]) expect(numberFieldError(v), v).toBe("");
  });
  it("SettingField shows the error under the field instead of saving", () => {
    const f = src("components/settings/useSettings.tsx");
    expect(f).toContain('const why = numberFieldError(value, props.min ?? 0, props.placeholder || "");');
    expect(f).toContain("if (why) return;");
    expect(f).toContain('<span className="set-field-error" role="alert">');
  });
});

describe("save feedback", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  /** A fake control inside a `.set-row` with a `.set-label`. */
  function control(connected = true) {
    const attrs = new Set<string>();
    const label = {
      setAttribute: (k: string) => attrs.add(k),
      removeAttribute: (k: string) => attrs.delete(k),
    };
    const row = { querySelector: (sel: string) => (sel === ".set-label" ? label : null) };
    const el = { isConnected: connected, closest: () => row } as unknown as Element;
    return { el, attrs };
  }

  it("marks the changed control's label, then clears it", () => {
    const { el, attrs } = control();
    expect(flashSaved(el)).toBe(true);
    expect(attrs.has("data-saved")).toBe(true);
    vi.advanceTimersByTime(2000);
    expect(attrs.has("data-saved")).toBe(false);
  });

  it("a save through saveField marks the last touched control", () => {
    const { el, attrs } = control();
    noteTouched(el);
    expect(flashSaved()).toBe(true);
    expect(attrs.has("data-saved")).toBe(true);
  });

  it("no control on screen → false, so the caller toasts instead", () => {
    expect(flashSaved(control(false).el)).toBe(false);
  });

  it("is wired into saveField, toggles and the notification rules", () => {
    expect(src("components/settings/useSettings.tsx")).toContain('if (!flashSaved()) toast("Saved");');
    const dialog = src("components/settings/SettingsDialog.tsx");
    expect(dialog).toContain("onChangeCapture={(e) => noteTouched(e.target)}");
    expect(dialog).toContain("onBlurCapture={(e) => noteTouched(e.target)}");
    expect(src("components/settings/screens/Notifications.tsx")).toContain("if (!flashSaved(row))");
    expect(css("components/settings/settingsMode.css")).toContain('content: "✓ Saved"');
  });
});

describe("phone alerts (ntfy)", () => {
  const n = () => src("components/settings/screens/Notifications.tsx");
  it("Set up generates AND saves a topic when none is saved", () => {
    expect(n()).toContain("setSetupTick((n) => n + 1);");
    expect(n()).toContain("<Ntfy onChange={(v) => setPhoneOn(!!v.enabled)} setup={setupTick} />");
    expect(n()).toContain("if (view.topic || !view.suggested_topic) return;");
    expect(n()).toContain("void save({ topic: view.suggested_topic });");
  });
  it("never shows the unsaved random name as the topic's grey text", () => {
    expect(code("components/settings/screens/Notifications.tsx")).not.toContain("placeholder={view.suggested_topic");
    expect(n()).toContain('placeholder="press Generate"');
  });
  it("promises no code it may not show", () => {
    expect(n()).not.toContain("or scan the code");
  });
  it("Send a test says why it waits", () => {
    expect(ntfyTestBlocked("", false)).toBe("Needs a topic first: press Generate.");
    expect(ntfyTestBlocked("mindflock-abc", false)).toBe("");
    expect(n()).toContain('id="ntfy-test-why"');
  });
});

describe("Search settings…", () => {
  it("renders a search box on the palette's index", () => {
    const html = renderToStaticMarkup(createElement(SettingsSearch, { onPick: () => undefined }));
    expect(html).toContain('id="settings-search"');
    expect(html).toContain('placeholder="Search settings…"');
    const f = src("components/settings/SettingsSearch.tsx");
    expect(f).toContain('from "../palette/settingsSearch"');
    expect(f).toContain("searchSettings(query, 8)");
    expect(f).toContain("revealInSettings(e.anchor);");
  });
});

describe("the nav", () => {
  afterEach(() => {
    useUi.setState({ openDialog: null, dialogTarget: null } as never);
    Object.assign(useUi.getInitialState(), useUi.getState());
  });

  it("opens with the search box and carries a select of the same screens", () => {
    useUi.setState({ openDialog: "settings", dialogTarget: null } as never);
    Object.assign(useUi.getInitialState(), useUi.getState());
    const html = renderToStaticMarkup(
      createElement(QueryClientProvider, { client: new QueryClient() }, createElement(SettingsDialog, {}))
    );
    const nav = html.split('id="settings-nav"', 2)[1].split("</nav>", 1)[0];
    expect(nav.indexOf('id="settings-search"')).toBeLessThan(nav.indexOf("set-nav-item"));
    const select = nav.split('id="settings-nav-select"', 2)[1].split("</select>", 1)[0];
    const options = [...select.matchAll(/<option value="([^"]+)"/g)].map((m) => m[1]);
    const buttons = [...nav.matchAll(/class="set-nav-item[^"]*" data-screen="([^"]+)"/g)].map((m) => m[1]);
    expect(options).toEqual(buttons);
  });

  it("is one select at phone width, so nothing re-flows with the active tab", () => {
    const sheet = css("components/settings/screens/screens.css");
    // The block up to its closing brace at column 0.
    const narrow = sheet.split("@media (max-width: 600px)", 2)[1].split("\n}\n", 1)[0];
    expect(narrow).toMatch(/#settings-nav \.set-nav-item \{\s*display: none;/);
    expect(narrow).toMatch(/#settings-nav \.set-nav-select \{\s*display: block;/);
    expect(narrow).not.toContain("flex-wrap: wrap");
  });
});

describe("Workspace: where worktrees live", () => {
  const w = () => src("components/settings/screens/Workspace.tsx");
  it("shows both absolute paths, copyable, from the server", () => {
    expect(w()).toContain('"/api/settings/workspace-paths"');
    expect(w()).toContain('id="workspace-paths"');
    expect(w()).toContain('label="Sessions you start"');
    expect(w()).toContain('label="Ticket and PR sessions"');
    expect(w()).toContain("copyText(path)");
  });
  it("checks a typed folder and shows the warning under it", () => {
    expect(w()).toContain("pathsUrl(typed === stored ? undefined : typed)");
    expect(w()).toContain('id="workspace-dir-check"');
  });
  it("holds the worktree/clone control (moved off Engine & updates)", () => {
    expect(w()).toMatch(/group="engine"\s+field="mode"/);
    expect(code("components/settings/screens/Advanced.tsx")).not.toContain('data-field="mode"');
  });
  it("no internal hook id as an example", () => {
    expect(w()).not.toContain("gitnexus-index");
    expect(w()).toContain('placeholder="e.g. black"');
  });
});

describe("People", () => {
  it("doesn't point at a repo file a GUI user can't open", () => {
    expect(code("components/settings/screens/PeerLinks.tsx")).not.toContain("docs/peer-link.md");
  });
});
