/** UI sweep round 1: the connecting pieces the integrator did itself, outside
 * any worker's fence.
 *  - The outbox's Stop the route for a closed session (WaitingRow
 *    `StopRouteButton`) asks "Stop this route?" first, like the strip (#22).
 *  - The palette lists the new Diff action (Ctrl+K G, #32) next to Map.
 *  - Settings → Extensions' switch shows the inline "✓ Saved" on the
 *    extension's name (savedCue knows its row), not a toast (#29). */

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

describe("the palette lists Diff", () => {
  it("diff sits after map, and Merge stays last", () => {
    const pal = code("components/palette/CommandPalette.tsx");
    const list = pal.slice(pal.indexOf("const PALETTE_ACTIONS"), pal.indexOf("];", pal.indexOf("const PALETTE_ACTIONS")));
    const ids = [...list.matchAll(/"(\w+)"/g)].map((m) => m[1]);
    expect(ids.indexOf("diff")).toBe(ids.indexOf("map") + 1);
    expect(ids[ids.length - 1]).toBe("merge");
  });
});

describe("an extension's switch gets the inline Saved cue", () => {
  const cue = code("components/settings/savedCue.ts");
  it("its row is one savedCue marks, on the extension's name", () => {
    expect(cue).toMatch(/const ROW = "[^"]*\.ext-row-head[^"]*";/);
    expect(cue).toContain('row.querySelector(".set-label") || row.querySelector(".prov-name") || row');
  });
  it("the switch sits inside that row and hands its element to the save", () => {
    const ext = code("components/settings/screens/Extensions.tsx");
    const head = ext.slice(ext.indexOf('className="prov-conn-head ext-row-head"'));
    expect(head.slice(0, head.indexOf("</div>"))).toContain("setEnabled(ext, e.target.checked, e.target)");
  });
});

describe("the account chip is a button, like the cost chip", () => {
  it("Tab reaches it and Enter opens the account list", () => {
    const src = code("components/grid/AccountChip.tsx");
    expect(src).toMatch(/<button\s+type="button"\s+ref=\{chipRef\}\s+className="tok usage-trigger acct-chip"/);
    expect(src).toContain("aria-expanded={open}");
    expect(src).toContain("useRef<HTMLButtonElement | null>");
  });
});

describe("Intake's own tabs say when refreshing has backed off", () => {
  it.each([
    ["components/intake/PullRequestsTab.tsx", "prsQuery"],
    ["components/intake/IssuesTab.tsx", "issuesQuery"],
    ["components/intake/TicketsTab.tsx", "ticketsQuery"],
  ])("%s passes its query's stalled to the list", (file, q) => {
    expect(code(file)).toContain(`stalled={${q}.stalled}`);
  });
});

describe("the chat pill opens the chat by hand", () => {
  it("so its card takes the cursor, like the launcher", () => {
    const src = code("components/presence/GuestPill.tsx");
    expect(src).toContain("openChatByHand(title)");
    expect(src).not.toMatch(/\bopenChat\(title\)/);
  });
});

describe("Edit its stops reaches the floating palette too", () => {
  it("SkyView hands editPlan to the palette, which passes it to the catalog body", () => {
    const pal = code("components/flights/SkyPalette.tsx");
    expect(pal).toContain("onEditPlan?(plan: FlightPlan): void;");
    expect(pal).toContain("onEditPlan={onEditPlan}");
    const sky = code("components/flights/SkyView.tsx");
    const at = sky.indexOf("<SkyPalette");
    expect(sky.slice(at, sky.indexOf("/>", at))).toContain("onEditPlan={(plan) => void editPlan(plan)}");
  });
});

describe("a stops-only route's group save says what every save says", () => {
  it("savedWords, not 'on the shelf'", () => {
    const src = code("components/flights/useGroundedLine.tsx");
    expect(src).toContain("toast(savedWords(saved?.name || name, !!plan.id))");
    expect(src).not.toContain("” on the shelf\"");
    expect(src).toContain('"Saving the route failed: "');
  });
});
