/** Where each guest is (docs/people.md, backend.web.core.guests): the session
 * their page has selected, as a pill with their name on the owner's rail row
 * — and "follow", which makes the owner's selection move with theirs.
 *
 * Fed two ways: the rail's GuestBar polls `GET /api/guests` (each link row
 * carries `focus: {title, ts} | null`, the state for a freshly opened page),
 * and `guest.focus_changed` on the event bus moves the pill the moment the
 * guest clicks another row. A focus the server has not heard about for 90 s
 * is gone (the guest closed the tab): the server drops it from the rows, and
 * the poll drops it here.
 *
 * A PEER on one of my sessions shared over a peer link (docs/peer-link.md
 * "Shared sessions") is a pill too, keyed `peer:<link id>`: only the event
 * bus feeds it (the guest poll knows nothing of peers and keeps them), and
 * the server says `left: true` when they are gone.
 *
 * Follow is per link and lives in this browser only: following Pat means
 * every `guest.focus_changed` for Pat's link also selects that session here
 * (lib/sessionActions.selectSession, the one selection authority). Clicking
 * the pill toggles it; a guest who leaves ends it. */

import { useSyncExternalStore } from "react";
import type { GuestLink } from "../api/types";
import type { EventEnvelope } from "../state/queries";
import { displayName } from "../state/store";
import { selectSession } from "./sessionActions";
import { toast } from "./toast";

export interface GuestFocus {
  link: string;
  /** The name the guest picked (an older link: the owner's label; "" →
   * shown as "Guest"). */
  label: string;
  /** The session their page has selected. */
  title: string;
  ts: number;
}

let focus: Record<string, GuestFocus> = {};
let following: string | null = null;
const listeners = new Set<() => void>();
let bridged = false;

function notify(): void {
  for (const l of listeners) l();
}

/** The pill's text: the label the owner typed, else "Guest". */
export function guestName(label: string): string {
  return (label || "").trim() || "Guest";
}

/** The next state after a `GET /api/guests` listing — the server's word on
 * who is here; a link whose row carries no focus has left. Identity-stable
 * when nothing changed (every quiet poll). */
export function focusFromLinks(prev: Record<string, GuestFocus>, links: GuestLink[]): Record<string, GuestFocus> {
  const next: Record<string, GuestFocus> = {};
  // Peers are not guest links: only their own events move or end them.
  for (const f of Object.values(prev)) if (isPeerFocus(f.link)) next[f.link] = f;
  for (const l of links || []) {
    if (!l?.id || !l.focus?.title) continue;
    next[l.id] = { link: l.id, label: l.name || l.label || "", title: l.focus.title, ts: Number(l.focus.ts) || 0 };
  }
  const same =
    Object.keys(next).length === Object.keys(prev).length &&
    Object.values(next).every((f) => {
      const p = prev[f.link];
      return !!p && p.title === f.title && p.label === f.label && p.ts === f.ts;
    });
  return same ? prev : next;
}

/** Take the server's listing (GuestBar's poll). */
export function feedGuestFocus(links: GuestLink[]): void {
  const next = focusFromLinks(focus, links);
  if (next === focus) return;
  focus = next;
  if (following && !focus[following]) following = null; // they left
  notify();
}

/** A pill for a peer (shared session over a peer link), not a guest link. */
export function isPeerFocus(link: string): boolean {
  return link.startsWith("peer:");
}

/** One `guest.focus_changed` envelope: move the pill, and the owner's
 * selection with it when following that guest. Returns what to toast ("" for
 * nothing): a guest arriving is worth a line, a click of theirs is not.
 * `left: true` (a peer gone) takes the pill away and ends following it. */
export function applyFocusEvent(env: EventEnvelope): string {
  const d = (env?.data || {}) as { link?: string; label?: string; title?: string; joined?: boolean; left?: boolean };
  const link = String(d.link || "");
  if (link && d.left) {
    if (!focus[link]) return "";
    const { [link]: _gone, ...rest } = focus;
    focus = rest;
    if (following === link) following = null;
    notify();
    return "";
  }
  const title = String(d.title || env?.session || "");
  if (!link || !title) return "";
  focus = { ...focus, [link]: { link, label: String(d.label || ""), title, ts: Number(env.ts) || Date.now() / 1000 } };
  notify();
  if (following === link) selectSession(title, { noKeyboard: true });
  return d.joined ? `${guestName(String(d.label || ""))} joined — on ${displayName(title)}` : "";
}

/** Subscribe to the bus once per page (the owner's rail does this). */
export function installGuestFocusEvents(): void {
  const ev = window.mindflock?.events;
  if (bridged || !ev) return;
  bridged = true;
  ev.subscribe("guest.focus_changed", (env: EventEnvelope) => {
    if (typeof ev.isReplay === "function" && ev.isReplay(env)) return;
    const say = applyFocusEvent(env);
    if (say) toast(say);
  });
}

/** Follow this guest (their clicks select here too) — or stop. */
export function toggleFollow(link: string): boolean {
  following = following === link ? null : link;
  const f = following ? focus[following] : null;
  if (f) selectSession(f.title, { noKeyboard: true });
  notify();
  return following === link;
}

export function isFollowing(link: string): boolean {
  return following === link;
}

/** The guests on `title` right now (the pills for one rail row). */
export function guestsOn(title: string): GuestFocus[] {
  return Object.values(focus).filter((f) => f.title === title);
}

function subscribe(l: () => void): () => void {
  listeners.add(l);
  return () => listeners.delete(l);
}

interface Snapshot {
  focus: Record<string, GuestFocus>;
  following: string | null;
}
let snap: Snapshot = { focus, following };
function snapshot(): Snapshot {
  if (snap.focus !== focus || snap.following !== following) snap = { focus, following };
  return snap;
}

/** Every guest's focus and who is being followed; re-renders on change. */
export function useGuestFocus(): Snapshot {
  return useSyncExternalStore(subscribe, snapshot, snapshot);
}

/** Test seam. */
export function _resetGuestFocus(): void {
  focus = {};
  following = null;
  bridged = false;
  notify();
}
