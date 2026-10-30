/** The Sky (the main area's sky view, docs/flights.md "The Sky"): where lines
 * go (lib/skyLayout placeLines — rows, newest first, no overlaps, a dragged
 * line stays put), what is remembered per device (`mf_sky_layout`, read from
 * the old `mf_sky_pos` once), the camera (zoom about a point, fit, clamped
 * 40%–300%), which sessions get a line in each view, where the bird sits
 * (lib/flightDiagram birdPerch), the address (`#sky/<title>`), and the wiring
 * the source text pins: one transformed plane, no library, the wheel listener
 * not passive, the intake lane and the new-session card mounted with their
 * contracts. The view switch itself is skyView.test.ts. */

import { describe, expect, it } from "vitest";
import type { Instance } from "../api/types";
import { code, css } from "./sourceText";
import {
  FIT_PAD,
  INTAKE_W,
  LINES_LEFT,
  SKY_GAP,
  SKY_LAYOUT_KEY,
  ZOOM_MAX,
  ZOOM_MIN,
  boundsOf,
  centerOn,
  clampZoom,
  fitCamera,
  intakeFrame,
  linePerched,
  objectFrame,
  paletteFrame,
  PALETTE_MAX,
  PALETTE_MIN,
  PLACE_GAP,
  spotNear,
  loadSkyLayout,
  patchSkyLine,
  placeLines,
  readSkyLayout,
  readSkyPos,
  skyLineTitles,
  toPlane,
  zoomAt,
  type LineBox,
  type Pos,
} from "../lib/skyLayout";
import { birdPerch, sessionDiagram, type Diagram, type DiagramNode } from "../lib/flightDiagram";
import { legacyFlightsRoute } from "../lib/flights";
import { parseFlightsHash, parseViewHash, viewHash } from "../lib/surfaceHash";

const overlap = (a: Pos & { w: number; h: number }, b: Pos & { w: number; h: number }) =>
  a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h;

describe("placing the lines", () => {
  const L = (id: string, w = 400, h = 160): LineBox => ({ id, w, h });

  it("flows them into rows in the order given (newest first), wrapping at the width", () => {
    const pos = placeLines([L("c"), L("b"), L("a")], {}, { width: 1000, left: 100, top: 20 });
    expect(pos.c).toEqual({ x: 100, y: 20 });
    expect(pos.b).toEqual({ x: 100 + 400 + SKY_GAP, y: 20 });
    // 100 + 2 × (400 + gap) + 400 > 1000: the third starts the next row.
    expect(pos.a).toEqual({ x: 100, y: 20 + 160 + SKY_GAP });
  });

  it("a row is as tall as its tallest line", () => {
    const pos = placeLines([L("tall", 300, 400), L("short", 300, 100), L("next", 600, 100)], {}, { width: 900, left: 0, top: 0 });
    expect(pos.next.y).toBe(400 + SKY_GAP);
  });

  it("keeps a dragged (saved) line where it was put and flows the rest around it, never on top of it", () => {
    const lines = [L("a"), L("b"), L("c"), L("d")];
    const saved = { b: { x: 0, y: 0 } };
    const pos = placeLines(lines, saved, { width: 1400, left: 0, top: 0 });
    expect(pos.b).toEqual({ x: 0, y: 0 });
    const rects = lines.map((l) => ({ ...pos[l.id], w: l.w, h: l.h }));
    for (let i = 0; i < rects.length; i++) for (let j = i + 1; j < rects.length; j++) expect(overlap(rects[i], rects[j]), lines[i].id + "/" + lines[j].id).toBe(false);
  });

  it("never overlaps, whatever the sizes and saved spots", () => {
    const lines = Array.from({ length: 24 }, (_, i) => L("s" + i, 280 + ((i * 97) % 500), 120 + ((i * 53) % 260)));
    const saved = { s3: { x: 500, y: 40 }, s9: { x: 1500, y: 300 }, s14: { x: 420, y: 700 } };
    const pos = placeLines(lines, saved, { width: 1800, left: LINES_LEFT, top: 40 });
    const rects = lines.map((l) => ({ ...pos[l.id], w: l.w, h: l.h }));
    expect(rects.every((r) => Number.isFinite(r.x) && Number.isFinite(r.y))).toBe(true);
    for (let i = 0; i < rects.length; i++) for (let j = i + 1; j < rects.length; j++) expect(overlap(rects[i], rects[j])).toBe(false);
  });

  it("a line wider than the sky still gets a row of its own", () => {
    const pos = placeLines([L("wide", 3000), L("next")], {}, { width: 1000, left: 0, top: 0 });
    expect(pos.wide).toEqual({ x: 0, y: 0 });
    expect(pos.next).toEqual({ x: 0, y: 160 + SKY_GAP });
  });

  it("auto-placed lines start right of the intake lane", () => {
    expect(LINES_LEFT).toBeGreaterThan(INTAKE_W);
  });
});

describe("what a device remembers (mf_sky_layout)", () => {
  const mem = () => {
    const m = new Map<string, string>();
    return { getItem: (k: string) => m.get(k) ?? null, setItem: (k: string, v: string) => void m.set(k, v), m };
  };

  it("is { title: {x, y, pinned, size, open, …} } under one key, rounded, defaults left out", () => {
    const s = mem();
    const next = patchSkyLine({}, "fix-login", { x: 10.4, y: 20.6, pinned: true, size: "normal", open: false }, s);
    expect(next).toEqual({ "fix-login": { x: 10, y: 21, pinned: true } });
    expect(JSON.parse(s.m.get(SKY_LAYOUT_KEY)!)).toEqual({ "fix-login": { x: 10, y: 21, pinned: true } });
    expect(loadSkyLayout(s)).toEqual({ "fix-login": { x: 10, y: 21, pinned: true } });
  });

  it("reads back only finite pairs and known sizes; junk is nothing", () => {
    expect(readSkyPos('{"a":{"x":1,"y":2},"b":{"x":"1"},"c":null,"d":{"x":1,"y":null}}')).toEqual({ a: { x: 1, y: 2 } });
    expect(readSkyLayout('{"a":{"x":1,"y":2,"pinned":true},"b":{"size":"huge"},"c":null,"d":{"size":"wide","paneW":-3}}')).toEqual({
      a: { x: 1, y: 2, pinned: true },
      d: { size: "wide" },
    });
    expect(readSkyLayout("not json")).toEqual({});
    expect(readSkyLayout("[1,2]")).toEqual({});
    expect(readSkyLayout(null)).toEqual({});
  });

  it("never throws: a blocked store just doesn't remember", () => {
    const broken = {
      getItem: () => {
        throw new Error("blocked");
      },
      setItem: () => {
        throw new Error("blocked");
      },
    };
    expect(loadSkyLayout(broken)).toEqual({});
    expect(patchSkyLine({}, "x", { x: 1, y: 1, pinned: true }, broken)).toEqual({ x: { x: 1, y: 1, pinned: true } });
    expect(loadSkyLayout(null)).toEqual({});
  });
});

describe("the camera", () => {
  it("zooms between 10% and 300% — the owner wants to see the whole board", () => {
    expect(clampZoom(0.1)).toBe(ZOOM_MIN);
    expect(clampZoom(9)).toBe(ZOOM_MAX);
    expect(clampZoom(Number.NaN)).toBe(1);
    expect([ZOOM_MIN, ZOOM_MAX]).toEqual([0.1, 3]);
  });

  it("zooms about the pointer: the plane point under it stays under it", () => {
    const c = { x: 30, y: -20, z: 1 };
    const before = toPlane(c, 400, 300);
    const after = zoomAt(c, 1.6, 400, 300);
    const p = toPlane(after, 400, 300);
    expect(after.z).toBeCloseTo(1.6);
    expect(p.x).toBeCloseTo(before.x);
    expect(p.y).toBeCloseTo(before.y);
  });

  it("fits everything, centred, no closer than its ceiling", () => {
    const b = boundsOf([
      { x: 100, y: 100, w: 400, h: 200 },
      { x: 600, y: 400, w: 400, h: 200 },
    ])!;
    expect(b).toEqual({ x: 100, y: 100, w: 900, h: 500 });
    const c = fitCamera(b, 1000, 700, 1);
    expect(c.z).toBeCloseTo(Math.min(1, (1000 - 2 * FIT_PAD) / 900, (700 - 2 * FIT_PAD) / 500));
    // Centred horizontally.
    expect(c.x + (b.x + b.w / 2) * c.z).toBeCloseTo(500);
    // A lone small line doesn't balloon past the ceiling.
    expect(fitCamera({ x: 0, y: 0, w: 100, h: 50 }, 1000, 700, 1.5).z).toBe(1.5);
    expect(fitCamera(null, 1000, 700)).toEqual({ x: 0, y: 0, z: 1 });
  });

  it("centres one line to fly to it", () => {
    const c = centerOn({ x: 500, y: 500, w: 200, h: 100 }, 1000, 600, 1);
    expect(c.x + 600 * c.z).toBeCloseTo(500);
  });
});

const row = (title: string, over: Partial<Instance> & Record<string, unknown> = {}) =>
  ({ title, status: "running", activity: "idle", stage: "agent", task_headline: "Do " + title, parent: "", flight: null, autopilot: null, ...over }) as unknown as Instance;

describe("which sessions get a line", () => {
  const rows = [
    row("old"),
    row("lead", { task_headline: "Lead the login work" }),
    row("worker", { parent: "lead" }),
    row("orphan", { parent: "gone" }),
    row("perched", { flight: { id: "f", plan_name: "p", cursor: 1, total: 3, state: "waiting_you", step: { id: "s", kind: "you", title: "Approve", state: "waiting_you", actor: "you" } } }),
    row("halted", { flight: { id: "g", plan_name: "p", cursor: 0, total: 3, state: "halted", step: { id: "s", kind: "ship", title: "Push", state: "running", actor: "robot" } } }),
    row("verify-run", { hidden_by: "verify" }),
  ];

  it("All: every session newest first, a worker inside its lead's line (round 12); never a hidden helper", () => {
    expect(skyLineTitles(rows, "all")).toEqual(["halted", "perched", "orphan", "lead", "old"]);
  });

  it("Mine: the same — a worker folds into its parent's line; an orphan stands alone", () => {
    expect(skyLineTitles(rows, "mine")).toEqual(["halted", "perched", "orphan", "lead", "old"]);
  });

  it("a worker is a line only when its parent is not one: parent hidden, filtered out, or itself folded", () => {
    const fam = [
      row("lead"),
      row("w1", { parent: "lead" }),
      row("w2", { parent: "lead" }),
      row("w1a", { parent: "w1" }),
      row("helper", { hidden_by: "verify" }),
      row("hw", { parent: "helper" }),
      row("selfish", { parent: "selfish" }),
    ];
    // Both workers sit in lead's line; w1's own worker keeps a line (its
    // parent w1 is folded); a hidden helper's worker stands alone.
    expect(skyLineTitles(fam, "all")).toEqual(["selfish", "hw", "w1a", "lead"]);
    // Find: a worker matched without its lead is a line, its lead's box above.
    expect(skyLineTitles(fam, "all", { query: "w2" })).toEqual(["w2"]);
    // Perched on you: a perched worker of a perched lead folds; of a calm lead
    // it is a line.
    const perch = { flight: { id: "f", plan_name: "p", cursor: 1, total: 3, state: "waiting_you", step: { id: "s", kind: "you", title: "Approve", state: "waiting_you", actor: "you" } } } as Record<string, unknown>;
    expect(skyLineTitles([row("lead", perch), row("w", { parent: "lead", ...perch })], "perched")).toEqual(["lead"]);
    expect(skyLineTitles([row("lead"), row("w", { parent: "lead", ...perch })], "perched")).toEqual(["w"]);
    // A cycle of parents never loses them all.
    expect(skyLineTitles([row("a", { parent: "b" }), row("b", { parent: "a" })], "all").length).toBeGreaterThan(0);
  });

  it("Perched on you: lines whose flight is perched on you or halted", () => {
    expect(skyLineTitles(rows, "perched")).toEqual(["halted", "perched"]);
    expect(linePerched(rows[0])).toBe(false);
  });

  it("This session: one line, or none when it is gone or hidden", () => {
    expect(skyLineTitles(rows, "one", { focus: "worker" })).toEqual(["worker"]);
    expect(skyLineTitles(rows, "one", { focus: "nope" })).toEqual([]);
    expect(skyLineTitles(rows, "one", { focus: "verify-run" })).toEqual([]);
  });

  it("find filters by name or task", () => {
    expect(skyLineTitles(rows, "all", { query: "LOGIN" })).toEqual(["lead"]);
    expect(skyLineTitles(rows, "mine", { query: "orph" })).toEqual(["orphan"]);
  });
});

describe("the bird", () => {
  const node = (id: string, kind: DiagramNode["kind"], state: DiagramNode["state"], extra: Partial<DiagramNode> = {}): DiagramNode => ({
    id,
    kind,
    row: "main",
    title: id,
    sub: "",
    state,
    icon: kind,
    ...extra,
  });
  const d = (...nodes: DiagramNode[]): Diagram => ({ nodes, edges: [], flightId: "", mode: "none", insertFrom: 0 });
  const me = (tone: string) => node("session:me", "session", "session", { chip: { label: "x", tone } });

  it("sits on the flight's current stop, flying", () => {
    expect(birdPerch(d(me("working"), node("stop:a", "stop", "done"), node("stop:b", "stop", "current"), node("stop:c", "stop", "pending")))).toEqual({
      nodeId: "stop:b",
      tone: "working",
    });
  });

  it("gold on a perch waiting on you, red on a halted stop, still on a paused one", () => {
    expect(birdPerch(d(me("idle"), node("stop:a", "stop", "perched")))).toEqual({ nodeId: "stop:a", tone: "you" });
    expect(birdPerch(d(me("idle"), node("stop:a", "stop", "done"), node("stop:b", "stop", "halted")))).toEqual({ nodeId: "stop:b", tone: "halted" });
    expect(birdPerch(d(me("idle"), node("stop:a", "stop", "paused")))).toEqual({ nodeId: "stop:a", tone: "paused" });
  });

  it("no flight: the fast-track's current rung (red when halted)", () => {
    expect(birdPerch(d(me("idle"), node("rung:commit", "rung", "done"), node("rung:push", "rung", "current")))).toEqual({ nodeId: "rung:push", tone: "working" });
    expect(birdPerch(d(me("idle"), node("rung:commit", "rung", "halted")))).toEqual({ nodeId: "rung:commit", tone: "halted" });
  });

  it("nothing armed: the session box while the agent works or asks you, nowhere when it idles", () => {
    expect(birdPerch(d(me("working"), node("add", "add", "pending")))).toEqual({ nodeId: "session:me", tone: "working" });
    expect(birdPerch(d(me("attention")))).toEqual({ nodeId: "session:me", tone: "you" });
    expect(birdPerch(d(me("neutral"), node("add", "add", "pending")))).toBeNull();
    expect(birdPerch(d())).toBeNull();
  });

  it("never on a stop that failed and was flown past", () => {
    expect(birdPerch(d(me("neutral"), node("stop:a", "stop", "failed"), node("stop:b", "stop", "done")))).toBeNull();
  });

  it("reads a real session diagram: a row's flight perched on you", () => {
    const rows = [
      row("me", { flight: { id: "f", plan_name: "p", cursor: 1, total: 3, state: "waiting_you", step: { id: "s2", kind: "you", title: "Approve", state: "waiting_you", actor: "you" } } }),
    ];
    expect(birdPerch(sessionDiagram(rows, "me"))).toEqual({ nodeId: "stop:i1", tone: "you" });
  });
});

describe("the address", () => {
  it("#sky is the sky; #sky/<title> one session's; the old Flights → Sky and Board links still land there", () => {
    expect(viewHash({ view: "sky", skyMode: "mine", focused: "fix login/x" })).toBe("#sky");
    const h = viewHash({ view: "sky", skyMode: "one", focused: "fix login/x" });
    expect(h).toBe("#sky/fix%20login%2Fx");
    expect(parseViewHash(h)).toEqual({ view: "sky", focus: "fix login/x" });
    expect(parseViewHash("#sky")).toEqual({ view: "sky", focus: "" });
    expect(viewHash({ view: "grid", skyMode: "one", focused: "x" })).toBe("");
    const old = parseFlightsHash("#flights/sky/fix%20login%2Fx");
    expect(legacyFlightsRoute(old)).toEqual({ to: "sky", focus: "fix login/x" });
    expect(legacyFlightsRoute(parseFlightsHash("#flights/board"))).toEqual({ to: "sky", focus: "" });
  });
});

describe("the wiring", () => {
  const sky = code("components/flights/SkyView.tsx");
  const sheet = css("components/flights/Sky.css");

  it("is one transformed plane, no library: translate + scale on one container, the grid scaled with it", () => {
    expect(sky).toMatch(/className="sky-plane"[^>]*\n?[^>]*style=\{\{ transform: `translate\(\$\{cam\.x\}px, \$\{cam\.y\}px\) scale\(\$\{cam\.z\}\)` \}\}/);
    expect(sky).toContain("backgroundSize: `${GRID * cam.z}px ${GRID * cam.z}px`");
    expect(sheet).toMatch(/\.sky-plane \{[^}]*transform-origin: 0 0;/);
    expect(sheet).toMatch(/\.sky-viewport \{[^}]*touch-action: none;[^}]*radial-gradient/);
    // React and the app's own modules — no pan/zoom or graph library.
    for (const m of sky.matchAll(/from "([^"]+)"/g)) expect(m[1] === "react" || m[1].startsWith("."), m[1]).toBe(true);
  });

  it("pans on empty sky, zooms with Ctrl/⌘+wheel and a pinch, Space+drag pans anywhere, 0 fits", () => {
    expect(sky).toContain('el.addEventListener("wheel", onWheel, { passive: false });');
    expect(sky).toContain("if (e.ctrlKey || e.metaKey) {");
    expect(sky).toContain("pinch.current = { d: Math.hypot(a.x - b.x, a.y - b.y) || 1, z: camRef.current.z };");
    expect(sky).toContain("onPointerDownCapture={onDownCapture}");
    expect(sky).toContain('} else if (k === "0") {');
    expect(sky).toContain("onDoubleClick={onDouble}");
  });

  it("a line drags by its bar (SkyLine's onDragStart) and its spot is remembered, pinned; a closed one fades out", () => {
    expect(sky).toContain("onLayout(id, { x: p.x, y: p.y, pinned: true });");
    expect(sky).toContain("onDragStart={at ? (e) => onDragStart(id, e) : undefined}");
    expect(sky).toContain('className="sky-line-bar"');
    expect(sky).toContain('"sky-line sky-line-leaving"');
    expect(sheet).toMatch(/\.sky-line-leaving \{[^}]*animation: sky-out/);
  });

  it("mounts the intake lane on the plane and the new-session card where it was asked for", () => {
    // A sky object at its spot (round 12): the layout key "intake".
    expect(sky).toMatch(/<IntakeLane\s+rows=\{rows\}\s+plans=\{plans\}\s+onFly=\{flashLine\}\s+width=\{intakeW\}/);
    expect(sky).toContain("transform: `translate(${intakeAt.x}px, ${intakeAt.y}px)`");
    expect(sky).toMatch(/<NewSessionBlock\s+x=\{newAt\.x\}\s+y=\{newAt\.y\}\s+plans=\{plans\}/);
    expect(sky).toContain("setLayout((l) => patchSkyLine(l, title, { x: newAt.x, y: newAt.y, pinned: true }));");
  });

  it("the bird hops (a transition on left / top) and bobs only while the flock works", () => {
    const canvas = css("components/flights/FlywayCanvas.css");
    expect(canvas).toMatch(/\.fl-bird-marker \{[^}]*transition: left [^;]*top /);
    expect(canvas).toMatch(/\.fl-bird-marker\.tone-working \.fl-bird-big \{[^}]*animation: fl-bird-bob/);
    expect(canvas).toMatch(/\.fl-bird-marker\.tone-you \{[^}]*var\(--tone-attention\)/);
    expect(canvas).toMatch(/\.fl-bird-marker\.tone-halted \{[^}]*var\(--red\)/);
  });

  it("the main area, in the grid's place; a phone gets the list", () => {
    expect(sheet).toMatch(/#sky-view \{[^}]*min-height: 0;/);
    expect(sheet).not.toContain("sky-full");
    expect(sky).toContain('if (narrow)\n    return (\n      <main id="sky-view" className="sky sky-narrow" data-tour="flights-sky">');
    expect(sheet).toContain(".light .sky {");
  });

  it("the pane's Flight tab opens it on its session", () => {
    expect(code("components/flights/SessionFlight.tsx")).toContain("onClick={() => showInSky(title)}");
  });
});

describe("fitCamera at the zoom floor", () => {
  it("starts wide content at its left edge instead of cutting both ends", () => {
    // 60000px of plane in a 1000px window: the floor (0.1) still leaves 6000px.
    const cam = fitCamera({ x: 40, y: 40, w: 60000, h: 300 }, 1000, 800);
    expect(cam.z).toBe(0.1);
    expect(cam.x).toBe(FIT_PAD - 40 * 0.1); // pad - b.x * z: the lane at x=40 stays visible
  });
  it("still centres content that fits", () => {
    const cam = fitCamera({ x: 0, y: 0, w: 500, h: 300 }, 1000, 800);
    expect(cam.x).toBeGreaterThan(0);
  });
});

describe("round 13: where a placed block lands, a sky object's frame", () => {
  const view = { x: 0, y: 0, w: 1200, h: 800 };
  const size = { w: 300, h: 120 };
  const clear = (a: { x: number; y: number }, taken: { x: number; y: number; w: number; h: number }[]) =>
    taken.every((t) => a.x + size.w + PLACE_GAP - 1 <= t.x || t.x + t.w + PLACE_GAP - 1 <= a.x || a.y + size.h + PLACE_GAP - 1 <= t.y || t.y + t.h + PLACE_GAP - 1 <= a.y);

  it("an empty board: exactly where it wants to be", () => {
    expect(spotNear({ x: 450, y: 660 }, [], size, view)).toEqual({ x: 450, y: 660 });
  });

  it("the spot taken: the nearest free one, clear of everything, inside the view", () => {
    const taken = [{ x: 400, y: 600, w: 400, h: 200 }];
    const at = spotNear({ x: 450, y: 660 }, taken, size, view);
    expect(clear(at, taken)).toBe(true);
    expect(at.x >= 0 && at.y >= 0 && at.x + size.w <= 1200 && at.y + size.h <= 800).toBe(true);
    // Above or beside it, not far off.
    expect(Math.hypot(at.x - 450, at.y - 660)).toBeLessThan(600);
  });

  it("a second block placed next to the first never stacks on it", () => {
    const first = spotNear({ x: 450, y: 660 }, [], size, view);
    const second = spotNear({ x: 450, y: 660 }, [{ ...first, ...size }], size, view);
    expect(second).not.toEqual(first);
    expect(clear(second, [{ ...first, ...size }])).toBe(true);
  });

  it("no room in the view: the nearest free spot anywhere", () => {
    const full = [{ x: -100, y: -100, w: 1500, h: 1100 }];
    const at = spotNear({ x: 450, y: 660 }, full, size, view);
    expect(clear(at, full)).toBe(true);
  });

  it("objectFrame clamps a dragged corner; none dragged = it sizes itself; intakeFrame is its wrapper", () => {
    expect(objectFrame(undefined, { w: 10, h: 10 }, { w: 50, h: 50 })).toBeNull();
    expect(objectFrame({ x: 1 }, { w: 10, h: 10 }, { w: 50, h: 50 })).toBeNull();
    expect(objectFrame({ w: 5, h: 99 }, { w: 10, h: 10 }, { w: 50, h: 50 })).toEqual({ w: 10, h: 50 });
    expect(intakeFrame({ w: 50, h: 5000 })).toEqual({ w: 200, h: 1600 });
    expect(paletteFrame({ w: 1, h: 1 })).toEqual(PALETTE_MIN);
    expect(paletteFrame({ w: 99999, h: 99999 })).toEqual(PALETTE_MAX);
  });
});
