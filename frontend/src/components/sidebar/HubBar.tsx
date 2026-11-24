/** The sidebar's Shared files row (components/hub; lib/hub.ts): the label,
 * a calm count of what an agent or your phone added since you last looked,
 * and two buttons — Open (the drawer, as Alt+H does) and Add (the drawer,
 * straight into its file picker). Anatomy mirrors the extension bars beside
 * it (#assistant-bar / .ext-bar), so it drags and hides like the others
 * (Customize → Sidebar, key "hub").
 *
 * The count is never a "needs you" badge: nothing in Shared files waits on
 * anyone, so it wears the extension bars' calm accent pill, never the red.
 * A guest link has no Shared files: the row draws nothing (`.bar-slot:empty`
 * collapses the slot).
 *
 * In a narrow window the sidebar is a drawer drawn over the page, and the
 * Shared files drawer would open UNDER it: opening it from here closes the
 * sidebar drawer first (as a dialog opened from it does). */

import { useEffect, useState } from "react";
import { useConfig } from "../../state/queries";
import { useUi } from "../../state/store";
import { newCount, useHub, useHubUi } from "../../lib/hub";
import { keymapVersion, subscribeKeymap } from "../../lib/keymap";
import { inShell } from "../../lib/shell";
import { usableShortcuts } from "../palette/ShortcutsSheet";

/** The row's tooltip (the drawer's own words for what it is). */
export const HUB_TITLE = "Shared files — files, pictures and text every session can use";

/** Close the sidebar when it is a narrow window's drawer (it would cover
 * the Shared files drawer). */
function leaveNarrowDrawer(): void {
  const ui = useUi.getState();
  if (ui.narrow && !ui.sidebarHidden) ui.toggleSidebar();
}

export function HubBar() {
  const guest = !!useConfig().data?.guest;
  const open = useHubUi((s) => s.open);
  const items = useHub(!guest).data?.items;
  // The tooltip's shortcut comes from the live keymap (a rebind included).
  const [, setKeysVer] = useState(keymapVersion());
  useEffect(() => subscribeKeymap(() => setKeysVer(keymapVersion())), []);
  if (guest) return null;
  const count = open ? 0 : newCount(items || []);
  const k = usableShortcuts("hub", !inShell());
  const key = k.length ? " (" + k.join(" / ") + ")" : "";
  const countLine = count + " added by an agent or your phone since you last looked";
  return (
    <div id="hub-bar" title={HUB_TITLE}>
      <span className="hb-label">
        <span className="hb-label-text">Shared files</span>
        {count > 0 && (
          <span className="hb-new" title={countLine} aria-label={countLine}>
            {count > 99 ? "99+" : count}
          </span>
        )}
      </span>
      <span className="hb-actions">
        <button
          id="hub-open-btn"
          type="button"
          className={"as-toggle" + (open ? " active" : "")}
          aria-pressed={open}
          title={(open ? "Close Shared files" : "Open Shared files") + key}
          onClick={() => {
            if (!open) leaveNarrowDrawer();
            useHubUi.getState().toggle();
          }}
        >
          Open
        </button>
        <button
          id="hub-add-btn"
          type="button"
          className="as-toggle"
          title="Add files to Shared files — or drop or paste them in the open drawer"
          onClick={() => {
            leaveNarrowDrawer();
            useHubUi.getState().openAdd();
          }}
        >
          Add
        </button>
      </span>
    </div>
  );
}
