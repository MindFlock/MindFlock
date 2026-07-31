/** Settings → Notifications (partial 105 + section 21's wiring): the browser
 * notification opt-in (shared addon API, synced via "mf-notify-state"), the
 * optional ntfy phone-push channel (/api/notify/ntfy), and the per-rule on/off
 * list (POST /api/notify/rules/<id>) that governs BOTH channels. */

import { useCallback, useEffect, useState } from "react";
import { api } from "../../../api/client";
import { toast } from "../../../lib/toast";
import { SECRET_MASK } from "../useSettings";
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
      <p className="set-hint">
        Pick exactly which events notify you — turn off any you don't want. These switches
        apply to both channels — the browser above and phone push below.
      </p>
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
      <Ntfy />
    </>
  );
}

/* --------------------------------------------------------------------------
 * ntfy — the phone-push channel.
 *
 * Why it's a section of its own rather than another rule switch: it's a
 * *delivery channel*, not a trigger. The rule list above decides WHAT notifies
 * you; browser and ntfy decide WHERE. ntfy is the one that works with MindFlock
 * closed, since the push happens server-side. It renders last on purpose: the
 * triggers read first, then the two channels they feed.
 * ------------------------------------------------------------------------ */
interface NtfyView {
  enabled?: boolean;
  server?: string;
  server_default?: string;
  topic?: string;
  has_token?: boolean;
  click_url?: string;
  configured?: boolean;
  active?: boolean;
  public_server?: boolean;
  subscribe_url?: string;
  qr_svg?: string | null;
  suggested_topic?: string;
  last?: { ts?: number; ok?: boolean; error?: string };
  note?: string;
}

function ago(ts: number): string {
  const secs = Math.max(0, Math.round(Date.now() / 1000 - ts));
  if (secs < 60) return "just now";
  if (secs < 3600) return Math.round(secs / 60) + " min ago";
  if (secs < 86400) return Math.round(secs / 3600) + "h ago";
  return Math.round(secs / 86400) + "d ago";
}

function Ntfy() {
  const [view, setView] = useState<NtfyView | null>(null);
  const [failed, setFailed] = useState(false);
  // Text fields are uncontrolled between saves (same flow as SettingField):
  // seeded from the server view, committed on blur/Enter.
  const [server, setServer] = useState("");
  const [topic, setTopic] = useState("");
  const [token, setToken] = useState("");
  const [click, setClick] = useState("");
  const [testing, setTesting] = useState(false);
  const [verdict, setVerdict] = useState<{ ok: boolean; text: string } | null>(null);

  const apply = useCallback((v: NtfyView) => {
    setView(v);
    setServer(v.server || "");
    setTopic(v.topic || "");
    setClick(v.click_url || "");
    setToken(""); // never populated with the stored secret
    if (v.note) toast(v.note);
  }, []);

  const load = useCallback(async () => {
    try {
      apply(await api<NtfyView>("/api/notify/ntfy"));
      setFailed(false);
    } catch {
      setFailed(true);
    }
  }, [apply]);

  useEffect(() => {
    load();
  }, [load]);

  /** Persist a patch; on rejection show why and re-sync from the server. */
  const save = useCallback(
    async (patch: Record<string, unknown>) => {
      try {
        apply(await api<NtfyView>("/api/notify/ntfy", { json: patch }));
      } catch (err) {
        toast((err as Error).message || "Couldn't save the ntfy settings");
        load();
      }
    },
    [apply, load]
  );

  if (failed)
    return (
      <>
        <h3 className="set-section-title">Phone push (ntfy)</h3>
        <p className="muted">Couldn't load the ntfy settings.</p>
      </>
    );
  if (!view) return <h3 className="set-section-title">Phone push (ntfy)</h3>;

  const on = !!view.enabled;
  const last = view.last || {};

  return (
    <>
      <h3 className="set-section-title">Phone push (ntfy)</h3>
      <p className="set-hint">
        Get these alerts on your phone — even with MindFlock closed, because the server
        sends them, not the browser. Install the free{" "}
        <a href="https://ntfy.sh" target="_blank" rel="noopener noreferrer">
          ntfy
        </a>{" "}
        app, subscribe to the topic below, and check in on the flock from anywhere.
      </p>
      <div
        className="set-row set-switch-row"
        id="ntfy-row"
        title="Push notifications to an ntfy topic your phone subscribes to"
      >
        <span className="set-label">Push to ntfy</span>
        <label className="ca-switch">
          <input
            type="checkbox"
            id="ntfy-toggle"
            checked={on}
            onChange={(e) =>
              save({
                enabled: e.target.checked,
                // Send the typed-but-uncommitted topic too, so flipping the
                // switch right after typing one doesn't fail validation.
                topic: topic.trim(),
                server: server.trim(),
              })
            }
          />
          <span className="ca-slider" />
        </label>
      </div>
      <div className="ntfy-field-row">
        <span className="set-label">Topic</span>
        <input
          id="ntfy-topic"
          value={topic}
          autoComplete="off"
          spellCheck={false}
          placeholder={view.suggested_topic || "mindflock-…"}
          onChange={(e) => setTopic(e.target.value)}
          onBlur={() => topic.trim() !== (view.topic || "") && save({ topic: topic.trim() })}
          onKeyDown={(e) => e.key === "Enter" && (e.target as HTMLInputElement).blur()}
        />
        <button
          type="button"
          className="test-btn"
          id="ntfy-generate"
          title="Use a long random topic name — on the public server the topic name is the only thing keeping strangers out"
          onClick={() => {
            // The server supplies the random name (crypto-strength, and the
            // same charset ntfy accepts); every response carries a fresh one.
            const fresh = view.suggested_topic;
            if (!fresh) return;
            setTopic(fresh);
            save({ topic: fresh });
          }}
        >
          Generate
        </button>
      </div>
      <div className="ntfy-field-row">
        <span className="set-label">Server</span>
        <input
          id="ntfy-server"
          value={server}
          autoComplete="off"
          spellCheck={false}
          placeholder={view.server_default || "https://ntfy.sh"}
          onChange={(e) => setServer(e.target.value)}
          onBlur={() =>
            server.trim() !== (view.server || "") && save({ server: server.trim() })
          }
          onKeyDown={(e) => e.key === "Enter" && (e.target as HTMLInputElement).blur()}
        />
        <span />
        <span className="set-hint">Leave as-is for the public server, or point at your own.</span>
      </div>
      <div className="ntfy-field-row">
        <span className="set-label">Access token</span>
        <input
          id="ntfy-token"
          type="password"
          value={token}
          autoComplete="off"
          placeholder={view.has_token ? SECRET_MASK + " (saved)" : "only for a protected topic"}
          onChange={(e) => setToken(e.target.value)}
          onBlur={() => token && save({ token })}
          onKeyDown={(e) => e.key === "Enter" && (e.target as HTMLInputElement).blur()}
        />
        <span />
        <span className="set-hint">
          Needed only if your topic is access-protected (self-hosted, or a reserved ntfy.sh
          topic). Blank keeps the saved one.
        </span>
      </div>
      <div className="ntfy-field-row">
        <span className="set-label">Tapping opens</span>
        <input
          id="ntfy-click"
          value={click}
          autoComplete="off"
          spellCheck={false}
          placeholder="optional — e.g. your phone URL from Settings → Mobile"
          onChange={(e) => setClick(e.target.value)}
          onBlur={() =>
            click.trim() !== (view.click_url || "") && save({ click_url: click.trim() })
          }
          onKeyDown={(e) => e.key === "Enter" && (e.target as HTMLInputElement).blur()}
        />
        <span />
        <span className="set-hint">
          Opened when you tap the notification. Don't paste an access token into it — it
          would be stored on the ntfy server (MindFlock strips one if you do).
        </span>
      </div>
      <div className="ntfy-test-row">
        <button
          type="button"
          className="test-btn"
          id="ntfy-test"
          disabled={testing || !topic.trim()}
          onClick={async () => {
            setTesting(true);
            setVerdict(null);
            try {
              const r = await api<{ ok?: boolean; error?: string }>("/api/notify/ntfy/test", {
                json: {
                  server: server.trim(),
                  topic: topic.trim(),
                  token: token || undefined,
                  click_url: click.trim(),
                },
              });
              setVerdict(
                r?.ok
                  ? { ok: true, text: "Sent — check your phone." }
                  : { ok: false, text: r?.error || "The push failed." }
              );
            } catch (err) {
              setVerdict({ ok: false, text: (err as Error).message || "The push failed." });
            }
            setTesting(false);
          }}
        >
          {testing ? "Sending…" : "Send a test"}
        </button>
        {verdict && (
          <span
            id="ntfy-verdict"
            className={verdict.ok ? "ntfy-verdict-ok" : "ntfy-verdict-bad"}
          >
            {verdict.text}
          </span>
        )}
        {!verdict && last.ts && (
          <span className={last.ok ? "ntfy-verdict-ok" : "ntfy-verdict-bad"}>
            {last.ok ? "Last push sent " + ago(last.ts) : "Last push failed: " + last.error}
          </span>
        )}
      </div>
      {view.qr_svg && (
        <>
          {/* Server-generated segno SVG (trusted), same renderer as Settings → Mobile. */}
          <div id="ntfy-qr" className="qr-card" dangerouslySetInnerHTML={{ __html: view.qr_svg }} />
          <p className="set-hint">
            Scan this in the ntfy app to subscribe, or open{" "}
            <a href={view.subscribe_url} target="_blank" rel="noopener noreferrer">
              {view.subscribe_url}
            </a>
            .
          </p>
        </>
      )}
      {on && view.public_server && (
        <p className="ntfy-warning" id="ntfy-public-warning">
          You're using the public <code>ntfy.sh</code> server: your session titles travel
          through it, and <strong>anyone who knows the topic name can read them</strong> (or
          send you fakes). Keep a long random topic — or self-host ntfy and point the Server
          field at it.
        </p>
      )}
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
