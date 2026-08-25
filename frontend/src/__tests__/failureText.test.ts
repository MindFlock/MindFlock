import { describe, it, expect } from "vitest";
import { shortReason } from "../lib/failureText";

/** The reason that forced this: a `failed` ledger entry carries the whole git
 * error, and it turned a one-line row into a paragraph. */
const REAL = `failed earlier: failed to create provisioned worktree: branch
'feature/shortcut-21255/classifier-collage-needs-to-stop-alertin' is already
checked out at /home/emandel2630/.mindflock/worktrees/feature/shortcut-21255/classifier-collage-needs-to-stop-alertin_18cc9f57ec67d4e3.
Kill that session first, or use a different story id / title.`;

describe("shortReason", () => {
  it("leaves an ordinary reason exactly as it is", () => {
    for (const short of [
      "queued for ingestion (pending)",
      "a feature branch for it already exists on the remote",
      "not assigned to the configured member id",
    ]) {
      expect(shortReason(short)).toEqual({ short, clipped: false });
    }
  });

  it("clips a git error to a chip-sized head", () => {
    const r = shortReason(REAL);
    expect(r.clipped).toBe(true);
    expect(r.short.length).toBeLessThanOrEqual(59);
    expect(r.short.endsWith("…")).toBe(true);
    // The head still says what happened, not just "failed".
    expect(r.short).toContain("failed earlier");
  });

  it("collapses the newlines the server's message carries", () => {
    expect(shortReason(REAL).short).not.toMatch(/\s\s|\n/);
  });

  it("never ends mid-word when there is a space to cut on", () => {
    const r = shortReason("alpha beta gamma delta epsilon zeta eta theta", 20);
    expect(r.short).toBe("alpha beta gamma…");
  });

  it("hard-cuts a single unbroken token", () => {
    // A bare branch name or absolute path has no space to break on; returning
    // the whole thing would defeat the point of clipping.
    const r = shortReason("a".repeat(200), 20);
    expect(r.short).toBe("a".repeat(20) + "…");
  });

  it("drops trailing punctuation before the ellipsis", () => {
    expect(shortReason("something went wrong: because", 21).short).toBe(
      "something went wrong…"
    );
  });

  it("survives an empty or missing reason", () => {
    expect(shortReason("")).toEqual({ short: "", clipped: false });
    expect(shortReason(undefined as unknown as string).clipped).toBe(false);
  });
});
