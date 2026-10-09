/** The live half of prefs (lib/prefsSync.ts): what a page load and a re-pull
 * actually send and adopt, against a mocked server. These pin the ways the
 * pull used to lose a person's data or leave the page stale: a dirty write
 * overwritten by the server's older copy, events missed across a reconnect,
 * and a cleared accent that never reached a device whose `ui` group held
 * nothing else. */

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";

const calls: Array<{ path: string; json?: unknown }> = [];
let routes: Record<string, () => unknown> = {};

vi.mock("../api/client", () => ({
  api: vi.fn(async (path: string, opts?: { json?: unknown; method?: string }) => {
    const method = opts?.method || (opts && "json" in opts ? "POST" : "GET");
    calls.push({ path: method + " " + path, json: opts?.json });
    const r = routes[method + " " + path];
    if (!r) throw new Error("no route " + method + " " + path);
    return r();
  }),
}));
const adoptAccent = vi.fn();
let accent = "";
vi.mock("../components/settings/screens/Appearance", () => ({
  adoptAccent: (n: string) => adoptAccent(n),
  storedAccent: () => accent,
}));
vi.mock("../state/queries", () => ({
  queryClient: { invalidateQueries: vi.fn(async () => {}) },
  refreshConfig: vi.fn(async () => {}),
}));
vi.mock("../state/store", () => ({ useUi: { setState: vi.fn() } }));
vi.mock("../lib/keymap", () => ({ reloadKeymap: vi.fn() }));
const toasts: string[] = [];
vi.mock("../lib/toast", () => ({ toast: (m: string) => void toasts.push(m) }));

function memKV(init: Record<string, string> = {}) {
  const data: Record<string, string> = { ...init };
  return {
    data,
    getItem: (k: string) => (k in data ? data[k] : null),
    setItem: (k: string, v: string) => {
      data[k] = String(v);
    },
    removeItem: (k: string) => {
      delete data[k];
    },
  };
}

type Handler = (env: { ts?: number }) => void;
function fakeEvents(replay: boolean) {
  const subs: Record<string, Handler[]> = {};
  const status: Array<(s: string) => void> = [];
  return {
    subs,
    status,
    subscribe(name: string, cb: Handler) {
      (subs[name] ||= []).push(cb);
      return () => {};
    },
    onStatus(cb: (s: string) => void) {
      status.push(cb);
      return () => {};
    },
    isReplay: () => replay,
  };
}

const g = globalThis as Record<string, unknown>;
const saved: Record<string, unknown> = {};

beforeEach(() => {
  vi.resetModules();
  calls.length = 0;
  toasts.length = 0;
  adoptAccent.mockClear();
  accent = "";
  routes = {};
  for (const k of ["localStorage", "window", "document"]) saved[k] = g[k];
  g.document = {
    dispatchEvent: () => true,
    addEventListener: () => {},
    visibilityState: "visible",
    documentElement: { classList: { toggle: () => {} } },
  };
});
afterEach(() => {
  vi.useRealTimers();
  for (const k of Object.keys(saved)) {
    if (saved[k] === undefined) delete g[k];
    else g[k] = saved[k];
  }
});

describe("pullPrefs", () => {
  it("uploads a dirty field FIRST and never overwrites it with the server's older copy", async () => {
    const kv = memKV({
      mf_prefs_seeded: "1",
      "mindflock.prompt_presets": JSON.stringify([{ name: "Mine", prompt: "x" }]),
      mf_prefs_dirty: '["prompt_presets"]',
    });
    g.localStorage = kv;
    routes = {
      "GET /api/prefs": () => ({ prompt_presets: [] }),
      "POST /api/prefs": () => ({ ok: true }),
      "GET /api/settings": () => ({ settings: {} }),
    };
    const { pullPrefs } = await import("../lib/prefsSync");
    await pullPrefs();
    expect(calls.map((c) => c.path)).toEqual(["GET /api/prefs", "POST /api/prefs", "GET /api/settings"]);
    expect(calls[1].json).toEqual({ prompt_presets: [{ name: "Mine", prompt: "x" }] });
    expect(JSON.parse(kv.data["mindflock.prompt_presets"])).toEqual([{ name: "Mine", prompt: "x" }]);
    expect(kv.data.mf_prefs_dirty).toBeUndefined(); // confirmed → no longer dirty
  });

  it("a failed upload keeps the field dirty and the value in place", async () => {
    const kv = memKV({ mf_prefs_seeded: "1", cs_theme: "light", mf_prefs_dirty: '["theme"]' });
    g.localStorage = kv;
    routes = { "GET /api/prefs": () => ({ theme: "dark" }) }; // POST fails
    const { pullPrefs } = await import("../lib/prefsSync");
    await pullPrefs();
    expect(kv.data.cs_theme).toBe("light");
    expect(kv.data.mf_prefs_dirty).toBe('["theme"]');
  });

  it("an unseeded browser's saved prompts are merged with the server's, not replaced", async () => {
    const kv = memKV({ "mindflock.prompt_presets": JSON.stringify([{ name: "A", prompt: "a" }]) });
    g.localStorage = kv;
    routes = {
      "GET /api/prefs": () => ({ prompt_presets: [{ name: "B", prompt: "b" }] }),
      "POST /api/prefs": () => ({ ok: true }),
      "GET /api/settings": () => ({ settings: {} }),
    };
    const { pullPrefs } = await import("../lib/prefsSync");
    await pullPrefs();
    const union = [
      { name: "B", prompt: "b" },
      { name: "A", prompt: "a" },
    ];
    expect(calls[1]).toEqual({ path: "POST /api/prefs", json: { prompt_presets: union } });
    expect(JSON.parse(kv.data["mindflock.prompt_presets"])).toEqual(union);
  });

  it("folds case-variant saved prompts (keeping the newer) and says so", async () => {
    const kv = memKV({
      mf_prefs_seeded: "1",
      "mindflock.prompt_presets": JSON.stringify([
        { name: "Deploy", prompt: "old" },
        { name: "deploy", prompt: "new" },
      ]),
    });
    g.localStorage = kv;
    routes = {
      "GET /api/prefs": () => ({ prompt_presets: [{ name: "Deploy", prompt: "old" }] }),
      "POST /api/prefs": () => ({ ok: true }),
      "GET /api/settings": () => ({ settings: {} }),
    };
    const { pullPrefs } = await import("../lib/prefsSync");
    await pullPrefs();
    expect(toasts.join(" ")).toContain("“Deploy”");
    // The newer one won locally AND went to the server, not the other way round.
    expect(JSON.parse(kv.data["mindflock.prompt_presets"])).toEqual([{ name: "deploy", prompt: "new" }]);
    expect(calls.find((c) => c.path === "POST /api/prefs")?.json).toEqual({
      prompt_presets: [{ name: "deploy", prompt: "new" }],
    });
  });

  it("a settings document with no ui group clears this device's accent (a cleared accent propagates)", async () => {
    g.localStorage = memKV({ mf_prefs_seeded: "1" });
    accent = "violet";
    routes = {
      "GET /api/prefs": () => ({}),
      "GET /api/settings": () => ({ settings: { general: {} } }),
    };
    const { pullPrefs } = await import("../lib/prefsSync");
    await pullPrefs();
    expect(adoptAccent).toHaveBeenCalledWith("");
  });

  it("an unseeded browser's accent is uploaded even when the server has no ui group", async () => {
    g.localStorage = memKV({});
    accent = "cardinal";
    routes = {
      "GET /api/prefs": () => ({}),
      "GET /api/settings": () => ({ settings: {} }),
      "POST /api/settings": () => ({ ok: true }),
    };
    const { pullPrefs } = await import("../lib/prefsSync");
    await pullPrefs();
    expect(calls.find((c) => c.path === "POST /api/settings")?.json).toEqual({ ui: { accent: "cardinal" } });
    expect(adoptAccent).not.toHaveBeenCalled();
  });
});

describe("pullPrefs while a pull is in flight", () => {
  it("a local write confirmed DURING a pull isn't reverted by the pull's older snapshot", async () => {
    const kv = memKV({ mf_prefs_seeded: "1", cs_theme: "dark" });
    g.localStorage = kv;
    let server: Record<string, unknown> = { theme: "dark" };
    let release!: () => void;
    const gate = new Promise<void>((r) => (release = r));
    let first = true;
    routes = {
      "GET /api/prefs": () => {
        const snap = { ...server };
        if (!first) return snap;
        first = false;
        return gate.then(() => snap);
      },
      "POST /api/prefs": () => ({ ok: true }),
      "GET /api/settings": () => ({ settings: {} }),
    };
    const { pullPrefs } = await import("../lib/prefsSync");
    const prefs = await import("../lib/prefs");
    prefs.setPrefSender(async (body) => {
      server = { ...server, ...(body as object) };
    });
    const p = pullPrefs(); // e.g. a reconnect's re-pull: its GET reads "dark"
    await Promise.resolve();
    kv.setItem("cs_theme", "light"); // the person toggles the theme meanwhile
    prefs.notePrefWrite("cs_theme", kv);
    await prefs.flushPrefWrites(kv); // the POST lands; the field is no longer dirty
    expect(server.theme).toBe("light");
    expect(kv.data.mf_prefs_dirty).toBeUndefined();
    const again = pullPrefs(); // the POST's own settings.synced echo
    release();
    await p;
    expect(kv.data.cs_theme).toBe("light");
    await again;
    expect(kv.data.cs_theme).toBe("light");
  });

  it("a sync event during a pull queues exactly one more pull after it", async () => {
    const kv = memKV({ mf_prefs_seeded: "1", cs_theme: "dark" });
    g.localStorage = kv;
    let server: Record<string, unknown> = { theme: "dark" };
    let release!: () => void;
    const gate = new Promise<void>((r) => (release = r));
    let first = true;
    routes = {
      "GET /api/prefs": () => {
        const snap = { ...server };
        if (!first) return snap;
        first = false;
        return gate.then(() => snap);
      },
      "GET /api/settings": () => ({ settings: {} }),
    };
    const { pullPrefs } = await import("../lib/prefsSync");
    const p = pullPrefs();
    await Promise.resolve();
    server = { theme: "light" }; // another device's change, adopted after the GET read
    const a = pullPrefs(); // its settings.synced re-pull
    const b = pullPrefs(); // …and a reconnect in the same window
    expect(b).toBe(a); // one follow-up, shared
    release();
    await p;
    await a;
    expect(kv.data.cs_theme).toBe("light");
    expect(calls.filter((c) => c.path === "GET /api/prefs").length).toBe(2);
  });
});

describe("installPrefsSync", () => {
  it("the first reconnect re-pulls even when the socket was already up at install", async () => {
    vi.useFakeTimers();
    g.localStorage = memKV({ mf_prefs_seeded: "1" });
    // events.js connects at load, before the bundle's rAF installs this; its
    // onStatus doesn't replay the current state.
    const ev = { ...fakeEvents(false), connected: true };
    g.window = { mindflock: { events: ev }, addEventListener: () => {} };
    routes = { "GET /api/prefs": () => ({}), "GET /api/settings": () => ({ settings: {} }) };
    const { installPrefsSync } = await import("../lib/prefsSync");
    installPrefsSync();
    await vi.advanceTimersByTimeAsync(0);
    const count = () => calls.filter((c) => c.path === "GET /api/prefs").length;
    const base = count();
    ev.status[0]("disconnected"); // laptop slept; same server, backlog overflowed
    ev.status[0]("connected");
    await vi.advanceTimersByTimeAsync(400);
    expect(count()).toBe(base + 1);
  });

  it("a settings.synced REPLAYED after a reconnect still re-pulls (it is exactly what this page missed)", async () => {
    vi.useFakeTimers();
    g.localStorage = memKV({ mf_prefs_seeded: "1" });
    const ev = fakeEvents(true);
    g.window = { mindflock: { events: ev }, addEventListener: () => {} };
    routes = { "GET /api/prefs": () => ({}), "GET /api/settings": () => ({ settings: {} }) };
    const { installPrefsSync } = await import("../lib/prefsSync");
    installPrefsSync();
    await vi.advanceTimersByTimeAsync(0);
    const before = calls.filter((c) => c.path === "GET /api/prefs").length;
    ev.subs["settings.synced"][0]({ ts: 1 });
    await vi.advanceTimersByTimeAsync(400);
    expect(calls.filter((c) => c.path === "GET /api/prefs").length).toBe(before + 1);
  });

  it("re-pulls on every reconnect after the first", async () => {
    vi.useFakeTimers();
    g.localStorage = memKV({ mf_prefs_seeded: "1" });
    const ev = fakeEvents(false);
    g.window = { mindflock: { events: ev }, addEventListener: () => {} };
    routes = { "GET /api/prefs": () => ({}), "GET /api/settings": () => ({ settings: {} }) };
    const { installPrefsSync } = await import("../lib/prefsSync");
    installPrefsSync();
    await vi.advanceTimersByTimeAsync(0);
    const count = () => calls.filter((c) => c.path === "GET /api/prefs").length;
    const base = count();
    ev.status[0]("connected"); // the first connection: the load's own pull covers it
    await vi.advanceTimersByTimeAsync(400);
    expect(count()).toBe(base);
    ev.status[0]("disconnected");
    ev.status[0]("connected");
    await vi.advanceTimersByTimeAsync(400);
    expect(count()).toBe(base + 1);
  });

  it("flushes unsent writes on pagehide", async () => {
    g.localStorage = memKV({ mf_prefs_seeded: "1", cs_theme: "light", mf_prefs_dirty: '["theme"]' });
    const listeners: Record<string, () => void> = {};
    g.window = {
      addEventListener: (n: string, cb: () => void) => {
        listeners[n] = cb;
      },
    };
    routes = { "GET /api/prefs": () => ({ theme: "light" }), "GET /api/settings": () => ({ settings: {} }) };
    const prefs = await import("../lib/prefs");
    const beaconed: unknown[] = [];
    prefs.setPrefBeacon(async (b) => {
      beaconed.push(b);
    });
    const { installPrefsSync } = await import("../lib/prefsSync");
    installPrefsSync();
    // Before the boot pull settles, the page goes away.
    listeners.pagehide();
    expect(beaconed).toEqual([{ theme: "light" }]);
  });
});
