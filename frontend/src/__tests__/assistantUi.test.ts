/** The voice assistant's surfaces and wiring: the strip under the mic (its
 * states, rendered to a string), the cheat sheet's assistant rows, the Alt+M
 * hotkey, the router's `assistant` kind, the queue speaking a line in the
 * assistant's voice, and how TopBarMic hands phrases over (source pins). */

import { createElement } from "react";
import { renderToString } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({ api: vi.fn() }));
vi.mock("../api/client", async (orig) => ({ ...(await orig<typeof import("../api/client")>()), api: mocks.api }));

import { AssistantStripBody, stripHead, stripHolds } from "../components/voice/AssistantStrip";
import { ASSISTANT_COMMANDS, ASSISTANT_GROUP, groupCommands, withAssistant } from "../components/voice/VoiceCommands";
import { KEYMAP, VOICE_ASSISTANT_EVENT, shortcutLabels } from "../lib/keymap";
import { EMPTY_VIEW, type AssistantView } from "../lib/voiceAssistant";
import { PendingChoice, SpeechQueue, builtinCommands, routePhrase, type Speaker } from "../lib/voiceDesktop";
import { code, css } from "./sourceText";

const view = (p: Partial<AssistantView>): AssistantView => ({ ...EMPTY_VIEW, ...p });
const html = (v: AssistantView) => renderToString(createElement(AssistantStripBody, { v }));

describe("the strip", () => {
  it("says where the words go: Talking to MindFlock while the mode listens", () => {
    expect(stripHead({ mode: true, phase: "listening" })).toBe("Talking to MindFlock");
    expect(stripHead({ mode: true, phase: "thinking" })).toBe("Thinking…");
    expect(stripHead({ mode: false, phase: "replying" })).toBe("MindFlock");
    const h = html(view({ mode: true, phase: "listening" }));
    expect(h).toContain("Talking to MindFlock");
    expect(h).toContain("assist-dot on");
    expect(h).toContain('aria-label="Stop talking to MindFlock"');
  });

  it("thinking: the phrase heard and the stepping dots", () => {
    const h = html(view({ phase: "thinking", heard: "open intake" }));
    expect(h).toContain("“open intake”");
    expect(h).toContain("assist-thinking");
  });

  it("the reply's caption; an error in the error style", () => {
    expect(html(view({ phase: "replying", caption: "Two sessions need you." }))).toContain("Two sessions need you.");
    expect(html(view({ caption: "Couldn't.", error: true }))).toContain("assist-caption is-error");
  });

  it("chips: what it did — a button when it can jump there, plain text otherwise", () => {
    const h = html(
      view({
        chips: [
          { text: "Opened Intake", ok: true, open: "intake" },
          { text: "Started session fix-login", ok: true, session: "fix-login" },
          { text: "Showing the Sky", ok: true },
          { text: "No session called x", ok: false },
        ],
      })
    );
    expect(h.match(/<button[^>]*class="assist-chip"/g)?.length).toBe(2);
    expect(h).toContain("Started session fix-login");
    expect(h).toContain('<span class="assist-chip">Showing the Sky</span>');
    expect(h).toContain("assist-chip is-error");
  });

  it("a confirm: Yes / No buttons beside the spoken question; an ask has none", () => {
    const c = html(view({ caption: "Delete fix-login?", question: { kind: "confirm", text: "Delete fix-login?" } }));
    expect(c).toContain('id="assist-yes"');
    expect(c).toContain('id="assist-no"');
    const a = html(view({ caption: "Which repo?", question: { kind: "ask", text: "Which repo?" } }));
    expect(a).not.toContain("assist-yes");
  });

  it("holds (no fade) while the mode is on, a turn thinks or a question waits; fades otherwise", () => {
    expect(stripHolds(view({ mode: true }))).toBe(true);
    expect(stripHolds(view({ phase: "thinking" }))).toBe(true);
    expect(stripHolds(view({ question: { kind: "ask", text: "?" } }))).toBe(true);
    expect(stripHolds(view({ caption: "Done", chips: [{ text: "x", ok: true }] }))).toBe(false);
  });

  it("is the accent, not the dictation's red; the mic in assistant mode too; still under reduced motion", () => {
    const s = css("components/voice/AssistantStrip.css");
    expect(s).toMatch(/\.assist-strip \{[^}]*border-left: 3px solid var\(--accent\)/);
    expect(s).toMatch(/#topbar #mic-btn\.assistant \{\s*color: var\(--accent\);/);
    expect(s).toMatch(/\.mic-ring\.assistant::after \{\s*border-color: var\(--accent\);/);
    expect(s).toMatch(/prefers-reduced-motion: reduce/);
    expect(s).toMatch(/\.assist-flash \{/);
  });
});

describe("the cheat sheet", () => {
  it("adds 4–6 assistant examples to whatever list it shows, once", () => {
    expect(ASSISTANT_COMMANDS.length).toBeGreaterThanOrEqual(4);
    expect(ASSISTANT_COMMANDS.length).toBeLessThanOrEqual(6);
    const rows = withAssistant(builtinCommands());
    expect(rows.filter((r) => r.group === ASSISTANT_GROUP)).toHaveLength(ASSISTANT_COMMANDS.length);
    expect(withAssistant(rows)).toHaveLength(rows.length);
    expect(groupCommands(rows).map((g) => g.group)).toContain(ASSISTANT_GROUP);
    expect(ASSISTANT_COMMANDS.some((c) => /^MindFlock, /.test(c.say))).toBe(true);
    expect(ASSISTANT_COMMANDS.some((c) => /that's all/.test(c.say))).toBe(true);
    expect(code("components/voice/VoiceCommands.tsx")).toContain("groupCommands(withAssistant(items))");
  });
});

describe("the hotkey", () => {
  it("Alt+M turns assistant mode on/off through the mic's event, and nothing else is on Alt+M", () => {
    const e = KEYMAP.find((b) => b.id === "assistant");
    expect(e).toMatchObject({ key: "m", alt: true });
    expect(KEYMAP.filter((b) => b.key.toLowerCase() === "m" && b.alt === true && !b.mod)).toHaveLength(1);
    expect(shortcutLabels("assistant")[0]).toBe("Alt+M");
    const seen: string[] = [];
    const w = globalThis as unknown as { window?: unknown };
    const had = w.window;
    const target = new EventTarget();
    w.window = target;
    target.addEventListener(VOICE_ASSISTANT_EVENT, (ev) => seen.push(ev.type));
    try {
      e?.run(new KeyboardEvent("keydown"), "down");
    } catch {
      e?.run({} as KeyboardEvent, "down");
    }
    w.window = had;
    expect(seen).toEqual([VOICE_ASSISTANT_EVENT]);
  });

  it("its help never says 'talk' (the mic's push-to-talk label finds the Talk entry by that word)", () => {
    const e = KEYMAP.find((b) => b.id === "assistant");
    expect(String(e?.help?.[2])).not.toMatch(/\btalk\b/i);
  });
});

describe("the router's assistant kind", () => {
  it("routePhrase passes {kind: assistant, text} through", async () => {
    mocks.api.mockResolvedValueOnce({ kind: "assistant", text: "open intake" });
    expect(await routePhrase("MindFlock, open intake", "", new PendingChoice())).toEqual({ kind: "assistant", text: "open intake" });
  });

  it("an older server's answer still falls back to a prompt", async () => {
    mocks.api.mockResolvedValueOnce({ kind: "who-knows" });
    expect((await routePhrase("x", "", new PendingChoice())).kind).toBe("prompt");
  });
});

describe("the assistant's voice", () => {
  it("a queued line can name its own engine; the others keep the queue's", async () => {
    const said: string[] = [];
    const sp = (name: string): Speaker => ({
      speak: async (t) => void said.push(name + ":" + t),
      cancel: () => {},
    });
    const q = new SpeechQueue(() => sp("normal"));
    await q.say("Two sessions need you.", { title: "\u0000mindflock", voice: sp("assistant") });
    await q.say("Login fix: done.");
    expect(said).toEqual(["assistant:Two sessions need you.", "normal:Login fix: done."]);
  });

  it("the server speaker can add to /api/voice/speak's body; the mic's assistant speaker sends as: assistant", () => {
    const vd = code("lib/voiceDesktop.ts");
    expect(vd).toContain("body: JSON.stringify({ ...opts.body, text: part })");
    const mic = code("components/TopBarMic.tsx");
    expect(mic).toMatch(/const assistantSp = serverSpeaker\(browserSp, \{[^}]*body: \{ as: "assistant" \}/);
    expect(mic).toContain('voice: ttsEngine(cfgNow) === "server" ? assistantSp : undefined');
  });
});

describe("the mic's wiring (TopBarMic)", () => {
  const mic = code("components/TopBarMic.tsx");

  it("“MindFlock, …” (the router's assistant kind) goes to the assistant", () => {
    expect(mic).toMatch(/if \(reply\.kind === "assistant"\) \{\s*askAssistant\(reply\.text \|\| addressed\(text\) \|\| text\);\s*return true;/);
  });

  it("a prompt with no session (and no text box) goes to the assistant; with one it is typed as ever", () => {
    expect(mic).toMatch(/if \(reply\.kind === "prompt"\) \{\s*if \(!answering && \(into \|\| session\)\) return false;\s*askAssistant\(text\);/);
  });

  it("a question it asked: the next phrase is its (a yes / no before the router)", () => {
    expect(mic).toMatch(/const answering = assistant\.wantsNext\(\);\s*if \(answering && yesNo\(text\) !== null\)/);
    expect(mic).toMatch(/assistantHooks\.asked = \(\) => \{\s*void queue\.drained\(\)\.then/);
  });

  it("assistant mode: nothing typed (no target), every phrase routed, the deterministic commands kept, ended by its words / the stop words / a minute of quiet", () => {
    expect(mic).toContain("opts.assistant ? null : target(into, opts.title)");
    expect(mic).toContain("if (opts.assistant) return assistantRoute(");
    expect(mic).toMatch(/if \(assistantMode\.current && isAssistantEnd\(words\)\) \{\s*endAssistant\(\);\s*return "end";/);
    expect(mic).toMatch(/await routed\(reply, words, session, spokeAt\);\s*return reply\.kind === "stop" \|\| reply\.kind === "off" \|\| reply\.kind === "cancel" \? "end" : true;/);
    expect(mic).toContain("idleMs.current = ASSISTANT_IDLE_MS;");
    expect(mic).toContain("if (opts.assistant) return { target: null, hint: LISTENING_LINE };");
  });

  it("turning it on warms the model and reads the talk level; Alt+M and the menu item toggle it", () => {
    expect(mic).toMatch(/assistant\.setMode\(true\);[\s\S]{0,200}warmAssistant\(\);\s*void refreshCfg\(true\);\s*loadAssistantTalk\(\);/);
    expect(mic).toContain("window.addEventListener(VOICE_ASSISTANT_EVENT, on);");
    expect(mic).toContain('id="mic-assistant"');
    expect(mic).toContain('"Talk to MindFlock"');
    expect(mic).toContain("<AssistantStrip");
  });

  it("barge-in: a new phrase drops what is left of the last answer", () => {
    expect(mic).toMatch(/const askAssistant = \(text: string\) => \{\s*closeCheat\(\);\s*queue\.cancel\(ASSISTANT_LINES\);/);
  });
});
