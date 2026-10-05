/** The Thread tab's pure half (grid/ThreadTab.tsx renders it): a session's
 * family — the orchestrator, its workers — as worker rows, a read-only log
 * of what passed between them, and the composer that types into a member's
 * prompt AS YOU.
 *
 * Everything here reads row fields or a `GET …/thread` body, and nothing
 * here writes: the log never marks mail read (the route is non-consuming),
 * and the composer's routes (/send, /queue) are the Queue tab's own — your
 * words never go through the agents' mailbox.
 *
 * Normalizers never throw: a half-built response renders as "less", not a
 * crash (the rule codemapApi.ts and flockActions.ts follow). */

import type { DiffStat, Instance, LastReport, ThreadItem, ThreadMember, ThreadResponse } from "../api/types";
import { childrenOf, workerState, type WorkerState } from "./agentMessages";

type Obj = Record<string, unknown>;
const obj = (v: unknown): Obj => (v && typeof v === "object" && !Array.isArray(v) ? (v as Obj) : {});
const str = (v: unknown, d = ""): string => (typeof v === "string" ? v : v == null ? d : String(v));
const num = (v: unknown): number => {
  const n = Number(v);
  return Number.isFinite(n) ? n : 0;
};
const strOrNull = (v: unknown): string | null => {
  const s = typeof v === "string" ? v.trim() : "";
  return s ? s : null;
};

// --- Normalizing GET …/thread ----------------------------------------------------

function normReport(v: unknown): LastReport | null {
  const o = obj(v);
  if (!Object.keys(o).length) return null;
  return { id: str(o.id), status: str(o.status), summary: str(o.summary), ts: num(o.ts) };
}

function normDiffStat(v: unknown): DiffStat | null {
  if (!v || typeof v !== "object") return null;
  return v as DiffStat;
}

function normMember(v: unknown): ThreadMember | null {
  const o = obj(v);
  const title = str(o.title).trim();
  if (!title) return null;
  return {
    title,
    role: str(o.role, "child"),
    status: str(o.status),
    activity: str(o.activity, "idle"),
    activity_since: num(o.activity_since),
    branch: str(o.branch),
    diff_stat: normDiffStat(o.diff_stat),
    created_at: num(o.created_at),
    last_report: normReport(o.last_report),
    base_sha: strOrNull(o.base_sha),
  };
}

function normItem(v: unknown): ThreadItem | null {
  const o = obj(v);
  const id = str(o.id);
  const type = str(o.type);
  if (!id || !type) return null;
  return {
    type,
    id,
    ts: num(o.ts),
    from: str(o.from),
    to: str(o.to),
    text: str(o.text),
    status: strOrNull(o.status),
    state: strOrNull(o.state),
    base_sha: strOrNull(o.base_sha),
  };
}

/** A `GET /api/instances/{t}/thread` body, shape-checked. */
export function normThread(v: unknown, title = ""): ThreadResponse {
  const o = obj(v);
  const members = Array.isArray(o.members)
    ? o.members.map(normMember).filter((m): m is ThreadMember => !!m)
    : [];
  const items = Array.isArray(o.items) ? o.items.map(normItem).filter((i): i is ThreadItem => !!i) : [];
  return { title: str(o.title, title) || title, parent: str(o.parent), members, items, more: o.more === true };
}

/** Prepend an older page to the items already shown (paging back with
 * `before`), dropping any overlap so a racing refresh can't double a row. */
export function mergeOlder(older: ThreadItem[], current: ThreadItem[]): ThreadItem[] {
  const seen = new Set(current.map((i) => i.id));
  return older.filter((i) => !seen.has(i.id)).concat(current);
}

// --- The family, from the rail's rows ----------------------------------------------

/** The rows the family logic reads. */
export type FamilyInst = Pick<Instance, "title"> &
  Partial<
    Pick<
      Instance,
      | "parent"
      | "device"
      | "pending"
      | "activity"
      | "activity_since"
      | "status"
      | "last_report"
      | "diff_stat"
      | "provider"
    >
  >;

/** `title`'s live parent ("" for none) and its live workers, in rail order —
 * workers by the shared rule (agentMessages.isChildOf): a remote device's
 * rows are not this rail's family (their `parent` names a title on that
 * device), and a pending one can't take a message yet, so "all workers"
 * never sends to it. */
export function familyOf<T extends FamilyInst>(
  title: string,
  rows: readonly T[]
): { parent: string; children: T[] } {
  const me = rows.find((r) => r.title === title && !r.device);
  const live = new Set(rows.filter((r) => !r.device).map((r) => r.title));
  const p = String(me?.parent || "");
  const parent = p && p !== title && live.has(p) ? p : "";
  const children = childrenOf(title, rows);
  return { parent, children };
}

/** Whether a pane shows the Thread tab at all: only for a session in a family
 * (it has a parent or workers) — or one opened on purpose (Ctrl+K T / S, the
 * palette), which is what `opened` says. */
export function threadTabShown(hasFamily: boolean, opened: boolean): boolean {
  return hasFamily || opened;
}

/** The Thread tab's badge: workers that need YOUR answer, plus reports that
 * arrived since you last looked at the tab (`lastSeenMs`, epoch ms; report
 * `ts` is epoch seconds). A report counts once: answering its worker's
 * prompt later doesn't make it new again. 0 = no badge. */
export function threadBadge(
  children: readonly FamilyInst[],
  lastSeenMs: number,
  actOf: (r: FamilyInst) => string
): { count: number; needs: number; fresh: number } {
  let needs = 0;
  let fresh = 0;
  for (const c of children) {
    const act = actOf(c);
    if (act === "clarify") needs++;
    const r = c.last_report;
    if (r && String(r.status || "").trim() && Number(r.ts) * 1000 > (lastSeenMs || 0)) fresh++;
  }
  return { count: needs + fresh, needs, fresh };
}

/** The newest report among `children` (epoch seconds), 0 for none — the tab
 * marks itself seen again whenever this moves while it is open. */
export function newestReportTs(children: readonly FamilyInst[]): number {
  let best = 0;
  for (const c of children) best = Math.max(best, Number(c.last_report?.ts) || 0);
  return best;
}

// --- Worker rows --------------------------------------------------------------------

/** One worker as the Thread draws it: the live row's fields where it has one
 * (fresher than the fetch), the thread member's otherwise. */
export interface WorkerRow {
  title: string;
  state: WorkerState;
  activity: string;
  activitySince: number;
  report: LastReport | null;
  diff: DiffStat | null;
  baseSha: string | null;
}

/** Where each state sorts: needs-you first, then the reported ones (what you
 * act on next), then everything still running. */
const RANK: Record<WorkerState, number> = {
  ask: 0,
  failed: 1,
  blocked: 1,
  done: 2,
  limit: 3,
  working: 4,
  idle: 5,
};

/** The worker rows, needs-you first; stable within a group (rail order). */
export function workerRows(
  children: readonly FamilyInst[],
  members: readonly ThreadMember[],
  actOf: (r: FamilyInst) => string
): WorkerRow[] {
  const byTitle = new Map(members.map((m) => [m.title, m]));
  const rows = children.map((c) => {
    const m = byTitle.get(c.title);
    const act = actOf(c);
    const merged: FamilyInst = {
      ...c,
      last_report: c.last_report !== undefined ? c.last_report : (m?.last_report ?? null),
      activity_since: c.activity_since ?? m?.activity_since,
    };
    return {
      title: c.title,
      state: workerState(merged, act),
      activity: act,
      activitySince: Number(merged.activity_since) || 0,
      report: merged.last_report ?? null,
      diff: c.diff_stat ?? m?.diff_stat ?? null,
      baseSha: m?.base_sha ?? null,
    };
  });
  return rows
    .map((r, i) => ({ r, i }))
    .sort((a, b) => RANK[a.r.state] - RANK[b.r.state] || a.i - b.i)
    .map((x) => x.r);
}

const isReported = (s: WorkerState) => s === "done" || s === "blocked" || s === "failed";

/** How many workers have a report standing (done, blocked or failed). */
export function reportedCount(rows: readonly WorkerRow[]): number {
  return rows.filter((r) => isReported(r.state)).length;
}

/** The commit the workers forked from, short — "" when unknown or when they
 * did not all fork from the same one. */
export function forkPoint(rows: readonly WorkerRow[]): string {
  const shas = new Set(rows.map((r) => r.baseSha).filter((s): s is string => !!s));
  if (shas.size !== 1) return "";
  return [...shas][0].slice(0, 7);
}

/** One coloured part of the header's summary line. */
export interface SummaryPart {
  text: string;
  cls: "" | "needs" | "ok" | "bad" | "sha";
}

const plural = (n: number, word: string) => n + " " + word + (n === 1 ? "" : "s");

/** "3 workers forked from 3f2c1a0 · 1 needs your answer · 1 reported ·
 * 1 working" — every non-zero group, most urgent first. */
export function headerSummary(rows: readonly WorkerRow[]): SummaryPart[] {
  if (!rows.length) return [];
  const n = (s: WorkerState) => rows.filter((r) => r.state === s).length;
  const sha = forkPoint(rows);
  const parts: SummaryPart[] = [{ text: plural(rows.length, "worker") + (sha ? " forked from " : ""), cls: "" }];
  if (sha) parts.push({ text: sha, cls: "sha" });
  const ask = n("ask");
  if (ask) parts.push({ text: ask + (ask === 1 ? " needs" : " need") + " your answer", cls: "needs" });
  const bad = n("failed") + n("blocked");
  if (n("failed")) parts.push({ text: n("failed") + " failed", cls: "bad" });
  if (n("blocked")) parts.push({ text: n("blocked") + " blocked", cls: "bad" });
  const done = n("done");
  if (done) parts.push({ text: done + " reported", cls: bad ? "" : "ok" });
  if (n("limit")) parts.push({ text: n("limit") + " at the usage limit", cls: "" });
  if (n("working")) parts.push({ text: n("working") + " working", cls: "" });
  if (n("idle")) parts.push({ text: n("idle") + " idle without a report", cls: "" });
  return parts;
}

/** "40s", "6m", "2h", "3d" since `ts` (epoch seconds). */
export function since(ts: number, now: number = Date.now() / 1000): string {
  const secs = Math.max(0, Math.floor(now - ts));
  if (secs < 60) return secs + "s";
  if (secs < 3600) return Math.floor(secs / 60) + "m";
  if (secs < 86400) return Math.floor(secs / 3600) + "h";
  return Math.floor(secs / 86400) + "d";
}

/** A worker row's status: the coloured word and the muted detail after it. */
export function workerStatus(
  row: WorkerRow,
  parentName: string,
  now: number = Date.now() / 1000
): { word: string; cls: string; detail: string } {
  const t = row.activitySince;
  switch (row.state) {
    case "ask":
      return {
        word: "needs your answer",
        cls: "needs",
        detail: [t ? "asking for " + since(t, now) : "", parentName ? parentName + " is waiting on it" : ""]
          .filter(Boolean)
          .join(" · "),
      };
    case "done":
    case "blocked":
    case "failed":
      return {
        word: "reported " + row.state,
        cls: row.state === "done" ? "ok" : "bad",
        detail: row.report?.ts ? since(row.report.ts, now) + " ago" : "",
      };
    case "limit":
      return { word: "usage limit", cls: "bad", detail: "its queue resumes when the window resets" };
    case "working":
      return { word: "working", cls: "work", detail: [t ? since(t, now) : "", "no report yet"].filter(Boolean).join(" · ") };
    default:
      return { word: "idle", cls: "idle", detail: "stopped without a report" };
  }
}

/** "+41 −3 · 2 files", or "" for no change. */
export function diffText(ds: DiffStat | null | undefined): string {
  if (!ds) return "";
  const f = ds.files || 0;
  const a = ds.additions || 0;
  const d = ds.deletions || 0;
  if (!f && !a && !d) return "";
  return `+${a} −${d} · ${f} file${f === 1 ? "" : "s"}`;
}

// --- The "Between sessions" log --------------------------------------------------------

export type LogFilter = "all" | "reports";

/** One card of the log. Spawns an orchestrator made in one go (same parent,
 * within a couple of minutes) share a card: "api → a, b, c". */
export interface LogEntry {
  key: string;
  kind: "spawn" | "message" | "result";
  ts: number;
  from: string;
  to: string[];
  /** The items behind the card (one, except for a grouped spawn). */
  items: ThreadItem[];
}

/** Spawns closer together than this (seconds) fold into one card. */
export const SPAWN_GROUP_S = 120;

export function logEntries(items: readonly ThreadItem[], filter: LogFilter): LogEntry[] {
  const out: LogEntry[] = [];
  for (const it of items) {
    if (filter === "reports" && it.type !== "result") continue;
    const kind = it.type === "spawn" ? "spawn" : it.type === "result" ? "result" : "message";
    const last = out[out.length - 1];
    if (
      kind === "spawn" &&
      last &&
      last.kind === "spawn" &&
      last.from === it.from &&
      it.ts - last.items[last.items.length - 1].ts <= SPAWN_GROUP_S
    ) {
      last.items.push(it);
      last.to.push(it.to);
      continue;
    }
    out.push({ key: it.id, kind, ts: it.ts, from: it.from, to: [it.to], items: [it] });
  }
  return out;
}

/** The footer of a message card: what happened to it on the other side. The
 * log only REPORTS this; reading the thread never changes it. */
export function deliveryText(state: string | null, to: string): string {
  switch (state) {
    case "read":
      return "read by " + to;
    case "delivered":
      return "delivered to " + to;
    case "pending":
      return "waiting for " + to;
    case "held":
      return "held for " + to + " — it reads it when it checks its messages";
    default:
      return "";
  }
}

/** "10:03" today, "Oct 3 10:03" before today (local time). */
export function clockTime(ts: number, now: Date = new Date()): string {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  const hm = String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0");
  const same =
    d.getFullYear() === now.getFullYear() && d.getMonth() === now.getMonth() && d.getDate() === now.getDate();
  if (same) return hm;
  return d.toLocaleString("en-US", { month: "short" }) + " " + d.getDate() + " " + hm;
}

/** Split text on `backtick` spans, so the card can set them in mono. */
export function codeSpans(text: string): { code: boolean; text: string }[] {
  const out: { code: boolean; text: string }[] = [];
  const re = /`([^`\n]+)`/g;
  let at = 0;
  let m: RegExpExecArray | null;
  while ((m = re.exec(text))) {
    if (m.index > at) out.push({ code: false, text: text.slice(at, m.index) });
    out.push({ code: true, text: m[1] });
    at = m.index + m[0].length;
  }
  if (at < text.length) out.push({ code: false, text: text.slice(at) });
  return out;
}

// --- The composer --------------------------------------------------------------------

/** Where the composer can write. `titles` is who receives it ("all workers"
 * is several). */
export interface ComposeChip {
  key: string;
  label: string;
  titles: string[];
}

export const ALL_WORKERS = "*workers";

/** The To chips: the session itself, its parent (when it is a worker), each
 * worker, and "all workers" when there is more than one. */
export function composeChips(
  title: string,
  parent: string,
  workers: readonly string[],
  nameOf: (t: string) => string
): ComposeChip[] {
  const chips: ComposeChip[] = [{ key: title, label: nameOf(title), titles: [title] }];
  if (parent) chips.push({ key: parent, label: nameOf(parent), titles: [parent] });
  for (const w of workers) chips.push({ key: w, label: nameOf(w), titles: [w] });
  if (workers.length > 1) chips.push({ key: ALL_WORKERS, label: "all workers", titles: [...workers] });
  return chips;
}

/** The chip a request to write to `to` lands on — the session itself when
 * `to` is not (or no longer) in the family. */
export function chipFor(chips: readonly ComposeChip[], to: string | null | undefined): ComposeChip {
  return chips.find((c) => c.key === to) || chips[0];
}

/** A session in one of these is showing a dialog or waiting out a usage
 * window: text typed now would answer the dialog (or be lost), so the
 * composer queues it for when the session is free instead. */
export const NOT_FREE: ReadonlySet<string> = new Set(["clarify", "limit"]);

/** What "Send now" does for these recipients: `now` get /send, `later` get
 * /queue (they are on a prompt or at the usage limit). When nobody can take
 * it now, the button itself turns into "When it's free". */
export function sendPlan(
  titles: readonly string[],
  actOf: (t: string) => string
): { now: string[]; later: string[]; label: string } {
  const now: string[] = [];
  const later: string[] = [];
  for (const t of titles) (NOT_FREE.has(actOf(t)) ? later : now).push(t);
  return { now, later, label: now.length ? "Send now" : "When it's free" };
}

/** The newest `threadOpen` request (its `seq`) each session's composer has
 * acted on. */
const composeHandled = new Map<string, number>();

/** Whether `threadOpen` request `seq` is news for `title`'s composer — and,
 * if it is, mark it handled. A request is acted on ONCE: re-applying it each
 * time the tab comes back on screen, or when the pane remounts in another
 * grid slot, would silently readdress the composer and pull the caret out
 * of the terminal the user just picked. */
export function claimComposeRequest(title: string, seq: number): boolean {
  if (!seq || (composeHandled.get(title) ?? 0) >= seq) return false;
  composeHandled.set(title, seq);
  return true;
}

/** The placeholder names who you are writing to. */
export function composePlaceholder(chip: ComposeChip): string {
  if (chip.key === ALL_WORKERS) return `Message all ${chip.titles.length} workers — typed into each prompt as you`;
  return `Message ${chip.label} — typed into its prompt as you`;
}

/** How a mindflock MCP tool is spelled to a CLI (playbooks.tool_name's rule:
 * Claude lists them as mcp__mindflock__<tool>, other CLIs by the bare name). */
export function toolName(tool: string, provider: string | undefined): string {
  return (provider || "").trim().toLowerCase() === "claude" ? "mcp__mindflock__" + tool : tool;
}

/** One line, at most `max` characters. */
function oneLine(text: string, max: number): string {
  const s = String(text || "")
    .replace(/[\u0000-\u001f\u007f]+/g, " ")
    .replace(/\s+/g, " ")
    .trim();
  return s.length > max ? s.slice(0, max - 1).trimEnd() + "…" : s;
}

/** A session title quoted for a prompt the way the server's playbooks quote
 * one (playbooks._quote): control characters gone, line breaks as spaces,
 * double quotes neutralized, cut at 120 with "…" — otherwise spelled exactly
 * as the title is (runs of spaces kept), so the agent can address it. */
export function quoteTitle(title: string): string {
  let t = String(title || "")
    .replace(/[\t\n]/g, " ")
    .replace(/[\u0000-\u001f\u007f-\u009f]/g, "")
    .replace(/"/g, "'");
  if (t.length > 120) t = t.slice(0, 119).trimEnd() + "…";
  return '"' + t + '"';
}

/** "Let api decide": the prompt queued to the orchestrator, asking IT to
 * answer its worker's dialog. One paragraph, well under the 600 characters
 * Claude shows as text rather than "[Pasted text]"; names only real tools.
 * `worker` is the session's TITLE, never its alias: the agent addresses
 * sessions by title, and an alias can be another session's title. */
export function decidePrompt(
  worker: string,
  dialog: { question?: string; command?: string | null } | null,
  provider: string | undefined
): string {
  const q = dialog ? oneLine([dialog.command, dialog.question].filter(Boolean).join(" — "), 160) : "";
  const who = quoteTitle(worker);
  return (
    `Your MindFlock worker ${who} is waiting on a prompt` +
    (q ? `: “${q}”.` : ".") +
    ` Look at it with ${toolName("read_output", provider)} (view screen) and answer it with` +
    ` ${toolName("answer_prompt", provider)} if you are sure that is safe for this task;` +
    " otherwise leave it and tell me why."
  );
}
