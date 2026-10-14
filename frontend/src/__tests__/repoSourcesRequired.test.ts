/** Intake/Verify repositories (RepoSources.tsx): the add flow says what is
 * wrong with a slug only once you tried to add it — never on a box just
 * opened — and every field's help is one plain line. */

/// <reference types="vite/client" />
import { describe, expect, it } from "vitest";
import RAW from "../components/intake/RepoSources.tsx?raw";
import CSS from "../components/intake/IntakeDialog.css?raw";

const flat = RAW.replace(/\s+/g, " ");

describe("the Repository box's cue", () => {
  it("waits for an attempt to add it", () => {
    // Red only after "Add & continue" / "Finish with defaults" / Enter refused
    // the slug; the sentence sits in the flow's footer, as an alert.
    expect(flat).toContain('className={error && !REPO_RE.test(repo) ? "field-missing" : ""}');
    expect(flat).toContain('setError("Type the repository as owner/name — e.g. mindflockai/MindFlock.");');
    expect(flat).toContain('<span className="ik-add-error" role="alert">');
    expect(CSS).toContain(".ik-add input.field-missing");
  });

  it("forgets the cue once something is typed", () => {
    expect(flat).toContain("setSlug(e.target.value); setError(\"\");");
  });
});

describe("each field's help is one plain line", () => {
  it("has no paragraph-length hints left", () => {
    const spans = [...RAW.matchAll(/<span className="set-hint">([^{<]+?)<\/span>/g)].map((m) => m[1]);
    // Hints handed to BlockInput as a string prop.
    const props = [...RAW.matchAll(/hint="([^"]+)"/g)].map((m) => m[1]);
    const hints = [...spans, ...props].map((h) => h.replace(/\s+/g, " ").trim());
    expect(hints.length).toBeGreaterThan(5);
    for (const h of hints) expect(h.length, h).toBeLessThanOrEqual(90);
  });
});
