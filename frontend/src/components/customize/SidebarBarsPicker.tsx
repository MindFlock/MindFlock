/** The Sidebar tab of Customize: which bars show in the sidebar.
 *
 * The checklist the footer's popover used to hold, moved whole: the same
 * `orderedBars(barOrder, extensionBars)` order the sidebar renders (extension
 * bars included), the same `toggleBarHidden`, persisted as `mf_hiddenbars`.
 * Availability gating still applies on top — a checked bar can stay absent (the
 * Tickets bar with no tracker connected) — so each built-in bar carries a
 * static note saying when it shows, with a door to the place that makes it
 * show. The notes are always true, never computed: a line that read live
 * state would be one more thing to disagree with the bar itself. */

import { useUi, type DialogName } from "../../state/store";
import { useExtensionBarDefs } from "../../extensions/ExtensionBar";
import { orderedBars } from "../sidebar/barDefs";

interface BarNote {
  text: string;
  link?: { label: string; dialog: DialogName; target?: string };
}

const NOTES: Record<string, BarNote> = {
  usage: { text: "What your sessions cost" },
  ingestion: {
    text: "Appears once a ticket source is connected",
    link: { label: "Intake → Tickets", dialog: "intake", target: "tickets" },
  },
  "pr-review": {
    text: "Appears once a repository is added",
    link: { label: "Intake → Pull requests", dialog: "intake", target: "prs" },
  },
  "issue-handling": {
    text: "Appears once a repository is added",
    link: { label: "Intake → Issues", dialog: "intake", target: "issues" },
  },
  verify: {
    text: "Appears once Verify tracks a repository or has a checklist",
    link: { label: "Open Verify", dialog: "verify" },
  },
  assistant: { text: "Chat, a todo list, and its editable agent file" },
  prompts: {
    text: "Your saved prompts — pick one to paste it into the session you are in",
    link: { label: "Manage prompts", dialog: "prompts" },
  },
};

const EXTENSION_NOTE: BarNote = { text: "From an extension" };

export function SidebarBarsPicker() {
  const hiddenBars = useUi((s) => s.hiddenBars);
  const toggleBarHidden = useUi((s) => s.toggleBarHidden);
  const barOrder = useUi((s) => s.barOrder);
  const openDialogFor = useUi((s) => s.openDialogFor);
  // Extension bars appear here too — same defs the sidebar renders, so the
  // list order mirrors the live order, extensions included.
  const extBars = useExtensionBarDefs();

  return (
    <div id="customize-sidebar" className="cz-bars">
      <h3 className="cz-heading">Show in the sidebar</h3>
      <ul className="cz-bar-list">
        {orderedBars(barOrder, extBars).map((b) => {
          const note = NOTES[b.key] || EXTENSION_NOTE;
          return (
            <li key={b.key} className="cz-bar" data-bar={b.key}>
              <label className="cz-bar-check">
                <input
                  type="checkbox"
                  checked={!hiddenBars.has(b.key)}
                  onChange={() => toggleBarHidden(b.key)}
                />
                <span className="cz-bar-name">{b.label}</span>
              </label>
              <span className="cz-bar-note muted">
                {note.text}
                {note.link && (
                  <>
                    {" · "}
                    <button
                      type="button"
                      className="linklike cz-bar-link"
                      onClick={() => openDialogFor(note.link!.dialog, note.link!.target ?? null)}
                    >
                      {note.link.label}
                    </button>
                  </>
                )}
              </span>
            </li>
          );
        })}
      </ul>
      <p className="cz-foot muted">Drag a bar's ⠿ grip in the sidebar to reorder it.</p>
    </div>
  );
}
