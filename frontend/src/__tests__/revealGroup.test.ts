/** lib/revealGroup.ts — a group event with no session and no lead (a finish, a
 * shipped line, an escalation already answered) shows the group where it
 * lives: its rail header, scrolled to and pulsed. A group whose header is gone
 * is a quiet no-op, never an error. Node environment: a stub document. */
import { describe, it, expect, afterEach, vi } from "vitest";
import { revealGroup } from "../lib/revealGroup";

const g = globalThis as Record<string, unknown>;
const hadDoc = "document" in g;
const prevDoc = g.document;
const hadWin = "window" in g;
const prevWin = g.window;

afterEach(() => {
  vi.useRealTimers();
  if (hadDoc) g.document = prevDoc;
  else delete g.document;
  if (hadWin) g.window = prevWin;
  else delete g.window;
});

function head(run: string) {
  const cls = new Set<string>();
  return {
    dataset: { run },
    offsetWidth: 0,
    scrolled: 0,
    scrollIntoView() {
      this.scrolled++;
    },
    classList: {
      add: (c: string) => cls.add(c),
      remove: (c: string) => cls.delete(c),
      has: (c: string) => cls.has(c),
    },
  };
}

function withHeads(heads: ReturnType<typeof head>[]) {
  let asked = "";
  g.document = {
    querySelectorAll: (sel: string) => {
      asked = sel;
      return heads;
    },
  };
  g.window = { setTimeout: (fn: () => void, ms: number) => setTimeout(fn, ms) };
  return () => asked;
}

describe("revealGroup", () => {
  it("scrolls the group's own header into view and pulses it once", () => {
    vi.useFakeTimers();
    const a = head("r1");
    const b = head('r"2');
    const asked = withHeads([a, b]);
    expect(revealGroup('r"2')).toBe(true);
    expect(asked()).toBe("li.run-group-head[data-run]");
    expect(b.scrolled).toBe(1);
    expect(b.classList.has("rg-flash")).toBe(true);
    expect(a.scrolled).toBe(0);
    expect(a.classList.has("rg-flash")).toBe(false);
    vi.advanceTimersByTime(1700);
    expect(b.classList.has("rg-flash")).toBe(false);
  });

  it("is a quiet no-op for a pruned group, no id, or no document", () => {
    withHeads([head("r1")]);
    expect(revealGroup("gone")).toBe(false);
    expect(revealGroup("")).toBe(false);
    expect(revealGroup(null)).toBe(false);
    delete g.document;
    expect(revealGroup("r1")).toBe(false);
  });
});
