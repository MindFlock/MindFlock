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
  | "committed"
  | "pushed"
  | "pr"
  | "merged";

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
  pr_url: string | null;
  failed_step?: string | null;
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
