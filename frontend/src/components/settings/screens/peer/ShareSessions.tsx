/** Settings → People → a link card → **Share a session…** (docs/peer-link.md
 * "Shared sessions — CONTRACT"): put one of this device's sessions on the
 * other person's rail, as a row they open and type into.
 *
 * The dialog picks ONE root session (the guest-link picker's rule — not
 * another device's or a peer's row, not a sub-session) and how:
 *  - Gated to its folder: a sandboxed copy of its folder and branch (the
 *    link's one shared folder) — they type into an agent confined to it;
 *  - Full join: the session as it is — they type into my agent, on my
 *    machine. The red warning and its "I understand" tick gate Share, as
 *    they do for a guest link.
 * Under the picker: what is already shared on this link, each with
 * Withdraw. The card itself lists what THEY share with me (TheirSessions),
 * each with Open (its `<peer>::<title>` row on the rail). */

import { useCallback, useEffect, useMemo, useState } from "react";
import { selectSession } from "../../../../lib/sessionActions";
import type { PeerLink } from "../../../../lib/peer";
import {
  MODE_CHOICES,
  canShare,
  fetchPeerSessions,
  isPeerRow,
  mineLine,
  shareableSessions,
  sharePeerSession,
  theirsLine,
  withdrawPeerSession,
  type PeerMode,
  type SessionsView,
} from "../../../../lib/peerSessions";
import { toast } from "../../../../lib/toast";
import type { Instance } from "../../../../api/types";
import { queryClient, useInstances } from "../../../../state/queries";
import { displayName, useUi } from "../../../../state/store";
import { Modal } from "../../../Modal";
import { errText } from "./useAct";

const EMPTY: SessionsView = { mine: [], theirs: [], connected: false };

/** The link's sessions view, re-read on `tick` (People's poll) and after
 * every change here. */
export function usePeerSessions(linkId: string, tick: number) {
  const [view, setView] = useState<SessionsView>(EMPTY);
  const [err, setErr] = useState("");
  const load = useCallback(async () => {
    try {
      setView(await fetchPeerSessions(linkId));
      setErr("");
    } catch (e) {
      setErr(errText(e));
    }
  }, [linkId]);
  useEffect(() => {
    void load();
  }, [load, tick]);
  return { view, setView, err, reload: load };
}

export function ShareSessionsDialog(props: {
  link: PeerLink;
  sandbox: boolean;
  view: SessionsView;
  onChange(view: SessionsView): void;
  onClose(): void;
}) {
  const { link, sandbox, view, onChange, onClose } = props;
  const { data: instances } = useInstances();
  const sessions = useMemo(() => shareableSessions(instances), [instances]);
  const [title, setTitle] = useState("");
  const [mode, setMode] = useState<PeerMode>(sandbox ? "gated" : "full");
  const [understood, setUnderstood] = useState(false);
  const [busy, setBusy] = useState("");
  const [err, setErr] = useState("");
  const shared = new Set(view.mine.map((m) => m.title));
  const ok = canShare({ title, mode, understood, busy: !!busy });

  const share = async () => {
    if (!ok) return;
    setBusy("share");
    setErr("");
    try {
      onChange(await sharePeerSession(link.link_id, title, mode));
      toast(
        mode === "full"
          ? `${link.peer_name} can open ${displayName(title)} now`
          : `Sharing a sandboxed copy of ${displayName(title)} with ${link.peer_name}`
      );
      setTitle("");
      setUnderstood(false);
    } catch (e) {
      setErr(errText(e));
    } finally {
      setBusy("");
    }
  };

  const withdraw = async (t: string) => {
    setBusy("withdraw:" + t);
    setErr("");
    try {
      onChange(await withdrawPeerSession(link.link_id, t));
    } catch (e) {
      setErr(errText(e));
    } finally {
      setBusy("");
    }
  };

  return (
    <Modal id="peer-sessions-share" role="dialog" aria-modal="true" portal onClose={onClose}>
      <form
        id="peer-sessions-share-form"
        onSubmit={(e) => {
          e.preventDefault();
          void share();
        }}
      >
        <h2>Share a session with {link.peer_name}</h2>
        <p className="set-hint">
          It shows on {link.peer_name}’s rail under your name; they open it and type into it. Withdraw it here
          whenever you like.
        </p>
        {sessions.length === 0 ? (
          <span className="set-hint">Start a session first — you share sessions that exist.</span>
        ) : (
          <ul className="peer-guest-pick" aria-label="Sessions">
            {sessions.map((s) => (
              <li key={s.title}>
                <label>
                  <input
                    type="radio"
                    name="peer-share-session"
                    checked={title === s.title}
                    onChange={() => setTitle(s.title)}
                    data-session={s.title}
                  />{" "}
                  {displayName(s.title)}
                  {s.branch && s.branch !== s.title ? <span className="set-hint"> · {s.branch}</span> : null}
                  {shared.has(s.title) ? <span className="set-hint"> · shared</span> : null}
                </label>
              </li>
            ))}
          </ul>
        )}
        <fieldset className="peer-share-modes">
          <legend className="set-hint">How</legend>
          {MODE_CHOICES.map((c) => {
            const off = c.mode === "gated" && !sandbox;
            return (
              <label key={c.mode} className={off ? "muted" : undefined}>
                <input
                  type="radio"
                  name="peer-share-mode"
                  value={c.mode}
                  checked={mode === c.mode}
                  disabled={off}
                  onChange={() => setMode(c.mode)}
                />{" "}
                {c.label}
                <span className="set-hint peer-share-mode-hint">
                  {off ? "Needs Linux or macOS (the sandbox)." : c.hint}
                </span>
              </label>
            );
          })}
        </fieldset>
        {mode === "gated" && link.shared && (
          <p className="set-hint">
            This link already has a shared folder: a link carries one gated session, so withdraw it first — or
            share this one in full.
          </p>
        )}
        {mode === "full" && (
          <>
            <div className="peer-danger peer-guest-warn" id="peer-sessions-warn" role="alert">
              <strong>This lets {link.peer_name} act on your computer.</strong> In that session they type at your
              agent as you: they can read and change the files in its worktree, run commands, and spend your
              agent’s credits — exactly what a guest link gives. Only share a session with someone you would hand
              your keyboard to, and withdraw it when you’re done.
            </div>
            <label className="peer-guest-ack">
              <input
                type="checkbox"
                id="peer-sessions-ack"
                checked={understood}
                onChange={(e) => setUnderstood(e.target.checked)}
              />{" "}
              I understand this lets {link.peer_name} act on my machine
            </label>
          </>
        )}
        {err && <p className="error">{err}</p>}
        <div className="modal-actions">
          <button type="button" id="peer-sessions-cancel" onClick={onClose}>
            Close
          </button>
          <button
            type="submit"
            className="btn-primary"
            id="peer-sessions-share-btn"
            disabled={!ok}
            title={!title ? "Pick a session" : mode === "full" && !understood ? "Tick “I understand” first" : undefined}
          >
            {busy === "share" ? "Sharing…" : "Share"}
          </button>
        </div>
        {view.mine.length > 0 && (
          <>
            <h3 className="peer-share-sub">Shared with {link.peer_name}</h3>
            <ul className="peer-guest-links" id="peer-sessions-mine">
              {view.mine.map((m) => (
                <li key={m.title} className="peer-row">
                  <span className="peer-grow">{mineLine(m)}</span>
                  <button
                    type="button"
                    className="test-btn peer-danger"
                    disabled={!!busy}
                    data-withdraw={m.title}
                    onClick={() => void withdraw(m.title)}
                  >
                    {busy === "withdraw:" + m.title ? "Withdrawing…" : "Withdraw"}
                  </button>
                </li>
              ))}
            </ul>
          </>
        )}
      </form>
    </Modal>
  );
}

/** What they share with me, on the card: one line each, Open selects its
 * row on the rail (looked up when pressed — the card renders outside the
 * query provider in its tests, so no hook here). */
export function TheirSessions(props: { link: PeerLink; view: SessionsView }) {
  const { link, view } = props;
  if (view.their_unsupported) {
    return (
      <p className="set-hint" id="peer-sessions-theirs">
        {link.peer_name} can’t share sessions yet — they need to update MindFlock.
      </p>
    );
  }
  if (!view.theirs.length) return null;
  const rowOf = (t: string) =>
    (queryClient.getQueryData<Instance[]>(["instances"]) || []).find(
      (i) => isPeerRow(i) && i.peer_link === link.link_id && i.title.endsWith("::" + t)
    );
  return (
    <div id="peer-sessions-theirs">
      <span className="set-hint">{link.peer_name} shares with you:</span>
      <ul className="peer-guest-links">
        {view.theirs.map((t) => {
          return (
            <li key={t.title} className="peer-row">
              <span className="peer-grow">{theirsLine(t)}</span>
              <button
                type="button"
                className="test-btn"
                title="Open it on your rail"
                onClick={() => {
                  const row = rowOf(t.title);
                  if (!row) return toast("It shows on your rail in a moment");
                  selectSession(row.title);
                  useUi.getState().closeDialog();
                }}
              >
                Open
              </button>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
