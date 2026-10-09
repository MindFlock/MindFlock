/** Prefs that follow the person (lib/prefs.ts): the localStorage ↔ prefs
 * mapping, the page-load reconcile, and the debounced write-back.
 *
 * The reconcile is where this can quietly destroy someone's setup, so most of
 * these pin its two dangerous edges: a browser's own keymap must reach a
 * server that never heard of it (first load), and a value cleared on one
 * browser must not be re-uploaded by another one that still has it (every
 * load after). */

import { describe, it, expect, afterEach, beforeEach, vi } from "vitest";
import {
  DIRTY_KEY,
  PREF_DEFAULTS,
  PREF_MAP,
  dirtyFields,
  flushPrefWrites,
  flushPrefsOnHide,
  mergePresets,
  normalizePresets,
  isUnset,
  lsKeyFor,
  notePrefWrite,
  pendingFields,
  planBoot,
  prefFieldFor,
  readAllLocal,
  readLocal,
  setPrefBeacon,
  setPrefSender,
  withoutEcho,
  writeLocal,
  type KV,
  type Prefs,
} from "../lib/prefs";

function memKV(init: Record<string, string> = {}): KV & { data: Record<string, string> } {
  const data: Record<string, string> = { ...init };
  return {
    data,
    getItem: (k) => (k in data ? data[k] : null),
    setItem: (k, v) => {
      data[k] = String(v);
    },
    removeItem: (k) => {
      delete data[k];
    },
  };
}

describe("PREF_MAP", () => {
  it("maps exactly the spec's thirteen keys, one field each", () => {
    expect(Object.fromEntries(PREF_MAP.map((p) => [p.ls, p.field]))).toEqual({
      mf_keymap: "keymap",
      "mindflock.prompt_presets": "prompt_presets",
      cs_theme: "theme",
      cs_diffmode: "diff_mode",
      mf_diffbase: "diff_base",
      mf_hiddenbars: "hidden_bars",
      mf_barorder: "bar_order",
      mf_reduce_motion: "reduce_motion",
      mf_break_on: "break_on",
      mf_break_every: "break_every",
      mf_idle_flock: "idle_flock",
      mf_idle_after: "idle_after",
      mf_hints: "hints",
    });
    // Every prefs field is mapped, and nothing else is.
    expect(PREF_MAP.map((p) => p.field).sort()).toEqual(Object.keys(PREF_DEFAULTS).sort());
  });

  it("looks up both ways, and per-browser keys are not prefs", () => {
    expect(prefFieldFor("mf_keymap")).toBe("keymap");
    expect(lsKeyFor("theme")).toBe("cs_theme");
    for (const k of ["cs_order", "mf_aliases", "cs_sidebar", "mf_hints_seen", "cs_accent", "mf_prefs_seeded"])
      expect(prefFieldFor(k)).toBeNull();
  });
});

describe("readLocal / writeLocal", () => {
  it("parses JSON keys and keeps raw string keys raw (index.html reads cs_theme bare)", () => {
    const kv = memKV({
      mf_keymap: JSON.stringify({ keys: { palette: [{ key: "j", mod: true }] }, chords: {} }),
      cs_theme: "light",
      mf_break_every: "45",
      mf_hints: "false",
      mf_hiddenbars: "[]",
    });
    expect(readLocal("keymap", kv)).toEqual({ keys: { palette: [{ key: "j", mod: true }] }, chords: {} });
    expect(readLocal("theme", kv)).toBe("light");
    expect(readLocal("break_every", kv)).toBe(45);
    expect(readLocal("hints", kv)).toBe(false);
    expect(readLocal("hidden_bars", kv)).toEqual([]);
    expect(readLocal("bar_order", kv)).toBeUndefined();

    writeLocal("theme", "dark", kv);
    expect(kv.data.cs_theme).toBe("dark");
    writeLocal("bar_order", ["usage", "tickets"], kv);
    expect(kv.data.mf_barorder).toBe('["usage","tickets"]');
  });

  it("treats garbage as absent rather than passing it on", () => {
    const kv = memKV({
      cs_theme: "solarized",
      cs_diffmode: "sideways",
      mf_break_every: "not json",
      mf_hints: '"yes"',
      mf_keymap: "[1,2]",
    });
    expect(readAllLocal(kv)).toEqual({});
  });

  it("an unset value removes the key, so the reader falls back to its own default", () => {
    const kv = memKV({ cs_theme: "light", mf_hiddenbars: '["usage"]', mf_barorder: '["a"]' });
    writeLocal("theme", "", kv);
    writeLocal("hidden_bars", null, kv);
    writeLocal("bar_order", [], kv);
    expect(kv.data).toEqual({});
  });

  it("survives storage that throws", () => {
    const bad: KV = {
      getItem: () => {
        throw new Error("denied");
      },
      setItem: () => {
        throw new Error("denied");
      },
      removeItem: () => {
        throw new Error("denied");
      },
    };
    expect(readLocal("theme", bad)).toBeUndefined();
    expect(() => writeLocal("theme", "light", bad)).not.toThrow();
  });
});

describe("isUnset", () => {
  it("a keymap with no rebinds is the defaults however it is spelled", () => {
    expect(isUnset("keymap", {})).toBe(true);
    expect(isUnset("keymap", { keys: {}, chords: {} })).toBe(true);
    expect(isUnset("keymap", { keys: {}, chords: { n: "m" } })).toBe(false);
  });

  it("an empty hidden-bars list is a choice (every bar shown); only null is unset", () => {
    expect(isUnset("hidden_bars", [])).toBe(false);
    expect(isUnset("hidden_bars", null)).toBe(true);
    expect(isUnset("bar_order", [])).toBe(true);
  });

  it("false and 0-ish values are real choices", () => {
    expect(isUnset("hints", false)).toBe(false);
    expect(isUnset("idle_flock", false)).toBe(false);
    expect(isUnset("theme", "")).toBe(true);
  });
});

describe("planBoot", () => {
  const server = (p: Partial<Prefs>): Partial<Prefs> => ({ ...PREF_DEFAULTS, ...p });

  it("first load: uploads what this browser has and the server doesn't", () => {
    const local = { keymap: { keys: { palette: [{ key: "j" }] }, chords: {} }, theme: "light" };
    const plan = planBoot(server({}), local, false);
    expect(plan.upload).toEqual(local);
    expect(plan.apply).toEqual({});
  });

  it("a set server value wins over this browser's, seeded or not", () => {
    const local = { theme: "light", hints: true };
    for (const seeded of [false, true]) {
      const plan = planBoot(server({ theme: "dark", hints: false }), local, seeded);
      expect(plan.apply).toEqual({ theme: "dark", hints: false });
      expect(plan.upload).toEqual({});
    }
  });

  it("adopts a server value this browser never had", () => {
    const presets = [{ name: "Review", prompt: "review it" }];
    const plan = planBoot(server({ prompt_presets: presets }), {}, false);
    expect(plan.apply).toEqual({ prompt_presets: presets });
  });

  it("does nothing when they already agree", () => {
    const plan = planBoot(server({ bar_order: ["a", "b"] }), { bar_order: ["a", "b"] }, true);
    expect(plan).toEqual({ upload: {}, apply: {} });
  });

  it("after seeding, an unset server value clears this browser's (no resurrection)", () => {
    // Prompts deleted in the desktop app; this tab still has them cached.
    const plan = planBoot(server({}), { prompt_presets: [{ name: "old", prompt: "x" }] }, true);
    expect(plan.upload).toEqual({});
    expect(plan.apply).toEqual({ prompt_presets: [] });
  });

  it("a browser holding only defaults uploads nothing", () => {
    const plan = planBoot(server({}), { keymap: { keys: {}, chords: {} }, bar_order: [] }, false);
    expect(plan).toEqual({ upload: {}, apply: {} });
  });

  it("tolerates an older server's partial payload", () => {
    const plan = planBoot({ theme: "light" }, { hints: false }, false);
    expect(plan.apply).toEqual({ theme: "light" });
    expect(plan.upload).toEqual({ hints: false });
  });
});

describe("notePrefWrite → POST /api/prefs", () => {
  let sent: Array<Partial<Prefs>>;
  let prev: (b: Partial<Prefs>) => Promise<unknown>;
  const g = globalThis as Record<string, unknown>;
  let prevLS: unknown;

  beforeEach(() => {
    vi.useFakeTimers();
    sent = [];
    prev = setPrefSender(async (b) => {
      sent.push(b);
    });
    prevLS = g.localStorage;
  });
  afterEach(async () => {
    await flushPrefWrites(memKV());
    setPrefSender(prev);
    vi.useRealTimers();
    if (prevLS === undefined) delete g.localStorage;
    else g.localStorage = prevLS;
  });

  it("batches writes in the 500 ms window into one POST of current values", async () => {
    const kv = memKV({ cs_theme: "light", mf_break_on: "true" });
    g.localStorage = kv;
    notePrefWrite("cs_theme");
    notePrefWrite("mf_break_on");
    notePrefWrite("cs_order"); // not a pref: ignored
    expect(sent).toEqual([]);
    expect([...pendingFields()].sort()).toEqual(["break_on", "theme"]);
    kv.data.cs_theme = "dark"; // the value at send time is what goes
    await vi.advanceTimersByTimeAsync(500);
    expect(sent).toEqual([{ theme: "dark", break_on: true }]);
    expect(pendingFields().size).toBe(0);
  });

  it("a removed key is sent as null (clears the server field)", async () => {
    await flushPrefWrites(memKV());
    notePrefWrite("mf_hiddenbars");
    await flushPrefWrites(memKV());
    expect(sent).toEqual([{ hidden_bars: null }]);
  });

  it("writes made while adopting server values are not echoed back", async () => {
    withoutEcho(() => notePrefWrite("cs_theme"));
    expect(pendingFields().size).toBe(0);
    await vi.advanceTimersByTimeAsync(600);
    expect(sent).toEqual([]);
  });

  it("a failing POST (older server) never throws", async () => {
    setPrefSender(async () => {
      throw new Error("404");
    });
    notePrefWrite("cs_theme");
    await expect(flushPrefWrites(memKV({ cs_theme: "light" }))).resolves.toBeUndefined();
  });
});

describe("prompt presets: one per name, ignoring case", () => {
  it("normalizePresets keeps the NEWER of a case-variant pair and reports the older", () => {
    const { list, dropped } = normalizePresets([
      { name: "Deploy", prompt: "old" },
      { name: " Review ", prompt: "r" },
      { name: "deploy", prompt: "new" },
      { name: "", prompt: "nameless" },
    ]);
    expect(list).toEqual([
      { name: "deploy", prompt: "new" },
      { name: "Review", prompt: "r" },
    ]);
    expect(dropped).toEqual(["Deploy"]);
  });

  it("mergePresets: the server's order, this browser's entry on a shared name, its extras appended", () => {
    const merged = mergePresets(
      [
        { name: "B", prompt: "server b" },
        { name: "Shared", prompt: "server" },
      ],
      [
        { name: "A", prompt: "mine a" },
        { name: "shared", prompt: "mine" },
      ]
    );
    expect(merged).toEqual([
      { name: "B", prompt: "server b" },
      { name: "shared", prompt: "mine" },
      { name: "A", prompt: "mine a" },
    ]);
  });
});

describe("planBoot never loses this browser's own data", () => {
  const server = (p: Partial<Prefs>): Partial<Prefs> => ({ ...PREF_DEFAULTS, ...p });

  it("an unseeded browser MERGES its saved prompts into the server's list (and uploads the union)", () => {
    // review/prefs_loss.test.ts: this used to apply [B] and drop A for good.
    const plan = planBoot(
      server({ prompt_presets: [{ name: "B", prompt: "b" }] }),
      { prompt_presets: [{ name: "A", prompt: "a" }] },
      false
    );
    const union = [
      { name: "B", prompt: "b" },
      { name: "A", prompt: "a" },
    ];
    expect(plan.upload.prompt_presets).toEqual(union);
    expect(plan.apply.prompt_presets).toEqual(union);
  });

  it("an unseeded browser whose prompts the server already has adopts nothing and uploads nothing", () => {
    const list = [{ name: "B", prompt: "b" }];
    const plan = planBoot(server({ prompt_presets: list }), { prompt_presets: list }, false);
    expect(plan).toEqual({ upload: {}, apply: {} });
  });

  it("a seeded browser still takes the server's list (it is the latest edit)", () => {
    const plan = planBoot(
      server({ prompt_presets: [{ name: "B", prompt: "b" }] }),
      { prompt_presets: [{ name: "A", prompt: "a" }] },
      true
    );
    expect(plan.apply.prompt_presets).toEqual([{ name: "B", prompt: "b" }]);
    expect(plan.upload).toEqual({});
  });

  it("a dirty field is uploaded, never cleared, even when seeded and the server is at its default", () => {
    const local = { prompt_presets: [{ name: "Mine", prompt: "x" }], keymap: { keys: { a: [] }, chords: {} } };
    const plan = planBoot(server({}), local, true, new Set(["prompt_presets", "keymap"] as const));
    expect(plan.apply).toEqual({});
    expect(plan.upload).toEqual(local);
  });

  it("a dirty field beats an older SET server value", () => {
    const plan = planBoot(server({ theme: "dark" }), { theme: "light" }, true, new Set(["theme"] as const));
    expect(plan.apply).toEqual({});
    expect(plan.upload).toEqual({ theme: "light" });
  });

  it("a dirty removal is uploaded as null (the clear is the newest edit)", () => {
    const plan = planBoot(server({ theme: "dark" }), {}, true, new Set(["theme"] as const));
    expect(plan.upload).toEqual({ theme: null });
    expect(plan.apply).toEqual({});
  });
});

describe("dirty writes survive a failed POST and a reload", () => {
  const g = globalThis as Record<string, unknown>;
  let prevLS: unknown;
  let prev: (b: Partial<Prefs>) => Promise<unknown>;
  let prevBeacon: (b: Partial<Prefs>) => Promise<unknown>;

  beforeEach(() => {
    vi.useFakeTimers();
    prevLS = g.localStorage;
    prev = setPrefSender(async () => {});
    prevBeacon = setPrefBeacon(async () => {});
  });
  afterEach(async () => {
    setPrefSender(async () => {});
    await flushPrefWrites(memKV());
    setPrefSender(prev);
    setPrefBeacon(prevBeacon);
    vi.useRealTimers();
    if (prevLS === undefined) delete g.localStorage;
    else g.localStorage = prevLS;
  });

  it("a POST that fails leaves the field dirty in localStorage, so the next load uploads it instead of wiping it", async () => {
    // review/prefs_loss.test.ts case 1: the server was restarting.
    const kv = memKV({ mf_prefs_seeded: "1" });
    g.localStorage = kv;
    kv.setItem("mindflock.prompt_presets", JSON.stringify([{ name: "Mine", prompt: "x" }]));
    setPrefSender(async () => {
      throw new TypeError("Failed to fetch");
    });
    notePrefWrite("mindflock.prompt_presets");
    expect(JSON.parse(kv.data[DIRTY_KEY])).toEqual(["prompt_presets"]);
    await flushPrefWrites(kv);
    expect([...dirtyFields(kv)]).toEqual(["prompt_presets"]);
    // Next page load: the server still has nothing.
    const plan = planBoot({ prompt_presets: [] }, readAllLocal(kv), true, dirtyFields(kv));
    expect(plan.apply).toEqual({});
    expect(plan.upload.prompt_presets).toEqual([{ name: "Mine", prompt: "x" }]);
  });

  it("a reload inside the debounce: the mark is already in localStorage before any POST", () => {
    const kv = memKV({ cs_theme: "light" });
    g.localStorage = kv;
    notePrefWrite("cs_theme");
    // No timer has fired; the page goes away here.
    expect([...dirtyFields(kv)]).toEqual(["theme"]);
  });

  it("a failed send is retried with backoff and the mark clears once it lands", async () => {
    const kv = memKV({ cs_theme: "light" });
    g.localStorage = kv;
    let fail = true;
    const sent: Array<Partial<Prefs>> = [];
    setPrefSender(async (b) => {
      if (fail) throw new TypeError("Failed to fetch");
      sent.push(b);
    });
    notePrefWrite("cs_theme");
    await vi.advanceTimersByTimeAsync(500); // debounce → fails
    expect(sent).toEqual([]);
    expect(dirtyFields(kv).has("theme")).toBe(true);
    fail = false;
    await vi.advanceTimersByTimeAsync(5000); // first retry
    expect(sent).toEqual([{ theme: "light" }]);
    expect(kv.data[DIRTY_KEY]).toBeUndefined();
  });

  it("a write made while its POST was in flight stays dirty", async () => {
    const kv = memKV({ cs_theme: "light" });
    g.localStorage = kv;
    let release!: () => void;
    setPrefSender(
      () =>
        new Promise<void>((r) => {
          release = r;
        })
    );
    notePrefWrite("cs_theme");
    const inFlight = flushPrefWrites(kv);
    kv.data.cs_theme = "dark";
    notePrefWrite("cs_theme");
    release();
    await inFlight;
    expect(dirtyFields(kv).has("theme")).toBe(true);
  });

  it("a 4xx the server means (e.g. 404 on an older server) stops retrying", async () => {
    const kv = memKV({ cs_theme: "light" });
    g.localStorage = kv;
    setPrefSender(async () => {
      throw Object.assign(new Error("/api/prefs -> 404"), { status: 404 });
    });
    notePrefWrite("cs_theme");
    await flushPrefWrites(kv);
    expect(dirtyFields(kv).size).toBe(0);
  });

  it("a 409 (settings.json unreadable) keeps the field dirty and retries, so fixing the file can't revert it", async () => {
    const kv = memKV({ cs_theme: "light", mf_prefs_seeded: "1" });
    g.localStorage = kv;
    let fail = true;
    const posted: Array<Partial<Prefs>> = [];
    setPrefSender(async (b) => {
      if (fail) throw Object.assign(new Error("/api/prefs -> 409"), { status: 409 });
      posted.push(b);
    });
    notePrefWrite("cs_theme");
    await flushPrefWrites(kv);
    expect(dirtyFields(kv).has("theme")).toBe(true);
    // The file is fixed; the server still holds the old theme. A pull now
    // must not apply it over the unsent local change.
    const plan = planBoot({ theme: "dark" } as Partial<Prefs>, readAllLocal(kv), true, dirtyFields(kv));
    expect(plan.apply.theme).toBeUndefined();
    fail = false;
    await vi.advanceTimersByTimeAsync(60_000);
    expect(posted).toEqual([{ theme: "light" }]);
    expect(dirtyFields(kv).size).toBe(0);
  });

  it("page hide sends every unsent field with the keepalive sender", async () => {
    const kv = memKV({ cs_theme: "light", mf_hints: "false", [DIRTY_KEY]: '["hints"]' });
    g.localStorage = kv;
    const beaconed: Array<Partial<Prefs>> = [];
    setPrefBeacon(async (b) => {
      beaconed.push(b);
    });
    const posted: Array<Partial<Prefs>> = [];
    setPrefSender(async (b) => {
      posted.push(b);
    });
    notePrefWrite("cs_theme");
    flushPrefsOnHide(kv);
    await vi.advanceTimersByTimeAsync(0);
    expect(beaconed).toEqual([{ theme: "light", hints: false }]);
    expect(dirtyFields(kv).size).toBe(0);
    await vi.advanceTimersByTimeAsync(600);
    expect(posted).toEqual([]); // the debounce was folded into the beacon
  });
});

describe("the page-hide sender", () => {
  it("POSTs /api/prefs with keepalive, so the request outlives the page", async () => {
    const seen: Array<{ url: string; init: RequestInit }> = [];
    vi.stubGlobal("fetch", async (url: string, init: RequestInit) => {
      seen.push({ url, init });
      return { ok: true, status: 200 } as Response;
    });
    try {
      const kv = memKV({ cs_theme: "dark", mf_prefs_dirty: '["theme"]' });
      flushPrefsOnHide(kv);
      await Promise.resolve();
      await Promise.resolve();
      expect(seen).toHaveLength(1);
      expect(seen[0].url).toBe("/api/prefs");
      expect(seen[0].init.keepalive).toBe(true);
      expect(JSON.parse(String(seen[0].init.body))).toEqual({ theme: "dark" });
      await new Promise((r) => setTimeout(r, 0));
      expect(kv.data.mf_prefs_dirty).toBeUndefined();
    } finally {
      vi.unstubAllGlobals();
    }
  });
});

describe("saving a prompt whose name differs only in case", () => {
  const g = globalThis as Record<string, unknown>;
  let prevLS: unknown;
  let prev: (b: Partial<Prefs>) => Promise<unknown>;
  beforeEach(() => {
    prevLS = g.localStorage;
    prev = setPrefSender(async () => {});
  });
  afterEach(async () => {
    await flushPrefWrites(memKV());
    setPrefSender(prev);
    if (prevLS === undefined) delete g.localStorage;
    else g.localStorage = prevLS;
  });

  it("replaces the old one and reports it, instead of keeping both for the server to drop one", async () => {
    const { upsertUserPreset, loadUserPresets, saveUserPresets } = await import("../lib/presets");
    const kv = memKV({ "mindflock.prompt_presets": JSON.stringify([{ name: "Deploy", prompt: "old" }]) });
    g.localStorage = kv;
    const r = upsertUserPreset("deploy", "new");
    expect(r.replaced).toBe("Deploy");
    expect(loadUserPresets()).toEqual([{ name: "deploy", prompt: "new" }]);
    // Same spelling: a plain overwrite, nothing to report.
    expect(upsertUserPreset("deploy", "newer").replaced).toBeNull();
    // A list handed in with both keeps the newer (later) one.
    expect(saveUserPresets([{ name: "X", prompt: "1" }, { name: "x", prompt: "2" }])).toEqual(["X"]);
    expect(loadUserPresets()).toEqual([{ name: "x", prompt: "2" }]);
  });
});
