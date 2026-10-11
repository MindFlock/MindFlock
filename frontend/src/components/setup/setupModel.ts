/** The first-run checklist's small decisions, pure so vitest covers them.
 *
 * The server owns the five steps and their state (`GET /api/setup`, built by
 * backend/web/core/setup_steps.py — the same definition `mindflock init`
 * prints). This module only decides what to draw from it: whether the empty
 * grid shows the card, whether the rail chip shows, what the chip says, and
 * how a step's row reads. */

import type { SetupLogin, SetupStatus, SetupStep, SetupStepId } from "../../api/types";

/** The order every surface uses — the server's, restated so a stale or
 * partial payload still draws in the right order. */
export const STEP_ORDER: SetupStepId[] = ["tools", "agent_login", "repo", "tracker", "phone"];

/** The server's names, kept here only as the fallback for a payload that has
 * not arrived yet (the card still draws its five rows). */
export const STEP_TITLES: Record<SetupStepId, string> = {
  tools: "Engine and tools",
  agent_login: "Sign in to your agent",
  repo: "Pick a repo",
  tracker: "Connect a tracker and GitHub",
  phone: "Your phone",
};

export const REQUIRED_STEPS: SetupStepId[] = ["tools", "agent_login", "repo"];

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

/** "Setup 3/5". */
export function chipLabel(status: SetupStatus | null | undefined): string {
  const s = status?.summary;
  if (!s) return "Setup";
  return "Setup " + s.done + "/" + s.total;
}

/** ①…⑤ for a step. */
export function stepNumber(id: SetupStepId | string): string {
  const i = STEP_ORDER.indexOf(id as SetupStepId);
  return i < 0 ? "•" : "①②③④⑤".charAt(i) || "•";
}

/** The agent CLIs step 2 offers a button for: the installed ones. The default
 * agent comes first; the rest keep the registry's order. */
export function installedLogins(logins: SetupLogin[] | null | undefined): SetupLogin[] {
  const rows = (logins || []).filter((l) => l.installed);
  return rows.filter((l) => l.default).concat(rows.filter((l) => !l.default));
}

/** One agent's sign-in line: what the probe found. */
export function loginLine(l: SetupLogin): { text: string; ok: boolean | null } {
  if (l.logged_in === true) return { text: "signed in", ok: true };
  if (l.logged_in === false) return { text: "not signed in yet", ok: false };
  return { text: "sign-in can't be checked — fine if it works", ok: null };
}

/** Whether step 5 should offer to install Tailscale rather than the QR: the
 * doctor's Tailscale row says it is missing (or broken). A payload without
 * the row (doctor unavailable) still offers the QR — Settings → Your phone is
 * the authority on whether it can be served. */
export function tailscaleMissing(status: SetupStatus | null | undefined): boolean {
  const ts = status?.tailscale;
  if (!ts) return false;
  return ts.status !== "ok";
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
