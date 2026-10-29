/** The Sky (Flights → Sky, docs/flights.md "The Sky"): where lines go
 * (lib/skyLayout placeLines — rows, newest first, no overlaps, a dragged line
 * stays put), what is remembered per device (`mf_sky_pos`), the camera
 * (zoom about a point, fit, clamped 40%–200%), which sessions get a line in
 * each view, where the bird sits (lib/flightDiagram birdPerch), the address
 * (`#flights/sky/<title>`), and the wiring the source text pins: one
 * transformed plane, no library, the wheel listener not passive, the intake
 * lane and the new-session card mounted with their contracts. */

import { describe, expect, it } from "vitest";
import type { Instance } from "../api/types";
import { code, css } from "./sourceText";
import {
  INTAKE_W,
  LINES_LEFT,
  SKY_GAP,
  SKY_POS_KEY,
  ZOOM_MAX,
  ZOOM_MIN,
  boundsOf,
  centerOn,
  clampZoom,
  fitCamera,
  lineFamily,
  linePerched,
  loadSkyPos,
  placeLines,
  readSkyPos,
  rememberSkyPos,
  skyLineTitles,
  skyTarget,
  toPlane,
  zoomAt,
  type LineBox,
  type Pos,
} from "../lib/skyLayout";
import { birdPerch, sessionDiagram, type Diagram, type DiagramNode } from "../lib/flightDiagram";
import { parseFlightsTarget } from "../lib/flights";
import { hashFor, parseHash } from "../lib/surfaceHash";

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

describe("what a device remembers (mf_sky_pos)", () => {
  const mem = () => {
    const m = new Map<string, string>();
    return { getItem: (k: string) => m.get(k) ?? null, setItem: (k: string, v: string) => void m.set(k, v), m };
  };

  it("is { title: {x, y} } under one key, rounded", () => {
    const s = mem();
    const next = rememberSkyPos({}, "fix-login", { x: 10.4, y: 20.6 }, s);
    expect(next).toEqual({ "fix-login": { x: 10, y: 21 } });
    expect(JSON.parse(s.m.get(SKY_POS_KEY)!)).toEqual({ "fix-login": { x: 10, y: 21 } });
    expect(loadSkyPos(s)).toEqual({ "fix-login": { x: 10, y: 21 } });
  });

  it("reads back only finite pairs; junk is nothing", () => {
    expect(readSkyPos('{"a":{"x":1,"y":2},"b":{"x":"1"},"c":null,"d":{"x":1,"y":null}}')).toEqual({ a: { x: 1, y: 2 } });
    expect(readSkyPos("not json")).toEqual({});
    expect(readSkyPos("[1,2]")).toEqual({});
    expect(readSkyPos(null)).toEqual({});
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
    expect(loadSkyPos(broken)).toEqual({});
    expect(rememberSkyPos({}, "x", { x: 1, y: 1 }, broken)).toEqual({ x: { x: 1, y: 1 } });
    expect(loadSkyPos(null)).toEqual({});
  });
});

describe("the camera", () => {
  it("zooms between 40% and 200%", () => {
    expect(clampZoom(0.1)).toBe(ZOOM_MIN);
    expect(clampZoom(9)).toBe(ZOOM_MAX);
    expect(clampZoom(Number.NaN)).toBe(1);
    expect([ZOOM_MIN, ZOOM_MAX]).toEqual([0.4, 2]);
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
    expect(c.z).toBeCloseTo(Math.min(1, (1000 - 80) / 900, (700 - 80) / 500));
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

  it("All: every session, workers too, newest first; never a hidden helper", () => {
    expect(skyLineTitles(rows, "all")).toEqual(["halted", "perched", "orphan", "worker", "lead", "old"]);
  });

  it("Mine: top-level sessions only (a worker folds into its parent's line; an orphan stands alone)", () => {
    expect(skyLineTitles(rows, "mine")).toEqual(["halted", "perched", "orphan", "lead", "old"]);
    expect(lineFamily("mine")).toBe(true);
    expect(lineFamily("all")).toBe(false);
  });

  it("Perched on you: lines whose flight is perched on you or halted", () => {
    expect(skyLineTitles(rows, "perched")).toEqual(["halted", "perched"]);
    expect(linePerched(rows[0])).toBe(false);
  });

  it("This session: one line, or none when it is gone or hidden", () => {
    expect(skyLineTitles(rows, "one", { focus: "worker" })).toEqual(["worker"]);
    expect(skyLineTitles(rows, "one", { focus: "nope" })).toEqual([]);
    expect(skyLineTitles(rows, "one", { focus: "verify-run" })).toEqual([]);
    expect(lineFamily("one")).toBe(true);
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
  it("#flights is the Sky; #flights/sky/<title> one session's; the Board's old link lands on the Sky", () => {
    expect(skyTarget()).toBe("sky");
    expect(skyTarget("fix login/x")).toBe("sky/fix login/x");
    expect(parseFlightsTarget(skyTarget("fix login/x"))).toEqual({ tab: "sky", rest: "fix login/x" });
    expect(parseFlightsTarget("board")).toEqual({ tab: "sky", rest: "" });
    expect(hashFor("flights", "sky")).toBe("#flights");
    const h = hashFor("flights", skyTarget("fix login/x"));
    expect(h).toBe("#flights/sky/fix%20login%2Fx");
    expect(parseHash(h)).toEqual({ name: "flights", target: "sky/fix login/x" });
  });
});

describe("the wiring", () => {
  const sky = code("components/flights/Sky.tsx");
  const sheet = css("components/flights/Sky.css");

  it("is one transformed plane, no library: translate + scale on one container, the grid scaled with it", () => {
    expect(sky).toContain('<div className="sky-plane" style={{ transform: `translate(${cam.x}px, ${cam.y}px) scale(${cam.z})` }}>');
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
    expect(sky).toContain('} else if (e.key === "0") {');
    expect(sky).toContain("onDoubleClick={onDouble}");
  });

  it("a line drags by its bar and its spot is remembered; a closed one fades out", () => {
    expect(sky).toContain("setSaved((s) => rememberSkyPos(s, id, p));");
    expect(sky).toContain('className="sky-line-bar"');
    expect(sky).toContain('"sky-line sky-line-leaving"');
    expect(sheet).toMatch(/\.sky-line-leaving \{[^}]*animation: sky-out/);
  });

  it("mounts the intake lane on the plane and the new-session card where it was asked for", () => {
    expect(sky).toContain("<IntakeLane rows={rows} plans={plans} onFly={flyTo} width={INTAKE_W} />");
    expect(sky).toMatch(/<NewSessionBlock\s+x=\{newAt\.x\}\s+y=\{newAt\.y\}\s+plans=\{plans\}/);
    expect(sky).toContain("setSaved((s) => rememberSkyPos(s, title, newAt));");
  });

  it("the bird hops (a transition on left / top) and bobs only while the flock works", () => {
    const canvas = css("components/flights/FlywayCanvas.css");
    expect(canvas).toMatch(/\.fl-bird-marker \{[^}]*transition: left [^;]*top /);
    expect(canvas).toMatch(/\.fl-bird-marker\.tone-working \.fl-bird-big \{[^}]*animation: fl-bird-bob/);
    expect(canvas).toMatch(/\.fl-bird-marker\.tone-you \{[^}]*var\(--tone-attention\)/);
    expect(canvas).toMatch(/\.fl-bird-marker\.tone-halted \{[^}]*var\(--red\)/);
  });

  it("the full window: .sky-full under the top bar; a phone gets the list", () => {
    expect(sheet).toMatch(/\.modal\.top-dialog\.sky-full \{[^}]*padding: var\(--topbar-h, 40px\) 0 0;/);
    expect(sky).toContain('if (narrow)\n    return (\n      <div className="sky sky-narrow" data-tour="flights-sky">');
    expect(sheet).toContain(".light .sky {");
  });

  it("the pane's Flight tab opens it on its session", () => {
    expect(code("components/flights/SessionFlight.tsx")).toContain('onClick={() => useUi.getState().openDialogFor("flights", skyTarget(title))}');
  });
});
