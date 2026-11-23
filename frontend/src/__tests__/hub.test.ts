/** The hub in the app (lib/hub.ts, components/hub): the pure rules — the
 * `hub:` token a composer answers, which items match, the words an item is
 * described with, a pasted screenshot's name — and the wiring pinned against
 * the sources: where the Hub button, the shortcut, the composers, the rail
 * rows and the terminal paste reach it. */

import { describe, expect, it, vi } from "vitest";
import PANEL from "../components/hub/HubPanel.tsx?raw";
import PANEL_CSS from "../components/hub/HubPanel.css?raw";
import SEND from "../components/hub/HubSendDialog.tsx?raw";
import ATTACH from "../components/hub/HubAttach.tsx?raw";
import TOPBAR from "../components/TopBar.tsx?raw";
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
const { hubTargets } = await import("../components/hub/HubSendDialog");
const panel = await import("../components/hub/HubPanel");
const attach = await import("../components/hub/HubAttach");

const item = (over: Partial<import("../lib/hub").HubItem> = {}) =>
  ({
    id: "k7m2",
    ref: "hub:k7m2",
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

describe("the hub: token a composer answers", () => {
  it("opens on hub: at a word start, with what follows as the query", () => {
    expect(hub.refTokenAt("look at hub:", 12)).toEqual({ start: 8, query: "" });
    expect(hub.refTokenAt("hub:k7", 6)).toEqual({ start: 0, query: "k7" });
    expect(hub.refTokenAt("see (hub:log", 12)).toEqual({ start: 5, query: "log" });
  });
  it("never inside a word or a path, nor after the caret moved on", () => {
    expect(hub.refTokenAt("github:", 7)).toBeNull();
    expect(hub.refTokenAt("a/hub:", 6)).toBeNull();
    expect(hub.refTokenAt("hub:k7m2  and more", 18)).toBeNull();
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
    expect(SEND).toContain("freePath(title, item, path.trim())");
  });
});

describe("wiring", () => {
  it("the Hub button sits in the top bar with a calm count, never for a guest", () => {
    expect(TOPBAR).toContain('id="hub-btn"');
    expect(TOPBAR).toContain('data-fold="hub"');
    expect(TOPBAR).toContain('className="tb-count is-new"');
    expect(TOPBAR).toContain("newCount(hubItems || [])");
    expect(TOPBAR).toContain('"Hub — files and text every session can use"');
    expect(TOPBAR).toContain("{!guest && (");
  });
  it("Alt+H opens it, guarded in text fields", () => {
    expect(KEYMAP).toMatch(/key: "h",\s*alt: true,\s*id: "hub"/);
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
    // The client's reference rule is the server's (core/hub.py HANDLE_RE).
    expect(HUBREF).toContain("(?<![A-Za-z0-9_./:@-])hub:([A-Za-z0-9]{4,8})(?![A-Za-z0-9_])");
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
  it("the top-bar count is only what an agent or your phone added since you looked", () => {
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
  it("the composer's chips come from the codes in the text; ✕ takes one out", () => {
    expect(attach.attachedRefs("see hub:K7M2 and hub:zz11, hub:k7m2 again; github:abcd")).toEqual(["hub:k7m2", "hub:zz11"]);
    expect(attach.removeRef("see hub:k7m2 and hub:zz11 ok", "hub:k7m2")).toBe("see and hub:zz11 ok");
  });
  it("one wording: code, Point the agent at it, Save a copy", () => {
    expect(PANEL).toContain("Copy code");
    expect(SEND).toContain("Point the agent at it");
    expect(SEND).toContain("Save a copy in its project folder");
    expect(SEND).toContain('"Save copy"');
    expect(SEND).toContain("Add a message (optional)");
    expect(SEND).toContain("Save as (inside {name}'s folder)");
    expect(SEND).toContain("Tell {name} it's at");
    expect(PANEL + SEND).not.toMatch(/Copy reference/);
  });
  it("hub notices sit at the top; the Send dialog retires a stale one", () => {
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
