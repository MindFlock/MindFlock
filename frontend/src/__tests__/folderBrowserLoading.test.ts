/** The New Session folder browser says "Loading…" until its first listing
 * lands, instead of an empty list. Rendered with renderToString (node env, no
 * jsdom): effects never run there, so the render is exactly the moment before
 * /api/browse answers. */

import { createElement } from "react";
import { renderToString } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { css } from "./sourceText";

const { FolderBrowser } = await import("../components/dialogs/newSession/FolderBrowser");

const render = () =>
  renderToString(
    createElement(FolderBrowser, {
      initialPath: "/home/me",
      selected: "",
      onSelect: () => {},
      onPick: () => {},
    })
  );

describe("FolderBrowser before the first listing", () => {
  it("shows Loading… inside the list", () => {
    const html = render();
    expect(html).toMatch(/<div id="rb-list"[^>]*><p class="rb-loading">Loading…<\/p>/);
    // No rows and no "use this folder" yet: there is nothing to use.
    expect(html).not.toContain("rb-use");
  });

  it("is styled as a muted line", () => {
    expect(css("components/dialogs/NewSessionDialog.css")).toMatch(/\.rb-loading\s*\{[^}]*color:\s*var\(--muted\)/);
  });
});
