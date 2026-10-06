/** The ⏩ picker — what the pane head's ⏩ button opens (and Ctrl+K F, the
 * row › menu's "Fast-track…" and the palette open on the same button):
 *
 *   Off / Commit / Push / Open a PR / Merge when green
 *        how far MindFlock carries the session once its agent is done
 *        (POST /api/instances/{t}/lane — the API's word for the target)
 *   Ask me before it ships
 *        a toggle on the same call: stop one step short, wait in the Outbox
 *
 * Every item acts right away through the server; nothing is typed into the
 * agent. The current target is ticked and the highlight starts on it.
 * Body-mounted and fixed (the pane head scrolls sideways and would clip it),
 * keyboard-first: arrows, Enter, Esc and each item's letter. The menu keeps
 * the focus and names the highlighted item with `aria-activedescendant`. Its
 * id is in the keymap's MODAL_DOM_IDS, so a Delete or Ctrl+W pressed while it
 * is open can never end the session behind it.
 *
 * Doing ONE step by hand is not here: that is the Commit… / Push / Make PR
 * button beside ⏩, which stays the guided manual ladder. */

import { useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useInstances } from "../../state/queries";
import { useUi } from "../../state/store";
import {
  ASK_FIRST_DESC,
  ASK_FIRST_LABEL,
  fastTrackModel,
  pickFastTrack,
  type Lane,
} from "../../lib/laneActions";
import { toast } from "../../lib/toast";

/** One row of the picker: a rung, or the ask-first toggle. */
type Entry = { kind: "lane"; lane: Lane } | { kind: "ask" };

export function FastTrackMenu({
  title,
  anchor,
  onClose,
}: {
  title: string;
  anchor: HTMLElement;
  /** `refocus`: give the keyboard back to the pane's terminal (Esc). */
  onClose(refocus: boolean): void;
}) {
  const { data: rows } = useInstances();
  const name = useUi((s) => s.aliases[title]) || title;
  const inst = (rows || []).find((r) => r.title === title && !r.device) ||
    (rows || []).find((r) => r.title === title);
  const model = useMemo(() => fastTrackModel(inst || {}), [inst]);
  const entries: Entry[] = useMemo(
    () => [...model.items.map((i) => ({ kind: "lane" as const, lane: i.lane })), { kind: "ask" as const }],
    [model]
  );
  // The highlight starts on the current target — what Enter would re-affirm.
  const [sel, setSel] = useState(() =>
    Math.max(
      0,
      model.items.findIndex((i) => i.current)
    )
  );
  const menuRef = useRef<HTMLDivElement | null>(null);
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;
  const uid = useId();
  const itemId = (i: number) => uid + "-item-" + i;

  // The keyboard belongs to the picker while it is open — arrows and letters
  // pressed next must not go on into the agent's terminal.
  useEffect(() => {
    menuRef.current?.focus({ preventScroll: true });
  }, []);

  // Under the button, right edges aligned; clamped to the window. A pane low
  // in the grid has little room below its head, so the picker opens UPWARD
  // when it doesn't fit below and there is more room above; whatever still
  // doesn't fit scrolls rather than running off the screen.
  useLayoutEffect(() => {
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
  });

  // Outside click, resize and a scroll anywhere but inside the picker close
  // it. The anchor is exempt: its own click handler toggles.
  useEffect(() => {
    const inside = (t: EventTarget | null) =>
      t instanceof Node && (!!menuRef.current?.contains(t) || anchor.contains(t));
    const onDown = (e: MouseEvent) => {
      if (!inside(e.target)) onCloseRef.current(false);
    };
    const onScroll = (e: Event) => {
      if (!inside(e.target)) onCloseRef.current(false);
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

  const whyOf = (e: Entry): string => (e.kind === "ask" ? model.ask.why : model.lock);
  const keyOf = (e: Entry): string =>
    e.kind === "ask" ? "A" : model.items.find((i) => i.lane === e.lane)?.key || "";

  const activate = (e: Entry | undefined) => {
    if (!e) return;
    const why = whyOf(e);
    if (why) {
      toast(why, { duration: 5000 });
      return;
    }
    if (!inst) return;
    // Close first: the answer is the ⏩ button itself (it flips at once) and a
    // toast, not this picker.
    close();
    const cur = model.current;
    if (e.kind === "ask") void pickFastTrack(title, name, cur.lane, !model.ask.on);
    else void pickFastTrack(title, name, e.lane, cur.askFirst);
  };

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    const k = e.key;
    const handled = () => {
      e.preventDefault();
      e.stopPropagation();
    };
    if (k === "ArrowDown") {
      handled();
      setSel((i) => (i + 1) % entries.length);
    } else if (k === "ArrowUp") {
      handled();
      setSel((i) => (i - 1 + entries.length) % entries.length);
    } else if (k === "Enter" || k === " ") {
      handled();
      activate(entries[sel]);
    } else if (k === "Escape") {
      handled();
      close(true);
    } else if (k.length === 1 && /[a-z]/i.test(k)) {
      const i = entries.findIndex((x) => keyOf(x) === k.toUpperCase());
      if (i >= 0) {
        handled();
        setSel(i);
        activate(entries[i]);
      }
    }
  };

  const row = (e: Entry, i: number) => {
    const why = whyOf(e);
    const it = e.kind === "lane" ? model.items.find((x) => x.lane === e.lane) : null;
    const checked = it ? it.current : model.ask.on;
    const label = it ? it.label : ASK_FIRST_LABEL;
    const desc = it ? it.desc : ASK_FIRST_DESC;
    return (
      <div
        key={e.kind === "lane" ? e.lane : "ask"}
        id={itemId(i)}
        className={"pb-item" + (i === sel ? " sel" : "") + (why ? " off" : "") + (checked ? " on" : "")}
        role={e.kind === "lane" ? "menuitemradio" : "menuitemcheckbox"}
        aria-checked={checked}
        tabIndex={-1}
        aria-disabled={why ? true : undefined}
        title={why || undefined}
        data-ft={e.kind === "lane" ? e.lane : "ask"}
        onMouseMove={() => setSel(i)}
        onClick={() => activate(e)}
      >
        <span className="pb-name">
          <span className="ft-check" aria-hidden="true">
            {checked ? "✓" : ""}
          </span>
          {label}
        </span>
        <span className="pb-key">{keyOf(e)}</span>
        <span className={"pb-desc" + (why ? " pb-why" : "")}>{why || desc}</span>
      </div>
    );
  };

  return createPortal(
    <div
      id="fast-track-menu"
      className="pb-menu ft-menu"
      role="menu"
      aria-label={`Fast-track — ${name}`}
      tabIndex={-1}
      ref={menuRef}
      aria-activedescendant={itemId(sel)}
      style={{ top: 0, left: 0 }}
      onKeyDown={onKeyDown}
      onMouseDown={(e) => e.stopPropagation()}
    >
      <div className="pb-head">
        <b>⏩ Fast-track</b>
        <span className="muted">→ {name}</span>
      </div>
      <div className="pb-sec">When the agent is done, go as far as</div>
      {entries.slice(0, -1).map((e, i) => row(e, i))}
      <div className="pb-sep" />
      {row(entries[entries.length - 1], entries.length - 1)}
      <div className="pb-foot">
        Acts right away — nothing is typed into the agent. To do one step yourself, use the
        button beside <b>⏩</b>. New sessions start at Settings → Workspace's default.
      </div>
    </div>,
    document.body
  );
}
