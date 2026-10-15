/** ONE agent card, the same everywhere an agent's state is shown: the Setup
 * checklist's step ②, Settings → Agents (the default and "another agent"),
 * and Settings → Custom agents. It IS the Connections row
 * (`ConnectionRowView`, read from `GET /api/connections`), so all four say the
 * same status and offer the same single next action — Install → Sign in →
 * Test — instead of pointing at each other.
 *
 * Cards mounted together share one request (`loadAgentRows`), and every card
 * re-reads when something announces a change (`mf:connections-changed`: a
 * sign-in, an install, a Test). */

import { useCallback, useEffect, useState } from "react";
import { api } from "../../api/client";
import type { ConnectionRow, ConnectionsResponse } from "../../api/types";
import { useUi } from "../../state/store";
import { openSettings, type SettingsScreen } from "../../lib/openSettings";
import { agentName } from "../../lib/agentNames";
import {
  CONNECTIONS_CHANGED_EVENT,
  ConnectionRowView,
  runTest,
  type TestOutcome,
} from "../settings/screens/Connections";

/** Requests started within this window share one answer. */
export const SHARE_MS = 3000;

let shared: { at: number; rows: Promise<ConnectionRow[]> } | null = null;

/** The agent rows of `/api/connections`, one request for every card that
 * asks within `SHARE_MS`. `refresh` re-probes (after a sign-in or install). */
export function loadAgentRows(refresh = false): Promise<ConnectionRow[]> {
  const now = Date.now();
  if (!refresh && shared && now - shared.at < SHARE_MS) return shared.rows;
  const rows = api<ConnectionsResponse>("/api/connections" + (refresh ? "?refresh=1" : ""))
    .then((d) => (d?.connections || []).filter((c) => c.kind === "agent"))
    .catch(() => [] as ConnectionRow[]);
  shared = { at: now, rows };
  return rows;
}

/** The row for `provider` ("" = the default agent). */
export function agentRowFor(rows: ConnectionRow[], provider: string): ConnectionRow | null {
  const want = (provider || "").toLowerCase();
  const prov = (c: ConnectionRow) => (c as ConnectionRow & { provider?: string }).provider;
  return (want && rows.find((c) => prov(c) === want)) || (!want ? rows.find((c) => c.default) : null) || null;
}

export function AgentCard({
  provider,
  here,
  gotoScreen,
  onChanged,
  tourNext = false,
}: {
  /** The agent ("" = the default). */
  provider: string;
  /** The Settings screen this card sits on: an action leading back here is
   * dropped rather than offered as a no-op. */
  here?: string;
  /** Opens a Settings screen; outside Settings it opens Settings on it. */
  gotoScreen?: (name: string) => void;
  /** After a sign-in, install or Test. */
  onChanged?(): void;
  /** Its action is the setup checklist's next step (`data-tour="setup-next"`). */
  tourNext?: boolean;
}) {
  const [row, setRow] = useState<ConnectionRow | null | undefined>(undefined);
  const [result, setResult] = useState<TestOutcome | undefined>();
  const [testing, setTesting] = useState(false);
  const openDialogFor = useUi((st) => st.openDialogFor);
  const closeDialog = useUi((st) => st.closeDialog);

  const load = useCallback(
    async (refresh = false) => {
      const rows = await loadAgentRows(refresh);
      setRow(agentRowFor(rows, provider));
    },
    [provider]
  );

  useEffect(() => {
    setResult(undefined);
    void load();
  }, [load]);

  useEffect(() => {
    const on = () => {
      setResult(undefined);
      void load(true);
    };
    window.addEventListener(CONNECTIONS_CHANGED_EVENT, on);
    return () => window.removeEventListener(CONNECTIONS_CHANGED_EVENT, on);
  }, [load]);

  if (row === undefined)
    return (
      <div className="agent-card agent-card-loading" aria-busy="true">
        <span className="set-hint">Checking {agentName(provider || "claude")}…</span>
      </div>
    );
  if (row === null) return null;
  const toHere = !!here && row.action?.kind === "screen" && row.action.target === here;
  const shown: ConnectionRow = toHere ? { ...row, action: null, settings_screen: "" } : row;
  const go = gotoScreen || ((name: string) => openSettings(name as SettingsScreen));
  return (
    <div className="agent-card" data-provider={(row as ConnectionRow & { provider?: string }).provider || ""}>
      <ConnectionRowView
        c={shown}
        deps={{ gotoScreen: go, openDialogFor, closeDialog }}
        result={result}
        testing={testing}
        tourNext={tourNext}
        onTest={
          row.test
            ? async () => {
                setTesting(true);
                const out = await runTest(row.test!);
                setTesting(false);
                setResult(out);
                void load(true);
                onChanged?.();
              }
            : undefined
        }
        onChanged={() => {
          void load(true);
          onChanged?.();
        }}
      />
    </div>
  );
}
