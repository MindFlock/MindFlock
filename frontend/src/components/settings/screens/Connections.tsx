/** Settings → Connections: THE dashboard of every outside thing MindFlock
 * talks to — agent CLIs, GitHub, ticket trackers, your devices / phone /
 * people, the MCP, databases, ntfy, extensions. One row each, three states
 * (connected / needs attention / off), one action and one Test per row, and
 * the fix command with Copy when there is one.
 *
 * The server builds the rows (backend/web/addons/connections.py); this
 * renders what it gets, grouped by `kind`. It refreshes every 30 s while open
 * and whenever something dispatches `mf:connections-changed`. */

import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../../../api/client";
import type {
  ConnectionAction,
  ConnectionRow,
  ConnectionStatus,
  ConnectionsResponse,
  ConnectionTestSpec,
} from "../../../api/types";
import { useUi } from "../../../state/store";
import type { ScreenProps } from "../SettingsDialog";
import { openConnectGitHub } from "../../../lib/githubConnect";
import { copyText } from "../../../lib/clipboard";
import { toast } from "../../../lib/toast";
import { runCommand } from "../../../extensions/host";
import { AgentSignIn } from "../../dialogs/AgentSignIn";
import { AdvancedFold, useAdvancedSettings } from "../settingsMode";
import "./connections.css";

/** Dispatch after anything that changes a connection (a sign-in, a saved
 * token, a test elsewhere) so an open Connections screen re-reads at once. */
export const CONNECTIONS_CHANGED_EVENT = "mf:connections-changed";

export function notifyConnectionsChanged(): void {
  if (typeof window === "undefined") return;
  window.dispatchEvent(new CustomEvent(CONNECTIONS_CHANGED_EVENT));
}

/** While the screen is open it re-reads this often. */
export const REFRESH_MS = 30_000;

export const PILL_TEXT: Record<ConnectionStatus, string> = {
  connected: "Connected",
  needs_attention: "Needs attention",
  off: "Off",
};

/** The three-state status of a row — tolerating an older server whose rows
 * only carry the legacy vocabulary (connected / attention / not_connected). */
export function rowStatus(c: { status?: string; legacy_status?: string }): ConnectionStatus {
  const s = c.status || "";
  if (s === "connected" || s === "needs_attention" || s === "off") return s;
  if (s === "attention") return "needs_attention";
  return "off";
}

export function pillText(c: { status?: string }): string {
  return PILL_TEXT[rowStatus(c)];
}

/** Group headings, in the order the server sorts kinds. */
export const KIND_LABELS: Record<string, string> = {
  agent: "Agent CLIs",
  github: "GitHub",
  tracker: "Ticket trackers",
  devices: "Devices and people",
  mcp: "Agent orchestration",
  database: "Databases",
  notifications: "Notifications",
  extension: "Extensions",
  system: "System",
};

/** The kinds a new user sets up: the agents, GitHub and the tracker. The
 * rest (devices and people, orchestration, databases, phone push,
 * extensions, system) sit in the screen's Advanced fold. */
export const BASIC_KINDS: ReadonlySet<string> = new Set(["agent", "github", "tracker"]);

export function isBasicKind(kind: string | undefined): boolean {
  return BASIC_KINDS.has(kind || "other");
}

/** Rows grouped by kind, groups in first-seen order (the server's order). */
export function groupRows(rows: ConnectionRow[]): Array<{ kind: string; label: string; rows: ConnectionRow[] }> {
  const out: Array<{ kind: string; label: string; rows: ConnectionRow[] }> = [];
  const at = new Map<string, number>();
  for (const r of rows) {
    const kind = r.kind || "other";
    let i = at.get(kind);
    if (i === undefined) {
      i = out.length;
      at.set(kind, i);
      out.push({ kind, label: KIND_LABELS[kind] || kind.charAt(0).toUpperCase() + kind.slice(1), rows: [] });
    }
    out[i].rows.push(r);
  }
  return out;
}

/** "N connected · M need attention · K off" over the rows shown. */
export function summaryLine(rows: Array<{ status?: string }>): string {
  if (!rows.length) return "";
  const n = { connected: 0, needs_attention: 0, off: 0 };
  for (const r of rows) n[rowStatus(r)] += 1;
  const need = n.needs_attention === 1 ? "needs" : "need";
  return `${n.connected} connected · ${n.needs_attention} ${need} attention · ${n.off} off`;
}

export interface TestOutcome {
  ok: boolean;
  text: string;
}

/** One line out of whatever a row's Test endpoint answered: the agent probe
 * ({ok, cli, auth}), GitHub ({ok, detail}), a ticket source ({ok, message} /
 * {ok, error}), a database ({ok, server} / {ok, error}), ntfy ({ok, error}). */
export function describeTestResult(res: unknown): TestOutcome {
  const r = (res && typeof res === "object" ? res : {}) as Record<string, unknown>;
  const str = (v: unknown) => (typeof v === "string" ? v.trim() : "");
  const sub = (k: string) => {
    const v = r[k];
    return v && typeof v === "object" ? str((v as Record<string, unknown>).detail) : "";
  };
  const ok = r.ok === true;
  const text =
    str(r.detail) ||
    str(r.message) ||
    str(r.error) ||
    str(r.server) ||
    (ok ? sub("auth") || sub("cli") : sub("cli") || sub("auth"));
  return { ok, text: text || (ok ? "Works" : "Failed") };
}

/** Run a row's Test. Never throws: a refused or failed call is a failed test. */
export async function runTest(t: ConnectionTestSpec): Promise<TestOutcome> {
  try {
    const res = await api<unknown>(t.endpoint, { method: t.method || "POST", json: t.body });
    return describeTestResult(res);
  } catch (err) {
    return { ok: false, text: err instanceof Error ? err.message : String(err) };
  }
}

/** Legacy rows (an older server): Configure opened a dialog when the server
 * named one, else the owning settings screen. */
export function configureAction(
  c: { configure_dialog?: string; settings_screen?: string },
  gotoScreen: (name: string) => void
): (() => void) | null {
  if (c.configure_dialog === "connect-github") return openConnectGitHub;
  if (c.settings_screen) return () => gotoScreen(c.settings_screen!);
  return null;
}

interface ActionDeps {
  gotoScreen(name: string): void;
  openDialogFor(name: "intake", target?: string | null): void;
  closeDialog(): void;
}

/** What a row's action button does (agent-sign-in renders its own button;
 * copy actions are the fix line's Copy). */
export function actionHandler(a: ConnectionAction, deps: ActionDeps): (() => void) | null {
  switch (a.kind) {
    case "screen":
      return () => deps.gotoScreen(a.target);
    case "copy":
      return () => void copyCommand(a.target);
    case "route":
      return () => window.open(a.target, "_blank", "noopener");
    case "dialog":
      if (a.target === "connect-github") return openConnectGitHub;
      if (a.target === "intake") return () => deps.openDialogFor("intake", a.tab || "tickets");
      if (a.target === "db-explorer")
        return () => {
          // The explorer is its own dialog: Settings steps aside for it.
          deps.closeDialog();
          void runCommand("dbclient", "dbclient.explorer");
        };
      return null;
  }
  return null;
}

async function copyCommand(cmd: string): Promise<void> {
  const ok = await copyText(cmd);
  toast(ok ? "Copied" : "Could not copy — select the command and copy it by hand");
}

/** One row. `onChanged` re-reads the list (after a Test or a sign-in). */
export function ConnectionRowView({
  c,
  deps,
  result,
  testing,
  onTest,
  onChanged,
}: {
  c: ConnectionRow;
  deps: ActionDeps;
  result?: TestOutcome;
  testing?: boolean;
  onTest?(): void;
  onChanged(): void;
}) {
  const status = rowStatus(c);
  const a = c.action;
  const fixCmd = status !== "connected" ? c.fix_command || "" : "";
  // A copy action IS the fix line's Copy: one button, not two.
  const copyIsFix = a?.kind === "copy" && (!fixCmd || a.target === fixCmd);
  const handler = a && !copyIsFix && a.target !== "agent-sign-in" ? actionHandler(a, deps) : null;
  const legacy = !a ? configureAction(c, deps.gotoScreen) : null;
  return (
    <div className="conn-row" data-conn={c.id} data-status={status}>
      <div className="conn-main">
        <div className="conn-head">
          <span className="conn-name">{c.name}</span>
          {c.default && <span className="conn-badge">default</span>}
        </div>
        <div className="conn-detail muted">{c.detail || c.purpose || ""}</div>
        {fixCmd || (copyIsFix && a) ? (
          <div className="conn-fix">
            <code>{fixCmd || a!.target}</code>
            <button type="button" className="test-btn conn-copy" onClick={() => void copyCommand(fixCmd || a!.target)}>
              Copy
            </button>
          </div>
        ) : status !== "connected" && c.fix ? (
          <div className="conn-fix muted">{c.fix}</div>
        ) : null}
        {result && (
          <div className={"conn-test-result " + (result.ok ? "ok" : "bad")} role="status">
            {result.ok ? "✓ " : "✗ "}
            {result.text}
          </div>
        )}
      </div>
      <span className={"conn-pill conn-" + status}>{PILL_TEXT[status]}</span>
      <div className="conn-actions">
        {c.test && onTest && (
          <button type="button" className="test-btn conn-test" disabled={testing} onClick={onTest}>
            {testing ? "Testing…" : "Test"}
          </button>
        )}
        {a?.kind === "dialog" && a.target === "agent-sign-in" && a.provider ? (
          <AgentSignIn provider={a.provider} profile={a.profile} label={a.label || "Sign in"} onDone={onChanged} />
        ) : null}
        {handler && (
          <button type="button" className="test-btn conn-configure" onClick={handler}>
            {a!.label}
          </button>
        )}
        {legacy && (
          <button type="button" className="test-btn conn-configure" onClick={legacy}>
            Configure
          </button>
        )}
        {c.docs && status !== "connected" && (
          <a className="conn-docs" href={c.docs} target="_blank" rel="noopener noreferrer">
            Docs
          </a>
        )}
      </div>
    </div>
  );
}

export function Connections({ gotoScreen }: ScreenProps) {
  const [conns, setConns] = useState<ConnectionRow[] | null>(null);
  const [error, setError] = useState("");
  const [results, setResults] = useState<Record<string, TestOutcome>>({});
  const [testing, setTesting] = useState<Record<string, boolean>>({});
  const openDialogFor = useUi((s) => s.openDialogFor);
  const closeDialog = useUi((s) => s.closeDialog);
  const alive = useRef(true);

  const load = useCallback(async (refresh: boolean) => {
    try {
      const data = await api<ConnectionsResponse>("/api/connections" + (refresh ? "?refresh=1" : ""));
      if (!alive.current) return;
      setConns(data?.connections || []);
      setError("");
    } catch {
      if (alive.current) setError("Could not load connections.");
    }
  }, []);

  useEffect(() => {
    alive.current = true;
    void load(true);
    const timer = window.setInterval(() => void load(false), REFRESH_MS);
    const onChanged = () => void load(true);
    window.addEventListener(CONNECTIONS_CHANGED_EVENT, onChanged);
    return () => {
      alive.current = false;
      window.clearInterval(timer);
      window.removeEventListener(CONNECTIONS_CHANGED_EVENT, onChanged);
    };
  }, [load]);

  const test = async (c: ConnectionRow) => {
    if (!c.test) return;
    setTesting((t) => ({ ...t, [c.id]: true }));
    const out = await runTest(c.test);
    if (!alive.current) return;
    setTesting((t) => ({ ...t, [c.id]: false }));
    setResults((r) => ({ ...r, [c.id]: out }));
    // The server records most verdicts (GitHub, sources, databases): re-read.
    void load(true);
  };

  const deps: ActionDeps = { gotoScreen, openDialogFor, closeDialog };
  const groups = conns ? groupRows(conns) : [];
  const basic = groups.filter((g) => isBasicKind(g.kind));
  const more = groups.filter((g) => !isBasicKind(g.kind));
  // The count covers what the screen shows unfolded: in basic mode that is
  // the basic rows, so a device row nobody asked about never reads as
  // "needs attention" up here.
  const advanced = useAdvancedSettings();
  const counted = conns ? (advanced ? conns : conns.filter((c) => isBasicKind(c.kind))) : null;
  const renderGroup = (g: (typeof groups)[number]) => (
    <section className="conn-group" key={g.kind} data-kind={g.kind}>
      <h4 className="conn-group-title">{g.label}</h4>
      {g.rows.map((c) => (
        <ConnectionRowView
          key={c.id || c.name}
          c={c}
          deps={deps}
          result={results[c.id]}
          testing={testing[c.id]}
          onTest={() => void test(c)}
          onChanged={() => void load(true)}
        />
      ))}
    </section>
  );

  return (
    <>
      <h3 className="set-section-title">
        Connections{" "}
        <span id="settings-connections-summary" className="muted">
          {counted ? summaryLine(counted) : ""}
        </span>
      </h3>
      <p className="set-hint">
        Everything MindFlock talks to, one row each. Connect sets one up; Test checks it now.
      </p>
      <div id="settings-connections-list" className="conn-list">
        {error ? (
          <p className="error">{error}</p>
        ) : conns === null ? (
          <p className="set-hint">Checking…</p>
        ) : (
          <>
            {basic.map(renderGroup)}
            {more.length ? (
              <AdvancedFold id="connections-advanced">{more.map(renderGroup)}</AdvancedFold>
            ) : null}
          </>
        )}
      </div>
    </>
  );
}
