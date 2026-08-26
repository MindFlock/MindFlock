/** The thinking-effort ladder, client side — pure, so it is unit-testable.
 *
 * Mirrors backend/providers/effort.py, including the clamping rule: the rungs
 * are NEUTRAL, and the CLI that ends up running the item translates them into
 * its own spelling (`claude --effort xhigh`, `codex -c
 * model_reasoning_effort=high`, `agy --effort high`) — clamping anything above
 * its own ceiling instead of forwarding a value it would reject.
 *
 * The server is the authority on that translation; what lives here is only what
 * the picker has to SAY about it, so a row can tell you where the CLI it is
 * about to run tops out before you press the button rather than after.
 */

/** Rungs a caller may ask for, cheapest first. */
export const EFFORTS = ["low", "medium", "high", "xhigh", "max", "ultra"] as const;

/** How the ladder reads in a dropdown. Short on purpose: this picker shares one
 * line with the CLI and depth pickers inside a work row. */
export const EFFORT_LABELS: Record<string, string> = {
  low: "Low",
  medium: "Medium",
  high: "High",
  xhigh: "Extra high",
  max: "Max",
  ultra: "Ultra",
};

/** What a CLI can do with the ladder, as /api/providers reports it: the neutral
 * rungs it can actually distinguish, plus how its top rung is spelled — either a
 * level name of its own (`ultra_level`, Claude Code's `--effort ultracode`) or a
 * keyword added to the prompt (`keyword`). At most one of the two is set. */
export interface EffortCap {
  levels: string[];
  ultra_level?: string;
  keyword: string;
}

export function normalizeEffort(value: string | null | undefined): string {
  const e = String(value || "")
    .trim()
    .toLowerCase();
  return (EFFORTS as readonly string[]).includes(e) ? e : "";
}

/** Whether this CLI can be asked for an effort at all. An unknown provider
 * (a custom program, or caps not fetched yet) reads as "maybe" — never as "no",
 * which would hide a control that does work. */
export function supportsEffort(cap: EffortCap | undefined): boolean {
  return !cap || cap.levels.length > 0;
}

/** The rung this CLI would actually run for `want` — its highest rung at or
 * below the request. `""` when it has no effort control. */
export function appliedEffort(want: string, cap: EffortCap | undefined): string {
  const level = normalizeEffort(want);
  if (!level || !cap || cap.levels.length === 0) return "";
  if (cap.levels.includes(level)) return level;
  const below = EFFORTS.filter(
    (r) => EFFORTS.indexOf(r) <= EFFORTS.indexOf(level as never) && cap.levels.includes(r)
  );
  // Nothing at or below the request: give its floor rather than nothing, which
  // is what the server does too.
  return below.length ? below[below.length - 1] : cap.levels[0];
}

/** How one rung reads in the dropdown for THIS CLI: its own name for the rung
 * when it has one (claude calls Ultra `ultracode`), or the rung it would clamp
 * to when the request is above its ceiling. */
export function effortOptionLabel(rung: string, cap: EffortCap | undefined): string {
  const label = EFFORT_LABELS[rung] || rung;
  if (rung === "ultra" && cap?.ultra_level)
    return label + " (" + cap.ultra_level + ")";
  const got = appliedEffort(rung, cap);
  return got && got !== rung ? label + " (→ " + EFFORT_LABELS[got] + ")" : label;
}

/** The picker's tooltip: what the control is, then what THIS CLI does with it.
 *
 * Names the CLI and its ceiling, because the same pick means different things on
 * different queues — and says plainly when the CLI ignores it, rather than
 * leaving an enabled control that does nothing. */
export function effortTitle(provider: string, cap: EffortCap | undefined): string {
  const head =
    "How hard to think about this one — just this start, not the whole queue.";
  const cli = provider || "the configured CLI";
  if (!cap) return head;
  if (cap.levels.length === 0)
    return head + "\n" + cli + " has no effort setting, so this start ignores it.";
  const top = cap.levels[cap.levels.length - 1];
  let ceiling: string;
  if (top !== "ultra")
    ceiling =
      cli + " tops out at " + EFFORT_LABELS[top] + "; anything above that runs there.";
  else if (cap.ultra_level)
    ceiling =
      cli + " goes all the way to Ultra, which it calls `" + cap.ultra_level +
      "` and runs for the whole session.";
  else
    ceiling =
      cli + " goes all the way to Ultra, which also puts `" + cap.keyword + "` in the prompt.";
  return head + "\n" + ceiling;
}
