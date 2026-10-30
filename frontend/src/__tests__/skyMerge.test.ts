/** Round 13 (docs/flights.md "As built: round 13 sky"): a grounded line
 * dragged by its bar onto a session's line merges into it. The pure decision
 * (lib/skySelect `mergeTargetAt`: which line is under the pointer — only a
 * session's) and the wiring the source text pins: the target lights
 * (`.is-merge-target`), the dragged line goes see-through (`.is-merging`),
 * the release attaches or tacks on as one group and deletes the grounded
 * flight, and the toast's Undo makes it again where it was. */

import { describe, expect, it } from "vitest";
import { code, css } from "./sourceText";
import { mergeTargetAt } from "../lib/skySelect";

const r = (x: number, y: number, w = 300, h = 120) => ({ x, y, w, h });

describe("mergeTargetAt", () => {
  const lines = [
    { id: "fix-login", rect: r(0, 0) },
    { id: "flight:f_2", rect: r(400, 0) },
    { id: "intake", rect: r(0, 200) },
    { id: "palette", rect: r(400, 200) },
    { id: "worker-2", rect: r(800, 0) },
  ];

  it("a session's line under the point takes it", () => {
    expect(mergeTargetAt({ x: 10, y: 10 }, lines)).toBe("fix-login");
    expect(mergeTargetAt({ x: 1100, y: 120 }, lines)).toBe("worker-2");
  });

  it("empty sky, a grounded line, the intake lane or the palette: none — the drop just moves it", () => {
    expect(mergeTargetAt({ x: 350, y: 10 }, lines)).toBe("");
    expect(mergeTargetAt({ x: 450, y: 50 }, lines)).toBe("");
    expect(mergeTargetAt({ x: 50, y: 250 }, lines)).toBe("");
    expect(mergeTargetAt({ x: 450, y: 250 }, lines)).toBe("");
  });

  it("never the line being dragged; the topmost (last drawn) of two that overlap", () => {
    expect(mergeTargetAt({ x: 10, y: 10 }, lines, "fix-login")).toBe("");
    const over = [{ id: "a", rect: r(0, 0) }, { id: "b", rect: r(100, 0) }];
    expect(mergeTargetAt({ x: 150, y: 10 }, over)).toBe("b");
    expect(mergeTargetAt({ x: 50, y: 10 }, over)).toBe("a");
  });

  it("the edges count (a pointer on the border is on the line)", () => {
    expect(mergeTargetAt({ x: 300, y: 120 }, [{ id: "a", rect: r(0, 0) }])).toBe("a");
    expect(mergeTargetAt({ x: 301, y: 120 }, [{ id: "a", rect: r(0, 0) }])).toBe("");
  });
});

describe("the sky wires it", () => {
  const sky = code("components/flights/SkyView.tsx");

  it("only a grounded line dragged alone looks for a target, as it moves; the others sit still", () => {
    expect(sky).toContain("const merging = isGroundedKey(id) && ids.length === 1;");
    expect(sky).toContain("mergeTargetAt(planeAt(ev.clientX, ev.clientY), others, id)");
    expect(sky).toMatch(/setMergeOver\(\(cur\) => \(cur === t \? cur : t\)\);/);
    // Released over one: it merges and is not pinned; elsewhere it just moves.
    expect(sky).toMatch(/if \(into\) \{\s*void mergeGrounded\(id, into, from\[id\]\);\s*return;\s*\}/);
  });

  it("no flight on the session: attach; a flight: tack on as one group named after the line, then the grounded flight goes", () => {
    expect(sky).toContain("if (!live) ok = !!(await attachGrounded(f.id, { title }));");
    expect(sky).toContain("else if (await tackOnSteps(live.id, steps, undefined, name || undefined)) ok = await deleteFlight(f.id);");
    expect(sky).toContain('const msg = "Moved " + n + (n === 1 ? " stop" : " stops") + " onto " + windowName(title);');
    // Undo: the same stops grounded again, pinned where the line was.
    expect(sky).toContain("void startGrounded({ steps, ...(name ? { name } : {}) }).then((g) => {");
    expect(sky).toContain("patchSkyLine(l, groundedKey(g.id), { x: was.x, y: was.y, pinned: true })");
  });

  it("the target lights and the dragged line goes see-through", () => {
    expect(sky).toContain("mergeTarget={mergeOver === id}");
    expect(sky).toContain("merging={!!mergeOver && !!drag?.from[id]}");
    expect(code("components/flights/SkyLine.tsx")).toContain('(p.mergeTarget ? " is-merge-target" : "")');
    expect(code("components/flights/GroundedLine.tsx")).toContain('(p.merging ? " is-merging" : "")');
    expect(css("components/flights/SkyLine.css")).toMatch(/\.skl\.is-merge-target \{[^}]*border-color: var\(--accent\)/);
    expect(css("components/flights/GroundedLine.css")).toMatch(/\.skl\.skl-grounded\.is-merging \{[^}]*opacity:/);
  });
});
