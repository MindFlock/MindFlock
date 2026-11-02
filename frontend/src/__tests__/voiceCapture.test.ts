/** The desktop mic's capture off the main thread (voice cycle 4b): the
 * AudioWorklet's resampler and blocks (lib/voiceCapture.worklet), the one
 * capture graph MicKeeper keeps (lib/voiceMic: pre-roll + recorder + meter),
 * its fallbacks, and the server recognizer recording through it. */
import { afterEach, describe, expect, it, vi } from "vitest";
import { CAPTURE_BLOCK_FRAMES, CAPTURE_PROCESSOR, CAPTURE_RATE, CaptureCore, StreamResampler, type CaptureBlock } from "../lib/voiceCapture.worklet";
import { CAPTURE_WAIT_MS, MicCapture, MicKeeper, PRE_ROLL_MS } from "../lib/voiceMic";
import { serverRecognizer, voiceAudioCtx, type RecognizerHandlers } from "../lib/voiceDesktop";

const mocks = vi.hoisted(() => ({ apiRaw: vi.fn(), startWavRecording: vi.fn() }));
vi.mock("../api/client", () => ({ api: vi.fn(), apiRaw: mocks.apiRaw, instApi: vi.fn(), ApiError: Error }));
vi.mock("../lib/terminals", () => ({ peekTerm: vi.fn() }));
vi.mock("../lib/wavRecorder", async (orig) => ({ ...(await orig<typeof import("../lib/wavRecorder")>()), startWavRecording: mocks.startWavRecording }));

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  mocks.apiRaw.mockReset();
  mocks.startWavRecording.mockReset();
  WNode.all = [];
});

// --- the resampler and the blocks (the audio thread's logic) ----------------- //

const tone = (n: number, rate: number, hz: number, amp = 0.5, phase = 0) =>
  Float32Array.from({ length: n }, (_, i) => amp * Math.sin((2 * Math.PI * hz * i) / rate + phase));
const mix = (n: number, rate: number) => {
  const a = tone(n, rate, 440, 0.4);
  const b = tone(n, rate, 2500, 0.2, 1);
  return a.map((v, i) => v + b[i]);
};
/** Feed `x` in render quanta of 128 (the worklet's), joined. */
function streamed(rs: StreamResampler, x: Float32Array, q = 128): Float32Array {
  const parts: Float32Array[] = [];
  for (let i = 0; i < x.length; i += q) parts.push(rs.push(x.subarray(i, i + q)));
  const out = new Float32Array(parts.reduce((n, p) => n + p.length, 0));
  let at = 0;
  for (const p of parts) {
    out.set(p, at);
    at += p.length;
  }
  return out;
}
const rmsOf = (x: Float32Array, from = 0, to = x.length) => {
  let s = 0;
  for (let i = from; i < to; i++) s += x[i] * x[i];
  return Math.sqrt(s / (to - from));
};

describe("the capture worklet's resampler (StreamResampler)", () => {
  it("in render quanta it gives what wavRecorder's one-shot windowed sinc gives (48 kHz and 44.1 kHz → 16 kHz)", async () => {
    const { resample } = await vi.importActual<typeof import("../lib/wavRecorder")>("../lib/wavRecorder");
    for (const rate of [48_000, 44_100]) {
      const x = mix(rate, rate); // one second
      const want = resample(x, rate, CAPTURE_RATE);
      const got = streamed(new StreamResampler(rate, CAPTURE_RATE), x);
      // It lags by its filter's half (≈ 1 ms): all but the last few are out.
      expect(want.length - got.length).toBeGreaterThanOrEqual(0);
      expect(want.length - got.length).toBeLessThan(20);
      let worst = 0;
      for (let i = 20; i < got.length; i++) worst = Math.max(worst, Math.abs(got[i] - want[i]));
      expect([rate, worst < 1e-4]).toEqual([rate, true]);
    }
  });

  it("keeps the speech band and filters out what would fold back into it", () => {
    const rs = new StreamResampler(48_000, CAPTURE_RATE);
    const kept = streamed(rs, tone(48_000, 48_000, 1000, 0.5));
    expect(rmsOf(kept, 400)).toBeCloseTo(0.5 / Math.SQRT2, 2);
    // 12 kHz at 48 kHz: over the new Nyquist (8 kHz) — unfiltered it lands at 4 kHz.
    const gone = streamed(new StreamResampler(48_000, CAPTURE_RATE), tone(48_000, 48_000, 12_000, 0.5));
    expect(rmsOf(gone, 400)).toBeLessThan(0.005);
  });

  it("the same rate passes through", () => {
    const x = mix(1000, 16_000);
    expect(Array.from(streamed(new StreamResampler(16_000, 16_000), x))).toEqual(Array.from(x));
  });
});

describe("the capture worklet's blocks (CaptureCore)", () => {
  it("one block per 4096 input frames whatever the input's pieces, with its RMS and the frames counted", () => {
    for (const q of [128, 1000, 4096, 10_240]) {
      const got: CaptureBlock[] = [];
      const core = new CaptureCore(48_000, (b) => got.push(b));
      const x = new Float32Array(CAPTURE_BLOCK_FRAMES * 5).fill(0.25);
      for (let i = 0; i < x.length; i += q) core.push(x.subarray(i, i + q));
      expect(got.map((b) => b.frames)).toEqual([1, 2, 3, 4, 5].map((k) => k * CAPTURE_BLOCK_FRAMES));
      for (const b of got) expect(b.rms).toBeCloseTo(0.25, 6);
      // 16 kHz out: a third of the frames, less the filter's lag.
      const out = got.reduce((n, b) => n + b.pcm.length, 0);
      expect(Math.abs(out - (5 * CAPTURE_BLOCK_FRAMES) / 3)).toBeLessThan(20);
      expect(got[3].pcm[100]).toBeCloseTo(0.25, 5); // a constant stays constant
    }
  });

  it("registers no processor outside an AudioWorkletGlobalScope (the page imports it for its classes)", async () => {
    const reg = vi.fn();
    vi.stubGlobal("registerProcessor", reg);
    // AudioWorkletProcessor missing: the page's own globals never register.
    vi.resetModules();
    await import("../lib/voiceCapture.worklet");
    expect(reg).not.toHaveBeenCalled();
  });

  it("in an AudioWorkletGlobalScope it registers the processor, which posts each block over its port (its buffer transferred)", async () => {
    const reg = vi.fn();
    class Proc {
      port = { postMessage: vi.fn(), onmessage: null as ((e: { data: unknown }) => void) | null };
    }
    vi.stubGlobal("registerProcessor", reg);
    vi.stubGlobal("AudioWorkletProcessor", Proc);
    vi.stubGlobal("sampleRate", 48_000);
    vi.resetModules();
    await import("../lib/voiceCapture.worklet");
    expect(reg).toHaveBeenCalledWith(CAPTURE_PROCESSOR, expect.any(Function));
    const Ctor = reg.mock.calls[0][1] as new (o: unknown) => Proc & { process(i: Float32Array[][]): boolean };
    const p = new Ctor({ processorOptions: {} });
    for (let i = 0; i < CAPTURE_BLOCK_FRAMES / 128; i++) expect(p.process([[new Float32Array(128).fill(0.1)]])).toBe(true);
    expect(p.port.postMessage).toHaveBeenCalledTimes(1);
    const [msg, transfer] = p.port.postMessage.mock.calls[0];
    expect(msg.frames).toBe(CAPTURE_BLOCK_FRAMES);
    expect(transfer).toEqual([msg.pcm.buffer]);
    expect(p.process([[]])).toBe(true); // no input yet: nothing, alive
    p.port.onmessage!({ data: "stop" });
    expect(p.process([[new Float32Array(128)]])).toBe(false);
  });
});

// --- the capture graph (lib/voiceMic) ----------------------------------------- //

/** An AudioWorkletNode: its port's messages are the blocks a test hands over. */
class WNode {
  static all: WNode[] = [];
  port = { onmessage: null as ((e: { data: CaptureBlock }) => void) | null, postMessage: vi.fn() };
  disconnect = vi.fn();
  connect = vi.fn();
  constructor(
    public ctx: unknown,
    public name: string,
    public opts: Record<string, unknown>
  ) {
    WNode.all.push(this);
  }
  get live() {
    return !!this.port.onmessage;
  }
}

/** An AudioContext with an AudioWorklet (`load`: how its addModule answers). */
class WorkletCtx {
  state: AudioContextState = "running";
  sampleRate = 48_000;
  destination = {};
  modules: string[] = [];
  procs = 0;
  load: () => Promise<void> = () => Promise.resolve();
  audioWorklet = {
    addModule: (url: string) => {
      this.modules.push(url);
      return this.load();
    },
  };
  frames = 0;
  resume() {
    return Promise.resolve();
  }
  createMediaStreamSource() {
    return { connect: vi.fn(), disconnect: vi.fn() };
  }
  createGain() {
    return { gain: { value: 1 }, connect: vi.fn(), disconnect: vi.fn() };
  }
  createScriptProcessor(size: number) {
    this.procs++;
    return { onaudioprocess: null, size, connect: vi.fn(), disconnect: vi.fn() };
  }
  createAnalyser() {
    return { fftSize: 2048, connect: vi.fn(), disconnect: vi.fn(), getFloatTimeDomainData: (b: Float32Array) => b.fill(0) };
  }
  /** One block from the audio thread into every live capture: `n` 16 kHz
   * samples of `value` (an 85 ms block at 48 kHz is ~1365). */
  feed(value: number, n = 1365) {
    this.frames += CAPTURE_BLOCK_FRAMES;
    for (const node of WNode.all.filter((x) => x.live && x.ctx === this))
      node.port.onmessage!({ data: { pcm: new Float32Array(n).fill(value), rms: Math.abs(value), frames: this.frames } });
  }
}

function fakeStream() {
  const track = { stop: vi.fn(), addEventListener: vi.fn() };
  return { active: true, getTracks: () => [track], track } as unknown as MediaStream;
}
const flush = async () => {
  for (let i = 0; i < 5; i++) await Promise.resolve();
};
const samplesOf = (wav: Blob) => (wav.size - 44) / 2;
async function pcmOf(wav: Blob): Promise<number[]> {
  const v = new DataView(await wav.arrayBuffer());
  const out: number[] = [];
  for (let at = 44; at < v.byteLength; at += 2) out.push(Math.round((v.getInt16(at, true) / 32767) * 1000) / 1000);
  return out;
}

describe("MicKeeper's one capture graph (an AudioWorklet)", () => {
  it("the stream opening loads the module once and runs ONE worklet node (no ScriptProcessor): the pre-roll is its last 500 ms at 16 kHz; release stops it", async () => {
    vi.stubGlobal("AudioWorkletNode", WNode);
    const ctx = new WorkletCtx();
    const mic = new MicKeeper(60_000, async () => fakeStream(), () => ctx as unknown as AudioContext);
    await mic.get();
    await flush();
    expect(ctx.modules).toHaveLength(1);
    expect(WNode.all).toHaveLength(1);
    expect(WNode.all[0].name).toBe(CAPTURE_PROCESSOR);
    expect(WNode.all[0].opts).toMatchObject({ channelCount: 1, channelCountMode: "explicit", processorOptions: { outRate: CAPTURE_RATE, blockFrames: CAPTURE_BLOCK_FRAMES } });
    expect(ctx.procs).toBe(0);
    for (let i = 0; i < 14; i++) ctx.feed(i === 2 ? 0.002 : 0.05);
    const pre = mic.preRoll()!;
    expect(pre.length).toBe((PRE_ROLL_MS / 1000) * CAPTURE_RATE); // 16 kHz already: no resampling here
    expect(pre[0]).toBeCloseTo(0.05);
    expect(mic.quietFloor()).toBeCloseTo(0.05); // the quiet block has left the ring
    // A recording ending cuts it: never the last phrase's tail twice.
    mic.cut();
    expect(mic.preRoll()).toBeNull();
    ctx.feed(0.01);
    expect(mic.preRoll()!.length).toBe(1365);
    // The same graph for the recording: capture() is it, at once.
    expect(mic.capture(ctx as unknown as AudioContext)).toBeInstanceOf(MicCapture);
    expect(WNode.all).toHaveLength(1);
    mic.release();
    expect(WNode.all[0].port.postMessage).toHaveBeenCalledWith("stop");
    expect(WNode.all[0].disconnect).toHaveBeenCalled();
    expect(WNode.all[0].live).toBe(false);
    expect(mic.preRoll()).toBeNull();
    // A second context on a new stream: the module loads there once too.
    await mic.get();
    await flush();
    expect(ctx.modules).toHaveLength(1); // the same context: loaded already
    expect(WNode.all).toHaveLength(2);
  });

  it("falls back to the ScriptProcessor ring when the module can't load; with no AudioWorklet capture() is null (the recorder's own ScriptProcessor)", async () => {
    vi.stubGlobal("AudioWorkletNode", WNode);
    const ctx = new WorkletCtx();
    ctx.load = () => Promise.reject(new Error("404"));
    const mic = new MicKeeper(60_000, async () => fakeStream(), () => ctx as unknown as AudioContext);
    await mic.get();
    await flush();
    expect(WNode.all).toHaveLength(0);
    expect(ctx.procs).toBe(1); // the old ring
    expect(mic.capture(ctx as unknown as AudioContext)).toBeNull();

    vi.unstubAllGlobals(); // no AudioWorkletNode: an insecure page, an old browser
    const ctx2 = new WorkletCtx();
    const mic2 = new MicKeeper(60_000, async () => fakeStream(), () => ctx2 as unknown as AudioContext);
    await mic2.get();
    expect(ctx2.modules).toHaveLength(0);
    expect(ctx2.procs).toBe(1);
    expect(mic2.capture(ctx2 as unknown as AudioContext)).toBeNull();
  });

  it("a recording waits for a slow module at most CAPTURE_WAIT_MS", async () => {
    vi.useFakeTimers();
    vi.stubGlobal("AudioWorkletNode", WNode);
    const ctx = new WorkletCtx();
    ctx.load = () => new Promise<void>(() => {});
    const mic = new MicKeeper(60_000, async () => fakeStream(), () => null);
    await mic.get();
    const p = mic.capture(ctx as unknown as AudioContext);
    expect(p).toBeInstanceOf(Promise);
    let got: unknown = "pending";
    void (p as Promise<unknown>).then((c) => (got = c));
    await vi.advanceTimersByTimeAsync(CAPTURE_WAIT_MS - 1);
    expect(got).toBe("pending");
    await vi.advanceTimersByTimeAsync(1);
    expect(got).toBeNull();
  });

  it("the microphone going away stops the graph: a recording on it gets nothing more", async () => {
    vi.stubGlobal("AudioWorkletNode", WNode);
    const ctx = new WorkletCtx();
    const st = fakeStream() as unknown as { track: { addEventListener: ReturnType<typeof vi.fn> } };
    const mic = new MicKeeper(60_000, async () => st as unknown as MediaStream, () => ctx as unknown as AudioContext);
    await mic.get();
    await flush();
    const cap = mic.capture(ctx as unknown as AudioContext) as MicCapture;
    const levels: number[] = [];
    const rec = cap.record({ onLevel: (r) => levels.push(r) });
    ctx.feed(0.1);
    const ended = st.track.addEventListener.mock.calls.find(([ev]) => ev === "ended")![1] as () => void;
    ended();
    ctx.feed(0.1);
    expect(levels).toEqual([0.1]);
    expect(samplesOf(await rec.stop())).toBe(1365);
    expect(WNode.all[0].live).toBe(false);
  });
});

describe("MicCapture.record: wavRecorder's recorder on the capture's blocks", () => {
  async function rig() {
    vi.stubGlobal("AudioWorkletNode", WNode);
    const ctx = new WorkletCtx();
    const mic = new MicKeeper(60_000, async () => fakeStream(), () => ctx as unknown as AudioContext);
    await mic.get();
    await flush();
    return { ctx, cap: mic.capture(ctx as unknown as AudioContext) as MicCapture };
  }

  it("records the blocks from now on: take() is the 16 kHz samples as they came (no resampling), onLevel per block, seconds counted", async () => {
    const { ctx, cap } = await rig();
    ctx.feed(0.9); // before the recording: not in it
    const levels: number[] = [];
    const rec = cap.record({ targetRate: 16_000, onLevel: (r) => levels.push(r) });
    expect(rec.take()).toBeNull();
    ctx.feed(0.1, 3000); // 187 ms: under take()'s 0.3 s
    expect(rec.take()).toBeNull();
    ctx.feed(0.2, 2000);
    const chunk = rec.take()!;
    expect(chunk.type).toBe("audio/wav");
    expect(samplesOf(chunk)).toBe(5000);
    const pcm = await pcmOf(chunk);
    expect([pcm[0], pcm[2999], pcm[3000], pcm[4999]]).toEqual([0.1, 0.1, 0.2, 0.2]);
    expect(levels).toEqual([0.1, 0.2]);
    ctx.feed(0.3, 800);
    expect(rec.seconds()).toBeCloseTo(5800 / 16_000, 6);
    expect(samplesOf((await rec.last(0.01))!)).toBe(160);
    const tail = await rec.stop();
    expect(samplesOf(tail)).toBe(800); // only what came after the take
    ctx.feed(0.3);
    expect(levels).toHaveLength(3); // stopped: no more
    expect(rec.take(0)).toBeNull();
  });

  it("skipS drops the start unmetered; mute drops the app's voice, and a barge-in's unmute rewinds into what was held", async () => {
    const { ctx, cap } = await rig();
    const levels: number[] = [];
    const rec = cap.record({ skipS: 0.1, onLevel: (r) => levels.push(r) });
    ctx.feed(0.5, 1600); // the skipped 100 ms
    ctx.feed(0.1, 1600);
    expect(levels).toEqual([0.1]);
    rec.mute!(true);
    ctx.feed(0.7, 1600);
    ctx.feed(0.8, 1600);
    expect(levels).toEqual([0.1]); // muted: unmetered
    rec.mute!(false, 0, 0.05); // a barge-in: keep the last 50 ms heard muted
    ctx.feed(0.2, 1600);
    const pcm = await pcmOf(rec.take(0)!);
    expect(pcm.length).toBe(1600 + 800 + 1600);
    expect([pcm[1599], pcm[1600], pcm[2399], pcm[2400]]).toEqual([0.1, 0.8, 0.8, 0.2]);
    // An unmute that isn't a barge-in skips the voice's tail instead.
    rec.mute!(true);
    ctx.feed(0.7, 1600);
    rec.mute!(false, 0.1, 0);
    ctx.feed(0.6, 1600); // the tail: dropped
    ctx.feed(0.3, 1600);
    expect(await pcmOf(rec.take(0)!)).toEqual(new Array(1600).fill(0.3));
    rec.cancel();
    expect(await rec.stop().then(samplesOf)).toBe(0);
  });

  it("keepS keeps a rolling window (the wake word's standby)", async () => {
    const { ctx, cap } = await rig();
    const rec = cap.record({ keepS: 0.2 });
    for (let i = 0; i < 10; i++) ctx.feed(i / 10, 1600);
    const pcm = await pcmOf((await rec.last(5))!);
    expect(pcm.length).toBe(3200);
    expect(pcm[0]).toBe(0.8);
  });
});

// --- the server recognizer, recording through it (lib/voiceDesktop) ---------- //

describe("the server recognizer on the capture graph", () => {
  function rig() {
    vi.stubGlobal("AudioWorkletNode", WNode);
    vi.stubGlobal("window", { AudioContext: WorkletCtx });
    const ctx = voiceAudioCtx() as unknown as WorkletCtx;
    ctx.state = "running";
    vi.stubGlobal("navigator", { language: "en-US", userAgent: "", mediaDevices: { getUserMedia: vi.fn() } });
    const mic = new MicKeeper(60_000, async () => fakeStream(), () => ctx as unknown as AudioContext);
    const clock = { now: 1_000_000 };
    vi.spyOn(Date, "now").mockImplementation(() => clock.now);
    const sent: Blob[] = [];
    mocks.apiRaw.mockImplementation(async (url: string, init: { body?: Blob }) => {
      if (url === "/api/voice/stream") return { ok: true, status: 200, json: async () => ({ id: "s1" }) };
      if (url.endsWith("/chunk")) {
        sent.push(init.body!);
        return { ok: true, status: 200, json: async () => ({ committed: "", partial: "hi", ms: 120 }) };
      }
      return { ok: true, status: 200, json: async () => ({ committed: "hi", same: true }) };
    });
    const r = serverRecognizer({ stream: () => true, mic });
    const flushed: Array<number | undefined> = [];
    const h: RecognizerHandlers = {
      onInterim: () => {},
      onFinal: () => {},
      onError: () => {},
      onEnd: () => {},
      onFlush: (_w, _f, spokeAt) => flushed.push(spokeAt),
      blank: () => false,
    };
    const settle = async () => {
      for (let i = 0; i < 8; i++) await new Promise((res) => setTimeout(res, 0));
    };
    const live = () => WNode.all.filter((n) => n.live && n.ctx === ctx);
    return {
      r,
      ctx,
      mic,
      clock,
      sent,
      flushed,
      live,
      settle,
      start: async () => {
        await mic.get();
        await flush();
        for (let i = 0; i < 8; i++) ctx.feed(0.004); // the room, in the pre-roll
        r.start(h);
        await settle();
      },
      /** One 85 ms block of `rms`, handed over `late` ms after its audio ended. */
      block: async (rms: number, late = 0) => {
        clock.now += 85 + late;
        ctx.feed(rms);
        clock.now -= late;
        await settle();
      },
    };
  }

  it("records through the worklet capture (never its own ScriptProcessor): the chunks are its 16 kHz samples, the pre-roll in front of the first", async () => {
    const t = rig();
    await t.start();
    expect(mocks.startWavRecording).not.toHaveBeenCalled();
    expect(t.live()).toHaveLength(1); // one graph: the pre-roll's is the recording's
    expect(t.ctx.procs).toBe(0);
    await t.block(0.3); // the voice from the first block: the pre-roll goes with it
    await t.block(0.3);
    expect(t.sent.length).toBeGreaterThan(0);
    // The first chunk: 500 ms of pre-roll + its two blocks, all 16 kHz.
    expect(samplesOf(t.sent[0])).toBe((PRE_ROLL_MS / 1000) * CAPTURE_RATE + 2 * 1365);
    t.r.stop();
  });

  it("a busy main thread (200 ms) hands blocks over in a burst: none is lost, spokeAt stays the voice's true start, and the heard quiet still ends the phrase", async () => {
    const t = rig();
    await t.start();
    for (let i = 0; i < 4; i++) await t.block(0.004); // the room, 340 ms
    const zero = t.clock.now - 340;
    // The page stalls 200 ms: the next three blocks (the voice's start) arrive at once.
    t.clock.now += 3 * 85 + 200;
    for (const v of [0.004, 0.3, 0.3]) t.ctx.feed(v);
    await t.settle();
    // The voice began at the 6th block's start (5 × 85 ms), not 200 ms later.
    expect(t.r.spokeAt).toBe(zero + 5 * 85);
    let chunked = 0;
    for (const b of t.sent) chunked += samplesOf(b);
    // Every block heard since the first loud one is in the chunks (nothing dropped).
    expect(chunked).toBeGreaterThanOrEqual(2 * 1365);
    for (let i = 0; i < 6; i++) await t.block(0.3);
    let n = 0;
    while (t.r.spokeAt && n < 20) {
      await t.block(0.004);
      n++;
    }
    expect(n).toBe(13); // 13 × 85 = 1105 ms > SILENCE_MS: the fixed quiet, as before
    await t.settle();
    expect(t.flushed).toEqual([zero + 5 * 85]);
    t.r.stop();
  });
});
