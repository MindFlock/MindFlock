/** Settings → General (partial 101 + the scroll-speed wiring from
 * section 20): the workers request and model (lib/workersRequest); in the
 * Advanced fold, the cost budget, what happens when usage
 * runs out or MindFlock restarts, idle sleep, the plan estimate and terminal
 * scroll speed; then, last, Help — the tours (replay any of them) and the
 * hints switch. One plain line per control; the long why sits in a "Learn
 * more" fold. The comfort switches (Reduce motion, Take a break, Idle flock)
 * live on Appearance — they are about how the app looks and feels. */

import { useEffect, useState } from "react";
import { api } from "../../../api/client";
import { setWheelDamping } from "../../../lib/terminals";
import { toast } from "../../../lib/toast";
import { useUi } from "../../../state/store";
import { SettingField, useSettings } from "../useSettings";
import type { ScreenProps } from "../SettingsDialog";
import { InfoTip } from "../../InfoTip";
import { AdvancedFold } from "../settingsMode";
import { flashSaved } from "../savedCue";
import { TOURS, TOUR_IDS, type TourId } from "../../../lib/tours";
import { replayTour } from "../../onboarding/replayTour";
import { replayPeopleTour } from "./peer/replayPeopleTour";
import {
  ASK_WORKERS_KEYS,
  DEFAULT_WORKERS_MODEL,
  DEFAULT_WORKERS_TEMPLATE,
  FLOCK_KEYS,
} from "../../../lib/workersRequest";
import "./General.css";

export function General(_: ScreenProps) {
  return (
    <>
      {/* Budgets, resuming, reviving and sleeping only mean something once
          sessions run, so they wait in a NAMED fold, closed until opened —
          the screen used to open on a bare "▼ Advanced" heading (the owner
          disliked that) — advanced mode only, like every power-user row on a
          basic screen (settingsMode.tsx); their keys are unchanged. */}
      {/* The one everyday knob leads: the terminal's wheel speed (basic mode
          shows it too). */}
      <ScrollSpeedRow />
      {/* Asking for MindFlock workers (lib/workersRequest): the words the
          Alt+G hotkey types, and Flock mode adds to every prompt. */}
      <WorkersRows />
      <AdvancedFold id="general-advanced" summary="Budgets, resume and sleep" defaultOpen={false}>
        <label className="set-row">
          <span className="set-label">Per-session budget (USD, 0 = off)</span>
          <SettingField group="general" field="session_budget_usd" type="number" placeholder="0" />
          <span className="set-hint">One alert when a session's estimated cost passes this.</span>
        </label>
        <ResumeOnUsageResetRow />
        <ReviveOnStartRow />
        <SleepIdleRow />
        <label className="set-row">
          <span className="set-label">Plan window budget (≈USD per window, 0 = off)</span>
          <SettingField group="general" field="window_budget_usd" type="number" placeholder="0" />
          <span className="set-hint">
            Powers the "% left" in the sidebar's Usage bar. An estimate, not billed dollars.
          </span>
        </label>
      </AdvancedFold>
      {/* Last, under Help: where to replay a tour is a help question, not the
          first thing General is for (the owner found it odd up top). Basic
          mode shows only this; the Settings tour's last step points here. */}
      <Tours />
    </>
  );
}

/** The workers request: a template (`{model}` becomes "Workers model") the
 * server fills in and serves as `/api/config` → `flock.request`. Empty means
 * the default (shown as the placeholder). Saved on blur like every General
 * field; the row marks "✓ Saved", a failure toasts and re-syncs. */
function WorkersRows() {
  const s = useSettings();
  const stored = s.get("general", "workers_request");
  const display = typeof stored === "string" ? stored : "";
  const [value, setValue] = useState(display);
  useEffect(() => setValue(display), [display]);
  return (
    <>
      <label className="set-row" id="general-workers">
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
          Added to a prompt by the hotkey ({ASK_WORKERS_KEYS}), or to every prompt while a session's Flock is on
          ({FLOCK_KEYS}). <code>{"{model}"}</code> becomes the model below; empty uses the default.
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
          Tell a session that ran out of usage to carry on once your usage resets.
          <InfoTip>
            An agent that runs out of usage waits on its limit screen, even after the window
            resets. With this on, MindFlock tells it to continue the moment usage returns.
            Sessions with something queued resume either way.
          </InfoTip>
        </span>
      </span>
      {/* label wraps only the switch, so clicking the row text no longer flips it */}
      <label className="ca-switch">
        <input
          type="checkbox"
          checked={on}
          onChange={(e) => {
            // saveField marks the row "✓ Saved" (savedCue.ts).
            s.saveField("general", "resume_on_usage_reset", e.target.checked);
          }}
        />
        <span className="ca-slider" />
      </label>
    </div>
  );
}

/** Revive after a restart: once the server has booted (past its quiet
 * window) it relaunches, two at a time, every session whose agent was running
 * when it went down — the Offline chip's Restart, for all of them
 * (backend/web/core/revive.py). The same switch covers an agent the system
 * killed for memory: it comes back once memory recovers, a few times an hour
 * at most. Unset reads as on. */
function ReviveOnStartRow() {
  const s = useSettings();
  const on = s.get("general", "revive_on_start") !== false;
  return (
    <div className="set-row set-switch-row" id="general-revive-on-start">
      <span className="notif-rule-text">
        <span className="set-label">Revive sessions after a restart</span>
        <span className="set-hint notif-rule-desc">
          Restart running agents on their own after MindFlock restarts, or after
          the system kills one for memory.
          <InfoTip>
            Without this, they wait as Offline until you click Restart. An agent killed
            for running out of memory comes back once memory recovers, at most 3 times an
            hour. Paused sessions and sessions out of usage are left alone.
          </InfoTip>
        </span>
      </span>
      {/* label wraps only the switch, so clicking the row text doesn't flip it */}
      <label className="ca-switch">
        <input
          type="checkbox"
          checked={on}
          onChange={(e) => {
            s.saveField("general", "revive_on_start", e.target.checked);
          }}
        />
        <span className="ca-slider" />
      </label>
    </div>
  );
}

/** Sleeping agents (backend/web/core/sleep.py): an agent whose turn ended
 * and that has sat idle, unwatched, with nothing queued or waiting for it
 * this many minutes has its CLI and MCP servers stopped to give their memory
 * back; opening it or sending it anything wakes it. Empty = 45, 0 = never. */
function SleepIdleRow() {
  return (
    <label
      className="set-row"
      id="general-sleep-idle"
      title="0 turns sleeping off. Empty = 45 minutes. While memory is low, idle agents sleep after 5 minutes."
    >
      <span className="set-label">Put idle agents to sleep after (minutes, 0 = never)</span>
      <SettingField group="general" field="sleep_idle_minutes" type="number" placeholder="45" />
      <span className="set-hint">
        An agent that finished its turn and sits unwatched, with nothing queued, stops its CLI and MCP
        servers to free their memory and shows Sleeping. Opening it or sending it anything wakes it, back
        in the same conversation. While memory is low this happens after 5 minutes.
      </span>
    </label>
  );
}

/** Help: one Replay per tour (lib/tours) — each opens its surface and walks
 * it — and the hints switch. Turning hints back on re-arms every hint the
 * user had dismissed; with hints off, no tour opens by itself either
 * (onboarding/useAutoTour), though every Replay still works. Keeps the id
 * `general-tours` (deep links, the search index) and carries the Settings
 * tour's `settings-tours` hook on the same element. */
function Tours() {
  const enabled = useUi((s) => s.hintsEnabled);
  const setHintsEnabled = useUi((s) => s.setHintsEnabled);
  return (
    <div className="onboarding-block general-help" id="general-tours" data-tour="settings-tours">
      <h3 className="set-section-title">Help</h3>
      <h4 className="set-subtitle">Tours</h4>
      {TOUR_IDS.map((id) => (
        <div className="set-row onboarding-action" key={id} data-tour-row={id}>
          <div className="onboarding-action-text">
            <span className="set-label">{id === "welcome" ? "Welcome tour" : TOURS[id].title}</span>
            <span className="set-hint">{TOUR_HINT[id]}</span>
          </div>
          <button type="button" className="test-btn" title={`Replay the ${TOURS[id].title} tour`} onClick={() => (id === "people" ? replayPeopleTour() : replayTour(id))}>
            Replay
          </button>
        </div>
      ))}
      <div
        className="set-row set-switch-row"
        title="Small inline 💡 tips that point out features around the app. Turn them back on any time to see the ones you dismissed again."
      >
        <span className="notif-rule-text">
          <span className="set-label">Show hints</span>
          <span className="set-hint notif-rule-desc">💡 tips around the app, and each tour the first time you open its menu.</span>
        </span>
        {/* label wraps only the switch, so clicking the row text no longer flips it */}
        <label className="ca-switch">
          <input
            type="checkbox"
            checked={enabled}
            onChange={(e) => {
              setHintsEnabled(e.target.checked);
              if (!flashSaved(e.currentTarget)) toast(e.target.checked ? "Hints re-enabled" : "Hints turned off");
            }}
          />
          <span className="ca-slider" />
        </label>
      </div>
    </div>
  );
}

/** One line per tour: what it walks you through. */
const TOUR_HINT: Record<TourId, string> = {
  welcome: "The three steps to your first session.",
  new: "Starting a session: the task, the repo, Start.",
  intake: "Turning tickets, pull requests and issues into sessions.",
  verify: "Checking that a change actually works.",
  schedule: "Templates, and sessions that start by themselves.",
  settings: "Where your connections and every setting live.",
  people: "Working with another person: invite, share a folder, a joint task.",
  voice: "Talking to sessions: the mic, the words that send and stop, the wake word.",
};

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
