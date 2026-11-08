/** Polish pass 5 (peer links): Remove connection… says only what exists —
 * a connection that never shared a folder isn't told about "their shared
 * session" or offered a sandbox copy to delete — and the invite's red
 * warning reads at the screen's size, not the page's 16px. */

import { describe, expect, it, vi } from "vitest";
import CARD_SRC from "../components/settings/screens/peer/PeerLinkCard.tsx?raw";
import CSS_SRC from "../components/settings/screens/PeerLinks.css?raw";

vi.mock("../api/client", () => ({ api: vi.fn(async () => ({})) }));
vi.mock("../lib/toast", () => ({ toast: vi.fn() }));
vi.stubGlobal("localStorage", {
  getItem: () => null,
  setItem: () => {},
  removeItem: () => {},
  clear: () => {},
});

const { unlinkText } =
  await import("../components/settings/screens/peer/PeerLinkCard");

describe("Remove connection… confirm", () => {
  it("nothing shared: just that they can no longer reach you", () => {
    expect(
      unlinkText({ peer_name: "Bob", shared: false, their_share: null }),
    ).toBe("Bob can no longer reach you.");
  });
  it("names only the shared folders that exist", () => {
    const theirs = unlinkText({
      peer_name: "Bob",
      shared: false,
      their_share: { shared: true, agent: "running" },
    });
    expect(theirs).toContain("Their shared session is closed, not deleted.");
    expect(theirs).not.toContain("tick delete");
    const mine = unlinkText({
      peer_name: "Bob",
      shared: true,
      their_share: { shared: false, agent: "none" },
    });
    expect(mine).toContain(
      "Your shared session is closed, not deleted, unless you tick delete.",
    );
    expect(mine).not.toContain("Their");
    const both = unlinkText({
      peer_name: "Bob",
      shared: true,
      their_share: { shared: true, agent: "running" },
    });
    expect(both).toContain(
      "Their shared session is closed, not deleted; yours too unless you tick delete.",
    );
  });
  it("offers the delete box only when there is a folder of yours", () => {
    expect(CARD_SRC).toMatch(
      /const mayDelete = confirm === "unshare" \|\| \(confirm === "unlink" && !!link\.shared\)/,
    );
    expect(CARD_SRC).toMatch(/\{mayDelete && \(\s*<label className="check">/);
    expect(CARD_SRC).toContain(
      'deleteFiles && mayDelete ? "?delete_files=1" : ""',
    );
    expect(CARD_SRC).toContain("Also delete the sandbox copy");
  });
});

describe("invite warning size", () => {
  it("the red warning and its I understand use the screen's 13px", () => {
    const rule = (sel: string) =>
      CSS_SRC.split(sel + " {")[1]?.split("}")[0] || "";
    expect(rule(".peer-guest-warn")).toContain("font-size: 13px");
    expect(rule(".peer-guest-ack")).toContain("font-size: 13px");
  });
});
