/** The desktop mic answering a question about MindFlock (lib/voiceDesktop):
 * the router's `help` kind is known, "Looking that up…" is captioned while
 * POST /api/voice/help reads the docs, and the answer is spoken whatever the
 * replies choice (`speak`; TopBarMic queues it, so "say that again" repeats
 * it). A failure is a line, never a throw. */

import { afterEach, describe, expect, it, vi } from "vitest";
import MIC_SRC from "../components/TopBarMic.tsx?raw";
import {
  HELP_FAILED_LINE,
  HELP_LOOKING_LINE,
  askHelp,
  routePhrase,
  runVoiceCommand,
  type CommandDeps,
  type RouteReply,
} from "../lib/voiceDesktop";

const mocks = vi.hoisted(() => ({ api: vi.fn() }));
vi.mock("../api/client", () => ({ api: mocks.api, apiRaw: vi.fn(), instApi: vi.fn(), ApiError: Error }));
vi.mock("../lib/terminals", () => ({ peekTerm: vi.fn() }));
vi.mock("../lib/wavRecorder", () => ({ startWavRecording: vi.fn(), resamplerName: () => "browser resampler" }));

const ANSWER = {
  say: "From Fences: A fence keeps a worker inside the paths it may change.",
  source: { doc: "mcp.md", heading: "Fences" },
  model: true,
};

function deps(over: Partial<CommandDeps> = {}) {
  const out = { said: [] as string[], captions: [] as Array<[string, boolean | undefined]> };
  const d: CommandDeps = {
    railOrder: () => ["t1"],
    focused: () => "t1",
    select: () => undefined,
    name: (t) => t,
    caption: (l, sticky) => void out.captions.push([l, sticky]),
    announce: (l) => void out.said.push(l),
    route: vi.fn(async () => ({ kind: "prompt" }) as RouteReply),
    approveDialogs: async () => true,
    activity: () => "idle",
    replies: () => true,
    submitted: () => undefined,
    ...over,
  };
  return { d, out };
}

describe("voice help", () => {
  afterEach(() => mocks.api.mockReset());

  it("routePhrase passes the help kind through with its question", async () => {
    mocks.api.mockResolvedValueOnce({ kind: "help", text: "how does the fence work" });
    expect(await routePhrase("how does the fence work?", "t1")).toEqual({ kind: "help", text: "how does the fence work" });
  });

  it("captions 'Looking that up…', asks /api/voice/help, then announces the answer", async () => {
    mocks.api.mockResolvedValueOnce(ANSWER);
    const { d, out } = deps();
    expect(await runVoiceCommand({ kind: "help", text: "how does the fence work" }, "How does the fence work?", d)).toBe(true);
    expect(mocks.api).toHaveBeenCalledWith("/api/voice/help", { json: { text: "how does the fence work" } });
    expect(out.captions[0]).toEqual([HELP_LOOKING_LINE, undefined]);
    expect(out.said).toEqual([ANSWER.say]);
  });

  it("falls back to the phrase said when the router sent no text", async () => {
    mocks.api.mockResolvedValueOnce(ANSWER);
    const { d } = deps();
    await runVoiceCommand({ kind: "help" }, "what is a team run", d);
    expect(mocks.api).toHaveBeenCalledWith("/api/voice/help", { json: { text: "what is a team run" } });
  });

  it("with replies off the answer is still spoken (speak, not announce)", async () => {
    mocks.api.mockResolvedValueOnce(ANSWER);
    const spoken: string[] = [];
    const { d, out } = deps({ replies: () => false, speak: (l) => void spoken.push(l) });
    await runVoiceCommand({ kind: "help", text: "q" }, "q", d);
    expect(spoken).toEqual([ANSWER.say]);
    expect(out.said).toEqual([]);
  });

  it("TopBarMic's speak captions and queues the line (so 'say that again' has it)", () => {
    const speak = MIC_SRC.split("speak: (line) => {")[1].split("},")[0];
    expect(speak).toContain("say(line, true);");
    expect(speak).toContain("queue.say(line);");
  });

  it("a failed or empty lookup is a line to say", async () => {
    mocks.api.mockRejectedValueOnce(new Error("HTTP 500"));
    expect(await askHelp("q")).toEqual({ say: HELP_FAILED_LINE, source: null, model: false });
    mocks.api.mockResolvedValueOnce({ say: "  " });
    expect((await askHelp("q")).say).toBe(HELP_FAILED_LINE);
    mocks.api.mockRejectedValueOnce(new Error("offline"));
    const { d, out } = deps();
    await runVoiceCommand({ kind: "help", text: "q" }, "q", d);
    expect(out.said).toEqual([HELP_FAILED_LINE]);
  });
});
