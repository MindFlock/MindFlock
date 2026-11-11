/** Polish pass 5 (notes/polish-loop/pass5-g.md), the Sky walked as a new
 * user: a stop placed while the view hides routes with no session never
 * vanishes without a word; the empty card never covers the floating palette
 * (All stops); a Wait for me stop that has flown stops promising "waits for
 * your Done". */

import { describe, expect, it } from "vitest";
import { groundedLanding, GROUNDED_HIDDEN_LINE } from "../lib/skyLayout";
import { flightStops, youFlownSub, YOU_WAITS } from "../lib/flightDiagram";
import type { Flight } from "../lib/flights";
import { norm, src } from "./sourceText";

const SKY = norm(src("components/flights/SkyView.tsx"));

describe("a route with no session placed on a view that hides it", () => {
  it("All and Mine draw it; This session with no session moves to All; else a toast says where", () => {
    expect(groundedLanding("all", true)).toBe("shown");
    expect(groundedLanding("mine", false)).toBe("shown");
    expect(groundedLanding("one", true)).toBe("switch");
    expect(groundedLanding("one", false)).toBe("say");
    expect(groundedLanding("perched", true)).toBe("say");
  });

  it("every way one lands asks groundedLanded — the right-click and the drop too", () => {
    expect(SKY).toContain("setLayout((l) => patchSkyLine(l, groundedKey(f.id), { x: at.x, y: at.y, pinned: true })); groundedLanded(f); };");
    expect(SKY).toContain('if (how === "switch") setSkyMode("all");');
    expect(SKY).toContain('{ action: { label: "Show all", onClick: () => setSkyMode("all") } }');
    // Flow polish: a saved route's "Edit its stops" copy lands the same way (5).
    expect(SKY.match(/groundedLanded\(f\)/g)?.length).toBe(5);
    // The old per-call toast is gone (it only covered the shelf and the lane).
    expect(SKY).not.toContain('const m = useUi.getState().skyMode; if (m === "one" || m === "perched") toast(');
  });

  it("This session with no session but such a route is not 'Nothing in the Sky yet'", () => {
    expect(GROUNDED_HIDDEN_LINE).toBe("Your routes with no session yet show under All.");
    expect(SKY).toContain('const groundedHidden = mode === "one" && groundedIds.size > 0;');
    expect(SKY).toContain("groundedHidden && noSessions && !focused && !query ? GROUNDED_HIDDEN_LINE");
    expect(SKY).toMatch(/const blank = [^;]*&& !groundedHidden;/);
  });
});

describe("the empty card and the floating palette", () => {
  it("the card is not drawn while the palette (All stops) floats over a blank sky", () => {
    expect(SKY).toContain("{!(paletteOn && blank) && <SkyEmpty blank={blank}");
  });
});

describe("a Wait for me stop once it has flown", () => {
  const you = { kind: "you", config: {} };
  it("says done / skipped instead of the promise", () => {
    expect(youFlownSub(you, "done")).toBe("done");
    expect(youFlownSub(you, "skipped")).toBe("skipped");
    expect(youFlownSub(you, "perched")).toBe("");
    expect(youFlownSub({ kind: "you", config: { text: "Try the login page" } }, "done")).toBe("");
    expect(youFlownSub({ kind: "command", config: {} }, "done")).toBe("");
  });

  it("on a live route: the done one reads done, the next one still waits", () => {
    const f = {
      id: "f1",
      plan_id: "adhoc",
      plan_name: "Route",
      steps: [
        { id: "s1", kind: "you", title: "Check it", config: {}, on_fail: "halt" },
        { id: "s2", kind: "you", title: "Approve", config: {}, on_fail: "halt" },
      ],
      subject: { kind: "manual", source: "", id: "", title: "", url: "" },
      session: "fix-login",
      state: "waiting_you",
      cursor: 1,
      step_states: {
        s1: { state: "done", since: 0, note: "", detail: {}, rounds: 0 },
        s2: { state: "waiting_you", since: 0, note: "needs you", detail: {}, rounds: 0 },
      },
      reason: "",
      history: [],
      started: 0,
      updated: 0,
      finished: 0,
      by: "user",
    } as unknown as Flight;
    const [a, b] = flightStops(f);
    expect(a.sub).toBe("done");
    expect(b.sub).toBe(YOU_WAITS);
  });
});
