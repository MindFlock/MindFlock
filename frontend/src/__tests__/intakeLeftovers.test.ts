/** Polish pass 5 — Intake leftovers from pass 3 (d): where the "Not saved"
 * pill sits on an edit card, and the Tickets tab's intro once a tracker is
 * connected. (The Repo URL's red cue: repoListField.test; the auto-start
 * header link and the duplicate "Auto-start: off" line: intakeNoPause.test.) */

import { describe, expect, it } from "vitest";
import { fieldPart } from "../components/intake/TicketsTab";
import { code } from "./sourceText";

const CRED = [{ key: "api_token" }, { key: "site" }];
const FILTER = [{ key: "assignee_scope" }, { key: "states" }];

describe("the edit card's Not saved pill sits on the step at fault", () => {
  it("maps a refused field to the step it is drawn on", () => {
    expect(fieldPart("api_token", CRED, FILTER)).toBe("connect");
    // Anyone's with no state: What it takes, not Connect.
    expect(fieldPart("states", CRED, FILTER)).toBe("takes");
    expect(fieldPart("assignee_scope", CRED, FILTER)).toBe("takes");
    expect(fieldPart("repo_url", CRED, FILTER)).toBe("goes");
    expect(fieldPart(undefined, CRED, FILTER)).toBe("connect");
  });

  it("the pill follows the refused field, and the editor opens there", () => {
    const tab = code("components/intake/TicketsTab.tsx");
    expect(tab).toContain("{key === pillPart ? (");
    expect(tab).not.toContain('{key === "connect" ? (');
    expect(tab).toMatch(/useState<EditPart>\(\(\) =>\s*errPart \|\|/);
  });
});

describe("the Tickets tab's intro", () => {
  it("asks to connect a tracker only while none is connected", () => {
    const tab = code("components/intake/TicketsTab.tsx");
    expect(tab).toContain('{stored.length ? null : "Connect a tracker. "}');
  });
});
