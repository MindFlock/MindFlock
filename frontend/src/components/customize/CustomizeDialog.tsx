/** Customize — two pages, switched by the tabs at the top (the last one you
 * looked at opens next time):
 *  - Sidebar: which bars the sidebar shows (Usage, Tickets, …, Assistant,
 *    Prompts), in what order, and its filter box (SidebarBarsPicker);
 *  - Pane header: which tabs and buttons a session pane's header shows, in
 *    what order, with a live preview (PaneHeaderPicker).
 * Each page has one line saying what it changes and its own "Reset to
 * default". Opened from the sidebar footer's ⚙ Customize and the palette.
 *
 * Esc closes the dialog unless something inside claimed it first
 * (`defaultPrevented`) — the Modal shell's listener sits on window, which a
 * keydown reaches after every document listener, so a child's claim is always
 * made before it reads it. */

import { useUi } from "../../state/store";
import { Modal } from "../Modal";
import { SidebarBarsPicker } from "./SidebarBarsPicker";
import { PaneHeaderPicker } from "./PaneHeaderPicker";

const PAGES = [
  {
    id: "sidebar" as const,
    label: "Sidebar",
    intro: "Choose which bars sit around your session list, and in what order.",
  },
  {
    id: "pane" as const,
    label: "Pane header",
    intro: "Choose the tabs and buttons along the top of every session window. The rest stay in its ⋯ menu.",
  },
];

export function CustomizeDialog() {
  const open = useUi((s) => s.openDialog === "customize");
  const closeDialog = useUi((s) => s.closeDialog);
  const page = useUi((s) => s.customizePage);
  const setPage = useUi((s) => s.setCustomizePage);
  const resetSidebarBars = useUi((s) => s.resetSidebarBars);
  const setPaneHeader = useUi((s) => s.setPaneHeader);
  const paneCustomized = useUi((s) => s.paneHeader !== null);
  const barsCustomized = useUi((s) => s.barsExplicit || s.barOrder.length > 0 || !s.showFilter);

  if (!open) return null;
  const cur = PAGES.find((p) => p.id === page) || PAGES[0];
  const canReset = cur.id === "pane" ? paneCustomized : barsCustomized;

  return (
    <Modal
      id="customize-dialog"
      role="dialog"
      aria-modal="true"
      aria-labelledby="customize-title"
      onClose={closeDialog}
    >
      <div id="customize-panel">
        <div className="ws-head">
          <h2 id="customize-title">Customize</h2>
          <div className="customize-tabs" role="tablist" aria-label="What to customize">
            {PAGES.map((p) => (
              <button
                key={p.id}
                type="button"
                role="tab"
                id={"customize-tab-" + p.id}
                aria-selected={p.id === cur.id}
                aria-controls="customize-body"
                className={p.id === cur.id ? "active" : ""}
                onClick={() => setPage(p.id)}
              >
                {p.label}
              </button>
            ))}
          </div>
          <button type="button" id="customize-close" onClick={closeDialog}>
            Close
          </button>
        </div>
        <div className="cz-intro">
          <p>{cur.intro}</p>
          <button
            type="button"
            id="customize-reset"
            className="linklike"
            disabled={!canReset}
            title={canReset ? "Back to what a fresh install shows" : "Already the default"}
            onClick={() => (cur.id === "pane" ? setPaneHeader(null) : resetSidebarBars())}
          >
            Reset to default
          </button>
        </div>
        <div id="customize-body" data-tab={cur.id} role="tabpanel" aria-labelledby={"customize-tab-" + cur.id}>
          {cur.id === "pane" ? <PaneHeaderPicker /> : <SidebarBarsPicker />}
        </div>
      </div>
    </Modal>
  );
}
