/** Shared server queries (state/queries.ts). GET /api/doctor and the
 * ["mindflock-status"] poll used to be fetched by hand in six and four places,
 * the status one with four different response types — a shared query key
 * with two different intervals lets whichever component mounted first decide
 * the poll rate. One definition each; these checks keep it that way. */

import { describe, expect, it } from "vitest";

const SOURCES = import.meta.glob(["../**/*.ts", "../**/*.tsx", "!../__tests__/**"], {
  query: "?raw",
  import: "default",
  eager: true,
}) as Record<string, string>;

function filesMatching(re: RegExp): string[] {
  return Object.entries(SOURCES)
    .filter(([, src]) => re.test(src))
    .map(([path]) => path)
    .sort();
}

describe("one definition per shared query", () => {
  it('GET /api/doctor is fetched only by state/queries (useDoctor / reprobeDoctor)', () => {
    // Sub-routes (/api/doctor/install-state, /ack-state-notice, …) are other endpoints.
    expect(filesMatching(/["']\/api\/doctor(\?[^"']*)?["']|["']\/api\/doctor["']\s*\+/)).toEqual([
      "../state/queries.ts",
    ]);
  });

  it('the ["mindflock-status"] query is defined once', () => {
    expect(filesMatching(/queryKey:\s*\["mindflock-status"\]/)).toEqual(["../state/queries.ts"]);
    expect(filesMatching(/["']\/api\/mindflock\/status["']/)).toEqual(["../state/queries.ts"]);
  });

  it("the status payload has one type, in api/types.ts", () => {
    expect(SOURCES["../api/types.ts"]).toMatch(/export interface MindflockStatus \{/);
    expect(filesMatching(/interface (MfStatus|IngestionStatus)\b/)).toEqual([]);
  });
});
