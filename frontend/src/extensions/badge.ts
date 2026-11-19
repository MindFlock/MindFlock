/** An extension bar's badge (ExtensionSpec.badge): the pure decisions behind
 * the count pill ExtensionBar draws after the bar's label — split out so
 * vitest covers them in node. The host fetches the manifest's `badge` path
 * with the app's api() wrapper (no extension code runs for it), refetches it
 * about every minute and on any `addon.<ext id>.*` bus event, and shows
 * nothing at all when the answer is missing, malformed, zero or an error. */

/** How often a bar's badge is refetched without an event. */
export const BADGE_POLL_MS = 60_000;

/** What the pill shows: its text and its tooltip. */
export interface BadgeView {
  count: number;
  /** "99+" past 99, so the pill keeps its width. */
  text: string;
  title: string;
}

/** A badge answer (`{count: n, title?: "…"}`) → the pill, or null for none:
 * anything that is not a positive finite count draws nothing. */
export function badgeView(resp: unknown): BadgeView | null {
  if (!resp || typeof resp !== "object") return null;
  const r = resp as { count?: unknown; title?: unknown };
  const n = typeof r.count === "number" && Number.isFinite(r.count) ? Math.floor(r.count) : 0;
  if (n <= 0) return null;
  const title = typeof r.title === "string" && r.title.trim() ? r.title.trim() : String(n);
  return { count: n, text: n > 99 ? "99+" : String(n), title };
}

/** Does this bus event mean the extension's badge may have changed? Any
 * event the extension's addon emits (`addon.<id>.<anything>`). */
export function badgeRefreshOn(extId: string, eventName: string | null | undefined): boolean {
  return !!extId && typeof eventName === "string" && eventName.startsWith("addon." + extId + ".");
}

/** The manifest's badge path, when it is a usable same-origin API path. */
export function badgePath(badge: string | null | undefined): string | null {
  return typeof badge === "string" && badge.startsWith("/") && !badge.startsWith("//") ? badge : null;
}
