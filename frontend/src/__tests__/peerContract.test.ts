/** People, round 6 (2) — the UI wired to the backend's peer contract
 * (docs/web-api.md → "Peer links — the safety number, unlinking, the off
 * switch, export"):
 *
 * - bell rows / toasts / OS notifications: an unlink by them names the kept
 *   folder, a re-pair's new safety number asks to confirm it again, every
 *   incoming message is a row with its text_head;
 * - Share, Mirror, Export and Reopen are off until It matches, saying the
 *   server's sentence; the viewer's tabs too, with one line why;
 * - the off switch asks "Disconnect from … and pause N shared sessions?",
 *   calls /api/peer/disable and keeps the cards listed, greyed;
 * - a kept-but-closed share offers Reopen; Export says what came home;
 * - Recently closed sends a shared-folder session to People. */

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import PEOPLE_SRC from "../components/settings/screens/PeerLinks.tsx?raw";
import ADV_SRC from "../components/settings/screens/peer/PeerAdvanced.tsx?raw";
import CARD_SRC from "../components/settings/screens/peer/PeerLinkCard.tsx?raw";
import VIEWER_SRC from "../components/PeerSessionViewer.tsx?raw";
import TOASTS_SRC from "../components/EventToasts.tsx?raw";
import RECENT_SRC from "../components/dialogs/RecentDialog.tsx?raw";

vi.mock("../api/client", () => ({ api: vi.fn(async () => ({})) }));
vi.mock("../lib/toast", () => ({ toast: vi.fn() }));

const storage = new Map<string, string>();
vi.stubGlobal("localStorage", {
  getItem: (k: string) => (storage.has(k) ? storage.get(k)! : null),
  setItem: (k: string, v: string) => void storage.set(k, String(v)),
  removeItem: (k: string) => void storage.delete(k),
  clear: () => storage.clear(),
});

const {
  PEER_NEWS_EVENTS,
  disableConfirmText,
  exportDoneText,
  exportErrorText,
  isPeerShareRow,
  listenText,
  noteSasChanged,
  peerEventNote,
  rememberSas,
  sasChanged,
  shareClosed,
  viewerGateText,
} = await import("../lib/peer");
const { desktopNoteFor } = await import("../lib/desktopNotify");
const { notifFromEvent } = await import("../components/NotificationsBell");
const { PeerLinkCard } = await import("../components/settings/screens/peer/PeerLinkCard");
const { MirrorRow } = await import("../components/settings/screens/peer/MirrorRow");

import type { PeerLink } from "../lib/peer";

const link = (over: Partial<PeerLink> = {}): PeerLink => ({
  link_id: "lk1",
  peer_name: "Bea",
  role: "listener",
  sas: "482-019-337-5",
  perms: { messages: true, diff: true, read_file: true },
  shared: false,
  share_open: false,
  connected: true,
  sas_verified: true,
  joint: null,
  mirror: null,
  their_share: null,
  ...over,
});

const card = (l: Partial<PeerLink> = {}) =>
  renderToStaticMarkup(
    createElement(PeerLinkCard, {
      link: link(l),
      agents: ["claude"],
      sandbox: true,
      tick: 0,
      reload: () => {},
      onVerified: () => {},
    })
  );

const env = (event: string, data: Record<string, unknown>) => ({
  event,
  session: "",
  seq: 1,
  ts: 0,
  old: null,
  new: null,
  data,
});

describe("bell rows and toasts", () => {
  it("their unlink names the kept folder; without one, just who", () => {
    const kept = peerEventNote("peer.link_removed", { peer_name: "Bea", by: "peer", kept_folder: "/home/al/.mindflock/peer/x/work" });
    expect(kept?.text).toBe("Bea unlinked — your shared folder is kept at /home/al/.mindflock/peer/x/work");
    expect(kept?.toast).toBe(kept?.text);
    expect(peerEventNote("peer.link_removed", { peer_name: "Bea", by: "peer", kept_folder: null })?.text).toBe("Bea unlinked");
    // your own Remove is not news
    expect(peerEventNote("peer.link_removed", { peer_name: "Bea", by: "you", kept_folder: "/x" })).toBeNull();
    expect(notifFromEvent(env("peer.link_removed", { peer_name: "Bea", by: "peer", kept_folder: "/k" }))?.text).toBe(
      "Bea unlinked — your shared folder is kept at /k"
    );
  });

  it("a re-pair's new safety number is a row and a toast; a plain connect is not", () => {
    const n = peerEventNote("peer.state", { peer_name: "Bea", connected: true, sas_changed: true, sas: "111-222-333-4" });
    expect(n?.text).toBe("New safety number after reconnecting — confirm it with Bea again");
    expect(n?.toast).toBe(n?.text);
    expect(n?.cls).toBe("n-warn");
    expect(peerEventNote("peer.state", { peer_name: "Bea", connected: false })).toBeNull();
    expect(notifFromEvent(env("peer.state", { peer_name: "Bea", sas_changed: true, sas: "1" }))?.text).toBe(
      "New safety number after reconnecting — confirm it with Bea again"
    );
    expect(notifFromEvent(env("peer.state", { peer_name: "Bea", connected: true }))).toBeNull();
    expect(PEER_NEWS_EVENTS as readonly string[]).toContain("peer.state");
  });

  it("every incoming message is a row, with its text_head", () => {
    const stored = peerEventNote("peer.message", {
      peer_name: "Bea",
      text_head: "pushed the fix",
      text: "pushed the fix\nand more",
      stored: true,
      delivered_to: null,
    });
    expect(stored?.text).toBe("Bea sent you a message: pushed the fix");
    const delivered = peerEventNote("peer.message", { peer_name: "Bea", text_head: "ok", stored: false, delivered_to: "peer-bea-abc123" });
    expect(delivered?.text).toBe("Bea sent a message (your shared agent has it too): ok");
    expect(delivered?.text).not.toContain("peer-bea-abc123");
    // no text at all still makes a row
    expect(notifFromEvent(env("peer.message", { peer_name: "Bea", stored: false }))?.text).toBe(
      "Bea sent a message (your shared agent has it too)"
    );
  });

  it("toasts remember a new safety number for the card, even from a replay", () => {
    expect(TOASTS_SRC).toMatch(/if \(name === "peer\.state" && env\.data\?\.sas_changed\)\s+noteSasChanged\(/);
    expect(TOASTS_SRC.indexOf("noteSasChanged(String")).toBeLessThan(TOASTS_SRC.indexOf("if (isReplay(env)) return;\n          desktopNotify(env.event, env.data);\n          const n = peerEventNote"));
  });
});

describe("OS notifications (desktop app)", () => {
  it("a joint task proposed to you", () => {
    expect(desktopNoteFor("peer.joint_proposed", { peer_name: "Bea", brief: "Add GET /items" })).toEqual({
      title: "Bea proposes a joint task",
      body: "Add GET /items",
      target: "peer",
    });
  });

  it("a mirror that failed", () => {
    expect(desktopNoteFor("peer.mirror_failed", { peer_name: "Bea", repo_path: "/home/al/api", error: "the repo is gone" })).toEqual({
      title: "Couldn't mirror Bea's work into api",
      body: "the repo is gone",
      target: "peer",
    });
  });

  it("their unlink only, naming the kept folder", () => {
    expect(desktopNoteFor("peer.link_removed", { peer_name: "Bea", by: "peer", kept_folder: "/k" })).toEqual({
      title: "Bea unlinked",
      body: "Your shared folder is kept at /k",
      target: "peer",
    });
    expect(desktopNoteFor("peer.link_removed", { peer_name: "Bea", by: "you" })).toBeNull();
  });
});

describe("the new safety number callout", () => {
  it("reads sas_verified === false plus the server's sas_changed, not only a remembered number", () => {
    storage.clear();
    expect(sasChanged(link({ sas_verified: false }))).toBe(false);
    noteSasChanged("lk1", "482-019-337-5");
    expect(sasChanged(link({ sas_verified: false }))).toBe(true);
    expect(card({ sas_verified: false })).toContain("New safety number after reconnecting — confirm it with Bea again.");
    // an older server (no flag) and a confirmed number: no callout
    expect(sasChanged(link({ sas_verified: undefined }))).toBe(false);
    expect(sasChanged(link({ sas_verified: true }))).toBe(false);
    // confirming clears it
    rememberSas("lk1", "482-019-337-5");
    expect(sasChanged(link({ sas_verified: false }))).toBe(false);
    storage.clear();
  });
});

describe("nothing crosses before It matches", () => {
  const gate = "Confirm the safety number with Bea first.";

  it("Export is off with the server's sentence", () => {
    const html = card({ sas_verified: false, shared: true, share_open: true, session_title: "peer-bea-abc123" });
    expect(html).toMatch(/peer-export"[^>]*disabled=""[^>]*>Export</);
    expect(html).toContain(gate);
    expect(card({ shared: true, share_open: true })).not.toContain(gate);
  });

  it("Mirror is off with the same sentence", () => {
    const html = renderToStaticMarkup(
      createElement(MirrorRow, { link: link({ sas_verified: false }), reload: () => {} })
    );
    expect(html).toMatch(/peer-mirror-enable"[^>]*disabled=""/);
    expect(html).toContain(gate);
  });

  it("a refusal from the server is shown as it is, one sentence", () => {
    expect(exportErrorText("Confirm the safety number with Bea first")).toBe("Confirm the safety number with Bea first.");
    const history =
      "That repo doesn't have the history the shared folder started from — pick the repo you shared from, or a clone of it.";
    expect(exportErrorText(history)).toBe(history);
  });

  it("the viewer's tabs are off until then, with one line why", () => {
    expect(viewerGateText("Bea")).toBe(
      "Confirm the safety number with Bea in People first — then their screen, files and changes show here."
    );
    expect(VIEWER_SRC).toContain("const gated = link?.sas_verified === false;");
    expect(VIEWER_SRC).toContain("disabled={gated}");
    expect(VIEWER_SRC).toMatch(/\{gated \? \(\s*<p className="psv-msg psv-gate">\s*\{viewerGateText\(name\)\}/);
  });
});

describe("a kept share whose session is closed", () => {
  it("offers Reopen, to POST …/share/reopen", () => {
    expect(shareClosed({ shared: true, share_open: false })).toBe(true);
    expect(shareClosed({ shared: true, share_open: true })).toBe(false);
    expect(shareClosed({ shared: true })).toBe(false); // older server: assume open
    const html = card({ shared: true, share_open: false, session_title: "peer-bea-abc123" });
    expect(html).toContain("Your shared session is closed — its folder is kept.");
    expect(html).toMatch(/peer-reopen-btn"[^>]*>Reopen</);
    expect(html).not.toMatch(/peer-reopen-btn"[^>]*disabled/);
    expect(html).not.toContain(">Open session<");
    expect(CARD_SRC).toContain('api(base + "/share/reopen", { json: chosen ? { program: chosen } : {} })');
    expect(CARD_SRC).toContain('{reopenErr && <span className="error peer-reopen-error">{reopenErr}</span>}');
  });

  it("is off until It matches", () => {
    const html = card({ shared: true, share_open: false, sas_verified: false });
    expect(html).toMatch(/peer-reopen-btn"[^>]*disabled=""/);
  });

  it("an open one keeps Open session", () => {
    const html = card({ shared: true, share_open: true, session_title: "peer-bea-abc123" });
    expect(html).toContain(">Open session<");
    expect(html).not.toContain("peer-reopen-btn");
  });
});

describe("Export says what came home", () => {
  it("branch, repo and count; zero is nothing new yet", () => {
    expect(
      exportDoneText({ branch: "peer/bea", repo_path: "/home/al/api", commits: 2, sha: "abc", target: "/home/al/api" }, "")
    ).toBe("Brought home as peer/bea in api (2 commits)");
    expect(exportDoneText({ branch: "peer/bea", repo_path: "/home/al/api", commits: 0 }, "")).toBe("Nothing new to bring home yet");
  });

  it("the result sits next to the Export button", () => {
    expect(CARD_SRC).toMatch(/\{busyExport \? "Bringing home…" : "Export"\}\s*<\/button>[\s\S]{0,200}\{exported && \(/);
    expect(CARD_SRC.indexOf("{exported && (")).toBeLessThan(CARD_SRC.indexOf("Stop sharing…"));
  });
});

describe("Unlink", () => {
  it("says nothing is deleted on their side, nor yours unless you tick delete", () => {
    expect(CARD_SRC).toContain("Their shared session is closed, not deleted; yours too unless you tick delete.");
  });
});

describe("the off switch", () => {
  it("asks who you disconnect from and how many shared sessions pause", () => {
    expect(disableConfirmText([link({ share_open: true })])).toBe("Disconnect from Bea and pause 1 shared session?");
    expect(
      disableConfirmText([
        link({ peer_name: "Bea", share_open: true, shared: true }),
        link({ peer_name: "Cy", share_open: false, shared: true }),
        link({ peer_name: "Dee", share_open: true, shared: true }),
      ])
    ).toBe("Disconnect from Bea, Cy and Dee and pause 2 shared sessions?");
    expect(disableConfirmText([link({ peer_name: "Bea", share_open: false })])).toBe("Disconnect from Bea?");
  });

  it("confirms first, then POST /api/peer/disable and refetch", () => {
    expect(ADV_SRC).toContain("if (!e.target.checked && people > 0) setAsking(true);");
    expect(ADV_SRC).toContain("title={disableConfirmText(st.links)}");
    expect(PEOPLE_SRC).toContain('await api(enable ? "/api/peer/enable" : "/api/peer/disable", { json: {} });');
    expect(PEOPLE_SRC).toMatch(/await s\.reload\(\);\s+setToggling\(false\);\s+load\(\);/);
    expect(PEOPLE_SRC).toContain("onToggle={(v) => void toggle(v)}");
  });

  it("keeps the cards listed, greyed, while off", () => {
    expect(PEOPLE_SRC).toContain('<div className={"peer-cards" + (st.enabled ? "" : " peer-cards-off")} inert={!st.enabled}>');
    expect(PEOPLE_SRC).toContain("Connections to people are off — nobody connects until you turn them on.");
  });

  it("the status line says where the listener really is", () => {
    expect(listenText({ host: "0.0.0.0", port: 8799, listening: true, bound: "100.64.0.2:8799" })).toBe(
      "direct connections open at 100.64.0.2:8799"
    );
    expect(listenText({ host: "0.0.0.0", port: 8799, listening: false, bound: null })).toBe("direct connections closed");
    // an older server without `bound`
    expect(listenText({ host: "0.0.0.0", port: 8799, listening: true })).toBe("direct connections open (0.0.0.0:8799)");
    expect(ADV_SRC).toContain("{listenText(st.listen)}");
  });
});

describe("Recently closed", () => {
  it("knows a shared-folder session by its marker or its title shape", () => {
    expect(isPeerShareRow({ title: "peer-bea-3f2a9c" })).toBe(true);
    expect(isPeerShareRow({ title: "peer-bea-smith-3f2a9c-2" })).toBe(true);
    expect(isPeerShareRow({ title: "x", peer_share: true })).toBe(true);
    expect(isPeerShareRow({ title: "peer-review" })).toBe(false);
    expect(isPeerShareRow({ title: "api-fix" })).toBe(false);
  });

  it("offers Reopen from People instead of the generic Reopen", () => {
    expect(RECENT_SRC).toMatch(/isPeerShareRow\(e\) && \([\s\S]{0,300}onClick=\{\(\) => openSettings\(PEER_SCREEN\)\}[\s\S]{0,60}Reopen from People/);
    expect(RECENT_SRC).toContain('{e.source === "closed" && !isPeerShareRow(e) && (');
  });
});
