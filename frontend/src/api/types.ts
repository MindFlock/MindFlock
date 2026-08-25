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
  stage: Stage | string;
  /** The owner pressed "back to idle" on a finished branch: show the guided
   * ladder from the start even though `stage` (which stays git-derived truth)
   * says committed/pushed/pr. Released server-side as soon as the worktree
   * moves — see backend/web/core/stage_reset.py. */
  stage_reset?: boolean;
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

/* --- Ticketing sources (GET /api/settings/providers/ticketing, GET/PUT
 * /api/settings/ticketing/sources) ---------------------------------------
 *
 * Shared between the query cache that holds them and Intake → Tickets, which
 * renders a form from them: two copies of these would let the cache and the
 * form disagree about what a provider asks for. */

/** One input on a provider's connection card. */
export interface TicketingCatalogField {
  key: string;
  label: string;
  secret?: boolean;
  placeholder?: string;
  /** "state" = workflow-state picker, "choice" = <select>. */
  type?: string;
  /** "choice" only. */
  options?: { value: string; label: string }[];
  hint?: string;
}

/** One connectable ticketing platform, and what it asks for. */
export interface TicketingCatalogEntry {
  id: string;
  label: string;
  blurb?: string;
  fields: TicketingCatalogField[];
}

/** One connected source. Free-form beyond `id`/`provider` because each provider
 * contributes its own fields (token, workspace, project key, …). */
export type TicketingSource = Record<string, string> & { id: string; provider: string };

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

/** GET /api/test-plans (backend/web/core/test_plans.py) — the Verify surface.
 *
 * One plan per session whose branch has landed on origin: the steps a person (or
 * an agent acting for them) walks to confirm the change really works from the
 * outside, plus the history of every attempt. It is a local JSON file rather than
 * an upstream fan-out, which is why it is NOT one of the intake `PANELS` — see
 * the note above `useTestPlans` in state/queries.ts.
 *
 * The state names carry the whole lifecycle and the UI reads them literally:
 * generating (the headless one-shot is still writing the steps) → generated
 * (written, but the code has not reached the live branch yet) → due (it IS live;
 * go check it) → running (a verify session is working the agent-checkable steps)
 * → done. `failed` means generation itself fell over and `error` says why —
 * which is a different thing from a run whose verdict is "fail", since that is a
 * real and useful answer. */
export type TestStepActor = "agent" | "human";
export type TestStepResult = "pass" | "fail" | "blocked" | "";
export type TestPlanState =
  | "generating" | "generated" | "due" | "running" | "done" | "failed";

export interface TestStep {
  id: string;
  text: string;
  expect: string;
  /** Who can settle this step. "agent" = checkable from a shell (a command, a
   * file, an HTTP endpoint); "human" = visual judgement, a real browser, or an
   * external service. The server defaults anything it doesn't recognise to
   * "human", because a person confirming something is never wrong while an agent
   * silently passing what it could not actually check is. */
  actor: TestStepActor;
  /** True when a PERSON added this step rather than the generator. It is what
   * makes the step survive a regeneration (the model is being re-asked about
   * the diff, and it was never asked about this) — and, because of that, the
   * only step kind the UI offers to delete: nothing else would ever remove it. */
  manual?: boolean;
}

export interface TestStepResultEntry {
  result: TestStepResult;
  note: string;
  at: number;
  by: string;
}

export interface TestRun {
  /** The commit the run actually worked, and the one it was supposed to.
   *
   * The run prompt asks the agent to check out `origin/<live branch>`; a fetch
   * that fails quietly leaves it working whatever HEAD the worktree was cut
   * from, and the plan then records "it works" about a tree nobody can name. The
   * server asks git both questions when the answers land. Either may be "" —
   * unknown, which is never treated as a mismatch. */
  tested_sha?: string;
  expected_sha?: string;
  /** Where the steps were worked: the repo's deployment when it has one, "" when
   * a checkout was the system under test. */
  target?: string;
  at: number;
  by: string;
  session: string;
  /** Keyed by TestStep.id. Sparse on purpose: a run that gives up half way
   * settles only the steps it actually reached, and the missing ids are exactly
   * what still needs a human. */
  results: Record<string, TestStepResultEntry>;
  verdict: "pass" | "fail" | "partial";
}

export interface TestPlan {
  id: string;
  title: string;
  /** The MAIN repo, never the session's worktree: worktrees get reclaimed, and a
   * plan outlives the session that produced it. */
  repo_root: string;
  branch: string;
  sha: string;
  live_branch: string;
  /** What `repo_root` resolves to TODAY — this plan's repo asked the chain
   * again, including that repo's own override. Compare `live_branch` against
   * this, never against the response's flock-wide `live_branch`: plans are
   * stamped per repo, so a repo with an override would otherwise read as
   * permanently out of date against a default it was never measured by. */
  effective_live_branch: string;
  state: TestPlanState;
  error: string;
  generated_at: number;
  /** When the CURRENT generation attempt started (epoch seconds; 0 = never, or
   * a plan written before the server stamped it). `generated_at` is when one
   * finished — a plan that never finishes is what this one is for: past
   * `GENERATION_STALE_S` the attempt is abandoned, not slow, and both the server
   * and the row stop waiting for it. */
  gen_started: number;
  /** Generation attempts since the last one settled. The server auto-retries a
   * stalled generation once and then parks the plan in `failed`. */
  gen_attempts: number;
  /** When the work was first seen MERGED. Distinct from `live_at`, which is
   * when it became yours to check: merged is a git fact, true the instant a PR
   * lands, while what a checklist tests is a service a pipeline reaches minutes
   * later. The gap between the two is the repo's deploy window. */
  merged_at: number;
  live_at: number;
  /** The branch on origin this work has most recently reached — "" while it is
   * still only on the branch it was pushed to.
   *
   * NOT `live_branch`, which is the branch this checklist is WAITING for. In a
   * repo that ships from `main` through a `staging` step the two disagree for
   * most of a change's life, and the disagreement is the interesting part: the
   * work is merged, just not where the checklist is watching. Ancestry answers
   * it where it can; a squash-merged branch is answered by its PR's base. */
  merged_into: string;
  /** When it got there (epoch seconds; 0 = the rung that answered could not say,
   * which is the squash-merge case). */
  merged_into_at: number;
  /** The trail, best first and one name per landing — `["main", "staging"]` for
   * work promoted from staging. Branches that arrived in the same merge are
   * folded together, so this is not "every branch that contains the commit":
   * every branch cut from `main` after a merge does. */
  merged_into_all: string[];
  /** One sentence, in a user's words, naming what this change lets somebody do.
   * The model writes it alongside the steps; "" for a plan generated before the
   * contract asked for one, which is why nothing may depend on it existing.
   *
   * This is what `title` should have been. `title` is the session's name — the
   * key everything addresses the plan by — so a checklist coming due three weeks
   * later was headed "sc-1234-fix-filters" over a list of imperatives, and the
   * reader had to reconstruct what shipped from the steps themselves. */
  summary: string;
  /** What this work was ASKED to do, snapshotted at push time (ticket title,
   * description and acceptance criteria, or the prompt somebody typed).
   *
   * Stored on the plan rather than read off the session, because plans outlive
   * sessions: read live, every rewrite after the session was deleted ran with no
   * ticket at all — i.e. the button you press because the first draft missed the
   * point ran on strictly less evidence than the draft it replaced. */
  intent: string;
  /** What you said the last draft got wrong, from the rewrite box. Kept so a
   * later push that re-reads the branch keeps honouring it. */
  focus: string;
  /** When the "it shipped" push went out, so it goes out once. */
  notified_at: number;
  /** Why this checklist is not coming due, when the answer is not "not yet".
   *
   * Distinct from `error`, which means an operation you asked for went wrong.
   * Nothing failed here: the plan is waiting for a branch origin does not have,
   * which is a configuration answer and the user's to fix. Clears itself the
   * moment the branch shows up. */
  live_problem: string;
  steps: TestStep[];
  /** Capped server-side (newest kept), so this is recent history, not all of it. */
  runs: TestRun[];
  /** The live run's session title; "" when nothing is running. */
  run_session: string;
}

export interface TestPlansResponse {
  plans: TestPlan[];
  /** The FLOCK-WIDE default, resolved server-side (repository.live_branch,
   * falling back through pr_base_branch / base_branch to "main") with no repo in
   * the question, so the UI can name what a repo that overrides nothing
   * inherits. What an individual plan waits on is its own
   * `effective_live_branch`, which may differ. */
  live_branch: string;
}
