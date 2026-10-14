/** Settings → Notifications' words and groups, kept apart from the screen so
 * tests can read them without a React tree.
 *
 * The server's rules (backend/web/addons/notify.py) carry a `title`/`body`
 * that are the NOTIFICATION's text — templates with `{session}`/`{detail}`
 * filled in when one fires. Shown as a settings description they read as a
 * broken product ("A team run needs you — {detail}"), so the screen never
 * shows them: each known rule id has a plain label + one-line description
 * here, and an unknown rule (a newer server) falls back to its own label
 * with any templated body dropped.
 *
 * Rule ids are the stable keys (POST /api/notify/rules/<id>, muted_rules /
 * enabled_rules in settings.json); only the words live here. */

export interface RuleCopy {
  label: string;
  desc: string;
}

export const RULE_COPY: Record<string, RuleCopy> = {
  // Needs you
  needs_input: { label: "A session asks you a question", desc: "The agent is waiting for your answer." },
  session_idle: {
    label: "A session finishes its work",
    desc: "The agent stopped and nothing else is queued. Handy when you leave sessions running.",
  },
  session_offline: {
    label: "A session stops unexpectedly",
    desc: "The agent closed. Restart it from the sidebar.",
  },
  device_join: {
    label: "A computer asks to join your devices",
    desc: "Approve it on this computer within 10 minutes.",
  },
  // Pull requests
  pr_approved: { label: "A pull request is approved", desc: "A reviewer approved it. It's ready to merge." },
  pr_closed: { label: "A pull request is merged or closed", desc: "The pull request is no longer open." },
  pr_changes_requested: {
    label: "A reviewer asks for changes",
    desc: "Someone requested changes on a session's pull request.",
  },
  // Usage & cost
  usage_limit: {
    label: "A session runs out of usage",
    desc: "It waits until your plan's usage resets.",
  },
  usage_restored: {
    label: "Usage comes back",
    desc: "A session that ran out of usage can run again.",
  },
  budget_exceeded: {
    label: "A session goes over its cost budget",
    desc: "Set the budget in Settings → General.",
  },
  // More (folded)
  run_needs_you: {
    label: "A team run needs you",
    desc: "A session in a team run is stuck or blocked.",
  },
  run_finished: { label: "A team run finishes", desc: "Every session in the team run is done." },
  run_task_shipped: {
    label: "A team run session ships",
    desc: "One session in a team run reached its goal, such as opening its pull request.",
  },
  precommit_running: {
    label: "Pre-commit hooks start",
    desc: "A commit is running your repo's pre-commit checks.",
  },
  precommit_failed: {
    label: "Pre-commit hooks block a commit",
    desc: "The commit didn't land. The session needs to commit again.",
  },
  verify_plan_ready: { label: "A checklist is ready", desc: "Verify finished writing a checklist for a push." },
  verify_plan_failed: { label: "A checklist couldn't be written", desc: "Open Verify to see why." },
  verify_run_finished: {
    label: "An agent finishes a checklist",
    desc: "Open Verify to see what it found.",
  },
  verify_run_gave_up: {
    label: "A checklist run stops without answers",
    desc: "The checklist goes back to unchecked.",
  },
  red_zone_blocked: {
    label: "An edit to an off-limits file is blocked",
    desc: "An agent tried to change a file in a zone you protected.",
  },
  red_zone_breached: {
    label: "An off-limits file was changed",
    desc: "Look at it before it's pushed.",
  },
  red_zone_tampered: {
    label: "Zone protection was tampered with",
    desc: "Something changed the files that enforce your zones.",
  },
};

/** The groups, in screen order. The first three are what a new user cares
 * about; "more" (team runs, pre-commit hooks, Verify, zones) is folded. */
export const RULE_GROUPS: ReadonlyArray<{ key: string; title: string; ids: readonly string[] }> = [
  { key: "needs", title: "Needs you", ids: ["needs_input", "session_idle", "session_offline", "device_join"] },
  { key: "prs", title: "Pull requests", ids: ["pr_approved", "pr_closed", "pr_changes_requested"] },
  { key: "usage", title: "Usage & cost", ids: ["usage_limit", "usage_restored", "budget_exceeded"] },
];

/** Where a rule id that is in no named group goes. */
export const MORE_GROUP = "more";

export interface RuleLike {
  id: string;
  label?: string;
  title?: string;
  event?: string;
  body?: string;
  enabled?: boolean;
}

/** Whether `text` still carries a `{placeholder}` — template text, never a
 * description. */
export function hasPlaceholder(text: string | undefined | null): boolean {
  return /\{[a-z_]+\}/i.test(text || "");
}

/** The words a rule row shows: our copy for a known id; else the server's
 * label (an id is never shown as words) and its body only when it is a
 * finished sentence. */
export function ruleCopy(rule: RuleLike): RuleCopy {
  const known = RULE_COPY[rule.id];
  if (known) return known;
  const label = [rule.label, rule.title].find((t) => t && !hasPlaceholder(t)) || "Another event";
  const desc = rule.body && !hasPlaceholder(rule.body) ? rule.body : "";
  return { label, desc };
}

/** The rule's group key ("needs" | "prs" | "usage" | "more"). */
export function ruleGroup(id: string): string {
  return RULE_GROUPS.find((g) => g.ids.includes(id))?.key || MORE_GROUP;
}

/** Rules sorted into the named groups (in the groups' own order) plus the
 * folded rest (in the server's order). Empty groups are dropped. */
export function groupRules<T extends RuleLike>(
  rules: readonly T[]
): { groups: Array<{ key: string; title: string; rules: T[] }>; more: T[] } {
  const byId = new Map(rules.map((r) => [r.id, r]));
  const groups = RULE_GROUPS.map((g) => ({
    key: g.key,
    title: g.title,
    rules: g.ids.map((id) => byId.get(id)).filter((r): r is T => !!r),
  })).filter((g) => g.rules.length);
  const more = rules.filter((r) => ruleGroup(r.id) === MORE_GROUP);
  return { groups, more };
}

/** The plain one-liner under "Where alerts go" when nothing but the bell is
 * on — the reason the switches below seem to do nothing. */
export const ONLY_BELL_HINT =
  "Right now alerts only show in the bell. Turn on desktop or phone alerts to hear about them when you're away.";
