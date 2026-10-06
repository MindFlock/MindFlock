/** Customize — the extras you opt into, in one dialog: Sidebar · Prompts.
 *
 * Two things that are nobody's daily loop and so have no top-bar slot: which
 * bars the sidebar shows, and your saved prompts. Each tab IS a dialog name —
 * "customize" (Sidebar), "prompts" — so every caller that opened the old
 * Prompts dialog (New's "Manage…") lands on its tab with no change, and a tab
 * click is just openDialogFor(that name).
 *
 * Esc closes the dialog unless something inside claimed it first
 * (`defaultPrevented` — the Prompts preview, an inline edit). The listener sits
 * on window, which a keydown reaches after every document listener, so a
 * child's claim is always made before this reads it. */

import { useEffect } from "react";
import { useUi, type DialogName } from "../../state/store";
import { SidebarBarsPicker } from "./SidebarBarsPicker";
import { PromptsPanel } from "../dialogs/PromptsDialog";

type CustomizeTab = "sidebar" | "prompts";

const TABS: Array<{ key: CustomizeTab; label: string; dialog: DialogName }> = [
  { key: "sidebar", label: "Sidebar", dialog: "customize" },
  { key: "prompts", label: "Prompts", dialog: "prompts" },
];

/** The tab a dialog name opens on, or null when it is not one of Customize's. */
export function customizeTab(name: DialogName | null): CustomizeTab | null {
  if (name === "customize") return "sidebar";
  if (name === "prompts") return name;
  return null;
}

export function CustomizeDialog() {
  const tab = useUi((s) => customizeTab(s.openDialog));
  const closeDialog = useUi((s) => s.closeDialog);
  const openDialogFor = useUi((s) => s.openDialogFor);
  const open = tab !== null;

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape" || e.defaultPrevented) return;
      e.preventDefault();
      closeDialog();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, closeDialog]);

  if (!tab) return null;

  return (
    <div
      id="customize-dialog"
      className="modal"
      role="dialog"
      aria-modal="true"
      aria-labelledby="customize-title"
      onClick={(e) => {
        if (e.target === e.currentTarget) closeDialog();
      }}
    >
      <div id="customize-panel">
        <div className="ws-head">
          <h2 id="customize-title">Customize</h2>
          <span className="ik-subtitle">Extras you can switch on</span>
          <button type="button" id="customize-close" onClick={closeDialog}>
            Close
          </button>
        </div>
        <nav id="customize-tabs" aria-label="Customize">
          {TABS.map((t) => (
            <button
              key={t.key}
              type="button"
              className={"ik-tab" + (tab === t.key ? " active" : "")}
              data-customize-tab={t.key}
              aria-current={tab === t.key ? "page" : undefined}
              onClick={() => {
                if (tab !== t.key) openDialogFor(t.dialog);
              }}
            >
              {t.label}
            </button>
          ))}
        </nav>
        <div id="customize-body" data-tab={tab}>
          {tab === "sidebar" && <SidebarBarsPicker />}
          {tab === "prompts" && <PromptsPanel />}
        </div>
      </div>
    </div>
  );
}
