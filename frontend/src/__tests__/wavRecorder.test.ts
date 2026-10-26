import { describe, it, expect } from "vitest";
import { encodeWav, resample, startWavRecording } from "../lib/wavRecorder";

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

  it("keeps a linear ramp monotonic", () => {
    const ramp = Float32Array.from({ length: 4800 }, (_, i) => i / 4800);
    for (const [from, to] of [
      [48000, 16000],
      [44100, 16000],
      [8000, 16000],
    ]) {
      const out = resample(ramp, from, to);
      // Non-decreasing: upsampling holds the last input sample at the edge.
      for (let i = 1; i < out.length; i++) expect(out[i]).toBeGreaterThanOrEqual(out[i - 1]);
      expect(out[out.length - 1]).toBeGreaterThan(out[0] + 0.99);
    }
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
