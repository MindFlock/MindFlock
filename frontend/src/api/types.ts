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

// --- First-run checklist (GET /api/setup; backend/web/core/setup_steps.py) ----

export type SetupStepId = "tools" | "agent_login" | "repo" | "github" | "tracker" | "phone";

/** Which group a step is drawn in: `start` = Get started (the required
 * three), `connect` = Connect your work (optional, counted), `later` =
 * Optional, later (folded, not counted in the summary). */
export type SetupGroup = "start" | "connect" | "later";

/** One checklist step, as stored (`general.setup`) and as drawn. */
export interface SetupStep {
  id: SetupStepId | string;
  title: string;
  required: boolean;
  /** Absent from an older server: the client's STEP_GROUP fills it in. */
  group?: SetupGroup | string;
  done: boolean;
  at: string | null;
  skipped: boolean;
  status: "done" | "skipped" | "todo" | string;
}

/** The chip's "3/5" and the two gates: `required_done` retires the grid
 * card, `complete` retires the chip. */
export interface SetupProgress {
  done: number;
  total: number;
  required_done: boolean;
  complete: boolean;
  next: SetupStepId | "" | string;
}

/** One agent CLI's sign-in state (step 2 shows a button per installed one).
 * `logged_in` is null when the CLI names no credential location to check. */
export interface SetupLogin {
  name: string;
  installed: boolean;
  logged_in: boolean | null;
  login_cmd: string;
  default: boolean;
}

export interface SetupStatus {
  steps: SetupStep[];
  summary: SetupProgress;
  doctor: {
    ok: boolean;
    checks: Array<Record<string, unknown>>;
    install?: { steps?: Array<{ id: string; label: string; cmd: string }>; packages?: string[] };
  } | null;
  /** The doctor's Tailscale row (status != "ok" → "Install Tailscale"). */
  tailscale: { status: string; detail: string; fix: string; docs: string } | null;
  logins: SetupLogin[];
  default_provider: string;
  /** What `mindflock serve` printed at a first start ("" on later boots). */
  report: string;
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

/** A job the agent backgrounded that outlived its turn
 * (backend/providers/claude_background.py). */
export interface BackgroundJob {
  /** Claude Code's task id. */
  id: string;
  pid: number;
  command: string;
  /** running: still doing work · waiting: quiet, will wake the agent when it
   * ends · stuck: can't finish (proven, or quiet past the limit). */
  state: "running" | "waiting" | "stuck" | string;
  reason: string;
  started: number;
  quiet_s: number;
  /** How long it is expected to take, in seconds (lib/jobExpectation). The
   * fields below are optional: an older server doesn't send them. */
  expect_s?: number | null;
  /** Who said so — a person beats the agent beats the command's
   * `MINDFLOCK_EXPECT=6h` prefix. */
  expected_by?: "agent" | "you" | "command" | null;
  /** Epoch seconds it should be done by (started + expect_s). */
  eta?: number | null;
  /** The longest quiet stretch it has come back from (learned). */
  longest_gap_s?: number;
  /** Quiet this long and it is called stuck. */
  threshold_s?: number;
  /** Running longer than expect_s (only with an expectation). */
  past_expected?: boolean;
}

/** Why and since when a session's agent is asleep (Instance.sleeping). */
export interface SleepInfo {
  /** Epoch seconds it fell asleep. */
  since: number;
  /** "idle" (the sweep), "memory" (the sweep, sooner: memory was low), "manual". */
  reason: string;
  /** Roughly how much memory stopping it gave back, in MB (0 = unknown). */
  freed_mb: number;
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
  /** Whether the session's folder is in a git repository (a worktree session
   * always is; an in-place one when its folder or a parent has a .git) — what
   * greys out Branch for a plain folder. Optional: older servers don't send
   * it, and absent reads as "yes" (the server still refuses). */
  git_repo?: boolean;
  diff_stat: DiffStat | null;
  workspace_missing: boolean;
  /** Asleep (backend core/sleep.py): the agent was stopped on purpose, idle,
   * to give its memory back — its `activity` reads "offline" meanwhile. Opening
   * it or sending it anything wakes it, resuming the conversation. Optional:
   * an older server (or a remote device on one) never sends it. */
  sleeping?: SleepInfo | null;
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
  /** The feature that owns this session and keeps it off the rail, the window
   * numbers and the prompt-target pickers: `"verify"` for a checklist run,
   * `""` for the user's own work. Optional only because an older server
   * doesn't send it — see `isVerifySession` for the fallback that covers. */
  hidden_by?: string;
  /** A guest link's crossed-out row (backend.web.core.guests): the server
   * sent title and status only, and every call on it is a 403. Never set
   * for the owner. lib/guest.ts keeps these out of `useInstances()`. */
  locked?: boolean;
  /** The id of the person the session was created for (GET /api/person;
   * docs/people.md). `""` for a session that predates people or one a peer
   * link made. Optional: an older server doesn't send it. */
  person?: string;
  /** Works ON a pushed branch others push to too (created with
   * `continue_branch`; backend/web/core/shared_branch.py). */
  shared_branch?: boolean;
  /** Commits the remote branch has that this session lacks, from the last
   * fetch (push check, rebase, or a shared session's 5-minute refresh). */
  behind?: number;
  /** Who the session belongs to (defaults to `person`; changed only by POST
   * /owner), who holds its keyboard (`""` = nobody) and who has a terminal
   * open on it — backend/web/core/ownership.py, docs/people.md. Optional: an
   * older server doesn't send them. Remote rows carry their device's. */
  owner?: string;
  driver?: string;
  /** How to name the driver in a sentence ("" when nobody drives). */
  driver_name?: string;
  viewers?: Record<string, Viewer>;
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
  /** An agent the kernel killed (likely out of memory) coming back by itself
   * ("pending"), or left Offline after too many kills ("gave_up") — the server's
   * auto-revive (backend/web/core/revive.py). `reason` replaces the Offline
   * chip's stock hint. */
  auto_revive?: { state: "pending" | "gave_up"; reason: string } | null;
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
  /** The session is asking you something (backend ask-signal; lib/ask.ts):
   * a dialog on screen, or a reply that ended with a question nobody has
   * answered. Optional: an older server doesn't send it. */
  asking?: {
    kind: "dialog" | "question";
    text: string | null;
    ts: number;
    dialog_id: string | null;
    parsed: boolean;
    source: string | null;
  } | null;
  /** Jobs the agent backgrounded and left running after its turn ended
   * (lib/backgroundJobs). Optional: an older server doesn't send it. */
  background?: BackgroundJob[] | null;
  last_turn: string | null;
  /** First line of the newest USER prompt (≤120 chars) — pinned above the
   * agent terminal so you always see what the session was asked to do.
   * Optional: an older server doesn't send it. */
  last_prompt?: string | null;
  /** The same prompt's whole body (capped ~4000 chars) — the pin's
   * hover/click expansion. */
  last_prompt_full?: string | null;
  /** What the session is working on as a short verb phrase ("Fix pin bar
   * below terminal"), read from its newest MEANINGFUL prompt — "continue",
   * "yes" and MindFlock's own notes keep the previous one; the create task's
   * before any prompt. The rail row's task line. Optional: an older server
   * doesn't send it. */
  task_headline?: string | null;
  /** The prompt `task_headline` was read from (the remembered meaningful
   * one, or the create task) — the task line's hover, so a later "continue"
   * never stands in for it. Optional: an older server doesn't send it. */
  task_headline_prompt?: string | null;
  /** What the session was asked to do when it was created (the create
   * prompt, trimmed; first line is what the UI shows) — the pane's "Task:"
   * line until the transcript names a newer prompt. Null when it was started
   * without one. Optional: an older server doesn't send it. */
  task?: string | null;
  setup: SetupSummary | null;
  check: SetupSummary | null;
  ports: { base: number; count: number } | null;
  /** Present on rows proxied from another tailnet device (title is
   * "<device>::<title>"). */
  device?: string;
  /** That device's hostname (falls back to `device`), for showing WHERE. */
  device_label?: string;
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
  /** Where this worker stands in its orchestrator's order (core.worker_order):
   * held until its turn, running, done or stopped — null when it was never
   * ordered or fenced. Optional: an older server doesn't send it. */
  order?: RowOrder | null;
  /** A shared-folder (peer-link) session: it runs sandboxed, and the server
   * refuses ship / push / PR / the shell pane / the IDE / rename / copies for
   * it (backend core/peer_guard.py). Optional: an older server doesn't send
   * it. */
  peer_share?: boolean;
  /** For a shared session: who it is shared with, and whether they are
   * connected now. null/absent otherwise. */
  peer_with?: { link_id: string; name: string; connected: boolean } | null;
}

/** A fence an orchestrator set on one worker: the only paths it may change
 * and/or paths it may not (globs, red-zone syntax). */
export interface WorkerFence {
  only: string[];
  keep_out: string[];
  reason?: string;
  by?: string;
}

/** A worker's row field: its order state and what it waits on. */
export interface RowOrder {
  state: "held" | "running" | "done" | "stopped" | string;
  word: string;
  detail: string;
  after: string[];
  fence: WorkerFence | null;
}

/** One card of the Thread's order diagram. `planned` = a title a step names
 * that no worker has yet. */
export interface OrderCard {
  title: string;
  state: "held" | "running" | "done" | "stopped" | "planned" | string;
  word: string;
  detail: string;
  after: string[];
  /** Why it runs after each predecessor: "asked", "step", "one at a time",
   * or "overlap: <path>". */
  why: Record<string, string>;
  fence: WorkerFence | null;
  released_at: number | null;
  ended_at: number | null;
  planned: boolean;
}

/** The order an orchestrator's workers run in (`GET …/order`, and the
 * thread's `order`): steps left to right, each one's cards run together. */
export interface WorkerOrder {
  mode: "parallel" | "serial" | string;
  max_parallel: number;
  /** How many may run at once: 1 for serial, max_parallel, 0 = no limit. */
  cap: number;
  steps: Array<{ n: number; workers: OrderCard[] }>;
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
  /** Claude's question prompt (Dialog kind "select") adds "text" (Type
   * something), "chat" (Chat about this). */
  kind: "yes" | "always" | "no" | "other" | "text" | "chat" | string;
  /** The tmux key names that pick it, when not just `key` (an arrow-key
   * picker): /answer is sent `keys ?? [key]`. */
  keys?: string[];
  /** A select option's description line, when it has one. */
  desc?: string | null;
  /** A multi-select's checkbox: ticked or not; null/absent = no checkbox. */
  checked?: boolean | null;
}

/** One tab of Claude's question prompt: a question, or the closing Submit. */
export interface DialogStep {
  label: string;
  done: boolean;
  /** The review step ("Submit"): `review` holds the answers. */
  submit?: boolean;
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
  /** "dialog" (a prompt on screen) or "question" (the agent's reply ended
   * with a question in plain words — id "q:<ts>", no options; answered by
   * typing, never /answer). Absent from an older server: a dialog. */
  kind?: "dialog" | "question" | "select";
  /** The dialog's own heading ("Permission required", "Ready to code?"),
   * when the provider reads one. */
  heading?: string;
  /** kind "select" (Claude's AskUserQuestion): several answers can be
   * ticked — digits toggle, `nav.next` moves on. */
  multi?: boolean;
  /** kind "select": its tabs, the questions then Submit; `step` the shown one. */
  steps?: DialogStep[];
  step?: number;
  /** kind "select", the review step: what was answered. */
  review?: Array<{ question: string; answer: string }>;
  /** kind "select": the keys that move between steps and submit. */
  nav?: { next: string[]; prev: string[]; submit: string[] };
  /** kind "select": the option key under Claude's cursor. */
  cursor?: string | null;
  /** kind "select", multi: the prompt's own button ("Next" / "Submit"). */
  button?: string | null;
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

/** A worker that was closed or deleted, as its orchestrator's Thread keeps
 * it (backend.web.core.finished_children): what it was, how far it got, and
 * its final report. `how` "closed" = reopenable from Recently closed. */
export interface FinishedChild {
  title: string;
  branch: string;
  created_at: number;
  ended_at: number;
  how: "closed" | "deleted" | string;
  stage: string;
  pr_url: string;
  diff_stat: DiffStat | null;
  last_report: LastReport | null;
}

export interface ThreadResponse {
  title: string;
  parent: string;
  members: ThreadMember[];
  /** Workers that are gone, oldest first (absent on an older server = []). */
  finished: FinishedChild[];
  items: ThreadItem[];
  more: boolean;
  /** The order its workers run in, or null (absent on an older server). */
  order?: WorkerOrder | null;
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
  /** A per-session fence's zone (scope "session"): only the orchestrator that
   * set it (`by`) lifts it — no remove / allow from the Map. */
  locked?: boolean;
  by?: string;
  /** A zone the REPO shares (scope "shared"): from the committed
   * `.mindflock/zones.toml` in this worktree. Who added it and when. */
  author?: string;
  added?: string;
  /** On a local zone: the committed file has the same zone. */
  shared?: boolean;
  shared_author?: string;
  /** Where a shared zone comes from (".mindflock/zones.toml"). */
  source?: string;
}

/** GET/POST /api/instances/{title}/shared-zones — the repo's committed zones
 * as this session's worktree has them (they follow its branch). */
export interface SharedZonesBody {
  shared: RedZone[];
  /** Why the file is being ignored ("" = fine / no file). */
  shared_error: string;
  shared_path: string;
  shared_exists: boolean;
  /** POST only: the zone written, and whether it was new. */
  zone?: RedZone;
  created?: boolean;
}

/** A 409 from POST /api/tickets/start: the tracker says a teammate is on it.
 * `override` names the field the owner re-POSTs to start it anyway. */
export interface TicketClaimedBy {
  kind: "tracker" | string;
  name: string;
  why: "label" | "started" | string;
}

export interface RedZoneSummary {
  zones: number;
  breaches: number;
  last_block_ts: number | null;
  guard: "guarded" | "arming" | "detect" | "off" | "none" | string;
  /** v3: "green" while the worktree has an enforced green zone. */
  mode?: "green" | "red" | null | string;
  /** Files in the agent's declared plan while it waits for Go (the Map's
   * plan loop); absent/0 when none, once work started or after Go. */
  plan_pending?: number;
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
    /** The session's own fence refused it (an orchestrator's only= /
     * keep_out=): no scope request — the worker reports blocked. */
    fence?: boolean;
  } | null;
  /** A fence event MindFlock put on the feed (`ev: "mindflock"`, `kind:
   * "fence"`): what changed the session's fence, who and why. An ending
   * (`ended`, `done`) is the fence finishing, never an unblock. */
  fence?: {
    event: "set" | "narrowed" | "widened" | "replaced" | "lifted" | "ended" | "done" | "removed" | string;
    by?: string;
    reason?: string;
    only?: string[];
    keep_out?: string[];
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
  /** The fences on this session's workers (an orchestrator's Map lists them
   * as "Workers' fences"). Absent on older servers. */
  /** `last`: the newest change after the fence was set (a fence that was
   * lifted leaves the worker listed with empty paths and this). */
  fences?: Array<WorkerFence & { session: string; last?: FeedRecord["fence"] }>;
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
  /** max_pieces: the most pieces a split may have (MINDFLOCK_MAX_CHILDREN). */
  team_runs?: { split: boolean; together: boolean; max_pieces?: number };
  /** The spawn guard-rails as they apply now (Settings → Agent orchestration):
   * each cap's effective value, built-in default, where it comes from (an env
   * var overrides the setting) and that env var's name. Absent on an older
   * server. */
  orchestration?: Record<"max_children" | "max_spawn_depth" | "max_spawned", OrchestrationCap>;
}

export interface OrchestrationCap {
  value: number;
  default: number;
  source: "env" | "settings" | "default";
  env: string;
}

export interface Config {
  /** The resolved fast-track rung (never "off") — what `POST /fast-track`
   * arms when a request omits a depth. */
  fasttrack_depth?: string;
  /** THE fast-track default for new sessions (Settings → Workspace "Fast-track
   * goes as far as"): a rung, or "off". Seeds the New dialog. */
  fasttrack_default?: string;
  default_program: string;
  provisioning_available: boolean;
  caps: Caps;
  home: string;
  ide_name: string;
  onboarded: boolean;
  auth_mode: string;
  auth_enabled: boolean;
  /** Set when this browser is signed in with a guest link: the owner's label
   * for it and the session titles it opens. Null (or absent, older server)
   * for the owner. */
  guest?: GuestScope | null;
}

/** What a guest link opens (`/api/config` → `guest`). */
export interface GuestScope {
  label: string;
  sessions: string[];
}

/** One guest link as the owner sees it (`GET /api/guests`): the URL carries
 * the token; the row never does on its own. */
export interface GuestLink {
  id: string;
  label: string;
  sessions: string[];
  created_at: number;
  url: string;
  phone_url: string;
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

/** One other MindFlock server on the tailnet (GET /api/devices). */
export interface Device {
  /** The MagicDNS label: the key a remote session's title is namespaced by
   * (`<device>::<title>`) and the `<device>` in /api/devices/<device>/…. */
  device: string;
  host: string;
  ip?: string;
  os?: string;
  version?: string;
  reachable?: boolean;
  remote_control?: boolean;
  auth?: boolean;
  has_token?: boolean;
  needs_token?: boolean;
  /** One of "Your devices": it stays connected through the shared device
   * key, so Disconnect doesn't apply — remove it in Settings → Your devices. */
  member?: boolean;
  /** The "Your devices" join protocol it speaks (0 = too old to join). */
  fleet_proto?: number;
  /** It belongs to some group of devices; to this one's. */
  in_fleet?: boolean;
  same_fleet?: boolean;
  /** Paired, permitted and answering: sessions can be driven AND started there. */
  connected: boolean;
  error?: string;
  sessions?: number;
  note?: string;
}

export interface DevicesResponse {
  self: { device: string; host: string; os?: string } | null;
  remote_control?: boolean;
  devices: Device[];
}

/** "Your devices" (GET /api/fleet): the computers one person owns, sharing a
 * fleet key — settings sync, sign-in and ticket claims only talk to these. */
export interface FleetMember {
  /** The device key (MagicDNS label), as in /api/devices. */
  key: string;
  host: string;
  added_at: number;
  self: boolean;
  reachable: boolean;
  version: string;
  /** Its hello names the same fleet — false while it hasn't heard yet, or
   * after it left/was removed while offline. */
  same_fleet: boolean;
  error: string;
  /** It runs PR review and issue handling: the synced
   * `github.automation_device`, or — when that names no live member — the
   * live member with the lowest key, so exactly one does. Missing from an
   * older MindFlock (null: its hello doesn't say). */
  automation?: boolean | null;
  /** Same group id and epoch but a different key (two halves set up apart);
   * also said in `error`. Missing from an older MindFlock. */
  key_conflict?: boolean;
  /** Why it can't be reached right now ("connection refused on :8765 — …",
   * "asleep — Tailscale last saw it 2 h ago"), "" when it can or nobody knows. */
  reason?: string;
  /** When MindFlock last answered there (epoch s, 0 = never seen). */
  last_seen?: number;
  /** The commit its engine was installed from ("" = unknown: an editable
   * install, or a MindFlock too old to say). */
  commit?: string;
  /** How its engine is installed: `uv-tool` (updatable from here),
   * `editable` (a dev checkout) or `other`; "" when its hello doesn't say. */
  install?: string;
  /** Its desktop app's version ("" = none reported: headless, or a
   * browser-only setup). */
  shell_version?: string;
}

/** One device's row in an "Update all my devices" rollout. */
export interface FleetRolloutRow {
  key: string;
  host: string;
  self?: boolean;
  step: "queued" | "updating" | "restarting" | "done" | "current" | "skipped" | "failed" | "not_started";
  detail?: string;
  shell_version?: string;
}

/** GET /api/fleet → `update` (backend.web.core.fleet_update.status). */
export interface FleetRollout {
  state: "idle" | "running" | "done" | "halted";
  tag: string;
  version: string;
  error?: string;
  started_at?: number;
  finished_at?: number;
  members: FleetRolloutRow[];
}

/** A tombstone: a device taken out of the group, and who took it out —
 * "rig removed laptop at 10:32" is how a forged removal shows. */
export interface FleetRemoved {
  key: string;
  host?: string;
  /** When (epoch seconds); older payloads call it `at`. */
  removed_at?: number;
  at?: number;
  /** The device key that removed it ("" / missing on old tombstones). */
  removed_by?: string;
  removed_by_host?: string;
  /** The device left on its own (it wrote its own tombstone): not a removal
   * to suspect, and not sticky — a later re-admit elsewhere revives it. */
  left?: boolean;
}

/** A device removed on this one that another member has added back. */
export interface FleetReadmitted {
  key: string;
  host?: string;
  /** The device key that added it back. */
  by?: string;
  /** Its display name ("" when unknown). */
  by_host?: string;
}

/** A live "add a device" code (only shown on this device). */
export interface FleetInvite {
  code: string;
  expires_at: number;
  /** `mindflock devices join <this device> <code>` — runs on the new one. */
  command: string;
  /** "local_only": made, but no other computer can reach this one to use it. */
  warning?: string;
}

/** Another device asking to join; both screens show `code`. */
export interface FleetRequest {
  id: string;
  device: string;
  host: string;
  code: string;
  created_at: number;
  expires_at: number;
  /** The member it waits on, "" for this device. Approving one with `via`
   * sends the answer there (POST …/approve with {via, code}). */
  via?: string;
  via_host?: string;
}

/** This device's own outgoing join attempt (one at a time). */
export interface FleetJoin {
  state: "idle" | "waiting" | "joining" | "joined" | "denied" | "expired" | "error";
  device: string;
  host: string;
  code: string;
  error: string;
  id: string;
  /** Set once joined: false = this device is local-only, so the others can't
   * reach it (Make reachable). */
  self_reachable?: boolean;
}

/** A MindFlock on the tailnet that is not one of your devices (yet). */
export interface FleetCandidate {
  device: string;
  host: string;
  version: string;
  /** 0 = a MindFlock too old to join. */
  fleet_proto: number;
  reachable: boolean;
  member: boolean;
  /** It belongs to some group of devices already. */
  in_fleet: boolean;
  same_fleet: boolean;
  /** This device holds its pasted access token: one-click add works. */
  has_token: boolean;
  /** Answered once but not now: why ("connection refused on :8765 — …"). */
  reason?: string;
}

/** A tailnet device that isn't (reachably) a MindFlock, and why. */
export interface FleetPeer {
  device: string;
  host: string;
  os?: string;
  online: boolean;
  tagged: boolean;
  /** ok | refused | timeout | tls | not_mindflock | http_<n> | asleep | unreachable */
  outcome: string;
  /** Tailscale's last contact (epoch s, 0 unknown). */
  last_seen: number;
  reason: string;
}

/** A device this one let in lately, and whether it can reach it. */
export interface FleetAdmit {
  device: string;
  host: string;
  at: number;
  state: "checking" | "reachable" | "unreachable_joiner";
  reason: string;
}

/** Who answers the shared phone link, per device. */
export interface FleetPhoneLink {
  name: string;
  hosts: { key: string; host: string; self: boolean; state: "hosting" | "waiting" | "off" | "unknown" }[];
}

export interface FleetStatus {
  in_fleet: boolean;
  id: string;
  epoch: number;
  self: { key: string; host: string; ip?: string; port?: number };
  /** The other devices can reach this one (false = bound to 127.0.0.1). */
  self_reachable?: boolean;
  listening?: "local" | "tailnet" | "";
  gate_on?: boolean;
  admitted?: FleetAdmit[];
  tailnet_peers?: FleetPeer[];
  /** The tailnet policy lines that open this port between your devices. */
  policy_grant?: string;
  /** What "Match my other devices" would change here (null: nothing). */
  match?: { reachable: boolean; shared_link: string } | null;
  phone_link?: FleetPhoneLink | null;
  members: FleetMember[];
  invites: FleetInvite[];
  requests: FleetRequest[];
  join: FleetJoin;
  /** Every reachable member refuses this device's key: it was removed (or
   * the key changed) while it was offline. */
  stale_key: boolean;
  /** The access gate is off on a tailnet-reachable device. */
  gate_warning: boolean;
  candidates: FleetCandidate[];
  /** Devices taken out of the group (tombstones), with who removed them. */
  removed?: FleetRemoved[];
  /** Removed here but live on another member's roster: "Allow it here"
   * (POST /api/fleet/members/{key}/allow) lets it back in on this device. */
  readmitted_elsewhere?: FleetReadmitted[];
  /** "Update all my devices": the last (or running) rollout. */
  update?: FleetRollout;
}

/** GET /api/settings/sync (backend.web.core.settings_sync.status). */
export interface SyncStatus {
  enabled: boolean;
  device: string;
  joined_from: string;
  in_fleet: boolean;
  devices: {
    key: string;
    label: string;
    syncing: boolean;
    last_sync: number | null;
    error: string;
  }[];
  /** Base paths ("group.field" or "store:<name>") kept different here, and
   * single entries of a keyed list ("ticketing.sources#<id>"). */
  pinned: string[];
  /** Unit pins kept separate at join → the device whose different entry
   * shares the id (its label). Missing from an older server. */
  separate?: Record<string, string>;
  /** Incoming values not applied here yet (an agent CLI not installed). */
  deferred: { path: string; value?: unknown; reason: string }[];
  warnings: string[];
  /** settings.json exists but doesn't parse: sync is paused until it's fixed
   * or deleted ("" when fine). */
  error?: string;
  /** Set when one scan would have cleared most of this device's settings at
   * once (a reset settings.json): sync stopped instead of spreading that.
   * The sentence to show; `choices` are POST /api/settings/sync/resume's
   * `keep` values. */
  paused?: string;
  choices?: string[];
  /** Everything that can be pinned, with a human label. */
  syncable: { path: string; label: string; group: string }[];
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
  /** "choice" only. An option's `hint` explains that option; the form shows
   * the selected option's hint in place of the field's. */
  options?: { value: string; label: string; hint?: string }[];
  hint?: string;
  /** The server refuses to save the source without it (PUT → 400 per field)
   * — unless `auto` is also set, in which case Test fills it in and it is
   * required before the source counts as connected, not before it saves. */
  required?: boolean;
  /** Filled in by Test (member id, the Asana workspace), not typed. */
  auto?: boolean;
  /** The shape the server checks a non-blank value against ("url", "email",
   * "key", "gid"); the message comes back per field on a 400. */
  validate?: string;
  /** An optional extra a first setup never needs (claim label, move on
   * start, …): the source form folds it under "Advanced options". Never set
   * on a required field. */
  advanced?: boolean;
}

/** One workflow state (or Asana section) a source's Test listed. */
export interface TicketingWorkflowState {
  id: string | number;
  name?: string;
  type?: string;
  group?: string;
  label?: string;
}

/** The last connection test recorded on a source (`last_test`): written only
 * by `POST /api/settings/test/ticketing` with `record: true`, carried over by
 * the sources PUT while the tested fields are unchanged and dropped otherwise.
 * `connected` on a source means this passed within the last day. */
export interface TicketingLastTest {
  ok: boolean;
  /** ISO-8601, UTC. */
  at: string;
  message: string;
  member_id?: string;
  states?: TicketingWorkflowState[];
  /** Asana only: the workspaces the token sees when none is picked yet. */
  workspaces?: { gid: string; name: string }[];
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
 * contributes its own fields (token, workspace, project key, …). The one
 * non-string passenger is `last_test` (a `TicketingLastTest`), read through
 * `components/intake/sourceForm.ts::lastTestOf` rather than typed here, so the
 * form's string fields stay string-typed. */
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
  /** A check of the BRANCH, made before the work merged ("Check now" before
   * merge). Absent on an older store: an ordinary run. */
  prelive?: boolean;
}

export interface TestPlan {
  id: string;
  title: string;
  /** "manual": created empty on purpose (`POST /api/test-plans/branch` with
   * `empty: true`) — a person writes its steps, no model call was made. ""
   * for a generated checklist. Optional: an older server doesn't send it. */
  origin?: "" | "manual";
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

// --- Team runs and GET /api/outbox (SPEC §5) ---------------------------------
//
// A "run" is the server's object for sessions started together; on screen it is
// only ever a group header (plus the bell's waiting rows and the header's Copy
// summary), or for a split / one-for-all group its lead's Thread tab. Every field the UI reads is
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
  /** The lead works directly in its folder (an in-place session). */
  in_place?: boolean;
  /** The lead sits on its base/trunk branch (or a detached HEAD). */
  trunk?: boolean;
}

/** Where a split's pieces run (`""` until the plan is approved). */
export type SplitMode = "" | "worktrees" | "same_folder";

/** The session a split was started from when MindFlock had to start a lead
 * of its own for the pieces (it worked in its folder, or sat on its trunk):
 * never merged into, switched or pushed. */
export interface RunOrigin {
  title: string;
  branch?: string;
  head?: string;
  in_place?: boolean;
  trunk?: boolean;
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
  /** Memory is low: queued lines wait for headroom before they start. */
  waiting_for_memory?: boolean;
  /** Phase 3: one-for-all and split groups. */
  split?: boolean;
  /** An auto-split: its lead may decide not to split (the group then
   * dissolves into its one session). */
  optional?: boolean;
  /** The most pieces its lead may propose (0 = the server's limit). */
  max_pieces?: number;
  /** A split's one line (what the lead was asked to split). */
  goal?: string;
  lead?: RunLead | null;
  plan?: RunPlan | null;
  check?: RunCheck | null;
  release?: RunRelease | null;
  /** Where the pieces run, chosen on the plan card. */
  mode?: SplitMode;
  origin?: RunOrigin | null;
  /** Same folder: changes (and commits) in the lead's folder no piece owns. */
  stray?: { paths: string[]; commits: string[] } | null;
  /** Written when the group finishes: what it did, as Markdown. The run keeps
   * it; `GET /api/outbox` carries it only for SUMMARY_DAYS (7). */
  summary?: { text_md?: string } | null;
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

/* --- Agent providers (GET /api/providers/status) ------------------------- */

/** One provider's connection status — a row of Settings → Agent providers. */
export interface ProviderStatus {
  name: string;
  aliases?: string[];
  binary?: string;
  installed: boolean;
  /** The resolved executable when installed. */
  path?: string;
  authenticated: boolean;
  auth_known: boolean;
  auth_detail?: string;
  /** Whether a miss means anything: the provider named credential files or
   * env vars to look at (false = it keeps its token where we never look). */
  auth_probe?: boolean;
  login_command?: string;
  /** A login step the provider DECLARED; false = "no sign-in needed". */
  has_login?: boolean;
  install_hint?: string;
  /** Why a custom provider's CLI can't be resolved (shell alias, bad path). */
  launch_hint?: string;
  is_default?: boolean;
  /** This device's saved binary path (coding_cli.binary_paths[name]), "" when none. */
  binary_override?: string;
  /** The env var (MINDFLOCK_PROVIDER_BIN_<NAME>) pinning the binary, "" when unset. */
  binary_override_env?: string;
}

/** A custom provider TOML the server could not load. */
export interface ProviderLoadError {
  file: string;
  error: string;
}

export interface ProvidersStatusResponse {
  providers?: ProviderStatus[];
  default?: string;
  errors?: ProviderLoadError[];
}

/** GET /api/repos/branches — the New dialog's Base branch menu for the
 * folder in its Folder field: local branches (the checked-out one first),
 * then remote-tracking refs, capped at 200. `default` is what the server
 * cuts from when nothing is chosen (origin's HEAD, else main/master, else
 * the checked-out branch). Not a repo: `is_git: false`, empty list. */
export interface RepoBranches {
  path: string;
  is_git: boolean;
  current: string;
  default: string;
  branches: Array<{ name: string; remote: boolean; current: boolean }>;
  truncated?: boolean;
}

/** Recently closed entry fields for a session that left WITHOUT the user
 * closing it (GET /api/recent and /api/recently-closed). The engine drops a
 * session at load when its folder and its tmux are both gone and records it
 * with `reason: "workspace missing"`, `wiped` (nothing to reopen) and
 * `branch_exists` (whether its branch is still in the repo, so the work can be
 * recovered by hand). All absent on an ordinary close. */
export interface ClosedReason {
  reason?: string;
  branch_exists?: boolean | null;
  wiped?: boolean;
}

/** One person with a terminal open on a session (`Instance.viewers`, keyed by
 * person id). `device` is the one they used last, `devices` all of them;
 * `typing` = input within the last few seconds. `name` is "" for another
 * person (names don't travel between servers). */
export interface Viewer {
  name: string;
  device: string;
  devices?: string[];
  since: number;
  typing: boolean;
}

/** The person all of one person's devices share — GET/POST /api/person
 * (backend/web/core/person.py, docs/people.md). `id` is agreed between the
 * devices (pairing, then settings sync) and never edited; the rest syncs like
 * any shared setting. An identifier, never a credential. */
export interface Person {
  id: string;
  name: string;
  /** Who MindFlock's OWN commits (fast-track, team-run merges, peer exports)
   * are authored as. Both blank = the repo's git config decides. */
  git_name: string;
  git_email: string;
}

/** POST /api/person: `""` clears a field, an absent one is left alone. */
export type PersonPatch = Partial<Pick<Person, "name" | "git_name" | "git_email">>;

/** GET /api/connections — Settings → Connections, one row per outside thing
 * MindFlock talks to (backend/web/addons/connections.py `_row`). */
export type ConnectionStatus = "connected" | "needs_attention" | "off";

export interface ConnectionAction {
  label: string;
  /** dialog: a named in-app dialog (connect-github, agent-sign-in, intake,
   * db-explorer); screen: a Settings screen key; route: an in-app URL;
   * copy: a command to copy. */
  kind: "dialog" | "screen" | "route" | "copy";
  target: string;
  /** agent-sign-in: which CLI, and the auth profile to sign in (optional). */
  provider?: string;
  profile?: string;
  /** intake: the tab, and the ticket source the row is about. */
  tab?: string;
  source_id?: string;
}

export interface ConnectionTestSpec {
  endpoint: string;
  method: string;
  body?: Record<string, unknown>;
}

export interface ConnectionRow {
  id: string;
  name: string;
  kind: string;
  status: ConnectionStatus;
  detail: string;
  action: ConnectionAction | null;
  test: ConnectionTestSpec | null;
  fix_command?: string;
  fix?: string;
  docs?: string;
  updated_at?: string;
  /** The default agent provider's row. */
  default?: boolean;
  /** Old row fields, kept by the server for one release. */
  legacy_status?: "connected" | "attention" | "not_connected";
  settings_screen?: string;
  configure_dialog?: string;
  purpose?: string;
}

export interface ConnectionsSummary {
  connected: number;
  needs_attention: number;
  off: number;
  total: number;
}

export interface ConnectionsResponse {
  connections: ConnectionRow[];
  summary?: Partial<ConnectionsSummary>;
}

/** A user extension that failed to load at startup (GET /api/addons `failed`). */
export interface ExtensionLoadFailure {
  id: string;
  file: string;
  error: string;
  at?: number;
}

/** One step of the one-shot "install everything missing" plan
 * (`backend.doctor.install_plan`). */
export interface InstallStep {
  id: string;
  label: string;
  cmd: string;
}

export interface DoctorCheckItem {
  id?: string;
  label?: string;
  status?: "ok" | "info" | "warn" | "fail" | string;
  detail?: string;
  fix?: string;
  /** The runnable fix (an agent-auth row's declared login command). */
  cmd?: string;
  /** The agent provider an agent row is about (drives "Sign in to …"). */
  provider?: string;
}

/** A state.json written by a NEWER MindFlock was moved aside (StateNotice). */
export interface StateNotice {
  file_version?: number;
  supported_version?: number;
  backup_path?: string;
}

/** GET /api/doctor (state/queries.ts `useDoctor`). */
export interface DoctorPayload {
  ok: boolean;
  checks?: DoctorCheckItem[];
  /** Everything missing that this machine needs, as one install (see
   * `backend.doctor.install_plan`). */
  install?: { steps?: InstallStep[]; packages?: string[] };
  /** The engine's installed version ("0+unknown" from a bare source tree). */
  version?: string;
  state_notice?: StateNotice | null;
}

/** GET /api/mindflock/status — the ticket-ingestion pipeline and the PR /
 * issue automation it hosts (state/queries.ts `useMindflockStatus`). */
export interface MindflockStatus {
  available: boolean;
  running: boolean;
  /** The last state the ticket toggle was set to (desired on + not running =
   * starting up, or died). */
  desired?: boolean;
  /** A live connectivity problem (only while running). */
  net_error?: boolean;
  /** github.enabled AND repos configured, folded server-side. */
  pr_enabled?: boolean;
  /** github.issues_enabled AND repos configured. */
  issues_enabled?: boolean;
  /** False when PR review and issue handling run on another of your devices
   * (Settings → Your devices); `automation_device` names it ("" when unknown).
   * Missing from an older server: read as "here". */
  automation_here?: boolean;
  automation_device?: string;
  /** True while a ticket / a PR batch / an issue is actually being brought in. */
  tickets_active?: boolean;
  pr_active?: boolean;
  issues_active?: boolean;
  /** Set while the next ticket waits on engine.max_sessions. */
  held_for_slot?: { live: number; max: number } | null;
}

/** `GET /api/system/memory` — the machine's memory headroom
 * (backend/memory_pressure.py). While `level` is low or critical, agent-
 * spawned sessions are refused, team runs start no new lines and the revive
 * sweep waits. `unknown`: nothing to read (non-Linux) — never gates. */
export interface MemoryReading {
  level: "ok" | "low" | "critical" | "unknown";
  available?: number;
  total?: number;
  available_pct?: number;
  swap_total?: number;
  swap_free?: number;
  psi?: { some_avg10?: number; some_avg60?: number; full_avg10?: number; full_avg60?: number } | null;
  at?: number;
  /** One-line reason ("memory is low: 2.1 GB free of 24.0 GB (9%)"); "" when ok. */
  message?: string;
}

/** One holder or waiter of a heavy-job slot (`mindflock slot`). */
export interface SlotJob {
  slot?: number;
  pid?: number | null;
  cmd?: string;
  cwd?: string;
  since?: number;
  max?: number;
  /** The MindFlock session it runs in; "" when none. */
  session?: string;
  /** Waiters only: 1 = next in line. */
  position?: number;
}

/** `GET /api/system/slots` — heavy commands gated by `mindflock slot`
 * (backend/slot.py): who holds a slot, who is queued for one. */
export interface SlotsReading {
  slots: number;
  source: "env" | "setting" | "default";
  pools: { name: string; holders: SlotJob[]; waiters: SlotJob[] }[];
  running: number;
  waiting: number;
}

/** POST /api/voice/help: a spoken question about MindFlock answered from its
 * docs — `say` names the section it came from; `model`: worded by the model
 * (else the section's own first sentences). */
export interface VoiceHelp {
  say: string;
  source: { doc: string; heading: string } | null;
  model: boolean;
}
