/** The session chat (docs/people.md, "A small chat"; backend.web.core.chat):
 * a little floating window per session for the PEOPLE on it — the owner, a
 * guest on a guest link, a peer with it open. Agents are never in it, and
 * nothing is kept past the server's memory.
 *
 * Two halves. The pure one — windows (open / minimized, unread), the "don't
 * pop again for this person on this session after a close" memory, message
 * merging — is exported for the tests. The live one is a small store the
 * panel (components/chat/ChatPanel) renders through `useChat()`:
 *
 *  - `guest.focus_changed` with `joined: true` (someone arrived on one of my
 *    sessions) opens that session's chat — minimized if I closed it earlier
 *    in this page load, and not at all if I closed it while that same person
 *    was on it;
 *  - `session.chat` for a window that isn't open opens it minimized with an
 *    unread count (a guest's own browser opens it outright: the owner said
 *    something to them);
 *  - an open window loads `GET …/chat`, follows `session.chat` live, and
 *    polls every 15 s in case the event socket is down.
 *
 * `openChat(title)` opens one by hand — the panel's own launcher does, and
 * the rail's guest pill may later. Rows of another device (`<device>::…`)
 * never get one: their chat lives on that device, whose events don't reach
 * this page. A PEER's shared session (`<peer>::<title>`, a row with
 * `remote_peer`) does: my server carries its chat over the peer link
 * (backend.peer.chat_bridge) and emits `session.chat` for the row, so it is
 * a chat like any other — the panel registers those rows (`setPeerRows`),
 * and the card pops open when the sharer writes (a pill once I closed it). */

import { useSyncExternalStore } from "react";
import { instApi } from "../api/client";
import type { EventEnvelope } from "../state/queries";

export interface ChatMessage {
  /** "c<seq>", monotonic on the server. */
  id: string;
  /** The person id, "guest:<link>" or "peer:<link>". */
  from: string;
  name: string;
  text: string;
  ts: number;
}

export interface ChatMe {
  id: string;
  name: string;
}

export interface ChatWindow {
  title: string;
  mode: "open" | "min";
  unread: number;
}

export interface ChatState {
  /** Oldest first; the newest sits rightmost. */
  windows: ChatWindow[];
  /** Titles closed by hand in this page load: a later arrival opens them
   * minimized, not in your face. */
  closedTitles: Record<string, true>;
  /** `<person>|<title>` closed while that person was on it: they don't pop
   * it again. */
  noPop: Record<string, true>;
}

export const EMPTY_CHAT: ChatState = { windows: [], closedTitles: {}, noPop: {} };

/** How often an open chat re-reads, in case the event socket is down. */
export const CHAT_POLL_MS = 15_000;
/** The longest message the server keeps (core.chat.MAX_TEXT). */
export const CHAT_MAX = 2000;

function seq(id: string): number {
  return Number(String(id || "").replace(/^c/, "")) || 0;
}

/** `<peer>::<title>` rows on this rail → the peer's name (`setPeerRows`). */
let peerRows: Record<string, string> = {};

/** The peers' shared sessions on this rail right now (rows with
 * `remote_peer`): their chats ride the peer link. */
export function setPeerRows(rows: { title: string; name: string }[]): void {
  const next: Record<string, string> = {};
  for (const r of rows) if (r.title) next[r.title] = r.name || r.title.split("::")[0];
  peerRows = next;
}

/** The peer whose shared session `title` is, or "" (a session of mine). */
export function peerChatName(title: string): string {
  return peerRows[title] || "";
}

/** A session this page can chat on: mine, or a peer's shared session — not
 * another device's row. */
export function chatable(title: string): boolean {
  return !!title && (!title.includes("::") || title in peerRows);
}

function put(s: ChatState, w: ChatWindow): ChatState {
  const rest = s.windows.filter((x) => x.title !== w.title);
  return { ...s, windows: [...rest, w] };
}

/** Open `title`'s window: "open" shows it (and reads it: unread 0);
 * "min" adds it as a pill but never shrinks a window already open. */
export function openWindow(s: ChatState, title: string, mode: "open" | "min" = "open"): ChatState {
  const cur = s.windows.find((w) => w.title === title);
  if (mode === "min") {
    if (cur) return s;
    return put(s, { title, mode: "min", unread: 0 });
  }
  if (cur && cur.mode === "open" && cur.unread === 0) return s;
  return put(s, { title, mode: "open", unread: 0 });
}

export function minimizeWindow(s: ChatState, title: string): ChatState {
  const cur = s.windows.find((w) => w.title === title);
  if (!cur || cur.mode === "min") return s;
  return { ...s, windows: s.windows.map((w) => (w.title === title ? { ...w, mode: "min" } : w)) };
}

/** Close `title`'s window by hand. `people` are the ones on it right now
 * (link ids): none of them pops it open again in this page load. */
export function closeWindow(s: ChatState, title: string, people: string[] = []): ChatState {
  const noPop = { ...s.noPop };
  for (const p of people) if (p) noPop[p + "|" + title] = true;
  return {
    windows: s.windows.filter((w) => w.title !== title),
    closedTitles: { ...s.closedTitles, [title]: true },
    noPop,
  };
}

/** Someone (`person`, a link id) arrived on `title`. */
export function onJoined(s: ChatState, title: string, person: string): ChatState {
  if (!chatable(title) || s.noPop[person + "|" + title]) return s;
  return openWindow(s, title, s.closedTitles[title] ? "min" : "open");
}

/** A new message on `title` that isn't mine: one more unread on a
 * minimized window; a missing window opens minimized with it — or, in a
 * guest's browser, opens outright; on a peer's shared session (`peer`) too,
 * unless I closed it in this page load. */
export function onMessage(s: ChatState, title: string, opts: { guest?: boolean; peer?: boolean } = {}): ChatState {
  if (!chatable(title)) return s;
  const cur = s.windows.find((w) => w.title === title);
  if (cur) {
    if (cur.mode === "open") return s;
    return { ...s, windows: s.windows.map((w) => (w.title === title ? { ...w, unread: w.unread + 1 } : w)) };
  }
  if (opts.guest || (opts.peer && !s.closedTitles[title])) return put(s, { title, mode: "open", unread: 0 });
  return put(s, { title, mode: "min", unread: 1 });
}

/** `prev` plus `incoming`, deduped by id and in id order; `added` is what
 * was new. Identity-stable when nothing was. */
export function mergeMessages(prev: ChatMessage[], incoming: ChatMessage[]): { list: ChatMessage[]; added: ChatMessage[] } {
  const have = new Set(prev.map((m) => m.id));
  const added: ChatMessage[] = [];
  for (const m of incoming || []) {
    if (!m?.id || have.has(m.id)) continue;
    have.add(m.id);
    added.push(m);
  }
  if (!added.length) return { list: prev, added };
  const list = [...prev, ...added].sort((a, b) => seq(a.id) - seq(b.id));
  return { list, added };
}

export function totalUnread(s: ChatState): number {
  return s.windows.reduce((n, w) => n + w.unread, 0);
}

// --------------------------------------------------------------------------- //
// the live store
// --------------------------------------------------------------------------- //
let state: ChatState = EMPTY_CHAT;
let msgs: Record<string, ChatMessage[]> = {};
let me: ChatMe | null = null;
let isGuest = false;
let bridged = false;
const loading = new Map<string, Promise<void>>();
const listeners = new Set<() => void>();

function notify(): void {
  for (const l of listeners) l();
}

function setState(next: ChatState): void {
  if (next === state) return;
  state = next;
  notify();
}

/** Read `title`'s chat (all of it the first time, then what's new). Live
 * messages that weren't mine bump the unread when `count` is set. */
export function loadChat(title: string, count = false): Promise<void> {
  if (!chatable(title)) return Promise.resolve();
  const busy = loading.get(title);
  if (busy) return busy;
  const prev = msgs[title];
  const since = prev?.length ? prev[prev.length - 1].id : "";
  const p = instApi<{ messages: ChatMessage[]; me: ChatMe }>(title, "/chat" + (since ? "?since=" + encodeURIComponent(since) : ""))
    .then((r) => {
      if (r?.me?.id) me = r.me;
      const { list, added } = mergeMessages(msgs[title] || [], r?.messages || []);
      if (list !== msgs[title]) msgs = { ...msgs, [title]: list };
      if (count && prev) for (const m of added) if (m.from !== me?.id) state = onMessage(state, title, popOpts(title));
      notify();
    })
    .catch(() => {})
    .finally(() => loading.delete(title));
  loading.set(title, p);
  return p;
}

/** Open `title`'s chat (by hand: the launcher, the guest pill). */
export function openChat(title: string): void {
  if (!chatable(title)) return;
  setState(openWindow(state, title, "open"));
  void loadChat(title);
}

export function minimizeChat(title: string): void {
  setState(minimizeWindow(state, title));
}

export function closeChat(title: string, people: string[] = []): void {
  setState(closeWindow(state, title, people));
}

/** Say something on `title`. Resolves to "" when sent, else why not. */
export async function sendChat(title: string, text: string): Promise<string> {
  const body = String(text || "").trim();
  if (!body) return "";
  try {
    const m = await instApi<ChatMessage>(title, "/chat", { json: { text: body } });
    if (m?.from) me = me || { id: m.from, name: m.name };
    msgs = { ...msgs, [title]: mergeMessages(msgs[title] || [], [m]).list };
    notify();
    return "";
  } catch (e) {
    return e instanceof Error ? e.message : "Couldn't send that.";
  }
}

function popOpts(title: string): { guest: boolean; peer: boolean } {
  return { guest: isGuest, peer: !!peerRows[title] };
}

/** One `session.chat` envelope. */
export async function applyChatEvent(env: EventEnvelope): Promise<void> {
  const title = String(env?.session || "");
  const m = (env?.data || {}) as unknown as ChatMessage;
  if (!title || !m?.id) return;
  // My own server emits `session.chat` for a `::` title only for a peer's
  // shared session (its copy of their chat): one the rail hasn't listed yet.
  if (title.includes("::") && !peerRows[title] && !isGuest) {
    peerRows = { ...peerRows, [title]: title.split("::")[0] };
  }
  if (!chatable(title)) return;
  const known = !!msgs[title];
  const isNew = !(msgs[title] || []).some((x) => x.id === m.id);
  if (!known || !me) await loadChat(title); // who I am, and what came before
  msgs = { ...msgs, [title]: mergeMessages(msgs[title] || [], [m]).list };
  if (isNew && m.from !== me?.id) state = onMessage(state, title, popOpts(title));
  notify();
}

/** One `guest.focus_changed` envelope (lib/guestFocus.applyFocusEvent's
 * shape): an arrival opens the chat — in the owner's browser only. */
export function applyJoinEvent(env: EventEnvelope): void {
  const d = (env?.data || {}) as { link?: string; title?: string; joined?: boolean };
  const title = String(d.title || env?.session || "");
  if (isGuest || !d.joined || !d.link || !title) return;
  const next = onJoined(state, title, String(d.link));
  if (next === state) return;
  setState(next);
  void loadChat(title);
}

/** Subscribe to the bus once per page. False while events.js hasn't loaded
 * (call again later). */
export function installChatEvents(guest: boolean): boolean {
  isGuest = guest;
  const ev = window.mindflock?.events;
  if (bridged) return true;
  if (!ev) return false;
  bridged = true;
  const live = (env: EventEnvelope) => !(typeof ev.isReplay === "function" && ev.isReplay(env));
  ev.subscribe("session.chat", (env: EventEnvelope) => {
    if (live(env)) void applyChatEvent(env);
  });
  ev.subscribe("guest.focus_changed", (env: EventEnvelope) => {
    if (live(env)) applyJoinEvent(env);
  });
  return true;
}

/** Re-read every window (the 15 s fallback). */
export function pollChats(): void {
  for (const w of state.windows) void loadChat(w.title, true);
}

function subscribe(l: () => void): () => void {
  listeners.add(l);
  return () => listeners.delete(l);
}

interface Snapshot {
  state: ChatState;
  msgs: Record<string, ChatMessage[]>;
  me: ChatMe | null;
}
let snap: Snapshot = { state, msgs, me };
function snapshot(): Snapshot {
  if (snap.state !== state || snap.msgs !== msgs || snap.me !== me) snap = { state, msgs, me };
  return snap;
}

/** The windows, their messages and who I am; re-renders on change. */
export function useChat(): Snapshot {
  return useSyncExternalStore(subscribe, snapshot, snapshot);
}

/** The windows right now, outside React. */
export function peekChat(): ChatState {
  return state;
}

/** Test seam. */
export function _resetChat(): void {
  state = EMPTY_CHAT;
  msgs = {};
  me = null;
  isGuest = false;
  bridged = false;
  peerRows = {};
  loading.clear();
  notify();
}
