/** Settings → Advanced (partial 115): engine + platform fields, and the
 * on-demand server restart. */

import { SettingField, useSettings } from "../useSettings";
import { useServerRestart } from "../useServerRestart";
import type { ScreenProps } from "../SettingsDialog";

export function Advanced(_: ScreenProps) {
  const s = useSettings();
  const mode = String(s.get("engine", "mode") ?? "");
  const { restarting, timedOut, restart } = useServerRestart();
  return (
    <>
      <h3 className="set-section-title">Engine</h3>
      <label className="set-row">
        <span className="set-label">Worktree mode</span>
        <select
          data-group="engine"
          data-field="mode"
          value={mode}
          onChange={(e) => s.saveField("engine", "mode", e.target.value)}
        >
          <option value="">(default)</option>
          <option value="worktree">worktree — fast</option>
          <option value="clone">clone — full standalone</option>
        </select>
      </label>
      <h3 className="set-section-title">
        Platform <span className="set-hint">(Windows / WSL only)</span>
      </h3>
      <label className="set-row">
        <span className="set-label">WSL distro</span>
        <SettingField group="platform" field="wsl_distro" placeholder="(your default distro)" />
        <span className="set-hint">
          Name used for `wsl.exe -d &lt;distro&gt;`. Leave empty to use your default distro —
          the one the Windows installer put MindFlock in (`wsl -l -v` lists them).
        </span>
      </label>
      <label className="set-row">
        <span className="set-label">Windows Terminal command</span>
        <SettingField group="platform" field="wt_command" placeholder="wt.exe" />
      </label>
      <h3 className="set-section-title">Server</h3>
      <p className="set-hint">
        Restarts the server process, then reloads this window once it answers again — so both
        halves come back fresh. Your sessions are tmux sessions and the ingestion pipeline is
        its own process, so nothing running is lost. Use it to pick up a config change that
        needs a fresh boot, or to clear a server that has gotten stuck.
      </p>
      <button
        type="button"
        className="test-btn"
        disabled={restarting}
        onClick={() => restart({ reload: true })}
      >
        {restarting ? "Restarting…" : "Restart server & UI"}
      </button>
      {timedOut && (
        <p className="error">
          The server didn’t come back within 30s. Check Settings → System logs, or restart it
          from the terminal.
        </p>
      )}
    </>
  );
}
