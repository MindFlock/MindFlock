/** Guest links (lib/guest.ts, lib/guestFocus.ts, docs/people.md): a browser
 * signed in with a link sees the owner's shared sessions — under the owner's
 * names — and nothing else; the owner sees where the guest is.
 *
 * Pinned here:
 *  - splitLockedRows still strips `locked` stubs an older server might send
 *    (identity-stable when nothing is locked), and the rail no longer draws
 *    crossed-out rows at all — a session off the link is simply absent;
 *  - useInstances runs the split, so every consumer of the query is clean;
 *  - the maker is warning-first: ticking "I understand" opens the picker
 *    dialog (root sessions only — sub-sessions come with their parent);
 *  - renames reach a guest read-only (lib/aliasSync) and the guest's focus
 *    is a pill on the owner's rail row with follow (lib/guestFocus);
 *  - the People screen mounts the link maker, which never carries the
 *    once-only trust controls (settingsScreens.test guards the marker). */
import { afterEach, describe, expect, it, vi } from "vitest";
import type { GuestLink, Instance } from "../api/types";
import { _resetLockedRows, guestNote, lockedRows, splitLockedRows } from "../lib/guest";
import { guestLinkLine, killedText, tunnelLine } from "../components/settings/screens/peer/GuestLinks";
import { guestBarText } from "../components/sidebar/GuestBar";

vi.mock("../lib/sessionActions", () => ({ selectSession: vi.fn() }));
vi.mock("../lib/toast", () => ({ toast: vi.fn() }));
import { selectSession } from "../lib/sessionActions";
import {
  _resetGuestFocus,
  applyFocusEvent,
  feedGuestFocus,
  focusFromLinks,
  guestName,
  guestsOn,
  isFollowing,
  isPeerFocus,
  toggleFollow,
} from "../lib/guestFocus";

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
const APP = Object.values(
  import.meta.glob<string>("../App.tsx", { query: "?raw", import: "default", eager: true })
)[0];
const ROW = Object.values(
  import.meta.glob<string>("../components/sidebar/SidebarRow.tsx", { query: "?raw", import: "default", eager: true })
)[0];
const BAR = Object.values(
  import.meta.glob<string>("../components/sidebar/GuestBar.tsx", { query: "?raw", import: "default", eager: true })
)[0];

const row = (title: string, extra: Partial<Instance> = {}): Instance =>
  ({ title, status: "running", branch: title, ...extra }) as Instance;

afterEach(() => {
  _resetLockedRows();
  _resetGuestFocus();
  vi.mocked(selectSession).mockClear();
});

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

  it("the rail never draws a crossed-out row — a session off the link is absent", () => {
    expect(SIDEBAR).not.toContain("LockedRows");
    expect(SIDEBAR).not.toContain("useLockedRows");
    // The guest note sits above the list.
    expect(SIDEBAR).toMatch(/id="guest-note"[\s\S]{0,300}<ul id="instance-list">/);
  });

  it("renames reach every page live, and a guest's read-only", () => {
    expect(APP).toContain('import { installAliasSync } from "./lib/aliasSync";');
    expect(APP).toMatch(/installAliasSync\(isGuest\)/);
    // The guest's page reports where it is, on change and on a heartbeat.
    expect(APP).toMatch(/if \(!isGuest \|\| !guestNamed \|\| !focusedTitle\) return;[\s\S]{0,400}setInterval\(report, GUEST_FOCUS_HEARTBEAT_MS\)/);
    // The guest names themselves first; the owner never does.
    expect(APP).toContain("<GuestNameDialog />");
    expect(GUESTS).not.toContain('id="peer-guests-label"');
    expect(GUESTS).toContain("json: { label: \"\", sessions:");
    expect(APP).toContain('encodeURIComponent(focusedTitle) + "/focus"');
  });

  it("the guest's pill sits on the rail row and the bar feeds it", () => {
    expect(ROW).toContain('import { GuestPills } from "../presence/GuestPill";');
    expect(ROW).toMatch(/<PresenceDots inst=\{inst\} me=\{me\} \/>\}[\s\S]{0,500}<GuestPills title=\{inst\.title\} \/>/);
    expect(BAR).toContain("installGuestFocusEvents();");
    expect(BAR).toContain("feedGuestFocus(r.links || []);");
  });

  it("the People screen mounts the link maker", () => {
    expect(PEOPLE).toContain('import { GuestLinks } from "./peer/GuestLinks";');
    expect(PEOPLE).toContain("<GuestLinks />");
  });

  it("the kill switch is on the People block and on the rail, both DELETE /api/guests/tunnel", () => {
    expect(GUESTS).toMatch(/id="peer-guests-kill-btn"[\s\S]{0,600}Kill the connection/);
    expect(GUESTS).toMatch(/api<\{ revoked: number \}>\("\/api\/guests\/tunnel", \{ method: "DELETE" \}\)/);
    expect(SIDEBAR).toContain('import { GuestBar } from "./GuestBar";');
    expect(SIDEBAR).toMatch(/<OfflineBar \/>\s*<GuestBar \/>/);
  });

  it("the link's public address comes from the tunnel and the block polls while it starts", () => {
    expect(GUESTS).toMatch(/tunnel\?\.state === "starting"/);
    expect(GUESTS).toMatch(/setInterval\(\(\) => void load\(\), 2000\)/);
    expect(GUESTS).toContain("<InstallMissing steps={install}");
  });

  it("the maker warns that a link is access to this computer; the tick opens the picker, which gates the button", () => {
    expect(GUESTS).toMatch(/id="peer-guests-warn" role="alert"[\s\S]{0,120}access to your computer/);
    expect(GUESTS).toMatch(/id="peer-guests-ack"[\s\S]{0,80}onChange=\{\(e\) => ack\(e\.target\.checked\)\}/);
    expect(GUESTS).toMatch(/const ack = \(on: boolean\) => \{\s*setUnderstood\(on\);\s*setPickOpen\(on\);/);
    // Cancelling the picker unticks; the picker is a dialog of its own.
    expect(GUESTS).toMatch(/const cancelPick = \(\) => \{\s*setPickOpen\(false\);\s*setUnderstood\(false\);/);
    expect(GUESTS).toMatch(/\{pickOpen && \(\s*<Modal id="peer-guests-pick"/);
    expect(GUESTS).toMatch(/id="peer-guests-create"[\s\S]{0,120}disabled=\{busy \|\| picked\.size === 0 \|\| !understood\}/);
    // Root sessions only: a sub-session always comes with its parent.
    expect(GUESTS).toMatch(/!i\.pending && !i\.locked && !i\.parent\)/);
  });

  it("the phone page drops the stubs (nothing to open there)", () => {
    expect(MOBILE).toMatch(/\.filter\(function \(i\) \{ return !i\.locked; \}\)/);
  });
});

describe("guest focus (the pill and follow)", () => {
  const link = (id: string, label: string, focus: GuestLink["focus"]): GuestLink =>
    ({ id, label: "", name: label, sessions: ["a"], created_at: 0, url: null, phone_url: null, local_url: "", focus }) as GuestLink;

  it("takes the server's listing: who is here and where; identity-stable when unchanged", () => {
    const prev = focusFromLinks({}, [link("g1", "Pat", { title: "a", ts: 1 }), link("g2", "", null)]);
    expect(prev).toEqual({ g1: { link: "g1", label: "Pat", title: "a", ts: 1 } });
    expect(focusFromLinks(prev, [link("g1", "Pat", { title: "a", ts: 1 })])).toBe(prev);
    expect(focusFromLinks(prev, [link("g1", "Pat", { title: "b", ts: 2 })]).g1.title).toBe("b");
    expect(focusFromLinks(prev, [])).toEqual({});
  });

  it("moves the pill on the bus event and says when a guest arrives", () => {
    const env = (title: string, joined: boolean) => ({
      seq: 1,
      event: "guest.focus_changed",
      session: title,
      old: null,
      new: title,
      ts: 5,
      data: { link: "g1", label: "Pat", title, joined },
    });
    expect(applyFocusEvent(env("a", true))).toBe("Pat joined — on a");
    expect(guestsOn("a").map((f) => f.label)).toEqual(["Pat"]);
    expect(applyFocusEvent(env("b", false))).toBe("");
    expect(guestsOn("a")).toEqual([]);
    expect(guestsOn("b")[0].title).toBe("b");
    expect(selectSession).not.toHaveBeenCalled();
  });

  it("follow: the owner's selection moves with the guest's, until they leave", () => {
    feedGuestFocus([link("g1", "Pat", { title: "a", ts: 1 })]);
    expect(toggleFollow("g1")).toBe(true);
    expect(selectSession).toHaveBeenCalledWith("a", { noKeyboard: true });
    applyFocusEvent({ seq: 2, event: "guest.focus_changed", session: "b", old: "a", new: "b", ts: 6, data: { link: "g1", label: "Pat", title: "b" } });
    expect(selectSession).toHaveBeenLastCalledWith("b", { noKeyboard: true });
    // Another guest's click does not move you.
    applyFocusEvent({ seq: 3, event: "guest.focus_changed", session: "c", old: "", new: "c", ts: 7, data: { link: "g2", label: "", title: "c" } });
    expect(selectSession).toHaveBeenCalledTimes(2);
    expect(isFollowing("g1")).toBe(true);
    // They left (no focus in the listing): follow ends.
    feedGuestFocus([link("g1", "Pat", null), link("g2", "", { title: "c", ts: 7 })]);
    expect(isFollowing("g1")).toBe(false);
    expect(guestsOn("c").length).toBe(1);
    // Toggle off by hand.
    expect(toggleFollow("g2")).toBe(true);
    expect(toggleFollow("g2")).toBe(false);
  });

  it("a peer on a shared session: event-fed, kept by the guest poll, gone on left: true", () => {
    _resetGuestFocus();
    const ev = (data: Record<string, unknown>) => ({
      seq: 9,
      event: "guest.focus_changed",
      session: String(data.title || ""),
      old: "",
      new: String(data.title || ""),
      ts: 8,
      data,
    });
    expect(applyFocusEvent(ev({ link: "peer:ab", label: "Bob", title: "api", joined: true }))).toBe(
      "Bob joined — on api"
    );
    expect(isPeerFocus("peer:ab")).toBe(true);
    expect(isPeerFocus("g1")).toBe(false);
    // The guest-links poll knows nothing of peers: the pill stays.
    feedGuestFocus([link("g1", "Pat", { title: "a", ts: 1 })]);
    expect(guestsOn("api").map((f) => f.label)).toEqual(["Bob"]);
    expect(toggleFollow("peer:ab")).toBe(true);
    // They left: the pill goes, follow ends.
    expect(applyFocusEvent(ev({ link: "peer:ab", label: "Bob", title: "api", joined: false, left: true }))).toBe("");
    expect(guestsOn("api")).toEqual([]);
    expect(isFollowing("peer:ab")).toBe(false);
    expect(guestsOn("a").length).toBe(1);
  });

  it("a peer's focus from the replayed backlog: the pill only, no toast, no follow", () => {
    // A peer who only SELECTED my row (session_focus, no terminal yet) is
    // announced once; a page opened after that has only the backlog.
    const ev = (data: Record<string, unknown>) => ({
      seq: 11,
      event: "guest.focus_changed",
      session: String(data.title || ""),
      old: "",
      new: String(data.title || ""),
      ts: 9,
      data,
    });
    toggleFollow("peer:cd");
    vi.mocked(selectSession).mockClear();
    expect(applyFocusEvent(ev({ link: "peer:cd", label: "Ann", title: "web", joined: true }), { replay: true })).toBe("");
    expect(guestsOn("web").map((f) => f.label)).toEqual(["Ann"]);
    expect(selectSession).not.toHaveBeenCalled();
    // Live, follow moves the selection with them.
    applyFocusEvent(ev({ link: "peer:cd", label: "Ann", title: "api", joined: false }));
    expect(selectSession).toHaveBeenCalledWith("api", { noKeyboard: true });
    applyFocusEvent(ev({ link: "peer:cd", label: "Ann", title: "api", left: true }), { replay: true });
    expect(guestsOn("api")).toEqual([]);
    expect(isFollowing("peer:cd")).toBe(false);
  });

  it("guestName", () => {
    expect(guestName("Pat")).toBe("Pat");
    expect(guestName("  ")).toBe("Guest");
  });
});

describe("the words", () => {
  it("guestNote", () => {
    expect(guestNote(null)).toBe("");
    expect(guestNote({ label: "", name: "Pat", sessions: ["a"] })).toBe("You're here as Pat — one session");
    expect(guestNote({ label: "", sessions: ["a", "b"] })).toBe("Guest link — 2 sessions");
  });

  it("tunnelLine / killedText / guestBarText", () => {
    const t = (state: string, extra = {}) => ({ state, url: "", installed: true, error: "", reason: "", ...extra });
    expect(tunnelLine(t("up"), 1)).toMatch(/^Public link up/);
    expect(tunnelLine(t("starting"), 1)).toMatch(/^Starting the public link/);
    expect(tunnelLine(t("failed", { error: "rate limited" }), 1)).toBe("rate limited");
    expect(tunnelLine(t("off", { installed: false }), 0)).toMatch(/needs cloudflared/);
    expect(tunnelLine(t("off"), 0)).toBe("");
    expect(killedText(0)).toBe("Guest access cut — public link closed.");
    expect(killedText(2)).toBe("Guest access cut — 2 links revoked, public link closed.");
    expect(guestBarText(1, t("up"))).toBe("1 guest link · public link up");
    expect(guestBarText(2, t("starting"))).toBe("2 guest links · public link starting…");
    expect(guestBarText(1, null)).toBe("1 guest link");
  });

  it("guestLinkLine", () => {
    expect(guestLinkLine({ label: "", sessions: ["a"] })).toBe("Guest · not opened yet · 1 session: a");
    expect(guestLinkLine({ label: "", name: "Pat", sessions: ["a", "b"] })).toBe("Pat · 2 sessions: a, b");
  });
});
