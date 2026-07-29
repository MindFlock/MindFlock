import { describe, expect, it } from "vitest";
import { sessionLabel } from "../lib/sessionLabel";

describe("sessionLabel", () => {
  it("pulls a ticket's feature name out of its branch", () => {
    const l = sessionLabel("sc-12345", "feature/sc-12345/add-dark-mode");
    expect(l.text).toBe("(tix) add-dark-mode/sc-12345");
    expect(l.kind).toBe("tix");
    expect(l.name).toBe("add-dark-mode");
    expect(l.slug).toBe("sc-12345");
  });

  it("labels a PR review session from its head ref", () => {
    expect(sessionLabel("pr-app-42", "fix/login-crash").text).toBe("(pr) login-crash/app-42");
  });

  it("labels an issue session from its feature branch", () => {
    expect(sessionLabel("issue-app-77", "feature/issue-app-77/cant-open").text).toBe(
      "(iss) cant-open/app-77"
    );
  });

  it("leaves a hand-made session title alone", () => {
    const l = sessionLabel("my-refactor", "feature/my-refactor");
    expect(l.text).toBe("my-refactor");
    expect(l.kind).toBe("");
  });

  it("keeps the tag when there is no branch to name the work", () => {
    // A pending PR row before its head ref is known.
    expect(sessionLabel("pr-app-42", "").text).toBe("(pr) app-42");
  });

  it("drops a branch that only restates the slug", () => {
    expect(sessionLabel("pr-app-42", "pr-app-42").text).toBe("(pr) app-42");
  });

  it("clips a long feature name in the row but not in the tooltip", () => {
    const long = "rework-the-entire-billing-pipeline";
    const l = sessionLabel("sc-9", `feature/sc-9/${long}`);
    expect(l.text).toBe("(tix) rework-the-entire-b…/sc-9");
    expect(l.full).toBe(`(tix) ${long}/sc-9`);
  });

  it("survives regex-special characters in a title", () => {
    // Provider ids are sanitized upstream, but a title reaching the regex path
    // must never throw.
    expect(() => sessionLabel("sc+1(a)", "feature/sc+1(a)/name")).not.toThrow();
    expect(sessionLabel("sc+1(a)", "feature/sc+1(a)/name").text).toBe("(tix) name/sc+1(a)");
  });

  it("handles an empty title", () => {
    expect(sessionLabel("", "").text).toBe("");
  });
});
