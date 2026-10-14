/** Intake/Verify repository cards (RepoSources.tsx): "Required" is said only
 * after the Repository box was left empty, never on a card just opened, and
 * every field's help is one plain line. */

/// <reference types="vite/client" />
import { describe, expect, it } from "vitest";
import RAW from "../components/intake/RepoSources.tsx?raw";
import CSS from "../components/intake/IntakeDialog.css?raw";

const flat = RAW.replace(/\s+/g, " ");

describe("the Repository box's Required cue", () => {
  it("waits for a blur that left it empty", () => {
    expect(flat).toContain(
      'className={!card.repo && leftEmpty.has(card.key) ? "field-missing" : ""}'
    );
    // The old cue was unconditional.
    expect(flat).not.toContain('className={card.repo ? "" : "field-missing"}');
    expect(flat).not.toContain("Required — press Enter or click away to save.");
    const cue = flat.indexOf("Required — type owner/name, then press Enter.");
    expect(cue).toBeGreaterThan(-1);
    // …inside the left-empty branch, as an alert.
    expect(flat.lastIndexOf("{!card.repo && leftEmpty.has(card.key) ? (", cue)).toBeGreaterThan(-1);
    expect(flat.slice(cue - 120, cue)).toContain('className="set-hint ik-field-error" role="alert"');
    expect(CSS).toContain(".set-row .set-hint.ik-field-error");
  });

  it("forgets the cue once something is typed", () => {
    expect(flat).toContain("const empty = !e.target.value.trim();");
    expect(flat).toContain("if (empty) next.add(card.key); else next.delete(card.key);");
  });
});

describe("each field's help is one plain line", () => {
  it("has no paragraph-length hints left", () => {
    const hints = [...RAW.matchAll(/<span className="set-hint">([\s\S]*?)<\/span>/g)].map((m) =>
      m[1].replace(/\{[^}]*\}/g, "X").replace(/<[^>]+>/g, "").replace(/\s+/g, " ").trim()
    );
    expect(hints.length).toBeGreaterThan(5);
    for (const h of hints) expect(h.length, h).toBeLessThanOrEqual(90);
  });
});
