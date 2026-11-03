/** The Behaviors board's ⋯ settings (docs/behaviors.md §3): the one
 * "Write behaviors on every push" switch, watched repositories, "Check
 * another branch…", "Start empty behaviors for a session…".
 *
 * SEAM (pre-committed by the integrator). The props are frozen; the body is
 * vs-card's. The Behaviors board (vs-board) mounts it. It carries its own
 * settings model (SettingsCtx), so the reused Intake/Verify pieces —
 * VerifySources, AddRepoFlow, RepoEditor — work wherever it is shown. */

import { useEffect, useState, type JSX } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../../api/client";
import type { Json } from "../../api/types";
import { openBehaviors } from "../../lib/behaviorsFocus";
import { askBehaviorsForBranch, fetchVerifyRepos, startEmptyBehaviors, type VerifyRepo } from "../../lib/verifyActions";
import { queryClient, useInstances, useTestPlans } from "../../state/queries";
import { checklistTargets, type ChecklistTarget, type ClosedTarget } from "../dialogs/verify";
import { VerifySources } from "../dialogs/VerifyChrome";
import { AutomationSwitch } from "../intake/kit";
import { AddRepoFlow } from "../intake/RepoSources";
import { SettingsCtx, useSettings, useSettingsModel } from "../settings/useSettings";
import "./BehaviorsCard.css";
import "./BehaviorsSettings.css";

export interface BehaviorsSettingsProps {
  /** Close the popover/panel it is shown in. */
  onClose?(): void;
}

/** `repository.verify_enabled` — absent means on (the backend's default). */
export function writesOnPush(settings: Json | null | undefined): boolean {
  const repo = (settings?.repository || {}) as { verify_enabled?: boolean };
  return repo.verify_enabled !== false;
}

/** The sessions "Start empty behaviors for a session…" may offer: open or
 * recently closed, on a branch, with no behaviors yet (a branch with no
 * commits still qualifies — you write them by hand). */
export function emptyTargets(targets: readonly ChecklistTarget[]): ChecklistTarget[] {
  return targets.filter((t) => !t.planId && (!t.reason || t.noCommits));
}

/** The picker's line for one session. */
export function targetLabel(t: ChecklistTarget): string {
  return (t.branch || t.title) + (t.title !== t.branch ? " — " + t.title : "") + (t.closed ? " (closed)" : "");
}

export function BehaviorsSettings({ onClose }: BehaviorsSettingsProps): JSX.Element {
  const model = useSettingsModel(true);
  return (
    <SettingsCtx.Provider value={model}>
      <SettingsBody onClose={onClose} />
    </SettingsCtx.Provider>
  );
}

function SettingsBody({ onClose }: BehaviorsSettingsProps): JSX.Element {
  const model = useSettings();
  const plansQ = useTestPlans();
  const liveBranch = plansQ.data?.live_branch || "";
  const on = writesOnPush(model.settings);
  const watched = ((model.settings.repository || {}) as { verify_repos?: string[] }).verify_repos || [];
  const [adding, setAdding] = useState(false);

  return (
    <div className="bh-settings" role="group" aria-label="Behaviors settings" data-no-drag onKeyDown={(e) => e.stopPropagation()}>
      <AutomationSwitch
        label="Write behaviors on every push"
        title="Pause or resume writing behaviors on push — nothing is deleted"
        rowId="bh-push-row"
        inputId="bh-push"
        statusId="bh-push-status"
        checked={on}
        note={watched.length ? undefined : "Covers your sessions' branches, and any repository you watch below."}
        onChange={async (next) => {
          const ok = await model.saveGroup("repository", { verify_enabled: next }, next ? "Writing behaviors on every push" : "No new behaviors on push — nothing is deleted");
          if (ok) {
            // The sidebar bar's copy and the plan list read the same switch.
            void queryClient.invalidateQueries({ queryKey: ["verify-settings"] });
            void queryClient.invalidateQueries({ queryKey: ["test-plans"] });
          }
        }}
      />

      <section className="bh-set-section" aria-labelledby="bh-set-repos">
        <h3 id="bh-set-repos" className="bh-set-head">
          Watched repositories
        </h3>
        {watched.length ? <VerifySources liveBranch={liveBranch} /> : <p className="bh-hint">None yet — behaviors are written for your sessions' branches.</p>}
        {adding ? (
          <AddRepoFlow title="Watch a GitHub repository" uses={["verify"]} only="verify" liveBranch={liveBranch} onCancel={() => setAdding(false)} onDone={() => setAdding(false)} />
        ) : (
          <button type="button" className="test-btn" id="bh-watch-repo" onClick={() => setAdding(true)}>
            Watch a GitHub repository for every push
          </button>
        )}
      </section>

      <AnotherBranch onClose={onClose} />
      <EmptyForSession liveBranch={liveBranch} onClose={onClose} />
    </div>
  );
}

/** "Check another branch…": a repo + a branch no session has been on → the
 * agent writes behaviors for it (or start them empty). */
function AnotherBranch({ onClose }: BehaviorsSettingsProps): JSX.Element {
  const [open, setOpen] = useState(false);
  const [repos, setRepos] = useState<VerifyRepo[] | null>(null);
  const [root, setRoot] = useState("");
  const [branch, setBranch] = useState("");
  const [busy, setBusy] = useState("");
  const repo = repos?.find((r) => r.root === root) || repos?.[0];

  useEffect(() => {
    if (!open || repos !== null) return;
    let live = true;
    fetchVerifyRepos().then(
      (list) => live && setRepos(list),
      () => live && setRepos([]),
    );
    return () => {
      live = false;
    };
  }, [open, repos]);

  const go = async (empty: boolean) => {
    const b = branch.trim();
    if (!repo || !b || busy) return;
    setBusy(empty ? "empty" : "ask");
    try {
      const id = await askBehaviorsForBranch(repo, b, { empty });
      if (id) {
        setBranch("");
        onClose?.();
        openBehaviors({ planId: id });
      }
    } finally {
      setBusy("");
    }
  };

  return (
    <details className="bh-set-section bh-set-fold" open={open} onToggle={(e) => setOpen((e.currentTarget as HTMLDetailsElement).open)}>
      <summary className="bh-set-head">Check another branch…</summary>
      <div className="bh-set-form">
        <select id="bh-branch-repo" className="fl-input" aria-label="Repository" value={repo?.root || ""} disabled={!!busy || !repos?.length} onChange={(e) => setRoot(e.target.value)}>
          {repos === null ? (
            <option value="">Loading repositories…</option>
          ) : !repos.length ? (
            <option value="">No repositories yet</option>
          ) : (
            repos.map((r) => (
              <option key={r.root} value={r.root} title={r.root}>
                {r.name}
              </option>
            ))
          )}
        </select>
        <input
          id="bh-branch-name"
          type="text"
          className="fl-input"
          aria-label="Branch"
          placeholder="branch, e.g. feature/login"
          autoComplete="off"
          value={branch}
          disabled={!!busy}
          onChange={(e) => setBranch(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") void go(false);
          }}
        />
        <div className="bh-edit-actions">
          <button type="button" id="bh-branch-ask" className="btn-primary" disabled={!!busy || !repo || !branch.trim()} onClick={() => void go(false)}>
            {busy === "ask" ? "Asking…" : "Ask the agent"}
          </button>
          <button type="button" id="bh-branch-empty" className="test-btn" disabled={!!busy || !repo || !branch.trim()} onClick={() => void go(true)}>
            {busy === "empty" ? "Starting…" : "Start empty"}
          </button>
        </div>
        <p className="bh-hint">
          {repos && !repos.length
            ? "No repositories yet — open a session in one first."
            : "For a branch no session has been on — the agent reads its changes and writes behaviors (about three minutes)."}
        </p>
      </div>
    </details>
  );
}

/** "Start empty behaviors for a session…": no agent involved — you write
 * what the work should do. Open and recently closed sessions are offered. */
function EmptyForSession({ liveBranch, onClose }: { liveBranch: string; onClose?(): void }): JSX.Element {
  const [open, setOpen] = useState(false);
  const [picked, setPicked] = useState("");
  const [busy, setBusy] = useState(false);
  const instancesQ = useInstances();
  const plansQ = useTestPlans();
  const closedQ = useQuery({
    queryKey: ["recently-closed"],
    queryFn: () => api<ClosedTarget[]>("/api/recently-closed"),
    enabled: open,
    staleTime: 30_000,
    retry: false,
  });
  const targets = emptyTargets(checklistTargets(instancesQ.data || [], plansQ.data?.plans || [], closedQ.data || [], liveBranch));
  const target = targets.some((t) => t.title === picked) ? picked : targets[0]?.title || "";

  const go = async () => {
    if (!target || busy) return;
    setBusy(true);
    try {
      const id = await startEmptyBehaviors(target);
      if (id) {
        onClose?.();
        openBehaviors({ planId: id });
      }
    } finally {
      setBusy(false);
    }
  };

  return (
    <details className="bh-set-section bh-set-fold" open={open} onToggle={(e) => setOpen((e.currentTarget as HTMLDetailsElement).open)}>
      <summary className="bh-set-head">Start empty behaviors for a session…</summary>
      <div className="bh-set-form">
        <select id="bh-empty-pick" className="fl-input" aria-label="Session" value={target} disabled={busy || !targets.length} onChange={(e) => setPicked(e.target.value)}>
          {targets.length ? (
            targets.map((t) => (
              <option key={t.title} value={t.title}>
                {targetLabel(t)}
              </option>
            ))
          ) : (
            <option value="">No session without behaviors</option>
          )}
        </select>
        <div className="bh-edit-actions">
          <button type="button" id="bh-empty-go" className="test-btn" disabled={busy || !target} onClick={() => void go()}>
            {busy ? "Starting…" : "Start empty behaviors"}
          </button>
        </div>
        <p className="bh-hint">No agent involved — you write what the work should do, then check it.</p>
      </div>
    </details>
  );
}
