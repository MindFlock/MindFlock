/** Settings → Agents (key "coding"; partial 106 + sections 21/22's pickers):
 * the default agent and its card (the SAME card Connections, Custom agents
 * and the Setup checklist show — components/setup/AgentCard — so "is my
 * agent ready?" has one answer and one button wherever you look), per-agent
 * default launch flags (a name -> flags map re-sent whole on every edit), how
 * many sessions run at once, "Not installed — how to install" (any other
 * agent's card, next to the picker that greys those agents out) and — under
 * Advanced — the window-refresh keepalive. */

import { SKIP_PERMS_LABEL, SKIP_PERMS_TITLE } from "../../../lib/vocab";
import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../../../api/client";
import { toast } from "../../../lib/toast";
import { flashSaved } from "../savedCue";
import { useAutosave } from "../../intake/autosave";
import { agentName } from "../../../lib/agentNames";
import { pickerOptions } from "../../../lib/providerRows";
import { FlagChips } from "../../dialogs/FlagChips";
import { SettingField, useSettings } from "../useSettings";
import type { ScreenProps } from "../SettingsDialog";
import { AdvancedFold } from "../settingsMode";
import { AgentCard } from "../../setup/AgentCard";

/** The flags field's example, per agent: a harmless flag that agent really
 * takes — never a permission-skipping one, which as grey placeholder text
 * read as if it were already set. */
export const FLAG_EXAMPLES: Record<string, string> = {
  claude: "e.g. --model opus",
  codex: "e.g. --search",
  antigravity: "e.g. --sandbox",
  aider: "e.g. --no-auto-commits",
};

/** Flags that let an agent act without asking (each agent's "skip
 * permissions" chip: claude/agy, codex, aider, opencode). */
export const SKIP_PERMISSION_FLAGS = [
  "--dangerously-skip-permissions",
  "--dangerously-bypass-approvals-and-sandbox",
  "--yes-always",
  "--auto",
];

/** The "skip permissions" chip's words, for every place that draws one
 * (Settings → Agents' flag chips and the New dialog's): the label says it is a
 * risk, the tooltip says what it does. dialogs/FlagChips.tsx owns the chips;
 * it should read these. */
// One source for the warning: lib/vocab (the New dialog's chips use it too).
export const SKIP_PERMISSIONS_LABEL = SKIP_PERMS_LABEL;
export const SKIP_PERMISSIONS_TIP = SKIP_PERMS_TITLE;

export function skipsPermissions(flags: string): boolean {
  const tokens = (flags || "").split(/\s+/);
  return SKIP_PERMISSION_FLAGS.some((f) => tokens.includes(f));
}

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
  /** Which other agent's card the fold shows; "" = the first one that isn't the default. */
  const [otherProvider, setOtherProvider] = useState("");

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
    void s.saveField("coding_cli", "default_provider", name).then((ok) => ok && setDefaultSaved((n) => n + 1));
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

  const others = providers.filter((p) => p.name !== selected);
  // The picker greys out every agent that isn't installed; the fold beside it
  // says how to install one, starting on the first of those.
  const missing = others.filter((p) => !p.installed);
  const other =
    others.find((p) => p.name === otherProvider)?.name || missing[0]?.name || others[0]?.name || "";

  return (
    <>
      <h3 className="set-section-title">Agents</h3>
      <label className="set-row" data-tour="settings-default-agent">
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
        <span className="set-hint">New sessions start with this agent.</span>
      </label>
      {/* Next to the picker, not under Advanced: the greyed-out agents above
          are the question this answers ("where's Codex?"). */}
      {others.length > 0 && (
        <details className="pr-advanced set-adv-fold coding-other-fold" id="coding-other-agent-fold">
          <summary>{missing.length ? "Not installed — how to install" : "Set up another agent"}</summary>
          <div className="set-row" id="coding-other-agent">
            <span className="set-label">Set up another agent</span>
            <select
              id="agent-test-provider"
              aria-label="Another agent"
              value={other}
              onChange={(e) => setOtherProvider(e.target.value)}
            >
              {others.map((p) => (
                <option key={p.name} value={p.name}>
                  {agentName(p.name) + (p.installed ? "" : " — not installed")}
                </option>
              ))}
            </select>
            {other && <AgentCard key={other} provider={other} here="coding" gotoScreen={gotoScreen} />}
          </div>
        </details>
      )}
      <div className="set-row coding-agent-status" id="coding-agent-status">
        <span className="set-label">Status</span>
        <AgentCard key={selected + ":" + defaultSaved} provider={selected} here="coding" gotoScreen={gotoScreen} />
      </div>
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
        <span className="set-hint">Added every time the default agent starts.</span>
        {skipsPermissions(flags) && (
          <span className="set-hint flags-danger" id="default-launch-args-warning" role="note">
            {SKIP_PERMISSIONS_LABEL}: {SKIP_PERMISSIONS_TIP.charAt(0).toLowerCase() + SKIP_PERMISSIONS_TIP.slice(1)}.
          </span>
        )}
      </label>
      <SessionsAtOnce gotoScreen={gotoScreen} />
      <AdvancedFold id="coding-advanced">
        <WindowRefresh />
      </AdvancedFold>
    </>
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

  /** `el` is the control the change came from: "✓ Saved" lands beside it. */
  const save = async (patch: Record<string, unknown>, el?: Element | null) => {
    try {
      await api("/api/window-refresh", { json: patch });
      await load();
      if (!flashSaved(el)) toast("Saved");
    } catch {
      toast("Couldn't update window refresh");
    }
  };
  // The time and the interval are typed: sent once typing pauses (or on
  // blur / the dialog closing), not one POST per keystroke.
  const anchorEl = useRef<HTMLInputElement>(null);
  const intervalEl = useRef<HTMLInputElement>(null);
  const anchorSave = useAutosave<string>((v) => {
    if (v !== (cfg.anchor_time || "")) void save({ anchor_time: v }, anchorEl.current);
  });
  const intervalSave = useAutosave<number>((v) => {
    if (v !== cfg.interval_hours) void save({ interval_hours: v }, intervalEl.current);
  });

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
            onChange={(e) => save({ enabled: e.target.checked }, e.currentTarget)}
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
              ref={anchorEl}
              className="num-sm"
              defaultValue={cfg.anchor_time || ""}
              onChange={(e) => anchorSave.schedule(e.target.value || "")}
              onBlur={() => anchorSave.flush()}
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
              ref={intervalEl}
              className="num-sm"
              min={0.25}
              max={168}
              step={0.25}
              defaultValue={typeof cfg.interval_hours === "number" ? cfg.interval_hours : undefined}
              onChange={(e) => {
                const v = parseFloat(e.target.value);
                if (!isNaN(v)) intervalSave.schedule(v);
              }}
              onBlur={() => intervalSave.flush()}
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
                      save({ providers: [...next] }, e.currentTarget);
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
