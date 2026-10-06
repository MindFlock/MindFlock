/** The New dialog's list mode, the pure half: reading the describe box as a
 * list of things to work on, the request that starts them (SPEC §5
 * `POST /api/runs`), and the plain-language sentence that says what will
 * happen before anything does.
 *
 * The server is the authority on what a line IS (`POST /api/runs/preview`
 * resolves ticket IDs against the configured sources). The local reading
 * here only decides WHEN to ask it — two lines, or anything shaped like a
 * ticket — and draws placeholder rows while it answers. One plain line never
 * reaches the server's parser at all: it is today's single session. */

import { laneDefault, type Lane } from "./laneActions";

// --- Reading the box ------------------------------------------------------------

/** A token shaped like a ticket reference: `PAY-412`, `sc-1234`, `#318`,
 * `owner/repo#318`, or a link. Deliberately loose — it only decides that the
 * server should look; the server decides what it is. */
const TICKET_TOKEN = /^(?:[A-Za-z][A-Za-z0-9]{0,15}-\d+|#\d+|[\w.-]+\/[\w.-]+#\d+|https?:\/\/\S+)$/;

export function isTicketToken(tok: string): boolean {
  return TICKET_TOKEN.test(tok.trim());
}

export type LocalItem = { kind: "ticket"; ref: string } | { kind: "task"; text: string };

/** The box, read locally. A line made only of ticket-shaped tokens (spaces or
 * commas between them) is that many tickets; any other non-blank line is one
 * task. Duplicate tickets are kept once. */
export function localItems(text: string): LocalItem[] {
  const out: LocalItem[] = [];
  const seen = new Set<string>();
  for (const raw of String(text || "").split(/\r?\n/)) {
    const line = raw.trim();
    if (!line) continue;
    const toks = line.split(/[\s,]+/).filter(Boolean);
    if (toks.length && toks.every(isTicketToken)) {
      for (const t of toks) {
        const k = t.toLowerCase();
        if (seen.has(k)) continue;
        seen.add(k);
        out.push({ kind: "ticket", ref: t });
      }
    } else {
      out.push({ kind: "task", text: line });
    }
  }
  return out;
}

/** List mode: two or more things, or any ticket at all. One plain line stays
 * the single-session flow it has always been. */
export function isListMode(items: readonly LocalItem[]): boolean {
  return items.length >= 2 || items.some((i) => i.kind === "ticket");
}

/** "Split a big line into parallel pieces first" applies to exactly one task
 * line — splitting a list, or a ticket, is not what it does. */
export function splitApplies(items: readonly LocalItem[]): boolean {
  return items.length === 1 && items[0].kind === "task";
}

/** Why the split box is off for what is in the box, or "". */
export function splitShapeReason(items: readonly LocalItem[]): string {
  if (!items.length) return "";
  if (items.length > 1) return "one line only — this is a list of " + items.length;
  return items[0].kind === "ticket" ? "a ticket starts as itself" : "";
}

// --- The server's reading (SPEC §5 POST /api/runs/preview) ----------------------

export interface PreviewItem {
  kind: "ticket" | "task" | string;
  source?: string;
  id?: string;
  ref?: string;
  title?: string;
  text?: string;
  repo?: string;
  title_hint?: string;
  has_session?: boolean;
  error?: string | null;
}

export interface PreviewResponse {
  items: PreviewItem[];
  name_suggestion?: string;
  /** Not read: the New dialog's one default is Settings' (defaultLaneFor). */
  lane_default?: string;
  warnings?: string[];
}

/** One row of the list, whichever reading it came from. */
export interface ItemRow {
  key: string;
  kind: "ticket" | "task";
  ref: string;
  title: string;
  where: string;
  error: string;
  /** The server has not answered for this text yet. */
  pending: boolean;
  /** What ✕ removes from the box. */
  token: string;
}

/** Rows for the list: the server's, when it has answered for exactly this
 * text, else the local reading drawn as placeholders. */
export function itemRows(
  local: readonly LocalItem[],
  preview: PreviewResponse | null,
  repoLabel: string,
  sourceLabel: (key: string) => string = (k) => k
): ItemRow[] {
  if (preview) {
    return preview.items.map((it, i) => {
      if (it.kind === "ticket") {
        const ref = String(it.ref || it.id || "");
        return {
          key: "t" + i + ":" + ref,
          kind: "ticket",
          ref,
          title: String(it.title || ""),
          where: it.source ? sourceLabel(it.source) : "",
          error: it.error ? String(it.error) : "",
          pending: false,
          token: ref,
        };
      }
      const text = String(it.text || "");
      return {
        key: "k" + i + ":" + text,
        kind: "task",
        ref: "",
        title: text,
        where: String(it.repo || repoLabel),
        error: it.error ? String(it.error) : "",
        pending: false,
        token: text,
      };
    });
  }
  return local.map((it, i) =>
    it.kind === "ticket"
      ? {
          key: "t" + i + ":" + it.ref,
          kind: "ticket",
          ref: it.ref,
          title: "",
          where: "",
          error: "",
          pending: true,
          token: it.ref,
        }
      : {
          key: "k" + i + ":" + it.text,
          kind: "task",
          ref: "",
          title: it.text,
          where: repoLabel,
          error: "",
          pending: false,
          token: it.text,
        }
  );
}

/** The box with one item taken out: a task's whole line, or a ticket's token
 * from whichever line holds it (the line goes too if that was all it held). */
export function removeItem(text: string, row: Pick<ItemRow, "kind" | "token">): string {
  const lines = String(text || "").split(/\r?\n/);
  const want = row.token.trim();
  if (row.kind === "task") {
    const i = lines.findIndex((l) => l.trim() === want);
    if (i >= 0) lines.splice(i, 1);
    return lines.join("\n");
  }
  const low = want.toLowerCase();
  for (let i = 0; i < lines.length; i++) {
    const toks = lines[i].split(/([\s,]+)/);
    const j = toks.findIndex((t) => t.toLowerCase() === low);
    if (j < 0) continue;
    toks.splice(j, 1);
    // Tidy the separator the token leaves behind.
    const next = toks
      .join("")
      .replace(/^[\s,]+|[\s,]+$/g, "")
      .replace(/\s*,\s*,\s*/g, ", ")
      .replace(/ {2,}/g, " ");
    if (next) lines[i] = next;
    else lines.splice(i, 1);
    break;
  }
  return lines.join("\n");
}

/** A group name when the server suggested none: the first task's first few
 * words, or the tickets' common prefix ("PAY tickets"). */
export function fallbackName(items: readonly LocalItem[]): string {
  const task = items.find((i) => i.kind === "task") as { text: string } | undefined;
  if (task) {
    const words = task.text.split(/\s+/).slice(0, 4).join(" ");
    return words.length > 40 ? words.slice(0, 40).trimEnd() : words;
  }
  const refs = items.map((i) => (i.kind === "ticket" ? i.ref : ""));
  const pre = refs.map((r) => (/^([A-Za-z][A-Za-z0-9]*)-\d+$/.exec(r) || [])[1] || "");
  if (pre.length && pre[0] && pre.every((p) => p.toLowerCase() === pre[0].toLowerCase()))
    return pre[0].toUpperCase() + " tickets";
  return "Batch";
}

/** Intake's ticked tickets as the box's text: one line of references, the
 * way the user would have typed them. The tracker's own ID when it reads as
 * one (PAY-412), else the slug (sc-1234), else the link — which the server
 * also takes — so a ticket whose IDs are bare numbers still resolves to the
 * right one. */
export function startTogetherText(
  tickets: ReadonlyArray<{ slug?: string; url?: string; id: string | number }>
): string {
  return tickets
    .map((t) => {
      const id = String(t.id ?? "").trim();
      const slug = String(t.slug || "").trim();
      if (id && isTicketToken(id)) return id;
      if (slug && isTicketToken(slug)) return slug;
      if (t.url) return String(t.url);
      return slug || id;
    })
    .join(" ");
}

// --- The request -----------------------------------------------------------------

export type Grouping = "each" | "together";

export const CONCURRENCY_MIN = 1;
export const CONCURRENCY_MAX = 8;
export const CONCURRENCY_DEFAULT = 3;

export function clampConcurrency(n: number): number {
  if (!Number.isFinite(n)) return CONCURRENCY_DEFAULT;
  return Math.min(CONCURRENCY_MAX, Math.max(CONCURRENCY_MIN, Math.round(n)));
}

/** The items a create sends: the server's reading, trimmed to the fields
 * §5 names. Rows that failed to resolve are never sent (and never quietly
 * turned into tasks): the caller refuses to start while any are listed. */
export function requestItems(preview: PreviewResponse): Array<Record<string, string>> {
  return preview.items
    .filter((it) => !it.error)
    .map((it): Record<string, string> =>
      it.kind === "ticket"
        ? {
            kind: "ticket",
            source: String(it.source || ""),
            id: String(it.id || it.ref || ""),
          }
        : { kind: "task", text: String(it.text || "") }
    );
}

/** One PR for all (or a split) commits every line into the group's branch —
 * that IS the shape — so fast-track Off means "Commit" there (nothing leaves
 * this machine), and "ask me first" is the group's release, which always
 * asks. The dialog shows exactly what is sent. */
export function oneForAllLane(lane: Lane): Lane {
  return lane === "leave" ? "commit" : lane;
}

export function runBody(o: {
  name: string;
  items: Array<Record<string, string>>;
  lane: Lane;
  askFirst: boolean;
  grouping: Grouping;
  concurrency: number;
  program: string;
  repoPath: string;
  split: boolean;
}): Record<string, unknown> {
  const together = o.split || o.grouping === "together";
  const lane = together ? oneForAllLane(o.lane) : o.lane;
  return {
    name: o.name.trim(),
    items: o.items,
    policy: {
      lane,
      ask_first: !together && lane !== "leave" && o.askFirst,
      grouping: o.split ? "together" : o.grouping,
      release: "ask",
    },
    concurrency: clampConcurrency(o.concurrency),
    program: o.program.trim(),
    repo_path: o.repoPath.trim(),
    split: o.split,
  };
}

/** The fast-track the dialog starts on before the user picks (the owner's
 * rule: "Off unless I pick"):
 *
 *  - a SINGLE new session (one line, the "Set it up myself" form) starts
 *    Off, whatever Settings says;
 *  - a BATCH (a list, tickets, Intake's Start together, a split) starts on
 *    Settings → Workspace "Fast-track goes as far as" — itself Off when unset.
 *
 * The preview's own `lane_default` is not read: the setting is the one
 * default. */
export function defaultLaneFor(batch: boolean, setting: string | null | undefined): Lane {
  return batch ? laneDefault(setting) : "leave";
}

/** The primary button. One item keeps today's words; a list says how many;
 * a split starts its lead. */
export function startLabel(n: number, split: boolean): string {
  if (split) return "Start the lead";
  if (n >= 2) return `Start ${n} sessions`;
  return "Create session";
}

// --- The sentence ------------------------------------------------------------------

/** What will happen, in the user's words, from the choices on screen. `lead`
 * is the main clause, `tail` the quieter follow-on; null when there is
 * nothing to say beyond a single session with fast-track off. */
export function summarySentence(o: {
  n: number;
  concurrency: number;
  lane: Lane;
  askFirst: boolean;
  grouping: Grouping;
  split: boolean;
}): { lead: string; tail: string } | null {
  const together = o.split || (o.n >= 2 && o.grouping === "together");
  const ask = !together && o.lane !== "leave" && o.askFirst;
  const asks = ask ? " Before the first commit it stops and asks you in the Outbox." : "";
  if (o.split) {
    const lead =
      "One lead session in a new worktree. Its agent proposes pieces with separate paths; you " +
      "approve the split in its Thread tab, then MindFlock starts the workers, fences each to its " +
      "paths and merges them back.";
    const tail =
      o.lane === "pr"
        ? "Then it opens one PR — after you say go."
        : o.lane === "merge"
          ? "Then it opens one PR and merges it once checks pass — after you say go."
          : o.lane === "push"
            ? "Then the merged branch is pushed — after you say go."
            : "The merged branch waits for you; nothing is pushed.";
    return { lead, tail: tail + asks };
  }
  if (o.n >= 2) {
    const c = clampConcurrency(o.concurrency);
    const count = c >= o.n ? `${o.n} sessions, all at once.` : `${o.n} sessions, ${c} at a time.`;
    if (o.grouping === "together") {
      const body: Record<Lane, [string, string]> = {
        // (never sent: one for all commits each line — see oneForAllLane)
        leave: [
          " Each one is committed as it finishes and merged into one shared branch.",
          "Nothing is pushed.",
        ],
        commit: [
          " Each one is committed as it finishes and merged into one shared branch.",
          "Nothing is pushed.",
        ],
        push: [
          " Each one is committed as it finishes and merged into one shared branch, which is pushed once all are in.",
          "No PR is opened.",
        ],
        pr: [
          " Each one is committed as it finishes and merged into one shared branch; when all are in and the tests pass, MindFlock opens one PR.",
          "It asks you before it opens it.",
        ],
        merge: [
          " Each one is committed as it finishes and merged into one shared branch; when all are in and the tests pass, MindFlock opens one PR and merges it once checks pass.",
          "It asks you before it opens it.",
        ],
      };
      const [l, t] = body[o.lane];
      return { lead: count + l, tail: t + asks };
    }
    const body: Record<Lane, [string, string]> = {
      leave: [
        " Each one stops when its agent does — nothing is committed.",
        "You'll see each in the rail.",
      ],
      commit: [
        " Each one is committed with a message written from its diff once its agent stops and your hooks pass.",
        "Nothing is pushed.",
      ],
      push: [
        " Each one is committed with a message written from its diff and pushed once its agent stops and your hooks pass.",
        "No PRs are opened.",
      ],
      pr: [
        " Each one is committed with a message written from its diff, pushed and opened as its own PR once its agent stops and your hooks pass.",
        "Nothing merges; you'll see each PR in the Outbox.",
      ],
      merge: [
        " Each one is committed with a message written from its diff, pushed, opened as its own PR and merged once its checks pass.",
        "You'll see each PR in the Outbox.",
      ],
    };
    const [l, t] = body[o.lane];
    return { lead: count + l, tail: t + asks };
  }
  if (o.lane === "leave") return null;
  const one: Record<Exclude<Lane, "leave">, [string, string]> = {
    commit: [
      "When its agent stops and your hooks pass, MindFlock commits it with a message written from its diff.",
      "Nothing is pushed.",
    ],
    push: [
      "When its agent stops and your hooks pass, MindFlock commits it with a message written from its diff and pushes it.",
      "No PR is opened.",
    ],
    pr: [
      "When its agent stops and your hooks pass, MindFlock commits it with a message written from its diff, pushes it and opens a PR.",
      "Nothing merges.",
    ],
    merge: [
      "When its agent stops and your hooks pass, MindFlock commits it, pushes it, opens a PR and merges it once checks pass.",
      "",
    ],
  };
  const [l, t] = one[o.lane];
  return { lead: l, tail: (t + asks).trim() };
}

// --- Where typed tasks start ---------------------------------------------------------

/** The "tasks start in" picker's sentinel: choosing it opens the folder
 * browser (an inline panel — Electron has no prompt) instead of setting a path. */
export const BROWSE_VALUE = "\u0000browse";

/** The picker's options: the folder in the form first when no suggestion is
 * it (so a browsed-to folder shows as itself), every suggested repo, then
 * "Browse…" — any folder at all, not only the suggestions. */
export function runRepoOptions(
  repoPath: string,
  suggestions: ReadonlyArray<{ path: string; name?: string }>,
  leaf: (p: string) => string = (p) => p.replace(/\/+$/, "").split("/").pop() || p
): Array<{ value: string; label: string }> {
  const out: Array<{ value: string; label: string }> = [];
  if (!suggestions.some((s) => s.path === repoPath)) out.push({ value: repoPath, label: leaf(repoPath) || repoPath || "—" });
  for (const s of suggestions) out.push({ value: s.path, label: s.name || leaf(s.path) });
  out.push({ value: BROWSE_VALUE, label: "Browse…" });
  return out;
}
