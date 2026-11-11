// The voice judge's cycle-4 MF2 probes (voice-loop/cycle4/judge-data), kept
// as the regression suite for MF2-residual (cycle 5): a NEW dictation must
// never type into a dialog this page saw before the mic opened — J1 (a
// sub-agent's prompt the pop-up showed, the row stuck on "working") and J2
// (a main dialog the pop-up showed before the next 4 s rows poll) typed
// "yes and also " / "yes " on 7ac99c44. J3–J5 are the judge's controls. Plus:
// a dialog that has really gone (answered, a /dialog 204, the row or event
// leaving it) unblocks typing at once — no stale lockout.
import { afterEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({ instApi: vi.fn() }));
vi.mock("../api/client", async (orig) => {
  const real = (await orig()) as Record<string, unknown>;
  return { ...real, instApi: mocks.instApi };
});

import {
  DialogSeen,
  LiveDictation,
  dialogSeen,
  recheckDialog,
  runVoiceCommand,
  type DictTarget,
  type LiveHooks,
} from "../lib/voiceDesktop";
import type { Dialog } from "../api/types";

function session(): DictTarget & { keys: string[] } {
  const keys: string[] = [];
  return { kind: "session", title: "t1", name: "Login fix", keys, type: (t) => keys.push(t) > 0, erase: (t) => keys.push("⌫" + t) > 0 };
}

function dictation(o: { hooks?: Partial<LiveHooks>; act?: string; continuous?: boolean } = {}) {
  const t = session();
  const st = { act: o.act ?? "working", spoke: 0 };
  const routed: string[] = [];
  const captions: string[] = [];
  const d = new LiveDictation(
    t,
    {
      caption: (l) => void captions.push(l),
      halt: () => {},
      route: async (w) => {
        routed.push(w);
        return false;
      },
      activity: () => st.act,
      replies: () => false,
      submitted: () => {},
      spokeAt: () => st.spoke,
      ...o.hooks,
    },
    { continuous: o.continuous ?? true }
  );
  return { t, st, d, routed, captions };
}

const tick = () => new Promise((r) => setTimeout(r, 5));

afterEach(() => {
  dialogSeen.note("t1", null);
  mocks.instApi.mockReset();
  vi.useRealTimers();
});

describe("JUDGE MF2 probes (cycle 4) — regression suite", () => {
  it("J1 a sub-agent's prompt the pop-up showed (row stays 'working'), then the mic opened on that session: nothing typed, the words go to the router", async () => {
    dialogSeen.note("t1", "d-subagent"); // the pop-up read it: a live, unanswered dialog
    await tick();
    const x = dictation(); // Ctrl+T a moment later, the row still 'working'
    await x.d.heard("yes and also");
    expect(x.t.keys).toEqual([]);
    expect(x.routed).toEqual(["yes and also"]);
    expect(x.d.active).toBe(false);
  });

  it("J1 streamed: the guesses never reach the terminal either", async () => {
    dialogSeen.note("t1", "d-subagent");
    await tick();
    const x = dictation();
    await x.d.stream("", "yes");
    await x.d.stream("", "yes and also");
    await x.d.flush("", "yes and also");
    expect(x.t.keys).toEqual([]);
    expect(x.routed).toEqual(["yes and also"]);
  });

  it("J2 a main-session dialog the pop-up showed before the mic opened, rows cache still 'working' (4 s poll): nothing typed", async () => {
    dialogSeen.note("t1", "d-main");
    await tick();
    const x = dictation({ act: "working" });
    await x.d.heard("yes");
    expect(x.t.keys).toEqual([]);
    expect(x.routed).toEqual(["yes"]);
  });

  it("J2 by the event alone: session.activity_changed → clarify holds typing before /dialog's id or the rows poll is back", async () => {
    dialogSeen.activity("t1", "clarify", "working");
    const x = dictation({ act: "working" });
    await x.d.heard("yes");
    expect(x.t.keys).toEqual([]);
    expect(dialogSeen.since("t1")).toBe(0); // no id yet: it times no answer
  });

  it("J3 a dialog raised after a continuous 'over' send blocks the next message", async () => {
    mocks.instApi.mockResolvedValue({ ok: true });
    const x = dictation();
    await x.d.heard("fix the login over");
    await new Promise((r) => setTimeout(r, 400)); // SUBMIT_SETTLE_MS etc.
    dialogSeen.note("t1", "d-after-send");
    const n = x.t.keys.length;
    await x.d.heard("yes do it");
    expect(x.t.keys.slice(n).join("")).not.toContain("yes");
  });

  it("J4 a dialog up mid-message stops it; a 204 since (the dialog gone) lets the next dictation type", async () => {
    const x = dictation();
    await x.d.heard("please fix the login");
    dialogSeen.note("t1", "d-x");
    await x.d.heard("and add");
    expect(x.t.keys.join("")).not.toContain("and add");
    dialogSeen.note("t1", null); // /dialog said 204
    const y = dictation();
    await y.d.heard("yes");
    expect(y.t.keys).toEqual(["yes "]);
  });

  it("J5 a 'limit' row (usage limit menu) also blocks", async () => {
    const x = dictation();
    await x.d.heard("please fix the login");
    x.st.act = "limit";
    await x.d.heard("one");
    expect(x.t.keys.join("")).not.toContain("one");
  });
});

describe("a dialog that has gone unblocks at once (no stale lockout)", () => {
  const ways: Array<[string, () => void]> = [
    ["answered (by voice, by hand, on the pop-up)", () => dialogSeen.note("t1", null)],
    ["a /dialog 204", () => dialogSeen.note("t1", null)],
    [
      "the row leaving the dialog",
      () => {
        dialogSeen.rows([{ title: "t1", activity: "clarify", asking: { kind: "dialog", dialog_id: "d1" } }]);
        dialogSeen.rows([{ title: "t1", activity: "working", asking: null }]);
      },
    ],
    ["the event leaving the dialog", () => dialogSeen.activity("t1", "working", "clarify")],
  ];
  for (const [how, gone] of ways) {
    it(how, async () => {
      dialogSeen.activity("t1", "clarify", "working");
      dialogSeen.note("t1", "d1");
      const held = dictation();
      await held.d.heard("yes");
      expect(held.t.keys).toEqual([]);
      gone();
      expect(dialogSeen.blocks("t1")).toBe(false);
      const x = dictation();
      await x.d.heard("please fix the login");
      expect(x.t.keys).toEqual(["please fix the login "]);
    });
  }

  it("mid-dictation: the next phrase after the dialog went types (a continuous listen held by a sighting goes on once it clears)", async () => {
    const x = dictation();
    await x.d.heard("please fix the login");
    expect(x.t.keys).toEqual(["please fix the login "]);
    dialogSeen.activity("t1", "working", "clarify"); // nothing up: no change
    await x.d.heard("and add a test");
    expect(x.t.keys).toEqual(["please fix the login ", "and add a test "]);
  });

  it("an event to another non-dialog activity leaves a sub-agent's sighting alone", () => {
    dialogSeen.note("t1", "d-subagent");
    dialogSeen.activity("t1", "idle", "working");
    expect(dialogSeen.blocks("t1")).toBe(true);
  });
});

describe("a held sighting the row doesn't confirm is read again (recheckDialog)", () => {
  const dlg = (id: string) => ({ id }) as Dialog;

  it("a dictation held by a sighting alone asks for /dialog again, at most every 2 s", async () => {
    dialogSeen.note("t1", "d-subagent");
    const asked: string[] = [];
    const x = dictation({ hooks: { dialogCheck: (t) => void asked.push(t) } });
    await x.d.stream("", "yes");
    // "yes and" may still be "yes and don't ask again" (the router's): held
    // whole. Words that can be no command are what would be typed.
    await x.d.stream("", "yes and run the tests");
    expect(asked).toEqual(["t1"]);
  });

  it("not when the row says clarify (the row clears it when it goes)", async () => {
    const asked: string[] = [];
    const x = dictation({ act: "clarify", hooks: { dialogCheck: (t) => void asked.push(t) } });
    await x.d.heard("yes");
    expect(asked).toEqual([]);
  });

  it("gone (two 204s): cleared, so the next words type", async () => {
    const seen = new DialogSeen();
    seen.note("t1", "d-subagent");
    const fetch = vi.fn(async () => null);
    await recheckDialog("t1", fetch, seen, 1);
    expect(fetch).toHaveBeenCalledTimes(2);
    expect(seen.blocks("t1")).toBe(false);
  });

  it("one 204 then the dialog again (a redraw): kept", async () => {
    const seen = new DialogSeen();
    seen.note("t1", "d-subagent");
    const at = seen.since("t1");
    const fetch = vi.fn().mockResolvedValueOnce(null).mockResolvedValueOnce(dlg("d-subagent"));
    await recheckDialog("t1", fetch, seen, 1);
    expect(seen.blocks("t1")).toBe(true);
    expect(seen.since("t1")).toBe(at);
  });

  it("an error changes nothing; another id is noted", async () => {
    const seen = new DialogSeen();
    seen.note("t1", "d-subagent");
    await recheckDialog("t1", async () => Promise.reject(new Error("net")), seen, 1);
    expect(seen.blocks("t1")).toBe(true);
    await recheckDialog("t1", async () => dlg("d-next"), seen, 1);
    expect(seen.at("t1", "d-next")).toBeGreaterThan(0);
  });

  it("nothing held: no read", async () => {
    const fetch = vi.fn(async () => null);
    await recheckDialog("t1", fetch, new DialogSeen(), 1);
    expect(fetch).not.toHaveBeenCalled();
  });
});

describe("the router's prompt path refuses to type into a dialog raised by the event", () => {
  it("a 'yes' that isn't an answer (no dialog_id) is not typed as a prompt", async () => {
    dialogSeen.activity("t1", "clarify", "working");
    const said: string[] = [];
    await runVoiceCommand(
      { kind: "answer", title: "t1" } as never,
      "yes",
      {
        railOrder: () => [],
        focused: () => "t1",
        select: () => {},
        name: () => "Login fix",
        caption: () => {},
        announce: (l: string) => void said.push(l),
        speak: () => {},
        route: async () => ({ kind: "prompt" }) as never,
        approveDialogs: async () => true,
        activity: () => "working",
        replies: () => false,
        submitted: () => {},
      } as never,
      Date.now()
    );
    expect(said).toEqual(["Login fix is on a dialog — answer it on the screen."]);
  });
});

describe("wired (TopBarMic)", () => {
  it("the activity event marks/clears at once and reads the id; a held dictation asks for a recheck; a sighting makes a lone answer go to the router at once", async () => {
    const { code } = await import("./sourceText");
    const MIC = code("components/TopBarMic.tsx");
    expect(MIC).toContain("dialogSeen.activity(env.session, env.new, env.old);");
    expect(MIC).toContain("if (dl?.id) dialogSeen.note(env.session, dl.id);");
    expect(MIC).toContain("dialogCheck: (title) => void recheckDialog(title, (t) => fetchDialog(t)),");
    expect(MIC).toContain('pending: (title) => liveActivity(title) === "clarify" || dialogSeen.blocks(title) || questions.has(title),');
  });
});
