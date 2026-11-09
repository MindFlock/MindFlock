import { describe, expect, it } from "vitest";
import {
  fmtUsedOfTotal,
  levelWords,
  machineFromDetail,
  machineFromReading,
  memoryPopModel,
  memoryRowText,
  memoryRowTone,
  shortCmd,
  sparkPoints,
  type MemoryDetail,
} from "../components/usage/memoryModel";
import { calmHidesBar } from "../lib/calm";
import { DEFAULT_VISIBLE_BARS, SIDEBAR_BARS } from "../components/sidebar/barDefs";

const GiB = 1024;
const MIB = 1024 * 1024;

/** The /api/memory payload of the 2026-11-05 reboot, roughly. */
const DETAIL: MemoryDetail = {
  system: {
    total_mb: 19968, // 19.5 GB
    available_mb: 19968 - 10035, // 9.8 GB used
    swap_total_mb: 5120,
    swap_used_mb: 1300,
    psi_some_avg10: 2.1,
    psi_full_avg10: 0.4,
    level: "low",
  },
  sessions: [
    { title: "small", mb: 300, agent_mb: 200, mcp_mb: 100, other_mb: 0, procs: 4 },
    { title: "big", mb: 2400, agent_mb: 500, mcp_mb: 400, other_mb: 1500, procs: 23 },
    { title: "idle", mb: 0 },
  ],
  others: [
    { pid: 42, name: "python3", cmd: "/usr/bin/python3 /home/me/voice-lab/serve.py --port 8770", mb: 5000 },
    { pid: 7, name: "python", cmd: "python -m backend.web.run local", mb: 600, server: true },
  ],
  at: 1_700_000_000,
};

describe("the row: used / total in binary GB", () => {
  it("reads like the owner's sketch: Memory 9.8 / 19.5 GB", () => {
    const m = machineFromDetail(DETAIL.system);
    expect(m.usedMb).toBe(10035);
    expect(memoryRowText(m).head).toBe("Memory 9.8 / 19.5 GB");
    expect(memoryRowText(m).share).toBeCloseTo(10035 / 19968, 5);
  });
  it("drops a trailing .0 and keeps the decimal past 10 GB", () => {
    expect(fmtUsedOfTotal(8 * GiB, 16 * GiB)).toBe("8 / 16 GB");
    expect(fmtUsedOfTotal(12.25 * GiB, 31.9 * GiB)).toBe("12.3 / 31.9 GB");
  });
  it("falls back to fmtMb's words on a sub-GB side", () => {
    expect(fmtUsedOfTotal(812, 1946)).toBe("812 MB / 1.9 GB");
  });
  it("says — when the machine is not measured", () => {
    const m = machineFromDetail({ level: "unknown" });
    expect(fmtUsedOfTotal(m.usedMb, m.totalMb)).toBeNull();
    expect(memoryRowText(m).head).toBe("Memory —");
    expect(memoryRowText(m).share).toBeNull();
  });
  it("names the pressure in the tooltip only when it matters", () => {
    expect(memoryRowText(machineFromDetail(DETAIL.system)).title).toContain("low — new agents wait");
    expect(memoryRowText(machineFromDetail({ ...DETAIL.system, level: "ok" })).title).not.toMatch(/low|critical/);
  });
});

describe("the tone rule", () => {
  it("is neutral normally, amber at low, red at critical", () => {
    expect(memoryRowTone("ok")).toBe("");
    expect(memoryRowTone("unknown")).toBe("");
    expect(memoryRowTone(undefined)).toBe("");
    expect(memoryRowTone("low")).toBe("warn");
    expect(memoryRowTone("critical")).toBe("crit");
  });
  it("puts each level in words", () => {
    expect(levelWords("ok")).toMatch(/^OK/);
    expect(levelWords("low")).toMatch(/^Low/);
    expect(levelWords("critical")).toMatch(/^Critical/);
    expect(levelWords("bogus")).toBe("Not measured");
  });
});

describe("the popover model from /api/memory", () => {
  const model = memoryPopModel(DETAIL, machineFromReading(null), { big: "Big task" });

  it("leads with the machine line: in use, swap, pressure", () => {
    expect(model.machine).toEqual([
      ["In use", "9.8 / 19.5 GB (50%)"],
      ["Swap", "1.3 GB of 5 GB"],
      ["Pressure", "Low — new agents wait"],
    ]);
    expect(model.tone).toBe("warn");
    expect(model.machineOnly).toBe(false);
  });
  it("lists sessions biggest first, skipping empty ones, under their rail name", () => {
    expect(model.sessions.map((r) => r.title)).toEqual(["big", "small"]);
    expect(model.sessions[0].label).toBe("Big task");
    expect(model.sessions[0].value).toBe("2.3 GB");
    expect(model.sessions[0].tip).toContain("agent 500 MB · MCP servers 400 MB · other 1.5 GB");
  });
  it("scales sessions and outsiders on ONE bar, so the culprit outside stands out", () => {
    expect(model.others[0].frac).toBe(1);
    expect(model.sessions[0].frac).toBeCloseTo(2400 / 5000, 5);
  });
  it("lists the processes outside MindFlock with a short command", () => {
    expect(model.others.map((r) => r.label)).toEqual(["python3", "MindFlock server"]);
    expect(model.others[0].title).toBe(""); // not a click target
    expect(model.others[0].sub).toBe("python3 serve.py --port 8770");
    expect(model.others[0].tip).toContain("/home/me/voice-lab/serve.py");
    expect(model.others[1].value).toBe("600 MB");
  });
  it("leaves swap out when none is used", () => {
    const m = memoryPopModel({ ...DETAIL, system: { ...DETAIL.system, swap_used_mb: 0 } }, machineFromReading(null));
    expect(m.machine.map((r) => r[0])).toEqual(["In use", "Pressure"]);
  });
});

describe("Windows under WSL: the host's own memory", () => {
  const hostShort: MemoryDetail = {
    ...DETAIL,
    system: { ...DETAIL.system, swap_used_mb: 0, host_total_mb: 32 * GiB, host_available_mb: 2 * GiB, host_level: "low", guest_level: "ok" },
  };
  it("adds a Windows line and says Windows is the cause", () => {
    const m = memoryPopModel(hostShort, machineFromReading(null));
    expect(m.machine).toEqual([
      ["In use", "9.8 / 19.5 GB (50%)"],
      ["Windows", "30 / 32 GB (94%)"],
      ["Pressure", "Windows: Low — new agents wait"],
    ]);
    expect(memoryRowText(machineFromDetail(hostShort.system)).title).toContain("Windows is short of memory");
  });
  it("keeps the plain pressure words when the VM, not Windows, is the cause", () => {
    const sys = { ...hostShort.system, host_available_mb: 16 * GiB, host_level: "ok" };
    const m = memoryPopModel({ ...hostShort, system: sys }, machineFromReading(null));
    expect(m.machine.at(-1)).toEqual(["Pressure", "Low — new agents wait"]);
    expect(memoryRowText(machineFromDetail(sys)).title).not.toContain("Windows");
  });
  it("does not blame Windows when the VM is just as short", () => {
    const sys = { ...hostShort.system, guest_level: "low" };
    const m = memoryPopModel({ ...hostShort, system: sys }, machineFromReading(null));
    expect(m.machine.at(-1)).toEqual(["Pressure", "Low — new agents wait"]);
  });
  it("has no Windows line off WSL or from an older server", () => {
    expect(memoryPopModel(DETAIL, machineFromReading(null)).machine.map((r) => r[0])).not.toContain("Windows");
  });
});

describe("the fallback: an older server without /api/memory", () => {
  const reading = {
    level: "critical" as const,
    total: 16 * 1024 * MIB,
    available: 1 * 1024 * MIB,
    swap_total: 4 * 1024 * MIB,
    swap_free: 4 * 1024 * MIB,
  };
  it("reads the row from /api/system/memory's bytes", () => {
    const m = machineFromReading(reading);
    expect(m.totalMb).toBe(16384);
    expect(m.usedMb).toBe(15360);
    expect(memoryRowText(m).head).toBe("Memory 15 / 16 GB");
    expect(memoryRowTone(m.level)).toBe("crit");
  });
  it("shows only the machine line in the popover", () => {
    const model = memoryPopModel(null, machineFromReading(reading));
    expect(model.machineOnly).toBe(true);
    expect(model.sessions).toEqual([]);
    expect(model.others).toEqual([]);
    expect(model.machine).toEqual([
      ["In use", "15 / 16 GB (94%)"],
      ["Pressure", "Critical — close something"],
    ]);
  });
  it("reads nothing from nothing", () => {
    const m = machineFromReading(undefined);
    expect(m.totalMb).toBeNull();
    expect(m.level).toBe("unknown");
  });
});

describe("shortCmd", () => {
  it("drops directories and ellipsizes", () => {
    expect(shortCmd("/usr/bin/node /opt/app/server.js")).toBe("node server.js");
    expect(shortCmd("x".repeat(60), 10)).toBe("xxxxxxxxx…");
    expect(shortCmd("")).toBe("");
  });
});

describe("the last-hour sparkline", () => {
  const s = (at: number, used: number) => ({ at, system: { total_mb: 1000, available_mb: 1000 - used } });
  it("maps used memory to y against the total (top = full)", () => {
    const sp = sparkPoints([s(0, 0), s(30, 500), s(60, 1000)], 100, 10);
    expect(sp?.points).toBe("0,10 50,5 100,0");
    expect(sp?.lastUsedMb).toBe(1000);
    expect(sp?.totalMb).toBe(1000);
  });
  it("needs two usable samples", () => {
    expect(sparkPoints([s(0, 1)], 100, 10)).toBeNull();
    expect(sparkPoints([s(0, 1), { at: 5, system: { level: "unknown" } }], 100, 10)).toBeNull();
    expect(sparkPoints(undefined, 100, 10)).toBeNull();
  });
  it("thins a long hour to at most maxPoints, keeping the last", () => {
    const many = Array.from({ length: 500 }, (_, i) => s(i * 30, i));
    const sp = sparkPoints(many, 200, 28, 120)!;
    const n = sp.points.split(" ").length;
    expect(n).toBeLessThanOrEqual(121);
    expect(sp.lastUsedMb).toBe(499);
  });
});

describe("the Memory bar", () => {
  it("is a sidebar bar right under Usage, on by default", () => {
    const keys = SIDEBAR_BARS.map((b) => b.key);
    expect(keys.indexOf("memory")).toBe(keys.indexOf("usage") + 1);
    expect(DEFAULT_VISIBLE_BARS).toContain("memory");
  });
  it("waits out the calm first hour like Usage (its numbers' twin)", () => {
    expect(calmHidesBar("memory")).toBe(true);
    expect(calmHidesBar("usage")).toBe(true);
  });
});
