/** The Verify stop's four plain choices (where / ready when / who checks /
 * notes) and post-to-PR, and the repo's ENVIRONMENTS they name — the pure
 * half of the stop's editor, its face sentence and the repo card's
 * environments list (docs/flights.md "Verify stops").
 *
 * Wire shapes (the server normalizes; we stay robust to older configs):
 *  - step config: {where: "branch" | <env name>, ready: "now" | "on_branch" |
 *    "after_prev", ready_minutes: 0..1440 (0 = the environment's deploy time),
 *    who: "both" | "agent" | "you", notes, post_to_pr}. A legacy
 *    `wait_live: true` reads as where "live", ready "on_branch".
 *  - GET /api/test-plans/environments → {repos: {"owner/name": [env…]}, names}.
 *  - repository.verify_repo_settings[slug].environments: [{name, reach,
 *    branch, deploy_minutes}]. */

export type VerifyWhere = string; // "branch" or an environment's name
export type VerifyReady = "now" | "on_branch" | "after_prev";
export type VerifyWho = "both" | "agent" | "you";

export interface VerifyConfig {
  where: VerifyWhere;
  ready: VerifyReady;
  ready_minutes: number;
  who: VerifyWho;
  notes: string;
  post_to_pr: boolean;
}

/** One environment as the repo card stores it. */
export interface VerifyEnv {
  name: string;
  reach: string;
  branch: string;
  deploy_minutes: number;
}

/** One environment as GET /api/test-plans/environments lists it. */
export interface VerifyEnvInfo extends VerifyEnv {
  /** The repo's old fields read as "live" (nothing configured by that name). */
  implicit?: boolean;
}

export interface VerifyEnvsResponse {
  repos: Record<string, VerifyEnvInfo[]>;
  names: string[];
}

export const WHERE_BRANCH = "branch";
export const READY_MINUTES_MAX = 1440;
export const ENV_NAME_MAX = 40;
export const ENV_REACH_MAX = 500;
export const VERIFY_NOTES_MAX = 2000;

const READY: readonly VerifyReady[] = ["now", "on_branch", "after_prev"];
const WHO: readonly VerifyWho[] = ["both", "agent", "you"];

const int = (v: unknown, d: number): number => {
  const n = Number(v);
  return v !== "" && v !== null && v !== undefined && Number.isFinite(n) ? Math.trunc(n) : d;
};

/** A stop's config as the editor and the face read it: defaults filled, a
 * legacy `wait_live: true` mapped to where "live" + ready "on_branch" (only
 * where the new fields say nothing — a config the server already normalized
 * wins). */
export function verifyConfig(c: Record<string, unknown> | null | undefined): VerifyConfig {
  const cfg = c || {};
  const legacy = cfg.wait_live === true || cfg.wait_live === "true";
  const rawWhere = String(cfg.where ?? "").trim();
  const where = rawWhere || (legacy ? "live" : WHERE_BRANCH);
  const rawReady = String(cfg.ready ?? "");
  const ready: VerifyReady = (READY as readonly string[]).includes(rawReady) ? (rawReady as VerifyReady) : legacy ? "on_branch" : "now";
  const rawWho = String(cfg.who ?? "");
  const who: VerifyWho = (WHO as readonly string[]).includes(rawWho) ? (rawWho as VerifyWho) : "both";
  return {
    where,
    ready: where === WHERE_BRANCH && ready === "on_branch" ? "now" : ready,
    ready_minutes: Math.min(READY_MINUTES_MAX, Math.max(0, int(cfg.ready_minutes, 0))),
    who,
    notes: String(cfg.notes ?? ""),
    post_to_pr: cfg.post_to_pr === true || cfg.post_to_pr === "true",
  };
}

/** The config to save after one choice changes: the normalized config with
 * the change, the legacy `wait_live` dropped (the new fields say it now).
 * Picking where "branch" while ready is "on_branch" — a branch has no
 * environment branch to reach — moves ready to "now". */
export function withVerifyChoice(c: Record<string, unknown> | null | undefined, patch: Partial<VerifyConfig>): Record<string, unknown> {
  const next = { ...verifyConfig(c), ...patch };
  if (next.where === WHERE_BRANCH && next.ready === "on_branch") next.ready = "now";
  const out: Record<string, unknown> = { ...(c || {}), ...next };
  delete out.wait_live;
  return out;
}

/** "Ready when" choices for a `where`: "When it reaches the environment's
 * branch" means nothing on this branch's own code, so it is left out there. */
export function readyChoices(where: string): Array<{ value: VerifyReady; label: string }> {
  const all: Array<{ value: VerifyReady; label: string }> = [
    { value: "now", label: "Right away" },
    { value: "on_branch", label: "When it reaches the environment's branch" },
    { value: "after_prev", label: "Minutes after the stop before" },
  ];
  return where === WHERE_BRANCH ? all.filter((o) => o.value !== "on_branch") : all;
}

/** Whether "Minutes" shows: only when there is something to wait after. */
export function showsReadyMinutes(ready: string): boolean {
  return ready === "on_branch" || ready === "after_prev";
}

export const READY_MINUTES_HINT = "0 = the environment's deploy time";

/** Who checks, in the stop editor's words, with its one-line explanation. */
export const WHO_CHOICES: ReadonlyArray<{ value: VerifyWho; label: string; explain: string }> = [
  { value: "both", label: "Agent, then me", explain: "The agent checks what it can; the ones only a person can check park the route here for you." },
  { value: "agent", label: "Agent only", explain: "Person-only checks stay open on the checklist but don't hold the route." },
  { value: "you", label: "Only me", explain: "Every check waits for you." },
];

/** Who checks, as the face says it. */
export const VERIFY_FACE_WHO: Readonly<Record<VerifyWho, string>> = { both: "agent, then you", agent: "agent only", you: "you" };

// --- Environments known to the UI --------------------------------------------------

let known: VerifyEnvsResponse | null = null;

/** Remember the server's environments list (the query that fetches it calls
 * this), so a stop's face can say "once on release" — the environment's
 * branch — without the diagram reaching for the network. */
export function rememberEnvironments(r: VerifyEnvsResponse | null | undefined): void {
  known = r && typeof r === "object" ? { repos: r.repos || {}, names: r.names || [] } : null;
}

/** The branch environment `name` deploys from, when every repo that has it
 * agrees on one; "" otherwise. */
export function envBranch(name: string, envs: VerifyEnvsResponse | null | undefined = known): string {
  const branches = new Set<string>();
  for (const list of Object.values(envs?.repos || {})) for (const e of list || []) if (e.name === name && e.branch) branches.add(e.branch);
  return branches.size === 1 ? [...branches][0] : "";
}

/** "Verify on sandbox · agent, then you · 15 min after the stop before". */
export function verifyFace(c: Record<string, unknown> | null | undefined, envs: VerifyEnvsResponse | null | undefined = known): string {
  const v = verifyConfig(c);
  const onBranch = v.where === WHERE_BRANCH;
  const where = onBranch ? "this branch" : v.where;
  return "Verify on " + where + " · " + VERIFY_FACE_WHO[v.who] + " · " + readyWords(v, envs);
}

/** "right away" · "once on release" (+ "+ 15 min") · "15 min after the stop
 * before" · "after the deploy time" (0 minutes: the environment's own). */
export function readyWords(v: VerifyConfig, envs: VerifyEnvsResponse | null | undefined = known): string {
  const onBranch = v.where === WHERE_BRANCH;
  if (v.ready === "on_branch" && !onBranch) {
    const b = envBranch(v.where, envs) || v.where;
    return "once on " + b + (v.ready_minutes > 0 ? " + " + v.ready_minutes + " min" : "");
  }
  if (v.ready === "after_prev") {
    if (v.ready_minutes > 0) return v.ready_minutes + " min after the stop before";
    // This branch's code has no deploy to wait for.
    return onBranch ? "after the stop before" : "after the deploy time";
  }
  return "right away";
}

// --- The where picker ----------------------------------------------------------------

export interface WhereOption {
  value: string;
  label: string;
  /** The repos that have it, and how each is reached — the chip's hover. */
  title?: string;
  /** Not one the server lists (typed by hand, or since removed). */
  custom?: boolean;
}

/** The where picker's choices: "This branch's code", then every environment
 * the server knows (its repos and how to reach it as the hover), then the
 * current value if it is none of those (so it stays visible until changed).
 * "Other…" is the picker's own, not an option here. */
export function whereOptions(envs: VerifyEnvsResponse | null | undefined, current = ""): WhereOption[] {
  const out: WhereOption[] = [{ value: WHERE_BRANCH, label: "This branch's code", title: "A fresh checkout of this branch — no deployment" }];
  const names: string[] = [];
  for (const n of envs?.names || []) if (n && n !== WHERE_BRANCH && !names.includes(n)) names.push(n);
  for (const list of Object.values(envs?.repos || {})) for (const e of list || []) if (e.name && e.name !== WHERE_BRANCH && !names.includes(e.name)) names.push(e.name);
  for (const n of names) out.push({ value: n, label: n, title: envTitle(n, envs) || undefined });
  const cur = current.trim();
  if (cur && !out.some((o) => o.value === cur)) out.push({ value: cur, label: cur, title: "Not an environment of any watched repo yet", custom: true });
  return out;
}

/** "owner/app: https://… · deploys from sandbox, 10 min" per repo with it. */
export function envTitle(name: string, envs: VerifyEnvsResponse | null | undefined): string {
  const lines: string[] = [];
  for (const [repo, list] of Object.entries(envs?.repos || {})) {
    const e = (list || []).find((x) => x.name === name);
    if (!e) continue;
    const bits = [e.reach?.trim() ? clip(e.reach.trim(), 60) : "", e.branch ? "deploys from " + e.branch : "", e.deploy_minutes ? e.deploy_minutes + " min to deploy" : ""].filter(Boolean);
    lines.push(repo + (bits.length ? ": " + bits.join(" · ") : ""));
  }
  return lines.join("\n");
}

const clip = (s: string, n: number) => (s.length > n ? s.slice(0, n - 1) + "…" : s);

// --- Repo environments (the Verify repo card) ---------------------------------------

const ENV_NAME_RE = /^[a-z0-9_-]+$/;

/** Why a name can't be saved, "" when it can — the server's own rules:
 * 1..40 lowercase letters, digits, - and _; not "branch"; unique per repo
 * (`others`: the other rows' names; "live" counts as taken only when a row
 * says so — the implicit one is overridden by configuring it). */
export function envNameError(name: string, others: readonly string[] = []): string {
  const n = name.trim();
  if (!n) return "Give it a name";
  if (n.length > ENV_NAME_MAX) return "At most " + ENV_NAME_MAX + " characters";
  if (!ENV_NAME_RE.test(n)) return "Lowercase letters, digits, - and _ only";
  if (n === WHERE_BRANCH) return "“branch” means this branch's own code — pick another name";
  if (others.includes(n)) return "Another environment has this name";
  return "";
}

/** One error per row ("" = fine). */
export function envRowErrors(rows: readonly Pick<VerifyEnv, "name">[]): string[] {
  return rows.map((r, i) => envNameError(r.name, rows.filter((_, j) => j !== i).map((x) => x.name.trim())));
}

/** The environments a repo block stores (robust to junk). */
export function envsOf(block: unknown): VerifyEnv[] {
  const list = block && typeof block === "object" ? (block as Record<string, unknown>).environments : null;
  if (!Array.isArray(list)) return [];
  return list
    .filter((e): e is Record<string, unknown> => !!e && typeof e === "object")
    .map((e) => ({
      name: String(e.name ?? ""),
      reach: String(e.reach ?? ""),
      branch: String(e.branch ?? ""),
      deploy_minutes: Math.min(READY_MINUTES_MAX, Math.max(0, int(e.deploy_minutes, 0))),
    }));
}

/** A row just added and not typed in yet: left out of what is saved. */
export const isBlankEnv = (r: VerifyEnv): boolean => !r.name.trim() && !r.reach.trim() && !r.branch.trim() && !int(r.deploy_minutes, 0);

/** The list to store: trimmed, reach capped, minutes clamped, untouched new
 * rows left out — or null when a row is not valid yet (nothing is saved until
 * every name is). */
export function envsToSave(rows: readonly VerifyEnv[]): VerifyEnv[] | null {
  const kept = rows.filter((r) => !isBlankEnv(r));
  if (envRowErrors(kept).some(Boolean)) return null;
  return kept.map((r) => ({
    name: r.name.trim(),
    reach: r.reach.trim().slice(0, ENV_REACH_MAX),
    branch: r.branch.trim(),
    deploy_minutes: Math.min(READY_MINUTES_MAX, Math.max(0, int(r.deploy_minutes, 0))),
  }));
}
