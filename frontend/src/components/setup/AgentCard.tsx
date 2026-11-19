/** ONE agent card, the same everywhere an agent's state is shown: the Setup
 * checklist's step ②, Settings → Agents (the default and "another agent"),
 * and Settings → Custom agents. It IS the Connections row
 * (`ConnectionRowView`, read from `GET /api/connections`), so all four say the
 * same status and offer the same single next action — Install → Sign in →
 * Test — instead of pointing at each other.
 *
 * Cards mounted together share one request (`loadAgentRows`), and every card
 * re-reads when something announces a change (`mf:connections-changed`: a
 * sign-in, an install, a Test).
 *
 * When `/api/connections` can't be read the card says so, with Retry, and
 * still offers the agent's sign-in — never an empty space where step ②'s
 * button should be. */

import { useCallback, useEffect, useState } from "react";
import { api } from "../../api/client";
import type { ConnectionRow, ConnectionsResponse } from "../../api/types";
import { useUi } from "../../state/store";
import { openSettings, type SettingsScreen } from "../../lib/openSettings";
import { agentName } from "../../lib/agentNames";
import { AgentSignIn } from "../dialogs/AgentSignIn";
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
 * asks within `SHARE_MS`. `refresh` re-probes (after a sign-in or install).
 * Rejects when the read fails, so a card can tell "couldn't check" from "no
 * such agent"; a failed read is not kept for the next ask. */
export function loadAgentRows(refresh = false): Promise<ConnectionRow[]> {
  const now = Date.now();
  if (!refresh && shared && now - shared.at < SHARE_MS) return shared.rows;
  const rows = api<ConnectionsResponse>("/api/connections" + (refresh ? "?refresh=1" : "")).then((d) =>
    (d?.connections || []).filter((c) => c.kind === "agent")
  );
  const mine = { at: now, rows };
  shared = mine;
  rows.catch(() => {
    if (shared === mine) shared = null;
  });
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
  const [failed, setFailed] = useState(false);
  const [result, setResult] = useState<TestOutcome | undefined>();
  const [testing, setTesting] = useState(false);
  const openDialogFor = useUi((st) => st.openDialogFor);
  const closeDialog = useUi((st) => st.closeDialog);

  const load = useCallback(
    async (refresh = false) => {
      try {
        const rows = await loadAgentRows(refresh);
        setFailed(false);
        setRow(agentRowFor(rows, provider));
      } catch {
        setFailed(true);
        setRow(null);
      }
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

  if (failed)
    return (
      <AgentCardError
        provider={provider}
        tourNext={tourNext}
        onRetry={() => void load(true)}
        onSignedIn={() => {
          void load(true);
          onChanged?.();
        }}
      />
    );
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

/** The card when `/api/connections` couldn't be read: say so, offer Retry,
 * and still offer the sign-in for this agent (`provider` "" = the default,
 * which can't be named without the read — Retry only). */
export function AgentCardError({
  provider,
  tourNext = false,
  onRetry,
  onSignedIn,
}: {
  provider: string;
  tourNext?: boolean;
  onRetry(): void;
  onSignedIn(): void;
}) {
  return (
    <div className="agent-card agent-card-error" data-provider={provider}>
      <span className="set-hint agent-card-failed">
        Couldn't check {agentName(provider || "claude")}.{" "}
        <button
          type="button"
          className="linklike agent-card-retry"
          onClick={(e) => {
            e.stopPropagation();
            onRetry();
          }}
        >
          Retry
        </button>
      </span>
      {provider ? (
        <AgentSignIn
          provider={provider}
          onDone={onSignedIn}
          dataTour={tourNext ? "setup-next" : undefined}
        />
      ) : null}
    </div>
  );
}
