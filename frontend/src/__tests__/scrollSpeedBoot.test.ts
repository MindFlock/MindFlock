import { describe, expect, it } from "vitest";
import APP_SRC from "../App.tsx?raw";
import GENERAL_SRC from "../components/settings/screens/General.tsx?raw";

describe("terminal scroll speed survives a reload and a restart", () => {
  it("the app applies the stored wheel speed at boot, not only when General mounts", () => {
    expect(APP_SRC).toContain('api<{ speed?: number }>("/api/scroll-speed")');
    expect(APP_SRC).toContain("setWheelDamping(s.speed)");
    expect(APP_SRC).toContain('import { setWheelDamping } from "./lib/terminals"');
  });

  it("General leads with the scroll speed; budgets wait in a named, closed fold", () => {
    const row = GENERAL_SRC.indexOf("<ScrollSpeedRow />");
    const fold = GENERAL_SRC.indexOf('<AdvancedFold id="general-advanced"');
    expect(row).toBeGreaterThan(-1);
    expect(row).toBeLessThan(fold);
    expect(GENERAL_SRC).toContain('summary="Budgets, resume and sleep" defaultOpen={false}');
    expect(GENERAL_SRC.indexOf("<Tours />")).toBeGreaterThan(fold);
  });
});
