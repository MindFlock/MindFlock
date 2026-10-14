/** "Pause intake" — one control that stops every auto-start at once.
 *
 * Intake has three automations, each with its own switch: tickets (the
 * pipeline's ticket half, POST /api/mindflock/start|stop), PR comments
 * (`github.enabled`) and issues (`github.issues_enabled`). They are genuinely
 * independent — the pipeline runs whichever halves are on — which is right for
 * tuning and wrong for "I'm on holiday": that took three switches on three tabs,
 * and nothing on screen said all three were off.
 *
 * Pausing turns every switch that is on OFF and records which ones were on in
 * the server's settings (`general.intake_paused`), so a reload, the desktop app
 * or another browser on this machine all see "paused" and can Resume. Resuming
 * turns exactly those back on and clears the record. With an empty record (an
 * older browser-only pause carried over) it resumes every kind that has
 * something configured, which is what "turn intake back on" means to someone
 * who never touched the individual switches.
 *
 * "Paused" is "the Pause button was used, and every switch is still off": the
 * record says the first half, the switches the second, so the two can never
 * disagree. A setup that was simply never switched on is OFF, not paused —
 * calling it paused offered to "resume" something that never ran. Turning a
 * switch back on by hand ends the pause (the record is cleared), so a later
 * "everything off" is off again, not a stale pause. */

import { useEffect, useState } from "react";
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

/** The browser-only record older builds kept. Read once as a fallback (a
 * pause made before the record moved to the server still reads as paused) and
 * removed on the next Pause or Resume. */
const LEGACY_REMEMBER_KEY = "mf_intake_paused_kinds";

function cleanKinds(v: unknown): IntakeKind[] | null {
  return Array.isArray(v)
    ? (v.filter((k) => k === "tickets" || k === "prs" || k === "issues") as IntakeKind[])
    : null;
}

export function loadLegacyRemembered(): IntakeKind[] | null {
  try {
    return cleanKinds(JSON.parse(localStorage.getItem(LEGACY_REMEMBER_KEY) || "null"));
  } catch {
    return null;
  }
}

function dropLegacyRemembered() {
  try {
    localStorage.removeItem(LEGACY_REMEMBER_KEY);
  } catch {
    /* storage unavailable: nothing to drop */
  }
}

/** What Pause turned off, as the server stores it (`general.intake_paused`;
 * null = not paused), falling back to an older browser-only record. */
export function useIntakePaused() {
  return useQuery({
    queryKey: ["intake-paused"],
    queryFn: async () => {
      const r = await api<{ settings?: { general?: { intake_paused?: unknown } } }>("/api/settings");
      return cleanKinds(r?.settings?.general?.intake_paused) ?? loadLegacyRemembered();
    },
    refetchInterval: 30_000,
    retry: false,
  });
}

async function saveRemembered(kinds: IntakeKind[] | null) {
  await api("/api/settings", { json: { general: { intake_paused: kinds } } });
  dropLegacyRemembered();
}

/** When the switches last moved through Pause / Resume — a reading older than
 * this may still show a switch on that the pause just turned off, so it must
 * not be taken as "a switch was turned back on by hand". */
let lastPauseToggle = 0;

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
  const statusQ = useMindflockStatus();
  const status = statusQ.data;
  const gh = useGithubSettings();
  const pausedQ = useIntakePaused();
  const remembered = pausedQ.data ?? null;
  const [busy, setBusy] = useState(false);
  const sw = intakeSwitches({
    ticketsDesired: !!(status?.desired ?? status?.running),
    ticketSources,
    gh: gh.data,
  });
  const paused = isPaused(sw, remembered);
  const onKinds = (["tickets", "prs", "issues"] as IntakeKind[]).filter((k) => sw.on[k]);
  const anyConfigured = sw.configured.tickets || sw.configured.prs || sw.configured.issues;

  // A switch turned back on by hand ends the pause: clear the record, or the
  // next "everything off" would read as paused and offer to resume a set that
  // is no longer what anyone chose. Only on readings taken after the last
  // Pause/Resume, so the pause's own in-flight state never clears itself.
  const fresh =
    !!status &&
    !!gh.data &&
    statusQ.dataUpdatedAt > lastPauseToggle &&
    gh.dataUpdatedAt > lastPauseToggle &&
    pausedQ.dataUpdatedAt > lastPauseToggle;
  const stale = remembered !== null && onKinds.length > 0 && fresh && !busy;
  useEffect(() => {
    if (!stale) return;
    lastPauseToggle = Date.now();
    void saveRemembered(null)
      .catch(() => {
        /* best effort: it is re-tried on the next reading */
      })
      .finally(() => queryClient.invalidateQueries({ queryKey: ["intake-paused"] }));
  }, [stale]);

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
    if (!paused && !onKinds.length) return;
    setBusy(true);
    lastPauseToggle = Date.now();
    try {
      if (paused) {
        const kinds = resumeKinds(remembered, sw.configured);
        await setKinds(kinds, true);
        await saveRemembered(null);
        toast("Intake resumed — auto-start is on for " + kindsText(kinds));
      } else {
        // Recorded first: if a switch then fails to turn off, Resume still
        // knows what was on.
        await saveRemembered(onKinds);
        await setKinds(onKinds, false);
        toast("Intake paused — nothing new starts on its own");
      }
    } catch (err) {
      errorPop(paused ? "Resume intake failed" : "Pause intake failed", (err as Error).message || "error");
    } finally {
      lastPauseToggle = Date.now();
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["github-settings"] }),
        queryClient.invalidateQueries({ queryKey: ["intake-paused"] }),
        refreshMindflockStatus(),
      ]);
      setBusy(false);
      after?.();
    }
  };

  /** Whether `kind` is off because of the pause (rather than plain off). */
  const pausedKind = (kind: IntakeKind) =>
    paused && (remembered && remembered.length ? remembered.includes(kind) : sw.configured[kind]);

  return { status, sw, paused, pausedKind, onKinds, anyConfigured, busy, toggle };
}
