/** The session chat's floating windows (lib/chat.ts, docs/people.md "A small
 * chat"): bottom-right, one per session — a card when open, a pill with an
 * unread count when minimized; a full-width bottom sheet on a phone. They
 * pop up by themselves (someone arrived on one of my sessions, someone
 * wrote); the small "Chat" launcher opens one by hand for the session in
 * focus while someone else is on it (a guest's browser: always; a peer's
 * shared session on my rail: always — its sharer is on it, and the header
 * says "with <peer>"). Mounted once in App.tsx. Styles: ChatPanel.css. */

import { useEffect, useMemo, useRef, useState, type KeyboardEvent } from "react";
import {
  CHAT_MAX,
  CHAT_POLL_MS,
  chatable,
  closeChat,
  installChatEvents,
  minimizeChat,
  openChat,
  peerChatName,
  pollChats,
  sendChat,
  setPeerRows,
  useChat,
  type ChatMe,
  type ChatMessage,
  type ChatWindow,
} from "../../lib/chat";
import { guestName, useGuestFocus } from "../../lib/guestFocus";
import { isPeerRow } from "../../lib/peerSessions";
import { windowName } from "../../lib/windowName";
import { useConfig, useInstances } from "../../state/queries";
import { useUi } from "../../state/store";
import "./ChatPanel.css";

export function ChatPanel() {
  const { data: config } = useConfig();
  const guest = config ? !!config.guest : null;
  const { state, msgs, me } = useChat();
  const { focus } = useGuestFocus();
  const { data: instances } = useInstances();
  const focused = useUi((s) => s.focused) || "";
  useUi((s) => s.aliases); // re-render on a rename: headers use windowName

  // The bus: events.js may load after the first render, so keep trying.
  useEffect(() => {
    if (guest === null) return;
    if (installChatEvents(guest)) return;
    const t = setInterval(() => {
      if (installChatEvents(guest)) clearInterval(t);
    }, 1000);
    return () => clearInterval(t);
  }, [guest]);

  // A peer's shared sessions on my rail chat over the peer link.
  const peerNames = useMemo(() => {
    const out: Record<string, string> = {};
    for (const r of (instances || []).filter(isPeerRow)) out[r.title] = r.device_label || r.device || "";
    return out;
  }, [instances]);
  useEffect(() => {
    setPeerRows(Object.entries(peerNames).map(([title, name]) => ({ title, name })));
  }, [peerNames]);
  const peerOf = (title: string) => (title in peerNames ? peerNames[title] || title.split("::")[0] : peerChatName(title));

  const any = state.windows.length > 0;
  useEffect(() => {
    if (!any) return;
    const t = setInterval(pollChats, CHAT_POLL_MS);
    return () => clearInterval(t);
  }, [any]);

  const peopleOn = (title: string) => Object.values(focus).filter((f) => f.title === title);
  // Who the header says I'm with: the peer whose session it is, then
  // whoever else is on it.
  const withWhom = (title: string) => {
    const peer = peerOf(title);
    return [...(peer ? [peer] : []), ...peopleOn(title).map((f) => guestName(f.label))];
  };
  const hasWindow = state.windows.some((w) => w.title === focused);
  const showLauncher =
    guest !== null &&
    !!focused &&
    (chatable(focused) || !!peerOf(focused)) &&
    !hasWindow &&
    (guest || !!peerOf(focused) || peopleOn(focused).length > 0);

  if (!any && !showLauncher) return null;
  return (
    <div className="chat-dock" aria-label="Session chat">
      {state.windows.map((w) =>
        w.mode === "open" ? (
          <ChatCard
            key={w.title}
            title={w.title}
            messages={msgs[w.title] || []}
            me={me}
            people={withWhom(w.title)}
            onClose={() => closeChat(w.title, peopleOn(w.title).map((f) => f.link))}
          />
        ) : (
          <ChatPill key={w.title} w={w} />
        )
      )}
      {showLauncher && (
        <button
          type="button"
          className="chat-launcher"
          title={"Chat with the people on " + windowName(focused)}
          onClick={() => openChat(focused)}
        >
          💬 Chat
        </button>
      )}
    </div>
  );
}

function ChatPill({ w }: { w: ChatWindow }) {
  return (
    <button
      type="button"
      className={"chat-pill" + (w.unread ? " unread" : "")}
      data-chat={w.title}
      title={w.unread ? `${w.unread} new — open the chat` : "Open the chat"}
      onClick={() => openChat(w.title)}
    >
      <span className="chat-pill-name">💬 {windowName(w.title)}</span>
      {w.unread > 0 && <span className="chat-unread">{w.unread}</span>}
    </button>
  );
}

function ChatCard({
  title,
  messages,
  me,
  people,
  onClose,
}: {
  title: string;
  messages: ChatMessage[];
  me: ChatMe | null;
  people: string[];
  onClose: () => void;
}) {
  const [draft, setDraft] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const listRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    const el = listRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages.length]);

  const send = async () => {
    if (busy || !draft.trim()) return;
    setBusy(true);
    const why = await sendChat(title, draft);
    setBusy(false);
    setError(why);
    if (!why) setDraft("");
    inputRef.current?.focus();
  };
  const onKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    e.stopPropagation(); // the app's keymap must not see chat typing
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      void send();
    } else if (e.key === "Escape") {
      e.preventDefault();
      minimizeChat(title);
    }
  };

  return (
    <section className="chat-card" data-chat={title} aria-label={"Chat on " + windowName(title)}>
      <header className="chat-head">
        <div className="chat-title">
          <span className="chat-name">{windowName(title)}</span>
          {people.length > 0 && <span className="chat-people">with {people.join(", ")}</span>}
        </div>
        <button type="button" className="chat-btn" title="Minimize" aria-label="Minimize" onClick={() => minimizeChat(title)}>
          ▁
        </button>
        <button type="button" className="chat-btn" title="Close" aria-label="Close" onClick={onClose}>
          ×
        </button>
      </header>
      <div className="chat-list" ref={listRef}>
        {messages.length === 0 && (
          <p className="chat-empty">Say hi — only the people on this session see this (agents don't).</p>
        )}
        {messages.map((m) => {
          const mine = !!me && m.from === me.id;
          return (
            <div key={m.id} className={"chat-msg" + (mine ? " mine" : "")}>
              {!mine && <span className="chat-from">{m.name}</span>}
              <span className="chat-text">{m.text}</span>
            </div>
          );
        })}
      </div>
      {error && <p className="chat-error">{error}</p>}
      <textarea
        ref={inputRef}
        className="chat-input"
        rows={1}
        maxLength={CHAT_MAX}
        placeholder="Message…  (Enter sends)"
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onKeyDown={onKey}
        aria-label="Message"
      />
    </section>
  );
}
