/** The voice sweep (desktop): the undo word ("scratch that"), the router's
 * new answers (scroll, interrupt, switch_back, where, commands), `heard` on
 * the route call, the cheat sheet, and the rail that follows the focus —
 * pure parts pinned here, the wiring pinned against the source (vitest runs
 * in node: no DOM). */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import MIC_SRC from "../components/TopBarMic.tsx?raw";
import SIDEBAR_SRC from "../components/sidebar/Sidebar.tsx?raw";
import ROW_CSS from "../components/sidebar/SidebarRow.css?raw";
import ACTIONS_SRC from "../lib/sessionActions.ts?raw";

const mocks = vi.hoisted(() => ({
  api: vi.fn(),
  apiRaw: vi.fn(),
  instApi: vi.fn(),
  ApiError: class ApiError extends Error {
    status = 0;
    body: unknown = null;
  },
}));
vi.mock("../api/client", () => ({ api: mocks.api, apiRaw: mocks.apiRaw, instApi: mocks.instApi, ApiError: mocks.ApiError }));

import {
  builtinCommands,
  Dictation,
  holdBack,
  isSwitchCommand,
  mayBeCommand,
  NO_PREVIOUS_LINE,
  parseRadio,
  previousSession,
  routePhrase,
  runVoiceCommand,
  scrollLine,
  undoLine,
  type CommandDeps,
  type RouteReply,
} from "../lib/voiceDesktop";
import * as W from "../lib/voiceWords";
import { WORD_ROWS } from "../components/settings/screens/VoiceWords";
import { groupCommands } from "../components/voice/VoiceCommands";
import { CLICK_GRACE_MS, foldOver, outOfView, type RailFold } from "../components/sidebar/railFollow";
import { useUi } from "../state/store";

afterEach(() => W.resetVoiceWords());

describe("the undo word (`undo`: “scratch that”)", () => {
  it("is a voice-words kind with defaults, kept when an older server's payload lacks it", () => {
    expect(W.PHRASE_KINDS).toContain("undo");
    expect(W.BUILTIN_VOICE_WORDS.undo).toEqual(["scratch that", "undo that"]);
    W.applyVoiceView({ words: { send: ["roger"] }, words_default: { send: ["over"] } });
    expect(W.voiceWords().undo).toEqual(["scratch that", "undo that"]);
    W.applyVoiceView({ words: { send: ["over"], undo: ["take that back"] } });
    expect(W.voiceWords().undo).toEqual(["take that back"]);
    expect(parseRadio("fix it take that back")).toEqual({ kind: "undo", text: "fix it" });
  });

  it("ends a phrase: the words before it; alone: nothing before it", () => {
    expect(parseRadio("fix the login bug scratch that")).toEqual({ kind: "undo", text: "fix the login bug" });
    expect(parseRadio("Scratch that.")).toEqual({ kind: "undo", text: "" });
    expect(parseRadio("undo that")).toEqual({ kind: "undo", text: "" });
    // Mid-sentence it is words.
    expect(parseRadio("scratch that itch").kind).toBe("");
    // Its heard forms (the server's `words.heard.undo`) count too.
    W.applyVoiceView({ words: { ...W.BUILTIN_VOICE_WORDS, heard: { undo: ["scratched that"] } } });
    expect(parseRadio("fix it scratched that")).toEqual({ kind: "undo", text: "fix it" });
  });

  it("a phrase another kind already says stays that kind (a clear word “scratch” keeps its tail)", () => {
    W.setVoiceWords({ ...W.BUILTIN_VOICE_WORDS, clear: ["scratch"] });
    expect(parseRadio("toss it scratch that")).toEqual({ kind: "disregard", text: "toss it" });
    expect(parseRadio("toss it undo that")).toEqual({ kind: "undo", text: "toss it" });
  });

  it("is held back while “that” may follow, so “scratch” is never typed early", () => {
    expect(holdBack("fix the bug scratch")).toEqual({ type: "fix the bug", held: "scratch" });
    expect(holdBack("fix the bug scratch that")).toEqual({ type: "fix the bug", held: "scratch that" });
    expect(holdBack("scratch the surface").held).toBe("");
  });

  it("at the end of a phrase: that phrase types nothing; earlier phrases stay", () => {
    const d = new Dictation();
    d.begin();
    expect(d.heard("fix the login bug")).toEqual({ kind: "type", text: "fix the login bug " });
    d.wrote("fix the login bug ");
    // The next phrase's guess went in as it was said…
    expect(d.hearing("and add a")).toMatchObject({ kind: "type", text: "and add a", guess: true });
    d.wrote("and add a");
    // …and its end takes exactly that back.
    expect(d.heard("and add a test scratch that")).toEqual({ kind: "undo", text: "and add a", alone: false });
    expect(d.typed).toBe("fix the login bug ");
    expect(d.listening).toBe(true);
  });

  it("a first phrase ending in it types nothing at all", () => {
    const d = new Dictation();
    d.begin();
    expect(d.heard("fix the login bug scratch that")).toEqual({ kind: "undo", text: "", alone: false });
    expect(d.typed).toBe("");
  });

  it("alone: takes back the last phrase typed and not yet sent, its characters exactly — again for the one before", () => {
    const d = new Dictation();
    d.begin();
    d.wrote((d.heard("fix the login bug") as { text: string }).text);
    d.wrote((d.heard("and add a test") as { text: string }).text);
    expect(d.typed).toBe("fix the login bug and add a test ");
    expect(d.heard("scratch that")).toEqual({ kind: "undo", text: "and add a test ", alone: true });
    expect(d.typed).toBe("fix the login bug ");
    expect(d.heard("undo that")).toEqual({ kind: "undo", text: "fix the login bug ", alone: true });
    expect(d.typed).toBe("");
    expect(d.heard("scratch that")).toEqual({ kind: "undo", text: "", alone: true });
  });

  it("never reaches past a send (next()) — and “disregard” still clears everything", () => {
    const d = new Dictation();
    d.continuous = true;
    d.begin();
    d.wrote((d.heard("first message") as { text: string }).text);
    d.next();
    expect(d.heard("scratch that")).toEqual({ kind: "undo", text: "", alone: true });
    d.wrote((d.heard("one") as { text: string }).text);
    d.wrote((d.heard("two") as { text: string }).text);
    expect(d.heard("disregard")).toEqual({ kind: "erase", text: "one two " });
  });

  it("said as push to talk is let go: taken back, and listening stops", () => {
    const d = new Dictation();
    d.begin();
    d.wrote((d.heard("fix the login bug") as { text: string }).text);
    d.hearing("scratch that");
    expect(d.finish()).toEqual({ kind: "undo", text: "fix the login bug ", alone: true, end: true });
    expect(d.listening).toBe(false);
  });

  it("an idle stop with “scratch that” in flight types none of it", () => {
    const d = new Dictation();
    d.begin();
    d.stream("fix it scratch that", "");
    expect(d.expire()).toEqual({ kind: "leave", text: "" });
  });

  it("its caption says what went, never a spoken line", () => {
    expect(undoLine({ text: "and add a test ", alone: true })).toBe("Took back “and add a test” — go on");
    expect(undoLine({ text: "", alone: true })).toBe("Nothing to take back — go on");
    expect(undoLine({ text: "", alone: false })).toBe("Scratched — go on");
    expect(undoLine({ text: "x".repeat(60), alone: true })).toMatch(/^Took back “x{39}…”/);
  });

  it("has its row in Settings → Voice → Voice words", () => {
    const row = WORD_ROWS.find((r) => r.kind === "undo");
    expect(row?.label).toBe("Undo last phrase");
    expect(WORD_ROWS.map((r) => r.kind)).toEqual(["send", "end", "clear", "undo", "stop", "hush"]);
  });
});

describe("the new commands wait whole for the router", () => {
  it.each([
    "scroll up",
    "scroll down a bit",
    "page down",
    "scroll to the bottom",
    "more",
    "keep going",
    "interrupt",
    "interrupt it",
    "press escape",
    "go back",
    "switch back",
    "where am I",
    "which session is this",
    "what can I say",
    "voice commands",
    "Scroll",
    "Go",
  ])("%j may be a command (never typed while the router decides)", (t) => {
    expect(mayBeCommand(t)).toBe(true);
  });

  it.each(["fix the scroll bug", "more tests please", "where is the config", "interrupted sleep is bad"])(
    "%j can't be one (typed as it comes)",
    (t) => expect(mayBeCommand(t)).toBe(false)
  );

  it("a whole scroll or “switch back” acts from the guess like a switch; “go back to …” waits", () => {
    expect(isSwitchCommand("scroll up")).toBe(true);
    expect(isSwitchCommand("page down please")).toBe(true);
    expect(isSwitchCommand("scroll to the top")).toBe(true);
    expect(isSwitchCommand("switch back")).toBe(true);
    expect(isSwitchCommand("go back")).toBe(false);
    expect(isSwitchCommand("scroll up and fix the header")).toBe(false);
  });
});

describe("routePhrase carries `heard`", () => {
  beforeEach(() => mocks.api.mockReset());

  it("sends Moonshine's text beside whisper's when the stream gave one, never an empty one", async () => {
    mocks.api.mockResolvedValue({ kind: "scroll", dir: "up", amount: "page", count: 1, title: "t1", ok: true });
    const r = await routePhrase("scroll up", "t1", undefined, undefined, undefined, " scroll up ");
    expect(r.kind).toBe("scroll");
    expect(mocks.api.mock.calls[0][1]).toEqual({ json: { text: "scroll up", session: "t1", heard: "scroll up" } });
    await routePhrase("scroll up", "t1", undefined, undefined, undefined, "");
    expect(mocks.api.mock.calls[1][1]).toEqual({ json: { text: "scroll up", session: "t1" } });
  });

  it("takes the new kinds; an unknown kind is still a prompt", async () => {
    for (const kind of ["scroll", "interrupt", "switch_back", "where", "commands"]) {
      mocks.api.mockResolvedValueOnce({ kind });
      expect((await routePhrase("x", "t1")).kind).toBe(kind);
    }
    mocks.api.mockResolvedValueOnce({ kind: "warp" });
    expect((await routePhrase("x", "t1")).kind).toBe("prompt");
  });

  it("is wired: TopBarMic passes the recognizer's `heard`, taken once per phrase", () => {
    expect(MIC_SRC).toContain("r === serverRec ? serverRec.takeHeard() : \"\"");
  });
});

function fakeDeps(over: Partial<CommandDeps> = {}) {
  const log = { caption: [] as string[], announce: [] as string[], speak: [] as string[], select: [] as string[], sheet: null as unknown };
  const deps: CommandDeps = {
    railOrder: () => ["a", "b", "c"],
    focused: () => "b",
    select: (t) => void log.select.push(t),
    name: (t) => t.toUpperCase(),
    caption: (l) => void log.caption.push(l),
    announce: (l) => void log.announce.push(l),
    speak: (l) => void log.speak.push(l),
    route: async () => ({ kind: "prompt" }),
    approveDialogs: async () => false,
    activity: () => "idle",
    replies: () => false,
    submitted: () => {},
    previous: () => "a",
    showCommands: (items) => void (log.sheet = items),
    ...over,
  };
  return { deps, log };
}

describe("the router's new answers (runVoiceCommand)", () => {
  it("scroll: a caption only — nothing said, nothing selected", async () => {
    const { deps, log } = fakeDeps();
    const r: RouteReply = { kind: "scroll", dir: "up", amount: "page", count: 1, title: "b", ok: true };
    expect(await runVoiceCommand(r, "scroll up", deps)).toBe(true);
    expect(log.caption).toEqual(["Scrolled up"]);
    expect(log.announce).toEqual([]);
    expect(log.speak).toEqual([]);
  });

  it("scrollLine: the ends, a count, a failure", () => {
    expect(scrollLine({ amount: "bottom", dir: "down" })).toBe("At the bottom");
    expect(scrollLine({ amount: "top", dir: "up" })).toBe("At the top");
    expect(scrollLine({ amount: "page", dir: "down", count: 3 })).toBe("Scrolled down ×3");
    expect(scrollLine({ ok: false }, "login fix")).toBe("Couldn't scroll login fix");
  });

  it("interrupt: “Interrupted <name>”, a caption, nothing said", async () => {
    const { deps, log } = fakeDeps();
    await runVoiceCommand({ kind: "interrupt", title: "c", ok: true }, "interrupt", deps);
    await runVoiceCommand({ kind: "interrupt", ok: false }, "interrupt", deps);
    expect(log.caption).toEqual(["Interrupted C", "Couldn't interrupt B"]);
    expect(log.announce.concat(log.speak)).toEqual([]);
  });

  it("switch_back: selects the previous session like a switch; none → a caption", async () => {
    const { deps, log } = fakeDeps();
    await runVoiceCommand({ kind: "switch_back" }, "go back", deps);
    expect(log.select).toEqual(["a"]);
    expect(log.announce).toEqual(["A."]);
    const none = fakeDeps({ previous: () => null });
    await runVoiceCommand({ kind: "switch_back" }, "go back", none.deps);
    expect(none.log.select).toEqual([]);
    expect(none.log.caption).toEqual([NO_PREVIOUS_LINE]);
  });

  it("where: the focused session's name, said whatever the replies choice (a question, not an echo)", async () => {
    const { deps, log } = fakeDeps();
    await runVoiceCommand({ kind: "where" }, "where am I", deps);
    expect(log.speak).toEqual(["B."]);
    const none = fakeDeps({ focused: () => "" });
    await runVoiceCommand({ kind: "where" }, "where am I", none.deps);
    expect(none.log.speak).toEqual(["No session is focused."]);
  });

  it("commands: says `say`, shows the sheet with the server's rows (the desktop's own when it sends none)", async () => {
    const { deps, log } = fakeDeps();
    const items = [{ say: "scroll up", does: "Scroll the pane up", group: "Reading" }];
    await runVoiceCommand({ kind: "commands", say: "Here is what you can say.", items }, "what can I say", deps);
    expect(log.speak).toEqual(["Here is what you can say."]);
    expect(log.sheet).toEqual(items);
    const bare = fakeDeps();
    await runVoiceCommand({ kind: "commands" }, "what can I say", bare.deps);
    expect(bare.log.sheet).toEqual(builtinCommands());
    expect(bare.log.speak).toEqual([]);
  });

  it("the desktop's own sheet names the current words", () => {
    W.setVoiceWords({ ...W.BUILTIN_VOICE_WORDS, undo: ["take that back"] });
    const rows = builtinCommands();
    expect(rows.find((r) => r.does === "Take back the last phrase")?.say).toBe("take that back");
    expect(rows.every((r) => r.say.trim() && r.does.trim())).toBe(true);
  });
});

describe("previousSession (“go back”)", () => {
  const live = ["a", "b", "c", "d"];
  it("the one focused before, else the MRU's next — never a window, the focused one, gone or hidden", () => {
    expect(previousSession({ focused: "b", prev: "a", mru: ["b", "c", "a"], live })).toBe("a");
    expect(previousSession({ focused: "b", prev: null, mru: ["b", "\u0000mindflock-logs", "c"], live })).toBe("c");
    expect(previousSession({ focused: "b", prev: "gone", mru: ["b", "c"], live })).toBe("c");
    expect(previousSession({ focused: "b", prev: "a", mru: ["b", "c"], live, hidden: new Set(["a"]) })).toBe("c");
    expect(previousSession({ focused: "b", prev: null, mru: ["b"], live })).toBeNull();
  });

  it("bounces: the store remembers the focus it replaced", () => {
    const ui = useUi.getState();
    ui.setFocused("a");
    ui.setFocused("b");
    expect(useUi.getState().prevFocused).toBe("a");
    const back = previousSession({ focused: "b", prev: useUi.getState().prevFocused, mru: [], live });
    expect(back).toBe("a");
    ui.setFocused(back);
    expect(previousSession({ focused: "a", prev: useUi.getState().prevFocused, mru: [], live })).toBe("b");
    // Clearing the selection keeps the memory.
    ui.setFocused(null);
    expect(useUi.getState().prevFocused).toBe("b");
  });

  it("a voice switch asks the rail to pulse its row (a new pulse each time)", () => {
    const ui = useUi.getState();
    ui.pulseRail("a");
    const first = useUi.getState().railPulse;
    ui.pulseRail("a");
    expect(useUi.getState().railPulse?.key).toBe("a");
    expect(useUi.getState().railPulse?.n).toBe((first?.n || 0) + 1);
    expect(ACTIONS_SRC).toContain("if (opts?.voice) ui.pulseRail(title);");
    expect(MIC_SRC).toContain("selectSession(title, { noKeyboard: true, voice: true })");
  });
});

describe("the cheat sheet", () => {
  it("groups rows in the order their groups appear; no groups → one flat list; empty rows dropped", () => {
    const g = groupCommands([
      { say: "scroll up", does: "a", group: "Reading" },
      { say: "go back", does: "b", group: "Sessions" },
      { say: "more", does: "c", group: "Reading" },
      { say: " ", does: "d" },
    ]);
    expect(g.map((x) => [x.group, x.items.map((i) => i.say)])).toEqual([
      ["Reading", ["scroll up", "more"]],
      ["Sessions", ["go back"]],
    ]);
    expect(groupCommands([{ say: "a", does: "" }, { say: "b", does: "" }])).toEqual([
      { group: "", items: [{ say: "a", does: "" }, { say: "b", does: "" }] },
    ]);
  });

  it("is reachable without voice (the mic's options), and the next phrase closes it", () => {
    expect(MIC_SRC).toContain("Voice commands…");
    expect(MIC_SRC).toContain("onClick={openCommands}");
    expect(MIC_SRC).toContain("<VoiceCommands items={cheat}");
    // onFinal and onFlush both close it before the phrase is handled.
    expect(MIC_SRC.match(/closeCheat\(\);/g)?.length).toBeGreaterThanOrEqual(2);
  });
});

describe("the rail follows the focus", () => {
  const folds: RailFold[] = [
    { kind: "run", id: "r1", collapsed: true, keys: ["a", "b"] },
    { kind: "run", id: "r2", collapsed: false, keys: ["c"] },
    { kind: "device", id: "__self", collapsed: true, keys: ["a", "b", "c"] },
    { kind: "device", id: "mini", collapsed: false, keys: ["mini::x"] },
  ];

  it("unfolds what hides the row, the device before the run group inside it", () => {
    expect(foldOver("a", folds).map((f) => f.id)).toEqual(["__self", "r1"]);
    expect(foldOver("c", folds).map((f) => f.id)).toEqual(["__self"]);
    expect(foldOver("mini::x", folds)).toEqual([]);
    expect(foldOver("", folds)).toEqual([]);
  });

  it("scrolls only for a row not wholly in view", () => {
    const view = { top: 100, bottom: 500 };
    expect(outOfView({ top: 120, bottom: 150 }, view)).toBe(false);
    expect(outOfView({ top: 480, bottom: 510 }, view)).toBe(true);
    expect(outOfView({ top: 90, bottom: 120 }, view)).toBe(true);
    expect(outOfView({ top: 900, bottom: 930 }, view)).toBe(true);
    expect(CLICK_GRACE_MS).toBeGreaterThan(0);
  });

  it("lives where the rail renders (every switch path), with a reduced-motion pulse", () => {
    expect(SIDEBAR_SRC).toContain("useRailFollow({");
    expect(SIDEBAR_SRC).toContain("pulse: ui.railPulse");
    expect(SIDEBAR_SRC).toContain("ui.toggleRunCollapsed(f.id) : ui.toggleDeviceCollapsed(f.id)");
    expect(ROW_CSS).toContain(".inst.voice-pulse .inst-row");
    expect(ROW_CSS).toMatch(/prefers-reduced-motion: reduce\)\s*\{\s*\.inst\.voice-pulse/);
  });
});
