/** The Prompts panel's paste-into-all target.
 *
 * Moved from tests/unit/test_frontend_addons.py (which grepped the built
 * bundle for "All running sessions"): the templates manager is the SPA's own
 * TemplatesDialog now, and the paste-into-every-running-session choice lives
 * in the Prompts panel, built by lib/promptTargets. The addon side (no vanilla
 * templates module, builtin_ui) stays pinned in Python. */

import { describe, expect, it } from "vitest";
import { ALL_RUNNING, promptTargets } from "../lib/promptTargets";
import { src } from "./sourceText";

describe("the Prompts panel offers every running session at once", () => {
  it("labels the all-target 'All running sessions (n)'", () => {
    const all = promptTargets(null, ["a", "b"], (t) => t).find((o) => o.value === ALL_RUNNING);
    expect(all?.label).toBe("All running sessions (2)");
  });

  it("is the panel's own picker", () => {
    expect(src("components/dialogs/PromptsDialog.tsx")).toContain("promptTargets(focused, running,");
  });
});
