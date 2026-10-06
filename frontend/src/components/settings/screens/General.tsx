/** Settings → General (partial 101 + the scroll-speed wiring from
 * section 20): budgets + terminal scroll speed. */

import { useEffect, useState } from "react";
import { api } from "../../../api/client";
import {
  BREAK_MAX_MINUTES,
  BREAK_MIN_MINUTES,
  clampBreakMinutes,
  clampIdleMinutes,
  IDLE_MAX_MINUTES,
  IDLE_MIN_MINUTES,
} from "../../../lib/breakTimer";
import { setWheelDamping } from "../../../lib/terminals";
import { toast } from "../../../lib/toast";
import { useUi } from "../../../state/store";
import { useConfig } from "../../../state/queries";
import { SettingField, useSettings } from "../useSettings";
import type { ScreenProps } from "../SettingsDialog";

export function General(_: ScreenProps) {
  return (
    <>
      {/* First, because it is the part of General a newcomer came for — and
          the part an old hand never needs to scroll past to reach budgets. */}
      <GettingStarted />
      <h3 className="set-section-title">General</h3>
      <label
        className="set-row"
        title="When a session's estimated cost crosses this figure, MindFlock fires a one-time warning (toast, desktop notification, shell hooks). 0 or empty = off."
      >
        <span className="set-label">Per-session budget (USD, 0 = off)</span>
        <SettingField group="general" field="session_budget_usd" type="number" placeholder="0" />
        <span className="set-hint">
          Runaway-agent insurance — emits session.budget_exceeded once per session.
        </span>
      </label>
      <label
        className="set-row"
        title="Your estimate of how much API-equivalent usage your plan allows per rolling window (e.g. per 5h on Anthropic plans). Powers the '% left' in the sidebar's Usage bar — leave 0 to show only the reset countdown."
      >
        <span className="set-label">Plan window budget (≈USD per window, 0 = off)</span>
        <SettingField group="general" field="window_budget_usd" type="number" placeholder="0" />
        <span className="set-hint">
          Subscription plans only — the '% left' estimate in the sidebar's Usage bar is
          measured against this. Not billed dollars.
        </span>
      </label>
      <ResumeOnUsageResetRow />
      <ScrollSpeedRow />
      <ReduceMotionRow />
      <TakeABreakRow />
      <IdleFlockRow />
      {/* Folded: it is on by default and the people who tune it know it is
          here. Two rows of orchestration vocabulary in the middle of General
          were the first thing a newcomer had to read past. */}
      <details className="pr-advanced agent-mcp-fold">
        <summary>Agent orchestration (MindFlock MCP)</summary>
        <div className="pr-advanced-body">
          <AgentMcpRows />
        </div>
      </details>
    </>
  );
}

/** Auto-resume after a usage limit: nudge a session that ran out mid-task to
 * carry on once the provider's window reopens. The prompt queue has always done
 * this for sessions with something queued; this covers the ones with an empty
 * queue, which otherwise sit on the CLI's limit screen until someone comes
 * back. Unset reads as on (see settings.GeneralSettings). */
function ResumeOnUsageResetRow() {
  const s = useSettings();
  const stored = s.get("general", "resume_on_usage_reset");
  const on = stored !== false && stored !== "false" && stored !== "0";
  return (
    <div className="set-row set-switch-row">
      <span className="notif-rule-text">
        <span className="set-label">Resume sessions when usage comes back</span>
        <span className="set-hint notif-rule-desc">
          When an agent runs out of usage it parks on its CLI's limit screen and
          stays there — even after the window resets. With this on, MindFlock
          watches those sessions and tells them to continue the moment usage
          returns, the same way the prompt queue already resumes sessions that
          have something queued. You get a notification either way (Settings →
          Notifications).
        </span>
      </span>
      {/* label wraps only the switch, so clicking the row text no longer flips it */}
      <label className="ca-switch">
        <input
          type="checkbox"
          checked={on}
          onChange={(e) => {
            s.saveField("general", "resume_on_usage_reset", e.target.checked);
            toast(e.target.checked ? "Auto-resume on" : "Auto-resume off");
          }}
        />
        <span className="ca-slider" />
      </label>
    </div>
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

/** Onboarding controls, parked at the bottom of General: the master hints
 * switch and a button to replay the welcome walkthrough. Turning hints back on
 * re-arms every hint the user had dismissed. */
function GettingStarted() {
  const enabled = useUi((s) => s.hintsEnabled);
  const setHintsEnabled = useUi((s) => s.setHintsEnabled);
  const openTour = useUi((s) => s.openTour);
  const closeDialog = useUi((s) => s.closeDialog);
  return (
    <div className="onboarding-block">
      <h3 className="set-section-title">Getting started</h3>
      <p className="set-hint">
        Tips and a guided tour to help you set up MindFlock's features.
      </p>
      <div
        className="set-row set-switch-row"
        title="Small inline 💡 tips that point out features around the app. Turn them back on any time to see the ones you dismissed again."
      >
        <span className="set-label">Show getting-started hints</span>
        {/* label wraps only the switch, so clicking the row text no longer flips it */}
        <label className="ca-switch">
          <input
            type="checkbox"
            checked={enabled}
            onChange={(e) => {
              setHintsEnabled(e.target.checked);
              toast(e.target.checked ? "Hints re-enabled" : "Hints turned off");
            }}
          />
          <span className="ca-slider" />
        </label>
      </div>
      <div className="set-row onboarding-action">
        <div className="onboarding-action-text">
          <span className="set-label">Welcome walkthrough</span>
          <span className="set-hint">
            A short tour: sessions and the grid, shipping, and where work comes from (Intake, Verify, Customize).
          </span>
        </div>
        <button
          type="button"
          className="test-btn"
          onClick={() => {
            closeDialog();
            openTour();
          }}
        >
          Replay tour
        </button>
      </div>
    </div>
  );
}

/** Reduce motion: while an agent is running, cover its terminal with a static
 * "running" panel instead of the live (flickering) output — easier on the eyes.
 * Off by default; the cover lifts on interaction (see Pane's RunningCover). */
function ReduceMotionRow() {
  const reduceMotion = useUi((s) => s.reduceMotion);
  const setReduceMotion = useUi((s) => s.setReduceMotion);
  return (
    <div className="set-row set-switch-row">
      <span className="notif-rule-text">
        <span className="set-label">Reduce motion</span>
        <span className="set-hint notif-rule-desc">
          While an agent is running, the Agent tab's live terminal scrolls
          constantly, which some people find tiring to look at. With this on, a
          running agent's terminal is hidden behind a still "running" panel
          instead. Clicking, scrolling, or typing anywhere in that window brings
          the live output back; it returns to the panel after 10 seconds with no
          input. Only the Agent tab is affected — Terminal, Diff, and Queue are
          never covered. Off by default.
        </span>
      </span>
      {/* label wraps only the switch, so clicking the row text no longer flips it */}
      <label className="ca-switch">
        <input
          type="checkbox"
          checked={reduceMotion}
          onChange={(e) => {
            setReduceMotion(e.target.checked);
            toast(e.target.checked ? "Reduce motion on" : "Reduce motion off");
          }}
        />
        <span className="ca-slider" />
      </label>
    </div>
  );
}

/** Take a break: a reminder on a timer, with the flock from mindflock.ai flying
 * over your grid. Snooze pushes it back five minutes; "Resume Working" restarts
 * the full interval. Off by default — an app that interrupts you uninvited is a
 * worse app.
 *
 * The row's whole description is the sentence it configures, with the interval
 * editable in place. Shown whether or not the switch is on, because it is what
 * tells you what the switch does. */
function TakeABreakRow() {
  const on = useUi((s) => s.breakReminder);
  const every = useUi((s) => s.breakEveryMin);
  const setOn = useUi((s) => s.setBreakReminder);
  const setEvery = useUi((s) => s.setBreakEveryMin);
  // Local text state so the field can be empty mid-edit; the store only ever
  // sees a clamped whole number (typing "9" on the way to "90" would otherwise
  // be clamped to the 5-minute floor and eat the keystroke).
  const [draft, setDraft] = useState(String(every));
  useEffect(() => setDraft(String(every)), [every]);

  const commit = (raw: string) => {
    const next = clampBreakMinutes(raw === "" ? every : raw);
    setEvery(next);
    setDraft(String(next));
    if (on) toast("Break reminder every " + next + " min");
  };

  return (
    <div className="set-row set-switch-row">
      <span className="notif-rule-text">
        <span className="set-label">Take a break</span>
        <span className="set-hint notif-rule-desc break-every">
          Reminder to take a break every{" "}
          <input
            type="number"
            aria-label="Minutes between break reminders"
            min={BREAK_MIN_MINUTES}
            max={BREAK_MAX_MINUTES}
            step={5}
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onBlur={(e) => commit(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") (e.target as HTMLInputElement).blur();
            }}
          />{" "}
          minutes.
        </span>
      </span>
      {/* label wraps only the switch, so clicking the row text no longer flips it */}
      <label className="ca-switch">
        <input
          type="checkbox"
          checked={on}
          onChange={(e) => {
            setOn(e.target.checked);
            toast(
              e.target.checked
                ? "Break reminder on — every " + every + " min"
                : "Break reminder off"
            );
          }}
        />
        <span className="ca-slider" />
      </label>
    </div>
  );
}

/** The idle flock: birds over the grid once nobody has touched this window for
 * a while. On by default, and the one animation in the app that is safe to
 * leave on — it can only appear when you are not there to be interrupted. The
 * delay is the same sentence-with-a-field shape as the break row above.
 *
 * "Touched" means a click, a keystroke, a scroll or a tap in THIS window; a
 * drifting mouse and a streaming agent are not you (see breaks/useIdle). */
function IdleFlockRow() {
  const on = useUi((s) => s.idleFlock);
  const after = useUi((s) => s.idleFlockAfterMin);
  const setOn = useUi((s) => s.setIdleFlock);
  const setAfter = useUi((s) => s.setIdleFlockAfterMin);
  // Local text state so the field can be empty mid-edit — same reason as the
  // break interval: typing "1" on the way to "15" must not commit a 1.
  const [draft, setDraft] = useState(String(after));
  useEffect(() => setDraft(String(after)), [after]);

  const commit = (raw: string) => {
    const next = clampIdleMinutes(raw === "" ? after : raw);
    setAfter(next);
    setDraft(String(next));
    if (on) toast("Idle flock after " + next + " min");
  };

  return (
    <div className="set-row set-switch-row">
      <span className="notif-rule-text">
        <span className="set-label">Idle flock</span>
        <span className="set-hint notif-rule-desc break-every">
          Fly the flock over your grid after{" "}
          <input
            type="number"
            aria-label="Minutes idle before the flock appears"
            min={IDLE_MIN_MINUTES}
            max={IDLE_MAX_MINUTES}
            step={5}
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onBlur={(e) => commit(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") (e.target as HTMLInputElement).blur();
            }}
          />{" "}
          minutes with no click, keystroke or scroll in this window. It hides
          nothing and takes no input — the first touch sends the birds home.
        </span>
      </span>
      {/* label wraps only the switch, so clicking the row text no longer flips it */}
      <label className="ca-switch">
        <input
          type="checkbox"
          checked={on}
          onChange={(e) => {
            setOn(e.target.checked);
            toast(
              e.target.checked ? "Idle flock on — after " + after + " min" : "Idle flock off"
            );
          }}
        />
        <span className="ca-slider" />
      </label>
    </div>
  );
}

/** Slider position = thirds of a line (1–9 → 0.33…3); tmux gets the whole-line
 * part and the fractional residue applies client-side (setWheelDamping). */
function ScrollSpeedRow() {
  const [pos, setPos] = useState(3);
  const fmt = (v: number) => String(Math.round(v * 100) / 100);

  useEffect(() => {
    (async () => {
      try {
        const s = await api<{ speed?: number }>("/api/scroll-speed");
        if (s?.speed) {
          setPos(Math.round(s.speed * 3));
          setWheelDamping(s.speed);
        }
      } catch {
        /* keep the default */
      }
    })();
  }, []);

  const commit = async (p: number) => {
    const want = p / 3;
    try {
      const s = await api<{ speed?: number }>("/api/scroll-speed", { json: { speed: want } });
      if (s?.speed) {
        setPos(Math.round(s.speed * 3));
        setWheelDamping(s.speed);
      }
      toast("Terminal scroll speed: " + fmt((s?.speed ?? want)) + " lines");
    } catch {
      toast("Scroll speed change failed");
    }
  };

  return (
    <label
      className="set-row"
      title="How many lines the mouse wheel scrolls in the terminal, in thirds of a line (0.33–3). Below 1, wheel input is damped so a notch scrolls less than a line. Applies immediately to all open terminals."
    >
      <span className="set-label">Terminal scroll speed</span>
      <span className="ss-row">
        <input
          type="range"
          id="scroll-speed"
          min={1}
          max={9}
          step={1}
          value={pos}
          onChange={(e) => setPos(parseInt(e.target.value, 10))}
          onMouseUp={() => commit(pos)}
          onTouchEnd={() => commit(pos)}
          onKeyUp={(e) => {
            if (e.key === "ArrowLeft" || e.key === "ArrowRight") commit(pos);
          }}
        />
        <span id="scroll-speed-val" className="ss-val">{fmt(pos / 3)}</span>
        <span className="set-hint">lines per wheel notch</span>
      </span>
    </label>
  );
}
