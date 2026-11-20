/** Saying why the app is slow when memory is short (lib/memorySlow.ts, used
 * by components/MemoryNotice.tsx and components/ConnBanner.tsx). */

import { describe, expect, it } from "vitest";
import { FREE_MEMORY_HINT, SLOW_SESSIONS_LINE, memoryConnText, memoryIsShort } from "../lib/memorySlow";
import { code } from "./sourceText";

describe("memoryConnText", () => {
  it("says nothing while memory is fine or unknown", () => {
    expect(memoryConnText("ok", true)).toBeNull();
    expect(memoryConnText("unknown", false)).toBeNull();
    expect(memoryConnText(undefined, true)).toBeNull();
  });

  it("puts a slow server down to low memory and says what helps", () => {
    const t = memoryConnText("low", true)!;
    expect(t.head).toBe("MindFlock is slow to respond: memory is low");
    expect(t.text).toContain("slow to answer");
    expect(t.text).toContain(FREE_MEMORY_HINT);
  });

  it("names a critical level and a lagging update stream", () => {
    const t = memoryConnText("critical", false)!;
    expect(t.head).toContain("critically low");
    expect(t.text).toContain("Live updates are lagging");
    expect(t.text).toContain("sessions may be slow to respond");
  });

  it("uses no emoji and no browser dialogs", () => {
    const all = [SLOW_SESSIONS_LINE, FREE_MEMORY_HINT, memoryConnText("low", true)!.text, memoryConnText("critical", false)!.head].join(" ");
    expect(all).not.toMatch(/\p{Extended_Pictographic}/u);
    expect(code("lib/memorySlow.ts")).not.toMatch(/window\.(prompt|alert|confirm)/);
  });

  it("reads low and critical as short", () => {
    expect(memoryIsShort("low")).toBe(true);
    expect(memoryIsShort("critical")).toBe(true);
    expect(memoryIsShort("ok")).toBe(false);
  });
});

describe("wiring", () => {
  it("the memory notice says sessions may lag", () => {
    const src = code("components/MemoryNotice.tsx");
    expect(src).toContain("{SLOW_SESSIONS_LINE}");
    expect(src).toContain("mn-slow");
  });

  it("the connection card reads the last memory level and falls back after a while", () => {
    const src = code("components/ConnBanner.tsx");
    expect(src).toContain('getQueryData<MemoryReading>(["system-memory"])');
    expect(src).toContain("memoryConnText(memLevel, pollFails >= 2)");
    expect(src).toContain("SLOW_MAX_MS");
    expect(src).toContain("{head}");
  });
});
