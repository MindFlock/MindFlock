import { describe, expect, it } from "vitest";
import { anywhereLine, anywherePollMs, anywhereWarn, type AnywhereStatus } from "../lib/devicesAnywhere";

const base: AnywhereStatus = {
  on: true,
  state: "on",
  text: "On · reachable through a private tunnel",
  retry: false,
  install: false,
  installed: true,
  tunnel: "up",
  github: { state: "ok", text: "" },
  published_at: 1,
  found: [],
  via_tunnel: [],
};

describe("Reach my devices from anywhere — the line under the switch", () => {
  it("is Off when off, and the server's words when on", () => {
    expect(anywhereLine({ ...base, on: false, state: "off", text: "Off" })).toBe("Off");
    expect(anywhereLine(base)).toBe("On · reachable through a private tunnel");
    expect(anywhereLine(null)).toBe("");
  });

  it("says how many devices it reaches through the tunnel", () => {
    expect(anywhereLine({ ...base, via_tunnel: ["rig"] })).toBe(
      "On · reachable through a private tunnel · 1 device reached this way"
    );
    expect(anywhereLine({ ...base, via_tunnel: ["a", "b"] })).toMatch(/2 devices reached this way$/);
  });

  it("warns on a failed tunnel or a GitHub permission problem, not while starting", () => {
    expect(anywhereWarn({ ...base, state: "failed", text: "Couldn't start the tunnel — x", retry: true })).toBe(true);
    expect(anywhereWarn({ ...base, state: "needs_github", text: "GitHub sign-in is missing the repo permission — …" })).toBe(
      true
    );
    expect(anywhereWarn({ ...base, state: "starting" })).toBe(false);
    expect(anywhereWarn({ ...base, on: false, state: "failed" })).toBe(false);
  });

  it("polls fast only while something is changing", () => {
    expect(anywherePollMs({ ...base, state: "starting" })).toBe(2000);
    expect(anywherePollMs(base)).toBe(15000);
    expect(anywherePollMs(null)).toBe(15000);
  });
});
