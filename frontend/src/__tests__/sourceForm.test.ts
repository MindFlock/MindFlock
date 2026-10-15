import { describe, it, expect } from "vitest";
import {
  TEST_FRESH_MS,
  TEST_STATE_LABELS,
  advancedFields,
  advancedHasValues,
  basicFields,
  clearSecretPatch,
  fieldErrorsFrom,
  hasSecret,
  isTestSensitive,
  lastTestOf,
  missingAutoFields,
  missingRequiredFields,
  putBody,
  requiredMarker,
  savedStates,
  shouldAutoTest,
  sourceTestState,
  splitForSave,
  withLastTest,
  workspaceChoices,
} from "../components/intake/sourceForm";
import type { TicketingCatalogEntry, TicketingSource } from "../api/types";
import TICKETS_TAB from "../components/intake/TicketsTab.tsx?raw";
import INTAKE_CSS from "../components/intake/IntakeDialog.css?raw";

/** A Shortcut catalog entry as /api/settings/providers/ticketing serves it:
 * the token is typed and required, the member id is required but Test fills
 * it, the states are optional. */
const shortcut: TicketingCatalogEntry = {
  id: "shortcut",
  label: "Shortcut",
  fields: [
    { key: "api_token", label: "API token", secret: true, required: true },
    { key: "member_id", label: "Member ID", required: true, auto: true },
    { key: "assignee_scope", label: "Whose tickets", type: "choice", options: [] },
    { key: "workflow_state", label: "Ingest states", type: "state" },
  ],
};

const asana: TicketingCatalogEntry = {
  id: "asana",
  label: "Asana",
  fields: [
    { key: "api_token", label: "Access token", secret: true, required: true },
    { key: "project", label: "Workspace", required: true, auto: true, validate: "gid" },
  ],
};

const src = (extra: Record<string, unknown> = {}): TicketingSource =>
  ({ id: "sc", provider: "shortcut", ...extra }) as unknown as TicketingSource;

const NOW = Date.parse("2026-10-10T12:00:00Z");
const freshAt = new Date(NOW - 3600 * 1000).toISOString();
const staleAt = new Date(NOW - TEST_FRESH_MS - 1000).toISOString();

describe("requiredMarker (data-driven from the catalog)", () => {
  it("marks a required typed field, says 'filled by Test' for an auto one, nothing otherwise", () => {
    expect(requiredMarker(shortcut.fields[0])).toBe("required");
    expect(requiredMarker(shortcut.fields[1])).toBe("filled by Test");
    expect(requiredMarker(shortcut.fields[3])).toBe("");
  });

  it("splits what the user must fill from what Test fills", () => {
    const s = src();
    expect(missingRequiredFields(shortcut, s).map((f) => f.key)).toEqual(["api_token"]);
    expect(missingAutoFields(shortcut, s).map((f) => f.key)).toEqual(["member_id"]);
    expect(missingRequiredFields(shortcut, src({ api_token: "t" }))).toEqual([]);
    expect(missingAutoFields(shortcut, src({ api_token: "t", member_id: "m" }))).toEqual([]);
  });
});

describe("inline errors (the PUT's 400 body)", () => {
  it("keeps the per-source, per-field messages and drops anything else", () => {
    const body = {
      error: "Some sources are missing or have invalid fields",
      errors: {
        sc: { api_token: "Required", assignee_scope: "Anyone's needs at least one state" },
        jira: { base_url: "Must be a full URL starting with https://", junk: 7 },
        bad: "not an object",
      },
    };
    expect(fieldErrorsFrom(body)).toEqual({
      sc: { api_token: "Required", assignee_scope: "Anyone's needs at least one state" },
      jira: { base_url: "Must be a full URL starting with https://" },
    });
  });

  it("reads any other failure as no field errors (so it goes to the error card)", () => {
    expect(fieldErrorsFrom(null)).toEqual({});
    expect(fieldErrorsFrom("boom")).toEqual({});
    expect(fieldErrorsFrom({ error: "settings.json couldn't be read" })).toEqual({});
  });
});

describe("auto-test after save", () => {
  it("follows a save that touched a test-sensitive field on a complete card", () => {
    const s = src({ api_token: "tok" });
    expect(shouldAutoTest(shortcut, s, { api_token: "tok" })).toBe(true);
    expect(shouldAutoTest(shortcut, s, { workflow_state: "500" })).toBe(true);
    expect(shouldAutoTest(shortcut, s, { provider: "shortcut" })).toBe(true);
  });

  it("never for a label, repo, agent or depth change — a test would learn nothing", () => {
    const s = src({ api_token: "tok" });
    const patches: Array<Record<string, string | null>> = [{ label: "x" }, { repo_url: "git@x" }, { agent: "codex" }, { depth: "pr" }];
    for (const patch of patches) {
      expect(isTestSensitive(shortcut, patch)).toBe(false);
      expect(shouldAutoTest(shortcut, s, patch)).toBe(false);
    }
  });

  it("never on a card still missing a typed required field (no token = no test)", () => {
    expect(shouldAutoTest(shortcut, src(), { provider: "shortcut" })).toBe(false);
    // The member id is Test's to fill, so its absence does not hold the test back.
    expect(shouldAutoTest(shortcut, src({ api_token: "t" }), { api_token: "t" })).toBe(true);
  });

  it("never without a catalog entry to judge by", () => {
    expect(shouldAutoTest(null, src({ api_token: "t" }), { api_token: "t" })).toBe(false);
  });
});

describe("Clear control (explicit null clears a secret)", () => {
  it("offers Clear only while a secret is stored or typed", () => {
    expect(hasSecret(src(), shortcut.fields[0])).toBe(false);
    expect(hasSecret(src({ api_token: "•••set" }), shortcut.fields[0])).toBe(true);
    expect(hasSecret(src({ api_token: "typed" }), shortcut.fields[0])).toBe(true);
    // Never on a non-secret, whatever it holds.
    expect(hasSecret(src({ member_id: "m" }), shortcut.fields[1])).toBe(false);
  });

  it("sends null, not an empty string, which the server reads as 'keep'", () => {
    expect(clearSecretPatch("api_token")).toEqual({ api_token: null });
    const body = putBody([src({ api_token: "" }), src({ id: "jira", provider: "jira", api_token: "" })], {
      sc: ["api_token"],
    });
    expect(body.sources[0].api_token).toBeNull();
    expect(body.sources[1].api_token).toBe("");
  });
});

describe("the three-state pill (mirrors connections.py::source_test_state)", () => {
  it("is off with no credentials", () => {
    expect(sourceTestState(src(), shortcut, NOW)).toEqual({
      state: "off",
      message: "no credentials yet",
    });
    expect(TEST_STATE_LABELS.off).toBe("Off");
  });

  it("GitHub Issues borrows the GitHub login, so it is never off for want of a token", () => {
    const gh = src({ id: "gh", provider: "github_issues" });
    expect(sourceTestState(gh, null, NOW).state).toBe("needs_attention");
  });

  it("needs attention until a test has run, with the reason", () => {
    const r = sourceTestState(src({ api_token: "•••set", member_id: "m" }), shortcut, NOW);
    expect(r.state).toBe("needs_attention");
    expect(r.message).toContain("run Test");
  });

  it("needs attention when the last test failed, carrying its message", () => {
    const s = withLastTest(src({ api_token: "•••set", member_id: "m" }), {
      ok: false,
      at: freshAt,
      message: "Shortcut rejected the token (HTTP 401)",
    });
    expect(sourceTestState(s, shortcut, NOW)).toEqual({
      state: "needs_attention",
      message: "Shortcut rejected the token (HTTP 401)",
    });
  });

  it("needs attention when a passed test is older than a day", () => {
    const s = withLastTest(src({ api_token: "•••set", member_id: "m" }), {
      ok: true,
      at: staleAt,
      message: "Connected — ethan",
    });
    expect(sourceTestState(s, shortcut, NOW).state).toBe("needs_attention");
    expect(sourceTestState(s, shortcut, NOW).message).toContain("over a day ago");
  });

  it("needs attention while Test still has a required field to fill", () => {
    const s = withLastTest(src({ api_token: "•••set" }), {
      ok: true,
      at: freshAt,
      message: "Connected",
    });
    const r = sourceTestState(s, shortcut, NOW);
    expect(r.state).toBe("needs_attention");
    expect(r.message).toContain("Member ID");
  });

  it("is connected on a fresh pass with everything filled", () => {
    const s = withLastTest(src({ api_token: "•••set", member_id: "m" }), {
      ok: true,
      at: freshAt,
      message: "Connected — ethan",
    });
    expect(sourceTestState(s, shortcut, NOW)).toEqual({
      state: "connected",
      message: "Connected — ethan",
    });
    expect(TEST_STATE_LABELS.connected).toBe("Connected");
  });
});

describe("the stored verdict on the source JSON", () => {
  it("is read back with its states and workspaces, or null when absent or malformed", () => {
    const verdict = {
      ok: true,
      at: freshAt,
      message: "Connected",
      states: [{ id: "1", name: "Ready" }],
      workspaces: [{ gid: "9", name: "Acme" }],
    };
    const s = withLastTest(src({ api_token: "•••set" }), verdict);
    expect(lastTestOf(s)).toEqual({ ...verdict, member_id: undefined });
    expect(savedStates(s)).toEqual([{ id: "1", name: "Ready" }]);
    expect(lastTestOf(src())).toBeNull();
    expect(lastTestOf({ ...src(), last_test: "junk" } as unknown as TicketingSource)).toBeNull();
    expect(lastTestOf(withLastTest(s, null))).toBeNull();
  });

  it("offers the Asana workspaces Test found only while none is picked", () => {
    const a = (extra: Record<string, unknown>) =>
      withLastTest(
        { id: "as", provider: "asana", api_token: "•••set", ...extra } as unknown as TicketingSource,
        {
          ok: true,
          at: freshAt,
          message: "Connected — pick a workspace",
          workspaces: [
            { gid: "1", name: "Acme" },
            { gid: "2", name: "Side" },
          ],
        }
      );
    expect(workspaceChoices(a({})).map((w) => w.name)).toEqual(["Acme", "Side"]);
    expect(workspaceChoices(a({ project: "1" }))).toEqual([]);
    // And the pill says the workspace is what is missing.
    expect(sourceTestState(a({}), asana, NOW).message).toContain("Workspace");
    expect(sourceTestState(a({ project: "1" }), asana, NOW).state).toBe("connected");
  });
});

describe("splitForSave (one half-filled new card never blocks the others)", () => {
  const jira: TicketingCatalogEntry = {
    id: "jira",
    label: "Jira",
    fields: [
      { key: "api_token", label: "API token", secret: true, required: true },
      { key: "email", label: "Email", required: true },
    ],
  };
  const catalog = [shortcut, asana, jira];
  const stored = src({ api_token: "•••set", member_id: "m-9" });

  it("holds a never-stored card missing a typed required field, with its inline errors", () => {
    const fresh = { id: "jira", provider: "jira", api_token: "t" } as unknown as TicketingSource;
    const { send, held } = splitForSave([stored, fresh], catalog, new Set(["sc"]));
    expect(send.map((s) => s.id)).toEqual(["sc"]);
    expect(held).toEqual({ jira: { email: "Required" } });
  });

  it("sends the new card once it is complete — a Test-filled field doesn't count", () => {
    const fresh = { id: "as", provider: "asana", api_token: "t" } as unknown as TicketingSource;
    const { send, held } = splitForSave([stored, fresh], catalog, new Set(["sc"]));
    expect(send.map((s) => s.id)).toEqual(["sc", "as"]);
    expect(held).toEqual({});
  });

  it("always sends a stored card, so the server still judges edits to it", () => {
    const emptied = src({ api_token: "" });
    const { send, held } = splitForSave([emptied], catalog, new Set(["sc"]));
    expect(send).toEqual([emptied]);
    expect(held).toEqual({});
  });

  it("sends a card it has no catalog entry to judge by", () => {
    const odd = { id: "x", provider: "mystery" } as unknown as TicketingSource;
    expect(splitForSave([odd], catalog, new Set()).send).toEqual([odd]);
  });

  it("is what TicketsTab's save sends — never the whole local list", () => {
    // The add flow's unsaved draft is left out of every save but Finish's.
    expect(TICKETS_TAB).toMatch(
      /splitForSave\(\s*pending \? list\.filter\(\(s\) => s\.id !== pending\) : list,\s*catalog,\s*storedIds\.current\s*\)/
    );
    expect(TICKETS_TAB).toContain("putBody(send, opts.nulls)");
    expect(TICKETS_TAB).not.toContain("putBody(list");
  });
});

describe("Basic vs Advanced (the source form's fold)", () => {
  const jira: TicketingCatalogEntry = {
    id: "jira",
    label: "Jira",
    fields: [
      { key: "api_token", label: "API token", secret: true, required: true },
      { key: "member_id", label: "Account ID", auto: true, advanced: true },
      { key: "workflow_state", label: "Ingest statuses", type: "state" },
      { key: "start_state", label: "Move to status on start", type: "state_one", advanced: true },
      { key: "claim_label", label: "Claim label", advanced: true },
    ],
  };
  const jsrc = (extra: Record<string, unknown> = {}): TicketingSource =>
    ({ id: "jira", provider: "jira", ...extra }) as unknown as TicketingSource;

  it("splits the catalog by its `advanced` flag, keeping catalog order", () => {
    expect(basicFields(jira).map((f) => f.key)).toEqual(["api_token", "workflow_state"]);
    expect(advancedFields(jira).map((f) => f.key)).toEqual(["member_id", "start_state", "claim_label"]);
    expect(basicFields(null)).toEqual([]);
    expect(advancedFields(undefined)).toEqual([]);
  });

  it("starts closed on a fresh source and on one Test filled in", () => {
    expect(advancedHasValues(jira, jsrc())).toBe(false);
    // member_id is advanced but Test writes it on every connect: not a choice.
    expect(advancedHasValues(jira, jsrc({ member_id: "abc", api_token: "•••set" }))).toBe(false);
    // A stored "agent" depth IS Off.
    expect(advancedHasValues(jira, jsrc({ depth: "agent", effort: "" }))).toBe(false);
  });

  it("opens when anything inside already holds a value", () => {
    expect(advancedHasValues(jira, jsrc({ claim_label: "erik" }))).toBe(true);
    expect(advancedHasValues(jira, jsrc({ start_state: "3" }))).toBe(true);
    expect(advancedHasValues(jira, jsrc({ effort: "high" }))).toBe(true);
    // Fast-track is not in the fold any more (it sits with the agent), so a
    // chosen depth no longer opens it.
    expect(advancedHasValues(jira, jsrc({ depth: "pr" }))).toBe(false);
  });

  it("opens when the server refused a field inside it — an error is never hidden", () => {
    expect(advancedHasValues(jira, jsrc(), { start_state: "bad" })).toBe(true);
    expect(advancedHasValues(jira, jsrc(), { api_token: "Required" })).toBe(false);
  });

  it("renders basic fields up front and the rest in a details fold", () => {
    // Basic fields split around the Test: credentials, then Test, then the
    // filters it unlocks (whose tickets, which states).
    expect(TICKETS_TAB).toContain("const basics = basicFields(meta);");
    expect(TICKETS_TAB).toContain("credFields.map(renderField)");
    expect(TICKETS_TAB).toContain("(paired ? filterFields.slice(2) : filterFields).map(renderField)");
    const order = ["credFields.map(renderField)", 'className="set-row tk-test-row"', "renderField(filterFields[0])", "<RepoListField", 'className="tk-agent"', 'className="tk-depth"'];
    for (let i = 1; i < order.length; i++)
      expect(TICKETS_TAB.indexOf(order[i]), order[i]).toBeGreaterThan(TICKETS_TAB.indexOf(order[i - 1]));
    // The source's name is an advanced extra now.
    expect(TICKETS_TAB.indexOf('className="tk-label"')).toBeGreaterThan(
      TICKETS_TAB.search(/<summary>\s*Advanced options/)
    );
    expect(TICKETS_TAB).toContain("advancedFields(meta).map(renderField)");
    expect(TICKETS_TAB).toMatch(/<details\s+className="tk-advanced"/);
    expect(TICKETS_TAB).toMatch(/<summary>\s*Advanced options/);
    // A closed fold still says what it holds (round 5).
    expect(TICKETS_TAB).toContain('{" — " + (advancedSummary || "nothing changed")}');
    expect(TICKETS_TAB).toContain("advancedHasValues(meta, source, errs)");
    // Thinking effort lives in the fold; Fast-track sits with the agent, up
    // front — where a repository's editor puts it too.
    const fold = TICKETS_TAB.search(/<summary>\s*Advanced options/);
    expect(TICKETS_TAB.indexOf('className="tk-effort"')).toBeGreaterThan(fold);
    expect(TICKETS_TAB.indexOf('className="tk-depth"')).toBeLessThan(fold);
    expect(TICKETS_TAB.indexOf('className="tk-agent"')).toBeLessThan(fold);
  });

  it("spaces fields apart and binds each hint to its own field", () => {
    const css = INTAKE_CSS.replace(/\/\*[\s\S]*?\*\//g, "").replace(/\s+/g, " ");
    // The catalog wrapper joins the body's column (it had no gap at all — the
    // "Comment when moved" hint ran into "Claim label").
    expect(css).toContain("#intake-panel .tk-source .tk-fields { display: contents; }");
    expect(css).toMatch(/#intake-panel \.tk-source \.tk-body \{ gap: 18px;/);
    expect(css).toContain("#intake-panel .tk-source .set-row { gap: 5px; }");
    expect(css).toMatch(/#intake-panel \.tk-advanced-body \{[^}]*gap: 18px;/);
  });
});
