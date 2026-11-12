/**
 * The desktop voice held to what it promises in the moments nobody plans for
 * (the stability sweep, 2026-11-12).
 *
 * The ask pop-up's mic is push to talk through the same Talk event as the
 * hotkey: in "Hold to talk" a press starts the listen and the release ends
 * it. A pointer the browser cancels (a touch turned into a scroll, a pen
 * lifted off the edge) never sends its release — the top-bar mic ends the
 * hold on pointercancel, the pop-up's did not: the mic went on capturing
 * after the finger was gone, and the next press was taken for a key's
 * auto-repeat and ignored.
 */
import { describe, expect, it } from "vitest";
import { code } from "./sourceText";

describe("the ask pop-up's mic", () => {
  const ASK = code("components/dialogs/AskDialog.tsx");
  const row = ASK.slice(ASK.indexOf("function AskMicRow("));

  it("ends a hold when the pointer is cancelled, as the top-bar mic does", () => {
    expect(row).toContain('onPointerUp={() => talk("up")}');
    expect(row).toContain('onPointerCancel={() => talk("up")}');
    const MIC = code("components/TopBarMic.tsx");
    expect(MIC).toMatch(/onPointerCancel=\{\(\) => \{\s*if \(!holding\.current\) return;\s*holding\.current = false;\s*finish\(\);/);
  });
});
