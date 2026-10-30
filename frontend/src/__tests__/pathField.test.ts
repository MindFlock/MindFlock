/** Tab completion in PathField (usePathCompletion + PathCandidates) — every
 * "type OR Browse…" folder field completes like a shell. The vitest
 * environment is node: the markup through react-dom/server, the key handling
 * through source pins, the rules themselves in pathComplete.test.ts. */

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import { code, src } from "./sourceText";

const api = vi.fn();
vi.mock("../api/client", async (orig) => ({
  ...((await orig()) as object),
  api: (...a: unknown[]) => api(...a),
}));

const { PathCandidates, PathField, browseNames, remoteCompletionTitle } = await import("../components/PathField");

const PATHFIELD = code("components/PathField.tsx");
const CSS = src("components/PathField.css");

describe("the field", () => {
  it("renders the input first, then Browse…, then a polite live region — no row until a Tab", () => {
    const html = renderToStaticMarkup(
      createElement(PathField, { id: "ws", value: "~/pro", onChange: () => undefined })
    );
    expect(html).toMatch(/^<span class="path-field"><input[^>]*id="ws"/);
    expect(html).toContain('<span class="path-field-live" role="status" aria-live="polite"></span>');
    expect(html).not.toContain("path-cands");
    expect(html).not.toContain("<datalist");
    // No title of its own on this computer: the caller's (if any) stands.
    expect(html).not.toMatch(/<input[^>]*title=/);
  });

  it("tells another device's field that Tab won't complete there", () => {
    const html = renderToStaticMarkup(
      createElement(PathField, { value: "/srv", device: "mac-mini", onChange: () => undefined })
    );
    expect(html).toContain('title="' + remoteCompletionTitle("mac-mini") + '"');
    expect(remoteCompletionTitle("mac-mini")).toMatch(/this computer only.*mac-mini/);
  });

  it("lets a caller's own title win", () => {
    const html = renderToStaticMarkup(
      createElement(PathField, { value: "/srv", device: "mac", title: "mine", onChange: () => undefined })
    );
    expect(html).toContain('title="mine"');
  });
});

describe("the candidates row", () => {
  it("is a wrapping row of button chips with the highlight marked, never a datalist", () => {
    const html = renderToStaticMarkup(
      createElement(PathCandidates, {
        id: "ws-matches",
        prefix: "~/",
        matches: ["projects", "proto"],
        sel: 1,
        onPick: () => undefined,
      })
    );
    expect(html).toContain('<span id="ws-matches" class="path-cands" role="group" aria-label="Folders that match">');
    expect(html).toMatch(/<button type="button" tabindex="-1" class="path-cand" aria-pressed="false" title="~\/projects\/">projects\/<\/button>/);
    expect(html).toMatch(/<button type="button" tabindex="-1" class="path-cand active" aria-pressed="true" title="~\/proto\/">proto\/<\/button>/);
    expect(html).toContain("Tab cycles · Enter takes · Esc closes");
  });

  it("is styled by the app's own tokens, light and dark alike", () => {
    expect(CSS).toMatch(/\.path-cands\s*\{[^}]*flex-wrap:\s*wrap/);
    expect(CSS).toMatch(/\.path-cand\.active\s*\{[^}]*border-color:\s*var\(--accent\)/);
    expect(CSS).toMatch(/@keyframes path-miss\b/);
    expect(CSS).toMatch(/prefers-reduced-motion/);
    const added = CSS.slice(CSS.indexOf(".path-cands"));
    expect(added).not.toMatch(/#[0-9a-f]{3,6}\b/i);
  });
});

describe("browseNames", () => {
  it("lists one folder through /api/browse (\"\" = home), names only", async () => {
    api.mockResolvedValueOnce({ path: "/home/me", entries: [{ name: "Music", path: "/home/me/Music", is_git: false }] });
    expect(await browseNames("~/a b")).toEqual(["Music"]);
    expect(api).toHaveBeenLastCalledWith("/api/browse?path=~%2Fa%20b");
    api.mockResolvedValueOnce({});
    expect(await browseNames("")).toEqual([]);
    expect(api).toHaveBeenLastCalledWith("/api/browse?path=");
  });
});

describe("the key handling (source pins)", () => {
  it("runs completion before the caller's own onKeyDown", () => {
    expect(PATHFIELD).toMatch(/onKeyDown=\{\(e\) => \{\s*if \(!comp\.onKeyDown\(e\)\) onKeyDown\?\.\(e\);/);
  });

  it("takes a plain Tab only at the end of a non-empty path, on this computer", () => {
    expect(PATHFIELD).toContain('e.key === "Tab" && !e.ctrlKey && !e.metaKey && !e.altKey');
    expect(PATHFIELD).toContain("if (e.shiftKey || device) return false;");
    expect(PATHFIELD).toContain("if (!text || el.selectionStart !== text.length || el.selectionEnd !== text.length) return false;");
    // The focus stays in the field while the listing is fetched.
    expect(PATHFIELD).toMatch(/e\.preventDefault\(\);\s*void complete\(text\);/);
  });

  it("cycles with Tab / Shift+Tab once the row is open", () => {
    expect(PATHFIELD).toContain('cycle(row.matches, latest.current, row.prefix, e.shiftKey ? -1 : 1)');
  });

  it("closes only the row on Escape, never the dialog around it", () => {
    expect(PATHFIELD).toMatch(/e\.key === "Escape"\) \{\s*e\.preventDefault\(\);\s*e\.stopPropagation\(\);\s*setRow\(null\);/);
  });

  it("fills through onChange, exactly like typing, and drops a late listing", () => {
    expect(PATHFIELD).toContain("if (r && r.text !== text) onChange(r.text);");
    expect(PATHFIELD).toContain("if (latest.current !== text) return;");
  });

  it("never uses a native <datalist> or list= attribute, nor prompt/alert/confirm", () => {
    expect(PATHFIELD).not.toMatch(/<datalist|\blist="/);
    expect(PATHFIELD).not.toMatch(/window\.prompt|\bprompt\(|confirm\(|alert\(/);
  });
});
