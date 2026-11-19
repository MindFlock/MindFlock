/** UI sweep round 1: the connecting pieces the integrator did itself, outside
 * any worker's fence.
 *  - The outbox's Stop the route for a closed session (WaitingRow
 *    `StopRouteButton`) asks "Stop this route?" first, like the strip (#22). */

import { describe, expect, it } from "vitest";
import { code } from "./sourceText";

describe("the outbox's Stop the route asks first", () => {
  const src = code("components/outbox/WaitingRow.tsx");
  const btn = src.slice(src.indexOf("function StopRouteButton"), src.indexOf("function RouteMain"));

  it("the button only opens the question", () => {
    expect(btn).toMatch(/onClick=\{\(\) => setAsking\(true\)\}/);
  });

  it("the question carries the strip's words, and only its confirm cancels", () => {
    expect(btn).toContain('title="Stop this route?"');
    expect(btn).toContain('cancelLabel="Keep it running"');
    expect(btn).toContain("<InlineConfirm");
    const confirm = btn.slice(btn.indexOf("onConfirm"));
    expect(confirm).toContain('flightPath(id, "/cancel")');
    expect(btn.slice(0, btn.indexOf("<InlineConfirm"))).not.toContain('"/cancel"');
  });
});
