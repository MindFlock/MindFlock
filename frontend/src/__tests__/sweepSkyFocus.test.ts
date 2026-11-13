/** Stability sweep (2026-11-12): the grounded line last pressed
 * (`groundFocus`) was only cleared by a press on another line, Esc or empty
 * sky. A session focused from the rail or the bell, or the grounded line
 * deleted, left it behind: two lines drew focused, and Delete looked for a
 * selected box only on that stale line — finding none, it fell through to
 * the app's keymap, which closes the focused SESSION instead of removing the
 * stop selected on its line. */
import { describe, expect, it } from "vitest";
import { code } from "./sourceText";

describe("one focused line, and Delete finds the box selected on it", () => {
  const sky = code("components/flights/SkyView.tsx");

  it("a session focused from anywhere takes the focus from a grounded line", () => {
    expect(sky).toMatch(/useEffect\(\(\) => \{\s*if \(focused\) setGroundFocus\(""\);\s*\}, \[focused\]\);/);
  });

  it("the selected box: the grounded line's, else the focused session's", () => {
    const fn = sky.slice(sky.indexOf("const selectedBox = ()"), sky.indexOf("const removeKeys = useRef"));
    expect(fn).toContain("for (const id of [groundFocus, focusLine])");
    expect(fn).not.toContain("const id = groundFocus || focusLine;");
  });
});
