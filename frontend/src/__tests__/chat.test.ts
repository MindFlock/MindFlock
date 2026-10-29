/** The session chat (lib/chat.ts, components/chat/ChatPanel, docs/people.md
 * "A small chat"): a little window per session for the people on it.
 *
 * Pinned here:
 *  - the open / minimize / close state machine, and that minimizing never
 *    happens by itself to a window already open;
 *  - unread counting: a message on a minimized window counts, on an open
 *    one doesn't, and opening reads them;
 *  - arrivals pop the chat — minimized after a close, and never again for
 *    the person who was on it when it was closed; a guest's browser opens
 *    outright when spoken to; another device's rows never get one;
 *  - a peer's shared session (`<peer>::<title>`) is a chat like mine: it
 *    pops open when the sharer writes (a pill after a close), and an event
 *    for one the rail hasn't listed yet still lands;
 *  - message merging dedupes by id and keeps id order;
 *  - the live store: an event for an unknown title loads it first, my own
 *    messages never count, replays are skipped by the bridge;
 *  - App.tsx mounts <ChatPanel />. */
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("../api/client", () => ({ instApi: vi.fn() }));
import { instApi } from "../api/client";
import {
  EMPTY_CHAT,
  _resetChat,
  applyChatEvent,
  applyJoinEvent,
  chatable,
  closeWindow,
  mergeMessages,
  minimizeWindow,
  onJoined,
  onMessage,
  openWindow,
  peerChatName,
  setPeerRows,
  totalUnread,
  type ChatMessage,
} from "../lib/chat";
import appSrc from "../App.tsx?raw";

const msg = (id: string, from = "o", text = "hi"): ChatMessage => ({ id, from, name: from, text, ts: 1 });
const env = (event: string, session: string, data: object) =>
  ({ seq: 1, event, session, old: null, new: null, ts: 1, data: data as Record<string, unknown> });

afterEach(() => {
  _resetChat();
  vi.mocked(instApi).mockReset();
});

describe("windows", () => {
  it("opens, minimizes, reopens and closes", () => {
    let s = openWindow(EMPTY_CHAT, "alpha");
    expect(s.windows).toEqual([{ title: "alpha", mode: "open", unread: 0 }]);
    expect(openWindow(s, "alpha")).toBe(s); // already open: no change
    expect(openWindow(s, "alpha", "min")).toBe(s); // never shrinks an open one
    s = minimizeWindow(s, "alpha");
    expect(s.windows[0].mode).toBe("min");
    expect(minimizeWindow(s, "alpha")).toBe(s);
    s = openWindow(s, "beta", "min");
    expect(s.windows.map((w) => [w.title, w.mode])).toEqual([["alpha", "min"], ["beta", "min"]]);
    s = closeWindow(s, "alpha", ["L1"]);
    expect(s.windows.map((w) => w.title)).toEqual(["beta"]);
    expect(s.closedTitles).toEqual({ alpha: true });
    expect(s.noPop).toEqual({ "L1|alpha": true });
  });

  it("counts unread on a minimized window only, and opening reads them", () => {
    let s = openWindow(EMPTY_CHAT, "alpha");
    expect(onMessage(s, "alpha")).toBe(s);
    s = minimizeWindow(s, "alpha");
    s = onMessage(onMessage(s, "alpha"), "alpha");
    expect(s.windows[0].unread).toBe(2);
    // A message for a window that isn't there opens it minimized with one.
    s = onMessage(s, "beta");
    expect(s.windows.find((w) => w.title === "beta")).toEqual({ title: "beta", mode: "min", unread: 1 });
    expect(totalUnread(s)).toBe(3);
    s = openWindow(s, "alpha");
    expect(s.windows.find((w) => w.title === "alpha")?.unread).toBe(0);
    expect(totalUnread(s)).toBe(1);
  });

  it("a guest's browser opens outright when spoken to", () => {
    const s = onMessage(EMPTY_CHAT, "alpha", { guest: true });
    expect(s.windows).toEqual([{ title: "alpha", mode: "open", unread: 0 }]);
  });

  it("an arrival pops it; after a close, minimized; never again for that person", () => {
    let s = onJoined(EMPTY_CHAT, "alpha", "L1");
    expect(s.windows).toEqual([{ title: "alpha", mode: "open", unread: 0 }]);
    s = closeWindow(s, "alpha", ["L1"]);
    expect(onJoined(s, "alpha", "L1")).toBe(s); // Pat again: no
    const t = onJoined(s, "alpha", "L2"); // someone else: a pill, not a card
    expect(t.windows).toEqual([{ title: "alpha", mode: "min", unread: 0 }]);
    expect(onJoined(s, "beta", "L1").windows.map((w) => w.mode)).toEqual(["open"]);
  });

  it("another device's row never gets a chat", () => {
    expect(chatable("mac::alpha")).toBe(false);
    expect(chatable("")).toBe(false);
    expect(onJoined(EMPTY_CHAT, "mac::alpha", "L1")).toBe(EMPTY_CHAT);
    expect(onMessage(EMPTY_CHAT, "mac::alpha")).toBe(EMPTY_CHAT);
  });
});

describe("a peer's shared session", () => {
  it("is chatable once the rail lists it, and says whose it is", () => {
    expect(chatable("Erik::alpha")).toBe(false);
    setPeerRows([{ title: "Erik::alpha", name: "Erik" }]);
    expect(chatable("Erik::alpha")).toBe(true);
    expect(peerChatName("Erik::alpha")).toBe("Erik");
    expect(chatable("mac::alpha")).toBe(false); // a device's row still isn't
    expect(peerChatName("alpha")).toBe("");
  });

  it("pops open when the sharer writes; a pill once I closed it", () => {
    setPeerRows([{ title: "Erik::alpha", name: "Erik" }]);
    const s = onMessage(EMPTY_CHAT, "Erik::alpha", { peer: true });
    expect(s.windows).toEqual([{ title: "Erik::alpha", mode: "open", unread: 0 }]);
    const closed = closeWindow(s, "Erik::alpha");
    expect(onMessage(closed, "Erik::alpha", { peer: true }).windows).toEqual([
      { title: "Erik::alpha", mode: "min", unread: 1 },
    ]);
  });

  it("an event for a row not listed yet loads its chat and opens it", async () => {
    const mod = await import("../lib/chat");
    vi.mocked(instApi).mockResolvedValue({ messages: [msg("c4", "peer:L:owner")], me: { id: "o", name: "Pat" } });
    await applyChatEvent(env("session.chat", "Erik::alpha", msg("c4", "peer:L:owner")));
    expect(vi.mocked(instApi)).toHaveBeenCalledWith("Erik::alpha", "/chat");
    expect(mod.peekChat().windows).toEqual([{ title: "Erik::alpha", mode: "open", unread: 0 }]);
    // My own line echoed back never pops anything.
    mod.closeChat("Erik::alpha");
    await applyChatEvent(env("session.chat", "Erik::alpha", msg("c5", "o")));
    expect(mod.peekChat().windows).toEqual([]);
  });
});

describe("mergeMessages", () => {
  it("dedupes by id, keeps id order, and is stable when nothing is new", () => {
    const prev = [msg("c1"), msg("c3")];
    const { list, added } = mergeMessages(prev, [msg("c3"), msg("c2"), msg("c10"), msg("c2")]);
    expect(list.map((m) => m.id)).toEqual(["c1", "c2", "c3", "c10"]);
    expect(added.map((m) => m.id)).toEqual(["c2", "c10"]);
    expect(mergeMessages(list, [msg("c1")]).list).toBe(list);
  });
});

describe("the live store", () => {
  it("an event for an unknown title loads it, then counts a message from someone else", async () => {
    const mod = await import("../lib/chat");
    vi.mocked(instApi).mockResolvedValue({ messages: [msg("c1", "o"), msg("c2", "guest:L1")], me: { id: "o", name: "Erik" } });
    await applyChatEvent(env("session.chat", "alpha", msg("c2", "guest:L1")));
    expect(vi.mocked(instApi)).toHaveBeenCalledWith("alpha", "/chat");
    // The history came in silently; only the live message counts.
    expect(mod.peekChat().windows).toEqual([{ title: "alpha", mode: "min", unread: 1 }]);
  });

  it("my own message never counts and a join opens the owner's chat", async () => {
    const mod = await import("../lib/chat");
    vi.mocked(instApi).mockResolvedValue({ messages: [], me: { id: "o", name: "Erik" } });
    await mod.applyChatEvent(env("session.chat", "alpha", msg("c1", "o")));
    expect(mod.peekChat().windows).toEqual([]);
    await mod.applyChatEvent(env("session.chat", "alpha", msg("c2", "guest:L1")));
    expect(mod.peekChat().windows).toEqual([{ title: "alpha", mode: "min", unread: 1 }]);
    // The same message again (event + poll) doesn't count twice.
    await mod.applyChatEvent(env("session.chat", "alpha", msg("c2", "guest:L1")));
    expect(mod.peekChat().windows[0].unread).toBe(1);
    applyJoinEvent(env("guest.focus_changed", "beta", { link: "L1", label: "Pat", title: "beta", joined: true }));
    expect(mod.peekChat().windows.map((w) => [w.title, w.mode])).toEqual([["alpha", "min"], ["beta", "open"]]);
    // A move (not an arrival) does nothing.
    applyJoinEvent(env("guest.focus_changed", "gamma", { link: "L1", label: "Pat", title: "gamma", joined: false }));
    expect(mod.peekChat().windows).toHaveLength(2);
  });

  it("skips replayed events at the bridge", async () => {
    const mod = await import("../lib/chat");
    const subs: Record<string, (e: unknown) => void> = {};
    vi.stubGlobal("window", {
      mindflock: {
        events: {
          subscribe: (name: string, cb: (e: unknown) => void) => (subs[name] = cb),
          isReplay: () => true,
        },
      },
    });
    expect(mod.installChatEvents(false)).toBe(true);
    subs["guest.focus_changed"](env("guest.focus_changed", "beta", { link: "L1", title: "beta", joined: true }));
    expect(mod.peekChat().windows).toEqual([]);
    vi.unstubAllGlobals();
  });
});

describe("wiring", () => {
  it("App.tsx mounts <ChatPanel /> once", () => {
    expect(appSrc).toContain('import { ChatPanel } from "./components/chat/ChatPanel"');
    expect(appSrc.split("<ChatPanel />").length - 1).toBe(1);
  });
});
