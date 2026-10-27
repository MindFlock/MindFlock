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
 * always there; Your phone keeps only a pointer. The local-models installer:
 * its recommendation line and button only under a basic built-in recognizer
 * or picked-but-missing local models, gone once they are ready or on a
 * server without the installer; the POST, the 2 s polling and the
 * done / failed / 409 / 403 texts; a non-owner's button disabled with why.
 * The layout: only the shared Settings pieces — the section titles in order,
 * every field a .set-row, the switch a .set-switch-row, the installer and the
 * Try-it buttons General's action rows — and none of the old one-off grid. */

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { beforeEach, describe, expect, it, vi } from "vitest";
import mobileSrc from "../components/settings/screens/Mobile.tsx?raw";
import voiceSrc from "../components/settings/screens/Voice.tsx?raw";
import voiceCss from "../components/settings/screens/voice.css?raw";
import micSrc from "../components/TopBarMic.tsx?raw";

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
  INSTALL_BASIC_LINE,
  INSTALL_DONE,
  INSTALL_LABEL,
  INSTALL_REFUSED,
  basicRecognizer,
  installLocalModels,
  installOffer,
  installView,
  localModelsMissing,
  tryInstallFix,
  TRY_INSTALL_LINE,
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
type InstallProps = import("../components/settings/screens/Voice").InstallProps;
type InstallStatus = import("../components/settings/screens/Voice").InstallStatus;
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

function render(voice: Record<string, unknown>, view: VoiceView | null = null, install?: InstallProps): string {
  const m = model(voice);
  return renderToStaticMarkup(
    createElement(
      SettingsCtx.Provider,
      { value: m },
      createElement(VoiceSectionView, { model: m, view, onClearKey: () => {}, install })
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

describe("the layout is the shared Settings one", () => {
  const titles = (html: string) =>
    [...html.matchAll(/<h3 class="set-section-title"[^>]*>([^<]*)<\/h3>/g)].map((m) => m[1]);

  it("one titled section per concern, in order", () => {
    expect(titles(render({}, BUILTIN))).toEqual([
      "Recognition",
      "Voice",
      "On the phone page",
      "This computer&#x27;s mic",
      "Voice words",
      "Permission dialogs",
      "Try it",
      "Privacy",
    ]);
    // The screen's name is the nav's: no "Voice" title above the intro.
    expect(render({}, BUILTIN)).toMatch(/^<section id="voice-screen"><p class="set-hint set-block-hint" id="voice-intro">/);
  });

  it("the anchors are shared .set-section blocks around their titles", () => {
    const html = render({}, BUILTIN);
    expect(html).toMatch(/<div class="set-section" id="voice-engines"><h3 class="set-section-title" data-kind="stt">Recognition</);
    expect(html).toMatch(/<div class="set-section" id="voice-phone"><h3 class="set-section-title">On the phone page</);
    expect(html).toMatch(/<div class="set-section" id="voice-try"><h3 class="set-section-title">Try it</);
  });

  it("no one-off grid is left, in the markup, the source or the stylesheet", () => {
    const old = ["voice-field-row", "voice-kind", "voice-sub", "voice-notes", "voice-try-buttons", "voice-install-row", "voice-ready"];
    const html = render({ stt_backend: "openai", tts_backend: "local", stt_api_key: SECRET_MASK }, VIEW);
    for (const c of old) {
      expect(html, c).not.toContain(c + '"');
      expect(html, c).not.toContain(c + " ");
      expect(voiceSrc, c).not.toMatch(new RegExp(`className="[^"]*\\b${c}\\b`));
      expect(voiceCss, c).not.toMatch(new RegExp(`\\.${c}\\b`));
    }
    expect(voiceCss).not.toContain("grid-template-columns");
  });

  it("every field is a stacked .set-row: label, control, hint", () => {
    const html = render({ stt_backend: "openai", tts_backend: "local", stt_api_key: SECRET_MASK }, VIEW);
    for (const f of ["stt_backend", "stt_url", "stt_api_key", "stt_model", "tts_backend", "tts_voice", "stt", "tts"])
      expect(html, f).toMatch(new RegExp(`class="set-row"[^>]*><span class="set-label">[^<]+</span><(select|input)[^>]*data-field="${f}"`));
    // A saved key's Clear sits in the hint line, the shared test-row.
    expect(html).toMatch(/data-field="stt_api_key"[^>]*\/><span class="test-row"><button[^>]*id="voice-clear-stt"/);
  });

  it("the switch is the standard switch row", () => {
    expect(render({}, BUILTIN)).toMatch(
      /<div class="set-row set-switch-row" id="voice-approve-row"><span class="notif-rule-text"><span class="set-label" id="voice-approve-label">[\s\S]*?<label class="ca-switch"><input type="checkbox" id="voice-approve"/
    );
  });

  it("the installer and the Try-it buttons are General's action rows, their status in the hint", () => {
    const BASIC: VoiceView = { ...BUILTIN, system: { ...SYSTEM, stt_quality: "basic" } };
    const html = render({}, BASIC, { supported: true, status: { state: "failed", error: "pip exited 1" }, touched: true, onInstall: () => {} });
    expect(html).toMatch(
      /<div class="set-row onboarding-action" id="voice-install" data-offer="basic"><div class="onboarding-action-text"><span class="set-label">Local models<\/span><span class="set-hint" id="voice-install-why">/
    );
    expect(html).toMatch(/<span class="set-hint" id="voice-install-status" role="status"><span class="mobile-row-glyph st-fail"[^>]*>✗ <\/span>Couldn/);
    expect(html).toMatch(/<\/div><button type="button" class="test-btn" id="voice-install-btn"/);
    for (const k of ["say", "hear"])
      expect(html, k).toMatch(
        new RegExp(`<div class="set-row onboarding-action" data-try="${k}"><div class="onboarding-action-text"><span class="set-label">[^<]+</span><span class="set-hint" id="voice-try-${k}-hint">[^<]+</span></div><button[^>]*id="voice-try-${k}"`)
      );
    // The readiness lines carry the same glyph.
    expect(html).toMatch(/<p class="set-hint" id="voice-ready-stt"><span class="mobile-row-glyph st-ok" aria-hidden="true">✓ <\/span>Server recognition ready/);
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

describe("the local-models installer", () => {
  /** Windows' basic recognizer, on the built-in engine (the new server key). */
  const BASIC: VoiceView = {
    ...BUILTIN,
    server_ready: {
      ...BUILTIN.server_ready,
      stt_detail: "Windows speech recognizer en-US, basic accuracy — install local models for better recognition",
    },
    system: { ...SYSTEM, stt_quality: "basic" },
  };
  const MAC: VoiceView = { ...BUILTIN, system: { ...SYSTEM, platform: "mac", stt_quality: "good" } };
  /** Local models picked, not installed. */
  const LOCAL_MISSING: VoiceView = {
    ...BUILTIN,
    server_ready: { ...BUILTIN.server_ready, stt: false, stt_backend: "", stt_detail: "local models not installed: uv sync --group voice" },
  };
  const LOCAL_READY: VoiceView = {
    ...BUILTIN,
    server_ready: { ...BUILTIN.server_ready, stt: true, stt_backend: "local", stt_detail: "faster-whisper small" },
    system: { ...SYSTEM, stt_quality: "basic" },
  };
  const inst = (status: InstallStatus = { state: "idle" }, more: Partial<InstallProps> = {}): InstallProps => ({
    supported: true,
    status,
    touched: false,
    onInstall: () => {},
    ...more,
  });
  const esc = (t: string) => t.replace(/'/g, "&#x27;");

  it("a basic recognizer is the server's word, or the built-in engine's detail on a server between the two", () => {
    expect(basicRecognizer(BASIC)).toBe(true);
    expect(basicRecognizer({ ...BASIC, system: SYSTEM })).toBe(true);
    expect(basicRecognizer(MAC)).toBe(false);
    expect(basicRecognizer(BUILTIN)).toBe(false);
    expect(basicRecognizer(null)).toBe(false);
  });

  it("is offered for a basic built-in recognizer or missing local models, never once they recognise", () => {
    expect(installOffer("system", BASIC)).toBe("basic");
    expect(installOffer("system", MAC)).toBe(null);
    expect(installOffer("openai", BASIC)).toBe(null);
    expect(installOffer("local", LOCAL_MISSING)).toBe("local");
    expect(installOffer("local", LOCAL_READY)).toBe(null);
    expect(installOffer("system", LOCAL_READY)).toBe(null);
    expect(installOffer("system", null)).toBe(null);
  });

  it("basic: the recommendation and the button under Recognition", () => {
    const html = render({}, BASIC, inst());
    expect(html).toContain(esc(INSTALL_BASIC_LINE));
    expect(html).toMatch(/data-kind="stt"[\s\S]*id="voice-install-why"[\s\S]*data-kind="tts"/);
    expect(tag(html, "voice-install-btn")).not.toContain("disabled");
    expect(html).toContain(INSTALL_LABEL);
  });

  it("local models picked but missing: the button by the readiness line, no recommendation, on any platform", () => {
    const html = render({ stt_backend: "local" }, { ...LOCAL_MISSING, system: { ...SYSTEM, platform: "mac", stt_quality: "good" } }, inst());
    expect(html).toMatch(/id="voice-ready-stt"[\s\S]*id="voice-install-btn"/);
    expect(html).not.toContain('id="voice-install-why"');
  });

  it("hidden for a good recognizer, once local models are ready, and on a server without the installer", () => {
    for (const h of [
      render({}, MAC, inst()),
      render({}, BUILTIN, inst()),
      render({ stt_backend: "local" }, LOCAL_READY, inst()),
      render({}, BASIC, inst({ state: "idle" }, { supported: false })),
      render({}, BASIC),
    ])
      expect(h).not.toContain('id="voice-install');
  });

  it("a non-owner's button is disabled with the reason", () => {
    const html = render({}, { ...BASIC, configure_allowed: false }, inst());
    expect(tag(html, "voice-install-btn")).toContain("disabled");
    expect(html).toContain(esc(INSTALL_REFUSED));
  });

  it("running: busy button, progress, the step and the last log line; done: the sentence, no button", () => {
    const running = render({}, BASIC, inst({ state: "running", step: "Downloading the recognition model (small)", log: ["a", "", "Fetching model.bin 40%"] }, { touched: true }));
    expect(tag(running, "voice-install-btn")).toContain("disabled");
    expect(running).toContain("Installing…");
    expect(running).toContain("<progress");
    expect(running).toContain("Installing: Downloading the recognition model (small)");
    expect(running).toMatch(/id="voice-install-log"[^>]*>Fetching model.bin 40%</);
    // Done, and the server switched recognition to local models: still said.
    const done = render({ stt_backend: "local" }, LOCAL_READY, inst({ state: "done" }, { touched: true }));
    expect(done).toContain(INSTALL_DONE + ".");
    expect(done).not.toContain('id="voice-install-btn"');
  });

  it("failed: the error and Try again", () => {
    const html = render({}, BASIC, inst({ state: "failed", error: "pip exited 1." }, { touched: true }));
    expect(html).toContain("Couldn&#x27;t install: pip exited 1.");
    expect(tag(html, "voice-install-btn")).not.toContain("disabled");
    expect(html).toMatch(/id="voice-install-btn"[^>]*>Try again</);
  });

  it("installView: 409, 403 and idle", () => {
    expect(installView({ state: "running", already: true }, true)).toMatchObject({ button: "busy", text: "Already installing" });
    expect(installView({ state: "failed", refused: true, error: "only the owner" }, true)).toMatchObject({
      button: "install",
      disabled: true,
      text: "only the owner.",
    });
    expect(installView({ state: "idle" }, true)).toEqual({ button: "install", disabled: false, text: "", last: "" });
  });

  describe("installLocalModels", () => {
    const noWait = vi.fn(async () => {});
    beforeEach(() => noWait.mockClear());

    /** A fake `api`: the POST's answer, then the GETs in order. */
    function server(post: () => unknown, gets: InstallStatus[]) {
      const calls: Array<[string, RequestInit | undefined]> = [];
      const call = (async (path: string, opts?: RequestInit) => {
        calls.push([path, opts]);
        if (opts?.method === "POST") return post();
        const g = gets.shift();
        if (!g) throw new Error("no more answers");
        return g;
      }) as <T>(path: string, opts?: RequestInit) => Promise<T>;
      return { call, calls };
    }

    it("POSTs, polls every 2 s while running, ends on done", async () => {
      const { call, calls } = server(
        () => ({ state: "running" }),
        [
          { state: "running", step: "Installing packages", log: ["Collecting faster-whisper"] },
          { state: "running", step: "Downloading the recognition model (small)", log: ["x"] },
          { state: "done", step: "Done", log: [] },
        ]
      );
      const seen: InstallStatus[] = [];
      const end = await installLocalModels((s) => seen.push(s), { call, wait: noWait });
      expect(calls[0]).toEqual(["/api/voice/install-local", { method: "POST" }]);
      expect(calls.slice(1).map((c) => c[0])).toEqual(Array(3).fill("/api/voice/install-local"));
      expect(calls.slice(1).every((c) => !c[1]?.method)).toBe(true);
      expect(noWait).toHaveBeenCalledTimes(3);
      expect(noWait).toHaveBeenCalledWith(2000);
      expect(seen.map((s) => s.state)).toEqual(["running", "running", "running", "done"]);
      expect(seen[1].step).toBe("Installing packages");
      expect(end.state).toBe("done");
      expect(installView(end, true).text).toBe(INSTALL_DONE + ".");
    });

    it("a failed install ends with the server's error", async () => {
      const { call } = server(() => ({}), [{ state: "failed", error: "No space left on device", log: [] }]);
      const end = await installLocalModels(() => {}, { call, wait: noWait });
      expect(installView(end, true)).toMatchObject({ button: "retry", text: "Couldn't install: No space left on device." });
    });

    it("409: says it is already installing and follows that install", async () => {
      const { call } = server(
        () => {
          throw Object.assign(new Error("an install is already running"), { status: 409 });
        },
        [{ state: "running", step: "Downloading the piper voice" }, { state: "done" }]
      );
      const seen: InstallStatus[] = [];
      const end = await installLocalModels((s) => seen.push(s), { call, wait: noWait });
      expect(installView(seen[0], true).text).toBe("Already installing");
      expect(installView(seen[1], true).text).toBe("Already installing: Downloading the piper voice");
      expect(end.state).toBe("done");
    });

    it("403: the owner check's sentence, no polling", async () => {
      const { call, calls } = server(() => {
        throw Object.assign(new Error("make the change on that computer itself"), { status: 403 });
      }, []);
      const end = await installLocalModels(() => {}, { call, wait: noWait });
      expect(calls).toHaveLength(1);
      expect(end).toMatchObject({ state: "failed", refused: true });
      expect(installView(end, true)).toMatchObject({ disabled: true, text: "make the change on that computer itself." });
    });

    it("three unanswered polls in a row end it as failed; a closed screen stops polling", async () => {
      const { call } = server(() => ({}), []);
      const end = await installLocalModels(() => {}, { call, wait: noWait });
      expect(end.state).toBe("failed");
      expect(end.error).toMatch(/^lost track of the install/);
      expect(noWait).toHaveBeenCalledTimes(3);

      noWait.mockClear();
      const two = server(() => ({}), [{ state: "running" }, { state: "running" }]);
      const seen: InstallStatus[] = [];
      const stopped = await installLocalModels((s) => seen.push(s), { call: two.call, wait: noWait, alive: () => false });
      expect(stopped.state).toBe("running");
      expect(two.calls).toHaveLength(1);
    });
  });
});


describe("this computer's mic (the popover's prefs, in Settings too)", () => {
  it("has the Listen for “MindFlock” switch with its hint — off by default, on when the pref says so", async () => {
    const html = render({}, BUILTIN);
    const row = /<div class="set-row set-switch-row" id="voice-wake-row">[\s\S]*?<\/label><\/div>/.exec(html)?.[0] || "";
    expect(row).toContain('<span class="set-label" id="voice-wake-label">Listen for “MindFlock”</span>');
    expect(row).toContain("the browser&#x27;s mic light stays on");
    expect(row).toMatch(/<input type="checkbox" id="voice-wake" aria-labelledby="voice-wake-label"\/>/);
    expect(html.indexOf('id="voice-ptt-row"')).toBeLessThan(html.indexOf('id="voice-wake-row"'));
    expect(html.indexOf('id="voice-wake-row"')).toBeLessThan(html.indexOf("Permission dialogs"));
    const { VOICE_PREFS_KEY } = await import("../lib/voiceDesktop");
    vi.stubGlobal("localStorage", { getItem: (k: string) => (k === VOICE_PREFS_KEY ? '{"wake":true}' : null), setItem: () => {} });
    try {
      expect(render({}, BUILTIN)).toMatch(/<input type="checkbox" id="voice-wake" aria-labelledby="voice-wake-label" checked=""\/>/);
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("shows Read replies aloud (Off by default, three ways) and the push-to-talk choice before Permission dialogs", () => {
    const html = render({}, BUILTIN);
    expect(html).toContain('<h3 class="set-section-title">This computer&#x27;s mic</h3>');
    expect(html.indexOf("This computer&#x27;s mic")).toBeLessThan(html.indexOf("Permission dialogs"));
    expect(html).toMatch(
      /<div class="set-row" id="voice-replies-row"><span class="notif-rule-text"><span class="set-label" id="voice-replies-label">Read replies aloud<\/span>/,
    );
    expect(html).toMatch(/id="voice-replies" role="radiogroup" aria-labelledby="voice-replies-label"/);
    const radios = html.match(/<input type="radio" name="voice-replies"[^>]*>/g) || [];
    expect(radios.map((r) => /value="(\w+)"/.exec(r)?.[1])).toEqual(["off", "mine", "all"]);
    expect(radios.map((r) => /checked=""/.test(r))).toEqual([true, false, false]);
    expect(html).toContain("Replies to what I said by voice");
    expect(html).toContain("Everything, every session");
    expect(html).toContain('id="voice-ptt-row"');
    expect(html).toContain("Click to talk");
    expect(html).toContain("Hold to talk");
    // The popover and this screen read the same prefs module (and choices).
    expect(voiceSrc).toContain("loadVoicePrefs, onVoicePrefs, saveVoicePrefs, REPLIES_CHOICES");
    expect(micSrc).toMatch(/REPLIES_CHOICES\.map\(\(c\) =>[\s\S]*?name="mic-replies"/);
  });
});

describe("Try it → Recognition offers the install when missing local models are what stops it", () => {
  const MISSING: VoiceView = {
    ...BUILTIN,
    server_ready: {
      ...BUILTIN.server_ready,
      stt: false,
      stt_backend: "",
      stt_detail: "local models not installed: Settings → Voice installs them",
    },
  };
  const READY: VoiceView = {
    ...BUILTIN,
    server_ready: { ...BUILTIN.server_ready, stt: true, stt_backend: "local", stt_detail: "faster-whisper small" },
  };
  const inst = (status: InstallStatus = { state: "idle" }, more: Partial<InstallProps> = {}): InstallProps => ({
    supported: true,
    status,
    touched: false,
    onInstall: () => {},
    ...more,
  });
  const hearRow = (html: string) => html.slice(html.indexOf('data-try="hear"'));
  const esc = (t: string) => t.replace(/'/g, "&#x27;");

  it("reads the server's verdict", () => {
    expect(localModelsMissing(MISSING)).toBe(true);
    expect(localModelsMissing(READY)).toBe(false);
    expect(localModelsMissing(BUILTIN)).toBe(false);
    expect(localModelsMissing(null)).toBe(false);
    expect(tryInstallFix(MISSING, inst())).toBe(true);
    expect(tryInstallFix(MISSING, inst({ state: "idle" }, { supported: false }))).toBe(false);
    expect(tryInstallFix(MISSING, undefined)).toBe(false);
    expect(tryInstallFix(READY, inst())).toBe(false);
    // Running (a click here, or the server putting the packages back after an update): shown wherever it is.
    expect(tryInstallFix(BUILTIN, inst({ state: "running", step: "x" }))).toBe(true);
    // A failure this page saw stays in the row with Try again; a stale one from before does not.
    expect(tryInstallFix({ ...BUILTIN, server_ready: { ...BUILTIN.server_ready, stt: false, stt_detail: "x" } }, inst({ state: "failed", error: "e" }, { touched: true }))).toBe(true);
    expect(tryInstallFix({ ...BUILTIN, server_ready: { ...BUILTIN.server_ready, stt: false, stt_detail: "x" } }, inst({ state: "failed", error: "e" }))).toBe(false);
  });

  it("the Recognition row carries the install button, not a disabled Try button with a terminal command", () => {
    const html = hearRow(render({ stt_backend: "local" }, MISSING, inst()));
    expect(html).toContain('id="voice-try-install-btn"');
    expect(tag(html, "voice-try-install-btn")).not.toContain("disabled");
    expect(html).toContain(esc(INSTALL_LABEL));
    expect(html).toContain(esc(TRY_INSTALL_LINE));
    expect(html).not.toContain("Try recognition");
    expect(html).not.toContain("No recognition is ready on the server");
    expect(html).not.toContain("uv sync");
  });

  it("without the installer (an older server) the plain disabled Try button and the server's sentence", () => {
    for (const html of [hearRow(render({ stt_backend: "local" }, MISSING)), hearRow(render({ stt_backend: "local" }, MISSING, inst({ state: "idle" }, { supported: false })))]) {
      expect(html).not.toContain("voice-try-install");
      expect(tag(html, "voice-try-hear")).toContain("disabled");
      expect(html).toContain("No recognition is ready on the server: local models not installed: Settings → Voice installs them.");
    }
  });

  it("running: the step, the progress bar and the busy button in that row; a non-owner's button disabled with why", () => {
    const html = hearRow(render({ stt_backend: "local" }, MISSING, inst({ state: "running", step: "reinstalling the local models' packages after the update", log: ["Fetching…"] }, { touched: true })));
    expect(tag(html, "voice-try-install-btn")).toContain("disabled");
    expect(html).toContain("Installing: reinstalling the local models");
    expect(html).toContain("voice-install-progress");
    expect(html).toContain('id="voice-try-install-log"');
    const other = hearRow(render({ stt_backend: "local" }, { ...MISSING, configure_allowed: false }, inst()));
    expect(tag(other, "voice-try-install-btn")).toContain("disabled");
    expect(other).toContain(esc(INSTALL_REFUSED));
  });

  it("once local models are ready the Try button is back", () => {
    const html = hearRow(render({ stt_backend: "local" }, READY, inst({ state: "done" }, { touched: true })));
    expect(html).not.toContain("voice-try-install");
    expect(html).toContain('id="voice-try-hear"');
    // (Disabled here only because node has no microphone.)
    expect(tag(html, "voice-try-hear")).toContain("can&#x27;t record");
  });
});
