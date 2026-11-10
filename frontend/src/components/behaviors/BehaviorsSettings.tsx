/** The Verify board's ⋯ settings (docs/behaviors.md §3): the one "Write
 * Verify steps on every push" switch, watched repositories, "Check another
 * branch…". (No "start empty": an empty plan has no way to gain steps since
 * the redesign — Verify has the session's own agent write them.)
 *
 * SEAM (pre-committed by the integrator). The props are frozen; the body is
 * vs-card's. The Behaviors board (vs-board) mounts it. It carries its own
 * settings model (SettingsCtx), so the reused Intake/Verify pieces —
 * WatchedRepositories, AddRepoFlow, RepoEditor — work wherever it is shown. */

import { useEffect, useState, type JSX } from "react";
import type { Json } from "../../api/types";
import { openBehaviors } from "../../lib/behaviorsFocus";
import { askBehaviorsForBranch, fetchVerifyRepos, type VerifyRepo } from "../../lib/verifyActions";
import { queryClient, useTestPlans } from "../../state/queries";
import { WatchedRepositories } from "./WatchedRepositories";
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
    <div className="bh-settings" role="group" aria-label="Verify settings" data-no-drag onKeyDown={(e) => e.stopPropagation()}>
      <AutomationSwitch
        label="Write Verify steps on every push"
        title="Pause or resume writing steps on push — nothing is deleted"
        rowId="bh-push-row"
        inputId="bh-push"
        statusId="bh-push-status"
        checked={on}
        note={watched.length ? undefined : "Covers your sessions' branches, and any repository you watch below."}
        onChange={async (next) => {
          const ok = await model.saveGroup("repository", { verify_enabled: next }, next ? "Writing Verify steps on every push" : "No new steps on push — nothing is deleted");
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
        {watched.length ? <WatchedRepositories liveBranch={liveBranch} /> : <p className="bh-hint">None yet — steps are written for your sessions' branches.</p>}
        {adding ? (
          <AddRepoFlow title="Watch a GitHub repository" uses={["verify"]} only="verify" liveBranch={liveBranch} onCancel={() => setAdding(false)} onDone={() => setAdding(false)} />
        ) : (
          <button type="button" className="test-btn" id="bh-watch-repo" onClick={() => setAdding(true)}>
            Watch a GitHub repository for every push
          </button>
        )}
      </section>

      <AnotherBranch onClose={onClose} />
    </div>
  );
}

/** "Check another branch…": a repo + a branch no session has been on → the
 * agent writes the steps to verify it. */
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

  const go = async () => {
    const b = branch.trim();
    if (!repo || !b || busy) return;
    setBusy("ask");
    try {
      const id = await askBehaviorsForBranch(repo, b);
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
            if (e.key === "Enter") void go();
          }}
        />
        <div className="bh-edit-actions">
          <button type="button" id="bh-branch-ask" className="btn-primary" disabled={!!busy || !repo || !branch.trim()} onClick={() => void go()}>
            {busy === "ask" ? "Asking…" : "Ask the agent"}
          </button>
        </div>
        <p className="bh-hint">
          {repos && !repos.length
            ? "No repositories yet — open a session in one first."
            : "For a branch no session has been on — the agent reads its changes and writes the steps to verify it (about three minutes)."}
        </p>
      </div>
    </details>
  );
}
