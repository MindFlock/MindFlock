/** "That's all" stops dictation only said on its own (the owner, 2026-11-10:
 * the desktop trace heard "…so make sure that that's all." and stopped
 * listening mid-message). The stop family splits in two (lib/voiceWords
 * STOP_ALONE, core.voice_words, mobile.js — the same rule):
 *  - "stop listening", "mic off", "pause listening", "stop recording" end
 *    any phrase: "fix it, stop listening" stops with "fix it" typed;
 *  - "that's all", "done talking" (and their kin and heard forms) stop only
 *    as the WHOLE phrase — everything since the last pause, a filler like
 *    "okay" / "um" before it allowed — or after an end phrase ("over and
 *    out, that's all" sends, then stops). Anywhere else they are words. */

import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("../api/client", () => ({
  api: vi.fn(),
  apiRaw: vi.fn(),
  instApi: vi.fn(),
  ApiError: class extends Error {},
}));
vi.mock("../lib/toast", () => ({ toast: vi.fn() }));

const W = await import("../lib/voiceWords");
const { Dictation, stopTail, holdBack } = await import("../lib/voiceDesktop");

afterEach(() => W.resetVoiceWords());

/** The owner's phrase, exactly as the trace logged it. */
const OWNER = "that Because it's bad practice, so make sure that that's all.";

function dictation(): InstanceType<typeof Dictation> {
  const d = new Dictation();
  d.continuous = true;
  d.begin();
  return d;
}

describe("a conversational stop phrase at the end of a sentence is words", () => {
  it("the owner's sentence is typed whole and listening goes on", () => {
    const d = dictation();
    // Words already typed: the phrase is not the message's first (no router).
    d.wrote("Don't hardcode the token. ");
    d.next();
    d.wrote("Keep it in the env ");
    expect(d.heard(OWNER)).toEqual({ kind: "type", text: OWNER + " " });
    expect(d.listening).toBe(true);
    expect(stopTail(OWNER)).toBeNull();
  });

  it("streamed: the committed words never stop, and the phrase's end types it", () => {
    const d = dictation();
    d.wrote("Keep it in the env ");
    expect(d.words("so make sure that that's").kind).not.toBe("leave");
    expect(d.words("all.").kind).not.toBe("leave");
    expect(d.listening).toBe(true);
    const end = d.flush("");
    expect(end.kind).toBe("type");
    expect(d.listening).toBe(true);
  });

  it("“done talking” and the heard forms too", () => {
    for (const t of ["fix it, I'm done talking", "fix it done talkin", "fix it then talking", "fix it, that is all"]) {
      expect(stopTail(t)).toBeNull();
    }
  });
});

describe("on its own it stops", () => {
  it("“That's all.” alone stops; nothing is typed", () => {
    const d = dictation();
    d.wrote("Keep it in the env ");
    expect(d.heard("That's all.")).toEqual({ kind: "leave", text: "", stop: true });
    expect(d.listening).toBe(false);
  });

  it("a filler before it is still alone, and is not typed", () => {
    for (const t of ["Okay, that's all.", "um that is all", "So, I'm done talking."]) {
      const d = dictation();
      d.wrote("Keep it in the env ");
      expect(d.heard(t)).toEqual({ kind: "leave", text: "", stop: true });
    }
  });

  it("streamed: it waits for the phrase's end (“that's all I need” is words), then stops", () => {
    const d = dictation();
    d.wrote("Keep it in the env ");
    expect(d.words("that's all").kind).toBe("continue");
    expect(d.listening).toBe(true);
    // Held back meanwhile: nothing of it is typed as a guess.
    expect(holdBack("that's all").type).toBe("");
    expect(d.flush("")).toEqual({ kind: "leave", text: "", stop: true });
    expect(d.listening).toBe(false);
  });
});

describe("the unambiguous stop phrases end any phrase", () => {
  it("“fix it, stop listening” stops with “fix it” typed", () => {
    const d = dictation();
    expect(d.heard("fix it, stop listening")).toEqual({ kind: "leave", text: "fix it ", stop: true });
    expect(d.listening).toBe(false);
  });

  it("streamed, the moment the phrase settles", () => {
    const d = dictation();
    expect(d.words("fix it, stop listening")).toMatchObject({ kind: "leave", stop: true });
    expect(d.listening).toBe(false);
  });

  it("mic off, pause listening, stop recording too", () => {
    expect(stopTail("fix it, mic off")).toBe("fix it");
    expect(stopTail("fix it pause listening")).toBe("fix it");
    expect(stopTail("fix it, stop recording.")).toBe("fix it");
  });
});

describe("“over and out, that's all”", () => {
  it("sends, then stops", () => {
    const d = dictation();
    expect(d.heard("ship it over and out, that's all")).toEqual({ kind: "send", text: "ship it ", end: true, out: true });
    expect(d.listening).toBe(false);
    const e = dictation();
    expect(e.heard("over and out, that's all")).toEqual({ kind: "send", text: "", end: true, out: true });
  });
});

describe("the person's own stop phrases", () => {
  it("two words or more end any phrase; one word only on its own (the router's)", () => {
    W.setVoiceWords({ stop: ["halt", "we're done", "that's all"] });
    expect(stopTail("fix it, we're done")).toBe("fix it");
    expect(stopTail("fix it halt")).toBeNull();
    // A conversational default kept in the list stays conversational.
    expect(stopTail("fix it, that's all")).toBeNull();
    expect(stopTail("that's all")).toBe("");
  });
});
