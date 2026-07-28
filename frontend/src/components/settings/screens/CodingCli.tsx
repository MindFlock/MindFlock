/** Settings → Coding CLI (partial 106 + sections 21/22's pickers): default
 * provider, per-provider default launch flags (a name -> flags map re-sent
 * whole on every edit), agent test, and the window-refresh keepalive. */

import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../../../api/client";
import { toast } from "../../../lib/toast";
import { FlagChips } from "../../dialogs/FlagChips";
import { useSettings } from "../useSettings";
import type { ScreenProps } from "../SettingsDialog";

interface Provider {
  name: string;
  aliases?: string[];
  command?: string;
  installed?: boolean;
  path?: string;
}

export function CodingCli(_: ScreenProps) {
  const s = useSettings();
  const [providers, setProviders] = useState<Provider[]>([]);
  const [selected, setSelected] = useState("");
  const [flags, setFlags] = useState("");
  const flagsMap = useRef<Record<string, string>>({});
  const [agentTest, setAgentTest] = useState<{ testing: boolean; ok?: boolean; msg?: string }>({
    testing: false,
  });

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

  const pickProvider = (name: string) => {
    setSelected(name);
    setFlags(flagsMap.current[name] || "");
    s.saveField("coding_cli", "default_provider", name);
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
        { method: "POST" }
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
      <h3 className="set-section-title">Agent CLI</h3>
      <label className="set-row">
        <span className="set-label">Default provider</span>
        <select id="default-provider-select" value={selected} onChange={(e) => pickProvider(e.target.value)}>
          {providers
            .filter((p) => p.installed)
            .map((p) => (
              <option key={p.name} value={p.name}>
                {p.name}
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
          Program new sessions launch by default — only installed agent CLIs are listed.
        </span>
      </label>
      <label className="set-row">
        <span className="set-label">Default launch flags</span>
        <input
          type="text"
          id="default-launch-args-input"
          autoComplete="off"
          placeholder="--dangerously-skip-permissions"
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
          Extra CLI flags added to every new session of the <b>Default provider</b> above —
          flags are per-agent, so switch the provider to set another CLI's defaults.
        </span>
      </label>
      <div className="set-row">
        <span className="set-label">Agent CLI check</span>
        <span className="test-row">
          <button type="button" id="agent-test-btn" className="test-btn" onClick={runAgentTest}>
            Test agent CLI
          </button>
          <span
            id="agent-test-result"
            className={"test-result" + (agentTest.msg ? (agentTest.ok ? " ok" : " bad") : "")}
          >
            {agentTest.testing ? "testing…" : agentTest.msg ? (agentTest.ok ? "✓ " : "✗ ") + agentTest.msg : ""}
          </span>
        </span>
        <span className="set-hint">Probes the configured CLI binary and its login state.</span>
      </div>
      <WindowRefresh />
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
        Sends a 1-token message (no MCPs/connections attached) to anchor the rolling usage
        window. Pick the time you want a totally fresh window to begin — usually the start of
        your work day.
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
            <span className="set-hint">
              A tiny ping fires daily at this local time, so your usage window resets right
              then. Leave empty to use a fixed interval instead.
            </span>
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
            <span className="set-hint">
              Fallback when no time is set — matches the provider's rolling window
              (Claude/Codex: 5h).
            </span>
          </label>
          <div className="set-row">
            <span className="set-label">Providers</span>
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
