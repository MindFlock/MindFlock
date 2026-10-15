/** The per-session menu's items (lib/sessionMenu), drawn the same way in the
 * sidebar row's accordion and the pane header's ⋯ popover. The wrapper (where
 * it hangs, when it closes) is the caller's; this draws the groups, the
 * folded "More", and Delete's inline confirm strip (the desktop app has no
 * native confirm dialog). */

import { useLayoutEffect, useRef, useState, type MouseEvent } from "react";
import type { MenuItem, SessionMenuModel } from "../../lib/sessionMenu";

export function SessionMenu({
  model,
  onDone,
  onInUse,
}: {
  model: SessionMenuModel;
  /** After an item ran (the popover closes; the accordion folds). */
  onDone?: () => void;
  /** A Delete refused because other sessions hold the folder. */
  onInUse?: (titles: string[]) => void;
}) {
  const [moreOpen, setMoreOpen] = useState(false);
  const [deleteArmed, setDeleteArmed] = useState(false);
  const moreRef = useRef<HTMLDivElement | null>(null);

  // More ▸ unfolded in a menu too tall for the window (it scrolls, Close and
  // Delete pinned at its foot): scroll the fold into sight above that foot,
  // never so far that More itself leaves the top.
  useLayoutEffect(() => {
    const fold = moreRef.current;
    if (!moreOpen || !fold) return;
    let box = fold.parentElement;
    while (box && !(box.scrollHeight > box.clientHeight && /auto|scroll/.test(getComputedStyle(box).overflowY)))
      box = box.parentElement;
    if (!box) return;
    const view = box.getBoundingClientRect();
    const foot = fold.parentElement?.querySelector(".sm-danger")?.getBoundingClientRect();
    const r = fold.getBoundingClientRect();
    const by = Math.min(r.bottom - (foot ? foot.top : view.bottom), r.top - view.top);
    if (by > 0) box.scrollTop += by;
  }, [moreOpen]);

  const fire = (it: MenuItem) => (e: MouseEvent) => {
    e.stopPropagation();
    if (it.disabled) return;
    if (it.confirm && !deleteArmed) {
      setDeleteArmed(true);
      return;
    }
    setDeleteArmed(false);
    const r = it.run();
    if (it.key === "delete" && r && typeof (r as Promise<unknown>).then === "function")
      void (r as Promise<unknown>).then((res) => {
        const inUse = (res as { inUse?: string[] } | null)?.inUse;
        if (inUse) onInUse?.(inUse);
      });
    onDone?.();
  };

  const item = (it: MenuItem) =>
    it.confirm && deleteArmed ? (
      <div key={it.key} className="del-confirm" role="group" aria-label={it.label.replace(/…$/, "") + "?"}>
        <span className="del-q">{it.confirm}</span>
        <span className="del-acts">
          <button type="button" className="danger" data-menu={it.key} onClick={fire(it)}>
            {it.label.replace(/…$/, "")}
          </button>
          {/* Focus lands on the harmless answer: a second Enter never deletes. */}
          <button
            type="button"
            autoFocus
            onClick={(e) => {
              e.stopPropagation();
              setDeleteArmed(false);
            }}
          >
            Cancel
          </button>
        </span>
      </div>
    ) : (
      <button
        key={it.key}
        type="button"
        role="menuitem"
        data-menu={it.key}
        className={(it.danger ? "danger" : "") + (it.note ? " has-note" : "") || undefined}
        disabled={it.disabled}
        aria-disabled={it.disabled || undefined}
        title={it.title}
        onClick={fire(it)}
      >
        <span className="sm-verb">
          <span className="sm-label">
            {it.label}
            {it.external ? " ↗" : ""}
          </span>
          {it.keys && <span className="kbd">{it.keys}</span>}
        </span>
        {it.note && <span className="end-sub">{it.note}</span>}
      </button>
    );

  const group = (g: SessionMenuModel["groups"][number], first: boolean) => (
    <div key={g.id} className={"sm-group sm-" + g.id} data-group={g.id}>
      {!first && <div className="menu-sep" />}
      {g.label && <div className="sm-head">{g.label}</div>}
      {g.items.map(item)}
    </div>
  );
  const body = model.groups.filter((g) => g.id !== "danger");
  const danger = model.groups.find((g) => g.id === "danger");

  return (
    <div className="session-menu">
      {body.map((g, i) => group(g, i === 0))}
      {/* The rare ones, folded, between the work and the two ends. */}
      {model.more.length > 0 && (
        <div className="sm-group sm-more-group" ref={moreRef}>
          {body.length > 0 && <div className="menu-sep" />}
          <button
            type="button"
            className="sm-more"
            aria-expanded={moreOpen}
            data-menu="more"
            title="The rarer actions: tabs, copies, the IDE, preview, select, hide…"
            onClick={(e) => {
              e.stopPropagation();
              setMoreOpen((v) => !v);
            }}
          >
            <span className="sm-verb">
              <span className="sm-label">More</span>
              <span className="sm-caret" aria-hidden="true">
                {moreOpen ? "▾" : "▸"}
              </span>
            </span>
          </button>
          {moreOpen && <div className="sm-more-items">{model.more.map(item)}</div>}
        </div>
      )}
      {danger && group(danger, !body.length && !model.more.length)}
    </div>
  );
}
