/** Settings → People: the moment a pairing lands, on both sides
 * (docs/peer-link.md "How to use").
 *
 *  - The INVITER: "Bob joined — fix-login and api are open to them", the
 *    safety number and It matches. What the invite opened was shared by the
 *    server as they joined; one that couldn't be shared says why. An invite
 *    with no sessions offers **Share a session…** (peer/ShareSessions'
 *    dialog), which waits for It matches like any manual share.
 *  - The JOINER: "You're connected to Erik — fix-login (full) and api
 *    (gated) are on your rail", one **Open** per session (it selects that
 *    `<peer>::<title>` row), the safety number and It matches. Their list is
 *    read from `GET /api/peer/links/<id>/sessions` and again whenever
 *    `peer.session_shared` / `peer.session_unshared` names this link (an
 *    invite's sessions may land a moment after the join). */

import { useEffect, useState } from "react";
import type { Instance } from "../../../../api/types";
import {
  connectedText,
  joinedText,
  shareErrorText,
  type PeerInviteSession,
  type PeerLink,
} from "../../../../lib/peer";
import { isPeerRow } from "../../../../lib/peerSessions";
import { selectSession } from "../../../../lib/sessionActions";
import { toast } from "../../../../lib/toast";
import { queryClient, type EventEnvelope } from "../../../../state/queries";
import { useUi } from "../../../../state/store";
import { ShareSessionsDialog, usePeerSessions } from "./ShareSessions";

export interface Joined {
  link_id: string;
  name: string;
  sas: string;
  repaired?: boolean;
  /** Inviter: what the invite they joined opens (peer.link_added). */
  sessions?: PeerInviteSession[];
}

/** Open THEIR shared session `title`: its `<peer>::<title>` row. */
export function openTheirs(linkId: string, peerName: string, title: string): void {
  const row = (queryClient.getQueryData<Instance[]>(["instances"]) || []).find(
    (i) => isPeerRow(i) && i.peer_link === linkId && i.title.endsWith("::" + title)
  );
  if (!row) toast("It shows on your rail in a moment");
  selectSession(row ? row.title : peerName + "::" + title);
  useUi.getState().closeDialog();
}

function SafetyLine(props: { sas: string; verified: boolean; onMatch(): void }) {
  return (
    <div className="set-row" id="peer-joined-sas">
      <span className="set-hint">
        Safety number <code className="peer-sas">{props.sas}</code>
        {props.verified ? " · ✓ checked with them." : " · read it to each other."}
      </span>
      {!props.verified && (
        <button type="button" className="test-btn" id="peer-joined-match" onClick={props.onMatch}>
          It matches
        </button>
      )}
    </div>
  );
}

function ShareLater(props: { link: PeerLink; sandbox: boolean; onClose(): void }) {
  const { view, setView } = usePeerSessions(props.link.link_id, 0);
  return (
    <ShareSessionsDialog link={props.link} sandbox={props.sandbox} view={view} onChange={setView} onClose={props.onClose} />
  );
}

function Inviter(props: {
  joined: Joined;
  link: PeerLink | null;
  sandbox: boolean;
  onMatch(): void;
  onDone(): void;
}) {
  const { joined, link } = props;
  const [sharing, setSharing] = useState(false);
  const verified = !!link?.sas_verified;
  const errors = link?.share_errors || [];
  const none = !joined.sessions?.length;
  return (
    <>
      <p id="peer-joined-line">
        <strong>{joinedText(joined.name, joined.sessions, errors, joined.repaired)}</strong>
      </p>
      <SafetyLine sas={joined.sas} verified={verified} onMatch={props.onMatch} />
      {!verified && (
        <span className="set-hint">If theirs is different, press It doesn't match on their card below.</span>
      )}
      {errors.map((e) => (
        <p className="error" key={e.title} data-share-error={e.title}>
          {shareErrorText(e)}
        </p>
      ))}
      <div className="set-row">
        {none && (
          <button
            type="button"
            className="test-btn"
            id="peer-joined-share"
            disabled={!link || !verified}
            title={verified ? undefined : "Press It matches first — sharing waits for the safety number"}
            onClick={() => setSharing(true)}
          >
            Share a session…
          </button>
        )}
        <button type="button" className="test-btn" id="peer-joined-done" onClick={props.onDone}>
          {verified ? "Done" : "Later"}
        </button>
      </div>
      {sharing && link && <ShareLater link={link} sandbox={props.sandbox} onClose={() => setSharing(false)} />}
    </>
  );
}

function Joiner(props: { joined: Joined; link: PeerLink | null; tick: number; onMatch(): void; onDone(): void }) {
  const { joined } = props;
  const [bump, setBump] = useState(0);
  const { view } = usePeerSessions(joined.link_id, props.tick + bump);
  useEffect(() => {
    const ev = window.mindflock?.events;
    if (!ev) return;
    const again = (env: EventEnvelope) => {
      const d = (env.data || {}) as Record<string, unknown>;
      if (d.link_id === joined.link_id) setBump((b) => b + 1);
    };
    const offs = [ev.subscribe("peer.session_shared", again), ev.subscribe("peer.session_unshared", again)];
    return () => offs.forEach((off) => off());
  }, [joined.link_id]);
  const verified = !!props.link?.sas_verified;
  return (
    <>
      <p id="peer-joined-line">
        <strong>{connectedText(joined.name, view.theirs)}</strong>
      </p>
      {view.theirs.length > 0 ? (
        <ul className="peer-guest-links" id="peer-joined-theirs">
          {view.theirs.map((t) => (
            <li key={t.title} className="peer-row">
              <span className="peer-grow">{t.name || t.title}</span>
              <button
                type="button"
                className="test-btn"
                data-open={t.title}
                title="Open it on your rail"
                onClick={() => openTheirs(joined.link_id, joined.name, t.title)}
              >
                Open
              </button>
            </li>
          ))}
        </ul>
      ) : (
        <span className="set-hint">Anything {joined.name} shares with you appears here and on your rail.</span>
      )}
      <SafetyLine sas={joined.sas} verified={verified} onMatch={props.onMatch} />
      <div className="set-row">
        <button type="button" className="test-btn" id="peer-joined-done" onClick={props.onDone}>
          Done
        </button>
      </div>
    </>
  );
}

export function JoinedCard(props: {
  side: "inviter" | "joiner";
  joined: Joined;
  /** The link as GET /api/peer last listed it (null until it has). */
  link: PeerLink | null;
  sandbox: boolean;
  tick: number;
  onMatch(): void;
  onDone(): void;
}) {
  return props.side === "inviter" ? (
    <Inviter joined={props.joined} link={props.link} sandbox={props.sandbox} onMatch={props.onMatch} onDone={props.onDone} />
  ) : (
    <Joiner joined={props.joined} link={props.link} tick={props.tick} onMatch={props.onMatch} onDone={props.onDone} />
  );
}
