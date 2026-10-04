/** Code tree — subagents as their own birds (pure).
 *
 * A Claude Code subagent's tool calls reach the hook with the parent's session
 * plus `agent` (its agent_id) and `agent_type`; the parent's own Agent / Task
 * call carries `desc` (its description) and `atype` (its subagent_type). So:
 *   - every record with `agent` belongs to that helper, never to the main bird
 *   - a helper is named after the parent's most recent unmatched Agent call
 *     made before its first record: "<agent_type> · <desc>"
 *   - it works while it has records / its Agent call is open; it is done when
 *     that call's post (or fail) arrives with nothing after it, after
 *     SUB_IDLE_S without a record, or when the session goes idle
 *   - a finished helper stays listed SUB_LINGER_S, then drops off and its
 *     work folds into the parent's. */

import type { FeedRecord } from "../../api/types";

/** No record for this long (seconds) and a helper counts as finished. */
export const SUB_IDLE_S = 120;
/** A finished helper stays listed (card + its own marks) this long. */
export const SUB_LINGER_S = 45;

export interface SubInfo {
  /** the subagent's agent_id */
  id: string;
  /** 1-based, stable for the life of the view */
  index: number;
  atype: string;
  desc: string;
  /** "Explore · Map the config loader" (cards) */
  name: string;
  /** "explore#1" (canvas tags, badges) */
  short: string;
  firstTs: number;
  lastTs: number;
  /** the parent's Agent call it was matched to */
  callId: string | null;
  done: boolean;
  /** server ts it finished (Infinity while working) */
  doneTs: number;
  /** still shown on its own: working, or finished less than SUB_LINGER_S ago */
  listed: boolean;
  recs: FeedRecord[];
}

/** The feed split by who made each call: the main agent, or a subagent. */
export function splitFeed(feed: FeedRecord[]): { main: FeedRecord[]; byAgent: Map<string, FeedRecord[]> } {
  const main: FeedRecord[] = [];
  const byAgent = new Map<string, FeedRecord[]>();
  for (const r of feed) {
    const a = r.agent ? String(r.agent) : "";
    if (!a) {
      main.push(r);
      continue;
    }
    const list = byAgent.get(a);
    if (list) list.push(r);
    else byAgent.set(a, [r]);
  }
  return { main, byAgent };
}

/** "<agent_type> · <desc>", else whichever is known, else "helper N". */
export function subName(atype: string, desc: string, index: number): string {
  const t = atype.trim(),
    d = desc.trim();
  if (t && d) return `${t} · ${d}`;
  return t || d || `helper ${index}`;
}

/** "explore#1": short enough for a tag on the canvas and a badge. */
export function subShort(atype: string, index: number): string {
  const t = atype.trim().toLowerCase().replace(/\s+/g, "-");
  return `${t || "helper"}#${index}`;
}

interface Call {
  id: string;
  ts: number;
  desc: string;
  atype: string;
  closedTs: number | null;
  taken: boolean;
}

/** The subagents in the feed, oldest first. `idx` keeps each helper's index
 * stable across polls (the feed is a sliding window): a new id takes the next
 * number. `activity` is the session's own (idle / offline ⇒ every helper is
 * done). */
export function subagentsOf(feed: FeedRecord[], serverNow: number, activity = "", idx: Map<string, number> = new Map()): SubInfo[] {
  const recs = feed.slice().sort((a, b) => (a.ts || 0) - (b.ts || 0));
  const calls: Call[] = [];
  const callById = new Map<string, Call>();
  const subs = new Map<string, { first: number; last: number; type: string; recs: FeedRecord[] }>();
  for (const r of recs) {
    const ts = r.ts || 0;
    if (r.agent) {
      const id = String(r.agent);
      let s = subs.get(id);
      if (!s) subs.set(id, (s = { first: ts, last: ts, type: "", recs: [] }));
      s.last = Math.max(s.last, ts);
      if (!s.type && r.agent_type) s.type = String(r.agent_type);
      s.recs.push(r);
      continue;
    }
    if (r.kind !== "agent") continue;
    const id = r.id || "";
    let c = id ? callById.get(id) : undefined;
    if (r.ev === "pre") {
      if (!c) {
        c = { id, ts, desc: r.desc || "", atype: r.atype || "", closedTs: null, taken: false };
        calls.push(c);
        if (id) callById.set(id, c);
      }
    } else if (c) c.closedTs = ts;
    else if (id) {
      // a post whose pre scrolled out of the window: still a call that ended
      c = { id, ts: -Infinity, desc: r.desc || "", atype: r.atype || "", closedTs: ts, taken: false };
      calls.push(c);
      callById.set(id, c);
    }
  }
  let next = 1;
  for (const v of idx.values()) next = Math.max(next, v + 1);
  const idle = activity === "idle" || activity === "offline";
  const out: SubInfo[] = [];
  const order = [...subs.entries()].sort((a, b) => a[1].first - b[1].first);
  for (const [id, s] of order) {
    // the parent's most recent Agent call before the helper's first record that no
    // other helper took: one still open then first, the right type first
    const before = calls.filter((c) => !c.taken && c.ts <= s.first);
    const rank = (c: Call) => (c.closedTs === null || c.closedTs >= s.first ? 2 : 0) + (s.type && c.atype === s.type ? 1 : 0);
    let call: Call | null = null;
    for (const c of before) if (!call || rank(c) > rank(call) || (rank(c) === rank(call) && c.ts >= call.ts)) call = c;
    if (call) call.taken = true;
    let index = idx.get(id);
    if (index === undefined) {
      index = next++;
      idx.set(id, index);
    }
    const atype = s.type || (call ? call.atype : "");
    const desc = call ? call.desc : "";
    let doneTs = Infinity;
    if (call && call.closedTs !== null && call.closedTs >= s.last) doneTs = call.closedTs;
    else if (serverNow - s.last > SUB_IDLE_S) doneTs = s.last + SUB_IDLE_S;
    else if (idle) doneTs = s.last;
    const done = doneTs !== Infinity;
    out.push({
      id,
      index,
      atype,
      desc,
      name: subName(atype, desc, index),
      short: subShort(atype, index),
      firstTs: s.first,
      lastTs: s.last,
      callId: call ? call.id : null,
      done,
      doneTs,
      listed: !done || serverNow - doneTs < SUB_LINGER_S,
      recs: s.recs,
    });
  }
  return out;
}

// --- colour -------------------------------------------------------------------

function parseColour(c: string): [number, number, number] | null {
  const s = c.trim();
  let m = /^#([0-9a-f]{6})$/i.exec(s);
  if (m) {
    const x = parseInt(m[1], 16);
    return [x >> 16, (x >> 8) & 255, x & 255];
  }
  m = /^#([0-9a-f]{3})$/i.exec(s);
  if (m) return [0, 1, 2].map((i) => parseInt(m![1][i] + m![1][i], 16)) as [number, number, number];
  m = /^rgba?\(\s*([\d.]+)[\s,]+([\d.]+)[\s,]+([\d.]+)/i.exec(s);
  if (m) return [+m[1], +m[2], +m[3]].map((v) => Math.max(0, Math.min(255, Math.round(v)))) as [number, number, number];
  return null;
}

function toHsl([r, g, b]: [number, number, number]): [number, number, number] {
  const R = r / 255,
    G = g / 255,
    B = b / 255;
  const mx = Math.max(R, G, B),
    mn = Math.min(R, G, B);
  const l = (mx + mn) / 2;
  if (mx === mn) return [0, 0, l];
  const d = mx - mn;
  const s = l > 0.5 ? d / (2 - mx - mn) : d / (mx + mn);
  const h = mx === R ? ((G - B) / d + (G < B ? 6 : 0)) * 60 : mx === G ? ((B - R) / d + 2) * 60 : ((R - G) / d + 4) * 60;
  return [h, s, l];
}

function fromHsl(h: number, s: number, l: number): string {
  const k = (n: number) => (n + h / 30) % 12;
  const a = s * Math.min(l, 1 - l);
  const f = (n: number) => l - a * Math.max(-1, Math.min(k(n) - 3, Math.min(9 - k(n), 1)));
  const hex = (v: number) => Math.round(v * 255).toString(16).padStart(2, "0");
  return "#" + hex(f(0)) + hex(f(8)) + hex(f(4));
}

/** A helper's colour: its parent's, lifted (dark sky) or pressed (light sky)
 * and turned a little round the wheel — the same family, told apart from the
 * parent and from its siblings. (The glyph + index carries identity for
 * colour-blind eyes: ●1, ●2.) Unparseable colours come back unchanged. */
export function subColour(parent: string, index: number, light: boolean): string {
  const rgb = parseColour(parent);
  if (!rgb) return parent;
  const [h, s, l] = toHsl(rgb);
  const k = Math.max(1, index);
  const step = 14 + 9 * Math.floor((k - 1) / 2);
  const dh = (k % 2 ? 1 : -1) * Math.min(40, step);
  const L = light ? Math.max(0.22, Math.min(0.42, l - 0.1 - 0.02 * ((k - 1) % 3))) : Math.max(0.62, Math.min(0.84, l + 0.1 + 0.03 * ((k - 1) % 3)));
  const S = Math.max(0.35, Math.min(0.9, s * 0.92));
  return fromHsl((h + dh + 360) % 360, S, L);
}

/** A helper's card line: "editing x.py", "reading x.py", "blocked at x.py",
 * "done", "thinking". */
export function helperLine(status: string, file: string): string {
  if (status === "done") return "done";
  if (status === "blocked") return file ? `blocked at ${file}` : "blocked";
  if ((status === "reading" || status === "editing" || status === "creating") && file) return `${status} ${file}`;
  if (status === "planning") return "planning";
  return "thinking";
}
