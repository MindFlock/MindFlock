/** The desktop microphone's capture side (voice-command polish round): nothing
 * the person says is clipped or dropped on the way to the recognizer.
 *
 * Pinned here, on the capture graph (lib/voiceMic MicCapture) and the server
 * recognizer (lib/voiceDesktop serverRecognizer):
 *  - the start chime is recorded, not skipped: no word starts under it, the
 *    pre-roll stays, a word said over it keeps its start;
 *  - a word that starts later is sent with a rolling LEAD_IN_MS lead-in
 *    from the ring (never the 0–255 ms the old trims happened to leave);
 *  - the dictation's level detector (PhraseLevel): the floor frozen in
 *    speech, a lone blip (one loud block, no word from the server) never
 *    becomes an (empty) phrase — before/after on synthetic level sequences;
 *  - a command said over a confirmation ("Sent.") is rewound and sent;
 *  - a 409 for the server's stream cap is asked again, and never makes the
 *    rest of the dictation clips;
 *  - a wake's lead-in (what was said after the word, before the chime) is
 *    the first phrase. */
import { afterEach, describe, expect, it, vi } from "vitest";
import { CAPTURE_BLOCK_FRAMES, type CaptureBlock } from "../lib/voiceCapture.worklet";
import { CAPTURE_MUTE_HOLD_S, MicKeeper, PRE_ROLL_MS, RING_KEEP_MS, isLoud, noiseFloor } from "../lib/voiceMic";
import {
  FLOOR_RELEARN_MS,
  LEAD_IN_MS,
  ONSET_RUN,
  PhraseLevel,
  SILENCE_MS,
  STREAM_OPEN_RETRIES,
  leadSpeech,
  serverRecognizer,
  streamFull,
  voiceAudioCtx,
  type RecognizerHandlers,
} from "../lib/voiceDesktop";
import { code } from "./sourceText";

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

// --- fakes: the capture worklet's node and a context with an AudioWorklet ---- //

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

class WorkletCtx {
  state: AudioContextState = "running";
  sampleRate = 48_000;
  destination = {};
  audioWorklet = { addModule: () => Promise.resolve() };
  frames = 0;
  /** What the barge-in detector's analyser reads (an RMS: a constant signal). */
  analyser = 0;
  resume() {
    return Promise.resolve();
  }
  createMediaStreamSource() {
    return { connect: vi.fn(), disconnect: vi.fn() };
  }
  createGain() {
    return { gain: { value: 1 }, connect: vi.fn(), disconnect: vi.fn() };
  }
  createScriptProcessor() {
    return { onaudioprocess: null, connect: vi.fn(), disconnect: vi.fn() };
  }
  createAnalyser() {
    return { fftSize: 2048, connect: vi.fn(), disconnect: vi.fn(), getFloatTimeDomainData: (b: Float32Array) => b.fill(this.analyser) };
  }
  /** One 85 ms block (1365 samples at 16 kHz) of `value` into every live capture. */
  feed(value: number, n = 1365) {
    this.frames += CAPTURE_BLOCK_FRAMES;
    for (const node of WNode.all.filter((x) => x.live && x.ctx === this))
      node.port.onmessage!({ data: { pcm: new Float32Array(n).fill(value), rms: Math.abs(value), frames: this.frames } });
  }
}

function fakeStream() {
  const track = { stop: vi.fn(), addEventListener: vi.fn() };
  return { active: true, getTracks: () => [track] } as unknown as MediaStream;
}
const flush = async () => {
  for (let i = 0; i < 5; i++) await Promise.resolve();
};
async function pcmOf(wav: Blob): Promise<number[]> {
  const v = new DataView(await wav.arrayBuffer());
  const out: number[] = [];
  for (let at = 44; at < v.byteLength; at += 2) out.push(Math.round((v.getInt16(at, true) / 32767) * 1000) / 1000);
  return out;
}
const BLOCK = 1365;
const ROOM = 0.004;
const VOICE = 0.3;

type Answer = { status?: number; body: Record<string, unknown> };

/** A streaming server recognizer recording through the capture graph; the
 * server answers opens with `opens` (in turn, then 200), chunks with an
 * empty pass, /end with `end`. */
function rig(o: { opens?: Answer[]; end?: Record<string, unknown> } = {}) {
  vi.stubGlobal("AudioWorkletNode", WNode);
  vi.stubGlobal("window", { AudioContext: WorkletCtx });
  const ctx = voiceAudioCtx() as unknown as WorkletCtx;
  ctx.state = "running";
  vi.stubGlobal("navigator", { language: "en-US", userAgent: "", mediaDevices: { getUserMedia: vi.fn() } });
  const mic = new MicKeeper(60_000, async () => fakeStream(), () => ctx as unknown as AudioContext);
  const clock = { now: 1_000_000 };
  vi.spyOn(Date, "now").mockImplementation(() => clock.now);
  const chunks: Blob[] = [];
  const calls: string[] = [];
  const opens = [...(o.opens || [])];
  let sid = 0;
  mocks.apiRaw.mockImplementation(async (url: string, init: { method?: string; body?: Blob }) => {
    calls.push((init?.method || "GET") + " " + url);
    const reply = (status: number, body: unknown) => ({ ok: status < 300, status, json: async () => body });
    if (url === "/api/voice/stream") {
      const a = opens.shift();
      return a ? reply(a.status ?? 200, a.body) : reply(200, { id: "s" + ++sid });
    }
    if (url.endsWith("/chunk")) {
      chunks.push(init.body!);
      return reply(200, { committed: "", partial: "", ms: 120 });
    }
    if (url.endsWith("/end")) return reply(200, o.end || { committed: "next session", same: true });
    if (url.startsWith("/api/voice/transcribe")) return reply(200, { text: "the clip" });
    return reply(204, {});
  });
  const r = serverRecognizer({ stream: () => true, mic });
  const ev: string[] = [];
  const h: RecognizerHandlers = {
    onInterim: () => {},
    onFinal: (t) => void ev.push("final:" + t),
    onError: (c, d) => void ev.push("error:" + (d || c)),
    onEnd: () => void ev.push("end"),
    onSpeech: () => void ev.push("speech"),
    onFlush: (t) => void ev.push("flush:" + t),
    onNotice: (t) => void ev.push("notice:" + t),
    onBarge: () => void ev.push("barge"),
    blank: () => true,
  };
  const settle = async () => {
    for (let i = 0; i < 8; i++) await new Promise((res) => setTimeout(res, 0));
  };
  return {
    r,
    ctx,
    mic,
    clock,
    chunks,
    calls,
    ev,
    h,
    settle,
    /** The microphone open, the room in the ring (`roomBlocks` of it). */
    open: async (roomBlocks = 8) => {
      await mic.get();
      await flush();
      for (let i = 0; i < roomBlocks; i++) {
        clock.now += 85;
        ctx.feed(ROOM);
      }
    },
    start: async (opts: { skipMs?: number; chimeMs?: number } = {}) => {
      r.start(h, opts);
      await settle();
    },
    /** One 85 ms block of `rms` (the clock moves with it). */
    block: async (rms: number) => {
      clock.now += 85;
      ctx.feed(rms);
      await settle();
    },
  };
}
const samples = (wavs: Blob[]) => Promise.all(wavs.map(pcmOf)).then((a) => a.flat());

describe("the start chime: recorded, but no word starts in it", () => {
  it("a word said over the chime keeps its start and the pre-roll; the chime's blocks never open a phrase", async () => {
    const t = rig();
    await t.open();
    await t.start({ chimeMs: 150 });
    // The chime (and the word's first syllable under it): two blocks.
    await t.block(VOICE);
    await t.block(VOICE);
    expect(t.ev).not.toContain("speech");
    // After it: the word is heard — from a block whose audio began before the
    // chime ended, so its pre-roll and the whole recording go in front.
    await t.block(VOICE);
    expect(t.ev).toContain("speech");
    await t.block(VOICE);
    await vi.waitFor(() => expect(t.chunks.length).toBeGreaterThan(0));
    const first = await pcmOf(t.chunks[0]);
    // Before: 150 ms skipped (two whole blocks) and no pre-roll at all.
    expect(first.length).toBe((PRE_ROLL_MS / 1000) * 16_000 + 4 * BLOCK);
    expect(first.filter((v) => v === VOICE).length).toBe(4 * BLOCK);
    t.r.stop();
  });

  it("the mic hands the chime's span to the recognizer as chimeMs — the barge-in's skip stays a skip", () => {
    const MIC = code("components/TopBarMic.tsx");
    expect(MIC).toMatch(/const recOpts = \{ skipMs, chimeMs: chimeSkip \};/);
    expect(MIC).not.toContain("Math.max(skipMs, chimeSkip)");
  });
});

describe("the lead-in before a phrase's first word: a rolling LEAD_IN_MS", () => {
  it("a word that starts later is sent with the 300 ms before its first loud block, from the ring", async () => {
    const t = rig();
    await t.open();
    await t.start();
    // 510 ms of room: the recording's trims leave 170 ms of it (the old lead-in).
    for (let i = 0; i < 6; i++) await t.block(ROOM);
    await t.block(VOICE);
    await t.block(VOICE);
    await vi.waitFor(() => expect(t.chunks.length).toBe(1));
    const first = await pcmOf(t.chunks[0]);
    const lead = first.findIndex((v) => v === VOICE);
    expect(lead).toBe((LEAD_IN_MS / 1000) * 16_000);
    expect(first.slice(0, lead).every((v) => v === ROOM)).toBe(true);
    // The word whole, nothing twice: two blocks of it, then the recording's own.
    expect(first.length).toBe(lead + 2 * BLOCK);
    await t.block(VOICE);
    await t.block(VOICE);
    await t.block(VOICE);
    // The phrase ends: its tail goes too — every block of the word once.
    for (let i = 0; i < Math.ceil(SILENCE_MS / 85) + 2; i++) await t.block(ROOM);
    await t.settle();
    const all = await samples(t.chunks);
    expect(all.filter((v) => v === VOICE).length).toBe(5 * BLOCK);
    t.r.stop();
  });
});

// --- the dictation's level detector: before / after ------------------------ //

/** The old dictation detector: the floor always creeps (5% a block). */
function oldLoud(seq: number[], floor0: number): boolean[] {
  let floor = floor0;
  return seq.map((rms) => {
    floor = noiseFloor(floor, rms);
    return isLoud(rms, floor);
  });
}
/** The new one, as the recognizer feeds it (speech from the first loud block). */
function newLoud(seq: number[], floor0: number): { loud: boolean[]; best: number } {
  const lv = new PhraseLevel(floor0);
  let heard = false;
  const loud = seq.map((rms) => {
    const l = lv.feed(rms, 85, { speech: heard });
    heard ||= l;
    return l;
  });
  return { loud, best: lv.best };
}
const firstLoud = (a: boolean[]) => a.indexOf(true);

describe("PhraseLevel: the floor frozen in speech; a blip is not a phrase", () => {
  const room = 0.004;

  it("a steady voice stays speech (before: the floor crept up to it and the phrase ended mid-sentence)", () => {
    const voice = Array<number>(40).fill(0.05); // 3.4 s, steady
    const before = oldLoud(voice, room);
    const after = newLoud(voice, room);
    expect(before.indexOf(false)).toBeGreaterThan(0); // it went "quiet" while still talking
    expect(before.indexOf(false)).toBeLessThan(40);
    expect(after.loud.every(Boolean)).toBe(true);
  });

  it("quiet speech starts exactly as before: same first loud block, at the same level", () => {
    for (const seq of [
      [room, room, 0.021, 0.025, 0.022, room],
      [room, 0.03, room, 0.03, 0.03, room],
      [room, room, room, 0.05, 0.06, 0.05],
    ]) {
      expect(firstLoud(newLoud(seq, room).loud)).toBe(firstLoud(oldLoud(seq, room)));
    }
  });

  it("a new steady noise in a long stretch is learnt after FLOOR_RELEARN_MS", () => {
    const fan = Array<number>(Math.ceil((FLOOR_RELEARN_MS + 3000) / 85)).fill(0.03);
    const after = newLoud(fan, room).loud;
    expect(after.slice(0, Math.floor(FLOOR_RELEARN_MS / 85))).not.toContain(false);
    expect(after.at(-1)).toBe(false);
  });

  it("a click or a knock is one loud block (best 1); a syllable two or more", () => {
    expect(newLoud([room, 0.2, room, room], room).best).toBe(1);
    expect(newLoud([room, 0.2, room, 0.2, room], room).best).toBe(1); // keys apart
    expect(newLoud([room, 0.05, 0.06, room], room).best).toBeGreaterThanOrEqual(ONSET_RUN);
    // Before: every one of them opened a phrase all the same.
    expect(firstLoud(oldLoud([room, 0.2, room, room], room))).toBe(1);
  });

  it("the chime (hush): never loud, and the floor only goes down in it", () => {
    const lv = new PhraseLevel(room);
    expect(lv.feed(0.3, 85, { hush: true })).toBe(false);
    expect(lv.floor).toBe(room);
    expect(lv.feed(0.001, 85, { hush: true })).toBe(false);
    expect(lv.floor).toBe(0.001);
  });

  it("leadSpeech finds a word in audio (start and end), and nothing in room or a click", () => {
    const rate = 16_000;
    const pcm = (parts: Array<[number, number]>) => {
      const out: number[] = [];
      for (const [v, ms] of parts) out.push(...Array<number>(Math.round((ms / 1000) * rate)).fill(v));
      return Float32Array.from(out);
    };
    const said = leadSpeech(pcm([[room, 255], [0.2, 510], [room, 340]]), rate, room)!;
    expect(said.startS).toBeCloseTo(0.255, 2);
    expect(said.endS).toBeCloseTo(0.765, 2);
    expect(leadSpeech(pcm([[room, 1000]]), rate, room)).toBeNull();
    expect(leadSpeech(pcm([[room, 340], [0.3, 85], [room, 340]]), rate, room)).toBeNull();
  });
});

describe("a blip in a streamed listen is not a phrase", () => {
  async function phrase(loud: number[]) {
    const t = rig({ end: { committed: "", same: true } });
    await t.open();
    await t.start();
    for (let i = 0; i < 6; i++) await t.block(ROOM);
    for (const v of loud) await t.block(v);
    await t.block(ROOM);
    await vi.waitFor(() => expect(t.chunks.length).toBe(1)); // the server heard it: no word
    for (let i = 0; i < Math.ceil(SILENCE_MS / 85) + 2; i++) await t.block(ROOM);
    await t.settle();
    return t;
  }

  it("one loud block with no word back: dropped (its stream DELETEd), never an empty phrase flushed", async () => {
    const t = await phrase([0.2]);
    expect(t.calls.some((c) => c.endsWith("/end"))).toBe(false);
    expect(t.calls).toContain("DELETE /api/voice/stream/s1");
    expect(t.ev.filter((e) => e.startsWith("flush:"))).toEqual([]);
    t.r.stop();
  });

  it("two in a row (a syllable) still ends as a phrase, /end and all", async () => {
    const t = await phrase([0.2, 0.2]);
    expect(t.calls).toContain("POST /api/voice/stream/s1/end");
    expect(t.ev).toContain("flush:");
    t.r.stop();
  });
});

describe("a command said over a confirmation (\"Sent.\") is not lost", () => {
  it("the voice heard over a line that plays on is rewound from its start when the mute lifts, and the phrase ends and goes", async () => {
    const t = rig();
    await t.open();
    await t.start();
    await t.block(ROOM);
    // "Sent." starts: muted (the barge-in detector listens).
    t.ctx.analyser = 0.01;
    t.r.mute!(true);
    await new Promise((res) => setTimeout(res, 150)); // its level is learnt
    await t.block(0.01);
    await t.block(0.01);
    // The person talks over it ("next session"): a barge-in — but a
    // confirmation is not cut, it plays on.
    t.ctx.analyser = VOICE;
    await vi.waitFor(() => expect(t.ev).toContain("barge"), { timeout: 2000 });
    for (let i = 0; i < 6; i++) await t.block(VOICE);
    t.ctx.analyser = 0.01;
    await t.block(0.01);
    await t.block(0.01);
    // The line ends; the mute lifts. Nothing loud comes after.
    t.r.mute!(false);
    for (let i = 0; i < Math.ceil(SILENCE_MS / 85) + 3; i++) await t.block(ROOM);
    await t.settle();
    // Before: 270 ms rewound and never heard — no chunk, nothing sent.
    const all = await samples(t.chunks);
    expect(all.filter((v) => v === VOICE).length).toBe(6 * BLOCK);
    expect(t.calls).toContain("POST /api/voice/stream/s1/end");
    expect(t.ev).toContain("speech");
    t.r.stop();
  });

  it("the capture recording holds CAPTURE_MUTE_HOLD_S of muted audio for that", () => {
    expect(CAPTURE_MUTE_HOLD_S).toBeGreaterThanOrEqual(1.5);
  });
});

describe("a 409 on the stream's open", () => {
  const FULL = { status: 409, body: { error: "2 voice streams are already open: end one first" } };

  it("the server's stream cap is asked again — streaming goes on", async () => {
    expect(streamFull(FULL.body.error)).toBe(true);
    expect(streamFull("whisper does not stream")).toBe(false);
    const t = rig({ opens: [FULL, FULL] });
    await t.open();
    await t.start();
    await vi.waitFor(() => expect(t.calls.filter((c) => c === "POST /api/voice/stream").length).toBe(3), { timeout: 3000 });
    await t.block(VOICE);
    await t.block(VOICE);
    await t.block(VOICE);
    await vi.waitFor(() => expect(t.chunks.length).toBeGreaterThan(0));
    expect(t.r.streaming).toBe(true);
    expect(t.ev.filter((e) => e.startsWith("notice:"))).toEqual([]);
    t.r.stop();
  });

  it("still full after ~1.5 s: that phrase goes as a clip, the next one streams again (never a downgrade that sticks)", async () => {
    const t = rig({ opens: Array<Answer>(STREAM_OPEN_RETRIES + 1).fill(FULL) });
    await t.open();
    await t.start();
    await t.block(VOICE);
    await t.block(VOICE);
    await vi.waitFor(() => expect(t.calls.filter((c) => c === "POST /api/voice/stream").length).toBe(STREAM_OPEN_RETRIES + 1), {
      timeout: 4000,
    });
    await t.settle();
    for (let i = 0; i < Math.ceil(SILENCE_MS / 85) + 2; i++) await t.block(ROOM);
    await vi.waitFor(() => expect(t.ev).toContain("final:the clip"));
    expect(t.r.streaming).toBe(true);
    // The next phrase opens a stream again.
    await vi.waitFor(() => expect(t.calls.filter((c) => c === "POST /api/voice/stream").length).toBe(STREAM_OPEN_RETRIES + 2));
    t.r.stop();
  });
});

describe("a wake's lead-in: what was said after the word, before the chime", () => {
  const WORD = 0.2;

  it("\"MindFlock — next session\" said without waiting: the command is the first phrase, the word never in it", async () => {
    const t = rig();
    await t.open(6);
    // "MindFlock" (510 ms), a breath, then "next session" said while the
    // server checked the word.
    for (let i = 0; i < 6; i++) await t.block(WORD);
    const soundEnd = t.clock.now;
    for (let i = 0; i < 3; i++) await t.block(ROOM);
    for (let i = 0; i < 7; i++) await t.block(VOICE);
    for (let i = 0; i < 3; i++) await t.block(ROOM);
    // The check said yes: listening starts now (lib/wakeWord marks the word's end).
    t.mic.markLead(soundEnd);
    await t.start({ chimeMs: 150 });
    expect(t.ev).toContain("speech"); // begun already: no word needed after the chime
    // The chime's blocks, then quiet: the phrase ends on the quiet after the command.
    for (let i = 0; i < Math.ceil(SILENCE_MS / 85) + 3; i++) await t.block(ROOM);
    await t.settle();
    const all = await samples(t.chunks);
    expect(all.filter((v) => v === VOICE).length).toBe(7 * BLOCK);
    expect(all.filter((v) => v === WORD)).toEqual([]);
    expect(t.calls).toContain("POST /api/voice/stream/s1/end");
    expect(t.ev).toContain("flush:next session");
    t.r.stop();
  });

  it("the word alone (they waited for the chime): no lead-in, the phrase starts at the first word as ever", async () => {
    const t = rig();
    await t.open(6);
    for (let i = 0; i < 6; i++) await t.block(WORD);
    const soundEnd = t.clock.now;
    for (let i = 0; i < 6; i++) await t.block(ROOM);
    t.mic.markLead(soundEnd);
    await t.start({ chimeMs: 150 });
    expect(t.ev).not.toContain("speech");
    t.r.stop();
  });

  it("the mark is taken once, only by the listening started right then; never more than the ring holds", async () => {
    const t = rig();
    await t.open(60);
    t.mic.markLead(t.clock.now - 500);
    expect(t.mic.takeLead()!.length).toBe(8000);
    expect(t.mic.takeLead()).toBeNull();
    t.mic.markLead(t.clock.now - 500);
    t.clock.now += 1500; // a start long after the wake (a click): not its
    expect(t.mic.takeLead()).toBeNull();
    t.mic.markLead(t.clock.now - RING_KEEP_MS - 100);
    expect(t.mic.takeLead()).toBeNull();
    // The ring holds RING_KEEP_MS; the room's quiet is still its last PRE_ROLL_MS.
    expect(t.mic.preRoll(RING_KEEP_MS)!.length).toBe((RING_KEEP_MS / 1000) * 16_000);
    t.ctx.feed(0.001);
    for (let i = 0; i < 8; i++) t.ctx.feed(ROOM);
    expect(t.mic.quietFloor()).toBe(ROOM);
  });
});
