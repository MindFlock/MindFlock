/// <reference types="vite/client" />
// The Code Tree's SUPPORT ROOTS: there is no scaffold. Every file that is not code — docs, CI, config, the repo
// root's files, a tests folder's data, a mostly-non-code folder — is a root tip in the soil exactly like a test
// file: the same nodule, the same strand fans (roots.ts). A code folder's non-code files hang on their own limb
// beside that folder's tests ("frontend · non-code"); the rest hang as clumps of one TAP ROOT under the trunk
// ("docs, CI and config": docs, .github, repo root files, test data · tests/). Pins the classification, where each
// file hangs, the limb labels (never "tests: tests"), the split of a big group, that a support tip is drawn as a
// root tip, and that nothing of the scaffold is left.
import { describe, expect, it } from "vitest";
import { folderPath, pick } from "../lib/codetree/draw";
import { fitWhole, pictureBox, WHOLE_PAD } from "../lib/codetree/fit";
import { rawFromSnapshot } from "../lib/codetree/input";
import {
  SUPPORT_SPLIT,
  TAP_LABEL,
  allNodes,
  buildModel,
  isSupport,
  nameTop,
  parseFiles,
  type Model,
  type RawFile,
  type RawRepo,
  type TNode,
} from "../lib/codetree/model";
import { readPalette } from "../lib/codetree/palette";
import { rootNetOf, rootTipG, tipSize } from "../lib/codetree/roots";
import type { TreeView } from "../lib/codetree/types";
import fixtureRaw from "./fixtures/codetree_repo.json?raw";

const NODE = (globalThis as unknown as { process: { env: Record<string, string | undefined> } }).process;
const fs = (await import(/* @vite-ignore */ "node:" + "fs")) as { existsSync(p: string): boolean; readFileSync(p: string, enc: string): string };
const FIXD = (NODE.env.HOME || "/nonexistent") + "/mindflock-prototypes/map-ux/fixtures/";
const HAVE_SC = fs.existsSync(FIXD + "sc_snap.json");
const fixture = (tag: string) => buildModel(rawFromSnapshot(JSON.parse(fs.readFileSync(FIXD + tag + "_snap.json", "utf8")), tag));

const FX = JSON.parse(fixtureRaw) as RawRepo;
const REPO = buildModel({ name: FX.name, files: FX.files });

/** A small repo with every kind of support file: a code folder's README and config, its test data, a tests
 * folder's data, an unplaced test, CI, the repo root's files and a docs folder big enough to split. */
function mixed(): RawRepo {
  const files: RawFile[] = [];
  for (const d of ["app", "lib"]) for (let i = 0; i < 8; i++) files.push({ p: `frontend/src/${d}/m${i}.ts`, n: 60 + i * 13, k: "c" });
  files.push({ p: "frontend/README.md", n: 80, k: "d" }, { p: "frontend/package.json", n: 40, k: "d" }, { p: "frontend/tsconfig.json", n: 20, k: "d" });
  files.push({ p: "frontend/src/__tests__/app.test.ts", n: 120, k: "c", t: true, g: "frontend/src/app" });
  files.push({ p: "frontend/src/__tests__/fixtures/snap.json", n: 300, k: "d", t: true });
  for (let i = 0; i < 10; i++) files.push({ p: `backend/core/c${i}.py`, n: 90 + i * 7, k: "c" });
  files.push({ p: "backend/pyproject.toml", n: 50, k: "d" });
  files.push({ p: "tests/unit/test_core.py", n: 200, k: "c", t: true, g: "backend/core" });
  files.push({ p: "tests/other/test_misc.py", n: 60, k: "c", t: true });
  files.push({ p: "tests/fixtures/case.json", n: 90, k: "d", t: true });
  for (let i = 0; i < 6; i++) files.push({ p: `docs/page-${i}.md`, n: 100 + i, k: "d" });
  for (const sub of ["api", "howto", "img"]) for (let i = 0; i < 9; i++) files.push({ p: `docs/${sub}/p${i}.md`, n: 50 + i * 3, k: sub === "img" ? "a" : "d" });
  files.push({ p: ".github/workflows/ci.yml", n: 80, k: "d" });
  for (const f of ["README.md", "LICENSE", "pyproject.toml"]) files.push({ p: f, n: 60, k: "d" });
  return { name: "mixed", files };
}
const MIX = buildModel(mixed());
const MODELS: Array<[string, Model]> = [
  ["repo fixture", REPO],
  ["mixed repo", MIX],
];

const limbOf = (n: TNode | null): TNode | null => {
  let q = n;
  while (q && q.depth > 1) q = q.parent;
  return q;
};
const fileOf = (M: Model, p: string) => M.byPath.get(p)!;
const tap = (M: Model) => M.roots.kids.find((k) => k.folder === "") || null;

describe.each(MODELS)("support roots (%s)", (_name, M) => {
  it("every file is crown or root, and every non-code file is a root tip below the ground line", () => {
    for (const f of M.files) expect(["crown", "root"]).toContain(f.place);
    expect("piles" in M).toBe(false);
    expect("scaffold" in M).toBe(false);
    let n = 0;
    for (const f of M.files) {
      if (f.kind === "c") continue;
      n++;
      expect(f.place, f.path).toBe("root");
      expect(isSupport(f)).toBe(true);
      const l = f.leaf!;
      expect(l.region, f.path).toBe("root");
      // (the root search may set a top row ON the line, as for tests: codetreeRoots.test.ts)
      expect(l.y, f.path).toBeGreaterThan(M.groundY - tipSize(l)[0]);
    }
    expect(n).toBeGreaterThan(0);
  });

  it("no crown leaf is below the ground line, and the crown is code", () => {
    for (const l of M.leaves) {
      if (l.region !== "crown") continue;
      expect(l.y).toBeLessThan(M.groundY);
      expect(l.file.kind, l.file.path).toBe("c");
    }
  });

  it("a support tip is a root tip: a nodule at the end of its own strand in a root fan", () => {
    for (const l of M.leaves) {
      if (!isSupport(l.file)) continue;
      const g = l.g || rootTipG(l);
      const w = Math.hypot(g[2] - g[6], g[3] - g[7]),
        len = Math.hypot(g[4] - g[0], g[5] - g[1]);
      expect(w / len, l.file.path).toBeGreaterThan(0.79); // a nodule, not a leaf lens (0.37)
      const net = rootNetOf(l.term!)!;
      expect(net, l.file.path).toBeDefined();
      expect(net.nodes.filter((q) => q.leaf === l).length).toBe(1);
      expect(net.crossings).toBe(0);
    }
  });

  it("support limbs are their own: never mixed into a test fan, and a test fan holds no support file", () => {
    for (const n of allNodes(M.roots)) {
      if (n.depth < 1) continue;
      const top = limbOf(n)!;
      for (const f of n.files) expect(isSupport(f), f.path).toBe(!!top.support);
    }
  });

  it("the labels say what a root holds: never 'tests: tests', never 'tests for' on a support root", () => {
    for (const n of allNodes(M.roots)) {
      if (n.depth < 1) continue;
      const lb = n.label || "";
      expect(lb).not.toMatch(/tests: |^tests · tests\b/);
      if (n.support) expect(lb).not.toMatch(/^tests for /);
    }
  });

  it("the twin-less groups are clumps of one tap root under the trunk, with no crown twin", () => {
    const T = tap(M)!;
    expect(T).toBeTruthy();
    expect(T.label).toBe(TAP_LABEL);
    expect(T.support).toBe(true);
    expect(T.crownTwin).toBeNull();
    const docs = fileOf(M, M.files.find((f) => f.path.startsWith("docs/"))!.path).node!;
    expect(limbOf(docs)).toBe(T);
    expect(docs.crownTwin).toBeNull();
    // it hangs under the tree, not out past it
    expect(T.F![0]).toBeGreaterThan(M.crownBounds.x0);
    expect(T.F![0]).toBeLessThan(M.crownBounds.x1);
    // the repo root's non-code files are one clump of it, by that name
    const rf = M.files.find((f) => !f.path.includes("/") && f.kind !== "c");
    if (rf) {
      expect(limbOf(rf.node)).toBe(T);
      expect(rf.node!.label).toBe("repo root files");
    }
  });

  it("the whole view holds every support tip (no band for a scaffold any more)", () => {
    const B = pictureBox(M);
    for (const l of M.leaves) if (isSupport(l.file)) expect(l.y).toBeLessThanOrEqual(B.y1 + 1e-6);
    expect(Object.keys(WHOLE_PAD)).not.toContain("piles");
    expect(fitWhole(B, 1200, 800, { top: 0, bot: 0, left: 0, right: 0 }).z).toBeGreaterThan(0);
  });
});

describe("where a support file hangs (mixed repo)", () => {
  const M = MIX;
  it("a code folder's README and config hang on its own non-code limb, beside its tests", () => {
    const readme = fileOf(M, "frontend/README.md");
    const limb = limbOf(readme.node)!;
    expect(limb.crownTwin).toBe(M.nodeOf.get("frontend"));
    expect(limb.label).toBe("frontend · non-code");
    expect(limb.support).toBe(true);
    expect(fileOf(M, "frontend/package.json").node).toBe(readme.node);
    expect(limbOf(fileOf(M, "backend/pyproject.toml").node)!.label).toBe("backend · non-code");
    // beside: the same crown twin as the tests for frontend, so the crown-x sort puts them side by side
    const tests = limbOf(fileOf(M, "frontend/src/__tests__/app.test.ts").node)!;
    expect(tests.label).toMatch(/^tests for frontend/);
    expect(limbOf(tests.crownTwin || null)).toBe(limb.crownTwin);
    const order = M.roots.kids.slice().sort((a, b) => a.F![0] - b.F![0]);
    expect(Math.abs(order.indexOf(tests) - order.indexOf(limb))).toBeLessThanOrEqual(2);
  });

  it("a tests folder's data hangs on a test data limb: beside its code folder's tests, else on the tap root", () => {
    const near = limbOf(fileOf(M, "frontend/src/__tests__/fixtures/snap.json").node)!;
    expect(near.label).toBe("test data");
    expect(near.crownTwin).toBe(M.nodeOf.get("frontend"));
    expect(near.data).toBe(true);
    const far = fileOf(M, "tests/fixtures/case.json").node!;
    expect(far.label).toBe("test data · tests/");
    expect(limbOf(far)).toBe(tap(M));
  });

  it("an unplaced test is 'other tests · tests/', a test still (not a support root)", () => {
    const u = limbOf(fileOf(M, "tests/other/test_misc.py").node)!;
    expect(u.label).toMatch(/^other tests · tests\/(other\/)?$/); // (one sub-folder: its chain folds into the limb)
    expect(u.support).toBeFalsy();
  });

  it("CI and the repo root's files are clumps of the tap root, named for their folder", () => {
    expect(fileOf(M, ".github/workflows/ci.yml").node!.label).toBe(".github");
    expect(fileOf(M, "LICENSE").node!.label).toBe("repo root files");
    expect(limbOf(fileOf(M, "LICENSE").node)).toBe(tap(M));
  });

  it(`a group of more than ${SUPPORT_SPLIT} files splits into clumps by its second path segment`, () => {
    const docs = M.nodes.find((n) => n.kind === "root" && n.folder === "docs")!;
    expect(docs.nFiles).toBeGreaterThan(SUPPORT_SPLIT);
    expect(docs.kids.map((k) => k.folder).sort()).toEqual(["docs/api", "docs/howto", "docs/img"]);
    expect(docs.files.map((f) => f.path).every((p) => p.split("/").length === 2)).toBe(true); // its own files stay on it
    for (const k of docs.kids) expect(k.nFiles).toBeLessThanOrEqual(SUPPORT_SPLIT);
  });

  it("a tap root clump is named like a top-level folder: 'docs', 'docs › api' — never 'docs, CI and config › docs'", () => {
    const docs = M.nodes.find((n) => n.kind === "root" && n.folder === "docs")!;
    const api = docs.kids.find((k) => k.folder === "docs/api")!;
    expect(nameTop(api)).toBe(docs);
    expect(folderPath(docs)).toBe("docs");
    expect(folderPath(api)).toBe("docs › api");
    const u = limbOf(fileOf(M, "tests/other/test_misc.py").node)!;
    for (const k of u.kids) expect(folderPath(k)).not.toMatch(/tests · tests/);
  });

  it("a mostly-non-code folder is a root, code and all", () => {
    const files: RawFile[] = [{ p: "src/a.py", n: 400, k: "c" }];
    for (let i = 0; i < 20; i++) files.push({ p: `site/page${i}.md`, n: 400, k: "d" });
    files.push({ p: "site/conf.py", n: 40, k: "c" });
    const F = parseFiles({ name: "x", files });
    expect(F.find((f) => f.path === "site/conf.py")!.place).toBe("root");
    expect(F.find((f) => f.path === "src/a.py")!.place).toBe("crown");
  });

  it("a click on a support tip picks that file, as on a test", () => {
    const l = fileOf(M, "frontend/README.md").leaf!;
    const v = { M, cam: { x: l.x, y: l.y, z: 4 }, W: 800, H: 600, agents: [], tool: "explore" } as unknown as TreeView;
    const hit = pick(v, 400, 300);
    expect(hit && hit.leaf).toBe(l);
  });
});

describe("the support tone", () => {
  it("is its own cooler soil tone in both themes (same strands, same tip shape)", () => {
    const g = globalThis as Record<string, unknown>;
    const had = g.document;
    const theme = (light: boolean) => {
      if (!had) g.document = { documentElement: { classList: { contains: (c: string) => c === "light" && light } } };
      else (had as Document).documentElement.classList.toggle("light", light);
      return readPalette(null);
    };
    const both = [theme(true), theme(false)];
    if (!had) delete g.document;
    expect(both[0].light).toBe(true);
    for (const P of both) {
      expect(P.supRoot.strand).not.toBe(P.BARK.root);
      expect(P.supRoot.tip).not.toBe(P.rootTip);
      expect(P.supRoot.fine).not.toBe(P.rootFine);
      // a cool hue (blue-grey), where the test roots are warm (brown)
      for (const c of [P.supRoot.strand, P.supRoot.fine, P.supRoot.tip]) expect(Number(/hsl\((\d+)/.exec(c)![1])).toBeGreaterThan(180);
    }
  });
});

describe.runIf(HAVE_SC)("support roots on a big repo (sc fixture)", () => {
  it("no support fan is wider than the widest test fan, and none crosses itself", () => {
    const M = fixture("sc");
    let sup = 0,
      test = 0;
    for (const t of M.terms) {
      if (t.region !== "root" || !t.leaves.length) continue;
      const s = t.leaves.every((l) => isSupport(l.file));
      if (s) sup = Math.max(sup, t.leaves.length);
      else test = Math.max(test, t.leaves.length);
      expect(rootNetOf(t)!.crossings, t.node.path).toBe(0);
    }
    expect(sup).toBeGreaterThan(0);
    expect(sup).toBeLessThanOrEqual(test);
  });
});
