/** Settings → Peer links (docs/peer-link.md): pair this MindFlock with another
 * person's using a one-time code, then bind ONE shared folder per link — an
 * agent runs in it inside a bubblewrap sandbox and talks to the peer's agent.
 *
 * Everything goes through /api/peer*. The invite code is shown only in the
 * response that created it (the server never lists it again); links show
 * their SAS so both people can compare it out of band. */

import { useCallback, useEffect, useState } from "react";
import { api } from "../../../api/client";
import { copyText } from "../../../lib/clipboard";
import { toast } from "../../../lib/toast";
import { SettingField, useSettings, InlineConfirm } from "../useSettings";
import type { ScreenProps } from "../SettingsDialog";

interface PeerLink {
  link_id: string;
  peer_name: string;
  role: string;
  peer_addr?: string;
  sas: string;
  perms: { messages: boolean; diff: boolean; read_file: boolean };
  shared: boolean;
  session_title?: string | null;
  connected: boolean;
}

interface PeerStatus {
  enabled: boolean;
  display_name: string;
  fingerprint: string | null;
  sandbox: { available: boolean; reason: string };
  listen: { host: string; port: number; listening: boolean };
  relay?: {
    mode: string;
    running: boolean;
    public_host: string | null;
    address: string | null;
    error: string | null;
    cloudflared?: boolean;
  };
  links: PeerLink[];
  invites: Array<{ invite_id: string; expires_in: number | null }>;
}

interface Invite {
  invite_id: string;
  code: string;
  expires_in: number;
  host: string;
  port: number;
  relay?: string;
}

const RELAY_OPTIONS = [
  { value: "off", label: "Off — peers dial me directly (Tailscale / LAN)" },
  { value: "cloudflare", label: "Cloudflare quick tunnel (needs cloudflared)" },
  { value: "url", label: "My own HTTPS relay (relay URL below)" },
];

const PERM_LABELS: Array<[keyof PeerLink["perms"], string]> = [
  ["messages", "send messages"],
  ["diff", "see my diff"],
  ["read_file", "read my files"],
];

function peerErrText(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

export function PeerLinks(_: ScreenProps) {
  const s = useSettings();
  const stored = s.get("peer", "enabled");
  const on = stored === true || stored === "true";
  const [st, setSt] = useState<PeerStatus | null>(null);
  const [error, setError] = useState("");
  const [invite, setInvite] = useState<Invite | null>(null);
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      setSt(await api<PeerStatus>("/api/peer"));
      setError("");
    } catch (e) {
      setError(peerErrText(e));
    }
  }, []);

  useEffect(() => {
    load();
  }, [load, on]);

  const run = async (fn: () => Promise<unknown>, ok?: string) => {
    setBusy(true);
    try {
      await fn();
      if (ok) toast(ok);
    } catch (e) {
      toast(peerErrText(e));
    }
    setBusy(false);
    load();
  };

  const createInvite = () =>
    run(async () => {
      setInvite(await api<Invite>("/api/peer/invites", { json: {} }));
    });

  const join = () =>
    run(async () => {
      const link = await api<PeerLink>("/api/peer/join", { json: { code: code.trim() } });
      setCode("");
      toast(`Paired with ${link.peer_name} — compare the SAS ${link.sas} with them`);
    });

  return (
    <>
      <h3 className="set-section-title">Peer links</h3>
      <p className="set-hint set-block-hint">
        Pair-code with another MindFlock user. You pair once with a one-time code, then each of
        you shares <strong>one folder</strong>: an agent works in it inside a sandbox and talks
        to the other person's agent. Only that folder is ever exposed — see{" "}
        <code>docs/peer-link.md</code>.
      </p>
      <div className="set-row set-switch-row">
        <span className="set-label">Peer links</span>
        <label className="ca-switch">
          <input
            type="checkbox"
            id="peer-enabled"
            checked={on}
            onChange={(e) => s.saveField("peer", "enabled", e.target.checked)}
          />
          <span className="ca-slider" />
        </label>
      </div>
      <label className="set-row">
        <span className="set-label">Your name</span>
        <SettingField group="peer" field="display_name" placeholder="this computer's name" />
        <span className="set-hint">What your peer sees.</span>
      </label>
      <label className="set-row">
        <span className="set-label">Listen port</span>
        <SettingField group="peer" field="listen_port" placeholder="8799" />
        <span className="set-hint">
          When you invite, your peer dials this port — it must be reachable from their machine
          (Tailscale recommended).
        </span>
      </label>
      <label className="set-row">
        <span className="set-label">Advertise address</span>
        <SettingField group="peer" field="advertise_host" placeholder="auto: Tailscale IP, else LAN IP" />
        <span className="set-hint">The address written into your invite codes — the one your peer dials.</span>
      </label>
      <label className="set-row">
        <span className="set-label">Relay</span>
        <SettingField group="peer" field="relay" options={RELAY_OPTIONS} />
        <span className="set-hint">
          For peers on another network. Your invites then go through a public relay, carrying
          the same end-to-end, key-pinned encryption: the relay can block the connection but
          never read or change it. Your peer needs nothing extra.
        </span>
      </label>
      <label className="set-row">
        <span className="set-label">Relay URL</span>
        <SettingField group="peer" field="relay_url" placeholder="wss://peer.example.com/mindflock" />
        <span className="set-hint">Relay "My own HTTPS relay" only: forwards (path unchanged) to the relay port.</span>
      </label>
      <label className="set-row">
        <span className="set-label">Relay port</span>
        <SettingField group="peer" field="relay_port" placeholder="auto" />
        <span className="set-hint">Loopback port your relay forwards to (blank = any free port).</span>
      </label>
      <label className="set-row">
        <span className="set-label">Extra egress hosts</span>
        <SettingField group="peer" field="egress_allow" placeholder="e.g. pypi.org, .github.com" />
        <span className="set-hint">
          Hosts the sandboxed agent may reach on 443, besides its own API. A leading dot allows
          subdomains.
        </span>
      </label>

      {error && <p className="error">{error}</p>}
      {on && st && (
        <div id="peer-body">
          <p className="set-hint">
            Sandbox:{" "}
            {st.sandbox.available ? (
              <strong>ready</strong>
            ) : (
              <span className="error">unavailable — {st.sandbox.reason}</span>
            )}
            {st.fingerprint && (
              <>
                {" · "}identity <code>{st.fingerprint}</code>
              </>
            )}
            {" · "}listener {st.listen.listening ? "on" : "off"} ({st.listen.host}:{st.listen.port})
            {st.relay && st.relay.mode !== "off" && (
              <>
                {" · "}relay <span id="peer-relay-state">{st.relay.running ? "up" : "down"}</span>
                {st.relay.public_host && <> at <code>{st.relay.public_host}</code></>}
                {st.relay.error && <span className="error"> — {st.relay.error}</span>}
                {st.relay.mode === "cloudflare" && st.relay.cloudflared === false && (
                  <span className="error"> — cloudflared is not installed</span>
                )}
              </>
            )}
          </p>

          <h4 className="set-subtitle">Invite</h4>
          <button type="button" className="test-btn" id="peer-invite" disabled={busy} onClick={createInvite}>
            Create invite code
          </button>
          {invite && (
            <div className="set-row" id="peer-invite-code">
              <input readOnly value={invite.code} onClick={(e) => (e.target as HTMLInputElement).select()} />
              <button
                type="button"
                className="test-btn"
                onClick={() => copyText(invite.code).then((ok) => toast(ok ? "Code copied" : "Copy failed"))}
              >
                Copy
              </button>
              <span className="set-hint">
                Single use, expires in {Math.round(invite.expires_in / 60)} min.{" "}
                {invite.relay
                  ? `Your peer connects through the relay at ${invite.host}.`
                  : `Your peer dials ${invite.host}:${invite.port}.`}
              </span>
            </div>
          )}

          <h4 className="set-subtitle">Join</h4>
          <div className="set-row">
            <input
              id="peer-join-code"
              placeholder="paste a mfp1:… or mfp2:… code"
              value={code}
              onChange={(e) => setCode(e.target.value)}
            />
            <button type="button" className="test-btn" disabled={busy || !code.trim()} onClick={join}>
              Join
            </button>
          </div>

          <h4 className="set-subtitle">Links</h4>
          {st.links.length === 0 && <p className="set-hint">No peer links yet.</p>}
          {st.links.map((l) => (
            <PeerLinkCard key={l.link_id} link={l} busy={busy} run={run} />
          ))}
        </div>
      )}
    </>
  );
}

function PeerLinkCard(props: {
  link: PeerLink;
  busy: boolean;
  run(fn: () => Promise<unknown>, ok?: string): void;
}) {
  const { link, busy, run } = props;
  const base = "/api/peer/links/" + encodeURIComponent(link.link_id);
  const [repo, setRepo] = useState("");
  const [branch, setBranch] = useState("");
  const [program, setProgram] = useState("claude");
  const [target, setTarget] = useState("");
  const [exportBranch, setExportBranch] = useState("peer/" + (link.peer_name || "work").replace(/[^A-Za-z0-9._-]+/g, "-"));
  const [confirm, setConfirm] = useState<"" | "unlink" | "unshare">("");
  const [deleteFiles, setDeleteFiles] = useState(false);

  return (
    <div className="peer-link-card" data-link-id={link.link_id}>
      <p>
        <strong>{link.peer_name}</strong> ({link.role}, {link.connected ? "connected" : "offline"}) ·
        SAS <code className="peer-sas">{link.sas}</code>
      </p>
      <p className="set-hint">Compare the SAS with your peer by voice or chat. If it differs, unlink now.</p>
      <div className="set-row">
        <span className="set-label">Peer may</span>
        {PERM_LABELS.map(([key, label]) => (
          <label className="check" key={key}>
            <input
              type="checkbox"
              checked={!!link.perms[key]}
              disabled={busy}
              onChange={(e) =>
                run(() => api(base + "/perms", { json: { [key]: e.target.checked } }))
              }
            />
            {label}
          </label>
        ))}
      </div>

      {link.shared ? (
        <>
          <p className="set-hint">
            Shared folder in session <strong>{link.session_title || "?"}</strong>.
          </p>
          <div className="set-row">
            <input placeholder="your repo to export into" value={target} onChange={(e) => setTarget(e.target.value)} />
            <input placeholder="peer/branch" value={exportBranch} onChange={(e) => setExportBranch(e.target.value)} />
            <button
              type="button"
              className="test-btn"
              disabled={busy || !target.trim() || !exportBranch.startsWith("peer/")}
              onClick={() =>
                run(
                  () => api(base + "/export", { json: { target_repo: target.trim(), branch_name: exportBranch } }),
                  "Exported to " + exportBranch
                )
              }
            >
              Export
            </button>
          </div>
          <button type="button" className="test-btn" disabled={busy} onClick={() => setConfirm("unshare")}>
            Unshare…
          </button>
        </>
      ) : (
        <div className="set-row">
          <input placeholder="repo path to share" value={repo} onChange={(e) => setRepo(e.target.value)} />
          <input placeholder="branch (optional)" value={branch} onChange={(e) => setBranch(e.target.value)} />
          <select value={program} onChange={(e) => setProgram(e.target.value)}>
            <option value="claude">claude</option>
            <option value="codex">codex</option>
          </select>
          <button
            type="button"
            className="test-btn"
            disabled={busy || !repo.trim()}
            onClick={() =>
              run(
                () =>
                  api(base + "/share", {
                    json: { repo_path: repo.trim(), branch: branch.trim() || undefined, program },
                  }),
                "Shared — the sandboxed session is starting"
              )
            }
          >
            Share a folder
          </button>
        </div>
      )}
      <button type="button" className="test-btn" disabled={busy} onClick={() => setConfirm("unlink")}>
        Unlink…
      </button>
      {confirm && (
        <InlineConfirm
          title={confirm === "unlink" ? `Unlink ${link.peer_name}?` : "Stop sharing this folder?"}
          body={
            <label className="check">
              <input type="checkbox" checked={deleteFiles} onChange={(e) => setDeleteFiles(e.target.checked)} />
              also delete the shared folder
            </label>
          }
          confirmLabel={confirm === "unlink" ? "Unlink" : "Unshare"}
          busy={busy}
          onCancel={() => setConfirm("")}
          onConfirm={() => {
            const q = deleteFiles ? "?delete_files=1" : "";
            const path = confirm === "unlink" ? base + q : base + "/share" + q;
            setConfirm("");
            run(() => api(path, { method: "DELETE" }), confirm === "unlink" ? "Unlinked" : "Unshared");
          }}
        />
      )}
    </div>
  );
}
