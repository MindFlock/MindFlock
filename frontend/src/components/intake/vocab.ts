/** Intake's user-facing wording, kept apart from the components so it can be
 * tested without a DOM and so the four tabs say one thing one way.
 *
 * Two jobs (the server's skip reasons already speak this vocabulary —
 * ticket_start.skip_reasons, pr_review, issue_start — so rows show them as-is):
 *
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

/** One sentence for a source the panel could not list, under its display
 * name — never the provider's raw JSON body. `name` is what the user knows the
 * source as ("Shortcut", "Jira — payments"). */
export function friendlySourceError(name: string, raw: string, onCard = false): string {
  // In the source's own editor, "press Fix on its heading" points at where you
  // already are.
  if (onCard) return friendlySourceError(name, raw).replace(/ — (press Fix on its heading|check its permissions with Fix on its heading|check it with Fix on its heading|finish it with Fix on its heading)/, "");
  const text = String(raw || "").trim();
  const status = text.match(/\b(?:HTTP\s*)?(401|403|404|429|5\d\d)\b/i)?.[1] || "";
  if (status === "401" || /unauthori[sz]ed|invalid token|bad credentials/i.test(text))
    return name + " rejected the token — press Fix on its heading.";
  if (status === "403" || /forbidden/i.test(text))
    return name + " refused access with this token — check its permissions with Fix on its heading.";
  if (status === "404") return name + " couldn't find what this source points at — check it with Fix on its heading.";
  if (status === "429") return name + " is rate-limiting requests — try Refresh in a minute.";
  if (status.startsWith("5")) return name + " is having trouble (server error) — try Refresh later.";
  if (/timed? ?out|timeout|connection|resolve|network|unreachable|ssl/i.test(text))
    return name + " can't be reached — check your network, then Refresh.";
  if (/missing|required/i.test(text)) return name + " isn't set up yet — finish it with Fix on its heading.";
  // Unknown: keep the words, drop any JSON tail.
  const clean = text.replace(/[:\s]*[{[].*$/s, "").trim();
  return name + " can't be listed" + (clean ? " — " + clean : "") + ".";
}

/** Where each tracker hands out the token its source asks for — the
 * "Create a token ↗" link under the API token field (GitHub has its own in
 * the Connect GitHub dialog). */
export const TOKEN_URLS: Record<string, string> = {
  jira: "https://id.atlassian.com/manage-profile/security/api-tokens",
  shortcut: "https://app.shortcut.com/settings/account/api-tokens",
  linear: "https://linear.app/settings/account/security",
  asana: "https://app.asana.com/0/my-apps",
};

/** The host a source's Site URL names ("acme.atlassian.net"), or "" when it
 * names none. */
export function siteHost(url: string | undefined): string {
  const raw = String(url || "").trim();
  if (!raw) return "";
  try {
    return new URL(/^[a-z]+:\/\//i.test(raw) ? raw : "https://" + raw).host;
  } catch {
    return "";
  }
}

/** What a failed Test means, in a sentence that names the field to fix.
 * Jira's own answers ("Jira API returned HTTP 404") say nothing about which of
 * its three fields is wrong; everything else keeps the tracker's words, which
 * already say it (Shortcut: "rejected the token (HTTP 401)"). */
export function friendlyTestError(provider: string, raw: string, baseUrl?: string): string {
  const text = String(raw || "").trim();
  if (provider !== "jira") return text;
  const host = siteHost(baseUrl) || "that address";
  const status = text.match(/\bHTTP\s*(\d{3})\b/i)?.[1] || text.match(/\breturned (\d{3})\b/i)?.[1] || "";
  if (status === "404") return "No Jira site at " + host + " — check the Site URL";
  if (status === "401" || status === "403" || /rejected the credentials/i.test(text))
    return "Jira rejected the email + token pair";
  if (/network error|reaching jira|timed? ?out|resolve|connect/i.test(text))
    return "Can't reach " + host + " from this machine";
  return text;
}

/** One sentence for a ticket that failed to START because its tracker
 * refused — never the provider's raw JSON body. Anything that isn't a tracker
 * refusal (a git error, a full disk) keeps its own words, minus a JSON tail. */
export function friendlyStartError(name: string, raw: string): string {
  const text = String(raw || "").trim();
  const status = text.match(/\bAPI returned (\d{3})\b/i)?.[1] || "";
  if (status === "401" || (!!status && /unauthori[sz]ed|bad credentials/i.test(text)))
    return name + " rejected the API token. Fix it in Intake → Tickets — press Fix on the source's heading.";
  if (status === "403") return name + " refused access with this token. Check its permissions in Intake → Tickets.";
  if (status === "404") return name + " couldn't find this ticket — it may have been deleted or moved.";
  if (status === "429") return name + " is rate-limiting requests — try again in a minute.";
  if (status.startsWith("5")) return name + " is having trouble (server error) — try again later.";
  return text.replace(/[:\s]*[{[].*$/s, "").trim() || "the server gave no reason";
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
 * - `kind: "waiting"` — nothing saved on a source that needs a pick first
 *   (`needs_filter`, every source Intake creates): NOTHING auto-starts.
 * - `kind: "none"` — nothing saved on an older source: every state
 *   auto-starts. */
export function filterSummary(opts: {
  ids: string[];
  names: string[];
  labels: string[];
  /** The source's `needs_filter` is on. */
  waiting?: boolean;
}): { kind: "states" | "labels" | "none" | "waiting"; text: string; named: boolean } {
  const { ids, names, labels } = opts;
  if (ids.length) {
    const named = names.length >= ids.length;
    const text = named
      ? listNames(names)
      : ids.length + (ids.length === 1 ? " saved state" : " saved states");
    return { kind: "states", text, named };
  }
  if (labels.length) return { kind: "labels", text: "label " + listNames(labels), named: true };
  if (opts.waiting) return { kind: "waiting", text: "nothing yet", named: true };
  return { kind: "none", text: "every state", named: true };
}
