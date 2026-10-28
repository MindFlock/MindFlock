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
  canShare,
  installPeerFocus,
  isPeerRow,
  mineLine,
  modeWords,
  peerGroups,
  peerRowChip,
  peerRowTooltip,
  shareableSessions,
  theirsLine,
  type PeerRow,
} from "../lib/peerSessions";

const raw = (glob: Record<string, string>) => Object.values(glob)[0];
const SIDEBAR = raw(
  import.meta.glob<string>("../components/sidebar/Sidebar.tsx", { query: "?raw", import: "default", eager: true })
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
    expect(chip.connected).toBeNull();
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
