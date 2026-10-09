/** Settings → Work with someone (screen key "peer"; docs/peer-link.md): pair
 * this MindFlock with another person's using a one-time invite, then bind ONE
 * shared folder per link — an agent runs in it inside a sandbox and talks to
 * the other person's agent. A side that can't run the sandbox (a Mac, or a
 * Linux box before it shares) still takes part: messages, an inbox, and a
 * read-only view of their changes.
 *
 * Two actions up front — Invite someone, Join with a code — and nothing to set
 * first: inviting or joining turns peer links on. When this computer can only
 * make same-network invites (no cloudflared), the first invite asks where the
 * other person is. Every knob lives under Advanced.
 *
 * The screen follows the server live (peer.* events): the inviter sees the
 * other person join, the long Create / Join calls show their stages, and the
 * cards change as links connect, drop or get messages. No protocol words on
 * screen — "safety number", "You invited them", never SAS / listener / dialer.
 *
 * Everything goes through /api/peer*. The invite code is shown only in the
 * response that created it (the server never lists it again). */

import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../../../api/client";
import { copyText } from "../../../lib/clipboard";
import { toast } from "../../../lib/toast";
import { selectSession } from "../../../lib/sessionActions";
import { useUi } from "../../../state/store";
import type { EventEnvelope } from "../../../state/queries";
import { SettingField, useSettings, InlineConfirm } from "../useSettings";
import type { ScreenProps } from "../SettingsDialog";
import { InstallMissing, type InstallStep } from "../../dialogs/InstallTerminal";
import type { DoctorPayload } from "../../dialogs/SetupDialog";
import {
  carrierText,
  inviteExpiryText,
  joinPeerCode,
  newOpId,
  reconnectKind,
  roleText,
  type PeerInvite,
  type PeerLink,
  type PeerMessage,
  type PeerReach,
  type PeerStatus,
} from "../../../lib/peer";
import { PEER_JOIN_EVENT, takePendingPeerJoin } from "../../../lib/peerActions";

const RELAY_OPTIONS = [
  { value: "auto", label: "Automatic — through Cloudflare when cloudflared is installed" },
  { value: "off", label: "Off — they connect to this computer directly (same network or tailnet)" },
  { value: "cloudflare", label: "Always through Cloudflare (needs cloudflared)" },
  { value: "url", label: "My own HTTPS relay (relay URL below)" },
];

const PERM_LABELS: Array<[keyof PeerLink["perms"], string]> = [
  ["messages", "message me"],
  ["diff", "see my changes"],
  ["read_file", "read my files"],
];

const PEER_EVENTS = [
  "peer.link_added",
  "peer.link_removed",
  "peer.state",
  "peer.message",
  "peer.relay_changed",
];

function peerErrText(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

function nowSec(): number {
  return Date.now() / 1000;
}

export function PeerLinks(_: ScreenProps) {
  const s = useSettings();
  const stored = s.get("peer", "enabled");
  const on = stored === true || stored === "true";
  const [st, setSt] = useState<PeerStatus | null>(null);
  const [loadedAt, setLoadedAt] = useState(nowSec());
  const [tick, setTick] = useState(0);
  const [error, setError] = useState("");
  const [now, setNow] = useState(nowSec());
  const [install, setInstall] = useState<InstallStep[]>([]);
  const [doctorTick, setDoctorTick] = useState(0);

  // Invite side.
  const [invite, setInvite] = useState<(PeerInvite & { at: number }) | null>(null);
  const [joined, setJoined] = useState<{ link_id: string; name: string; sas: string; repaired: boolean } | null>(null);
  const [inviteBusy, setInviteBusy] = useState(false);
  const [inviteErr, setInviteErr] = useState("");
  const [inviteStage, setInviteStage] = useState("");
  const [remoteChosen, setRemoteChosen] = useState(false);
  const inviteOp = useRef("");

  // Join side.
  const [code, setCode] = useState("");
  const [joinBusy, setJoinBusy] = useState(false);
  const [joinErr, setJoinErr] = useState("");
  const [joinStage, setJoinStage] = useState("");
  const [joinNote, setJoinNote] = useState("");
  const joinOp = useRef("");
  const joinInput = useRef<HTMLInputElement | null>(null);

  const load = useCallback(async () => {
    try {
      setSt(await api<PeerStatus>("/api/peer"));
      setLoadedAt(nowSec());
      setTick((t) => t + 1);
      setError("");
    } catch (e) {
      setError(peerErrText(e));
    }
  }, []);

  useEffect(() => {
    load();
  }, [load, on]);

  // Live: a link joining, dropping, a message, a moved relay — refetch now;
  // the long calls' stages land on the action that started them.
  const inviteRef = useRef(invite);
  inviteRef.current = invite;
  useEffect(() => {
    const ev = window.mindflock?.events;
    if (!ev) return;
    const offs = PEER_EVENTS.map((name) =>
      ev.subscribe(name, (env: EventEnvelope) => {
        if (name === "peer.link_added") {
          const d = (env.data || {}) as Record<string, unknown>;
          // The person we just invited arrived: the invite box becomes "B
          // joined — read them your safety number".
          if (d.role === "listener" && inviteRef.current) {
            setJoined({
              link_id: String(d.link_id || ""),
              name: String(d.peer_name || "Your peer"),
              sas: String(d.sas || ""),
              repaired: !!d.repaired,
            });
          }
        }
        load();
      })
    );
    offs.push(
      ev.subscribe("peer.progress", (env: EventEnvelope) => {
        const d = (env.data || {}) as Record<string, unknown>;
        const op = String(d.op_id || "");
        const text = String(d.text || "");
        if (op && op === inviteOp.current) setInviteStage(text);
        if (op && op === joinOp.current) setJoinStage(text);
      })
    );
    return () => offs.forEach((off) => off());
  }, [load]);

  // "Join with a code…" from the palette: focus (and prefill) the join box.
  useEffect(() => {
    const focus = (prefill: string) => {
      if (prefill) setCode(prefill);
      setTimeout(() => joinInput.current?.focus(), 0);
    };
    const pending = takePendingPeerJoin();
    if (pending) focus(pending.code);
    const onJoin = (e: Event) => {
      takePendingPeerJoin();
      focus(String((e as CustomEvent<{ code?: string } | null>).detail?.code || ""));
    };
    document.addEventListener(PEER_JOIN_EVENT, onJoin);
    return () => document.removeEventListener(PEER_JOIN_EVENT, onJoin);
  }, []);

  // The countdowns tick while anything is counting down.
  const counting = !!invite || (st?.invites.length || 0) > 0;
  useEffect(() => {
    if (!counting) return;
    const t = setInterval(() => setNow(nowSec()), 1000);
    return () => clearInterval(t);
  }, [counting]);

  const cloudflaredMissing = st?.relay?.setting === "auto" && st.relay.cloudflared === false;
  // Missing pieces (the sandbox, cloudflared) are installed from right here,
  // by the same one-shot install the setup checklist runs.
  const needsInstall = !!st && (!st.sandbox.available || cloudflaredMissing);
  useEffect(() => {
    if (!needsInstall) {
      setInstall([]);
      return;
    }
    let live = true;
    api<DoctorPayload>("/api/doctor?refresh=1")
      .then((d) => live && setInstall(d.install?.steps || []))
      .catch(() => live && setInstall([]));
    return () => {
      live = false;
    };
  }, [needsInstall, on, doctorTick]);

  // Inviting or joining turns peer links on server-side; re-read the switch so
  // the screen (and Advanced's toggle) agrees.
  const afterPair = () => void s.reload();

  const createInvite = async (reach: PeerReach) => {
    const op = newOpId();
    inviteOp.current = op;
    setInviteBusy(true);
    setInviteErr("");
    setInviteStage("");
    setJoined(null);
    try {
      const inv = await api<PeerInvite>("/api/peer/invites", { json: { reach, op_id: op } });
      setInvite({ ...inv, at: nowSec() });
      afterPair();
    } catch (e) {
      setInviteErr(peerErrText(e));
    }
    inviteOp.current = "";
    setInviteStage("");
    setInviteBusy(false);
    load();
  };

  // "They're somewhere else": turn peer links on (no invite yet) so the
  // install plan offers cloudflared, then create the invite through it.
  const chooseRemote = async () => {
    setRemoteChosen(true);
    setInviteErr("");
    try {
      await api("/api/peer/enable", { json: {} });
      afterPair();
      setDoctorTick((t) => t + 1);
    } catch (e) {
      setInviteErr(peerErrText(e));
    }
  };

  const cancelInvite = async (id: string) => {
    try {
      await api("/api/peer/invites/" + encodeURIComponent(id), { method: "DELETE" });
      if (invite?.invite_id === id) setInvite(null);
    } catch (e) {
      toast(peerErrText(e));
    }
    load();
  };

  const join = async () => {
    const op = newOpId();
    joinOp.current = op;
    setJoinBusy(true);
    setJoinErr("");
    setJoinStage("");
    setJoinNote("");
    try {
      const link = await joinPeerCode(code, op);
      setCode("");
      afterPair();
      setJoinNote(
        link.reconnected
          ? `Reconnected to ${link.peer_name} — same link, your shared folder is kept.`
          : `Connected to ${link.peer_name}. Read each other your safety number ${link.sas} — if it differs, unlink.`
      );
    } catch (e) {
      setJoinErr(peerErrText(e));
    }
    joinOp.current = "";
    setJoinStage("");
    setJoinBusy(false);
    load();
  };

  const markVerified = async (linkId: string) => {
    try {
      await api("/api/peer/links/" + encodeURIComponent(linkId) + "/verified", { json: { verified: true } });
      setJoined(null);
      setInvite(null);
    } catch (e) {
      toast(peerErrText(e));
    }
    load();
  };

  const inviteLeft = invite ? invite.expires_in - (now - invite.at) : 0;
  const otherInvites = (st?.invites || []).filter((i) => i.invite_id !== invite?.invite_id);
  const relayAddr = st?.relay?.address || "";

  return (
    <>
      <h3 className="set-section-title">Work with someone</h3>
      <p className="set-hint set-block-hint">
        Work together with someone else's MindFlock. Send them an invite, they paste it, and
        you're connected. Then either of you can share <strong>one folder</strong>: an agent
        works in it inside a sandbox and talks to the other person's agent. You can also message
        them and look at their changes. Nothing else on your machine is exposed — see{" "}
        <code>docs/peer-link.md</code>.
      </p>

      <div className="peer-actions" id="peer-actions">
        <div className="peer-action">
          <h4 className="set-subtitle">Invite someone</h4>
          {joined ? (
            <div id="peer-joined" className="peer-invite">
              <p>
                <strong>{joined.name}</strong> {joined.repaired ? "reconnected" : "joined"} — read them your
                safety number <code className="peer-sas">{joined.sas}</code>. If theirs is different, unlink
                now.
              </p>
              <div className="set-row">
                <button type="button" className="test-btn" id="peer-joined-match" onClick={() => markVerified(joined.link_id)}>
                  It matches
                </button>
                <button
                  type="button"
                  className="test-btn"
                  onClick={() => {
                    setJoined(null);
                    setInvite(null);
                  }}
                >
                  Later
                </button>
              </div>
            </div>
          ) : invite ? (
            <div id="peer-invite-code" className="peer-invite">
              {invite.fallback && (
                <p className="peer-warn" id="peer-invite-fallback">
                  {invite.fallback.text}
                </p>
              )}
              <textarea
                readOnly
                rows={5}
                value={invite.message || invite.code}
                onClick={(e) => (e.target as HTMLTextAreaElement).select()}
              />
              <div className="set-row">
                <button
                  type="button"
                  className="test-btn"
                  id="peer-invite-copy"
                  disabled={inviteLeft <= 0}
                  onClick={() =>
                    copyText(invite.message || invite.code).then((ok) =>
                      toast(ok ? "Invite copied — send it to them" : "Copy failed")
                    )
                  }
                >
                  Copy invite
                </button>
                {inviteLeft > 0 ? (
                  <button type="button" className="test-btn" onClick={() => cancelInvite(invite.invite_id)}>
                    Cancel invite
                  </button>
                ) : (
                  <button type="button" className="test-btn" onClick={() => setInvite(null)}>
                    Dismiss
                  </button>
                )}
              </div>
              <span className="set-hint" id="peer-invite-expiry">
                {inviteExpiryText(inviteLeft)}
                {inviteLeft > 0 && " · works once. "}
                {inviteLeft > 0 &&
                  (invite.relay && !invite.direct
                    ? "They can be anywhere — it connects through a relay that can't read your traffic."
                    : `They connect to ${invite.host}:${invite.port}, so they need to be on your network or tailnet.`)}
              </span>
              <span className="set-hint">Waiting for them to join — this updates by itself.</span>
            </div>
          ) : cloudflaredMissing ? (
            <div id="peer-reach" className="peer-invite">
              <span className="set-hint">Where is the person you're inviting?</span>
              <button
                type="button"
                className="test-btn"
                id="peer-reach-direct"
                disabled={inviteBusy}
                onClick={() => createInvite("direct")}
              >
                They're on my network or tailnet
              </button>
              <button
                type="button"
                className="test-btn"
                id="peer-reach-remote"
                disabled={inviteBusy}
                onClick={chooseRemote}
              >
                They're somewhere else
              </button>
              {remoteChosen && (
                <>
                  <span className="set-hint">
                    To reach someone on another network this computer needs <strong>cloudflared</strong> (free,
                    no account; MindFlock never downloads it for you). Install it, then create the invite.
                  </span>
                  <InstallMissing steps={install} onDone={() => load()} />
                  <button
                    type="button"
                    className="test-btn"
                    id="peer-invite"
                    disabled={inviteBusy}
                    onClick={() => createInvite("tunnel")}
                  >
                    {inviteBusy ? "Creating…" : "Create invite"}
                  </button>
                </>
              )}
            </div>
          ) : (
            <>
              <button
                type="button"
                className="test-btn"
                id="peer-invite"
                disabled={inviteBusy}
                onClick={() => createInvite("auto")}
              >
                {inviteBusy ? "Creating…" : "Create invite"}
              </button>
              <button
                type="button"
                className="linklike"
                id="peer-invite-direct"
                disabled={inviteBusy}
                onClick={() => createInvite("direct")}
              >
                Same network or tailnet? Make a direct invite
              </button>
            </>
          )}
          {inviteBusy && inviteStage && (
            <span className="set-hint peer-stage" id="peer-invite-stage">
              {inviteStage}
            </span>
          )}
          {inviteErr && (
            <p className="error" id="peer-invite-error">
              {inviteErr}
            </p>
          )}
          {otherInvites.length > 0 && (
            <ul className="peer-invites" id="peer-invites">
              {otherInvites.map((i) => {
                const left = (i.expires_in ?? 0) - (now - loadedAt);
                return (
                  <li key={i.invite_id}>
                    <span className="set-hint">
                      {i.direct ? "Same-network invite" : "Invite"} waiting · {inviteExpiryText(left)}
                    </span>{" "}
                    <button type="button" className="test-btn" onClick={() => cancelInvite(i.invite_id)}>
                      Cancel
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
        </div>
        <div className="peer-action">
          <h4 className="set-subtitle">Join with a code</h4>
          <div className="set-row">
            <input
              id="peer-join-code"
              ref={joinInput}
              placeholder="paste the invite you were sent"
              value={code}
              onChange={(e) => setCode(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && code.trim() && !joinBusy) join();
              }}
            />
            <button type="button" className="test-btn" id="peer-join" disabled={joinBusy || !code.trim()} onClick={join}>
              {joinBusy ? "Joining…" : "Join"}
            </button>
          </div>
          {joinBusy ? (
            <span className="set-hint peer-stage" id="peer-join-stage">
              {joinStage || "Connecting…"}
            </span>
          ) : (
            <span className="set-hint">
              Paste the whole message or just the code — either works. A fresh invite from someone you're
              already linked with reconnects that link.
            </span>
          )}
          {joinErr && (
            <p className="error" id="peer-join-error">
              {joinErr}
            </p>
          )}
          {joinNote && (
            <p className="set-hint" id="peer-join-note">
              {joinNote}
            </p>
          )}
        </div>
      </div>

      {error && <p className="error">{error}</p>}

      {st && !st.sandbox.available && (
        <div className="peer-needs" id="peer-needs">
          <p className="set-hint">
            On this computer you can pair, message and view their work; running an agent in a shared
            folder needs Linux{st.sandbox.reason ? <> (<span className="error">{st.sandbox.reason}</span>)</> : null}.
          </p>
          {!cloudflaredMissing && <InstallMissing steps={install} onDone={() => load()} />}
        </div>
      )}
      {st && cloudflaredMissing && !remoteChosen && (st.links.length > 0 || st.invites.length > 0) && (
        <div className="peer-needs">
          <p className="set-hint">
            Your invites only reach people on your own network or tailnet until <strong>cloudflared</strong> is
            installed. Links that already connect directly keep working after you install it.
          </p>
          <InstallMissing steps={install} onDone={() => load()} />
        </div>
      )}

      {st && st.links.length > 0 && (
        <>
          <h4 className="set-subtitle">People you work with</h4>
          {st.links.map((l) => (
            <PeerLinkCard
              key={l.link_id}
              link={l}
              agents={st.agents || []}
              sandbox={st.sandbox.available}
              tick={tick}
              reload={load}
              onVerified={() => markVerified(l.link_id)}
            />
          ))}
        </>
      )}

      <details className="peer-advanced" id="peer-advanced">
        <summary>Advanced</summary>
        <div className="set-row set-switch-row">
          <span className="set-label">Work with someone</span>
          <label className="ca-switch">
            <input
              type="checkbox"
              id="peer-enabled"
              checked={on}
              onChange={(e) => s.saveField("peer", "enabled", e.target.checked)}
            />
            <span className="ca-slider" />
          </label>
          <span className="set-hint">
            Turned on by your first invite or join. Off: nobody can connect, and this computer connects to
            nobody.
          </span>
        </div>
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
    </>
  );
}

/** One person you're linked with: who, how, the safety number, what they may
 * do, the shared folder (or the share form), messages, their changes, and —
 * on a link you joined that dropped — Reconnect. Each action has its own busy
 * flag and its own inline error. */
function PeerLinkCard(props: {
  link: PeerLink;
  agents: string[];
  sandbox: boolean;
  tick: number;
  reload(): void;
  onVerified(): void;
}) {
  const { link, agents, sandbox, tick, reload, onVerified } = props;
  const base = "/api/peer/links/" + encodeURIComponent(link.link_id);
  const [busy, setBusy] = useState("");
  const [err, setErr] = useState("");
  const [repo, setRepo] = useState("");
  const [branch, setBranch] = useState("");
  const [program, setProgram] = useState("");
  // The first offered agent is the default CLI (the server orders it so).
  const chosen = program || agents[0] || "";
  const [target, setTarget] = useState("");
  const [exportBranch, setExportBranch] = useState(
    "peer/" + (link.peer_name || "work").replace(/[^A-Za-z0-9._-]+/g, "-")
  );
  const [confirm, setConfirm] = useState<"" | "unlink" | "unshare">("");
  const [deleteFiles, setDeleteFiles] = useState(false);
  const [reconnect, setReconnect] = useState("");
  const [msgsOpen, setMsgsOpen] = useState(false);
  const [msgs, setMsgs] = useState<PeerMessage[]>([]);
  const [draft, setDraft] = useState("");
  const [diff, setDiff] = useState<{ stat: unknown[]; diff: string; truncated: boolean } | null>(null);

  const act = async (what: string, fn: () => Promise<unknown>, ok?: string) => {
    setBusy(what);
    setErr("");
    try {
      await fn();
      if (ok) toast(ok);
    } catch (e) {
      setErr(peerErrText(e));
    }
    setBusy("");
    reload();
  };

  // The message log: fetched when opened and again whenever the screen
  // reloads (a peer.message event reloads it); opening it marks them read.
  useEffect(() => {
    if (!msgsOpen) return;
    let live = true;
    api<{ messages: PeerMessage[]; unread: number }>(base + "/messages")
      .then((r) => {
        if (!live) return;
        setMsgs(r.messages || []);
        if (r.unread) void api(base + "/messages/read", { json: {} }).catch(() => undefined);
      })
      .catch((e) => live && setErr(peerErrText(e)));
    return () => {
      live = false;
    };
  }, [msgsOpen, tick, base]);

  const send = () =>
    act("send", async () => {
      const r = await api<{ delivered: boolean }>(base + "/message", { json: { text: draft.trim() } });
      setDraft("");
      if (!r.delivered) toast(`${link.peer_name} isn't taking messages right now — it's in your log`);
    });

  const showDiff = () =>
    act("diff", async () => {
      setDiff(await api<{ stat: unknown[]; diff: string; truncated: boolean }>(base + "/diff"));
    });

  const doReconnect = () => {
    const kind = reconnectKind(reconnect);
    if (!kind) {
      setErr("Paste a fresh invite from them, or their relay address (wss://…) or host:port.");
      return;
    }
    void act(
      "reconnect",
      async () => {
        if (kind === "invite") await joinPeerCode(reconnect, newOpId());
        else await api(base + "/address", { json: { address: reconnect.trim() } });
        setReconnect("");
      },
      "Reconnecting to " + link.peer_name + "…"
    );
  };

  const openSession = () => {
    if (!link.session_title) return;
    selectSession(link.session_title);
    useUi.getState().closeDialog();
  };

  const how = carrierText(link.carrier);
  const unread = link.unread || 0;

  return (
    <div className="peer-link-card" data-link-id={link.link_id}>
      <p>
        <strong>{link.peer_name}</strong> · {roleText(link.role)} ·{" "}
        <span className={link.connected ? "peer-on" : "peer-off"}>{link.connected ? "connected" : "offline"}</span>
        {how && link.connected ? " " + how : ""}
        {link.peer_app ? <span className="muted"> · MindFlock {link.peer_app}</span> : null}
      </p>
      {link.sas_verified ? (
        <p className="set-hint">
          Safety number <code className="peer-sas">{link.sas}</code> ✓ checked with them.
        </p>
      ) : (
        <div className="set-row">
          <span className="set-hint">
            Safety number <code className="peer-sas">{link.sas}</code> — read it to each other. If it differs,
            unlink now.
          </span>
          <button type="button" className="test-btn" onClick={onVerified}>
            It matches
          </button>
        </div>
      )}
      <div className="set-row">
        <span className="set-label">They may</span>
        {PERM_LABELS.map(([key, label]) => (
          <label className="check" key={key}>
            <input
              type="checkbox"
              checked={!!link.perms[key]}
              disabled={!!busy}
              onChange={(e) => act("perms", () => api(base + "/perms", { json: { [key]: e.target.checked } }))}
            />
            {label}
          </label>
        ))}
      </div>

      {link.role === "dialer" && !link.connected && (
        <div className="set-row peer-reconnect">
          <input
            placeholder="paste a fresh invite from them (or their relay address)"
            value={reconnect}
            onChange={(e) => setReconnect(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && reconnect.trim() && !busy) doReconnect();
            }}
          />
          <button type="button" className="test-btn" disabled={!!busy || !reconnect.trim()} onClick={doReconnect}>
            {busy === "reconnect" ? "Reconnecting…" : "Reconnect"}
          </button>
        </div>
      )}

      {link.shared ? (
        <>
          <div className="set-row">
            <span className="set-hint">
              Shared folder in session <strong>{link.session_title || "?"}</strong>.
            </span>
            {link.session_title && (
              <button type="button" className="test-btn" onClick={openSession}>
                Open session
              </button>
            )}
          </div>
          <div className="set-row">
            <span className="set-label">Bring work home</span>
            <input placeholder="your repo to bring it into" value={target} onChange={(e) => setTarget(e.target.value)} />
            <input placeholder="peer/branch" value={exportBranch} onChange={(e) => setExportBranch(e.target.value)} />
            <button
              type="button"
              className="test-btn"
              disabled={!!busy || !target.trim() || !exportBranch.startsWith("peer/")}
              onClick={() =>
                act(
                  "export",
                  () => api(base + "/export", { json: { target_repo: target.trim(), branch_name: exportBranch } }),
                  "Brought home as " + exportBranch
                )
              }
            >
              Export
            </button>
          </div>
          <button type="button" className="test-btn" disabled={!!busy} onClick={() => setConfirm("unshare")}>
            Stop sharing…
          </button>
        </>
      ) : sandbox ? (
        <div className="set-row">
          <input placeholder="repo path to share" value={repo} onChange={(e) => setRepo(e.target.value)} />
          <input placeholder="branch (optional)" value={branch} onChange={(e) => setBranch(e.target.value)} />
          <select value={chosen} onChange={(e) => setProgram(e.target.value)} disabled={!agents.length}>
            {agents.length ? (
              agents.map((a) => (
                <option value={a} key={a}>
                  {a}
                </option>
              ))
            ) : (
              <option value="">no agent CLI can run here</option>
            )}
          </select>
          <button
            type="button"
            className="test-btn"
            disabled={!!busy || !repo.trim() || !chosen}
            onClick={() =>
              act(
                "share",
                () =>
                  api(base + "/share", {
                    json: { repo_path: repo.trim(), branch: branch.trim() || undefined, program: chosen },
                  }),
                "Shared — the sandboxed session is starting"
              )
            }
          >
            {busy === "share" ? "Sharing…" : "Share a folder"}
          </button>
        </div>
      ) : (
        <p className="set-hint">
          Here you can message {link.peer_name} and look at their changes; sharing a folder needs Linux.
        </p>
      )}

      <div className="set-row">
        <button
          type="button"
          className="test-btn peer-msgs-toggle"
          aria-expanded={msgsOpen}
          onClick={() => setMsgsOpen((o) => !o)}
        >
          Messages{unread > 0 && <span className="queue-tab-badge">{unread > 99 ? "99+" : unread}</span>}
        </button>
        <button type="button" className="test-btn" disabled={!!busy || !link.connected} onClick={showDiff}>
          {busy === "diff" ? "Loading…" : "Their changes"}
        </button>
        <button type="button" className="test-btn" disabled={!!busy} onClick={() => setConfirm("unlink")}>
          Unlink…
        </button>
      </div>

      {msgsOpen && (
        <div className="peer-msgs">
          {msgs.length ? (
            <ul className="peer-msg-log">
              {msgs.map((m) => (
                <li key={m.id} className={"peer-msg peer-msg-" + m.dir}>
                  <span className="muted">
                    {m.dir === "in" ? link.peer_name : m.by === "agent" ? "your agent" : "you"}
                    {m.dir === "in" && m.delivered_to ? " → " + m.delivered_to : ""}:
                  </span>{" "}
                  {m.text}
                </li>
              ))}
            </ul>
          ) : (
            <p className="set-hint">No messages yet.</p>
          )}
          <div className="set-row">
            <textarea
              rows={2}
              placeholder={`message ${link.peer_name}`}
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && (e.ctrlKey || e.metaKey) && draft.trim() && !busy) send();
              }}
            />
            <button type="button" className="test-btn" disabled={!!busy || !draft.trim() || !link.connected} onClick={send}>
              {busy === "send" ? "Sending…" : "Send"}
            </button>
          </div>
          {!link.connected && <span className="set-hint">{link.peer_name} is offline — send once they're back.</span>}
        </div>
      )}

      {diff && (
        <div className="peer-diff">
          <div className="set-row">
            <span className="set-hint">
              {link.peer_name}'s changes (read only){diff.truncated ? " — cut short" : ""}
            </span>
            <button type="button" className="test-btn" onClick={() => setDiff(null)}>
              Close
            </button>
          </div>
          {diff.stat?.length ? (
            <pre className="peer-diff-stat">
              {diff.stat
                .map((x) => (typeof x === "string" ? x : Object.values(x as Record<string, unknown>).join(" ")))
                .join("\n")}
            </pre>
          ) : null}
          <pre className="peer-diff-body">{diff.diff || "No changes."}</pre>
        </div>
      )}

      {err && <p className="error">{err}</p>}

      {confirm && (
        <InlineConfirm
          title={confirm === "unlink" ? `Unlink ${link.peer_name}?` : "Stop sharing this folder?"}
          body={
            <label className="check">
              <input type="checkbox" checked={deleteFiles} onChange={(e) => setDeleteFiles(e.target.checked)} />
              also delete the shared folder
            </label>
          }
          confirmLabel={confirm === "unlink" ? "Unlink" : "Stop sharing"}
          busy={!!busy}
          onCancel={() => setConfirm("")}
          onConfirm={() => {
            const q = deleteFiles ? "?delete_files=1" : "";
            const path = confirm === "unlink" ? base + q : base + "/share" + q;
            const unlinking = confirm === "unlink";
            setConfirm("");
            void act(unlinking ? "unlink" : "unshare", () => api(path, { method: "DELETE" }), unlinking ? "Unlinked" : "Stopped sharing");
          }}
        />
      )}
    </div>
  );
}
