/** Settings → Agents (key "coding"; partial 106 + sections 21/22's pickers):
 * the default agent and its sign-in state (the SAME row Connections shows,
 * so the two screens can never disagree), per-agent default launch flags (a
 * name -> flags map re-sent whole on every edit), how many sessions run at
 * once, and — folded — a check for any other agent plus the window-refresh
 * keepalive. */

import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../../../api/client";
import type { ConnectionRow, ConnectionsResponse } from "../../../api/types";
import { useUi } from "../../../state/store";
import { toast } from "../../../lib/toast";
import { pickerOptions } from "../../../lib/providerRows";
import { FlagChips } from "../../dialogs/FlagChips";
import { SettingField, useSettings } from "../useSettings";
import type { ScreenProps } from "../SettingsDialog";
import { AdvancedFold } from "../settingsMode";
import {
  CONNECTIONS_CHANGED_EVENT,
  ConnectionRowView,
  runTest,
  type TestOutcome,
} from "./Connections";

/** The flags field's example, per agent: a harmless flag that agent really
 * takes — never a permission-skipping one, which as grey placeholder text
 * read as if it were already set. */
export const FLAG_EXAMPLES: Record<string, string> = {
  claude: "e.g. --model opus",
  codex: "e.g. --search",
  antigravity: "e.g. --sandbox",
  aider: "e.g. --no-auto-commits",
};

export function flagsPlaceholder(provider: string): string {
  return FLAG_EXAMPLES[(provider || "").trim().toLowerCase()] || "";
}

interface Provider {
  name: string;
  aliases?: string[];
  command?: string;
  installed: boolean;
  path?: string;
}

export function CodingCli({ gotoScreen }: ScreenProps) {
  const s = useSettings();
  const [providers, setProviders] = useState<Provider[]>([]);
  const [selected, setSelected] = useState("");
  const [flags, setFlags] = useState("");
  const flagsMap = useRef<Record<string, string>>({});
  const [agentTest, setAgentTest] = useState<{ testing: boolean; ok?: boolean; msg?: string }>({
    testing: false,
  });
  /** Which CLI "Test agent CLI" probes; "" = the default provider above. */
  const [testProvider, setTestProvider] = useState("");

  useEffect(() => {
    (async () => {
      let provs: Provider[] = [];
      let saved = "";
      try {
        // Use /status (not /manage): it carries `installed`, and only an
        // installed CLI may be the default — you can't launch what isn't there.
        const [d, st] = await Promise.all([
          api<{ providers?: Provider[] }>("/api/providers/status"),
          api<{ settings?: { coding_cli?: { default_provider?: string; default_launch_args?: Record<string, string> } } }>(
            "/api/settings"
          ),
        ]);
        provs = d?.providers || [];
        const cc = st?.settings?.coding_cli;
        if (cc?.default_provider != null) saved = String(cc.default_provider).trim();
        flagsMap.current =
          cc?.default_launch_args && typeof cc.default_launch_args === "object"
            ? { ...cc.default_launch_args }
            : {};
      } catch {
        /* providers/settings are optional */
      }
      setProviders(provs);
      // Candidate defaults are installed CLIs only. Map the saved raw command /
      // alias to an installed provider NAME.
      const installed = provs.filter((p) => p.installed);
      const lower = saved.toLowerCase();
      const match = installed.find(
        (p) =>
          (p.name || "").toLowerCase() === lower ||
          (p.aliases || []).some((a) => String(a).toLowerCase() === lower) ||
          String(p.command || "").toLowerCase() === lower
      );
      // If the stored default isn't installed, fall back to the first installed
      // CLI and PERSIST the correction, so the default is never a missing CLI.
      const value = match ? match.name : saved ? installed[0]?.name || "" : "";
      setSelected(value);
      setFlags(flagsMap.current[value] || "");
      if (saved && !match && value) {
        s.saveField("coding_cli", "default_provider", value);
      }
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  /** Bumped after the default changes, so the sign-in row re-reads it. */
  const [defaultSaved, setDefaultSaved] = useState(0);
  const pickProvider = (name: string) => {
    setSelected(name);
    setFlags(flagsMap.current[name] || "");
    void s.saveField("coding_cli", "default_provider", name).then(() => setDefaultSaved((n) => n + 1));
  };

  const saveFlags = useCallback(
    async (value: string) => {
      const prov = selected.trim();
      if (!prov) return;
      const val = value.trim();
      if (val) flagsMap.current[prov] = val;
      else delete flagsMap.current[prov];
      // Re-send the whole map so saving one provider never wipes another's.
      try {
        await api("/api/settings", {
          json: { coding_cli: { default_launch_args: flagsMap.current } },
        });
        toast("Saved launch flags");
      } catch {
        toast("Save failed: launch flags");
      }
    },
    [selected]
  );

  const runAgentTest = async () => {
    setAgentTest({ testing: true });
    try {
      const r = await api<{ ok?: boolean; cli?: { detail?: string }; auth?: { detail?: string } }>(
        "/api/settings/test/agent",
        { method: "POST", json: { provider: testProvider || selected } }
      );
      const bits: string[] = [];
      if (r?.cli?.detail) bits.push(r.cli.detail);
      if (r?.auth?.detail) bits.push(r.auth.detail);
      setAgentTest({
        testing: false,
        ok: !!r?.ok,
        msg: bits.join(" · ") || (r?.ok ? "agent CLI ready" : "agent CLI not ready"),
      });
    } catch (e) {
      setAgentTest({ testing: false, ok: false, msg: (e as Error).message });
    }
  };

  return (
    <>
      <h3 className="set-section-title">Agents</h3>
      <label className="set-row">
        <span className="set-label">Default agent</span>
        <select id="default-provider-select" value={selected} onChange={(e) => pickProvider(e.target.value)}>
          {/* Every known agent, the absent ones disabled with the reason — you
              can't launch what isn't there, but hiding them made "where's
              codex?" a mystery. */}
          {pickerOptions(providers).map((o) => (
            <option key={o.name} value={o.name} disabled={o.disabled}>
              {o.label}
            </option>
          ))}
        </select>
        <input
          type="hidden"
          id="default-provider-input"
          data-group="coding_cli"
          data-field="default_provider"
          value={selected}
          readOnly
        />
        <span className="set-hint">
          New sessions start with this agent. To install or sign in to another, see{" "}
          <button type="button" className="linklike" data-goto-screen="connections" onClick={() => gotoScreen("connections")}>
            Connections
          </button>
          .
        </span>
      </label>
      <DefaultAgentStatus provider={selected} refresh={defaultSaved} gotoScreen={gotoScreen} />
      <label className="set-row">
        <span className="set-label">Extra flags for new sessions</span>
        <input
          type="text"
          id="default-launch-args-input"
          autoComplete="off"
          placeholder={flagsPlaceholder(selected)}
          value={flags}
          onChange={(e) => setFlags(e.target.value)}
          onBlur={() => saveFlags(flags)}
          onKeyDown={(e) => {
            if (e.key === "Enter") (e.target as HTMLInputElement).blur();
          }}
        />
        <FlagChips
          provider={selected}
          value={flags}
          onChange={(v) => {
            setFlags(v);
            saveFlags(v);
          }}
        />
        <span className="set-hint">
          Added every time the default agent starts. "Skip permissions", "Bypass approvals" and
          "Yes to all" let the agent edit files and run commands without asking you first.
        </span>
      </label>
      <SessionsAtOnce gotoScreen={gotoScreen} />
      <AdvancedFold id="coding-advanced">
        <WindowRefresh />
        <div className="set-row">
          <span className="set-label">Check another agent</span>
          <span className="test-row">
            <select
              id="agent-test-provider"
              aria-label="Agent to check"
              value={testProvider || selected}
              onChange={(e) => {
                setTestProvider(e.target.value);
                setAgentTest({ testing: false });
              }}
            >
              {providers.map((p) => (
                <option key={p.name} value={p.name}>
                  {p.name}
                  {p.name === selected ? " (default)" : ""}
                </option>
              ))}
            </select>
            <button type="button" id="agent-test-btn" className="test-btn" onClick={runAgentTest}>
              Test
            </button>
            <span
              id="agent-test-result"
              className={"test-result" + (agentTest.msg ? (agentTest.ok ? " ok" : " bad") : "")}
            >
              {agentTest.testing ? "testing…" : agentTest.msg ? (agentTest.ok ? "✓ " : "✗ ") + agentTest.msg : ""}
            </span>
          </span>
          <span className="set-hint">Is it installed, and can MindFlock see a sign-in?</span>
        </div>
      </AdvancedFold>
    </>
  );
}

/** The default agent's own Connections row: the same status, Test and Sign
 * in as Settings → Connections, read from the same endpoint — so "is my
 * agent signed in?" has one answer wherever you look. */
function DefaultAgentStatus({
  provider,
  refresh,
  gotoScreen,
}: {
  provider: string;
  refresh: number;
  gotoScreen(name: string): void;
}) {
  const [row, setRow] = useState<ConnectionRow | null>(null);
  const [result, setResult] = useState<TestOutcome | undefined>();
  const [testing, setTesting] = useState(false);
  const openDialogFor = useUi((st) => st.openDialogFor);
  const closeDialog = useUi((st) => st.closeDialog);

  const load = useCallback(async () => {
    try {
      const d = await api<ConnectionsResponse>("/api/connections?refresh=1");
      const rows = d?.connections || [];
      const want = (provider || "").toLowerCase();
      const found =
        rows.find((c) => c.kind === "agent" && (c as ConnectionRow & { provider?: string }).provider === want) ||
        rows.find((c) => c.kind === "agent" && c.default) ||
        null;
      setRow(found);
    } catch {
      setRow(null);
    }
  }, [provider]);

  useEffect(() => {
    setResult(undefined);
    void load();
  }, [load, refresh]);

  useEffect(() => {
    const on = () => void load();
    window.addEventListener(CONNECTIONS_CHANGED_EVENT, on);
    return () => window.removeEventListener(CONNECTIONS_CHANGED_EVENT, on);
  }, [load]);

  if (!row) return null;
  // "Manage" on this row leads HERE: drop it rather than offer a no-op.
  const toHere = row.action?.kind === "screen" && row.action.target === "coding";
  const shown: ConnectionRow = toHere ? { ...row, action: null, settings_screen: "" } : row;
  return (
    <div className="set-row coding-agent-status" id="coding-agent-status">
      <span className="set-label">Sign-in</span>
      <ConnectionRowView
        c={shown}
        deps={{ gotoScreen, openDialogFor, closeDialog }}
        result={result}
        testing={testing}
        onTest={
          row.test
            ? async () => {
                setTesting(true);
                const out = await runTest(row.test!);
                setTesting(false);
                setResult(out);
                void load();
              }
            : undefined
        }
        onChanged={() => void load()}
      />
    </div>
  );
}

/** "How many sessions can run at once?" — answered plainly. There is no
 * cap on the sessions you start; auto-started ticket sessions have one
 * (engine.max_sessions, the same field as Intake → Queue), and sessions an
 * agent starts have their own limits under Agent orchestration. */
function SessionsAtOnce({ gotoScreen }: { gotoScreen(name: string): void }) {
  return (
    <>
      <h3 className="set-section-title" id="coding-sessions-at-once">
        Sessions running at once
      </h3>
      <p className="set-hint">Sessions you start yourself have no limit. Start as many as you like.</p>
      <label className="set-row">
        <span className="set-label">Ticket sessions at once</span>
        <SettingField group="engine" field="max_sessions" type="number" placeholder="no limit" />
        <span className="set-hint">
          The most auto-started ticket sessions running together. Extra tickets wait their turn.
        </span>
      </label>
      <p className="set-hint">
        Sessions an agent starts for you have their own limits, under{" "}
        <button
          type="button"
          className="linklike"
          data-goto-screen="orchestration"
          onClick={() => gotoScreen("orchestration")}
        >
          Agent orchestration
        </button>
        .
      </p>
    </>
  );
}

/** Window-refresh keepalive (E): anchor a CLI's rolling usage window. */
function WindowRefresh() {
  interface WrOption {
    name: string;
    window?: { hours?: number; kind?: string; note?: string };
    next_fire?: number;
  }
  interface WrConfig {
    enabled?: boolean;
    interval_hours?: number;
    anchor_time?: string;
    providers?: string[];
    options?: WrOption[];
  }
  const [cfg, setCfg] = useState<WrConfig>({});

  const load = useCallback(async () => {
    try {
      setCfg(await api<WrConfig>("/api/window-refresh"));
    } catch {
      /* optional */
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const save = async (patch: Record<string, unknown>) => {
    try {
      await api("/api/window-refresh", { json: patch });
      await load();
    } catch {
      toast("Couldn't update window refresh");
    }
  };

  const selected = new Set(cfg.providers || []);
  const opts = cfg.options || [];
  const fmtWhen = (epoch: number) => {
    try {
      return new Date(epoch * 1000).toLocaleString();
    } catch {
      return "soon";
    }
  };
  const statusLines = opts
    .filter((o) => selected.has(o.name) && o.next_fire)
    .map((o) => o.name + ": next ~" + fmtWhen(o.next_fire!));

  return (
    <>
      <h3 className="set-section-title">Keep usage windows warm</h3>
      <div
        className="set-row set-switch-row"
        title="Send a tiny, connection-free ping on a schedule so a provider's rolling usage window anchors when you want it to."
      >
        <span className="set-label">Scheduled refresh</span>
        {/* label wraps only the switch, so clicking the row text no longer flips it */}
        <label className="ca-switch">
          <input
            type="checkbox"
            id="wr-enabled"
            checked={!!cfg.enabled}
            onChange={(e) => save({ enabled: e.target.checked })}
          />
          <span className="ca-slider" />
        </label>
      </div>
      <p className="set-hint">
        Sends a tiny message on a schedule so a fresh usage window starts when you want it,
        such as the start of your work day.
      </p>
      {cfg.enabled && (
        <div id="wr-opts">
          <label className="set-row">
            <span className="set-label">Fresh window at</span>
            <input
              type="time"
              id="wr-anchor"
              className="num-sm"
              defaultValue={cfg.anchor_time || ""}
              onChange={(e) => save({ anchor_time: e.target.value || "" })}
            />
            <span className="set-hint">Daily, at this local time. Leave empty to use a fixed interval.</span>
          </label>
          <label
            className={"set-row" + ((cfg.anchor_time || "").trim() ? " feature-off" : "")}
            id="wr-interval-row"
          >
            <span className="set-label">Or every</span>
            <input
              type="number"
              id="wr-interval"
              className="num-sm"
              min={0.25}
              max={168}
              step={0.25}
              defaultValue={typeof cfg.interval_hours === "number" ? cfg.interval_hours : undefined}
              onChange={(e) => {
                const v = parseFloat(e.target.value);
                if (!isNaN(v)) save({ interval_hours: v });
              }}
            />{" "}
            <span className="muted">hours</span>
            <span className="set-hint">Used when no time is set (Claude and Codex: 5 hours).</span>
          </label>
          <div className="set-row">
            <span className="set-label">Agents</span>
            <div id="wr-providers" className="wr-providers">
              {opts.map((o) => (
                <label className="wr-prov" key={o.name}>
                  <input
                    type="checkbox"
                    checked={selected.has(o.name)}
                    onChange={(e) => {
                      const next = new Set(selected);
                      if (e.target.checked) next.add(o.name);
                      else next.delete(o.name);
                      save({ providers: [...next] });
                    }}
                  />
                  <span title={o.window?.note || ""}>
                    {o.name +
                      (o.window?.hours
                        ? " · " + o.window.hours + "h window"
                        : o.window?.kind
                          ? " · " + o.window.kind
                          : "")}
                  </span>
                </label>
              ))}
            </div>
            <span className="set-hint" id="wr-status">
              {cfg.enabled && statusLines.length ? statusLines.join(" · ") : ""}
            </span>
          </div>
        </div>
      )}
    </>
  );
}
