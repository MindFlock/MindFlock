/** Saved prompts are templates now (lib/templates.ts, lib/presets.ts): the
 * per-browser list is pushed to the server once, deduped by name+body, and
 * every old reader keeps its signature over the templates cache. */

import { describe, expect, it, vi } from "vitest";

const calls: Array<{ path: string; opts?: { json?: unknown; method?: string } }> = [];
let serverList: Array<Record<string, unknown>> = [];
vi.mock("../api/client", () => ({
  api: vi.fn(async (path: string, opts?: { json?: unknown; method?: string }) => {
    calls.push({ path, opts });
    if (opts?.method === "DELETE") {
      const name = decodeURIComponent(path.split("/").pop() || "").toLowerCase();
      const before = serverList.length;
      serverList = serverList.filter((t) => String(t.name).toLowerCase() !== name);
      return { deleted: serverList.length !== before };
    }
    if (opts?.json) {
      const t = opts.json as Record<string, unknown>;
      serverList = serverList.filter((x) => String(x.name).toLowerCase() !== String(t.name).toLowerCase());
      serverList.push(t);
      return { template: t, templates: serverList };
    }
    return { templates: serverList };
  }),
}));

const tpl = await import("../lib/templates");
const presets = await import("../lib/presets");

function memKV(init: Record<string, string> = {}) {
  const data = { ...init };
  return {
    data,
    getItem: (k: string) => (k in data ? data[k] : null),
    setItem: (k: string, v: string) => {
      data[k] = v;
    },
  };
}

describe("planPresetMigration — deduped by name AND body", () => {
  it("saves what the server lacks, skips an identical one, keeps a clashing body under a new name", () => {
    const { save, skipped } = tpl.planPresetMigration(
      [
        { name: "Deploy", prompt: "ship it" },
        { name: "fix", prompt: "fix tests" },
        { name: "docs", prompt: "DIFFERENT" },
        { name: "", prompt: "nameless" },
        { name: "empty", prompt: "  " },
      ],
      [
        { name: "deploy", prompt: "ship it" },
        { name: "docs", prompt: "write docs", program: "claude" },
      ]
    );
    expect(save).toEqual([
      { name: "fix", prompt: "fix tests" },
      { name: "docs (saved prompt)", prompt: "DIFFERENT" },
    ]);
    expect(skipped).toEqual(["Deploy"]);
  });
  it("skips a clash whose fallback spelling is taken too", () => {
    const { save, skipped } = tpl.planPresetMigration(
      [{ name: "docs", prompt: "x" }],
      [{ name: "docs", prompt: "y" }, { name: "docs (saved prompt)", prompt: "z" }]
    );
    expect(save).toEqual([]);
    expect(skipped).toEqual(["docs"]);
  });
});

describe("migratePresetsOnce — once per browser", () => {
  it("pushes the legacy list, marks the browser, and never runs again", async () => {
    const kv = memKV({ "mindflock.prompt_presets": JSON.stringify([{ name: "A", prompt: "a" }, { name: "B", prompt: "b" }]) });
    const posted: unknown[] = [];
    const post = async (t: unknown) => {
      posted.push(t);
    };
    expect(await tpl.migratePresetsOnce([{ name: "b", prompt: "b" }], kv, post)).toBe(1);
    expect(posted).toEqual([{ name: "A", prompt: "a" }]);
    expect(kv.data.mf_presets_migrated).toBe("1");
    expect(await tpl.migratePresetsOnce([], kv, post)).toBe(0);
    expect(posted.length).toBe(1);
  });
  it("a failed push leaves the marker unset, so the next load retries — and never fails the read", async () => {
    const kv = memKV({ "mindflock.prompt_presets": JSON.stringify([{ name: "A", prompt: "a" }]) });
    expect(await tpl.migratePresetsOnce([], kv, async () => { throw new Error("502"); })).toBe(0);
    expect(kv.data.mf_presets_migrated).toBeUndefined();
  });
  it("a successful migration empties the legacy list, so a fresh browser has nothing to re-migrate", async () => {
    const kv = memKV({ "mindflock.prompt_presets": JSON.stringify([{ name: "A", prompt: "a" }]) });
    expect(await tpl.migratePresetsOnce([], kv, async () => {})).toBe(1);
    expect(kv.data.mf_presets_migrated).toBe("1");
    expect(JSON.parse(kv.data["mindflock.prompt_presets"] || "[]")).toEqual([]);
  });
  it("nothing to migrate: no marker needed either way", async () => {
    const kv = memKV();
    expect(await tpl.migratePresetsOnce([], kv, async () => {})).toBe(0);
  });
});

describe("presets.ts — the old readers over the server list", () => {
  it("loadUserPresets lists the templates that are a prompt, after the fetch lands", async () => {
    tpl.resetTemplatesCache();
    serverList = [
      { name: "fix", prompt: "fix tests" },
      { name: "setup-only", program: "claude" },
    ];
    await tpl.ensureTemplates();
    expect(presets.loadUserPresets()).toEqual([{ name: "fix", prompt: "fix tests" }]);
    expect(presets.findPreset("u:fix")?.prompt).toBe("fix tests");
    expect(presets.findPreset("b:Fix failing tests")).toBeTruthy();
  });
  it("upsertUserPreset POSTs a template, replacing a case-variant; a full template is never a saved prompt", async () => {
    tpl.resetTemplatesCache();
    serverList = [{ name: "Deploy", prompt: "old" }, { name: "setup", prompt: "p", program: "codex" }];
    await tpl.ensureTemplates();
    calls.length = 0;
    // the full template carries a prompt but is not a saved prompt: removing
    // a saved prompt must never delete somebody's session setup
    expect(presets.loadUserPresets()).toEqual([{ name: "Deploy", prompt: "old" }]);
    const r = presets.upsertUserPreset("deploy", "new");
    expect(r.replaced).toBe("Deploy");
    expect(presets.loadUserPresets()).toEqual([{ name: "deploy", prompt: "new" }]);
    const post = calls.find((c) => c.opts?.json);
    expect(post?.opts?.json).toEqual({ name: "deploy", prompt: "new" });
    expect(presets.upsertUserPreset("deploy", "newer").replaced).toBeNull();
  });
  it("saveUserPresets deletes what was dropped and saves what changed; names fold ignoring case", async () => {
    tpl.resetTemplatesCache();
    serverList = [{ name: "gone", prompt: "g" }, { name: "kept", prompt: "k" }];
    await tpl.ensureTemplates();
    calls.length = 0;
    expect(presets.saveUserPresets([{ name: "kept", prompt: "k" }, { name: "X", prompt: "1" }, { name: "x", prompt: "2" }])).toEqual(["X"]);
    expect(presets.loadUserPresets()).toEqual([{ name: "kept", prompt: "k" }, { name: "x", prompt: "2" }]);
    expect(calls.some((c) => c.opts?.method === "DELETE" && c.path.endsWith("/gone"))).toBe(true);
    expect(calls.filter((c) => c.opts?.json).map((c) => (c.opts!.json as { name: string }).name)).toEqual(["x"]);
    expect(presets.tidyUserPresets()).toEqual([]);
  });
  it("each built-in starter exists once, and the vanilla modal's duplicates are gone", () => {
    const names = presets.BUILTIN_PRESETS.map((p) => p.name.toLowerCase());
    expect(new Set(names).size).toBe(names.length);
  });
});

describe("what counts as a saved prompt (round 2 glue)", () => {
  it("a template carrying a fast-track or a schedule is a template, not a prompt", async () => {
    const { isPromptOnly } = await import("../lib/templates");
    expect(isPromptOnly({ name: "a", prompt: "do it" })).toBe(true);
    expect(isPromptOnly({ name: "b", prompt: "do it", fasttrack: "pr" })).toBe(false);
    expect(isPromptOnly({ name: "c", prompt: "do it", ask_first: true })).toBe(false);
    expect(isPromptOnly({ name: "d", prompt: "do it", schedule: { at: "09:00", days: "daily" } })).toBe(false);
    expect(isPromptOnly({ name: "e", prompt: "do it", repo_path: "/r" })).toBe(false);
  });

  it("the Auto-split hint names the field by its label, Task", async () => {
    const src = (await import("../lib/newSessionSubmit.ts?raw")).default;
    expect(src).toContain("write it in the Task below");
    expect(src).not.toContain("the Prompt below");
  });
});
