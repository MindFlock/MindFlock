/** The conversation with one person, wherever you are: People's Talk group
 * (with its own send box) and the shared-session viewer (above its send box).
 * Fetched on mount, again on every peer.message for this connection and
 * whenever `refresh` changes (after a send); seeing it marks it read.
 * Senders are people — "Bea", "you", "your agent" — never a session id. */

import { useEffect, useRef, useState } from "react";
import { api } from "../../../../api/client";
import { relTime } from "../../../../lib/format";
import { toast } from "../../../../lib/toast";
import type { EventEnvelope } from "../../../../state/queries";
import type { PeerMessage } from "../../../../lib/peer";
import { errText } from "./useAct";

/** Who said it, in plain words. */
export function senderText(m: Pick<PeerMessage, "dir" | "by">, peerName: string): string {
  if (m.dir === "in") return peerName;
  return m.by === "agent" ? "your agent" : "you";
}

export function MessageLog({ msgs, peerName, now }: { msgs: PeerMessage[]; peerName: string; now?: number }) {
  const end = useRef<HTMLLIElement | null>(null);
  useEffect(() => {
    end.current?.scrollIntoView?.({ block: "nearest" });
  }, [msgs.length]);
  if (!msgs.length) return <p className="set-hint peer-msg-empty">No messages yet — say hello.</p>;
  return (
    <ul className="peer-msg-log">
      {msgs.map((m, i) => (
        <li key={m.id} className={"peer-msg peer-msg-" + m.dir} ref={i === msgs.length - 1 ? end : undefined}>
          <span className="peer-msg-who">{senderText(m, peerName)}</span>
          {m.ts ? <span className="muted peer-msg-at"> · {relTime(m.ts, { now })}</span> : null}
          <span className="peer-msg-text">{m.text}</span>
        </li>
      ))}
    </ul>
  );
}

export function MessageThread(props: {
  linkId: string;
  peerName: string;
  connected: boolean;
  /** Draw the send box too (People's Talk group); the viewer has its own. */
  withSend?: boolean;
  /** Bump to refetch (the viewer, after it sends). */
  refresh?: number;
}) {
  const { linkId, peerName, connected, withSend, refresh } = props;
  const base = "/api/peer/links/" + encodeURIComponent(linkId);
  const [msgs, setMsgs] = useState<PeerMessage[]>([]);
  const [err, setErr] = useState("");
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const [bump, setBump] = useState(0);

  useEffect(() => {
    let live = true;
    api<{ messages: PeerMessage[]; unread: number }>(base + "/messages")
      .then((r) => {
        if (!live) return;
        setMsgs(r.messages || []);
        setErr("");
        if (r.unread) void api(base + "/messages/read", { json: {} }).catch(() => undefined);
      })
      .catch((e) => live && setErr(errText(e)));
    return () => {
      live = false;
    };
  }, [base, refresh, bump]);

  // A message from them (or their agent) lands while you look.
  useEffect(() => {
    const ev = window.mindflock?.events;
    if (!ev) return;
    return ev.subscribe("peer.message", (env: EventEnvelope) => {
      if (String((env.data || {}).link_id || "") === linkId) setBump((b) => b + 1);
    });
  }, [linkId]);

  const send = async () => {
    const text = draft.trim();
    if (!text || sending) return;
    setSending(true);
    try {
      const r = await api<{ delivered: boolean }>(base + "/message", { json: { text } });
      setDraft("");
      if (!r.delivered) toast(`${peerName} isn't taking messages right now — it's in your log`);
      setBump((b) => b + 1);
    } catch (e) {
      setErr("Couldn't send: " + errText(e));
    }
    setSending(false);
  };

  return (
    <div className="peer-msgs">
      <MessageLog msgs={msgs} peerName={peerName} />
      {withSend && (
        <div className="peer-row peer-msg-send">
          <textarea
            rows={2}
            placeholder={`Message ${peerName} — their agent sees it too`}
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && (e.ctrlKey || e.metaKey) && draft.trim()) void send();
            }}
          />
          <button type="button" className="test-btn" disabled={sending || !draft.trim() || !connected} onClick={() => void send()}>
            {sending ? "Sending…" : "Send"}
          </button>
        </div>
      )}
      {withSend && !connected && <span className="set-hint">{peerName} is offline — send once they're back.</span>}
      {err && <p className="error">{err}</p>}
    </div>
  );
}
