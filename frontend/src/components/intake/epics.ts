/** Intake → Epics (docs/epics.md): the shapes `GET /api/epics` answers with,
 * and the pure rules the tab and its badge share — so the list and the count
 * on the tab strip can never disagree. No React here: the tests import it
 * directly. */

export interface EpicRow {
  source: string;
  source_label: string;
  id: string;
  /** The epic's lead-session title (`has_session` = it exists). */
  title: string;
  name: string;
  url: string;
  state: string;
  /** The tracker's state type ("unstarted", "started", …); newer servers. */
  state_type?: string;
  /** The state's position in its workflow — the order the groups go in. */
  state_rank?: number;
  /** In one of the source's auto-start epic states. */
  in_states?: boolean;
  stories_total: number;
  mine_open: number | null;
  has_session: boolean;
  /** The source's epic auto-start is on. */
  auto: boolean;
  labels?: string[];
  /** Would start by itself: in the states, carries one of the source's
   * Auto-start labels (when any are set), not already taken. */
  eligible: boolean;
  /** Start works: not taken, stories of yours left (a missing label only
   * stops the AUTO start). Older servers: falls back to `eligible`. */
  startable?: boolean;
  reasons: string[];
}

/** An epics-capable source — every one gets a heading, epics or not. */
export interface EpicSource {
  source: string;
  source_label?: string;
  /** State NAMES whose epics auto-start. */
  epic_states?: string[];
  auto?: boolean;
}

export interface EpicsResponse {
  epics?: EpicRow[];
  /** Newer servers only — older ones list epics alone. */
  sources?: EpicSource[];
  errors?: Array<{ source: string; error: string }>;
}

/** The line under an epic's name: its state and your open stories. Whether it
 * starts by itself is the row's chip, not part of this line. */
export function epicMeta(e: Pick<EpicRow, "state" | "mine_open" | "stories_total">): string {
  const bits = [e.state];
  if (e.mine_open == null) bits.push(e.stories_total + " stories");
  else bits.push(e.mine_open + " of yours to do" + (e.stories_total ? " (of " + e.stories_total + ")" : ""));
  return bits.filter(Boolean).join(" · ");
}

/** What auto-start would take: eligible and no lead yet. The tab badge. */
export function epicsToStart(epics: EpicRow[] | null | undefined): EpicRow[] {
  return (epics || []).filter((e) => e.eligible && !e.has_session);
}

/** Whether a row is marked as one that starts by itself. */
export function autoStarts(e: EpicRow): boolean {
  return e.eligible && e.auto && !e.has_session;
}

export const NO_STATE = "No state";

export interface EpicStateGroup {
  state: string;
  rows: EpicRow[];
}

export interface EpicSourceGroup {
  source: string;
  label: string;
  /** null = the server didn't describe this source (older server, or an
   * epic/error naming a source it no longer lists). */
  meta: EpicSource | null;
  error: string;
  states: EpicStateGroup[];
  total: number;
}

/** source → state → rows. Sources in the server's `sources` order (every one,
 * even with no epics), then any other source an epic or error names; states by
 * `state_rank` (unranked last, then by name); rows in the server's order. */
export function groupEpics(data: EpicsResponse | null | undefined): EpicSourceGroup[] {
  const epics = data?.epics || [];
  const sources = data?.sources || [];
  const errors = new Map((data?.errors || []).map((x) => [x.source, x.error]));
  const order: string[] = [];
  const add = (s: string) => {
    if (s && !order.includes(s)) order.push(s);
  };
  sources.forEach((s) => add(s.source));
  epics.forEach((e) => add(e.source));
  errors.forEach((_, s) => add(s));

  return order.map((src) => {
    const meta = sources.find((s) => s.source === src) || null;
    const rows = epics.filter((e) => e.source === src);
    const byState = new Map<string, { rank: number; rows: EpicRow[] }>();
    for (const e of rows) {
      const st = e.state || NO_STATE;
      const rank = typeof e.state_rank === "number" ? e.state_rank : Infinity;
      const g = byState.get(st);
      if (g) {
        g.rows.push(e);
        g.rank = Math.min(g.rank, rank);
      } else byState.set(st, { rank, rows: [e] });
    }
    const states = [...byState.entries()]
      .sort((a, b) => (a[1].rank === b[1].rank ? a[0].localeCompare(b[0]) : a[1].rank < b[1].rank ? -1 : 1))
      .map(([state, g]) => ({ state, rows: g.rows }));
    const label = meta?.source_label || rows[0]?.source_label || src;
    return { source: src, label, meta, error: errors.get(src) || "", states, total: rows.length };
  });
}

/** A source heading's note: what it auto-starts. `kind` picks where pressing
 * it goes — the source's editor (states) or the switches (off). */
export function epicSourceNote(meta: EpicSource | null): { text: string; kind: "on" | "off" | "nostates" } | null {
  if (!meta) return null;
  const states = meta.epic_states || [];
  if (!states.length) return { text: "no auto-start states — set them on the source card in Tickets", kind: "nostates" };
  if (!meta.auto) return { text: "auto-start off", kind: "off" };
  return { text: "auto-starts epics in: " + states.join(", "), kind: "on" };
}
