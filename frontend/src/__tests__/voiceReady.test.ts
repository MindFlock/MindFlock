/** The mic is not red until recognition answers (lib/voiceDesktop
 * makeSttReady / readyWanted / micPill `readying`, LiveDictation.wait, and
 * TopBarMic's awaitReady): "loaded" was not "ready" on a machine short of
 * memory, so the mic said talk while the first words waited 10–40 s. */
import { afterEach, describe, expect, it, vi } from "vitest";
import { code, css as cssOf } from "./sourceText";

const mocks = vi.hoisted(() => ({ apiRaw: vi.fn(), api: vi.fn(async () => ({})) }));
vi.mock("../api/client", () => ({ api: mocks.api, apiRaw: mocks.apiRaw, instApi: vi.fn(), ApiError: Error }));

import {
  DEFAULT_VOICE_CFG,
  LiveDictation,
  READYING_LINE,
  READYING_PILL,
  READY_EARLY_LINE,
  READY_QUICK_MS,
  makeSttReady,
  micPill,
  notReadyLine,
  readyWanted,
  type LiveHooks,
  type VoiceCfg,
} from "../lib/voiceDesktop";

const reply = (status: number, body: unknown) => ({ status, ok: status >= 200 && status < 300, json: async () => body });

afterEach(() => mocks.apiRaw.mockReset());

describe("which listens wait", () => {
  const cfg = (stt: VoiceCfg["stt"], backend: string): VoiceCfg => ({ ...DEFAULT_VOICE_CFG, stt, server_ready: { stt_backend: backend } });
  it("only the server's local recognition", () => {
    expect(readyWanted(cfg("server", "local"))).toBe(true);
    expect(readyWanted(cfg("server", "openai"))).toBe(false);
    expect(readyWanted(cfg("server", "system"))).toBe(false);
    expect(readyWanted(cfg("phone", "local"))).toBe(false);
    expect(readyWanted(DEFAULT_VOICE_CFG)).toBe(false);
  });
});

describe("asking the server", () => {
  it("ready, hot or after a pass", async () => {
    mocks.apiRaw.mockResolvedValueOnce(reply(200, { ready: true, hot: true, ms: 0 }));
    expect(await makeSttReady()).toMatchObject({ ok: true, hot: true, error: "" });
    mocks.apiRaw.mockResolvedValueOnce(reply(200, { ready: true, hot: false, ms: 11000 }));
    expect(await makeSttReady()).toMatchObject({ ok: true, hot: false });
    expect(mocks.apiRaw).toHaveBeenCalledWith("/api/voice/ready", { method: "POST" });
  });
  it("not ready says why", async () => {
    mocks.apiRaw.mockResolvedValueOnce(reply(503, { ready: false, error: "faster-whisper failed: no memory" }));
    const r = await makeSttReady();
    expect(r).toMatchObject({ ok: false, error: "faster-whisper failed: no memory" });
    expect(notReadyLine(r.error)).toBe("Recognition is not ready: faster-whisper failed: no memory");
    mocks.apiRaw.mockResolvedValueOnce(reply(500, {}));
    expect((await makeSttReady()).error).toBe("HTTP 500");
    expect(notReadyLine("")).toBe("Recognition is not ready");
  });
  it("a server that never answers is not ready", async () => {
    mocks.apiRaw.mockRejectedValueOnce(new Error("Failed to fetch"));
    expect(await makeSttReady()).toMatchObject({ ok: false, error: "Failed to fetch" });
  });
  it("an older server has no such call: ready, as before", async () => {
    mocks.apiRaw.mockResolvedValueOnce(reply(404, { detail: "Not Found" }));
    expect(await makeSttReady()).toMatchObject({ ok: true });
    mocks.apiRaw.mockResolvedValueOnce(reply(405, {}));
    expect(await makeSttReady()).toMatchObject({ ok: true });
  });
});

describe("what the mic says meanwhile", () => {
  it("the pill is Getting ready…, never Listening…", () => {
    expect(micPill({ listening: true, speaking: false, quiet: false, readying: true })).toBe(READYING_PILL);
    expect(micPill({ listening: true, speaking: false, quiet: false, readying: true, warming: true })).toBe(READYING_PILL);
    expect(micPill({ listening: true, speaking: false, quiet: false, readying: false })).toBe("Listening…");
    expect(micPill({ listening: false, speaking: false, quiet: false, readying: true })).toBe("");
    expect(READYING_PILL).not.toMatch(/listen/i);
    expect(READYING_LINE).toContain("wait");
    expect(READY_EARLY_LINE).toContain("still getting ready");
    expect(READY_QUICK_MS).toBeLessThanOrEqual(300);
  });

  it("the caption holds the wait line until ready, then is the listen's own", () => {
    const captions: string[] = [];
    const h: LiveHooks = {
      caption: (t) => void captions.push(t),
      halt: () => {},
      route: async () => false,
      hush: () => {},
      activity: () => "idle",
      replies: () => false,
      submitted: () => {},
    };
    const d = new LiveDictation(null, h);
    d.show();
    const before = captions[captions.length - 1];
    expect(before).toContain("Listening");
    d.wait(READYING_LINE);
    expect(captions[captions.length - 1]).toBe(READYING_LINE);
    // The level meter hearing something must not say "Hearing you…" yet.
    d.ready();
    d.loud(true);
    d.show();
    expect(captions[captions.length - 1]).toBe(READYING_LINE);
    d.waited();
    expect(captions[captions.length - 1]).not.toBe(READYING_LINE);
    // Not ready: the listen ends with the line, never silently.
    const e = new LiveDictation(null, h);
    e.wait(READYING_LINE);
    e.fail(notReadyLine("no memory"));
    expect(captions[captions.length - 1]).toBe("Recognition is not ready: no memory");
    expect(e.active).toBe(false);
  });
});

describe("the top bar's mic", () => {
  const tsx = code("components/TopBarMic.tsx");
  it("is red only when not getting ready", () => {
    expect(tsx).toContain('listening && !readying && "listening"');
    expect(tsx).toContain('listening && readying && "readying"');
    const css = cssOf("components/TopBarMic.css");
    const rule = css.slice(css.indexOf(".mic-ring.readying::after"));
    expect(rule.slice(0, rule.indexOf("}"))).toContain("var(--muted)");
    expect(rule.slice(0, rule.indexOf("}"))).not.toContain("--red");
    // The red icon is the .listening class's alone.
    expect(css).toContain("#topbar #mic-btn.listening");
    expect(css).not.toContain("#mic-btn.readying {");
  });
  it("waits on every start of a local server listen, and never treats the wait as silence", () => {
    expect(tsx).toContain("if (r === serverRec && readyWanted(c)) awaitReady(d);");
    expect(tsx).toContain("if (readyFor.current) return armIdle();");
    expect(tsx).toContain("d.fail(READY_EARLY_LINE)");
    expect(tsx).toContain("d.fail(notReadyLine(res.error))");
    expect(tsx).toContain("if (readyFor.current === d) timer = setTimeout(giveUp, ms);");
  });
});
