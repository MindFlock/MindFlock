/** todo/model.js — the To-do window's pure logic: no DOM, no fetch, no clock
 * of its own (every time-dependent function takes `now`), so vitest can pin it
 * (frontend/src/__tests__/todoExtension.test.ts).
 *
 * The item shape is the shared contract's (GET /api/todo): one array, open
 * and done mixed, in display order, open items stably sorted by tier
 * (high → normal → low). Priority lives in `priority` ("high" | "low"; absent
 * = normal), the person-facing kind in `kind` ("manual_test" | "needs_you";
 * absent = a plain task). Due times are LOCAL "YYYY-MM-DDTHH:MM" strings, the
 * same format an <input type="datetime-local"> speaks. */

export const TIERS = ["high", "normal", "low"];

export const TIER_LABELS = { high: "High", normal: "Normal", low: "Low" };

export const KIND_LABELS = { manual_test: "Manual test", needs_you: "Needs you" };

/** Where an item came from, when it was not the person. */
export const SOURCE_LABELS = {
  agent: "from agent",
  assistant: "from Assistant",
  verify: "from verify",
  flight: "from route",
  run: "from team run",
};

/** An item's tier: "high" | "normal" | "low" (anything else reads as normal,
 * so a hand-edited file never hides a row). */
export function tierOf(item) {
  const p = item && item.priority;
  return p === "high" || p === "low" ? p : "normal";
}

function tierRank(item) {
  return TIERS.indexOf(tierOf(item));
}

/** True for the items an agent or a route is waiting on the person for. */
export function isNeedsYou(item) {
  return !!item && (item.kind === "manual_test" || item.kind === "needs_you");
}

/** Open items, in list order. */
export function openItems(todos) {
  return (todos || []).filter((t) => t && !t.done);
}

/** A copy sorted by tier, keeping the incoming order inside a tier (stable,
 * whatever the engine's sort). */
export function sortByTier(items) {
  return (items || [])
    .map((item, i) => ({ item, i }))
    .sort((a, b) => tierRank(a.item) - tierRank(b.item) || a.i - b.i)
    .map((x) => x.item);
}

/** Lower-cased search terms of a filter string (whitespace separated). */
function terms(q) {
  return String(q || "")
    .toLowerCase()
    .split(/\s+/)
    .filter(Boolean);
}

/** Does the item match the filter? Every term must appear somewhere in the
 * text, the details or the session (case-insensitive). An empty filter
 * matches everything. */
export function matchesFilter(item, q) {
  const want = terms(q);
  if (!want.length) return true;
  const hay = [item && item.text, item && item.details, item && item.session]
    .filter(Boolean)
    .join("\n")
    .toLowerCase();
  return want.every((w) => hay.includes(w));
}

/** Group needs-you items by session, groups in order of first appearance;
 * the items without a session form the last group (session ""). */
export function groupBySession(items) {
  const groups = new Map();
  for (const item of items || []) {
    const s = (item && item.session) || "";
    if (!groups.has(s)) groups.set(s, []);
    groups.get(s).push(item);
  }
  const out = [...groups.entries()].map(([session, rows]) => ({ session, items: rows }));
  out.sort((a, b) => (a.session === "") - (b.session === ""));
  return out;
}

/** What the To do tab shows, from the whole list and the filter:
 *   needsYou   [{session, items}]  open manual_test / needs_you items
 *   rest       [{tier, items}]     the other open items, one entry per tier
 *                                  in use (high → low)
 *   showTiers  true when more than one tier is in use among `rest`
 *   open       open count (ignoring the filter)
 *   needs      needs-you count (ignoring the filter)
 *   shown      how many rows the filter let through */
export function buildSections(todos, q) {
  const open = sortByTier(openItems(todos));
  const needsAll = open.filter(isNeedsYou);
  const visible = open.filter((t) => matchesFilter(t, q));
  const needsYou = groupBySession(visible.filter(isNeedsYou));
  const others = visible.filter((t) => !isNeedsYou(t));
  const rest = TIERS.map((tier) => ({ tier, items: others.filter((t) => tierOf(t) === tier) })).filter(
    (g) => g.items.length
  );
  return {
    needsYou,
    rest,
    showTiers: rest.length > 1,
    open: open.length,
    needs: needsAll.length,
    shown: visible.length,
  };
}

/** The pane title: "To-do", plus how many items need the person. */
export function paneTitle(needs) {
  if (!needs) return "To-do";
  return "To-do · " + needs + (needs === 1 ? " needs you" : " need you");
}

/** The ids of the open items in the order a drop leaves them, and the tier
 * the dragged item ends in.
 *
 *   order      the open items in their current list order (tier-sorted)
 *   dragId     the item being moved
 *   anchorId   the row it was dropped on
 *   place      "before" | "after" the anchor
 *
 * The dragged item takes the anchor's tier (a drop inside another tier's run
 * re-prioritizes it: the caller PATCHes `priority` first, then POSTs the
 * order). Returns {ids, priority, changed}: `priority` is the new tier when it
 * changed, else null; `changed` is false for a drop that moves nothing. */
export function orderAfterDrop(order, dragId, anchorId, place) {
  const list = (order || []).slice();
  const ids = list.map((t) => t.id);
  const from = ids.indexOf(dragId);
  if (from < 0 || dragId === anchorId || ids.indexOf(anchorId) < 0) {
    return { ids, priority: null, changed: false };
  }
  const [dragged] = list.splice(from, 1);
  const anchorAt = list.findIndex((t) => t.id === anchorId);
  const anchor = list[anchorAt];
  const at = place === "after" ? anchorAt + 1 : anchorAt;
  const oldTier = tierOf(dragged);
  const newTier = tierOf(anchor);
  list.splice(at, 0, newTier === oldTier ? dragged : Object.assign({}, dragged, { priority: newTier }));
  const next = sortByTier(list).map((t) => t.id);
  const priority = newTier === oldTier ? null : newTier;
  const changed = priority !== null || next.some((id, i) => id !== ids[i]);
  return { ids: next, priority, changed };
}

/** Keyboard move (Alt+Up / Alt+Down) inside the rows the person sees:
 * `visible` is that run of rows (one section) in screen order. Returns the
 * orderAfterDrop result, or null when the row is already at that edge. */
export function orderAfterStep(order, visible, id, dir) {
  const at = (visible || []).findIndex((t) => t.id === id);
  if (at < 0) return null;
  const to = at + (dir < 0 ? -1 : 1);
  if (to < 0 || to >= visible.length) return null;
  return orderAfterDrop(order, id, visible[to].id, dir < 0 ? "before" : "after");
}

/** Local mirror of the server's POST /api/todo/order: the named open ids
 * first in that order, the open items not named after them (relative order
 * kept), tiers still win; done items keep trailing. Unknown ids are ignored. */
export function applyOrder(todos, ids) {
  const all = todos || [];
  const byId = new Map(all.map((t) => [t.id, t]));
  const seen = new Set();
  const named = [];
  for (const id of ids || []) {
    const t = byId.get(id);
    if (t && !t.done && !seen.has(id)) {
      named.push(t);
      seen.add(id);
    }
  }
  const restOpen = all.filter((t) => !t.done && !seen.has(t.id));
  const done = all.filter((t) => t.done);
  return sortByTier(named.concat(restOpen)).concat(done);
}

/** Local mirror of a priority PATCH: the item takes `tier` and moves to the
 * END of that tier's open run. */
export function applyTier(todos, id, tier) {
  const all = (todos || []).slice();
  const at = all.findIndex((t) => t.id === id);
  if (at < 0) return all;
  const item = Object.assign({}, all[at]);
  if (tier === "high" || tier === "low") item.priority = tier;
  else delete item.priority;
  all.splice(at, 1);
  const open = all.filter((t) => !t.done);
  const done = all.filter((t) => t.done);
  if (item.done) return sortByTier(open).concat(done.concat([item]));
  return sortByTier(open.concat([item])).concat(done);
}

// --- details → blocks -------------------------------------------------------

const URL_RE = /\bhttps?:\/\/[^\s<>"'`]+/g;

/** Split one run of text into [{type:"text", text} | {type:"link", href,
 * text}], trimming sentence punctuation (and an unbalanced closing bracket)
 * off the end of a URL. */
export function linkify(text) {
  const out = [];
  const s = String(text || "");
  let last = 0;
  URL_RE.lastIndex = 0;
  let m;
  while ((m = URL_RE.exec(s))) {
    let url = m[0];
    for (;;) {
      const tail = url.slice(-1);
      if (/[.,;:!?]/.test(tail)) url = url.slice(0, -1);
      else if (tail === ")" && (url.match(/\(/g) || []).length < (url.match(/\)/g) || []).length) url = url.slice(0, -1);
      else if (tail === "]" && (url.match(/\[/g) || []).length < (url.match(/\]/g) || []).length) url = url.slice(0, -1);
      else break;
    }
    if (m.index > last) out.push({ type: "text", text: s.slice(last, m.index) });
    out.push({ type: "link", href: url, text: url });
    last = m.index + url.length;
    URL_RE.lastIndex = last;
  }
  if (last < s.length) out.push({ type: "text", text: s.slice(last) });
  return out;
}

/** Details text → read-friendly blocks:
 *   {type: "text", parts: linkify(…)}             prose between fences
 *   {type: "code", lang, code, open?: true}       a ``` fenced block
 * A fence line is ``` (optionally indented, optionally followed by a
 * language word). An unterminated fence runs to the end of the text and is
 * marked `open: true` — still shown as code, so a half-pasted command stays
 * copyable. Blank-only prose between blocks is dropped. */
export function parseDetails(details) {
  const lines = String(details || "")
    .replace(/\r\n?/g, "\n")
    .split("\n");
  const blocks = [];
  let prose = [];
  let code = null;
  const flushProse = () => {
    const text = prose.join("\n").replace(/^\n+|\n+$/g, "");
    if (text.trim()) blocks.push({ type: "text", parts: linkify(text) });
    prose = [];
  };
  for (const line of lines) {
    const fence = /^\s*```\s*([\w+#.-]*)\s*$/.exec(line);
    if (code) {
      if (/^\s*```\s*$/.test(line)) {
        blocks.push({ type: "code", lang: code.lang, code: code.lines.join("\n") });
        code = null;
      } else code.lines.push(line);
    } else if (fence) {
      flushProse();
      code = { lang: fence[1] || "", lines: [] };
    } else prose.push(line);
  }
  if (code) blocks.push({ type: "code", lang: code.lang, code: code.lines.join("\n"), open: true });
  else flushProse();
  return blocks;
}

// --- time -------------------------------------------------------------------

function pad(n) {
  return String(n).padStart(2, "0");
}

const DAY_MS = 86400000;
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

function startOfDay(d) {
  return new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
}

/** Whole local days between two instants (b's day minus a's day). DST-safe:
 * rounds the midnight-to-midnight distance. */
function dayDiff(a, b) {
  return Math.round((startOfDay(b) - startOfDay(a)) / DAY_MS);
}

/** "HH:MM" (24 h) of a Date. */
export function clock(d) {
  return pad(d.getHours()) + ":" + pad(d.getMinutes());
}

/** "Nov 16", or "Nov 16, 2025" outside `now`'s year. */
function shortDate(d, now) {
  const base = MONTHS[d.getMonth()] + " " + d.getDate();
  return d.getFullYear() === now.getFullYear() ? base : base + ", " + d.getFullYear();
}

/** A due string ("YYYY-MM-DDTHH:MM", local) → Date, or null when empty or
 * malformed. Accepts seconds and a space separator from hand edits. */
export function parseDue(due) {
  const m = /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})/.exec(String(due || "").trim());
  if (!m) return null;
  const d = new Date(+m[1], +m[2] - 1, +m[3], +m[4], +m[5]);
  return isNaN(d.getTime()) ? null : d;
}

/** A Date → the contract's / datetime-local's "YYYY-MM-DDTHH:MM". */
export function toDueString(d) {
  return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate()) + "T" + pad(d.getHours()) + ":" + pad(d.getMinutes());
}

/** The due chip: {label, overdue, soon, title} or null without a due.
 *   overdue   "overdue · 14:30" today, "overdue · Nov 14" earlier
 *   today     "today 14:30"     (soon = within the next hour)
 *   tomorrow  "tomorrow 09:00"
 *   ≤ 6 days  "Fri 09:00"
 *   later     "Nov 30" */
export function formatDue(due, now) {
  const d = parseDue(due);
  if (!d) return null;
  const n = now instanceof Date ? now : new Date(now);
  const title = "Due " + WEEKDAYS[d.getDay()] + " " + shortDate(d, n) + " " + clock(d);
  const days = dayDiff(n, d);
  if (d.getTime() < n.getTime()) {
    return { label: "overdue · " + (days === 0 ? clock(d) : shortDate(d, n)), overdue: true, soon: false, title };
  }
  const soon = d.getTime() - n.getTime() <= 3600000;
  if (days === 0) return { label: "today " + clock(d), overdue: false, soon, title };
  if (days === 1) return { label: "tomorrow " + clock(d), overdue: false, soon, title };
  if (days <= 6) return { label: WEEKDAYS[d.getDay()] + " " + clock(d), overdue: false, soon: false, title };
  return { label: shortDate(d, n), overdue: false, soon: false, title };
}

/** A day heading for epoch seconds: "Today", "Yesterday", else
 * "Mon, Nov 16" (with the year outside `now`'s year). */
export function dayLabel(epoch, now) {
  const d = new Date(epoch * 1000);
  const n = now instanceof Date ? now : new Date(now);
  const days = dayDiff(d, n);
  if (days === 0) return "Today";
  if (days === 1) return "Yesterday";
  return WEEKDAYS[d.getDay()] + ", " + shortDate(d, n);
}

/** History entries (newest first) → [{key, label, items}] by the local day
 * of `closed_at` (falling back to done_at, then created_at). Entries keep
 * their incoming order inside a day; days are newest first. */
export function groupHistoryByDay(history, now) {
  const n = now instanceof Date ? now : new Date(now);
  const days = new Map();
  for (const h of history || []) {
    const at = closedAt(h);
    const d = new Date(at * 1000);
    const key = d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate());
    if (!days.has(key)) days.set(key, { key, label: dayLabel(at, n), at, items: [] });
    days.get(key).items.push(h);
  }
  return [...days.values()].sort((a, b) => b.at - a.at).map(({ key, label, items }) => ({ key, label, items }));
}

/** When a history entry closed, epoch seconds (0 when nothing says). */
export function closedAt(h) {
  return Number((h && (h.closed_at || h.done_at || h.created_at)) || 0);
}
