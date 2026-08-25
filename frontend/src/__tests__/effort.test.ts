import { describe, it, expect } from "vitest";
import {
  EFFORTS,
  EFFORT_LABELS,
  appliedEffort,
  effortOptionLabel,
  effortTitle,
  normalizeEffort,
  supportsEffort,
} from "../lib/effort";

/** The shapes the server reports, as of the CLIs MindFlock bundles. */
const CLAUDE = {
  levels: [...EFFORTS] as string[],
  ultra_level: "ultracode",
  keyword: "",
};
const CODEX = { levels: ["low", "medium", "high", "xhigh"], keyword: "" };
const AGY = { levels: ["low", "medium", "high"], keyword: "" };
const AIDER = { levels: [] as string[], keyword: "" };
/** A CLI whose top rung exists only as a word in the prompt (the TOML shape). */
const KEYWORD_CLI = { levels: ["low", "high", "ultra"], keyword: "megathink" };

describe("normalizeEffort", () => {
  it("accepts the ladder and rejects everything else", () => {
    expect(normalizeEffort("Ultra")).toBe("ultra");
    expect(normalizeEffort(" xhigh ")).toBe("xhigh");
    expect(normalizeEffort("teleport")).toBe("");
    expect(normalizeEffort("")).toBe("");
    expect(normalizeEffort(undefined)).toBe("");
  });
  it("every rung has a label", () => {
    for (const rung of EFFORTS) expect(EFFORT_LABELS[rung]).toBeTruthy();
  });
});

describe("supportsEffort", () => {
  it("treats unknown capability as maybe, never as no", () => {
    // Caps not fetched yet, or a custom program no provider claims: disabling
    // the control here would hide a control that does work.
    expect(supportsEffort(undefined)).toBe(true);
    expect(supportsEffort(AIDER)).toBe(false);
    expect(supportsEffort(AGY)).toBe(true);
  });
});

describe("appliedEffort", () => {
  it("passes a supported rung through", () => {
    expect(appliedEffort("xhigh", CLAUDE)).toBe("xhigh");
    expect(appliedEffort("ultra", CLAUDE)).toBe("ultra");
    expect(appliedEffort("high", AGY)).toBe("high");
  });
  it("clamps a rung above the CLI's ceiling instead of forwarding it", () => {
    // The reason this matters: codex forwards an unknown level to the API,
    // which 400s, and claude warns and silently uses its default.
    expect(appliedEffort("max", CODEX)).toBe("xhigh");
    expect(appliedEffort("ultra", CODEX)).toBe("xhigh");
    expect(appliedEffort("xhigh", AGY)).toBe("high");
  });
  it("is empty for a CLI with no effort control", () => {
    expect(appliedEffort("max", AIDER)).toBe("");
    expect(appliedEffort("", CLAUDE)).toBe("");
  });
});

describe("effortOptionLabel", () => {
  it("shows the CLI's own name for the top rung", () => {
    // The bug this pins on the UI side: "Ultra" alone left the pick looking
    // like one more rung above Max, when for claude it is ultracode mode.
    expect(effortOptionLabel("ultra", CLAUDE)).toBe("Ultra (ultracode)");
    expect(effortOptionLabel("max", CLAUDE)).toBe("Max");
  });
  it("shows where a rung above the ceiling would actually run", () => {
    expect(effortOptionLabel("ultra", CODEX)).toBe("Ultra (→ Extra high)");
    expect(effortOptionLabel("xhigh", AGY)).toBe("Extra high (→ High)");
  });
  it("is the plain label before caps arrive", () => {
    expect(effortOptionLabel("ultra", undefined)).toBe("Ultra");
  });
});

describe("effortTitle", () => {
  it("says plainly when the CLI ignores the pick", () => {
    const t = effortTitle("aider", AIDER);
    expect(t).toContain("aider has no effort setting");
  });
  it("names the ceiling so the same pick isn't a mystery per queue", () => {
    expect(effortTitle("antigravity", AGY)).toContain("tops out at High");
    expect(effortTitle("codex", CODEX)).toContain("tops out at Extra high");
  });
  it("names the top rung the way the CLI names it", () => {
    const t = effortTitle("claude", CLAUDE);
    expect(t).toContain("ultracode");
    expect(t).toContain("whole session");
    expect(effortTitle("codex", CODEX)).not.toContain("ultracode");
  });
  it("mentions the prompt keyword only for a CLI that has one", () => {
    expect(effortTitle("mycli", KEYWORD_CLI)).toContain("`megathink` in the prompt");
    expect(effortTitle("claude", CLAUDE)).not.toContain("in the prompt");
  });
  it("falls back to the plain description before caps arrive", () => {
    const t = effortTitle("", undefined);
    expect(t).toContain("just this start");
    expect(t).not.toContain("tops out");
  });
});
