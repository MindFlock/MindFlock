/** Voice Lab off when it isn't used (the owner, 2026-11-04: "I want Voice Lab
 * off when I'm not using it"). A local voice host stops after 10 idle
 * minutes, so nothing but speech may reach it:
 *  - dictation never readies the server's voice (recognition runs in the
 *    server itself): the mic's press does only when something will be SAID
 *    with it, its menu only while voice is in use (speaksOnServer);
 *  - Settings → Voice reads the verdict with `?warm=0` and says a local host
 *    "starts when you use voice" instead of asking it;
 *  - the first line after the voice went idle may take a while (the host
 *    starts and loads its model): past a second the pill says
 *    "Starting voice…" (serverSpeaker's onStarting). */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  DEFAULT_VOICE_CFG,
  DEFAULT_VOICE_PREFS,
  STARTING_VOICE_LINE,
  VOICE_COLD_MS,
  VOICE_STARTING_MS,
  micPill,
  serverSpeaker,
  speaksOnServer,
  voiceMayBeCold,
  type Speaker,
  type VoiceCfg,
  type VoicePrefs,
} from "../lib/voiceDesktop";
import { VOICE_HOST_ON_USE, readinessLine } from "../components/settings/screens/Voice";
import { code } from "./sourceText";

const mocks = vi.hoisted(() => ({ apiRaw: vi.fn() }));
vi.mock("../api/client", () => ({ api: vi.fn(), apiRaw: mocks.apiRaw, instApi: vi.fn(), ApiError: Error }));
vi.mock("../lib/terminals", () => ({ peekTerm: vi.fn() }));
vi.mock("../lib/wavRecorder", () => ({ startWavRecording: vi.fn(), resamplerName: () => "browser resampler" }));

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  mocks.apiRaw.mockReset();
});

const HOST: VoiceCfg = {
  ...DEFAULT_VOICE_CFG,
  tts: "server",
  server_ready: { tts: true, tts_backend: "openai", tts_detail: "tts-1 (Donna) at 127.0.0.1:8770" },
};
const prefs = (p: Partial<VoicePrefs>): VoicePrefs => ({ ...DEFAULT_VOICE_PREFS, ...p });

describe("only speech readies the server's voice", () => {
  it("dictation alone (replies off, no confirmations, no standby) never does", () => {
    expect(speaksOnServer(prefs({}), HOST, true)).toBe(false);
    expect(speaksOnServer(prefs({}), HOST, false)).toBe(false);
  });

  it("a reply read out, a spoken confirmation, standby's spoken switch, or Read everything does", () => {
    expect(speaksOnServer(prefs({ replies: "mine" }), HOST, true)).toBe(true);
    expect(speaksOnServer(prefs({ replies: "all" }), HOST, true)).toBe(true);
    expect(speaksOnServer(prefs({ confirm: true }), HOST, true)).toBe(true);
    expect(speaksOnServer(prefs({ wake: true }), HOST, true)).toBe(true);
    expect(speaksOnServer(prefs({}), { ...HOST, say_all: true }, true)).toBe(true);
  });

  it("not when the browser's own voice speaks (no server voice ready, or a browser tab on phone voice)", () => {
    expect(speaksOnServer(prefs({ replies: "all" }), { ...HOST, server_ready: {} }, true)).toBe(false);
    expect(speaksOnServer(prefs({ replies: "all" }), { ...HOST, tts: "phone" }, false)).toBe(false);
  });

  it("the mic's press asks with speaksOnServer, its menu only while voice is in use", () => {
    const mic = code("components/TopBarMic.tsx");
    expect(mic).toContain("if (speaksOnServer(prefsRef.current, cfgNow)) void refreshCfg(true);");
    expect(mic).not.toContain('if (cfgNow.tts === "server") void refreshCfg(true);');
    const menu = mic.slice(mic.indexOf("const openMenu = () => {"));
    expect(menu.slice(0, menu.indexOf("};"))).toContain("void refreshCfg();");
  });
});

describe("Settings → Voice never wakes the voice", () => {
  it("reads the verdict with warm=0", () => {
    const screen = code("components/settings/screens/Voice.tsx");
    expect(screen).toContain('api<VoiceView>("/api/voice?warm=0")');
    expect(screen).not.toContain('api<VoiceView>("/api/voice")');
  });

  it("a local voice host is reported as starting on use, not asked", () => {
    const view = { server_ready: HOST.server_ready };
    expect(readinessLine("tts", view, "server").text).toBe(
      "Server voice ready (openai): tts-1 (Donna) at 127.0.0.1:8770 — " + VOICE_HOST_ON_USE + "."
    );
    // A paid remote host and piper get no such note.
    const remote = { server_ready: { tts: true, tts_backend: "openai", tts_detail: "gpt-4o-mini-tts (alloy) at api.openai.com" } };
    expect(readinessLine("tts", remote, "server").text).not.toContain(VOICE_HOST_ON_USE);
    const piper = { server_ready: { tts: true, tts_backend: "local", tts_detail: "piper en_US-lessac-medium" } };
    expect(readinessLine("tts", piper, "server").text).not.toContain(VOICE_HOST_ON_USE);
  });
});

describe("the first line after the voice went idle", () => {
  it("cold: nothing said yet, or the last line VOICE_COLD_MS ago", () => {
    expect(voiceMayBeCold(0, 5)).toBe(true);
    expect(voiceMayBeCold(1000, 1000 + VOICE_COLD_MS - 1)).toBe(false);
    expect(voiceMayBeCold(1000, 1000 + VOICE_COLD_MS)).toBe(true);
  });

  it("the pill says Starting voice… only while a line is being said", () => {
    expect(micPill({ listening: false, speaking: true, quiet: false, starting: true })).toBe(STARTING_VOICE_LINE);
    expect(micPill({ listening: false, speaking: true, quiet: false, starting: false })).toBe("Speaking…");
    expect(micPill({ listening: true, speaking: true, quiet: false, starting: true })).toBe("Listening…");
    expect(micPill({ listening: false, speaking: false, quiet: false, starting: true })).toBe("");
  });

  describe("serverSpeaker's onStarting", () => {
    let synthMs = 0;
    class Ctx {
      state = "running";
      destination = {};
      currentTime = 0;
      resume() {
        return Promise.resolve();
      }
      decodeAudioData() {
        return Promise.resolve({ duration: 0.01 });
      }
      createBufferSource() {
        const node = {
          buffer: null,
          onended: null as null | (() => void),
          connect() {},
          start() {
            setTimeout(() => node.onended?.(), 10);
          },
          stop() {},
        };
        return node;
      }
    }
    beforeEach(() => {
      vi.useFakeTimers();
      vi.stubGlobal("window", { AudioContext: Ctx });
      mocks.apiRaw.mockImplementation(
        () =>
          new Promise((resolve) =>
            setTimeout(() => resolve({ ok: true, status: 200, arrayBuffer: async () => new ArrayBuffer(4) }), synthMs)
          )
      );
    });
    const quiet: Speaker = { speak: async () => {}, cancel: () => {} };

    it("a cold voice past a second says it is starting until the line comes back; a warm one never", async () => {
      const seen: Array<[boolean, number]> = [];
      const t0 = Date.now();
      const sp = serverSpeaker(quiet, { onStarting: (on) => void seen.push([on, Date.now() - t0]) });
      synthMs = 30_000; // Voice Lab starting and loading F5
      void sp.speak("Hello there.");
      await vi.advanceTimersByTimeAsync(VOICE_STARTING_MS - 1);
      expect(seen).toEqual([]);
      await vi.advanceTimersByTimeAsync(1);
      expect(seen).toEqual([[true, VOICE_STARTING_MS]]);
      await vi.advanceTimersByTimeAsync(30_000);
      expect(seen).toEqual([
        [true, VOICE_STARTING_MS],
        [false, 30_000],
      ]);
      // Warm now: a slow line is just a slow line.
      seen.length = 0;
      synthMs = 3000;
      void sp.speak("Again, please.");
      await vi.advanceTimersByTimeAsync(5000);
      expect(seen).toEqual([]);
      // Idle past the host's stop: cold again.
      await vi.advanceTimersByTimeAsync(VOICE_COLD_MS);
      void sp.speak("After a while.");
      await vi.advanceTimersByTimeAsync(5000);
      expect(seen.map(([on]) => on)).toEqual([true, false]);
    });

    it("a quick answer shows nothing; cancel clears a shown note", async () => {
      const seen: boolean[] = [];
      const sp = serverSpeaker(quiet, { onStarting: (on) => void seen.push(on) });
      synthMs = 400;
      void sp.speak("Quick one.");
      await vi.advanceTimersByTimeAsync(2000);
      expect(seen).toEqual([]);
      const sp2 = serverSpeaker(quiet, { onStarting: (on) => void seen.push(on) });
      synthMs = 30_000;
      void sp2.speak("Slow one.");
      await vi.advanceTimersByTimeAsync(1500);
      expect(seen).toEqual([true]);
      sp2.cancel();
      expect(seen).toEqual([true, false]);
    });
  });
});
