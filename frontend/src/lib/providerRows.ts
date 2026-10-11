/** Settings → Agent providers: the row each provider (and each broken custom
 * TOML) renders as, derived from `GET /api/providers/status`. Pure, so the
 * installed / signed-in / not-installed / parse-error shapes are unit-tested
 * without a DOM (the screen only lays rows out). */

import type { ProviderLoadError, ProviderStatus } from "../api/types";

export type SignInState =
  | "signed-in"
  | "not-signed-in"
  | "unknown"
  | "none"
  | "not-installed";

export interface ProviderRow {
  kind: "provider";
  name: string;
  isDefault: boolean;
  installed: boolean;
  /** Chip text: "installed" / "not installed". */
  installLabel: string;
  /** Chip tooltip: the resolved path, or why it's missing. */
  installTitle: string;
  /** Copy-paste install command, only when not installed. */
  installHint: string;
  /** Why a custom CLI can't be resolved (alias, bad path), only when not installed. */
  launchHint: string;
  signIn: SignInState;
  /** Chip text for the sign-in state ("" when not installed: one chip is enough). */
  signInLabel: string;
  /** Tooltip / detail: the evidence found, or what "unknown" means. */
  signInDetail: string;
  /** Offer the in-app login terminal. */
  canSignIn: boolean;
  /** This device's saved binary path ("" = none). */
  binaryPath: string;
  /** The env var that pins the binary instead, "" when none. */
  binaryPinnedBy: string;
}

export interface ErrorRow {
  kind: "error";
  file: string;
  error: string;
}

export type Row = ProviderRow | ErrorRow;

/** The sign-in verdict for one provider.
 *
 * Evidence wins. A provider that declares no login step at all needs none
 * (aider reads API keys). One that declared where its credentials live and
 * has none there is probably logged out. Anything else is "unknown" — a
 * miss from a CLI that hides its token is not a logout, so the screen says
 * so instead of nagging. */
export function signInState(p: ProviderStatus): SignInState {
  if (!p.installed) return "not-installed";
  if (p.authenticated) return "signed-in";
  if (p.has_login === false) return "none";
  if (p.auth_probe) return "not-signed-in";
  return "unknown";
}

const SIGN_IN_LABEL: Record<SignInState, string> = {
  "signed-in": "signed in",
  "not-signed-in": "not signed in",
  unknown: "sign-in unknown",
  none: "no sign-in needed",
  "not-installed": "",
};

function signInDetail(p: ProviderStatus, state: SignInState): string {
  if (state === "signed-in") return p.auth_detail || "login found";
  if (state === "none") return "reads API keys or has no login step — nothing to sign in to";
  if (state === "not-signed-in") return "CLI is installed but no sign of a login was found";
  if (state === "unknown") return "this CLI keeps its login where MindFlock doesn't look — check inside the CLI";
  return "";
}

export function providerRow(p: ProviderStatus): ProviderRow {
  const state = signInState(p);
  return {
    kind: "provider",
    name: p.name,
    isDefault: !!p.is_default,
    installed: !!p.installed,
    installLabel: p.installed ? "installed" : "not installed",
    installTitle: p.installed
      ? p.path || "on PATH"
      : p.binary_override
        ? `${p.binary_override} is not an executable file`
        : "not found on PATH",
    installHint: !p.installed ? p.install_hint || "" : "",
    launchHint: !p.installed ? p.launch_hint || "" : "",
    signIn: state,
    signInLabel: SIGN_IN_LABEL[state],
    signInDetail: signInDetail(p, state),
    // The in-app terminal runs a login flow the provider DECLARED; signed-in
    // rows don't need it, and an absent CLI can't run it.
    canSignIn: !!p.installed && !!p.has_login && !p.authenticated,
    binaryPath: p.binary_override || "",
    binaryPinnedBy: p.binary_override_env || "",
  };
}

export function errorRow(e: ProviderLoadError): ErrorRow {
  return { kind: "error", file: e.file, error: e.error };
}

/** Providers in server order, then one red row per TOML that failed to load. */
export function providerRows(
  statuses: ProviderStatus[],
  errors?: ProviderLoadError[] | null
): Row[] {
  return [...statuses.map(providerRow), ...(errors || []).map(errorRow)];
}

/** Settings → Agent CLI's default picker: every known provider, with the
 * not-installed ones disabled and saying why (you can't launch what isn't
 * there, but hiding them made "where's codex?" a mystery). */
export interface PickerOption {
  name: string;
  disabled: boolean;
  label: string;
}

export function pickerOptions(
  statuses: Array<Pick<ProviderStatus, "name" | "installed">>
): PickerOption[] {
  return statuses.map((p) => ({
    name: p.name,
    disabled: !p.installed,
    label: p.installed ? p.name : `${p.name} — not installed`,
  }));
}
