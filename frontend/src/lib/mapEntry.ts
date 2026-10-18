/** Where each way into the Map lands, and what the Map tab's badge says.
 *
 * Every "Open map" goes through `useUi.openMap(title, focus)` (state/store);
 * these pick the focus — the file and the part of the Map — from what the
 * caller already holds: a push/commit refusal's breach list, a red-zone
 * event, the rail's zone chip. A breach lands on Breaches and its first file;
 * a block outside a green zone on Scope and the blocked file; a plain open
 * gets no focus. Pure. */

import type { MapSection } from "../state/store";

export interface MapTarget {
  path?: string;
  section?: MapSection;
}

/** A push / PR / merge refusal or a commit warning: the Breaches section, on
 * the first breached file. */
export function breachFocus(breaches: ReadonlyArray<{ path: string }>): MapTarget {
  return { section: "breaches", path: breaches[0]?.path || undefined };
}

/** The `data` of a session.red_zone_* event (backend/web/core/red_zone_monitor.py). */
export interface RedZoneEventData {
  paths?: string[];
  kind?: string;
  push?: boolean;
}

/** A red-zone toast's click: a breach (or a push refused for one) → Breaches;
 * an edit refused outside the green zone(s) → Scope; a keep-out block → Zones;
 * always on the first file the event names. */
export function redZoneEventFocus(event: string, data: RedZoneEventData | null | undefined): MapTarget {
  const d = data || {};
  const path = Array.isArray(d.paths) && d.paths.length ? String(d.paths[0]) : undefined;
  if (event === "session.red_zone_breached" || (event === "session.red_zone_blocked" && d.push))
    return { section: "breaches", path };
  if (event === "session.red_zone_blocked") return { section: d.kind === "green" ? "scope" : "zones", path };
  return { section: "zones" };
}

/** The one file a toast can offer to Allow: an edit refused outside the
 * green zone(s), naming exactly one path. Anything else → null (several
 * files, a keep-out zone, a push). */
export function allowableBlock(event: string, data: RedZoneEventData | null | undefined): string | null {
  const d = data || {};
  if (event !== "session.red_zone_blocked" || d.push || d.kind !== "green") return null;
  const paths = Array.isArray(d.paths) ? d.paths.filter(Boolean) : [];
  return paths.length === 1 ? String(paths[0]) : null;
}

/** The last part of a path, for a button that must stay short. */
export function baseName(path: string): string {
  const parts = path.split("/").filter(Boolean);
  return parts[parts.length - 1] || path;
}

/** The zone chip (lib/codemapSeen redZoneChip) → where its click lands: the
 * breaches it counts, the scope a green session is held to, a pending plan,
 * else the zones. */
export function zoneChipFocus(
  rz: { breaches?: number; mode?: string | null } | null | undefined,
  chip: { cls: string; section?: MapSection } | null
): MapTarget {
  if ((rz?.breaches || 0) > 0) return { section: "breaches" };
  if (chip?.section === "plan" || (chip && /plan/.test(chip.cls))) return { section: "plan" };
  if (rz?.mode === "green") return { section: "scope" };
  return { section: "zones" };
}

export interface MapTabBadge {
  cls: string;
  label: string;
  /** What a screen reader hears after "Map". */
  title: string;
}

/** The Map tab's badge: the zone chip when it says something that needs you
 * (a breach, an unseen block, the guard off, a plan waiting), else a quiet
 * count of changed files, else the calm chip (🛡 / ✓) — or nothing. */
export function mapTabBadge(chip: { cls: string; label: string; title: string } | null, changedFiles: number): MapTabBadge | null {
  if (chip && chip.cls !== "rz-ok") return chip;
  const n = Math.max(0, changedFiles || 0);
  if (n > 0)
    return { cls: "map-changed", label: n > 99 ? "99+" : String(n), title: `${n} changed file${n === 1 ? "" : "s"}` };
  return chip;
}

/** Whether the Map has anything to show yet: changed files, or any zone state
 * (zones, breaches, a block, a chip). The calm first hour folds the Map tab
 * into ⋯ until it does. */
export function mapHasState(
  inst: {
    diff_stat?: { files?: number } | null;
    redzone?: { zones?: number; breaches?: number; last_block_ts?: number | null } | null;
  },
  chip: unknown
): boolean {
  const rz = inst.redzone;
  return (
    (inst.diff_stat?.files || 0) > 0 ||
    !!chip ||
    (!!rz && ((rz.zones || 0) > 0 || (rz.breaches || 0) > 0 || (rz.last_block_ts || 0) > 0))
  );
}
