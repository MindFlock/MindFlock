/** Settings → Connections (partial 104 + renderConnectionsInline): the
 * at-a-glance status list; Configure jumps to the owning screen. */

import { useEffect, useState } from "react";
import { api } from "../../../api/client";
import type { ScreenProps } from "../SettingsDialog";

const CONN_PILL: Record<string, string> = {
  connected: "Connected",
  attention: "Action needed",
  not_connected: "Not connected",
};

interface Conn {
  name: string;
  status?: string;
  detail?: string;
  purpose?: string;
  settings_screen?: string;
}

export function Connections({ gotoScreen }: ScreenProps) {
  const [conns, setConns] = useState<Conn[] | null>(null);
  const [summary, setSummary] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    (async () => {
      try {
        const data = await api<{ connections?: Conn[]; summary?: { connected?: number; total?: number } }>(
          "/api/connections?refresh=1"
        );
        const list = data?.connections || [];
        setConns(list);
        const sum = data?.summary || {};
        setSummary(list.length ? `${sum.connected || 0}/${sum.total || list.length} connected` : "");
      } catch {
        setError("Could not load connections.");
      }
    })();
  }, []);

  return (
    <>
      <h3 className="set-section-title">
        Connections <span id="settings-connections-summary" className="muted">{summary}</span>
      </h3>
      <p className="set-hint">
        The outside services MindFlock talks to — your coding agent, GitHub, ticketing and
        phone access. Configure opens that service's setup.
      </p>
      <div id="settings-connections-list" className="conn-list">
        {error ? (
          <p className="error">{error}</p>
        ) : conns === null ? (
          <p className="set-hint">Checking…</p>
        ) : (
          conns.map((c) => (
            <div className="conn-row" key={c.name}>
              <div className="conn-main">
                <div className="conn-head">
                  <span className="conn-name">{c.name}</span>
                </div>
                <div className="conn-detail muted">{c.detail || c.purpose || ""}</div>
              </div>
              <span className={"conn-pill conn-" + (c.status || "not_connected")}>
                {CONN_PILL[c.status || ""] || c.status || ""}
              </span>
              {c.settings_screen && (
                <button
                  type="button"
                  className="test-btn conn-configure"
                  onClick={() => gotoScreen(c.settings_screen!)}
                >
                  Configure
                </button>
              )}
            </div>
          ))
        )}
      </div>
    </>
  );
}
