/** Shared sessions over a peer link (lib/peerSessions, docs/peer-link.md
 * "Shared sessions — CONTRACT"): a peer's rows grouped under their name on
 * the rail, the words for the two modes, the picker (which sessions, the
 * "I understand" gate for a full join), the focus reporter, and the wiring
 * in the Sidebar / People card / row chip. */

import { afterEach, describe, expect, it, vi } from "vitest";
import type { Instance } from "../api/types";

vi.mock("../api/client", () => ({ api: vi.fn(async () => ({})) }));

import { api } from "../api/client";
import { useUi } from "../state/store";
import {
  FOCUS_HEARTBEAT_MS,
  MODE_CHOICES,
  OFFLINE_KEEP_MS,
  _resetPeerMemory,
  canShare,
  forgetPeerRow,
  hereWords,
  installPeerFocus,
  installPeerRowMemory,
  isOfflinePeerRow,
  isPeerRow,
  mineLine,
  modeWords,
  nextOfflineExpiry,
  notePeerRows,
  offlinePeerRows,
  peerBackToast,
  peerGroupWords,
  peerGroups,
  peerLinkDown,
  peerLinkUp,
  peerPaneStrip,
  peerRowChip,
  peerRowKey,
  peerRowTooltip,
  peerSessionName,
  peerSessionNote,
  shareableSessions,
  theirsLine,
  withOfflinePeers,
  type PeerRow,
} from "../lib/peerSessions";

const raw = (glob: Record<string, string>) => Object.values(glob)[0];
const SIDEBAR = raw(
  import.meta.glob<string>("../components/sidebar/Sidebar.tsx", { query: "?raw", import: "default", eager: true })
);
const PANE = raw(
  import.meta.glob<string>("../components/grid/Pane.tsx", { query: "?raw", import: "default", eager: true })
);
const TOASTS = raw(
  import.meta.glob<string>("../components/EventToasts.tsx", { query: "?raw", import: "default", eager: true })
);
const BELL = raw(
  import.meta.glob<string>("../components/NotificationsBell.tsx", { query: "?raw", import: "default", eager: true })
);
const ROW = raw(
  import.meta.glob<string>("../components/sidebar/SidebarRow.tsx", { query: "?raw", import: "default", eager: true })
);
const CARD = raw(
  import.meta.glob<string>("../components/settings/screens/peer/PeerLinkCard.tsx", {
    query: "?raw",
    import: "default",
    eager: true,
  })
);
const DIALOG = raw(
  import.meta.glob<string>("../components/settings/screens/peer/ShareSessions.tsx", {
    query: "?raw",
    import: "default",
    eager: true,
  })
);

const inst = (title: string, extra: Partial<PeerRow> = {}): Instance =>
  ({ title, branch: "", status: "running", ...extra }) as unknown as Instance;

const peer = (title: string, device: string, extra: Partial<PeerRow> = {}): Instance =>
  inst(device + "::" + title, {
    device,
    device_label: device,
    remote_peer: true,
    peer_link: "ab".repeat(16),
    peer_mode: "full",
    peer_share: true,
    ...extra,
  });

describe("peer rows on the rail", () => {
  it("isPeerRow: only rows the server marks remote_peer", () => {
    expect(isPeerRow(peer("api", "Bob"))).toBe(true);
    expect(isPeerRow(inst("mac::api", { device: "mac" }))).toBe(false);
    expect(isPeerRow(inst("api"))).toBe(false);
    expect(isPeerRow(null)).toBe(false);
  });

  it("groups by peer in first-seen order, devices and local rows left out", () => {
    const rows = [
      inst("local"),
      peer("api", "Bob"),
      inst("mac::x", { device: "mac" }),
      peer("docs", "Ann", { peer_mode: "gated", peer_link: "cd".repeat(16) }),
      peer("web", "Bob"),
    ];
    const g = peerGroups(rows);
    expect(g.map((x) => [x.device, x.rows.map((r) => r.title)])).toEqual([
      ["Bob", ["Bob::api", "Bob::web"]],
      ["Ann", ["Ann::docs"]],
    ]);
    expect(g[1].link).toBe("cd".repeat(16));
    expect(peerGroups([])).toEqual([]);
  });

  it("the mode words, tooltip and row chip", () => {
    expect(modeWords("gated")).toBe("gated to its folder");
    expect(modeWords("full")).toBe("full");
    expect(modeWords(undefined)).toBe("full");
    const gated = peer("docs", "Ann", { peer_mode: "gated" }) as PeerRow;
    expect(peerRowTooltip(gated)).toBe("shared by Ann · gated to its folder");
    expect(peerRowTooltip(peer("api", "Bob") as PeerRow)).toBe("shared by Bob · full");
    const chip = peerRowChip(gated);
    expect(chip.label).toBe("⇄ Ann · gated");
    expect(chip.title).toMatch(/runs on Ann's machine/);
    // The chip's dot: connected, else (a remembered row) offline.
    expect(chip.connected).toBe(true);
    const gone = peerRowChip({ ...(gated as PeerRow), peer_offline: true });
    expect(gone.connected).toBe(false);
    expect(gone.title).toMatch(/Ann is offline/);
  });
});

describe("the Share a session picker", () => {
  it("offers this device's root sessions only (GuestLinks' rule, no peer rows)", () => {
    const rows = [
      inst("mine"),
      inst("worker", { parent: "mine" }),
      inst("verify-1", { hidden_by: "verify" }),
      inst("starting", { pending: true } as Partial<PeerRow>),
      inst("stub", { locked: true }),
      inst("mac::x", { device: "mac" }),
      peer("api", "Bob"),
    ];
    expect(shareableSessions(rows).map((i) => i.title)).toEqual(["mine"]);
    expect(shareableSessions(undefined)).toEqual([]);
  });

  it("a full join needs the I-understand tick; gated does not; nothing without a session", () => {
    expect(canShare({ title: "", mode: "gated", understood: true })).toBe(false);
    expect(canShare({ title: "a", mode: "gated", understood: false })).toBe(true);
    expect(canShare({ title: "a", mode: "full", understood: false })).toBe(false);
    expect(canShare({ title: "a", mode: "full", understood: true })).toBe(true);
    expect(canShare({ title: "a", mode: "full", understood: true, busy: true })).toBe(false);
  });

  it("the two choices say what they are", () => {
    expect(MODE_CHOICES.map((c) => c.label)).toEqual([
      "Gated to its folder (sandboxed copy)",
      "Full join (they type into it as it is)",
    ]);
  });

  it("the lines for what is shared", () => {
    const m = { title: "api", mode: "full" as const, since: 1, source: "", live: true, open: true, focused: false };
    expect(mineLine(m)).toBe("api · full · they have it open");
    expect(mineLine({ ...m, open: false, focused: true })).toBe("api · full · they are looking at it");
    expect(mineLine({ ...m, live: false })).toBe("api · full · closed");
    expect(
      theirsLine({
        title: "api",
        name: "API fix",
        status: "running",
        activity: "working",
        stage: "agent",
        branch: "b",
        folder: "app",
        mode: "gated",
      })
    ).toBe("API fix · gated to its folder · working");
  });
});

describe("focus: their rail shows our name on the row we selected", () => {
  afterEach(() => {
    vi.useRealTimers();
    useUi.setState({ focused: null });
  });

  it("posts on select, heartbeats, and says left when the selection moves off", () => {
    vi.useFakeTimers();
    const mocked = vi.mocked(api);
    mocked.mockClear();
    const rows = [inst("local"), peer("api", "Bob")];
    const stop = installPeerFocus(() => rows);
    useUi.setState({ focused: "local" });
    expect(mocked).not.toHaveBeenCalled();
    useUi.setState({ focused: "Bob::api" });
    expect(mocked).toHaveBeenLastCalledWith("/api/instances/Bob%3A%3Aapi/focus", { json: {} });
    vi.advanceTimersByTime(FOCUS_HEARTBEAT_MS);
    expect(mocked).toHaveBeenCalledTimes(2);
    useUi.setState({ focused: "local" });
    expect(mocked).toHaveBeenLastCalledWith("/api/instances/Bob%3A%3Aapi/focus", { json: { left: true } });
    vi.advanceTimersByTime(FOCUS_HEARTBEAT_MS * 3);
    expect(mocked).toHaveBeenCalledTimes(3);
    stop();
  });
});

describe("the wiring", () => {
  it("the Sidebar draws a group per peer with the ⇄ marker and reports focus", () => {
    expect(SIDEBAR).toContain("peerGroups(filtered)");
    expect(SIDEBAR).toContain("installPeerFocus(");
    expect(SIDEBAR).toMatch(/className="dev-peer"/);
    // A peer's rows are numbered with the rest (published rail order).
    expect(SIDEBAR).toMatch(/\.\.\.peerRails\.map/);
  });

  it("a peer row's chip names the peer, not 'Shared with'", () => {
    expect(ROW).toContain("isPeerRow(inst) ? peerRowChip(inst) : peerChip(inst)");
  });

  it("the People card opens the dialog and lists what they share", () => {
    expect(CARD).toContain("Share a session…");
    expect(CARD).toContain("<ShareSessionsDialog");
    expect(CARD).toContain("<TheirSessions");
    expect(DIALOG).toContain('id="peer-sessions-warn"');
    expect(DIALOG).toContain("canShare(");
    expect(DIALOG).toContain("shareableSessions(");
    expect(DIALOG).toContain("Withdraw");
  });
});

const LINK = "ab".repeat(16);

describe("the joiner's rail: the group header and the row's name", () => {
  it("says connected, or offline — reconnecting when every row is remembered", () => {
    const live = peerGroups([peer("api", "Erik", { device_label: "Erik" })])[0];
    expect(peerGroupWords(live)).toMatchObject({ text: "Erik · connected", offline: false });
    expect(peerGroupWords(live).title).toBe("Shared by Erik over a peer link · full");
    const gone = peerGroups([peer("api", "Erik", { peer_offline: true }), peer("web", "Erik", { peer_offline: true, peer_mode: "gated" })])[0];
    const w = peerGroupWords(gone);
    expect(w.text).toBe("Erik · offline — reconnecting");
    expect(w.offline).toBe(true);
    expect(w.title).toMatch(/full, gated to its folder — Erik is offline/);
    // One live row is enough to be connected.
    const mixed = peerGroups([peer("api", "Erik", { peer_offline: true }), peer("web", "Erik")])[0];
    expect(peerGroupWords(mixed).offline).toBe(false);
  });

  it("the rail shows the sharer's name for the row (display_title), never the raw title", () => {
    // SidebarRow and Pane both label a row by display_title before its title.
    expect(ROW).toContain("display_title || inst.title");
    expect(PANE).toContain("display_title || title");
  });

  it("an offline row is greyed and its click does nothing; rename and the IDE are never theirs", () => {
    expect(ROW).toContain('(offlinePeer ? " peer-gone" : "")');
    expect(ROW).toMatch(/if \(offlinePeer\) return;/);
    expect(ROW).toMatch(/if \(pending \|\| picking \|\| theirs\) return;/);
    expect(ROW).toContain("!theirs) ideSession(");
    expect(SIDEBAR).toContain("peerGroupWords(g)");
    expect(SIDEBAR).toContain("peerOffline={words.offline}");
    expect(SIDEBAR).toContain('"dev-peer-dot"');
  });
});

describe("the offline memory: a dropped link's rows stay a minute", () => {
  afterEach(() => _resetPeerMemory());

  const erik = (title: string, extra: Partial<PeerRow> = {}) =>
    peer(title, "Erik", { display_title: title.toUpperCase(), ...extra }) as PeerRow;

  it("remembers the last rows, ghosts them on link down, drops them after 60 s", () => {
    const local = inst("local");
    notePeerRows([local, erik("api"), erik("web")]);
    // The poll that no longer lists them beats the peer.state: nothing yet.
    expect(withOfflinePeers([local], 1000)).toEqual([local]);
    peerLinkDown(LINK, 1000);
    const rows = withOfflinePeers([local], 1000);
    expect(rows.map((r) => r.title)).toEqual(["local", "Erik::api", "Erik::web"]);
    expect(rows.slice(1).every(isOfflinePeerRow)).toBe(true);
    // Stable once merged: the cache listener stops on identity.
    expect(withOfflinePeers(rows, 2000)).toBe(rows);
    expect(nextOfflineExpiry(1000)).toBe(OFFLINE_KEEP_MS);
    // A minute on, they go.
    expect(withOfflinePeers(rows, 1000 + OFFLINE_KEEP_MS).map((r) => r.title)).toEqual(["local"]);
    expect(offlinePeerRows(1000 + OFFLINE_KEEP_MS)).toEqual([]);
    expect(nextOfflineExpiry(1000 + OFFLINE_KEEP_MS)).toBeNull();
  });

  it("the server listing them again replaces the ghosts; that link is back", () => {
    notePeerRows([erik("api")]);
    peerLinkDown(LINK, 0);
    expect(peerLinkUp(LINK)).toBe(true);
    const ghosts = withOfflinePeers([], 0);
    expect(ghosts).toHaveLength(1);
    notePeerRows([erik("api")]);
    expect(withOfflinePeers([erik("api")], 0).every((r) => !isOfflinePeerRow(r))).toBe(true);
    // Not down any more: no second "is back".
    expect(peerLinkUp(LINK)).toBe(false);
  });

  it("a withdrawn session is never remembered; no rows, no 'is back'", () => {
    notePeerRows([erik("api"), erik("web")]);
    forgetPeerRow(LINK, "api");
    peerLinkDown(LINK, 0);
    expect(offlinePeerRows(0).map((r) => r.title)).toEqual(["Erik::web"]);
    forgetPeerRow(LINK, "web");
    expect(offlinePeerRows(0)).toEqual([]);
    expect(peerLinkUp(LINK)).toBe(false);
    // A link we never saw rows for drops quietly.
    peerLinkDown("ff".repeat(16), 0);
    expect(peerLinkUp("ff".repeat(16))).toBe(false);
  });

  it("keeps the ghosts in the instances cache through each poll", () => {
    let data: Instance[] | undefined = [inst("local"), erik("api")];
    let listener: ((e: { type: string; query: { queryKey: readonly unknown[] }; action?: { type?: string } }) => void) | null = null;
    const qc = {
      getQueryData: <T,>() => data as unknown as T,
      setQueryData: <T,>(_k: readonly unknown[], d: T) => {
        data = d as unknown as Instance[];
        listener?.({ type: "updated", query: { queryKey: ["instances"] }, action: { type: "success" } });
      },
      getQueryCache: () => ({
        subscribe: (fn: typeof listener) => {
          listener = fn;
          return () => (listener = null);
        },
      }),
    };
    const mem = installPeerRowMemory(qc);
    mem.apply();
    peerLinkDown(LINK);
    // The next poll: the server no longer lists Erik's row.
    data = [inst("local")];
    listener!({ type: "updated", query: { queryKey: ["instances"] }, action: { type: "success" } });
    expect(data.map((r) => [r.title, isOfflinePeerRow(r)])).toEqual([
      ["local", false],
      ["Erik::api", true],
    ]);
    mem.stop();
    expect(listener).toBeNull();
  });

  it("names a session the way the rail did, even after it was withdrawn", () => {
    const rows = [erik("api")];
    notePeerRows(rows);
    const d = { link_id: LINK, peer_name: "Erik", title: "api" };
    expect(peerRowKey(d, rows)).toBe("Erik::api");
    expect(peerSessionName(d, rows)).toBe("API");
    forgetPeerRow(LINK, "api");
    expect(peerSessionName(d, [])).toBe("API");
    // Not listed yet: the key it will have.
    expect(peerRowKey({ link_id: LINK, peer_name: "Erik", title: "new" }, [])).toBe("Erik::new");
    // A numbered prefix comes from the row itself.
    const two = [peer("fix", "Erik (2)", { device_label: "Erik" })];
    expect(peerRowKey({ link_id: LINK, peer_name: "Erik", title: "fix" }, two)).toBe("Erik (2)::fix");
  });
});

describe("the pane strip", () => {
  it("says whose session it is and how you are in it — or that they are offline", () => {
    const full = peer("api", "Erik") as PeerRow;
    expect(peerPaneStrip(full)).toEqual({ text: "Erik's session · full join — you type as them", offline: false });
    expect(peerPaneStrip(peer("api", "Erik", { peer_mode: "gated" }) as PeerRow).text).toBe(
      "Erik's session · gated to its folder"
    );
    expect(peerPaneStrip({ ...full, peer_offline: true })).toEqual({
      text: "Erik is offline — the terminal reconnects when they're back",
      offline: true,
    });
  });

  it("on my session, who is here", () => {
    expect(hereWords([])).toBe("");
    expect(hereWords(["Bob"])).toBe("Bob is here");
    expect(hereWords(["Bob", "Ann"])).toBe("Bob and Ann are here");
    expect(hereWords(["Bob", "Ann", "Cy"])).toBe("Bob, Ann and Cy are here");
  });

  it("the pane draws it, with the owner-only ↺ off for a peer's row", () => {
    expect(PANE).toContain("peerPaneStrip(inst)");
    expect(PANE).toContain('className={"peer-strip"');
    expect(PANE).toContain("isPeerFocus(f.link)");
    expect(PANE).toContain("const rs = theirs ? null : resetStep(inst);");
    // The shell tab and the git steps already follow peer_share.
    expect(PANE).toContain("sharedFolder = !!inst.peer_share");
  });
});

describe("bell and toast words", () => {
  const theirs = { link_id: LINK, peer_name: "Erik", title: "fix-login", mode: "full", by: "peer", theirs: true };
  const mine = { link_id: LINK, peer_name: "Bob", title: "fix-login", mode: "full", by: "you", theirs: false };

  it("their share: a toast that opens the row, and a bell row", () => {
    const n = peerSessionNote("peer.session_shared", theirs, { name: "fix-login", rowKey: "Erik::fix-login" })!;
    expect(n.toast).toBe("Erik shared fix-login with you — click to open");
    expect(n.text).toBe("Erik shared fix-login with you (full)");
    expect(n.select).toBe("Erik::fix-login");
  });

  it("my own share: a quiet bell row only", () => {
    const n = peerSessionNote("peer.session_shared", mine, { name: "fix-login" })!;
    expect(n).toMatchObject({ text: "You shared fix-login with Bob (full)", toast: "", select: "" });
    expect(peerSessionNote("peer.session_shared", { ...mine, mode: "gated" }, { name: "x" })!.text).toBe(
      "You shared x with Bob (gated to its folder)"
    );
  });

  it("a withdrawal: a bell row, never a toast", () => {
    const gone = peerSessionNote("peer.session_unshared", { ...theirs, mode: "" }, { name: "fix-login" })!;
    expect(gone).toMatchObject({ text: "Erik withdrew fix-login", toast: "" });
    expect(peerSessionNote("peer.session_unshared", { ...mine, mode: "" }, { name: "fix-login" })!.text).toBe(
      "You stopped sharing fix-login with Bob"
    );
    expect(peerSessionNote("peer.state", theirs, { name: "x" })).toBeNull();
  });

  it("their return", () => {
    expect(peerBackToast("Erik")).toBe("Erik is back — your shared sessions are live");
    expect(peerBackToast("")).toBe("Your peer is back — your shared sessions are live");
  });

  it("the bridges: replay-guarded toasts, the memory installed, bell rows by session", () => {
    expect(TOASTS).toContain("installPeerRowMemory(queryClient)");
    for (const ev of ["peer.session_shared", "peer.session_unshared"]) expect(TOASTS).toContain(`"${ev}"`);
    const block = TOASTS.split('ev.subscribe("peer.state"')[1].split("for (const name of")[0];
    expect(block).toContain("if (isReplay(env)) return;");
    expect(block).toContain("peerLinkDown(link)");
    expect(block).toContain("peerBackToast(");
    const share = TOASTS.split('for (const name of ["peer.session_shared", "peer.session_unshared"])')[1].split("// A newer release")[0];
    expect(share).toContain("if (isReplay(env)) return;");
    expect(share).toContain("forgetPeerRow(");
    expect(share).not.toContain("desktopNotify");
    expect(BELL).toContain('case "peer.session_shared":');
    expect(BELL).toContain("session: n.select");
  });
});
