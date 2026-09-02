/** An extension's sidebar bar — 100% host-rendered chrome (label + one button
 * per manifest button), so no extension code runs until a button is clicked.
 * Anatomy mirrors #assistant-bar; the bar drags and hides exactly like the
 * built-ins because it lives in the same BarSlot machinery (Sidebar.tsx). */

import { useMemo } from "react";
import { useExtensions } from "../state/queries";
import { EXT_BAR_PREFIX, type BarDef } from "../components/sidebar/barDefs";
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

export function ExtensionBar({ extId }: { extId: string }) {
  const { data: extensions } = useExtensions();
  const ext = extensions?.find((e) => e.id === extId && e.enabled);
  // A bar whose extension vanished mid-refetch collapses via .bar-slot:empty,
  // matching how the built-in bars handle their own unavailability.
  if (!ext) return null;
  return (
    <div className="ext-bar" id={"ext-bar-" + ext.id}>
      <span className="ext-label">{ext.extension.bar_label || ext.label}</span>
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
