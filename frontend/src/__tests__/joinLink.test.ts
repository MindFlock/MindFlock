/** Join by link: the app reads `#join=<code>` off its own URL (the desktop
 * shell's hand-off), "Copy invite" leads with the link, and Join someone
 * takes the link too. */

import { describe, expect, it, vi } from "vitest";
import PEOPLE_SRC from "../components/settings/screens/PeerLinks.tsx?raw";

vi.mock("../lib/peerActions", () => ({ openJoinWithCode: vi.fn() }));
vi.mock("../api/client", () => ({ api: vi.fn(async () => ({})) }));

const { joinCodeFromLocation } = await import("../lib/joinLink");
const { extractPeerCode, inviteMessage, isPeerCode } = await import("../lib/peer");

const CODE = "mfp2:" + "abcdefgh234567".repeat(4) + "-wxyz";

describe("joinCodeFromLocation", () => {
  it("reads #join=<code>", () => {
    expect(joinCodeFromLocation("http://127.0.0.1:8765/#join=" + CODE)).toBe(CODE);
    expect(joinCodeFromLocation("http://127.0.0.1:8765/?setup=1#join=" + encodeURIComponent(CODE))).toBe(CODE);
    expect(joinCodeFromLocation("http://127.0.0.1:8765/#join=" + CODE.toUpperCase())).toBe(CODE);
  });

  it("is empty for anything else", () => {
    for (const href of [
      "http://127.0.0.1:8765/",
      "http://127.0.0.1:8765/#",
      "http://127.0.0.1:8765/#" + CODE, // the /join page's hash, not ours
      "http://127.0.0.1:8765/#join=",
      "http://127.0.0.1:8765/#join=mfp2:abc",
      "http://127.0.0.1:8765/#join=" + CODE + "<script>",
      "http://127.0.0.1:8765/?join=" + CODE,
      "not a url",
    ])
      expect(joinCodeFromLocation(href)).toBe("");
  });
});

describe("the invite message", () => {
  const inv = { code: CODE, message: "Join me on MindFlock: …\n\n" + CODE, url: "https://abc.trycloudflare.com/join#" + CODE };

  it("leads with the link, then the code and how to paste it", () => {
    const text = inviteMessage(inv);
    expect(text.startsWith("Open this link to join me in MindFlock: " + inv.url + "\n\n")).toBe(true);
    expect(text.endsWith(inv.message)).toBe(true);
  });

  it("is the server's message alone without a link (an older server)", () => {
    expect(inviteMessage({ code: CODE, message: inv.message })).toBe(inv.message);
    expect(inviteMessage({ code: CODE })).toBe(CODE);
  });

  it("Copy invite copies the whole message; the card shows the link", () => {
    expect(PEOPLE_SRC).toContain("copyText(inviteMessage(invite))");
    expect(PEOPLE_SRC).toMatch(/id="peer-invite-url"\s+readOnly\s+value=\{invite\.url\}/);
    expect(PEOPLE_SRC).toContain("Copy link");
  });
});

describe("Join someone takes the link", () => {
  it("finds the code in either link, encoded or not", () => {
    for (const t of [
      "https://abc.trycloudflare.com/join#" + CODE,
      "https://abc.trycloudflare.com/join#" + encodeURIComponent(CODE),
      "mindflock://join?code=" + encodeURIComponent(CODE),
    ]) {
      expect(isPeerCode(t)).toBe(true);
      expect(extractPeerCode(t)).toBe(CODE);
    }
    expect(extractPeerCode("100% sure")).toBe("");
  });

  it("says what it takes", () => {
    expect(PEOPLE_SRC).toContain('placeholder="paste the link, the message or the code"');
  });
});
