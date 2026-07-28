/** Settings → System logs (partial 112 + loadLogs, section 21): read-only
 * tail of the server / ingestion log with optional 3s auto-refresh. */

import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../../../api/client";
import type { ScreenProps } from "../SettingsDialog";

interface LogsPayload {
  sources?: Array<{ name: string; label: string; path?: string }>;
  selected?: string;
  text?: string;
  size?: number;
  exists?: boolean;
  truncated?: boolean;
}

export function SystemLogs({ onOpenSysLogsPane }: ScreenProps) {
  const [sources, setSources] = useState<Array<{ name: string; label: string; path?: string }>>([]);
  const [selected, setSelected] = useState("server");
  const [text, setText] = useState("Loading…");
  const [meta, setMeta] = useState("");
  const [follow, setFollow] = useState(false);
  const viewRef = useRef<HTMLPreElement | null>(null);

  const load = useCallback(async () => {
    let d: LogsPayload;
    try {
      d = await api<LogsPayload>("/api/logs?name=" + encodeURIComponent(selected || "server"));
    } catch {
      setText("Could not load logs.");
      return;
    }
    setSources(d.sources || []);
    const view = viewRef.current;
    const atBottom = view ? view.scrollTop + view.clientHeight >= view.scrollHeight - 4 : true;
    setText(d.text || (d.exists ? "(log is empty)" : "(no log file yet)"));
    if (atBottom && view) requestAnimationFrame(() => (view.scrollTop = view.scrollHeight));
    const src = (d.sources || []).find((s) => s.name === d.selected);
    const kb = Math.round((d.size || 0) / 1024);
    setMeta((src?.path ? src.path + "  ·  " : "") + kb + " KB" + (d.truncated ? " (showing last 256 KB)" : ""));
  }, [selected]);

  useEffect(() => {
    load();
  }, [load]);

  useEffect(() => {
    if (!follow) return;
    const t = setInterval(() => {
      if (!document.hidden) load();
    }, 3000);
    return () => clearInterval(t);
  }, [follow, load]);

  return (
    <>
      <h3 className="set-section-title">System logs</h3>
      <p className="set-hint">
        The server's own log — every request plus errors and background activity, newest last
        (up to 256 KB). Handy when something misbehaves; copy the tail when reporting an issue.
      </p>
      <div className="logs-toolbar">
        <select
          id="logs-source"
          title="Which log to view"
          value={selected}
          onChange={(e) => setSelected(e.target.value)}
        >
          {sources.map((s) => (
            <option key={s.name} value={s.name}>
              {s.label}
            </option>
          ))}
        </select>
        <button type="button" id="logs-refresh" className="test-btn" onClick={load}>
          Refresh
        </button>
        <button
          type="button"
          id="logs-open-pane"
          className="test-btn"
          title="Open the log as a live pane on the main window"
          onClick={onOpenSysLogsPane}
        >
          Open as pane
        </button>
        <label className="logs-follow" title="Re-fetch the log every 3s">
          <input type="checkbox" id="logs-follow" checked={follow} onChange={(e) => setFollow(e.target.checked)} />{" "}
          Auto-refresh
        </label>
        <span id="logs-meta" className="muted">{meta}</span>
      </div>
      <pre id="logs-view" className="logs-view" ref={viewRef}>
        {text}
      </pre>
    </>
  );
}
