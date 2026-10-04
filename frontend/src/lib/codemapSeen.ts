/** When each session's Map tab was last looked at (epoch seconds ON THE
 * SERVER'S CLOCK), so the rail chip can say "a red zone blocked something you
 * haven't seen yet" and stop saying it once you have. The block time it is
 * compared with (`last_block_ts`) is the hook's time.time() on the server, and
 * the browser may be another machine (a phone, the Windows side of WSL2, whose
 * clock drifts after sleep): stamped with the browser clock, a server running
 * behind would hide every block inside the skew and one running ahead would
 * keep a seen block red. Per browser, in localStorage (`mf_codemap_seen`) —
 * what one person has looked at is not server state. Every storage access is
 * wrapped: private windows and blocked storage still render, they just forget.
 *
 * Subscribable (useSyncExternalStore) so a row clears its chip the moment the
 * Map opens, not on the next instances poll. */

const KEY = "mf_codemap_seen";
const MAX_TITLES = 200;

let cache: Record<string, number> | null = null;
const listeners = new Set<() => void>();

function load(): Record<string, number> {
  if (cache) return cache;
  try {
    const raw = localStorage.getItem(KEY);
    const v = raw ? (JSON.parse(raw) as unknown) : null;
    cache = v && typeof v === "object" ? (v as Record<string, number>) : {};
  } catch {
    cache = {};
  }
  return cache;
}

export function codemapSeenAt(title: string): number {
  return Number(load()[title]) || 0;
}

/** Now on the server's clock, epoch seconds. `skew` is server minus client,
 * measured at a poll (`live.now − Date.now()/1000`). */
export function serverNow(skew: number, clientNowS = Date.now() / 1000): number {
  return clientNowS + (Number.isFinite(skew) ? skew : 0);
}

/** Mark the Map for `title` as seen at `ts` — server-clock epoch seconds (see
 * serverNow); there is deliberately no browser-clock default. Cheap to call on
 * every poll. */
export function markCodemapSeen(title: string, ts: number): void {
  if (!Number.isFinite(ts) || ts <= 0) return;
  const cur = load();
  if ((cur[title] || 0) >= ts - 1) return;
  const next: Record<string, number> = { ...cur, [title]: ts };
  const keys = Object.keys(next);
  if (keys.length > MAX_TITLES) {
    keys.sort((a, b) => next[a] - next[b]);
    for (const k of keys.slice(0, keys.length - MAX_TITLES)) delete next[k];
  }
  cache = next;
  try {
    localStorage.setItem(KEY, JSON.stringify(next));
  } catch {
    /* storage unavailable — in-memory for this page */
  }
  listeners.forEach((l) => l());
}

export function subscribeCodemapSeen(cb: () => void): () => void {
  listeners.add(cb);
  return () => listeners.delete(cb);
}

/** The rail chip for a row's red-zone summary, or null for no chip. Pure. */
export function redZoneChip(
  rz: { zones: number; breaches: number; last_block_ts: number | null; guard: string; mode?: string | null } | null | undefined,
  seenAt: number
): { cls: string; label: string; title: string } | null {
  if (!rz) return null;
  const zones = rz.zones || 0;
  const breaches = rz.breaches || 0;
  const blockTs = rz.last_block_ts || 0;
  if (breaches > 0)
    return {
      cls: "rz-breach",
      label: "⛔" + breaches,
      title: `${breaches} red-zone file${breaches === 1 ? "" : "s"} changed on this branch — pushing is blocked until reverted. Click to open the map.`,
    };
  if (blockTs > seenAt)
    return {
      cls: "rz-breach",
      label: "⛔",
      title: "A red zone blocked an edit since you last looked. Click to open the map.",
    };
  if (zones > 0 && (rz.guard === "off" || rz.guard === "arming"))
    return {
      cls: "rz-warn",
      label: "!",
      title:
        rz.guard === "off"
          ? "Red-zone guard is off — MindFlock is re-arming it. Edits are detected, not blocked, until then."
          : "Red-zone guard is arming — it takes effect on the agent's next tool call.",
    };
  if (zones > 0 && rz.guard === "guarded" && rz.mode === "green")
    return {
      cls: "rz-ok",
      label: "✓",
      title: "Scoped to its green zones — edits outside them are blocked. Click to open the map.",
    };
  if (zones > 0 && rz.guard === "guarded")
    return {
      cls: "rz-ok",
      label: "🛡",
      title: `${zones} red zone${zones === 1 ? "" : "s"} guarded — edits there are blocked. Click to open the map.`,
    };
  // Detect-only (a provider with no hook guard) gets no shield on purpose: a
  // shield would claim a protection that isn't there. Its breaches still
  // surface through the red chip above.
  return null;
}
