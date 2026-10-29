/** Sessions shared over a peer link (docs/peer-link.md "Shared sessions —
 * CONTRACT"): one of my sessions on someone else's rail, one of theirs on
 * mine.
 *
 * A peer's shared session arrives in `GET /api/instances` as a row titled
 * `<peer>::<title>` — the shape of another tailnet device's rows — with
 * `device` (the peer's name on this rail), `peer_link`, `peer_mode` and
 * `remote_peer: true`. Its terminal websocket is the usual
 * `/api/instances/<title>/terminal` (the server bridges it over the link),
 * and every owner-only action is off for it (the server also sets
 * `peer_share`, which the action tables already honour; anything else on the
 * row answers 409).
 *
 * Two modes, the words People and the rail use for them:
 *  - "gated": the link's sandboxed shared folder — they type into a session
 *    confined to that folder;
 *  - "full": the session as it is — they type into my agent, on my machine
 *    (a guest link's trust).
 *
 * Presence: while one of THEIR rows is selected here, this page tells the
 * server every 30 s (`POST /api/instances/<peer>::<title>/focus`, which
 * becomes `session_focus` on the wire), and `{left: true}` once the
 * selection moves off — so their rail shows our name on it. */

import type { Instance } from "../api/types";
import { api } from "../api/client";
import { useUi } from "../state/store";

export type PeerMode = "gated" | "full";

/** The fields a peer row carries beyond an ordinary Instance (declared here,
 * not in api/types.ts). */
export interface PeerRowFields {
  remote_peer?: boolean;
  peer_link?: string;
  peer_mode?: PeerMode | string;
  device_label?: string;
  display_title?: string;
  /** Not served now — its link dropped; remembered for a minute
   * (installPeerRowMemory below). Never set by the server. */
  peer_offline?: boolean;
}

export type PeerRow = Instance & PeerRowFields;

/** What I share on a link (`GET /api/peer/links/<id>/sessions` → mine). */
export interface MySharedSession {
  title: string;
  mode: PeerMode;
  since: number;
  /** The session a gated share was cut from ("" for a full share). */
  source: string;
  /** The session exists (and, gated, is still the link's shared folder). */
  live: boolean;
  /** They have its terminal open now. */
  open: boolean;
  /** Their page has it selected now. */
  focused: boolean;
}

/** What they share with me (→ theirs). */
export interface TheirSharedSession {
  title: string;
  name: string;
  status: string;
  activity: string;
  stage: string;
  branch: string;
  folder: string;
  mode?: PeerMode;
  program?: string;
}

export interface SessionsView {
  mine: MySharedSession[];
  theirs: TheirSharedSession[];
  connected: boolean;
  /** Their MindFlock answered "unsupported": it needs an update. */
  their_unsupported?: boolean;
}

export function isPeerRow(i: Instance | null | undefined): i is PeerRow {
  return !!i && !!(i as PeerRow).remote_peer;
}

/** The mode in a few words (rail tooltip, People lists). */
export function modeWords(mode: string | undefined): string {
  return mode === "gated" ? "gated to its folder" : "full";
}

/** The picker's choice labels. */
export const MODE_CHOICES: { mode: PeerMode; label: string; hint: string }[] = [
  {
    mode: "gated",
    label: "Gated to its folder (sandboxed copy)",
    hint: "They get a sandboxed session on a copy of its folder and branch — nothing else on this machine.",
  },
  {
    mode: "full",
    label: "Full join (they type into it as it is)",
    hint: "They type into this very session, on this machine, as if they were you.",
  },
];

/** The row's tooltip / the group marker's: "shared by Bob · gated to its folder". */
export function peerRowTooltip(i: PeerRow): string {
  const who = i.device_label || i.device || "them";
  return `shared by ${who} · ${modeWords(String(i.peer_mode || ""))}`;
}

/** The rail chip on a peer's row: "⇄ Bob · full" (same shape as
 * lib/peer.peerChip, which is for MY shared-folder sessions). */
export function peerRowChip(i: PeerRow): { label: string; title: string; connected: boolean | null } {
  const who = i.device_label || i.device || "them";
  const gated = i.peer_mode === "gated";
  const offline = !!i.peer_offline;
  return {
    label: `⇄ ${who} · ${gated ? "gated" : "full"}`,
    // The dot: connected, or (a remembered row) offline.
    connected: !offline,
    title: offline
      ? `${peerRowTooltip(i)} — ${who} is offline; it comes back when they reconnect.`
      : `${peerRowTooltip(i)} — it runs on ${who}'s machine: open it to type into it; ` +
        "ship, push, rename and the rest are theirs.",
  };
}

/** One rail group per peer: `{device, label, link, rows}` in first-seen
 * order (the rows arrive grouped by link from the server). */
export interface PeerGroup {
  device: string;
  label: string;
  link: string;
  rows: PeerRow[];
}

export function peerGroups(rows: Instance[]): PeerGroup[] {
  const out: PeerGroup[] = [];
  const byDev = new Map<string, PeerGroup>();
  for (const r of rows) {
    if (!isPeerRow(r) || !r.device) continue;
    let g = byDev.get(r.device);
    if (!g) {
      g = { device: r.device, label: r.device_label || r.device, link: r.peer_link || "", rows: [] };
      byDev.set(r.device, g);
      out.push(g);
    }
    g.rows.push(r);
  }
  return out;
}

/** The sessions the "Share a session…" picker offers: this device's ROOT
 * sessions — not another device's or a peer's row, not one a feature keeps
 * off the rail, not one still being created, not a guest's crossed-out
 * stub, not a sub-session (it rides with its parent). Mirrors GuestLinks. */
export function shareableSessions(instances: Instance[] | undefined): Instance[] {
  return (instances || []).filter(
    (i) => !i.device && !i.hidden_by && !i.pending && !i.locked && !i.parent && !isPeerRow(i)
  );
}

/** Whether the picker's Share button may be pressed: a session picked, and a
 * full share needs the "I understand" tick (gated does not — its sandbox is
 * the protection). */
export function canShare(opts: { title: string; mode: PeerMode; understood: boolean; busy?: boolean }): boolean {
  if (opts.busy || !opts.title) return false;
  return opts.mode === "gated" || opts.understood;
}

/** One line per thing I share: "api-fix · full · they have it open". */
export function mineLine(m: MySharedSession): string {
  const bits = [m.title, modeWords(m.mode)];
  if (!m.live) bits.push("closed");
  else if (m.open) bits.push("they have it open");
  else if (m.focused) bits.push("they are looking at it");
  return bits.join(" · ");
}

/** One line per thing they share: "API fix · gated to its folder · working". */
export function theirsLine(t: TheirSharedSession): string {
  const bits = [t.name || t.title, modeWords(t.mode)];
  if (t.activity) bits.push(t.activity);
  return bits.join(" · ");
}

function base(linkId: string): string {
  return "/api/peer/links/" + encodeURIComponent(linkId) + "/sessions";
}

export function fetchPeerSessions(linkId: string): Promise<SessionsView> {
  return api<SessionsView>(base(linkId));
}

export function sharePeerSession(linkId: string, title: string, mode: PeerMode): Promise<SessionsView> {
  return api<SessionsView>(base(linkId), { json: { title, mode } });
}

export function withdrawPeerSession(linkId: string, title: string): Promise<SessionsView> {
  return api<SessionsView>(base(linkId) + "/" + encodeURIComponent(title), { method: "DELETE" });
}

// --------------------------------------------------------------------------- //
// Presence: tell the peer which of their rows this page has selected
// --------------------------------------------------------------------------- //
/** The peer re-forgets a focus after 90 s of silence (core.guests' rule). */
export const FOCUS_HEARTBEAT_MS = 30_000;

/** Is `title` one of a peer's rows (`<peer>::<title>` with a peer_link)? */
function peerRowTitle(title: string | null, rows: () => Instance[]): string {
  if (!title || !title.includes("::")) return "";
  return rows().some((r) => r.title === title && isPeerRow(r)) ? title : "";
}

function post(title: string, body: unknown): void {
  api("/api/instances/" + encodeURIComponent(title) + "/focus", { json: body }).catch(() => {});
}

/** Report this page's selection of peer rows; returns the stop function.
 * `rows()` reads the current instance list (the rail passes its own). Idempotent
 * per page: a second install returns the first one's stop. */
let installed: (() => void) | null = null;
export function installPeerFocus(rows: () => Instance[]): () => void {
  if (installed) return installed;
  let current = "";
  let timer: ReturnType<typeof setInterval> | null = null;
  const sync = (focused: string | null) => {
    const next = peerRowTitle(focused, rows);
    if (next === current) return;
    if (current) post(current, { left: true });
    if (timer) clearInterval(timer);
    timer = null;
    current = next;
    if (next) {
      post(next, {});
      timer = setInterval(() => post(next, {}), FOCUS_HEARTBEAT_MS);
    }
  };
  sync(useUi.getState().focused);
  const unsub = useUi.subscribe((s) => sync(s.focused));
  const stop = () => {
    unsub();
    if (timer) clearInterval(timer);
    if (current) post(current, { left: true });
    current = "";
    installed = null;
  };
  installed = stop;
  return stop;
}

// --------------------------------------------------------------------------- //
// How a shared session FEELS on the joiner's side: the group header, the
// pane strip, the words for the bell / toasts, and a short memory of a
// peer's rows across a dropped link
// --------------------------------------------------------------------------- //
/** Who shared a row, as the rail names them. */
export function peerWho(i: PeerRow): string {
  return i.device_label || i.device || "them";
}

/** A row the server no longer lists because its link dropped, kept on the
 * rail (greyed, not clickable) for {@link OFFLINE_KEEP_MS}. */
export function isOfflinePeerRow(i: Instance | null | undefined): boolean {
  return isPeerRow(i) && !!(i as PeerRow).peer_offline;
}

/** The peer group's header words: "Erik · connected" / "Erik · offline —
 * reconnecting". A group is offline when every row in it is remembered. */
export function peerGroupWords(g: PeerGroup): { text: string; offline: boolean; title: string } {
  const offline = g.rows.length > 0 && g.rows.every(isOfflinePeerRow);
  const modes = Array.from(new Set(g.rows.map((r) => modeWords(String(r.peer_mode || "")))));
  return {
    text: g.label + " · " + (offline ? "offline — reconnecting" : "connected"),
    offline,
    title:
      `Shared by ${g.label} over a peer link · ${modes.join(", ")}` +
      (offline ? ` — ${g.label} is offline; these come back when they reconnect` : ""),
  };
}

/** The thin strip above a peer row's terminal. */
export function peerPaneStrip(i: PeerRow): { text: string; offline: boolean } {
  const who = peerWho(i);
  if (isOfflinePeerRow(i)) return { text: `${who} is offline — the terminal reconnects when they're back`, offline: true };
  return {
    text: `${who}'s session · ` + (i.peer_mode === "gated" ? "gated to its folder" : "full join — you type as them"),
    offline: false,
  };
}

/** The strip on MY session while peers are on it: "Bob is here", "Bob and
 * Ann are here", "Bob, Ann and Cy are here". "" for nobody. */
export function hereWords(names: string[]): string {
  const n = names.filter(Boolean);
  if (!n.length) return "";
  if (n.length === 1) return n[0] + " is here";
  return n.slice(0, -1).join(", ") + " and " + n[n.length - 1] + " are here";
}

/** `peer.session_shared` / `peer.session_unshared` / `peer.state` data. */
export interface PeerSessionEventData {
  link_id?: string;
  peer_name?: string;
  title?: string;
  mode?: string;
  by?: string;
  theirs?: boolean;
  connected?: boolean;
}

/** The rail key of THEIR session `title` on link `link`: the row the server
 * lists (its prefix may be numbered, "Bob (2)"), else `<peer name>::<title>`
 * — what it will be once the list catches up. */
export function peerRowKey(d: PeerSessionEventData, rows: Instance[]): string {
  const title = String(d.title || "");
  const link = String(d.link_id || "");
  const hit = rows.find((r) => isPeerRow(r) && r.peer_link === link && r.title.endsWith("::" + title));
  if (hit) return hit.title;
  for (const r of lastSeen.get(link) || []) if (r.title.endsWith("::" + title)) return r.title;
  return (String(d.peer_name || "").trim() || "peer") + "::" + title;
}

/** Their name for a shared session (what the rail shows), from the row or
 * the memory; else the raw title. */
export function peerSessionName(d: PeerSessionEventData, rows: Instance[]): string {
  const key = peerRowKey(d, rows);
  const row = rows.find((r) => r.title === key) || (lastSeen.get(String(d.link_id || "")) || []).find((r) => r.title === key);
  return (
    (row as PeerRow | undefined)?.display_title ||
    seenNames.get(String(d.link_id || "") + "\n" + String(d.title || "")) ||
    String(d.title || "")
  );
}

/** One shared-session event as the bell's row (`text`, "" for none) and the
 * toast (`toast`, "" for none); `select` is the row a click opens (theirs).
 * `name` is the session's name as this side shows it (theirs: their name
 * for it; mine: my alias). null: not news. */
export function peerSessionNote(
  event: string,
  d: PeerSessionEventData | null | undefined,
  ctx: { name: string; rowKey?: string }
): { text: string; cls: string; toast: string; select: string } | null {
  const data = d || {};
  const who = String(data.peer_name || "").trim() || "Your peer";
  const name = ctx.name || String(data.title || "");
  if (event === "peer.session_shared") {
    if (data.theirs) {
      const text = `${who} shared ${name} with you (${modeWords(data.mode)})`;
      return { text, cls: "n-info", toast: `${who} shared ${name} with you — click to open`, select: ctx.rowKey || "" };
    }
    return { text: `You shared ${name} with ${who} (${modeWords(data.mode)})`, cls: "n-info", toast: "", select: "" };
  }
  if (event === "peer.session_unshared") {
    if (data.theirs) return { text: `${who} withdrew ${name}`, cls: "n-info", toast: "", select: "" };
    return { text: `You stopped sharing ${name} with ${who}`, cls: "n-info", toast: "", select: "" };
  }
  return null;
}

/** The toast when a peer whose sessions were on this rail comes back. */
export function peerBackToast(peerName: string): string {
  return (String(peerName || "").trim() || "Your peer") + " is back — your shared sessions are live";
}

// ---- the memory ------------------------------------------------------------ //
/** How long a dropped link's rows stay on the rail, greyed. */
export const OFFLINE_KEEP_MS = 60_000;

/** link → the peer rows the last list that had any carried. */
const lastSeen = new Map<string, PeerRow[]>();
/** link → when it dropped (cleared once its rows are listed again). */
const offlineSince = new Map<string, number>();
/** `link\n<their title>` → their name for it, kept past a withdrawal so
 * "Erik withdrew API fix" names it the way the rail did. */
const seenNames = new Map<string, string>();

/** Record what the server lists now (ghosts ignored). A link with no rows in
 * this list keeps its last rows: the poll may beat the `peer.state` that says
 * why they went. A link listed again is back. */
export function notePeerRows(rows: Instance[]): void {
  const by = new Map<string, PeerRow[]>();
  for (const r of rows) {
    if (!isPeerRow(r) || r.peer_offline || !r.peer_link) continue;
    const cut = r.title.indexOf("::");
    if (r.display_title && cut > 0) seenNames.set(r.peer_link + "\n" + r.title.slice(cut + 2), r.display_title);
    if (!by.has(r.peer_link)) by.set(r.peer_link, []);
    by.get(r.peer_link)!.push(r);
  }
  for (const [link, list] of by) {
    lastSeen.set(link, list);
    offlineSince.delete(link);
  }
}

/** The link dropped: its last rows stay, greyed, for {@link OFFLINE_KEEP_MS}. */
export function peerLinkDown(link: string, now = Date.now()): void {
  if (link && (lastSeen.get(link) || []).length && !offlineSince.has(link)) offlineSince.set(link, now);
}

/** The link is up again: true when it was down with rows on this rail (the
 * "is back" toast). The rows stay greyed until the list carries them. */
export function peerLinkUp(link: string): boolean {
  return !!link && offlineSince.has(link) && (lastSeen.get(link) || []).length > 0;
}

/** They withdrew `title` (their raw title): never remember it. */
export function forgetPeerRow(link: string, title: string): void {
  const list = lastSeen.get(link);
  if (!list) return;
  const left = list.filter((r) => !r.title.endsWith("::" + title));
  if (left.length) lastSeen.set(link, left);
  else {
    lastSeen.delete(link);
    offlineSince.delete(link);
  }
}

/** The remembered rows still within their minute, flagged `peer_offline`. */
export function offlinePeerRows(now = Date.now()): PeerRow[] {
  const out: PeerRow[] = [];
  for (const [link, since] of offlineSince) {
    if (now - since >= OFFLINE_KEEP_MS) continue;
    for (const r of lastSeen.get(link) || []) out.push({ ...r, peer_offline: true, activity: "" });
  }
  return out;
}

/** `rows` with the remembered ones added (those the server doesn't list).
 * Returns `rows` itself when that changes nothing — the cache listener below
 * relies on it to stop. */
export function withOfflinePeers(rows: Instance[], now = Date.now()): Instance[] {
  const live = rows.filter((r) => !isOfflinePeerRow(r));
  const have = new Set(live.map((r) => r.title));
  const ghosts = offlinePeerRows(now).filter((r) => !have.has(r.title));
  const before = rows.filter(isOfflinePeerRow).map((r) => r.title).join("\n");
  if (before === ghosts.map((r) => r.title).join("\n")) return rows;
  return [...live, ...ghosts];
}

/** When the next remembered row expires (ms from `now`), else null. */
export function nextOfflineExpiry(now = Date.now()): number | null {
  let soonest: number | null = null;
  for (const since of offlineSince.values()) {
    const left = since + OFFLINE_KEEP_MS - now;
    if (left > 0 && (soonest === null || left < soonest)) soonest = left;
  }
  return soonest;
}

/** Keep the remembered rows in the instances cache, so the rail and an open
 * pane both still have them while the link is down (the pane's terminal
 * keeps retrying, and reattaches once the row is served again). Returns
 * `apply`, to call after the memory changed; `stop` on `.stop`. */
interface InstancesCache {
  getQueryData<T>(key: readonly unknown[]): T | undefined;
  setQueryData<T>(key: readonly unknown[], data: T): unknown;
  getQueryCache(): {
    subscribe(fn: (e: { type: string; query: { queryKey: readonly unknown[] }; action?: { type?: string } }) => void): () => void;
  };
}
export function installPeerRowMemory(qc: InstancesCache): { apply: () => void; stop: () => void } {
  let timer: ReturnType<typeof setTimeout> | null = null;
  const apply = () => {
    const rows = qc.getQueryData<Instance[]>(["instances"]);
    if (!rows) return;
    notePeerRows(rows);
    const next = withOfflinePeers(rows);
    if (next !== rows) qc.setQueryData<Instance[]>(["instances"], next);
    if (timer) clearTimeout(timer);
    const wait = nextOfflineExpiry();
    timer = wait === null ? null : setTimeout(apply, wait + 50);
  };
  const unsub = qc.getQueryCache().subscribe((e) => {
    if (e.type === "updated" && e.query.queryKey[0] === "instances" && e.action?.type === "success") apply();
  });
  return {
    apply,
    stop: () => {
      unsub();
      if (timer) clearTimeout(timer);
    },
  };
}

/** Test seam. */
export function _resetPeerMemory(): void {
  lastSeen.clear();
  offlineSince.clear();
  seenNames.clear();
}
