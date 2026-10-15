/** People, round 6 judge fixes — the link card in three groups (Trust / Share
 * / Talk, the next step first), the safety number's two answers and the
 * "new number after reconnecting" callout, Share gated on it, labelled share
 * fields, Bring work home's result and failure sentences, the conversation
 * where you are (card + shared-session viewer), the screen's one-line intro,
 * and the vocabulary (person / connection, Tailscale). */

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import PEOPLE_SRC from "../components/settings/screens/PeerLinks.tsx?raw";
import ADV_SRC from "../components/settings/screens/peer/PeerAdvanced.tsx?raw";
import VIEWER_SRC from "../components/PeerSessionViewer.tsx?raw";
import TOASTS_SRC from "../components/EventToasts.tsx?raw";
import BELL_SRC from "../components/NotificationsBell.tsx?raw";
import PALETTE_SRC from "../components/palette/CommandPalette.tsx?raw";

vi.mock("../api/client", () => ({ api: vi.fn(async () => ({})) }));
vi.mock("../lib/toast", () => ({ toast: vi.fn() }));

const storage = new Map<string, string>();
vi.stubGlobal("localStorage", {
  getItem: (k: string) => (storage.has(k) ? storage.get(k)! : null),
  setItem: (k: string, v: string) => void storage.set(k, String(v)),
  removeItem: (k: string) => void storage.delete(k),
  clear: () => storage.clear(),
});

const { exportDoneText, exportErrorText, isCutOffInvite, isPeerSender, rememberSas, sasChanged } = await import("../lib/peer");
const { PeerLinkCard } = await import("../components/settings/screens/peer/PeerLinkCard");
const { MessageLog, senderText } = await import("../components/settings/screens/peer/MessageThread");

import type { PeerLink } from "../lib/peer";

const link = (over: Partial<PeerLink> = {}): PeerLink => ({
  link_id: "lk1",
  peer_name: "Bea",
  role: "listener",
  sas: "482-019-337-5",
  perms: { messages: true, diff: true, read_file: true },
  shared: false,
  connected: true,
  sas_verified: false,
  joint: null,
  mirror: null,
  their_share: null,
  ...over,
});

const card = (l: Partial<PeerLink> = {}, sandbox = true) =>
  renderToStaticMarkup(
    createElement(PeerLinkCard, {
      link: link(l),
      agents: ["claude", "codex"],
      sandbox,
      tick: 0,
      reload: () => {},
      onVerified: () => {},
    })
  );

const groups = (html: string) => [...html.matchAll(/class="peer-group peer-group-([a-z]+)"/g)].map((m) => m[1]);

describe("the card's three groups", () => {
  it("leads with Trust until the safety number is confirmed", () => {
    expect(groups(card())).toEqual(["trust", "share", "talk"]);
  });

  it("then Share, Talk, Trust", () => {
    expect(groups(card({ sas_verified: true }))).toEqual(["share", "talk", "trust"]);
  });

  it("each group says its name", () => {
    const html = card();
    for (const g of ["Trust", "Share", "Talk"]) expect(html).toContain(`<h5 class="peer-group-title">${g}</h5>`);
  });
});

describe("Trust", () => {
  it("asks for both answers, and says why the number matters", () => {
    const html = card();
    expect(html).toMatch(/peer-sas-match"[^>]*>It matches</);
    expect(html).toMatch(/peer-sas-mismatch"[^>]*>It doesn&#x27;t match</);
    expect(html).toContain("The same number on both screens means nobody is in between.");
  });

  it("a confirmed number shows the tick and no buttons", () => {
    const html = card({ sas_verified: true });
    expect(html).toContain("✓ checked with them.");
    expect(html).not.toContain("It doesn&#x27;t match");
  });

  it("calls out a NEW number after a reconnect", () => {
    storage.clear();
    expect(card()).not.toContain("New safety number");
    rememberSas("lk1", "111-111-111-1");
    expect(sasChanged(link())).toBe(true);
    expect(card()).toContain("New safety number after reconnecting — confirm it again.");
    // same number, or confirmed: no callout
    rememberSas("lk1", "482-019-337-5");
    expect(sasChanged(link())).toBe(false);
    expect(sasChanged(link({ sas_verified: true, sas: "999" }))).toBe(false);
    storage.clear();
  });

  it("permissions name the folder, and Remove connection looks destructive", () => {
    const html = card();
    expect(html).toContain('<span class="set-label">In the folder you share, they may</span>');
    expect(html).toMatch(/class="test-btn peer-danger"[^>]*>Remove connection…</);
    expect(html).not.toContain("Unlink");
  });
});

describe("Share", () => {
  it("is off until the safety number is confirmed, and says so", () => {
    const html = card();
    expect(html).toContain("Confirm the safety number before you share.");
    expect(html).toMatch(/peer-share-btn"[^>]*disabled=""[^>]*>Share a folder</);
    expect(card({ sas_verified: true })).not.toContain("Confirm the safety number before you share.");
  });

  it("labels every field: Folder, Branch, Agent", () => {
    const html = card({ sas_verified: true });
    for (const l of ["Folder", "Branch", "Agent"]) expect(html).toContain(`<span class="set-hint">${l}</span>`);
    expect(html).toContain('<option value="claude"');
  });

  it("once shared: Bring work home with labelled fields, and Stop sharing", () => {
    const html = card({ sas_verified: true, shared: true, session_title: "peer-bea-api" });
    expect(html).toContain("Your folder is shared in session <strong>peer-bea-api</strong>.");
    expect(html).toContain('<span class="set-hint">Your repo</span>');
    expect(html).toContain('<span class="set-hint">As branch</span>');
    expect(html).toMatch(/peer-export"[^>]*>Export</);
    expect(html).toContain(">Stop sharing…<");
  });

  it("the joint task and its tour hook live in Share", () => {
    const html = card({ sas_verified: true });
    const share = html.slice(html.indexOf("peer-group-share"), html.indexOf("peer-group-talk"));
    expect(share).toContain('data-tour="people-joint"');
    expect(share).toContain('data-tour="people-share"');
  });
});

describe("Bring work home says what happened", () => {
  it("success names the branch and the repo, and how much when told", () => {
    expect(exportDoneText({ branch: "peer/Bea", target: "/home/al/api", sha: "abc1234def" }, "")).toBe( // pragma: allowlist secret
      "Brought home → peer/Bea in api (abc1234)."
    );
    expect(exportDoneText({ branch: "peer/Bea", target: "/home/al/api", commits: 3 }, "")).toBe(
      "Brought 3 commits home → peer/Bea in api."
    );
    expect(exportDoneText({ branch: "peer/Bea", commits: 0 }, "/x/api")).toBe(
      "Nothing new to bring home — peer/Bea in api is already up to date."
    );
  });

  it("failure is one sentence: never doubled, never a field name", () => {
    expect(exportErrorText("export failed: export failed: branch not updated")).toBe(
      "Couldn't bring work home: the branch didn't move — pick the repo you shared from (it must share history with the shared folder)."
    );
    expect(exportErrorText("target_repo must be an existing repository")).toBe(
      "Couldn't bring work home: that folder isn't a git repository — pick your repo."
    );
    expect(exportErrorText("export failed: branch peer/Bea is checked out in the target")).toBe(
      "Couldn't bring work home: branch peer/Bea is checked out in that repo — switch branches there first."
    );
    expect(exportErrorText("export failed: export failed: ")).toBe("Couldn't bring work home: something went wrong.");
  });
});

describe("Talk: the conversation where you are", () => {
  it("names senders by person, never by session", () => {
    expect(senderText({ dir: "in", by: "peer" }, "Bea")).toBe("Bea");
    expect(senderText({ dir: "out", by: "you" }, "Bea")).toBe("you");
    expect(senderText({ dir: "out", by: "agent" }, "Bea")).toBe("your agent");
    const html = renderToStaticMarkup(
      createElement(MessageLog, {
        peerName: "Bea",
        now: 1000,
        msgs: [{ id: 1, dir: "in", by: "peer", text: "pushed the fix", ts: 940, delivered_to: "peer-bea-ceb1bc" }],
      })
    );
    expect(html).toContain('<span class="peer-msg-who">Bea</span>');
    expect(html).toContain("1m ago");
    expect(html).not.toContain("peer-bea-ceb1bc");
  });

  it("the card shows the thread with its send box; the viewer shows it above its own", () => {
    const html = card({ sas_verified: true });
    const talk = html.slice(html.indexOf("peer-group-talk"));
    expect(talk).toContain("No messages yet — say hello.");
    expect(talk).toContain('placeholder="Message Bea — their agent sees it too"');
    expect(talk).toContain(">Watch their session<");
    expect(VIEWER_SRC).toMatch(/<div className="psv-thread"[\s\S]*<MessageThread[\s\S]*<form\s+className="psv-say"/);
  });

  it("a person's message is toasted once, by person — not as peer:Bea → session id", () => {
    expect(isPeerSender("peer:Bea")).toBe(true);
    expect(isPeerSender("orch")).toBe(false);
    expect(TOASTS_SRC).toContain("if (isPeerSender(from)) return;");
    expect(BELL_SRC).toContain('? "People"');
    expect(BELL_SRC).not.toContain('"Collaborate"');
  });
});

describe("the screen", () => {
  it("says in one line what it is for, and leads with Invite", () => {
    expect(PEOPLE_SRC).toContain("one of you invites, the other joins");
    expect(PEOPLE_SRC).toMatch(/className="btn-primary"\s+id="peer-invite"/);
    expect(PEOPLE_SRC).toContain('<h4 className="set-subtitle">Join someone</h4>');
    expect(PEOPLE_SRC).not.toContain("Join a peer</h4>");
  });

  it("only offers a direct invite when the relay isn't already off", () => {
    expect(PEOPLE_SRC).toMatch(/st\?\.relay\?\.setting !== "off" && \(\s*<button[\s\S]{0,200}id="peer-invite-direct"/);
  });

  it("catches a cut-off invite before asking the server", () => {
    expect(isCutOffInvite("mfp1:abcdef")).toBe(true);
    expect(isCutOffInvite("mfp1:abcdefgh234567-wxyz")).toBe(false);
    expect(isCutOffInvite("hello")).toBe(false);
    expect(PEOPLE_SRC).toContain('setJoinErr("This invite is cut off — copy the whole message again.")');
  });

  it("a reconnect only promises a kept folder when there is one, and asks to compare a new number", () => {
    expect(PEOPLE_SRC).toContain('(link.shared ? " — your shared folder is kept" : "")');
    expect(PEOPLE_SRC).toContain("again — it's new.");
  });

  it("turning connections off asks first when anyone would be dropped", () => {
    expect(ADV_SRC).toContain('<span className="set-label">Connections to people</span>');
    expect(ADV_SRC).toContain("if (!e.target.checked && people > 0) setAsking(true);");
    expect(ADV_SRC).toContain('title="Turn off connections to people?"');
  });

  it("the palette finds People by the words a colleague uses", () => {
    expect(PALETTE_SRC).toMatch(/label: "People…"[\s\S]{0,200}openSettings\("peer"\)/);
    for (const w of ["join", "pair", "invite", "colleague", "teammate", "collaborate"])
      expect(PALETTE_SRC.slice(PALETTE_SRC.indexOf('label: "People…"'), PALETTE_SRC.indexOf('label: "People…"') + 500)).toContain(w);
  });
});
