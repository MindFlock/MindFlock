/** Shapes served by the FastAPI backend (see mindflock/web/server.py and
 * web/core/snapshot.py). Core polling payloads are typed strictly; long-tail
 * settings payloads stay loose (Record) — they are round-tripped, not
 * interpreted, by the UI. */

export interface DiffStat {
  files: number;
  additions: number;
  deletions: number;
  uncommitted?: { files: number; additions: number; deletions: number } | null;
}

export interface QueueSummary {
  pending: number;
  enabled: boolean;
  loop: boolean;
  wait_for_limit: boolean;
  limited_until: number;
}

export interface BudgetStatus {
  cost: number;
  base: number;
  limit: number;
  expires: number | null;
  locked: boolean;
}

export interface SetupSummary {
  state: string;
  steps?: Array<{ name: string; state: string; detail?: string }>;
  failed_step?: string | null;
}

export type Activity = "working" | "clarify" | "limit" | "idle" | "offline";

export type Stage =
  | "provisioning"
  | "agent"
  | "precommit"
  // A pre-commit hook blocked the commit. The server has always emitted this and
  // every consumer handles it; it was simply missing from the union.
  | "interrupt"
  | "committed"
  | "pushed"
  | "pr"
  | "merged";

/** How far an armed session is being carried automatically, and where it is.
 *
 * One concept with two entry points: the per-session fast-track button and the
 * per-item / per-source depth on an ingested ticket, PR or issue. `state` is
 * "running" | "halted" | "done"; a halted run always carries a `reason`, because
 * a chain that stops silently is the failure mode that destroys trust in it. */
export interface AutopilotRun {
  depth: string;
  state: "running" | "halted" | "done" | string;
  step: string;
  reason: string;
  source: string;
  item: string;
  /** What the current pass is waiting on, in the server's own words ("waiting for
   * checks to finish", "prompt queue still has work"). */
  note?: string;
  /** The PR this run opened, so the client can bring it up exactly once. */
  url?: string;
  skipped?: string[];
}

/** Whether a branch's PR can actually be merged, and what is stopping it.
 *
 * The absence of this object (null) means "we could not find out" — no token, no
 * GitHub behind origin, no open PR, or a network fault. It never means "no": the
 * UI must leave the merge affordance alone rather than claim knowledge. */
export interface MergeState {
  number: number;
  url: string;
  /** GitHub's mergeable_state: clean | dirty | blocked | behind | unstable | draft
   * | unknown. */
  state: string;
  mergeable: boolean | null;
  checks: "ok" | "failed" | "pending" | "none" | "unknown" | string;
  can_merge: boolean;
  blockers: string[];
}

export interface Instance {
  title: string;
  branch: string;
  repo: string;
  folder: string;
  folder_label: string;
  program: string;
  provider: string;
  path: string;
  status: string; // "running" | "paused" | "loading" | …
  started: boolean;
  tmux_name: string;
  provisioned: boolean;
  workspace_strategy: string;
  in_place: boolean;
  diff_stat: DiffStat | null;
  workspace_missing: boolean;
  has_origin: boolean;
  /** Auth profile: the stored pin ("" = inherit the global default,
   * "default" = the CLI's own login), its resolution, and the resolved
   * profile's display label ("" when no profile applies). Optional: an older
   * server doesn't send them. */
  profile_id?: string;
  profile_effective?: string;
  profile_label?: string;
  stage: Stage | string;
  pr_url: string | null;
  /** Present only at the "pr" stage; null = could not find out. */
  merge_state?: MergeState | null;
  failed_step?: string | null;
  /** The failing pre-commit hook's ID (not its display name — pre-commit's
   * `name:` is free text and cannot be mapped back to an id). Keys the retry. */
  failed_hook?: string | null;
  autopilot?: AutopilotRun | null;
  queue: QueueSummary | null;
  tokens: number;
  tokens_in: number;
  tokens_cache_read: number;
  tokens_cache_write: number;
  tokens_ctx: number;
  tokens_ctx_window: number;
  tokens_cost: number;
  tokens_model: string;
  budget: BudgetStatus | null;
  activity: Activity | string;
  activity_since: number;
  last_turn: string | null;
  /** First line of the newest USER prompt (≤120 chars) — pinned above the
   * agent terminal so you always see what the session was asked to do.
   * Optional: an older server doesn't send it. */
  last_prompt?: string | null;
  /** The same prompt's whole body (capped ~4000 chars) — the pin's
   * hover/click expansion. */
  last_prompt_full?: string | null;
  setup: SetupSummary | null;
  check: SetupSummary | null;
  ports: { base: number; count: number } | null;
  /** Present on rows proxied from another tailnet device (title is
   * "<device>::<title>"). */
  device?: string;
  /** True for a force-started PR/issue/ticket the server has accepted but
   * whose session does not exist yet (it is still cloning). The row shows as
   * provisioning; there is nothing to act on until it becomes real. */
  pending?: boolean;
}

export interface Caps {
  git: boolean;
  tailscale: boolean;
  ticketing: boolean;
  /** MindFlock can open/merge PRs itself — gh is authenticated OR a GitHub
   * token resolves. False only means "we can't do it for you": pushing is
   * always plain git, and the PR surfaces fall back to GitHub's own compare
   * page. Optional so an older server that doesn't report it is treated as
   * capable (feature-detected with `=== false`, never `!caps.github`). */
  github?: boolean;
}

export interface Config {
  /** The resolved fast-track rung, for LABELLING the ⏩ button. The server still
   * decides the actual depth when a request omits one. */
  fasttrack_depth?: string;
  default_program: string;
  provisioning_available: boolean;
  caps: Caps;
  home: string;
  ide_name: string;
  onboarded: boolean;
  auth_mode: string;
  auth_enabled: boolean;
}

/** One auth profile (Settings → Accounts): an identity a session's CLI can run
 * under. `api_key` is always the mask sentinel or "" on the wire. */
export interface AuthProfile {
  id: string;
  label?: string;
  kind: "account" | "api_key" | "openrouter" | string;
  provider?: string;
  config_dir?: string;
  api_key?: string;
  base_url?: string;
  model?: string;
  env?: Record<string, string>;
  /** Server-derived, read-only: where an account profile's login lives. */
  resolved_config_dir?: string;
  /** Server-derived, read-only: the shell command that logs its CLI in. */
  login_command?: string;
}

export interface AuthProfilesResponse {
  profiles: AuthProfile[];
  default_profile: string;
  kinds?: string[];
}

export interface Device {
  name: string;
  host: string;
  ip?: string;
  os?: string;
  connected: boolean;
  has_token?: boolean;
  note?: string;
}

export interface DevicesResponse {
  self: string | null;
  devices: Device[];
}

/** /api/usage — per-provider usage descriptors. Rendering is data-driven, so
 * the UI treats most of this as opaque. */
export interface UsageWindow {
  label?: string;
  used_pct?: number | null;
  resets_at?: number | null;
  [k: string]: unknown;
}

export interface ProviderUsage {
  provider: string;
  plan?: string | null;
  windows?: UsageWindow[];
  tokens?: number;
  cost?: number;
  [k: string]: unknown;
}

export interface UsageResponse {
  providers?: ProviderUsage[];
  mode?: string;
  [k: string]: unknown;
}

export interface QueueItem {
  id: string;
  text: string;
  queued_at?: number;
  flags?: Record<string, unknown>;
}

export interface QueueState {
  items: QueueItem[];
  paused: boolean;
  draining?: boolean;
  [k: string]: unknown;
}

export interface DoctorCheck {
  name: string;
  ok: boolean;
  required?: boolean;
  detail?: string;
  hint?: string;
}

export type Json = Record<string, unknown>;

/** GET /api/traffic (backend/web/addons/traffic.py) — GitHub stars/forks,
 * per-release download counts, and click totals for the mindflock.ai/go/
 * tracked links. `errors.*` is set when that ONE section's upstream failed;
 * the rest of the payload still renders. */
export interface TrafficReleaseAsset {
  name: string;
  downloads: number;
}

export interface TrafficStarPoint {
  day: string;
  stars: number;
}

export interface TrafficRelease {
  tag: string;
  published_at: string | null;
  prerelease: boolean;
  assets: TrafficReleaseAsset[];
  total_downloads: number;
}

export interface TrafficClickRow {
  day: string;
  slug: string;
  os: string;
  clicks: number;
}

/* The people-shaped click sections. Every grain is counted by the Worker at
 * that grain and must be READ at that grain: unique visitors are not additive,
 * so summing `TrafficVisitorDay.visitors` over a window does NOT give
 * `TrafficClickTotals.visitors` — one person visiting on ten days is ten daily
 * uniques and one window unique. `new_visitors` is the one field that does
 * sum, since a first sighting happens on exactly one date.
 *
 * All of these are null/empty against a Worker deployed before visitor
 * attribution existed, and against click rows written before that deploy. */
export interface TrafficVisitorDay {
  day: string;
  visitors: number;
  new_visitors: number;
  returning_visitors: number;
  unknown_visitors: number;
}

export interface TrafficVisitorSlug {
  slug: string;
  visitors: number;
  new_visitors: number;
  clicks: number;
}

export interface TrafficClickTotals {
  clicks: number;
  visitors: number;
  new_visitors: number;
}

/** First-time visitors who went on to click a platform download button —
 * the closest observable proxy for new-user acquisition, since GitHub's
 * download counters carry no identity. `by_slug` can overlap (one person
 * clicking macOS and Linux is in both), so it may sum to more than
 * `new_visitors_clicked`; that field is the deduped one. */
export interface TrafficDownloadFunnel {
  new_visitors: number;
  new_visitors_clicked: number;
  by_slug: Array<{ slug: string; new_visitors: number; visitors: number; clicks: number }>;
}

export interface TrafficResponse {
  generated: number;
  repo: { stars: number | null; forks: number | null; open_issues: number | null; url: string } | null;
  star_history: TrafficStarPoint[];
  releases: TrafficRelease[];
  downloads_total: number;
  clicks: {
    days: number;
    series: TrafficClickRow[];
    totals_by_slug: Record<string, number>;
    visitors_by_day: TrafficVisitorDay[];
    visitors_by_slug: TrafficVisitorSlug[];
    totals: TrafficClickTotals | null;
    downloads: TrafficDownloadFunnel | null;
    error: string;
  };
  errors: { github: string | null; clicks: string | null };
}
