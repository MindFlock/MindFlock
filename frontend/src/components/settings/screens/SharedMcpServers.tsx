/** Settings → Agent orchestration → Shared MCP servers.
 *
 * Every agent window normally starts its own copy of every MCP server in the
 * user's Claude config. A server switched on here runs ONCE per machine in
 * MindFlock's bridge (backend.mcp.bridge) and every window connects to that
 * copy instead — bound to `general.mcp_shared_servers`, read at launch, so a
 * change applies to each session's next (re)launch. Off by default: a server
 * that works on its launch folder or keeps per-client state behaves
 * differently when shared, and the row's caution line says so. */

import { useCallback, useEffect, useState } from "react";
import { api } from "../../../api/client";
import { toast } from "../../../lib/toast";
import { useSettings } from "../useSettings";

/** One row of GET /api/settings/mcp-shared (backend.providers.claude_mcp.list_servers). */
export interface McpServerRow {
  name: string;
  transport: string;
  command: string;
  shareable: boolean;
  caution: string;
  reason: string;
  shared: boolean;
}

interface BridgeUpstream {
  name: string;
  pid: number | null;
  running: boolean;
  sessions: number;
  refused_requests?: Record<string, number>;
}

/** GET /api/settings/mcp-shared. */
export interface McpSharedInfo {
  available: boolean;
  servers: McpServerRow[];
  shared: string[];
  bridge: { running: boolean; sessions?: number; upstreams: BridgeUpstream[]; log_dir?: string };
}

export function SharedMcpServers() {
  const s = useSettings();
  const [info, setInfo] = useState<McpSharedInfo | null>(null);
  const [failed, setFailed] = useState(false);
  const stored = s.get("general", "mcp_shared_servers");
  const shared = Array.isArray(stored) ? (stored as string[]) : [];

  const load = useCallback(() => {
    api<McpSharedInfo>("/api/settings/mcp-shared")
      .then((r) => {
        setInfo(r || null);
        setFailed(false);
      })
      .catch(() => setFailed(true));
  }, []);
  useEffect(load, [load]);

  const toggle = async (name: string, on: boolean) => {
    const next = shared.filter((n) => n !== name);
    if (on) next.push(name);
    // A failed save is already toasted; don't follow it with "shared".
    if (!(await s.saveField("general", "mcp_shared_servers", next))) return;
    toast(
      on
        ? `${name} shared — from each session's next launch`
        : `${name} back to one copy per window — from each session's next launch`
    );
    load();
  };

  return (
    <SharedMcpServersView info={info} failed={failed} shared={shared} onToggle={toggle} />
  );
}

/** The section itself, from a GET /api/settings/mcp-shared payload (`null`
 * while loading) — pure, so it renders in tests. */
export function SharedMcpServersView({
  info,
  failed,
  shared,
  onToggle,
}: {
  info: McpSharedInfo | null;
  failed: boolean;
  shared: string[];
  onToggle: (name: string, on: boolean) => void;
}) {
  const upstream = (name: string) => info?.bridge?.upstreams?.find((u) => u.name === name);

  return (
    <>
      <h3 className="set-section-title">Shared MCP servers</h3>
      <p className="set-hint orch-intro">
        Every agent window starts its own copy of each MCP server in your Claude config — often
        hundreds of MB per window. A server switched on here runs once on this machine and every
        session connects to that copy. Applies on each session's next launch; needs the MindFlock
        MCP switch above to be on.
      </p>
      {failed && <span className="muted">Couldn't read your MCP servers.</span>}
      {!failed && info === null && <span className="muted">Reading your MCP servers…</span>}
      {info && !info.available && (
        <span className="set-hint mcp-share-unavailable">
          Off on this machine: the server was started with MINDFLOCK_MCP_SHARE=0 (or this OS has no
          bridge).
        </span>
      )}
      {info && info.servers.length === 0 && (
        <span className="muted mcp-share-empty">No MCP servers in your Claude config.</span>
      )}
      {info &&
        info.servers.map((row) => {
          const on = shared.includes(row.name);
          const up = upstream(row.name);
          const refused = Object.keys(up?.refused_requests || {});
          return (
            <div className="set-row set-switch-row mcp-share-row" key={row.name} data-mcp-server={row.name}>
              <span className="notif-rule-text">
                <span className="set-label">
                  {row.name} <span className="muted mcp-share-cmd">{row.command}</span>
                </span>
                {row.shareable ? (
                  <span className="set-hint notif-rule-desc">
                    {row.caution ? (
                      <span className="mcp-share-caution">{row.caution}</span>
                    ) : (
                      "One copy for every session instead of one per window."
                    )}
                    {on && up?.running && (
                      <span className="mcp-share-live">
                        {" "}
                        Running once (pid {up.pid}) for {up.sessions} session
                        {up.sessions === 1 ? "" : "s"}.
                      </span>
                    )}
                    {on && refused.length > 0 && (
                      <span className="mcp-share-caution">
                        {" "}
                        It asked the client for {refused.join(", ")}, which a shared copy can't
                        answer — switch it off.
                      </span>
                    )}
                  </span>
                ) : (
                  <span className="set-hint notif-rule-desc">{row.reason}</span>
                )}
              </span>
              <label className="ca-switch">
                <input
                  type="checkbox"
                  aria-label={`Share ${row.name} across sessions`}
                  checked={on && row.shareable}
                  disabled={!row.shareable || !info.available}
                  onChange={(e) => onToggle(row.name, e.target.checked)}
                />
                <span className="ca-slider" />
              </label>
            </div>
          );
        })}
    </>
  );
}
