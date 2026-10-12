/** Bulk session actions: the sidebar's batch bar and its styles, and the
 * per-row checkbox that is gone. Moved from tests/unit/test_frontend_bulk.py,
 * which grepped the built bundle. */

/// <reference types="vite/client" />
import { describe, expect, it } from "vitest";
import { ALL_CODE } from "./sourceText";
import BULK from "../components/sidebar/BulkBar.tsx?raw";
import BULK_CSS from "../components/sidebar/BulkBar.css?raw";


describe("the bulk bar", () => {
  it("is mounted as #bulk-bar", () => {
    expect(BULK).toContain('<div id="bulk-bar"');
  });

  it("has no per-row bulk checkbox", () => {
    // The per-row bulk-select checkbox was removed from the sidebar (visual
    // clutter); sessions are acted on individually via the row ⋯ menu.
    expect(ALL_CODE).not.toContain('class="bulk-cb"');
    expect(ALL_CODE).not.toContain('"bulk-cb"');
  });

  it("is styled", () => {
    for (const sel of ["#bulk-bar", ".bulk-acts"]) expect(BULK_CSS, sel).toContain(sel);
  });
});
