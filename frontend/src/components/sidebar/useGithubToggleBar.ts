/** Shared query + optimistic-toggle logic behind the two GitHub automation
 * bars (PrReviewBar, GitIssueBar). Both poll the same `github-settings` +
 * `mindflock-status` queries, re-read on dialog close, and flip a single
 * github.<settingKey> flag with the same optimistic/refetch dance — they
 * differ only in which setting/repo-list key they read, the default polarity
 * (PR review defaults on once repos exist; issue handling is opt-in), which
 * status flag means "actively working", and their labels. Each bar keeps its
 * own markup; only this behavior is shared. */

import { useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../../api/client";
import { errMsg } from "../../lib/format";
import { useConfig } from "../../state/queries";
import { useUi } from "../../state/store";

interface GithubSettings {
  enabled?: boolean;
  issues_enabled?: boolean;
  repos?: string[];
  issue_repos?: string[];
}

interface MfStatus {
  available: boolean;
  running: boolean;
  pr_active?: boolean;
  issues_active?: boolean;
}

export interface GithubToggleBar {
  /** True once caps allow it AND repositories exist (the bar renders). */
  visible: boolean;
  repos: string[];
  on: boolean;
  running: boolean;
  active: boolean;
  starting: boolean;
  busy: boolean;
  toggle: (enable: boolean) => Promise<void>;
}

export function useGithubToggleBar(opts: {
  settingKey: "enabled" | "issues_enabled";
  reposKey: "repos" | "issue_repos";
  /** absent flag => on (PR review) vs off (issue handling, opt-in). */
  defaultOn: boolean;
  activeFlag: "pr_active" | "issues_active";
  /** Prefix for the toggle-failure alert ("PR review" / "Issue handling"). */
  toggleLabel: string;
}): GithubToggleBar {
  const { settingKey, reposKey, defaultOn, activeFlag, toggleLabel } = opts;
  const { data: config } = useConfig();
  const openDialog = useUi((s) => s.openDialog);
  const qc = useQueryClient();
  const [busy, setBusy] = useState(false);
  const [optimistic, setOptimistic] = useState<boolean | null>(null);

  const { data: gh, refetch } = useQuery({
    queryKey: ["github-settings"],
    queryFn: async () => {
      const r = await api<{ settings?: { github?: GithubSettings } }>("/api/settings");
      return r?.settings?.github || {};
    },
    refetchInterval: 30_000,
    retry: false,
  });
  // Same key AND interval as AutomationBar's poll — TanStack dedupes it into
  // one request, and a mismatched interval would make which one wins matter.
  const { data: ingestion } = useQuery({
    queryKey: ["mindflock-status"],
    queryFn: () => api<MfStatus>("/api/mindflock/status"),
    refetchInterval: 4_000,
    retry: false,
  });

  // The Settings dialog edits github.* without touching this query — re-read
  // whenever a dialog closes so the bar reflects it at once.
  useEffect(() => {
    if (openDialog === null) refetch();
  }, [openDialog, refetch]);

  const capsOk = !(config?.caps && (!config.caps.git || !config.caps.ticketing));
  const reposRaw = gh ? gh[reposKey] : undefined;
  const repos = Array.isArray(reposRaw) ? reposRaw : [];
  const visible = capsOk && !!gh && repos.length > 0;

  const flag = gh ? gh[settingKey] : undefined;
  const on = optimistic ?? (defaultOn ? flag !== false : flag === true);
  const running = !!ingestion?.running;
  // Green only while one is actually being worked on; running-but-idle
  // "waits" (gold) for a new item to appear.
  const active = running && !!ingestion?.[activeFlag];
  const starting = on && !running;

  const toggle = async (enable: boolean) => {
    if (busy) return;
    setBusy(true);
    setOptimistic(enable);
    try {
      await api("/api/settings", { json: { github: { [settingKey]: enable } } });
    } catch (err) {
      alert(`${toggleLabel} ${enable ? "on" : "off"} failed: ` + errMsg(err));
    } finally {
      setBusy(false);
      setOptimistic(null);
      refetch();
      // The toggle may start/stop/bounce the shared pipeline process — pull
      // fresh status now instead of waiting out the 10s poll.
      qc.invalidateQueries({ queryKey: ["mindflock-status"] });
    }
  };

  return { visible, repos, on, running, active, starting, busy, toggle };
}
