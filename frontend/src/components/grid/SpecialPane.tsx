/** Special grid panes (ports app.js sections 17/18's logs / system-logs /
 * assistant-chat panes). They hold grid slots and drag like session panes. */

import { useEffect, useRef, useState } from "react";
import { api } from "../../api/client";
import { useWsTerm } from "../../lib/wsTerm";
import { useUi } from "../../state/store";
import { dropSideFor } from "./layout";
import { sentinel, type DragCtx, type SpecialPaneDesc } from "./TerminalGrid";

export function SpecialPane({ desc, drag }: { desc: SpecialPaneDesc; drag: DragCtx }) {
  const title = sentinel(desc.kind, desc.session);
  const paneRef = useRef<HTMLElement | null>(null);

  const headDrag = {
    draggable: true,
    onDragStart: (ev: React.DragEvent) => {
      const t = ev.target as HTMLElement;
      if (t.closest("select, input, textarea")) {
        ev.preventDefault();
        return;
      }
      ev.dataTransfer.setData("text/plain", title);
      ev.dataTransfer.effectAllowed = "move";
      drag.start(title);
    },
    onDragEnd: () => drag.end(),
  };
  const paneDrag = {
    onDragOver: (ev: React.DragEvent) => {
      if (!drag.dragTitle) return;
      ev.preventDefault();
      if (title === drag.dragTitle) return;
      const rect = paneRef.current!.getBoundingClientRect();
      drag.hover(title, dropSideFor(rect, ev.clientX, ev.clientY));
    },
    onDrop: (ev: React.DragEvent) => {
      ev.preventDefault();
      drag.commit();
    },
  };

  return (
    <section
      ref={paneRef as React.RefObject<HTMLElement>}
      className={
        "pane focused " +
        (desc.kind === "logs"
          ? "logs-pane"
          : desc.kind === "syslogs"
            ? "syslogs-pane"
            : desc.kind === "verify"
              ? "verify-pane"
              : "chat-pane")
      }
      data-title={title}
      {...paneDrag}
    >
      {desc.kind === "verify" && <VerifyBody desc={desc} headDrag={headDrag} />}
      {desc.kind === "logs" && <LogsBody desc={desc} headDrag={headDrag} />}
      {desc.kind === "syslogs" && <SysLogsBody desc={desc} headDrag={headDrag} />}
      {desc.kind === "chat" && <ChatBody desc={desc} headDrag={headDrag} />}
    </section>
  );
}

type HeadDrag = {
  draggable: boolean;
  onDragStart(ev: React.DragEvent): void;
  onDragEnd(): void;
};

/** A verify run, watched. Read-only ON PURPOSE.
 *
 * `useWsTerm(..., false)` sets `disableStdin`, so this is a window you look at
 * and not one you talk to. That is the point: the run is working a checklist it
 * was given, and its answers are the artifact — typing at it mid-run would
 * produce a report about a conversation nobody can reconstruct later. If you
 * want to take over, the session is real and the pane says where it is.
 *
 * The same websocket an ordinary session pane uses, so what you see here is
 * exactly what the agent is doing, live. Closing this pane does NOT stop the
 * run: it keeps working and writes its results file, and the Verify dialog
 * reopens the window for as long as the session exists. */
function VerifyBody({ desc, headDrag }: { desc: SpecialPaneDesc; headDrag: HeadDrag }) {
  const hostRef = useRef<HTMLDivElement | null>(null);
  const session = desc.session || "";
  const state = useWsTerm(
    hostRef,
    "/api/instances/" + encodeURIComponent(session) + "/terminal",
    false
  );
  return (
    <>
      <div className="pane-head" {...headDrag}>
        <span className="grip" title="Drag to move this window">⠿</span>
        <span className="title">Verifying</span>
        <span className="branch">{session}</span>
        <span className="state">{state}</span>
        <div className="actions">
          {/* The way back. Watch is a ONE-WAY trip without it: the Verify dialog
              has to close to open this pane (it is a full-screen modal, so a
              pane opened behind it is a press with no visible effect), which
              leaves somebody who was triaging three checklists with no route
              back to the other two except remembering Alt+V. */}
          <button
            className="act verify-pane-back"
            title="Back to Verify (Alt+V)"
            onClick={(e) => {
              e.stopPropagation();
              useUi.getState().openDialogFor("verify");
            }}
          >
            Verify
          </button>
          <button
            className="act verify-pane-close"
            title="Close this window — the run keeps going"
            onClick={(e) => {
              e.stopPropagation();
              desc.onClose();
            }}
          >
            Close
          </button>
        </div>
      </div>
      <div className="pane-body">
        <div className="pane-term" ref={hostRef} />
      </div>
    </>
  );
}

function LogsBody({ desc, headDrag }: { desc: SpecialPaneDesc; headDrag: HeadDrag }) {
  const hostRef = useRef<HTMLDivElement | null>(null);
  const state = useWsTerm(hostRef, "/api/mindflock/logs", false);
  return (
    <>
      <div className="pane-head" {...headDrag}>
        <span className="grip" title="Drag to move this window">⠿</span>
        <span className="title">MindFlock logs</span>
        <span className="branch">logs/mindflock-ui.log</span>
        <span className="state">{state}</span>
        <div className="actions">
          <button className="act logs-close" title="Close logs" onClick={(e) => { e.stopPropagation(); desc.onClose(); }}>
            Close
          </button>
        </div>
      </div>
      <div className="pane-body">
        <div className="pane-term" ref={hostRef} />
      </div>
    </>
  );
}

function ChatBody({ desc, headDrag }: { desc: SpecialPaneDesc; headDrag: HeadDrag }) {
  const hostRef = useRef<HTMLDivElement | null>(null);
  const state = useWsTerm(hostRef, "/api/assistant/terminal", true);
  return (
    <>
      <div className="pane-head" {...headDrag}>
        <span className="grip" title="Drag to move this window">⠿</span>
        <span className="title">Assistant</span>
        <span className="state">{state}</span>
        <div className="actions">
          <button className="act chat-close" title="Close chat" onClick={(e) => { e.stopPropagation(); desc.onClose(); }}>
            Close
          </button>
        </div>
      </div>
      <div className="pane-body">
        <div className="pane-term" ref={hostRef} />
      </div>
    </>
  );
}

interface LogsPayload {
  sources?: Array<{ name: string; label: string }>;
  selected?: string;
  text?: string;
  size?: number;
  exists?: boolean;
  truncated?: boolean;
}

function SysLogsBody({ desc, headDrag }: { desc: SpecialPaneDesc; headDrag: HeadDrag }) {
  const [sources, setSources] = useState<Array<{ name: string; label: string }>>([]);
  const [selected, setSelected] = useState("server");
  const [text, setText] = useState("Loading…");
  const [meta, setMeta] = useState("");
  const viewRef = useRef<HTMLPreElement | null>(null);

  useEffect(() => {
    let live = true;
    const load = async () => {
      let d: LogsPayload;
      try {
        d = await api<LogsPayload>("/api/logs?name=" + encodeURIComponent(selected));
      } catch {
        if (live) setMeta("error");
        return;
      }
      if (!live) return;
      setSources(d.sources || []);
      const t = d.text || (d.exists ? "(log is empty)" : "(no log file yet)");
      const view = viewRef.current;
      const atBottom = view
        ? view.scrollTop + view.clientHeight >= view.scrollHeight - 4
        : true;
      setText(t);
      if (atBottom && view) requestAnimationFrame(() => (view.scrollTop = view.scrollHeight));
      const kb = Math.round((d.size || 0) / 1024);
      setMeta(kb + " KB" + (d.truncated ? " (last 256 KB)" : ""));
    };
    load();
    // Live tail every 2s while visible; a hidden tab catches up next tick.
    const timer = setInterval(() => {
      if (!document.hidden) load();
    }, 2000);
    return () => {
      live = false;
      clearInterval(timer);
    };
  }, [selected]);

  return (
    <>
      <div className="pane-head" {...headDrag}>
        <span className="grip" title="Drag to move this window">⠿</span>
        <span className="title">System logs</span>
        <select
          className="syslogs-source"
          title="Which log to view"
          value={selected}
          onClick={(e) => e.stopPropagation()}
          onChange={(e) => {
            setText("Loading…");
            setSelected(e.target.value);
          }}
        >
          {sources.map((s) => (
            <option key={s.name} value={s.name}>
              {s.label}
            </option>
          ))}
        </select>
        <span className="state">{meta}</span>
        <div className="actions">
          <button className="act syslogs-close" title="Close logs" onClick={(e) => { e.stopPropagation(); desc.onClose(); }}>
            Close
          </button>
        </div>
      </div>
      <div className="pane-body">
        <pre className="syslogs-view" ref={viewRef}>
          {text}
        </pre>
      </div>
    </>
  );
}
