import { afterEach, describe, it, expect, vi } from "vitest";
import { TAKE_MIN_S, encodeWav, resample, resampleTo, resamplerName, startWavRecording } from "../lib/wavRecorder";
import { code } from "./sourceText";

async function view(blob: Blob): Promise<DataView> {
  return new DataView(await blob.arrayBuffer());
}

function ascii(v: DataView, at: number, len: number): string {
  let s = "";
  for (let i = 0; i < len; i++) s += String.fromCharCode(v.getUint8(at + i));
  return s;
}

describe("encodeWav", () => {
  it("writes the exact 44-byte PCM mono header", async () => {
    const blob = encodeWav(new Float32Array(10), 16000);
    expect(blob.type).toBe("audio/wav");
    expect(blob.size).toBe(44 + 20);
    const v = await view(blob);
    expect(ascii(v, 0, 4)).toBe("RIFF");
    expect(v.getUint32(4, true)).toBe(36 + 20);
    expect(ascii(v, 8, 4)).toBe("WAVE");
    expect(ascii(v, 12, 4)).toBe("fmt ");
    expect(v.getUint32(16, true)).toBe(16);
    expect(v.getUint16(20, true)).toBe(1); // PCM
    expect(v.getUint16(22, true)).toBe(1); // mono
    expect(v.getUint32(24, true)).toBe(16000);
    expect(v.getUint32(28, true)).toBe(32000); // byte rate
    expect(v.getUint16(32, true)).toBe(2); // block align
    expect(v.getUint16(34, true)).toBe(16); // bits
    expect(ascii(v, 36, 4)).toBe("data");
    expect(v.getUint32(40, true)).toBe(20);
  });

  it("scales full range and clamps beyond it", async () => {
    const v = await view(encodeWav(new Float32Array([1, -1, 0, 2, -3, 0.5]), 8000));
    const pcm = [0, 1, 2, 3, 4, 5].map((i) => v.getInt16(44 + i * 2, true));
    expect(pcm).toEqual([32767, -32768, 0, 32767, -32768, Math.round(0.5 * 32767)]);
  });

  it("encodes an empty clip as a bare header", async () => {
    const v = await view(encodeWav(new Float32Array(0), 16000));
    expect(v.byteLength).toBe(44);
    expect(v.getUint32(40, true)).toBe(0);
  });
});

describe("resample", () => {
  it("returns the input untouched when the rates match", () => {
    const s = new Float32Array([0.1, 0.2, 0.3]);
    expect(resample(s, 16000, 16000)).toBe(s);
  });

  it("48 kHz → 16 kHz yields ⌊n·16/48⌋ samples", () => {
    for (const n of [0, 1, 2, 3, 4096, 4097, 48001]) {
      expect(resample(new Float32Array(n), 48000, 16000).length).toBe(Math.floor((n * 16) / 48));
    }
    expect(resample(new Float32Array(44100), 44100, 16000).length).toBe(16000);
  });

  it("keeps a constant signal constant", () => {
    const out = resample(new Float32Array(1000).fill(0.25), 44100, 16000);
    expect(out.length).toBeGreaterThan(0);
    for (const x of out) expect(x).toBeCloseTo(0.25, 6);
  });

  it("keeps a linear ramp monotonic away from the clip's edges, and spans it end to end", () => {
    const ramp = Float32Array.from({ length: 4800 }, (_, i) => i / 4800);
    for (const [from, to] of [
      [48000, 16000],
      [44100, 16000],
      [8000, 16000],
    ]) {
      const out = resample(ramp, from, to);
      // The filter reaches 16 output samples each way; past that the ramp is
      // reproduced (at the edges the taps run off the clip).
      for (let i = 17; i < out.length - 17; i++) expect(out[i]).toBeGreaterThanOrEqual(out[i - 1]);
      expect(out[out.length - 1]).toBeGreaterThan(out[0] + 0.99);
    }
  });

  /** RMS of the middle of a clip (the edges carry the filter's run-in). */
  const rms = (x: Float32Array) => {
    const a = Math.floor(x.length / 4);
    const b = Math.floor((x.length * 3) / 4);
    let s = 0;
    for (let i = a; i < b; i++) s += x[i] * x[i];
    return Math.sqrt(s / (b - a));
  };
  const tone = (hz: number, rate: number, n: number) =>
    Float32Array.from({ length: n }, (_, i) => 0.5 * Math.sin((2 * Math.PI * hz * i) / rate));
  const dB = (after: number, before: number) => 20 * Math.log10(after / before);

  it.each([10_000, 12_000, 15_000])(
    "filters out a %i Hz tone (above the new 8 kHz Nyquist) instead of folding it into the speech band",
    (hz) => {
      const x = tone(hz, 48000, 48000);
      const y = resample(x, 48000, 16000);
      // Linear interpolation keeps most of it, aliased to |16k − f|.
      expect(dB(rms(y), rms(x))).toBeLessThan(-20);
    }
  );

  it("keeps a 300 Hz tone (speech) at full strength", () => {
    const x = tone(300, 48000, 48000);
    const y = resample(x, 48000, 16000);
    expect(Math.abs(dB(rms(y), rms(x)))).toBeLessThan(0.5);
    // …and in phase: sample i of the output is input sample 3i.
    for (const i of [4000, 6000, 9000]) expect(y[i]).toBeCloseTo(x[i * 3], 2);
  });

  it("keeps a 7 kHz tone — under the new Nyquist, it is speech band, not alias", () => {
    const x = tone(7000, 48000, 48000);
    expect(dB(rms(resample(x, 48000, 16000)), rms(x))).toBeGreaterThan(-6);
  });

  it("is a windowed sinc, never linear interpolation", () => {
    expect(code("lib/wavRecorder.ts")).not.toMatch(/Linear-interpolation|\(samples\[i1\] - samples\[i0\]\) \* frac/);
    expect(code("lib/wavRecorder.ts")).toContain("Math.sin(Math.PI * arg) / (Math.PI * arg)");
  });
});

describe("resampleTo: the browser's own resampler first", () => {
  afterEach(() => vi.unstubAllGlobals());

  /** An OfflineAudioContext that records what it was asked and renders a
   * marker, so the test can tell its output from the fallback's. */
  function fakeOffline(fail = false) {
    const made: Array<{ channels: number; length: number; rate: number; buffer?: { rate: number; data: Float32Array }; started: boolean }> = [];
    class FakeOffline {
      destination = { name: "destination" };
      rec: (typeof made)[number];
      constructor(channels: number, length: number, rate: number) {
        if (fail) throw new Error("NotSupportedError: sample rate");
        this.rec = { channels, length, rate, started: false };
        made.push(this.rec);
      }
      createBuffer(_ch: number, n: number, rate: number) {
        const data = new Float32Array(n);
        this.rec.buffer = { rate, data };
        return { getChannelData: () => data };
      }
      createBufferSource() {
        const rec = this.rec;
        return {
          buffer: null as unknown,
          connect() {},
          start() {
            rec.started = true;
          },
        };
      }
      startRendering() {
        const out = new Float32Array(this.rec.length).fill(0.125);
        return Promise.resolve({ getChannelData: () => out });
      }
    }
    vi.stubGlobal("OfflineAudioContext", FakeOffline);
    return made;
  }

  it("renders the clip through an OfflineAudioContext(1, ⌈n·16000/rate⌉, 16000)", async () => {
    const made = fakeOffline();
    expect(resamplerName()).toBe("browser resampler");
    const clip = new Float32Array(4097).fill(0.3);
    const out = await resampleTo(clip, 48000, 16000);
    expect(made).toHaveLength(1);
    expect(made[0].channels).toBe(1);
    expect(made[0].rate).toBe(16000);
    expect(made[0].length).toBe(Math.ceil((4097 * 16000) / 48000));
    expect(made[0].buffer!.rate).toBe(48000);
    expect(made[0].buffer!.data[4096]).toBeCloseTo(0.3, 6);
    expect(made[0].started).toBe(true);
    expect(out.length).toBe(made[0].length);
    expect(out[0]).toBe(0.125);
  });

  it("falls back to the windowed sinc where the context refuses the rate, or there is none", async () => {
    fakeOffline(true);
    const clip = new Float32Array(4800).fill(0.25);
    const out = await resampleTo(clip, 48000, 16000);
    expect(out.length).toBe(1600);
    expect(out[800]).toBeCloseTo(0.25, 6);
    vi.unstubAllGlobals();
    expect(resamplerName()).toBe("windowed-sinc resampler");
    expect((await resampleTo(clip, 48000, 16000)).length).toBe(1600);
  });

  it("the recorder's stop() goes through it", async () => {
    const made = fakeOffline();
    const g = fakeGraph(48000);
    const rec = startWavRecording(STREAM, g.ctx);
    g.fire(new Float32Array(4096).fill(0.5));
    const v = await view(await rec.stop());
    expect(made).toHaveLength(1);
    const n = Math.ceil((4096 * 16000) / 48000);
    expect(v.getUint32(40, true)).toBe(n * 2);
    expect(v.getInt16(44, true)).toBe(Math.round(0.125 * 32767));
  });
});

// --- a fake Web Audio graph -------------------------------------------------

interface FakeNode {
  connected: unknown[];
  disconnects: number;
  connect(to: unknown): void;
  disconnect(): void;
}

function fakeNode(): FakeNode {
  return {
    connected: [],
    disconnects: 0,
    connect(to) {
      this.connected.push(to);
    },
    disconnect() {
      this.disconnects++;
      // A real node throws InvalidAccessError when disconnecting twice in some
      // engines; make the fake as strict.
      if (this.disconnects > 1) throw new Error("already disconnected");
      this.connected = [];
    },
  };
}

function fakeGraph(sampleRate = 48000) {
  const source = fakeNode();
  const processor = Object.assign(fakeNode(), {
    args: [] as number[],
    onaudioprocess: null as ((e: unknown) => void) | null,
  });
  const gain = Object.assign(fakeNode(), { gain: { value: 1 } });
  const destination = { name: "destination" };
  const streams: unknown[] = [];
  const ctx = {
    sampleRate,
    destination,
    createMediaStreamSource(s: unknown) {
      streams.push(s);
      return source;
    },
    createScriptProcessor(...args: number[]) {
      processor.args = args;
      return processor;
    },
    createGain() {
      return gain;
    },
  };
  const fire = (block: Float32Array) =>
    processor.onaudioprocess?.({ inputBuffer: { getChannelData: (ch: number) => (ch === 0 ? block : null) } });
  return { ctx: ctx as unknown as AudioContext, source, processor, gain, destination, streams, fire };
}

const STREAM = { id: "mic" } as unknown as MediaStream;

describe("startWavRecording", () => {
  it("wires source → ScriptProcessor(4096,1,1) → muted gain → destination", () => {
    const g = fakeGraph();
    startWavRecording(STREAM, g.ctx);
    expect(g.streams).toEqual([STREAM]);
    expect(g.processor.args).toEqual([4096, 1, 1]);
    expect(g.source.connected).toEqual([g.processor]);
    expect(g.processor.connected).toEqual([g.gain]);
    expect(g.gain.connected).toEqual([g.destination]);
    expect(g.gain.gain.value).toBe(0);
    expect(typeof g.processor.onaudioprocess).toBe("function");
  });

  it("skipS drops the first audio unrecorded and unmetered (the app's own voice, cut at a barge-in)", () => {
    const g = fakeGraph(48000);
    const levels: number[] = [];
    // 0.15 s at 48 kHz = 7200 frames: two 4096 blocks cover it.
    const rec = startWavRecording(STREAM, g.ctx, { onLevel: (r) => levels.push(r), skipS: 0.15 });
    g.fire(new Float32Array(4096).fill(0.9));
    g.fire(new Float32Array(4096).fill(0.9));
    expect(rec.seconds()).toBe(0);
    expect(levels).toEqual([]);
    g.fire(new Float32Array(4096).fill(0.25));
    expect(rec.seconds()).toBeCloseTo(4096 / 48000, 9);
    expect(levels).toHaveLength(1);
    expect(levels[0]).toBeCloseTo(0.25, 6);
  });

  it("mute drops the audio while on (a confirmation plays), then its tail; recording goes on", () => {
    const g = fakeGraph(48000);
    const levels: number[] = [];
    const rec = startWavRecording(STREAM, g.ctx, { onLevel: (r) => levels.push(r) });
    g.fire(new Float32Array(4096).fill(0.25));
    rec.mute!(true);
    g.fire(new Float32Array(4096).fill(0.9)); // "Sent." in the speakers
    g.fire(new Float32Array(4096).fill(0.9));
    expect(rec.seconds()).toBeCloseTo(4096 / 48000, 9);
    expect(levels).toHaveLength(1);
    // Off: 0.15 s more (7200 frames — two blocks) is the voice dying away.
    rec.mute!(false, 0.15);
    g.fire(new Float32Array(4096).fill(0.9));
    g.fire(new Float32Array(4096).fill(0.9));
    expect(rec.seconds()).toBeCloseTo(4096 / 48000, 9);
    g.fire(new Float32Array(4096).fill(0.3));
    expect(rec.seconds()).toBeCloseTo(8192 / 48000, 9);
    expect(levels).toHaveLength(2);
    expect(levels[1]).toBeCloseTo(0.3, 6);
    // A second "off" without an "on" adds no skip.
    rec.mute!(false, 0.15);
    g.fire(new Float32Array(4096).fill(0.3));
    expect(rec.seconds()).toBeCloseTo(12288 / 48000, 9);
  });

  it("accumulates blocks, reports level, and stops into one WAV at the target rate", async () => {
    const g = fakeGraph(48000);
    const levels: number[] = [];
    const rec = startWavRecording(STREAM, g.ctx, { onLevel: (r) => levels.push(r) });
    expect(rec.seconds()).toBe(0);

    const block = new Float32Array(4096).fill(0.5);
    g.fire(block);
    expect(rec.seconds()).toBeCloseTo(4096 / 48000, 9);
    g.fire(new Float32Array(4096)); // silence
    expect(rec.seconds()).toBeCloseTo(8192 / 48000, 9);

    expect(levels).toHaveLength(2);
    expect(levels[0]).toBeCloseTo(0.5, 6);
    expect(levels[1]).toBe(0);

    const wav = await rec.stop();
    expect(wav.type).toBe("audio/wav");
    const v = await view(wav);
    const n = Math.floor((8192 * 16000) / 48000);
    expect(v.getUint32(24, true)).toBe(16000);
    expect(v.getUint32(40, true)).toBe(n * 2);
    expect(wav.size).toBe(44 + n * 2);
    // First half loud, tail silent.
    expect(v.getInt16(44, true)).toBe(Math.round(0.5 * 32767));
    expect(v.getInt16(44 + (n - 1) * 2, true)).toBe(0);

    for (const node of [g.source, g.processor, g.gain]) expect(node.disconnects).toBe(1);
    // Blocks after stop are ignored.
    g.fire(new Float32Array(4096).fill(1));
    expect(rec.seconds()).toBeCloseTo(8192 / 48000, 9);
  });

  it("keepS keeps a rolling window (whole blocks, never under it); last(s) is the tail, without draining", async () => {
    const g = fakeGraph(16000); // the target rate: no resampling in the way
    const levels: number[] = [];
    const rec = startWavRecording(STREAM, g.ctx, { keepS: 0.5, onLevel: (r) => levels.push(r) });
    for (const v of [0.1, 0.2, 0.3, 0.4]) g.fire(new Float32Array(4096).fill(v));
    expect(levels).toHaveLength(4); // the level of every block, kept or not
    expect(rec.seconds()).toBeCloseTo((4 * 4096) / 16000, 9);
    // 0.5 s = 8000 frames: the last two blocks (8192) are what is kept.
    const all = await view((await rec.last(10))!);
    expect(all.getUint32(40, true)).toBe(8192 * 2);
    expect(all.getInt16(44, true)).toBe(Math.round(0.3 * 32767));
    // The last 0.1 s: 1600 frames off the end of the newest block.
    const tail = await view((await rec.last(0.1))!);
    expect(tail.getUint32(40, true)).toBe(1600 * 2);
    expect(tail.getInt16(44, true)).toBe(Math.round(0.4 * 32767));
    // Across a block boundary, in order.
    const two = await view((await rec.last(5000 / 16000))!);
    expect(two.getInt16(44, true)).toBe(Math.round(0.3 * 32767));
    expect(two.getInt16(44 + 4999 * 2, true)).toBe(Math.round(0.4 * 32767));
    // Nothing drained: the same window again; nothing once cancelled.
    expect((await view((await rec.last(10))!)).getUint32(40, true)).toBe(8192 * 2);
    rec.cancel();
    expect(await rec.last(1)).toBeNull();
  });

  it("without keepS nothing is dropped, and last() before any audio is null", async () => {
    const g = fakeGraph(16000);
    const rec = startWavRecording(STREAM, g.ctx);
    expect(await rec.last(1)).toBeNull();
    for (let i = 0; i < 6; i++) g.fire(new Float32Array(4096).fill(0.1));
    expect((await view((await rec.last(100))!)).getUint32(40, true)).toBe(6 * 4096 * 2);
  });

  it("copies each block instead of keeping the engine's reused buffer", async () => {
    const g = fakeGraph(16000);
    const rec = startWavRecording(STREAM, g.ctx);
    const shared = new Float32Array(4).fill(0.25);
    g.fire(shared);
    shared.fill(-0.25);
    g.fire(shared);
    const v = await view(await rec.stop());
    expect(v.getInt16(44, true)).toBe(Math.round(0.25 * 32767));
    expect(v.getInt16(44 + 4 * 2, true)).toBe(Math.round(-0.25 * 32768));
  });

  it("honours targetRate and clamps out-of-range samples", async () => {
    const g = fakeGraph(8000);
    const rec = startWavRecording(STREAM, g.ctx, { targetRate: 8000 });
    g.fire(new Float32Array([2, -2, 0.1]));
    const v = await view(await rec.stop());
    expect(v.getUint32(24, true)).toBe(8000);
    expect(v.getUint32(40, true)).toBe(6);
    expect(v.getInt16(44, true)).toBe(32767);
    expect(v.getInt16(46, true)).toBe(-32768);
  });

  it("cancel disconnects and drops everything", async () => {
    const g = fakeGraph();
    const levels: number[] = [];
    const rec = startWavRecording(STREAM, g.ctx, { onLevel: (r) => levels.push(r) });
    g.fire(new Float32Array(4096).fill(0.3));
    rec.cancel();
    for (const node of [g.source, g.processor, g.gain]) expect(node.disconnects).toBe(1);
    expect(g.processor.onaudioprocess).toBeNull();
    expect(() => rec.cancel()).not.toThrow();
    // A stop after cancel neither throws nor resurrects the audio.
    const v = await view(await rec.stop());
    expect(v.getUint32(40, true)).toBe(0);
    expect(levels).toHaveLength(1);
  });

  /** The samples of a 16-bit WAV, back in -1..1. */
  async function pcmOf(blob: Blob): Promise<number[]> {
    const v = await view(blob);
    const n = v.getUint32(40, true) / 2;
    return Array.from({ length: n }, (_, i) => v.getInt16(44 + i * 2, true) / 32767);
  }

  describe("take(): the chunks of a stream, recording on", () => {
    it("consecutive takes are disjoint and in order, each at the target rate", async () => {
      const g = fakeGraph(16000);
      const rec = startWavRecording(STREAM, g.ctx);
      const n = Math.ceil(TAKE_MIN_S * 16000);
      g.fire(new Float32Array(n).fill(0.25));
      const a = rec.take()!;
      g.fire(new Float32Array(n).fill(-0.5));
      g.fire(new Float32Array(n).fill(0.75));
      const b = rec.take()!;
      expect(a.type).toBe("audio/wav");
      expect((await view(a)).getUint32(24, true)).toBe(16000);
      const pa = await pcmOf(a);
      const pb = await pcmOf(b);
      expect(pa).toHaveLength(n);
      expect(pb).toHaveLength(2 * n);
      expect(pa.every((x) => Math.abs(x - 0.25) < 1e-3)).toBe(true);
      expect(pb[0]).toBeCloseTo(-0.5, 3);
      expect(pb[2 * n - 1]).toBeCloseTo(0.75, 3);
      // Recording went on: nothing torn down.
      for (const node of [g.source, g.processor, g.gain]) expect(node.disconnects).toBe(0);
      expect(rec.seconds()).toBeCloseTo((3 * n) / 16000, 9);
    });

    it("resamples like stop(): 48 kHz in, 16 kHz out", async () => {
      const g = fakeGraph(48000);
      const rec = startWavRecording(STREAM, g.ctx);
      g.fire(new Float32Array(4800 * 3).fill(0.5)); // 0.3 s
      const v = await view(rec.take()!);
      expect(v.getUint32(24, true)).toBe(16000);
      expect(v.getUint32(40, true)).toBe(4800 * 2);
      expect(v.getInt16(44 + 2400 * 2, true)).toBe(Math.round(0.5 * 32767));
    });

    it("under TAKE_MIN_S is null — and kept for the next take", async () => {
      expect(TAKE_MIN_S).toBe(0.3);
      const g = fakeGraph(16000);
      const rec = startWavRecording(STREAM, g.ctx);
      expect(rec.take()).toBeNull();
      g.fire(new Float32Array(4000).fill(0.1)); // 0.25 s
      expect(rec.take()).toBeNull();
      g.fire(new Float32Array(1000).fill(0.2)); // 0.3125 s in all
      const p = await pcmOf(rec.take()!);
      expect(p).toHaveLength(5000);
      expect(p[0]).toBeCloseTo(0.1, 3);
      expect(p[4999]).toBeCloseTo(0.2, 3);
      expect(rec.take()).toBeNull();
    });

    it("stop() after takes returns only the remainder; take() after stop or cancel is null", async () => {
      const g = fakeGraph(16000);
      const rec = startWavRecording(STREAM, g.ctx);
      g.fire(new Float32Array(4800).fill(0.25));
      expect(rec.take()).not.toBeNull();
      g.fire(new Float32Array(100).fill(-0.25)); // too short to take, still the tail
      const tail = await pcmOf(await rec.stop());
      expect(tail).toHaveLength(100);
      expect(tail[0]).toBeCloseTo(-0.25, 3);
      expect(rec.take()).toBeNull();
      const g2 = fakeGraph(16000);
      const rec2 = startWavRecording(STREAM, g2.ctx);
      g2.fire(new Float32Array(4800).fill(0.25));
      rec2.cancel();
      expect(rec2.take()).toBeNull();
    });
  });

  it("stop twice does not throw and returns the same clip", async () => {
    const g = fakeGraph();
    const rec = startWavRecording(STREAM, g.ctx);
    g.fire(new Float32Array(4096).fill(0.1));
    const a = await rec.stop();
    let second: Promise<Blob> | undefined;
    expect(() => {
      second = rec.stop();
    }).not.toThrow();
    expect(await second).toBe(a);
    expect(() => rec.cancel()).not.toThrow();
  });
});
