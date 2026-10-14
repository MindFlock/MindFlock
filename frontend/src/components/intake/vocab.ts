/** Intake's user-facing wording, kept apart from the components so it can be
 * tested without a DOM and so the four tabs say one thing one way.
 *
 * Three jobs:
 *
 * - **Reason chips.** The server explains every skipped row in the pipeline's
 *   own vocabulary ("not in an ingest state — won't auto-ingest", "already
 *   ingested (recorded completed in the ledger)"). The UI says *auto-start*,
 *   never *ingest*, and never "ledger" — `reasonText` translates the known
 *   phrasings and passes anything else through untouched.
 * - **Source errors.** A provider that refuses the token answers with its raw
 *   JSON (`Shortcut API returned 401: {"message":"Unauthorized",…}`).
 *   `friendlySourceError` turns that into one sentence a person can act on.
 * - **The saved safety filter.** The states a source auto-starts from are
 *   stored as provider ids on the source itself. The ticket listing can only
 *   NAME them when the tracker answers, which made an unreachable source read
 *   as "no filter set" — the opposite of the truth. `savedFilter` reads the
 *   filter from the saved source, so "is there a filter?" never depends on the
 *   network, and only the names do.
 */

import type { TicketingSource, TicketingWorkflowState } from "../../api/types";

/** Server skip reasons (ticket_start.skip_reasons, pr_review, issue_start) in
 * the UI's words. Matched on their stable stems, so a server that adds detail
 * after the stem still translates. */
const REASONS: Array<[RegExp, (m: RegExpMatchArray) => string]> = [
  [/^not in an ingest state\b.*$/i, () => "not in an auto-start state"],
  [/^missing an ingest label \(([^)]*)\).*$/i, (m) => "missing an auto-start label (" + m[1] + ")"],
  [/^already ingested\b.*$/i, () => "already started before"],
  [/^skipped earlier \(recorded in the ledger\)$/i, () => "skipped earlier"],
  [/^already in the processed ledger \(([^)]*)\)$/i, (m) => "already handled before (" + m[1] + ")"],
  [/^queued for ingestion \(pending\)$/i, () => "already queued to start"],
  [/^already reviewed \(recorded in the processed ledger\)$/i, () => "already handled before"],
  [/^already handled \(recorded in the processed ledger\)$/i, () => "already handled before"],
  [/^not assigned to the configured member id$/i, () => "not assigned to you"],
];

export function reasonText(reason: string): string {
  const r = (reason || "").trim();
  for (const [re, fn] of REASONS) {
    const m = r.match(re);
    if (m) return fn(m);
  }
  return r;
}

/** One sentence for a source the panel could not list, under its display
 * name — never the provider's raw JSON body. `name` is what the user knows the
 * source as ("Shortcut", "Jira — payments"). */
export function friendlySourceError(name: string, raw: string, onCard = false): string {
  // On the card itself, "open its card below" points at where you already are.
  if (onCard) return friendlySourceError(name, raw).replace(/ — (open its card below to fix it|check its (permissions on the )?card below|finish its card below)/, "");
  const text = String(raw || "").trim();
  const status = text.match(/\b(?:HTTP\s*)?(401|403|404|429|5\d\d)\b/i)?.[1] || "";
  if (status === "401" || /unauthori[sz]ed|invalid token|bad credentials/i.test(text))
    return name + " rejected the token — open its card below to fix it.";
  if (status === "403" || /forbidden/i.test(text))
    return name + " refused access with this token — check its permissions on the card below.";
  if (status === "404") return name + " couldn't find what this source points at — check its card below.";
  if (status === "429") return name + " is rate-limiting requests — try Refresh in a minute.";
  if (status.startsWith("5")) return name + " is having trouble (server error) — try Refresh later.";
  if (/timed? ?out|timeout|connection|resolve|network|unreachable|ssl/i.test(text))
    return name + " can't be reached — check your network, then Refresh.";
  if (/missing|required/i.test(text)) return name + " isn't set up yet — finish its card below.";
  // Unknown: keep the words, drop any JSON tail.
  const clean = text.replace(/[:\s]*[{[].*$/s, "").trim();
  return name + " can't be listed" + (clean ? " — " + clean : "") + ".";
}

/** The state ids a source auto-starts from, as SAVED (the catalog's
 * `workflow_state` field, comma-joined). Never depends on the tracker. */
export function savedFilter(source: TicketingSource | null | undefined): string[] {
  return String((source && source.workflow_state) || "")
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
}

/** The saved auto-start labels, as typed (comma-separated). */
export function savedLabels(source: TicketingSource | null | undefined): string[] {
  return String((source && source.ingest_labels) || "")
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
}

/** A state id's name, from whatever list is at hand; null when unknown. */
export function stateName(
  id: string,
  ...lists: Array<TicketingWorkflowState[] | undefined>
): string | null {
  for (const list of lists) {
    const st = (list || []).find((s) => String(s.id) === String(id));
    if (st && st.name) return st.name;
  }
  return null;
}

/** "A", "A and B", "A, B and C", then "A, B and 4 more". */
export function listNames(names: string[]): string {
  if (names.length <= 2) return names.join(" and ");
  if (names.length === 3) return names[0] + ", " + names[1] + " and " + names[2];
  return names.slice(0, 2).join(", ") + " and " + (names.length - 2) + " more";
}

/** What a source's safety filter is, in words, for its group heading and the
 * panel hint. `names` are the state names the tracker gave (may be fewer than
 * the saved ids, or none, when it can't be reached).
 *
 * - `kind: "states"` — a saved state filter; `text` names them when it can,
 *   else counts them ("1 saved state").
 * - `kind: "labels"` — no states, but auto-start labels.
 * - `kind: "none"` — genuinely nothing saved: every state auto-starts. */
export function filterSummary(opts: {
  ids: string[];
  names: string[];
  labels: string[];
}): { kind: "states" | "labels" | "none"; text: string; named: boolean } {
  const { ids, names, labels } = opts;
  if (ids.length) {
    const named = names.length >= ids.length;
    const text = named
      ? listNames(names)
      : ids.length + (ids.length === 1 ? " saved state" : " saved states");
    return { kind: "states", text, named };
  }
  if (labels.length) return { kind: "labels", text: "label " + listNames(labels), named: true };
  return { kind: "none", text: "every state", named: true };
}
