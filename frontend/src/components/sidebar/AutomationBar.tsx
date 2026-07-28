/** MindFlock ticket-ingestion bar (port of the automation toggle, section 17).
 * Hidden entirely without a connected ticketing source. The switch reflects the
 * DESIRED state of the TICKET half of the pipeline (persisted server-side,
 * restored on reboot; the PR-review bar gates the other half of the same
 * process). The dot reflects reality — gold while starting or idle-waiting
 * for a ticket, green while one is actually being handled. Ingestion logs
 * live in Settings → System logs. */

import { useState, useSyncExternalStore } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../../api/client";
import { errMsg } from "../../lib/format";
import { refreshInstances, useConfig } from "../../state/queries";
import { useUi } from "../../state/store";

interface MfStatus {
  available: boolean;
  running: boolean;
  net_error?: boolean;
  desired?: boolean;
  tickets_active?: boolean;
  pr_active?: boolean;
  pr_enabled?: boolean;
}

function subscribeOnline(cb: () => void) {
  window.addEventListener("online", cb);
  window.addEventListener("offline", cb);
  return () => {
    window.removeEventListener("online", cb);
    window.removeEventListener("offline", cb);
  };
}

export function AutomationBar() {
  const { data: config } = useConfig();
  const openDialogFor = useUi((s) => s.openDialogFor);
  const [busy, setBusy] = useState(false);
  const [optimistic, setOptimistic] = useState<boolean | null>(null);
  const { data: status, refetch } = useQuery({
    queryKey: ["mindflock-status"],
    queryFn: () => api<MfStatus>("/api/mindflock/status"),
    refetchInterval: 10_000,
    retry: false,
  });

  const online = useSyncExternalStore(subscribeOnline, () => navigator.onLine);

  if (config?.caps && !config.caps.ticketing) return null;
  if (!status || !status.available) return null;
  const running = !!status.running;
  // Old servers don't send `desired` — fall back to the live state.
  const desired = optimistic ?? (status.desired ?? running);
  // Green only while a ticket is actually being handled; a running-but-idle
  // pipeline "waits" (gold) for the next ticket to come in.
  const active = running && !!status.tickets_active;
  const starting = desired && !running;
  const netIssue = !online || !!status.net_error;

  const toggle = async (start: boolean) => {
    if (busy) return;
    setBusy(true);
    setOptimistic(start);
    try {
      await api<MfStatus>(`/api/mindflock/${start ? "start" : "stop"}`, { method: "POST" });
    } catch (err) {
      alert(`MindFlock ${start ? "start" : "stop"} failed: ` + errMsg(err));
    } finally {
      setBusy(false);
      setOptimistic(null);
      refetch();
      refreshInstances();
    }
  };

  return (
    <div
      id="mindflock-bar"
      title="Run/stop ticket ingestion (polls your ticketing provider + PRs and auto-creates sessions). Stays in this state across restarts."
    >
      <span
        id="mindflock-dot"
        className={
          "dc-dot " +
          (netIssue ? "error" : !desired ? "off" : active ? "on" : "idle")
        }
        title={
          netIssue
            ? online
              ? "Connection issues in the ingestion log — see Settings → System logs"
              : "No network connection"
            : starting
              ? "Set to on but not running yet — starting, or the pipeline exited (flip the switch off and on to restart it)"
              : active
                ? "A ticket is being handled right now"
                : desired
                  ? "Waiting for an assigned ticket — turns green while one is being handled"
                  : undefined
        }
      />
      <span className="dc-label">Ticket Ingestion</span>
      <span className="dc-actions">
        <button
          id="mindflock-tickets-btn"
          className="dc-toggle"
          title="Ticketing sources and ingestion options (Settings → Ticketing)"
          onClick={() => openDialogFor("settings", "ticketing")}
        >
          Tickets
        </button>
        <label className="dc-switch" title="Flip to run/stop ticket ingestion">
          <input
            type="checkbox"
            id="mindflock-toggle"
            checked={desired}
            disabled={busy}
            onChange={(e) => toggle(e.target.checked)}
          />
          <span className="dc-slider" />
        </label>
      </span>
    </div>
  );
}
