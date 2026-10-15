/** People, round 6 — joint tasks on a link card (settings/screens/peer/):
 * what the card draws in each phase (no joint / proposed by me / proposed by
 * them / active / ended), the Contract panel (rev, open proposals), the
 * Mirror row, the joint checklist slot, and the bell / toast line for each new
 * peer.* event. Server renders + the pure helpers in lib/peer.ts; the HTTP
 * shapes are round6/JOINT-TASK-CONTRACT.md's. */

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import TOASTS_SRC from "../components/EventToasts.tsx?raw";
import PEOPLE_SRC from "../components/settings/screens/PeerLinks.tsx?raw";

vi.mock("../api/client", () => ({ api: vi.fn(async () => ({})) }));
vi.mock("../lib/toast", () => ({ toast: vi.fn() }));

const {
  CONTRACT_TEMPLATE,
  PEER_LIVE_EVENTS,
  PEER_NEWS_EVENTS,
  baseName,
  contractLineDiff,
  defaultMirrorBranch,
  jointPath,
  jointPhase,
  mirrorStatusText,
  openProposals,
  peerEventNote,
  syncedText,
} = await import("../lib/peer");
const { JointView, JointStart, JointProposed, jointStartBlocker } = await import(
  "../components/settings/screens/peer/JointTask"
);
const { ContractPanel } = await import("../components/settings/screens/peer/JointContract");
const { contractBlocks } = await import("../components/settings/screens/peer/ContractMarkdown");
const { MirrorRow } = await import("../components/settings/screens/peer/MirrorRow");
const { JointCheck } = await import("../components/settings/screens/peer/JointCheck");
const { notifFromEvent } = await import("../components/NotificationsBell");

import type { PeerJoint, PeerLink, PeerMirror } from "../lib/peer";

const NOW = Date.parse("2026-10-15T09:15:00Z") / 1000;

const link = (over: Partial<PeerLink> = {}): PeerLink => ({
  link_id: "lk1",
  peer_name: "Bea",
  role: "listener",
  sas: "482-019-337-5",
  perms: { messages: true, diff: true, read_file: true },
  shared: false,
  connected: true,
  sas_verified: true,
  joint: null,
  mirror: null,
  their_share: null,
  ...over,
});

const joint = (over: Partial<PeerJoint> = {}): PeerJoint => ({
  id: "jt_abc123def456",
  brief: "Add GET /items to the API; the client fetches and prints it",
  proposed_by: "me",
  created: "2026-10-15T09:00:00Z",
  state: "proposed",
  mine: { repo_path: "/home/al/api", branch: "joint/items", program: "claude", share_id: "", session_title: "" },
  theirs: { repo_name: "client", branch: "main", program: "codex", shared: false },
  contract: {
    text: "## What we're building\n- `GET /items` returns **JSON**\n\n## Done means\nThe client prints the list.",
    rev: 3,
    updated: "2026-10-15T09:10:00Z",
    by: "peer",
  },
  proposals: [],
  ...over,
});

const view = (j: PeerJoint | null, l: Partial<PeerLink> = {}, sandbox = true) =>
  renderToStaticMarkup(
    createElement(JointView, {
      link: link(l),
      joint: j,
      agents: ["claude", "codex"],
      sandbox,
      tick: 0,
      reload: () => {},
      now: NOW,
    })
  );

describe("jointPhase", () => {
  it("reads the card's phase from state and who proposed", () => {
    expect(jointPhase(null)).toBe("none");
    expect(jointPhase(joint())).toBe("proposed-by-me");
    expect(jointPhase(joint({ proposed_by: "peer" }))).toBe("proposed-by-peer");
    expect(jointPhase(joint({ state: "active" }))).toBe("active");
    expect(jointPhase(joint({ state: "done" }))).toBe("ended");
    expect(jointPhase(joint({ state: "cancelled" }))).toBe("ended");
  });

  it("names the routes the contract gives", () => {
    expect(jointPath("a b")).toBe("/api/peer/links/a%20b/joint");
    expect(jointPath("lk1", "/accept")).toBe("/api/peer/links/lk1/joint/accept");
  });
});

describe("no joint task", () => {
  it("offers Start a joint task, carrying the tour hook", () => {
    const html = view(null);
    expect(html).toContain('data-tour="people-joint"');
    expect(html).toContain('data-joint-phase="none"');
    expect(html).toMatch(/<button[^>]*class="test-btn peer-joint-start"[^>]*>Start a joint task</);
    expect(html).not.toMatch(/peer-joint-start"[^>]*disabled/);
  });

  it("says why it can't start yet, as a sentence: safety number first, then online", () => {
    expect(jointStartBlocker(link({ sas_verified: false }))).toBe(
      "Check the safety number with Bea first — then you can start a joint task."
    );
    expect(jointStartBlocker(link({ connected: false }))).toBe("Bea is offline — start a joint task once they're back.");
    expect(jointStartBlocker(link())).toBe("");
    const html = view(null, { sas_verified: false });
    expect(html).toMatch(/<button[^>]*disabled=""[^>]*>Start a joint task</);
    expect(html).toContain("Check the safety number with Bea first");
  });

  it("the form: brief, my folder, branch, agent, and the starter contract template", () => {
    const html = renderToStaticMarkup(
      createElement(JointStart, { link: link(), agents: ["claude"], sandbox: true, tick: 0, reload: () => {}, formOpen: true })
    );
    expect(html).toContain("Start a joint task with Bea");
    expect(html).toContain("peer-joint-brief");
    expect(html).toContain('placeholder="your folder"');
    expect(html).toContain('placeholder="branch (optional)"');
    expect(html).toContain('<option value="claude"');
    for (const h of ["## What we&#x27;re building", "## Interfaces between the halves", "## Who owns what", "## Done means"])
      expect(html).toContain(h);
    // nothing to propose until there's a brief and a folder
    expect(html).toMatch(/<button type="submit"[^>]*disabled=""[^>]*>Propose to Bea</);
  });

  it("the template is the contract's four headings", () => {
    expect(CONTRACT_TEMPLATE.split("\n").filter((l) => l.startsWith("## "))).toEqual([
      "## What we're building",
      "## Interfaces between the halves",
      "## Who owns what",
      "## Done means",
    ]);
  });

  it("without the sandbox, the form says so instead of asking for a folder", () => {
    const html = renderToStaticMarkup(
      createElement(JointStart, { link: link(), agents: [], sandbox: false, tick: 0, reload: () => {}, formOpen: true })
    );
    expect(html).not.toContain('placeholder="your folder"');
    expect(html).toContain("Your half needs Linux or macOS");
  });
});

describe("proposed", () => {
  it("by me: waiting for them, with Withdraw", () => {
    const html = view(joint());
    expect(html).toContain('data-joint-phase="proposed-by-me"');
    expect(html).toContain("<strong>Proposed to Bea</strong> — waiting for them to accept.");
    expect(html).toContain(joint().brief);
    expect(html).toContain(">Withdraw<");
  });

  it("by them: the brief, their contract rendered, Accept with… and Decline", () => {
    const html = view(joint({ proposed_by: "peer" }));
    expect(html).toContain('data-joint-phase="proposed-by-peer"');
    expect(html).toContain("<strong>Bea proposes a joint task</strong>");
    expect(html).toContain("their half in client");
    expect(html).toContain("<h5>What we&#x27;re building</h5>");
    expect(html).toContain("<code>GET /items</code>");
    expect(html).toContain(">Accept with…<");
    expect(html).toContain(">Decline<");
  });

  it("Accept with… asks for my folder, branch and agent", () => {
    const html = renderToStaticMarkup(
      createElement(JointProposed, {
        link: link(),
        joint: joint({ proposed_by: "peer" }),
        agents: ["claude"],
        sandbox: true,
        tick: 0,
        reload: () => {},
        formOpen: true,
      })
    );
    expect(html).toContain('placeholder="your folder"');
    expect(html).toContain("Your half starts in this folder as soon as you accept.");
    expect(html).toMatch(/<button[^>]*peer-joint-accept"[^>]*disabled=""[^>]*>Accept</);
    expect(html).toContain(">Back<");
  });

  it("offline: Accept waits, Decline still works", () => {
    const html = view(joint({ proposed_by: "peer" }), { connected: false });
    expect(html).toMatch(/<button[^>]*disabled=""[^>]*>Accept with…</);
    expect(html).toContain("Bea is offline — answer once they&#x27;re back.");
  });
});

describe("active", () => {
  const active = (over: Partial<PeerJoint> = {}) =>
    joint({
      state: "active",
      mine: { repo_path: "/home/al/api", branch: "joint/items", program: "claude", share_id: "sh1", session_title: "peer-bea-api" },
      theirs: { repo_name: "client", branch: "main", program: "codex", shared: true },
      ...over,
    });

  it("shows the brief and both halves with Open session / Watch their session", () => {
    const html = view(active());
    expect(html).toContain('data-joint-phase="active"');
    expect(html).toContain("<strong>Joint task with Bea</strong>");
    expect(html).toContain("api · joint/items · claude");
    expect(html).toContain("Bea&#x27;s half");
    expect(html).toContain("client · main · codex");
    expect(html).toMatch(/peer-half-open"[^>]*>Open session</);
    expect(html).toMatch(/<button type="button" class="test-btn peer-half-watch"(?![^>]*disabled)[^>]*>Watch their session</);
  });

  it("Watch their session waits for their half and for them to be online", () => {
    const notYet = view(active({ theirs: { repo_name: "client", branch: "", program: "", shared: false } }));
    expect(notYet).toMatch(/peer-half-watch"[^>]*disabled=""[^>]*title="Their half hasn&#x27;t started yet"/);
    expect(view(active(), { connected: false })).toMatch(/peer-half-watch"[^>]*disabled=""[^>]*title="They&#x27;re offline"/);
  });

  it("my half without a session yet says it is starting; no folder says so", () => {
    expect(view(active({ mine: { ...active().mine, session_title: "" } }))).toContain("Its session is starting…");
    expect(view(active({ mine: { repo_path: "", branch: "", program: "", share_id: "", session_title: "" } }))).toContain(
      "No folder from your side"
    );
  });

  it("my half's start failure is a sentence in its place", () => {
    const html = view(active({ mine: { ...active().mine, session_title: "", error: "the sandbox couldn't start (bubblewrap is missing)" } }));
    expect(html).toContain("Your half didn&#x27;t start: the sandbox couldn&#x27;t start (bubblewrap is missing)");
    expect(html).not.toContain("Its session is starting…");
  });

  it("holds the Contract panel, the Mirror row, Done and Cancel", () => {
    const html = view(active());
    expect(html).toContain('class="peer-contract" data-rev="3"');
    expect(html).toContain("rev 3 · changed by Bea 5m ago");
    expect(html).toContain(">Edit<");
    expect(html).toContain('class="peer-mirror" data-mirror="off"');
    expect(html).toContain(">Done<");
    expect(html).toContain(">Cancel…<");
  });

  it("the joint checklist slot is empty until the server has one, then shows it", () => {
    expect(view(active())).not.toContain("peer-joint-check");
    const html = view(
      active({
        check: {
          steps: [
            { id: "s1", text: "GET /items returns a list", expect: "200 + JSON array", actor: "agent", side: "mine" },
            { id: "s2", text: "The client prints it", expect: "items on stdout", actor: "human", side: "theirs" },
          ],
          mine: { results: { s1: { result: "pass", note: "", at: "", by: "agent" } }, tested_sha: "abc", at: "" },
          theirs: { results: {}, tested_sha: "", at: "" },
          verdict: "needs_you",
        },
      })
    );
    expect(html).toContain('class="peer-joint-check" data-verdict="needs_you"');
    // The joint check's card (JointCheck.tsx, r6w-joint-verify): steps by
    // side with who checks them; read-only without a link id.
    expect(html).toContain("Needs Bea — 1 step");
    expect(html).toContain("Your side");
    expect(html).toContain("Bea&#x27;s side");
    expect(html).toMatch(/peer-joint-actor-human">person</);
    expect(html).toMatch(/peer-joint-step-pass[\s\S]*>Pass</);
    expect(html).toMatch(/peer-joint-step-blank[\s\S]*>Not yet</);
  });
});

describe("ended", () => {
  it("cancelled or done: says which, and offers a new one", () => {
    const c = view(joint({ state: "cancelled" }));
    expect(c).toContain('data-joint-phase="ended"');
    expect(c).toContain("The joint task was cancelled: ");
    expect(c).toContain(">Start another joint task<");
    expect(view(joint({ state: "done" }))).toContain("The joint task is done: ");
  });
});

describe("the Contract panel", () => {
  const panel = (j: Partial<PeerJoint>) =>
    renderToStaticMarkup(
      createElement(ContractPanel, { linkId: "lk1", peerName: "Bea", joint: joint(j), reload: () => {}, now: NOW })
    );

  it("renders the contract as Markdown with its rev", () => {
    const html = panel({});
    expect(html).toContain("rev 3");
    expect(html).toContain("<h5>Done means</h5>");
    expect(html).toContain("<li><code>GET /items</code> returns <strong>JSON</strong></li>");
  });

  it("lists only OPEN proposals, each with Accept / Reject and whose agent asked", () => {
    const html = panel({
      proposals: [
        { id: "cp_1", by: "peer", text: "## Interfaces\n- add `?limit=`", at: "2026-10-15T09:13:00Z", state: "open" },
        { id: "cp_2", by: "me", text: "x", at: "2026-10-15T09:14:00Z", state: "open" },
        { id: "cp_3", by: "peer", text: "old", at: "2026-10-15T09:00:00Z", state: "rejected" },
      ],
    });
    expect(html.match(/class="peer-proposal"/g)).toHaveLength(2);
    expect(html).toContain('data-proposal-id="cp_1"');
    expect(html).not.toContain('data-proposal-id="cp_3"');
    expect(html).toContain("<strong>Bea&#x27;s agent</strong> proposes a change");
    expect(html).toContain("<strong>Your agent</strong> proposes a change");
    expect(html.match(/>Accept</g)).toHaveLength(2);
    expect(html.match(/>Reject</g)).toHaveLength(2);
  });

  it("shows a proposal as what it changes, the whole text folded", () => {
    const html = panel({
      proposals: [{ id: "cp_1", by: "peer", text: joint().contract.text.replace("returns **JSON**", "returns **JSON**, `?limit=`"), at: "", state: "open" }],
    });
    expect(html).toContain('<span class="peer-diff-del">− - `GET /items` returns **JSON**</span>');
    expect(html).toContain('<span class="peer-diff-add">+ - `GET /items` returns **JSON**, `?limit=`</span>');
    expect(html).toContain("<summary>The whole proposed contract</summary>");
  });

  it("contractLineDiff: adds and removes in order, ignores blank lines", () => {
    expect(contractLineDiff("a\nb\nc", "a\nB\nc\n\nd")).toEqual([
      { op: "-", text: "b" },
      { op: "+", text: "B" },
      { op: "+", text: "d" },
    ]);
    expect(contractLineDiff("same", "same")).toEqual([]);
  });

  it("an empty contract says what to do", () => {
    expect(panel({ contract: { text: "", rev: 0, updated: "", by: "me" } })).toContain("The contract is empty — Edit to write one.");
  });

  it("openProposals keeps order and drops answered ones", () => {
    expect(
      openProposals(
        joint({
          proposals: [
            { id: "a", by: "peer", text: "", at: "", state: "accepted" },
            { id: "b", by: "peer", text: "", at: "", state: "open" },
          ],
        })
      ).map((p) => p.id)
    ).toEqual(["b"]);
  });
});

describe("contract Markdown", () => {
  it("splits headings, lists, fences and paragraphs", () => {
    expect(contractBlocks("# A\n\npara one\nstill one\n\n- x\n- y\n1. n\n```\ncode\n```")).toEqual([
      { kind: "h", level: 1, text: "A" },
      { kind: "p", text: "para one still one" },
      { kind: "ul", items: ["x", "y"] },
      { kind: "ol", items: ["n"] },
      { kind: "code", text: "code" },
    ]);
    expect(contractBlocks("")).toEqual([]);
  });

  it("never passes markup through", () => {
    const html = renderToStaticMarkup(
      createElement(ContractPanel, {
        linkId: "lk1",
        peerName: "Bea",
        joint: joint({ contract: { text: "<img src=x onerror=alert(1)>", rev: 1, updated: "", by: "peer" } }),
        reload: () => {},
      })
    );
    expect(html).not.toContain("<img");
    expect(html).toContain("&lt;img");
  });
});

describe("the Mirror row", () => {
  const mirror = (over: Partial<PeerMirror> = {}): PeerMirror => ({
    repo_path: "/home/al/api",
    branch: "peer/Bea",
    enabled: true,
    last_head: "abc1234def", // pragma: allowlist secret
    last_at: "2026-10-15T09:10:00Z",
    last_error: "",
    ...over,
  });
  const row = (m: PeerMirror | null) =>
    renderToStaticMarkup(createElement(MirrorRow, { link: { link_id: "lk1", peer_name: "Bea", mirror: m }, reload: () => {}, now: NOW }));

  it("off: Mirror their work into [repo] as peer/<name>, Enable once a repo is picked", () => {
    const html = row(null);
    expect(html).toContain("Mirror their work into");
    expect(html).toContain('value="peer/Bea"');
    expect(html).toMatch(/peer-mirror-enable"[^>]*disabled=""[^>]*>Enable</);
  });

  it("on: what it mirrors, when it last updated, and Disable", () => {
    const html = row(mirror());
    expect(html).toContain('data-mirror="on"');
    expect(html).toContain("Mirroring Bea&#x27;s work into peer/Bea in api — last updated 5m ago (abc1234).");
    expect(html).toContain(">Disable<");
  });

  it("the last error is a sentence", () => {
    expect(row(mirror({ last_error: "peer/Bea is checked out in api — switch branches there first." }))).toContain(
      "Last mirror failed: peer/Bea is checked out in api — switch branches there first."
    );
  });

  it("status line before the first update, and when off", () => {
    expect(mirrorStatusText(mirror({ last_head: "", last_at: "" }), "Bea", NOW)).toBe(
      "Mirroring Bea's work into peer/Bea in api — waiting for their first change."
    );
    expect(mirrorStatusText(mirror({ enabled: false }), "Bea", NOW)).toBe("Mirror is off.");
    expect(mirrorStatusText(mirror({ updating: true }), "Bea", NOW)).toBe("Mirroring Bea's work into peer/Bea in api — updating now…");
  });

  it("suggests a peer/ branch from their name", () => {
    expect(defaultMirrorBranch("Bea Q.")).toBe("peer/Bea-Q.");
    expect(defaultMirrorBranch("")).toBe("peer/work");
    expect(baseName("/home/al/api/")).toBe("api");
    expect(baseName("C:\\src\\api")).toBe("api");
  });

  it("an empty checklist renders nothing", () => {
    expect(renderToStaticMarkup(createElement(JointCheck, { check: null, peerName: "Bea" }))).toBe("");
  });
});

describe("bell rows and toasts for the joint and mirror events", () => {
  const env = (event: string, data: Record<string, unknown>) => ({
    event,
    session: "",
    seq: 1,
    ts: 0,
    old: null,
    new: null,
    data,
  });

  it("a proposal: who, and where to accept", () => {
    const n = peerEventNote("peer.joint_proposed", { link_id: "lk1", peer_name: "Bea", brief: "Add GET /items" });
    expect(n?.toast).toBe("Bea proposes a joint task — accept in People");
    expect(n?.text).toBe("Bea proposes a joint task — accept in People: Add GET /items");
    expect(notifFromEvent(env("peer.joint_proposed", { peer_name: "Bea" }))?.peer).toBe(true);
  });

  it("a contract event is a bell row only for THEIR agent's open proposal; my agent's is a toast", () => {
    const base = { link_id: "lk1", peer_name: "Bea", joint_id: "jt_1" };
    const theirs = peerEventNote("peer.joint_contract", { ...base, kind: "proposal", by: "peer", open: true, proposal_id: "cp_1", state: "open", rev: 3 });
    expect(theirs?.text).toBe("Bea's agent proposes a contract change");
    expect(theirs?.toast).toBe(theirs?.text);
    const mine = peerEventNote("peer.joint_contract", { ...base, kind: "proposal", by: "agent", open: true, proposal_id: "cp_2", state: "open" });
    expect(mine?.text).toBe("");
    expect(mine?.toast).toBe("Your agent proposes a contract change — review it in People");
    expect(notifFromEvent(env("peer.joint_contract", { ...base, kind: "proposal", by: "agent", open: true }))).toBeNull();
    // a revision, an answered proposal, my own click: not news
    expect(peerEventNote("peer.joint_contract", { ...base, kind: "revision", by: "peer", open: false, rev: 4 })).toBeNull();
    expect(peerEventNote("peer.joint_contract", { ...base, kind: "proposal", by: "peer", open: false, state: "accepted" })).toBeNull();
    expect(peerEventNote("peer.joint_contract", { ...base, kind: "proposal", by: "you", open: true })).toBeNull();
    expect(notifFromEvent(env("peer.joint_contract", { ...base, kind: "proposal", by: "peer", open: true }))?.peer).toBe(true);
  });

  it("an action the other side missed (offline) says it will sync", () => {
    expect(syncedText("Contract saved", { synced: false }, "Bea")).toBe("Contract saved — will sync when Bea reconnects");
    expect(syncedText("Contract saved", { synced: true }, "Bea")).toBe("Contract saved");
    expect(syncedText("Declined", null, "Bea")).toBe("Declined");
  });

  it("their answer to a joint task is news; my own clicks are not", () => {
    expect(peerEventNote("peer.joint_state", { peer_name: "Bea", state: "active", by: "peer" })?.text).toBe(
      "Bea accepted the joint task"
    );
    expect(peerEventNote("peer.joint_state", { peer_name: "Bea", state: "cancelled", by: "peer" })?.cls).toBe("n-warn");
    expect(peerEventNote("peer.joint_state", { peer_name: "Bea", state: "active", by: "you" })).toBeNull();
    expect(peerEventNote("peer.joint_state", { peer_name: "Bea", state: "active" })).toBeNull();
  });

  it("the mirror: updated names the branch and repo; failed is a sentence with the reason", () => {
    expect(
      peerEventNote("peer.mirror_updated", { peer_name: "Bea", branch: "peer/Bea", repo_path: "/home/al/api" })?.text
    ).toBe("Bea's work updated peer/Bea in api");
    const f = peerEventNote("peer.mirror_failed", { peer_name: "Bea", repo_path: "/home/al/api", error: "the repo is gone" });
    expect(f?.text).toBe("Couldn't mirror Bea's work into api: the repo is gone");
    expect(f?.toast).toBe("Couldn't mirror Bea's work into api");
    expect(f?.cls).toBe("n-warn");
    expect(notifFromEvent(env("peer.mirror_updated", { peer_name: "Bea", branch: "peer/Bea", repo_name: "api" }))?.text).toBe(
      "Bea's work updated peer/Bea in api"
    );
  });

  it("toasts subscribe to every news event; the screen refetches on every live one", () => {
    expect(TOASTS_SRC).toContain("for (const name of PEER_NEWS_EVENTS)");
    expect(PEOPLE_SRC).toContain("PEER_LIVE_EVENTS.map((name) =>");
    for (const e of ["peer.joint_proposed", "peer.joint_contract", "peer.mirror_updated", "peer.mirror_failed"]) {
      expect(PEER_NEWS_EVENTS as readonly string[]).toContain(e);
      expect(PEER_LIVE_EVENTS as readonly string[]).toContain(e);
    }
    expect(PEER_LIVE_EVENTS as readonly string[]).toContain("peer.joint_state");
    // peer.state is news only when a re-pair brought a new safety number;
    // a plain connect / drop is the card's dot.
    expect(PEER_NEWS_EVENTS as readonly string[]).toContain("peer.state");
    expect(peerEventNote("peer.state", { peer_name: "Bea", connected: true })).toBeNull();
  });
});
