/** Customize → Sidebar: which bars the sidebar shows, and in what order.
 *
 * The same `orderedBars(barOrder, extensionBars)` order the sidebar renders
 * (extension bars included), the same `toggleBarHidden`, persisted as the
 * synced prefs `hidden_bars` / `bar_order`. Each row is a Customize row
 * (CustomizeRow: grip, switch, name, one line, ↑ ↓); the session list is drawn
 * as a fixed divider between them, since a bar can sit above or below it —
 * moving a bar past it moves it across the session list, exactly as dragging
 * its ⠿ grip in the sidebar does.
 *
 * Availability gating still applies on top — a switched-on bar can stay
 * absent (the Tickets bar with no tracker connected) — so each built-in bar
 * carries a static note saying when it shows, with a door to the place that
 * makes it show. The notes are always true, never computed: a line that read
 * live state would be one more thing to disagree with the bar itself. */

import { useUi, type DialogName } from "../../state/store";
import { useExtensionBarDefs } from "../../extensions/ExtensionBar";
import { SESSIONS_KEY, orderedBars, orderedSections } from "../sidebar/barDefs";
import { CustomizeRow } from "./CustomizeRow";

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
    link: { label: "Intake → GitHub PRs", dialog: "intake", target: "prs" },
  },
  "issue-handling": {
    text: "Appears once a repository is added",
    link: { label: "Intake → GitHub issues", dialog: "intake", target: "issues" },
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
  const setBarOrder = useUi((s) => s.setBarOrder);
  const openDialogFor = useUi((s) => s.openDialogFor);
  const showFilter = useUi((s) => s.showFilter);
  const setShowFilter = useUi((s) => s.setShowFilter);
  // Extension bars appear here too — same defs the sidebar renders, so the
  // list order mirrors the live order, extensions included.
  const extBars = useExtensionBarDefs();
  const extKeys = extBars.map((b) => b.key);
  const bars = orderedBars(barOrder, extBars);
  const byKey = new Map(bars.map((b) => [b.key, b]));
  // The whole section order, the session list included: what ↑ ↓ and a drop
  // rearrange, and what the list below draws (the anchor as a divider).
  const sections = orderedSections(barOrder, extKeys);

  const move = (key: string, dir: -1 | 1) => {
    const next = [...sections];
    const i = next.indexOf(key);
    const j = i + dir;
    if (i < 0 || j < 0 || j >= next.length) return;
    [next[i], next[j]] = [next[j], next[i]];
    setBarOrder(next);
  };
  const dropBefore = (dragged: string, before: string) => {
    const next = sections.filter((k) => k !== dragged);
    next.splice(next.indexOf(before), 0, dragged);
    setBarOrder(next);
  };

  return (
    <div id="customize-sidebar" className="cz-page">
      <h3 className="cz-heading">Show in the sidebar</h3>
      <ul className="cz-list" aria-label="Sidebar bars">
        {sections.map((key, i) => {
          if (key === SESSIONS_KEY)
            return (
              <li key={key} className="cz-anchor" aria-label="The session list">
                <span>Your sessions</span>
              </li>
            );
          const b = byKey.get(key);
          if (!b) return null;
          const note = NOTES[b.key] || EXTENSION_NOTE;
          return (
            <CustomizeRow
              key={b.key}
              id={b.key}
              list="bars"
              label={b.label}
              on={!hiddenBars.has(b.key)}
              onToggle={() => toggleBarHidden(b.key)}
              onMove={(dir) => move(b.key, dir)}
              onDropBefore={(dragged) => dropBefore(dragged, b.key)}
              first={i === 0}
              last={i === sections.length - 1}
              note={
                <>
                  {note.text}
                  {note.link && (
                    <>
                      {" · "}
                      <button
                        type="button"
                        className="linklike cz-link"
                        onClick={() => openDialogFor(note.link!.dialog, note.link!.target ?? null)}
                      >
                        {note.link.label}
                      </button>
                    </>
                  )}
                </>
              }
            />
          );
        })}
      </ul>
      <h3 className="cz-heading">Session list</h3>
      <ul className="cz-list">
        <li className="cz-row cz-row-plain" data-item="session-filter">
          <label className="cz-switch" htmlFor="cz-session-filter">
            <input
              id="cz-session-filter"
              type="checkbox"
              role="switch"
              checked={showFilter}
              onChange={() => setShowFilter(!showFilter)}
            />
            <span className="cz-track" aria-hidden="true" />
          </label>
          <span className="cz-text">
            <label className="cz-name" htmlFor="cz-session-filter">
              Filter box
            </label>
            <span className="cz-note">
              Filter sessions by name or branch · hidden, <kbd>/</kbd> still opens it
            </span>
          </span>
        </li>
      </ul>
    </div>
  );
}
