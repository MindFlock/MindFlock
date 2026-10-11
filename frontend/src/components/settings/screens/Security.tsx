/** Settings → Security: the access token (show / copy / regenerate) and the
 * gate that asks for it — nothing else. Trusting another machine lives on
 * Settings → Your devices (pair by code, Make reachable, and under Advanced
 * the trusted Tailscale accounts, Allow remote control and pair-by-token that
 * used to sit here); the pointer line below says so. Settings sync lives
 * there too. */

import { useState } from "react";
import { api } from "../../../api/client";
import { copyText } from "../../../lib/clipboard";
import { rotatedToast } from "../../../lib/fleet";
import { toast } from "../../../lib/toast";
import { InlineConfirm, useSettings } from "../useSettings";
import type { ScreenProps } from "../SettingsDialog";

const AUTH_TOKEN_MASK = "••••••••••••••••";

let authTokenCache: string | null = null;
/** Why the server didn't hand the token over (it only does to this machine
 * or a caller holding it — not to one signed in with the devices' key). */
let authTokenWithheld = "";
async function fetchAuthToken(): Promise<string> {
  if (authTokenCache === null) {
    const r = (await api<{ token?: string | null; reason?: string }>("/api/settings/auth-token")) || {};
    authTokenCache = r.token || "";
    authTokenWithheld = r.token == null && r.reason ? r.reason : "";
  }
  return authTokenCache;
}

export function Security(p: ScreenProps) {
  const s = useSettings();
  const [shown, setShown] = useState(false);
  const [tokenText, setTokenText] = useState(AUTH_TOKEN_MASK);
  const authMode = String(s.get("general", "auth_mode") ?? "auto") || "auto";
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
      const r = await api<{ token?: string | null; rekeyed?: string[]; missed?: string[]; fleet_error?: string }>(
        "/api/settings/auth-token/rotate",
        { method: "POST" }
      );
      // A caller that isn't this machine (or doesn't hold the old token) is
      // not handed the new one: re-read it on the next Show.
      authTokenCache = r?.token || null;
      if (shown) setTokenText(authTokenCache || "(regenerated — open Security on this computer to see it)");
      toast(rotatedToast(r), { duration: 10000 });
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
        id="auth-token-row"
        data-control="access-token"
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
                const t = await fetchAuthToken();
                setTokenText(t || (authTokenWithheld ? "(hidden: " + authTokenWithheld + ")" : "(none set)"));
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
                  toast(
                    authTokenWithheld
                      ? "The token isn't shown here (" + authTokenWithheld + ") — copy it on this computer"
                      : "No access token is set"
                  );
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
              "Every other signed-in browser and token-paired MindFlock stops working until it " +
              "signs in with the new token, and your phone must scan the QR again. If this " +
              "computer is one of your devices, their shared device key is replaced too — they " +
              "get the new one on their own (one that's offline, when it's back). This browser " +
              "stays signed in."
            }
            confirmLabel={rotating ? "Regenerating…" : "Regenerate"}
            busy={rotating}
            onConfirm={() => void rotate()}
            onCancel={() => setConfirmRotate(false)}
          />
        )}
        <span className="set-hint">
          Enter this at the browser sign-in page when the token gate is on, or on another
          MindFlock to let it control this one (Your devices → Advanced → Pair by pasted token,
          there). Regenerate if the token may have leaked — every signed-in browser and
          token-paired device must then sign in with the new token, your phone must scan the QR
          again, and your devices move to a new shared key.
        </span>
      </div>
      <p className="set-hint set-block-hint" id="security-devices-pointer">
        Trusting your own devices lives in{" "}
        <button type="button" className="linklike" id="security-open-devices" onClick={() => p.gotoScreen("devices")}>
          Your devices
        </button>
        : pair by code, Make reachable, settings sync, and under Advanced the trusted Tailscale
        accounts, Allow remote control and pairing by pasted token.
      </p>
    </>
  );
}
