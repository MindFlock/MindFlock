/** Addon slots + the New dialog's agent picker (Stage E), at the source.
 * Moved from tests/unit/test_frontend_slots.py, which grepped the built
 * bundle; that the bundle's /core/slots.js is one the server serves stays a
 * Python contract test there. */

/// <reference types="vite/client" />
import { describe, expect, it } from "vitest";
import AGENT from "../components/dialogs/newSession/AgentAccount.tsx?raw";
import SIDEBAR from "../components/sidebar/Sidebar.tsx?raw";
import MAIN from "../main.tsx?raw";

describe("addon slots and the agent picker", () => {
  it("the New-session agent picker is a <select>, populated from /api/providers", () => {
    // (was a datalist-bound free-text "Program" input)
    expect(AGENT).toMatch(/<select\s+id="new-program"/);
  });

  it("the sidebar mounts #addon-bars", () => {
    expect(SIDEBAR).toContain('<div id="addon-bars" ref={addonBarsRef} />');
  });

  it("core/slots.js is loaded after app.js's first paint", () => {
    expect(MAIN).toContain('s.src = "/core/slots.js";');
  });
});
