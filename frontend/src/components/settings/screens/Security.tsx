/** Settings → Security (partial 114 + section 21's auth wiring): the
 * access-token gate, token reveal/copy/rotate, remote control. */

import { useState } from "react";
import { api } from "../../../api/client";
import { copyText } from "../../../lib/clipboard";
import { toast } from "../../../lib/toast";
import { useSettings } from "../useSettings";
import type { ScreenProps } from "../SettingsDialog";

const AUTH_TOKEN_MASK = "••••••••••••••••";

let authTokenCache: string | null = null;
async function fetchAuthToken(): Promise<string> {
  if (authTokenCache === null)
    authTokenCache = ((await api<{ token?: string }>("/api/settings/auth-token")) || {}).token || "";
  return authTokenCache;
}

export function Security(_: ScreenProps) {
  const s = useSettings();
  const [shown, setShown] = useState(false);
  const [tokenText, setTokenText] = useState(AUTH_TOKEN_MASK);
  const authMode = String(s.get("general", "auth_mode") ?? "auto") || "auto";
  const remote = String(s.get("general", "remote_control") ?? "");

  const setAuthMode = (value: string) => {
    // Warn before turning the gate fully off.
    if (value === "off") {
      const ok = confirm(
        "Turn the access-token gate OFF?\n\nAnyone who can reach this server's URL " +
          "(e.g. on your tailnet/LAN) will be able to drive your agents and commit code " +
          "with no sign-in. Only do this on a network you fully trust."
      );
      if (!ok) {
        s.saveField("general", "auth_mode", "auto");
        return;
      }
    }
    s.saveField("general", "auth_mode", value);
  };

  return (
    <>
      <h3 className="set-section-title">Access token</h3>
      <label
        className="set-row"
        title="Whether opening MindFlock in a browser requires the access token from the startup banner."
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
            onClick={async () => {
              // Compromise recovery: this browser's cookie is re-issued in the
              // same response, so only OTHER devices get signed out.
              const ok = confirm(
                "Regenerate the access token?\n\nEvery other signed-in browser, phone QR code, " +
                  "and paired MindFlock device stops working until it re-authenticates with the " +
                  "new token. This browser stays signed in."
              );
              if (!ok) return;
              try {
                const r = await api<{ token?: string }>("/api/settings/auth-token/rotate", {
                  method: "POST",
                });
                authTokenCache = r?.token || null;
                if (shown) setTokenText(authTokenCache || "(none set)");
                toast("Access token regenerated — other devices must sign in again");
              } catch (e) {
                toast("Couldn't regenerate the token: " + (e as Error).message);
              }
            }}
          >
            Regenerate
          </button>
        </div>
        <span className="set-hint">
          Enter this on another MindFlock device (its sidebar's "Connect…" button next to this
          device's name) to let it control this one, or at the browser sign-in page when the
          token gate is on. Regenerate if the token may have leaked — every signed-in device,
          QR code, and paired device must then re-authenticate with the new token.
        </span>
      </div>
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
    </>
  );
}
