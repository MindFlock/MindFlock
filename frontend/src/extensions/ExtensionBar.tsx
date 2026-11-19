/** An extension's sidebar bar — 100% host-rendered chrome (label, an optional
 * count badge, one button per manifest button), so no extension code runs
 * until a button is clicked. Anatomy mirrors #assistant-bar; the bar drags and
 * hides exactly like the built-ins because it lives in the same BarSlot
 * machinery (Sidebar.tsx). */

import { useEffect, useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import { queryClient, useExtensions } from "../state/queries";
import { EXT_BAR_PREFIX, type BarDef } from "../components/sidebar/barDefs";
import { BADGE_POLL_MS, badgePath, badgeRefreshOn, badgeView } from "./badge";
import { runCommand } from "./host";

/** The extension bars to thread through orderedSections/orderedBars — enabled
 * extensions only, shared by Sidebar and FooterCustomize so the sidebar and
 * the Customize menu can never disagree about which bars exist. */
export function useExtensionBarDefs(): BarDef[] {
  const { data: extensions } = useExtensions();
  return useMemo(
    () =>
      (extensions || [])
        .filter((e) => e.enabled)
        .map((e) => ({
          key: EXT_BAR_PREFIX + e.id,
          label: e.extension.bar_label || e.label,
        })),
    [extensions]
  );
}

/** The bar's count pill (ExtensionSpec.badge; extensions/badge.ts). Polled
 * about every minute and refetched on any `addon.<id>.*` bus event. Silent on
 * failure: an error or a zero count draws nothing. */
function ExtBadge({ extId, path }: { extId: string; path: string }) {
  const queryKey = useMemo(() => ["ext-badge", extId, path], [extId, path]);
  const q = useQuery({
    queryKey,
    queryFn: () => api<unknown>(path),
    refetchInterval: BADGE_POLL_MS,
    retry: false,
  });
  useEffect(() => {
    const ev = window.mindflock?.events;
    if (!ev) return;
    return ev.subscribe("*", (env) => {
      if (badgeRefreshOn(extId, env?.event)) void queryClient.invalidateQueries({ queryKey });
    });
  }, [extId, queryKey]);
  const view = q.isError ? null : badgeView(q.data);
  if (!view) return null;
  return (
    <span className="ext-badge" title={view.title} aria-label={view.title}>
      {view.text}
    </span>
  );
}

export function ExtensionBar({ extId }: { extId: string }) {
  const { data: extensions } = useExtensions();
  const ext = extensions?.find((e) => e.id === extId && e.enabled);
  // A bar whose extension vanished mid-refetch collapses via .bar-slot:empty,
  // matching how the built-in bars handle their own unavailability.
  if (!ext) return null;
  const badge = badgePath(ext.extension.badge);
  return (
    <div className="ext-bar" id={"ext-bar-" + ext.id}>
      <span className="ext-label">
        <span className="ext-label-text">{ext.extension.bar_label || ext.label}</span>
        {badge && <ExtBadge extId={ext.id} path={badge} />}
      </span>
      <span className="ext-actions">
        {ext.extension.buttons.map((b) => (
          <button
            key={b.command}
            type="button"
            className="ext-btn"
            title={b.title || undefined}
            onClick={() => void runCommand(ext.id, b.command)}
          >
            {b.label}
          </button>
        ))}
      </span>
    </div>
  );
}
