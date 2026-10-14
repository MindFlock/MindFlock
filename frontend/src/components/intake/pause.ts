/** "Pause intake" — one control that stops every auto-start at once.
 *
 * Intake has three automations, each with its own switch: tickets (the
 * pipeline's ticket half, POST /api/mindflock/start|stop), PR comments
 * (`github.enabled`) and issues (`github.issues_enabled`). They are genuinely
 * independent — the pipeline runs whichever halves are on — which is right for
 * tuning and wrong for "I'm on holiday": that took three switches on three tabs,
 * and nothing on screen said all three were off.
 *
 * Pausing turns every switch that is on OFF and remembers which ones were on
 * (per device, in localStorage — the switches themselves are synced settings).
 * Resuming turns exactly those back on. With nothing remembered (paused on
 * another device, or storage cleared) it resumes every kind that has something
 * configured, which is what "turn intake back on" means to someone who never
 * touched the individual switches.
 *
 * "Paused" is "the Pause button was used, and every switch is still off": the
 * remembered set says the first half, the switches the second, so the two can
 * never disagree. A setup that was simply never switched on is OFF, not
 * paused — calling it paused offered to "resume" something that never ran. */

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../../api/client";
import { queryClient, refreshMindflockStatus, useMindflockStatus } from "../../state/queries";
import { errorPop } from "../../lib/errorPop";
import { toast } from "../../lib/toast";

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

/** Paused = Pause was pressed (`remembered` is what it turned off) and
 * nothing auto-starts since. */
export function isPaused(sw: IntakeSwitches, remembered: IntakeKind[] | null): boolean {
  const any = (r: Record<IntakeKind, boolean>) => r.tickets || r.prs || r.issues;
  return remembered !== null && any(sw.configured) && !any(sw.on);
}

/** What Resume turns back on: the remembered set, limited to what is still
 * configured; with nothing remembered, everything configured. */
export function resumeKinds(
  remembered: IntakeKind[] | null,
  configured: Record<IntakeKind, boolean>
): IntakeKind[] {
  const all: IntakeKind[] = ["tickets", "prs", "issues"];
  const base = remembered && remembered.length ? remembered : all;
  return all.filter((k) => base.includes(k) && configured[k]);
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

const REMEMBER_KEY = "mf_intake_paused_kinds";

export function loadRemembered(): IntakeKind[] | null {
  try {
    const v = JSON.parse(localStorage.getItem(REMEMBER_KEY) || "null");
    return Array.isArray(v) ? (v.filter((k) => k === "tickets" || k === "prs" || k === "issues") as IntakeKind[]) : null;
  } catch {
    return null;
  }
}

function saveRemembered(kinds: IntakeKind[] | null) {
  try {
    if (kinds === null) localStorage.removeItem(REMEMBER_KEY);
    else localStorage.setItem(REMEMBER_KEY, JSON.stringify(kinds));
  } catch {
    /* storage unavailable: Resume falls back to "everything configured" */
  }
}

/** The same `github-settings` query the sidebar's PR / Issues bars read
 * (useGithubToggleBar) — one cache, so a pause shows on every switch at once. */
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

/** Everything the Pause control needs, for the Intake header and the sidebar
 * bar alike. `ticketSources` is the number of SAVED ticket sources. `after`
 * runs once the switches have moved (the Intake dialog reloads its settings). */
export function useIntakePause(ticketSources: number, after?: () => void) {
  const { data: status } = useMindflockStatus();
  const gh = useGithubSettings();
  const [busy, setBusy] = useState(false);
  const sw = intakeSwitches({
    ticketsDesired: !!(status?.desired ?? status?.running),
    ticketSources,
    gh: gh.data,
  });
  const paused = isPaused(sw, loadRemembered());
  const onKinds = (["tickets", "prs", "issues"] as IntakeKind[]).filter((k) => sw.on[k]);
  const anyConfigured = sw.configured.tickets || sw.configured.prs || sw.configured.issues;

  const setKinds = async (kinds: IntakeKind[], on: boolean) => {
    const github: Record<string, boolean> = {};
    if (kinds.includes("prs")) github.enabled = on;
    if (kinds.includes("issues")) github.issues_enabled = on;
    if (Object.keys(github).length) await api("/api/settings", { json: { github } });
    if (kinds.includes("tickets"))
      await api(`/api/mindflock/${on ? "start" : "stop"}`, { method: "POST" });
  };

  const toggle = async () => {
    if (busy) return;
    setBusy(true);
    try {
      if (paused) {
        const kinds = resumeKinds(loadRemembered(), sw.configured);
        await setKinds(kinds, true);
        saveRemembered(null);
        toast("Intake resumed — auto-start is on for " + kindsText(kinds));
      } else {
        await setKinds(onKinds, false);
        saveRemembered(onKinds);
        toast("Intake paused — nothing new starts on its own");
      }
    } catch (err) {
      errorPop(paused ? "Resume intake failed" : "Pause intake failed", (err as Error).message || "error");
    } finally {
      setBusy(false);
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["github-settings"] }),
        refreshMindflockStatus(),
      ]);
      after?.();
    }
  };

  return { status, sw, paused, onKinds, anyConfigured, busy, toggle };
}
