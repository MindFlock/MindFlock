/** The first-run plan (`GET /api/onboarding`, built by backend/onboarding.py)
 * and the small decisions Setup makes from it — pure, so vitest covers them.
 *
 * The server owns the order and every step's status; Setup only draws it.
 * The order is deps → devices ("First computer, or join one you already
 * have?") → agent → tailscale → github → repo: the join question comes
 * BEFORE the agent and GitHub steps because joining brings the default agent,
 * the GitHub token and ticket sources (never the agent's sign-in — accounts
 * stay on each computer). */

export type StepId = "deps" | "devices" | "agent" | "tailscale" | "github" | "repo";
export type StepStatus = "ok" | "todo" | "skip";

export interface OnboardingStep {
  id: StepId;
  title: string;
  status: StepStatus | string;
  reason: string;
  /** devices: the stored answer ("first" | "join" | ""). */
  choice?: string;
  /** devices: the question still needs asking. */
  ask?: boolean;
  /** agent: the provider, and whether it declares a login flow. */
  provider?: string;
  sign_in?: boolean;
  /** tailscale: the one fix. */
  fix?: string;
  /** github: whether git knows who you are. */
  identity?: boolean;
  /** The terminal equivalent (mindflock init prints it). */
  cli?: string;
}

export interface OnboardingPlan {
  steps: OnboardingStep[];
  next: StepId | "";
  done: boolean;
  os?: string;
}

/** The order every surface uses — the server's, restated so a stale or
 * partial payload still draws in the right order. */
export const STEP_ORDER: StepId[] = ["deps", "devices", "agent", "tailscale", "github", "repo"];

/** The plan's steps in {@link STEP_ORDER} (unknown ids dropped). */
export function orderedSteps(plan: OnboardingPlan | null | undefined): OnboardingStep[] {
  const by = new Map((plan?.steps || []).map((s) => [s.id, s]));
  return STEP_ORDER.map((id) => by.get(id)).filter((s): s is OnboardingStep => !!s);
}

/** The step the person should look at now: the server's `next`, else the
 * first one still to do. "" when everything is done. */
export function currentStep(plan: OnboardingPlan | null | undefined): StepId | "" {
  if (!plan) return "";
  if (plan.next) return plan.next;
  return orderedSteps(plan).find((s) => s.status === "todo")?.id || "";
}

/** ①…⑥ for a step, counted over the steps Setup draws (`ids`, in order). */
export function stepNumber(id: StepId, ids: StepId[] = STEP_ORDER): string {
  const i = ids.indexOf(id);
  return i < 0 ? "•" : "①②③④⑤⑥".charAt(i) || "•";
}

/** Whether a step should be drawn at all. Tailscale is only part of the plan
 * when going multi-device; until then it isn't worth a row. */
export function stepVisible(s: OnboardingStep): boolean {
  return !(s.id === "tailscale" && s.status === "skip");
}

/** The steps Setup draws, in order. Before the plan has loaded, every step
 * but Tailscale (the checklist still works from its own probes). */
export function visibleSteps(plan: OnboardingPlan | null | undefined): StepId[] {
  if (!plan) return STEP_ORDER.filter((id) => id !== "tailscale");
  return orderedSteps(plan)
    .filter(stepVisible)
    .map((s) => s.id);
}

export const STATUS_GLYPH: Record<string, string> = { ok: "✓", todo: "•", skip: "–" };

// --- Connect GitHub ----------------------------------------------------------

export interface GithubFlow {
  state: "idle" | "pending" | "done" | "expired" | "denied" | "error" | string;
  user_code: string;
  verification_uri: string;
  expires_at: number;
  interval: number;
  login: string;
  error: string;
}

export interface GithubStatus {
  connected: boolean;
  source: string;
  login?: string;
  scopes?: string[];
  user_error?: string;
  gh?: { installed: boolean; authenticated: boolean };
  /** Sign-in methods available here, best first: device | gh | token. */
  methods?: string[];
  token_url?: string;
  identity?: { name: string; email: string };
  identity_suggested?: { name: string; email: string };
  credential_helper?: string;
  flow?: GithubFlow;
}

export type GithubMethod = "device" | "gh" | "token";

/** The one way Setup offers first: GitHub's device flow when an OAuth App is
 * configured, else `gh auth login --web` when gh is installed, else a
 * pre-filled token page + a paste box. */
export function primaryGithubMethod(st: GithubStatus | null | undefined): GithubMethod {
  const m = st?.methods || [];
  if (m.includes("device")) return "device";
  if (m.includes("gh") || st?.gh?.installed) return "gh";
  return "token";
}

/** Whether the identity form should show: git doesn't know your name or
 * email yet. */
export function needsIdentity(st: GithubStatus | null | undefined): boolean {
  const id = st?.identity;
  return !!st && !(id?.name && id?.email);
}

/** The device-flow poll is done (stop polling). */
export function flowFinished(f: GithubFlow | null | undefined): boolean {
  return !!f && f.state !== "pending";
}

/** Seconds until the next poll: the interval GitHub set, never under 2s. */
export function pollDelay(f: GithubFlow | null | undefined): number {
  return Math.max(2, Number(f?.interval || 5));
}

// --- Devices: each member's own readiness (GET /api/fleet/readiness) ---------

export interface MemberReadiness {
  version?: string;
  deps_ok?: boolean;
  missing?: string[];
  agent?: { provider: string; signed_in: boolean | null };
  deferred?: string[];
  push?: { ok: boolean | null; message: string };
  tailscale?: { running: boolean; key_expiry_days: number | null; key_expired: boolean; key_warn: boolean };
  ready?: boolean;
  fixes?: string[];
  error?: string;
}

/** One line for a member row: "ready to work", or what's missing there —
 * computed by that member about itself; the fix runs on that device. */
export function readinessLine(r: MemberReadiness | null | undefined): { text: string; warn: boolean } | null {
  if (!r) return null;
  if (r.error) return { text: "readiness unknown — " + r.error, warn: false };
  const bits: string[] = [];
  if (r.missing?.length) bits.push("missing " + r.missing.join(", "));
  if (r.agent?.signed_in === false) bits.push((r.agent.provider || "agent") + " not signed in");
  if (r.deferred?.length) bits.push(r.deferred.join(", ") + " not installed");
  if (r.push?.ok === false) bits.push("can't push");
  if (r.tailscale?.key_expired) bits.push("Tailscale key expired");
  else if (r.tailscale?.key_warn && r.tailscale.key_expiry_days != null)
    bits.push("Tailscale key expires in " + r.tailscale.key_expiry_days + "d");
  if (!bits.length) return { text: "ready to work", warn: false };
  return { text: bits.join(" · "), warn: true };
}

/** Whether the doctor's install plan carries a synced agent CLI (settings
 * sync is holding a default agent back until it is installed here). */
export function hasSyncedAgentStep(steps: { id: string }[] | null | undefined): boolean {
  return (steps || []).some((s) => /^synced-.+-cli$/.test(s.id));
}

/** The toast for `session.push_failed`: what went wrong and where the fix
 * is. `setup` = the fix is in Setup (Connect GitHub / git identity). */
export function pushFailedNote(
  session: string,
  data: { reason?: string; message?: string; fix?: string } | null | undefined
): { text: string; setup: boolean } {
  const reason = String(data?.reason || "");
  const setup = reason === "https_auth" || reason === "identity";
  const what = String(data?.message || "the push was refused");
  const tail = setup
    ? reason === "identity"
      ? " — set your git name and email in Setup"
      : " — Connect GitHub in Setup"
    : data?.fix
      ? " — " + data.fix
      : "";
  return { text: "Push failed on " + session + ": " + what + tail, setup };
}
