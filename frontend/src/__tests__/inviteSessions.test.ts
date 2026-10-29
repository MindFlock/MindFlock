/** People → Invite someone, the guest link's flow for a peer invite
 * (peer/InviteSessions): the red warning and "I understand" open the
 * picker, the picker's rules (one gated session, zero sessions allowed),
 * the invite message saying what it opens, and the "joined" cards on both
 * sides (peer/JoinedCard). */

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import PEOPLE_SRC from "../components/settings/screens/PeerLinks.tsx?raw";
import INVITE_SRC from "../components/settings/screens/peer/InviteSessions.tsx?raw";
import JOINED_SRC from "../components/settings/screens/peer/JoinedCard.tsx?raw";

vi.mock("../api/client", () => ({ api: vi.fn(async () => ({ mine: [], theirs: [], connected: true })) }));
vi.mock("../lib/toast", () => ({ toast: vi.fn() }));
vi.mock("../state/queries", () => ({
  useInstances: () => ({ data: [] }),
  queryClient: { getQueryData: () => [] },
}));

const { andList, connectedText, inviteMessage, inviteOpensText, joinedText, sessionListText, shareErrorText } =
  await import("../lib/peer");
const { defaultMode, gatedFree, inviteSessionsBody, makeInviteLabel, InviteSessions } = await import(
  "../components/settings/screens/peer/InviteSessions"
);
const { JoinedCard } = await import("../components/settings/screens/peer/JoinedCard");

import type { PeerLink, PeerStatus } from "../lib/peer";

const TWO = [
  { title: "fix-login", mode: "full" as const },
  { title: "api", mode: "gated" as const },
];

describe("the words", () => {
  it("lists sessions the way people say them", () => {
    expect(andList([])).toBe("");
    expect(andList(["a"])).toBe("a");
    expect(andList(["a", "b"])).toBe("a and b");
    expect(andList(["a", "b", "c"])).toBe("a, b and c");
    expect(sessionListText(TWO)).toBe("fix-login (full) and api (gated)");
  });

  it("the invite card says what the link opens", () => {
    expect(inviteOpensText(TWO)).toBe(
      "Opens: fix-login (full), api (gated) — they appear on their rail the moment they join."
    );
    expect(inviteOpensText([])).toBe("Opens no sessions — you just connect, and share later from their card.");
    expect(inviteOpensText(undefined)).toContain("you just connect");
  });

  it("the inviter's joined line names what is open to them, less what failed", () => {
    expect(joinedText("Bob", TWO, [])).toBe("Bob joined — fix-login and api are open to them");
    expect(joinedText("Bob", TWO, [{ title: "api", mode: "gated", error: "no sandbox" }])).toBe(
      "Bob joined — fix-login is open to them"
    );
    expect(joinedText("Bob", [], [])).toBe("Bob joined");
    expect(joinedText("Bob", TWO, [], true)).toBe("Bob reconnected — fix-login and api are open to them");
    expect(shareErrorText({ title: "api", mode: "gated", error: "no sandbox" })).toBe("Couldn't open api: no sandbox");
  });

  it("the joiner's line names what is on their rail", () => {
    expect(connectedText("Erik", [])).toBe("You're connected to Erik");
    expect(
      connectedText("Erik", [
        { title: "fix-login", mode: "full" },
        { title: "api", name: "API", mode: "gated" },
      ])
    ).toBe("You're connected to Erik — fix-login (full) and API (gated) are on your rail");
  });
});

describe("the invite message", () => {
  const inv = {
    code: "mfp2:abc-wxyz",
    url: "https://x.trycloudflare.com/join#mfp2:abc-wxyz",
    message: "Join me on MindFlock: … mfp2:abc-wxyz …",
  };

  it("says which sessions the link opens, between the link and the code", () => {
    const text = inviteMessage({ ...inv, sessions: TWO });
    expect(text.startsWith("Open this link to join me in MindFlock: " + inv.url + "\n\n")).toBe(true);
    expect(text).toContain(
      "It opens my sessions fix-login (full) and api (gated) — they show up on your rail as soon as you join."
    );
    expect(text.endsWith(inv.message)).toBe(true);
  });

  it("is unchanged for an invite that just connects", () => {
    expect(inviteMessage({ ...inv, sessions: [] })).toBe(inviteMessage(inv));
    expect(inviteMessage(inv)).not.toContain("It opens");
  });
});

describe("the picker's rules", () => {
  it("a freshly ticked session is gated when it can be, else full", () => {
    expect(defaultMode(new Map(), true)).toBe("gated");
    expect(defaultMode(new Map([["api", "gated"]]), true)).toBe("full");
    expect(defaultMode(new Map(), false)).toBe("full");
  });

  it("one gated session per link", () => {
    const picked = new Map<string, "gated" | "full">([
      ["api", "gated"],
      ["fix-login", "full"],
    ]);
    expect(gatedFree(picked, "api", true)).toBe(true);
    expect(gatedFree(picked, "fix-login", true)).toBe(false);
    expect(gatedFree(new Map(), "api", false)).toBe(false);
  });

  it("sends the ticked sessions in the rail's order", () => {
    const picked = new Map<string, "gated" | "full">([
      ["api", "gated"],
      ["fix-login", "full"],
    ]);
    expect(inviteSessionsBody(["fix-login", "docs", "api"], picked)).toEqual(TWO);
    expect(inviteSessionsBody(["docs"], new Map())).toEqual([]);
  });

  it("zero sessions is still an invite", () => {
    expect(makeInviteLabel(0)).toBe("Make an invite without sessions");
    expect(makeInviteLabel(1)).toBe("Make an invite (1 session)");
    expect(makeInviteLabel(2)).toBe("Make an invite (2 sessions)");
    expect(makeInviteLabel(2, true)).toBe("Making…");
  });
});

const ST = {
  enabled: true,
  display_name: "erik",
  fingerprint: null,
  sandbox: { available: true, reason: "" },
  listen: { host: "0.0.0.0", port: 8799, listening: false },
  links: [],
  invites: [],
} as PeerStatus;

const block = (over: Record<string, unknown> = {}) =>
  renderToStaticMarkup(
    createElement(InviteSessions, {
      st: ST,
      invite: null,
      setInvite: () => {},
      now: 0,
      loadedAt: 0,
      cloudflaredMissing: false,
      install: [],
      remoteChosen: false,
      onRemote: () => {},
      afterPair: () => {},
      reload: () => {},
      ...over,
    })
  );

describe("the invite block", () => {
  it("is warning first: the red box and the I-understand tick, no picker yet", () => {
    const html = block();
    expect(html).toContain('id="peer-invite-warn"');
    expect(html).toContain('class="peer-danger peer-guest-warn"');
    expect(html).toContain("Whoever opens this invite gets the sessions you pick, the moment they join.");
    expect(html).toContain('id="peer-invite-ack"');
    expect(html).toContain("I understand this lets them act on my machine — pick the sessions");
    expect(html).not.toContain("peer-invite-pick");
  });

  it("ticking opens the picker; cancelling unticks", () => {
    expect(INVITE_SRC).toMatch(/const ack = \(on: boolean\) => \{\s+setUnderstood\(on\);\s+setPickOpen\(on\);/);
    expect(INVITE_SRC).toMatch(/const cancelPick = \(\) => \{\s+setPickOpen\(false\);\s+setUnderstood\(false\);/);
    expect(INVITE_SRC).toContain('<Modal id="peer-invite-pick" role="dialog" aria-modal="true" portal onClose={cancelPick}>');
    // Make is refused without the tick, and without a reach when one is needed.
    expect(INVITE_SRC).toContain("disabled={busy || !understood || !reach}");
    expect(INVITE_SRC).toContain("if (!understood || !reach || busy) return;");
  });

  it("reuses the shared-session pieces: the mode choices and the root-session rule", () => {
    expect(INVITE_SRC).toContain("MODE_CHOICES.map((c) =>");
    expect(INVITE_SRC).toContain("shareableSessions(instances)");
    expect(INVITE_SRC).toContain("...(body.length ? { sessions: body } : {})");
  });

  it("folds the reach choice in: where they are without cloudflared, a direct tick with a relay", () => {
    expect(INVITE_SRC).toContain("They're on my network or Tailscale");
    expect(INVITE_SRC).toContain("They're somewhere else");
    expect(INVITE_SRC).toMatch(/st\?\.relay\?\.setting !== "off" \? \(\s*<label>\s*<input\s+type="checkbox"\s+id="peer-invite-direct"/);
  });

  it("the card shows the link, both copies, and what it opens", () => {
    const html = block({
      invite: {
        invite_id: "00000000000000aa",
        code: "mfp2:abc-wxyz",
        url: "https://x.trycloudflare.com/join#mfp2:abc-wxyz",
        message: "m",
        expires_in: 600,
        host: "x.trycloudflare.com",
        port: 443,
        relay: "tunnel",
        direct: false,
        sessions: TWO,
        at: 0,
      },
    });
    expect(html).toContain('id="peer-invite-url"');
    expect(html).toContain(">Copy link<");
    expect(html).toContain(">Copy invite message<");
    expect(html).toContain("Opens: fix-login (full), api (gated) — they appear on their rail the moment they join.");
    expect(html).not.toContain('id="peer-invite-warn"');
  });

  it("lists waiting invites with what they open", () => {
    const html = block({
      st: { ...ST, invites: [{ invite_id: "b", expires_in: 300, direct: false, sessions: TWO }] },
    });
    expect(html).toContain("Invite (opens fix-login, api) waiting");
  });
});

const LINK: PeerLink = {
  link_id: "lk1",
  peer_name: "Bob",
  role: "listener",
  sas: "482-019-337-5",
  perms: { messages: true, diff: true, read_file: true },
  shared: false,
  connected: true,
  sas_verified: false,
};

const joinedCard = (side: "inviter" | "joiner", over: Record<string, unknown> = {}, link: Partial<PeerLink> = {}) =>
  renderToStaticMarkup(
    createElement(JoinedCard, {
      side,
      joined: { link_id: "lk1", name: "Bob", sas: "482-019-337-5", sessions: TWO, ...over },
      link: { ...LINK, ...link },
      sandbox: true,
      tick: 0,
      onMatch: () => {},
      onDone: () => {},
    })
  );

describe("the joined card", () => {
  it("inviter: who joined, what is open to them, the safety number and It matches", () => {
    const html = joinedCard("inviter");
    expect(html).toContain("Bob joined — fix-login and api are open to them");
    expect(html).toContain('<code class="peer-sas">482-019-337-5</code>');
    expect(html).toContain(">It matches<");
    expect(html).not.toContain("Share a session…"); // the invite already shared
  });

  it("inviter: a session that couldn't be shared says why", () => {
    const html = joinedCard("inviter", {}, { share_errors: [{ title: "api", mode: "gated", error: "no sandbox" }] });
    expect(html).toContain("Bob joined — fix-login is open to them");
    expect(html).toContain("Couldn&#x27;t open api: no sandbox");
  });

  it("inviter: an invite without sessions offers Share a session…, once It matches", () => {
    const before = joinedCard("inviter", { sessions: [] });
    expect(before).toContain("Bob joined");
    expect(before).toMatch(/id="peer-joined-share" disabled=""[^>]*>Share a session…/);
    const after = joinedCard("inviter", { sessions: [] }, { sas_verified: true });
    expect(after).toContain("✓ checked with them.");
    expect(after).not.toContain(">It matches<");
    expect(after).toMatch(/id="peer-joined-share"(?! disabled)[^>]*>Share a session…/);
    expect(JOINED_SRC).toContain('import { ShareSessionsDialog, usePeerSessions } from "./ShareSessions";');
  });

  it("joiner: connected, then their sessions with Open, and the safety number", () => {
    const html = joinedCard("joiner", { sessions: undefined });
    expect(html).toContain("You&#x27;re connected to Bob");
    expect(html).toContain(">It matches<");
    expect(JOINED_SRC).toContain("connectedText(joined.name, view.theirs)");
    expect(JOINED_SRC).toContain('ev.subscribe("peer.session_shared", again)');
    expect(JOINED_SRC).toContain('selectSession(row ? row.title : peerName + "::" + title);');
  });

  it("never uses window.prompt / alert / confirm (the desktop app has none)", () => {
    for (const src of [INVITE_SRC, JOINED_SRC]) expect(src).not.toMatch(/\b(prompt|alert|confirm)\(/);
  });
});

describe("the People screen mounts them", () => {
  it("InviteSessions in Invite someone, JoinedCard on both sides", () => {
    expect(PEOPLE_SRC).toContain('import { InviteSessions, type MadeInvite } from "./peer/InviteSessions";');
    expect(PEOPLE_SRC).toContain('import { JoinedCard, type Joined } from "./peer/JoinedCard";');
    expect(PEOPLE_SRC).toContain("<InviteSessions");
    expect(PEOPLE_SRC).toMatch(/<JoinedCard\s+side="inviter"/);
    expect(PEOPLE_SRC).toMatch(/<JoinedCard\s+side="joiner"/);
    // What the invite opened rides on the link_added event.
    expect(PEOPLE_SRC).toContain("sessions: Array.isArray(d.sessions) ? (d.sessions as PeerInviteSession[]) : []");
  });
});
