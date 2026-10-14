/** A one-shot "open Settings → Accounts on the API-key form" request.
 *
 * Setup ②'s "Using an API key instead?" link lands on Accounts, an advanced
 * screen whose add form starts on "CLI account" — the wrong form for someone
 * who said "API key". The link leaves this intent; Accounts takes it once on
 * mount (`takeAccountsIntent()`) and opens its add form with kind `api_key`
 * preselected for the default agent. A plain module value, not storage: it
 * only has to survive the dialog switch, and a reload should not re-open it. */

export type AccountsIntent = { kind: "api_key"; provider: string };

let pending: AccountsIntent | null = null;

export function requestApiKeyForm(provider: string): void {
  pending = { kind: "api_key", provider: provider || "claude" };
}

/** The pending intent, cleared as it is read (so it applies exactly once). */
export function takeAccountsIntent(): AccountsIntent | null {
  const out = pending;
  pending = null;
  return out;
}
