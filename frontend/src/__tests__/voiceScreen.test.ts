/** Settings → Voice (screens/Voice.tsx; was Your phone → Voice): the pure
 * helpers — which engine fields a kind shows, the backend a block shows, the
 * readiness line, the Try-it buttons' state and their two requests — and the
 * screen's markup, rendered through react-dom/server inside a fake settings
 * model (the vitest environment is node; the requests go to a mocked apiRaw).
 *
 * What is pinned: the built-in engine is first and the default, and asks for
 * nothing but a voice (a select of its voices, free text when it lists
 * none); a host unfolds URL / key / model and local models a model size and
 * a piper voice; a saved key shows Clear; the readiness line is the server's
 * verdict word for word, with the built-in engine's own sentence when it is
 * picked, and copes with a server that has no `system` key; the phone page's
 * phone | server choice is its own block; Try it is disabled with a reason
 * until the server has an engine ready; the privacy and iPhone sentences are
 * always there; Your phone keeps only a pointer. */

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { beforeEach, describe, expect, it, vi } from "vitest";
import mobileSrc from "../components/settings/screens/Mobile.tsx?raw";

// apiRaw records the call; `raw` answers it. (A vi.fn returning a rejected
// promise fails the test under vitest 5 even when the caller catches it.)
const apiRaw = vi.fn();
let raw: () => Promise<unknown> = async () => ({});
const answer = (r: unknown) => {
  raw = async () => r;
};
vi.mock("../api/client", () => ({
  api: vi.fn(),
  apiRaw: (...a: unknown[]) => {
    apiRaw(...a);
    return raw();
  },
}));
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

const {
  VoiceSectionView,
  VOICE_BACKENDS,
  effectiveBackend,
  micErrorText,
  readinessLine,
  speakTest,
  systemVoiceOptions,
  transcribeTest,
  tryState,
  voiceFields,
  TEST_SENTENCE,
} = await import("../components/settings/screens/Voice");
const { SECRET_MASK, SettingsCtx } = await import("../components/settings/useSettings");
const { resolveScreenKey } = await import("../components/settings/screenKeys");
type VoiceView = import("../components/settings/screens/Voice").VoiceView;
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

const SYSTEM = {
  available: true,
  platform: "wsl",
  detail: "Windows via WSL — recognizer en-US, voices David, Zira",
  voices: ["Microsoft David Desktop", "Microsoft Zira Desktop"],
  recognizers: ["en-US"],
};

/** A fresh install on WSL: both kinds on the built-in engine, both ready. */
const BUILTIN: VoiceView = {
  stt: "phone",
  tts: "phone",
  backends: { stt: ["system", "openai", "local"], tts: ["system", "openai", "local"] },
  server_ready: {
    stt: true,
    tts: true,
    stt_backend: "system",
    stt_detail: "Windows speech recognizer en-US",
    tts_backend: "system",
    tts_detail: "Windows voices",
  },
  system: SYSTEM,
};

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
const tag = (html: string, id: string) => html.match(new RegExp(`<button[^>]*id="${id}"[^>]*>`))?.[0] || "";

describe("voiceFields", () => {
  it("shows nothing while speech runs on the phone", () => {
    expect(voiceFields("stt", "phone", "openai")).toEqual({ backend: false, url: false, key: false, model: false, voice: false });
    expect(voiceFields("tts", "", "")).toMatchObject({ backend: false, voice: false });
  });

  it("the built-in engine asks only for a voice", () => {
    expect(voiceFields("tts", "server", "system")).toEqual({ backend: true, url: false, key: false, model: false, voice: true });
    expect(voiceFields("stt", "server", "system")).toEqual({ backend: true, url: false, key: false, model: false, voice: false });
  });

  it("unfolds the host fields for a host", () => {
    expect(voiceFields("stt", "server", "openai")).toEqual({ backend: true, url: true, key: true, model: true, voice: false });
    expect(voiceFields("tts", "server", "openai")).toEqual({ backend: true, url: true, key: true, model: true, voice: true });
  });

  it("local models need no URL or key; only recognition has a model size", () => {
    expect(voiceFields("stt", "server", "local")).toEqual({ backend: true, url: false, key: false, model: true, voice: false });
    expect(voiceFields("tts", "server", "local")).toEqual({ backend: true, url: false, key: false, model: false, voice: true });
  });
});

describe("effectiveBackend", () => {
  it("the saved one wins; a blank one is what the server resolved it to, else the built-in engine", () => {
    expect(effectiveBackend("stt", "local", BUILTIN)).toBe("local");
    expect(effectiveBackend("stt", "", BUILTIN)).toBe("system");
    expect(effectiveBackend("stt", "", VIEW)).toBe("openai");
    expect(effectiveBackend("tts", "", VIEW)).toBe("system");
    expect(effectiveBackend("tts", "", null)).toBe("system");
  });
});

describe("readinessLine", () => {
  it("is empty while that kind runs on the phone, and waits for the server otherwise", () => {
    expect(readinessLine("stt", VIEW, "phone")).toEqual({ ok: false, text: "" });
    expect(readinessLine("stt", null, "server")).toEqual({ ok: false, text: "Checking the server…" });
  });

  it("repeats the server's verdict, backend and detail word for word", () => {
    expect(readinessLine("stt", VIEW, "server", "openai")).toEqual({
      ok: true,
      text: "Server recognition ready (openai): gpt-transcribe at api.openai.com.",
    });
    expect(readinessLine("tts", VIEW, "server", "local")).toEqual({
      ok: false,
      text: "Server voice not ready: local models not installed: uv sync --group voice.",
    });
  });

  it("the built-in engine's own sentence is the detail", () => {
    expect(readinessLine("tts", BUILTIN, "server", "system").text).toBe(
      "Server voice ready (system): Windows via WSL — recognizer en-US, voices David, Zira."
    );
    // A blank backend the server resolved to the built-in engine reads the same.
    expect(readinessLine("stt", BUILTIN, "server").text).toBe(
      "Server recognition ready (system): Windows via WSL — recognizer en-US, voices David, Zira."
    );
    const linux: VoiceView = {
      server_ready: { stt: false, stt_detail: "nothing ready" },
      system: { available: false, platform: "none", detail: "Linux has no built-in speech engine: pick local models or a host." },
    };
    expect(readinessLine("stt", linux, "server", "system")).toEqual({
      ok: false,
      text: "Server recognition not ready: Linux has no built-in speech engine: pick local models or a host.",
    });
  });

  it("copes with a bare verdict and with a server that has no `system` key", () => {
    expect(readinessLine("tts", { server_ready: { tts: true } }, "server").text).toBe("Server voice ready (server).");
    expect(readinessLine("stt", {}, "server").text).toBe("Server recognition not ready.");
    expect(readinessLine("stt", { server_ready: { stt: false } }, "server", "system").text).toBe(
      "Server recognition not ready: this server has no built-in engine yet — update MindFlock."
    );
    // An older server that is ready on another backend keeps its own detail.
    expect(readinessLine("stt", VIEW, "server").text).toBe(
      "Server recognition ready (openai): gpt-transcribe at api.openai.com."
    );
  });
});

describe("tryState", () => {
  it("waits for the server, then follows its verdict per kind", () => {
    expect(tryState(null, true)).toEqual({
      say: { ok: false, why: "Checking the server…" },
      hear: { ok: false, why: "Checking the server…" },
    });
    expect(tryState(BUILTIN, true)).toEqual({ say: { ok: true, why: "" }, hear: { ok: true, why: "" } });
    const st = tryState(VIEW, true);
    expect(st.hear.ok).toBe(true);
    expect(st.say).toEqual({
      ok: false,
      why: "No voice is ready on the server: local models not installed: uv sync --group voice.",
    });
  });

  it("recognition also needs a browser that can record", () => {
    expect(tryState(BUILTIN, false).hear).toEqual({ ok: false, why: "This browser can't record from a microphone." });
    expect(tryState({ server_ready: {} }, true).hear).toEqual({ ok: false, why: "No recognition is ready on the server." });
  });
});

describe("Try it requests", () => {
  beforeEach(() => apiRaw.mockReset());

  function fakeCtx() {
    const src = { buffer: null as unknown, connect: vi.fn(), start: vi.fn() };
    return {
      src,
      ctx: {
        destination: {},
        decodeAudioData: vi.fn(async () => ({ duration: 1 })),
        createBufferSource: () => src,
      } as unknown as AudioContext,
    };
  }

  it("speaks the test sentence and plays the bytes", async () => {
    answer({ ok: true, arrayBuffer: async () => new ArrayBuffer(8) });
    const { ctx, src } = fakeCtx();
    let t = 1000;
    const r = await speakTest(ctx, "system", () => (t += 120));
    expect(apiRaw).toHaveBeenCalledWith("/api/voice/speak", expect.objectContaining({ method: "POST" }));
    expect(JSON.parse(apiRaw.mock.calls[0][1].body)).toEqual({ text: TEST_SENTENCE });
    expect(src.start).toHaveBeenCalled();
    expect(r).toEqual({ ok: true, text: "Spoke with system in 120 ms." });
  });

  it("says the server's error, or that there is no audio", async () => {
    answer({ ok: false, status: 502, json: async () => ({ error: "piper crashed" }) });
    expect(await speakTest(fakeCtx().ctx, "local")).toEqual({ ok: false, text: "Couldn't speak: piper crashed." });
    expect(await speakTest(null, "local")).toEqual({ ok: false, text: "This browser can't play audio from the server." });
  });

  it("sends the WAV to /transcribe and says what came back", async () => {
    answer({ ok: true, json: async () => ({ text: " hello there ", backend: "system", ms: 412.4 }) });
    const wav = new Blob([new Uint8Array(44)], { type: "audio/wav" });
    expect(await transcribeTest(wav, "en-US")).toEqual({ ok: true, text: "Heard: “hello there” (system, 412 ms)" });
    const [path, init] = apiRaw.mock.calls[0];
    expect(path).toBe("/api/voice/transcribe?lang=en-US");
    expect(init).toMatchObject({ method: "POST", headers: { "Content-Type": "audio/wav" }, body: wav });

    answer({ ok: true, json: async () => ({ text: "", backend: "system", ms: 90 }) });
    expect((await transcribeTest(wav, "en")).text).toBe("Heard nothing (system, 90 ms). Speak up, or try again.");
    answer({ ok: false, status: 409, json: async () => ({ error: "no recognizer" }) });
    expect(await transcribeTest(wav, "en")).toEqual({ ok: false, text: "Couldn't transcribe: no recognizer." });
    raw = async () => {
      throw new Error("Failed to fetch");
    };
    expect((await transcribeTest(wav, "en")).text).toBe("Couldn't reach the server: Failed to fetch.");
  });

  it("a refused microphone is a plain sentence", () => {
    expect(micErrorText({ name: "NotAllowedError" })).toMatch(/^MindFlock may not use the microphone\./);
    expect(micErrorText({ name: "NotFoundError" })).toBe("No microphone was found.");
  });
});

describe("VoiceSectionView", () => {
  it("lists the three engines with the built-in one first", () => {
    expect(VOICE_BACKENDS.map((o) => o.value)).toEqual(["system", "openai", "local"]);
    expect(VOICE_BACKENDS[0].label).toBe("This computer (built in)");
  });

  it("fresh install: the built-in engine, its voices in a select, ready lines and live Try-it buttons", () => {
    const html = render({}, BUILTIN);
    expect(html).toContain('id="voice-engines"');
    expect(html).toMatch(/data-field="stt_backend"[^>]*>.*<option value="system" selected="">This computer \(built in\)/);
    for (const f of ["stt_url", "stt_api_key", "stt_model", "tts_url", "tts_api_key", "tts_model"]) expect(field(html, f)).toBe(false);
    // The voice name is a select of the system's voices, default first.
    expect(html).toMatch(
      /<select data-group="voice" data-field="tts_voice"><option value="" selected="">The system&#x27;s default<\/option><option value="Microsoft David Desktop">/
    );
    expect(html).toContain("Server voice ready (system): Windows via WSL — recognizer en-US, voices David, Zira.");
    expect(tag(html, "voice-try-say")).not.toContain("disabled");
    expect(html).toContain("Say a test sentence");
    expect(html).toContain("Try recognition");
  });

  it("the built-in voice name is free text when the system lists no voices", () => {
    const html = render({ tts_backend: "system" }, { ...BUILTIN, system: { ...SYSTEM, voices: [] } });
    expect(html).toMatch(/<input[^>]*data-field="tts_voice"[^>]*placeholder="the system&#x27;s default"/);
  });

  it("a saved voice this computer lacks stays in the list, said so", () => {
    expect(systemVoiceOptions(["Zira"], "alloy")).toEqual([
      { value: "", label: "The system's default" },
      { value: "Zira", label: "Zira" },
      { value: "alloy", label: "alloy (not on this computer)" },
    ]);
    expect(systemVoiceOptions(["Zira"], "Zira")).toHaveLength(2);
  });

  it("the phone page's choice is its own block; the intro says what runs where", () => {
    const html = render({}, BUILTIN);
    expect(html).toMatch(/id="voice-phone"[\s\S]*data-field="stt"[\s\S]*data-field="tts"/);
    expect(html).toContain('<option value="phone" selected="">The phone</option>');
    expect(html).toContain("The desktop app always uses this computer.");
    expect(html).toContain("The desktop app&#x27;s mic (in the top bar) always uses this computer&#x27;s engines");
    expect(html).toContain("Out of the box this computer uses its built-in engine");
    // Approve-by-voice defaults on (an unset field reads as on, like the server).
    expect(html).toMatch(/id="voice-approve"[^>]*checked=""/);
    expect(html).toContain("A spoken yes or no answers a permission dialog after its command is read aloud.");
    expect(html).toContain("keep your audio on this computer");
    expect(html).toContain("The phone&#x27;s own recognition sends audio to Google on Android and to Apple on iOS");
    expect(html).toContain("the home-screen icon cannot hear you.");
  });

  it("a host: URL, key, model, and Clear only once a key is saved", () => {
    const base = { stt_backend: "openai" };
    const html = render(base, VIEW);
    expect(field(html, "stt_url")).toBe(true);
    expect(html).toMatch(/data-field="stt_api_key"[^>]*type="password"/);
    expect(field(html, "stt_model")).toBe(true);
    expect(html).toContain('placeholder="gpt-transcribe"');
    expect(html).not.toContain('id="voice-clear-stt"');
    expect(html).toContain("Server recognition ready (openai): gpt-transcribe at api.openai.com.");
    const saved = render({ ...base, stt_api_key: SECRET_MASK }, VIEW);
    expect(saved).toContain('id="voice-clear-stt"');
    expect(saved).not.toContain('id="voice-clear-tts"');
  });

  it("local models: no URL or key, a whisper size for recognition, a piper voice for the voice", () => {
    const html = render({ stt_backend: "local", tts_backend: "local" }, VIEW);
    expect(field(html, "stt_url")).toBe(false);
    expect(field(html, "stt_api_key")).toBe(false);
    expect(field(html, "tts_url")).toBe(false);
    expect(html).toMatch(/data-field="stt_model"[^>]*placeholder="small"/);
    expect(field(html, "tts_model")).toBe(false);
    expect(html).toMatch(/data-field="tts_voice"[^>]*placeholder="en_US-lessac-medium"/);
    expect(html).toMatch(/id="voice-ready-tts"[^>]*>.*✗/);
  });

  it("Try it is disabled with the reason until an engine is ready", () => {
    const waiting = render({}, null);
    expect(waiting).toContain("Checking the server…");
    expect(tag(waiting, "voice-try-say")).toContain("disabled");
    expect(tag(waiting, "voice-try-hear")).toContain("disabled");
    const html = render({ tts_backend: "local" }, VIEW);
    expect(tag(html, "voice-try-say")).toContain("disabled");
    expect(html).toContain("No voice is ready on the server: local models not installed: uv sync --group voice.");
  });

  it("the switch reflects the setting", () => {
    expect(render({ approve_dialogs: false }, BUILTIN)).not.toMatch(/id="voice-approve"[^>]*checked=""/);
  });
});

describe("the move off Your phone", () => {
  it("Your phone keeps one pointer line, still #mobile-voice", () => {
    expect(mobileSrc).toMatch(/id="mobile-voice"[\s\S]{0,200}Voice settings moved to[\s\S]{0,200}gotoScreen\("voice"\)/);
    expect(mobileSrc).not.toContain("VoiceSection");
    expect(mobileSrc).not.toContain("mobileVoice.css");
  });

  it("the old and feature names open the Voice screen", () => {
    const keys = ["mobile", "voice"];
    for (const k of ["voice", "mobile-voice", "speech", "microphone"]) expect(resolveScreenKey(k, keys), k).toBe("voice");
  });
});
