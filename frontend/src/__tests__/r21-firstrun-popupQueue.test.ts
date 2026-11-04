/** Round 21 (JUDGE-R21-2 #6): What's new and the ask pop-up ("a session is
 * asking you something") queue — one pop-up at a time, whichever opened first
 * stays, What's new first when both are due at page load, neither lost. */

import { describe, expect, it } from "vitest";
import { popupTurn, WHATS_NEW_DECIDE_MS, type PopupQueue } from "../lib/whatsNew";
import { whatsNewStage } from "../components/whatsNew/seen";
import { RELEASES } from "../whatsNew/releases";
import { code } from "./sourceText";

const q = (o: Partial<PopupQueue>): PopupQueue => ({
  whatsNew: "done",
  askOpen: false,
  askWaiting: false,
  otherOpen: false,
  sinceLoadMs: 60_000,
  ...o,
});

describe("popupTurn", () => {
  it("What's new open stays; the ask waits behind it", () => {
    expect(popupTurn(q({ whatsNew: "shown", askWaiting: true }))).toBe("whats-new");
  });

  it("an ask pop-up open stays; a due What's new waits for it to close", () => {
    expect(popupTurn(q({ whatsNew: "due", askOpen: true }))).toBe("ask");
    // …and once it closes, What's new goes next, ahead of any other ask.
    expect(popupTurn(q({ whatsNew: "due", askOpen: false, askWaiting: true }))).toBe("whats-new");
  });

  it("both due at page load: What's new first, then the ask", () => {
    // Still deciding (versions and prefs being read): nobody opens yet.
    expect(popupTurn(q({ whatsNew: "deciding", askWaiting: true, sinceLoadMs: 1500 }))).toBeNull();
    // Decided to pop: What's new.
    expect(popupTurn(q({ whatsNew: "due", askWaiting: true, sinceLoadMs: 2500 }))).toBe("whats-new");
    expect(popupTurn(q({ whatsNew: "shown", askWaiting: true, sinceLoadMs: 3000 }))).toBe("whats-new");
    // Closed: the ask that waited opens — not lost.
    expect(popupTurn(q({ whatsNew: "done", askWaiting: true, sinceLoadMs: 9000 }))).toBe("ask");
  });

  it("a decision that never comes holds the ask back only so long", () => {
    expect(popupTurn(q({ whatsNew: "deciding", askWaiting: true, sinceLoadMs: WHATS_NEW_DECIDE_MS - 1 }))).toBeNull();
    expect(popupTurn(q({ whatsNew: "deciding", askWaiting: true, sinceLoadMs: WHATS_NEW_DECIDE_MS }))).toBe("ask");
  });

  it("a tour or another dialog holds both", () => {
    expect(popupTurn(q({ whatsNew: "due", otherOpen: true }))).toBeNull();
    expect(popupTurn(q({ askWaiting: true, otherOpen: true }))).toBeNull();
  });

  it("nothing waiting: nothing opens", () => {
    expect(popupTurn(q({}))).toBeNull();
  });
});

describe("whatsNewStage", () => {
  it("open wins over the automatic stage", () => {
    expect(whatsNewStage({ shown: RELEASES.slice(0, 1), auto: "done" })).toBe("shown");
    expect(whatsNewStage({ shown: [], auto: "due" })).toBe("due");
    expect(whatsNewStage({ shown: null, auto: "deciding" })).toBe("deciding");
  });
});

describe("the wiring", () => {
  it("the ask pop-up asks popupTurn before opening by itself, and re-tries while it waits", () => {
    const ask = code("components/dialogs/AskDialog.tsx");
    expect(ask).toMatch(/popupTurn\(\{\s*whatsNew: whatsNewStage\(useWhatsNew\.getState\(\)\)/);
    expect(ask).toMatch(/return turn === "ask" \? found : null/);
    // The interval runs another attempt (a bump alone never re-ran the effect).
    expect(ask).toMatch(/setInterval\(\(\) => \{[^}]*attempt\(\);/);
  });

  it("What's new opens by itself only on its turn, and marks its stage", () => {
    const wn = code("components/whatsNew/WhatsNew.tsx");
    expect(wn).toMatch(/const turn = popupTurn\(\{\s*whatsNew: "due",\s*askOpen: dialog === "ask"/);
    expect(wn).toContain('useWhatsNew.setState({ auto: "due" })');
    expect(wn).toContain('useWhatsNew.setState({ auto: "done" })');
  });
});
