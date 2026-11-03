/** What's new: which release entries (whatsNew/releases.ts) to show, and when
 * the window opens by itself (docs/vision/whats-new.md). Pure — the window
 * (components/whatsNew) reads the running version, the seen version and the
 * onboarding state and asks here.
 *
 * Rules:
 *  - It pops only when an UNSEEN entry with `big: true` sits at or below the
 *    running version. Unseen = newer than the last seen version.
 *  - It then lists ALL unseen entries at or below the running version, newest
 *    first; the window folds the small ones under "Also in this release".
 *  - A fresh install (nothing seen yet AND onboarding not finished) never pops:
 *    the running version is marked seen instead — a first-time user gets the
 *    welcome tour, not a changelog.
 *  - An entry above the running version (a downgrade, a dev build behind the
 *    newest entry, an entry written ahead of its release) is never shown.
 *  - An unknown running version (a source tree: "" or "0+unknown") pops nothing
 *    and records nothing.
 * Versions compare as numbers, part by part ("0.10.0" > "0.9.9"). */

import type { WhatsNewRelease } from "../whatsNew/releases";

/** [major, minor, patch] of "1.2.3" (a leading "v" and any "-dev"/"+local"
 * tail ignored); null when it doesn't start with a number. */
export function parseVersion(raw: string | null | undefined): [number, number, number] | null {
  const m = /^v?(\d+)(?:\.(\d+))?(?:\.(\d+))?/.exec((raw || "").trim());
  if (!m) return null;
  return [Number(m[1]), Number(m[2] || 0), Number(m[3] || 0)];
}

/** <0, 0, >0 like a sort comparator. An unparseable version sorts lowest. */
export function compareVersions(a: string | null | undefined, b: string | null | undefined): number {
  const pa = parseVersion(a);
  const pb = parseVersion(b);
  if (!pa || !pb) return (pa ? 1 : 0) - (pb ? 1 : 0);
  for (let i = 0; i < 3; i++) if (pa[i] !== pb[i]) return pa[i] - pb[i];
  return 0;
}

/** A version a real release reports: "" and source-tree "0+unknown" are not. */
export function isReleaseVersion(raw: string | null | undefined): boolean {
  const v = (raw || "").trim();
  return !!parseVersion(v) && !v.startsWith("0+");
}

/** "0.11" of "0.11.0" — the window's title says the minor release. */
export function minorOf(raw: string): string {
  const p = parseVersion(raw);
  return p ? `${p[0]}.${p[1]}` : raw;
}

/** Entries newest first (the file is written that way; this makes sure). */
function sorted(releases: readonly WhatsNewRelease[]): WhatsNewRelease[] {
  return [...releases].sort((a, b) => compareVersions(b.version, a.version));
}

export interface WhatsNewInput {
  releases: readonly WhatsNewRelease[];
  /** The running engine's version. */
  running: string | null | undefined;
  /** The last version this person has seen What's new for (null = never). */
  lastSeen: string | null | undefined;
  /** Past first-run: the welcome tour seen, or a session ever started. */
  onboarded: boolean;
}

export interface WhatsNewPlan {
  /** Open the window by itself. */
  pop: boolean;
  /** What it lists, newest first (empty when it doesn't pop). */
  entries: WhatsNewRelease[];
  /** Write this as the seen version now, without showing anything (a fresh
   * install), or null. */
  markSeen: string | null;
}

/** Decide what happens at page load (see the rules at the top). */
export function planWhatsNew(input: WhatsNewInput): WhatsNewPlan {
  const none: WhatsNewPlan = { pop: false, entries: [], markSeen: null };
  if (!isReleaseVersion(input.running)) return none;
  const running = input.running!.trim();
  const seen = isReleaseVersion(input.lastSeen) ? input.lastSeen!.trim() : null;
  if (!seen) {
    // Never seen anything: a fresh install marks the version seen; someone
    // who has used MindFlock before (an update from before What's new) sees
    // what's new up to now.
    if (!input.onboarded) return { pop: false, entries: [], markSeen: running };
  }
  const entries = sorted(input.releases).filter(
    (r) => compareVersions(r.version, running) <= 0 && (!seen || compareVersions(r.version, seen) > 0)
  );
  if (!entries.some((r) => r.big)) return none;
  return { pop: true, entries, markSeen: null };
}

/** The entries from the newest one down to (and including) the newest big
 * one; the newest entry alone when none is big. */
function sinceNewestBig(newestFirst: readonly WhatsNewRelease[]): WhatsNewRelease[] {
  const i = newestFirst.findIndex((r) => r.big);
  return i < 0 ? newestFirst.slice(0, 1) : newestFirst.slice(0, i + 1);
}

/** What the window lists when someone opens it by hand (the palette, Settings
 * → Help, the version in the top bar): the latest release's entries — from the
 * newest at or below the running version down to the newest big one. A dev
 * build with no release version reads every entry as at or below it. */
export function reopenEntries(releases: readonly WhatsNewRelease[], running: string | null | undefined): WhatsNewRelease[] {
  const cap = isReleaseVersion(running) ? running!.trim() : null;
  const below = sorted(releases).filter((r) => !cap || compareVersions(r.version, cap) <= 0);
  return sinceNewestBig(below);
}

/** The higher of two seen versions, either possibly missing — the version to
 * store when the window closes is laterVersion(running, lastSeen), so a
 * downgrade never re-arms a newer release. */
export function laterVersion(a: string | null | undefined, b: string | null | undefined): string | null {
  const x = isReleaseVersion(a) ? a!.trim() : null;
  const y = isReleaseVersion(b) ? b!.trim() : null;
  if (!x || !y) return x || y;
  return compareVersions(x, y) >= 0 ? x : y;
}

/** The window's title: "What's new in MindFlock 0.11" — the minor release of
 * the newest big entry it shows (else the newest entry). */
export function whatsNewTitle(entries: readonly WhatsNewRelease[]): string {
  const lead = entries.find((r) => r.big) || entries[0];
  return lead ? `What's new in MindFlock ${minorOf(lead.version)}` : "What's new in MindFlock";
}

/** Split for the window: the big entries drawn in full, newest first, and
 * the small ones folded under "Also in this release". */
export function splitEntries(entries: readonly WhatsNewRelease[]): { big: WhatsNewRelease[]; small: WhatsNewRelease[] } {
  return { big: entries.filter((r) => r.big), small: entries.filter((r) => !r.big) };
}

/** Only features whose action id is known keep their button (an unknown id
 * draws no button, never a crash). */
export function knownAction<T extends { action?: { run: string } }>(f: T, known: (id: string) => boolean): T["action"] | undefined {
  return f.action && known(f.action.run) ? f.action : undefined;
}
