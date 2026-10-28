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
  return {
    label: `⇄ ${who} · ${gated ? "gated" : "full"}`,
    connected: null,
    title:
      `${peerRowTooltip(i)} — it runs on ${who}'s machine: open it to type into it; ` +
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
