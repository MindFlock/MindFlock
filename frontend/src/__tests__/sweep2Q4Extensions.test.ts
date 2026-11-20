/** UI sweep round 2, package Q4 — #14: a failed extension window can retry.
 *
 * A module that failed to load once (e.g. during a server restart) used to say
 * "failed" until a page reload, and "See Settings → Extensions for details."
 * was plain text. Now the window has a "Try again" (the host loads it again in
 * place, under a new module URL) and the Settings line is a link. */

import { describe, expect, it, vi } from "vitest";
import { createElement } from "react";
import { renderToString } from "react-dom/server";
import { code, squash } from "./sourceText";
import type { ExtensionInfo } from "../extensions/types";

// A failed activation toasts; the toast host is DOM, and not under test.
vi.mock("../lib/toast", () => ({ toast: () => {} }));

const host = await import("../extensions/host");
const { ExtSurfaceError } = await import("../extensions/ExtPaneBody");

// The host touches `window` (makeApi's shared seams) and `document` (each
// window's keep-alive body); node has neither, and nothing here needs more
// than these few members. Set after the imports: modules that check for a
// DOM at import time (the UI store) must still see none.
type FakeEl = { className: string; textContent: string; remove(): void; replaceWith(n: FakeEl): void };
const g = globalThis as Record<string, unknown>;
g.window ??= {};
g.document ??= {
  createElement: (): FakeEl => ({ className: "", textContent: "", remove() {}, replaceWith() {} }),
  head: { appendChild() {} },
};

const EXT: ExtensionInfo = {
  id: "retryfix",
  label: "Retry fixture",
  enabled: true,
  extension: {
    module: "/src/__tests__/fixtures/extRetry.js",
    buttons: [],
    commands: [],
    surfaces: [{ id: "pane", kind: "pane", title: "Retry fixture" }],
    stylesheet: false,
    api_version: 1,
  },
} as unknown as ExtensionInfo;

const settle = () => new Promise((r) => setTimeout(r, 50));

describe("Try again on a failed extension window", () => {
  it("loads the module again in place and renders the window", async () => {
    host.syncExtensions([EXT]);
    const key = host.openExtPane("retryfix", "pane");
    expect(key).toBe("retryfix:pane");
    await settle();
    // The first load failed: the window says so, and stays failed by itself.
    expect(host.extPaneView(key)).toMatchObject({ status: "error", error: "server restarting" });
    await host.activateExtension(EXT);
    expect(host.extPaneView(key).status).toBe("error");

    host.retryExtSurface(key, "pane");
    await settle();
    expect(host.extPaneView(key).status).toBe("ready");
    expect(host.extActivationError("retryfix")).toBeUndefined();
    host.syncExtensions([]);
  });

  it("the import URL carries the retry, so a remembered failure can't answer", () => {
    const HOST = squash(code("extensions/host.ts"));
    expect(HOST).toContain('const url = rec.retries ? spec.module + "?retry=" + rec.retries : spec.module;');
    expect(HOST).toContain("await import( url)"); // (the @vite-ignore comment is stripped)
  });

  it("the window shows Try again and a link to Settings → Extensions", () => {
    let retried = 0;
    const html = renderToString(
      createElement(ExtSurfaceError, { label: "Database", error: "boom", onRetry: () => (retried += 1) })
    ).replace(/<!-- -->/g, "");
    expect(html).toContain("Database failed: boom");
    expect(html).toContain(">Try again<");
    expect(html).toMatch(/<button type="button" class="linklike">Settings → Extensions<\/button>/);
    expect(retried).toBe(0);
    const PANE = squash(code("extensions/ExtPaneBody.tsx"));
    expect(PANE).toContain('onClick={() => openSettings("extensions")}');
    expect(PANE).toContain('onRetry={() => retryExtSurface(extKey, "pane")}');
    expect(squash(code("extensions/ExtensionDialog.tsx"))).toContain(
      'onRetry={() => retryExtSurface(target, "dialog")}'
    );
    // The plain-text line is gone from both windows.
    expect(code("extensions/ExtensionDialog.tsx")).not.toContain("See Settings → Extensions for details.");
  });
});
