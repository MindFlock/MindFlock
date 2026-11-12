/** The words follow the focus (LiveDictation.follow, TopBarMic's focus
 * subscription): listening into "the focused session" and then looking at
 * another session used to keep typing into the first one. */
import { afterEach, describe, expect, it, vi } from "vitest";
import { code } from "./sourceText";

vi.mock("../api/client", () => ({ api: vi.fn(async () => ({})), apiRaw: vi.fn(), instApi: vi.fn(), ApiError: Error }));

import { FOLLOW_WAIT_MS, LiveDictation, followLine, type DictTarget, type LiveHooks } from "../lib/voiceDesktop";

function session(title: string): DictTarget & { text: string } {
  const t = {
    kind: "session" as const,
    title,
    name: title,
    text: "",
    type(s: string) {
      t.text += s;
      return true;
    },
    erase(s: string) {
      if (t.text.endsWith(s)) t.text = t.text.slice(0, t.text.length - s.length);
      return true;
    },
  };
  return t;
}

function setup() {
  const a = session("alpha");
  const b = session("beta");
  const captions: string[] = [];
  let focused: DictTarget = a;
  let ended = 0;
  const hooks: LiveHooks = {
    caption: (t) => void captions.push(t),
    halt: () => {},
    route: async () => false,
    retarget: () => ({ target: focused }),
    hush: () => {},
    activity: () => "idle",
    replies: () => false,
    submitted: () => {},
    endPhrase: () => void ended++,
  };
  const d = new LiveDictation(a, hooks);
  return { a, b, d, captions, focus: (t: DictTarget) => void (focused = t), ended: () => ended };
}

afterEach(() => vi.useRealTimers());

describe("looking at another session moves the words there", () => {
  it("between phrases: what was typed stays, the next phrase goes to the new session", async () => {
    const { a, b, d, captions, focus } = setup();
    await d.heard("fix the login bug");
    expect(a.text.trim()).toBe("fix the login bug");
    focus(b);
    d.follow();
    await d.heard("and add a test");
    expect(a.text.trim()).toBe("fix the login bug");
    expect(b.text.trim()).toBe("and add a test");
    expect(captions).toContain(followLine("beta"));
    expect(d.active).toBe(true);
  });

  it("mid-phrase: the phrase is ended and finishes where it began", async () => {
    const { a, b, d, focus, ended } = setup();
    await d.stream("fix the", "login");
    expect(a.text).toContain("fix the");
    focus(b);
    d.follow();
    expect(ended()).toBe(1);
    // Its late words and its final still belong to the first session.
    await d.flush("login bug", "Fix the login bug.");
    expect(a.text.trim()).toBe("Fix the login bug.");
    expect(b.text).toBe("");
    await d.heard("next one");
    expect(b.text.trim()).toBe("next one");
    expect(a.text.trim()).toBe("Fix the login bug.");
  });

  it("a phrase that never ends moves anyway after a moment", async () => {
    vi.useFakeTimers();
    const { a, b, d, focus } = setup();
    await d.stream("hello", "");
    focus(b);
    d.follow();
    await vi.advanceTimersByTimeAsync(FOLLOW_WAIT_MS + 10);
    await d.heard("over there");
    expect(b.text.trim()).toBe("over there");
    expect(a.text).toContain("hello");
  });

  it("the same session again (a spoken switch already moved it): no caption, nothing lost", async () => {
    const { a, d, captions } = setup();
    await d.heard("one");
    d.follow();
    await d.heard("two");
    expect(captions.some((c) => c.startsWith("Now typing into"))).toBe(false);
    expect(a.text).toContain("one");
    expect(a.text).toContain("two");
  });

  it("after the listen ended: nothing", async () => {
    const { b, d, focus } = setup();
    d.fail("stopped");
    focus(b);
    d.follow();
    await d.heard("late");
    expect(b.text).toBe("");
  });
});

describe("the top bar's mic", () => {
  const tsx = code("components/TopBarMic.tsx");
  it("follows the focus only for a listen into the focused session", () => {
    expect(tsx).toContain("followsFocus.current = !into && !opts.assistant;");
    expect(tsx).toContain("if (s.focused && followsFocus.current) live.current?.follow();");
    expect(tsx).toContain("followsFocus.current = false;");
  });
});
