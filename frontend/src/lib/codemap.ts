/** Code map — the pure half of the Map tab (components/grid/CodeMapTab.tsx
 * and its codemap/ parts).
 *
 * Everything here is data in, data out: no DOM, no fetch, no clock reads (the
 * caller passes `now`). That is what lets the zone classifier, the blast radius and the feed interpretation be pinned by
 * vitest, and it keeps the per-pane cost honest — up to nine panes can each
 * mount a Map, so anything expensive has to be something a caller can memoize
 * on its inputs.
 *
 * Paths are worktree-relative POSIX paths, exactly as the server's /code-map,
 * /code-map/live routes send them. The tree itself lives in lib/codetree/. */

import type { ChangedFile, CompanionRule, FeedRecord, PlanItem, RedZone } from "../api/types";

/** One file of the snapshot: `[rel, size, flags]`. */
export type FileEntry = [string, number, number];

/** Server flag bits (backend/web/core/code_map.list_files). */
export const F_IGNORED = 1;
export const F_TEST = 2;

function dirname(p: string): string {
  const i = p.lastIndexOf("/");
  return i < 0 ? "" : p.slice(0, i);
}

function basename(p: string): string {
  const i = p.lastIndexOf("/");
  return i < 0 ? p : p.slice(i + 1);
}

// --- Import graph ----------------------------------------------------------

/** Reverse adjacency in CSR form: importers of `i` are
 * `adj[off[i] .. off[i+1])`. Typed arrays because a 40k-file repo can carry
 * hundreds of thousands of edges and nine panes may hold one each. */
export interface RevIndex {
  n: number;
  off: Int32Array;
  adj: Int32Array;
}

/** `edges` are `[src, dst]` = "src imports dst"; the blast radius walks them
 * backwards (who depends on what changed). */
export function reverseIndex(edges: Array<[number, number]>, n: number): RevIndex {
  const count = new Int32Array(n + 1);
  let m = 0;
  for (const e of edges) {
    const s = e[0],
      d = e[1];
    if (s === d || s < 0 || d < 0 || s >= n || d >= n) continue;
    count[d + 1]++;
    m++;
  }
  for (let i = 0; i < n; i++) count[i + 1] += count[i];
  const off = Int32Array.from(count);
  const fill = Int32Array.from(count);
  const adj = new Int32Array(m);
  for (const e of edges) {
    const s = e[0],
      d = e[1];
    if (s === d || s < 0 || d < 0 || s >= n || d >= n) continue;
    adj[fill[d]++] = s;
  }
  return { n, off, adj };
}

export function importersOf(rev: RevIndex, i: number): Int32Array {
  if (i < 0 || i >= rev.n) return new Int32Array(0);
  return rev.adj.subarray(rev.off[i], rev.off[i + 1]);
}

/** Importers of the seeds, up to `depth` hops: `{idx: hops}` (1..depth).
 * Seeds themselves are never in the result. */
export function blastFrom(seeds: number[], rev: RevIndex, depth: number): Map<number, number> {
  const out = new Map<number, number>();
  const seen = new Set<number>();
  let frontier: number[] = [];
  for (const s of seeds) {
    if (s >= 0 && s < rev.n && !seen.has(s)) {
      seen.add(s);
      frontier.push(s);
    }
  }
  const max = Math.max(0, Math.min(3, depth | 0));
  for (let d = 1; d <= max && frontier.length; d++) {
    const next: number[] = [];
    for (const f of frontier) {
      const imp = importersOf(rev, f);
      for (let k = 0; k < imp.length; k++) {
        const t = imp[k];
        if (seen.has(t)) continue;
        seen.add(t);
        out.set(t, d);
        next.push(t);
      }
    }
    frontier = next;
  }
  return out;
}

// --- Live feed -------------------------------------------------------------

/** A bash command still running after this long gets the pulsing ring; a
 * shorter one would only flicker. */
export const RUNNING_AFTER_S = 1.5;
/** A pre with no post/fail this old is presumed lost, not running. Only the
 * backstop: a call refused outside MindFlock (the permission prompt answered
 * No, a permissions.deny rule, another hook's deny), a hook reload mid-command
 * and a killed agent all leave a pre that never gets a Post*, and the activity
 * gate and the "agent moved on" rule in feedState catch almost all of them
 * long before this. */
export const RUNNING_STALE_S = 10 * 60;

/** How long an agent touch keeps a card's activity dot: an edit 90 s, a
 * read 60 s (then it is history — the Activity list keeps it). */
export const EDIT_FADE_S = 90;
export const READ_FADE_S = 60;

export interface FileLive {
  lastTs: number;
  kind: "edit" | "read";
  running: boolean;
  /** ts of the newest denied attempt on this path (0 = none). */
  deniedTs: number;
  /** ts of the newest feed record that flagged a breach here (the Bash
   * backstop's stat-diff), 0 = none. A transient cue that fades like a deny —
   * never "breached now": the file may have been reverted since, and the
   * server's live.breaches is the only authority for the current set. */
  breachTs: number;
  agent: string;
}

export interface RunningCmd {
  id: string;
  cmd: string;
  ts: number;
  agent: string;
}

export interface FeedState {
  files: Map<string, FileLive>;
  running: RunningCmd[];
  /** Newest edit (post of an edit tool, or a bash write) — "work started". */
  lastEditTs: number;
}

/** Interpret the tool feed as per-file state at `now` (epoch seconds, server
 * clock). A pre is resolved by a post or fail with the same id; a deny is
 * terminal on its own (a denied tool fires no Post*).
 *
 * "Running" needs positive evidence, because an open pre is also what a call
 * refused outside MindFlock leaves behind: nothing runs unless `activity` (the
 * session's, from the same poll) is "working" — after a refusal the turn waits
 * on the user — and an open bash pre stops counting once the same agent
 * (main or subagent) has since STARTED another call that has FINISHED, i.e. the
 * agent moved on past it. A later pre alone proves nothing: parallel sibling
 * calls start together. */
export function feedState(records: FeedRecord[], now: number, activity: string): FeedState {
  const files = new Map<string, FileLive>();
  const recs = records.slice().sort((a, b) => (a.ts || 0) - (b.ts || 0));
  const closed = new Set<string>();
  const started = new Map<string, { ts: number; agent: string }>();
  for (const r of recs) {
    if (!r.id) continue;
    if (r.ev === "post" || r.ev === "fail") closed.add(r.id);
    else if (r.ev === "pre" && !started.has(r.id)) started.set(r.id, { ts: r.ts || 0, agent: r.agent || "" });
  }
  // Per agent: when the newest finished call started. A call whose pre fell
  // out of the feed started before everything still in it, so it can't count.
  const movedOn = new Map<string, number>();
  for (const id of closed) {
    const st = started.get(id);
    if (st && st.ts > (movedOn.get(st.agent) ?? -Infinity)) movedOn.set(st.agent, st.ts);
  }
  const working = activity === "working";

  const touch = (path: string, ts: number, kind: "edit" | "read", agent: string): FileLive => {
    let f = files.get(path);
    if (!f) {
      f = { lastTs: 0, kind, running: false, deniedTs: 0, breachTs: 0, agent: "" };
      files.set(path, f);
    }
    if (ts >= f.lastTs) {
      // An edit outranks a read that lands in the same instant.
      if (kind === "edit" || f.kind !== "edit" || ts > f.lastTs) f.kind = kind;
      f.lastTs = ts;
      f.agent = agent;
    }
    return f;
  };

  let lastEditTs = 0;
  const running: RunningCmd[] = [];
  for (const r of recs) {
    const ts = r.ts || 0;
    const agent = r.agent || "";
    if (r.deny) {
      // Terminal: the tool never ran. A push refusal is about the branch (it
      // carries a breach), not an edit to the file named, so no burst there.
      const p = r.deny.path;
      if (p && !r.deny.push) {
        let f = files.get(p);
        if (!f) {
          f = { lastTs: 0, kind: "edit", running: false, deniedTs: 0, breachTs: 0, agent };
          files.set(p, f);
        }
        f.deniedTs = Math.max(f.deniedTs, ts);
      }
      continue;
    }
    for (const b of r.breach || []) {
      if (!b || !b.path) continue;
      const f = touch(b.path, ts, "edit", agent);
      f.breachTs = Math.max(f.breachTs, ts);
    }
    if (r.ev === "pre") {
      const open = !!r.id && !closed.has(r.id);
      if (r.kind === "bash" && open) {
        const age = now - ts;
        const passed = (movedOn.get(agent) ?? -Infinity) > ts;
        if (working && !passed && age > RUNNING_AFTER_S && age < RUNNING_STALE_S) {
          running.push({ id: r.id || "", cmd: r.cmd || "", ts, agent });
          for (const p of r.writes || []) touch(p, ts, "edit", agent).running = true;
          for (const p of r.reads || []) touch(p, ts, "read", agent).running = true;
        }
        continue;
      }
      // Reads light up at pre time (the post adds nothing); edits wait for the
      // post, which is the moment the file actually changed.
      if (r.kind === "read") for (const p of r.reads || []) touch(p, ts, "read", agent);
      continue;
    }
    if (r.ev === "post") {
      for (const p of r.writes || []) {
        touch(p, ts, "edit", agent);
        lastEditTs = Math.max(lastEditTs, ts);
      }
      if (r.kind !== "edit") for (const p of r.reads || []) touch(p, ts, "read", agent);
      continue;
    }
    // ev === "fail": terminal, nothing changed on disk as far as we know.
  }
  return { files, running, lastEditTs };
}

/** The files breached NOW: the server's live.breaches (working tree and
 * zone-matched git-ignored files, committed or not) and nothing else. A feed
 * record's `breach` is history — it stays in the feed after the agent reverts
 * the file — so it may only ever be a fading cue (FileLive.breachTs), never
 * part of this set, which drives the Breaches list ("pushing is blocked") and
 * the standing breach frame. */
export function currentBreaches(breaches: Array<{ path: string }> | null | undefined): Set<string> {
  const out = new Set<string>();
  for (const b of breaches || []) if (b && b.path) out.add(b.path);
  return out;
}

/** When to re-read the snapshot (GET /code-map) after a live poll: there is
 * none yet; the live fingerprint moved; every `nullFpMs` when the server can't
 * fingerprint the worktree; and every `partialMs` while the held import graph
 * is partial. A graph build cut short by its time budget resumes only when
 * asked again, and during plan review an idle agent moves no fingerprint — so
 * without the last rule the blast radius stays partial until the next edit. */
export function snapStale(o: {
  snap: { fingerprint: string | null; graph_partial?: boolean } | null;
  liveFp: string | null | undefined;
  sinceSnapMs: number;
  nullFpMs: number;
  partialMs: number;
}): boolean {
  const snap = o.snap;
  if (!snap) return true;
  if (o.liveFp && o.liveFp !== snap.fingerprint) return true;
  if (!o.liveFp && o.sinceSnapMs > o.nullFpMs) return true;
  return !!snap.graph_partial && o.sinceSnapMs > o.partialMs;
}

// --- Heat, zones, plan -----------------------------------------------------

/** 0..1 by log(added+removed), against a floor so one two-line change is not
 * drawn as hot as a rewrite. A delete or an add with no countable lines
 * (binary) still reads as a change. */
export function heat(changed: ChangedFile[]): Map<string, number> {
  const out = new Map<string, number>();
  let max = 0;
  for (const c of changed) max = Math.max(max, (c.added || 0) + (c.removed || 0));
  const den = Math.log1p(Math.max(max, 40));
  for (const c of changed) {
    const n = (c.added || 0) + (c.removed || 0);
    const h = n > 0 ? Math.log1p(n) / den : 0.25;
    out.set(c.path, Math.max(0.12, Math.min(1, h)));
  }
  return out;
}

export interface ZoneMatcher {
  zone: RedZone;
  rx: RegExp;
}

/** Compile the server's regex sources (red_zones.compile_pattern emits a
 * source valid in both Python and JS). A source this engine rejects is
 * skipped rather than thrown — one bad zone must not blank the map. */
export function zoneMatchers(zones: RedZone[], ci = false): ZoneMatcher[] {
  const out: ZoneMatcher[] = [];
  for (const z of zones || []) {
    if (!z || !z.re) continue;
    try {
      out.push({ zone: z, rx: new RegExp(z.re, ci ? "i" : "") });
    } catch {
      /* not a JS-compatible source — leave it off the map */
    }
  }
  return out;
}

/** The zone governing `path`: an enforced one first, else a waived one (drawn
 * faintly), else null. */
export function zoneFor(ms: ZoneMatcher[], path: string): RedZone | null {
  let waived: RedZone | null = null;
  for (const m of ms) {
    if (!m.rx.test(path)) continue;
    if (!m.zone.waived) return m.zone;
    if (!waived) waived = m.zone;
  }
  return waived;
}

export function offPlanSet(offPlan: string[] | null | undefined): Set<string> {
  return new Set(offPlan || []);
}

export type PlanStatus = "done" | "untouched" | "new";

/** Per plan item: touched yet? Mid-flight a plan item is "done" once the file
 * is changed on the branch or an edit landed on it. */
export function planProgress(items: PlanItem[], changed: Set<string>, edited: Set<string>): Map<string, PlanStatus> {
  const out = new Map<string, PlanStatus>();
  for (const it of items) {
    if (changed.has(it.path) || edited.has(it.path)) out.set(it.path, "done");
    else out.set(it.path, it.new ? "new" : "untouched");
  }
  return out;
}

/** Plan while a plan exists and nothing has been edited since it; else Watch. */
export function autoMode(plan: { items?: PlanItem[]; ts?: number | null } | null | undefined, lastEditTs: number): "plan" | "watch" {
  if (!plan || !plan.items || !plan.items.length) return "watch";
  return lastEditTs > (plan.ts || 0) ? "watch" : "plan";
}

/** The filter box → a red-zone pattern. A plain word is a substring match at
 * any depth (`athena` → `*athena*`); anything already glob-shaped or
 * path-shaped is taken as written. */
export function parseFilterToPattern(text: string): string {
  let t = String(text || "").trim().replace(/\\/g, "/");
  if (!t) return "";
  if (t.startsWith("./")) t = t.slice(2);
  if (/[*?[\]]/.test(t) || t.includes("/")) return t;
  return "*" + t + "*";
}

/** The pattern the find box previews. A red-zone row clicked into the box
 * previews that zone's OWN pattern: routed through the free-text rule, bare
 * `athena` would widen to `*athena*` and highlight (and offer to red-zone)
 * `athenaeum.py` and `docs/athena_notes.md`, which the zone doesn't cover.
 * Typing anything else drops back to the free-text rule. */
export function findPattern(filter: string, zone: { pattern: string } | null | undefined): string {
  if (zone && zone.pattern && filter === zone.pattern) return zone.pattern;
  return parseFilterToPattern(filter);
}

/** A path picked on the map (a tile, an arm's folder, a selection chip) as a
 * zone pattern that means exactly that path. A path with an interior slash is
 * already anchored at the repo root; a root-level name is not — bare
 * `package.json` would red-zone every package.json at any depth — so it gets
 * the leading `/` that anchors it. Typed patterns never pass through here. */
export function anchoredZonePath(path: string): string {
  const p = globEscape(String(path || "").replace(/\/+$/, ""));
  if (!p || p.startsWith("/") || p.includes("/")) return p;
  return "/" + p;
}

/** A glob meaning exactly the literal path: each glob metacharacter becomes a
 * one-character class — the twin of red_zones.glob_escape, so zoning
 * `app/[slug]/page.tsx` from the map protects that file, not `app/s/page.tsx`. */
export function globEscape(rel: string): string {
  return String(rel || "").replace(/[[\]*?]/g, (c) => "[" + c + "]");
}

export interface GuardPill {
  cls: string;
  label: string;
  title: string;
}

const PILL_BASE = (s: string) => s.replace(/\s*\([^)]*\)\s*$/, "").trim().toLowerCase();

/** Short list of names for a label: "a, b +2". */
export function nameList(names: string[], max = 2): string {
  const u = Array.from(new Set(names.filter(Boolean)));
  if (u.length <= max) return u.join(", ");
  return u.slice(0, max).join(", ") + ` +${u.length - max}`;
}

/** The toolbar's guard pill. The LABEL follows `guard.state` in our own words;
 * the TOOLTIP is the server's `detail` sentence (it knows why — which CLI has
 * no hook, what went missing), falling back to our explanation when `detail`
 * is empty or merely repeats the label (older servers sent the label there,
 * which left the explanation unreachable). No guard yet (before the first
 * poll): enforced zones read as guarded, none as "No zones".
 *
 * Green mode (any enforced green zone, `green` = their names) takes over the
 * label — "✓ Only here: providers" is what the user most needs to see — and
 * keeps the state in the class and the tooltip, so a guard that is off or
 * detect-only still reads amber/red and says why. */
export function guardPill(
  guard: { state?: string; detail?: string } | null | undefined,
  zonesEnforced: number,
  provider: string,
  green: string[] = []
): GuardPill {
  const state = guard?.state || (zonesEnforced ? "guarded" : "none");
  let cls: string, label: string, explain: string;
  switch (state) {
    case "guarded":
      cls = "g-guarded";
      label = "Guarded";
      explain = "Edits to red zones are blocked before they happen (hook guard armed).";
      break;
    case "arming":
      cls = "g-arming";
      label = "Arming…";
      explain = "The guard is installed; it proves itself on the agent's next tool call.";
      break;
    case "detect":
      cls = "g-detect";
      label = `Detect-only${provider ? " (" + provider + ")" : ""}`;
      explain =
        "This agent CLI has no hook guard MindFlock manages: red-zone edits are detected, flagged and block pushes, but not prevented.";
      break;
    case "off":
      cls = "g-off";
      label = "Guard off — re-arming";
      explain = "The guard's hooks went missing; MindFlock is reinstalling them.";
      break;
    default:
      cls = "g-none";
      label = "No zones";
      explain = "Nothing is off-limits in this repo yet.";
  }
  const d = String(guard?.detail || "").trim();
  if (green.length) {
    const g = nameList(green);
    const how =
      state === "detect"
        ? " Edits outside are detected, flagged and block pushes, but not prevented (" + (provider || "this agent") + " has no hook guard)."
        : state === "off"
          ? " The guard's hooks went missing; MindFlock is reinstalling them."
          : state === "arming"
            ? " The guard takes effect on the agent's next tool call."
            : " Edits outside are blocked before they happen.";
    const base = `Agents may only change files inside ${g}. Everything else is read-only.` + how;
    return {
      cls: "g-green" + (state === "guarded" || state === "none" ? "" : " " + cls),
      label: "✓ Only here: " + g,
      title: d && PILL_BASE(d) !== PILL_BASE(label) ? d : base,
    };
  }
  const title = d && PILL_BASE(d) !== PILL_BASE(label) ? d : explain;
  return { cls, label, title };
}

// --- Zones v3: one classifier (mirrors red_zones.classify) -----------------

export type ZoneClass = "blocked" | "outside" | "companion" | "ok";

export function isGreen(z: Pick<RedZone, "kind"> | null | undefined): boolean {
  return !!z && z.kind === "green";
}

/** A zone-doc entry as the server and the shared fixture carry it: a glob
 * pattern string, or a zone/companion object with a compiled `re` (or at
 * least a `pattern`). */
export type ZoneEntry = string | { re?: string | null; pattern?: string | null };

/** Compiled zones for classifyPath: enforced (non-waived) red and green
 * matchers plus the companion rules. Build once per poll (memoize on the
 * zones array) — classifyPath runs per card and per file. */
export interface ZoneDoc {
  red: RegExp[];
  green: RegExp[];
  greenZones: RedZone[];
  companions: RegExp[];
  ci: boolean;
}

// --- Glob → regex (mirrors red_zones.normalize_pattern / compile_pattern) ---

const REGEX_SPECIAL = new Set(".^$*+?()[]{}|\\/".split(""));

function translateGlob(body: string): string {
  const out: string[] = [];
  let i = 0;
  const n = body.length;
  while (i < n) {
    const c = body[i];
    if (c === "*") {
      if (body[i + 1] === "*") {
        // gitignore: a whole `**/` segment spans zero or more directories.
        if (body[i + 2] === "/" && (i === 0 || body[i - 1] === "/")) {
          out.push("(?:.*/)?");
          i += 3;
          continue;
        }
        out.push(".*");
        i += 2;
        continue;
      }
      out.push("[^/]*");
      i++;
      continue;
    }
    if (c === "?") {
      out.push("[^/]");
      i++;
      continue;
    }
    if (c === "[") {
      let j = i + 1;
      if (j < n && (body[j] === "!" || body[j] === "^")) j++;
      if (j < n && body[j] === "]") j++;
      while (j < n && body[j] !== "]") j++;
      if (j >= n) {
        out.push("\\[");
        i++;
        continue;
      }
      let inner = body.slice(i + 1, j);
      const neg = inner.startsWith("!") || inner.startsWith("^");
      if (neg) inner = inner.slice(1);
      // Mirrors red_zones._translate_glob byte for byte: escape backslashes
      // and literal '[' members, and a LEADING ']' (JS reads `[]` as an
      // empty class and `[^]` as "any char"; Python reads it as a member).
      inner = inner.replace(/\\/g, "\\\\").replace(/\[/g, "\\[");
      if (inner.startsWith("]")) inner = "\\" + inner;
      out.push("[" + (neg ? "^" : "") + inner + "]");
      i = j + 1;
      continue;
    }
    out.push(REGEX_SPECIAL.has(c) ? "\\" + c : c);
    i++;
  }
  return out.join("");
}

/** A user glob → the same anchored regex SOURCE the server's
 * `red_zones.compile_pattern` emits (the shared pattern fixture pins the
 * behaviour), or null for a pattern the server would reject. Matches the path
 * itself and everything beneath it; a basename matches at any depth unless a
 * leading `/` anchors it. */
export function compilePattern(p: string | null | undefined): string | null {
  let s = String(p ?? "").trim().replace(/\\/g, "/");
  if (s.startsWith("./")) s = s.slice(2);
  let anchored = false;
  if (s.startsWith("/")) {
    anchored = true;
    s = s.replace(/^\/+/, "");
  }
  if (!s || s.includes("\u0000") || s.length > 400) return null;
  if (s.startsWith("~") || (s.length >= 2 && s[1] === ":")) return null;
  if (s.split("/").some((seg) => seg === "..")) return null;
  const body = s.replace(/\/+$/, "");
  const anyDepth = !body.includes("/") && !anchored;
  return (anyDepth ? "^(?:.*/)?" : "^") + translateGlob(body) + "(?:/.*)?$";
}

/** A regex source matching exactly `rel` (a companion FILE, whose name may
 * hold glob characters). Mirrors red_zones.exact_re. */
export function exactRe(rel: string): string {
  return "^" + Array.from(rel, (c) => (REGEX_SPECIAL.has(c) ? "\\" + c : c)).join("") + "$";
}

function rx(src: string, ci: boolean): RegExp | null {
  try {
    return new RegExp(src, ci ? "i" : "");
  } catch {
    return null;
  }
}

function entryRx(e: ZoneEntry | null | undefined, ci: boolean): RegExp | null {
  if (!e) return null;
  if (typeof e === "string") {
    const src = compilePattern(e);
    return src ? rx(src, ci) : null;
  }
  if (e.re) return rx(String(e.re), ci);
  if (e.pattern) return entryRx(String(e.pattern), ci);
  return null;
}

/** The classify document from raw entries — `{red, green, companions}` as
 * `red_zones.zones_doc` / the shared fixture carry them. Unusable entries
 * are skipped (one bad zone must not blank the map). */
export function zoneDocFrom(
  d: { red?: ZoneEntry[] | null; green?: ZoneEntry[] | null; companions?: ZoneEntry[] | null },
  ci = false
): ZoneDoc {
  const doc: ZoneDoc = { red: [], green: [], greenZones: [], companions: [], ci };
  for (const e of d.red || []) {
    const r = entryRx(e, ci);
    if (r) doc.red.push(r);
  }
  for (const e of d.green || []) {
    const r = entryRx(e, ci);
    if (r) doc.green.push(r);
  }
  for (const e of d.companions || []) {
    const r = entryRx(e, ci);
    if (r) doc.companions.push(r);
  }
  return doc;
}

/** The classify document for the Map: the live zones (enforced only — a
 * waived zone is drawn, not enforced), the live companion rules, and the exact
 * companion files. */
export function zoneDoc(
  zones: RedZone[] | null | undefined,
  companions: Array<CompanionRule | string> | null | undefined,
  companionFiles: string[] | null | undefined,
  ci = false
): ZoneDoc {
  const red: ZoneEntry[] = [];
  const green: ZoneEntry[] = [];
  const greenZones: RedZone[] = [];
  for (const z of zones || []) {
    if (!z || z.waived || (!z.re && !z.pattern)) continue;
    if (isGreen(z)) {
      green.push(z);
      greenZones.push(z);
    } else red.push(z);
  }
  const comps: ZoneEntry[] = (companions || []).slice();
  for (const f of companionFiles || []) if (f) comps.push({ re: exactRe(f) });
  const doc = zoneDocFrom({ red, green, companions: comps }, ci);
  // Keep only the green zones that compiled: the banner must not name a
  // zone the classifier can't see.
  doc.greenZones = greenZones.filter((z) => entryRx(z, ci));
  return doc;
}

/** MindFlock's own workspace artifacts at the worktree root (.mindflock_*):
 * always writable, whatever the green zones say (the verify step writes one). */
const ARTIFACT_RE = /^\.mindflock_[^/]*(?:\/.*)?$/;
/** A nested sandbox worktree (Claude Code's `.claude/worktrees/<n>/`). */
const NESTED_WT_RE = /^\.claude\/worktrees\/[^/]+(?:\/(.*))?$/;

function anyMatch(rs: RegExp[], p: string): boolean {
  for (const r of rs) if (r.test(p)) return true;
  return false;
}

/** "ok" / "companion" / "outside" for ONE representation while green zones
 * exist. The nested-sandbox dir itself is bookkeeping; a path inside it is
 * judged by its stripped twin (the one any-of allowance). */
function greenOne(doc: ZoneDoc, rel: string): ZoneClass {
  if (anyMatch(doc.green, rel) || ARTIFACT_RE.test(rel)) return "ok";
  const m = NESTED_WT_RE.exec(rel);
  let inner: string | null = null;
  if (m) {
    inner = m[1] || "";
    if (!inner) return "ok";
    if (anyMatch(doc.green, inner) || ARTIFACT_RE.test(inner)) return "ok";
  }
  if (anyMatch(doc.companions, rel) || (inner && anyMatch(doc.companions, inner))) return "companion";
  return "outside";
}

/** The ONE zone predicate, mirrored from `red_zones.classify(rel_real,
 * rel_lex, zones_doc, ci)` (the hook source carries a third copy; the shared
 * fixture tests/fixtures/zone_classify_cases.json pins all three): every
 * consumer — the guard, the monitor, the push gate, the live breaches, the
 * preview, the plan flags and the tree's dusk — must agree, or the map says
 * "fine" where the guard says "blocked". Order:
 *   1. any representation (real, lexical, each `.claude/worktrees/<n>/`-
 *      stripped) matches an enforced red zone → "blocked" (red always wins);
 *   2. no enforced green zone → "ok";
 *   3. EVERY representation must be writable — any "outside" → "outside",
 *      else any "companion" → "companion" (writable, amber, never a breach),
 *      else "ok" (green match, a `.mindflock_*` artifact, the sandbox dir).
 * Green is decided per representation, so a symlink inside the zone that
 * points outside it is outside. `relLex` is only passed where it differs. */
export function classifyPath(relReal: string, doc: ZoneDoc, relLex?: string | null): ZoneClass {
  const cands: string[] = [String(relReal ?? "")];
  if (relLex != null && relLex !== cands[0]) cands.push(relLex);
  if (doc.red.length) {
    for (const c of cands) {
      if (anyMatch(doc.red, c)) return "blocked";
      const m = NESTED_WT_RE.exec(c);
      if (m && m[1] && anyMatch(doc.red, m[1])) return "blocked";
    }
  }
  if (!doc.green.length) return "ok";
  const verdicts = cands.map((c) => greenOne(doc, c));
  if (verdicts.includes("outside")) return "outside";
  if (verdicts.includes("companion")) return "companion";
  return "ok";
}

// --- Paths ------------------------------------------------------------------

/** `p` is `dir` or lies under it ("" contains everything). */
export function underPath(p: string, dir: string): boolean {
  if (!dir) return true;
  return p === dir || p.startsWith(dir + "/");
}

/** The node of a level that holds `path` (nodes of one level are disjoint
 * path prefixes: a collapsed dir chain, a dir, or a file), or null. */
export function nodeOf(path: string, nodePaths: Set<string>): string | null {
  let p = path;
  while (p) {
    if (nodePaths.has(p)) return p;
    p = dirname(p);
  }
  return null;
}

export const KIND_GLYPH: Record<string, string> = {
  class: "C",
  struct: "S",
  interface: "I",
  trait: "T",
  enum: "E",
  type: "T",
  function: "ƒ",
  method: "ƒ",
  const: "K",
  module: "M",
  field: "·",
  route: "→",
  variable: "V",
  output: "O",
};

/** A path is a test by folder or file name (the usual conventions, all
 * languages) — used to split "used by" into code and "+N test files". */
export function isTestPath(p: string): boolean {
  const s = String(p || "");
  if (/(^|\/)(tests?[\w-]*|__tests__|testdata|fixtures|spec)(\/|$)/.test(s)) return true;
  const b = basename(s);
  return /^test_.*\.(py|c)$|_test\.(py|go|rs)$|\.(test|spec)\.[\w]+$|Tests?\.(java|kt|cs)$|Spec\.kt$/.test(b);
}

// --- Blast radius as a list --------------------------------------------------

export interface BlastRow {
  /** The level node the dependents sit in, or a top-level folder outside it. */
  path: string;
  count: number;
  tests: number;
  hops: number;
  /** Outside the level on screen: listed, can't be highlighted here. */
  outside: boolean;
  paths: string[];
}

const BLAST_PATHS_CAP = 200;

/** The blast radius (dependents → hops) grouped by the level's nodes,
 * counting each dependent once; tests counted apart (half the suite imports a
 * core module — listing it buries the dependents that matter). */
/** A grouping level: the folders the rows group by (null = group by each
 * dependent's own folder). */
export interface BlastLevel {
  path: string;
  nodes: Array<{ path: string; kind: string }>;
}

export function blastRows(dependents: Map<string, number>, level: BlastLevel | null, isTest: (p: string) => boolean): BlastRow[] {
  const nodePaths = new Set((level?.nodes || []).filter((n) => n.kind !== "more").map((n) => n.path));
  const lp = level?.path || "";
  const acc = new Map<string, BlastRow>();
  for (const [p, hops] of dependents) {
    let key = nodeOf(p, nodePaths);
    let outside = false;
    if (key === null) {
      // grouped by folder when no level is on screen (the tree): nothing is "elsewhere"
      outside = !!level;
      key = lp && !underPath(p, lp) ? p.split("/")[0] : dirname(p) || p;
    }
    let r = acc.get(key);
    if (!r) {
      r = { path: key, count: 0, tests: 0, hops, outside, paths: [] };
      acc.set(key, r);
    }
    r.hops = Math.min(r.hops, hops);
    if (isTest(p)) r.tests++;
    else {
      r.count++;
      if (r.paths.length < BLAST_PATHS_CAP) r.paths.push(p);
    }
  }
  return Array.from(acc.values()).sort(
    (a, b) => Number(a.outside) - Number(b.outside) || b.count - a.count || b.tests - a.tests || (a.path < b.path ? -1 : 1)
  );
}

// --- Scope requests + peeks (green mode, from the feed) ----------------------

export interface ScopeRequest {
  path: string;
  ts: number;
  reason: string;
  agent: string;
}

/** A green deny: the guard refused an edit outside the green zone(s). */
export function isGreenDeny(r: FeedRecord): boolean {
  const d = r.deny;
  if (!d || d.push || !d.path) return false;
  return d.kind === "green" || d.pattern === "outside green";
}

/** Each green deny is a scope request — "the agent wanted this file" — with an
 * [Allow this file] action. Newest first, one per path, and only while the path
 * is still outside (allowing it, or dropping the green zone, answers it). */
export function scopeRequests(feed: FeedRecord[], stillOutside: (p: string) => boolean): ScopeRequest[] {
  const seen = new Set<string>();
  const out: ScopeRequest[] = [];
  for (let i = feed.length - 1; i >= 0; i--) {
    const r = feed[i];
    if (!isGreenDeny(r)) continue;
    const p = r.deny!.path;
    if (seen.has(p)) continue;
    seen.add(p);
    if (!stillOutside(p)) continue;
    out.push({ path: p, ts: r.ts || 0, reason: r.deny!.reason || "", agent: r.agent || "" });
  }
  return out;
}

/** Reads outside the green zone(s) in one record: the server's `peek` mark
 * when it sent one, else the record's reads classified here. Advisory only —
 * reads are never blocked (shell reads can't be, and blinding the agent to the
 * callers of what it edits breaks things). */
export function peeksOf(r: FeedRecord, outside: (p: string) => boolean): string[] {
  if (Array.isArray(r.peek)) return r.peek.filter(Boolean);
  if (r.deny || r.ev === "fail") return [];
  if (r.kind !== "read" && r.kind !== "bash") return [];
  return (r.reads || []).filter((p) => p && outside(p));
}

/** The anchored green patterns "Go — only the planned files" asks for: each
 * existing planned file exactly, and a NEW file's parent folder (the agent has
 * to be able to create it). Mirrors the server's scope_to_plan. */
export function planScope(items: PlanItem[]): string[] {
  const out = new Set<string>();
  for (const it of items) {
    if (!it || !it.path) continue;
    if (it.new) {
      const d = dirname(it.path);
      out.add(d ? anchoredZonePath(d) : anchoredZonePath(it.path));
    } else out.add(anchoredZonePath(it.path));
  }
  return Array.from(out);
}

/** Exempt paths from the live payload, whichever shape it came in. */
export function exemptSet(ex: Record<string, string> | string[] | null | undefined): Set<string> {
  if (!ex) return new Set();
  return new Set(Array.isArray(ex) ? ex : Object.keys(ex));
}

/** tell_agent for a zone add: never while the agent waits on a prompt — the
 * "Tell the agent" checkbox then shows unchecked and disabled, and the request
 * must say what the checkbox says. */
export function zoneAddTell(tell: boolean, override: boolean | undefined, clarify: boolean): boolean {
  return (override ?? tell) && !clarify;
}

/** The Allow buttons' states as shown: "done" holds only while the path is
 * still allowed — once it is outside the green zones again (its zone removed),
 * a repeat scope request must be allowable with one click, not stuck on a
 * disabled "Allowed ✓". Returns `reqBusy` itself when nothing changes. */
export function liveAllowState(reqBusy: Record<string, string>, stillOutside: (p: string) => boolean): Record<string, string> {
  let out = reqBusy;
  for (const [p, st] of Object.entries(reqBusy)) {
    if (st !== "done" || !stillOutside(p)) continue;
    if (out === reqBusy) out = { ...reqBusy };
    delete out[p];
  }
  return out;
}

/** How many sessions share `title`'s worktree (its `folder`, the worktree
 * once started) — a green zone applies to every one of them. `path` is the
 * repo root, shared by every session on the repo, so it must not be the key. */
export function sessionsOnWorktree(list: Array<{ title: string; folder?: string | null }>, title: string): number {
  const norm = (f: string | null | undefined) => String(f || "").replace(/\/+$/, "");
  const me = list.find((i) => i.title === title);
  const f = norm(me?.folder);
  if (!f) return 1;
  return Math.max(1, list.filter((i) => norm(i.folder) === f).length);
}



/** Files that are tests for real: flagged test by name (snapshot flag 2)
 * MINUS any such file that non-test code imports — the twin of
 * code_map.effective_tests, so `test_plans.py` (a production module) counts
 * as code in the blast list exactly as it does on the tree and in the guard. */
export function effectiveTests(snap: { files?: Array<[string, number, number?]> | any[]; edges?: Array<[number, number]> }): Set<number> {
  const files = snap.files || [];
  const flagged = new Set<number>();
  files.forEach((f: any, i: number) => {
    if (((f && f[2]) || 0) & 2) flagged.add(i);
  });
  const importedByCode = new Set<number>();
  for (const e of snap.edges || []) {
    if (!flagged.has(e[0])) importedByCode.add(e[1]);
  }
  for (const i of importedByCode) flagged.delete(i);
  return flagged;
}
