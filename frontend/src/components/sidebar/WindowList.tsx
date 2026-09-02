/** Open windows as sidebar rows — full members of the session list.
 *
 * Every non-session window in the grid (MindFlock logs, system logs, the
 * assistant chat, verify watch windows, extension panes like a database table)
 * gets a row in the SAME list as the sessions, with no heading of its own:
 * these are windows, and the rail is the list of windows. The rows are
 * first-class: they carry the same Alt+N number badge, drag to reorder among
 * the sessions (ONE saved order holds both — a window's order key is its grid
 * sentinel, the same key it already answers to in the MRU and the grid rows),
 * select on click exactly as a session row does, and close on ✕ (the pane
 * heads carry the same ✕; both run the same close).
 *
 * Verify SESSIONS stay off the rail on purpose (they are not work); this lists
 * their WINDOWS, which is a different statement: "a watch window is open" —
 * closing the row closes the window, never the run. Rendered inside the
 * #instance-list <ul> so the rows inherit the .inst vocabulary. */

import { useUi, windowKey, type SpecialKind } from "../../state/store";
import { closeExtPaneByKey } from "../../extensions/host";
import { selectWindow } from "../../lib/sessionActions";
import { rowDndProps, type RowDndCallbacks } from "./rowDnd";

const FIXED_TITLES: Record<SpecialKind, string> = {
  logs: "MindFlock logs",
  syslogs: "System logs",
  chat: "Assistant",
};

export interface WindowRow {
  /** The window's order key AND grid sentinel (windowKey) — the one name it
   * goes by everywhere: the saved drag order, the MRU, the grid rows, and the
   * pane's data-title. */
  key: string;
  title: string;
  /** Small kind chip; "" for the fixed windows whose titles say it already. */
  kind: string;
  close(): void;
}

/** Every open window as a rail row, in open order. The ONE builder — the
 * sidebar renders these and the keymap host numbers their keys, so Alt+N and
 * the badges can never disagree about what the Nth row is. */
export function windowRows(s: {
  specialOpen: SpecialKind[];
  verifyPanes: string[];
  extPanes: Array<{ key: string; title: string }>;
}): WindowRow[] {
  return [
    ...s.specialOpen.map((kind) => ({
      key: windowKey(kind),
      title: FIXED_TITLES[kind],
      kind: "",
      close: () => useUi.getState().toggleSpecial(kind),
    })),
    ...s.verifyPanes.map((session) => ({
      key: windowKey("verify", session),
      title: session,
      kind: "verify",
      // Closes the WINDOW; the verify run keeps going and the Verify dialog
      // can reopen it for as long as the session exists.
      close: () => useUi.getState().closeVerifyPane(session),
    })),
    ...s.extPanes.map((p) => ({
      key: windowKey("ext", p.key),
      title: p.title,
      kind: "",
      // Through the host, not the store: the keep-alive body behind the pane
      // must be disposed along with its grid slot.
      close: () => closeExtPaneByKey(p.key),
    })),
  ];
}

interface ItemProps extends RowDndCallbacks {
  row: WindowRow;
  idx: number;
  onScreen: boolean;
  dropCue: "above" | "below" | null;
}

/** One window row — the same shape as a session row (grip, Alt+N number,
 * status lane, title, chip, ✕), minus everything that only means anything for
 * a session (stage chips, the actions menu, rename). */
export function WindowRowItem({ row, idx, onScreen, dropCue, ...dnd }: ItemProps) {
  const num = idx < 9 ? String(idx + 1) : "";
  return (
    <li
      className={
        "inst window-row" + (onScreen ? " active" : "") + (dropCue ? ` drop-${dropCue}` : "")
      }
      data-title={row.key}
      {...rowDndProps(row.key, dnd)}
    >
      <div className="inst-row" onClick={() => selectWindow(row.key)}>
        <span className="grip" title="Drag to reorder">⠿</span>
        <span className="idx" title={num ? `Ctrl+${num} / Alt+${num} to focus` : ""}>{num}</span>
        <span className="win-icon" aria-hidden="true">
          ▦
        </span>
        <span className="meta">
          <span className="title" title={row.title}>
            {row.title}
          </span>
        </span>
        {row.kind && <span className="stagechip win-kind">{row.kind}</span>}
        <button
          className="kill"
          title="Close window"
          onClick={(e) => {
            e.stopPropagation();
            row.close();
          }}
        >
          ✕
        </button>
      </div>
    </li>
  );
}
