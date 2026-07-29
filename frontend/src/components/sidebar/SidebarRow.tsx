/** One session row (ports app.js section 9's _createSidebarRow /
 * _updateSidebarRow / _instActionsHtml / _rowAction). */

import {
  memo,
  useCallback,
  useEffect,
  useRef,
  useState,
  useSyncExternalStore,
  type DragEvent,
  type MouseEvent,
} from "react";
import type { Instance } from "../../api/types";
import { instApi } from "../../api/client";
import { refreshInstances, useConfig } from "../../state/queries";
import { useUi } from "../../state/store";
import { copyText } from "../../lib/clipboard";
import { errMsg } from "../../lib/format";
import { chipState, checkChip } from "../../lib/stage";
import { sessionLabel } from "../../lib/sessionLabel";
import {
  cleanupMissing,
  commitSession,
  copySession,
  hideSession,
  ideSession,
  killSession,
  makePrSession,
  pauseSession,
  pushSession,
  resumeSession,
  selectSession,
} from "../../lib/sessionActions";
import { toast } from "../../lib/toast";
import { peekTerm, subscribeTermStates } from "../../lib/terminals";

/** How long a click on the selected row waits for a second click before it
 * turns into an inline rename. Under the browser's ~500ms dblclick ceiling,
 * but long enough that an unhurried double-click still opens the IDE. */
const DBLCLICK_MS = 300;

function displayTitle(inst: Instance): string {
  return (inst as unknown as { display_title?: string }).display_title || inst.title || "";
}

interface Props {
  inst: Instance;
  idx: number;
  onScreen: boolean;
  dropCue: "above" | "below" | null;
  onDragState(title: string | null): void;
  onDropCue(title: string, cue: "above" | "below" | null): void;
  onDropRow(dragTitle: string, targetTitle: string, before: boolean): void;
}

export const SidebarRow = memo(function SidebarRow({
  inst,
  idx,
  onScreen,
  dropCue,
  onDragState,
  onDropCue,
  onDropRow,
}: Props) {
  const [expanded, setExpanded] = useState(false);
  const [editing, setEditing] = useState(false);
  // Escape must abandon the edit; unmounting the focused input can still run
  // the blur handler, so the cancel is flagged rather than inferred.
  const cancelled = useRef(false);
  const renameTimer = useRef<number | null>(null);
  // When a click ends an edit, that same click also reaches the row — without
  // this the dismiss would immediately re-arm the rename.
  const editEndedAt = useRef(0);
  const { data: config } = useConfig();
  const focused = useUi((s) => s.focused);
  const hidden = useUi((s) => s.hidden.has(inst.title));
  const alias = useUi((s) => s.aliases[inst.title]);
  const openDialogFor = useUi((s) => s.openDialogFor);
  const setAlias = useUi((s) => s.setAlias);

  const title = inst.title;
  const missing = !!inst.workspace_missing;
  const paused = inst.status === "paused";
  // A force-start the server has accepted but not yet turned into a session:
  // it exists only as this row, so nothing on it is actionable yet.
  const pending = !!inst.pending;
  const caps = config?.caps ?? { git: true, tailscale: true, ticketing: true };
  const ideName = config?.ide_name || "Cursor";
  const chip = chipState(inst);
  const check = checkChip(inst);
  const num = idx < 9 ? String(idx + 1) : "";
  // Ticket/PR/issue sessions read as "(tix) add-dark-mode/sc-12345" instead of
  // the bare slug; a hand-made session's title is passed through unchanged.
  const label = sessionLabel(displayTitle(inst), inst.branch || "");
  const shown = alias || label.text;
  const folder = inst.folder || inst.path || "";
  // Subscribed (not a render-time snapshot): the row must clear its red dot
  // the moment the agent socket connects, not on the next instances poll.
  const agentWs = useSyncExternalStore(
    subscribeTermStates,
    useCallback(() => peekTerm(title, "agent")?.state, [title])
  );
  const disconnected = inst.status === "running" && onScreen && agentWs === "disconnected";

  const act = async (fn: () => void | Promise<void>, e?: MouseEvent) => {
    e?.stopPropagation();
    await fn();
  };

  const clearRenameTimer = () => {
    if (renameTimer.current !== null) {
      clearTimeout(renameTimer.current);
      renameTimer.current = null;
    }
  };
  useEffect(() => clearRenameTimer, []);

  /** Click on the row that's ALREADY selected → edit the name in place. The
   * second click of a double-click also lands here, so the edit is held for
   * the double-click window and cancelled by onDoubleClick (open in IDE). */
  const armRename = () => {
    clearRenameTimer();
    renameTimer.current = window.setTimeout(() => {
      renameTimer.current = null;
      cancelled.current = false;
      setEditing(true);
    }, DBLCLICK_MS);
  };

  const commitRename = (raw: string) => {
    setEditing(false);
    editEndedAt.current = Date.now();
    if (cancelled.current) {
      cancelled.current = false;
      return;
    }
    const next = raw.trim();
    // Blank, or typed back to what the row shows by itself, means "drop the
    // alias" — that's the default label as well as the raw title.
    const nextAlias =
      !next || next === label.text || next === displayTitle(inst) ? "" : next;
    if (nextAlias === (alias || "")) return;
    setAlias(title, nextAlias);
    toast(nextAlias ? `Renamed to “${nextAlias}”` : "Reset to real title");
  };

  const rowCls =
    "inst" +
    (focused === title ? " active" : "") +
    (hidden ? " is-hidden" : "") +
    (missing ? " ws-missing" : "") +
    (pending ? " is-pending" : "") +
    (dropCue ? ` drop-${dropCue}` : "");

  return (
    <li
      className={rowCls}
      data-title={title}
      // Row drag would hijack text selection inside the rename input.
      draggable={!editing}
      onDragStart={(ev: DragEvent) => {
        ev.dataTransfer.setData("text/plain", title);
        ev.dataTransfer.effectAllowed = "move";
        onDragState(title);
      }}
      onDragEnd={() => {
        onDragState(null);
        onDropCue(title, null);
      }}
      onDragOver={(ev: DragEvent) => {
        // A section (bar) is being dragged over the list — let it bubble to the
        // sessions-block drop target; rows aren't a target for it.
        if (ev.dataTransfer.types.includes("application/x-mf-section")) return;
        ev.preventDefault();
        const rect = (ev.currentTarget as HTMLElement).getBoundingClientRect();
        onDropCue(title, ev.clientY - rect.top < rect.height / 2 ? "above" : "below");
      }}
      onDragLeave={(ev: DragEvent) => {
        if (!(ev.currentTarget as HTMLElement).contains(ev.relatedTarget as Node))
          onDropCue(title, null);
      }}
      onDrop={(ev: DragEvent) => {
        // Section drags are handled by the sessions-block, not by rows.
        if (ev.dataTransfer.types.includes("application/x-mf-section")) return;
        ev.preventDefault();
        const rect = (ev.currentTarget as HTMLElement).getBoundingClientRect();
        onDropRow(
          ev.dataTransfer.getData("text/plain"),
          title,
          ev.clientY - rect.top < rect.height / 2
        );
        onDropCue(title, null);
      }}
    >
      <div
        className="inst-row"
        onClick={() => {
          if (editing || Date.now() - editEndedAt.current < DBLCLICK_MS + 100) return;
          if (focused !== title) {
            selectSession(title);
            return;
          }
          if (!pending) armRename();
        }}
        onDoubleClick={() => {
          clearRenameTimer();
          if (!missing && !pending) ideSession(title, true);
        }}
      >
        <span className="grip" title="Drag to reorder">⠿</span>
        <span className="idx" title={num ? `Ctrl+${num} / Alt+${num} to focus` : ""}>{num}</span>
        <span className={"dot " + inst.status + (disconnected ? " disconnected" : "")} />
        {!pending && (
          <button
            className={"chevron" + (expanded ? " open" : "")}
            title="Actions"
            onClick={(e) => act(() => setExpanded((v) => !v), e)}
          >
            ›
          </button>
        )}
        <span className="meta">
          {editing ? (
            <input
              className="title title-edit"
              type="text"
              defaultValue={shown}
              autoFocus
              autoComplete="off"
              spellCheck={false}
              onFocus={(e) => e.currentTarget.select()}
              onMouseDown={(e) => e.stopPropagation()}
              onClick={(e) => e.stopPropagation()}
              onDoubleClick={(e) => e.stopPropagation()}
              onBlur={(e) => commitRename(e.currentTarget.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  e.preventDefault();
                  commitRename(e.currentTarget.value);
                } else if (e.key === "Escape") {
                  e.preventDefault();
                  cancelled.current = true;
                  editEndedAt.current = Date.now();
                  setEditing(false);
                }
              }}
            />
          ) : (
            <span
              className={"title" + (alias ? " aliased" : "")}
              title={[
                alias ? `${alias}  ·  ${label.full}` : label.full,
                // The real title is the identity behind a reformatted label —
                // it's what every API path and `tmux attach` is keyed by.
                label.kind ? `session: ${displayTitle(inst)}` : "",
                inst.branch ? `branch: ${inst.branch}` : "",
                focused === title ? "Click again to rename" : "",
              ]
                .filter(Boolean)
                .join("\n")}
            >
              {shown}
            </span>
          )}
        </span>
        <span className={"stagechip " + chip.cls} title={chip.title}>{chip.label}</span>
        {check && (
          <span className={"stagechip checkchip " + check.cls} title={check.title}>
            {check.label}
          </span>
        )}
        {!pending && (
          <button
            className={"kill" + (missing ? " cleanup" : "")}
            title={
              missing
                ? "Clean up — workspace is gone; remove this session"
                : "End session — keeps worktree (Ctrl+W / Del; undo with Ctrl+Shift+T)"
            }
            onClick={(e) => act(() => (missing ? cleanupMissing(title) : killSession(title)), e)}
          >
            {missing ? "Clean up" : "✕"}
          </button>
        )}
      </div>
      {expanded && !pending && (
        <div className="inst-actions">
          <div className="folder-row">
            <span className="folder-path" title={folder}>{inst.folder_label || folder || "—"}</span>
            <button
              className="folder-copy"
              title="Copy full folder path"
              onClick={(e) =>
                act(async () => {
                  if (!folder) return;
                  if (await copyText(folder)) toast("Copied path");
                }, e)
              }
            >
              Copy path
            </button>
          </div>
          <div className="menu-sep" />
          {missing ? (
            <button className="danger" onClick={() => cleanupMissing(title)}>
              Clean up — remove session
            </button>
          ) : (
            <>
              {caps.git && (
                <>
                  <button onClick={() => commitSession(title)}>
                    Commit…<span className="kbd">Ctrl+K C</span>
                  </button>
                  <button onClick={() => pushSession(title)}>
                    Push<span className="kbd">Ctrl+K P</span>
                  </button>
                  <button onClick={() => makePrSession(title)}>
                    Make PR<span className="kbd">Ctrl+K R</span>
                  </button>
                  {inst.stage === "pr" && (
                    <button
                      onClick={() =>
                        act(async () => {
                          if (!confirm("Merge this branch's PR into staging?")) return;
                          try {
                            await instApi(title, "/merge-pr", { method: "POST" });
                          } catch (err) {
                            alert("Merge failed: " + errMsg(err));
                          }
                          await refreshInstances();
                        })
                      }
                    >
                      Merge to staging
                    </button>
                  )}
                  {inst.pr_url && (
                    <button onClick={() => window.open(inst.pr_url!, "_blank")}>Open PR ↗</button>
                  )}
                  <div className="menu-sep" />
                </>
              )}
              {inst.setup?.state === "failed" && (
                <button
                  onClick={(e) =>
                    act(async () => {
                      try {
                        await instApi(title, "/setup/rerun", { method: "POST" });
                        toast("Worktree setup re-running — watch the setup chip");
                      } catch (err) {
                        toast("Setup re-run failed: " + errMsg(err), { duration: 6000 });
                      }
                      await refreshInstances();
                    }, e)
                  }
                >
                  Re-run worktree setup
                </button>
              )}
              {inst.check && inst.check.state !== "running" && (
                <button
                  onClick={(e) =>
                    act(async () => {
                      try {
                        await instApi(title, "/check", { method: "POST" });
                        toast("Checks running…");
                      } catch (err) {
                        toast("Check run failed: " + errMsg(err), { duration: 6000 });
                      }
                      await refreshInstances();
                    }, e)
                  }
                >
                  Run checks now
                </button>
              )}
              <button onClick={() => openDialogFor("rename", title)}>Rename…</button>
              <button onClick={() => copySession(title)}>
                Duplicate session<span className="kbd">Ctrl+K D</span>
              </button>
              <button onClick={() => ideSession(title)}>
                Open / focus {ideName}<span className="kbd">Ctrl+K O</span>
              </button>
              <button onClick={() => hideSession(title)}>
                {hidden ? "Show window" : "Hide window"}
                {!hidden && <span className="kbd">Ctrl+K H</span>}
              </button>
              {inst.ports?.base ? (
                <button
                  onClick={(e) =>
                    act(
                      () =>
                        void window.open(
                          `http://${location.hostname}:${inst.ports!.base}/`,
                          "_blank"
                        ),
                      e
                    )
                  }
                >
                  Open preview ↗<span className="kbd">:{inst.ports.base}</span>
                </button>
              ) : null}
              <button onClick={() => (paused ? resumeSession(title) : pauseSession(title))}>
                {paused ? "Resume session" : "Pause session"}
              </button>
              {caps.git && (
                <button
                  className="danger"
                  onClick={() =>
                    act(async () => {
                      if (
                        !confirm(
                          `Delete '${title}' and PERMANENTLY remove its worktree directory?\nThis also closes its ${ideName} window. This cannot be undone.`
                        )
                      )
                        return;
                      try {
                        await instApi(title, "/cleanup", { method: "POST" });
                      } catch (err) {
                        alert("Cleanup failed: " + errMsg(err));
                      }
                      useUi.getState().setHidden(title, false);
                      await refreshInstances();
                    })
                  }
                >
                  Delete + wipe worktree
                </button>
              )}
            </>
          )}
        </div>
      )}
    </li>
  );
});
