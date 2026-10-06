/** The rail's group header for sessions started together, its queued lines,
 * and the "On their own" divider (ship lanes, SPEC §7.C.2).
 *
 * Same component family as the device header (`li.device-group`): a click
 * folds the group, the name reads in small caps, badges sit on the right.
 * Neither a header nor a queued line is a rail ROW: no number, no drag, no
 * drop target, never in the saved order or railOrder (lib/runs.ts says why).
 *
 * The ⋯ menu acts through the runs API at once — never a `window.prompt` /
 * `confirm` (Electron has neither): "Add lines…" and "Cancel…" open inline
 * rows inside the menu. It is the group's one home: its queued lines (Start
 * now / Remove), Pause, Cancel and, once it has finished, "Copy summary" —
 * what waits on you is the bell's. */

import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import type { RunTask } from "../../api/types";
import { api } from "../../api/client";
import { refreshRuns, useOutbox, useRun } from "../../state/runs";
import { useUi } from "../../state/store";
import { toast } from "../../lib/toast";
import { copyText } from "../../lib/clipboard";
import { errorPop } from "../../lib/errorPop";
import { errMsg } from "../../lib/format";
import { groupTitle, queuedLine, queuedTitle, shippedBadge, type RunGroup } from "../../lib/runs";
import { cancelRun, runAction as runAct, runPath, taskPath } from "../../lib/runsApi";
import { summaryFor, summaryText } from "../outbox/outbox";

export function RunGroupHeader({ group, repoPath }: { group: RunGroup; repoPath?: string }) {
  const toggle = useUi((s) => s.toggleRunCollapsed);
  const [menu, setMenu] = useState<DOMRect | null>(null);
  const more = useRef<HTMLButtonElement | null>(null);
  const cls =
    "device-group run-group-head" +
    (group.done ? " is-done" : "") +
    (group.paused ? " is-paused" : "") +
    (group.collapsed ? " is-folded" : "");
  return (
    <li
      className={cls}
      data-run={group.id}
      title={groupTitle(group)}
      onClick={() => toggle(group.id)}
    >
      <span className="dev-caret">{group.collapsed ? "▸" : "▾"}</span>
      {group.done && !group.cancelled && <span className="rg-done">✓</span>}
      <span className="dev-name">{group.name}</span>
      {group.lane && !group.done && <span className="rg-lane">{group.lane}</span>}
      <span className="rg-sp" />
      {group.needs > 0 && (
        <span className="dev-badge rg-needs" title={group.needs + " waiting on you"}>
          {group.needs}
        </span>
      )}
      {group.cancelled ? (
        <span className="dev-badge rg-paused rg-cancelled">cancelled</span>
      ) : group.paused ? (
        <span className="dev-badge rg-paused">paused</span>
      ) : group.waitingUsage && !group.done ? (
        <span className="dev-badge rg-usage">usage</span>
      ) : null}
      <span className={"dev-badge rg-shipped" + (group.done && !group.cancelled ? " ok" : "")}>{shippedBadge(group)}</span>
      {group.failed > 0 && <span className="dev-badge rg-failed">{group.failed} failed</span>}
      <button
        ref={more}
        type="button"
        className="rg-more"
        title="Pause, add lines, start the next one, cancel…"
        aria-label={"Actions for " + group.name}
        aria-haspopup="menu"
        onClick={(e) => {
          e.stopPropagation();
          setMenu(menu ? null : more.current!.getBoundingClientRect());
        }}
      >
        ⋯
      </button>
      {menu &&
        createPortal(
          <RunGroupMenu group={group} at={menu} repoPath={repoPath} onClose={() => setMenu(null)} />,
          document.body
        )}
    </li>
  );
}

function RunGroupMenu({
  group,
  at,
  repoPath,
  onClose,
}: {
  group: RunGroup;
  at: DOMRect;
  repoPath?: string;
  onClose(): void;
}) {
  const ref = useRef<HTMLDivElement | null>(null);
  const [mode, setMode] = useState<"" | "add" | "cancel">("");
  const [lines, setLines] = useState("");
  const [busy, setBusy] = useState(false);
  // A finished group's summary — the same cached query the bell keeps warm.
  // It holds only the last week's: an older group's comes from its own record.
  const { data: outbox } = useOutbox();
  const kept = group.done ? summaryFor(outbox, group.id) : null;
  const { data: record, isFetched: recordRead } = useRun(group.done && !kept ? group.id : null);
  const summary = group.done ? summaryText(outbox, group.id, record) : "";
  const looking = group.done && !summary && !kept && !recordRead;
  useEffect(() => {
    const onDown = (e: MouseEvent) => {
      if (!ref.current?.contains(e.target as Node)) onClose();
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        onClose();
      }
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey, true);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey, true);
    };
  }, [onClose]);
  const id = group.id;
  const go = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    try {
      await fn();
    } finally {
      setBusy(false);
      onClose();
    }
  };
  const addLines = async () => {
    const text = lines.trim();
    if (!text) return;
    setBusy(true);
    try {
      // The same parse the New dialog previews with: ticket IDs resolve, a
      // ticket that can't be found is refused rather than turned into a task.
      const pv = await api<{ items?: Array<Record<string, unknown>> }>("/api/runs/preview", {
        json: { text, repo_path: repoPath || "" },
      });
      const items = (pv?.items || []).filter((i) => !i.error);
      const bad = (pv?.items || []).filter((i) => i.error);
      if (!items.length) {
        errorPop("Nothing to add", bad.map((i) => String(i.ref || i.id || "") + ": " + String(i.error)).join("\n") || "no lines");
        return;
      }
      await api(runPath(id, "/tasks"), {
        json: {
          items: items.map((i) =>
            i.kind === "ticket" ? { kind: "ticket", source: i.source, id: i.id } : { kind: "task", text: i.text }
          ),
        },
      });
      toast("Added " + items.length + (items.length === 1 ? " line" : " lines") + " to " + group.name);
      if (bad.length) errorPop("Some lines weren't added", bad.map((i) => String(i.ref || i.id || "") + ": " + String(i.error)).join("\n"));
      onClose();
    } catch (err) {
      errorPop("Add lines failed", errMsg(err));
    } finally {
      setBusy(false);
      void refreshRuns();
    }
  };
  const left = Math.max(8, Math.min(at.right - 260, window.innerWidth - 268));
  return (
    <div
      ref={ref}
      className="rg-menu"
      role="menu"
      style={{ top: at.bottom + 4 + "px", left: left + "px" }}
      onClick={(e) => e.stopPropagation()}
    >
      <div className="rg-menu-head">{group.name}</div>
      {!group.done && (
        <button
          type="button"
          role="menuitem"
          disabled={busy}
          onClick={() =>
            go(() =>
              group.paused
                ? runAct("Resume", runPath(id, "/resume"), {}, group.name + " resumed")
                : runAct("Pause", runPath(id, "/pause"), { reason: "user" }, group.name + " paused — agents keep working; nothing new starts or ships")
            )
          }
        >
          {group.paused ? "Resume" : "Pause"}
          <span className="rg-hint">{group.paused ? "start and ship again" : "agents keep working"}</span>
        </button>
      )}
      {!group.done &&
        (mode === "add" ? (
          <div className="rg-inline">
            <textarea
              className="rg-lines"
              rows={3}
              autoFocus
              placeholder="One line per task, or ticket IDs"
              value={lines}
              onChange={(e) => setLines(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
                  e.preventDefault();
                  void addLines();
                }
              }}
            />
            <div className="rg-inline-acts">
              <button type="button" className="rg-primary" disabled={busy || !lines.trim()} onClick={() => void addLines()}>
                Add
              </button>
              <button type="button" onClick={() => setMode("")}>
                Back
              </button>
            </div>
          </div>
        ) : (
          <button type="button" role="menuitem" disabled={busy} onClick={() => setMode("add")}>
            Add lines…
          </button>
        ))}
      {group.queued.length > 0 && (
        <>
          <div className="rg-menu-sec">Queued · {group.queued.length}</div>
          {group.queued.map((t) => (
            <div className="rg-qline" key={t.id}>
              <span className="rg-qtitle" title={queuedTitle(t)}>
                {queuedTitle(t)}
              </span>
              <button
                type="button"
                disabled={busy}
                title="Start it now, past the at-a-time limit (once)"
                onClick={() => go(() => runAct("Start now", taskPath(id, t.id, "start-now"), {}, "Starting " + queuedTitle(t)))}
              >
                Start now
              </button>
              <button
                type="button"
                disabled={busy}
                title="Take it out of the group — it never starts"
                onClick={() => go(() => runAct("Remove", taskPath(id, t.id, "skip"), {}, "Removed " + queuedTitle(t)))}
              >
                Remove
              </button>
            </div>
          ))}
        </>
      )}
      {group.done && (
        <button
          type="button"
          role="menuitem"
          className="rg-copy-summary"
          disabled={!summary}
          title={
            summary
              ? "Copy what this group did, as Markdown"
              : looking
                ? "Looking for this group's summary…"
                : "MindFlock has no summary for this group"
          }
          onClick={async () => {
            if (!summary) return;
            toast((await copyText(summary)) ? "Copied the summary as Markdown" : "Couldn't copy the summary");
            onClose();
          }}
        >
          Copy summary
          <span className="rg-hint">as Markdown</span>
        </button>
      )}
      {!group.done &&
        (mode === "cancel" ? (
          <div className="rg-inline rg-confirm">
            <span>Stop starting new work and stop shipping? Sessions and branches are kept.</span>
            <div className="rg-inline-acts">
              <button
                type="button"
                className="rg-danger"
                disabled={busy}
                onClick={() => go(() => cancelRun(id, group.name))}
              >
                Cancel group
              </button>
              <button type="button" onClick={() => setMode("")}>
                Keep going
              </button>
            </div>
          </div>
        ) : (
          <button type="button" role="menuitem" className="rg-danger-item" disabled={busy} onClick={() => setMode("cancel")}>
            Cancel…
          </button>
        ))}
    </div>
  );
}

/** A line still waiting for a slot: dim, hollow dot, no number, no ✕ (Remove
 * lives in the header's ⋯). Not a rail row — see the module comment. */
export function QueuedRow({ task, pos, groupName }: { task: RunTask; pos: number; groupName: string }) {
  const title = queuedTitle(task);
  return (
    <li
      className="inst run-queued"
      data-queued={task.id}
      title={title + "\nQueued in " + groupName + " — it starts as a slot frees up. Start it now or remove it from the group's ⋯ menu."}
    >
      <div className="inst-row">
        <span className="idx" />
        <span className="dot queued" />
        <span className="chevron q-spacer" aria-hidden="true">
          ›
        </span>
        <span className="meta has-lineage">
          <span className="title">{title}</span>
          <span className="lineage rep-idle">{queuedLine(pos)}</span>
        </span>
        <span className="stagechip s-queued">queued</span>
      </div>
    </li>
  );
}

/** The divider over sessions that aren't in any group — only drawn under at
 * least one group header. */
export function OwnHeader() {
  return (
    <li className="device-group run-group-head run-own" title="Sessions not started in a group">
      <span className="dev-caret" />
      <span className="dev-name">On their own</span>
    </li>
  );
}
