/** The launch gate's frontend (backend core/launch_gate.py): a person's start
 * asks first while memory is low (lib/memoryConfirm — the app's own dialog,
 * since Electron has no window.confirm), a session whose automatic start is
 * queued reads "Waiting for memory" with Start anyway, and the rail says
 * "Memory low — new agents wait" while the gate holds anything. */

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import type { Instance, LaunchGateStatus, LaunchWait } from "../api/types";
import { code } from "./sourceText";

const { ApiError } = await import("../api/client");
const mc = await import("../lib/memoryConfirm");
const { holdingLine, launchGateDeps, startWaiting, cancelWaiting } = await import("../lib/launchGate");
const { deriveSessionState, WAITING_FOR_MEMORY_LABEL } = await import("../lib/sessionState");
const { sweepToast } = await import("../components/sidebar/OfflineBar");
const { MemoryConfirm } = await import("../components/MemoryConfirm");

const LOW = {
  memory_confirm: true,
  level: "low",
  message: "Memory is low: 3.1 GB free. Starting another agent may freeze the machine.",
  error: "memory is low — pass start_anyway to start it regardless",
};

function refusal(body: unknown = LOW) {
  return new ApiError(409, "memory is low", body);
}

function wait(over: Partial<LaunchWait> = {}): LaunchWait {
  return {
    id: "lq1",
    title: "w1",
    kind: "spawn",
    since: 1,
    reason: "memory is low: 2.0 GB free of 20.0 GB (10%)",
    position: 1,
    creates: true,
    ...over,
  };
}

describe("memoryConfirmOf", () => {
  it("reads only a 409 memory_confirm body", () => {
    expect(mc.memoryConfirmOf(refusal())?.level).toBe("low");
    expect(mc.memoryConfirmOf(refusal({ ...LOW, level: "critical" }))?.level).toBe("critical");
    expect(mc.memoryConfirmOf(new ApiError(409, "exists", { error: "exists" }))).toBeNull();
    expect(mc.memoryConfirmOf(new ApiError(500, "x", LOW))).toBeNull();
    expect(mc.memoryConfirmOf(new Error("x"))).toBeNull();
  });
});

describe("withMemoryConfirm", () => {
  it("goes straight through when the server starts it", async () => {
    const ask = vi.spyOn(mc.memoryConfirmDeps, "ask");
    const send = vi.fn(async (anyway: boolean) => (anyway ? "anyway" : "plain"));
    await expect(mc.withMemoryConfirm("x", send)).resolves.toBe("plain");
    expect(ask).not.toHaveBeenCalled();
    ask.mockRestore();
  });

  it("Start anyway resends with start_anyway", async () => {
    const ask = vi.spyOn(mc.memoryConfirmDeps, "ask").mockResolvedValue("start");
    const calls: boolean[] = [];
    const send = async (anyway: boolean) => {
      calls.push(anyway);
      if (!anyway) throw refusal();
      return "started";
    };
    await expect(mc.withMemoryConfirm("s1", send)).resolves.toBe("started");
    expect(calls).toEqual([false, true]);
    expect(ask).toHaveBeenCalledWith(expect.objectContaining({ level: "low" }), "s1", true);
    ask.mockRestore();
  });

  it("Cancel starts nothing", async () => {
    const ask = vi.spyOn(mc.memoryConfirmDeps, "ask").mockResolvedValue("cancel");
    const send = vi.fn(async (anyway: boolean) => {
      if (!anyway) throw refusal();
      return "started";
    });
    const err = await mc.withMemoryConfirm("s1", send).catch((e) => e);
    expect(mc.isMemoryCancel(err)).toBe(true);
    expect(send).toHaveBeenCalledTimes(1);
    ask.mockRestore();
  });

  it("sleep first: sleeps the idle agents, then tries again — once", async () => {
    const ask = vi.spyOn(mc.memoryConfirmDeps, "ask").mockResolvedValue("sleep");
    const sleepIdle = vi
      .spyOn(mc.memoryConfirmDeps, "sleepIdle")
      .mockResolvedValue({ slept: ["a", "b"], freed_mb: 1300 });
    const heard: string[] = [];
    let n = 0;
    const send = async () => {
      n += 1;
      if (n === 1) throw refusal();
      return "started";
    };
    await expect(mc.withMemoryConfirm("s1", send, (a) => heard.push(mc.sleptLine(a)))).resolves.toBe("started");
    expect(sleepIdle).toHaveBeenCalledTimes(1);
    expect(heard).toEqual(["Put 2 idle agents to sleep (freed ~1.3 GB)."]);
    // Still low after the sleep: the second ask offers no sleep, and a
    // second "sleep" answer is a cancel, never a silent Start anyway.
    n = 0;
    const always = async (anyway: boolean) => {
      if (!anyway) throw refusal();
      return "started";
    };
    const err = await mc.withMemoryConfirm("s1", always).catch((e) => e);
    expect(mc.isMemoryCancel(err)).toBe(true);
    expect(ask.mock.calls.at(-1)?.[2]).toBe(false);
    ask.mockRestore();
    sleepIdle.mockRestore();
  });

  it("rethrows any other failure", async () => {
    const send = async () => {
      throw new ApiError(409, "workspace no longer exists", { error: "workspace no longer exists" });
    };
    await expect(mc.withMemoryConfirm("s1", send)).rejects.toThrow("workspace no longer exists");
  });

  it("sleptLine says when nothing could sleep", () => {
    expect(mc.sleptLine({ slept: [], freed_mb: 0 })).toMatch(/none was put to sleep/);
    expect(mc.sleptLine({ slept: ["a"], freed_mb: 400 })).toBe("Put 1 idle agent to sleep (freed ~400 MB).");
  });
});

describe("the dialog", () => {
  it("renders the numbers and the three choices; Cancel/sleep settle the ask", async () => {
    const p = mc.askMemoryConfirm(mc.memoryConfirmOf(refusal())!, "s1");
    const html = renderToStaticMarkup(createElement(MemoryConfirm));
    expect(html).toContain("Memory is low");
    expect(html).toContain("3.1 GB free");
    expect(html).toContain("Start anyway");
    expect(html).toContain("Put idle agents to sleep first");
    expect(html).toContain("Cancel");
    mc.currentMemoryConfirm()!.resolve("sleep");
    await expect(p).resolves.toBe("sleep");
    expect(mc.currentMemoryConfirm()).toBeNull();
    expect(renderToStaticMarkup(createElement(MemoryConfirm))).toBe("");
  });

  it("critical says so and hides the sleep choice once it was tried", async () => {
    const p = mc.askMemoryConfirm(mc.memoryConfirmOf(refusal({ ...LOW, level: "critical" }))!, "s1", false);
    const html = renderToStaticMarkup(createElement(MemoryConfirm));
    expect(html).toContain("Memory is critically low");
    expect(html).not.toContain("Put idle agents to sleep first");
    mc.currentMemoryConfirm()!.resolve("cancel");
    await expect(p).resolves.toBe("cancel");
  });

  it("is the app's own dialog, never window.confirm", () => {
    const src = code("lib/memoryConfirm.ts") + code("components/MemoryConfirm.tsx");
    expect(src).not.toMatch(/window\.(confirm|prompt|alert)\(/);
    expect(code("App.tsx")).toContain("<MemoryConfirm />");
  });
});

describe("every person's start goes through the confirm", () => {
  it.each([
    ["lib/revive.ts", '"/revive"'],
    ["lib/wake.ts", '"/wake"'],
    ["lib/sessionActions.ts", '"/copy"'],
    ["lib/sessionActions.ts", '"/resume"'],
    ["components/sidebar/OfflineBar.tsx", '"/api/server/revive-all"'],
    ["lib/newSessionSubmit.ts", '"/api/instances"'],
    ["components/flights/NewSessionBlock.tsx", '"/api/instances"'],
  ])("%s %s", (file, path) => {
    // Some withMemoryConfirm( call wraps this request, and its retry carries
    // start_anyway.
    const src = code(file);
    const wraps = src
      .split("withMemoryConfirm(")
      .slice(1)
      .map((rest) => rest.slice(0, 500));
    expect(wraps.some((w) => w.includes(path) && w.includes("start_anyway: true"))).toBe(true);
  });
});

describe("Waiting for memory", () => {
  it("is the row's state, with Start anyway as its remedy", () => {
    const ss = deriveSessionState({ title: "w1", status: "loading", pending: true, waiting_for_memory: wait() } as Partial<Instance>);
    expect(ss.label).toBe(WAITING_FOR_MEMORY_LABEL);
    expect(ss.waiting).toBe(true);
    expect(ss.inBell).toBe(false);
    expect(ss.hint).toContain("memory is low: 2.0 GB free");
    expect(ss.hint).toContain("Start anyway");
    const third = deriveSessionState({ title: "x", status: "running", waiting_for_memory: wait({ position: 3 }) } as Partial<Instance>);
    expect(third.hint).toContain("3rd in line");
    expect(third.hint).toContain("Stop waiting");
    expect(deriveSessionState({ title: "x", status: "loading", pending: true } as Partial<Instance>).waiting).toBeUndefined();
  });

  it("the rail chip starts it anyway, and Stop waiting sits beside it", () => {
    const src = code("components/sidebar/SidebarRow.tsx");
    expect(src).toContain("ss.waiting && !inst.device");
    expect(src).toContain("startWaiting(title)");
    expect(src).toContain("cancelWaiting(title, !!wait?.creates)");
    expect(src).toMatch(/>\s*Stop waiting\s*</);
  });

  it("the rail line while the gate holds anything", () => {
    const g = (over: Partial<LaunchGateStatus>): LaunchGateStatus => ({
      holding: true,
      queued: [wait(), wait({ id: "lq2", title: "w2", position: 2 })],
      reason: "memory is low: 2.0 GB free of 20.0 GB (10%)",
      live: 10,
      cap: 16,
      warming: 0,
      projected_available: null,
      projected_level: "low",
      ...over,
    });
    expect(holdingLine(g({}))).toBe("Memory low — new agents wait (2 waiting)");
    expect(holdingLine(g({ reason: "16 agents are running; automatic starts stop at 16 on this machine" }))).toBe(
      "Agent limit reached — new agents wait (2 waiting)"
    );
    expect(holdingLine(g({ holding: false, queued: [] }))).toBe("");
    expect(holdingLine(undefined)).toBe("");
  });

  it("the rail line keys on the gate's why, and says memory only for memory", () => {
    const g = (why: string, over: Partial<LaunchGateStatus> = {}): LaunchGateStatus => ({
      holding: true,
      queued: [wait({ why, reason: "" })],
      reason: "",
      live: 3,
      cap: 16,
      warming: 0,
      projected_available: null,
      projected_level: "ok",
      why,
      ...over,
    });
    expect(holdingLine(g("memory"))).toBe("Memory low — new agents wait (1 waiting)");
    expect(holdingLine(g("warming"))).toBe("Agents still starting — new agents wait (1 waiting)");
    expect(holdingLine(g("cap"))).toBe("Agent limit reached — new agents wait (1 waiting)");
    expect(holdingLine(g("spacing"))).toBe("Starting agents one at a time (1 waiting)");
    expect(holdingLine(g("queue"))).toBe("New agents queued (1 waiting)");
    // The gate's own why wins over a stale reason text.
    expect(holdingLine(g("spacing", { reason: "memory is low: 2 GB free" }))).toBe("Starting agents one at a time (1 waiting)");
    // No gate why: the head of the queue's.
    expect(holdingLine(g("cap", { why: undefined }))).toMatch(/^Agent limit reached/);
  });

  it("a waiting row's chip, line and tooltip key on its why", () => {
    const now = 1_000_000 * 1000;
    const row = (over: Partial<LaunchWait>) =>
      deriveSessionState({ title: "w", status: "running", waiting_for_memory: wait({ since: 1_000_000 - 180, ...over }) } as Partial<Instance>, { now });
    expect(row({ why: "memory" }).label).toBe("Waiting for memory");
    expect(row({ why: "warming" }).label).toBe("Waiting — other agents are still starting");
    expect(row({ why: "spacing" }).label).toBe("Starting shortly");
    expect(row({ why: "cap" }).label).toBe("Waiting for a free agent slot");
    expect(row({ why: "queue", position: 3 }).label).toBe("Queued — 2 ahead");
    expect(row({ why: "restart" }).label).toBe("Queued");
    // An older server: no why, read from the reason.
    expect(row({ reason: "16 agents are running; automatic starts stop at 16 on this machine" }).label).toBe(
      "Waiting for a free agent slot"
    );
    expect(row({ reason: "the last agent started 4s ago; automatic starts are 20s apart" }).label).toBe("Starting shortly");
    const hint = row({ why: "cap", position: 2, reason: "" }).hint || "";
    expect(hint).toContain("New worker · waiting 3m · 2nd in line");
    expect(hint).not.toMatch(/memory/i);
  });
});

describe("Start anyway and Stop waiting", () => {
  function stub() {
    const said: string[] = [];
    const posts: { path: string; init: unknown }[] = [];
    const say = vi.spyOn(launchGateDeps, "say").mockImplementation((l: string) => void said.push(l));
    const refresh = vi.spyOn(launchGateDeps, "refresh").mockImplementation(() => {});
    return { said, posts, restore: () => (say.mockRestore(), refresh.mockRestore()) };
  }

  it("Start anyway goes through the memory confirm on a 409 memory_confirm", async () => {
    const s = stub();
    const ask = vi.spyOn(mc.memoryConfirmDeps, "ask").mockResolvedValue("start");
    const post = vi.spyOn(launchGateDeps, "post").mockImplementation(async (path: string, init: unknown) => {
      s.posts.push({ path, init });
      if (s.posts.length === 1) throw refusal({ ...LOW, level: "critical" });
      return { started: true } as never;
    });
    await expect(startWaiting("w1")).resolves.toBe(true);
    expect(s.posts.map((p) => p.path)).toEqual(["/api/launch-gate/w1/start", "/api/launch-gate/w1/start"]);
    expect(s.posts[1].init).toEqual({ json: { start_anyway: true } });
    expect(ask).toHaveBeenCalledWith(expect.objectContaining({ level: "critical" }), "w1", true);
    expect(s.said).toEqual(["Starting w1 now…"]);
    ask.mockRestore();
    post.mockRestore();
    s.restore();
  });

  it("Cancel in that dialog keeps it waiting, and says so", async () => {
    const s = stub();
    const ask = vi.spyOn(mc.memoryConfirmDeps, "ask").mockResolvedValue("cancel");
    const post = vi.spyOn(launchGateDeps, "post").mockImplementation(async () => {
      throw refusal();
    });
    await expect(startWaiting("w1")).resolves.toBe(false);
    expect(s.said).toEqual(["w1 keeps waiting"]);
    ask.mockRestore();
    post.mockRestore();
    s.restore();
  });

  it("Stop waiting cancels it and toasts what that means", async () => {
    const s = stub();
    const post = vi.spyOn(launchGateDeps, "post").mockImplementation(async (path: string, init: unknown) => {
      s.posts.push({ path, init });
      return {} as never;
    });
    await expect(cancelWaiting("w1", true)).resolves.toBe(true);
    await expect(cancelWaiting("w2")).resolves.toBe(true);
    expect(s.posts[0]).toEqual({ path: "/api/launch-gate/w1/cancel", init: { method: "POST" } });
    expect(s.said).toEqual([
      "Cancelled w1 — it won't be started",
      "w2 stopped waiting — start it yourself when you need it",
    ]);
    post.mockRestore();
    s.restore();
  });
});

describe("Put idle agents to sleep first always says what it did", () => {
  it("toasts the slept line when the caller passes no onSlept", async () => {
    const ask = vi.spyOn(mc.memoryConfirmDeps, "ask").mockResolvedValue("sleep");
    const sleepIdle = vi.spyOn(mc.memoryConfirmDeps, "sleepIdle").mockResolvedValue({ slept: ["a"], freed_mb: 2048 });
    const said: string[] = [];
    const say = vi.spyOn(mc.memoryConfirmDeps, "say").mockImplementation((l: string) => void said.push(l));
    let n = 0;
    const send = async () => {
      n += 1;
      if (n === 1) throw refusal();
      return "started";
    };
    await expect(mc.withMemoryConfirm("a new agent", send)).resolves.toBe("started");
    expect(said).toEqual(["Put 1 idle agent to sleep (freed ~2.0 GB)."]);
    ask.mockRestore();
    sleepIdle.mockRestore();
    say.mockRestore();
  });

  it("Start all says which wait for memory instead of failing them", () => {
    const line = sweepToast(
      { revived: ["a"], skipped: { b: "low memory", c: "low memory" }, waiting: ["b"] },
      ["a", "b", "c"]
    );
    expect(line).toBe(
      "Started 1 agent; 1 waiting for memory — it starts by itself once there is room; 1 not started: memory is low"
    );
  });
});
