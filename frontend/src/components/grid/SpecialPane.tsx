/** Special grid panes (ports app.js sections 17/18's logs / system-logs /
 * assistant-chat panes). They hold grid slots and drag like session panes. */

import { useEffect, useRef, useState } from "react";
import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import { api } from "../../api/client";
import { termTheme } from "../../lib/terminals";
import { dropSideFor } from "./layout";
import { sentinel, type DragCtx, type SpecialPaneDesc } from "./TerminalGrid";

export function SpecialPane({ desc, drag }: { desc: SpecialPaneDesc; drag: DragCtx }) {
  const title = sentinel(desc.kind);
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
        (desc.kind === "logs" ? "logs-pane" : desc.kind === "syslogs" ? "syslogs-pane" : "chat-pane")
      }
      data-title={title}
      {...paneDrag}
    >
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

/** Read-only ws-streamed terminal over a fixed ws path. */
function useWsTerm(hostRef: React.RefObject<HTMLDivElement | null>, wsPath: string, interactive: boolean) {
  const [state, setState] = useState("connecting");
  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;
    const term = new Terminal({
      cursorBlink: interactive,
      fontSize: 12,
      theme: termTheme(),
      disableStdin: !interactive,
      fontFamily: 'ui-monospace, "Cascadia Code", Menlo, Consolas, monospace',
      scrollback: 20000,
      macOptionClickForcesSelection: true,
    });
    const fit = new FitAddon();
    term.loadAddon(fit);
    term.open(host);
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(proto + "://" + location.host + wsPath);
    ws.binaryType = "arraybuffer";
    const doFit = (sendResize: boolean) => {
      try {
        fit.fit();
      } catch {
        return;
      }
      if (sendResize && ws.readyState === WebSocket.OPEN)
        ws.send(JSON.stringify({ type: "resize", cols: term.cols, rows: term.rows }));
    };
    ws.onopen = () => {
      setState("streaming");
      doFit(true);
    };
    ws.onmessage = (ev) => {
      if (typeof ev.data === "string") {
        try {
          const j = JSON.parse(ev.data);
          if (j.type === "error") {
            term.write("\r\n[error] " + j.message + "\r\n");
            return;
          }
        } catch {
          term.write(ev.data);
        }
      } else {
        term.write(new Uint8Array(ev.data));
      }
    };
    ws.onclose = () => setState("disconnected");
    ws.onerror = () => setState("error");
    if (interactive)
      term.onData((d) => {
        if (ws.readyState === WebSocket.OPEN) ws.send(d);
      });
    const obs = new ResizeObserver(() => doFit(true));
    obs.observe(host);
    const t = setTimeout(() => doFit(true), 50);
    return () => {
      clearTimeout(t);
      obs.disconnect();
      try {
        ws.close();
      } catch {
        /* closed */
      }
      term.dispose();
    };
  }, [hostRef, wsPath, interactive]);
  return state;
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
