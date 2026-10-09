import { afterEach, describe, expect, it } from "vitest";
import { agentCopyText, runsClaudeCode } from "../lib/agentCopy";
import { queryClient } from "../state/queries";

const set = (rows: Array<{ title: string; provider: string; program: string }>) =>
  queryClient.setQueryData(["instances"], rows);

describe("agent copy gate", () => {
  afterEach(() => queryClient.removeQueries({ queryKey: ["instances"] }));

  it("cleans only a session that runs Claude Code", () => {
    set([
      { title: "a", provider: "claude", program: "claude" },
      { title: "b", provider: "codex", program: "codex" },
      // a custom provider that launches Claude Code: only its program says so
      { title: "c", provider: "generic", program: "/home/me/bin/claude --flag" },
    ]);
    expect(runsClaudeCode("a")).toBe(true);
    expect(runsClaudeCode("b")).toBe(false);
    expect(runsClaudeCode("c")).toBe(true);
    expect(runsClaudeCode("missing")).toBe(false);
    expect(runsClaudeCode(undefined)).toBe(false);
  });

  it("passes other agents' text through untouched", () => {
    set([
      { title: "a", provider: "claude", program: "claude" },
      { title: "b", provider: "codex", program: "codex" },
    ]);
    const sel = "● hello\n  there";
    expect(agentCopyText(sel, "a", 60)).toBe("hello\nthere");
    expect(agentCopyText(sel, "b", 60)).toBe(sel);
  });
});
