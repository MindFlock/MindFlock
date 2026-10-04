/// <reference types="vite/client" />
import { describe, it, expect } from "vitest";
import type { FeedRecord, RedZone } from "../api/types";
import {
  RUNNING_STALE_S,
  anchoredZonePath,
  autoMode,
  blastFrom,
  blastRows,
  classifyPath,
  currentBreaches,
  exemptSet,
  feedState,
  findPattern,
  guardPill,
  heat,
  importersOf,
  isTestPath,
  nodeOf,
  offPlanSet,
  parseFilterToPattern,
  peeksOf,
  planProgress,
  planScope,
  reverseIndex,
  scopeRequests,
  snapStale,
  underPath,
  compilePattern,
  exactRe,
  zoneDoc,
  zoneDocFrom,
  zoneFor,
  zoneMatchers,
  type ZoneClass,
  type ZoneEntry,
} from "../lib/codemap";
import { normFileView } from "../lib/codemapApi";
import { markCodemapSeen, codemapSeenAt, redZoneChip, serverNow } from "../lib/codemapSeen";
import fixtureRaw from "./fixtures/red_zone_patterns.json?raw";
// The SHARED classify cases live with the backend (red_zones.classify and the
// hook's copy run the same file): three engines, one fixture.
import classifyRaw from "../../../tests/fixtures/zone_classify_cases.json?raw";

interface Case {
  pattern: string;
  re: string;
  match: string[];
  miss: string[];
}
const FIXTURE = JSON.parse(fixtureRaw) as { cases: Case[]; ci: Case[] };

interface ClassifyCase {
  name: string;
  zones: { red: ZoneEntry[]; green: ZoneEntry[]; companions: ZoneEntry[] };
  path: string;
  ci: boolean;
  expect: ZoneClass;
}
const CLASSIFY = JSON.parse(classifyRaw) as ClassifyCase[];

describe("import graph: reverseIndex / blastFrom", () => {
  // 0 ← 1 ← 2 ← 3 ; 4 imports 0 ; 5 isolated ; self-edge and junk ignored
  const edges: Array<[number, number]> = [
    [1, 0],
    [2, 1],
    [3, 2],
    [4, 0],
    [5, 5],
    [9, 0],
    [-1, 2],
  ];
  const rev = reverseIndex(edges, 6);

  it("indexes importers", () => {
    expect(Array.from(importersOf(rev, 0)).sort()).toEqual([1, 4]);
    expect(Array.from(importersOf(rev, 5))).toEqual([]);
    expect(Array.from(importersOf(rev, 42))).toEqual([]);
  });

  it("walks importers up to depth, excluding seeds", () => {
    expect([...blastFrom([0], rev, 1)].sort()).toEqual([
      [1, 1],
      [4, 1],
    ]);
    expect(blastFrom([0], rev, 3)).toEqual(
      new Map([
        [1, 1],
        [4, 1],
        [2, 2],
        [3, 3],
      ])
    );
    expect(blastFrom([0, 1], rev, 1).has(1)).toBe(false);
    expect(blastFrom([0], rev, 9).size).toBe(4); // clamped to 3
    expect(blastFrom([5], rev, 3).size).toBe(0);
  });
});

describe("feedState", () => {
  const rec = (r: Partial<FeedRecord>): FeedRecord => ({ ts: 0, ev: "pre", tool: "Edit", kind: "edit", ...r }) as FeedRecord;
  const W = "working";

  it("an edit lands at post; a read lights at pre", () => {
    const s = feedState(
      [
        rec({ ts: 10, ev: "pre", kind: "edit", id: "a", writes: ["x.py"] }),
        rec({ ts: 11, ev: "post", kind: "edit", id: "a", writes: ["x.py"] }),
        rec({ ts: 12, ev: "pre", tool: "Read", kind: "read", id: "b", reads: ["y.py"] }),
      ],
      20,
      W
    );
    expect(s.files.get("x.py")).toMatchObject({ lastTs: 11, kind: "edit", running: false });
    expect(s.files.get("y.py")).toMatchObject({ lastTs: 12, kind: "read" });
    expect(s.lastEditTs).toBe(11);
  });

  it("a pre with no post is not an edit (the tool may still fail)", () => {
    const s = feedState([rec({ ts: 10, ev: "pre", kind: "edit", id: "a", writes: ["x.py"] })], 11, W);
    expect(s.files.has("x.py")).toBe(false);
    expect(s.lastEditTs).toBe(0);
  });

  it("bash pre is running only after 1.5 s without a post/fail", () => {
    const pre = rec({ ts: 100, ev: "pre", tool: "Bash", kind: "bash", id: "b1", cmd: "pytest -x", writes: ["out.txt"] });
    expect(feedState([pre], 101, W).running).toEqual([]);
    const s = feedState([pre], 102, W);
    expect(s.running.map((r) => r.cmd)).toEqual(["pytest -x"]);
    expect(s.files.get("out.txt")?.running).toBe(true);
    const done = feedState([pre, rec({ ts: 105, ev: "post", tool: "Bash", kind: "bash", id: "b1", writes: ["out.txt"] })], 106, W);
    expect(done.running).toEqual([]);
    expect(done.files.get("out.txt")).toMatchObject({ kind: "edit", running: false });
  });

  it("pre → fail is terminal: not running, not an edit", () => {
    const s = feedState(
      [
        rec({ ts: 100, ev: "pre", tool: "Bash", kind: "bash", id: "b2", cmd: "make", writes: ["build/x"] }),
        rec({ ts: 101, ev: "fail", tool: "Bash", kind: "bash", id: "b2", err: "exit 2" }),
      ],
      200,
      W
    );
    expect(s.running).toEqual([]);
    expect(s.files.get("build/x")).toBeUndefined();
  });

  it("a deny is terminal and marks the path denied", () => {
    const s = feedState(
      [
        rec({
          ts: 50,
          ev: "pre",
          tool: "Bash",
          kind: "bash",
          id: "d1",
          cmd: "rm config.toml",
          writes: ["config.toml"],
          deny: { path: "config.toml", pattern: "config.toml", zone_id: "rz_1" },
        }),
      ],
      60,
      W
    );
    expect(s.running).toEqual([]);
    expect(s.files.get("config.toml")).toMatchObject({ deniedTs: 50, running: false });
  });

  it("breach records stamp a fading breachTs on the path, subagents are attributed", () => {
    const s = feedState(
      [
        rec({
          ts: 70,
          ev: "post",
          tool: "Bash",
          kind: "bash",
          id: "b3",
          agent: "ag_7",
          breach: [{ path: "backend/athena/a.py", pattern: "backend/athena" }],
        }),
      ],
      71,
      W
    );
    expect(s.files.get("backend/athena/a.py")).toMatchObject({ breachTs: 70, agent: "ag_7", kind: "edit" });
    // A cue with a time on it, never a standing "breached" flag: the file may
    // have been reverted since, and only the server's live.breaches knows.
    expect(s.files.get("backend/athena/a.py")).not.toHaveProperty("breach");
  });

  it("stale pres are not running forever", () => {
    const s = feedState([rec({ ts: 0, ev: "pre", tool: "Bash", kind: "bash", id: "z", cmd: "sleep" })], 999999, W);
    expect(s.running).toEqual([]);
  });

  // A pre with no Post* is also what a call refused OUTSIDE MindFlock leaves
  // (the permission prompt answered No, a permissions.deny rule, another hook):
  // it must not read as "running" for as long as it stays in the feed.
  const bashPre = rec({ ts: 100, ev: "pre", tool: "Bash", kind: "bash", id: "b1", cmd: "sed -i x a.py", writes: ["a.py"] });

  it("nothing is running unless the session is working", () => {
    for (const act of ["idle", "clarify", "limit", "offline", ""]) {
      const s = feedState([bashPre], 110, act);
      expect([act, s.running]).toEqual([act, []]);
      expect(s.files.get("a.py")?.running ?? false).toBe(false);
    }
    const w = feedState([bashPre], 110, "working");
    expect(w.running.map((r) => r.id)).toEqual(["b1"]);
    expect(w.files.get("a.py")?.running).toBe(true);
  });

  it("an open bash pre stops running once the same agent started AND finished a later call", () => {
    const later = [
      rec({ ts: 105, ev: "pre", tool: "Read", kind: "read", id: "r1", reads: ["b.py"] }),
      rec({ ts: 106, ev: "post", tool: "Read", kind: "read", id: "r1", reads: ["b.py"] }),
    ];
    expect(feedState([bashPre, ...later], 110, W).running).toEqual([]);
    // A later pre alone proves nothing: parallel sibling calls start together.
    expect(feedState([bashPre, later[0]], 110, W).running.map((r) => r.id)).toEqual(["b1"]);
    // A call that started BEFORE the bash (and merely ended after) proves nothing either.
    const earlier = [
      rec({ ts: 99, ev: "pre", tool: "Read", kind: "read", id: "r0", reads: ["c.py"] }),
      rec({ ts: 101, ev: "post", tool: "Read", kind: "read", id: "r0", reads: ["c.py"] }),
    ];
    expect(feedState([bashPre, ...earlier], 110, W).running.map((r) => r.id)).toEqual(["b1"]);
    // Another agent's finished call doesn't close the main agent's command.
    const sub = later.map((r) => ({ ...r, agent: "ag_2" }));
    expect(feedState([bashPre, ...sub], 110, W).running.map((r) => r.id)).toEqual(["b1"]);
  });

  it("the stale backstop is ten minutes", () => {
    expect(RUNNING_STALE_S).toBe(600);
    expect(feedState([bashPre], 100 + 599, W).running.length).toBe(1);
    expect(feedState([bashPre], 100 + 601, W).running).toEqual([]);
  });

  it("a push refusal is terminal and bursts no tile", () => {
    const push = rec({
      ts: 120,
      ev: "pre",
      tool: "Bash",
      kind: "bash",
      id: "p1",
      cmd: "git push origin feature",
      deny: { path: "config/settings.toml", pattern: null, zone_id: null, reason: "breach", push: true },
    });
    const s = feedState([push], 130, W);
    expect(s.running).toEqual([]);
    expect(s.files.has("config/settings.toml")).toBe(false);
  });
});

describe("snapStale", () => {
  const base = { nullFpMs: 20000, partialMs: 3000 };
  const snap = (fp: string | null, partial = false) => ({ fingerprint: fp, graph_partial: partial });

  it("fetches when there is no snapshot or the fingerprint moved", () => {
    expect(snapStale({ ...base, snap: null, liveFp: "a", sinceSnapMs: 0 })).toBe(true);
    expect(snapStale({ ...base, snap: snap("a"), liveFp: "b", sinceSnapMs: 0 })).toBe(true);
    expect(snapStale({ ...base, snap: snap("a"), liveFp: "a", sinceSnapMs: 999999 })).toBe(false);
  });

  it("re-reads a null fingerprint on its slow clock", () => {
    expect(snapStale({ ...base, snap: snap(null), liveFp: null, sinceSnapMs: 1000 })).toBe(false);
    expect(snapStale({ ...base, snap: snap(null), liveFp: null, sinceSnapMs: 20001 })).toBe(true);
  });

  it("keeps asking while the import graph is partial, even with an unchanged fingerprint", () => {
    // The idle-agent case: plan review, nothing moves the fingerprint.
    expect(snapStale({ ...base, snap: snap("a", true), liveFp: "a", sinceSnapMs: 1000 })).toBe(false);
    expect(snapStale({ ...base, snap: snap("a", true), liveFp: "a", sinceSnapMs: 3001 })).toBe(true);
    // Complete → back to fingerprint-only.
    expect(snapStale({ ...base, snap: snap("a", false), liveFp: "a", sinceSnapMs: 3001 })).toBe(false);
  });
});

describe("anchoredZonePath", () => {
  it("anchors a root-level tile so it means that one path", () => {
    expect(anchoredZonePath("package.json")).toBe("/package.json");
    expect(anchoredZonePath("config")).toBe("/config");
    expect(anchoredZonePath("config/")).toBe("/config");
  });

  it("leaves slash paths (already root-anchored), anchored and empty ones alone", () => {
    expect(anchoredZonePath("backend/config")).toBe("backend/config");
    expect(anchoredZonePath("/secrets")).toBe("/secrets");
    expect(anchoredZonePath("")).toBe("");
  });

  it("the anchored form compiles to a root-only match (shared fixture)", () => {
    const c = FIXTURE.cases.find((x) => x.pattern === anchoredZonePath("secrets"))!;
    expect(c).toBeTruthy();
    const rx = new RegExp(c.re);
    expect(rx.test("secrets/key.pem")).toBe(true);
    expect(rx.test("sub/secrets")).toBe(false);
  });
});

describe("findPattern", () => {
  it("a clicked zone previews its own pattern, not the free-text *word* rule", () => {
    expect(findPattern("athena", { pattern: "athena" })).toBe("athena");
    expect(findPattern("config.toml", { pattern: "config.toml" })).toBe("config.toml");
    // Typed text (or no zone) keeps the free-text rule.
    expect(findPattern("athena", null)).toBe("*athena*");
    expect(findPattern("athen", { pattern: "athena" })).toBe("*athen*");
  });
});

describe("guardPill", () => {
  it("labels from state; the tooltip is the server's sentence", () => {
    const p = guardPill({ state: "detect", detail: "Codex has no hook guard: edits are detected, not prevented." }, 2, "codex");
    expect(p.label).toBe("Detect-only (codex)");
    expect(p.title).toBe("Codex has no hook guard: edits are detected, not prevented.");
    expect(p.cls).toBe("g-detect");
  });

  it("falls back to our explanation when detail is empty or only the label again", () => {
    // What older servers sent: the label itself, which hid the explanation.
    for (const [state, detail] of [
      ["guarded", "Guarded"],
      ["arming", "Arming…"],
      ["detect", "Detect-only (codex)"],
      ["detect", "Detect-only (some-other-name)"],
      ["off", "Guard off — re-arming"],
      ["none", "No zones"],
      ["guarded", ""],
    ]) {
      const p = guardPill({ state, detail }, 1, "codex");
      expect([state, detail, p.title === p.label]).toEqual([state, detail, false]);
      expect(p.title.length).toBeGreaterThan(p.label.length);
    }
    expect(guardPill({ state: "detect", detail: "Detect-only (codex)" }, 1, "codex").title).toMatch(/not prevented/);
  });

  it("before the first poll: enforced zones read as guarded", () => {
    expect(guardPill(null, 2, "").label).toBe("Guarded");
    expect(guardPill(null, 0, "").label).toBe("No zones");
  });
});

describe("rail chip seen-time on the server clock", () => {
  // The block time is the hook's time.time() on the SERVER; the browser may be
  // another machine. Seen must be stamped on the same clock.
  it("serverNow adds the measured skew", () => {
    expect(serverNow(-120, 1000)).toBe(880);
    expect(serverNow(45, 1000)).toBe(1045);
    expect(serverNow(NaN, 1000)).toBe(1000);
  });

  const rz = (last: number) => ({ zones: 1, breaches: 0, last_block_ts: last, guard: "guarded" });

  it("server behind the browser: a block after the last look is still flagged", () => {
    const skew = -120;
    const client = 5_000_000;
    markCodemapSeen("skew-behind", serverNow(skew, client));
    const blockTs = client + 30 + skew; // 30 s later, stamped on the server
    expect(redZoneChip(rz(blockTs), codemapSeenAt("skew-behind"))?.label).toBe("⛔");
    markCodemapSeen("skew-behind", serverNow(skew, client + 40));
    expect(redZoneChip(rz(blockTs), codemapSeenAt("skew-behind"))?.label).toBe("🛡");
  });

  it("server ahead of the browser: a seen block clears", () => {
    const skew = 120;
    const client = 6_000_000;
    const blockTs = client - 30 + skew; // 30 s before the look, server clock
    markCodemapSeen("skew-ahead", serverNow(skew, client));
    expect(redZoneChip(rz(blockTs), codemapSeenAt("skew-ahead"))?.label).toBe("🛡");
  });
});

describe("heat", () => {
  it("maps log(added+removed) into 0..1 with a floor", () => {
    const h = heat([
      { path: "a", status: "M", added: 1, removed: 0 },
      { path: "b", status: "M", added: 300, removed: 200 },
      { path: "c", status: "A", added: 0, removed: 0 },
    ]);
    expect(h.get("b")).toBe(1);
    expect(h.get("a")!).toBeGreaterThan(0.1);
    expect(h.get("a")!).toBeLessThan(h.get("b")!);
    expect(h.get("c")!).toBeGreaterThan(0);
    for (const v of h.values()) expect(v).toBeLessThanOrEqual(1);
  });

  it("does not paint a lone small change as maximally hot", () => {
    const h = heat([{ path: "a", status: "M", added: 2, removed: 1 }]);
    expect(h.get("a")!).toBeLessThan(0.5);
  });
});

describe("zone matchers (shared fixture with the Python compiler)", () => {
  it("has cases", () => {
    expect(FIXTURE.cases.length).toBeGreaterThan(5);
  });

  for (const c of FIXTURE.cases) {
    it(`${c.pattern} → ${c.re}`, () => {
      const rx = new RegExp(c.re);
      for (const p of c.match) expect([p, rx.test(p)]).toEqual([p, true]);
      for (const p of c.miss) expect([p, rx.test(p)]).toEqual([p, false]);
      // No Python-only syntax leaked into the source.
      expect(c.re).not.toMatch(/\\Z|\(\?P|\(\?[aiLmsux]+\)/);
    });
  }

  for (const c of FIXTURE.ci) {
    it(`case-insensitive: ${c.pattern}`, () => {
      const zones: RedZone[] = [{ id: "rz_ci", pattern: c.pattern, name: "", re: c.re }];
      const ms = zoneMatchers(zones, true);
      for (const p of c.match) expect(zoneFor(ms, p)?.id).toBe("rz_ci");
      for (const p of c.miss) expect(zoneFor(ms, p)).toBeNull();
    });
  }

  it("prefers an enforced zone over a waived one and skips bad sources", () => {
    const zones: RedZone[] = [
      { id: "w", pattern: "backend", name: "", re: "^backend(?:/.*)?$", waived: true },
      { id: "bad", pattern: "(", name: "", re: "(" },
      { id: "e", pattern: "backend/athena", name: "Athena", re: "^backend/athena(?:/.*)?$" },
    ];
    const ms = zoneMatchers(zones);
    expect(ms.length).toBe(2);
    expect(zoneFor(ms, "backend/athena/q.py")?.id).toBe("e");
    expect(zoneFor(ms, "backend/other.py")?.id).toBe("w");
    expect(zoneFor(ms, "frontend/x.ts")).toBeNull();
  });
});

describe("plan helpers", () => {
  it("planProgress marks done / untouched / new", () => {
    const m = planProgress(
      [
        { path: "a.py", intent: "fix", new: false },
        { path: "b.py", intent: "x", new: false },
        { path: "c.py", intent: "add", new: true },
        { path: "d.py", intent: "edit", new: false },
      ],
      new Set(["a.py"]),
      new Set(["d.py"])
    );
    expect(Object.fromEntries(m)).toEqual({ "a.py": "done", "b.py": "untouched", "c.py": "new", "d.py": "done" });
  });

  it("autoMode: Plan while a plan exists and nothing was edited since", () => {
    const plan = { ts: 100, items: [{ path: "a", intent: "", new: false }] };
    expect(autoMode(plan, 0)).toBe("plan");
    expect(autoMode(plan, 99)).toBe("plan");
    expect(autoMode(plan, 101)).toBe("watch");
    expect(autoMode(null, 0)).toBe("watch");
    expect(autoMode({ ts: 1, items: [] }, 0)).toBe("watch");
  });

  it("offPlanSet", () => {
    expect(offPlanSet(["x", "y"]).has("y")).toBe(true);
    expect(offPlanSet(null).size).toBe(0);
  });
});

describe("parseFilterToPattern", () => {
  it("wraps a plain word as a substring glob", () => {
    expect(parseFilterToPattern("athena")).toBe("*athena*");
    expect(parseFilterToPattern("  config ")).toBe("*config*");
  });
  it("keeps globs and paths as written", () => {
    expect(parseFilterToPattern("*.pem")).toBe("*.pem");
    expect(parseFilterToPattern("data[0-9].csv")).toBe("data[0-9].csv");
    expect(parseFilterToPattern("backend/athena")).toBe("backend/athena");
    expect(parseFilterToPattern("./backend\\athena")).toBe("backend/athena");
  });
  it("empty stays empty", () => {
    expect(parseFilterToPattern("   ")).toBe("");
  });
  it("round-trips through the fixture's compile rule for a plain word", () => {
    const c = FIXTURE.cases.find((x) => x.pattern === parseFilterToPattern("athena"))!;
    expect(c).toBeTruthy();
    expect(new RegExp(c.re).test("backend/athena/x.py")).toBe(true);
  });
});


describe("breach set vs feed breach history", () => {
  const rec = (r: Partial<FeedRecord>): FeedRecord => ({ ts: 0, ev: "post", tool: "Bash", kind: "bash", ...r }) as FeedRecord;

  it("a Bash breach that was reverted is a fading cue, never a current breach", () => {
    // The agent's command changed a zoned file (the backstop flags it), then it
    // reverted: the server's live.breaches is now empty, the feed record stays.
    const feed = [rec({ ts: 1000, id: "b1", cmd: "python3 w.py", breach: [{ path: "config/settings.toml", pattern: "config" }] })];
    expect(currentBreaches([]).size).toBe(0);
    expect(currentBreaches(null).size).toBe(0);
    const f = feedState(feed, 1005, "working").files.get("config/settings.toml")!;
    expect(f.breachTs).toBe(1000);
  });

  it("the current set is exactly the server's", () => {
    const s = currentBreaches([{ path: "backend/athena/client.py" }, { path: "config.toml" }, { path: "" }]);
    expect(Array.from(s).sort()).toEqual(["backend/athena/client.py", "config.toml"]);
  });
});

describe("classifyPath (shared fixture with red_zones.classify)", () => {
  it("has cases covering every class", () => {
    expect(CLASSIFY.length).toBeGreaterThan(10);
    expect(new Set(CLASSIFY.map((c) => c.expect))).toEqual(new Set(["blocked", "outside", "companion", "ok"]));
  });
  for (const c of CLASSIFY) {
    it(c.name, () => {
      expect(classifyPath(c.path, zoneDocFrom(c.zones, c.ci))).toBe(c.expect);
    });
  }

  it("every representation must be writable; red hits on any (symlinks)", () => {
    const doc = zoneDocFrom({ red: ["/secrets"], green: ["src/green"], companions: [] });
    expect(classifyPath("src/other/b.py", doc, "src/green/b.py")).toBe("outside");
    expect(classifyPath("src/green/b.py", doc, "src/other/b.py")).toBe("outside");
    expect(classifyPath("src/green/c.py", doc, "src/green/link.py")).toBe("ok");
    expect(classifyPath("src/green/x.py", doc, "secrets/x.py")).toBe("blocked");
  });

  it("the Map's doc: live zones by kind, waived ones ignored, companion files exact", () => {
    const zones: RedZone[] = [
      { id: "g", pattern: "src", name: "", re: "^src(?:/.*)?$", kind: "green" },
      { id: "w", pattern: "docs", name: "", re: "^docs(?:/.*)?$", kind: "green", waived: true },
      { id: "bad", pattern: "(", name: "", re: "(", kind: "green" },
      { id: "r", pattern: "/src/keys", name: "", re: "^src\\/keys(?:/.*)?$" },
    ];
    const doc = zoneDoc(zones, [{ pattern: "yarn.lock", re: "^(?:.*/)?yarn\\.lock(?:/.*)?$" }, "*.snap"], ["web/static/app[1].js"]);
    expect(doc.greenZones.map((z) => z.id)).toEqual(["g"]);
    expect(classifyPath("src/a.py", doc)).toBe("ok");
    expect(classifyPath("src/keys/k.pem", doc)).toBe("blocked");
    expect(classifyPath("docs/a.md", doc)).toBe("outside");
    expect(classifyPath("x/yarn.lock", doc)).toBe("companion");
    expect(classifyPath("ui/a.test.tsx.snap", doc)).toBe("companion");
    expect(classifyPath("web/static/app[1].js", doc)).toBe("companion");
    expect(classifyPath("web/static/app1.js", doc)).toBe("outside");
  });
});

describe("compilePattern (the server's compile rule, shared pattern fixture)", () => {
  for (const c of FIXTURE.cases.concat(FIXTURE.ci)) {
    it(c.pattern, () => {
      const src = compilePattern(c.pattern)!;
      const r = new RegExp(src, FIXTURE.ci.includes(c) ? "i" : "");
      for (const p of c.match) expect([p, r.test(p)]).toEqual([p, true]);
      for (const p of c.miss) expect([p, r.test(p)]).toEqual([p, false]);
    });
  }
  it("rejects what the server rejects", () => {
    for (const p of ["", "  ", "/", "a/../b", "C:/x", "~/x"]) expect(compilePattern(p)).toBeNull();
  });
  it("exactRe escapes glob and regex characters", () => {
    expect(new RegExp(exactRe("a/b[1].js")).test("a/b[1].js")).toBe(true);
    expect(new RegExp(exactRe("a/b[1].js")).test("a/b1.js")).toBe(false);
  });
});




describe("paths → cards", () => {
  const set = new Set(["backend/web", "backend/cli.py", "backend/config"]);
  it("nodeOf finds the card that holds a path (collapsed chains, files)", () => {
    expect(nodeOf("backend/web/core/x.py", set)).toBe("backend/web");
    expect(nodeOf("backend/cli.py", set)).toBe("backend/cli.py");
    expect(nodeOf("backend/cli.pyc", set)).toBeNull();
    expect(nodeOf("frontend/a.ts", set)).toBeNull();
  });
  it("underPath", () => {
    expect(underPath("a/b", "")).toBe(true);
    expect(underPath("a/b", "a")).toBe(true);
    expect(underPath("ab/c", "a")).toBe(false);
    expect(underPath("a", "a")).toBe(true);
  });
  it("blastRows groups dependents by card, tests apart, outside-level last", () => {
    const L = { path: "backend", nodes: [{ path: "backend/web", kind: "dir" }, { path: "backend/config", kind: "dir" }] };
    const deps = new Map([
      ["backend/web/a.py", 1],
      ["backend/web/b.py", 2],
      ["backend/web/test_a.py", 1],
      ["backend/config/c.py", 1],
      ["frontend/x.ts", 1],
    ]);
    const rows = blastRows(deps, L, (p) => isTestPath(p));
    expect(rows.map((r) => [r.path, r.count, r.tests, r.hops, r.outside])).toEqual([
      ["backend/web", 2, 1, 1, false],
      ["backend/config", 1, 0, 1, false],
      ["frontend", 1, 0, 1, true],
    ]);
  });
  it("blastRows without a level (the tree's Session list): by folder, nothing 'elsewhere'", () => {
    const rows = blastRows(new Map([["a/b/x.py", 1], ["a/b/y.py", 1], ["c/z.py", 1], ["tests/test_x.py", 1]]), null, (p) => isTestPath(p));
    expect(rows.map((r) => [r.path, r.count, r.tests, r.outside])).toEqual([
      ["a/b", 2, 0, false],
      ["c", 1, 0, false],
      ["tests", 0, 1, false],
    ]);
  });
  it("isTestPath knows the usual conventions", () => {
    for (const p of ["tests/unit/x.py", "a/__tests__/b.ts", "pkg/x_test.go", "src/a.test.ts", "FooTest.java", "test_x.py", "testsv2/a.py"])
      expect([p, isTestPath(p)]).toEqual([p, true]);
    for (const p of ["src/testing_utils.py", "backend/latest.py", "contest/a.py"]) expect([p, isTestPath(p)]).toEqual([p, false]);
  });
});


describe("green mode: requests, peeks, plan scope, exemptions", () => {
  const rec = (r: Partial<FeedRecord>): FeedRecord => ({ ts: 0, ev: "pre", tool: "Edit", kind: "edit", ...r }) as FeedRecord;
  const outside = (p: string) => !p.startsWith("src/api/");
  it("each green deny is a request, newest first, one per path, only while still outside", () => {
    const feed = [
      rec({ ts: 1, deny: { path: "a.py", pattern: "outside green", kind: "green" } }),
      rec({ ts: 2, deny: { path: "b.py", pattern: "outside green" } }),
      rec({ ts: 3, deny: { path: "a.py", pattern: "outside green", kind: "green", reason: "r" } }),
      rec({ ts: 4, deny: { path: "secrets/k", pattern: "secrets" } }),
      rec({ ts: 5, deny: { path: "src/api/now_allowed.py", pattern: "outside green", kind: "green" } }),
      rec({ ts: 6, deny: { path: "x", pattern: null, push: true, kind: "green" } }),
    ];
    expect(scopeRequests(feed, outside).map((r) => [r.path, r.ts, r.reason])).toEqual([
      ["a.py", 3, "r"],
      ["b.py", 2, ""],
    ]);
  });
  it("peeks: the server's mark wins, else reads classified outside; never for edits or denies", () => {
    expect(peeksOf(rec({ kind: "read", reads: ["src/api/a.py", "lib/b.py"] }), outside)).toEqual(["lib/b.py"]);
    expect(peeksOf(rec({ kind: "read", reads: ["lib/b.py"], peek: [] }), outside)).toEqual([]);
    expect(peeksOf(rec({ kind: "bash", reads: ["lib/b.py"], peek: ["lib/c.py"] }), outside)).toEqual(["lib/c.py"]);
    expect(peeksOf(rec({ kind: "edit", reads: ["lib/b.py"] }), outside)).toEqual([]);
    expect(peeksOf(rec({ kind: "read", reads: ["lib/b.py"], deny: { path: "lib/b.py", pattern: "x" } }), outside)).toEqual([]);
  });
  it("planScope: exact anchored files, a new file's parent folder", () => {
    expect(
      planScope([
        { path: "backend/a.py", intent: "", new: false },
        { path: "backend/new/b.py", intent: "", new: true },
        { path: "README.md", intent: "", new: false },
        { path: "NEWS.md", intent: "", new: true },
      ])
    ).toEqual(["backend/a.py", "backend/new", "/README.md", "/NEWS.md"]);
  });
  it("exemptSet accepts a sha map or a list", () => {
    expect(Array.from(exemptSet({ "a.py": "abc", "b.py": "deleted" }))).toEqual(["a.py", "b.py"]);
    expect(Array.from(exemptSet(["c.py"]))).toEqual(["c.py"]);
    expect(exemptSet(null).size).toBe(0);
  });
  it("the guard pill says what is writable in green mode, and keeps the guard state", () => {
    const p = guardPill({ state: "guarded" }, 1, "claude", ["providers"]);
    expect(p.label).toBe("✓ Only here: providers");
    expect(p.cls).toBe("g-green");
    expect(p.title).toMatch(/only change files inside providers/);
    const d = guardPill({ state: "detect" }, 1, "codex", ["a", "b", "c"]);
    expect(d.label).toBe("✓ Only here: a, b +1");
    expect(d.cls).toBe("g-green g-detect");
    expect(d.title).toMatch(/not prevented/);
  });
});

describe("rail chip in green mode", () => {
  it("a guarded green worktree shows ✓, not the shield; breaches still win", () => {
    const base = { zones: 1, breaches: 0, last_block_ts: null, guard: "guarded" };
    expect(redZoneChip({ ...base, mode: "green" }, 0)?.label).toBe("✓");
    expect(redZoneChip({ ...base, mode: "red" }, 0)?.label).toBe("🛡");
    expect(redZoneChip({ ...base, mode: "green", breaches: 2 }, 0)?.label).toBe("⛔2");
  });
});

describe("normalizers never throw on half-built responses", () => {
  it("normFileView", () => {
    const f = normFileView({ path: "a.py", symbols: [{ name: "X", kind: "class", children: [{ name: "m" }] }], changed_lines: [[1, 2], [3], "x"] });
    expect(f.symbols[0].children![0].kind).toBe("function");
    expect(f.changed_lines).toEqual([[1, 2]]);
    expect(f.imports).toEqual({ internal: [], external: [] });
    expect(f.zones).toEqual({ red: false, green: null });
  });
});

describe("review follow-ups: literal paths, bracket classes, effective tests", () => {
  it("globEscape/anchoredZonePath keep bracketed paths literal", async () => {
    const m = await import("../lib/codemap");
    expect(m.globEscape("app/[slug]/page.tsx")).toBe("app/[[]slug[]]/page.tsx");
    expect(m.anchoredZonePath("a*b.txt")).toBe("/a[*]b.txt");
    expect(m.planScope([{ path: "app/[id]/x.ts", intent: "", new: false } as any])).toEqual(["app/[[]id[]]/x.ts"]);
  });
  it("a test-named file imported by code is code", async () => {
    const m = await import("../lib/codemap");
    const snap = { files: [["srv.py", 1, 0], ["test_plans.py", 1, 2], ["tests/test_x.py", 1, 2]], edges: [[0, 1], [2, 1]] };
    expect(Array.from(m.effectiveTests(snap as any))).toEqual([2]);
  });
});
