/** Shared files in the app (lib/hub.ts, components/hub — internally "the
 * hub"): the pure rules — the `file:` (and the first `hub:`) token a composer
 * answers, which items match, the words an item is described with, a pasted
 * screenshot's name, the selection's range and Pin split — and the wiring
 * pinned against the sources: the sidebar row and its calm count, the
 * shortcut, the palette, the composers, the rail rows, the terminal paste and
 * the send step inside the drawer. */

import { describe, expect, it, vi } from "vitest";
import PANEL from "../components/hub/HubPanel.tsx?raw";
import PANEL_CSS from "../components/hub/HubPanel.css?raw";
import SEND from "../components/hub/HubSendStep.tsx?raw";
import PALETTE from "../components/palette/CommandPalette.tsx?raw";
import LIBHUB from "../lib/hub.ts?raw";
import HUBDROP from "../components/hub/hubDrop.ts?raw";
import ATTACH from "../components/hub/HubAttach.tsx?raw";
import TOPBAR from "../components/TopBar.tsx?raw";
import HUBBAR from "../components/sidebar/HubBar.tsx?raw";
import BARS from "../components/sidebar/SidebarBars.tsx?raw";
import TOOLBARS_CSS from "../components/sidebar/toolbars.css?raw";
import KEYMAP from "../lib/keymap.ts?raw";
import CLIP from "../lib/clipboard.ts?raw";
import THREAD from "../components/grid/ThreadTab.tsx?raw";
import QUEUE from "../components/grid/QueueTab.tsx?raw";
import ROW from "../components/sidebar/SidebarRow.tsx?raw";
import HUBREF from "../lib/hubRef.ts?raw";

const noop = () => {};
vi.stubGlobal("localStorage", { getItem: () => null, setItem: noop, removeItem: noop });
const g = globalThis as unknown as { document?: unknown };
if (typeof g.document === "undefined")
  g.document = {
    hidden: false,
    title: "",
    documentElement: { classList: { contains: () => false, add: noop, remove: noop, toggle: noop } },
    addEventListener: noop,
    removeEventListener: noop,
  };

const hub = await import("../lib/hub");
const { hubTargets } = await import("../components/hub/HubSendStep");
const hubref = await import("../lib/hubRef");
const panel = await import("../components/hub/HubPanel");
const attach = await import("../components/hub/HubAttach");

const item = (over: Partial<import("../lib/hub").HubItem> = {}) =>
  ({
    id: "k7m2",
    ref: "file:k7m2",
    name: "login error.png",
    kind: "image",
    mime: "image/png",
    size: 120 * 1024,
    sha256: "x",
    by: "you",
    by_name: "",
    added: 1000,
    used: 1000,
    note: "",
    pinned: false,
    textual: false,
    preview: "",
    path: "/h/hub/k7m2/login error.png",
    expires: 1000 + 14 * 86400,
    ...over,
  }) as import("../lib/hub").HubItem;

describe("the code token a composer answers", () => {
  it("opens on file: (and the first hub:) at a word start, with what follows as the query", () => {
    expect(hub.refTokenAt("look at file:", 13)).toEqual({ start: 8, prefix: "file:", query: "" });
    expect(hub.refTokenAt("file:k7", 7)).toEqual({ start: 0, prefix: "file:", query: "k7" });
    expect(hub.refTokenAt("see (file:log", 13)).toEqual({ start: 5, prefix: "file:", query: "log" });
    expect(hub.refTokenAt("look at hub:", 12)).toEqual({ start: 8, prefix: "hub:", query: "" });
    expect(hub.refTokenAt("hub:k7", 6)).toEqual({ start: 0, prefix: "hub:", query: "k7" });
  });
  it("never inside a word or a path, nor after the caret moved on, nor for prose", () => {
    expect(hub.refTokenAt("github:", 7)).toBeNull();
    expect(hub.refTokenAt("profile:", 8)).toBeNull();
    expect(hub.refTokenAt("a/hub:", 6)).toBeNull();
    expect(hub.refTokenAt("a/file:", 7)).toBeNull();
    expect(hub.refTokenAt("hub:k7m2  and more", 18)).toBeNull();
    // "the config file: main.py" — a space after the colon is prose.
    expect(hub.refTokenAt("the config file: main", 21)).toBeNull();
  });
});

describe("matching items", () => {
  const items = [item({ id: "k7m2", name: "a.png" }), item({ id: "zz11", name: "server.log" }), item({ id: "k7aa", name: "b.png" })];
  it("by id prefix first, then by name", () => {
    expect(hub.matchItems(items, "k7").map((i) => i.id)).toEqual(["k7m2", "k7aa"]);
    expect(hub.matchItems(items, "log").map((i) => i.id)).toEqual(["zz11"]);
    expect(hub.matchItems(items, "").length).toBe(3);
  });
});

describe("words", () => {
  it("kind, size, who and expiry read the vocabulary's words", () => {
    expect(hub.kindLabel(item())).toBe("Picture");
    expect(hub.kindLabel(item({ kind: "text" }))).toBe("Snippet");
    expect(hub.kindLabel(item({ kind: "file", name: "spec.pdf" }))).toBe("PDF file");
    expect(hub.kindLabel(item({ kind: "file", name: "Makefile" }))).toBe("File");
    expect(hub.humanSize(120 * 1024)).toBe("120 KB");
    expect(hub.humanSize(1)).toBe("1 byte");
    expect(hub.whoLabel({ by: "phone", by_name: "" })).toBe("your phone");
    expect(hub.whoLabel({ by: "you", by_name: "" })).toBe("you");
    expect(hub.expiryLabel(item({ pinned: true }))).toBe("Pinned — kept until you remove it");
    expect(hub.expiryLabel(item(), 1000 + 5 * 86400)).toBe("Deleted automatically in 9 days unless used — Pin to keep");
    expect(hub.usageLine({ count: 1, used: 2048, quota: 1024 ** 3 })).toBe("1 item · 2.0 KB of 1.0 GB");
  });
  it("a pasted screenshot is named by the time, with no space in its path", () => {
    const f = hub.renamePasted(new File(["x"], "image.png", { type: "image/png" }), new Date(2026, 10, 22, 14, 3, 5));
    expect(f.name).toBe("screenshot-14-03-05.png");
    const kept = hub.renamePasted(new File(["x"], "diagram.png", { type: "image/png" }));
    expect(kept.name).toBe("diagram.png");
  });
  it("one item or several: a name or a count, the codes on one line", () => {
    expect(hub.itemsWord([item()])).toBe("login error.png");
    expect(hub.itemsWord([item(), item()])).toBe("2 items");
    expect(hub.codesOf([item(), item({ ref: "file:zz11" })])).toBe("file:k7m2 file:zz11");
    expect(hub.batchLine(3, 3, "Pinned")).toBe("Pinned 3 items");
    expect(hub.batchLine(1, 1, "Pinned")).toBe("Pinned 1 item");
    expect(hub.batchLine(2, 3, "Removed")).toBe("Removed 2 of 3 — why 1 couldn't be is in Shared files");
  });
  it("raw URLs: inline only by the server's choice, a download asks for one", () => {
    expect(hub.rawUrl(item())).toBe("/api/hub/items/k7m2/raw");
    expect(hub.rawUrl(item(), true)).toBe("/api/hub/items/k7m2/raw?download=1");
  });
});

describe("Send to session…", () => {
  it("offers this device's own sessions only", () => {
    const rows = [
      { title: "api" },
      { title: "mac::web" },
      { title: "shared", peer_share: true },
      { title: "guest-locked", locked: true },
    ] as unknown as import("../api/types").Instance[];
    expect(hubTargets(rows).map((r) => r.title)).toEqual(["api"]);
  });
  it("never overwrites silently: a taken path is said before Copy", () => {
    expect(SEND).toContain("already exists there");
    expect(SEND).toContain("Replace it");
    expect(SEND).toContain("freePath(title, one, path.trim())");
    // Several: each at hub/<name> or the next free name — the batch route.
    expect(SEND).toContain("copyIntoMany(title, items, tell)");
    expect(LIBHUB).toContain('batch("copy", items, title)');
  });
  it("is a step INSIDE the drawer (with Back), never a dialog the drawer could cover", () => {
    expect(PANEL).toContain("<HubSendStep");
    expect(SEND).not.toContain("<Modal");
    expect(SEND).toContain("← Back");
    expect(SEND).toMatch(/e\.key === "Escape"[\s\S]{0,120}onBack\(\)/);
    // The store opens the drawer on the step and forgets it on close.
    hub.useHubUi.getState().setOpen(false);
    hub.useHubUi.getState().openSend([item(), item({ id: "zz11", ref: "file:zz11" })]);
    expect(hub.useHubUi.getState().open).toBe(true);
    expect(hub.useHubUi.getState().sending?.length).toBe(2);
    hub.useHubUi.getState().setSelected(["k7m2"]);
    hub.useHubUi.getState().setOpen(false);
    expect(hub.useHubUi.getState().sending).toBeNull();
    expect(hub.useHubUi.getState().selected).toEqual([]);
  });
});

describe("several at once", () => {
  const ids = ["a", "b", "c", "d", "e"];
  it("a click ticks one; Shift+click ticks the range from the last one clicked", () => {
    expect(panel.rangeSelect([], ids, null, "b", true, false)).toEqual(["b"]);
    expect(panel.rangeSelect(["b"], ids, "b", "d", true, true)).toEqual(["b", "c", "d"]);
    // Backwards, and un-ticking a range.
    expect(panel.rangeSelect(["e"], ids, "e", "c", true, true)).toEqual(["c", "d", "e"]);
    expect(panel.rangeSelect(["a", "b", "c", "d"], ids, "a", "c", false, true)).toEqual(["d"]);
    // A ticked item the filter hides stays ticked (the bar says so).
    expect(panel.rangeSelect(["zz"], ids, null, "a", true, false)).toEqual(["a", "zz"]);
  });
  it("Pin and Unpin say how many each acts on when the selection is mixed", () => {
    expect(panel.pinSplit([item(), item({ pinned: true }), item()])).toEqual({ toPin: 2, toUnpin: 1 });
    expect(panel.pinSplit([item({ pinned: true })])).toEqual({ toPin: 0, toUnpin: 1 });
    expect(PANEL).toContain('{toUnpin ? "Pin " + toPin : "Pin"}');
    expect(PANEL).toContain('{toPin ? "Unpin " + toUnpin : "Unpin"}');
    expect(PANEL).toContain("not shown by the filter");
  });
  it("a drag of a ticked card carries the whole selection", () => {
    const all = [item({ id: "a1a1" }), item({ id: "b2b2" }), item({ id: "c3c3" })];
    expect(hub.dragSet(all[1], all, ["a1a1", "b2b2"]).map((i) => i.id)).toEqual(["a1a1", "b2b2"]);
    expect(hub.dragSet(all[2], all, ["a1a1", "b2b2"]).map((i) => i.id)).toEqual(["c3c3"]);
    const store: Record<string, string> = {};
    const dt = { setData: (k: string, v: string) => (store[k] = v), getData: (k: string) => store[k] || "", effectAllowed: "" };
    hubref.setDragItems(dt as unknown as DataTransfer, [
      { id: "a1a1", ref: "file:a1a1", name: "a" },
      { id: "b2b2", ref: "hub:b2b2", name: "b" },
    ]);
    expect(store["text/plain"]).toBe("file:a1a1 hub:b2b2");
    expect(hubref.dragItems(dt as unknown as DataTransfer).map((i) => i.ref)).toEqual(["file:a1a1", "hub:b2b2"]);
    // An older single-item payload still reads.
    store[hubref.HUB_MIME] = JSON.stringify({ id: "z", ref: "hub:zzzz", name: "z" });
    expect(hubref.dragItems(dt as unknown as DataTransfer).map((i) => i.ref)).toEqual(["hub:zzzz"]);
    expect(HUBDROP).toContain("putInPrompt(title, items,");
  });
  it("the batch is one request; Remove asks once, naming the count", () => {
    expect(LIBHUB).toContain('api<BatchResult>("/api/hub/batch"');
    expect(PANEL).toContain("removeItems(these)");
    expect(PANEL).toContain('Remove {selLocal.length === 1 ? "it" : selLocal.length}');
    expect(PANEL).toContain("Keep");
    expect(PANEL).toContain("Select all");
  });
});

describe("wiring", () => {
  it("Shared files is a sidebar row (Open / Add, a calm count) — nothing of it on the top bar", () => {
    // Gone from the top bar: no button, no ⋯ row, no dot; ⋯ is only the fold.
    expect(TOPBAR).not.toContain('id="hub-btn"');
    expect(TOPBAR).not.toContain('data-fold="hub"');
    expect(TOPBAR).not.toContain('key: "hub"');
    expect(TOPBAR).not.toContain("Shared files</span>");
    expect(TOPBAR).not.toContain("moreAlways");
    expect(TOPBAR).not.toContain("tb-new-dot");
    expect(TOPBAR).not.toContain("useHub");
    expect(TOPBAR).toContain("{hasFolded && (");
    // The sidebar row: the bar key "hub" renders HubBar.
    expect(BARS).toMatch(/case "hub":[\s\S]*?return <HubBar \/>;/);
    expect(HUBBAR).toContain('id="hub-bar"');
    expect(HUBBAR).toContain('"Shared files — files, pictures and text every session can use"');
    // Open toggles the drawer (as Alt+H does); Add opens it into the picker.
    expect(HUBBAR).toContain("useHubUi.getState().toggle();");
    expect(HUBBAR).toContain("useHubUi.getState().openAdd();");
    // A narrow window's sidebar drawer would cover the Shared files drawer.
    expect(HUBBAR).toContain("if (ui.narrow && !ui.sidebarHidden) ui.toggleSidebar();");
    // The count: only what is new to you, zero while the drawer is open,
    // drawn as the calm accent pill — never the red of "needs you".
    expect(HUBBAR).toContain("const count = open ? 0 : newCount(items || []);");
    expect(HUBBAR).toContain('className="hb-new"');
    expect(HUBBAR).not.toContain("is-failing");
    expect(TOOLBARS_CSS).toMatch(/#hub-bar \.hb-new \{[^}]*rgba\(var\(--accent-rgb\)/);
    expect(TOOLBARS_CSS).not.toMatch(/#hub-bar[^{]*\{[^}]*var\(--red/);
    // A guest link has none.
    expect(HUBBAR).toContain("if (guest) return null;");
  });
  it("Add opens the drawer straight into its file picker", () => {
    expect(LIBHUB).toContain("set({ open: true, sending: null, wantAdd: true });");
    expect(PANEL).toContain("useHubUi.setState({ wantAdd: false });\n    fileInput.current?.click();");
    // Closing forgets a pending Add.
    expect(LIBHUB).toContain("{ open: false, sending: null, selected: [], wantAdd: false }");
  });
  it("Alt+H opens it, guarded in text fields; the palette finds it by its old name too", () => {
    expect(KEYMAP).toMatch(/key: "h",\s*alt: true,\s*id: "hub"/);
    expect(KEYMAP).toContain('"Shared files — files, pictures and snippets every session can use"');
    expect(PALETTE).toContain('label: "Shared files…"');
    expect(PALETTE).toMatch(/keywords: "hub files share/);
  });
  it("the Thread and Queue boxes carry the picker, the paperclip and the drop", () => {
    for (const src of [THREAD, QUEUE]) {
      expect(src).toContain("useHubComposer(");
      expect(src).toContain("hubc.onKeyDown(e)");
      expect(src).toContain("onPaste={hubc.onPaste}");
      expect(src).toContain("{hubc.attach}");
      expect(src).toContain("{hubc.popup}");
    }
  });
  it("a rail row takes a dropped item; a terminal paste expands references", () => {
    expect(ROW).toContain("withHubDrop(");
    expect(CLIP).toContain("expandRefs(text)");
    expect(CLIP).toContain("HUB_MIME");
    // The client's code rule is the server's (core/hub.py HANDLE_RE): both prefixes.
    expect(HUBREF).toContain("(?<![A-Za-z0-9_./:@-])(?:file|hub)(?:@[A-Za-z0-9][A-Za-z0-9-]{0,62})?:([A-Za-z0-9]{4,8})(?![A-Za-z0-9_])");
    expect(hubref.REF_RE.test("see file:k7m2 now")).toBe(true);
    expect(hubref.REF_RE.test("see hub:k7m2 now")).toBe(true);
    expect(hubref.REF_RE.test("profile:k7m2")).toBe(false);
  });
  it("the panel: paste while open, never out of a text field; dialogs are in-page", () => {
    expect(PANEL).toContain('document.addEventListener("paste", onPaste)');
    expect(PANEL).toMatch(/if \(\s*editable\(e\.target\)/);
    expect(PANEL + SEND + ATTACH).not.toMatch(/window\.(prompt|confirm|alert)\(/);
  });
  it("every colour is a theme token", () => {
    expect(PANEL_CSS).not.toMatch(/#[0-9a-fA-F]{3,8}\b/);
  });
});

describe("round 4: what the owner sees", () => {
  it("the row's count is only what an agent or your phone added since you looked", () => {
    const items = [
      item({ by: "you", added: 5000 }),
      item({ by: "session", added: 5000 }),
      item({ by: "phone", added: 5000 }),
      item({ by: "session", added: 100 }),
    ];
    expect(hub.newCount(items, 1000)).toBe(2);
    expect(hub.newCount(items, 9000)).toBe(0);
  });
  it("each session in Send to session… says its state", () => {
    expect(hub.stateWord("working")).toBe("Working");
    expect(hub.stateWord("clarify")).toBe("Question");
    expect(hub.stateWord("idle")).toBe("Idle");
    expect(hub.stateWord(undefined)).toBe("Idle");
  });
  it("filters, day headings and a snippet's first line", () => {
    const items = [
      item({ id: "a1a1", kind: "image", by: "you" }),
      item({ id: "b2b2", kind: "text", by: "session", name: "note.txt", preview: "\nSELECT 1;\nmore" }),
      item({ id: "c3c3", kind: "file", textual: true, by: "phone", name: "x.log" }),
    ];
    expect(panel.filterItems(items, "pictures", "").map((i) => i.id)).toEqual(["a1a1"]);
    expect(panel.filterItems(items, "text", "").map((i) => i.id)).toEqual(["b2b2", "c3c3"]);
    expect(panel.filterItems(items, "agents", "").map((i) => i.id)).toEqual(["b2b2"]);
    expect(panel.filterItems(items, "all", "select").map((i) => i.id)).toEqual(["b2b2"]);
    expect(panel.firstLine(items[1])).toBe("SELECT 1;");
    const now = new Date(2026, 10, 22, 12).getTime() / 1000;
    expect(panel.dayLabel(now - 3600, now)).toBe("Today");
    expect(panel.dayLabel(now - 86400, now)).toBe("Yesterday");
  });
  it("the composer's chips come from the codes in the text (either prefix, one item once); ✕ takes one out", () => {
    expect(attach.attachedRefs("see hub:K7M2 and file:zz11, file:k7m2 again; github:abcd")).toEqual(["file:k7m2", "file:zz11"]);
    expect(attach.removeRef("see hub:k7m2 and file:zz11 ok", "file:k7m2")).toBe("see and file:zz11 ok");
    expect(attach.removeRef("see file:k7m2 and file:zz11 ok", "file:zz11")).toBe("see file:k7m2 and ok");
  });
  it("one wording: code, Point the agent at it, Save a copy", () => {
    expect(PANEL).toContain("Copy code");
    expect(SEND).toContain("Point the agent at {them}");
    expect(SEND).toContain('"Save a copy in its project folder"');
    expect(SEND).toContain('"Save copies in its project folder"');
    expect(SEND).toContain('"Save copy"');
    expect(SEND).toContain("Add a message (optional)");
    expect(SEND).toContain("Save as (inside {name}'s folder)");
    expect(SEND).toContain("Tell {name} it's at");
    expect(PANEL + SEND).not.toMatch(/Copy reference/);
  });
  it("notices sit at the top; the send step retires a stale one", () => {
    expect(PANEL_CSS).toMatch(/#hub-notice \{[^}]*top: calc\(var\(--topbar-h/);
    hub.say("Added x — Put in api's prompt", { actions: [{ label: "Put in api's prompt", run: () => {} }] });
    expect(hub.useHubUi.getState().notice).not.toBeNull();
    hub.useHubUi.getState().openSend(item());
    expect(hub.useHubUi.getState().notice).toBeNull();
  });
  it("a terminal paste or drop of a code goes through the dialog-safe send", () => {
    expect(CLIP).toContain("typeIntoAgent(session, text)");
    expect(HUBREF).toContain("submit: false, dialog_safe: true");
  });
});


describe("your other devices' shared files", () => {
  const devices = (over: Partial<import("../lib/hub").HubDevice> = {}): import("../lib/hub").HubDevices => ({
    self: "laptop",
    devices: [
      {
        device: "mac-mini",
        name: "Mac mini",
        state: "ok",
        note: "",
        dropped: 0,
        truncated: false,
        items: [
          { id: "p3qx", ref: "file:p3qx", name: "trace.log", kind: "file", mime: "", size: 10, sha256: "a", by: "you", by_name: "", added: 2000, note: "", textual: true, preview: "boom", secret: false, too_big: false, copy: null },
          { id: "k7m2", ref: "file@mac-mini:k7m2", name: "shot.png", kind: "image", mime: "image/png", size: 4 << 20, sha256: "b", by: "you", by_name: "", added: 1500, note: "", textual: false, preview: "", secret: false, too_big: false, copy: null },
          { id: "zz22", ref: "file:zz22", name: "here.txt", kind: "text", mime: "", size: 3, sha256: "c", by: "you", by_name: "", added: 1200, note: "", textual: true, preview: "x", secret: false, too_big: false, copy: "zz22" },
        ],
        ...over,
      },
      { device: "win-rig", name: "Win rig", state: "offline", note: "Win rig is offline — its shared files show here when it is back", items: [], dropped: 0, truncated: false },
      { device: "old-box", name: "Old box", state: "old", note: "Old box runs an older MindFlock — update it to see its shared files here", items: [], dropped: 0, truncated: false },
    ],
  });

  it("their items are rows of this list, by device code, and one already here is left out", () => {
    const rows = hub.remoteItems(devices());
    expect(rows.map((r) => r.id)).toEqual(["file@mac-mini:p3qx", "file@mac-mini:k7m2"]);
    expect(rows[1].ref).toBe("file@mac-mini:k7m2");
    expect(rows[0].remote).toMatchObject({ device: "mac-mini", deviceName: "Mac mini", id: "p3qx" });
  });

  it("a device that can't be asked is named, never an empty list", () => {
    expect(hub.deviceNotes(devices()).map((d) => d.state)).toEqual(["offline", "old"]);
    expect(hub.deviceNotes(devices())[1].note).toContain("update");
  });

  it("a big remote picture is never pulled to draw the list; a small one is its checked thumbnail", () => {
    const [, big] = hub.remoteItems(devices());
    expect(hub.hasThumb(big)).toBe(false);
    const small = { ...big, size: 1000 };
    expect(hub.hasThumb(small)).toBe(true);
    expect(hub.rawUrl(small)).toBe("/api/hub/devices/mac-mini/items/k7m2/thumb");
  });

  it("the Other devices filter and find by device name", () => {
    const rows = [...hub.remoteItems(devices()), item(), item({ id: "c0py", ref: "file:c0py", origin: { device: "mac-mini", name: "Mac mini", id: "zz22" } })];
    expect(panel.filterItems(rows, "devices", "").map((r) => r.id)).toEqual(["file@mac-mini:p3qx", "file@mac-mini:k7m2", "c0py"]);
    expect(panel.filterItems(rows, "all", "mac mini").length).toBe(3);
    expect(hub.whoLabel(rows[3])).toBe("from Mac mini");
  });

  it("a code finds its item: own first, then a fetched copy, then the one device offering it", () => {
    const remote = hub.remoteItems(devices());
    const local = [item(), item({ id: "c0py", ref: "file:c0py", origin: { device: "mac-mini", name: "Mac mini", id: "zz22" } })];
    expect(hub.findByRef("file:k7m2", local, remote)?.id).toBe("k7m2");
    expect(hub.findByRef("file@mac-mini:k7m2", local, remote)?.id).toBe("file@mac-mini:k7m2");
    expect(hub.findByRef("file:zz22", local, remote)?.id).toBe("c0py");
    expect(hub.findByRef("file:p3qx", local, remote)?.remote?.deviceName).toBe("Mac mini");
    expect(hub.findByRef("file:nope", local, remote)).toBeUndefined();
  });

  it("the composer's chips keep a device code's device; ✕ takes out only that one", () => {
    expect(attach.attachedRefs("a file@Mac-Mini:K7M2 b file:k7m2")).toEqual(["file@mac-mini:k7m2", "file:k7m2"]);
    expect(attach.removeRef("a file@mac-mini:k7m2 b file:k7m2 c", "file@mac-mini:k7m2")).toBe("a b file:k7m2 c");
    expect(attach.removeRef("a file@mac-mini:k7m2 b file:k7m2 c", "file:k7m2")).toBe("a file@mac-mini:k7m2 b c");
    expect(hubref.REF_RE.test("see file@mac-mini:k7m2")).toBe(true);
  });

  it("using one fetches it first; pin and remove stay this device's; the switch is in the footer", () => {
    expect(PANEL).toContain("useThen(selItems, (here) => useHubUi.getState().openSend(here))");
    expect(PANEL).toContain("pinItems(selLocal.filter((i) => !i.pinned), true)");
    expect(PANEL).toContain("Save to this device");
    expect(PANEL).toContain("Keep on this device only");
    expect(PANEL).toContain("Show these on my other devices");
    expect(LIBHUB).toContain('json: { action: "fetch", ids: rem.map((i) => i.id) }');
    expect(LIBHUB).toContain("Bring it anyway");
  });
});
