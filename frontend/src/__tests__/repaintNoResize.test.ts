/** A repaint never resizes tmux (owner bug 2026-11-11, after the white-pane fix).
 *
 * The white-pane fix (paneRepaint.test.ts) repainted every terminal on screen
 * on focus / restore / moves, and its repaint FIT the terminal first. The
 * Verify watcher (useWsTerm) sent its size on every fit, changed or not, and
 * it attaches to a session the grid shows too, at another size. Sessions run
 * `window-size latest`, so each alt-tab yanked the tmux window between the two
 * sizes, the agent re-rendered, and the pane "tweaked out", jumping around its
 * history. These pin the two rules, by source (vitest here has no DOM/xterm):
 * a window-trigger repaint redraws rows only, and useWsTerm sends a size only
 * when it changed on that connection.
 */
import { describe, it, expect } from "vitest";
import PANE from "../components/grid/Pane.tsx?raw";
import WSTERM from "../lib/wsTerm.ts?raw";

/** The body of each `trackRepaint({ ... })` call in `src`. */
function trackedRepaints(src: string): string[] {
  const out: string[] = [];
  let i = src.indexOf("trackRepaint({");
  while (i !== -1) {
    let depth = 0;
    let j = src.indexOf("{", i);
    for (; j < src.length; j++) {
      if (src[j] === "{") depth++;
      else if (src[j] === "}" && --depth === 0) break;
    }
    out.push(src.slice(i, j + 1));
    i = src.indexOf("trackRepaint({", j);
  }
  return out;
}

describe("window-trigger repaints draw rows only", () => {
  it("the grid pane's hub entry never fits", () => {
    const entries = trackedRepaints(PANE);
    expect(entries.length).toBe(1);
    expect(entries[0]).toContain("repaintTerm(t.term)");
    expect(entries[0]).not.toMatch(/doFit|resync/);
  });

  it("useWsTerm's hub entry never fits", () => {
    const entries = trackedRepaints(WSTERM);
    expect(entries.length).toBe(1);
    expect(entries[0]).toContain("repaintTerm(term)");
    expect(entries[0]).not.toMatch(/doFit/);
  });
});

describe("useWsTerm sends a size only when it changed", () => {
  it("guards the resize frame with the last size sent on this connection", () => {
    const fit = WSTERM.slice(WSTERM.indexOf("const doFit = (sendResize: boolean)"));
    const body = fit.slice(0, fit.indexOf("const connect = ()"));
    expect(body).toMatch(/if \(sentSize === size\) return;/);
    expect(body.indexOf("sentSize === size")).toBeLessThan(body.indexOf('type: "resize"'));
  });

  it("a fresh socket forgets the last size (the PTY spawns 24x80)", () => {
    const conn = WSTERM.slice(WSTERM.indexOf("const connect = ()"));
    expect(conn.slice(0, conn.indexOf("sock.onopen"))).toContain('sentSize = ""');
  });
});
