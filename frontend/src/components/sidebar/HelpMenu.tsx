/** The sidebar foot's "? Help": one button, one menu, where Linear, Notion
 * and Figma keep theirs — bottom left. In this order:
 *
 *   Ask how something works…  a question about the app, answered from its
 *                       docs with pointers at the real UI (lib/helpAsk)
 *   Send feedback…      the feedback box (lib/feedback.ts), unchanged
 *   Keyboard shortcuts  the shortcut sheet ("?" still opens it too)
 *   What's new          this release's notes (components/whatsNew)
 *
 * No Docs row: the app links to no documentation site, and a made-up URL is
 * worse than none.
 *
 * An agent's drafted report (the MCP's `report_issue`) waiting for the person
 * to read is a calm accent mark on the button and a "1 draft" pill on Send
 * feedback… (useFeedbackDrafts) — the toast that offered it goes away, the
 * draft must not. A guest link has no feedback and no questions (the server
 * refuses a guest both): no rows, no read.
 *
 * Behaves like the app's other menus: real buttons with role="menuitem",
 * the first focused on open, ↑/↓/Home/End move, Esc (or Tab) closes and
 * gives the keyboard back to the button, an outside click closes. Body-
 * mounted and fixed (the sidebar scrolls and clips), opening UPWARD from the
 * foot and clamped inside the window; its id is in the keymap's POPOVER_IDS
 * so a Delete or Ctrl+W pressed in it never reaches the session behind. */

import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useUi } from "../../state/store";
import { useConfig } from "../../state/queries";
import { openFeedback, useFeedbackDrafts } from "../../lib/feedback";
import { openAskHelp } from "../../lib/helpAsk";
import { openWhatsNew } from "../whatsNew/WhatsNew";
import { keymapVersion, subscribeKeymap } from "../../lib/keymap";
import { inShell } from "../../lib/shell";
import { usableShortcuts } from "../palette/ShortcutsSheet";

interface Row {
  key: string;
  label: string;
  title: string;
  /** A shortcut or a pill, drawn at the row's right end. */
  aside?: { text: string; draft?: boolean; title?: string };
  run(): void;
}

export function HelpMenu() {
  const guest = !!useConfig().data?.guest;
  const drafts = useFeedbackDrafts(!guest);
  const [open, setOpen] = useState(false);
  const btnRef = useRef<HTMLButtonElement>(null);
  const [, setKeysVer] = useState(keymapVersion());
  useEffect(() => subscribeKeymap(() => setKeysVer(keymapVersion())), []);
  const sheetKeys = usableShortcuts("sheet", !inShell());

  const draftLine =
    drafts === 1 ? "An agent drafted a report for you to read" : drafts + " reports drafted by an agent wait for you to read";
  const rows = ([
    !guest && {
      key: "ask",
      label: "Ask how something works…",
      title: "Ask a question about MindFlock: an answer from its docs, and where to find it",
      run: () => openAskHelp(),
    },
    !guest && {
      key: "feedback",
      label: "Send feedback…",
      title: drafts > 0 ? draftLine : "Tell us about a problem or an idea",
      aside: drafts > 0 ? { text: drafts === 1 ? "1 draft" : drafts + " drafts", draft: true, title: draftLine } : undefined,
      run: () => openFeedback(),
    },
    {
      key: "shortcuts",
      label: "Keyboard shortcuts",
      title: "Every keyboard shortcut" + (sheetKeys.length ? " (" + sheetKeys.join(" / ") + ")" : ""),
      aside: sheetKeys.length ? { text: sheetKeys[0] } : undefined,
      run: () => useUi.getState().openDialogFor("shortcuts"),
    },
    {
      key: "whats-new",
      label: "What's new",
      title: "This release's new features",
      run: () => openWhatsNew(),
    },
  ] as (Row | false)[]).filter((r): r is Row => !!r);

  const close = (refocus: boolean) => {
    setOpen(false);
    if (refocus) btnRef.current?.focus();
  };

  return (
    <>
      <button
        ref={btnRef}
        id="help-btn"
        type="button"
        className={"foot-link" + (drafts > 0 ? " has-draft" : "")}
        title={"Help — ask how something works, send feedback, keyboard shortcuts, what's new" + (drafts > 0 ? "\n" + draftLine : "")}
        aria-label={drafts > 0 ? "Help, " + draftLine.toLowerCase() : "Help"}
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
      >
        <span className="help-q" aria-hidden="true">
          ?
        </span>{" "}
        Help
        {drafts > 0 && <span className="help-draft-dot" aria-hidden="true" />}
      </button>
      {open && btnRef.current && <HelpMenuList anchor={btnRef.current} rows={rows} onClose={close} />}
    </>
  );
}

function HelpMenuList({ anchor, rows, onClose }: { anchor: HTMLElement; rows: Row[]; onClose(refocus: boolean): void }) {
  const menuRef = useRef<HTMLDivElement>(null);
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;

  // Above the button, right edges aligned (the button is at the foot's right
  // end); below only if a short window leaves more room there; always inside
  // the window.
  useLayoutEffect(() => {
    const m = menuRef.current;
    if (!m) return;
    const r = anchor.getBoundingClientRect();
    const h = m.offsetHeight;
    const above = r.top - 8;
    const below = window.innerHeight - r.bottom - 8;
    const top = h + 6 <= above || above >= below ? Math.max(8, r.top - 6 - h) : r.bottom + 6;
    m.style.top = Math.round(top) + "px";
    m.style.maxHeight = Math.max(120, window.innerHeight - top - 8) + "px";
    const left = Math.min(Math.max(8, r.right - m.offsetWidth), window.innerWidth - m.offsetWidth - 8);
    m.style.left = Math.round(Math.max(8, left)) + "px";
  }, [anchor]);

  useEffect(() => {
    menuRef.current?.querySelector<HTMLElement>('[role="menuitem"]')?.focus({ preventScroll: true });
    const inside = (t: EventTarget | null) =>
      t instanceof Node && (!!menuRef.current?.contains(t) || anchor.contains(t));
    const onDown = (e: MouseEvent) => {
      if (!inside(e.target)) onCloseRef.current(false);
    };
    const onResize = () => onCloseRef.current(false);
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        // Capture phase: this Escape is the menu's, not a dialog's below it.
        e.stopPropagation();
        e.preventDefault();
        onCloseRef.current(true);
      }
    };
    document.addEventListener("mousedown", onDown, true);
    document.addEventListener("keydown", onKey, true);
    window.addEventListener("resize", onResize);
    return () => {
      document.removeEventListener("mousedown", onDown, true);
      document.removeEventListener("keydown", onKey, true);
      window.removeEventListener("resize", onResize);
    };
  }, [anchor]);

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    const items = Array.from(menuRef.current?.querySelectorAll<HTMLElement>('[role="menuitem"]') || []);
    if (!items.length) return;
    const at = items.indexOf(document.activeElement as HTMLElement);
    let next = -1;
    if (e.key === "ArrowDown") next = (at + 1) % items.length;
    else if (e.key === "ArrowUp") next = (at - 1 + items.length) % items.length;
    else if (e.key === "Home") next = 0;
    else if (e.key === "End") next = items.length - 1;
    else if (e.key === "Tab") {
      e.preventDefault();
      onCloseRef.current(true);
      return;
    } else return;
    e.preventDefault();
    e.stopPropagation();
    items[next].focus();
  };

  return createPortal(
    <div
      id="help-menu"
      role="menu"
      aria-label="Help"
      ref={menuRef}
      style={{ top: 0, left: 0 }}
      onKeyDown={onKeyDown}
      onMouseDown={(e) => e.stopPropagation()}
    >
      {rows.map((r) => (
        <button
          key={r.key}
          type="button"
          role="menuitem"
          className="help-item"
          data-help={r.key}
          title={r.title}
          onClick={() => {
            // Close first: the answer is the dialog, not this menu.
            onCloseRef.current(false);
            r.run();
          }}
        >
          <span className="help-label">{r.label}</span>
          {r.aside && (
            <span className={r.aside.draft ? "help-draft" : "help-key"} title={r.aside.title}>
              {r.aside.text}
            </span>
          )}
        </button>
      ))}
    </div>,
    document.body
  );
}
