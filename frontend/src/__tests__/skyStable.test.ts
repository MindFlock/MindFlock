/** The still sky (docs/flights-stable.md): nothing moves unless the owner
 * moves it. A line gets a spot once (lib/skyLayout assignSpots) and keeps it
 * however it grows; a newcomer lands clear of every placed line; a leaver
 * frees its spot; Tidy forgets them all and is the only glide; the camera
 * fits once (no `shape` in the auto-fit's dependencies); the shelf's Session
 * chip drags onto the sky and the new-session block moves by its bar. */

import { describe, expect, it } from "vitest";
import { code, css } from "./sourceText";
import { LINES_LEFT, NEW_SESSION_MIME, SKY_GAP, SKY_MARGIN, assignSpots, closeSkyWindows, tidySkyLayout, type LineBox, type PlacedLine, type Pos } from "../lib/skyLayout";

const AREA = { width: 2000, left: LINES_LEFT, top: SKY_MARGIN };
const overlap = (a: PlacedLine, b: PlacedLine) => a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h;
/** What SkyView keeps between renders: the spots given so far. */
const placed = (lines: readonly LineBox[], spots: Readonly<Record<string, Pos>>): PlacedLine[] =>
  lines.filter((l) => spots[l.id]).map((l) => ({ id: l.id, ...spots[l.id], w: l.w, h: l.h }));

describe("assignSpots", () => {
  const first: LineBox[] = [
    { id: "c", w: 400, h: 200 },
    { id: "b", w: 400, h: 200 },
    { id: "a", w: 400, h: 200 },
  ];

  it("places a first batch the way placeLines always did: one row, newest first", () => {
    const spots = assignSpots([], first, AREA);
    expect(spots.c).toEqual({ x: LINES_LEFT, y: SKY_MARGIN });
    expect(spots.b.x).toBe(LINES_LEFT + 400 + SKY_GAP);
    expect(spots.a.x).toBe(LINES_LEFT + 2 * (400 + SKY_GAP));
  });

  it("an existing line keeps its spot when its size changes, and so do its neighbours", () => {
    const spots = assignSpots([], first, AREA);
    // b grows (a perch opens wide, its box opens into a pane): nothing moves.
    const grown = first.map((l) => (l.id === "b" ? { ...l, w: 1100, h: 700 } : l));
    expect(assignSpots(placed(grown, spots), grown, AREA)).toEqual({});
    // ...and it shrinks: still nothing.
    const shrunk = first.map((l) => (l.id === "b" ? { ...l, w: 120, h: 60 } : l));
    expect(assignSpots(placed(shrunk, spots), shrunk, AREA)).toEqual({});
  });

  it("a newcomer gets a free spot that overlaps no existing rectangle, at their sizes now", () => {
    const spots = assignSpots([], first, AREA);
    // b has grown since it was placed; the newcomer must clear its new size.
    const now = first.map((l) => (l.id === "b" ? { ...l, w: 800, h: 900 } : l));
    const existing = placed(now, spots);
    const fresh = assignSpots(existing, [{ id: "new", w: 500, h: 300 }], AREA);
    expect(Object.keys(fresh)).toEqual(["new"]);
    const mine = { id: "new", ...fresh.new, w: 500, h: 300 };
    for (const e of existing) expect(overlap(mine, e)).toBe(false);
    // Only the newcomer is answered for: the placed lines aren't in the result.
    expect(fresh.b).toBeUndefined();
  });

  it("several newcomers clear each other too", () => {
    const existing = placed(first, assignSpots([], first, AREA));
    const more: LineBox[] = [
      { id: "n1", w: 600, h: 250 },
      { id: "n2", w: 300, h: 400 },
      { id: "n3", w: 900, h: 150 },
    ];
    const fresh = assignSpots(existing, more, AREA);
    const all = [...existing, ...more.map((l) => ({ ...l, ...fresh[l.id] }))];
    for (let i = 0; i < all.length; i++) for (let j = i + 1; j < all.length; j++) expect(overlap(all[i], all[j])).toBe(false);
  });

  it("a leaver frees its spot: the next newcomer of its size takes it", () => {
    const spots = assignSpots([], first, AREA);
    const left = first.filter((l) => l.id !== "c");
    const fresh = assignSpots(placed(left, spots), [{ id: "d", w: 400, h: 200 }], AREA);
    expect(fresh.d).toEqual(spots.c);
  });

  it("a newcomer with a spot already given (or a pin) isn't placed again", () => {
    const fresh = assignSpots([{ id: "a", x: 900, y: 500, w: 400, h: 200 }], [{ id: "a", w: 400, h: 200 }], AREA);
    expect(fresh).toEqual({});
  });

  it("Tidy clears the auto spots and the pins: every line is placed again, in rows", () => {
    // Before: lines strewn about — an old auto spot, a pin.
    const strewn: PlacedLine[] = [
      { id: "c", x: 2000, y: 900, w: 400, h: 200 },
      { id: "b", x: 50, y: 1400, w: 400, h: 200 },
    ];
    // Tidy: the spots are forgotten, so nothing is "existing" any more.
    const again = assignSpots([], strewn, AREA);
    expect(again.c).toEqual({ x: LINES_LEFT, y: SKY_MARGIN });
    expect(again.b).toEqual({ x: LINES_LEFT + 400 + SKY_GAP, y: SKY_MARGIN });
    const store = { getItem: () => null, setItem: () => {} };
    expect(tidySkyLayout({ b: { x: 50, y: 1400, pinned: true, size: "wide" } }, undefined, store)).toEqual({ b: { size: "wide" } });
    const sky = code("components/flights/SkyView.tsx");
    expect(sky).toMatch(/const tidy = \(\) => \{\s*setAutoPos\(\{\}\);\s*setLayout\(\(l\) => closeSkyWindows\(tidySkyLayout\(l\)\)\);/);
  });
});

describe("the sky view stays still", () => {
  const sky = code("components/flights/SkyView.tsx");

  it("placement reads the given spots and the pins as saved; a newcomer's spot is kept once it is measured", () => {
    expect(sky).toContain("const saved: Record<string, Pos> = { ...autoPos, ...pinnedPositions(layout) };");
    // The intake lane is the first item placed (round 12), then the lines.
    expect(sky).toContain("const items = [{ id: INTAKE_KEY, ...intakeBox }, ...(paletteOn ? [{ id: PALETTE_KEY, ...paletteBox }] : []), ...lines];");
    expect(sky).toContain("const newcomers = items.filter((l) => !saved[l.id]);");
    expect(sky).toContain("assignSpots(existing, newcomers, area);");
    expect(sky).toMatch(/if \(flightsSettled && intakeReady\) for \(const \[id, at\] of Object\.entries\(fresh\)\) if \(measured\[id\] \|\| id === INTAKE_KEY \|\| id === PALETTE_KEY\) edit\(\)\[id\] = at;/);
    // A session that's gone (or grounded stops that took off or were
    // removed), or a line the owner pinned, frees its given spot.
    expect(sky).toContain("if (layout[id]?.pinned || !lineAlive(id)) delete edit()[id];");
    expect(sky).toContain("id === INTAKE_KEY ? intakeOn : id === PALETTE_KEY ? paletteOn : isGroundedKey(id) ? groundedIds.has(groundedIdOf(id)) : rows.some((r) => r.title === id && inSky(r))");
    expect(sky).not.toContain("placeLines(");
  });

  it("the camera fits once: the auto-fit's dependencies carry no shape", () => {
    const fit = sky.match(/if \(fitted\.current === fitKey\) return;[\s\S]*?\}, \[([^\]]*)\]\);/);
    expect(fit).not.toBeNull();
    const deps = fit![1].split(",").map((d) => d.trim());
    expect(deps).toContain("fitKey");
    expect(deps).not.toContain("shape");
    expect(deps.some((d) => /measured|lines|place/.test(d))).toBe(false);
    expect(sky).not.toMatch(/const shape = /);
    // No "until the owner moves the camera" refit any more.
    expect(sky).not.toContain("touched.current");
  });

  it("the slots glide only under [data-tidying], which only Tidy sets", () => {
    const sheet = css("components/flights/Sky.css");
    const slot = sheet.match(/\n\.sky-line-slot \{([^}]*)\}/);
    expect(slot).not.toBeNull();
    expect(slot![1]).not.toContain("transition");
    expect(sheet).toMatch(/\.sky-plane\[data-tidying\] \.sky-line-slot,\s*\.sky-plane\[data-tidying\] \.sky-intake,\s*\.sky-plane\[data-tidying\] \.sky-palette-slot \{\s*transition: transform 0\.28s ease;/);
    // No other transform transition on a line or a slot.
    for (const m of sheet.matchAll(/([^{}]*)\{([^}]*)\}/g)) {
      if (!/sky-line|\.skl\b/.test(m[1]) || /data-tidying/.test(m[1])) continue;
      expect(m[2], m[1].trim()).not.toMatch(/transition:[^;]*transform/);
    }
    expect(css("components/flights/SkyLine.css")).not.toMatch(/\.skl \{[^}]*transition:[^;]*transform/);
    expect(sky).toContain('data-tidying={tidying ? "" : undefined}');
    expect(sky).toMatch(/setTidying\(true\);[\s\S]{0,120}setTimeout\(\(\) => alive\.current && setTidying\(false\), TIDY_MS\)/);
    expect(sky).toContain("const TIDY_MS = 350;");
    // Reduced motion: no glide even then.
    expect(sheet).toMatch(/@media \(prefers-reduced-motion: reduce\) \{\s*\.sky-plane\[data-tidying\] \.sky-line-slot,\s*\.sky-plane\[data-tidying\] \.sky-intake,\s*\.sky-plane\[data-tidying\] \.sky-palette-slot \{\s*transition: none;/);
  });
});

describe("the Session chip drags onto the sky", () => {
  const sky = code("components/flights/SkyView.tsx");

  it("the toolbar's + New session is gone; the shelf's Session chip carries the new-session type, as a copy", () => {
    expect(NEW_SESSION_MIME).toBe("application/x-mindflock-new-session");
    expect(sky).not.toContain("+ New session");
    expect(sky).not.toContain('className="btn-primary fl-btn sky-new"');
    // Round 13: a chip click places (onPlace), never tacks onto the focused line.
    expect(sky).toMatch(/<StopShelf\s+onPlace=\{\(d\) => void placeGrounded\(d, "shelf"\)\}[\s\S]*?onSession=\{newHere\}/);
    expect(sky).not.toContain("onTack={onTack}");
    const shelf = code("components/flights/StopShelf.tsx");
    expect(shelf).toContain('[NEW_SESSION_MIME, "session"],');
    expect(shelf).toContain('e.dataTransfer.effectAllowed = "copy";');
    // Double-click and Ctrl/Alt+N (the view switch's newHere) still open it.
    expect(sky).toMatch(/const onDouble = [\s\S]*?newAtScreen\(e\.clientX, e\.clientY\);/);
    expect(sky).toContain("hostRef.current.newHere = newHere;");
  });

  it("the viewport takes the drop on empty sky and opens the block there, like a double-click", () => {
    expect(sky).toContain("onDragOver={onNewDragOver}");
    expect(sky).toContain("onDrop={onNewDrop}");
    expect(sky).toMatch(/const onNewDrop = \(e: ReactDragEvent<HTMLDivElement>\) => \{[\s\S]*?if \(t \|\| !ground\(e\.target\)\) return;[\s\S]*?newAtScreen\(e\.clientX, e\.clientY\);/);
    // On a grounded line: a block that makes a session for those stops.
    expect(sky).toMatch(/const onNewDrop = [\s\S]*?if \(isGroundedKey\(t\)\) \{[\s\S]*?newForGrounded\(t\);/);
    expect(sky).toMatch(/const onDouble = [\s\S]*?newAtScreen\(e\.clientX, e\.clientY\);/);
    expect(sky).toMatch(/const newAtScreen = [\s\S]*?toPlane\(camRef\.current, clientX - r\.left, clientY - r\.top\)/);
    expect(sky).toMatch(/const onNewDragOver = [\s\S]*?e\.preventDefault\(\);/);
    // A soft highlight while it is over the sky.
    expect(sky).toContain('(newOver ? " is-new-over" : "")');
    expect(css("components/flights/Sky.css")).toMatch(/\.sky-viewport\.is-new-over \{[^}]*background-color:/);
  });

  it("the block moves by its bar, and the session it makes is pinned where it sat", () => {
    expect(sky).toMatch(/onMove=\{narrow \? undefined : \(at\) => setNewAt\(\{ \.\.\.newAt, \.\.\.at \}\)\}/);
    expect(sky).toContain("setLayout((l) => patchSkyLine(l, title, { x: newAt.x, y: newAt.y, pinned: true }));");
    const block = code("components/flights/NewSessionBlock.tsx");
    expect(block).toMatch(/className=\{"sky-new-head" \+ \(onMove \? " is-movable" : ""\)\}[\s\S]*?onPointerDown=\{onBarDown\}/);
    expect(block).toContain("<Bird size={13} />");
    expect(block).toMatch(/onMove\(\{ x: Math\.round\(g\.x \+ \(ev\.clientX - g\.sx\) \/ g\.z\), y: Math\.round\(g\.y \+ \(ev\.clientY - g\.sy\) \/ g\.z\) \}\)/);
    expect(block).not.toMatch(/\bwindow\.(prompt|alert|confirm)\(/);
    const sheet = css("components/flights/NewSessionBlock.css");
    expect(sheet).toMatch(/\.sky-new-head\.is-movable \{[^}]*cursor: move;/);
    expect(sheet).toMatch(/\.light \.sky-new\.fl-form \{/);
  });
});

/** The owner, 2026-11-02: "when we tidy we minimize all open windows in sky
 * mode". Tidy forgets the spots AND closes every session window back to its
 * box; the pane keeps its size for the next open, the intake drawer stays. */
describe("Tidy minimises the open windows", () => {
  const mem = () => {
    const m = new Map<string, string>();
    return { getItem: (k: string) => m.get(k) ?? null, setItem: (k: string, v: string) => void m.set(k, v), removeItem: (k: string) => void m.delete(k) };
  };
  it("closes every line's window, keeps its pane size, leaves the intake lane", () => {
    const all = { a: { open: true, paneW: 700 }, b: { open: true }, c: { size: "wide" as const }, intake: { open: true } };
    expect(closeSkyWindows(all, undefined, mem())).toEqual({ a: { paneW: 700 }, c: { size: "wide" }, intake: { open: true } });
  });
  it("closes only the named lines' windows", () => {
    const all = { a: { open: true }, b: { open: true } };
    expect(closeSkyWindows(all, ["b"], mem())).toEqual({ a: { open: true } });
  });
  it("hands back the same map when nothing was open", () => {
    const all = { a: { size: "wide" as const } };
    expect(closeSkyWindows(all, undefined, mem())).toBe(all);
  });
  it("every Tidy minimises: T, Tidy these, Tidy this line", () => {
    const sky = code("components/flights/SkyView.tsx");
    expect(sky).toContain("setLayout((l) => closeSkyWindows(tidySkyLayout(l)));");
    expect(sky).toContain("closeSkyWindows(ids.reduce((acc, id) => tidySkyLayout(acc, id), l), ids)");
    expect(sky).toContain("closeSkyWindows(tidySkyLayout(l, id), [id])");
  });
});
