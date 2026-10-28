/** Spoken text (lib/spokenText): the fixture tests/unit/data/spoken_text.json
 * — one table, three implementations — through the TypeScript module AND
 * the static twin backend/web/static/spoken-text.js (the phone page's
 * `window.MindFlockSpokenText.shape`), with the twin's TABLES equal to the
 * module's. Every case is also idempotent (shaping the output changes
 * nothing), odd input is "", the owner's rows are normalised like the
 * server's, and a 500-character line shapes in well under 2 ms. */

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import { MAX_OUT, normalizePronounce, PRONOUNCE_MAX, shape, shapeForSpeech, TABLES } from "../lib/spokenText";
import { code } from "./sourceText";

const apiRaw = vi.fn();
vi.mock("../api/client", () => ({
  api: vi.fn(),
  apiRaw: (...a: unknown[]) => apiRaw(...a),
  instApi: vi.fn(),
  ApiError: class extends Error {},
}));
vi.mock("../lib/toast", () => ({ toast: vi.fn() }));
vi.mock("../lib/prefsSync", () => ({ firstPrefsPull: Promise.resolve() }));

const storage = new Map<string, string>();
(globalThis as Record<string, unknown>).localStorage = {
  getItem: (k: string) => (storage.has(k) ? storage.get(k)! : null),
  setItem: (k: string, v: string) => void storage.set(k, String(v)),
  removeItem: (k: string) => void storage.delete(k),
  clear: () => storage.clear(),
};

const { PronounceBlock, pronounceError, savedPronounce, speakAsWritten, withRule, withoutRule } = await import(
  "../components/settings/screens/Pronounce"
);

// This project installs no node types: the file reads go through a dynamic
// import the bundler leaves alone (as codetreeSupportRoots.test.ts does).
const fs = (await import(/* @vite-ignore */ "node:" + "fs")) as { readFileSync(p: string, enc: string): string };
const ROOT = new URL("../../../", import.meta.url).pathname;
const read = (rel: string) => fs.readFileSync(ROOT + rel, "utf8");
const fixture = JSON.parse(read("tests/unit/data/spoken_text.json")) as {
  cases: Array<{ family: string; name: string; in: string; out: string; overrides?: unknown }>;
};

/** The static twin, run as a classic script would run it. */
function twin() {
  const src = read("backend/web/static/spoken-text.js");
  const win: Record<string, unknown> = {};
  new Function("window", src)(win);
  return win.MindFlockSpokenText as {
    shape(text: unknown, overrides?: unknown): string;
    TABLES: unknown;
    normalizePronounce(raw: unknown): unknown;
  };
}

describe("the fixture, through the module", () => {
  for (const c of fixture.cases) {
    it(`[${c.family}] ${c.name}`, () => {
      const got = shapeForSpeech(c.in, c.overrides);
      expect(got).toBe(c.out);
      expect(shapeForSpeech(got, c.overrides)).toBe(got);
    });
  }
});

describe("the fixture, through the static twin", () => {
  const t = twin();
  it("exposes window.MindFlockSpokenText.shape and the module's tables", () => {
    expect(typeof t.shape).toBe("function");
    expect(t.TABLES).toEqual(TABLES);
    expect(t.normalizePronounce([{ say: " Live ", as: "lyve" }])).toEqual([{ say: "live", as: "lyve" }]);
  });
  for (const c of fixture.cases) {
    it(`[${c.family}] ${c.name}`, () => {
      expect(t.shape(c.in, c.overrides)).toBe(c.out);
    });
  }
});

describe("the contract", () => {
  it("shape is shapeForSpeech; odd input is empty", () => {
    expect(shape).toBe(shapeForSpeech);
    expect(shapeForSpeech("")).toBe("");
    expect(shapeForSpeech(null)).toBe("");
    expect(shapeForSpeech(42)).toBe("");
    expect(shapeForSpeech("   ")).toBe("");
  });

  it("the caption is never the shaped text: shaping is a separate pure call", () => {
    const line = "Everything goes live.";
    expect(shapeForSpeech(line)).toBe("Everything goes lyve.");
    expect(line).toBe("Everything goes live.");
  });

  it("the owner's rows are normalised like the server's", () => {
    expect(normalizePronounce(null)).toEqual([]);
    expect(normalizePronounce([{ say: "SQL", as: " S Q L " }, { say: "sql", as: "again" }, { say: "x".repeat(41), as: "y" }])).toEqual([
      { say: "sql", as: "S Q L" },
    ]);
    expect(normalizePronounce([{ say: "a<b", as: "x" }, { say: "ok", as: "<b>x</b>" }, { say: "ok", as: "fine" }])).toEqual([
      { say: "ok", as: "fine" },
    ]);
    const many = Array.from({ length: 50 }, (_, i) => ({ say: "w" + i, as: "x" }));
    expect(normalizePronounce(many)).toHaveLength(PRONOUNCE_MAX);
  });

  it("the output is bounded and cut at a space", () => {
    const out = shapeForSpeech("word ".repeat(2000));
    expect(out.length).toBeLessThanOrEqual(MAX_OUT);
    expect(out.endsWith("word")).toBe(true);
  });

  it("a 500-character line shapes in under 2 ms", () => {
    const line = "Once it is done everything goes live; see backend/web/core/speech.py:404 and PR #275. ".repeat(6).slice(0, 500);
    shapeForSpeech(line);
    const t0 = performance.now();
    for (let i = 0; i < 50; i++) shapeForSpeech(line);
    expect((performance.now() - t0) / 50).toBeLessThan(2);
  });
});

describe("Settings → Voice → Say it as (screens/Pronounce.tsx)", () => {
  type Model = import("../components/settings/useSettings").SettingsModel;
  const model = (pronounce: unknown): Model & { saveField: ReturnType<typeof vi.fn> } => {
    const settings = { voice: { pronounce } } as Record<string, Record<string, unknown>>;
    return {
      settings,
      reload: async () => {},
      saveField: vi.fn(async () => true),
      saveGroup: vi.fn(async () => true),
      saveGroups: vi.fn(async () => true),
      get: (g: string, f: string) => settings[g]?.[f],
    } as Model & { saveField: ReturnType<typeof vi.fn> };
  };
  const render = (pronounce: unknown, view: Record<string, unknown> | null = null) =>
    renderToStaticMarkup(createElement(PronounceBlock, { model: model(pronounce), view }));

  it("is a sub-block after Voice words, one row per saved pronunciation, a draft line, no h3 title", () => {
    const html = render([{ say: "live", as: "lyve" }, { say: "mindflock", as: "mind flock" }]);
    expect(html).toMatch(/^<div class="set-section" id="voice-pronounce"><h4 class="set-subtitle">Say it as<\/h4>/);
    expect(html).not.toContain("set-section-title");
    expect([...html.matchAll(/data-pronounce-say="([^"]+)"/g)].map((m) => m[1])).toEqual(["live", "mindflock"]);
    expect(html).toContain('<span class="voice-pronounce-say">live</span>');
    expect(html).toContain('<span class="voice-pronounce-as">lyve</span>');
    expect(html).toContain('aria-label="Remove live"');
    expect(html).toContain('id="voice-pronounce-say"');
    expect(html).toContain('id="voice-pronounce-as"');
    expect(html).toContain('id="voice-pronounce-add"');
    expect(html).toContain('id="voice-pronounce-try-draft"');
    expect(html).toContain('id="voice-pronounce-reset"');
    // Mounted right after the words block, in Voice.tsx.
    expect(code("components/settings/screens/Voice.tsx")).toMatch(/<VoiceWordsBlock model=\{model\} view=\{view\} \/>\s*<PronounceBlock model=\{model\} view=\{view\} \/>/);
  });

  it("nothing saved: no rows, no Remove all; a locked viewer is told", () => {
    const html = render(undefined);
    expect(html).not.toContain("data-pronounce-say");
    expect(html).not.toContain("voice-pronounce-reset");
    expect(render([], { configure_allowed: false })).toContain("Only the owner of this computer can change them.");
  });

  it("the saved rows are read the server's way", () => {
    expect(savedPronounce([{ say: " Live ", as: "lyve" }, "junk", { say: "live", as: "x" }])).toEqual([{ say: "live", as: "lyve" }]);
    expect(savedPronounce(null)).toEqual([]);
  });

  it("the editor refuses what the server would", () => {
    expect(pronounceError("", "x")).toBe("Type the word or phrase to change.");
    expect(pronounceError("live", " ")).toBe("Type how to say it.");
    expect(pronounceError("x".repeat(41), "y")).toMatch(/At most 40/);
    expect(pronounceError("a<b", "y")).toMatch(/A word or phrase/);
    expect(pronounceError("ok", "<b>")).toMatch(/can't hold/);
    expect(pronounceError("Pull Request", "P R")).toBe("");
  });

  it("adding a word that has a row replaces it; removing drops it; the list is bounded", () => {
    const rows = [{ say: "live", as: "lyve" }];
    expect(withRule(rows, { say: "Live", as: "liv" })).toEqual([{ say: "live", as: "liv" }]);
    expect(withRule(rows, { say: "SQL", as: "sequel" })).toEqual([...rows, { say: "sql", as: "sequel" }]);
    expect(withRule(rows, { say: "", as: "x" })).toBe(rows);
    expect(withoutRule(rows, "live")).toEqual([]);
    const many = Array.from({ length: PRONOUNCE_MAX }, (_, i) => ({ say: "w" + i, as: "x" }));
    expect(withRule(many, { say: "one more", as: "x" })).toHaveLength(PRONOUNCE_MAX);
  });

  it("Try speaks the respelling as written: POST /api/voice/speak with shape false", async () => {
    expect(await speakAsWritten(null, "lyve")).toMatch(/can't play audio/);
    apiRaw.mockResolvedValueOnce({ ok: false, status: 409, statusText: "Conflict", json: async () => ({ error: "no voice backend is ready" }) });
    const ctx = { decodeAudioData: vi.fn(), createBufferSource: vi.fn(), destination: {} } as unknown as AudioContext;
    expect(await speakAsWritten(ctx, "lyve")).toBe("Couldn't speak: no voice backend is ready.");
    expect(apiRaw).toHaveBeenCalledWith("/api/voice/speak", expect.objectContaining({ method: "POST", body: JSON.stringify({ text: "lyve", shape: false }) }));
  });
});
