/** Settings → Agent orchestration: what an agent may do with the rest of the
 * flock through the MindFlock MCP, and how far it may fan out.
 *
 * - the MCP switch and its scope (were a fold at the bottom of General);
 * - the spawn caps: live sub-sessions per orchestrator, how deep spawns may
 *   nest, and agent-spawned sessions alive at once. They are guard-rails the
 *   server applies at each spawn (backend.web.core.lineage), read per request,
 *   so a change applies to the next spawn — no relaunch. An env var
 *   (MINDFLOCK_MAX_*) set on the server still wins; the row says so. */

import { toast } from "../../../lib/toast";
import { useConfig } from "../../../state/queries";
import type { OrchestrationCap } from "../../../api/types";
import { SettingField, useSettings } from "../useSettings";
import type { ScreenProps } from "../SettingsDialog";

export function AgentOrchestration(_: ScreenProps) {
  return (
    <>
      <h3 className="set-section-title">Agent orchestration</h3>
      <p className="set-hint orch-intro">
        Agents can see the other sessions, message them, and start and steer
        sub-sessions of their own through the MindFlock MCP.
      </p>
      <AgentMcpRows />
      <h3 className="set-section-title">Spawn limits</h3>
      <p className="set-hint orch-intro">
        Guard-rails on how far one orchestrator can fan out. A change applies to
        the next spawn. Leave a box empty for the default.
      </p>
      <CapRow
        cap="max_children"
        field="agent_max_children"
        label="Sub-sessions per orchestrator"
        hint="How many live sub-sessions one session may have at once. Also the most pieces a split can make."
      />
      <CapRow
        cap="max_spawned"
        field="agent_max_spawned"
        label="Agent-spawned sessions in total"
        hint="Every agent-started session alive at once, across all orchestrators. Raise it with the per-orchestrator cap, or it stops you first."
      />
      <CapRow
        cap="max_spawn_depth"
        field="agent_max_spawn_depth"
        label="Nesting depth"
        hint="How deep spawns may nest: 1 = only your sessions spawn, 2 = their sub-sessions may spawn too, and so on."
      />
    </>
  );
}

/** One spawn cap: a number box over `general.<field>` (blank = the built-in
 * default, shown as the placeholder), and a line saying what applies now —
 * including when a server env var overrides the box. */
function CapRow({
  cap,
  field,
  label,
  hint,
}: {
  cap: "max_children" | "max_spawn_depth" | "max_spawned";
  field: string;
  label: string;
  hint: string;
}) {
  const { data: config } = useConfig();
  const now: OrchestrationCap | undefined = config?.caps?.orchestration?.[cap];
  return (
    <label className="set-row orch-cap-row" title={hint} data-cap={cap}>
      <span className="set-label">{label}</span>
      <SettingField
        group="general"
        field={field}
        type="number"
        placeholder={now ? String(now.default) : ""}
      />
      <span className="set-hint">
        {hint}
        {now && now.source === "env" && (
          <span className="orch-cap-env">
            {" "}
            Now {now.value}: the server was started with {now.env}={now.value},
            which overrides this box.
          </span>
        )}
        {now && now.source !== "env" && (
          <span className="orch-cap-now">
            {" "}
            Now {now.value}
            {now.source === "default" ? " (default)" : ""}.
          </span>
        )}
      </span>
    </label>
  );
}

/** The scope select's options. "" is the server's default (children); stored
 * only when the user picks something else (settings.GeneralSettings). */
export const AGENT_MCP_SCOPE_OPTIONS = [
  { value: "", label: "Default (children)" },
  { value: "children", label: "Children — manage only sessions it spawned" },
  { value: "readonly", label: "Read-only — look and check its inbox, no messaging" },
  { value: "all", label: "All — manage any session" },
];

/** MindFlock MCP auto-attach: every Claude / Codex session's CLI is launched
 * with the MindFlock MCP server, so its agent can list the flock, message other
 * sessions and spawn / steer workers. Unset reads as on (see
 * settings.GeneralSettings.agent_mcp). Both knobs are read at LAUNCH, so they
 * apply to each session's next (re)launch — a running agent keeps the tools it
 * started with. */
function AgentMcpRows() {
  const s = useSettings();
  const { data: config } = useConfig();
  const stored = s.get("general", "agent_mcp");
  const on = stored !== false && stored !== "false" && stored !== "0";
  // The server's MINDFLOCK_AGENT_MCP=0 wins over this switch; say so rather
  // than show an "on" that does nothing. `=== false` so an older server (no
  // agent_mcp cap) is not reported as overriding anything.
  const envOff = on && config?.caps?.agent_mcp?.enabled === false;
  const providers = config?.caps?.agent_mcp?.providers;
  // Provider ids are lower-case ("claude"); the sentence names products.
  const clis =
    providers && providers.length
      ? providers.map((p) => p.charAt(0).toUpperCase() + p.slice(1)).join(" and ")
      : "Claude and Codex";
  return (
    <>
      <div className="set-row set-switch-row agent-mcp-row">
        <span className="notif-rule-text">
          <span className="set-label">
            Give agents the MindFlock MCP (agent-to-agent messaging and orchestration)
          </span>
          <span className="set-hint notif-rule-desc">
            Launches each {clis} session with MindFlock's MCP server attached, so
            its agent can see the other sessions, message them, and spawn and
            steer worker sessions of its own. Applies on each session's next
            launch — running agents keep what they started with.
          </span>
          {envOff && (
            <span className="set-hint notif-rule-desc agent-mcp-env-off">
              Off for now: the server was started with MINDFLOCK_AGENT_MCP=0,
              which overrides this switch.
            </span>
          )}
        </span>
        {/* label wraps only the switch, so clicking the row text no longer flips it */}
        <label className="ca-switch">
          <input
            type="checkbox"
            checked={on}
            onChange={(e) => {
              s.saveField("general", "agent_mcp", e.target.checked);
              toast(
                e.target.checked
                  ? "Agent MCP on — from each session's next launch"
                  : "Agent MCP off — from each session's next launch"
              );
            }}
          />
          <span className="ca-slider" />
        </label>
      </div>
      <label
        className="set-row"
        title="How far an agent may MANAGE other sessions through the MCP (answer their prompts, kill them, re-parent them). Reading the flock and messaging are allowed in every scope except read-only."
      >
        <span className="set-label">Agent MCP scope</span>
        <SettingField group="general" field="agent_mcp_scope" options={AGENT_MCP_SCOPE_OPTIONS} />
        <span className="set-hint">
          Applies on each session's next launch. A guard-rail, not a security boundary.
        </span>
      </label>
    </>
  );
}
