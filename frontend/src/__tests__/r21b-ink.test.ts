/** Round 21b "ink" (JUDGE-R21-3 new findings 3, 5, 7): colour on the Sky
 * means STATE. A route's wires, its head's port and icon, and a
 * sub-session's frame are drawn in a neutral ink (theme tokens, light and
 * dark); the session's identity colour is only a dot. */

/// <reference types="vite/client" />
import { describe, expect, it } from "vitest";
import { css } from "./sourceText";

const SKY = css("components/flights/SkyChain.css");

/** The body of the first rule whose selector is exactly `sel`. */
const rule = (sheet: string, sel: string): string => {
  const at = sheet.indexOf(sel + " {");
  if (at < 0) throw new Error("no rule " + sel);
  return sheet.slice(at, sheet.indexOf("}", at));
};

describe("finding 3: the identity hue is only a dot", () => {
  it("a route's own wires are the neutral ink, never an inline session colour", () => {
    expect(rule(SKY, ".sky-wire-line,\n.sky-wire-head")).toContain("stroke: var(--sky-ink)");
  });

  it("the ink is a theme token, set for dark and for light", () => {
    expect(rule(SKY, ".sky-chain")).toMatch(/--sky-ink: color-mix\(in srgb, var\(--text\)/);
    expect(rule(SKY, ".light .sky-chain")).toMatch(/--sky-ink: color-mix\(in srgb, var\(--text\)/);
    expect(rule(SKY, ".sky-chain")).toMatch(/--sky-head-ink:/);
    expect(rule(SKY, ".light .sky-chain")).toMatch(/--sky-head-ink:/);
  });

  it("the head's icon, out port and a trigger's border are neutral", () => {
    expect(rule(SKY, ".sky-head .sky-tile-icon")).toContain("var(--sky-head-ink)");
    expect(rule(SKY, ".sky-head .sky-port-out")).not.toContain("--sky-head-color");
    expect(rule(SKY, ".sky-head-trigger .sky-tile")).not.toContain("--sky-head-color");
  });

  it("only the dots wear the identity colour", () => {
    const uses = SKY.split("}").filter((r) => /--sky-(head|sub)-color/.test(r)).map((r) => r.slice(0, r.indexOf("{")).trim());
    expect(uses.sort()).toEqual([".sky-head-dot", ".sky-sub-dot"]);
  });
});
