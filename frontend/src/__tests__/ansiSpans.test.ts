/** lib/ansiSpans — the shared-session viewer's colour renderer. */

import { describe, expect, it } from "vitest";
import { ansiLines, runCss, xterm256 } from "../lib/ansiSpans";

describe("ansiLines", () => {
  it("splits runs on SGR and carries style across lines", () => {
    const lines = ansiLines("\x1b[1;32mok\x1b[0m plain\n\x1b[31mred\nstill red\x1b[39m done");
    expect(lines[0]).toEqual([
      { text: "ok", style: { bold: true, color: "#0dbc79" } },
      { text: " plain", style: {} },
    ]);
    expect(lines[1]).toEqual([{ text: "red", style: { color: "#cd3131" } }]);
    expect(lines[2][0]).toEqual({ text: "still red", style: { color: "#cd3131" } });
    expect(lines[2][1].text).toBe(" done");
    expect(lines[2][1].style.color).toBeUndefined();
  });

  it("reads 256-colour and truecolour, foreground and background", () => {
    const [[a, b]] = ansiLines("\x1b[38;5;196mA\x1b[48;2;1;2;3mB");
    expect(a.style.color).toBe(xterm256(196));
    expect(b.style.background).toBe("rgb(1,2,3)");
  });

  it("drops the blank rows under the prompt", () => {
    expect(ansiLines("> \n\n  \n")).toHaveLength(1);
  });
});

describe("runCss", () => {
  it("swaps colours for reverse video", () => {
    expect(runCss({ reverse: true, color: "red" })).toEqual({ color: "var(--bg)", background: "red" });
    expect(runCss({ bold: true, dim: true })).toEqual({ fontWeight: "700", opacity: "0.7" });
  });
});
