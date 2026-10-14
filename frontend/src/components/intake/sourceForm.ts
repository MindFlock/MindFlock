/** Intake → Tickets source cards: the form logic that is not React.
 *
 * Everything the card decides from data lives here, so it can be tested
 * without a DOM and so the server and the card agree: which fields carry a
 * required marker (the catalog's `required`), what a save rejection means per
 * field (the PUT's `{errors: {<id>: {<field>: msg}}}`), when a save deserves an
 * automatic Test (a test-sensitive field changed on a source that is complete
 * enough to try), how a secret is cleared (`{api_token: null}` — absent and `""`
 * both mean "keep", for older clients), and what the three-state pill says.
 *
 * The pill rule mirrors `backend/web/addons/connections.py::source_test_state`
 * exactly: connected = the stored `last_test` passed within a day AND every
 * field Test fills in is filled; off = no credential; needs attention =
 * everything else, with the reason.
 */

import type {
  TicketingCatalogEntry,
  TicketingCatalogField,
  TicketingLastTest,
  TicketingSource,
  TicketingWorkflowState,
} from "../../api/types";

export type SourceTestState = "connected" | "needs_attention" | "off";

/** How long a passed test vouches for a source (the server's TEST_FRESH_SECONDS). */
export const TEST_FRESH_MS = 24 * 3600 * 1000;

export const SECRET_MASK = "•••set";

/** Per-field messages for one source, as the PUT returns them. */
export type FieldErrors = Record<string, string>;

/** The stored test verdict, which rides on the source JSON as an object even
 * though every other source field is a string. Read it through this so the
 * shape is checked once. */
export function lastTestOf(source: TicketingSource | null | undefined): TicketingLastTest | null {
  const raw = (source as unknown as { last_test?: unknown } | null | undefined)?.last_test;
  if (!raw || typeof raw !== "object") return null;
  const t = raw as Record<string, unknown>;
  if (typeof t.ok !== "boolean") return null;
  return {
    ok: t.ok,
    at: typeof t.at === "string" ? t.at : "",
    message: typeof t.message === "string" ? t.message : "",
    member_id: typeof t.member_id === "string" ? t.member_id : undefined,
    states: Array.isArray(t.states) ? (t.states as TicketingWorkflowState[]) : undefined,
    workspaces: Array.isArray(t.workspaces)
      ? (t.workspaces as { gid: string; name: string }[])
      : undefined,
  };
}

/** A copy of `source` carrying `verdict` as its stored test (null = none). */
export function withLastTest(
  source: TicketingSource,
  verdict: TicketingLastTest | null | undefined
): TicketingSource {
  const out = { ...source } as Record<string, unknown>;
  if (verdict) out.last_test = verdict;
  else delete out.last_test;
  return out as unknown as TicketingSource;
}

/** The workflow states the card's pickers open with: what the last Test saw. */
export function savedStates(source: TicketingSource): TicketingWorkflowState[] {
  return lastTestOf(source)?.states || [];
}

/** Asana: the workspaces Test found when the token sees several and none is
 * picked yet — the form offers them instead of asking for a gid. Empty once a
 * workspace is set (or for any other provider). */
export function workspaceChoices(source: TicketingSource): { gid: string; name: string }[] {
  if ((source.project || "").trim()) return [];
  return (lastTestOf(source)?.workspaces || []).filter((w) => w && w.gid);
}

/** The marker next to a field's label: "" (optional), "required", or
 * "filled by Test" for a required field the user never types. */
export function requiredMarker(field: TicketingCatalogField): "" | "required" | "filled by Test" {
  if (!field.required) return "";
  return field.auto ? "filled by Test" : "required";
}

/** Whether a secret field currently holds a value worth a Clear control —
 * either the mask the server sent back or something typed this session. */
export function hasSecret(source: TicketingSource, field: TicketingCatalogField): boolean {
  return !!field.secret && !!(source[field.key] || "").trim();
}

/** The patch that clears a secret: an explicit null. `""` would mean "keep". */
export function clearSecretPatch(key: string): Record<string, string | null> {
  return { [key]: null };
}

/** The required fields Test fills in that are still blank on `source`. */
export function missingAutoFields(
  meta: TicketingCatalogEntry | null | undefined,
  source: TicketingSource
): TicketingCatalogField[] {
  return (meta?.fields || []).filter(
    (f) => f.required && f.auto && !(source[f.key] || "").trim()
  );
}

/** The required fields the USER must fill that are still blank — what the
 * server will refuse the save over. */
export function missingRequiredFields(
  meta: TicketingCatalogEntry | null | undefined,
  source: TicketingSource
): TicketingCatalogField[] {
  return (meta?.fields || []).filter(
    (f) => f.required && !f.auto && !(source[f.key] || "").trim()
  );
}

/** Whether `patch` touches something a connection test looks at: the provider
 * or any catalog field. Label, repo, agent, effort and depth do not. */
export function isTestSensitive(
  meta: TicketingCatalogEntry | null | undefined,
  patch: Record<string, string | null>
): boolean {
  const keys = new Set(["provider", ...(meta?.fields || []).map((f) => f.key)]);
  return Object.keys(patch).some((k) => keys.has(k));
}

/** Whether a save of `source` should be followed by an automatic Test: a
 * test-sensitive change landed and every user-typed required field is in —
 * testing a card with no token yet would only record a failure nobody needs
 * telling about. */
export function shouldAutoTest(
  meta: TicketingCatalogEntry | null | undefined,
  source: TicketingSource,
  patch: Record<string, string | null>
): boolean {
  if (!meta) return false;
  if (!isTestSensitive(meta, patch)) return false;
  return missingRequiredFields(meta, source).length === 0;
}

/** The PUT body: the sources as held locally, with the fields cleared this
 * round sent as explicit nulls (the local copy holds "" for them, which the
 * server would read as "keep the stored secret"). */
export function putBody(
  sources: TicketingSource[],
  nulls: Record<string, string[]> = {}
): { sources: Record<string, unknown>[] } {
  return {
    sources: sources.map((s) => {
      const out: Record<string, unknown> = { ...s };
      for (const key of nulls[s.id] || []) out[key] = null;
      return out;
    }),
  };
}

/** Which cards a save sends, and which it holds back with their inline
 * errors. The PUT replaces the whole list and the server refuses all of it when
 * any new or changed source fails validation, so one half-filled "Add source"
 * card used to block every other card's save. A card that was never stored
 * (its id is not in `storedIds`) and still misses a field the USER must type
 * (`missingRequiredFields` — Test-filled ones don't count) stays local until
 * it is complete; a stored card is always sent, so the server keeps judging
 * edits to it (and a held card can never delete one). */
export function splitForSave(
  sources: TicketingSource[],
  catalog: TicketingCatalogEntry[],
  storedIds: ReadonlySet<string>
): { send: TicketingSource[]; held: Record<string, FieldErrors> } {
  const send: TicketingSource[] = [];
  const held: Record<string, FieldErrors> = {};
  for (const s of sources) {
    const meta = catalog.find((p) => p.id === s.provider) || null;
    const missing = storedIds.has(s.id) ? [] : missingRequiredFields(meta, s);
    if (missing.length) {
      held[s.id] = Object.fromEntries(missing.map((f) => [f.key, "Required"]));
    } else {
      send.push(s);
    }
  }
  return { send, held };
}

/** The per-source field errors in a rejected PUT (`ApiError.body`), or `{}`
 * for any other failure. */
export function fieldErrorsFrom(body: unknown): Record<string, FieldErrors> {
  if (!body || typeof body !== "object") return {};
  const errors = (body as { errors?: unknown }).errors;
  if (!errors || typeof errors !== "object") return {};
  const out: Record<string, FieldErrors> = {};
  for (const [id, fields] of Object.entries(errors as Record<string, unknown>)) {
    if (!fields || typeof fields !== "object") continue;
    const clean: FieldErrors = {};
    for (const [k, v] of Object.entries(fields as Record<string, unknown>)) {
      if (typeof v === "string" && v) clean[k] = v;
    }
    if (Object.keys(clean).length) out[id] = clean;
  }
  return out;
}

/** The three-state pill, from the source's stored test — the server's rule. */
export function sourceTestState(
  source: TicketingSource,
  meta: TicketingCatalogEntry | null | undefined,
  now: number = Date.now()
): { state: SourceTestState; message: string } {
  const provider = (source.provider || "").trim().toLowerCase();
  const hasToken = !!(source.api_token || "").trim() || provider === "github_issues";
  if (!hasToken) return { state: "off", message: "no credentials yet" };
  const test = lastTestOf(source);
  if (!test) return { state: "needs_attention", message: "not tested yet — run Test" };
  if (!test.ok) return { state: "needs_attention", message: test.message || "the last test failed" };
  const missing = missingAutoFields(meta, source);
  if (missing.length) {
    return {
      state: "needs_attention",
      message:
        (test.message ? test.message + " — " : "") +
        missing.map((f) => f.label).join(", ") +
        " still to fill in (run Test)",
    };
  }
  const at = Date.parse(test.at);
  if (!Number.isFinite(at) || now - at > TEST_FRESH_MS) {
    return { state: "needs_attention", message: "last tested over a day ago — retest" };
  }
  return { state: "connected", message: test.message || "Connected" };
}

export const TEST_STATE_LABELS: Record<SourceTestState, string> = {
  connected: "Connected",
  needs_attention: "Needs attention",
  off: "Off",
};

/* --- Basic vs Advanced ------------------------------------------------------
 * A source card shows what a first setup needs (provider, the credential,
 * Test, whose tickets, which states, repo, agent) and folds the rest under
 * "Advanced options": the source's name, the catalog fields marked `advanced`
 * plus the card's own per-queue tuning below. The fold opens on its own when anything in it is
 * already set, or when the server objected to a field inside it — a value or an
 * error hidden behind a closed fold is a setting nobody can see. */

/** The card's own (non-catalog) fields that live in the fold, each with the
 * stored values that mean "not set". `depth: "agent"` reads as Off. */
export const CARD_ADVANCED_DEFAULTS: Record<string, readonly string[]> = {
  label: [""],
  effort: [""],
  depth: ["", "agent"],
};

/** The catalog fields shown up front. */
export function basicFields(meta: TicketingCatalogEntry | null | undefined): TicketingCatalogField[] {
  return (meta?.fields || []).filter((f) => !f.advanced);
}

/** The catalog fields folded under Advanced options. */
export function advancedFields(meta: TicketingCatalogEntry | null | undefined): TicketingCatalogField[] {
  return (meta?.fields || []).filter((f) => !!f.advanced);
}

/** Whether the Advanced fold should start open: an advanced field holds a
 * value someone chose, or carries an error. A Test-filled (`auto`) field
 * doesn't count — Test writes it on every connect, so it would open the fold
 * on every card. */
export function advancedHasValues(
  meta: TicketingCatalogEntry | null | undefined,
  source: TicketingSource,
  errors: FieldErrors = {}
): boolean {
  for (const f of advancedFields(meta)) {
    if (errors[f.key]) return true;
    if (!f.auto && (source[f.key] || "").trim()) return true;
  }
  for (const [key, unset] of Object.entries(CARD_ADVANCED_DEFAULTS)) {
    if (errors[key]) return true;
    if (!unset.includes((source[key] || "").trim())) return true;
  }
  return false;
}
