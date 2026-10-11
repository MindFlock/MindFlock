/** Recently closed: the reason line a row carries when its session left
 * without the user closing it — today, the engine dropping it at startup
 * because its folder and tmux were gone (tests/unit/test_revive.py pins the
 * server side). The dialog itself is never rendered here (no DOM). */

import { describe, it, expect } from "vitest";
import { reasonText } from "../components/dialogs/RecentDialog";

describe("reasonText", () => {
  it("is empty for an ordinary close", () => {
    expect(reasonText({})).toBe("");
    expect(reasonText({ reason: "", branch: "feature/x" })).toBe("");
  });

  it("says the workspace went missing and names a surviving branch", () => {
    const t = reasonText({
      reason: "workspace missing",
      branch: "feature/x",
      branch_exists: true,
      wiped: true,
    });
    expect(t).toMatch(/^Workspace missing — removed at startup\./);
    expect(t).toContain("Branch feature/x is still in the repo");
  });

  it("says the branch is gone when it is", () => {
    expect(
      reasonText({ reason: "workspace missing", branch: "b", branch_exists: false })
    ).toBe("Workspace missing — removed at startup. Its branch is gone too.");
  });

  it("does not guess when branch_exists is unknown", () => {
    expect(reasonText({ reason: "workspace missing", branch: "b" })).toBe(
      "Workspace missing — removed at startup."
    );
  });

  it("shows any other reason as a sentence", () => {
    expect(reasonText({ reason: "removed by sync" })).toBe("Removed by sync.");
  });
});
