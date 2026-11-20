/** Settings → Advanced (partial 115): engine + platform fields, Updates, and
 * the on-demand server restart. */

import { useCallback, useEffect, useRef, useState } from "react";
import { SettingField, useSettings } from "../useSettings";
import { useServerRestart } from "../useServerRestart";
import { api } from "../../../api/client";
import { toast } from "../../../lib/toast";
import { flashSaved } from "../savedCue";
import type { ScreenProps } from "../SettingsDialog";
import { SetInIntake } from "./SetInIntake";
import {
  autoUpdateHint,
  shellUpdateMode,
  showAutoUpdateSwitch,
  type ShellUpdateFlags,
} from "../../../lib/shellUpdate";
import {
  installPath,
  restartLanded,
  runFailText,
  UPDATE_MODES,
  updateAvailable,
  updateButtonLabel,
  updateModeOf,
  updateStateLine,
  vLabel,
  type UpdateCheck,
  type UpdateMode,
  type UpdateRun,
} from "../../../lib/updates";
import { clearUpdateRestart, expectUpdateRestart } from "../../../lib/updateRestart";

/** Whether this viewer is on Windows: a Windows browser (the UA says so even
 * when the server runs inside WSL), or the desktop shell on win32 — read off
 * the same preload bridge lib/shell reads, since it exports no platform getter. */
function onWindows(): boolean {
  if (typeof navigator !== "undefined" && /Windows/.test(navigator.userAgent || "")) return true;
  if (typeof window === "undefined") return false;
  return (window as unknown as { mfshell?: { platform?: string } }).mfshell?.platform === "win32";
}

export function Advanced(_: ScreenProps) {
  const s = useSettings();
  const { restarting, timedOut, restart } = useServerRestart();
  // Platform's two fields only mean something on Windows. Shown there, or
  // anywhere they already hold a value (a config carried over from a Windows
  // box must stay visible to be cleared) — and nowhere else, where a heading
  // that says "(Windows / WSL only)" was a section telling you to skip it.
  const showPlatform =
    onWindows() ||
    !!String(s.get("platform", "wsl_distro") ?? "").trim() ||
    !!String(s.get("platform", "wt_command") ?? "").trim();
  return (
    <>
      <h3 className="set-section-title">Engine</h3>
      {/* Ticket sessions in MindFlock (engine.enabled) and worktree vs clone
          (engine.mode) moved to Intake › Auto-start's "Ticket sessions" fold,
          with the rest of ticket setup. */}
      <SetInIntake
        id="engine-ticket-sessions"
        label="Ticket sessions in MindFlock"
        hint="Whether tickets become MindFlock sessions, and how they copy the repo."
        selector="#ik-engine-sessions-row"
      />
      <UpdatesBlock />
      {showPlatform && (
        <>
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
        </>
      )}
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

/** Updates: ONE block, the same in a browser and in the desktop app
 * (docs/web-ui.md "Updates"). The choice (Automatic / Ask me / Off) is the
 * server's `updates.mode` (`POST /api/update/mode`); the version, what is
 * available and the one line of state come from `GET /api/update/check`.
 *
 * Who installs when the button is pressed (lib/updates installPath): the
 * desktop shell where it manages this engine (`window.mfengine.install`, so
 * the app updates too, and the shell reloads this window), else the server
 * itself (`/api/update/start`) — a development checkout included, with the
 * same button and progress as every other install. An older server sends no
 * `mode` / `managed_by_shell` / `dev`: then no choice is drawn (the desktop
 * app keeps its own automatic-updates switch) and the split is today's. */

/** How long to wait for the server to come back on a finished install. */
const NEW_BUILD_WAIT_MS = 180000;

/** "<what failed>: <reason>." — the reason a rejected call carried, or just
 * "<what failed>." when it carried none. */
export function failSentence(what: string, err: unknown): string {
  const why = String((err as { message?: unknown } | null)?.message ?? (typeof err === "string" ? err : "")).trim();
  return why ? what + ": " + why.replace(/\.$/, "") + "." : what + ".";
}

/** What `mfengine.updateInfo()` answers (electron/main.js
 * `engine:update-info`). `checked` comes from shells that report whether the
 * check succeeded. */
type EngineInfo = ShellUpdateFlags & {
  available?: boolean;
  checked?: boolean;
  current?: string;
  latest?: string;
  /** The app is behind too: Update does both halves. */
  appUpdate?: boolean;
  /** The engine has run ahead of this app (one line, nothing to click). */
  skew?: string;
};
type EngineRun = { state?: string; step?: string; message?: string; code?: number | null; lines?: string[] };
type ShellEngine = {
  updateInfo?: () => Promise<EngineInfo>;
  install?: () => Promise<unknown>;
  installState?: () => Promise<EngineRun>;
};
type ShellUpdate = {
  setAuto?: (on: boolean) => Promise<{ autoUpdate?: boolean }>;
};

function UpdatesBlock() {
  const w = window as unknown as { mfengine?: ShellEngine; mfupdate?: ShellUpdate };
  const mfengine = w.mfengine;
  const mfupdate = w.mfupdate;
  const [info, setInfo] = useState<UpdateCheck | null>(null);
  const [shellInfo, setShellInfo] = useState<EngineInfo | null>(null);
  const [checking, setChecking] = useState(false);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [lines, setLines] = useState<string[]>([]);
  const [run, setRun] = useState<EngineRun | null>(null);
  /** The install finished: waiting for the server to answer on it. */
  const [waiting, setWaiting] = useState(false);
  const [waitTimedOut, setWaitTimedOut] = useState(false);
  const timer = useRef<ReturnType<typeof setInterval> | null>(null);

  const check = useCallback(
    async (refresh: boolean) => {
      const shellAsk = mfengine?.updateInfo
        ? mfengine
            .updateInfo()
            .then(setShellInfo)
            .catch(() => {})
        : Promise.resolve();
      try {
        setInfo(await api<UpdateCheck>("/api/update/check" + (refresh ? "?refresh=1" : "")));
      } catch (err) {
        setError((err as Error).message);
      }
      await shellAsk;
    },
    [mfengine]
  );

  useEffect(() => {
    void check(false);
    return () => {
      if (timer.current) clearInterval(timer.current);
    };
  }, [check]);

  const checkNow = () => {
    if (checking) return;
    setChecking(true);
    setError("");
    void check(true).finally(() => setChecking(false));
  };

  // Installed but not running yet — on this screen's first look, or after
  // the server's install below: the server restarts itself onto it, so wait
  // until it answers without `restart_pending`, then reload onto the new
  // bundle. Gaps while it restarts are expected.
  const pending = !!info?.restart_pending;
  useEffect(() => {
    if (!pending && !waiting) return;
    const t0 = Date.now();
    const t = setInterval(async () => {
      if (Date.now() - t0 > NEW_BUILD_WAIT_MS) {
        clearInterval(t);
        setWaitTimedOut(true);
        return;
      }
      try {
        const c = await api<UpdateCheck>("/api/update/check");
        if (restartLanded(c)) {
          clearInterval(t);
          window.location.reload();
        }
      } catch {
        /* restarting */
      }
    }, 2000);
    return () => clearInterval(t);
  }, [pending, waiting]);

  const path = installPath({
    desktop: !!mfengine?.install,
    shellSource: shellUpdateMode(shellInfo) === "source",
    managedByShell: info?.managed_by_shell,
  });
  const mode = updateModeOf(info);

  // --- the choice --------------------------------------------------------- //
  const setMode = async (next: UpdateMode, el: Element | null) => {
    const prev = info?.mode;
    setInfo((i) => ({ ...(i || {}), mode: next, mode_set: true }));
    try {
      // The server holds the mode. The desktop app keeps its own copy (it
      // drives a packaged app's updates), told only once the server has the
      // new mode: its setAuto(false) posts "ask" itself while it still thinks
      // the mode is auto, so told first (or at once) it could turn "off"
      // into "ask".
      const r = await api<{ mode?: string }>("/api/update/mode", { json: { mode: next } });
      setInfo((i) => ({ ...(i || {}), mode: r?.mode || next }));
      await mfupdate?.setAuto?.(next === "auto").catch(() => {});
      if (!(el && flashSaved(el))) toast("Saved");
      void check(false); // auto.why follows the mode
    } catch (err) {
      setInfo((i) => ({ ...(i || {}), mode: prev }));
      toast(failSentence("Couldn't change automatic updates", err));
    }
  };

  // An older server and the desktop app: the shell's own switch, as before.
  const legacyAuto = !mode && path === "shell" && showAutoUpdateSwitch(shellInfo);
  const setLegacyAuto = (on: boolean) => {
    setShellInfo((i) => ({ ...(i || {}), autoUpdate: on }));
    mfupdate
      ?.setAuto?.(on)
      .then((r) => setShellInfo((i) => ({ ...(i || {}), autoUpdate: r?.autoUpdate !== false })))
      .catch((err: unknown) => {
        // The switch must not claim a change that didn't happen.
        setShellInfo((i) => ({ ...(i || {}), autoUpdate: !on }));
        toast(failSentence("Couldn't change automatic updates", err));
      });
  };

  // --- installing ------------------------------------------------------------ //
  const viaShell = () => {
    if (busy || !mfengine?.install) return;
    setBusy(true);
    setRun(null);
    expectUpdateRestart(shellInfo?.latest || info?.latest || "");
    mfengine.install().catch((err: unknown) => {
      // The run never started: stop the poll and say so.
      if (timer.current) clearInterval(timer.current);
      timer.current = null;
      setBusy(false);
      clearUpdateRestart();
      setRun({ state: "failed", message: failSentence("The update didn't start", err) });
    });
    if (timer.current) clearInterval(timer.current);
    timer.current = setInterval(async () => {
      try {
        const st = await mfengine.installState?.();
        if (!st) return;
        setRun(st);
        // The shell owns the whole run: once it reports done the new server
        // is answering (and this window is reloaded by the shell itself).
        if (st.state === "done" || st.state === "failed") {
          if (timer.current) clearInterval(timer.current);
          timer.current = null;
          setBusy(false);
          if (st.state === "failed") clearUpdateRestart();
          if (st.state === "done") void check(false);
        }
      } catch {
        /* transient — keep polling */
      }
    }, 1000);
  };

  const viaServer = async () => {
    setBusy(true);
    setError("");
    setLines([]);
    try {
      await api("/api/update/start", { method: "POST", json: {} });
    } catch (err) {
      setBusy(false);
      setError(failSentence("The update didn't start", err));
      return;
    }
    expectUpdateRestart(info?.latest || "");
    if (timer.current) clearInterval(timer.current);
    timer.current = setInterval(async () => {
      let st: UpdateRun;
      try {
        st = await api<UpdateRun>("/api/update/state");
      } catch {
        // The install replaces what this server runs out of, so a gap here is
        // expected. Keep polling; the restart below ends this loop.
        return;
      }
      setLines(st.log || []);
      if (st.restarting || st.state === "done") {
        if (timer.current) clearInterval(timer.current);
        timer.current = null;
        // The server restarts itself onto the install: wait for the new
        // build to answer and reload onto its bundle.
        setWaiting(true);
      } else if (st.state === "failed" || st.state === "rolled_back") {
        if (timer.current) clearInterval(timer.current);
        timer.current = null;
        setBusy(false);
        clearUpdateRestart();
        setError(runFailText(st));
        void check(false);
      }
    }, 2000);
  };

  // --- what to say --------------------------------------------------------- //
  const shellDrives = path === "shell" && !!shellInfo;
  const current = vLabel(info?.current || shellInfo?.current) || "unknown";
  const available = shellDrives ? !!shellInfo?.available : updateAvailable(info);
  const latest = vLabel(shellDrives ? shellInfo?.latest || info?.latest : info?.latest);
  const checked = shellDrives ? shellInfo?.checked !== false : !!info?.checked || !!info?.dev;
  const dev = info?.dev || null;
  const installing = info?.state === "started";
  const last = info?.last || {};
  const stateLine = updateStateLine(info) || (dev ? "" : String(info?.blocked || "").trim());
  const cantUpdate = dev ? dev.can_update === false : !!info?.blocked;
  const failed = run?.state === "failed";
  const lastNote =
    error || pending || waiting
      ? ""
      : info?.state === "rolled_back"
        ? vLabel(last.version || "?") + " didn’t start, so " + vLabel(last.from_version || "?") +
          " was put back (Settings → System logs has the installer output)."
        : info?.state === "failed" && last.error === "interrupted"
          ? "The last update was interrupted. Try again."
          : info?.state === "failed"
            ? "The last update failed. Settings → System logs has the installer output."
            : "";
  const buttonLabel = shellDrives ? (latest ? "Update to " + latest : "Update") : updateButtonLabel(info);
  const showButton = available && !pending && !waiting && !installing;

  return (
    <>
      <h3 className="set-section-title" id="set-updates">
        Updates
      </h3>
      {mode && (
        <div className="set-row" id="upd-mode-row">
          <span className="set-label" id="upd-mode-label">
            Install updates
          </span>
          <div className="upd-mode-choices" role="radiogroup" aria-labelledby="upd-mode-label">
            {UPDATE_MODES.map((m) => (
              <label className="upd-mode-choice" key={m.value} title={m.hint}>
                <input
                  type="radio"
                  name="upd-mode"
                  value={m.value}
                  checked={mode === m.value}
                  onChange={(e) => void setMode(m.value, e.currentTarget)}
                />
                <span>{m.label}</span>
              </label>
            ))}
          </div>
          <span className="set-hint">{UPDATE_MODES.find((m) => m.value === mode)?.hint}</span>
        </div>
      )}
      {legacyAuto && (
        <>
          <div className="set-row set-switch-row">
            <span className="set-label">Install updates automatically</span>
            <label className="ca-switch">
              <input
                type="checkbox"
                id="upd-auto"
                checked={shellInfo?.autoUpdate !== false}
                disabled={!mfupdate?.setAuto}
                onChange={(e) => setLegacyAuto(e.target.checked)}
              />
              <span className="ca-slider" />
            </label>
          </div>
          <p className="set-hint">{autoUpdateHint(shellInfo?.autoUpdate !== false)}</p>
        </>
      )}
      {(info || shellInfo) && (
        <p className="set-hint" id="upd-version">
          This is <strong>{current}</strong>
          {dev?.branch ? <> (a development checkout on {dev.branch})</> : null}.
          {available && latest ? (
            <>
              {" "}
              <strong>{latest}</strong> is available
              {info?.release_url && !dev ? (
                <>
                  {" "}
                  (
                  <a href={info.release_url} target="_blank" rel="noreferrer">
                    release notes
                  </a>
                  )
                </>
              ) : null}
              .
            </>
          ) : available && dev?.behind ? (
            <>
              {" "}
              {dev.behind} new commit{dev.behind === 1 ? "" : "s"} on {dev.default_branch || dev.branch}.
            </>
          ) : info && checked && !available && !cantUpdate ? (
            <> It’s the newest version.</>
          ) : info && !checked ? (
            <> Couldn’t check for a newer version just now.</>
          ) : null}
          {shellDrives && available && shellInfo?.appUpdate ? " The app is behind too: Update does both." : ""}
        </p>
      )}
      {shellInfo?.skew && !available ? (
        <p className="set-hint" id="upd-skew">
          {shellInfo.skew}
        </p>
      ) : null}
      {stateLine && (
        <p className="set-hint" id="upd-state">
          {stateLine}
        </p>
      )}
      {pending || waiting ? (
        <p className="set-hint" id="upd-restarting">
          Installed{last.version ? " " + vLabel(last.version) : ""}. Restarting onto it; this window
          reloads on its own once the server answers again.
        </p>
      ) : installing ? (
        <p className="set-hint" id="upd-installing">
          Installing{last.version ? " " + vLabel(last.version) : " the update"}…
        </p>
      ) : null}
      {lastNote && (
        <p className="set-hint" id="upd-last">
          {lastNote}
        </p>
      )}
      <div className="upd-btn-row">
        {showButton && (
          <button
            type="button"
            className="test-btn"
            id="upd-install"
            disabled={busy}
            onClick={() => (path === "shell" ? viaShell() : void viaServer())}
          >
            {busy ? "Updating…" : failed ? "Retry" : buttonLabel}
          </button>
        )}
        <button type="button" className="test-btn" disabled={checking || busy || waiting} onClick={checkNow}>
          {checking ? "Checking…" : "Check now"}
        </button>
      </div>
      {run?.message ? (
        <p className={failed ? "error" : "set-hint"} id="upd-run">
          {run.message}
        </p>
      ) : failed ? (
        <p className="error">The update didn’t finish. Try again, or check Settings → System logs.</p>
      ) : null}
      {error && <p className="error">{error}</p>}
      {waitTimedOut && (
        <p className="error">
          The update finished, but the server didn’t come back on it. Check Settings → System logs,
          or run <code>mindflock restart</code> on that machine.
        </p>
      )}
      {(lines.length > 0 || (run?.lines || []).length > 0) && (
        <details className="upd-fold">
          <summary>Installer output</summary>
          <pre className="upd-log">{(lines.length ? lines : run?.lines || []).join("\n")}</pre>
        </details>
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
