/** The sidebar's Tickets bar — Intake → Tickets' door and quick switch (port of
 * the automation toggle, section 17). Hidden entirely without a connected
 * ticketing source. Its label opens Intake → Tickets; its switch is the same
 * "Automated ingestion" switch that tab shows. The switch reflects the
 * DESIRED state of the TICKET half of the pipeline (persisted server-side,
 * restored on reboot; the PR-review bar gates the other half of the same
 * process). The dot reflects reality — gold while starting or idle-waiting
 * for a ticket, green while one is actually being handled. Ingestion logs
 * live in Settings → System logs. */

import { useState, useSyncExternalStore } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../../api/client";
import { errorPop } from "../../lib/errorPop";
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
    // Matches the sessions poll: the green window can be as short as one
    // provisioning, and a 10s poll missed those often enough that the light
    // looked like it never turned green at all.
    refetchInterval: 4_000,
    retry: false,
  });

  const online = useSyncExternalStore(subscribeOnline, () => navigator.onLine);

  if (config?.caps && !config.caps.ticketing) return null;
  if (!status || !status.available) return null;
  const running = !!status.running;
  // Old servers don't send `desired` — fall back to the live state.
  const desired = optimistic ?? (status.desired ?? running);
  // Green while a ticket is actually being brought in — by the pipeline OR by a
  // start forced from Intake → Tickets (the server folds both into
  // tickets_active). NOT gated on `running`: a forced ticket provisions with
  // the pipeline stopped, and gold would be a lie about it.
  const active = !!status.tickets_active;
  const starting = desired && !running;
  const netIssue = !online || !!status.net_error;

  const toggle = async (start: boolean) => {
    if (busy) return;
    setBusy(true);
    setOptimistic(start);
    try {
      await api<MfStatus>(`/api/mindflock/${start ? "start" : "stop"}`, { method: "POST" });
    } catch (err) {
      // A card, not alert(): the desktop app has no native dialogs, so an
      // alert() failure there said nothing at all.
      errorPop("Automated ingestion failed", errMsg(err));
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
      title="Tickets — automated ingestion turns your assigned tickets into sessions. Stays in this state across restarts."
    >
      <span
        id="mindflock-dot"
        className={
          // `active` outranks the switch: a ticket forced from Intake is
          // genuinely being brought in even with auto ingestion switched off,
          // and "off" would be a lie about the work in flight.
          // "dc-error", NOT a bare "error": `.error` is a GLOBAL class for error
          // TEXT (`.error { min-height: 16px }`), and this element is a 9px circle.
          // `.dc-dot` sets height but never min-height, so the global won
          // uncontested and rendered the red state as a 9x16 OVAL — the one state
          // that looked malformed, because it was the only one borrowing that name.
          "dc-dot " +
          (netIssue ? "dc-error" : active ? "on" : !desired ? "off" : "idle")
        }
        title={
          active
            ? "A ticket is being brought in right now (auto ingestion or a forced start)"
            : netIssue
              ? online
                ? "Connection issues in the ingestion log — see Settings → System logs"
                : "No network connection"
              : starting
                ? "Set to on but not running yet — starting, or the pipeline exited (flip the switch off and on to restart it)"
                : desired
                  ? "Waiting for an assigned ticket — turns green while one is being brought in"
                  : undefined
        }
      />
      {/* The label IS the door: one name, one button, Intake → Tickets. */}
      <button
        id="mindflock-tickets-btn"
        type="button"
        className="dc-label dc-open"
        title="Open Intake → Tickets"
        onClick={() => openDialogFor("intake", "tickets")}
      >
        Tickets
      </button>
      <span className="dc-actions">
        <label className="dc-switch" title="Automated ingestion — the same switch as Intake → Tickets">
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
