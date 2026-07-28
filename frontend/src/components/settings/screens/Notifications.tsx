/** Settings → Notifications (partial 105 + section 21's wiring): the browser
 * notification opt-in (shared addon API, synced via "mf-notify-state") and
 * the per-rule on/off list (POST /api/notify/rules/<id>). */

import { useCallback, useEffect, useState } from "react";
import { api } from "../../../api/client";
import { toast } from "../../../lib/toast";
import type { ScreenProps } from "../SettingsDialog";

const NOTIFY_KEY = "mindflock.notify.enabled";

interface NotifApi {
  state?: () => string;
  enable?: () => Promise<void> | void;
  disable?: () => void;
  refreshRules?: () => void;
  unavailableReason?: string;
}

function notifApi(): NotifApi | null {
  const w = window as unknown as { mindflockAddons?: { notify?: NotifApi } };
  return w.mindflockAddons?.notify || null;
}

function notifState(): string {
  const a = notifApi();
  if (a && typeof a.state === "function") return a.state();
  if (!("Notification" in window)) return "unsupported";
  if (Notification.permission === "denied") return "blocked";
  try {
    return localStorage.getItem(NOTIFY_KEY) === "1" ? "on" : "off";
  } catch {
    return "off";
  }
}

interface Rule {
  id: string;
  label?: string;
  title?: string;
  event?: string;
  body?: string;
  enabled?: boolean;
}

export function Notifications(_: ScreenProps) {
  const [state, setState] = useState(notifState());
  const [rules, setRules] = useState<Rule[] | null>(null);
  const [rulesError, setRulesError] = useState(false);

  useEffect(() => {
    const paint = () => setState(notifState());
    document.addEventListener("mf-notify-state", paint);
    return () => document.removeEventListener("mf-notify-state", paint);
  }, []);

  const loadRules = useCallback(async () => {
    try {
      const r = await fetch("/api/notify/config");
      const list = (r.ok && (await r.json()).rules) || [];
      setRules(list);
      setRulesError(false);
    } catch {
      setRulesError(true);
    }
  }, []);

  useEffect(() => {
    loadRules();
  }, [loadRules]);

  const usable = state === "on" || state === "off";
  const status =
    state === "unsupported"
      ? notifApi()?.unavailableReason ||
        (window.isSecureContext
          ? "This browser has no notification support."
          : "Browser notifications need a secure origin (HTTPS or localhost).")
      : state === "blocked"
        ? "Blocked for this site — allow notifications in your browser's site settings, then toggle again."
        : state === "on"
          ? "On — you'll get a desktop alert when an agent needs you, a PR merges, or a budget is exceeded."
          : "Off — enable to get desktop alerts even when this tab is in the background.";

  return (
    <>
      <h3 className="set-section-title">Browser notifications</h3>
      <div
        className="set-row set-switch-row"
        id="notif-browser-row"
        title="Show a desktop/Chrome notification when an agent needs you, a PR merges, or a budget is exceeded"
      >
        <span className="set-label" id="notif-browser-label">Desktop / Chrome notifications</span>
        {/* label wraps only the switch, so clicking the row text no longer flips it */}
        <label className="ca-switch">
          <input
            type="checkbox"
            id="notif-browser-toggle"
            checked={state === "on"}
            disabled={!usable}
            onChange={async (e) => {
              const a = notifApi();
              if (!a || typeof a.enable !== "function") {
                setState(notifState());
                return;
              }
              if (e.target.checked) await a.enable();
              else a.disable?.();
              setState(notifState());
            }}
          />
          <span className="ca-slider" />
        </label>
      </div>
      <p className="set-hint" id="notif-browser-status">{status}</p>
      <h3 className="set-section-title">What triggers a notification</h3>
      <p className="set-hint">Pick exactly which events notify you — turn off any you don't want.</p>
      <div id="notif-rules-list">
        {rulesError ? (
          <p className="muted">Couldn't load notification rules.</p>
        ) : rules === null ? (
          <p className="muted">Loading…</p>
        ) : !rules.length ? (
          <p className="muted">No notification rules configured.</p>
        ) : (
          rules.map((rule) => (
            <RuleRow key={rule.id} rule={rule} />
          ))
        )}
      </div>
      <p className="set-hint">Also reachable from the bell in the sidebar header.</p>
    </>
  );
}

function RuleRow({ rule }: { rule: Rule }) {
  const [on, setOn] = useState(rule.enabled !== false);
  const label = rule.label || rule.title || rule.event || "event";
  return (
    <div className="set-row set-switch-row notif-rule">
      <span className="notif-rule-text">
        <span className="set-label">{label}</span>
        {rule.body && <span className="set-hint notif-rule-desc">{rule.body}</span>}
      </span>
      {/* label wraps only the switch, so clicking the row text no longer flips it */}
      <label className="ca-switch">
        <input
          type="checkbox"
          checked={on}
          onChange={async (e) => {
            const want = e.target.checked;
            setOn(want);
            try {
              await api(`/api/notify/rules/${encodeURIComponent(rule.id)}`, {
                json: { enabled: want },
              });
              notifApi()?.refreshRules?.();
              toast(want ? "Notify: " + label : "Muted: " + label);
            } catch {
              setOn(!want); // revert on failure
              toast("Save failed: notification rule");
            }
          }}
        />
        <span className="ca-slider" />
      </label>
    </div>
  );
}
