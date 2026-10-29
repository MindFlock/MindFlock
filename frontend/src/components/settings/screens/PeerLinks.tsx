/** Settings → People (screen key "peer", once "People";
 * docs/peer-link.md): pair
 * this MindFlock with another person's using a one-time invite, then bind ONE
 * shared folder per link — an agent runs in it inside a sandbox and talks to
 * the other person's agent (bubblewrap on Linux, sandbox-exec on macOS). A
 * side that can't run the sandbox (a Linux box without bubblewrap, say) still
 * takes part: messages, an inbox, and a read-only view of their changes.
 *
 * Two actions up front — Invite someone, Join someone — and nothing to set
 * first: inviting or joining turns peer links on. When this computer can only
 * make same-network invites (no cloudflared), the first invite asks where the
 * other person is. Every knob lives under Advanced.
 *
 * The screen follows the server live (peer.* events): the inviter sees the
 * other person join, the long Create / Join calls show their stages, and the
 * cards change as links connect, drop or get messages. No protocol words on
 * screen — "safety number", "You invited them", never SAS / listener / dialer.
 *
 * Everything goes through /api/peer*. The invite code — and the invite link
 * that carries it (`…/join#<code>`) — is shown only in the response that
 * created it (the server never lists it again).
 *
 * Inviting is the guest link's flow (peer/InviteSessions): a red warning,
 * "I understand", a picker of sessions and how each is shared — the moment
 * the other person joins, those are shared with them (on their rail at
 * once). The moment of joining, on either side, is peer/JoinedCard.
 *
 * This file is the screen and the invite / join half. Each link is a
 * peer/PeerLinkCard (with its joint task, peer/JointTask, and the Mirror
 * row); Advanced is peer/PeerAdvanced. The "?" in the header replays the
 * People tour (lib/tours/people.ts). */

import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../../../api/client";
import { toast } from "../../../lib/toast";
import { useNow } from "../../../lib/useNow";
import { reprobeDoctor, type EventEnvelope } from "../../../state/queries";
import { useSettings } from "../useSettings";
import type { ScreenProps } from "../SettingsDialog";
import { InstallMissing, type InstallStep } from "../../dialogs/InstallTerminal";
import { TourButton } from "../../onboarding/TourButton";
import {
  isCutOffInvite,
  joinPeerCode,
  newOpId,
  PEER_LIVE_EVENTS,
  type PeerInviteSession,
  type PeerStatus,
} from "../../../lib/peer";
import { PEER_JOIN_EVENT, takePendingPeerJoin } from "../../../lib/peerActions";
import { PeerAdvanced } from "./peer/PeerAdvanced";
import { GuestLinks } from "./peer/GuestLinks";
import { InviteSessions, type MadeInvite } from "./peer/InviteSessions";
import { JoinedCard, type Joined } from "./peer/JoinedCard";
import { PeerLinkCard } from "./peer/PeerLinkCard";
import { errText } from "./peer/useAct";
import "./PeerLinks.css";

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
  const [install, setInstall] = useState<InstallStep[]>([]);
  const [doctorTick, setDoctorTick] = useState(0);

  // Invite side: the invite made (peer/InviteSessions), then who joined it.
  const [invite, setInvite] = useState<MadeInvite | null>(null);
  const [joined, setJoined] = useState<Joined | null>(null);
  const [inviteErr, setInviteErr] = useState("");
  const [remoteChosen, setRemoteChosen] = useState(false);

  // Join side.
  const [code, setCode] = useState("");
  const [joinBusy, setJoinBusy] = useState(false);
  const [joinErr, setJoinErr] = useState("");
  const [joinStage, setJoinStage] = useState("");
  const [joinNote, setJoinNote] = useState("");
  // The link just joined (not a reconnect): the "you're connected" card.
  const [joinedThem, setJoinedThem] = useState<Joined | null>(null);
  const joinOp = useRef("");
  const joinInput = useRef<HTMLInputElement | null>(null);
  // The connection just joined: its card scrolls into view and lights up once.
  const [fresh, setFresh] = useState("");

  const load = useCallback(async () => {
    try {
      setSt(await api<PeerStatus>("/api/peer"));
      setLoadedAt(nowSec());
      setTick((t) => t + 1);
      setError("");
    } catch (e) {
      setError(errText(e));
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
    const offs = PEER_LIVE_EVENTS.map((name) =>
      ev.subscribe(name, (env: EventEnvelope) => {
        if (name === "peer.link_added") {
          const d = (env.data || {}) as Record<string, unknown>;
          // The person we just invited arrived: the invite box becomes "B
          // joined — fix-login is open to them · safety number".
          if (d.role === "listener" && inviteRef.current) {
            setJoined({
              link_id: String(d.link_id || ""),
              name: String(d.peer_name || "Your peer"),
              sas: String(d.sas || ""),
              repaired: !!d.repaired,
              sessions: Array.isArray(d.sessions) ? (d.sessions as PeerInviteSession[]) : [],
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
        if (op && op === joinOp.current) setJoinStage(String(d.text || ""));
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
  const now = useNow(counting ? 1000 : false) / 1000;

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
    reprobeDoctor()
      .then((d) => live && setInstall(d.install?.steps || []))
      .catch(() => live && setInstall([]));
    return () => {
      live = false;
    };
  }, [needsInstall, on, doctorTick]);

  // Inviting or joining turns peer links on server-side; re-read the switch so
  // the screen (and Advanced's toggle) agrees.
  const afterPair = () => void s.reload();

  // The off switch (Advanced): off disconnects everyone and closes the open
  // shared sessions (kept) — the server does it all in one call and keeps
  // listing the links; on starts listening again and reopens nothing.
  const [toggling, setToggling] = useState(false);
  const [toggleErr, setToggleErr] = useState("");
  const toggle = async (enable: boolean) => {
    setToggling(true);
    setToggleErr("");
    try {
      await api(enable ? "/api/peer/enable" : "/api/peer/disable", { json: {} });
    } catch (e) {
      setToggleErr(errText(e));
    }
    await s.reload();
    setToggling(false);
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
      setInviteErr(errText(e));
    }
  };

  const join = async () => {
    if (isCutOffInvite(code)) {
      setJoinErr("This invite is cut off — copy the whole message again.");
      return;
    }
    const op = newOpId();
    joinOp.current = op;
    setJoinBusy(true);
    setJoinErr("");
    setJoinStage("");
    setJoinNote("");
    setJoinedThem(null);
    try {
      const link = await joinPeerCode(code, op);
      setCode("");
      afterPair();
      if (link.reconnected) {
        setJoinNote(
          `Reconnected to ${link.peer_name}` +
            (link.shared ? " — your shared folder is kept" : "") +
            (link.sas_verified ? "." : `. Compare the safety number ${link.sas} again — it's new.`)
        );
      } else {
        // What their invite opened lands on the rail as it is shared.
        setJoinedThem({ link_id: link.link_id, name: link.peer_name, sas: link.sas });
      }
      setFresh(link.link_id);
    } catch (e) {
      setJoinErr(errText(e));
    }
    joinOp.current = "";
    setJoinStage("");
    setJoinBusy(false);
    load();
  };

  // It matches. The "joined" cards stay (✓, then Share a session… / Done).
  const markVerified = async (linkId: string) => {
    try {
      await api("/api/peer/links/" + encodeURIComponent(linkId) + "/verified", { json: { verified: true } });
    } catch (e) {
      toast(errText(e));
    }
    load();
  };
  const linkOf = (id: string) => st?.links.find((l) => l.link_id === id) || null;

  return (
    <>
      <div className="peer-head">
        <h3 className="set-section-title">People</h3>
        <TourButton tour="people" />
      </div>
      <p className="set-hint set-block-hint" id="peer-intro">
        Work with another person's MindFlock: one of you invites, the other joins — then talk, share a folder, or build
        one feature together.
      </p>

      <div className="peer-actions" id="peer-actions">
        <div className="peer-action" data-tour="people-invite">
          <h4 className="set-subtitle">Invite someone</h4>
          {joined ? (
            <div id="peer-joined" className="peer-invite" data-tour="people-safety">
              <JoinedCard
                side="inviter"
                joined={joined}
                link={linkOf(joined.link_id)}
                sandbox={!!st?.sandbox.available}
                tick={tick}
                onMatch={() => markVerified(joined.link_id)}
                onDone={() => {
                  setJoined(null);
                  setInvite(null);
                }}
              />
            </div>
          ) : (
            <InviteSessions
              st={st}
              invite={invite}
              setInvite={(inv) => {
                setInvite(inv);
                setJoined(null);
              }}
              now={now}
              loadedAt={loadedAt}
              cloudflaredMissing={cloudflaredMissing}
              install={install}
              remoteChosen={remoteChosen}
              onRemote={chooseRemote}
              afterPair={afterPair}
              reload={load}
            />
          )}
          {inviteErr && (
            <p className="error" id="peer-invite-error">
              {inviteErr}
            </p>
          )}
        </div>
        <div className="peer-action" data-tour="people-join">
          <h4 className="set-subtitle">Join someone</h4>
          <div className="set-row">
            <input
              id="peer-join-code"
              ref={joinInput}
              placeholder="paste the link, the message or the code"
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
              Paste the link, the whole message or just the code — any of them works. A fresh invite from
              someone you're already connected to reconnects you.
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
          {joinedThem && (
            <div id="peer-joined-them" className="peer-invite">
              <JoinedCard
                side="joiner"
                joined={joinedThem}
                link={linkOf(joinedThem.link_id)}
                sandbox={!!st?.sandbox.available}
                tick={tick}
                onMatch={() => markVerified(joinedThem.link_id)}
                onDone={() => setJoinedThem(null)}
              />
            </div>
          )}
        </div>
      </div>

      {error && <p className="error">{error}</p>}

      <GuestLinks />

      {st && !st.sandbox.available && (
        <div className="peer-needs" id="peer-needs">
          <p className="set-hint">
            On this computer you can pair, message and view their work; running an agent in a shared
            folder needs Linux or macOS{st.sandbox.reason ? <> (<span className="error">{st.sandbox.reason}</span>)</> : null}.
          </p>
          {!cloudflaredMissing && <InstallMissing steps={install} onDone={() => load()} />}
        </div>
      )}
      {st && cloudflaredMissing && !remoteChosen && (st.links.length > 0 || st.invites.length > 0) && (
        <div className="peer-needs">
          <p className="set-hint">
            Your invites only reach people on your own network or Tailscale until <strong>cloudflared</strong> is
            installed. Links that already connect directly keep working after you install it.
          </p>
          <InstallMissing steps={install} onDone={() => load()} />
        </div>
      )}

      {st && st.links.length > 0 && (
        <>
          <h4 className="set-subtitle">People you work with</h4>
          {!st.enabled && (
            <div className="peer-row peer-off-note" id="peer-off-note">
              <span className="set-hint">Connections to people are off — nobody connects until you turn them on.</span>
              <button type="button" className="test-btn" disabled={toggling} onClick={() => void toggle(true)}>
                {toggling ? "Turning on…" : "Turn on"}
              </button>
            </div>
          )}
          <div className={"peer-cards" + (st.enabled ? "" : " peer-cards-off")} inert={!st.enabled}>
            {st.links.map((l) => (
              <PeerLinkCard
                key={l.link_id}
                link={l}
                agents={st.agents || []}
                sandbox={st.sandbox.available}
                tick={tick}
                reload={load}
                onVerified={() => markVerified(l.link_id)}
                fresh={fresh === l.link_id}
              />
            ))}
          </div>
        </>
      )}

      <PeerAdvanced on={on} onToggle={(v) => void toggle(v)} st={st} busy={toggling} error={toggleErr} />
    </>
  );
}
