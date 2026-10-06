/** "Ship & split" — the menu under a pane's fork-icon button (Ctrl+K F, and
 * Ctrl+K L to land on the lane). Every item ACTS, right away, through the
 * server; nothing is typed into the agent:
 *
 *   When it's done   Leave it / Commit / Open a PR / Merge when checks pass
 *                    (POST /api/instances/{t}/lane), and "Ask me before it
 *                    ships" as a toggle on the same call.
 *   Split            Split into parallel pieces… — a split run with this
 *                    session as the lead (POST /api/runs, split).
 *   {Group} · N      Ship it now (POST …/ship-now), Move out of the group
 *                    (the run's skip, which detaches and keeps the session),
 *                    then Message…, which opens the Thread composer as you.
 *
 * The current lane is ticked and the highlight starts on it. Body-mounted and
 * fixed (the pane head scrolls sideways and would clip it), keyboard-first:
 * arrows, Enter, Esc, and each item's letter. The menu keeps the focus and
 * names the highlighted item with `aria-activedescendant`. Its id is in the
 * keymap's MODAL_DOM_IDS, so a Delete or Ctrl+W pressed while it is open can
 * never end the session behind it. */

import { useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { refreshInstances, useConfig, useInstances } from "../../state/queries";
import { useUi } from "../../state/store";
import {
  entryKey,
  entryWhy,
  runShipEntry,
  shipEntries,
  shipMenuModel,
  type ShipEntry,
} from "../../lib/laneActions";
import { errMsg } from "../../lib/format";
import { toast } from "../../lib/toast";

export function ShipMenu({
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
  const { data: config } = useConfig();
  const name = useUi((s) => s.aliases[title]) || title;
  const inst = (rows || []).find((r) => r.title === title && !r.device);
  const model = useMemo(
    () =>
      shipMenuModel(
        inst || { provider: "", program: "", mcp_attached: null, activity: "" },
        rows || [],
        config?.caps
      ),
    [inst, rows, config?.caps]
  );
  const entries = useMemo(() => shipEntries(model), [model]);
  // The highlight starts on the current lane — what Enter would re-affirm.
  const [sel, setSel] = useState(() =>
    Math.max(
      0,
      entries.findIndex((e) => e.kind === "lane" && e.current)
    )
  );
  const [busy, setBusy] = useState(false);
  const menuRef = useRef<HTMLDivElement | null>(null);
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;
  const uid = useId();
  const itemId = (i: number) => uid + "-item-" + i;

  // The keyboard belongs to the menu while it is open — arrows and letters
  // pressed next must not go on into the agent's terminal.
  useEffect(() => {
    menuRef.current?.focus({ preventScroll: true });
  }, []);

  // Under the button, right edges aligned; clamped to the window. A pane low
  // in the grid has little room below its head (found on a real 2x3 grid: the
  // Split item sat below the fold of a 260px scroller), so the menu opens
  // UPWARD when it doesn't fit below and there is more room above; whatever
  // still doesn't fit scrolls rather than running off the screen.
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

  // Outside click, resize and a scroll anywhere but inside the menu close it.
  // The anchor is exempt: its own click handler toggles.
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

  const activate = (e: ShipEntry | undefined) => {
    if (!e) return;
    const why = entryWhy(e);
    if (why) {
      toast(why, { duration: 5000 });
      return;
    }
    if (e.kind === "message") {
      useUi.getState().threadOpen(title, { composeTo: title });
      close();
      return;
    }
    if (!inst || busy) return;
    // Close first: the result is a toast and the row's own line, not this menu.
    setBusy(true);
    close();
    runShipEntry(e, inst, name, model.current)
      .then((said) => {
        if (said) toast(said, { duration: 4500 });
        refreshInstances();
      })
      .catch((err) => toast(`${name}: ${errMsg(err)}`, { duration: 6000 }))
      .finally(() => setBusy(false));
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
    } else if (k === "Enter") {
      handled();
      activate(entries[sel]);
    } else if (k === "Escape") {
      handled();
      close(true);
    } else if (k.length === 1 && /[a-z]/i.test(k)) {
      const i = entries.findIndex((x) => entryKey(x) === k.toUpperCase());
      if (i >= 0) {
        handled();
        setSel(i);
        activate(entries[i]);
      }
    }
  };

  const item = (e: ShipEntry) => {
    const i = entries.indexOf(e);
    const why = entryWhy(e);
    let label = "";
    let desc = "";
    let key = entryKey(e);
    switch (e.kind) {
      case "lane":
        label = e.label + (e.current ? " ✓" : "");
        desc = e.desc + (e.current ? " — this session's lane now" : "");
        break;
      case "ask":
        label = "Ask me before it ships" + (e.on ? " ✓" : "");
        desc = "Stop at the next step and show it in the Outbox first";
        break;
      case "split":
        label = "Split into parallel pieces…";
        desc =
          "The agent proposes pieces with separate paths; you approve, MindFlock runs and merges them back";
        break;
      case "shipnow":
        label = "Ship it now";
        desc = "Don't wait for the agent — take what's there through the lane now";
        break;
      case "detach":
        label = "Move out of " + (model.group?.name || "the group");
        desc = "The group stops driving it; the session and its lane stay";
        break;
      case "message":
        label = "Message…";
        desc = "Write to a session yourself, in the Thread tab";
        key = "Ctrl+K S";
        break;
    }
    return (
      <div
        key={e.kind + (e.kind === "lane" ? e.lane : "")}
        id={itemId(i)}
        className={"pb-item" + (i === sel ? " sel" : "") + (why ? " off" : "")}
        role={
          e.kind === "lane" ? "menuitemradio" : e.kind === "ask" ? "menuitemcheckbox" : "menuitem"
        }
        aria-checked={e.kind === "lane" ? e.current : e.kind === "ask" ? e.on : undefined}
        tabIndex={-1}
        aria-disabled={why ? true : undefined}
        title={why || undefined}
        data-ship={e.kind === "lane" ? "lane-" + e.lane : e.kind}
        onMouseMove={() => setSel(i)}
        onClick={() => activate(e)}
      >
        <span className="pb-name">{label}</span>
        <span className="pb-key">{key}</span>
        <span className={"pb-desc" + (why ? " pb-why" : "")}>{why || desc}</span>
      </div>
    );
  };

  return createPortal(
    <div
      id="ship-menu"
      className="pb-menu"
      role="menu"
      aria-label={`Ship & split — ${name}`}
      tabIndex={-1}
      ref={menuRef}
      aria-activedescendant={itemId(sel)}
      aria-busy={busy || undefined}
      style={{ top: 0, left: 0 }}
      onKeyDown={onKeyDown}
      onMouseDown={(e) => e.stopPropagation()}
    >
      <div className="pb-head">
        <b>Ship &amp; split</b>
        <span className="muted">→ {name}</span>
      </div>
      <div className="pb-sec">When it's done</div>
      {model.lanes.map(item)}
      <div className="pb-sec">Split</div>
      {model.split.map(item)}
      {model.group ? (
        <div className="pb-sec">
          {model.group.name} · {model.group.count}
        </div>
      ) : (
        <div className="pb-sep" />
      )}
      {model.tail.map(item)}
      <div className="pb-foot">
        Every item acts right away — nothing is pasted into the agent. The <b>⏩</b> button and the
        row <b>›</b> menu show the same lane.
      </div>
    </div>,
    document.body
  );
}
