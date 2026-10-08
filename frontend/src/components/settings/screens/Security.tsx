/** Settings → Security (partial 114 + section 21's auth wiring): the
 * access-token gate, token reveal/copy/rotate, trusted Tailscale accounts,
 * remote control. (Settings sync lives in Settings → Devices.) */

import { useEffect, useState } from "react";
import { api } from "../../../api/client";
import { copyText } from "../../../lib/clipboard";
import { toast } from "../../../lib/toast";
import { InlineConfirm, useSettings } from "../useSettings";
import type { ScreenProps } from "../SettingsDialog";

const AUTH_TOKEN_MASK = "••••••••••••••••";

let authTokenCache: string | null = null;
async function fetchAuthToken(): Promise<string> {
  if (authTokenCache === null)
    authTokenCache = ((await api<{ token?: string }>("/api/settings/auth-token")) || {}).token || "";
  return authTokenCache;
}

/** GET /api/settings/tailnet-trust (backend.web.core.tailnet_trust.status). */
interface TailnetTrust {
  available: boolean;
  self_login: string;
  self_tagged: boolean;
  logins: string[];
  shared_link_supported: boolean;
}

/** "Trusted Tailscale accounts": one checkbox per login that owns an untagged
 * device on this tailnet — a checked login's own devices skip the token. */
function TailnetTrustRows() {
  const s = useSettings();
  const [info, setInfo] = useState<TailnetTrust | null>(null);
  const stored = s.get("general", "tailnet_trusted_logins");
  const trusted = Array.isArray(stored) ? (stored as string[]) : [];

  useEffect(() => {
    let live = true;
    api<TailnetTrust>("/api/settings/tailnet-trust")
      .then((r) => live && setInfo(r || null))
      .catch(() => live && setInfo(null));
    return () => {
      live = false;
    };
  }, []);

  // A trusted login that no longer owns a device here still shows, so it can
  // be unticked.
  const choices = [...new Set([...(info?.logins || []), ...trusted])].sort();
  const toggle = (login: string, on: boolean) => {
    const next = new Set(trusted);
    if (on) next.add(login);
    else next.delete(login);
    s.saveField("general", "tailnet_trusted_logins", [...next]);
  };

  return (
    <div
      className="set-row"
      id="tailnet-trust-row"
      title="Requests from these Tailscale accounts' own (untagged) devices need no access token."
    >
      <span className="set-label">Trusted Tailscale accounts</span>
      {info === null ? (
        <span className="muted">Checking Tailscale…</span>
      ) : !info.available && !choices.length ? (
        <span className="muted" id="tailnet-trust-unavailable">
          Tailscale isn't running on this device.
        </span>
      ) : (
        <div className="tailnet-trust-logins">
          {choices.map((login) => (
            <label className="check" key={login}>
              <input
                type="checkbox"
                data-login={login}
                checked={trusted.includes(login)}
                onChange={(e) => toggle(login, e.target.checked)}
              />
              {login}
              {login === info.self_login ? <span className="muted"> (this device's owner)</span> : null}
            </label>
          ))}
        </div>
      )}
      <span className="set-hint" id="tailnet-trust-hint">
        Your own phone and laptops, signed in to Tailscale as a ticked account, open MindFlock here
        without the access token. Tagged devices and devices shared in from other accounts still
        need it.
        {info && !info.shared_link_supported
          ? " On this OS that only covers this device's own address — requests through the shared phone link still ask for the token."
          : ""}
      </span>
    </div>
  );
}

export function Security(_: ScreenProps) {
  const s = useSettings();
  const [shown, setShown] = useState(false);
  const [tokenText, setTokenText] = useState(AUTH_TOKEN_MASK);
  const authMode = String(s.get("general", "auth_mode") ?? "auto") || "auto";
  const remote = String(s.get("general", "remote_control") ?? "");
  // The two guarded actions ask inline (see InlineConfirm for why not a native
  // confirm). Nothing is saved or sent until the go-ahead: picking "off" leaves
  // the select on the stored mode, so Cancel has nothing to undo.
  const [confirmOff, setConfirmOff] = useState(false);
  const [confirmRotate, setConfirmRotate] = useState(false);
  const [rotating, setRotating] = useState(false);

  const setAuthMode = (value: string) => {
    // Warn before turning the gate fully off.
    if (value === "off" && authMode !== "off") {
      setConfirmOff(true);
      return;
    }
    setConfirmOff(false);
    s.saveField("general", "auth_mode", value);
  };

  const rotate = async () => {
    // Compromise recovery: this browser's cookie is re-issued in the
    // same response, so only OTHER devices get signed out.
    setRotating(true);
    try {
      const r = await api<{ token?: string }>("/api/settings/auth-token/rotate", {
        method: "POST",
      });
      authTokenCache = r?.token || null;
      if (shown) setTokenText(authTokenCache || "(none set)");
      toast("Access token regenerated — other devices must sign in again");
    } catch (e) {
      toast("Couldn't regenerate the token: " + (e as Error).message);
    } finally {
      setRotating(false);
      setConfirmRotate(false);
    }
  };

  return (
    <>
      <h3 className="set-section-title">Access token</h3>
      <label
        className="set-row"
        title="Whether opening MindFlock in a browser requires this device's access token (shown below, or `mindflock token`)."
      >
        <span className="set-label">Require access token</span>
        <select
          data-group="general"
          data-field="auth_mode"
          id="auth-mode-select"
          value={authMode}
          onChange={(e) => setAuthMode(e.target.value)}
        >
          <option value="auto">Auto — only when exposed beyond localhost (default)</option>
          <option value="on">Always on — always require the token</option>
          <option value="off">Always off — never require a token</option>
        </select>
        <span className="set-hint" id="auth-mode-hint">
          The token guards a server reachable over your tailnet/LAN (it can drive agents and
          commit code). "Off" removes that gate entirely.
        </span>
      </label>
      {confirmOff && (
        <InlineConfirm
          id="auth-mode-confirm"
          title="Turn the access-token gate off?"
          body={
            "Anyone who can reach this server's URL (e.g. on your tailnet/LAN) will be able " +
            "to drive your agents and commit code with no sign-in. Only do this on a network " +
            "you fully trust."
          }
          confirmLabel="Turn it off"
          onConfirm={() => {
            setConfirmOff(false);
            s.saveField("general", "auth_mode", "off");
          }}
          onCancel={() => setConfirmOff(false)}
        />
      )}
      <div
        className="set-row"
        title="The token another MindFlock device enters to control this one, and the browser sign-in token."
      >
        <span className="set-label">This device's access token</span>
        <div className="token-reveal">
          <code id="auth-token-value">{shown ? tokenText : AUTH_TOKEN_MASK}</code>
          <button
            type="button"
            className="test-btn"
            id="auth-token-toggle"
            onClick={async () => {
              if (shown) {
                setShown(false);
                return;
              }
              try {
                setTokenText((await fetchAuthToken()) || "(none set)");
                setShown(true);
              } catch (e) {
                toast("Couldn't load the access token: " + (e as Error).message);
              }
            }}
          >
            {shown ? "Hide" : "Show"}
          </button>
          <button
            type="button"
            className="test-btn"
            id="auth-token-copy"
            onClick={async () => {
              try {
                const t = await fetchAuthToken();
                if (!t) {
                  toast("No access token is set");
                  return;
                }
                const ok = await copyText(t);
                toast(ok ? "Access token copied" : "Copy failed — use Show and copy manually");
              } catch (e) {
                toast("Couldn't load the access token: " + (e as Error).message);
              }
            }}
          >
            Copy
          </button>
          <button
            type="button"
            className="test-btn"
            id="auth-token-rotate"
            disabled={confirmRotate}
            onClick={() => setConfirmRotate(true)}
          >
            Regenerate
          </button>
        </div>
        {confirmRotate && (
          <InlineConfirm
            id="auth-token-rotate-confirm"
            title="Regenerate the access token?"
            body={
              "Every other signed-in browser, phone QR code, and paired MindFlock device " +
              "stops working until it re-authenticates with the new token. This browser " +
              "stays signed in."
            }
            confirmLabel={rotating ? "Regenerating…" : "Regenerate"}
            busy={rotating}
            onConfirm={() => void rotate()}
            onCancel={() => setConfirmRotate(false)}
          />
        )}
        <span className="set-hint">
          Enter this on another MindFlock device (its sidebar's "Connect…" button next to this
          device's name) to let it control this one, or at the browser sign-in page when the
          token gate is on. Regenerate if the token may have leaked — every signed-in device,
          QR code, and paired device must then re-authenticate with the new token.
        </span>
      </div>
      <TailnetTrustRows />
      <h3 className="set-section-title">Remote control</h3>
      <label
        className="set-row"
        title="Whether other MindFlock devices on your tailnet may list and drive this device's sessions."
      >
        <span className="set-label">Allow remote control</span>
        <select
          data-group="general"
          data-field="remote_control"
          value={remote}
          onChange={(e) => s.saveField("general", "remote_control", e.target.value)}
        >
          <option value="">Off (default) — other devices cannot control this one</option>
          <option value="on">On — devices with this device's access token can control it</option>
        </select>
        <span className="set-hint">
          Lets another MindFlock on your Tailscale network show this device's sessions in its
          sidebar and drive them (terminal, prompts, commits). The controlling device still
          needs this device's access token.
        </span>
      </label>
      {/* Settings sync moved to Settings → Devices: it only runs between your
          own devices, so it sits next to adding them. */}
    </>
  );
}
