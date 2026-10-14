/** Intake's three auto-start switches, read as one thing.
 *
 * Intake has three automations, each with its own switch: tickets (the
 * pipeline's ticket half, POST /api/mindflock/start|stop), PR comments
 * (`github.enabled`) and issues (`github.issues_enabled`). They are genuinely
 * independent — the pipeline runs whichever halves are on — and each is
 * turned on and off on its own tab (and its sidebar bar). This module reads
 * them together, so the Intake header, the row chips, the Auto-start tab and
 * New → Ticket all say the same thing about what starts on its own.
 *
 * There is no "pause everything" control any more (the owner found it
 * pointless: the switches already do the job one at a time). Older builds
 * recorded what such a pause turned off in `general.intake_paused`; that
 * record is no longer read — the switches it turned off simply show as off,
 * and turning one back on is the ordinary switch. */

import { useQuery } from "@tanstack/react-query";
import { api } from "../../api/client";
import { useMindflockStatus } from "../../state/queries";

export type IntakeKind = "tickets" | "prs" | "issues";

export interface IntakeGithub {
  enabled?: boolean;
  issues_enabled?: boolean;
  repos?: string[];
  issue_repos?: string[];
}

export interface IntakeSwitches {
  on: Record<IntakeKind, boolean>;
  configured: Record<IntakeKind, boolean>;
}

/** Whether PR comments auto-start: an explicit true, or — the rule saved
 * settings from before the default changed still follow — unset with repos
 * already watched. A fresh install adds its first repo with an explicit false
 * (see PullRequestsTab), so it starts off like Issues. */
export function prsOn(gh: IntakeGithub | null | undefined): boolean {
  const repos = Array.isArray(gh?.repos) ? gh!.repos!.filter(Boolean) : [];
  if (!repos.length) return false;
  return gh?.enabled === true || gh?.enabled == null;
}

export function issuesOn(gh: IntakeGithub | null | undefined): boolean {
  const repos = Array.isArray(gh?.issue_repos) ? gh!.issue_repos!.filter(Boolean) : [];
  return !!repos.length && gh?.issues_enabled === true;
}

export function intakeSwitches(opts: {
  ticketsDesired: boolean;
  ticketSources: number;
  gh: IntakeGithub | null | undefined;
}): IntakeSwitches {
  const gh = opts.gh || {};
  return {
    on: {
      tickets: !!opts.ticketsDesired && opts.ticketSources > 0,
      prs: prsOn(gh),
      issues: issuesOn(gh),
    },
    configured: {
      tickets: opts.ticketSources > 0,
      prs: (gh.repos || []).filter(Boolean).length > 0,
      issues: (gh.issue_repos || []).filter(Boolean).length > 0,
    },
  };
}

export const KIND_NAMES: Record<IntakeKind, string> = {
  tickets: "tickets",
  prs: "PR comments",
  issues: "issues",
};

/** "tickets and PR comments" — the kinds, in tab order, as a phrase. */
export function kindsText(kinds: IntakeKind[]): string {
  const names = (["tickets", "prs", "issues"] as IntakeKind[])
    .filter((k) => kinds.includes(k))
    .map((k) => KIND_NAMES[k]);
  if (names.length <= 1) return names.join("");
  return names.slice(0, -1).join(", ") + " and " + names[names.length - 1];
}

/** The same `github-settings` query the sidebar's PR / Issues bars read
 * (useGithubToggleBar) — one cache, so a switch flipped anywhere shows on
 * every surface at once. */
export function useGithubSettings() {
  return useQuery({
    queryKey: ["github-settings"],
    queryFn: async () => {
      const r = await api<{ settings?: { github?: IntakeGithub; ticketing?: { sources?: unknown[] } } }>(
        "/api/settings"
      );
      return r?.settings?.github || {};
    },
    refetchInterval: 30_000,
    retry: false,
  });
}

/** The three switches as they stand now, for the Intake header, the
 * Auto-start tab, the row chips and New → Ticket alike. `ticketSources` is the
 * number of SAVED ticket sources. */
export function useIntakeSwitches(ticketSources: number) {
  const status = useMindflockStatus().data;
  const gh = useGithubSettings();
  const sw = intakeSwitches({
    ticketsDesired: !!(status?.desired ?? status?.running),
    ticketSources,
    gh: gh.data,
  });
  const onKinds = (["tickets", "prs", "issues"] as IntakeKind[]).filter((k) => sw.on[k]);
  const anyConfigured = sw.configured.tickets || sw.configured.prs || sw.configured.issues;
  return { status, sw, onKinds, anyConfigured };
}
