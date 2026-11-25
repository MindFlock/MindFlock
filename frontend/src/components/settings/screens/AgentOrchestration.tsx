/** Settings → Agent orchestration: what an agent may do with the rest of the
 * flock through the MindFlock MCP, and how far it may fan out.
 *
 * - the MCP switch and its scope (were a fold at the bottom of General);
 * - Workers (sessions a session starts): the workers request and model
 *   (lib/workersRequest; moved here from General, saved under `general`);
 * - the spawn caps: live workers per orchestrator, how deep spawns may
 *   nest, and workers alive at once. They are guard-rails the
 *   server applies at each spawn (backend.web.core.lineage), read per request,
 *   so a change applies to the next spawn — no relaunch. An env var
 *   (MINDFLOCK_MAX_*) set on the server still wins; the row says so;
 * - shared MCP servers: which of the user's own MCP servers run once per
 *   machine instead of once per window (SharedMcpServers). */

import { useEffect, useState } from "react";
import {
  ASK_WORKERS_KEYS,
  DEFAULT_WORKERS_MODEL,
  DEFAULT_WORKERS_TEMPLATE,
  FLOCK_KEYS,
} from "../../../lib/workersRequest";
import { useConfig } from "../../../state/queries";
import type { OrchestrationCap } from "../../../api/types";
import { SettingField, useSettings } from "../useSettings";
import type { ScreenProps } from "../SettingsDialog";
import { SharedMcpServers } from "./SharedMcpServers";

export function AgentOrchestration(_: ScreenProps) {
  return (
    <>
      <h3 className="set-section-title">Agent orchestration</h3>
      <p className="set-hint orch-intro">
        Agents can see the other sessions, message them, and start and steer
        workers of their own through the MindFlock MCP.
      </p>
      <AgentMcpRows />
      <h3 className="set-section-title">Workers</h3>
      <p className="set-hint orch-intro">
        Sessions a session starts. Ask for them from a session's Workers… (its pane
        button or ⋯ menu): let the agent split it now, ask once, or on every prompt.
      </p>
      <WorkersRows />
      <AutoCloseRows />
      <ModelDownshiftRow />
      <h3 className="set-section-title">Spawn limits</h3>
      <p className="set-hint orch-intro">
        Guard-rails on how far one orchestrator can fan out. A change applies to
        the next spawn. Leave a box empty for the default.
      </p>
      <CapRow
        cap="max_children"
        field="agent_max_children"
        label="Workers per orchestrator"
        hint="How many live workers one session may have at once. Also the most workers a split can make."
      />
      <CapRow
        cap="max_spawned"
        field="agent_max_spawned"
        label="Workers in total"
        hint="Every worker alive at once, across all orchestrators. Raise it with the per-orchestrator cap, or it stops you first."
      />
      <CapRow
        cap="max_spawn_depth"
        field="agent_max_spawn_depth"
        label="Nesting depth"
        hint="How deep spawns may nest: 1 = only your sessions start workers, 2 = their workers may start workers too, and so on."
      />
      <SharedMcpServers />
    </>
  );
}

/** The workers request: a template (`{model}` becomes "Workers model") the
 * server fills in and serves as `/api/config` → `flock.request`. Empty means
 * the default (shown as the placeholder). Saved on blur (still under `general`, where the
 * server reads it); the row marks "✓ Saved", a failure toasts and re-syncs. */
function WorkersRows() {
  const s = useSettings();
  const stored = s.get("general", "workers_request");
  const display = typeof stored === "string" ? stored : "";
  const [value, setValue] = useState(display);
  useEffect(() => setValue(display), [display]);
  // Unset reads as on (settings.GeneralSettings.workers_decide).
  const decideStored = s.get("general", "workers_decide");
  const decide = decideStored !== false && decideStored !== "false" && decideStored !== "0";
  return (
    <>
      <div className="set-row set-switch-row workers-decide-row">
        <span className="notif-rule-text">
          <span className="set-label">Let the agent decide where each prompt goes</span>
          <span className="set-hint notif-rule-desc">
            With "Workers on every prompt" on ({FLOCK_KEYS}), keep sending ideas to one session: its agent
            does a small follow-up itself, splits a big one across workers and merges them back, and starts
            a separate idea as its own session on its own branch, off the base branch. Off: every prompt
            asks for workers.
          </span>
        </span>
        <label className="ca-switch">
          <input
            type="checkbox"
            data-group="general"
            data-field="workers_decide"
            checked={decide}
            onChange={(e) => void s.saveField("general", "workers_decide", e.target.checked)}
          />
          <span className="ca-slider" />
        </label>
      </div>
      <label className="set-row" id="orch-workers">
        <span className="set-label">Workers request</span>
        <textarea
          className="workers-request"
          data-group="general"
          data-field="workers_request"
          rows={4}
          style={{ resize: "vertical", lineHeight: 1.45 }}
          spellCheck={false}
          placeholder={DEFAULT_WORKERS_TEMPLATE}
          value={value}
          onChange={(e) => setValue(e.target.value)}
          onBlur={() => {
            if (value !== display) void s.saveField("general", "workers_request", value);
          }}
        />
        <span className="set-hint">
          What "Ask for workers once" types ({ASK_WORKERS_KEYS}), and how "Workers on every prompt" asks for
          workers ({FLOCK_KEYS}) — every time, or when the agent decides a prompt needs them.{" "}
          <code>{"{model}"}</code> becomes the model below; empty uses the default.
        </span>
      </label>
      <label className="set-row">
        <span className="set-label">Workers model</span>
        <SettingField group="general" field="workers_model" placeholder={DEFAULT_WORKERS_MODEL} />
        <span className="set-hint">The model the request asks workers to run on.</span>
      </label>
    </>
  );
}

/** The switch's words (exported for tests). */
export const AUTO_CLOSE_LABEL = "Close finished sub-sessions automatically";
export const AUTO_CLOSE_NOTE =
  "A worker an agent started closes by itself once it has reported done, sits idle, and its work is merged. It stays in Recently closed.";

export const MODEL_DOWNSHIFT_LABEL = "Switch to the next model when one runs out";

/** One model spent while the plan has room (backend core/model_downshift.py):
 * ON by default (unset reads as on, `general.model_downshift`). A session
 * whose newest notice is "your Fable limit resets Nov 30" is switched to the
 * next model down (`/model opus`) and told to carry on. */
function ModelDownshiftRow() {
  const s = useSettings();
  const stored = s.get("general", "model_downshift");
  const on = stored !== false && stored !== "false" && stored !== "0";
  return (
    <div className="set-row set-switch-row" id="orch-model-downshift">
      <span className="notif-rule-text">
        <span className="set-label">{MODEL_DOWNSHIFT_LABEL}</span>
        <span className="set-hint notif-rule-desc">
          When a session runs out of one model while your plan still has room (Claude: “your Fable limit
          resets…”), MindFlock switches it to the next one down (Fable → Opus → Sonnet → Haiku) and tells it to
          carry on, so the work doesn't stop.
        </span>
      </span>
      <label className="ca-switch">
        <input
          type="checkbox"
          data-group="general"
          data-field="model_downshift"
          checked={on}
          onChange={(e) => void s.saveField("general", "model_downshift", e.target.checked)}
        />
        <span className="ca-slider" />
      </label>
    </div>
  );
}

/** Finished workers close themselves (backend core/auto_close.py): ON by
 * default (unset reads as on, `general.auto_close_children`), after
 * `general.auto_close_grace_minutes` (empty = 10). Blocked workers, unmerged
 * work and anything you are looking at stay open; a session's ⋯ → Keep open
 * opts one out. */
function AutoCloseRows() {
  const s = useSettings();
  const stored = s.get("general", "auto_close_children");
  const on = stored !== false && stored !== "false" && stored !== "0";
  return (
    <>
      <div className="set-row set-switch-row" id="orch-auto-close">
        <span className="notif-rule-text">
          <span className="set-label">{AUTO_CLOSE_LABEL}</span>
          <span className="set-hint notif-rule-desc">
            {AUTO_CLOSE_NOTE} Blocked workers, unmerged work and a worker you have open stay. Turn it off
            for one worker from its ⋯ menu: Keep open.
          </span>
        </span>
        <label className="ca-switch">
          <input
            type="checkbox"
            data-group="general"
            data-field="auto_close_children"
            checked={on}
            onChange={(e) => void s.saveField("general", "auto_close_children", e.target.checked)}
          />
          <span className="ca-slider" />
        </label>
      </div>
      {on && (
        <label className="set-row">
          <span className="set-label">Minutes a finished worker stays open first</span>
          <SettingField group="general" field="auto_close_grace_minutes" type="number" placeholder="10" />
          <span className="set-hint">
            Time for its orchestrator to read it, counted from when it finished. Anything you type into it after its report keeps it open.
          </span>
        </label>
      )}
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

/** The scope select's options. "" is the server's default (`children`: its
 * workers); stored
 * only when the user picks something else (settings.GeneralSettings). */
export const AGENT_MCP_SCOPE_OPTIONS = [
  { value: "", label: "Default (its workers)" },
  { value: "children", label: "Its workers — manage only sessions it started" },
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
            // saveField marks "✓ Saved" by the switch once the save lands (a
            // failed save says so instead); the hint above already says when
            // it applies, so no second toast claiming success up front.
            onChange={(e) => void s.saveField("general", "agent_mcp", e.target.checked)}
          />
          <span className="ca-slider" />
        </label>
      </div>
      <label
        className="set-row"
        title="How far an agent may MANAGE other sessions through the MCP (answer their prompts, kill them, re-parent them). Seeing the other sessions and messaging are allowed in every scope except read-only."
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
