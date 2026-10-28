/** Settings → Notifications' groups and the words a rule row shows, kept
 * apart from the screen so tests can read them without a React tree.
 *
 * The server's rules (backend/web/addons/notify.py) carry a plain `label` and
 * a one-line `description` for this screen. Their `title`/`body` are the
 * NOTIFICATION's text — templates with `{session}`/`{detail}` filled in when
 * one fires — and read as a broken product as a description ("A team run
 * needs you — {detail}"), so a row never shows them unless they are a
 * finished sentence (a rule from before descriptions existed).
 *
 * Rule ids are the stable keys (POST /api/notify/rules/<id>, muted_rules /
 * enabled_rules in settings.json). */

export interface RuleCopy {
  label: string;
  desc: string;
}

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
  /** The server's one plain line for this screen. */
  description?: string;
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

/** The words a rule row shows: the server's label and description; failing
 * those, its title / body only when they are finished sentences (an id is
 * never shown as words). */
export function ruleCopy(rule: RuleLike): RuleCopy {
  const label = [rule.label, rule.title].find((t) => t && !hasPlaceholder(t)) || "Another event";
  const desc = [rule.description, rule.body].find((t) => t && !hasPlaceholder(t)) || "";
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

/* --------------------------------------------------------------------------
 * The ask pop-up (notifications.ask_popup, saved through the ntfy view like
 * the fields there): a session's permission or choice prompt opens a card in
 * the middle of the screen (components/dialogs/AskDialog); a question at the
 * end of its reply opens it only from its Reply.
 * ------------------------------------------------------------------------ */

export const ASK_POPUP_LABEL = "Pop up a session's permission and choice prompts in the middle of the screen";

export const ASK_POPUP_HINT =
  "When an agent asks for permission or shows a choice — Claude's own questions included — answer it right there, by click, keys or voice. A question asked in plain words at the end of a reply doesn't pop up: the bell, the rail and the notification show it, and its Reply opens it here. Later puts it off; the bell keeps it.";

/** Under the "needs input" rule: what it covers now. */
export const NEEDS_INPUT_EXTRA = "Also covers a question the agent asks in plain words at the end of its reply, when that switch is on.";

/* The second switch (notifications.prose_questions, off by default): whether
 * a reply that ends with a question in plain words counts as the session
 * asking at all. Off, only a real prompt — a permission, a plan approval,
 * Claude's question menu — raises a Question. */
export const PROSE_QUESTIONS_LABEL = "Count a question asked in plain words at the end of a reply";

export const PROSE_QUESTIONS_HINT =
  "Off: only a real prompt counts — a permission, a plan approval, Claude's question menu. On: a reply that ends with a question also puts a Question on the session's row, in the bell and in the notification, answered with Reply.";

/** The plain one-liner under "Where alerts go" when nothing but the bell is
 * on — the reason the switches below seem to do nothing. */
export const ONLY_BELL_HINT =
  "Right now alerts only show in the bell. Turn on desktop or phone alerts to hear about them when you're away.";

/* --------------------------------------------------------------------------
 * Pocket mode: the agent's words in phone pushes (notifications.ntfy_agent_words,
 * saved through the ntfy view as `agent_words`). While the phone is locked
 * nothing on the web can speak, so the push body itself carries the spoken
 * line of a finished turn and the question of a dialog — what a read-aloud
 * app (iOS Announce Notifications) says through the earbuds.
 * ------------------------------------------------------------------------ */

/** The switch's label. */
export const AGENT_WORDS_LABEL = "Include the agent's words in pushes";

/** What the words are and where they go. */
export const AGENT_WORDS_HINT =
  "The spoken summary of a finished turn and the question of a dialog travel through your ntfy server.";

/** The consequence on the public default server — shown only there: on a
 * self-hosted ntfy the exposure is the user's own server. */
export const AGENT_WORDS_PUBLIC_HINT = "Yours is the public ntfy.sh, so a public server stores them.";

/** The hint under the agent-words switch for the configured server. */
export function agentWordsHint(publicServer: boolean): string {
  return publicServer ? AGENT_WORDS_HINT + " " + AGENT_WORDS_PUBLIC_HINT : AGENT_WORDS_HINT;
}

/** The one plain line that makes pocket mode work on an iPhone. */
export const ANNOUNCE_TIP =
  "iPhone: turn on ntfy under Settings → Notifications → Announce Notifications to hear pushes through AirPods.";
