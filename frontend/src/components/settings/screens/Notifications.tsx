/** Settings → Notifications (partial 105 + section 21's wiring): the browser
 * notification opt-in (shared addon API, synced via "mf-notify-state"), the
 * optional ntfy phone-push channel (/api/notify/ntfy), and the per-rule on/off
 * list (POST /api/notify/rules/<id>) that governs BOTH channels. */

import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../../../api/client";
import { toast } from "../../../lib/toast";
import { relTime } from "../../../lib/format";
import { queryClient } from "../../../state/queries";
import { useNotifyConfig, type NotifyRule } from "../../../state/runs";
import { SECRET_MASK } from "../useSettings";
import type { ScreenProps } from "../SettingsDialog";
import { AdvancedFold } from "../settingsMode";
import { flashSaved } from "../savedCue";
import {
  AGENT_WORDS_LABEL,
  ANNOUNCE_TIP,
  ASK_POPUP_HINT,
  ASK_POPUP_LABEL,
  PROSE_QUESTIONS_HINT,
  PROSE_QUESTIONS_LABEL,
  agentWordsHint,
  groupRules,
  NEEDS_INPUT_EXTRA,
  ONLY_BELL_HINT,
  ruleCopy,
} from "./notifyCopy";
import { saveAskPopup, saveProseQuestions, useAskPopup, useProseQuestions } from "../../../lib/ask";

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

export function Notifications({ gotoScreen }: ScreenProps) {
  const [state, setState] = useState(notifState());
  // The rule switches come from the shared notify-config query (state/runs.ts)
  // the bell and the toasts already read.
  const config = useNotifyConfig();
  const rules = config.data ? config.data.rules || [] : null;
  const rulesError = config.isError && !config.data;
  // Phone push on/off for the "Where alerts go" block; the Ntfy form below
  // reports every save it makes, so the two never disagree.
  const [phoneOn, setPhoneOn] = useState<boolean | null>(null);
  useEffect(() => {
    let live = true;
    api<NtfyView>("/api/notify/ntfy")
      .then((v) => live && setPhoneOn(!!v.enabled))
      .catch(() => live && setPhoneOn(false));
    return () => {
      live = false;
    };
  }, []);

  useEffect(() => {
    const paint = () => setState(notifState());
    document.addEventListener("mf-notify-state", paint);
    return () => document.removeEventListener("mf-notify-state", paint);
  }, []);

  const usable = state === "on" || state === "off";
  const status =
    state === "unsupported"
      ? notifApi()?.unavailableReason ||
        (window.isSecureContext
          ? "This browser can't show notifications."
          : "Desktop alerts need a secure address (HTTPS or localhost).")
      : state === "blocked"
        ? "Blocked for this site. Allow notifications in your browser's site settings, then try again."
        : state === "on"
          ? "On. Alerts pop up on this computer, even when MindFlock is in the background."
          : "Off. Turn on to get pop-up alerts on this computer.";
  const grouped = rules ? groupRules(rules) : null;
  const moreOn = grouped ? grouped.more.filter((r) => r.enabled !== false).length : 0;

  // "Set up" presses: the Ntfy form saves a topic on the first one, so what
  // the person subscribes to is the topic MindFlock will send to.
  const [setupTick, setSetupTick] = useState(0);
  /** Open the phone fold, bring it into view, and make sure a topic exists. */
  const showPhone = () => {
    setSetupTick((n) => n + 1);
    const fold = document.getElementById("notif-advanced") as HTMLDetailsElement | null;
    if (!fold) return;
    fold.open = true;
    fold.scrollIntoView?.({ block: "start", behavior: "smooth" });
  };

  return (
    <>
      <h3 className="set-section-title">Where alerts go</h3>
      <div className="notif-channels" id="notif-channels">
        <div className="notif-channel" data-channel="bell">
          <span className="notif-rule-text">
            <span className="set-label">In MindFlock</span>
            <span className="set-hint notif-rule-desc">Always on: the bell in the top bar lists them.</span>
          </span>
          <span className="notif-channel-state on">On</span>
        </div>
        <AskPopupRow />
        <ProseQuestionsRow />
        <div
          className="notif-channel set-switch-row"
          id="notif-browser-row"
          data-channel="desktop"
          title="Pop-up alerts on this computer when an agent needs you, a pull request changes, or a budget is exceeded"
        >
          <span className="notif-rule-text">
            <span className="set-label" id="notif-browser-label">Desktop notifications</span>
            <span className="set-hint notif-rule-desc" id="notif-browser-status">{status}</span>
          </span>
          {/* label wraps only the switch, so clicking the row text no longer flips it */}
          <label className="ca-switch">
            <input
              type="checkbox"
              id="notif-browser-toggle"
              aria-labelledby="notif-browser-label"
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
        <div className="notif-channel" data-channel="phone" id="notif-phone-row">
          <span className="notif-rule-text">
            <span className="set-label">Phone alerts</span>
            <span className="set-hint notif-rule-desc">
              {phoneOn
                ? "On, through the free ntfy app. Works even with MindFlock closed."
                : "Only if you want to be pinged away from your desk. Uses the free ntfy app."}
            </span>
          </span>
          <button type="button" className="test-btn" id="notif-phone-setup" onClick={showPhone}>
            {phoneOn ? "Change" : "Set up"}
          </button>
        </div>
      </div>
      {state !== "on" && phoneOn === false && (
        <p className="set-hint notif-only-bell" id="notif-only-bell">
          {ONLY_BELL_HINT}
        </p>
      )}
      <p className="set-hint">
        Want to use MindFlock itself from your phone? That's{" "}
        <button type="button" className="linklike" data-goto-screen="mobile" onClick={() => gotoScreen("mobile")}>
          Your phone
        </button>{" "}
        (needs Tailscale).
      </p>
      <h3 className="set-section-title">What you're alerted about</h3>
      <div id="notif-rules-list">
        {rulesError ? (
          <p className="muted">Couldn't load the alert list.</p>
        ) : grouped === null ? (
          <p className="muted">Loading…</p>
        ) : !rules!.length ? (
          <p className="muted">No alerts to choose from.</p>
        ) : (
          <>
            {grouped.groups.map((g) => (
              <section className="notif-group" key={g.key} data-group={g.key}>
                <h4 className="notif-group-title">{g.title}</h4>
                {g.rules.map((rule) => (
                  <RuleRow key={rule.id} rule={rule} />
                ))}
              </section>
            ))}
            {grouped.more.length ? (
              <AdvancedFold
                id="notif-more-events"
                basic
                summary={`More alerts: team runs, hooks, Verify, zones (${moreOn} of ${grouped.more.length} on)`}
              >
                {grouped.more.map((rule) => (
                  <RuleRow key={rule.id} rule={rule} />
                ))}
              </AdvancedFold>
            ) : null}
          </>
        )}
      </div>
      {/* Phone push is a delivery channel, set up once by the few who want it:
          folded (in basic mode too — it is optional, not expert), opened
          from "Phone alerts → Set up" above. */}
      <AdvancedFold id="notif-advanced" summary="Phone alerts (ntfy)" basic>
        <Ntfy onChange={(v) => setPhoneOn(!!v.enabled)} setup={setupTick} />
      </AdvancedFold>
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
  /** Pocket mode: the agent's words in push bodies (default on). */
  agent_words?: boolean;
  configured?: boolean;
  active?: boolean;
  public_server?: boolean;
  subscribe_url?: string;
  qr_svg?: string | null;
  suggested_topic?: string;
  last?: { ts?: number; ok?: boolean; error?: string };
  note?: string;
}

/** Why "Send a test" can't run yet ("" when it can). */
export function ntfyTestBlocked(topic: string, testing: boolean): string {
  if (testing) return "";
  return topic.trim() ? "" : "Needs a topic first: press Generate.";
}

function Ntfy({ onChange, setup = 0 }: { onChange?(v: NtfyView): void; setup?: number } = {}) {
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

  const changeRef = useRef(onChange);
  changeRef.current = onChange;
  const apply = useCallback((v: NtfyView) => {
    setView(v);
    changeRef.current?.(v);
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
    async (patch: Record<string, unknown>, from?: Element | null) => {
      try {
        apply(await api<NtfyView>("/api/notify/ntfy", { json: patch }));
        if (from) flashSaved(from);
      } catch (err) {
        toast((err as Error).message || "Couldn't save the ntfy settings");
        load();
      }
    },
    [apply, load]
  );

  // "Set up" (the Phone alerts row): with no topic saved yet, generate one AND
  // save it, once per press. The topic used to be only a placeholder — a new
  // random one on every visit — so a person could subscribe to a name
  // MindFlock never sends to.
  const setupSeen = useRef(0);
  useEffect(() => {
    if (!setup || setup === setupSeen.current || !view) return;
    setupSeen.current = setup;
    if (view.topic || !view.suggested_topic) return;
    setTopic(view.suggested_topic);
    void save({ topic: view.suggested_topic });
  }, [setup, view, save]);

  if (failed)
    return (
      <>
        <p className="muted">Couldn't load the phone alert settings.</p>
      </>
    );
  if (!view) return <p className="muted">Loading…</p>;

  const on = !!view.enabled;
  const last = view.last || {};
  const testBlocked = ntfyTestBlocked(topic, testing);

  // No heading of its own: the fold's summary ("Phone alerts (ntfy)") is it.
  return (
    <>
      <p className="set-hint">
        Install the free{" "}
        <a href="https://ntfy.sh" target="_blank" rel="noopener noreferrer">
          ntfy
        </a>{" "}
        app, subscribe to the topic below, send a test, then turn this on. Works even with
        MindFlock closed.
      </p>
      <div
        className="set-row set-switch-row"
        id="ntfy-row"
        title="Push notifications to an ntfy topic your phone subscribes to"
      >
        <span className="set-label">Send alerts to my phone</span>
        <label className="ca-switch">
          <input
            type="checkbox"
            id="ntfy-toggle"
            checked={on}
            onChange={(e) =>
              save(
                {
                  enabled: e.target.checked,
                  // Send the typed-but-uncommitted topic too, so flipping the
                  // switch right after typing one doesn't fail validation.
                  topic: topic.trim(),
                  server: server.trim(),
                },
                e.currentTarget
              )
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
          // Never the suggested name as grey text: it changed on every visit
          // and was never saved. "Set up" / Generate save a real one.
          placeholder="press Generate"
          onChange={(e) => setTopic(e.target.value)}
          onBlur={(e) =>
            topic.trim() !== (view.topic || "") && save({ topic: topic.trim() }, e.currentTarget)
          }
          onKeyDown={(e) => e.key === "Enter" && (e.target as HTMLInputElement).blur()}
        />
        <button
          type="button"
          className="test-btn"
          id="ntfy-generate"
          title="Use a long random topic name — on the public server the topic name is the only thing keeping strangers out"
          onClick={(e) => {
            // The server supplies the random name (crypto-strength, and the
            // same charset ntfy accepts); every response carries a fresh one.
            const fresh = view.suggested_topic;
            if (!fresh) return;
            setTopic(fresh);
            save({ topic: fresh }, e.currentTarget);
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
          onBlur={(e) =>
            server.trim() !== (view.server || "") && save({ server: server.trim() }, e.currentTarget)
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
          onBlur={(e) => token && save({ token }, e.currentTarget)}
          onKeyDown={(e) => e.key === "Enter" && (e.target as HTMLInputElement).blur()}
        />
        {/* Blanking the field means "keep" (the app-wide secret convention), so
            without this there is no way back to having no token at all — and a
            wrong token is worse than none: ntfy 401s a bad credential on a
            topic that needs no credential. */}
        {view.has_token ? (
          <button
            type="button"
            className="test-btn"
            id="ntfy-token-clear"
            title="Remove the saved access token — most public topics need none"
            onClick={(e) => {
              setToken("");
              save({ clear_token: true }, e.currentTarget);
            }}
          >
            Clear
          </button>
        ) : (
          <span />
        )}
        <span className="set-hint">
          Only for a protected topic. Leave blank to keep the saved one.
        </span>
      </div>
      <div className="ntfy-field-row">
        <span className="set-label">Tapping opens</span>
        <input
          id="ntfy-click"
          value={click}
          autoComplete="off"
          spellCheck={false}
          placeholder="optional link"
          onChange={(e) => setClick(e.target.value)}
          onBlur={(e) =>
            click.trim() !== (view.click_url || "") &&
            save({ click_url: click.trim() }, e.currentTarget)
          }
          onKeyDown={(e) => e.key === "Enter" && (e.target as HTMLInputElement).blur()}
        />
        <span />
        <span className="set-hint">
          Opened when you tap an alert. Don't put an access token in it.
        </span>
      </div>
      {/* Pocket mode: the push body carries the agent's own words — a finished
          turn's spoken line, a dialog's question — so a phone that reads
          notifications aloud says something useful while it stays in a
          pocket. Saved through the same view as the fields above. */}
      <div
        className="set-row set-switch-row notif-rule"
        id="ntfy-agent-words-row"
        title="Put the agent's spoken line and a dialog's question in the push itself, so a phone that reads notifications aloud says something useful"
      >
        <span className="notif-rule-text">
          <span className="set-label" id="ntfy-agent-words-label">
            {AGENT_WORDS_LABEL}
          </span>
          <span className="set-hint notif-rule-desc" id="ntfy-agent-words-hint">
            {agentWordsHint(!!view.public_server)}
          </span>
        </span>
        <label className="ca-switch">
          <input
            type="checkbox"
            id="ntfy-agent-words"
            aria-labelledby="ntfy-agent-words-label"
            checked={view.agent_words !== false}
            onChange={(e) => save({ agent_words: e.target.checked }, e.currentTarget)}
          />
          <span className="ca-slider" />
        </label>
      </div>
      <p className="set-hint" id="ntfy-announce-tip">
        {ANNOUNCE_TIP}
      </p>
      <div className="ntfy-test-row">
        <button
          type="button"
          className="test-btn"
          id="ntfy-test"
          disabled={testing || !!testBlocked}
          title={testBlocked || undefined}
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
        {testBlocked && (
          <span className="set-hint" id="ntfy-test-why">
            {testBlocked}
          </span>
        )}
        {!verdict && !testBlocked && last.ts && (
          <span className={last.ok ? "ntfy-verdict-ok" : "ntfy-verdict-bad"}>
            {last.ok ? "Last push sent " + relTime(last.ts) : "Last push failed: " + last.error}
          </span>
        )}
      </div>
      {view.qr_svg && (
        <>
          {/* Server-generated segno SVG (trusted), same renderer as Settings → Your phone. */}
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

/** Whether a question in plain words counts at all (lib/ask.ts,
 * `notifications.prose_questions`, off by default), saved the same way. */
function ProseQuestionsRow() {
  const on = useProseQuestions();
  const [busy, setBusy] = useState(false);
  return (
    <div
      className="notif-channel set-switch-row"
      id="notif-prose-row"
      data-channel="prose"
      title="A reply that ends with a question in plain words counts as the session asking"
    >
      <span className="notif-rule-text">
        <span className="set-label" id="notif-prose-label">
          {PROSE_QUESTIONS_LABEL}
        </span>
        <span className="set-hint notif-rule-desc">{PROSE_QUESTIONS_HINT}</span>
      </span>
      <label className="ca-switch">
        <input
          type="checkbox"
          id="notif-prose-questions"
          aria-labelledby="notif-prose-label"
          checked={on}
          disabled={busy}
          onChange={async (e) => {
            const want = e.target.checked;
            const row = e.currentTarget;
            setBusy(true);
            try {
              await saveProseQuestions(want);
              if (!flashSaved(row)) toast(want ? "Questions in plain words count" : "Only real prompts count");
            } catch {
              toast("Save failed: questions in plain words");
            } finally {
              setBusy(false);
            }
          }}
        />
        <span className="ca-slider" />
      </label>
    </div>
  );
}

/** The ask pop-up's switch (lib/ask.ts, `notifications.ask_popup`): read
 * from and written to the notification settings view, like the ntfy fields. */
function AskPopupRow() {
  const on = useAskPopup();
  const [busy, setBusy] = useState(false);
  return (
    <div
      className="notif-channel set-switch-row"
      id="notif-ask-row"
      data-channel="popup"
      title="A session asking you something opens a card in the middle of the screen"
    >
      <span className="notif-rule-text">
        <span className="set-label" id="notif-ask-label">
          {ASK_POPUP_LABEL}
        </span>
        <span className="set-hint notif-rule-desc">{ASK_POPUP_HINT}</span>
      </span>
      <label className="ca-switch">
        <input
          type="checkbox"
          id="notif-ask-popup"
          aria-labelledby="notif-ask-label"
          checked={on}
          disabled={busy}
          onChange={async (e) => {
            const want = e.target.checked;
            const row = e.currentTarget;
            setBusy(true);
            try {
              await saveAskPopup(want);
              if (!flashSaved(row)) toast(want ? "Questions pop up" : "Questions stay in the bell");
            } catch {
              toast("Save failed: the question pop-up");
            } finally {
              setBusy(false);
            }
          }}
        />
        <span className="ca-slider" />
      </label>
    </div>
  );
}

function RuleRow({ rule }: { rule: NotifyRule }) {
  const [on, setOn] = useState(rule.enabled !== false);
  // Never the server's body: that is the notification's own template text
  // ("{detail}"), not a description (notifyCopy.ts).
  const { label, desc } = ruleCopy(rule);
  const labelId = "notif-rule-" + rule.id;
  return (
    <div className="set-row set-switch-row notif-rule" data-rule={rule.id}>
      <span className="notif-rule-text">
        <span className="set-label" id={labelId}>{label}</span>
        {desc && <span className="set-hint notif-rule-desc">{desc}</span>}
        {rule.id === "needs_input" && (
          <span className="set-hint notif-rule-desc notif-rule-extra" id="notif-needs-input-extra">
            {NEEDS_INPUT_EXTRA}
          </span>
        )}
      </span>
      {/* label wraps only the switch, so clicking the row text no longer flips it */}
      <label className="ca-switch">
        <input
          type="checkbox"
          aria-labelledby={labelId}
          checked={on}
          onChange={async (e) => {
            const want = e.target.checked;
            const row = e.currentTarget;
            setOn(want);
            try {
              await api(`/api/notify/rules/${encodeURIComponent(rule.id)}`, {
                json: { enabled: want },
              });
              notifApi()?.refreshRules?.();
              void queryClient.invalidateQueries({ queryKey: ["notify-config"] });
              // "✓ Saved" by the rule, like every other save in Settings.
              if (!flashSaved(row)) toast(want ? "Notify: " + label : "Muted: " + label);
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
