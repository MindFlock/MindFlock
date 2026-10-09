/** Pure helpers for Settings → Devices ("Your devices"): the code and
 * countdown formatting, and the one-line status texts the screen shows. No
 * DOM and no fetches, so the wording is tested in node (fleet.test.ts). */

import type {
  FleetCandidate,
  FleetJoin,
  FleetMember,
  FleetRemoved,
  FleetRollout,
  FleetRolloutRow,
  FleetStatus,
  SyncStatus,
} from "../api/types";
import { relTime } from "./format";

/** Crockford base32 — what the server's invite codes are drawn from. */
// Digits + letters without I L O U, kept as two literals: one 32-symbol
// literal reads as a high-entropy secret to the repo's secret scan.
const CODE_DIGITS = "0123456789";
const CODE_LETTERS = "ABCDEFGHJKMNPQRSTVWXYZ";
const CODE_ALPHABET = [CODE_DIGITS, CODE_LETTERS].join("");

/** The server's normalize_code, mirrored so the input can show what will be
 * sent: uppercase, no spaces or dashes, and the letters people mistype for
 * digits folded the same way Crockford does (I/L→1, O→0, U→V). */
export function normalizeCode(s: string): string {
  return (s || "")
    .toUpperCase()
    .replace(/[\s-]+/g, "")
    .replace(/[IL]/g, "1")
    .replace(/O/g, "0")
    .replace(/U/g, "V");
}

/** "ABCD-EFGH" for an 8-char invite code; anything else as normalized. */
export function formatCode(s: string): string {
  const n = normalizeCode(s);
  return n.length === 8 ? n.slice(0, 4) + "-" + n.slice(4) : n;
}

/** Whether a typed code could be an invite code at all (enables the button). */
export function plausibleCode(s: string): boolean {
  const n = normalizeCode(s);
  return n.length === 8 && [...n].every((ch) => CODE_ALPHABET.includes(ch));
}

/** "9:42" until an epoch-seconds deadline; "0:00" once past. */
export function fmtCountdown(expiresAt: number, nowSec = Date.now() / 1000): string {
  const left = Math.max(0, Math.floor(expiresAt - nowSec));
  return Math.floor(left / 60) + ":" + String(left % 60).padStart(2, "0");
}

/** The invite to show: the one expiring last (the newest), if any is live. */
export function liveInvite(st: FleetStatus | null, nowSec = Date.now() / 1000) {
  const live = (st?.invites || []).filter((i) => i.expires_at > nowSec);
  return live.length ? live.reduce((a, b) => (b.expires_at > a.expires_at ? b : a)) : null;
}

/** The member row's status line. A version mismatch is said as what to do
 * about it, because settings two versions apart may not mean the same thing.
 * Two builds that both call themselves the same version (one installed from
 * `main`) differ by commit, when both report one. */
export function memberStatus(m: FleetMember, selfVersion: string, selfCommit = ""): string {
  if (m.self) return "this device";
  if (m.error) return m.error;
  if (!m.reachable) return "offline";
  if (!m.same_fleet) return "hasn't picked up the change yet";
  if (selfVersion && m.version && m.version !== selfVersion)
    return "runs " + m.version + " — this one runs " + selfVersion + "; update both to the same version";
  if (selfCommit && m.commit && m.commit !== selfCommit)
    return "runs a different build of " + (m.version || "MindFlock") + "; update both to the same release";
  return "online";
}

/** Compare two "0.7.4"-style versions (a leading v is ignored): <0, 0, >0.
 * Mirrors the server's parse_version — non-numeric junk counts as 0. */
export function cmpVersion(a: string, b: string): number {
  const parse = (v: string) =>
    String(v || "")
      .trim()
      .replace(/^v/i, "")
      .split(".")
      .map((x) => parseInt(x, 10) || 0);
  const pa = parse(a);
  const pb = parse(b);
  for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
    const d = (pa[i] || 0) - (pb[i] || 0);
    if (d) return d;
  }
  return 0;
}

/** Why a member's engine can't be updated from here ("" when it can). */
export function memberUpdateBlocker(m: FleetMember): string {
  if (m.install === "editable") return "dev checkout — updates come from git there";
  if (m.install === "other") return "not installed by install.sh — update it there";
  return "";
}

/** The small chips next to a member's name about updating it: behind the
 * newest release, can't be updated from here, or its desktop app lagging
 * its engine (the app updates itself on its next launch). */
export function memberUpdateChips(m: FleetMember, latest: string): { text: string; warn: boolean }[] {
  const chips: { text: string; warn: boolean }[] = [];
  const blocker = memberUpdateBlocker(m);
  if (blocker) chips.push({ text: blocker, warn: false });
  else if (latest && m.version && (m.self || m.reachable) && cmpVersion(latest, m.version) > 0)
    chips.push({ text: "v" + latest + " available", warn: true });
  if (m.shell_version && m.version && cmpVersion(m.version, m.shell_version) > 0)
    chips.push({ text: "desktop app v" + m.shell_version + " updates on its next launch", warn: false });
  return chips;
}

/** Members (this device included) running an older version than `latest`,
 * reachable or not counted: what "N of your devices are behind" counts. */
export function devicesBehind(members: FleetMember[] | null | undefined, latest: string): FleetMember[] {
  if (!latest) return [];
  return (members || []).filter(
    (m) => (m.self || m.reachable) && !!m.version && cmpVersion(latest, m.version) > 0
  );
}

/** The line above "Update all my devices": what is behind, or "" when
 * nothing is. */
export function updateAllLine(members: FleetMember[] | null | undefined, latest: string): string {
  const behind = devicesBehind(members, latest);
  if (!behind.length) return "";
  const updatable = behind.filter((m) => !memberUpdateBlocker(m));
  const n = behind.length;
  let line = "MindFlock v" + latest + " is out — " + n + " of your devices " + (n === 1 ? "is" : "are") + " behind.";
  if (updatable.length < n)
    line += " " + (n - updatable.length) + " can't be updated from here (see " + (n - updatable.length === 1 ? "its row" : "their rows") + ").";
  return line;
}

const ROLLOUT_STEP_WORDS: Record<FleetRolloutRow["step"], string> = {
  queued: "waiting",
  updating: "updating…",
  restarting: "restarting…",
  done: "updated",
  current: "up to date",
  skipped: "skipped",
  failed: "failed",
  not_started: "not started",
};

/** One device's line in a rollout ("Mac mini: skipped — dev checkout …"). */
export function rolloutRowText(r: FleetRolloutRow): string {
  const name = (r.host || r.key) + (r.self ? " (this device)" : "");
  const word = ROLLOUT_STEP_WORDS[r.step] || r.step;
  return name + ": " + word + (r.detail ? " — " + r.detail : "");
}

/** The rollout as one sentence ("" when there never was one). */
export function rolloutLine(u: FleetRollout | null | undefined): string {
  if (!u || u.state === "idle") return "";
  const v = "v" + (u.version || u.tag.replace(/^v/i, ""));
  if (u.state === "running") return "Updating your devices to " + v + ", one at a time…";
  if (u.state === "halted") return "Stopped updating your devices: " + (u.error || "a device failed") + ".";
  const skipped = (u.members || []).filter((r) => r.step === "skipped").length;
  return (
    "Your devices are on " + v + (skipped ? " — " + skipped + " skipped (see below)." : ".")
  );
}

/** `update.available` (server core.update_watch) as the bell row / toast:
 * which Settings screen its click opens (Devices when other devices are
 * behind, else Advanced), and the toast's wording. */
export function updateNote(data: Record<string, unknown> | null | undefined): {
  text: string;
  screen: "devices" | "advanced";
  toast: string;
} | null {
  const d = data || {};
  const latest = String(d.latest || "");
  if (!latest) return null;
  const behind = Array.isArray(d.behind) ? d.behind : [];
  const count = Number(d.count) || behind.length + (d.here ? 1 : 0);
  const text =
    String(d.detail || "") ||
    "MindFlock v" + latest + " is out" + (behind.length ? " — " + count + " of your devices are behind" : "");
  if (behind.length) return { text, screen: "devices", toast: text + " — Update them in Settings → Devices" };
  return { text, screen: "advanced", toast: text + " — Update in Settings → Advanced" };
}

/** What blocks a candidate from joining (shown instead of its buttons), or
 * "" when it can. */
export function candidateBlocker(c: FleetCandidate): string {
  if (!c.reachable) return "offline";
  if (!c.fleet_proto) return "update MindFlock on " + (c.host || c.device) + " to add it";
  return "";
}

/** The candidate's secondary line. */
export function candidateNote(c: FleetCandidate): string {
  const bits: string[] = [];
  if (c.version) bits.push(c.version);
  if (c.in_fleet && !c.same_fleet) bits.push("already one of another set of devices");
  if (c.has_token) bits.push("paired");
  return bits.join(" · ");
}

/** This device's own outgoing join, as one sentence ("" when idle). */
export function joinLine(j: FleetJoin | null | undefined): string {
  if (!j) return "";
  const host = j.host || j.device;
  switch (j.state) {
    case "waiting":
      return "Waiting for approval on " + host + (j.code ? " — code " + j.code : "");
    case "joining":
      return "Joining " + host + "…";
    case "joined":
      return "Joined " + host + ".";
    case "denied":
      return host + " said no.";
    case "expired":
      return "The request to " + host + " expired — ask again.";
    case "error":
      return j.error || "Couldn't join " + host + ".";
    default:
      return "";
  }
}

/** One device's line under Settings sync. */
export function syncDeviceLine(d: SyncStatus["devices"][number]): string {
  if (d.error) return d.error;
  if (!d.syncing) return "sync is off there";
  if (!d.last_sync) return "waiting for the first sync";
  return "in sync · " + relTime(d.last_sync);
}

/** Human label for a pinnable base path ("ui.accent" → "Accent colour"),
 * falling back to the path itself. */
export function syncLabel(path: string, sync: SyncStatus | null | undefined): string {
  const hit = sync?.syncable?.find((s) => s.path === path)?.label;
  if (hit) return hit;
  // A unit-level pin ("ticketing.sources#jira-main"): one entry of a keyed
  // list kept on this device, e.g. a ticket source whose id means something
  // else on another of your devices.
  const hash = path.indexOf("#");
  if (hash > 0) {
    const base = path.slice(0, hash);
    const id = path.slice(hash + 1);
    const baseLabel = sync?.syncable?.find((s) => s.path === base)?.label || base;
    return baseLabel + ": " + id;
  }
  return path;
}

/** The pin picker's options: everything syncable that isn't pinned yet,
 * grouped (in first-seen order) under its group heading. */
export function pinChoices(sync: SyncStatus | null | undefined): Array<{ group: string; items: { path: string; label: string }[] }> {
  const pinned = new Set(sync?.pinned || []);
  const groups = new Map<string, { path: string; label: string }[]>();
  for (const s of sync?.syncable || []) {
    if (pinned.has(s.path)) continue;
    const g = s.group || "Other";
    if (!groups.has(g)) groups.set(g, []);
    groups.get(g)!.push({ path: s.path, label: s.label || s.path });
  }
  return [...groups].map(([group, items]) => ({ group, items }));
}

function sentence(s: string): string {
  return s ? s[0].toUpperCase() + s.slice(1) : s;
}

/** The bell row and toast for settings.sync_paused. */
export const SYNC_PAUSED_NOTE =
  "Settings sync paused on this device — its settings look reset, so nothing was sent. Choose what to keep in Settings → Devices.";

/** The device.* events (backend fleet.py) and settings.sync_paused as one line each — the bell's row
 * text and class, and the toast's wording for the two that toast. */
export function deviceEventNote(
  event: string,
  data: Record<string, unknown> | null | undefined
): { text: string; cls: string; toast: string } | null {
  const d = data || {};
  const host = String(d.host || d.device || "A device");
  const code = String(d.code || "");
  switch (event) {
    case "device.join_requested":
      return {
        text: String(d.detail || "") || host + " wants to join" + (code ? " · code " + code : ""),
        cls: "n-warn",
        toast: host + " wants to join your devices" + (code ? " — code " + code : ""),
      };
    case "device.joined": {
      // `via` is set only on the copy THIS device emits about its own join
      // (it joined someone, or someone added it): its detail is worded from
      // here, and the screen that did it already said so — no toast.
      const detail = String(d.detail || "");
      if (d.via) {
        const via = String(d.via);
        const text = /^joined /.test(detail)
          ? "This device " + detail
          : /^added /.test(detail)
            ? "This device was " + detail
            : detail || "This device joined " + via + "'s devices";
        return { text, cls: "n-done", toast: "" };
      }
      return {
        text: detail || host + " joined your devices",
        cls: "n-done",
        toast: host + " joined your devices",
      };
    }
    case "device.removed":
      return { text: sentence(String(d.detail || "")) || host + " is no longer one of your devices", cls: "n-info", toast: "" };
    // Emitted on the device that paused (its own scan saw most settings go
    // back to defaults at once): nothing syncs either way until it's answered.
    case "settings.sync_paused":
      return {
        text: SYNC_PAUSED_NOTE,
        cls: "n-warn",
        toast: SYNC_PAUSED_NOTE,
      };
    default:
      return null;
  }
}

/** The one line every join action shows before it runs: joining takes the
 * other computer's shared settings, but only where it has one — a setting
 * this computer has and that one doesn't stays (and spreads to the others). */
export function joinSettingsNote(host: string): string {
  if (!host)
    return "This computer takes the other computer's shared settings where it has them; your own stay where it has none.";
  return "This computer takes " + host + "'s shared settings where " + host + " has them; your own stay where it has none.";
}

/** The note beside "Add to my devices" — the other way round from joining:
 * the computer being added runs the join (it adopts THIS computer's bundle
 * and starts its settings from here), so it is the one that takes the
 * settings. */
export function addPairedNote(host: string): string {
  const h = host || "The other computer";
  return h + " takes this computer's shared settings where this one has them; its own stay where this one has none.";
}

/** The rows "Join another computer" offers buttons on: never a device that is
 * already one of yours. A member whose hello hasn't caught up with the group
 * yet still comes back as a candidate (`member: true`) for a discovery
 * interval — asking it to join, or re-adding it, would act on your own
 * device. */
export function joinableCandidates(candidates: FleetCandidate[]): FleetCandidate[] {
  return (candidates || []).filter((c) => !c.member);
}

/** The free-text box ("paste the code or command"): what to POST to
 * /api/fleet/join. A whole command names its device; a bare code doesn't,
 * so it goes to the one computer it can be for — the one whose Enter-code
 * row is open, or the only one that can be joined. With several and none
 * chosen, `error` says to pick one instead of letting the server say
 * "choose the device to join" with nothing to choose from. */
export function pasteJoinBody(
  text: string,
  candidates: FleetCandidate[],
  openFor: string | null
): { body: { text: string; device?: string } | null; error: string } {
  const t = (text || "").trim();
  if (!t) return { body: null, error: "" };
  // A command or "<device> <code>": the server reads the device from it.
  if (!plausibleCode(t)) return { body: { text: t }, error: "" };
  const joinable = joinableCandidates(candidates).filter((c) => !candidateBlocker(c));
  const device =
    (openFor && joinable.some((c) => c.device === openFor) ? openFor : "") ||
    (joinable.length === 1 ? joinable[0].device : "");
  if (device) return { body: { text: t, device }, error: "" };
  return {
    body: null,
    error: joinable.length
      ? "That's just a code — use Enter code next to the computer that showed it, or paste the whole command."
      : "No other MindFlock can be joined right now — make sure it's running, then Refresh.",
  };
}

/** The toast after Approve / Add to my devices: the device is in, but the
 * server may say settings sync couldn't start on this side (`sync_error`). */
export function admitToast(name: string, ok: string, syncError: unknown): string {
  const err = typeof syncError === "string" ? syncError.trim() : "";
  return err ? name + " is one of your devices, but settings sync didn't start here: " + err : ok;
}

/** The one thing removal can't do: a single shared key can't tell a removed
 * device from a member that never heard of the removal, so a lost or stolen
 * one is only cut off everywhere — including from devices that are offline
 * now — by taking it off the tailnet. Said in the confirm, the toast and the
 * CLI. */
export function tailnetAdvice(host: string): string {
  return (
    "If " +
    (host || "it") +
    " was lost or stolen, also remove it from your tailnet in the Tailscale admin console — " +
    "that cuts it off everywhere at once, even from devices that are offline now."
  );
}

/** What Remove does, said exactly (the confirm's body). */
export function removeConfirmText(host: string): string {
  return (
    host +
    " stops getting settings sync, sign-in and ticket claims from your other devices: every " +
    "device still with you gets a new device key (one that's offline right now gets it when " +
    "it's back). Its own sessions and settings stay on it. " +
    tailnetAdvice(host)
  );
}

/** The checkbox under Remove — on by default, because a device removed for
 * being lost or stolen still holds the access tokens it was given. */
export const ROTATE_TOKENS_LABEL =
  "Also replace every device's access token (do this if it was lost or stolen — your phone will need to scan the QR again)";

/** The toast after a removal, from POST …/remove's answer. A member that was
 * offline isn't lost: it is handed the new key (under the old one, which the
 * others keep for a while) the next time one of them reaches it. */
export function removedToast(
  host: string,
  r: { missed?: string[]; rotated?: string[]; rotate_failed?: string[] } | null | undefined,
  rotateAsked: boolean
): string {
  const bits = ["Removed " + host];
  const missed = r?.missed || [];
  if (missed.length) {
    const many = missed.length > 1;
    bits.push(
      missed.join(", ") +
        (many ? " were offline — they get" : " was offline — it gets") +
        " the new key when " +
        (many ? "they're" : "it's") +
        " back"
    );
  }
  if (rotateAsked) {
    const failed = r?.rotate_failed || [];
    if (failed.length)
      bits.push("couldn't replace the access token on " + failed.join(", ") + " — do it there in Security");
    else bits.push("access tokens replaced");
  } else bits.push("access tokens it already has still work");
  return bits.join(" — ") + ". " + tailnetAdvice(host);
}

/** "10:32" today, "Oct 3, 10:32" on another day (local time). */
export function clockTime(ts: number, nowSec = Date.now() / 1000): string {
  const d = new Date(ts * 1000);
  const n = new Date(nowSec * 1000);
  const hm = d.getHours() + ":" + String(d.getMinutes()).padStart(2, "0");
  const sameDay =
    d.getFullYear() === n.getFullYear() && d.getMonth() === n.getMonth() && d.getDate() === n.getDate();
  if (sameDay) return hm;
  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  return MONTHS[d.getMonth()] + " " + d.getDate() + ", " + hm;
}

/** How long a removal stays on the screen: long enough to notice one you
 * didn't make, short enough that old tombstones don't pile up. */
const REMOVED_SHOWN_S = 14 * 86400;

/** A tombstone the device wrote about itself: it left (Leave, `mindflock
 * devices leave`), nobody removed it. The server says so (`left`); an older
 * one only by naming the device as its own remover. */
function isLeave(r: FleetRemoved): boolean {
  return r.left === true || (!!r.removed_by && r.removed_by === r.key);
}

/** Removals another device made, newest first, as one line each: "rig removed
 * laptop at 10:32 — if that wasn't you, remove rig from your tailnet". Every
 * member holds the same key, so a removal from a device you don't recognise
 * is the one sign of a forged one — the remover is who has to go. This
 * device's own removals are left out (it knows it made them), and so are
 * devices that left on their own (`leftLines`: nothing to suspect there). */
export function removalLines(
  st: Pick<FleetStatus, "removed" | "members" | "self"> | null | undefined,
  nowSec = Date.now() / 1000
): Array<{ key: string; text: string }> {
  const me = st?.self?.key || "";
  const hostOf = (key: string, fallback?: string) =>
    key === me
      ? "this device"
      : st?.members?.find((m) => m.key === key)?.host ||
        st?.removed?.find((r) => r.key === key)?.host ||
        fallback ||
        key;
  return (st?.removed || [])
    .filter((r) => r && r.key && r.removed_by && r.removed_by !== me && !isLeave(r))
    .map((r) => ({ r, at: Number(r.removed_at ?? r.at ?? 0) }))
    .filter(({ at }) => Number.isFinite(at) && at > 0 && nowSec - at < REMOVED_SHOWN_S)
    .sort((a, b) => b.at - a.at)
    .map(({ r, at }) => {
      const by = hostOf(r.removed_by as string, r.removed_by_host);
      const what = r.key === me ? "this device" : r.host || r.key;
      const when = clockTime(at, nowSec);
      return {
        key: r.key,
        text:
          by +
          " removed " +
          what +
          (when.includes(",") ? " on " : " at ") +
          when +
          " — if that wasn't you, remove " +
          by +
          " from your tailnet",
      };
    });
}

/** Devices that left on their own, newest first: "laptop left your devices
 * at 10:32", said plainly — a leave is the device's own doing, so there is no
 * remover to suspect. Shown as long as a removal is. */
export function leftLines(
  st: Pick<FleetStatus, "removed" | "self"> | null | undefined,
  nowSec = Date.now() / 1000
): Array<{ key: string; text: string }> {
  const me = st?.self?.key || "";
  return (st?.removed || [])
    .filter((r) => r && r.key && r.key !== me && isLeave(r))
    .map((r) => ({ r, at: Number(r.removed_at ?? r.at ?? 0) }))
    .filter(({ at }) => Number.isFinite(at) && at > 0 && nowSec - at < REMOVED_SHOWN_S)
    .sort((a, b) => b.at - a.at)
    .map(({ r, at }) => {
      const when = clockTime(at, nowSec);
      return {
        key: r.key,
        text: (r.host || r.key) + " left your devices" + (when.includes(",") ? " on " : " at ") + when,
      };
    });
}

/** Devices removed here that another of your devices has since added back
 * (its roster says they're live): this device still refuses them until you
 * allow them here too — a removal is never undone behind your back. */
export function readmittedLines(
  st: Pick<FleetStatus, "readmitted_elsewhere" | "members" | "self"> | null | undefined
): Array<{ key: string; host: string; text: string }> {
  const me = st?.self?.key || "";
  const hostOf = (key: string, fallback?: string) =>
    key === me ? "this device" : st?.members?.find((m) => m.key === key)?.host || fallback || key;
  return (st?.readmitted_elsewhere || [])
    .filter((r) => r && r.key)
    .map((r) => {
      const host = r.host || r.key;
      const by = r.by ? hostOf(r.by, r.by_host) : "another of your devices";
      return {
        key: r.key,
        host,
        text:
          host +
          " was added back on " +
          by +
          ", but it's still removed on this device — it gets no settings sync, sign-in or " +
          "ticket claims from here until you allow it.",
      };
    });
}

/** Members that hold a different key under the same group id and epoch —
 * two halves of one group that were set up apart. Marked by the server
 * (`key_conflict`), or by the error it gives the member row. */
export function keyConflicts(members: FleetMember[] | null | undefined): FleetMember[] {
  return (members || []).filter(
    (m) => !m.self && (m.key_conflict === true || /different key for your devices/i.test(m.error || ""))
  );
}

/** The toast after Security → Regenerate: the phone's QR carried the old
 * token, and — in a group — the devices' shared key was replaced with it. */
export function rotatedToast(
  r:
    | {
        rekeyed?: string[];
        missed?: string[];
        rotated?: string[];
        rotate_failed?: string[];
        fleet_error?: string;
        fleet?: { rekeyed?: string[]; missed?: string[]; rotated?: string[]; rotate_failed?: string[] };
      }
    | null
    | undefined
): string {
  const rekeyed = r?.rekeyed || r?.fleet?.rekeyed || [];
  const missed = r?.missed || r?.fleet?.missed || [];
  const rotated = r?.rotated || r?.fleet?.rotated;
  const failed = r?.rotate_failed || r?.fleet?.rotate_failed || [];
  const bits = ["Access token regenerated — scan the QR again on your phone; other browsers sign in again"];
  if (rekeyed.length) bits.push("new device key sent to " + rekeyed.join(", "));
  if (missed.length)
    bits.push(missed.join(", ") + (missed.length > 1 ? " get" : " gets") + " the new device key when back online");
  // A server that also replaces each member's own access token (so the phone
  // is signed out everywhere) says where it did and where it couldn't; one
  // that was offline or refused keeps its old token until it's done there.
  if (rotated) {
    if (rotated.length) bits.push("access token replaced on " + rotated.join(", "));
    const byHand = [...failed, ...missed.filter((k) => !failed.includes(k))];
    if (byHand.length)
      bits.push(
        "regenerate the access token on " + byHand.join(", ") + " too (Security there) — " +
          (byHand.length > 1 ? "theirs still work" : "its old one still works")
      );
  }
  if (r?.fleet_error) bits.push(r.fleet_error);
  return bits.join(" · ");
}

/** The pause banner's two ways out, as POST /api/settings/sync/resume bodies. */
export const SYNC_RESUME = {
  theirs: { keep: "theirs", label: "Use my other devices' settings" },
  mine: { keep: "mine", label: "Keep this device's" },
} as const;

/** Which of your devices runs PR review and issue handling — exactly one
 * does (the server derives it from `github.automation_device`, falling back
 * to the live member with the lowest key, so every device names the same
 * one). `here` is this device; `text` is the line the screen shows; `canMove`
 * offers "Run here" (only on this device's own screen, for itself — another
 * device is moved from its own screen). Null when no member says (an older
 * MindFlock) or the group is just this device. */
export function automationLine(
  members: FleetMember[]
): { runner: FleetMember | null; here: boolean; text: string; canMove: boolean } | null {
  if ((members || []).length < 2) return null;
  const known = members.filter((m) => typeof m.automation === "boolean");
  if (!known.length) return null;
  const on = known.filter((m) => m.automation);
  const runner = on.length === 1 ? on[0] : null;
  const here = !!runner?.self;
  if (here)
    return {
      runner,
      here,
      text:
        "This device runs PR review and issue handling for all your devices. To move them, choose " +
        "Run here in Settings → Devices on the device that should run them.",
      canMove: false,
    };
  if (runner)
    return {
      runner,
      here,
      text:
        (runner.host || runner.key) +
        " runs PR review and issue handling for all your devices — your repos and ticket sources are " +
        "shared, but only one device reviews PRs and picks up issues.",
      canMove: true,
    };
  return { runner: null, here: false, text: automationHint(members), canMove: true };
}

/** The warning when it isn't exactly one (members on an older MindFlock that
 * decide for themselves): the same PRs get reviewed once per device, or none
 * means nobody does. "" when exactly one does. Members whose MindFlock
 * doesn't say are left out. */
export function automationHint(members: FleetMember[]): string {
  const known = members.filter((m) => typeof m.automation === "boolean");
  if (known.length < 2) return "";
  const on = known.filter((m) => m.automation);
  if (on.length === 1) return "";
  if (!on.length)
    return "None of your devices runs PR review and issue handling — choose Run here on the one that should.";
  return (
    on.map((m) => m.host || m.key).join(" and ") +
    " all run PR review and issue handling, so the same PRs get reviewed more than once — choose Run here on the one that should keep it."
  );
}

/** Unpinning a ticket source kept separate at join (`ticketing.sources#<id>`)
 * isn't "take the others' value again" like any other pin: the other device
 * has a DIFFERENT source under the same id, so this device's one — and its
 * API token — is replaced by it. The confirm's sentence; "" for a pin whose
 * unpin needs no confirm. */
export function unpinReplaces(path: string, sync: SyncStatus | null | undefined): string {
  const hash = path.indexOf("#");
  if (hash <= 0 || path.slice(0, hash) !== "ticketing.sources") return "";
  const id = path.slice(hash + 1);
  const from = sync?.joined_from || "";
  const leader =
    sync?.separate?.[path] || (from ? sync?.devices?.find((d) => d.key === from)?.label || from : "");
  return (
    "This replaces this device's “" +
    id +
    "” (and its token) with " +
    (leader ? leader + "'s" : "the one your other devices have under that id") +
    ". To keep both, give one of them a different id in Settings → Tickets instead."
  );
}
