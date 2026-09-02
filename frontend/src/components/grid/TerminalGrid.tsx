/** The terminal grid (ports app.js renderGrid + the section-3 drag layout).
 * Rows of panes; drag a pane's header onto a side of another to rearrange,
 * with a live drop-preview slot. Terminals live in lib/terminals' registry,
 * so React unmount/remount just re-adopts the same xterm DOM. */

import { useEffect, useMemo, useRef, useState } from "react";
import { useConfig, useInstances } from "../../state/queries";
import { windowKey, useUi } from "../../state/store";
import { releaseTerms } from "../../lib/terminals";
import { dropActivity } from "../../lib/stage";
import {
  computeVisibleSlots,
  DROP_PH,
  placeInGrid,
  previewRowsFor,
  reconcileGridRows,
  type DropSide,
} from "./layout";
import { Pane } from "./Pane";
import { SpecialPane } from "./SpecialPane";
import { SetupChecklist } from "../dialogs/SetupDialog";
import { isVerifySession } from "../dialogs/verify";

export interface SpecialPaneDesc {
  key: string;
  kind: "logs" | "syslogs" | "chat" | "verify" | "ext";
  title: string;
  /** `verify` only: the session whose terminal this pane watches. */
  session?: string;
  /** `ext` only: the extension pane key ("<ext>:<surface>[:<ref>]"). */
  extKey?: string;
  // No close callback: the pane head's ✕ (SpecialPane's CloseBtn) and the
  // sidebar's Windows rows both derive the close action from kind+ref, so the
  // two controls can never disagree.
}

/** The per-instance token a desc's sentinel is built from (verify: the watched
 * session; ext: the pane key — both kinds allow several panes at once). */
function sentinelRef(p: SpecialPaneDesc): string {
  return (p.kind === "ext" ? p.extKey : p.session) || "";
}

export interface DragCtx {
  dragTitle: string | null;
  start(title: string): void;
  hover(targetTitle: string, side: DropSide): void;
  commit(): void;
  end(): void;
}

export function TerminalGrid({ specialPanes }: { specialPanes: SpecialPaneDesc[] }) {
  const { data: instances, isSuccess } = useInstances();
  const { data: config } = useConfig();
  const ui = useUi();
  const [drag, setDrag] = useState<{ dragTitle: string; targetTitle?: string; side?: DropSide } | null>(null);

  // A verify run never gets an ordinary pane. It is a real session (it needs a
  // worktree and an agent that can run commands) but it is not one you work in:
  // it gets the read-only SpecialPane below instead, opened and closed from the
  // Verify dialog. Without this filter it would appear twice — once as a
  // terminal you could type into, which is exactly what it must not be.
  const workable = useMemo(
    () => (instances || []).filter((i) => !isVerifySession(i.title)),
    [instances]
  );
  const specialByKey = useMemo(
    () => new Map(specialPanes.map((p) => [sentinel(p.kind, sentinelRef(p)), p])),
    [specialPanes]
  );

  // ONE capped list for every window, sessions and specials alike — the
  // assistant and a database table take a slot exactly like a session does, so
  // at "view: 1" the one you last picked is the one on screen.
  const slotTitles = useMemo(
    () =>
      computeVisibleSlots(workable, [...specialByKey.keys()], {
        hidden: ui.hidden,
        viewMode: ui.viewMode,
        mru: ui.mru,
        order: ui.order,
      }),
    [workable, specialByKey, ui.hidden, ui.viewMode, ui.mru, ui.order]
  );
  const byTitle = useMemo(() => {
    const all = new Map(workable.map((i) => [i.title, i]));
    return new Map(
      slotTitles.filter((t) => all.has(t)).map((t) => [t, all.get(t)!])
    );
  }, [workable, slotTitles]);
  /** The SESSIONS on screen — terminal teardown and keyboard focus are about
   * those, never about a log tail or an extension pane. */
  const visible = useMemo(() => [...byTitle.values()], [byTitle]);

  // Reconcile the persisted row layout against that set.
  // (Skipped before the first snapshot: painting a stale [] would wipe the
  // persisted layout — the "windows reorder after reload" bug.)
  const rows = useMemo(() => {
    if (!isSuccess) return ui.gridRows;
    return reconcileGridRows(ui.gridRows, slotTitles);
  }, [isSuccess, ui.gridRows, slotTitles]);
  useEffect(() => {
    if (!isSuccess) return;
    if (JSON.stringify(rows) !== JSON.stringify(ui.gridRows)) ui.setGridRows(rows);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rows, isSuccess]);

  // Dispose terminals + activity state for sessions that left the visible set
  // (hidden or gone) — matches vanilla removePane semantics.
  const prevVisible = useRef<Set<string>>(new Set());
  useEffect(() => {
    if (!isSuccess) return;
    const now = new Set(visible.map((i) => i.title));
    const live = new Set((instances || []).map((i) => i.title));
    for (const t of prevVisible.current) {
      if (!now.has(t)) releaseTerms(t);
    }
    for (const t of prevVisible.current) {
      if (!live.has(t)) dropActivity(t);
    }
    prevVisible.current = now;
  }, [visible, instances, isSuccess]);

  // Focus fell out of the capped view -> focus the first visible pane
  // (logical focus only; never steal the keyboard from an input).
  useEffect(() => {
    if (!isSuccess) return;
    const titles = new Set(visible.map((i) => i.title));
    const focused = useUi.getState().focused;
    if (focused && !titles.has(focused)) useUi.getState().setFocused(null);
    if (!useUi.getState().focused && visible.length)
      useUi.getState().setFocused(visible[0].title);
  }, [visible, isSuccess]);

  const dragCtx: DragCtx = useMemo(
    () => ({
      dragTitle: drag?.dragTitle ?? null,
      start: (title) => setDrag({ dragTitle: title }),
      hover: (targetTitle, side) =>
        setDrag((d) =>
          d && d.dragTitle !== targetTitle ? { ...d, targetTitle, side } : d
        ),
      commit: () => {
        setDrag((d) => {
          if (d?.targetTitle && d.side)
            useUi
              .getState()
              .setGridRows(
                placeInGrid(useUi.getState().gridRows, d.dragTitle, d.targetTitle, d.side)
              );
          return null;
        });
      },
      end: () => setDrag(null),
    }),
    [drag?.dragTitle]
  );

  // Mid-drag: show the prospective layout with an empty slot.
  const shownRows = useMemo(() => {
    if (drag?.targetTitle && drag.side)
      return previewRowsFor(rows, drag.dragTitle, drag.targetTitle, drag.side);
    return rows;
  }, [rows, drag]);

  const booting = !isSuccess;
  const noneAtAll = (instances || []).length === 0;
  const showEmpty = !booting && visible.length === 0 && specialPanes.length === 0;
  const firstRun = noneAtAll && config && !config.onboarded;

  return (
    <main
      id="grid"
      onDragOver={(ev) => {
        if (drag) ev.preventDefault();
      }}
      onDrop={(ev) => {
        if (drag) {
          ev.preventDefault();
          dragCtx.commit();
        }
      }}
    >
      {booting && (
        <div id="boot-loading">
          <div className="spinner" />
          <p>Restoring sessions…</p>
        </div>
      )}
      {shownRows.map((row, ri) => (
        <div className="grid-row" key={ri}>
          {row.map((t) => {
            if (t === DROP_PH) return <div key="drop" className="pane grid-drop-preview" />;
            const special = specialByKey.get(t);
            if (special) return <SpecialPane key={t} desc={special} drag={dragCtx} />;
            const inst = byTitle.get(t);
            if (!inst) return null;
            return (
              <Pane
                key={t}
                inst={inst}
                drag={dragCtx}
                dragging={drag?.dragTitle === t}
              />
            );
          })}
        </div>
      ))}
      {showEmpty &&
        (firstRun ? (
          <div id="empty" data-mode="setup">
            <div className="setup-card">
              <h2>Welcome to MindFlock</h2>
              <p className="muted">Three steps to a running agent.</p>
              <div className="setup-body">
                <SetupChecklist standalone />
              </div>
            </div>
          </div>
        ) : (
          <div
            id="empty"
            data-mode="plain"
            className={noneAtAll ? "clickable" : ""}
            onClick={noneAtAll ? () => useUi.getState().openDialogFor("new-session") : undefined}
          >
            {noneAtAll
              ? "No sessions yet — click here to create one. (Ctrl+N)"
              : "All sessions hidden. Use a session's ⋯ menu to show one."}
          </div>
        ))}
    </main>
  );
}

/** The grid token for a special pane. Defined in the store (windowKey), which
 * is what lets OPENING a window select it; re-exported here because the panes
 * and the sidebar have always asked the grid for it. */
export function sentinel(kind: SpecialPaneDesc["kind"], ref = ""): string {
  return windowKey(kind, ref);
}
