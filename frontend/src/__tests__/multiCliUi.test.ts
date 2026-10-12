/** The UI surfaces for multi-CLI ingestion and the local-model path.
 *
 * Moved from tests/unit/test_frontend_multi_cli.py, which grepped the built
 * bundle. The endpoints these call (/api/providers,
 * /api/settings/test/local-model) are a backend contract and stay pinned in
 * Python; here: the per-source agent picker and the Local model screen. */

import { describe, expect, it } from "vitest";
import { attr, src, squash, tagsWith, tagsWithId } from "./sourceText";

const TICKETS = src("components/intake/TicketsTab.tsx");
const LOCAL = src("components/settings/screens/LocalModel.tsx");

describe("the per-source agent picker", () => {
  it("is a select writing the source's `agent` field, unset = a NAMED fallback", () => {
    const pickers = tagsWith(TICKETS, "className", "tk-agent");
    expect(pickers.map((t) => t.name)).toEqual(["select"]);
    expect(attr(pickers[0], "data-tk-field")).toBe("agent");
    expect(attr(pickers[0], "onChange")).toContain("onChange({ agent: e.target.value })");
    // The unset option names the fallback, so "App default" is never a
    // mystery value.
    expect(TICKETS).toContain('{agents.fallback ? `App default (${agents.fallback})` : "App default"}');
  });
});

describe("Settings → Local model", () => {
  it("is a registered screen", () => {
    expect(src("components/settings/SettingsDialog.tsx")).toContain('{ key: "localmodel", label: "Local model",');
  });

  it.each(["lm-enabled", "lm-runtime", "lm-base-url", "lm-model", "lm-test"])("has its %s control", (id) => {
    expect(tagsWithId(LOCAL, id).length).toBeGreaterThan(0);
  });

  it("is honest about Claude", () => {
    // Claude Code has no local route, so a session on it still uses the
    // hosted API — the one thing the privacy story cannot be quiet about.
    expect(squash(LOCAL)).toContain("Anthropic API, so a session on it keeps using that.");
  });
});
