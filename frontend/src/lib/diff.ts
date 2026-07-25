/** Pure ports (no DOM) of the diff helpers in 020-shared-utils.js:
 * colorizeDiff (as parseUnifiedDiff), buildSplitDiff, splitDiffByFile, and
 * the diffMode/diffBase localStorage persistence ("cs_diffmode" /
 * "mf_diffbase"). Parsers return typed structures; the React components do
 * the rendering that renderSplitDiff/diffFileSection did with innerHTML. */

export type UnifiedLineKind = "add" | "del" | "ctx" | "hunk" | "meta";

export interface UnifiedLine {
  kind: UnifiedLineKind;
  text: string;
}

/** Line classification matching colorizeDiff(): "@@" = hunk (cyan),
 * "+" = add (green) unless "+++", "-" = del (red) unless "---". The
 * "+++"/"---" file headers come back as "meta"; everything else is "ctx" —
 * both render unstyled, exactly like the vanilla escape-only branch. */
export function parseUnifiedDiff(text: string): UnifiedLine[] {
  return text.split("\n").map((line): UnifiedLine => {
    if (line.startsWith("@@")) return { kind: "hunk", text: line };
    if (line.startsWith("+") && !line.startsWith("+++")) return { kind: "add", text: line };
    if (line.startsWith("-") && !line.startsWith("---")) return { kind: "del", text: line };
    if (line.startsWith("+++") || line.startsWith("---")) return { kind: "meta", text: line };
    return { kind: "ctx", text: line };
  });
}

export type SplitRow =
  | { type: "file"; file: string }
  | { type: "note"; note: string }
  | { type: "hunk"; hunk: string }
  | {
      type: "line";
      lnum: number | "";
      ltext: string;
      ltype: "del" | "ctx" | "blank";
      rnum: number | "";
      rtext: string;
      rtype: "add" | "ctx" | "blank";
    };

/** Parse a unified `git diff` into side-by-side rows. Deletions/additions
 * inside a hunk are paired line-for-line (extra ones spill into blank cells
 * on the opposite side), context lines align on both sides — close enough to
 * an editor's split view without a heavy diff library. Same regexes as the
 * vanilla buildSplitDiff. */
export function buildSplitDiff(content: string): SplitRow[] {
  const rows: SplitRow[] = [];
  let oldNo = 0,
    newNo = 0;
  let rem: Array<{ n: number; t: string }> = [],
    add: Array<{ n: number; t: string }> = [];
  function flush() {
    const n = Math.max(rem.length, add.length);
    for (let i = 0; i < n; i++) {
      const r = rem[i],
        a = add[i];
      rows.push({
        type: "line",
        lnum: r ? r.n : "",
        ltext: r ? r.t : "",
        ltype: r ? "del" : "blank",
        rnum: a ? a.n : "",
        rtext: a ? a.t : "",
        rtype: a ? "add" : "blank",
      });
    }
    rem = [];
    add = [];
  }
  content.split("\n").forEach((line) => {
    if (/^(diff --git|index |--- |\+\+\+ |new file|deleted file|similarity |rename |copy |old mode|new mode|Binary )/.test(line)) {
      flush();
      if (line.startsWith("diff --git")) {
        const m = line.match(/ b\/(.+)$/);
        rows.push({ type: "file", file: m ? m[1] : line.replace(/^diff --git /, "") });
      } else if (line.startsWith("Binary ")) {
        rows.push({ type: "note", note: line });
      }
      return;
    }
    if (line.startsWith("@@")) {
      flush();
      const m = line.match(/@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/);
      if (m) {
        oldNo = parseInt(m[1], 10);
        newNo = parseInt(m[2], 10);
      }
      rows.push({ type: "hunk", hunk: line });
      return;
    }
    const c = line[0];
    if (c === "-") {
      rem.push({ n: oldNo++, t: line.slice(1) });
    } else if (c === "+") {
      add.push({ n: newNo++, t: line.slice(1) });
    } else {
      flush();
      const t = c === " " ? line.slice(1) : line;
      rows.push({
        type: "line",
        lnum: oldNo,
        ltext: t,
        ltype: "ctx",
        rnum: newNo,
        rtext: t,
        rtype: "ctx",
      });
      oldNo++;
      newNo++;
    }
  });
  flush();
  return rows;
}

export interface DiffFileSegment {
  file: string;
  lines: string[];
}

/** Split a multi-file `git diff` into per-file segments {file, lines}. The
 * `diff --git` separator line itself is dropped (the filename moves to the
 * collapsible header); everything else for that file lands in `lines`. */
export function splitDiffByFile(content: string): DiffFileSegment[] {
  const files: DiffFileSegment[] = [];
  let cur: DiffFileSegment | null = null;
  content.split("\n").forEach((line) => {
    if (line.startsWith("diff --git")) {
      const m = line.match(/ b\/(.+)$/);
      cur = { file: m ? m[1] : line.replace(/^diff --git /, ""), lines: [] };
      files.push(cur);
    } else {
      if (!cur) {
        cur = { file: "", lines: [] };
        files.push(cur);
      }
      cur.lines.push(line);
    }
  });
  return files;
}

/** Diff view mode for the Diff tab: "split" (side-by-side, IDE-style) or
 * "unified". Persisted per browser; split is the default. */
export type DiffMode = "split" | "unified";
/** Diff-tab baseline: "fork" = everything vs the base branch's fork point
 * (matches the header badge), "head" = uncommitted only. */
export type DiffBase = "fork" | "head";

export function getDiffMode(): DiffMode {
  let m = "split";
  try {
    m = localStorage.getItem("cs_diffmode") || "split";
  } catch {
    /* storage unavailable */
  }
  return m === "unified" || m === "split" ? m : "split";
}

export function setDiffMode(m: DiffMode): void {
  try {
    localStorage.setItem("cs_diffmode", m);
  } catch {
    /* storage unavailable */
  }
}

export function getDiffBase(): DiffBase {
  let b = "fork";
  try {
    b = localStorage.getItem("mf_diffbase") || "fork";
  } catch {
    /* storage unavailable */
  }
  return b === "fork" || b === "head" ? b : "fork";
}

export function setDiffBase(b: DiffBase): void {
  try {
    localStorage.setItem("mf_diffbase", b);
  } catch {
    /* storage unavailable */
  }
}
