/** Work with someone (peer links): the wording every surface shares — the
 * bell/toast line per peer.* event, the invite countdown, the rail chip — and
 * the code / reconnect parsing the join and Reconnect boxes run. */

import { describe, it, expect } from "vitest";
import {
  carrierText,
  extractPeerCode,
  fmtSecondsLeft,
  inviteExpiryText,
  isPeerCode,
  newOpId,
  peerChip,
  peerEventNote,
  reconnectKind,
  roleText,
} from "../lib/peer";
import { notifFromEvent } from "../components/NotificationsBell";
import type { EventEnvelope } from "../state/queries";

const CODE = "mfp1:abcdefgh234567-wxyz";

describe("isPeerCode / extractPeerCode", () => {
  it("finds the code in a bare paste, the whole invite message, or a link", () => {
    expect(isPeerCode(CODE)).toBe(true);
    expect(isPeerCode("Join me on MindFlock: paste this whole message.\n\n" + CODE + "\n\n(Works once)")).toBe(true);
    expect(isPeerCode("mindflock://join/" + CODE.replace("mfp1", "mfp2"))).toBe(true);
    expect(extractPeerCode("hey " + CODE.toUpperCase() + " thanks")).toBe(CODE);
  });

  it("is not fooled by a device code or a near miss", () => {
    expect(isPeerCode("ABCD-EFGH")).toBe(false);
    expect(isPeerCode("mfp3:abcd-wxyz")).toBe(false);
    expect(isPeerCode("mfp1:abcd")).toBe(false);
    expect(extractPeerCode("")).toBe("");
  });
});

describe("reconnectKind", () => {
  it("tells a fresh invite from a new address", () => {
    expect(reconnectKind(CODE)).toBe("invite");
    expect(reconnectKind("wss://quiet-fox.trycloudflare.com/abcdefghijklmnopqrstuvwxyz")).toBe("address");
    expect(reconnectKind("100.101.102.103:8799")).toBe("address");
    expect(reconnectKind("rig.tail1234.ts.net:8799")).toBe("address");
    expect(reconnectKind("[fd7a::1]:8799")).toBe("address");
  });

  it("refuses anything else instead of guessing", () => {
    expect(reconnectKind("")).toBeNull();
    expect(reconnectKind("   ")).toBeNull();
    expect(reconnectKind("https://example.com")).toBeNull();
    expect(reconnectKind("just some words")).toBeNull();
  });
});

describe("invite countdown", () => {
  it("ticks as m:ss and never goes negative", () => {
    expect(fmtSecondsLeft(581)).toBe("9:41");
    expect(fmtSecondsLeft(600)).toBe("10:00");
    expect(fmtSecondsLeft(5.9)).toBe("0:05");
    expect(fmtSecondsLeft(-3)).toBe("0:00");
  });

  it("says what to do once it ran out", () => {
    expect(inviteExpiryText(581)).toBe("Expires in 9:41");
    expect(inviteExpiryText(0)).toBe("Expired — create a new one");
    expect(inviteExpiryText(-10)).toBe("Expired — create a new one");
  });
});

describe("plain words", () => {
  it("never shows listener / dialer", () => {
    expect(roleText("listener")).toBe("You invited them");
    expect(roleText("dialer")).toBe("You joined them");
    expect(carrierText("tcp")).toContain("same network or tailnet");
    expect(carrierText("relay")).toBe("through the relay");
    expect(carrierText("")).toBe("");
  });

  it("makes op ids the server accepts", () => {
    const id = newOpId();
    expect(id).toMatch(/^[A-Za-z0-9_-]{1,32}$/);
    expect(newOpId()).not.toBe(id);
  });
});

describe("peerChip", () => {
  it("is null for an ordinary session", () => {
    expect(peerChip({})).toBeNull();
    expect(peerChip({ peer_share: false, peer_with: { name: "B", connected: true } })).toBeNull();
  });

  it("names who it is shared with and whether they are here", () => {
    const on = peerChip({ peer_share: true, peer_with: { link_id: "x", name: "Bea", connected: true } });
    expect(on?.label).toBe("Shared with Bea");
    expect(on?.connected).toBe(true);
    expect(on?.title).toContain("connected");
    expect(on?.title).toContain("Bring work home");
    const off = peerChip({ peer_share: true, peer_with: { link_id: "x", name: "Bea", connected: false } });
    expect(off?.connected).toBe(false);
    expect(off?.title).toContain("offline");
  });

  it("still marks a shared session an older server sends no peer_with for", () => {
    const c = peerChip({ peer_share: true });
    expect(c?.label).toBe("Shared");
    expect(c?.connected).toBeNull();
  });
});

describe("peerEventNote", () => {
  it("tells the inviter who joined and the safety number to compare", () => {
    const n = peerEventNote("peer.link_added", { peer_name: "Bea", sas: "482-019-337-5", role: "listener" });
    expect(n?.text).toBe("Bea joined — compare safety number 482-019-337-5");
    expect(n?.toast).toBe(n?.text);
    expect(n?.cls).toBe("n-done");
  });

  it("says reconnected for a re-pair, and does not toast the joiner's own click", () => {
    expect(peerEventNote("peer.link_added", { peer_name: "Bea", sas: "1", repaired: true, role: "listener" })?.text).toMatch(
      /^Bea reconnected/
    );
    const joiner = peerEventNote("peer.link_added", { peer_name: "Al", sas: "482-019-337-5", role: "dialer" });
    expect(joiner?.text).toContain("Al joined");
    expect(joiner?.toast).toBe("");
  });

  it("reports an unlink only when THEY did it", () => {
    expect(peerEventNote("peer.link_removed", { peer_name: "Bea", by: "peer" })?.toast).toBe("Bea unlinked");
    expect(peerEventNote("peer.link_removed", { peer_name: "Bea", by: "you" })).toBeNull();
  });

  it("surfaces a message only when it was stored for the human", () => {
    const n = peerEventNote("peer.message", { peer_name: "Bea", text: "pushed the fix", stored: true });
    expect(n?.toast).toBe("Bea sent you a message");
    expect(n?.text).toBe("Bea sent you a message: pushed the fix");
    expect(peerEventNote("peer.message", { peer_name: "Bea", text: "x", stored: false })).toBeNull();
  });

  it("names who needs a fresh invite when the relay moved", () => {
    const n = peerEventNote("peer.relay_changed", { host: "new.trycloudflare.com", peers: ["Bea", "Cy"] });
    expect(n?.text).toBe("Your relay address changed — send Bea, Cy a fresh invite to reconnect");
    expect(n?.cls).toBe("n-warn");
    expect(peerEventNote("peer.relay_changed", {})?.text).toContain("the people you invited");
  });

  it("ignores peer.state and progress (live screen only, not news)", () => {
    expect(peerEventNote("peer.state", { peer_name: "Bea", connected: true })).toBeNull();
    expect(peerEventNote("peer.progress", { op_id: "x", stage: "connecting" })).toBeNull();
  });
});

describe("the bell files peer events under Collaborate", () => {
  const env = (e: Partial<EventEnvelope>): EventEnvelope => ({
    event: "",
    session: "",
    seq: 1,
    ts: 0,
    old: null,
    new: null,
    data: {},
    ...e,
  });

  it("keeps a join as a peer row", () => {
    const n = notifFromEvent(env({ event: "peer.link_added", data: { peer_name: "Bea", sas: "482-019-337-5", role: "listener" } }));
    expect(n?.peer).toBe(true);
    expect(n?.text).toContain("Bea joined");
  });

  it("drops what is not news", () => {
    expect(notifFromEvent(env({ event: "peer.state", data: { connected: false } }))).toBeNull();
    expect(notifFromEvent(env({ event: "peer.link_removed", data: { peer_name: "Bea", by: "you" } }))).toBeNull();
  });
});
