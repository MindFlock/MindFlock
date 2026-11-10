import { describe, expect, it } from "vitest";
import { windowsDetail, windowsIsShort } from "../components/MemoryNotice";

const GB = 1024 ** 3;

describe("the memory notice names Windows when Windows is the one short", () => {
  const live = {
    level: "critical" as const,
    guest_level: "low" as const,
    available: 5.1 * GB,
    total: 19.5 * GB,
    available_pct: 24,
    host: { available: 0.5 * GB, total: 31.4 * GB, available_pct: 1.7, level: "critical" },
  };
  it("is Windows when the host is at least as bad as WSL", () => {
    expect(windowsIsShort(live)).toBe(true);
    expect(windowsIsShort({ ...live, host: { ...live.host, level: "ok" } })).toBe(false);
    expect(windowsIsShort({ ...live, guest_level: "critical", host: { ...live.host, level: "low" } })).toBe(false);
    expect(windowsIsShort({ level: "low", available: 1, total: 2 })).toBe(false);
  });
  it("shows Windows' numbers and what WSL is holding", () => {
    expect(windowsDetail(live)).toBe(
      "Windows: 0.5 GB free of 31.4 GB (2%) · 5.1 GB of WSL's 19.5 GB is unused inside WSL, but Windows can't take it back while WSL holds it",
    );
  });
  it("names what WSL can reuse when the server counts it", () => {
    const m = { ...live, host: { ...live.host, guest_reusable: 0.4 * GB } };
    expect(windowsDetail(m)).toContain("Windows: 0.5 GB free of 31.4 GB (2%) · WSL can reuse 0.4 GB it already holds · ");
  });
});
