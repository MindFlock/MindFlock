/** Workflow Optimizer extension (static/extensions/workflow): the duration
 * words its dashboard uses everywhere. The renderers build DOM and are
 * checked by screenshot; the numbers come from the server's analysis
 * (tests/unit/test_workflow_addon.py). */

import { describe, expect, it } from "vitest";
// @ts-ignore -- untyped ES module outside src/ (as dbclientSql.test.ts)
import { fmtDur } from "../../../backend/web/static/extensions/workflow/view.js";

describe("fmtDur", () => {
  it("says durations the way the cards do", () => {
    expect(fmtDur(null)).toBe("—");
    expect(fmtDur(42)).toBe("42s");
    expect(fmtDur(600)).toBe("10m");
    expect(fmtDur(3600 + 25 * 60)).toBe("1h 25m");
    expect(fmtDur(2 * 86400 + 3 * 3600)).toBe("2d 3h");
    expect(fmtDur(-5)).toBe("0s");
  });
});
