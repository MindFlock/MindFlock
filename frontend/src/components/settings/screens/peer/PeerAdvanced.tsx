/** People → Advanced: the peer-links switch, your name, how people on other
 * networks reach you (relay / port / address), what the shared agent may
 * reach, and the status line. Split out of PeerLinks.tsx unchanged. */

import { useState } from "react";
import { copyText } from "../../../../lib/clipboard";
import { toast } from "../../../../lib/toast";
import { InlineConfirm, SettingField } from "../../useSettings";
import type { PeerStatus } from "../../../../lib/peer";

const RELAY_OPTIONS = [
  { value: "auto", label: "Automatic — through Cloudflare when cloudflared is installed" },
  { value: "off", label: "Direct only — they connect to this computer (same network or Tailscale)" },
  { value: "cloudflare", label: "Always through Cloudflare (needs cloudflared)" },
  { value: "url", label: "My own HTTPS relay (relay URL below)" },
];

export function PeerAdvanced({ on, onToggle, st }: { on: boolean; onToggle(on: boolean): void; st: PeerStatus | null }) {
  const relayAddr = st?.relay?.address || "";
  // Turning it off drops every connection and stops shared sessions: ask
  // first, naming what stops, whenever there is anything to stop.
  const [asking, setAsking] = useState(false);
  const people = st?.links.length || 0;
  const shares = st?.links.filter((l) => l.shared).length || 0;
  return (
    <details className="peer-advanced" id="peer-advanced">
      <summary>Advanced</summary>
      <div className="set-row set-switch-row">
        <span className="set-label">Connections to people</span>
        <label className="ca-switch">
          <input
            type="checkbox"
            id="peer-enabled"
            checked={on}
            onChange={(e) => {
              if (!e.target.checked && people > 0) setAsking(true);
              else onToggle(e.target.checked);
            }}
          />
          <span className="ca-slider" />
        </label>
        <span className="set-hint">
          Turned on by your first invite or join. Off: nobody can connect, and this computer connects to
          nobody.
        </span>
      </div>
      {asking && (
        <InlineConfirm
          title="Turn off connections to people?"
          body={
            <span>
              You disconnect from {people === 1 ? "1 person" : people + " people"}
              {shares ? ` and ${shares === 1 ? "1 shared session stops" : shares + " shared sessions stop"}` : ""} until you turn
              it back on.
            </span>
          }
          confirmLabel="Turn off"
          onCancel={() => setAsking(false)}
          onConfirm={() => {
            setAsking(false);
            onToggle(false);
          }}
        />
      )}
      <label className="set-row">
        <span className="set-label">Your name</span>
        <SettingField group="peer" field="display_name" placeholder="this computer's name" />
        <span className="set-hint">What the other person sees.</span>
      </label>
      <label className="set-row">
        <span className="set-label">How they reach you</span>
        <SettingField group="peer" field="relay" options={RELAY_OPTIONS} />
        <span className="set-hint">
          How people on other networks reach your invites. Through a relay the connection stays
          end-to-end encrypted: the relay can block it but never read or change it. The other person
          needs nothing extra.
        </span>
      </label>
      {relayAddr && (
        <div className="set-row" id="peer-relay-address">
          <span className="set-label">Relay address</span>
          <code className="peer-addr">{relayAddr}</code>
          <button
            type="button"
            className="test-btn"
            onClick={() => copyText(relayAddr).then((ok) => toast(ok ? "Relay address copied" : "Copy failed"))}
          >
            Copy
          </button>
          <span className="set-hint">
            Someone who joined you through an older address can paste this into Reconnect on their side.
          </span>
        </div>
      )}
      <label className="set-row">
        <span className="set-label">Port</span>
        <SettingField group="peer" field="listen_port" placeholder="8799" />
        <span className="set-hint">
          Same-network invites only: the other person connects to this port, so it must be reachable
          from their machine (Tailscale recommended).
        </span>
      </label>
      <label className="set-row">
        <span className="set-label">Address in invites</span>
        <SettingField group="peer" field="advertise_host" placeholder="auto: Tailscale IP, else LAN IP" />
        <span className="set-hint">Same-network invites only: the address written into the code.</span>
      </label>
      <label className="set-row">
        <span className="set-label">Relay URL</span>
        <SettingField group="peer" field="relay_url" placeholder="wss://peer.example.com/mindflock" />
        <span className="set-hint">"My own HTTPS relay" only: forwards (path unchanged) to the relay port.</span>
      </label>
      <label className="set-row">
        <span className="set-label">Relay port</span>
        <SettingField group="peer" field="relay_port" placeholder="auto" />
        <span className="set-hint">Port on this computer your relay forwards to (blank = any free port).</span>
      </label>
      <label className="set-row">
        <span className="set-label">Extra hosts the shared agent may reach</span>
        <SettingField group="peer" field="egress_allow" placeholder="e.g. pypi.org, .github.com" />
        <span className="set-hint">
          Websites the sandboxed agent may reach (HTTPS), besides its own AI service. A leading dot
          allows subdomains.
        </span>
      </label>
      {st && (
        <p className="set-hint" id="peer-status">
          Sandbox:{" "}
          {st.sandbox.available ? <strong>ready</strong> : <span className="error">not available here</span>}
          {st.fingerprint && (
            <>
              {" · "}this computer's key <code>{st.fingerprint}</code>
            </>
          )}
          {" · "}direct connections {st.listen.listening ? "open" : "closed"} ({st.listen.host}:{st.listen.port})
          {st.relay && st.relay.mode !== "off" && (
            <>
              {" · "}relay <span id="peer-relay-state">{st.relay.running ? "up" : "down"}</span>
              {st.relay.public_host && (
                <>
                  {" "}at <code>{st.relay.public_host}</code>
                </>
              )}
              {st.relay.error && <span className="error"> — {st.relay.error}</span>}
            </>
          )}
        </p>
      )}
    </details>
  );
}
