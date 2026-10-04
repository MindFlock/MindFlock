/** Code tree — the Map snapshot (GET /code-map) as the tree's input.
 *
 * The snapshot carries `[path, size, flags]` per file and the import graph
 * `[src, dst]` ("src imports dst"). The tree needs per file: a size (lines —
 * bytes are the proxy: ~32 bytes a line), a kind (code / doc / asset, by
 * extension), whether it is a test (flag 2, minus files production code
 * imports — the server's own rule), what it imports, and for a test the folder
 * it tests (so its root grows under that branch). The matching is the
 * prototype extractor's: a test named after a code file tests that file's
 * folder (preferring a candidate it also imports); otherwise the deepest folder
 * holding most of what it imports. Pure. */

import { effectiveTests } from "../codemap";
import type { RawFile, RawRepo } from "./model";

export const BYTES_PER_LINE = 32;

const CODE_EXT = new Set(
  (
    "py ts tsx js jsx mjs cjs java kt kts scala go rs sh bash zsh ps1 sql html htm css scss sass less groovy rb c h cc cpp cxx hpp hh " +
    "swift m mm cs fs vue svelte php pl pm lua dart ex exs erl clj r jl"
  ).split(" ")
);
const ASSET_EXT = new Set(
  (
    "png jpg jpeg gif webp ico icns bmp tiff svgz woff woff2 ttf otf eot pdf zip gz tgz bz2 xz 7z jar war ear class so dylib dll exe bin " +
    "dat db sqlite sqlite3 mp3 mp4 mov wav ogg webm pyc wasm node pkl pickle joblib onnx pt pth ckpt h5 hdf5 npy npz safetensors " +
    "tflite pb parquet avro orc feather arrow mmdb model weights psd ai sketch fig"
  ).split(" ")
);

export function extOf(p: string): string {
  const b = p.slice(p.lastIndexOf("/") + 1);
  const i = b.lastIndexOf(".");
  return i > 0 ? b.slice(i + 1).toLowerCase() : "";
}
export function kindOf(p: string): RawFile["k"] {
  const e = extOf(p);
  return CODE_EXT.has(e) ? "c" : ASSET_EXT.has(e) ? "a" : "d";
}
/** Docs and data run longer lines (and a minified JSON is one): ~40 bytes a
 * line, and no single file may weigh more than this many "lines" — a data
 * dump or a generated bundle must not outweigh the code around it (it would
 * tip its whole top-level folder onto the ground pile). */
export const MAX_DOC_LINES = 2000;
export const MAX_CODE_LINES = 20000;
export function linesOf(size: number, kind: RawFile["k"]): number {
  if (kind === "a") return 0;
  if (kind === "d") return Math.max(1, Math.min(MAX_DOC_LINES, Math.round((size || 0) / 40)));
  return Math.max(1, Math.min(MAX_CODE_LINES, Math.round((size || 0) / BYTES_PER_LINE)));
}

const dirname = (p: string) => (p.includes("/") ? p.slice(0, p.lastIndexOf("/")) : "");
const basename = (p: string) => p.slice(p.lastIndexOf("/") + 1);

/** "test_foo.py" / "foo_test.go" / "Foo.test.ts" / "FooTest.java" → "foo". */
export function testStem(b: string): string {
  let s = b.includes(".") ? b.slice(0, b.lastIndexOf(".")) : b;
  s = s.replace(/\.(test|spec)$/, "");
  s = s.replace(/^test_/, "");
  s = s.replace(/_test$/, "");
  s = s.replace(/(Tests|Test|IT)$/, "");
  return s.toLowerCase();
}
const codeStem = (b: string) => (b.includes(".") ? b.slice(0, b.lastIndexOf(".")) : b).toLowerCase();

/** For each test (by index), the folder path it tests ("" = the repo root),
 * or null when nothing says. `tests` = which files are tests; `imports[i]` =
 * what file i imports. */
export function testTargets(paths: string[], kinds: RawFile["k"][], tests: Set<number>, imports: number[][]): Map<number, string | null> {
  const stemMap = new Map<string, number[]>();
  paths.forEach((p, i) => {
    if (tests.has(i) || kinds[i] !== "c") return;
    const st = codeStem(basename(p));
    const a = stemMap.get(st);
    if (a) a.push(i);
    else stemMap.set(st, [i]);
  });
  const out = new Map<number, string | null>();
  for (const i of tests) {
    const p = paths[i];
    const cands = stemMap.get(testStem(basename(p))) || [];
    const imps = (imports[i] || []).filter((j) => !tests.has(j));
    let tgt: number | null = null;
    if (cands.length) {
      const both = cands.filter((c) => imps.includes(c));
      const pool = both.length ? both : cands;
      const me = new Set(p.split("/"));
      let best = -1,
        bestK: [number, number] | null = null;
      for (const j of pool) {
        const segs = new Set(paths[j].split("/"));
        let shared = 0;
        for (const s of segs) if (me.has(s)) shared++;
        const k: [number, number] = [shared, -paths[j].length];
        if (!bestK || k[0] > bestK[0] || (k[0] === bestK[0] && k[1] > bestK[1])) {
          bestK = k;
          best = j;
        }
      }
      tgt = best;
    } else if (imps.length) {
      // the deepest folder holding most of what it imports
      const dirs = new Map<string, number>();
      for (const j of imps) {
        const parts = paths[j].split("/").slice(0, -1);
        for (let k = 1; k <= parts.length; k++) {
          const d = parts.slice(0, k).join("/");
          dirs.set(d, (dirs.get(d) || 0) + 1);
        }
      }
      const need = Math.max(1, Math.floor((imps.length + 1) / 2));
      let bestD: string | null = null;
      for (const [d, c] of dirs) if (c >= need && (bestD === null || d.split("/").length > bestD.split("/").length)) bestD = d;
      if (bestD !== null) {
        out.set(i, bestD);
        continue;
      }
      tgt = imps[0];
    }
    out.set(i, tgt === null ? null : dirname(paths[tgt]));
  }
  return out;
}

export interface SnapshotLike {
  files?: Array<[string, number, number]>;
  edges?: Array<[number, number]>;
  repo?: { label?: string } | null;
}

/** The snapshot → the tree's input (same file order: index i is snapshot row i). */
export function rawFromSnapshot(snap: SnapshotLike, name: string): RawRepo {
  const rows = snap.files || [];
  const paths = rows.map((r) => String(r[0] || ""));
  const kinds = paths.map(kindOf);
  const tests = effectiveTests({ files: rows, edges: snap.edges || [] });
  const imports: number[][] = rows.map(() => []);
  for (const e of snap.edges || []) {
    const [a, b] = e;
    if (a === b || a < 0 || b < 0 || a >= rows.length || b >= rows.length) continue;
    const l = imports[a];
    if (l[l.length - 1] !== b && !l.includes(b)) l.push(b);
  }
  for (const l of imports) l.sort((x, y) => x - y);
  const targets = testTargets(paths, kinds, tests, imports);
  const files: RawFile[] = rows.map((r, i) => {
    const f: RawFile = { p: paths[i], n: linesOf(Number(r[1]) || 0, kinds[i]), k: kinds[i] };
    if (tests.has(i)) f.t = true;
    if (imports[i].length) f.i = imports[i];
    const g = targets.get(i);
    if (g !== undefined && g !== null) f.g = g;
    return f;
  });
  return { name: name || "repo", files };
}
