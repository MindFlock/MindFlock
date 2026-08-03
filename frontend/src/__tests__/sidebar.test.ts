/** Sidebar width clamping (the draggable right edge) and the alias reset that
 * keeps a newly created session from wearing a closed one's rename. */

import { describe, it, expect, beforeEach } from "vitest";
import {
  SIDEBAR_DEFAULT_W,
  SIDEBAR_MAX_W,
  SIDEBAR_MIN_W,
  clampSidebarWidth,
  useUi,
} from "../state/store";
import { addPendingSession } from "../lib/sessionActions";
import { queryClient } from "../state/queries";
import type { Instance } from "../api/types";

describe("clampSidebarWidth", () => {
  it("keeps a width inside the bounds untouched (rounded to whole px)", () => {
    expect(clampSidebarWidth(320)).toBe(320);
    expect(clampSidebarWidth(320.4)).toBe(320);
  });

  it("clamps a drag past either bound", () => {
    expect(clampSidebarWidth(10)).toBe(SIDEBAR_MIN_W);
    expect(clampSidebarWidth(4000)).toBe(SIDEBAR_MAX_W);
  });

  it("floors at the default, so dragging can only widen the sidebar", () => {
    // Narrower than 260 spills the stage chip and ✕ past the sidebar's edge and
    // crushes the name to a couple of characters.
    expect(SIDEBAR_MIN_W).toBe(SIDEBAR_DEFAULT_W);
    expect(clampSidebarWidth(200)).toBe(SIDEBAR_DEFAULT_W);
    // A width persisted under an older, lower floor is raised on load.
    expect(clampSidebarWidth(259)).toBe(SIDEBAR_DEFAULT_W);
  });

  it("falls back to the default for a non-finite width (corrupt storage)", () => {
    // No drag can produce these — only a hand-edited/corrupt mf_sidebar_w — so
    // they reset rather than pinning the column to a bound.
    expect(clampSidebarWidth(NaN)).toBe(SIDEBAR_DEFAULT_W);
    expect(clampSidebarWidth(Infinity)).toBe(SIDEBAR_DEFAULT_W);
    expect(clampSidebarWidth(-Infinity)).toBe(SIDEBAR_DEFAULT_W);
  });
});

describe("setSidebarWidth", () => {
  it("stores the clamped value", () => {
    useUi.getState().setSidebarWidth(9999);
    expect(useUi.getState().sidebarWidth).toBe(SIDEBAR_MAX_W);
    useUi.getState().setSidebarWidth(300);
    expect(useUi.getState().sidebarWidth).toBe(300);
  });
});

describe("addPendingSession", () => {
  const rows = (titles: string[]) =>
    queryClient.setQueryData<Instance[]>(
      ["instances"],
      titles.map((t) => ({ title: t }) as unknown as Instance)
    );

  beforeEach(() => {
    rows([]);
    for (const t of Object.keys(useUi.getState().aliases))
      useUi.getState().setAlias(t, "");
  });

  it("numbers around titles already taken (mirrors the server)", () => {
    rows(["foo-copy", "foo-copy-2"]);
    expect(addPendingSession("foo-copy")).toBe("foo-copy-3");
  });

  it("drops a stale alias so a reused title is not named after a dead session", () => {
    // The user duplicated `foo` before, renamed the copy, then closed it. The
    // alias survives (a reopened session keeps its name) — but the NEXT
    // `foo-copy` is a different session and must read as itself.
    useUi.getState().setAlias("foo-copy", "Belisa's old scratch window");
    expect(addPendingSession("foo-copy")).toBe("foo-copy");
    expect(useUi.getState().aliases["foo-copy"]).toBeUndefined();
  });

  it("leaves aliases for other titles alone", () => {
    useUi.getState().setAlias("bar", "keep me");
    addPendingSession("foo-copy");
    expect(useUi.getState().aliases["bar"]).toBe("keep me");
  });
});
