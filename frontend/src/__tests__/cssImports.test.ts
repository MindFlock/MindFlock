/** Every component stylesheet must actually be in the bundle.
 *
 * Styles are collected by hand in styles/index.css (an ordered @import list —
 * the numbers are the cascade), NOT by importing CSS from the component that
 * uses it. That keeps the order explicit and reviewable, at the cost of one
 * failure mode: write Foo.tsx and Foo.css, forget the @import line, and
 * everything still compiles, the build still succeeds, CI still passes — the
 * component just renders with no styling at all. That is how HistoryOverlay
 * shipped: `position: absolute; inset: 0` never reached the page, so the
 * overlay opened as an unstyled block nobody could see, and the feature read
 * as "the gesture does nothing".
 *
 * A stylesheet imported directly from a .ts/.tsx module is fine too — it still
 * lands in the bundle. What must never happen is a .css file that nothing
 * references.
 */

import { describe, it, expect } from "vitest";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, relative } from "node:path";

const SRC = new URL("..", import.meta.url).pathname; // frontend/src

function walk(dir: string, hit: (path: string) => void): void {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) walk(p, hit);
    else hit(p);
  }
}

describe("stylesheet wiring", () => {
  it("imports every component stylesheet", () => {
    const sheets: string[] = [];
    const modules: string[] = [];
    walk(SRC, (p) => {
      if (p.endsWith(".css")) sheets.push(p);
      else if (p.endsWith(".ts") || p.endsWith(".tsx")) modules.push(p);
    });

    const index = readFileSync(join(SRC, "styles", "index.css"), "utf8");
    // The aggregator imports the rest; a sheet it pulls in is by definition
    // reachable, and so is one a module imports directly.
    const importedByModule = modules
      .map((m) => readFileSync(m, "utf8"))
      .join("\n");

    const orphans = sheets
      .filter((p) => !p.endsWith(join("styles", "index.css")))
      .filter((p) => {
        const base = p.slice(p.lastIndexOf("/") + 1);
        return !index.includes(base) && !importedByModule.includes(base);
      })
      .map((p) => relative(SRC, p));

    expect(orphans).toEqual([]);
  });
});
