/** Guest links (lib/guest.ts, docs/people.md): a browser signed in with a
 * link sees the owner's other sessions as crossed-out stubs (`locked`). The
 * stubs reach the rail and nothing else.
 *
 * Pinned here:
 *  - splitLockedRows keeps the usable rows (identity-stable when nothing is
 *    locked) and parks the stubs for useLockedRows;
 *  - useInstances runs the split, so every consumer of the query is clean;
 *  - the rail draws the stubs as `.inst.locked` rows with the ✕, after this
 *    device's rows, and never through SidebarRow;
 *  - the People screen mounts the link maker, which never carries the
 *    once-only trust controls (settingsScreens.test guards the marker). */
import { afterEach, describe, expect, it } from "vitest";
import type { Instance } from "../api/types";
import { _resetLockedRows, guestNote, lockedRows, splitLockedRows } from "../lib/guest";
import { guestLinkLine } from "../components/settings/screens/peer/GuestLinks";

const SIDEBAR = Object.values(
  import.meta.glob<string>("../components/sidebar/Sidebar.tsx", { query: "?raw", import: "default", eager: true })
)[0];
const QUERIES = Object.values(
  import.meta.glob<string>("../state/queries.ts", { query: "?raw", import: "default", eager: true })
)[0];
const PEOPLE = Object.values(
  import.meta.glob<string>("../components/settings/screens/PeerLinks.tsx", {
    query: "?raw",
    import: "default",
    eager: true,
  })
)[0];
const GUESTS = Object.values(
  import.meta.glob<string>("../components/settings/screens/peer/GuestLinks.tsx", {
    query: "?raw",
    import: "default",
    eager: true,
  })
)[0];
const MOBILE = Object.values(
  import.meta.glob<string>("../../../backend/web/static/mobile.js", { query: "?raw", import: "default", eager: true })
)[0];

const row = (title: string, extra: Partial<Instance> = {}): Instance =>
  ({ title, status: "running", branch: title, ...extra }) as Instance;

afterEach(() => _resetLockedRows());

describe("splitLockedRows", () => {
  it("returns the same list when nothing is locked (the owner's every poll)", () => {
    const list = [row("a"), row("b")];
    expect(splitLockedRows(list)).toBe(list);
  });

  it("keeps the usable rows and parks the stubs", () => {
    const list = [row("a"), row("b", { locked: true, status: "paused" }), row("mac::c", { locked: true, device: "mac" })];
    const kept = splitLockedRows(list);
    expect(kept.map((i) => i.title)).toEqual(["a"]);
    // The store is what the rail reads: title + status + device, no more.
    expect(lockedRows()).toEqual([
      { title: "b", status: "paused", device: undefined },
      { title: "mac::c", status: "running", device: "mac" },
    ]);
    // Identity-stable across an unchanged poll; cleared when the owner's
    // list (nothing locked) comes back.
    const before = lockedRows();
    splitLockedRows(list);
    expect(lockedRows()).toBe(before);
    splitLockedRows([row("a")]);
    expect(lockedRows()).toEqual([]);
  });

  it("tolerates a non-list body", () => {
    expect(splitLockedRows(null as unknown as Instance[])).toBeNull();
  });
});

describe("the wiring", () => {
  it("useInstances strips the stubs before anyone sees them", () => {
    expect(QUERIES).toMatch(/splitLockedRows\(await api<Instance\[\]>\("\/api\/instances"\)\)/);
  });

  it("the rail draws them crossed out, after this device's rows, not as SidebarRow", () => {
    expect(SIDEBAR).toContain("<LockedRows rows={lockedRows.filter((r) => !r.device)} />");
    expect(SIDEBAR).toMatch(/className="inst locked"[\s\S]{0,200}✕/);
    expect(SIDEBAR).toMatch(/function LockedRows[\s\S]{0,900}<\/li>/);
    expect(SIDEBAR).not.toMatch(/function LockedRows[\s\S]{0,900}<SidebarRow/);
    // The guest note sits above the list.
    expect(SIDEBAR).toMatch(/id="guest-note"[\s\S]{0,300}<ul id="instance-list">/);
  });

  it("the People screen mounts the link maker", () => {
    expect(PEOPLE).toContain('import { GuestLinks } from "./peer/GuestLinks";');
    expect(PEOPLE).toContain("<GuestLinks />");
  });

  it("the maker warns that a link is access to this computer, and the warning gates the button", () => {
    expect(GUESTS).toMatch(/id="peer-guests-warn" role="alert"[\s\S]{0,120}access to your computer/);
    expect(GUESTS).toMatch(/id="peer-guests-ack"/);
    expect(GUESTS).toMatch(/id="peer-guests-create"[\s\S]{0,120}disabled=\{busy \|\| picked\.size === 0 \|\| !understood\}/);
  });

  it("the phone page drops the stubs (nothing to open there)", () => {
    expect(MOBILE).toMatch(/\.filter\(function \(i\) \{ return !i\.locked; \}\)/);
  });
});

describe("the words", () => {
  it("guestNote", () => {
    expect(guestNote(null)).toBe("");
    expect(guestNote({ label: "Pat", sessions: ["a"] })).toBe("Guest link for Pat — one session");
    expect(guestNote({ label: "", sessions: ["a", "b"] })).toBe("Guest link — 2 sessions");
  });

  it("guestLinkLine", () => {
    expect(guestLinkLine({ label: "", sessions: ["a"] })).toBe("Guest · 1 session: a");
    expect(guestLinkLine({ label: "Pat", sessions: ["a", "b"] })).toBe("Pat · 2 sessions: a, b");
  });
});
