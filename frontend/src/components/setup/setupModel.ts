/** The first-run checklist's small decisions, pure so vitest covers them.
 *
 * The server owns the steps and their state (`GET /api/setup`, built by
 * backend/web/core/setup_steps.py — the same definition `mindflock init`
 * prints). This module only decides what to draw from it: whether the empty
 * grid shows the card, whether the rail chip shows, what the chip says, which
 * group a step sits in, and how a step's row reads.
 *
 * Three groups, drawn in this order:
 *   Get started        tools · agent_login · repo   (required)
 *   Connect your work  github · tracker              (optional, counted)
 *   Optional, later    phone                         (folded, not counted) */

import type {
  DoctorCheckItem,
  SetupGroup,
  SetupStatus,
  SetupStep,
  SetupStepId,
} from "../../api/types";

/** The order every surface uses — the server's, restated so a stale or
 * partial payload still draws in the right order. */
export const STEP_ORDER: SetupStepId[] = ["tools", "agent_login", "repo", "github", "tracker", "phone"];

/** The server's names, kept here only as the fallback for a payload that has
 * not arrived yet (the card still draws its rows). */
export const STEP_TITLES: Record<SetupStepId, string> = {
  // The server names the default agent ("Install Claude Code"); this is the
  // fallback before its payload lands, for the default default.
  tools: "Install Claude Code",
  agent_login: "Sign in to your agent",
  repo: "Start your first agent",
  github: "Connect GitHub",
  tracker: "Connect your tracker",
  phone: "Use it from your phone",
};

export const REQUIRED_STEPS: SetupStepId[] = ["tools", "agent_login", "repo"];

/** The server's groups (`group` on each step), restated for an older or
 * partial payload. Required is exactly the "start" group. */
export const STEP_GROUP: Record<SetupStepId, SetupGroup> = {
  tools: "start",
  agent_login: "start",
  repo: "start",
  github: "connect",
  tracker: "connect",
  phone: "later",
};

/** A step's group: the payload's, else the known one, else "later" (an
 * unknown step never lands in Get started). */
export function stepGroup(step: Pick<SetupStep, "id" | "group">): SetupGroup {
  const g = step.group;
  if (g === "start" || g === "connect" || g === "later") return g;
  return STEP_GROUP[step.id as SetupStepId] || "later";
}

export const STATUS_GLYPH: Record<string, string> = { done: "✓", skipped: "–", todo: "•" };

/** The steps in order, filled in from the payload; a missing one draws as
 * to-do under its known title. */
export function orderedSteps(status: SetupStatus | null | undefined): SetupStep[] {
  const by = new Map((status?.steps || []).map((s) => [s.id, s]));
  return STEP_ORDER.map(
    (id) =>
      by.get(id) || {
        id,
        title: STEP_TITLES[id],
        required: REQUIRED_STEPS.includes(id),
        group: STEP_GROUP[id],
        done: false,
        at: null,
        skipped: false,
        status: "todo",
      }
  );
}

/** Whether the empty grid shows the checklist card: a required step is still
 * to do. Until the payload lands the server's `onboarded` flag (true once a
 * session has ever existed) stands in, so a veteran's empty grid never flashes
 * the card while /api/setup is in flight. */
export function showSetupCard(opts: {
  sessions: number;
  status: SetupStatus | null | undefined;
  onboarded: boolean | undefined;
}): boolean {
  if (opts.sessions !== 0) return false;
  if (opts.status) return !opts.status.summary.required_done;
  return opts.onboarded === false;
}

/** Whether the compact "Setup 3/5" chip shows: something is still to do, and
 * the grid card is not already showing the same list. */
export function showSetupChip(opts: {
  sessions: number;
  status: SetupStatus | null | undefined;
  onboarded: boolean | undefined;
}): boolean {
  if (!opts.status) return false;
  if (opts.status.summary.complete) return false;
  return !showSetupCard(opts);
}

/** THE setup count — the chip, the checklist's first line and the grid card
 * all say it, so they can never disagree ("1 of 3" on the panel beside a
 * "Setup 1/5" chip was three readings of one list). It counts the three
 * REQUIRED steps (what "Get set up" promises); once those are done the
 * optional ones still to settle ride along as `optional`, never as a bigger
 * denominator. */
export function setupCount(status: SetupStatus | null | undefined): {
  done: number;
  total: number;
  optional: number;
} {
  const steps = orderedSteps(status);
  const req = steps.filter((x) => REQUIRED_STEPS.includes(x.id as SetupStepId));
  const done = req.filter((x) => x.done).length;
  const optional = steps.filter(
    (x) => stepGroup(x) === "connect" && !x.done && !x.skipped
  ).length;
  return { done, total: req.length, optional: done === req.length ? optional : 0 };
}

/** "Setup 1 of 3" — then "Setup 3 of 3 · 2 optional" once the required steps
 * are done and optional ones remain. */
export function chipLabel(status: SetupStatus | null | undefined): string {
  if (!status?.summary) return "Setup";
  const c = setupCount(status);
  return "Setup " + c.done + " of " + c.total + (c.optional ? " · " + c.optional + " optional" : "");
}

/** ①…⑤ for a counted step (Get started, then Connect your work); an
 * "Optional, later" extra has no number — it is not a step of setup. */
export function stepNumber(id: SetupStepId | string): string {
  const counted = STEP_ORDER.filter((s) => STEP_GROUP[s] !== "later");
  const i = counted.indexOf(id as SetupStepId);
  return i < 0 ? "" : "①②③④⑤⑥⑦⑧⑨".charAt(i);
}

/** The step to act on now: the server's `summary.next` (first to-do counted
 * step), or "tools" before the payload lands. */
export function nextStepId(status: SetupStatus | null | undefined): string {
  if (!status) return "tools";
  return status.summary.next || "";
}

/** Doctor rows that are optional extras — phone/tailnet access, peer links,
 * a local model, warm caches, the clipboard, the gh CLI (a token does its
 * job). Settings → Doctor lists them; getting started never does. */
export const EXTRA_CHECKS = new Set([
  "tailscale",
  "bwrap",
  "cloudflared",
  "local-model",
  "cache-seeds",
  "tailnet-mtu",
  "clipboard",
  "gh",
]);

/** A doctor row as served: `required` (whether it can block getting
 * started — backend/web/core/setup_steps.tag_required) and `install` (the
 * one-click install covers it) ride along with the shared type's fields. */
export type SetupCheck = DoctorCheckItem & { required?: boolean; install?: boolean };

/** Whether a doctor row can block steps ①–③: the server's `required` when it
 * says, else "not an optional extra" (an older server). The setup checklist's
 * "All checks" lists only these; phone, Tailscale, MTU and the like belong to
 * Settings → Your phone and Settings → Doctor. */
export function isRequiredCheck(c: SetupCheck): boolean {
  if (typeof c.required === "boolean") return c.required;
  return !EXTRA_CHECKS.has(String(c.id || ""));
}

/** The rows the checklist's "All checks (N)" fold lists. */
export function requiredChecks<T extends SetupCheck>(checks: readonly T[] | undefined): T[] {
  return (checks || []).filter(isRequiredCheck);
}

/** Whether the setup checklist's "Install the tools" step shows a doctor
 * row: only a problem (fail / warn) with something getting started needs.
 * Agent sign-in rows belong to the next step, extras to Settings → Doctor. */
export function isEssentialCheck(c: SetupCheck): boolean {
  const id = String(c.id || "");
  if (!isRequiredCheck(c)) return false;
  if (id.startsWith("agent-auth")) return false;
  return c.status === "fail" || c.status === "warn";
}

/** The problems step ① shows OUTSIDE "All checks": essential ones the
 * install button does not already fix. A row the button installs (`install`)
 * would only repeat the button ("Install Claude Code" next to "✗ agent CLI
 * (claude) not found"), so it shows inside All checks only. */
export function stepProblems<T extends SetupCheck>(checks: readonly T[] | undefined): T[] {
  return (checks || []).filter((c) => isEssentialCheck(c) && !c.install);
}

/** Whether the phone extra should offer to install Tailscale rather than the QR: the
 * doctor's Tailscale row says it is missing (or broken). A payload without
 * the row (doctor unavailable) still offers the QR — Settings → Your phone is
 * the authority on whether it can be served. */
export function tailscaleMissing(status: SetupStatus | null | undefined): boolean {
  const ts = status?.tailscale;
  if (!ts) return false;
  return ts.status !== "ok";
}

/** Whether Tailscale is actually absent (the doctor's row says "not found"),
 * as opposed to installed but stopped, signed out or unreachable — which
 * `tailscaleMissing` also covers, and whose fix starts it rather than
 * installs it. */
export function tailscaleNotInstalled(status: SetupStatus | null | undefined): boolean {
  const ts = status?.tailscale;
  return !!ts && ts.status !== "ok" && /^not found/i.test(ts.detail || "");
}

/** Did the desktop app (or a link) ask for the Setup modal? `?setup=1` is
 * what the desktop sends after a first-launch engine install; `install` is
 * its older spelling. Strips the parameter so a reload doesn't ask again. */
export function consumeSetupIntent(
  loc: { search: string; pathname: string; hash: string } = window.location,
  hist: { replaceState(data: unknown, unused: string, url?: string): void } = window.history
): boolean {
  const params = new URLSearchParams(loc.search);
  const v = params.get("setup");
  if (v !== "1" && v !== "install") return false;
  params.delete("setup");
  const q = params.toString();
  try {
    hist.replaceState(null, "", loc.pathname + (q ? "?" + q : "") + loc.hash);
  } catch {
    /* a URL we can't rewrite still opened the dialog */
  }
  return true;
}
