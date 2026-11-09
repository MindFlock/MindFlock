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

  it("saves the anchor time as anchor_time, once typing pauses", () => {
    const anchor = tagsWithId(CODING, "wr-anchor")[0];
    expect(anchor.text).toContain('anchorSave.schedule(e.target.value || "")');
    expect(anchor.text).toContain("anchorSave.flush()");
    expect(CODING).toContain("save({ anchor_time: v }, anchorEl.current)");
  });

  it("debounces the interval too, and marks the control saved", () => {
    expect(tagsWithId(CODING, "wr-interval")[0].text).toContain("intervalSave.schedule(v)");
    expect(CODING).toContain('if (!flashSaved(el)) toast("Saved")');
  });
});

describe("Keep usage windows warm names agents by product (polish pass 3, area c)", () => {
  it("labels each checkbox and the next-refresh line with agentName, not the CLI id", () => {
    const cli = src("components/settings/screens/CodingCli.tsx");
    expect(cli).toContain('agentName(o.name) + ": next ~"');
    expect(cli).toContain("{agentName(o.name) +");
    expect(cli).not.toContain("{o.name +");
  });
});
