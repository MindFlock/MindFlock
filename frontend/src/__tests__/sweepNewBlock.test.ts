import { describe, expect, it } from "vitest";
import { code } from "./sourceText";

/** Stability sweep (2026-11-12): the new-session block's callbacks read the
 * guessed title from `newAt` as it was in the render Fly was pressed in —
 * before `flying` was set — so it was always undefined: a refused create (or
 * a server title that differs from the guess) left the guess pinned in the
 * saved layout, where the next session of that name then sat. The block now
 * hands its guess to onFailed / onCreated. */

describe("the new-session block tidies its guessed title away", () => {
  it("the guess rides the callbacks; the sky never reads it off a stale newAt", () => {
    const block = code("components/flights/NewSessionBlock.tsx");
    expect(block.match(/onFailed\?\.\(guess\);/g)).toHaveLength(2);
    expect(block).toContain("onCreated(title, guess);");
    const sky = code("components/flights/SkyView.tsx");
    expect(sky).toContain("onFailed={(guess) => {");
    expect(sky).toContain("onCreated={(title, guess) => {");
    expect(sky).not.toContain("const guess = newAt.flying;");
  });
});
