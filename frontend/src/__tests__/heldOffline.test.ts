/** Held offline after a crash (backend core/held.py; lib/heldOffline).
 *
 * The WSL machine restarted twice under memory pressure, and every boot
 * relaunched every agent — the revive sweep, the auto-revive, and each grid
 * pane's terminal socket — straight back into the pressure that killed them.
 * A session whose agent died is now HELD: nothing starts it but a person's
 * Start. The server refuses its terminal socket with 4424 and its /send with
 * 409 `{"error", "held": true}` (the prompt is queued); the frontend shows a
 * placeholder with Start, never retries a 4424, says "Offline · Start" on the
 * rail and the Sky tile, and toasts "Queued" for a held send. */

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import type { Instance } from "../api/types";
import { code } from "./sourceText";

vi.mock("../lib/revive", () => ({
  reviveSession: vi.fn(() => Promise.resolve(true)),
  startSession: vi.fn(() => Promise.resolve({ title: "s", revived: true })),
}));

const { ApiError } = await import("../api/client");
const {
  HELD_CHIP,
  HELD_TEXT,
  heldHint,
  heldLine,
  heldNoticeHidden,
  heldNoticeKey,
  heldNoticeText,
  heldQueuedText,
  heldRows,
  heldSince,
  isHeldOffline,
  isHeldRefusal,
} = await import("../lib/heldOffline");
const { closeAction } = await import("../lib/terminals");
const { HELD_CLOSE, PAUSED_CLOSE, retryAfter } = await import("../lib/wsTerm");
const { HeldBody } = await import("../components/grid/HeldBody");
const { deriveSessionState } = await import("../lib/sessionState");
const { REVIVE_LABEL, REVIVE_NOTE } = await import("../components/settings/screens/General");

const row = (o: Partial<Instance>): Instance =>
  ({
    title: "s",
    branch: "feat/x",
    status: "running",
    stage: "agent",
    activity: "offline",
    activity_since: 0,
    ...o,
  }) as unknown as Instance;

const HELD = { held_offline: true, held_since: 1_800_000_000, held_reason: "the server restarted" };

describe("the held close code", () => {
  it("is 4424, final for both terminal implementations", () => {
    expect(HELD_CLOSE).toBe(4424);
    expect(PAUSED_CLOSE).toBe(4423);
    expect(closeAction(4424)).toBe("held");
    expect(closeAction(4423)).toBe("paused");
    expect(closeAction(1006)).toBe("retry");
    // Never auto-retried, even with reconnect on, even on the first close.
    expect(retryAfter(4424, 0, true)).toBe(null);
    expect(retryAfter(4424, 7, true)).toBe(null);
  });

  it("is said as held by the shared ws terminal, and a refused terminal is rebuilt after Start", () => {
    expect(code("lib/wsTerm.ts")).toContain('ev.code === HELD_CLOSE ? "held"');
    expect(code("lib/terminals.ts")).toContain('if (h && (h.state === "paused" || h.state === "held")) {');
  });
});

describe("the words", () => {
  it("says why it is down, and why it stays down", () => {
    expect(heldSince("the server restarted")).toBe("Offline since the server restarted.");
    expect(heldSince("out of memory (killed).")).toBe("Offline since out of memory (killed).");
    expect(heldSince("")).toBe("Offline since a crash.");
    expect(heldSince(undefined)).toBe("Offline since a crash.");
    expect(heldLine("the agent crashed")).toBe(
      "Offline since the agent crashed. It won't start by itself, so it uses no memory."
    );
    expect(heldHint("the agent crashed")).toContain("Press Start");
    expect(HELD_CHIP).toBe("Offline · Start");
  });

  it("counts the notice in plain words", () => {
    expect(heldNoticeText(1)).toBe("1 session stayed offline after a crash, so it uses no memory. Start the ones you need.");
    expect(heldNoticeText(3)).toBe("3 sessions stayed offline after a crash, so they use no memory. Start the ones you need.");
  });

  it("toasts a send to a held session as queued, not failed", () => {
    expect(heldQueuedText("fix-auth")).toBe("Queued — fix-auth is offline; press Start to send it.");
  });

  it("never says revive or restart on a held surface", () => {
    const said = [HELD_CHIP, HELD_TEXT, heldLine("x"), heldHint("x"), heldNoticeText(2), heldQueuedText("n"), REVIVE_LABEL, REVIVE_NOTE];
    for (const s of said) expect(s).not.toMatch(/revive|restart/i);
    for (const f of ["components/grid/HeldBody.tsx", "lib/heldOffline.ts"]) {
      // Every string literal but the import paths ("../../lib/revive").
      const strings = (code(f).replace(/^import .*$/gm, "").match(/"[^"]*"/g) || []);
      for (const s of strings) expect(s).not.toMatch(/revive|restart/i);
    }
  });
});

describe("isHeldOffline / heldRows", () => {
  it("reads the row's held_offline, on this device only", () => {
    expect(isHeldOffline(row(HELD))).toBe(true);
    expect(isHeldOffline(row({}))).toBe(false);
    expect(isHeldOffline(row({ ...HELD, device: "rig" }))).toBe(false);
    expect(isHeldOffline(row({ ...HELD, status: "paused" }))).toBe(false);
    expect(isHeldOffline(row({ ...HELD, workspace_missing: true }))).toBe(false);
    expect(isHeldOffline(null)).toBe(false);
  });

  it("the notice counts held rows, never a sleeping or pending one", () => {
    const rows = [
      row({ ...HELD, title: "a" }),
      row({ ...HELD, title: "b", sleeping: { since: 1, reason: "idle" } as Instance["sleeping"] }),
      row({ ...HELD, title: "c", pending: true } as Partial<Instance>),
      row({ title: "d" }),
    ];
    expect(heldRows(rows).map((r) => r.title)).toEqual(["a"]);
  });
});

describe("the notice's ×", () => {
  it("stays hidden for the same sessions, or fewer, and comes back for a new one", () => {
    const ab = heldNoticeKey([{ title: "b" }, { title: "a" }]);
    expect(ab).toBe("a\nb");
    expect(heldNoticeHidden(ab, ab)).toBe(true);
    expect(heldNoticeHidden(ab, heldNoticeKey([{ title: "a" }]))).toBe(true);
    expect(heldNoticeHidden(ab, heldNoticeKey([{ title: "a" }, { title: "c" }]))).toBe(false);
    expect(heldNoticeHidden("", ab)).toBe(false);
    expect(heldNoticeHidden(null, ab)).toBe(false);
  });
});

describe("isHeldRefusal", () => {
  it("is the /send 409 with held: true, nothing else", () => {
    expect(isHeldRefusal(new ApiError(409, "offline since a crash — press Start to bring it back", { error: "x", held: true }))).toBe(true);
    expect(isHeldRefusal(new ApiError(409, "paused", { error: "x", paused: true }))).toBe(false);
    expect(isHeldRefusal(new ApiError(500, "x", { held: true }))).toBe(false);
    expect(isHeldRefusal(new Error("held"))).toBe(false);
    expect(isHeldRefusal(null)).toBe(false);
  });

  it("is what the send paths check before calling it a failure", () => {
    for (const f of ["components/grid/QueueTab.tsx", "components/grid/ThreadTab.tsx", "components/flights/useChainMenu.tsx", "lib/playbooks.ts"])
      expect(code(f)).toContain("isHeldRefusal(");
    expect(code("lib/voiceDesktop.ts")).toContain("body?.held");
  });
});

describe("the session state", () => {
  it("is Offline with Start, out of the bell (the rail's one notice says it)", () => {
    const ss = deriveSessionState(row(HELD), { activity: "offline" });
    expect(ss.state).toBe("offline");
    expect(ss.label).toBe("Offline");
    expect(ss.held).toBe(true);
    expect(ss.restartable).toBe(true);
    expect(ss.inBell).toBe(false);
    expect(ss.hint).toBe(heldHint("the server restarted"));
  });

  it("whatever the activity reads, and never for a paused or sleeping session", () => {
    expect(deriveSessionState(row(HELD), { activity: "idle" }).held).toBe(true);
    expect(deriveSessionState(row({ ...HELD, status: "paused" })).label).toBe("Paused");
    expect(deriveSessionState(row({ ...HELD, sleeping: { since: 1, reason: "idle" } as Instance["sleeping"] })).held).toBeFalsy();
    // A plain crashed agent (not held) is still in the bell with Restart.
    const plain = deriveSessionState(row({}), { activity: "offline" });
    expect(plain.held).toBeFalsy();
    expect(plain.inBell).toBe(true);
  });
});

describe("HeldBody", () => {
  it("says why and offers Start, with no terminal", () => {
    const html = renderToStaticMarkup(createElement(HeldBody, { title: "s", reason: "out of memory (killed)", hidden: false }));
    expect(html).toContain("Offline since out of memory (killed). It won&#x27;t start by itself, so it uses no memory.");
    expect(html).toContain("▶ Start");
    expect(html).not.toContain("term-container");
    expect(html).not.toContain(" hidden");
  });

  it("hides with the Agent tab like the terminal it replaces", () => {
    const html = renderToStaticMarkup(createElement(HeldBody, { title: "s", hidden: true }));
    expect(html).toContain("pane-term agent-term pane-paused pane-held hidden");
    expect(html).toContain("Offline since a crash.");
  });

  it("an agent that isn't installed: says so and offers Install, not a Start that fails again", async () => {
    const { queryClient } = await import("../state/queries");
    queryClient.setQueryData(["instances"], [row({ title: "s", program: "codex", ...HELD })]);
    queryClient.setQueryData(["setup"], {
      default_provider: "claude",
      logins: [{ name: "codex", installed: false, logged_in: null, login_cmd: "", default: false }],
    });
    try {
      let html = renderToStaticMarkup(createElement(HeldBody, { title: "s", reason: "the agent crashed", hidden: false }));
      expect(html).toContain("Codex isn&#x27;t installed on this computer");
      expect(html).toContain("Install Codex…");
      expect(html).not.toContain("▶ Start");
      expect(html).not.toContain("crashed");
      // Installed since (Setup re-read): Start is back.
      queryClient.setQueryData(["setup"], {
        default_provider: "claude",
        logins: [{ name: "codex", installed: true, logged_in: true, login_cmd: "", default: false }],
      });
      html = renderToStaticMarkup(createElement(HeldBody, { title: "s", reason: "the agent crashed", hidden: false }));
      expect(html).toContain("▶ Start");
      expect(html).not.toContain("Install Codex");
    } finally {
      queryClient.removeQueries({ queryKey: ["instances"] });
      queryClient.removeQueries({ queryKey: ["setup"] });
    }
  });

  it("starts through the same POST as the chip, asking nothing through window dialogs", () => {
    const src = code("components/grid/HeldBody.tsx");
    expect(src).toContain("startSession(title)");
    expect(src).toContain("if (r) onStarted?.();");
    expect(src).not.toMatch(/\b(prompt|alert|confirm)\(/);
  });
});

describe("Pane", () => {
  const src = code("components/grid/Pane.tsx");

  it("renders HeldBody instead of mounting the agent terminal while held", () => {
    expect(src).toContain("const heldRow = !paused && isHeldOffline(inst);");
    expect(src).toContain('const heldOff = !paused && ((heldRow && !started) || wsState === "held");');
    expect(src).toContain('if (kind === "agent" && heldOff) return;');
    expect(src).toContain("<HeldBody");
  });

  it("drops an open agent terminal when the session becomes held, and remounts after Start", () => {
    expect(src).toMatch(/if \(!heldNow\) return;\s*releaseTerm\(title, "agent"\);/);
    expect(src).toMatch(/onStarted=\{\(\) => \{\s*setStarted\(true\);/);
  });
});

describe("the rail and the Sky", () => {
  it("the rail chip says Offline · Start and starts it", () => {
    const src = code("components/sidebar/SidebarRow.tsx");
    expect(src).toContain(") : ss.held && !paused ? (");
    expect(src).toContain("startSession(title)");
  });

  it("the Sky tile's state chip is the Start button for a held session", () => {
    const src = code("components/flights/SkyLine.tsx");
    expect(src).toContain("export function SkyStateChip(");
    expect(src).toContain("{busy ? \"Starting…\" : HELD_CHIP}");
    expect((src.match(/<SkyStateChip /g) || []).length).toBe(2);
  });

  it("the offline bar says Start all, never Restart all", () => {
    const src = code("components/sidebar/OfflineBar.tsx");
    expect(src).toContain("▶ Start all");
    expect(src).not.toContain("Restart all");
    expect(src).toContain("heldNoticeText(held.length)");
  });
});

describe("Settings → General", () => {
  it("names the switch for what it does, and reads unset as off", () => {
    expect(REVIVE_LABEL).toBe("Bring agents back by themselves after a crash");
    expect(REVIVE_NOTE).toBe(
      "Off: sessions whose agent died stay Offline until you press Start, so a crash can't refill memory."
    );
    expect(code("components/settings/screens/General.tsx")).toContain(
      'const on = s.get("general", "revive_on_start") === true;'
    );
  });
});

describe("the Queued toast only when the server kept the prompt", () => {
  it("queued:false (a paste, Enter, dictation) says send it again; queued or absent says Queued", async () => {
    const { ApiError } = await import("../api/client");
    const { heldWasQueued, heldQueuedText } = await import("../lib/heldOffline");
    const mk = (body: unknown) => new ApiError(409, "held", body as never);
    expect(heldWasQueued(mk({ held: true, queued: false }))).toBe(false);
    expect(heldWasQueued(mk({ held: true, queued: true }))).toBe(true);
    expect(heldWasQueued(mk({ held: true }))).toBe(true);
    expect(heldQueuedText("bot", false)).toContain("send it again");
    expect(heldQueuedText("bot")).toMatch(/^Queued/);
  });
});
