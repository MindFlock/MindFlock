/** One session row (ports app.js section 9's _createSidebarRow /
 * _updateSidebarRow / _instActionsHtml / _rowAction). */

import {
  memo,
  useCallback,
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
  const { data: config } = useConfig();
  const focused = useUi((s) => s.focused);
  const hidden = useUi((s) => s.hidden.has(inst.title));
  const alias = useUi((s) => s.aliases[inst.title]);
  const openDialogFor = useUi((s) => s.openDialogFor);

  const title = inst.title;
  const missing = !!inst.workspace_missing;
  const paused = inst.status === "paused";
  const caps = config?.caps ?? { git: true, tailscale: true, ticketing: true };
  const ideName = config?.ide_name || "Cursor";
  const chip = chipState(inst);
  const check = checkChip(inst);
  const num = idx < 9 ? String(idx + 1) : "";
  const shown = alias || displayTitle(inst);
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

  const rowCls =
    "inst" +
    (focused === title ? " active" : "") +
    (hidden ? " is-hidden" : "") +
    (missing ? " ws-missing" : "") +
    (dropCue ? ` drop-${dropCue}` : "");

  return (
    <li
      className={rowCls}
      data-title={title}
      draggable
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
        onClick={() => selectSession(title)}
        onDoubleClick={() => {
          if (!missing) ideSession(title, true);
        }}
      >
        <span className="grip" title="Drag to reorder">⠿</span>
        <span className="idx" title={num ? `Ctrl+${num} / Alt+${num} to focus` : ""}>{num}</span>
        <span className={"dot " + inst.status + (disconnected ? " disconnected" : "")} />
        <button
          className={"chevron" + (expanded ? " open" : "")}
          title="Actions"
          onClick={(e) => act(() => setExpanded((v) => !v), e)}
        >
          ›
        </button>
        <span className="meta">
          <span
            className={"title" + (alias ? " aliased" : "")}
            title={alias ? `${alias}  ·  ${displayTitle(inst)}` : displayTitle(inst)}
          >
            {shown}
          </span>
        </span>
        <span className={"stagechip " + chip.cls} title={chip.title}>{chip.label}</span>
        {check && (
          <span className={"stagechip checkchip " + check.cls} title={check.title}>
            {check.label}
          </span>
        )}
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
      </div>
      {expanded && (
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
