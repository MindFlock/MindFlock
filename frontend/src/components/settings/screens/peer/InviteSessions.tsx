/** Settings → People → Invite someone (docs/peer-link.md "How to use"):
 * the guest-link maker's flow for a peer invite. A red warning, then "I
 * understand" — ticking it opens the picker (a dialog: root sessions, how
 * each is shared, how they reach you), then **Make an invite**. The card
 * shows the LINK to send and what it opens; the moment they join, those
 * sessions are shared with them — on their rail at once, no second step on
 * either side (the server does it: POST /api/peer/invites `sessions`).
 *
 * Zero sessions is fine: "Make an invite without sessions" just connects,
 * and sessions are shared later from their card. Cancel unticks.
 *
 * The reach choice is the one People always offered, folded into the
 * picker: without cloudflared, "on my network or Tailscale" (a direct
 * invite) or "somewhere else" (install cloudflared right there); with a
 * relay, a tick for a direct invite anyway.
 *
 * The invite code — and the link that carries it — is shown only in the
 * response that made it (the server never lists it again). */

import { useEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import { api } from "../../../../api/client";
import type { Instance } from "../../../../api/types";
import { copyText } from "../../../../lib/clipboard";
import {
  inviteExpiryText,
  inviteMessage,
  inviteOpensText,
  newOpId,
  type PeerInvite,
  type PeerInviteSession,
  type PeerReach,
  type PeerStatus,
} from "../../../../lib/peer";
import { MODE_CHOICES, shareableSessions, type PeerMode } from "../../../../lib/peerSessions";
import { toast } from "../../../../lib/toast";
import type { EventEnvelope } from "../../../../state/queries";
import { useInstances } from "../../../../state/queries";
import { displayName } from "../../../../state/store";
import { InstallMissing, type InstallStep } from "../../../dialogs/InstallTerminal";
import { Modal } from "../../../Modal";
import { errText } from "./useAct";

/** The picker's card: the look of #peer-guests-pick-form (PeerLinks.css),
 * which is keyed to that form's id. */
const CARD: CSSProperties = {
  background: "var(--panel)",
  border: "1px solid var(--border)",
  borderRadius: 12,
  padding: 22,
  width: 500,
  maxWidth: "92vw",
  maxHeight: "85vh",
  overflow: "auto",
  boxSizing: "border-box",
  display: "flex",
  flexDirection: "column",
  gap: 12,
  color: "var(--text)",
};

export type MadeInvite = PeerInvite & { at: number };

/** The mode a freshly ticked session starts in: gated (a sandboxed copy)
 * when this computer can and no other ticked session is gated — a link
 * carries one — else full. */
export function defaultMode(picked: Map<string, PeerMode>, sandbox: boolean): PeerMode {
  return sandbox && ![...picked.values()].includes("gated") ? "gated" : "full";
}

/** Whether `title` may be switched to gated: the sandbox works here and no
 * other ticked session is gated. */
export function gatedFree(picked: Map<string, PeerMode>, title: string, sandbox: boolean): boolean {
  if (!sandbox) return false;
  for (const [t, m] of picked) if (m === "gated" && t !== title) return false;
  return true;
}

/** POST /api/peer/invites' `sessions`, in the rail's order. */
export function inviteSessionsBody(order: string[], picked: Map<string, PeerMode>): PeerInviteSession[] {
  return order.filter((t) => picked.has(t)).map((t) => ({ title: t, mode: picked.get(t)! }));
}

/** The picker's submit button. */
export function makeInviteLabel(n: number, busy = false): string {
  if (busy) return "Making…";
  return n ? `Make an invite (${n} session${n === 1 ? "" : "s"})` : "Make an invite without sessions";
}

export function InviteSessions(props: {
  st: PeerStatus | null;
  invite: MadeInvite | null;
  setInvite(inv: MadeInvite | null): void;
  /** The screen's clock (seconds) and when `st` was read. */
  now: number;
  loadedAt: number;
  cloudflaredMissing: boolean;
  install: InstallStep[];
  remoteChosen: boolean;
  /** "They're somewhere else": turn peer links on so the install plan
   * offers cloudflared. */
  onRemote(): void;
  afterPair(): void;
  reload(): void;
}) {
  const { st, invite, setInvite, now, loadedAt, cloudflaredMissing, install, remoteChosen } = props;
  const { data: instances } = useInstances();
  const sessions = useMemo<Instance[]>(() => shareableSessions(instances), [instances]);
  const sandbox = !!st?.sandbox.available;
  const [understood, setUnderstood] = useState(false);
  const [pickOpen, setPickOpen] = useState(false);
  const [picked, setPicked] = useState<Map<string, PeerMode>>(() => new Map());
  const [direct, setDirect] = useState<boolean | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [stage, setStage] = useState("");
  const op = useRef("");

  // A slow invite (the relay coming up) says each stage.
  useEffect(() => {
    const ev = window.mindflock?.events;
    if (!ev) return;
    return ev.subscribe("peer.progress", (env: EventEnvelope) => {
      const d = (env.data || {}) as Record<string, unknown>;
      if (d.op_id && String(d.op_id) === op.current) setStage(String(d.text || ""));
    });
  }, []);

  const ack = (on: boolean) => {
    setUnderstood(on);
    setPickOpen(on);
    setErr("");
  };
  const cancelPick = () => {
    setPickOpen(false);
    setUnderstood(false);
  };

  const toggle = (title: string) =>
    setPicked((prev) => {
      const next = new Map(prev);
      if (next.has(title)) next.delete(title);
      else next.set(title, defaultMode(prev, sandbox));
      return next;
    });
  const setMode = (title: string, mode: PeerMode) =>
    setPicked((prev) => new Map(prev).set(title, mode));

  // Without cloudflared they must say where the other person is first.
  const reach: PeerReach | null = cloudflaredMissing
    ? direct === true
      ? "direct"
      : direct === false && remoteChosen
      ? "tunnel"
      : null
    : direct
    ? "direct"
    : "auto";

  const create = async () => {
    if (!understood || !reach || busy) return;
    const id = newOpId();
    op.current = id;
    setBusy(true);
    setErr("");
    setStage("");
    try {
      const body = inviteSessionsBody(
        sessions.map((s) => s.title),
        picked
      );
      const inv = await api<PeerInvite>("/api/peer/invites", {
        json: { reach, op_id: id, ...(body.length ? { sessions: body } : {}) },
      });
      setInvite({ ...inv, sessions: inv.sessions || body, at: Date.now() / 1000 });
      setPicked(new Map());
      setPickOpen(false);
      setUnderstood(false);
      props.afterPair();
      if (inv.url) {
        const ok = await copyText(inv.url);
        toast(ok ? "Invite link copied — send it to them" : "Invite made — press Copy link to send it");
      }
    } catch (e) {
      setErr(errText(e));
    }
    op.current = "";
    setStage("");
    setBusy(false);
    props.reload();
  };

  const cancelInvite = async (id: string) => {
    try {
      await api("/api/peer/invites/" + encodeURIComponent(id), { method: "DELETE" });
      if (invite?.invite_id === id) setInvite(null);
    } catch (e) {
      toast(errText(e));
    }
    props.reload();
  };

  const inviteLeft = invite ? invite.expires_in - (now - invite.at) : 0;
  const otherInvites = (st?.invites || []).filter((i) => i.invite_id !== invite?.invite_id);

  return (
    <>
      {invite ? (
        <div id="peer-invite-code" className="peer-invite">
          {invite.fallback && (
            <p className="peer-warn" id="peer-invite-fallback">
              {invite.fallback.text}
            </p>
          )}
          {invite.url ? (
            <div className="set-row">
              <input
                id="peer-invite-url"
                readOnly
                value={invite.url}
                aria-label="Invite link"
                onClick={(e) => (e.target as HTMLInputElement).select()}
              />
              <button
                type="button"
                className="test-btn"
                id="peer-invite-copy-link"
                disabled={inviteLeft <= 0}
                onClick={() =>
                  copyText(invite.url || "").then((ok) => toast(ok ? "Invite link copied — send it to them" : "Copy failed"))
                }
              >
                Copy link
              </button>
            </div>
          ) : (
            <textarea
              readOnly
              rows={5}
              value={inviteMessage(invite)}
              onClick={(e) => (e.target as HTMLTextAreaElement).select()}
            />
          )}
          <span className="set-hint" id="peer-invite-opens">
            {inviteOpensText(invite.sessions)}
          </span>
          <div className="set-row">
            <button
              type="button"
              className="test-btn"
              id="peer-invite-copy"
              disabled={inviteLeft <= 0}
              onClick={() =>
                copyText(inviteMessage(invite)).then((ok) => toast(ok ? "Invite copied — send it to them" : "Copy failed"))
              }
            >
              Copy invite message
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
                : `They connect to ${invite.host}:${invite.port}, so they need to be on your network or Tailscale.`)}
          </span>
          <span className="set-hint">Waiting for them to join — this updates by itself.</span>
        </div>
      ) : (
        <div className="peer-guest-make" id="peer-invite-make">
          <div className="peer-danger peer-guest-warn" id="peer-invite-warn" role="alert">
            <strong>Whoever opens this invite gets the sessions you pick, the moment they join.</strong> In a full
            session they type at your agent as you: they can read and change the files in its worktree, run commands
            as you, and spend your agent's credits. A gated session is a sandboxed copy of its folder. The link works
            once, for 10 minutes — only send it to someone you would hand your keyboard to.
          </div>
          <label className="peer-guest-ack">
            <input type="checkbox" id="peer-invite-ack" checked={understood} onChange={(e) => ack(e.target.checked)} />{" "}
            I understand this lets them act on my machine — pick the sessions
          </label>
        </div>
      )}

      {pickOpen && (
        <Modal id="peer-invite-pick" role="dialog" aria-modal="true" portal onClose={cancelPick}>
          <form
            id="peer-invite-pick-form"
            style={CARD}
            onSubmit={(e) => {
              e.preventDefault();
              void create();
            }}
          >
            <h2>Which sessions?</h2>
            <p className="set-hint">
              The sessions you tick are shared with them the moment they join — they appear on their rail at once, no
              second step for either of you. None ticked? The invite just connects you, and you share sessions later
              from their card.
            </p>
            {sessions.length === 0 ? (
              <span className="set-hint">No sessions yet — you can still make an invite that just connects.</span>
            ) : (
              <ul className="peer-guest-pick" aria-label="Sessions">
                {sessions.map((s) => {
                  const mode = picked.get(s.title);
                  return (
                    <li key={s.title}>
                      <label>
                        <input
                          type="checkbox"
                          checked={!!mode}
                          onChange={() => toggle(s.title)}
                          data-session={s.title}
                        />{" "}
                        {displayName(s.title)}
                        {s.branch && s.branch !== s.title ? <span className="set-hint"> · {s.branch}</span> : null}
                      </label>
                      {mode && (
                        <select
                          aria-label={`How ${displayName(s.title)} is shared`}
                          data-mode-for={s.title}
                          value={mode}
                          onChange={(e) => setMode(s.title, e.target.value as PeerMode)}
                        >
                          {MODE_CHOICES.map((c) => (
                            <option
                              key={c.mode}
                              value={c.mode}
                              disabled={c.mode === "gated" && !gatedFree(picked, s.title, sandbox)}
                              title={c.hint}
                            >
                              {c.label}
                            </option>
                          ))}
                        </select>
                      )}
                    </li>
                  );
                })}
              </ul>
            )}
            {!sandbox && picked.size > 0 && (
              <span className="set-hint">Gated needs Linux or macOS (the sandbox) — here a session is shared in full.</span>
            )}
            {sandbox && picked.size > 1 && (
              <span className="set-hint">A link carries one gated session — share the others in full.</span>
            )}
            <fieldset className="peer-share-modes" id="peer-invite-reach">
              <legend className="set-hint">Where are they?</legend>
              {cloudflaredMissing ? (
                <>
                  <label>
                    <input
                      type="radio"
                      name="peer-invite-reach"
                      id="peer-reach-direct"
                      checked={direct === true}
                      onChange={() => setDirect(true)}
                    />{" "}
                    They're on my network or Tailscale
                  </label>
                  <label>
                    <input
                      type="radio"
                      name="peer-invite-reach"
                      id="peer-reach-remote"
                      checked={direct === false}
                      onChange={() => {
                        setDirect(false);
                        props.onRemote();
                      }}
                    />{" "}
                    They're somewhere else
                  </label>
                  {direct === false && (
                    <>
                      <span className="set-hint">
                        To reach someone on another network this computer needs <strong>cloudflared</strong> (free, no
                        account; MindFlock never downloads it for you). Install it, then make the invite.
                      </span>
                      <InstallMissing steps={install} onDone={() => props.reload()} />
                    </>
                  )}
                </>
              ) : st?.relay?.setting !== "off" ? (
                <label>
                  <input
                    type="checkbox"
                    id="peer-invite-direct"
                    checked={!!direct}
                    onChange={(e) => setDirect(e.target.checked)}
                  />{" "}
                  Same network or Tailscale? Make a direct invite
                  <span className="set-hint peer-share-mode-hint">
                    Otherwise it goes through a relay, so they can be anywhere.
                  </span>
                </label>
              ) : (
                <span className="set-hint">They need to be on your network or Tailscale (the relay is off).</span>
              )}
            </fieldset>
            {busy && stage && (
              <span className="set-hint peer-stage" id="peer-invite-stage">
                {stage}
              </span>
            )}
            {err && (
              <p className="error" id="peer-invite-error">
                {err}
              </p>
            )}
            <div className="modal-actions">
              <button type="button" id="peer-invite-cancel" onClick={cancelPick}>
                Cancel
              </button>
              <button
                type="submit"
                className="btn-primary"
                id="peer-invite"
                disabled={busy || !understood || !reach}
                title={!reach ? "Say where they are first" : undefined}
              >
                {makeInviteLabel(picked.size, busy)}
              </button>
            </div>
          </form>
        </Modal>
      )}

      {otherInvites.length > 0 && (
        <ul className="peer-invites" id="peer-invites">
          {otherInvites.map((i) => {
            const left = (i.expires_in ?? 0) - (now - loadedAt);
            const opens = i.sessions?.length ? ` (opens ${i.sessions.map((x) => x.title).join(", ")})` : "";
            return (
              <li key={i.invite_id}>
                <span className="set-hint">
                  {i.direct ? "Same-network invite" : "Invite"}
                  {opens} waiting · {inviteExpiryText(left)}
                </span>{" "}
                <button type="button" className="test-btn" onClick={() => cancelInvite(i.invite_id)}>
                  Cancel
                </button>
              </li>
            );
          })}
        </ul>
      )}
    </>
  );
}
