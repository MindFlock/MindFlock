/** Settings → Connections: THE dashboard of every outside thing MindFlock
 * talks to — agents, GitHub, ticket trackers, your devices / phone /
 * people, the MCP, databases, ntfy, extensions. One row each, three states
 * (connected / needs attention / off), one action and one Test per row, and
 * the fix command with Copy when there is one.
 *
 * It leads with what the person USES — their default agent, GitHub, their
 * ticket tracker ("What you use", which is also what the header counts, in
 * both modes). Every other agent CLI found on the machine sits folded under
 * "Other agents" with neutral pills: an agent you never set up is "Not set
 * up", not a warning. The rest (devices, orchestration, databases, phone
 * push, extensions, system) is the Advanced fold, as before.
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
import { InstallMissing } from "../../dialogs/InstallTerminal";
import { agentName } from "../../../lib/agentNames";
import { reprobeDoctor, useDoctor } from "../../../state/queries";
import { refreshSetup } from "../../setup/useSetup";
import { AdvancedFold } from "../settingsMode";
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
  agent: "Agents",
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

/** The fields the server sends on some rows that the shared type leaves out. */
type RowExtras = { provider?: string; account?: string; pr_review_enabled?: boolean };
const extras = (c: ConnectionRow): RowExtras => c as ConnectionRow & RowExtras;

/** The row's title: "Agent · Claude Code" for an agent (the server says
 * "Coding agent · Claude"), "Ticket tracker" for the not-configured one. */
export function rowName(c: ConnectionRow): string {
  const provider = extras(c).provider;
  if (c.kind === "agent" && provider) return "Agent · " + agentName(provider);
  if (c.kind === "agent" && c.name.startsWith("Coding agent · "))
    return "Agent · " + c.name.slice("Coding agent · ".length);
  if (c.kind === "tracker" && c.id === "ticketing") return "Ticket tracker";
  return c.name;
}

/** The default agent, GitHub and the ticket tracker(s): what a person set
 * up, and what the header counts. */
export function isCoreRow(c: ConnectionRow): boolean {
  return (c.kind === "agent" && !!c.default) || c.kind === "github" || c.kind === "tracker";
}

/** An agent CLI on the machine that is not the default — shown folded and
 * neutral. (A custom-agent file that failed to load has no provider: it is a
 * real error and keeps its warning.) */
export function isOtherAgent(c: ConnectionRow): boolean {
  return c.kind === "agent" && !c.default && !!extras(c).provider;
}

/** What you use / other agents / everything else, each in the server's order. */
export function splitRows(rows: ConnectionRow[]): {
  core: ConnectionRow[];
  otherAgents: ConnectionRow[];
  rest: ConnectionRow[];
} {
  const core = rows.filter(isCoreRow);
  const otherAgents = rows.filter((c) => !isCoreRow(c) && isOtherAgent(c));
  const rest = rows.filter((c) => !isCoreRow(c) && !isOtherAgent(c));
  return { core, otherAgents, rest };
}

/** Whether an installed agent's sign-in is simply unknowable from here. */
function signInUnknown(c: ConnectionRow): boolean {
  return /can't be checked/i.test(c.detail || "");
}

export interface ShownStatus {
  status: ConnectionStatus;
  pill: string;
  /** The pill's colour: a state, or "neutral" for "not your concern". */
  tone: ConnectionStatus | "neutral";
}

/** The pill a row shows. A fresh failing Test outranks an agent row's
 * stored status, so Connections never says "Connected" next to "✗". */
export function shownStatus(c: ConnectionRow, result?: TestOutcome): ShownStatus {
  let status = rowStatus(c);
  if (c.kind === "agent" && result) status = result.ok ? "connected" : status === "off" ? "off" : "needs_attention";
  if (isOtherAgent(c)) {
    if (status === "off") return { status, pill: "Not installed", tone: "neutral" };
    if (status === "needs_attention") return { status, pill: "Not set up", tone: "neutral" };
    if (signInUnknown(c) && !result) return { status, pill: "Installed", tone: "neutral" };
  } else if (c.kind === "agent" && status === "connected" && signInUnknown(c) && !result) {
    return { status, pill: "Installed", tone: "neutral" };
  } else if (c.kind === "extension" && status === "connected") {
    // An extension isn't connected to anything: it loaded.
    return { status, pill: "Loaded", tone: status };
  }
  return { status, pill: PILL_TEXT[status], tone: status };
}

/** One plain line for a row: an agent's sign-in state without file paths,
 * GitHub's state without raw `gh` output. Everything else is the server's
 * own sentence. */
export function plainDetail(c: ConnectionRow): string {
  const d = c.detail || "";
  if (c.kind === "agent" && extras(c).provider) {
    if (/^signed in/i.test(d)) return extras(c).account ? "Signed in as " + extras(c).account : "Signed in";
    if (/^no sign-in needed/i.test(d)) return "No sign-in needed";
    if (/can't be checked/i.test(d)) return "Installed. MindFlock can't check its sign-in from here.";
    if (/not signed in/i.test(d)) return "Installed, not signed in";
    if (/^not installed/i.test(d)) return "Not installed";
    return d;
  }
  if (c.kind === "github") {
    const status = rowStatus(c);
    const at = d.indexOf("PR review");
    const pr = at >= 0 ? d.slice(at) : "";
    const head = (at >= 0 ? d.slice(0, at) : d).replace(/\s*·\s*$/, "").trim();
    if (status === "off") return "Not connected. Agents can't open pull requests yet.";
    if (status === "needs_attention") return "Connected, but the last check failed" + (head ? ": " + head : ".");
    const who = extras(c).account ? " as @" + extras(c).account : "";
    return "Connected" + who + (pr && !/no repos/i.test(pr) ? " · " + pr : "");
  }
  if (c.kind === "tracker" && c.id === "ticketing")
    return "Not set up. Bring in tickets from Shortcut, Jira, Linear, Asana or GitHub Issues.";
  return d || c.purpose || "";
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
export function describeTestResult(res: unknown, ms?: number): TestOutcome {
  const r = (res && typeof res === "object" ? res : {}) as Record<string, unknown>;
  const agent = describeAgentTest(r, ms);
  if (agent) return agent;
  const str = (v: unknown) => (typeof v === "string" ? v.trim() : "");
  const sub = (k: string) => {
    const v = r[k];
    return v && typeof v === "object" ? str((v as Record<string, unknown>).detail) : "";
  };
  const ok = r.ok === true;
  // The agent probe ({cli, auth}): on a failure, the check that failed —
  // a missing login is the auth line, not the CLI's path.
  const cliOk = !!r.cli && typeof r.cli === "object" && (r.cli as Record<string, unknown>).status === "ok";
  const text =
    str(r.detail) ||
    str(r.message) ||
    str(r.error) ||
    str(r.server) ||
    (ok ? sub("auth") || sub("cli") : cliOk ? sub("auth") || sub("cli") : sub("cli") || sub("auth"));
  return { ok, text: text || (ok ? "Works" : "Failed") };
}

/** "answered in 0.4 s" — how long a Test took, rounded for reading. */
export function answeredIn(ms: number): string {
  const s = Math.max(0, ms) / 1000;
  return "answered in " + (s < 10 ? s.toFixed(1) : String(Math.round(s))) + " s";
}

/** An agent card's Test, in words: where the agent was found, how it is
 * signed in, and how long it took — "Found at /usr/bin/claude · signed in
 * as you@example.com · answered in 0.4 s" — instead of "✓ Checked just now",
 * which said when it checked but not what. `null` for any other test. */
export function describeAgentTest(r: Record<string, unknown>, ms?: number): TestOutcome | null {
  const cli = r.cli && typeof r.cli === "object" ? (r.cli as Record<string, unknown>) : null;
  if (!cli) return null;
  const str = (v: unknown) => (typeof v === "string" ? v.trim() : "");
  const ok = r.ok === true;
  const bits: string[] = [];
  if (cli.status === "ok") {
    // The doctor's detail is "<path> — <version>".
    const path = str(cli.detail).split(" — ")[0];
    if (path) bits.push("found at " + path);
  }
  const line = str(r.detail);
  if (line) bits.push(bits.length ? line.charAt(0).toLowerCase() + line.slice(1) : line);
  if (typeof ms === "number" && ok) bits.push(answeredIn(ms));
  const text = bits.join(" · ");
  if (!text) return null;
  return { ok, text: text.charAt(0).toUpperCase() + text.slice(1) };
}

/** Run a row's Test. Never throws: a refused or failed call is a failed test. */
export async function runTest(t: ConnectionTestSpec): Promise<TestOutcome> {
  const t0 = Date.now();
  try {
    const res = await api<unknown>(t.endpoint, { method: t.method || "POST", json: t.body });
    return describeTestResult(res, Date.now() - t0);
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
      // A source's own row lands on its heading with its editor open
      // (`tickets:<source id>`), not on the top of the tab.
      if (a.target === "intake")
        return () =>
          deps.openDialogFor("intake", (a.tab || "tickets") + (a.source_id ? ":" + a.source_id : ""));
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

/** An agent card's ONE next action, in the order a new user meets them:
 * Install (not on this computer) → Sign in (installed, no login found) →
 * Test (everything else). The card shows that button and no other, so
 * Connections, Settings → Agents, Custom agents and the Setup checklist all
 * offer the same single thing to press. */
export type AgentStage = "install" | "signin" | "test";

export function agentStage(c: ConnectionRow): AgentStage | null {
  if (c.kind !== "agent" || !extras(c).provider) return null;
  if (/^not installed/i.test(c.detail || "")) return "install";
  if (c.action?.kind === "dialog" && c.action.target === "agent-sign-in") return "signin";
  return "test";
}

/** The default agent's Install: the doctor's one-click install (the Setup
 * checklist's own button — it installs whatever else is missing too), or the
 * copyable command when the plan doesn't carry this agent. */
function AgentInstall({ provider, fallback }: { provider: string; fallback: string }) {
  const { data: doctor } = useDoctor();
  const steps = doctor?.install?.steps || [];
  const done = () => {
    void reprobeDoctor().catch(() => undefined);
    void refreshSetup(true);
    notifyConnectionsChanged();
  };
  if (steps.some((st) => st.id === "agent-cli"))
    return (
      <div className="conn-install" data-provider={provider}>
        <InstallMissing steps={steps} onDone={done} />
      </div>
    );
  if (!fallback) return null;
  return (
    <div className="conn-fix">
      <code>{fallback}</code>
      <button type="button" className="test-btn conn-copy" onClick={() => void copyCommand(fallback)}>
        Copy
      </button>
    </div>
  );
}

/** One row. `onChanged` re-reads the list (after a Test or a sign-in). */
export function ConnectionRowView({
  c,
  deps,
  result,
  testing,
  onTest,
  onChanged,
  tourNext = false,
}: {
  c: ConnectionRow;
  deps: ActionDeps;
  result?: TestOutcome;
  testing?: boolean;
  onTest?(): void;
  onChanged(): void;
  /** The row's action is the setup checklist's next step (its tour hook). */
  tourNext?: boolean;
}) {
  const shown = shownStatus(c, result);
  const status = shown.status;
  const a = c.action;
  const stage = agentStage(c);
  const install = a?.kind === "dialog" && a.target === "agent-install" && a.provider ? a.provider : "";
  // GitHub's fix is its Connect button: no `gh auth login` chip beside it.
  // An agent's sign-in is its Sign in button: no login command beside it.
  const fixCmd =
    status !== "connected" && c.kind !== "github" && stage !== "signin" && !install ? c.fix_command || "" : "";
  // A copy action IS the fix line's Copy: one button, not two.
  const copyIsFix = a?.kind === "copy" && (!fixCmd || a.target === fixCmd);
  const handler =
    a && !copyIsFix && a.target !== "agent-sign-in" && a.target !== "agent-install" ? actionHandler(a, deps) : null;
  // Only a row from an older server has no `action` field at all; `null` is
  // a server saying "nothing to do here".
  const legacy = a === undefined ? configureAction(c, deps.gotoScreen) : null;
  // Test is the agent card's action only once there is nothing to install or
  // sign in to; GitHub with no credential has nothing to test yet.
  const showTest =
    !!c.test && !!onTest && !(c.kind === "github" && status === "off") && (stage === null || stage === "test");
  // The Settings tour's GitHub step (lib/tours/settings.ts) rings Connect;
  // with no Connect showing (connected, or Connect is the Get set up
  // checklist's next step) it rings the whole card instead.
  const githubOnButton = c.kind === "github" && !!handler && !tourNext;
  return (
    <div
      className="conn-row"
      data-conn={c.id}
      data-status={status}
      data-tour={c.kind === "github" && !githubOnButton ? "settings-github" : undefined}
    >
      <div className="conn-main">
        <div className="conn-head">
          <span className="conn-name">{rowName(c)}</span>
          {c.default && <span className="conn-badge">default</span>}
        </div>
        {/* The server's full sentence (paths, gh output) stays on hover. */}
        <div className="conn-detail muted" title={c.detail || undefined}>
          {plainDetail(c)}
        </div>
        {install ? (
          <AgentInstall provider={install} fallback={c.fix_command || ""} />
        ) : fixCmd || (copyIsFix && a) ? (
          <div className="conn-fix">
            <code>{fixCmd || a!.target}</code>
            <button type="button" className="test-btn conn-copy" onClick={() => void copyCommand(fixCmd || a!.target)}>
              Copy
            </button>
          </div>
        ) : status !== "connected" && c.fix && c.kind !== "github" && stage !== "signin" ? (
          <div className="conn-fix muted">{c.fix}</div>
        ) : null}
        {result && (
          <div className={"conn-test-result " + (result.ok ? "ok" : "bad")} role="status">
            {result.ok ? "✓ " : "✗ "}
            {/* A pass that says what the line above already says: just "checked". */}
            {result.ok && result.text === plainDetail(c) ? "Checked just now" : result.text}
          </div>
        )}
      </div>
      <span className={"conn-pill conn-" + shown.tone}>{shown.pill}</span>
      <div className="conn-actions">
        {showTest && (
          <button type="button" className="test-btn conn-test" disabled={testing} onClick={onTest}>
            {testing ? "Testing…" : "Test"}
          </button>
        )}
        {a?.kind === "dialog" && a.target === "agent-sign-in" && a.provider ? (
          <AgentSignIn
            provider={a.provider}
            profile={a.profile}
            label={a.label || "Sign in"}
            onDone={onChanged}
            dataTour={tourNext ? "setup-next" : undefined}
          />
        ) : null}
        {handler && (
          <button
            type="button"
            className={stage && a!.kind === "screen" ? "linklike conn-configure conn-link" : "test-btn conn-configure"}
            data-tour={tourNext ? "setup-next" : githubOnButton ? "settings-github" : undefined}
            onClick={handler}
          >
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
  const { core, otherAgents, rest } = conns ? splitRows(conns) : { core: [], otherAgents: [], rest: [] };
  const more = groupRows(rest);
  // The count is "what you use" in BOTH modes, so it means one thing: an
  // agent CLI or a device row nobody asked about never reads as "needs
  // attention" up here, and advanced mode shows the same numbers.
  const counted = conns ? core : null;
  const renderRow = (c: ConnectionRow) => (
    <ConnectionRowView
      key={c.id || c.name}
      c={c}
      deps={deps}
      result={results[c.id]}
      testing={testing[c.id]}
      onTest={() => void test(c)}
      onChanged={() => void load(true)}
    />
  );
  const renderGroup = (g: (typeof more)[number]) => (
    <section className="conn-group" key={g.kind} data-kind={g.kind}>
      <h4 className="conn-group-title">{g.label}</h4>
      {g.rows.map(renderRow)}
    </section>
  );

  return (
    <>
      <h3 className="set-section-title" data-tour="settings-status">
        Connections{" "}
        <span id="settings-connections-summary" className="muted">
          {counted ? summaryLine(counted) : ""}
        </span>
      </h3>
      <p className="set-hint">What MindFlock talks to. Connect sets one up; Test checks it now.</p>
      <div id="settings-connections-list" className="conn-list">
        {error ? (
          <p className="error">{error}</p>
        ) : conns === null ? (
          <p className="set-hint">Checking…</p>
        ) : (
          <>
            {core.length ? (
              <section className="conn-group" data-kind="core" id="connections-core">
                <h4 className="conn-group-title">What you use</h4>
                {core.map(renderRow)}
              </section>
            ) : null}
            {otherAgents.length ? (
              // A plain fold, not AdvancedFold: it stays visible (closed) in
              // basic mode — "can I use Codex too?" is a basic question.
              <details className="pr-advanced set-adv-fold" id="connections-other-agents">
                <summary>{`Other agents on this computer (${otherAgents.length})`}</summary>
                <div className="pr-advanced-body set-adv-fold-body">
                  <p className="set-hint">Only if you want to try another agent. Nothing here needs fixing.</p>
                  {otherAgents.map(renderRow)}
                </div>
              </details>
            ) : null}
            {more.length ? (
              // Folded even in advanced mode: the rows above are what you
              // use; these are there when you go looking.
              <AdvancedFold id="connections-advanced" summary="More connections" defaultOpen={false}>
                {more.map(renderGroup)}
              </AdvancedFold>
            ) : null}
          </>
        )}
      </div>
    </>
  );
}
