/** Settings → Advanced (partial 115): engine + platform fields, and the
 * on-demand server restart. */

import { useEffect, useState } from "react";
import { SettingField, useSettings } from "../useSettings";
import { useServerRestart } from "../useServerRestart";
import type { ScreenProps } from "../SettingsDialog";

export function Advanced(_: ScreenProps) {
  const s = useSettings();
  const mode = String(s.get("engine", "mode") ?? "");
  // Tri-state on the wire: unset (never touched) means the backend default,
  // which is ON — so only an explicit stored `false` unchecks this.
  const engineSessions = s.get("engine", "enabled") !== false;
  const { restarting, timedOut, restart } = useServerRestart();
  return (
    <>
      <h3 className="set-section-title">Engine</h3>
      <div
        className="set-row set-switch-row"
        title="On: each ingested ticket becomes a MindFlock session — worktree, branch, seeded agent, and the guided commit → push → PR bar. Off: a detached tmux session plus an OS terminal tab, with no session in the app."
      >
        <span className="set-label">Ticket sessions in MindFlock</span>
        {/* label wraps only the switch, so clicking the row text no longer flips it */}
        <label className="ca-switch">
          <input
            type="checkbox"
            checked={engineSessions}
            onChange={(e) => s.saveField("engine", "enabled", e.target.checked)}
          />
          <span className="ca-slider" />
        </label>
      </div>
      <p className="set-hint">
        Leave this on to get the flow in the screenshots: tickets assigned to you become
        MindFlock sessions you can watch, commit, and open a PR from. Turning it off sends
        ingested tickets to a bare tmux session and an OS terminal tab instead. Takes effect
        the next time the ingestion pipeline starts.
      </p>
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
      <EngineUpdate />
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
      <UninstallSection />
    </>
  );
}

/** Engine update, desktop-shell only. Polls the shell (electron/main.js
 * `engine:update-info`) for whether the installed engine is behind the latest
 * released one; if so, offers a one-click update that pulls just the small
 * engine package (not the app) and self-restarts the server. In a plain
 * browser `window.mfengine` is absent → renders nothing. */
type EngineInfo = { available?: boolean; current?: string; latest?: string };
function EngineUpdate() {
  const mfengine = (
    window as unknown as {
      mfengine?: {
        updateInfo?: () => Promise<EngineInfo>;
        install?: () => Promise<unknown>;
        installState?: () => Promise<{ state?: string; code?: number }>;
      };
    }
  ).mfengine;
  const [info, setInfo] = useState<EngineInfo | null>(null);
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    if (!mfengine?.updateInfo) return;
    let alive = true;
    mfengine.updateInfo().then((i) => alive && setInfo(i)).catch(() => {});
    return () => {
      alive = false;
    };
  }, [mfengine]);

  if (!mfengine?.updateInfo) return null; // web build / not the desktop shell

  const update = () => {
    if (busy || !mfengine.install) return;
    setBusy(true);
    setFailed(false);
    mfengine.install().catch(() => {});
    const timer = setInterval(async () => {
      try {
        const st = await mfengine.installState?.();
        if (!st) return;
        // On success the shell restarts the server and reloads this window, so
        // there's nothing more to do here but stop polling.
        if (st.state === "done") clearInterval(timer);
        else if (st.state === "failed") {
          clearInterval(timer);
          setBusy(false);
          setFailed(true);
        }
      } catch {
        /* transient — keep polling */
      }
    }, 1000);
  };

  return (
    <>
      <h3 className="set-section-title">Engine updates</h3>
      {info?.available ? (
        <>
          <p className="set-hint">
            A new engine (<strong>{info.latest}</strong>) is available — you’re on {info.current}.
            This updates just the engine (a small download), not the app; the server restarts and
            this window reconnects on its own.
          </p>
          <button type="button" className="test-btn" disabled={busy} onClick={update}>
            {busy ? "Updating…" : `Update engine to ${info.latest}`}
          </button>
          {failed && (
            <p className="error">
              The update didn’t finish. Try again, or check Settings → System logs.
            </p>
          )}
        </>
      ) : (
        <p className="set-hint">
          The engine is up to date{info?.current ? ` (${info.current})` : ""}.
        </p>
      )}
    </>
  );
}

/** Uninstall, but only inside the desktop shell — a plain browser can't remove
 * an engine it merely connects to. On Windows we point at Add/Remove Programs
 * (its customUnInstall clears the engine); on mac/Linux the shell has no OS
 * uninstall hook, so the app does the teardown itself. See electron/main.js
 * `app:uninstall`. */
function UninstallSection() {
  const shell = (window as unknown as { mfshell?: { platform?: string } }).mfshell;
  const mfapp = (window as unknown as { mfapp?: { uninstall?: () => Promise<unknown> } }).mfapp;
  if (!shell) return null; // web build / plain browser
  const isWindows = shell.platform === "win32";
  return (
    <>
      <h3 className="set-section-title">Uninstall</h3>
      {isWindows ? (
        <p className="set-hint">
          Uninstall MindFlock from <strong>Windows Settings → Apps</strong>. That removes the
          app and the engine inside WSL (worktrees, agent hooks, and the <code>mindflock</code>{" "}
          tool) together. Your history &amp; settings are kept.
        </p>
      ) : (
        <p className="set-hint">
          Removes MindFlock and its engine: stops the server, reverses its changes to your
          repos (worktrees + agent hooks), and removes the <code>mindflock</code> tool. Your
          history &amp; settings are kept unless you opt in when asked. MindFlock then quits.
        </p>
      )}
      <button type="button" className="test-btn" onClick={() => mfapp?.uninstall?.()}>
        {isWindows ? "Open Windows uninstall…" : "Uninstall MindFlock…"}
      </button>
    </>
  );
}
