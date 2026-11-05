/** The pane header's memory chip (owner, 2026-11-05: "a memory tracker for
 * each session … an optional number we can see at the pane header like diff
 * number"): the compact format, the colour rule, the tooltip, the header item
 * (on by default, hideable, a save from before it keeps it on), and the
 * pane's wiring — mirrored on the diff-stat's. */

import { describe, expect, it, vi } from "vitest";
import { code, css } from "./sourceText";
import type { SessionMemory } from "../api/types";

const storage = new Map<string, string>();
vi.stubGlobal("localStorage", {
  getItem: (k: string) => (storage.has(k) ? storage.get(k)! : null),
  setItem: (k: string, v: string) => void storage.set(k, String(v)),
  removeItem: (k: string) => void storage.delete(k),
  clear: () => storage.clear(),
});

const { fmtMb, memoryRank, memoryReading, memoryTip, memoryTone } = await import("../lib/sessionMemory");
const { headerItems, resolvePaneHeader, togglePaneItem, paneItemDef } = await import("../lib/paneHeader");

const mem = (o: Partial<SessionMemory> = {}): SessionMemory => ({
  mb: 812,
  peak_mb: 812,
  agent_mb: 0,
  mcp_mb: 0,
  other_mb: 0,
  procs: 0,
  at: 1_762_300_000,
  ...o,
});

const ctx = { active: "agent", sharedFolder: false, git: true, thread: false, calm: false, queueBusy: false, ftBusy: false };

describe("fmtMb", () => {
  it("whole MB under a GB, one decimal to 10 GB, whole GB above", () => {
    expect(fmtMb(0)).toBe("0 MB");
    expect(fmtMb(42.6)).toBe("43 MB");
    expect(fmtMb(812)).toBe("812 MB");
    expect(fmtMb(1024)).toBe("1 GB");
    expect(fmtMb(1434)).toBe("1.4 GB");
    expect(fmtMb(2150)).toBe("2.1 GB");
    expect(fmtMb(12_800)).toBe("13 GB");
    expect(fmtMb(-5)).toBe("0 MB");
    expect(fmtMb(NaN)).toBe("0 MB");
  });
});

describe("memoryReading", () => {
  it("draws nothing without a real reading", () => {
    expect(memoryReading(undefined)).toBeNull();
    expect(memoryReading(null)).toBeNull();
    expect(memoryReading(mem({ mb: 0 }))).toBeNull();
    expect(memoryReading(mem({ mb: NaN }))).toBeNull();
    expect(memoryReading(mem())?.mb).toBe(812);
  });
});

describe("memoryTone — neutral unless it matters", () => {
  const TOTAL = 24 * 1024; // the owner's WSL box

  it("neutral for an ordinary session", () => {
    expect(memoryTone(812, TOTAL, "ok", 0)).toBe("");
  });

  it("amber at ≥ 10% of RAM or ≥ 2 GB", () => {
    expect(memoryTone(0.1 * TOTAL, TOTAL)).toBe("warn");
    expect(memoryTone(2048, 0)).toBe("warn"); // machine size unknown: the floor
    expect(memoryTone(2048, 128 * 1024)).toBe("warn"); // a big box still flags 2 GB
    expect(memoryTone(2047, 0)).toBe("");
  });

  it("red at ≥ 20% of RAM or ≥ 4 GB", () => {
    expect(memoryTone(0.2 * TOTAL, TOTAL)).toBe("crit");
    expect(memoryTone(4096, 0)).toBe("crit");
    expect(memoryTone(0.2 * 8192, 8192)).toBe("crit"); // 1.6 GB of an 8 GB box
  });

  it("red under memory pressure for one of the 3 biggest (≥ 512 MB)", () => {
    for (const level of ["low", "critical"]) {
      expect(memoryTone(812, TOTAL, level, 0)).toBe("crit");
      expect(memoryTone(812, TOTAL, level, 2)).toBe("crit");
      expect(memoryTone(812, TOTAL, level, 3)).toBe(""); // 4th biggest
      expect(memoryTone(300, TOTAL, level, 0)).toBe(""); // too small to be the culprit
      expect(memoryTone(812, TOTAL, level, -1)).toBe(""); // rank unknown (a peer's)
    }
    expect(memoryTone(812, TOTAL, "ok", 0)).toBe("");
    expect(memoryTone(812, TOTAL, "unknown", 0)).toBe("");
  });
});

describe("memoryRank", () => {
  const rows = [
    { title: "a", memory: mem({ mb: 300 }) },
    { title: "b", memory: mem({ mb: 2000 }) },
    { title: "c", memory: null },
    { title: "d" },
    { title: "e", memory: mem({ mb: 900 }) },
  ];
  it("0 = biggest; -1 without a reading", () => {
    expect(memoryRank(rows, "b")).toBe(0);
    expect(memoryRank(rows, "e")).toBe(1);
    expect(memoryRank(rows, "a")).toBe(2);
    expect(memoryRank(rows, "c")).toBe(-1);
    expect(memoryRank(rows, "d")).toBe(-1);
    expect(memoryRank(undefined, "a")).toBe(-1);
  });
});

describe("memoryTip", () => {
  it("the whole breakdown", () => {
    expect(memoryTip(mem({ mb: 1434, peak_mb: 2150, agent_mb: 520, mcp_mb: 410, other_mb: 480, procs: 23 }))).toBe(
      "Memory: 1.4 GB (peak 2.1 GB) — agent 520 MB · MCP servers 410 MB · other 480 MB (tests, servers…) · 23 processes",
    );
  });
  it("only the parts that say something; the peak only above now", () => {
    expect(memoryTip(mem({ mb: 812, peak_mb: 812 }))).toBe("Memory: 812 MB");
    expect(memoryTip(mem({ mb: 812, peak_mb: 600, agent_mb: 812, procs: 1 }))).toBe("Memory: 812 MB — agent 812 MB · 1 process");
  });
});

describe("the header item", () => {
  it("is a button, on by default, beside the diff-stat", () => {
    expect(paneItemDef("memory").kind).toBe("button");
    expect(paneItemDef("memory").on).toBe(true);
    const r = resolvePaneHeader(null);
    expect(r.shown.has("memory")).toBe(true);
    expect(r.buttons.indexOf("memory")).toBe(r.buttons.indexOf("diffstat") + 1);
  });

  it("hides and comes back in the same picker, like the diff-stat", () => {
    const off = togglePaneItem(null, "memory");
    expect(off.hidden).toContain("memory");
    expect(headerItems(off, ctx).buttons).not.toContain("memory");
    expect(headerItems(togglePaneItem(off, "memory"), ctx).buttons).toContain("memory");
  });

  it("a choice saved before it existed shows it (its default), after the diff-stat", () => {
    const saved = {
      order: ["agent", "shell", "diff", "queue", "map", "thread", "fasttrack", "copy", "next", "history", "cost", "diffstat", "hide"],
      hidden: ["cost"],
    };
    const r = resolvePaneHeader(saved);
    expect(r.shown.has("memory")).toBe(true);
    expect(r.buttons.slice(-3)).toEqual(["diffstat", "memory", "hide"]);
  });
});

describe("the pane's wiring (source)", () => {
  const pane = code("components/grid/Pane.tsx");
  it("draws the chip only with a reading, in the diff-stat's markup, coloured by the rule", () => {
    expect(pane).toContain("const mem = memoryReading(inst.memory);");
    expect(pane).toMatch(/memory: mem \? \(\s*<span key="memory" className=\{"ctxline mem-chip"/);
    expect(pane).toContain("title={memoryTip(mem)}");
    // Never the stage chip's class: `.pane-head .stagechip` is display:none.
    const chip = pane.slice(pane.indexOf('<span key="memory"'), pane.indexOf("</span>", pane.indexOf('<span key="memory"')));
    expect(chip).not.toContain("stagechip");
  });
  it("hidden, it is still one click away at the top of ⋯", () => {
    expect(pane).toContain('!shows("memory") && mem && memoryTip(mem),');
  });
  it("amber and red stay in a narrow header", () => {
    const s = css("components/grid/contextPresets.css");
    expect(s).toContain(".ctxline.mem-chip.mem-warn");
    expect(s).toContain(".ctxline.mem-chip.mem-crit");
    expect(s).toMatch(/\.pane-head \.ctxline\.mem-chip\.mem-crit\s*\{\s*display: inline-flex !important;/);
  });
});
