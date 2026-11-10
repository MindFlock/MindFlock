/** The desktop's command gate against the router. A first phrase reaches
 * POST /api/voice/route only when the gate (lib/voiceDesktop mayBeCommand,
 * and mayAddress for a phrase said to MindFlock) lets it; the rest is typed
 * into the session and the router never hears it. The gate had drifted:
 * ~45 phrasings the router takes were typed. tests/unit/data/
 * voice_gate_phrases.json pins them — the router's own test
 * (test_voice_gate_phrases.py) and the phone's read the same file:
 *  - `route`: every phrase the router takes passes the gate whole;
 *  - `want`: the wake word's mishearings pass too ("pause" stays words);
 *  - `text`: ordinary prompts never pass whole, and are typed live once
 *    their words say they are none — the gate never holds a prompt whole.
 */
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { Dictation, addressedRoute, isFastCommand, mayAddress, mayBeCommand } from "../lib/voiceDesktop";
import { applyVoiceView, resetVoiceWords } from "../lib/voiceWords";

interface Fixture {
  sessions: string[];
  route: [string, string][];
  want: string[];
  text: string[];
}
const fs = (await import(/* @vite-ignore */ "node:" + "fs")) as { readFileSync(p: string, enc: string): string };
const at = (p: string) => decodeURIComponent(new URL(p, import.meta.url).pathname);
const F = JSON.parse(fs.readFileSync(at("../../../tests/unit/data/voice_gate_phrases.json"), "utf8")) as Fixture;

// What the recogniser writes for the wake word comes with GET /api/voice
// (`words.heard.wake`: core/voice_words.HEARD["mindflock"]) — read from the
// server's own table, so the gate is tested with what the page really has.
const SERVER_WORDS = fs.readFileSync(at("../../../backend/web/core/voice_words.py"), "utf8");
const HEARD_WAKE = [...(/"mindflock": \(([\s\S]*?)\),/.exec(SERVER_WORDS)?.[1] || "").matchAll(/"([^"]+)"/g)].map((m) => m[1]);

beforeEach(() => applyVoiceView({ words: { heard: { wake: HEARD_WAKE } } }));
afterEach(() => resetVoiceWords());

/** The gate a phrase meets at its end (the router is asked). */
const routes = (p: string) => mayBeCommand(p, true) || mayAddress(p);
/** The gate a streamed guess meets: held (not typed) while true. */
const holds = (p: string) => mayBeCommand(p) || mayAddress(p);
const prefixes = (p: string) => {
  const w = p.split(" ");
  return w.map((_, i) => w.slice(0, i + 1).join(" "));
};

describe("the gate lets every phrase the router takes reach it", () => {
  it("reads the server's heard wake forms", () => {
    expect(HEARD_WAKE).toContain("my flock");
    expect(F.route.length).toBeGreaterThan(80);
  });

  it.each(F.route)("%j (the router's %s) is routed", (phrase) => {
    expect(routes(phrase)).toBe(true);
  });

  it.each(F.want.filter((p) => p !== "pause"))("%j (a wake word misheard) is held and routed", (phrase) => {
    expect(routes(phrase)).toBe(true);
    for (const p of prefixes(phrase)) expect(holds(p), p).toBe(true);
  });

  it("“pause” stays words: it could mean the mic or the agent", () => {
    expect(F.want).toContain("pause");
    expect(mayBeCommand("pause", true)).toBe(false);
  });

  it("a streamed guess of a route phrase is held from its first word, but for the few said name-first or past a short name", () => {
    // Held word by word: nothing typed only to be taken back. The exceptions
    // are decided at the phrase's end (its guess taken back then).
    const flashes = F.route
      .map(([p]) => p)
      .filter((p) => !mayAddress(p))
      .filter((p) => prefixes(p).some((q) => !mayBeCommand(q)));
    expect(flashes).toEqual(["docs yes"]);
  });

  it.each([
    ["um, what's docs doing"],
    ["okay so, status"],
    ["can you go to the next session"],
    ["alright next session"],
    ["could you switch to docs please"],
    ["now scroll up for me"],
  ])("fillers before and “please” after are the router's to drop: %j", (p) => {
    expect(routes(p)).toBe(true);
  });

  it("a name runs past the streamed bound once the phrase is whole", () => {
    expect(mayBeCommand("what does sitecheck bot 7 want")).toBe(false);
    expect(mayBeCommand("what does sitecheck bot 7 want", true)).toBe(true);
    expect(mayBeCommand("what's the sitecheck bot doing", true)).toBe(true);
  });

  it("sessions are numbered past nine", () => {
    for (const p of ["session ten", "session twelve", "session twenty one", "session 42", "row 2", "window number 2", "go to row two"])
      expect(routes(p), p).toBe(true);
  });
});

describe("ordinary dictation is typed — live, never held whole", () => {
  it.each(F.text)("%j is no command", (phrase) => {
    expect(mayBeCommand(phrase, true)).toBe(false);
    expect(mayAddress(phrase)).toBe(false);
    // Once a prefix is let go it stays let go: the typing never stops to
    // hold again, and the end of the phrase is typed as it comes.
    const p = prefixes(phrase);
    const first = p.findIndex((q) => !holds(q));
    expect(first, "never typed before its end").toBeGreaterThanOrEqual(0);
    expect(first, "held to its last word").toBeLessThan(p.length - 1);
    for (const q of p.slice(first)) expect(holds(q), q).toBe(false);
  });

  it.each([
    "fix the login bug",
    "what does this function do",
    "status of the tests",
    "the tests are failing again",
    "more tests please",
    "a bug in the parser",
    "show me how the parser handles nested quotes",
  ])("%j types within its first words", (phrase) => {
    const p = prefixes(phrase);
    expect(p.findIndex((q) => !holds(q))).toBeLessThanOrEqual(5);
  });
});

describe("a streamed first phrase, end to end (Dictation)", () => {
  const first = (words: string[], final: string) => {
    const d = new Dictation();
    d.begin();
    const typed: string[] = [];
    let partial = "";
    for (const w of words) {
      partial = (partial + " " + w).trim();
      const o = d.hearing(partial);
      if (o.kind === "type") {
        typed.push(o.text);
        d.wrote(o.text);
      }
    }
    return { typed, end: d.heard(final) };
  };

  it("“take me to the sitecheck session” is never typed: it waits whole for the router", () => {
    const { typed, end } = first("take me to the sitecheck session".split(" "), "Take me to the sitecheck session.");
    expect(typed).toEqual([]);
    expect(end).toMatchObject({ kind: "type", text: "Take me to the sitecheck session. " });
    expect(end).not.toHaveProperty("erase");
  });

  it("“what's docs doing” — the app's own advertised phrasing — waits whole", () => {
    const { typed } = first("what's docs doing".split(" "), "What's docs doing?");
    expect(typed).toEqual([]);
  });

  it("“docs yes” is typed as a guess, then taken back whole at its end for the router", () => {
    const { typed, end } = first(["docs", "yes"], "Docs, yes.");
    expect(typed.join("")).toBe("docs yes");
    expect(end).toMatchObject({ kind: "type", text: "Docs, yes. ", erase: "docs yes" });
  });

  it("a prompt types as it comes", () => {
    const { typed } = first("open the settings file and check the port".split(" "), "Open the settings file and check the port.");
    expect(typed.join("")).toContain("check the port");
  });
});

describe("the fast guess acts only on commands without a name", () => {
  it.each(["next session", "previous one", "session three", "session twenty one", "scroll up", "page down", "switch back", "focus 3"])(
    "%j acts from the guess",
    (p) => expect(isFastCommand(p)).toBe(true)
  );
  it.each(["switch to the login fix", "go to docs", "select the login fix", "session three yes", "take me to docs"])(
    "%j waits for the accurate final",
    (p) => expect(isFastCommand(p)).toBe(false)
  );
});

describe("a phrase said to MindFlock (addressedRoute)", () => {
  it.each([
    ["open settings", "MindFlock, open settings"],
    ["what's on the schedule", "MindFlock, what's on the schedule"],
    ["turn on the bell", "MindFlock, turn on the bell"],
    ["next session", "next session"],
    ["status", "status"],
    ["MindFlock, open intake", "MindFlock, open intake"],
  ])("%j goes to the router as %j", (p, want) => {
    expect(addressedRoute(p)).toBe(want);
  });
  it.each(["fix the login bug", "run the tests again", ""])("%j is dictation", (p) => {
    expect(addressedRoute(p)).toBeNull();
  });
});
