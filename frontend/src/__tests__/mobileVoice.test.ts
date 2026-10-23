/** Settings → Your phone → Voice (screens/Mobile.tsx, VoiceSection): the
 * pure helpers — which backend fields a kind shows, the readiness line — and
 * the section's markup, rendered through react-dom/server inside a fake
 * settings model (the vitest environment is node; nothing here fetches).
 *
 * What is pinned: nothing unfolds while speech runs on the phone; a server
 * choice unfolds the picker and the fields that apply to the backend (no
 * URL / key for local models); a saved key shows Clear and an unsaved one
 * doesn't; the readiness line is the server's verdict, word for word; the
 * approve-by-voice switch reflects the setting; the privacy and iPhone
 * sentences are always there. */

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";

vi.mock("../api/client", () => ({ api: vi.fn() }));
vi.mock("../lib/toast", () => ({ toast: vi.fn() }));
vi.mock("../lib/clipboard", () => ({ copyText: vi.fn(async () => true) }));
vi.mock("../state/queries", () => ({
  useConfig: () => ({ data: undefined }),
  useSettingsDoc: () => ({ data: undefined }),
  fetchSettingsDoc: vi.fn(async () => ({})),
  putSettingsDoc: vi.fn(),
  refreshConfig: vi.fn(),
}));
vi.mock("../components/settings/useServerRestart", () => ({
  useServerRestart: () => ({ restarting: false, restart: vi.fn() }),
}));

const { VoiceSectionView, VOICE_BACKENDS, readinessLine, voiceFields } = await import(
  "../components/settings/screens/Mobile"
);
const { SECRET_MASK, SettingsCtx } = await import("../components/settings/useSettings");
type VoiceView = import("../components/settings/screens/Mobile").VoiceView;
type Model = import("../components/settings/useSettings").SettingsModel;

function model(voice: Record<string, unknown>): Model {
  const settings = { voice } as Record<string, Record<string, unknown>>;
  return {
    settings,
    reload: async () => {},
    saveField: vi.fn(async () => true),
    saveGroup: vi.fn(async () => true),
    saveGroups: vi.fn(async () => true),
    get: (g, f) => settings[g]?.[f],
  };
}

function render(voice: Record<string, unknown>, view: VoiceView | null = null): string {
  const m = model(voice);
  return renderToStaticMarkup(
    createElement(
      SettingsCtx.Provider,
      { value: m },
      createElement(VoiceSectionView, { model: m, view, onClearKey: () => {} })
    )
  );
}

const VIEW: VoiceView = {
  stt: "server",
  tts: "server",
  backends: { stt: ["openai", "local"], tts: ["openai", "local"] },
  server_ready: {
    stt: true,
    tts: false,
    stt_backend: "openai",
    stt_detail: "gpt-transcribe at api.openai.com",
    tts_backend: "",
    tts_detail: "local models not installed: uv sync --group voice",
    detail: "",
  },
};

const field = (html: string, name: string) => html.includes(`data-field="${name}"`);

describe("voiceFields", () => {
  it("shows nothing while speech runs on the phone", () => {
    expect(voiceFields("stt", "phone", "openai")).toEqual({
      backend: false,
      url: false,
      key: false,
      model: false,
      voice: false,
    });
    expect(voiceFields("tts", "", "")).toMatchObject({ backend: false, voice: false });
  });

  it("unfolds the host fields for a host or for whichever", () => {
    expect(voiceFields("stt", "server", "")).toEqual({ backend: true, url: true, key: true, model: true, voice: false });
    expect(voiceFields("stt", "server", "openai")).toEqual({ backend: true, url: true, key: true, model: true, voice: false });
    expect(voiceFields("tts", "server", "openai")).toEqual({ backend: true, url: true, key: true, model: true, voice: true });
  });

  it("local models need no URL or key; only recognition has a model size", () => {
    expect(voiceFields("stt", "server", "local")).toEqual({ backend: true, url: false, key: false, model: true, voice: false });
    expect(voiceFields("tts", "server", "local")).toEqual({ backend: true, url: false, key: false, model: false, voice: true });
  });
});

describe("readinessLine", () => {
  it("is empty while that kind runs on the phone, and waits for the server otherwise", () => {
    expect(readinessLine("stt", VIEW, "phone")).toEqual({ ok: false, text: "" });
    expect(readinessLine("stt", null, "server")).toEqual({ ok: false, text: "Checking the server…" });
  });

  it("repeats the server's verdict, backend and detail word for word", () => {
    expect(readinessLine("stt", VIEW, "server")).toEqual({
      ok: true,
      text: "Server recognition ready (openai): gpt-transcribe at api.openai.com.",
    });
    expect(readinessLine("tts", VIEW, "server")).toEqual({
      ok: false,
      text: "Server voice not ready: local models not installed: uv sync --group voice.",
    });
  });

  it("copes with a bare verdict", () => {
    expect(readinessLine("tts", { server_ready: { tts: true } }, "server").text).toBe("Server voice ready (server).");
    expect(readinessLine("stt", {}, "server").text).toBe("Server recognition not ready.");
  });
});

describe("VoiceSectionView", () => {
  it("lists the three backend choices with whichever first", () => {
    expect(VOICE_BACKENDS.map((o) => o.value)).toEqual(["", "openai", "local"]);
  });

  it("phone/phone: the two where-selects, the switch, the notes — no backend fields", () => {
    const html = render({});
    expect(html).toContain('id="mobile-voice"');
    expect(html).toContain("Talk to a session from the phone page: tap the mic on /m.");
    expect(field(html, "stt")).toBe(true);
    expect(field(html, "tts")).toBe(true);
    for (const f of ["stt_backend", "stt_url", "stt_api_key", "stt_model", "tts_backend", "tts_url", "tts_api_key", "tts_model", "tts_voice"])
      expect(field(html, f)).toBe(false);
    expect(html).not.toContain('id="voice-ready-stt"');
    expect(html).not.toContain('id="voice-ready-tts"');
    // Approve-by-voice defaults on (an unset field reads as on, like the server).
    expect(html).toMatch(/id="voice-approve"[^>]*checked=""/);
    expect(html).toContain("A spoken yes or no answers a permission dialog after its command is read aloud.");
    expect(html).toContain("Phone recognition sends audio to Google on Android and to Apple on iOS");
    expect(html).toContain("the home-screen icon cannot hear you. Server recognition fixes that.");
  });

  it("server recognition on a host: URL, key, model, and Clear only once a key is saved", () => {
    const base = { stt: "server", stt_backend: "openai" };
    const html = render(base, VIEW);
    expect(field(html, "stt_backend")).toBe(true);
    expect(field(html, "stt_url")).toBe(true);
    expect(html).toMatch(/data-field="stt_api_key"[^>]*type="password"/);
    expect(field(html, "stt_model")).toBe(true);
    expect(html).toContain('placeholder="gpt-transcribe"');
    expect(html).not.toContain('id="voice-clear-stt"');
    // The voice still runs on the phone: none of its fields.
    expect(field(html, "tts_backend")).toBe(false);
    expect(field(html, "tts_voice")).toBe(false);
    expect(html).toContain('id="voice-ready-stt"');
    expect(html).toContain("Server recognition ready (openai): gpt-transcribe at api.openai.com.");
    expect(html).not.toContain('id="voice-ready-tts"');

    const saved = render({ ...base, stt_api_key: SECRET_MASK }, VIEW);
    expect(saved).toContain('id="voice-clear-stt"');
    expect(saved).not.toContain('id="voice-clear-tts"');
  });

  it("local models: no URL or key, a whisper size for recognition, a piper voice for the voice", () => {
    const html = render({ stt: "server", stt_backend: "local", tts: "server", tts_backend: "local" }, VIEW);
    expect(field(html, "stt_url")).toBe(false);
    expect(field(html, "stt_api_key")).toBe(false);
    expect(field(html, "tts_url")).toBe(false);
    expect(field(html, "tts_api_key")).toBe(false);
    expect(html).toMatch(/data-field="stt_model"[^>]*placeholder="small"/);
    expect(field(html, "tts_model")).toBe(false);
    expect(html).toMatch(/data-field="tts_voice"[^>]*placeholder="en_US-lessac-medium"/);
    expect(html).toContain("Server voice not ready: local models not installed: uv sync --group voice.");
    expect(html).toMatch(/id="voice-ready-tts"[^>]*>.*✗/);
  });

  it("the server's verdict is awaited, and the switch reflects the setting", () => {
    const html = render({ tts: "server", approve_dialogs: false });
    expect(html).toContain("Checking the server…");
    expect(html).not.toMatch(/id="voice-approve"[^>]*checked=""/);
    expect(field(html, "tts_voice")).toBe(true);
    expect(html).toContain('placeholder="alloy"');
  });
});
