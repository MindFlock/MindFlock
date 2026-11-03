/** The Workers… picker — the ONE entry for workers (sessions a session
 * starts). The pane head's Workers control opens it, and so do the row / pane
 * ⋯ menu's "Workers…" and the palette, on the same control:
 *
 *   Let the agent split this now   a split team run: it proposes workers,
 *                                  you approve them on its Thread tab
 *   Ask for workers once           Alt+G — types the workers request
 *   Workers on every prompt        Alt+Shift+G — a ✓ switch
 *
 * The modes are lib/sessionMenu `workersModes`; each acts right away. Built
 * like the ⏩ picker (FastTrackMenu, whose .pb-menu styles it shares):
 * body-mounted and fixed under its control, keyboard-first (arrows, Enter,
 * Esc, each mode's letter), and `data-modal`, so a Delete or Ctrl+W pressed
 * while it is open is about it, never the session behind it. */

import { useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useInstances } from "../../state/queries";
import { useUi } from "../../state/store";
import { actionCtx } from "../../lib/sessionActionTable";
import { workersModes } from "../../lib/sessionMenu";
import { toast } from "../../lib/toast";

export function WorkersMenu({
  title,
  anchor,
  keys,
  onClose,
}: {
  title: string;
  anchor: HTMLElement;
  /** The live shortcuts for [ask once, every prompt] (rebinds included). */
  keys?: [string, string];
  /** `refocus`: give the keyboard back to the pane's terminal (Esc). */
  onClose(refocus: boolean): void;
}) {
  const { data: rows } = useInstances();
  const name = useUi((s) => s.aliases[title]) || title;
  const inst = (rows || []).find((r) => r.title === title && !r.device) || (rows || []).find((r) => r.title === title);
  const modes = useMemo(() => {
    if (!inst) return [];
    const ms = workersModes(inst, actionCtx(title, { inst }));
    return keys ? ms.map((m) => (m.key === "ask" ? { ...m, keys: keys[0] } : m.key === "flock" ? { ...m, keys: keys[1] } : m)) : ms;
  }, [inst, title, keys]);
  // The highlight starts on the first mode that applies (a greyed split is
  // never what Enter does first).
  const [sel, setSel] = useState(() => Math.max(0, modes.findIndex((m) => !m.why)));
  const menuRef = useRef<HTMLDivElement | null>(null);
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;
  const uid = useId();
  const itemId = (i: number) => uid + "-item-" + i;

  useEffect(() => {
    menuRef.current?.focus({ preventScroll: true });
  }, []);

  // Under the control, right edges aligned, clamped to the window; upward
  // when it doesn't fit below and there is more room above.
  const placeRef = useRef<() => void>(() => {});
  placeRef.current = () => {
    const m = menuRef.current;
    if (!m) return;
    const r = anchor.getBoundingClientRect();
    m.style.maxHeight = "none";
    const natural = m.scrollHeight;
    const below = window.innerHeight - r.bottom - 18;
    const above = r.top - 18;
    if (natural > below && above > below) {
      const h = Math.min(natural, above);
      m.style.top = Math.max(8, Math.round(r.top - 6 - h)) + "px";
      m.style.maxHeight = h + "px";
    } else {
      const top = Math.round(r.bottom + 6);
      m.style.top = top + "px";
      m.style.maxHeight = Math.max(160, window.innerHeight - top - 12) + "px";
    }
    let left = r.right - m.offsetWidth + 10;
    left = Math.min(left, window.innerWidth - m.offsetWidth - 8);
    m.style.left = Math.max(8, left) + "px";
  };
  useLayoutEffect(() => placeRef.current());

  // Outside click, resize and a scroll anywhere but inside close it; the
  // anchor is exempt (its own click toggles), and a scroll of something that
  // holds it (the pane head scrolls sideways) follows it instead.
  useEffect(() => {
    const inside = (t: EventTarget | null) =>
      t instanceof Node && (!!menuRef.current?.contains(t) || anchor.contains(t));
    const onDown = (e: MouseEvent) => {
      if (!inside(e.target)) onCloseRef.current(false);
    };
    const onScroll = (e: Event) => {
      const t = e.target;
      if (inside(t)) return;
      if (t instanceof Node && t !== document && t.contains(anchor)) {
        placeRef.current();
        return;
      }
      onCloseRef.current(false);
    };
    const onResize = () => onCloseRef.current(false);
    document.addEventListener("mousedown", onDown, true);
    window.addEventListener("scroll", onScroll, true);
    window.addEventListener("resize", onResize);
    return () => {
      document.removeEventListener("mousedown", onDown, true);
      window.removeEventListener("scroll", onScroll, true);
      window.removeEventListener("resize", onResize);
    };
  }, [anchor]);

  const close = (refocus = false) => onCloseRef.current(refocus);

  const activate = (i: number) => {
    const m = modes[i];
    if (!m) return;
    if (m.why) {
      toast(m.why, { duration: 5000 });
      return;
    }
    // Close first: Ask once hands the keyboard to the terminal, and the
    // answer to the others is a toast and the Workers control itself.
    close();
    void m.run();
  };

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    const k = e.key;
    const handled = () => {
      e.preventDefault();
      e.stopPropagation();
    };
    if (!modes.length) return;
    if (k === "ArrowDown") {
      handled();
      setSel((i) => (i + 1) % modes.length);
    } else if (k === "ArrowUp") {
      handled();
      setSel((i) => (i - 1 + modes.length) % modes.length);
    } else if (k === "Enter" || k === " ") {
      handled();
      activate(sel);
    } else if (k === "Escape") {
      handled();
      close(true);
    } else if (k.length === 1 && /[a-z]/i.test(k)) {
      const i = modes.findIndex((m) => m.letter === k.toUpperCase());
      if (i >= 0) {
        handled();
        setSel(i);
        activate(i);
      }
    }
  };

  return createPortal(
    <div
      id="workers-menu"
      className="pb-menu ft-menu wk-menu"
      role="menu"
      data-modal=""
      aria-label={`Workers — ${name}`}
      tabIndex={-1}
      ref={menuRef}
      aria-activedescendant={modes.length ? itemId(sel) : undefined}
      style={{ top: 0, left: 0 }}
      onKeyDown={onKeyDown}
      onMouseDown={(e) => e.stopPropagation()}
    >
      <div className="pb-head">
        <b>Workers</b>
        <span className="muted">→ {name}</span>
      </div>
      <div className="pb-sec">Sessions this one starts, each on its own branch</div>
      {modes.map((m, i) => (
        <div
          key={m.key}
          id={itemId(i)}
          className={"pb-item" + (i === sel ? " sel" : "") + (m.why ? " off" : "") + (m.checked ? " on" : "")}
          role={m.key === "flock" ? "menuitemcheckbox" : "menuitem"}
          aria-checked={m.key === "flock" ? !!m.checked : undefined}
          aria-disabled={m.why ? true : undefined}
          tabIndex={-1}
          title={m.why || undefined}
          data-wk={m.key}
          onMouseMove={() => setSel(i)}
          onClick={() => activate(i)}
        >
          <span className="pb-name">
            <span className="ft-check" aria-hidden="true">
              {m.checked ? "✓" : ""}
            </span>
            {m.label}
          </span>
          <span className="pb-key">{m.keys || m.letter}</span>
          <span className={"pb-desc" + (m.why ? " pb-why" : "")}>{m.why || m.note}</span>
        </div>
      ))}
      <div className="pb-foot">
        The words it asks with, and the model workers run on: Settings → Agent orchestration.
      </div>
    </div>,
    document.body
  );
}
