/** Settings → Agent CLI → the window-refresh block.
 *
 * Moved from tests/unit/test_window_refresh.py, which grepped the built
 * bundle. The /api/window-refresh route the block reads and writes is a
 * backend contract and stays pinned in Python; here: the block's controls and
 * that it edits `anchor_time`. */

import { describe, expect, it } from "vitest";
import { src, tagsWithId } from "./sourceText";

const CODING = src("components/settings/screens/CodingCli.tsx");

describe("the window-refresh settings block", () => {
  it("is its own WindowRefresh component, mounted on the Agent CLI screen", () => {
    expect(CODING).toContain("function WindowRefresh()");
    expect(CODING).toContain("<WindowRefresh />");
  });

  it.each(["wr-enabled", "wr-interval", "wr-anchor", "wr-providers", "wr-status"])("has its %s control", (id) => {
    expect(tagsWithId(CODING, id)).toHaveLength(1);
  });

  it("saves the anchor time as anchor_time", () => {
    const anchor = tagsWithId(CODING, "wr-anchor")[0];
    expect(anchor.text).toContain('save({ anchor_time: e.target.value || "" })');
  });
});
