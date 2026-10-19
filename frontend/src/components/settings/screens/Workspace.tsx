/** Settings → Workspace (partial 108): where every session's copy of the
 * repo lives (the one answer to "where do my worktrees live?"), how ticket
 * sessions copy it, and the base branches. Fast-track tuning and the live
 * branch are advanced mode only.
 *
 * The paths come from GET /api/settings/workspace-paths: absolute, for every
 * kind of session — the ones you start (always <config dir>/worktrees) and
 * ticket/PR sessions (the Workspace folder field) — and a typed folder is
 * checked as you type (missing → "will be created", unwritable → warned). */

import { useEffect, useState } from "react";
import { api } from "../../../api/client";
import { copyText } from "../../../lib/clipboard";
import { toast } from "../../../lib/toast";
import { SettingField, useSettings } from "../useSettings";
import type { ScreenProps } from "../SettingsDialog";
import { InfoTip } from "../../InfoTip";
import { AdvancedFold } from "../settingsMode";
import { PathField } from "../../PathField";
import { RepoBranches } from "./RepoBranches";

/** GET /api/settings/workspace-paths. */
export interface WorkspacePaths {
  sessions_dir: string;
  tickets_dir: string;
  tickets_default?: boolean;
  /** ok | create | not_dir | not_writable */
  tickets_state?: string;
  tickets_warning?: string;
  env_override?: string;
}

function pathsUrl(typed?: string): string {
  return "/api/settings/workspace-paths" + (typed === undefined ? "" : "?dir=" + encodeURIComponent(typed));
}

/** One absolute path, selectable as a whole, with a Copy button. */
function PathLine({ id, label, path, hint }: { id: string; label: string; path: string; hint: string }) {
  return (
    <div className="set-row ws-path-row" id={id}>
      <span className="set-label">{label}</span>
      <span className="ws-path-line">
        <code className="ws-path">{path || "…"}</code>
        <button
          type="button"
          className="test-btn"
          disabled={!path}
          onClick={() => void copyText(path).then((ok) => toast(ok ? "Path copied" : "Copy failed"))}
        >
          Copy
        </button>
      </span>
      <span className="set-hint">{hint}</span>
    </div>
  );
}

/** "Where your worktrees live": both locations, absolute, plus the folder
 * field for the second one, checked as you type. */
function WorktreeHome() {
  const s = useSettings();
  const stored = String(s.get("repository", "workspace_dir") ?? "");
  const [typed, setTyped] = useState(stored);
  const [paths, setPaths] = useState<WorkspacePaths | null>(null);
  useEffect(() => setTyped(stored), [stored]);

  // Re-resolve as the person types (debounced), so the absolute path and any
  // warning describe the folder in the box, before it is saved.
  useEffect(() => {
    let live = true;
    const t = setTimeout(
      () => {
        api<WorkspacePaths>(pathsUrl(typed === stored ? undefined : typed))
          .then((p) => live && setPaths(p))
          .catch(() => undefined);
      },
      typed === stored ? 0 : 300
    );
    return () => {
      live = false;
      clearTimeout(t);
    };
  }, [typed, stored]);

  const warn = paths?.tickets_warning || "";
  const bad = paths?.tickets_state === "not_dir" || paths?.tickets_state === "not_writable";
  return (
    <section
      className="ws-home"
      id="workspace-paths"
      data-tour="settings-worktrees"
      aria-label="Where your worktrees live"
    >
      <h4 className="set-subtitle">Where your worktrees live</h4>
      <PathLine
        id="workspace-sessions-dir"
        label="Sessions you start"
        path={paths?.sessions_dir || ""}
        hint="Each one gets its own folder here, unless you pick “this folder directly”."
      />
      <PathLine
        id="workspace-tickets-dir"
        label="Ticket and PR sessions"
        path={paths?.tickets_dir || ""}
        hint={
          paths?.env_override
            ? "Set by " + paths.env_override + " on this computer, which wins over the folder below."
            : "Change it with the folder below."
        }
      />
      <label className="set-row">
        <span className="set-label">Workspace folder (ticket and PR sessions)</span>
        <PathField
          data-group="repository"
          data-field="workspace_dir"
          id="workspace-dir-input"
          placeholder="./workspaces"
          value={typed}
          aria-invalid={bad ? true : undefined}
          aria-describedby="workspace-dir-check"
          pickerTitle="Pick the workspace folder"
          onChange={setTyped}
          onBlur={() => typed.trim() !== stored && s.saveField("repository", "workspace_dir", typed.trim())}
          // A pick never blurs the box (focus is in the picker), so it saves here.
          onPicked={(p) => p.trim() !== stored && s.saveField("repository", "workspace_dir", p.trim())}
          onKeyDown={(e) => {
            if (e.key === "Enter") (e.target as HTMLInputElement).blur();
          }}
        />
        <span
          id="workspace-dir-check"
          className={bad ? "set-field-error" : "set-hint"}
          role={bad ? "alert" : undefined}
        >
          {warn || "A full path like ~/mindflock/workspaces is clearest. Blank = ./workspaces."}
        </span>
      </label>
      <label className="set-row" id="workspace-copy-mode">
        <span className="set-label">How ticket sessions copy the repo</span>
        <SettingField
          group="engine"
          field="mode"
          options={[
            { value: "", label: "Worktree — fast (default)" },
            { value: "clone", label: "Full clone — a standalone copy" },
          ]}
        />
        <span className="set-hint">A worktree shares your repo's history; a clone is fully separate.</span>
      </label>
    </section>
  );
}

export function Workspace({ gotoScreen }: ScreenProps) {
  return (
    <>
      <h3 className="set-section-title">Workspace</h3>
      <div className="caps-gate" data-caps-gate="git">
        <p>
          Install <strong>Git</strong> to get access to these features — isolated worktree
          checkouts per session, base branches, diffs, commits and pull requests. Until then,
          sessions simply run in the folder you pick.
        </p>
        <p>
          Install it (e.g. <code>sudo apt install git</code> / <code>brew install git</code>),
          then reload this page.{" "}
          <button type="button" className="linklike" data-goto-screen="doctor" onClick={() => gotoScreen("doctor")}>
            Check in Doctor
          </button>
        </p>
      </div>
      <p className="set-hint set-block-hint">
        Where sessions keep their copy of your repo, and which branches they start from.
      </p>
      <WorktreeHome />
      <label className="set-row">
        <span className="set-label">Default base branch</span>
        <SettingField group="repository" field="base_branch" placeholder="main" />
        <span className="set-hint">New session branches start from this, unless told otherwise.</span>
      </label>
      <label className="set-row">
        <span className="set-label">Default PR base branch</span>
        <SettingField group="repository" field="pr_base_branch" placeholder="use the session's own base" />
        <span className="set-hint">
          Where <strong>Open PR</strong> points. Blank = the branch the session started from.
        </span>
      </label>
      <RepoBranches />
      {/* What "shipped" means, fast-track's default reach and its hook
          retries: tuning for a flow that already works — advanced mode only. */}
      <AdvancedFold id="workspace-advanced">
        <label className="set-row">
          <span className="set-label">Live branch</span>
          <SettingField group="repository" field="live_branch" placeholder="main" />
          <span className="set-hint">
            The branch that means <em>shipped</em>. Verify asks you to check a change once it
            lands here. Blank = the PR base branch, then the default base branch.
          </span>
        </label>
        <label className="set-row">
          <span className="set-label">Fast-track goes as far as</span>
          <SettingField
            group="repository"
            field="fasttrack_depth"
            options={[
              { value: "", label: "Off (default)" },
              { value: "commit", label: "Commit" },
              { value: "push", label: "Push" },
              { value: "pr", label: "Open PR" },
              { value: "merge", label: "Merge when green" },
            ]}
          />
          <span className="set-hint">
            <strong>New sessions start Off; batches and ticket runs start at this default.</strong>{" "}
            Change one session any time with its <strong>⏩</strong> button.
            <InfoTip>
              A batch is a list in the New dialog or Intake's <em>Start together</em>. Fast-track
              waits for the agent to finish, then commits, pushes and carries on to this step. A
              ticket source keeps its own <em>Fast-track tickets to</em> (Intake), which never
              merges.
            </InfoTip>
          </span>
        </label>
        <label className="set-row">
          <span className="set-label">Retryable pre-commit hooks</span>
          <SettingField
            group="repository"
            field="precommit_retry_hooks"
            placeholder="e.g. black"
          />
          <span className="set-hint">
            Hooks fast-track may skip when they fail without changing any files.
            <InfoTip>
              Comma-separated pre-commit hook <strong>IDs</strong> (not display names:{" "}
              <code>Black format</code> is the hook <code>black</code>). Fast-track retries the
              commit once, then re-runs it with <code>SKIP=&lt;id&gt;</code> and says which hook it
              skipped. Test and secret-scanning hooks are refused here.
            </InfoTip>
          </span>
        </label>
        <FailureNotesRow />
      </AdvancedFold>
    </>
  );
}

/** When fast-track stops on something the agent can fix (a pre-commit step or
 * the check failed, red CI, a conflicting PR), MindFlock types a short note
 * into that agent saying what failed (backend/web/core/autopilot_driver.py
 * `_autopilot_tell_agent`). Unset reads as on. */
function FailureNotesRow() {
  const s = useSettings();
  const on = s.get("repository", "fasttrack_failure_notes") !== false;
  return (
    <div className="set-row set-switch-row" id="workspace-failure-notes">
      <span className="notif-rule-text">
        <span className="set-label">Tell the agent when fast-track stops</span>
        <span className="set-hint notif-rule-desc">
          When a pre-commit step or the verification check fails, CI goes red or the PR
          conflicts with its base, fast-track stops and leaves the agent a one-line note: what
          failed and to fix it — or, when it is a conflict with someone else's change, to stop
          and tell you. You still get the usual &quot;fast-track stopped&quot; notice.
        </span>
      </span>
      {/* label wraps only the switch, so clicking the row text doesn't flip it */}
      <label className="ca-switch">
        <input
          type="checkbox"
          checked={on}
          onChange={(e) => {
            s.saveField("repository", "fasttrack_failure_notes", e.target.checked);
            toast(e.target.checked ? "Fast-track will tell the agent what failed" : "Fast-track failure notes off");
          }}
        />
        <span className="ca-slider" />
      </label>
    </div>
  );
}
