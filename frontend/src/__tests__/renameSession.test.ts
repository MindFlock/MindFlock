/** A rename renames the SESSION (POST /api/instances/{title}/rename), and the
 * browser's per-title state follows it (lib/renameSession moveSessionTitle):
 *
 *  - the inventory of title-keyed localStorage keys it moves is pinned, so a
 *    reviewer sees it and a new key has to be added on purpose;
 *  - every key and store field moves old → new; the old rename (alias) of
 *    either title is dropped; a second call (the event after this browser's
 *    own answer) changes nothing;
 *  - renameSession posts {to}, moves the state on 200, does nothing on
 *    `unchanged`, toasts the server's sentence on a 4xx, and just drops an
 *    alias when the name typed is the session's own;
 *  - lib/aliasSync: the boot seed's `renamed` map moves each session and
 *    adopts the remaining aliases; a real `session.renamed` moves the state
 *    (a replay only when the snapshot agrees), the legacy shape re-reads the
 *    map;
 *  - every rename surface goes through renameSession, never setAlias. */

/// <reference types="vite/client" />
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Instance } from "../api/types";
import { code } from "./sourceText";

const storage = new Map<string, string>();
vi.stubGlobal("localStorage", {
  getItem: (k: string) => (storage.has(k) ? storage.get(k)! : null),
  setItem: (k: string, v: string) => void storage.set(k, String(v)),
  removeItem: (k: string) => void storage.delete(k),
  clear: () => storage.clear(),
});

const toast = vi.fn();
const errorPop = vi.fn();
const instApi = vi.fn();
const api = vi.fn();
vi.mock("../lib/toast", () => ({ toast: (...a: unknown[]) => toast(...a) }));
vi.mock("../lib/errorPop", () => ({
  errorPop: (...a: unknown[]) => errorPop(...a),
  confirmPop: vi.fn(),
}));
vi.mock("../api/client", async (orig) => ({
  ...((await orig()) as object),
  instApi: (...a: unknown[]) => instApi(...a),
  api: (...a: unknown[]) => api(...a),
}));

const { ApiError } = await import("../api/client");
const { useUi } = await import("../state/store");
const { queryClient } = await import("../state/queries");
const { TITLE_KEYED_STORAGE, moveSessionTitle, onSessionRenamed, renameMapKey, renameSession } = await import(
  "../lib/renameSession"
);
const { adoptSeedAnswer, onSessionRenamedEvent, realRename } = await import("../lib/aliasSync");
const { codemapSeenAt, markCodemapSeen } = await import("../lib/codemapSeen");
const { loadNodePos } = await import("../lib/skyNodePos");
const { SKY_LAYOUT_KEY } = await import("../lib/skyLayout");

const V = "\u0000verify:";
const json = (k: string) => JSON.parse(storage.get(k) || "null");
const rows = (titles: string[], extra: Partial<Instance> = {}) =>
  queryClient.setQueryData<Instance[]>(
    ["instances"],
    titles.map((t) => ({ title: t, ...extra }) as unknown as Instance)
  );

function seed() {
  storage.clear();
  useUi.setState({
    focused: "old",
    order: ["a", "old", "\u0000mindflock-logs", "b"],
    mru: ["old", "a"],
    gridRows: [["old", "a"], ["b"]],
    railOrder: ["a", "old", "b"],
    hidden: new Set(["old"]),
    collapsedFamilies: new Set(["old"]),
    verifyPanes: [],
    bulkSelected: new Set(["old", "a"]),
    bulkAnchor: "old",
    aliases: { old: "Login fix", a: "Alpha" },
    lastTab: { old: "diff", a: "agent" },
    threadLastSeen: { old: 123 },
    openDialog: "commit",
    dialogTarget: "old",
    threadComposeTarget: { title: "old", to: "old", seq: 1 },
    mapFocus: { title: "old", seq: 1 },
    diffFocus: { title: "old", path: "x.py", seq: 1 },
    fastTrackMenu: { title: "old" },
  } as never);
  storage.set(SKY_LAYOUT_KEY, JSON.stringify({ a: { x: 1, y: 1, pinned: true }, old: { x: 5, y: 6, pinned: true } }));
  storage.set("mf_sky_pos", JSON.stringify({ old: { x: 5, y: 6 } }));
  storage.set("mf_sky_node_pos", JSON.stringify({ old: { n1: { x: 2, y: 3 } } }));
  markCodemapSeen("old", 1000);
  rows(["a", "old", "b"]);
  queryClient.setQueryData<Instance[]>(["instances"], (p) => [...(p || []), { title: "kid", parent: "old" } as unknown as Instance]);
}

beforeEach(() => {
  toast.mockClear();
  errorPop.mockClear();
  instApi.mockReset();
  api.mockReset();
  seed();
});

describe("the title-keyed state inventory", () => {
  it("names every title-keyed localStorage key moveSessionTitle moves", () => {
    expect(Object.keys(TITLE_KEYED_STORAGE).sort()).toEqual(
      [
        "cs_gridrows",
        "cs_lasttab",
        "cs_mru",
        "cs_order",
        "mf_aliases",
        "mf_codemap_seen",
        "mf_familycollapse",
        "mf_hidden",
        "mf_sky_layout",
        "mf_sky_node_pos",
        "mf_sky_pos",
        "mf_thread_seen",
      ].sort()
    );
  });
});

describe("moveSessionTitle", () => {
  it("moves every store field and stored key from the old title to the new one", () => {
    moveSessionTitle("old", "Login fix");
    const s = useUi.getState();
    expect(s.focused).toBe("Login fix");
    expect(s.order).toEqual(["a", "Login fix", "\u0000mindflock-logs", "b"]);
    expect(s.mru).toEqual(["Login fix", "a"]);
    expect(s.gridRows).toEqual([["Login fix", "a"], ["b"]]);
    expect(s.railOrder).toEqual(["a", "Login fix", "b"]);
    expect([...s.hidden]).toEqual(["Login fix"]);
    expect([...s.collapsedFamilies]).toEqual(["Login fix"]);
    expect([...s.bulkSelected].sort()).toEqual(["Login fix", "a"]);
    expect(s.bulkAnchor).toBe("Login fix");
    expect(s.aliases).toEqual({ a: "Alpha" });
    expect(s.lastTab).toEqual({ a: "agent", "Login fix": "diff" });
    expect(s.threadLastSeen).toEqual({ "Login fix": 123 });
    expect(s.dialogTarget).toBe("Login fix");
    expect(s.threadComposeTarget).toMatchObject({ title: "Login fix", to: "Login fix" });
    expect(s.mapFocus?.title).toBe("Login fix");
    expect(s.diffFocus).toMatchObject({ title: "Login fix", path: "x.py" });
    expect(s.fastTrackMenu?.title).toBe("Login fix");

    expect(json("cs_order")).toEqual(s.order);
    expect(json("cs_mru")).toEqual(s.mru);
    expect(json("cs_gridrows")).toEqual(s.gridRows);
    expect(json("mf_hidden")).toEqual(["Login fix"]);
    expect(json("mf_familycollapse")).toEqual(["Login fix"]);
    expect(json("mf_aliases")).toEqual({ a: "Alpha" });
    expect(json("cs_lasttab")).toEqual(s.lastTab);
    expect(json("mf_thread_seen")).toEqual({ "Login fix": 123 });
    expect(Object.keys(json(SKY_LAYOUT_KEY))).toEqual(["a", "Login fix"]);
    expect(json(SKY_LAYOUT_KEY)["Login fix"]).toEqual({ x: 5, y: 6, pinned: true });
    expect(json("mf_sky_pos")).toEqual({ "Login fix": { x: 5, y: 6 } });
    expect(loadNodePos("Login fix")).toEqual({ n1: { x: 2, y: 3 } });
    expect(loadNodePos("old")).toEqual({});
    expect(codemapSeenAt("Login fix")).toBe(1000);
    expect(codemapSeenAt("old")).toBe(0);

    // The snapshot's row (and a child's parent) are under the new name now.
    const live = queryClient.getQueryData<Instance[]>(["instances"])!;
    expect(live.map((r) => r.title)).toEqual(["a", "Login fix", "b", "kid"]);
    expect(live.find((r) => r.title === "kid")?.parent).toBe("Login fix");
  });

  it("is idempotent: a second call (the event after this browser's own answer) changes nothing", () => {
    moveSessionTitle("old", "new");
    const before = useUi.getState();
    const stored = new Map(storage);
    moveSessionTitle("old", "new");
    const after = useUi.getState();
    for (const k of ["order", "mru", "gridRows", "hidden", "aliases", "lastTab", "threadLastSeen", "focused"] as const)
      expect(after[k]).toBe(before[k]);
    expect(new Map(storage)).toEqual(stored);
  });

  it("moves a verify pane's window token too, and never duplicates a title already there", () => {
    useUi.setState({ mru: [V + "old", "new", "old"], verifyPanes: ["old"] } as never);
    moveSessionTitle("old", "new");
    expect(useUi.getState().mru).toEqual([V + "new", "new"]);
    expect(useUi.getState().verifyPanes).toEqual(["new"]);
  });

  it("does nothing for an empty or unchanged pair", () => {
    const before = useUi.getState();
    moveSessionTitle("", "x");
    moveSessionTitle("old", "old");
    expect(useUi.getState()).toBe(before);
  });

  it("tells the holders outside the store (drafts, the Sky's layout in hand)", () => {
    const drafts = new Map([["old", "half a sentence"]]);
    const to = new Map([["a", "old"]]);
    const off = onSessionRenamed((f, t) => {
      renameMapKey(drafts, f, t);
      renameMapKey(to, f, t, { values: true });
    });
    moveSessionTitle("old", "new");
    off();
    expect([...drafts]).toEqual([["new", "half a sentence"]]);
    expect([...to]).toEqual([["a", "new"]]);
  });
});

describe("renameSession", () => {
  it("posts {to}, then moves the state and toasts the new name", async () => {
    instApi.mockResolvedValueOnce({ title: "Login fix", from: "old" });
    expect(await renameSession("old", "  Login fix ")).toBe("Login fix");
    expect(instApi).toHaveBeenCalledWith("old", "/rename", { json: { to: "Login fix" } });
    expect(useUi.getState().focused).toBe("Login fix");
    expect(toast).toHaveBeenCalledWith("Renamed to “Login fix”");
  });

  it("moves to the title the server answered with", async () => {
    instApi.mockResolvedValueOnce({ title: "login-fix", from: "old" });
    expect(await renameSession("old", "login fix")).toBe("login-fix");
    expect(useUi.getState().order).toContain("login-fix");
  });

  it("does nothing when the server says unchanged, or for a blank name", async () => {
    useUi.setState({ aliases: {} } as never);
    instApi.mockResolvedValueOnce({ title: "old", from: "old", unchanged: true });
    expect(await renameSession("old", "Other")).toBe("");
    expect(await renameSession("old", "   ")).toBe("");
    expect(await renameSession("old", "old")).toBe("");
    expect(useUi.getState().focused).toBe("old");
    expect(instApi).toHaveBeenCalledTimes(1);
  });

  it("a refusal is the server's own sentence, and nothing moves", async () => {
    instApi.mockRejectedValueOnce(
      new ApiError(409, "Another session is already named “a”", { error: "Another session is already named “a”", session: "a" })
    );
    expect(await renameSession("old", "a")).toBe("");
    expect(toast).toHaveBeenCalledWith("Another session is already named “a”");
    expect(useUi.getState().focused).toBe("old");
    instApi.mockRejectedValueOnce(new ApiError(400, "That name can't be a session's name", null));
    await renameSession("old", "bad/name");
    expect(toast).toHaveBeenLastCalledWith("That name can't be a session's name");
    instApi.mockRejectedValueOnce(new TypeError("Failed to fetch"));
    await renameSession("old", "x");
    expect(errorPop).toHaveBeenCalledWith("Rename failed", "Failed to fetch");
  });

  it("typing the session's own name (or its plain label) back drops an old alias, without a rename", async () => {
    api.mockResolvedValue({});
    expect(await renameSession("old", "old")).toBe("old");
    expect(useUi.getState().aliases.old).toBeUndefined();
    expect(instApi).not.toHaveBeenCalled();
    useUi.setState({ aliases: { old: "Login fix" } } as never);
    expect(await renameSession("old", "(tix) sc-1", { plain: "(tix) sc-1" })).toBe("old");
    expect(useUi.getState().aliases.old).toBeUndefined();
  });
});

describe("aliasSync: the boot migration and session.renamed", () => {
  const env = (data: Record<string, unknown>, session = "old") =>
    ({ seq: 1, event: "session.renamed", session, old: session, new: String(data.to || data.name || ""), ts: 0, data }) as never;

  it("the seed's `renamed` map moves each session, then the remaining aliases are adopted", () => {
    adoptSeedAnswer({ aliases: { a: "Alpha", b: "Taken name" }, renamed: { old: "Login fix" } });
    const s = useUi.getState();
    expect(s.focused).toBe("Login fix");
    expect(s.order).toContain("Login fix");
    expect(s.aliases).toEqual({ a: "Alpha", b: "Taken name" });
    expect(json("mf_aliases")).toEqual({ a: "Alpha", b: "Taken name" });
  });

  it("an older server's answer (aliases only) is adopted as before", () => {
    adoptSeedAnswer({ aliases: { a: "A2" } });
    expect(useUi.getState().aliases).toEqual({ a: "A2" });
    expect(useUi.getState().focused).toBe("old");
  });

  it("tells a real rename from the legacy alias shape", () => {
    expect(realRename(env({ from: "old", to: "new", name: "new", title: "new" }))).toEqual({ from: "old", to: "new" });
    expect(realRename(env({ name: "Login fix" }))).toBeNull();
  });

  it("a real rename moves the state; the legacy shape re-reads the map", () => {
    onSessionRenamedEvent(env({ from: "old", to: "new", name: "new", title: "new" }));
    expect(useUi.getState().focused).toBe("new");
    expect(api).not.toHaveBeenCalled();
    api.mockResolvedValueOnce({ aliases: { a: "Z" } });
    onSessionRenamedEvent(env({ name: "Z" }, "a"));
    expect(api).toHaveBeenCalledWith("/api/aliases");
  });

  it("a replayed real rename applies only when the snapshot agrees it happened", () => {
    // The old title is still live (reused by a new session): leave it alone.
    onSessionRenamedEvent(env({ from: "old", to: "new" }), true);
    expect(useUi.getState().focused).toBe("old");
    rows(["a", "new", "b"]);
    onSessionRenamedEvent(env({ from: "old", to: "new" }), true);
    expect(useUi.getState().focused).toBe("new");
    // A replayed legacy one never re-reads.
    onSessionRenamedEvent(env({ name: "Z" }, "a"), true);
    expect(api).not.toHaveBeenCalled();
  });
});

describe("every rename surface renames the session", () => {
  for (const f of ["components/dialogs/RenameDialog.tsx", "components/sidebar/SidebarRow.tsx", "components/flights/SkyLine.tsx"]) {
    it(f, () => {
      const src = code(f);
      expect(src).toContain("renameSession(");
      expect(src).not.toContain("setAlias(");
      expect(src).not.toMatch(/window\.(prompt|alert|confirm)\(/);
    });
  }
});
