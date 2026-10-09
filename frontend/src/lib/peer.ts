/** Peer links ("Work with someone", docs/peer-link.md): the shapes /api/peer
 * returns, the one join call every surface shares, and the pure wording —
 * countdowns, the bell/toast line per peer.* event, the rail chip. No DOM, so
 * the wording is tested in node (peer.test.ts).
 *
 * `joinPeerCode` / `isPeerCode` are the peer half of a future "paste any code"
 * router: an `mfp1:`/`mfp2:` code goes here, a device code to Devices. */

import { api } from "../api/client";

export interface PeerLink {
  link_id: string;
  peer_name: string;
  /** "listener": you invited them; "dialer": you joined them. */
  role: string;
  peer_addr?: string;
  sas: string;
  perms: { messages: boolean; diff: boolean; read_file: boolean };
  shared: boolean;
  share_id?: string | null;
  session_title?: string | null;
  connected: boolean;
  /** How they reach each other: "tcp" (same network / tailnet), "relay", or
   * "" when not known yet. Optional: an older server doesn't send it. */
  carrier?: string;
  /** Both people said the safety numbers match ("It matches"). */
  sas_verified?: boolean;
  /** Messages from them nobody here has read yet. */
  unread?: number;
  /** Their MindFlock version, when they said. */
  peer_app?: string | null;
}

export interface PeerRelay {
  mode: string;
  running: boolean;
  public_host: string | null;
  /** The full relay address (with its token) — local API only. */
  address: string | null;
  error: string | null;
  /** `peer.relay` as configured (`auto` unless chosen); `mode` is what it
   * resolved to. */
  setting?: string;
  cloudflared?: boolean;
}

export interface PeerInviteRow {
  invite_id: string;
  expires_in: number | null;
  /** A same-network / tailnet invite (not through the relay). */
  direct?: boolean;
}

export interface PeerStatus {
  enabled: boolean;
  display_name: string;
  fingerprint: string | null;
  sandbox: { available: boolean; reason: string };
  listen: { host: string; port: number; listening: boolean };
  relay?: PeerRelay;
  links: PeerLink[];
  invites: PeerInviteRow[];
  /** Installed agent CLIs that can run a shared folder here, default first. */
  agents?: string[];
}

export interface PeerInvite {
  invite_id: string;
  code: string;
  /** Ready to send: the code plus what to do with it. */
  message?: string;
  expires_in: number;
  host: string;
  port: number;
  relay?: string;
  direct?: boolean;
  /** The relay failed in automatic mode, so this is a direct invite that only
   * works on your network or tailnet — `text` says so in plain words. */
  fallback?: { reason: string; text: string };
}

export type PeerJoinResult = PeerLink & { reconnected?: boolean };

export interface PeerMessage {
  /** Per-link sequence number. */
  id: number;
  dir: "in" | "out";
  by: "peer" | "you" | "agent";
  text: string;
  ts: number;
  delivered_to: string | null;
}

/** How each side reaches the other. */
export type PeerReach = "auto" | "tunnel" | "direct";

const CODE_RE = /mfp[12]:[a-z2-7]+-[a-z2-7]{4}/i;

/** Whether pasted text carries a peer-link invite code anywhere in it (the
 * bare code, the whole invite message, a link). */
export function isPeerCode(text: string): boolean {
  return CODE_RE.test(text || "");
}

/** The code inside pasted text, lowercased, or "". */
export function extractPeerCode(text: string): string {
  const m = CODE_RE.exec(text || "");
  return m ? m[0].toLowerCase() : "";
}

/** What a Reconnect box holds: a fresh invite (re-pair, same link), a new
 * address (`wss://…` or `host:port`), or nothing usable. */
export function reconnectKind(text: string): "invite" | "address" | null {
  const t = (text || "").trim();
  if (!t) return null;
  if (isPeerCode(t)) return "invite";
  if (/^wss:\/\/[^\s]+$/i.test(t)) return "address";
  if (/^(\[[0-9a-f:.]+\]|[A-Za-z0-9.-]+):[0-9]{1,5}$/.test(t)) return "address";
  return null;
}

/** A short id tying an action to its `peer.progress` events ([A-Za-z0-9_-]). */
export function newOpId(): string {
  return "op" + Math.random().toString(36).slice(2, 12) + Date.now().toString(36);
}

/** Join (or re-join) with a pasted invite. Resolves to the link; rejects with
 * the server's sentence, which already says what to do next. */
export function joinPeerCode(code: string, opId?: string): Promise<PeerJoinResult> {
  return api<PeerJoinResult>("/api/peer/join", {
    json: { code: (code || "").trim(), ...(opId ? { op_id: opId } : {}) },
  });
}

/** "9:41" for a number of seconds left (never negative). */
export function fmtSecondsLeft(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  return Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0");
}

/** The line under an invite: a ticking countdown, then what to do once it
 * ran out. */
export function inviteExpiryText(secondsLeft: number): string {
  return secondsLeft > 0 ? "Expires in " + fmtSecondsLeft(secondsLeft) : "Expired — create a new one";
}

/** Plain words for a link's role. */
export function roleText(role: string): string {
  return role === "listener" ? "You invited them" : "You joined them";
}

/** Plain words for how a link connects. */
export function carrierText(carrier: string | undefined): string {
  if (carrier === "relay") return "through the relay";
  if (carrier === "tcp") return "directly (same network or tailnet)";
  return "";
}

/** The tooltip on every action a shared (sandboxed) session can't take. */
export const PEER_SANDBOX_HINT = "Runs in a sandbox — use Bring work home in Work with someone";

/** The settings screen key of "Work with someone" (kept as "peer" so old deep
 * links still route). */
export const PEER_SCREEN = "peer";

interface PeerWith {
  link_id?: string;
  name?: string;
  connected?: boolean;
}

/** The rail chip for a shared-folder session: "Shared with B" plus whether B
 * is connected — null for an ordinary session. */
export function peerChip(inst: {
  peer_share?: boolean;
  peer_with?: PeerWith | null;
}): { label: string; title: string; connected: boolean | null } | null {
  if (!inst.peer_share) return null;
  const w = inst.peer_with || null;
  const name = String(w?.name || "").trim();
  const connected = w ? !!w.connected : null;
  const label = name ? "Shared with " + name : "Shared";
  const state = connected === null ? "" : connected ? " — connected" : " — offline";
  return {
    label,
    connected,
    title:
      (name ? "A shared folder with " + name : "A shared folder") +
      state +
      ". It runs in a sandbox: ship, push, the terminal and the IDE are off — use Bring work home in Work with someone.",
  };
}

/** The peer.* events as one line each: the bell's row (`text`, `cls`) and the
 * toast (`toast`, "" for none). null for an event that is not news. */
export function peerEventNote(
  event: string,
  data: Record<string, unknown> | null | undefined
): { text: string; cls: string; toast: string } | null {
  const d = data || {};
  const name = String(d.peer_name || "").trim() || "Your peer";
  switch (event) {
    case "peer.link_added": {
      const sas = String(d.sas || "");
      const tail = sas ? " — compare safety number " + sas : "";
      const text = (d.repaired ? name + " reconnected" : name + " joined") + tail;
      // Only the inviter is surprised by it: the joiner just clicked Join.
      return { text, cls: "n-done", toast: d.role === "dialer" ? "" : text };
    }
    case "peer.link_removed":
      if (d.by !== "peer") return null;
      return { text: name + " unlinked", cls: "n-info", toast: name + " unlinked" };
    case "peer.message": {
      if (!d.stored) return null; // delivered into a shared session: that session says so
      const text = name + " sent you a message";
      const preview = String(d.text || "").trim();
      return { text: preview ? text + ": " + preview : text, cls: "n-info", toast: text };
    }
    case "peer.relay_changed": {
      const peers = Array.isArray(d.peers) ? d.peers.map(String).filter(Boolean) : [];
      const who = peers.length ? peers.join(", ") : "the people you invited";
      const text = "Your relay address changed — send " + who + " a fresh invite to reconnect";
      return { text, cls: "n-warn", toast: text };
    }
    default:
      return null;
  }
}
