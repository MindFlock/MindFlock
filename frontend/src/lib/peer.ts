/** Peer links ("People", docs/peer-link.md): the shapes /api/peer
 * returns, the one join call every surface shares, and the pure wording —
 * countdowns, the bell/toast line per peer.* event, the rail chip. No DOM, so
 * the wording is tested in node (peer.test.ts).
 *
 * `joinPeerCode` / `isPeerCode` are the peer half of a future "paste any code"
 * router: an `mfp1:`/`mfp2:` code goes here, a device code to Devices. */

import { api } from "../api/client";
import { relTime } from "./format";
import type { SettingsScreen } from "./openSettings";

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
  /** THEIR shared session as they last reported it (asked over the link
   * every ~10s while connected): null when not connected or not asked yet. */
  their_share?: { shared: boolean; agent: "running" | "stopped" | "none" | string; head?: string } | null;
  /** The link's joint task in short (null/absent: none, or an older server). */
  joint?: PeerJointSummary | null;
  /** The standing mirror of their work into one of my repos (null: off). */
  mirror?: PeerMirror | null;
}

/** Whose side something came from, as the side reading it sees it. */
export type JointWho = "me" | "peer";
export type JointState = "proposed" | "active" | "done" | "cancelled";

/** `link_view.joint` — enough for the card to know whether to fetch more. */
export interface PeerJointSummary {
  id: string;
  state: JointState;
  brief_head: string;
  contract_rev: number;
  open_proposals: number;
  mine_shared: boolean;
  theirs_shared: boolean;
}

/** A proposed edit to the contract (the whole new text), and its fate. */
export interface JointProposal {
  id: string;
  by: JointWho;
  text: string;
  at: string;
  state: "open" | "accepted" | "rejected";
}

export interface JointCheckStep {
  id: string;
  text: string;
  expect: string;
  actor: "agent" | "human";
  side: "mine" | "theirs";
}

export interface JointCheckResult {
  result: "pass" | "fail" | "blocked" | "";
  note: string;
  at: string;
  by: "agent" | "human";
}

export interface JointCheckSide {
  results: Record<string, JointCheckResult>;
  tested_sha: string;
  at: string;
}

export type JointVerdict = "verified" | "failed" | "needs_you" | "in_progress" | "empty";

/** The joint checklist (wave 2): steps split by side, each side's results. */
export interface JointCheckData {
  steps: JointCheckStep[];
  mine: JointCheckSide;
  theirs: JointCheckSide;
  verdict: JointVerdict;
}

/** GET /api/peer/links/{id}/joint → `{joint}`: one task, two halves, one
 * contract both agents follow. */
export interface PeerJoint {
  id: string;
  brief: string;
  proposed_by: JointWho;
  created: string;
  state: JointState;
  mine: { repo_path: string; branch: string; program: string; share_id: string; session_title: string };
  theirs: { repo_name: string; branch: string; program: string; shared: boolean };
  contract: { text: string; rev: number; updated: string; by: JointWho };
  proposals: JointProposal[];
  check?: JointCheckData | null;
}

/** `link_view.mirror`: their shared work fetched into my repo as a branch
 * whenever their HEAD moves. */
export interface PeerMirror {
  repo_path: string;
  branch: string;
  enabled: boolean;
  last_head: string;
  last_at: string;
  last_error: string;
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

/** The settings screen key of "People" (kept as "peer" so old deep
 * links still route). */
export const PEER_SCREEN = "peer" satisfies SettingsScreen;

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
      ". It runs in a sandbox: ship, push, the terminal and the IDE are off — use Bring work home in People.",
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
    case "peer.joint_proposed": {
      const text = name + " proposes a joint task — accept in People";
      const brief = String(d.brief || "").trim();
      return { text: brief ? text + ": " + clip(brief, 80) : text, cls: "n-info", toast: text };
    }
    case "peer.joint_contract": {
      // Only an open proposal waits on a person; a synced revision is not news.
      const p = (d.proposal || null) as { by?: string; state?: string } | null;
      if (!p || p.state !== "open") return null;
      const text = (p.by === "me" ? "Your agent" : name + "'s agent") + " proposes a contract change";
      return { text, cls: "n-info", toast: text };
    }
    case "peer.joint_state": {
      // Their answer to my proposal, or their side ending it; my own clicks
      // are not news.
      if (d.by !== "peer") return null;
      const state = String(d.state || "");
      const text =
        state === "active"
          ? name + " accepted the joint task"
          : state === "done"
          ? name + " marked the joint task done"
          : state === "cancelled"
          ? name + " cancelled the joint task"
          : "";
      if (!text) return null;
      return { text, cls: state === "cancelled" ? "n-warn" : "n-done", toast: text };
    }
    case "peer.mirror_updated": {
      const text = name + "'s work updated " + mirrorBranchText(d.branch) + " in " + repoText(d);
      return { text, cls: "n-done", toast: text };
    }
    case "peer.mirror_failed": {
      const err = String(d.error || "").trim();
      const text = "Couldn't mirror " + name + "'s work into " + repoText(d) + (err ? ": " + err : "");
      return { text, cls: "n-warn", toast: "Couldn't mirror " + name + "'s work into " + repoText(d) };
    }
    default:
      return null;
  }
}

function clip(s: string, n: number): string {
  return s.length > n ? s.slice(0, n - 1).trimEnd() + "…" : s;
}

/** The last folder name of a path ("/home/a/api" → "api"). */
export function baseName(path: string): string {
  const parts = String(path || "").replace(/[\\/]+$/, "").split(/[\\/]/);
  return parts[parts.length - 1] || String(path || "");
}

function repoText(d: Record<string, unknown>): string {
  return String(d.repo_name || "").trim() || baseName(String(d.repo_path || "")) || "your repo";
}

function mirrorBranchText(branch: unknown): string {
  return String(branch || "").trim() || "its peer/ branch";
}

/** Every peer.* event the People screen refetches on (and the bell / toasts
 * know about). */
export const PEER_LIVE_EVENTS = [
  "peer.link_added",
  "peer.link_removed",
  "peer.state",
  "peer.message",
  "peer.relay_changed",
  "peer.joint_proposed",
  "peer.joint_state",
  "peer.joint_contract",
  "peer.mirror_updated",
  "peer.mirror_failed",
] as const;

/** The peer.* events that can be news (a bell row / a toast). */
export const PEER_NEWS_EVENTS = [
  "peer.link_added",
  "peer.link_removed",
  "peer.message",
  "peer.relay_changed",
  "peer.joint_proposed",
  "peer.joint_state",
  "peer.joint_contract",
  "peer.mirror_updated",
  "peer.mirror_failed",
] as const;

// --------------------------------------------------------------------------
// Joint tasks (docs/peer-link.md → Joint tasks)
// --------------------------------------------------------------------------

/** Where a link's joint task stands, from THIS side: what the card shows. */
export type JointPhase = "none" | "proposed-by-me" | "proposed-by-peer" | "active" | "ended";

export function jointPhase(joint: Pick<PeerJoint, "state" | "proposed_by"> | null | undefined): JointPhase {
  if (!joint) return "none";
  if (joint.state === "active") return "active";
  if (joint.state === "proposed") return joint.proposed_by === "peer" ? "proposed-by-peer" : "proposed-by-me";
  return "ended";
}

/** The starter contract a new joint task's form is prefilled with. */
export const CONTRACT_TEMPLATE = [
  "## What we're building",
  "",
  "",
  "## Interfaces between the halves",
  "",
  "",
  "## Who owns what",
  "",
  "",
  "## Done means",
  "",
].join("\n");

/** The open proposals, oldest first. */
export function openProposals(joint: Pick<PeerJoint, "proposals"> | null | undefined): JointProposal[] {
  return (joint?.proposals || []).filter((p) => p.state === "open");
}

/** "/api/peer/links/<id>/joint" plus a tail ("/accept", "/contract", …). */
export function jointPath(linkId: string, tail = ""): string {
  return "/api/peer/links/" + encodeURIComponent(linkId) + "/joint" + tail;
}

/** The mirror branch a new mirror suggests: peer/<their name>. */
export function defaultMirrorBranch(peerName: string): string {
  return "peer/" + ((peerName || "work").replace(/[^A-Za-z0-9._-]+/g, "-").replace(/^-+|-+$/g, "") || "work");
}

/** An ISO time (or "") as epoch seconds, NaN when unreadable. */
export function isoSeconds(iso: string | null | undefined): number {
  return iso ? Date.parse(iso) / 1000 : NaN;
}

/** The mirror row's status line: what it is doing, and when it last did it. */
export function mirrorStatusText(m: PeerMirror, peerName: string, now = Date.now() / 1000): string {
  const where = m.branch + " in " + baseName(m.repo_path);
  if (!m.enabled) return "Mirror is off.";
  const t = isoSeconds(m.last_at);
  if (!m.last_head || !Number.isFinite(t)) return "Mirroring " + peerName + "'s work into " + where + " — waiting for their first change.";
  const ago = relTime(t, { now });
  return "Mirroring " + peerName + "'s work into " + where + " — last updated " + ago + " (" + m.last_head.slice(0, 7) + ").";
}

/** The joint checklist's verdict as words for its chip. */
export function jointVerdictText(v: JointVerdict | string | undefined): string {
  switch (v) {
    case "verified":
      return "Both halves agree";
    case "failed":
      return "A step failed";
    case "needs_you":
      return "Needs you";
    case "in_progress":
      return "Checking…";
    default:
      return "No steps yet";
  }
}

/** What a proposed contract changes, line by line: the lines it adds ("+")
 * and removes ("-"), in order, blank lines left out — so a person reviews the
 * change, not the whole contract again. A longest-common-subsequence diff;
 * contracts are short. */
export function contractLineDiff(before: string, after: string): Array<{ op: "+" | "-"; text: string }> {
  const a = String(before || "").replace(/\r\n?/g, "\n").split("\n");
  const b = String(after || "").replace(/\r\n?/g, "\n").split("\n");
  const n = a.length;
  const m = b.length;
  const lcs: number[][] = Array.from({ length: n + 1 }, () => new Array<number>(m + 1).fill(0));
  for (let i = n - 1; i >= 0; i--)
    for (let j = m - 1; j >= 0; j--) lcs[i][j] = a[i] === b[j] ? lcs[i + 1][j + 1] + 1 : Math.max(lcs[i + 1][j], lcs[i][j + 1]);
  const out: Array<{ op: "+" | "-"; text: string }> = [];
  let i = 0;
  let j = 0;
  while (i < n || j < m) {
    if (i < n && j < m && a[i] === b[j]) {
      i++;
      j++;
    } else if (j < m && (i >= n || lcs[i][j + 1] > lcs[i + 1][j])) {
      if (b[j].trim()) out.push({ op: "+", text: b[j] });
      j++;
    } else {
      if (a[i].trim()) out.push({ op: "-", text: a[i] });
      i++;
    }
  }
  return out;
}
