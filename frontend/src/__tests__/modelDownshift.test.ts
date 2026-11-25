import { describe, expect, it } from "vitest";
import { MODEL_DOWNSHIFT_EVENT, modelDownshiftText, resetWhen } from "../lib/modelDownshift";

describe("model downshift toast", () => {
  it("listens for the server's event", () => {
    expect(MODEL_DOWNSHIFT_EVENT).toBe("session.model_downshifted");
  });

  it("names the session, both models and when the old one is back", () => {
    const at = new Date(2026, 10, 30, 23, 0).getTime() / 1000;
    expect(modelDownshiftText({ from: "fable", to: "opus", reset_at: at }, "api", (t) => t)).toBe(
      "“api” switched from Fable to Opus: Fable is used up until Nov 30 at 11pm"
    );
  });

  it("leaves the date out when the reset is unknown", () => {
    expect(modelDownshiftText({ from: "opus", to: "sonnet" }, "w", (t) => t)).toBe(
      "“w” switched from Opus to Sonnet: Opus is used up"
    );
    expect(resetWhen(null)).toBe("");
  });

  it("writes minutes only when there are some", () => {
    expect(resetWhen(new Date(2026, 0, 5, 9, 30).getTime() / 1000)).toBe("Jan 5 at 9:30am");
    expect(resetWhen(new Date(2026, 0, 5, 0, 0).getTime() / 1000)).toBe("Jan 5 at 12am");
  });
});
