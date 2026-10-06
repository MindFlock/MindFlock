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
  /** The session's model override of the profile's pin ("" = the pin). */
  profile_model?: string;
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
  /** Red-zone summary for the rail chip (backend/web/core/red_zone_monitor
   * .summary). null/absent = no zones and nothing to say. */
  redzone?: RedZoneSummary | null;
  /** Lineage (MindFlock MCP): the title of the session that spawned or
   * adopted this one. "" when there is none OR the stored parent is no longer
   * a live local session (the server validates lazily). Optional: an older
   * server doesn't send it. */
  parent?: string;
  /** True when an agent created this session (the MCP's spawn_session), as
   * opposed to a human. Set once at create time, never afterwards. */
  spawned?: boolean;
  /** The playbook the session was CREATED with — "split" (the New dialog's
   * "Split across workers": an orchestrator from its first prompt, before
   * its first worker exists), "" for none. Set once at create time.
   * Optional: an older server doesn't send it. */
  playbook?: string;
  /** Whether THIS launch of the agent was given the MindFlock MCP attach args.
   * false = it launched without them (a resume after a server restart, attach
   * switched off, a CLI that can't take them) — the playbooks would name tools
   * the agent does not have; null = unknown (adopted from another process).
   * Optional: an older server doesn't send it. */
  mcp_attached?: boolean | null;
  /** On a WORKER's row: the newest `kind=result` message it sent its current
   * parent (consumed or not), or null when it has not reported. */
  last_report?: LastReport | null;
  /** Ship lanes (team runs, SPEC §5): the group this session was started in,
   * or null for a session on its own. Optional: an older server doesn't send
   * it. */
  run?: RunRef | null;
  /** How far MindFlock carries this session once its agent stops, or null for
   * none. `owner` names the window that actually drives a duplicated branch —
   * a copy window on the same branch shows the lane but is never armed. */
  lane?: LaneInfo | null;
}

/** A row's place in a group of sessions started together. */
export interface RunRef {
  id: string;
  name: string;
  task: string;
  role: "task" | "lead" | "piece" | string;
  grouping: "each" | "together" | string;
}

/** A session's ship lane. `target` "leave" means: MindFlock commits nothing. */
export interface LaneInfo {
  target: "leave" | "commit" | "push" | "pr" | "merge" | string;
  ask_first: boolean;
  owner?: string;
  /** Who chose it: "user", or "agent:<title>" (an agent's set_autopilot). */
  by?: string;
}

/** A worker's report as the rail and the playbook menu read it. `summary` is
 * server-sanitized and at most 140 chars. */
export interface LastReport {
  id: string;
  status: string;
  summary: string;
  ts: number;
}

// --- Playbooks, dialogs and the Thread (MindFlock MCP, from the UI) ---------

/** One argument a playbook takes. A "session" arg is picked from the flock, a
 * "text" arg is typed; an optional text arg left empty renders a prompt that
 * ends on a trailing "…: " so the user types it into the agent instead. */
export interface PlaybookArg {
  name: string;
  label: string;
  kind: "text" | "session";
  required: boolean;
}

/** One named prompt from `GET /api/playbooks`. `available` false comes with
 * the reason in `disabled_reason`; a `when: has_children` playbook on a
 * session with no live workers is omitted, never sent disabled. */
export interface Playbook {
  id: string;
  label: string;
  desc: string;
  letter: string;
  args: PlaybookArg[];
  when?: "any" | "has_children" | string;
  available: boolean;
  disabled_reason: string | null;
}

export interface PlaybooksResponse {
  playbooks: Playbook[];
}

/** One option of a CLI's permission/trust dialog (`GET …/dialog`). */
export interface DialogOption {
  key: string;
  label: string;
  kind: "yes" | "always" | "no" | "other" | string;
}

/** `GET /api/instances/{t}/dialog`: the prompt a session in `clarify` is
 * waiting on. `parsed` false = only the best-effort question line. */
export interface Dialog {
  id: string;
  parsed: boolean;
  question: string;
  command: string | null;
  options: DialogOption[];
  /** Who raised it, from the dialog's tab header — "general-purpose agent"
   * for a Claude background sub-agent's prompt. Only sent when known. */
  source?: string;
}

/** One member of a session's family as `GET …/thread` returns it. */
export interface ThreadMember {
  title: string;
  role: "self" | "parent" | "child" | string;
  status: string;
  activity: string;
  activity_since: number;
  branch: string;
  diff_stat: DiffStat | null;
  created_at: number;
  last_report: LastReport | null;
  base_sha: string | null;
}

/** One entry of the read-only "Between sessions" log (newest last). */
export interface ThreadItem {
  type: "spawn" | "message" | "result" | string;
  id: string;
  ts: number;
  from: string;
  to: string;
  text: string;
  status: string | null;
  state: string | null;
  base_sha: string | null;
}

export interface ThreadResponse {
  title: string;
  parent: string;
  members: ThreadMember[];
  items: ThreadItem[];
  more: boolean;
}

// --- Code map + red zones (docs/web-api.md "Code map & red zones") ----------

/** One red zone as the effective-zones routes return it. `re` is a regex
 * source valid in both Python and JS (red_zones.compile_pattern). */
export interface RedZone {
  id: string;
  pattern: string;
  name: string;
  note?: string;
  created?: number;
  scope?: "repo" | "worktree" | string;
  re?: string;
  /** A repo zone allowed ("waived") in this worktree: drawn, not enforced. */
  waived?: boolean;
  /** "red" = keep out (the v2 zone, the default when absent); "green" = only
   * here — while any enforced green zone exists, everything outside every
   * green zone is read-only. Green zones are worktree-scope only. */
  kind?: "red" | "green" | string;
}

export interface RedZoneSummary {
  zones: number;
  breaches: number;
  last_block_ts: number | null;
  guard: "guarded" | "arming" | "detect" | "off" | "none" | string;
  /** v3: "green" while the worktree has an enforced green zone. */
  mode?: "green" | "red" | null | string;
}

export interface RepoRef {
  id: string;
  label: string;
}

/** GET /api/instances/{title}/code-map. `files` rows are `[rel, size, flags]`
 * (flag 1 = zone-matched ignored file, 2 = test file); `edges` are
 * `[src, dst]` indices into `files` meaning "src imports dst". */
export interface CodeMapSnapshot {
  root: string;
  repo: RepoRef | null;
  fingerprint: string | null;
  files: Array<[string, number, number]>;
  truncated: boolean;
  edges: Array<[number, number]>;
  graph_partial: boolean;
  langs: Record<string, number>;
}

export interface ChangedFile {
  path: string;
  status: string;
  added: number;
  removed: number;
}

/** One tool-feed record (backend/providers/_tool_hook_src.py), with writes and
 * reads made repo-relative by the live route. */
export interface FeedRecord {
  v?: number;
  ts: number;
  ev: "pre" | "post" | "fail" | string;
  tool: string;
  kind: "edit" | "read" | "bash" | "plan" | "agent" | "mcp" | "other" | string;
  id?: string;
  /** set on a call made INSIDE a subagent: its agent_id (and agent_type) —
   * the Map draws each subagent as its own bird */
  agent?: string | null;
  agent_type?: string;
  /** the parent's Agent / Task call: its description and subagent_type */
  desc?: string;
  atype?: string;
  writes?: string[];
  reads?: string[];
  cmd?: string;
  /** A guard refusal. `push` marks a refused `git push` / `gh pr` / GitHub
   * push tool (the branch carries a breach): `path` is then the first breached
   * file, `pattern` null — the refusal is about the branch, not that file. */
  deny?: {
    path: string;
    pattern: string | null;
    name?: string;
    zone_id?: string | null;
    reason?: string;
    push?: boolean;
    /** v3: "green" = refused because the path is outside the green zone(s)
     * (pattern is then "outside green"); absent/"red" = a keep-out zone. */
    kind?: "red" | "green" | string;
  } | null;
  /** v3: reads outside the green zone(s) — advisory, never denied ("peeked
   * outside scope" in Activity). */
  peek?: string[] | null;
  /** v3 green backstop: new untracked files outside scope (soft flag). */
  artifact?: string[] | null;
  breach?: Array<{ path: string; pattern: string; kind?: string }> | null;
  plan?: string;
  err?: string;
  intr?: boolean;
}

export interface PlanItem {
  path: string;
  intent: string;
  new: boolean;
  /** v3: the item is outside the worktree's green zone(s). */
  outside?: boolean;
}

export interface CodeMapPlan {
  source: "declared" | "exitplan" | null | string;
  ts: number | null;
  items: PlanItem[];
  thread?: string | null;
}

export interface RedZoneBreach {
  path: string;
  /** The red pattern, or "outside green" for a green breach. */
  pattern: string;
  /** null for a green breach (it is outside every zone, not inside one). */
  zone_id: string | null;
  committed: boolean;
  kind?: "red" | "green" | string;
}

/** One companion rule (v3): a path the agent may write although it is outside
 * the green zone(s) — lockfiles, snapshots, tests importing the zone, the
 * repo's declared derived outputs. Allowed, flagged amber, never a breach.
 * `re` is a regex source like RedZone.re; a bare string is an exact path. */
export interface CompanionRule {
  pattern?: string;
  re?: string;
  source?: string;
}

export interface OtherEdit {
  session: string;
  path: string;
  ts: number;
}

/** GET /api/instances/{title}/code-map/live?since=. */
export interface CodeMapLive {
  now: number;
  fingerprint: string | null;
  repo: RepoRef | null;
  changed: ChangedFile[];
  feed: FeedRecord[];
  plan: CodeMapPlan | null;
  off_plan: string[];
  zones: RedZone[];
  breaches: RedZoneBreach[];
  /** `state` picks the pill's label; `detail` is the server's one-sentence
   * explanation (its tooltip). */
  guard: { state: string; detail?: string; hard: boolean; mode?: "green" | "red" | null | string } | null;
  activity: string;
  plan_supported: boolean;
  others: OtherEdit[];
  /** The worktree's filesystem is case-insensitive (macOS/Windows): the guard
   * matches zones ignoring case, so the Map must too. Absent on older servers
   * (treated as false). */
  ci?: boolean;
  /** v3: "green" while an enforced green zone exists, "red" with only red
   * zones, null with none. */
  mode?: "green" | "red" | null | string;
  /** v3: paths already changed outside the green zone(s) when it was added,
   * exempt from breaching while their content is unchanged. The server may
   * send `{rel: blob_sha}` or a list of paths. */
  exempt?: Record<string, string> | string[] | null;
  /** v3: companion rules (see CompanionRule) and exact companion files. */
  companions?: Array<CompanionRule | string> | null;
  companion_files?: string[] | null;
}

/** POST /api/instances/{title}/red-zones/preview. The green-only fields are
 * v3 (`kind: "green"` in the request). */
export interface RedZonePreview {
  re: string;
  count: number;
  sample: string[];
  ignored_count: number;
  changed: string[];
  writable_files?: number;
  changed_outside?: string[] | number;
  committed_outside?: number;
  roots?: string[];
  /** The typed green pattern is unanchored (matches at any depth). */
  unanchored?: boolean;
  /** Its anchored twin, offered as a one-click fix (null when no single
   * root matches). */
  anchored?: string | null;
  warnings?: string[];
}

// --- File cards + search (backend/web/core/code_outline.py) ----------------

export interface OutlineSymbol {
  name: string;
  kind: string;
  line: number;
  end?: number;
  public: boolean;
  sig: string;
  parent?: string | null;
  children?: OutlineSymbol[];
}

export interface EntryPoint {
  kind: "http" | "cli" | "event" | "main" | string;
  method: string;
  route: string;
  line: number;
  handler: string;
  path?: string;
  /** File view only: the route's lines changed vs the fork point. */
  changed?: boolean;
}

/** GET /api/instances/{title}/code-map/file?path=. */
export interface FileView {
  path: string;
  lang: string;
  loc: number;
  symbols: OutlineSymbol[];
  imports: { internal: Array<{ path: string; names: string[] }>; external: string[] };
  entry: EntryPoint[];
  used_by: Array<{ path: string; names: string[] }>;
  changed_lines: Array<[number, number]>;
  changed_symbols: string[];
  zones: { red: boolean; green: boolean | null };
  partial?: boolean;
  /** Test files that import this one (kept out of used_by). */
  tested_by?: string[];
}

export interface SearchItem {
  path: string;
  name: string;
  kind: string;
  line: number;
  score?: number;
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
  /** MindFlock MCP auto-attach: whether NEW launches attach it (settings
   * `general.agent_mcp`, overridden off by the server's MINDFLOCK_AGENT_MCP=0)
   * and which CLIs it attaches to. The one non-boolean cap; absent on an
   * older server. */
  agent_mcp?: { enabled: boolean; providers: string[] };
  /** Which team-run shapes `POST /api/runs` accepts yet: `together` = one PR
   * for the whole group, `split` = one line into parallel pieces. Both false
   * until the server's phase 3; absent on an older server (= false). Read
   * through laneActions.teamRunCaps, never directly. */
  team_runs?: { split: boolean; together: boolean };
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
  /** Server-derived, read-only: CLIs this profile has a verified route for.
   * A profile with raw `env` overrides applies to every CLI regardless. */
  supported_agents?: string[];
}

export interface AuthProfilesResponse {
  profiles: AuthProfile[];
  default_profile: string;
  kinds?: string[];
  /** Set when $MINDFLOCK_AUTH_PROFILE pins the app-wide default in the
   * server's environment: `default_profile` then reports the env value and
   * saving a different one has no effect until the variable is unset. */
  default_profile_env?: string;
  default_profile_locked?: boolean;
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

/** What POST /api/session-plan answers with: the New Session form's own fields,
 * resolved server-side from one sentence. Nothing is created by the call — this
 * is a form to read and correct, not a session.
 *
 * Every key is always sent, so nothing here is optional and nothing has to be
 * defaulted at the call site. `repo_path` is always a non-empty ABSOLUTE path,
 * because the server only ever hands back a path it built itself out of a
 * folder menu it walked: the model answers with an index into that menu, never
 * with text. That is what keeps a model answer from ever reaching the Folder
 * field as a bare name — see isNameQuery in NewSessionDialog for what the
 * server does with one of those. */
export interface PlanAnswer {
  title: string;
  repo_path: string;
  prompt: string;
  in_place: boolean;
  init_repo: boolean;
  /** Whether `repo_path` is ALREADY THERE — and the one key the dialog must
   * refuse to create a session over until the user has said yes.
   *
   * Creating a directory is the only thing a plan proposes that outlives the
   * session and that closing it never takes back: a worktree goes when the
   * session does, but nobody comes back for the folder. So false here arms the
   * confirm row in the Describe strip (see newFolderGate in NewSessionDialog),
   * and Create refuses until that row is ticked.
   *
   * False is exactly the `new:<name>` case: every numbered candidate the model
   * could pick came out of a walk of the real filesystem, so it is always true.
   * NOT the same question as `init_repo`, which is `git init` — a folder can
   * need making without needing a repo, and both can be true at once. */
  folder_exists: boolean;
  /** The ~-relative spelling of `repo_path`, for putting in that question. It is
   * the same string the `note` uses, so the question and the sentence above it
   * can never name the folder differently, and no client has to re-derive $HOME
   * to ask. It is never what a session is created with — `repo_path` is — so no
   * shortening here can change which directory gets opened. */
  folder_display: string;
  note: string;
}

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
  /** "state" = multi workflow-state filter, "state_one" = a single
   * workflow-state destination, "choice" = <select> over `options`. */
  type?: string;
  /** "choice" only. */
  options?: { value: string; label: string }[];
  hint?: string;
}

/** One connectable ticketing platform, and what it asks for. */
export interface TicketingCatalogEntry {
  id: string;
  label: string;
  /** Branch/slug prefix (`sc` for Shortcut). A new source's `id` is seeded from
   * it — the id IS the branch prefix (`feature/<id>-<ticket>/…`). */
  slug_prefix?: string;
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

// --- Team runs and the Outbox (SPEC §5) --------------------------------------
//
// A "run" is the server's object for sessions started together; on screen it is
// only ever a group header and the Outbox's tabs. Every field the UI reads is
// optional beyond the identity: the routes are new, and a server that predates
// one of them must degrade to "no groups", never to a crash.

export interface RunCounts {
  queued: number;
  active: number;
  needs_you: number;
  shipped: number;
  failed: number;
  total: number;
}

export interface RunPolicy {
  lane: string;
  ask_first: boolean;
  grouping: string;
  release?: string;
}

/** `GET /api/runs` → `{runs: RunSummary[]}`. */
export interface RunSummary {
  id: string;
  name: string;
  /** running | planning | plan_ready | checking | release_ready | releasing |
   * done | done_with_failures | cancelled */
  state: string;
  paused: boolean;
  /** user | budget | limit ("" when not paused) */
  pause_reason: string;
  policy: RunPolicy;
  counts: RunCounts;
  cost_usd: number;
  created_at: number;
}

/** One line of a run. `title` is reserved at plan time (the session it becomes).
 * `state`: queued | starting | working | needs_you | shipping | integrating |
 * shipped | integrated | failed | cancelled | skipped. */
export interface RunTask {
  id: string;
  kind: string;
  source?: string;
  ticket_id?: string;
  text: string;
  title: string;
  branch?: string;
  state: string;
  /** Set with needs_you / failed: prompt | stuck | blocked | ship_halted |
   * conflict | budget | restart | approve, or the server's own sentence. */
  reason: string;
  /** The one-line why behind needs_you / failed / a queued retry. */
  detail?: string;
  /** A failed create waits until this epoch second before its next try. */
  retry_at?: number;
  /** "checks_failed" = shipped (the PR is open) but not merged. */
  flag?: string;
  lane?: string | null;
  pr_url?: string;
  row_present?: boolean;
  /** A piece's "only here" paths (split runs). */
  paths?: string[];
  /** One-for-all / split: the branch head that was merged back. */
  head_sha?: string;
  /** Subjects of the piece's own commits. */
  commits?: string[];
  /** While handed to the lead to resolve a merge conflict, else null. */
  conflict?: { files: string[]; attempts?: number; at?: number } | null;
  /** The lead resolved its conflict. */
  conflict_fixed?: boolean;
  /** The `Tests:` line of the worker's report, or "". */
  tests?: string;
  merged_at?: number;
}

/** A split's lead (or a one-for-all group's integration session). */
export interface RunLead {
  title: string;
  branch?: string;
  base_branch?: string;
  incarnation?: number;
  adopted?: boolean;
  row_present?: boolean;
}

export interface PlanPiece {
  title: string;
  prompt: string;
  paths: string[];
}

export interface RunPlan {
  state: "proposed" | "approved" | string;
  round?: number;
  pieces: PlanPiece[];
  why?: string;
  by?: string;
  proposed_at?: number;
  approved_at?: number;
  base_sha?: string;
  note?: string;
}

/** The check on the merged branch. `none` = the repo has no check command. */
export interface RunCheck {
  state: "none" | "pending" | "running" | "ok" | "failed" | "skipped" | string;
  command?: string;
  tests?: number | null;
  summary?: string;
  sha?: string;
  attempts?: number;
  finished_at?: number;
}

/** The one PR a together/split group releases. */
export interface RunRelease {
  state: "none" | "ready" | "releasing" | "done" | "handoff" | "failed" | string;
  title?: string;
  body?: string;
  base?: string;
  branch?: string;
  lane?: string;
  pr_url?: string;
  compare_url?: string;
  head_sha?: string;
  files?: number;
  add?: number;
  del?: number;
  commits?: number;
  conflict_fixes?: number;
  /** The lead's origin when it is a folder on this machine: the release can
   * only push there, and no PR is ever opened. */
  local_origin?: string;
  detail?: string;
}

/** `GET /api/runs/{id}` → `{run: RunDTO}`. */
export interface RunDTO extends RunSummary {
  tasks: RunTask[];
  concurrency?: number;
  budget_usd?: number | null;
  /** Bumped on every change (the long-poll's `rev`). */
  rev?: number;
  /** A member is at its usage limit: nothing new starts on that CLI. */
  waiting_for_usage?: boolean;
  /** Phase 3: one-for-all and split groups. */
  split?: boolean;
  /** A split's one line (what the lead was asked to split). */
  goal?: string;
  lead?: RunLead | null;
  plan?: RunPlan | null;
  check?: RunCheck | null;
  release?: RunRelease | null;
}

export interface OutboxRunRef {
  id: string;
  name?: string;
  task?: string;
}

/** One "Waiting on you" row. `kind` "prompt" = an agent's permission prompt
 * (answered with the shared AnswerStrip), "approve" = a ship you asked to see
 * first (with `preview`), anything else = a run escalation with `reason`. */
export interface OutboxWaiting {
  key?: string;
  title: string;
  run?: OutboxRunRef | null;
  kind: string;
  reason?: string;
  since?: number;
  /** approve: the step it is held before — "commit" or "push". */
  step?: string;
  /** approve: the session's lane (how far the approval carries it). */
  lane?: string;
  /** approve: when its lane was armed — this approval's identity (an edited
   * message belongs to one card, never the next). */
  armed_at?: number;
  text?: string;
  preview?: {
    commit_message?: string | null;
    pr_title?: string | null;
    files?: number;
    add?: number;
    del?: number;
    /** plan: the proposed pieces. */
    pieces?: Array<{ title: string; paths: string[] }>;
    /** release: where the one PR goes. */
    base?: string;
    branch?: string;
    check?: string;
    /** release: the group's lane (labels the buttons; "Open the PR" never merges). */
    lane?: string;
    /** release: the lead's origin when it is a folder on this machine (push only, no PR). */
    local_origin?: string | null;
  } | null;
  actions?: string[];
}

export interface OutboxShipping {
  key?: string;
  title: string;
  run?: OutboxRunRef | null;
  step?: string;
  note?: string;
  lane?: string;
  text?: string;
}

export interface OutboxShipped {
  key?: string;
  title: string;
  run?: OutboxRunRef | null;
  pr_url?: string;
  pr_state?: string;
  checks?: string;
  commit_subject?: string;
  lane?: string | null;
  files?: number | null;
  /** The Verify checklist covering its branch, or null. */
  verify?: { id: string } | null;
  text?: string;
}

export interface OutboxQueued {
  run: OutboxRunRef;
  ref?: string | null;
  text?: string;
  /** The title reserved for it, when it has one. */
  title?: string | null;
  retry_at?: number | null;
}

export interface OutboxSummary {
  run: string;
  name: string;
  state?: string;
  finished_at?: number;
  text_md: string;
}

/** `GET /api/outbox?group=<run_id|own|all>`. */
export interface OutboxResponse {
  counts: { waiting: number; shipping: number; shipped: number; queued: number };
  groups: {
    waiting: OutboxWaiting[];
    shipping: OutboxShipping[];
    shipped: OutboxShipped[];
    queued: OutboxQueued[];
  };
  summaries?: OutboxSummary[];
}
